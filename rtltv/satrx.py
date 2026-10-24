"""Потоки приёма режима «Спутники»: демо-генераторы, поток DSP (LRPT/LRIT), декодирование файлов,
шумоподавитель звука для прослушивания спутников."""
import collections
import os
import queue
import threading
import time

import numpy as np

from . import elektro, lrpt
from .radio import RadioDemod
from .source import R828D_GAINS


class SynthSource(threading.Thread):
    """Демо-генератор с реальным темпом выдачи.
    kind="lrpt" — «Метеор» (OQPSK 72k, NRZ-M, доплер ±1,5 кГц); kind="lrit" — «Электро-Л» (BPSK 294k)."""

    FS = {"lrpt": 72000 * 16, "lrit": elektro.SYMBOL_RATE * 8}

    def __init__(self, sink, center, sig_freq, kind="lrpt"):
        super().__init__(daemon=True)
        self.sink = sink
        self.kind = kind
        self.fs = self.FS[kind]
        self.center = center
        self.sig = sig_freq
        self.running = True
        self.gain = 300

    def command(self, name, value=None):
        if name == "freq":
            self.center = float(value)

    def stop(self):
        self.running = False

    def _gen_lrpt(self):
        mod = lrpt.Modulator(16, True, True)
        big = {64 + k: lrpt.synth_image(1600, k) for k in range(3)}
        line = seq = cnt = 0
        while True:
            r0 = (line * 8) % (big[64].shape[0] - 16)
            imgs = {a: im[r0:r0 + 16] for a, im in big.items()}
            pk = lrpt.synth_packets(imgs, line0=line, seq0=seq)
            line += 2
            seq += len(pk)
            vc = lrpt.packets_to_vcdus(pk, count0=cnt)
            cnt += len(vc)
            yield mod.modulate(lrpt.vcdus_to_cadus(vc))

    def _gen_lrit(self):
        x, _fs, _ = elektro.synth_signal(nseg=4, size=480)
        while True:
            yield x

    def run(self):
        self.sink("opened", {"label": "Демо-генератор " + ("LRPT" if self.kind == "lrpt" else "LRIT"),
                             "tuner": "эмуляция", "gains": R828D_GAINS})
        gen = self._gen_lrpt() if self.kind == "lrpt" else self._gen_lrit()
        rng = np.random.default_rng()
        nco = t_sig = 0.0
        t_next = time.perf_counter()
        pending = np.zeros(0, np.complex64)
        while self.running:
            if pending.size < 131072:
                x = next(gen)
                df = (self.sig - self.center) + (1500.0 * np.sin(t_sig / 40.0) if self.kind == "lrpt" else 800.0)
                w = 2 * np.pi * df / self.fs
                ph = nco + w * np.arange(x.size)
                nco = float((nco + w * x.size) % (2 * np.pi))
                t_sig += x.size / self.fs
                y = 0.25 * x * np.exp(1j * ph).astype(np.complex64)
                y += (0.05 * (rng.standard_normal(y.size) + 1j * rng.standard_normal(y.size))).astype(np.complex64)
                pending = np.concatenate([pending, y.astype(np.complex64)])
                continue
            blk, pending = pending[:131072], pending[131072:]
            t_next += blk.size / self.fs
            dt = t_next - time.perf_counter()
            if dt > 0:
                time.sleep(dt)
            elif dt < -1.0:
                t_next = time.perf_counter()
            self.sink("stream", self.center, blk)


class SpecPacer:
    """Кадры спектра для интерфейса с равномерной выдачей.

    Блок IQ длиной T (50–150 мс) делится на k ≈ T·fps частей; каждая даёт свой кадр спектра со своим
    временем выдачи. Иначе все кадры блока приходят разом и картинка «дёргается» с частотой блоков.
    push() — в потоке приёма, take() — в потоке интерфейса (только последний наступивший кадр)."""

    def __init__(self, fps=50):
        # кадров чуть больше, чем частота отрисовки (до 30/с): таймер виджета всегда находит свежий кадр
        self.dt = 1.0 / fps
        self.q = collections.deque(maxlen=64)
        self.n = 0
        self.last_rel = 0.0
        self.snap = None
        self._win = {}

    def push(self, iq, center, fs, nfft, now=None):
        now = time.time() if now is None else now
        n = int(nfft)
        T = iq.size / fs
        k = max(1, min(12, int(round(T / self.dt)), iq.size // n))
        part = iq.size // k
        win = self._win.get(n)
        if win is None:
            win = self._win[n] = np.hanning(n).astype(np.float32)
        freqs = center + np.fft.fftshift(np.fft.fftfreq(n, 1 / fs))
        # если очередь отстала от реального времени (пачка блоков после старта) — начинаем с «сейчас»
        t = min(max(now, self.last_rel), now + T)
        step = T / k
        for j in range(k):
            seg = iq[j * part: j * part + n]
            if seg.size < n:
                break
            X = np.fft.fft(seg * win)
            psd = 10 * np.log10(np.fft.fftshift(X.real ** 2 + X.imag ** 2) / (n * n) + 1e-12).astype(np.float32)
            self.n += 1
            self.q.append((t, self.n, freqs, psd))
            t += step
        self.last_rel = t

    def take(self, now=None):
        now = time.time() if now is None else now
        q = self.q
        while q and q[0][0] <= now:
            _t, n, f, p = q.popleft()
            self.snap = (n, f, p)
        return self.snap


class RxWorker(threading.Thread):
    """Поток DSP: доплер, приёмник (LrptReceiver/LritReceiver), спектр, запись IQ. Интерфейс читает self.snap."""

    def __init__(self, fs, center, sig_freq, receiver, orbit=None, home=None, doppler=True, record_path=None,
                 clock=None):
        super().__init__(daemon=True)
        self.q = queue.Queue(maxsize=64)
        self.fs = fs
        self.center = center
        self.sig = sig_freq
        self.orbit = orbit
        self.home = home
        self.doppler_on = doppler
        self.rx = receiver
        self.running = True
        self.snap = {}
        self.dropped = 0
        self.rec = None
        self.rec_path = record_path
        self.adc_std = None
        self.adc_clip = None
        self.t_start = time.time()
        self.busy = 0.0
        self._spec_t = 0.0
        self._snap_t = 0.0
        self._dopp_t = 0.0
        self.doppler_hz = 0.0
        self.lock = threading.Lock()
        self.clock = clock          # время для доплера (по умолчанию — настоящее; в тестах — время записи)
        self.nfft = 4096
        self.pacer = SpecPacer()
        self.cons_snap = None
        self.preview_key = None     # что показывать на вкладке снимков (строка комбинации LRPT)
        self.preview = None         # dict(ver, images, arr, key) — готовится здесь, не в потоке интерфейса
        self._prev_t = 0.0
        self._prev_ver = -1

    def put(self, iq):
        try:
            self.q.put_nowait(iq)
        except queue.Full:
            self.dropped += 1

    def stop(self):
        self.running = False

    def run(self):
        if self.rec_path:
            try:
                os.makedirs(os.path.dirname(self.rec_path), exist_ok=True)
                self.rec = open(self.rec_path, "wb")
            except OSError:
                self.rec = None
        try:
            while self.running:
                try:
                    iq = self.q.get(timeout=0.2)
                except queue.Empty:
                    continue
                t0 = time.perf_counter()
                self._process(iq)
                dt = time.perf_counter() - t0
                self.busy = 0.9 * self.busy + 0.1 * dt / (iq.size / self.fs)
        finally:
            if self.rec:
                self.rec.close()
            with self.lock:
                self.rx.finish()

    def _process(self, iq):
        now = time.time()
        if self.rec:
            u8 = np.empty(iq.size * 2, np.uint8)
            u8[0::2] = np.clip(np.rint(iq.real * 128.0 + 127.4), 0, 255)
            u8[1::2] = np.clip(np.rint(iq.imag * 128.0 + 127.4), 0, 255)
            self.rec.write(u8.tobytes())
        tdop = self.clock() if self.clock is not None else now
        if abs(tdop - self._dopp_t) > 0.5:
            self._dopp_t = tdop
            d = 0.0
            if self.doppler_on and self.orbit is not None:
                try:
                    d = float(self.orbit.doppler(tdop, self.sig, *self.home))
                except (ValueError, FloatingPointError):
                    d = 0.0
            self.doppler_hz = d
            self.rx.set_offset(self.sig - self.center + d)
        # уровень АЦП: разброс отсчётов (в единицах 8-битного АЦП) и доля упёршихся в предел — перегрузка/помеха
        k = iq[::8]
        sd = float(np.sqrt(0.5 * np.mean(np.abs(k - k.mean()) ** 2))) * 128.0
        clip = float(np.mean((np.abs(k.real) > 0.985) | (np.abs(k.imag) > 0.985))) * 100.0
        self.adc_std = sd if self.adc_std is None else 0.8 * self.adc_std + 0.2 * sd
        self.adc_clip = clip if self.adc_clip is None else 0.8 * self.adc_clip + 0.2 * clip
        with self.lock:
            self.rx.feed(iq)
        self.pacer.push(iq, self.center, self.fs, int(self.nfft), now)
        self.cons_snap = self.rx.last_cons[-600:]          # созвездие — каждый блок, без копирования

        if now - self._snap_t > 0.2:
            self._snap_t = now
            st = self.rx.stats()
            st.update(adc_std=self.adc_std or 0.0, adc_clip=self.adc_clip or 0.0)
            st.update(doppler=self.doppler_hz, dropped=self.dropped, busy=self.busy,
                      elapsed=now - self.t_start, imgver=self.version())
            self.snap = dict(stats=st)
        if self.preview_key is not None and hasattr(self.rx, "imager") and now - self._prev_t > 3.0:
            ver = self.rx.imager.version
            if ver != self._prev_ver or (self.preview and self.preview["key"] != self.preview_key):
                self._prev_t = now
                self._prev_ver = ver
                imgs = self.rx.imager.all_images()
                arr, used = lrpt.preview(imgs, self.preview_key, True)
                self.preview = dict(ver=ver, images=imgs, arr=arr, key=self.preview_key, used=used)

    def take_spectrum(self, now):
        """Последний кадр спектра, время выдачи которого наступило (вызывается из потока интерфейса)."""
        return self.pacer.take(now)

    def version(self):
        im = getattr(self.rx, "imager", None) or getattr(self.rx, "asm", None)
        return im.version if im is not None else 0

    def images(self):
        with self.lock:
            if hasattr(self.rx, "imager"):
                return self.rx.imager.all_images()
            return None


class IqReceiver:
    """Приёмник без декодера: перенос на частоту спутника, фильтр, «созвездие» (точки IQ) и уровень.
    Используется для записи IQ и наблюдения спектра спутников, для которых декодера нет."""

    def __init__(self, fs, bw=40e3):
        from scipy import signal as sps
        self.fs = float(fs)
        self.bw = float(bw)
        self.dec = max(1, int(self.fs // (4 * bw)))
        self.h = sps.firwin(129, bw / 2, fs=self.fs).astype(np.float32)
        self.offset = 0.0
        self._nco = 0.0
        self.last_cons = np.zeros((0, 2), np.float32)
        self.level_db = -120.0
        self.noise_db = -120.0
        self.nblocks = 0

    def set_offset(self, hz):
        self.offset = float(hz)

    def feed(self, iq, want_cons=True):
        from scipy import signal as sps
        n = iq.size
        w = -2 * np.pi * self.offset / self.fs
        x = iq * np.exp(1j * (self._nco + w * np.arange(n))).astype(np.complex64)
        self._nco = float((self._nco + w * n) % (2 * np.pi))
        y = sps.oaconvolve(x[-min(n, 65536):], self.h, mode="valid")[:: self.dec]
        if y.size:
            p = float(np.mean(np.abs(y) ** 2)) + 1e-20
            self.level_db = 10 * np.log10(p)
            # шум: медиана спектра всего блока (сигнал узкий, медиана — уровень шума), пересчёт на полосу фильтра
            seg = iq[:8192]
            pn = float(np.median(np.abs(np.fft.fft(seg)) ** 2)) / seg.size / np.log(2)
            self.noise_db = 10 * np.log10(pn * self.bw / self.fs + 1e-20)
            z = y[-800:] / np.sqrt(p)
            self.last_cons = np.stack([z.real, z.imag], axis=1).astype(np.float32) * 0.7
        self.nblocks += 1

    def finish(self):
        pass

    def stats(self):
        return dict(carrier=0.0, residual_hz=self.offset, sync=False, frames=0, frames_ok=0, rs_pct=0.0,
                    rs_fixed=0, snr_db=self.level_db - self.noise_db, nrzm=False, hyp=None,
                    level_db=self.level_db)


class FileDecoder(threading.Thread):
    """Декодирование записи в фоне: func(path, progress=, cancel=, **kw) → приёмник."""

    def __init__(self, func, path, **kw):
        super().__init__(daemon=True)
        self.func, self.path, self.kw = func, path, kw
        self.progress = 0.0
        self.rx = None
        self.error = None
        self.cancelled = False
        self.stats = {}

    def run(self):
        def prog(fr, rx):
            self.progress = fr
            self.stats = rx.stats()
        try:
            self.rx = self.func(self.path, progress=prog, cancel=lambda: self.cancelled, **self.kw)
            self.stats = self.rx.stats()
        except Exception as e:  # noqa: BLE001 — показываем пользователю
            self.error = str(e)


# ---------------------------------------------------------------- звук

class SpectralNR:
    """Потоковый шумоподавитель (спектральное вычитание, окно 512, шаг 256, sqrt-Ханн).
    Шум оценивается по минимумам спектра; выход отстаёт от входа на буфер до 512 отсчётов."""

    def __init__(self, n=512, strength=1.6, floor=0.12):
        self.n, self.h = n, n // 2
        self.win = np.sqrt(np.hanning(n + 1)[:-1]).astype(np.float32)
        self.inbuf = np.zeros(0, np.float32)
        self.ola = np.zeros(n, np.float32)
        self.noise = None
        self.g = None
        self.strength, self.floor = strength, floor

    def process(self, x):
        self.inbuf = np.concatenate([self.inbuf, np.asarray(x, np.float32)])
        outs = []
        n, h = self.n, self.h
        while self.inbuf.size >= n:
            X = np.fft.rfft(self.inbuf[:n] * self.win)
            P = (X.real ** 2 + X.imag ** 2).astype(np.float32)
            if self.noise is None:
                self.ps = P.copy()
                self.cur_min = P.copy()
                self.mins = []
                self.nfr = 0
                self.noise = P.copy()
                self.g = np.ones_like(P)
            # «минимальная статистика»: минимум сглаженного спектра за ~1,5 с (6 окон по 0,25 с)
            self.ps = 0.8 * self.ps + 0.2 * P
            self.cur_min = np.minimum(self.cur_min, self.ps)
            self.nfr += 1
            if self.nfr % 47 == 0:
                self.mins = (self.mins + [self.cur_min])[-6:]
                self.cur_min = self.ps.copy()
                self.noise = 1.5 * np.min(np.stack(self.mins), axis=0)
            g = np.maximum(1.0 - self.strength * self.noise / (P + 1e-12), self.floor)
            self.g = 0.5 * self.g + 0.5 * g
            y = np.fft.irfft(X * self.g, n).astype(np.float32) * self.win
            self.ola += y
            outs.append(self.ola[:h].copy())
            self.ola = np.concatenate([self.ola[h:], np.zeros(h, np.float32)])
            self.inbuf = self.inbuf[h:]
        return np.concatenate(outs) if outs else np.zeros(0, np.float32)


class NrDemod(RadioDemod):
    """RadioDemod + шумоподавитель звука (для FM/SSB со спутников)."""

    def __init__(self, fs, mode="NFM", bandwidth=None):
        super().__init__(fs, mode, bandwidth)
        self.nr_on = False
        self._nr = SpectralNR()

    def process(self, iq):
        out = super().process(iq)
        if self.nr_on and out.ndim == 1 and out.size:
            out = self._nr.process(out) * 1.4
        return out.astype(np.float32)
