"""Тестовый эфир для радиорежимов: WFM-стерео, NFM, AM (авиа), SSB, CW, при наличии — SSTV и DMR.

Станции рисуются только если попадают в полосу приёмника. Выход — сырые байты, как у донгла.
"""

import numpy as np

MORSE = {"C": "-.-.", "Q": "--.-", "D": "-..", "E": ".", "R": ".-.", "T": "-", "L": ".-..", "V": "...-",
         " ": " "}


def _morse_env(text, wpm, fs, n0, n):
    dot = 1.2 / wpm
    seq = []
    for ch in text:
        code = MORSE.get(ch, "")
        if code == " ":
            seq.append((0, 4 * dot))
            continue
        for sym in code:
            seq.append((1, dot if sym == "." else 3 * dot))
            seq.append((0, dot))
        seq.append((0, 2 * dot))
    seq.append((0, 10 * dot))
    total = sum(d for _, d in seq)
    edges = np.cumsum([0] + [d for _, d in seq])
    vals = np.array([v for v, _ in seq], np.float32)
    t = ((n0 + np.arange(n)) / fs) % total
    return vals[np.clip(np.searchsorted(edges, t, side="right") - 1, 0, vals.size - 1)]


class Station:
    def __init__(self, freq, kind, level=0.2, **kw):
        self.freq, self.kind, self.level, self.kw = freq, kind, level, kw
        self.phase = 0.0
        self.audio_pos = 0
        self.clip = None


class RadioTestSignal:
    def __init__(self, fs, _std=None):
        self.fs = float(fs)
        self.n = 0
        self.rng = np.random.default_rng(7)
        self.stations = [
            Station(101.2e6, "wfm", 0.25, stereo=True, rds=("RTLTV FM", "Тестовый эфир RtlTV: музыка в стерео")),
            Station(104.0e6, "wfm", 0.12, stereo=False),
            Station(446.00625e6, "nfm", 0.15),
            Station(125.5e6, "am", 0.12),
            Station(14.230e6, "usb", 0.15),
            Station(7.030e6, "cw", 0.1),
            Station(145.800e6, "nfm_sstv", 0.15),
            Station(446.1e6, "dmr", 0.15),
        ]
        self._sstv_clip = None

    # ---------- звуковые программы ----------
    def _music(self, t, right=False):
        notes = [261.6, 329.6, 392.0, 523.3] if not right else [196.0, 246.9, 293.7, 392.0]
        k = (t / 0.5).astype(np.int64) % len(notes)
        f = np.array(notes)[k]
        env = 0.5 + 0.5 * np.cos(2 * np.pi * (t % 0.5) / 0.5 * 0.5) ** 2
        return 0.35 * env * np.sin(2 * np.pi * f * t)

    def _voice(self, t):
        # «речь»: гармонический сигнал с плавающим тоном и слоговой огибающей, 3 с передача / 2 с пауза
        on = (t % 5.0) < 3.0
        f0 = 140 + 30 * np.sin(2 * np.pi * 0.7 * t)
        ph = 2 * np.pi * np.cumsum(f0) / self.fs
        s = sum(np.sin(k * ph) / k for k in range(1, 8))
        env = 0.5 + 0.5 * np.sin(2 * np.pi * 4 * t) ** 2
        return 0.4 * s * env * on, on

    def _sstv_audio(self, n):
        if self._sstv_clip is None:
            try:
                from .sstv import encode
                from .testsignal import make_card
                card = make_card(256, 320)
                rgb = np.stack([card, np.flipud(card), card[:, ::-1]], axis=2)
                img = (rgb * 255).astype(np.uint8)
                a = encode(img, "Robot 36", fs=48000)
                self._sstv_clip = np.concatenate((np.zeros(48000 * 3), a, np.zeros(48000 * 5))).astype(np.float32)
            except Exception:  # noqa: BLE001 — декодера SSTV может ещё не быть
                self._sstv_clip = np.zeros(48000, np.float32)
        return self._sstv_clip

    def _clip_at(self, clip, t):
        # линейная интерполяция: ступеньки дали бы зеркальные копии спектра через каждые 48 кГц
        pos = (t * 48000) % clip.size
        i0 = pos.astype(np.int64)
        fr = pos - i0
        return clip[i0] * (1 - fr) + clip[(i0 + 1) % clip.size] * fr

    def _dmr_iq(self, st, t, off):
        if st.clip is None:
            try:
                from .dmr import test_signal  # синтетический DMR (частота, Гц @ 48 кГц)
                st.clip = np.asarray(test_signal(), np.float32)
            except Exception:  # noqa: BLE001
                st.clip = np.zeros(0, np.float32)
        if st.clip.size == 0:
            return None
        f = self._clip_at(st.clip, t)
        ph = st.phase + np.cumsum(2 * np.pi * (off + f) / self.fs)
        st.phase = float(ph[-1] % (2 * np.pi))
        return np.exp(1j * ph)

    # ---------- рендер ----------
    def render(self, fc, nsamples, gain_db):
        fs = self.fs
        t = (self.n + np.arange(nsamples)) / fs
        self.n += nsamples
        g = 10 ** ((gain_db - 30.0) / 20.0)
        x = (self.rng.standard_normal(nsamples) + 1j * self.rng.standard_normal(nsamples)) * (0.006 * g)
        for st in self.stations:
            off = st.freq - fc
            if abs(off) > fs / 2 - 20e3:
                continue
            y = self._station(st, t, off)
            if y is not None:
                x += y * (st.level * g)
        iq = np.empty(2 * nsamples, np.float32)
        iq[0::2] = x.real
        iq[1::2] = x.imag
        return np.clip(np.round(iq * 128 + 127.4), 0, 255).astype(np.uint8).tobytes()

    def _fm(self, st, t, off, mod_hz):
        ph = st.phase + np.cumsum(2 * np.pi * (off + mod_hz) / self.fs)
        st.phase = float(ph[-1] % (2 * np.pi))
        return np.exp(1j * ph)

    def _station(self, st, t, off):
        k = st.kind
        if k == "wfm":
            L = self._music(t)
            R = self._music(t, right=True) if st.kw.get("stereo") else L
            mpx = 0.45 * (L + R) + 0.45 * (L - R) * np.sin(2 * np.pi * 38000 * t)
            if st.kw.get("stereo"):
                mpx += 0.09 * np.sin(2 * np.pi * 19000 * t)
            if st.kw.get("rds"):
                if st.clip is None:
                    from .rds import RdsEncoder, make_groups
                    ps, rt = st.kw["rds"]
                    st.clip = RdsEncoder(make_groups(0x7A01, ps, rt, pty=10))
                # RDS: двухфазные символы 1187,5 бит/с на поднесущей 57 кГц (3-я гармоника пилота), 3 %
                mpx = mpx + 0.03 * st.clip.value(t) * np.sin(2 * np.pi * 57000 * t)
            return self._fm(st, t, off, 75000 * mpx)
        if k == "nfm":
            v, on = self._voice(t)
            return self._fm(st, t, off, 2500 * v) * on
        if k == "nfm_sstv":
            a = self._clip_at(self._sstv_audio(0), t)
            return self._fm(st, t, off, 3000 * a)
        if k == "am":
            v, _ = self._voice(t)
            return (1 + 0.6 * v) * np.exp(2j * np.pi * off * t)
        if k == "usb":
            a = self._clip_at(self._sstv_audio(0), t)
            # SSB = аналитический сигнал звука, сдвинутый на частоту несущей
            from scipy.signal import hilbert
            return hilbert(a) * np.exp(2j * np.pi * off * t)
        if k == "cw":
            env = _morse_env("CQ CQ DE RTLTV ", 18, self.fs, int(t[0] * self.fs), t.size)
            return env * np.exp(2j * np.pi * off * t)
        if k == "dmr":
            return self._dmr_iq(st, t, off)
        return None


def factory(fs, std=None):
    return RadioTestSignal(fs, std)
