"""Радио: выделение канала (VFO) и демодуляторы WFM (стерео + RDS), NFM, AM, USB, LSB, CW, DMR.

Частоты дискретизации радиорежима кратны 240 кГц и 48 кГц (0,96 … 2,88 MS/s), поэтому
канал выделяется целым прореживанием: WFM — до 240 кГц, остальные — сразу до 48 кГц.

Цепочка (как в SDR#): [бланкер импульсов, удаление постоянной, коррекция IQ — FrontEnd, до спектра]
-> перенос VFO в ноль -> прореживание -> фильтр канала -> демодулятор -> АРУ (AM/SSB/CW)
-> звуковой фильтр -> шумоподавитель звука -> шумодав (с гистерезисом и плавным открытием).
Все фильтры потоковые: результат не зависит от того, как поток порезан на блоки.
"""
import math

import numpy as np
from scipy import signal

AUDIO_RATE = 48000
RADIO_RATES = [960000, 1200000, 1440000, 1920000, 2400000, 2880000]
PILOT = 19000.0

MODES = {
    # имя: (полоса по умолчанию, Гц; диапазон полосы; шаг настройки, Гц)
    "WFM": (200000, (50000, 220000), 100000),
    "NFM": (12500, (5000, 25000), 12500),
    "AM": (10000, (3000, 20000), 5000),
    "USB": (2800, (1500, 4000), 100),
    "LSB": (2800, (1500, 4000), 100),
    "CW": (500, (100, 1500), 10),
    "DMR": (12500, (12500, 12500), 12500),
}

SSB_LOW = 100.0     # нижний край звуковой полосы SSB, Гц (от несущей)


class Decimator:
    """Потоковый FIR-дециматор (полифазный через upfirdn): только нужные выходные отсчёты."""

    def __init__(self, d, cutoff_rel, taps_per_phase=8):
        self.d = int(d)
        L = self.d * taps_per_phase + 1  # (L-1) кратно d — выходы ложатся на сетку upfirdn
        self.h = signal.firwin(L, min(0.98, cutoff_rel) / self.d) if self.d > 1 else np.array([1.0])
        self.L = self.h.size
        self.tail = None

    def __call__(self, x):
        if self.d == 1:
            return x
        if self.tail is None:
            self.tail = np.zeros(self.L - 1, dtype=x.dtype)
        buf = np.concatenate((self.tail, x))
        n = buf.size - (self.L - 1)
        if n <= 0:
            self.tail = buf
            return np.zeros(0, dtype=x.dtype)
        cnt = (n - 1) // self.d + 1
        y = signal.upfirdn(self.h, buf, 1, self.d)
        k0 = (self.L - 1) // self.d
        y = y[k0:k0 + cnt]
        nxt = (self.L - 1) + cnt * self.d
        self.tail = buf[nxt - (self.L - 1):]
        return y.astype(x.dtype, copy=False)


class Mixer:
    """Перенос частоты с непрерывной фазой. Таблица фазоров кэшируется — на блок одно умножение."""

    def __init__(self, fs):
        self.fs = fs
        self.ph = 0.0
        self._key = None
        self._tab = None

    def __call__(self, x, f):
        if f == 0:
            return x
        w = -2 * math.pi * f / self.fs
        key = (w, x.size)
        if key != self._key:
            self._key = key
            self._tab = np.exp(1j * w * np.arange(x.size)).astype(np.complex64)
        out = x * (self._tab * np.complex64(complex(math.cos(self.ph), math.sin(self.ph))))
        self.ph = (self.ph + w * x.size) % (2 * math.pi)
        return out


class Nco:
    """Генератор exp(j·2π·f·n/fs) с непрерывной фазой между блоками (complex128)."""

    def __init__(self, f, fs):
        self.w = 2 * math.pi * f / fs
        self.ph = 0.0
        self._n = -1
        self._tab = None

    def __call__(self, n):
        if n != self._n:
            self._n = n
            self._tab = np.exp(1j * self.w * np.arange(n))
        out = self._tab * complex(math.cos(self.ph), math.sin(self.ph))
        self.ph = (self.ph + self.w * n) % (2 * math.pi)
        return out


class Fir:
    """Потоковый КИХ с историей входа: коэффициенты можно менять на ходу без щелчка (set_taps)."""

    def __init__(self, h, dtype=np.float64):
        self.h = np.asarray(h, np.float64)
        self.hist = np.zeros(self.h.size - 1, dtype=dtype)

    def set_taps(self, h):
        h = np.asarray(h, np.float64)
        n = h.size - 1
        if n != self.hist.size:
            hist = np.zeros(n, dtype=self.hist.dtype)
            k = min(n, self.hist.size)
            if k:
                hist[-k:] = self.hist[-k:]
            self.hist = hist
        self.h = h

    def __call__(self, x):
        if x.size == 0:
            return np.zeros(0, dtype=np.result_type(x, self.h))
        buf = np.concatenate((self.hist, x))
        y = signal.convolve(buf, self.h, mode="valid")
        self.hist = buf[buf.size - self.hist.size:] if self.hist.size else self.hist
        return y


class Delay:
    def __init__(self, n):
        self.buf = np.zeros(int(n))

    def __call__(self, x):
        if self.buf.size == 0:
            return x
        z = np.concatenate((self.buf, x))
        self.buf = z[x.size:]
        return z[:x.size]


class Iir:
    def __init__(self, b, a, dtype=np.float64):
        self.b, self.a = b, a
        self.z = signal.lfilter_zi(b, a).astype(dtype) * 0

    def __call__(self, x):
        y, self.z = signal.lfilter(self.b, self.a, x, zi=self.z)
        return y


class Sos:
    """Потоковый БИХ в виде биквадов (устойчив и на очень низких частотах среза). Каналы — по оси 0."""

    def __init__(self, sos):
        self.sos = np.asarray(sos)
        self.z = None

    def __call__(self, x):
        if self.z is None or self.z.dtype != np.result_type(x, np.float64) or self.z.shape[2:] != x.shape[1:]:
            self.z = np.zeros((self.sos.shape[0], 2) + x.shape[1:], np.result_type(x, np.float64))
        y, self.z = signal.sosfilt(self.sos, x, axis=0, zi=self.z)
        return y


def deemph(tau, fs):
    a = math.exp(-1 / (fs * tau))
    return [1 - a], [1, -a]


class Agc:
    """АРУ звука в духе SDR#: огибающая по пикам 1-мс кусков с упреждением (атака раньше пика, без хлопка),
    спад с постоянной decay_ms, удержание (hang), порог и наклон. Усиление между кусками интерполируется —
    нет «ступенек» (треска). on=False — постоянное усиление manual_db."""

    CH = 48

    def __init__(self, fs=AUDIO_RATE, target=0.5, threshold_db=-100.0, decay_ms=400.0, slope_db=0.0,
                 hang=False, lookahead_ms=5.0):
        self.fs = fs
        self.target = target
        self.on = True
        self.manual_db = 40.0
        self.threshold_db = threshold_db
        self.decay_ms = decay_ms
        self.slope_db = slope_db
        self.hang = hang
        self.hang_ms = 500.0
        self.D = max(1, int(round(lookahead_ms * fs / 1000 / self.CH)))
        self.reset()

    def reset(self):
        self.env = 0.0
        self.hold = 0
        self.g = 1.0
        self._gh = np.ones(self.D)        # усиления последних D кусков (для упреждения)
        self.inbuf = np.zeros(0)
        self.dbuf = np.zeros(self.D * self.CH)   # задержка звука на D кусков

    def gain_db(self):
        return 20 * math.log10(max(self.g, 1e-12))

    def _law(self, env):
        thr = 10 ** (self.threshold_db / 20)
        e = max(env, thr)
        g = self.target / e
        if self.slope_db > 0:
            g *= (e / thr) ** (self.slope_db / 20.0)
        return min(g, 0.95 / max(env, 1e-12))

    def __call__(self, x):
        x = np.asarray(x, np.float64)
        CH = self.CH
        buf = np.concatenate((self.inbuf, x))
        nch = buf.size // CH
        take = nch * CH
        self.inbuf = buf[take:]
        if nch == 0:
            return np.zeros(0)
        blk = buf[:take]
        if not self.on:
            g = 10 ** (self.manual_db / 20)
            gains = np.full(nch, g)
        else:
            pk = np.abs(blk).reshape(nch, CH).max(axis=1)
            k = math.exp(-CH / self.fs / max(self.decay_ms / 1000, 1e-3))
            hang_n = int(self.hang_ms * self.fs / 1000 / CH) if self.hang else 0
            env, hold = self.env, self.hold
            gains = np.empty(nch)
            law = self._law
            for i in range(nch):
                p = pk[i]
                if p >= env:
                    env, hold = p, hang_n
                elif hold > 0:
                    hold -= 1
                else:
                    env = max(p, env * k)
                gains[i] = law(env)
            self.env, self.hold = env, hold
        # упреждение: к куску i-D применяем минимальное усиление окна [i-D, i]
        hist = np.concatenate((self._gh, gains))
        D = self.D
        g_out = hist[:nch].copy()
        for j in range(1, D + 1):
            np.minimum(g_out, hist[j:j + nch], out=g_out)
        self._gh = hist[-D:]
        # задержанный звук
        dl = np.concatenate((self.dbuf, blk))
        aud = dl[:take]
        self.dbuf = dl[take:]
        # плавная интерполяция усиления внутри куска
        g0 = np.concatenate(([self.g], g_out[:-1]))
        fr = (np.arange(1, CH + 1) / CH)[None, :]
        ramp = g0[:, None] + (g_out - g0)[:, None] * fr
        self.g = float(g_out[-1])
        return aud * ramp.ravel()


class FrontEnd:
    """Обработка всей полосы до спектра и демодулятора: бланкер импульсных помех, удаление постоянной
    составляющей (пик в центре спектра), коррекция дисбаланса I/Q (зеркальные копии сигналов)."""

    def __init__(self):
        self.nb = False
        self.nb_thr = 8.0         # порог бланкера: во сколько раз импульс выше среднего уровня
        self.nb_width_us = 4.0    # сколько вырезать вокруг импульса, мкс (шире — свой щелчок от провала)
        self.dc = True
        self.iq = False
        self._dc = None
        self._st = None
        self.blanked = 0.0        # доля вырезанных отсчётов в последнем блоке
        self.iq_err = (0.0, 0.0)  # дисбаланс: усиление, дБ; фаза, градусы

    def process(self, iq, fs):
        x = iq
        if self.nb:
            x = self._blank(x, fs)
        else:
            self.blanked = 0.0
        if self.dc:
            m = complex(np.mean(x[::4])) if x.size else 0j
            self._dc = m if self._dc is None else 0.8 * self._dc + 0.2 * m
            x = x - np.complex64(self._dc)
        if self.iq and x.size > 64:
            x = self._iq(x)
        return x.astype(np.complex64, copy=False)

    def _blank(self, x, fs):
        sub = x[::8]
        mag = np.abs(x)
        ref = float(np.median(np.abs(sub))) * 1.25 + 1e-9
        hit = np.flatnonzero(mag > self.nb_thr * ref)
        if hit.size == 0 or hit.size > 0.05 * x.size:
            # нет импульсов или это не импульсы (сильный сигнал во всей полосе) — не трогаем
            self.blanked = 0.0
            return x
        w = max(1, int(self.nb_width_us * 1e-6 * fs / 2))
        mask = np.zeros(x.size, bool)
        idx = (hit[:, None] + np.arange(-w, w + 1)[None, :]).ravel()
        mask[idx[(idx >= 0) & (idx < x.size)]] = True
        keep = np.flatnonzero(~mask)
        if keep.size < 2:
            return x
        # вырезанное заполняется прямой между краями: у нулей был бы свой щелчок (провал несущей)
        bad = np.flatnonzero(mask)
        x = x.copy()
        x[bad] = (np.interp(bad, keep, x.real[keep]) + 1j * np.interp(bad, keep, x.imag[keep])).astype(np.complex64)
        self.blanked = bad.size / x.size
        return x

    def _iq(self, x):
        s = x[::8]
        i, q = s.real.astype(np.float64), s.imag.astype(np.float64)
        st = np.array([np.mean(i * i), np.mean(q * q), np.mean(i * q)])
        self._st = st if self._st is None else 0.95 * self._st + 0.05 * st
        a, b, c = self._st
        if a <= 0 or b <= 0:
            return x
        g = math.sqrt(a / b)
        sn = max(-0.5, min(0.5, c / math.sqrt(a * b)))
        cs = math.sqrt(1 - sn * sn)
        self.iq_err = (20 * math.log10(g), math.degrees(math.asin(sn)))
        q2 = (x.imag * g - x.real * sn) / cs
        return (x.real + 1j * q2).astype(np.complex64)


class RadioDemod:
    """Канал на смещении offset (Гц) от центра приёмника -> звук 48 кГц (моно или стерео)."""

    def __init__(self, fs, mode="WFM", bandwidth=None):
        self.fs = int(fs)
        self.mode = mode
        self.bw = float(bandwidth or MODES[mode][0])
        self.offset = 0.0
        self.deemph_tau = 50e-6
        self.stereo = True
        self.squelch_db = -200.0
        self.level_db = -120.0
        self.stereo_lock = False
        self.pilot_level = 0.0     # амплитуда пилот-тона, доли девиации 75 кГц
        self.dmr = None            # DmrDecoder подключается снаружи
        self.dmr_events = []
        # настройки в духе SDR# (переживают смену режима)
        self.agc = Agc()
        self.cw_tone = 700.0
        self.audio_hp = 0.0        # ФВЧ звука, Гц (0 — выкл)
        self.audio_lp = 0.0        # ФНЧ звука, Гц (0 — выкл)
        self.audio_nr = False      # спектральный шумоподавитель звука
        self.nr_strength = 1.6
        self.rds_on = True
        self.rds = None
        self.sq_open = True
        self._sq_g = 1.0
        self._build()

    # ---------- настройка ----------
    def set_mode(self, mode, bandwidth=None):
        bw = float(bandwidth or MODES[mode][0])
        if mode == self.mode:
            self.bw = bw
            self._build_bw()
            return
        self.mode = mode
        self.bw = bw
        self._build()

    def set_bandwidth(self, bw):
        lo, hi = MODES[self.mode][1]
        self.bw = float(min(max(bw, lo), hi))
        self._build_bw()

    def set_deemph(self, tau):
        self.deemph_tau = tau
        if self.mode == "WFM":
            b, a = deemph(self.deemph_tau, AUDIO_RATE)
            self.de_l, self.de_r = Iir(b, a), Iir(b, a)

    def set_audio_filter(self, hp=None, lp=None):
        if hp is not None:
            self.audio_hp = float(hp)
        if lp is not None:
            self.audio_lp = float(lp)
        self._build_audio()

    def reset_station(self):
        """Новая станция: сбросить RDS и захват стерео (вызывает движок при перестройке VFO)."""
        if self.rds is not None:
            self.rds.reset()
        self.stereo_lock = False
        self._lock_cnt = 0

    def _build(self):
        fs = self.fs
        self.mix = Mixer(fs)
        self.ch_rate = 240000 if self.mode == "WFM" else AUDIO_RATE
        d = fs // self.ch_rate
        # первый каскад: прореживание до частоты канала с запасом под полосу (не зависит от полосы в режиме)
        lo, hi = MODES[self.mode][1]
        self.dec = Decimator(d, min(0.9, max(hi, 2 * 15000) / self.ch_rate * 1.1), 16)
        self.chan = None
        self.prev = 1 + 0j
        self.agc.reset()
        cr = self.ch_rate
        if self.mode == "WFM":
            # стерео: поднесущая 38 кГц восстанавливается из пилота «фазором» — пилот переносится в ноль,
            # узкий ФНЧ 40 Гц даёт его фазу без шума; sin(2·фазы) — поднесущая, (фаза·3) — несущая RDS.
            # L+R и L−R проходят один и тот же ФНЧ 15 кГц — задержки совпадают сами.
            self.mono_lp = Fir(signal.firwin(97, 15000, fs=cr))
            self.diff_lp = Fir(signal.firwin(97, 15000, fs=cr))
            # частотный дискриминатор (разность фаз соседних отсчётов) ослабляет 38 кГц в sinc(38k/cr) раз —
            # без поправки разделение каналов не выше ~34 дБ
            k = math.pi * 2 * PILOT / cr
            self.diff_gain = 2.0 * k / math.sin(k)
            self.pilot_nco = Nco(PILOT, cr)
            self.pilot_lp = Sos(signal.butter(2, 40, fs=cr, output="sos"))
            self._lock_cnt = 0
            self._blend = 0.0
            self.out_dec = Decimator(cr // AUDIO_RATE, 0.62, 16)
            self.out_dec_s = Decimator(cr // AUDIO_RATE, 0.62, 16)
            self.set_deemph(self.deemph_tau)
            if self.rds is None:
                from .rds import RdsDecoder
                self.rds = RdsDecoder(cr)
            else:
                self.rds.reset()
        elif self.mode in ("NFM", "DMR"):
            self.hp = Iir(*signal.butter(2, 300, "highpass", fs=AUDIO_RATE))
            self.lp = Iir(*signal.butter(4, 3400, fs=AUDIO_RATE))
        elif self.mode == "AM":
            # огибающая -> ФВЧ (убирает несущую сразу, без медленного слежения) -> ФНЧ -> АРУ
            self.am_hp = Sos(signal.butter(2, 60, "highpass", fs=AUDIO_RATE, output="sos"))
            self.lp = None
        else:
            self.ssb_lp = None
            self.ssb_mix1 = Mixer(AUDIO_RATE)
            self.ssb_mix2 = Mixer(AUDIO_RATE)
        self._build_bw()
        self._build_audio()

    def _build_bw(self):
        cr = self.ch_rate
        half = self.bw / 2
        if self.mode in ("USB", "LSB", "CW"):
            h = signal.firwin(129, 0.95 * 6000, fs=cr)   # полоса задаётся комплексным фильтром ниже
        else:
            h = signal.firwin(129 if cr == AUDIO_RATE else 65, min(half, 0.48 * cr), fs=cr)
        if self.chan is None:
            self.chan = Fir(h, np.complex128)
        else:
            self.chan.set_taps(h)
        if self.mode == "AM":
            sos = signal.butter(4, min(self.bw / 2, 8000), fs=AUDIO_RATE, output="sos")
            if self.lp is None:
                self.lp = Sos(sos)
            else:
                self.lp.sos = sos
        elif self.mode in ("USB", "LSB", "CW"):
            # SSB: середина звуковой полосы сдвигается в ноль, симметричный ФНЧ, сдвиг обратно, вещественная
            # часть. CW: узкий ФНЧ вокруг несущей и перенос на тон cw_tone.
            if self.mode == "CW":
                self.ssb_center = 0.0
            else:
                self.ssb_center = (SSB_LOW + half) * (1 if self.mode == "USB" else -1)
            h = signal.firwin(383, half, fs=AUDIO_RATE)
            if self.ssb_lp is None:
                self.ssb_lp = Fir(h, np.complex128)
            else:
                self.ssb_lp.set_taps(h)

    def _build_audio(self):
        stages = []
        if self.audio_hp > 0:
            stages.append(signal.butter(4, self.audio_hp, "highpass", fs=AUDIO_RATE, output="sos"))
        if 0 < self.audio_lp < AUDIO_RATE / 2:
            stages.append(signal.butter(6, self.audio_lp, fs=AUDIO_RATE, output="sos"))
        self.afilt = Sos(np.concatenate(stages)) if stages else None
        self._nr = None

    # ---------- обработка ----------
    def process(self, iq):
        """iq — блок complex64 на частоте fs. Возвращает звук float32 [N] или [N, 2]."""
        x = self.mix(iq, self.offset)
        x = self.dec(x)
        x = self.chan(x)
        pw = np.abs(x) ** 2
        p = float(np.mean(pw)) + 1e-20 if x.size else 1e-20
        self.level_db = 10 * math.log10(p)
        m = self.mode
        if m == "WFM":
            out = self._wfm(x)
        elif m in ("NFM", "DMR"):
            out = self._nfm(x)
        elif m == "AM":
            out = self._am(x)
        else:
            out = self._ssb(x)
        if m != "DMR" and out.size:
            if self.afilt is not None:
                out = self.afilt(out)
            if self.audio_nr:
                out = self._denoise(out)
            out = self._squelch(out, pw)
        return out.astype(np.float32)

    def _denoise(self, out):
        if self._nr is None:
            from .satrx import SpectralNR
            n = 2 if out.ndim == 2 else 1
            self._nr = [SpectralNR() for _ in range(n)]
        for nr in self._nr:
            nr.strength = self.nr_strength
        if out.ndim == 2:
            if len(self._nr) != 2:
                self._nr = None
                return out
            a, b = self._nr[0].process(out[:, 0]), self._nr[1].process(out[:, 1])
            n = min(a.size, b.size)
            return np.stack((a[:n], b[:n]), axis=1) * 1.2
        if len(self._nr) != 1:
            self._nr = None
            return out
        return self._nr[0].process(out) * 1.2

    def _squelch(self, out, pw):
        """Шумодав по уровню канала кусками по 10 мс: гистерезис 2 дБ, открытие/закрытие плавное (5 мс)."""
        if self.squelch_db <= -199:
            self.sq_open = True
            self._sq_g = 1.0
            return out
        seg = max(1, self.ch_rate // 100)
        n = pw.size // seg
        if n == 0:
            lv = np.array([self.level_db])
        else:
            lv = 10 * np.log10(pw[:n * seg].reshape(n, seg).mean(axis=1) + 1e-20)
        gates = np.empty(lv.size)
        op = self.sq_open
        for i, v in enumerate(lv):
            if op and v < self.squelch_db - 2.0:
                op = False
            elif not op and v >= self.squelch_db:
                op = True
            gates[i] = 1.0 if op else 0.0
        self.sq_open = op
        na = out.shape[0]
        g = gates[np.minimum((np.arange(na) * gates.size) // max(na, 1), gates.size - 1)]
        # плавный переход: одно-полюсное сглаживание с постоянной ~2 мс
        a = math.exp(-1.0 / (0.002 * AUDIO_RATE))
        g, zf = signal.lfilter([1 - a], [1, -a], g, zi=[self._sq_g * a])
        self._sq_g = float(g[-1]) if g.size else self._sq_g
        return out * (g[:, None] if out.ndim == 2 else g)

    def _disc(self, x):
        z = np.concatenate(([self.prev], x))
        self.prev = x[-1] if x.size else self.prev
        return np.angle(z[1:] * np.conj(z[:-1]))

    def _wfm(self, x):
        cr = self.ch_rate
        mpx = self._disc(x) * (cr / (2 * math.pi * 75000))  # 1.0 = девиация 75 кГц
        lo = self.pilot_nco(mpx.size)
        z = self.pilot_lp(mpx * np.conj(lo))
        mag = np.abs(z)
        u = z / (mag + 1e-12)
        self.pilot_level = 2 * float(np.mean(mag)) if mag.size else 0.0
        coh = float(np.abs(np.mean(u))) if u.size else 0.0
        good = self.pilot_level > 0.025 and coh > 0.8
        # гистерезис захвата: 2 хороших блока подряд — стерео, 3 плохих подряд — моно
        self._lock_cnt = min(max(self._lock_cnt, 0) + 1, 3) if good else max(min(self._lock_cnt, 0) - 1, -3)
        if self._lock_cnt >= 2:
            self.stereo_lock = True
        elif self._lock_cnt <= -3:
            self.stereo_lock = False
        ph = lo * u                       # фазор пилота: exp(j(ω0 n + ψ)), пилот = A·cos(ω0 n + ψ)
        ph2 = ph * ph
        s38 = -ph2.imag                   # sin(2(ω0 n + φ)), φ = ψ + π/2 — фаза пилота sin()
        if self.rds_on and self.rds is not None:
            self.rds.process(mpx, ph2 * ph if self.stereo_lock or self.pilot_level > 0.02 else None)
        mono = self.out_dec(self.mono_lp(mpx))
        diff = self.out_dec_s(self.diff_lp(mpx * s38 * self.diff_gain))
        n = min(mono.size, diff.size)
        mono, diff = mono[:n], diff[:n]
        want = 1.0 if (self.stereo and self.stereo_lock) else 0.0
        if n:
            # переход моно <-> стерео плавно за блок, без щелчка
            b = self._blend + (want - self._blend) * (np.arange(1, n + 1) / n)
            self._blend = want
            diff = diff * b
        left = self.de_l(0.5 * (mono + diff))
        right = self.de_r(0.5 * (mono - diff))
        return np.stack((left, right), axis=1) * 1.4

    def _nfm(self, x):
        f = self._disc(x) * (AUDIO_RATE / (2 * math.pi))  # мгновенная частота, Гц
        if self.mode == "DMR":
            out = np.zeros(f.size)
            if self.dmr is not None:
                audio, ev = self.dmr.process(f.astype(np.float32))
                self.dmr_events.extend(ev)
                out = np.zeros(f.size) if audio is None or audio.size == 0 else audio
            return out
        a = self.lp(self.hp(f / 3000.0))
        return a * 0.7

    def _am(self, x):
        e = np.abs(x)
        a = self.lp(self.am_hp(e))
        return self.agc(a)

    def _ssb(self, x):
        y = self.ssb_mix1(x, self.ssb_center)
        y = self.ssb_lp(y)
        y = self.ssb_mix2(y, -(self.ssb_center if self.mode != "CW" else self.cw_tone))
        return self.agc(np.real(y))


def spectrum_db(iq, nfft=4096, dbfs=False):
    """Спектр мощности блока, дБ. dbfs=True — шкала дБFS: незатухающий тон полной шкалы = 0 дБ
    при любом размере БПФ (как в SDR#); иначе — прежняя шкала (для режимов, где границы уже настроены)."""
    n = (iq.size // nfft) * nfft
    if n == 0:
        return None
    w = np.blackman(nfft).astype(np.float32)
    seg = iq[:n].reshape(-1, nfft)[:8] * w
    norm = float(w.sum()) ** 2 if dbfs else nfft * 0.42
    p = np.mean(np.abs(np.fft.fft(seg, axis=1)) ** 2, axis=0) / norm
    p = np.fft.fftshift(p)
    c = nfft // 2
    p[c - 1:c + 2] = 0.5 * (p[c - 4] + p[c + 4])
    return 10 * np.log10(p + 1e-20)


def s_units(dbm, freq):
    """Баллы S по уровню, дБм: S9 = −73 дБм ниже 30 МГц и −93 дБм выше, 6 дБ на балл."""
    s9 = -73.0 if freq < 30e6 else -93.0
    s = 9 + (dbm - s9) / 6.0
    if s <= 9:
        return f"S{max(0, int(math.floor(s + 1e-9)))}"
    return f"S9+{int(round(dbm - s9))}"
