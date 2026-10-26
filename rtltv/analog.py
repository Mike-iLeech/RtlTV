"""Аналоговое ТВ: AM-VSB видео с негативной модуляцией (огибающая + синхронизация) и FM-звук."""
import math

import numpy as np
from scipy import signal


def _box_peak(prof, w):
    """Кольцевая свёртка профиля с прямоугольником ширины w: позиция максимума и сама свёртка."""
    n = prof.size
    ext = np.concatenate((prof, prof[:w - 1]))
    cs = np.concatenate(([0.0], np.cumsum(ext, dtype=np.float64)))
    box = (cs[w:w + n] - cs[:n]) / w
    return int(np.argmax(box)), box


class AnalogVideoDecoder:
    """Огибающая -> строчная/кадровая синхронизация -> кадр яркости (0 = чёрный, 1 = белый).

    Ширина полосы недорогого приёмника (у RTL-SDR до ~3 МГц) меньше полосы видеосигнала (6 МГц), поэтому картинка
    чёрно-белая и примерно 130 точек по горизонтали: цветовая поднесущая (4,4 МГц) в окно не попадает.
    """

    OUT_W = 480

    def __init__(self, fs, std):
        self.fs = float(fs)
        self.std = std
        self.nominal = self.fs / std.line_freq
        self.field_swap = False
        self.reset()

    def reset(self):
        self.period = self.nominal
        self.buf = np.zeros(0, np.float32)
        self.pos = 0.0
        self.hlock = 0.0
        self.vlock = False
        self.vscore = None

    def feed(self, iq):
        e = np.abs(iq).astype(np.float32)
        self.buf = np.concatenate((self.buf, e)) if self.buf.size else e
        out = []
        L = self.std.lines
        while self.buf.size >= self.pos + (L + 3) * self.period + 2:
            out.append(self._frame())
            drop = int(self.pos) - 8
            if drop > 0:
                self.buf = self.buf[drop:]
                self.pos -= drop
        return out

    def _sample(self, idx):
        i0 = np.floor(idx).astype(np.int64)
        fr = (idx - i0).astype(np.float32)
        np.clip(i0, 0, self.buf.size - 2, out=i0)
        b = self.buf
        return b[i0] * (1.0 - fr) + b[i0 + 1] * fr

    def _frame(self):
        std, fs, P, L = self.std, self.fs, self.period, self.std.lines
        Np = int(round(P))
        colstep = P / Np
        starts = self.pos + np.arange(L) * P
        S = self._sample(starts[:, None] + (np.arange(Np) * colstep)[None, :])

        tip = max(float(np.percentile(S, 99.0)), 1e-6)
        sync = (S > 0.875 * tip).astype(np.float32)

        # строчная синхронизация: где по всем строкам чаще всего стоит импульс 4,7 мкс
        w = max(2, int(round(std.hsync_us * 1e-6 * fs / colstep)))
        k, box = _box_peak(sync.mean(0), w)
        q = float(box[k] - np.median(box))
        self.hlock = q
        err_cols = k if k <= Np // 2 else k - Np
        err = err_cols * colstep

        if q > 0.4:
            half = L // 2
            kt, _ = _box_peak(sync[:half].mean(0), w)
            kb, _ = _box_peak(sync[half:].mean(0), w)
            d = (kb - kt + Np // 2) % Np - Np // 2
            slope = d * colstep / half
            self.period += 0.5 * slope + 0.02 * err / L
            self.period = min(max(self.period, self.nominal * 0.998), self.nominal * 1.002)

        # строки заново по найденным фронтам строчных импульсов
        line0 = starts + err
        Sal = self._sample(line0[:, None] + (np.arange(Np) * colstep)[None, :])
        al = Sal > 0.875 * tip

        # кадровая синхронизация: шаблон начала поля 1 (две строки широких импульсов,
        # перед ними уравнивающие) и через полкадра — строка «уравнивающий | широкий» поля 2
        hN = Np // 2
        a0, a1 = int(0.25 * hN), int(0.8 * hN)
        m1 = al[:, a0:a1].mean(1)
        m2 = al[:, hN + a0:hN + a1].mean(1)
        F = L // 2
        score = (m1 + m2 + np.roll(m1, -1) + np.roll(m2, -1) + (1 - np.roll(m2, 1))
                 + np.roll(m2, -F) + (1 - np.roll(m1, -F)))
        # «маховик»: оценка копится по кадрам, кадр сдвигаем, только если другая строка явно лучше
        if self.vscore is None or self.vscore.size != L:
            self.vscore = np.zeros(L)
        self.vscore = 0.6 * self.vscore + 0.4 * score if q > 0.3 else 0.9 * self.vscore
        r = int(np.argmax(self.vscore))
        shift = None
        if r != 0 and self.vscore[r] > 4.8 and self.vscore[r] > self.vscore[0] + 0.6:
            shift = r
            self.vscore = np.roll(self.vscore, -r)
        elif self.vscore[0] > 4.3:
            shift = 0

        ra = np.arange(std.vis_count)
        r1, r2 = std.vis1 + ra, std.vis2 + ra
        bp0 = int(5.8e-6 * fs / colstep)
        bp1 = max(bp0 + 1, int(9.8e-6 * fs / colstep))
        normal = Sal[r1]
        tipv = float(normal[:, 1:max(2, w - 1)].mean())
        porch = normal[:, bp0:bp1]
        blank = float(porch.mean())
        noise = float(porch.std())
        if tipv - blank < 0.08 * tipv:
            blank = 0.75 * tipv
        span = 2.5 * (tipv - blank)

        W = self.OUT_W
        cols = (std.active_start_us + np.arange(W) * (std.active_us / W)) * 1e-6 * fs
        f1 = (blank - self._sample(line0[r1][:, None] + cols[None, :])) / span
        f2 = (blank - self._sample(line0[r2][:, None] + cols[None, :])) / span
        img = np.empty((2 * std.vis_count, W), np.float32)
        if self.field_swap:
            f1, f2 = f2, f1
        img[0::2] = f1
        img[1::2] = f2

        self.pos += L * P + (0.3 if q > 0.5 else 0.6) * err
        if shift is not None and shift != 0:
            self.pos += shift * P
            self.vlock = False
        else:
            self.vlock = shift == 0

        snr = 20 * math.log10(max(tipv - blank, 1e-6) / max(noise, 1e-6))
        return {
            "image": img,
            "hlock": q,
            "vlock": self.vlock,
            "level_db": 20 * math.log10(tip),
            "snr_db": snr if q > 0.3 else 0.0,
        }


class FmSoundDecoder:
    """FM-звук аналогового ТВ (девиация ±50 кГц, предыскажения 50 мкс) -> 48 кГц моно."""

    OUT_RATE = 48000

    def __init__(self, fs, carrier_offset, deemph=50e-6):
        fs = float(fs)
        self.w = -2 * math.pi * carrier_offset / fs
        self.ph = 0.0
        self.d1 = max(1, int(fs // 200e3))
        self.fs1 = fs / self.d1
        self.h1 = signal.firwin(8 * self.d1 + 1, 110e3, fs=fs)
        self.z1 = np.zeros(self.h1.size - 1, np.complex128)
        self.p1 = 0
        self.prev = 1 + 0j
        self.d2 = max(1, int(self.fs1 // 40e3))
        self.fs2 = self.fs1 / self.d2
        self.h2 = signal.firwin(6 * self.d2 + 1, 15e3, fs=self.fs1)
        self.z2 = np.zeros(self.h2.size - 1)
        self.p2 = 0
        a = 1 - math.exp(-1 / (self.fs2 * deemph))
        self.de_b, self.de_a = [a], [1, a - 1]
        self.zde = np.zeros(1)
        self.dc = 0.0
        self.ratio = self.fs2 / self.OUT_RATE
        self.t = 0.0
        self.last = 0.0

    def process(self, iq):
        n = iq.size
        mix = np.exp(1j * (self.ph + self.w * np.arange(n)))
        self.ph = (self.ph + self.w * n) % (2 * math.pi)
        y, self.z1 = signal.lfilter(self.h1, 1, iq * mix, zi=self.z1)
        y = y[self.p1::self.d1]
        self.p1 = (self.p1 - n) % self.d1
        if y.size == 0:
            return np.zeros(0, np.float32)
        z = np.concatenate(([self.prev], y))
        self.prev = y[-1]
        dem = np.angle(z[1:] * np.conj(z[:-1])) * (self.fs1 / (2 * math.pi * 50e3))
        a, self.z2 = signal.lfilter(self.h2, 1, dem, zi=self.z2)
        m = a.size
        a = a[self.p2::self.d2]
        self.p2 = (self.p2 - m) % self.d2
        a, self.zde = signal.lfilter(self.de_b, self.de_a, a, zi=self.zde)
        # убрать постоянную составляющую (расстройка несущей звука)
        if a.size:
            self.dc = 0.995 * self.dc + 0.005 * float(a.mean())
            a = a - self.dc
        data = np.concatenate(([self.last], a))
        if data.size < 2:
            return np.zeros(0, np.float32)
        span = data.size - 1
        cnt = int(math.ceil((span - self.t) / self.ratio)) if span > self.t else 0
        pos = self.t + np.arange(cnt) * self.ratio
        out = np.interp(pos, np.arange(data.size), data)
        self.t = (pos[-1] + self.ratio - span) if cnt else self.t - span
        self.last = float(data[-1])
        return out.astype(np.float32)
