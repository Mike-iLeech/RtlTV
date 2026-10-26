"""Эталоны для TvTest.kt: считает их Python-модулями rtltv/standards.py, rtltv/digital.py, rtltv/analog.py,
rtltv/testsignal.py и логикой поиска rtltv/engine.py (_scan_step) и пишет ресурс теста tv_ref.tsv.

Запуск из корня репозитория:  PYTHONUTF8=1 py -3.12 android/app/src/test/python/gen_tv_ref.py

Случайные числа — общий для Python и Kotlin генератор (xorshift64* + Бокс — Мюллер, как в gen_aprs_ref.py):
у TestSignal генератор numpy default_rng подменяется на него (TvTestSignal.kt делает то же самое), поэтому эфир
тестового сигнала совпадает до округления, и метрики сверяются с малыми допусками.
Формат строки: тег <TAB> поля...; массивы — числа через пробел.
"""
import math
import os
import sys

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), *[".."] * 5))
sys.path.insert(0, ROOT)
from rtltv import digital, standards, testsignal  # noqa: E402
from rtltv.analog import AnalogVideoDecoder, FmSoundDecoder  # noqa: E402
from rtltv.rtlsdr import u8_to_iq  # noqa: E402

OUT = os.path.join(ROOT, "android", "app", "src", "test", "resources", "tv_ref.tsv")
M64 = (1 << 64) - 1
SOUND_TUNE = 0.25e6


class Noise:
    def __init__(self, seed):
        self.x = (seed * 0x9E3779B97F4A7C15 + 1) & M64

    def nxt(self):
        x = self.x
        x ^= x >> 12
        x ^= (x << 25) & M64
        x ^= x >> 27
        self.x = x
        return (x * 0x2545F4914F6CDD1D) & M64

    def uni(self):
        return (self.nxt() >> 11) * (1.0 / 9007199254740992.0)

    def gauss(self):
        u1 = 1.0 - self.uni()
        u2 = self.uni()
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)

    def sign(self):
        return -1 if (self.nxt() >> 63) == 0 else 1


class Rng:
    """Подмена numpy default_rng в TestSignal: standard_normal и choice([-1, 1], m)."""

    def __init__(self, seed):
        self.g = Noise(seed)

    def standard_normal(self, n):
        return np.array([self.g.gauss() for _ in range(n)], np.float64)

    def choice(self, arr, m):
        assert list(arr) == [-1, 1]
        return np.array([self.g.sign() for _ in range(m)], np.int64)


def test_signal(fs, std="DK", seed=1):
    ts = testsignal.TestSignal(fs, std)
    ts.rng = Rng(seed)
    return ts


def r(v):
    return repr(float(v))


def arr(a):
    return " ".join(repr(float(v)) for v in np.asarray(a).ravel())


rows = []


def row(*f):
    rows.append("\t".join(str(x) for x in f))


# ---------------------------------------------------------------- стандарты и каналы
def gen_standards():
    for k, s in standards.STANDARDS.items():
        row("std", k, s.name, s.lines, r(s.line_freq), r(s.sound_offset), r(s.active_start_us), r(s.active_us),
            r(s.hsync_us), s.vis1, s.vis2, s.vis_count, s.n_broad, s.n_eq, r(s.tune_offset))
    for k, (name, std, _fn) in standards.PLANS.items():
        row("plan", k, name, std)
        for c in standards.channels(k):
            row("ch", k, c.name, r(c.video), r(c.center), r(c.bandwidth))


# ---------------------------------------------------------------- psd_db
def psd_input(n, seed):
    g = Noise(seed)
    i = np.arange(n)
    nr = np.array([g.gauss() for _ in range(n)])
    ni = np.array([g.gauss() for _ in range(n)])
    re = (0.01 * nr + 0.3 * np.cos(2 * math.pi * 0.1 * i) + 0.02).astype(np.float32)
    im = (0.01 * ni + 0.3 * np.sin(2 * math.pi * 0.1 * i) - 0.01).astype(np.float32)
    return re, im


def gen_psd():
    for n, nfft, seed in ((5000, 1024, 3), (20000, 4096, 4), (500, 1024, 5)):
        re, im = psd_input(n, seed)
        p = digital.psd_db((re + 1j * im).astype(np.complex64), nfft)
        row("psd", n, nfft, seed, arr(p))


# ---------------------------------------------------------------- OFDM: синтетика с циклическим префиксом
def ofdm_input(fs, mode, gi, n, noise, foff, dc, spur, seed):
    """Символы QPSK во временной области (полностью занятая полоса) + циклический префикс, уход частоты,
    шум, постоянная составляющая и паразитный тон (то, что убирает _prep). Все числа — Noise(seed)."""
    g = Noise(seed)
    lu = int(round(digital.FFT_MODES[mode] * fs))
    lg = int(round(lu * digital.GUARDS[gi])) if gi else 0
    s2 = math.sqrt(2.0)
    out = []
    have = 0
    while have < n:
        sym = np.empty(lu, np.complex128)
        for k in range(lu):
            a = g.sign()
            b = g.sign()
            sym[k] = complex(a / s2, b / s2)
        if gi:
            out.append(sym[-lg:])
        out.append(sym)
        have += lg + lu
    x = np.concatenate(out)[:n]
    t = np.arange(n)
    x = x * np.exp(2j * math.pi * foff / fs * t)
    nr = np.array([g.gauss() for _ in range(n)])
    ni = np.array([g.gauss() for _ in range(n)])
    x = x + noise * (nr + 1j * ni) + dc + spur[0] * np.exp(2j * math.pi * spur[1] / fs * t)
    return x.astype(np.complex64)


OFDM_CASES = [
    # имя, fs, режим, GI, n, шум, уход, DC, (тон: ампл., частота)
    ("t2_32k_1_16", 2.4e6, "32K", "1/16", 393216, 0.7, 310.0, 0.08 + 0.03j, (0.2, 412e3)),
    ("t_8k_1_4", 2.88e6, "8K", "1/4", 393216, 1.0, -950.0, 0.0, (0.0, 0.0)),
    ("t_2k_1_8", 2.4e6, "2K", "1/8", 300000, 0.5, 120.0, 0.05, (0.1, -300e3)),
    ("t2_16k_19_128", 2.88e6, "16K", "19/128", 393216, 0.8, 40.0, 0.0, (0.0, 0.0)),
    ("t2_32k_1_128", 2.4e6, "32K", "1/128", 393216, 0.3, 0.0, 0.0, (0.0, 0.0)),
    ("noise", 2.4e6, "8K", None, 393216, 1.0, 0.0, 0.03, (0.3, 250e3)),
    ("weak_8k_1_32", 2.4e6, "8K", "1/32", 393216, 3.0, 200.0, 0.0, (0.0, 0.0)),
]


def ofdm_row(tag, name, o):
    row(tag, name, o["found"], o["mode"], o.get("guard", "None"), r(o["rho"]), r(o.get("peak", float("nan"))),
        o["captures"], r(o.get("freq_offset_hz", float("nan"))))


def gen_ofdm():
    for name, fs, mode, gi, n, noise, foff, dc, spur in OFDM_CASES:
        x = ofdm_input(fs, mode, gi, n, noise, foff, dc, spur, len(name) * 7 + 1)
        xp = digital._prep(x)
        sc = digital.mode_scores(xp, fs)
        row("modes", name, " ".join(f"{k}={v!r}" for k, v in sc.items()))
        gs = digital.guard_scores(xp, fs, mode)
        row("guards", name, mode, " ".join(f"{k}={v[0]!r},{v[1]!r}" for k, v in gs.items()))
        ofdm_row("ofdm", name, digital.detect_ofdm(x, fs))
    # накопление по захватам: три захвата одного эфира (разные куски)
    est = digital.OfdmEstimator(2.4e6)
    for i in range(3):
        x = ofdm_input(2.4e6, "8K", "1/8", 262144, 1.5, 500.0, 0.0, (0.0, 0.0), 100 + i)
        ofdm_row("ofdm_acc", str(i), est.add(x))
    x = ofdm_input(2.4e6, "8K", None, 262144, 1.0, 0.0, 0.0, (0.0, 0.0), 200)
    ofdm_row("ofdm_acc", "3", est.add(x))


# ---------------------------------------------------------------- мультиплекс по тестовому эфиру
def gen_mux():
    fs = 2.4e6
    # план повторяется по кругу (как repeat=True в движке): итог второго круга уже несёт оценку OFDM
    for name, center, bw, sweeps, cycles in (("mux", 546e6, 8e6, 2, 2), ("empty", 650e6, 8e6, 1, 2),
                                             ("analog", 586e6, 8e6, 1, 1), ("edge", 543e6, 7e6, 1, 1)):
        ts = test_signal(fs)
        mux = digital.MuxAnalyzer(fs, center, bw)
        plan = mux.plan(True, sweeps)
        row("mux_plan", name, " ".join(f"{f!r}:{n}" for f, n in plan))
        res = []
        for f, n in plan * cycles:
            iq = u8_to_iq(ts.render(f, n, 30.0))
            res.append(mux.add(f, iq))
        nn = sum(1 for x in res if x is not None)
        last = [x for x in res if x is not None][-1]
        o = last["ofdm"]
        row("mux", name, nn, r(last["cn_db"]), r(last["level_db"]), r(last["flatness_db"]), last["present"],
            last["other"], last["psd"].size)
        row("mux_psd", name, arr(last["freqs"][::9]), arr(last["psd"][::9]))
        if o:
            ofdm_row("mux_ofdm", name, o)


# ---------------------------------------------------------------- аналоговое ТВ: видео, звук, поиск
def gen_analog():
    fs = 2.4e6
    std = standards.STANDARDS["DK"]
    ts = test_signal(fs)
    fc = testsignal.TEST_ANALOG_VC + std.tune_offset
    dec = AnalogVideoDecoder(fs, std)
    k = 0
    for b, n in enumerate([65536] * 3 + [40000, 100000] + [65536] * 4):
        iq = u8_to_iq(ts.render(fc, n, 30.0))
        for fr in dec.feed(iq):
            img = fr["image"]
            row("vframe", k, b, r(fr["hlock"]), fr["vlock"], r(fr["level_db"]), r(fr["snr_db"]), r(dec.period),
                r(float(img.astype(np.float64).mean())), arr(img[::16, ::16]))
            k += 1
    # звук
    ts = test_signal(fs)
    fc = testsignal.TEST_ANALOG_VC + std.sound_offset + SOUND_TUNE
    fm = FmSoundDecoder(fs, -SOUND_TUNE)
    outs = []
    for n in (65536, 30001, 7, 65536, 12345, 65536):
        outs.append(fm.process(u8_to_iq(ts.render(fc, n, 30.0))))
    a = np.concatenate(outs)
    row("fm", a.size, arr(a))


def gen_scan():
    fs = 2.4e6
    std = standards.STANDARDS["DK"]
    chans = standards.channels("RU")
    for kind, names in (("analog", ["33 (ДМВ)", "34 (ДМВ)", "35 (ДМВ)", "36 (ДМВ)"]),
                        ("digital", ["27 (ДМВ)", "28 (ДМВ)", "29 (ДМВ)", "30 (ДМВ)", "31 (ДМВ)", "40 (ДМВ)"])):
        ts = test_signal(fs, seed=5)
        sel = [c for c in chans if c.name in names]
        res = []
        for i, ch in enumerate(sel):
            f = ch.video + std.tune_offset if kind == "analog" else ch.center
            iq = u8_to_iq(ts.render(f, 65536, 30.0))
            p = digital.psd_db(iq, 4096)
            bins = (np.arange(4096) - 2048) * (fs / 4096)
            if kind == "analog":
                win = np.abs(bins - (-std.tune_offset)) < 60e3
                snr = float(p[win].max() - float(np.median(p)))
                res.append({"ch": ch, "value": snr, "found": snr > 15})
            else:
                win = (np.abs(bins) > 0.1e6) & (np.abs(bins) < 1.1e6)
                res.append({"ch": ch, "value": float(np.median(p[win])), "found": None})
                if i == len(sel) - 1:
                    vals = np.array([x["value"] for x in res])
                    ref = float(np.percentile(vals, 25))
                    for x in res:
                        x["value"] -= ref
                        x["found"] = x["value"] > 3.0
        for x in res:
            row("scan", kind, x["ch"].name, r(x["value"]), x["found"])


def main():
    gen_standards()
    gen_psd()
    gen_ofdm()
    gen_mux()
    gen_analog()
    gen_scan()
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# сгенерировано android/app/src/test/python/gen_tv_ref.py — не править руками\n")
        fh.write("\n".join(rows) + "\n")
    print(OUT, len(rows), "строк")


if __name__ == "__main__":
    main()
