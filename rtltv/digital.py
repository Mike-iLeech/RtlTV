"""Цифровое ТВ: измеритель мультиплекса (DVB-T/T2).

Картинку DVB-T2 RTL-SDR не покажет: мультиплекс занимает 7,8 МГц, а донгл стабильно отдаёт
не больше ~3 МГц полосы, т.е. в АЦП попадает только треть поднесущих OFDM. Поэтому цифровой режим —
это прибор для наведения антенны: весь канал собирается из нескольких перестроек, считаются
уровень, C/N и по циклическому префиксу определяется режим FFT, защитный интервал и уход частоты.
"""
import math

import numpy as np

HOP_STEP = 2.0e6
HOP_KEEP = 1.0e6
SPEC_FFT = 1024

# полезная длительность символа, 8 МГц (элементарный период 7/64 мкс)
FFT_MODES = {"1K": 112e-6, "2K": 224e-6, "4K": 448e-6, "8K": 896e-6, "16K": 1792e-6, "32K": 3584e-6}
GUARDS = {"1/128": 1 / 128, "1/32": 1 / 32, "1/16": 1 / 16, "19/256": 19 / 256,
          "1/8": 1 / 8, "19/128": 19 / 128, "1/4": 1 / 4}


def psd_db(iq, nfft=SPEC_FFT):
    n = (iq.size // nfft) * nfft
    if n == 0:
        return np.full(nfft, -120.0)
    seg = iq[:n].reshape(-1, nfft) * np.hanning(nfft).astype(np.float32)
    p = np.mean(np.abs(np.fft.fft(seg, axis=1)) ** 2, axis=0) / (nfft * 0.375)
    p = np.fft.fftshift(p)
    c = nfft // 2
    p[c - 1:c + 2] = 0.5 * (p[c - 3] + p[c + 3])  # провал вместо пика постоянной составляющей
    return 10 * np.log10(p + 1e-12)


class MuxAnalyzer:
    def __init__(self, fs, center, bandwidth):
        self.fs = float(fs)
        self.center = float(center)
        self.bandwidth = float(bandwidth)
        span = bandwidth / 2 + 1.0e6
        n = int(math.ceil(span / HOP_STEP))
        self.offsets = [i * HOP_STEP for i in range(-n, n + 1)]
        self.parts = {}
        self.cp = None
        self.ofdm = OfdmEstimator(self.fs)

    def plan(self, with_cp, sweeps=3):
        """Несколько проходов по каналу и один длинный захват для анализа OFDM (он долгий —
        если делать его каждый проход, спектр обновлялся бы ~1 раз/с)."""
        items = [(self.center + o, 32768) for o in self.offsets] * max(1, sweeps)
        if with_cp:
            items.append((self.center, 393216))
        return items

    def add(self, freq, iq):
        """Возвращает итог, когда собран весь канал."""
        if iq.size >= 300000:
            self.cp = self.ofdm.add(iq)
            return None
        off = freq - self.center
        p = psd_db(iq)
        f = (np.arange(SPEC_FFT) - SPEC_FFT // 2) * (self.fs / SPEC_FFT)
        keep = np.abs(f) <= HOP_KEEP
        self.parts[round(off)] = (f[keep] + off, p[keep])
        if len(self.parts) < len(self.offsets):
            return None
        # канал уже собран целиком: обновляем итог после каждой перестройки (скользящая сборка)
        fr = np.concatenate([self.parts[k][0] for k in sorted(self.parts)])
        pr = np.concatenate([self.parts[k][1] for k in sorted(self.parts)])
        return self._metrics(fr, pr)

    def _metrics(self, f, p):
        hb = self.bandwidth / 2
        inb = np.abs(f) < hb - 0.25e6
        outb = (np.abs(f) > hb + 0.15e6)
        lin = 10 ** (p / 10)
        sig = float(np.mean(lin[inb]))
        noise = float(np.median(lin[outb])) if outb.any() else float(np.percentile(lin, 5))
        cn = 10 * math.log10(max(sig - noise, 1e-12) / max(noise, 1e-12)) if sig > noise else 0.0
        flat = float(np.std(p[inb]))
        level = 10 * math.log10(sig * self.bandwidth / self.fs + 1e-12)
        peaky = float(p[inb].max() - np.median(p[inb])) > 15.0  # узкая несущая: аналоговое ТВ и т.п.
        ofdm_ok = bool(self.cp and self.cp.get("found"))
        present = cn > 3.0 and (ofdm_ok or (flat < 5.0 and not peaky))
        return {
            "freqs": f + self.center,
            "psd": p,
            "cn_db": cn,
            "level_db": level,
            "flatness_db": flat,
            "present": present,
            "other": cn > 3.0 and not present,
            "ofdm": self.cp,
        }


def _prep(iq):
    """Убрать постоянную составляющую и узкие паразитные тоны донгла: они коррелируют
    на любом сдвиге и забивают слабую корреляцию циклического префикса."""
    x = iq.astype(np.complex64)
    n = 1 << int(math.floor(math.log2(x.size)))
    X = np.fft.fft(x[:n])
    p = np.abs(X) ** 2
    k = 129
    ref = np.convolve(np.concatenate((p[-k:], p, p[:k])), np.ones(k) / k, "same")[k:-k]
    bad = p > 6 * ref
    bad |= np.roll(bad, 1) | np.roll(bad, -1)
    bad[0] = True
    X[bad] = 0
    return np.fft.ifft(X).astype(np.complex64)


def mode_scores(x, fs):
    """Нормированная автокорреляция на сдвиге полезной длительности символа каждого режима FFT."""
    pw = float(np.mean(np.abs(x) ** 2)) + 1e-12
    out = {}
    for name, tu in FFT_MODES.items():
        lag = int(round(tu * fs))
        if lag * 4 > x.size:
            continue
        out[name] = abs(complex(np.mean(x[:-lag] * np.conj(x[lag:])))) / pw
    return out


def guard_scores(x, fs, mode):
    """Свёртка произведения по длине символа: у верного защитного интервала пик острее.
    Возвращает {GI: (острота, фаза пика)}."""
    lag = int(round(FFT_MODES[mode] * fs))
    prod = x[:-lag] * np.conj(x[lag:])
    n = prod.size
    nb = 256
    out = {}
    for gname, g in GUARDS.items():
        period = lag * (1 + g)
        if period * 3 > n:
            continue
        bins = ((np.arange(n) % period) / period * nb).astype(np.int64)
        folded = (np.bincount(bins, weights=prod.real, minlength=nb)
                  + 1j * np.bincount(bins, weights=prod.imag, minlength=nb))
        mag = np.abs(folded)
        w = max(1, int(round(nb * g / (1 + g))))
        sm = np.convolve(np.concatenate((mag, mag[:w - 1])), np.ones(w) / w, "valid")[:nb]
        out[gname] = (float(sm.max() / (np.median(sm) + 1e-12)), float(np.angle(folded[int(np.argmax(mag))])))
    return out


class OfdmEstimator:
    """Оценки копятся по захватам: на реальном эфире с многолучёвостью один захват ненадёжен."""

    def __init__(self, fs):
        self.fs = fs
        self.modes = {}
        self.guards = {}
        self.n = 0

    def add(self, iq):
        """Режим выбирается не по величине корреляции, а по остроте пика в свёртке по символу:
        у DVB-T2 пилоты дают корреляцию и на долях Tu (Tu/4, Tu/6...), но она ровная во времени,
        а циклический префикс даёт пик ровно в защитном интервале."""
        x = _prep(iq)
        a = 0.35 if self.n else 1.0
        self.n += 1
        scores = mode_scores(x, self.fs)
        for k, v in scores.items():
            self.modes[k] = (1 - a) * self.modes.get(k, v) + a * v
        thr = 5.0 / math.sqrt(x.size)
        phases = {}
        for m, rho in scores.items():
            if rho < thr:
                continue
            for g, (pk, ph) in guard_scores(x, self.fs, m).items():
                key = (m, g)
                self.guards[key] = (1 - a) * self.guards.get(key, pk) + a * pk
                phases[key] = ph
        for key in list(self.guards):
            if key[0] not in scores or scores[key[0]] < thr:
                self.guards[key] *= (1 - a)
        if not self.guards:
            best = max(self.modes, key=self.modes.get) if self.modes else "?"
            return {"found": False, "mode": best, "rho": self.modes.get(best, 0.0), "captures": self.n}
        (mode, gi), pk = max(self.guards.items(), key=lambda kv: kv[1])
        out = {"found": pk > 5.0, "mode": mode, "guard": gi, "rho": self.modes.get(mode, 0.0),
               "peak": pk, "captures": self.n}
        if (mode, gi) in phases:
            out["freq_offset_hz"] = -phases[(mode, gi)] / (2 * math.pi * FFT_MODES[mode])
        return out


def detect_ofdm(iq, fs):
    """Разовая оценка по одному захвату."""
    return OfdmEstimator(fs).add(iq)
