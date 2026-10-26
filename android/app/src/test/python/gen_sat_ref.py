"""Эталоны для SatTest.kt: считает их пакетом sgp4 (ускоренный Satrec, тот, что использует rtltv/satpass.py),
модулями rtltv/satpass.py, rtltv/satrx.py и rtltv/elektro.py и пишет ресурс теста sat/sat_ref.tsv.

Запуск из корня репозитория:  PYTHONUTF8=1 py -3.12 android/app/src/test/python/gen_sat_ref.py
Нужны пакеты sgp4 (2.x), numpy, scipy. Сеть не нужна: TLE берутся из ресурса sat/tle_fixture.txt
(снимок кэша celestrak 26.10.2026) и из проверочного набора Vallado SGP4-VER.TLE, который входит в пакет sgp4.

Строки ресурса (поля через табуляцию, числа — repr Python, то есть точное значение double):
  VER  l1 l2                         набор SGP4-VER;  V tsince err x y z vx vy vz  — TEME, км и км/с
  T    norad t err  teme(6) ecef(6) sub(3) look(4) doppler   — по TLE из tle_fixture.txt
  PASSES name lat lon h t0 hours min_el;  P norad aos los tca max_el az_aos az_los az_tca
  GMST, GEOD, VIS, TRACK, PERIOD, EPOCH, GEOLOOK — геометрия;  NR*, IQ*, SP* — satrx.
"""
import math
import os
import sys

import numpy as np
from sgp4.api import Satrec

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), *[".."] * 5))
sys.path.insert(0, ROOT)
from rtltv import elektro, satcat, satpass  # noqa: E402
from rtltv.satrx import IqReceiver, SpecPacer, SpectralNR  # noqa: E402

RES = os.path.join(ROOT, "android", "app", "src", "test", "resources", "sat")
OUT = os.path.join(RES, "sat_ref.tsv")
VER = os.path.join(os.path.dirname(__import__("sgp4").__file__), "SGP4-VER.TLE")

OBSERVERS = [("Москва", 55.7558, 37.6173, 0.15), ("Новосибирск", 55.0302, 82.9204, 0.16),
             ("Кейптаун", -33.9249, 18.4241, 0.0)]
T0 = 1792972800.0        # 2026-10-26 00:00:00 UTC

lines = []


def w(*xs):
    lines.append("\t".join(x if isinstance(x, str) else repr(float(x)) if isinstance(x, (float, np.floating))
                           else str(x) for x in xs))


# ---------------------------------------------------------------- SGP4-VER (Vallado)
def ver():
    rows = [ln.rstrip("\n") for ln in open(VER, encoding="ascii") if not ln.startswith("#") and ln.strip()]
    for i in range(0, len(rows) - 1, 2):
        l1, l2 = rows[i], rows[i + 1]
        if not (l1.startswith("1 ") and l2.startswith("2 ")):
            continue
        parts = l2[69:].split()
        start, stop, step = (float(x) for x in parts[:3])
        s = Satrec.twoline2rv(l1[:69], l2[:69])
        w("VER", l1[:69], l2[:69])
        ts = [0.0] + list(np.arange(start, stop + 1e-9, step))
        if ts[-1] != stop:
            ts.append(stop)
        for t in ts[:200]:
            e, r, v = s.sgp4_tsince(float(t))
            w("V", float(t), int(e), *r, *v)


# ---------------------------------------------------------------- положения по TLE из кэша
def fixture():
    text = open(os.path.join(RES, "tle_fixture.txt"), encoding="ascii").read()
    tles = satpass.parse_tle(text)
    w("PARSED", len(tles), *[f"{n}:{t.name}" for n, t in tles.items()])
    name, lat, lon, h = OBSERVERS[0]
    freqs = {s.norad: s.txs[0].center for s in satcat.all_sats()}
    rng = np.random.default_rng(20261026)
    for n, tle in tles.items():
        o = satpass.Orbit(tle)
        ep = tle.epoch_unix
        w("EPOCH", n, ep)
        w("PERIOD", n, o.period_min)
        ts = np.concatenate([ep + np.arange(-86400.0, 86400.0 + 1, 3600.0), T0 + rng.uniform(0, 86400, 23)])
        f = freqs.get(n, 137.9e6)
        jd, fr = satpass.jd_from_unix(ts)
        e, rt, vt = o.sat.sgp4_array(jd, fr)
        re, ve = o.ecef(ts)
        la, lo, hh = satpass.ecef_to_geodetic(re[0], re[1], re[2])
        az, el, rg, rr = o.look(ts, lat, lon, h)
        dop = o.doppler(ts, f, lat, lon, h)
        for k, t in enumerate(ts):
            w("T", n, float(t), int(e[k]), *rt[k], *vt[k], *re[:, k], *ve[:, k], la[k], lo[k], hh[k],
              az[k], el[k], rg[k], rr[k], f, dop[k])
        tr = o.orbit_track(T0, 1.0, 1.0, 120)
        w("TRACK", n, T0, len(tr), *[c for p in (tr[:3] + tr[-3:]) for c in p])
    return tles


def passes(tles):
    leo = [satpass.Orbit(t) for n, t in tles.items() if n not in satcat.GEO_NORADS]
    for (name, lat, lon, h), min_el in zip(OBSERVERS, (0.0, 10.0, 0.0)):
        w("PASSES", name, lat, lon, h, T0, 24.0, min_el)
        for o in leo:
            for p in o.passes(lat, lon, T0, 24.0, min_el, h):
                w("P", p.norad, p.aos, p.los, p.tca, p.max_el, p.az_aos, p.az_los, p.az_tca)
    name, lat, lon, h = OBSERVERS[0]
    allp = satpass.all_passes(leo, lat, lon, T0, 36.0, 0.0)
    w("ALLPASS", lat, lon, T0, 36.0, len(allp), *[f"{p.norad}:{p.aos!r}" for p in allp])


def geometry():
    for t in (T0, T0 + 12345.678, 946728000.0, 1700000000.5):
        jd, fr = satpass.jd_from_unix(t)
        w("GMST", t, float(jd), float(fr), float(satpass.gmst(jd, fr)))
    for la, lo, hh in ((55.75, 37.62, 0.15), (-33.9, 18.4, 0.0), (89.9, -170.0, 3.0), (0.0, 0.0, 400.0)):
        x, y, z = satpass.geodetic_to_ecef(la, lo, hh)
        b = satpass.ecef_to_geodetic(x, y, z)
        w("GEOD", la, lo, hh, x, y, z, *[float(v) for v in b])
    for la, lo, alt, mn in ((55.75, 37.62, 830.0, 0.0), (60.0, 170.0, 420.0, 10.0)):
        pts = satpass.visibility_circle(la, lo, alt, mn, 24)
        w("VIS", la, lo, alt, mn, 24, *[c for p in pts for c in p])
    for la, lo, sl in ((55.75, 37.62, 76.0), (55.75, 37.62, -14.5), (43.1, 131.9, 165.8)):
        w("GEOLOOK", la, lo, sl, *elektro.geo_look(la, lo, sl))


# ---------------------------------------------------------------- satrx
class Lcg:
    """64-битный ЛКГ Кнута; отсчёт — целое −1000…1000 / 2048 (точно представимо во float32)."""

    def __init__(self, seed):
        self.x = seed

    def nxt(self):
        self.x = (self.x * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
        return ((self.x >> 33) % 2001 - 1000) / 2048.0


TONE = [0.0, 0.375, 0.6875, 0.875, 0.9375, 0.875, 0.6875, 0.375, 0.0, -0.375, -0.6875, -0.875, -0.9375, -0.875,
        -0.6875, -0.375]


def nr():
    g = Lcg(1)
    x = np.array([0.05 * g.nxt() + (0.25 * TONE[i % 16] if (i // 4000) % 2 == 0 else 0.0) for i in range(16000)],
                 np.float32)
    chunks = [100, 1000, 37, 2000, 511, 512, 513, 4000, 3000, 4327]
    nrx = SpectralNR()
    p = 0
    for c in chunks:
        y = nrx.process(x[p:p + c])
        p += c
        w("NR", c, len(y), *[str(np.float32(v)) for v in y])


def lcg_iq(seed, n):
    g = Lcg(seed)
    re = np.empty(n, np.float32)
    im = np.empty(n, np.float32)
    for i in range(n):
        re[i] = g.nxt() * 0.5 + 0.25 * TONE[i % 16]
        im[i] = g.nxt() * 0.5 + 0.25 * TONE[(i + 4) % 16]
    return (re + 1j * im).astype(np.complex64)


def iqrx():
    fs, bw = 240000.0, 15000.0
    rx = IqReceiver(fs, bw)
    w("IQH", fs, bw, rx.dec, *[float(v) for v in rx.h])
    for blk, (seed, n, off) in enumerate(((2, 20000, 0.0), (3, 24000, 12345.0), (4, 9000, -15000.0))):
        rx.set_offset(off)
        rx.feed(lcg_iq(seed, n))
        c = rx.last_cons
        w("IQ", seed, n, off, rx.level_db, rx.noise_db, c.shape[0], *[float(v) for v in c[:5].ravel()])


def specpacer():
    sp = SpecPacer()
    fs, center, nfft = 240000.0, 137.5e6, 1024
    for blk, (seed, n, now) in enumerate(((5, 24000, 1000.0), (6, 12000, 1000.02), (7, 36000, 1000.5))):
        sp.push(lcg_iq(seed, n), center, fs, nfft, now)
    w("SPN", sp.n, sp.last_rel)
    for i, (t, k, f, p) in enumerate(sp.q):
        w("SP", t, k, float(f[0]), float(f[1]), float(f[-1]), *([str(np.float32(v)) for v in p] if i < 3 else []))
    for now in (999.0, 1000.05, 1000.3, 1001.0):
        s = sp.take(now)
        w("SPTAKE", now, s[0] if s else 0)


def main():
    ver()
    tles = fixture()
    passes(tles)
    geometry()
    nr()
    iqrx()
    specpacer()
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print(OUT, len(lines), "строк")


if __name__ == "__main__":
    main()
