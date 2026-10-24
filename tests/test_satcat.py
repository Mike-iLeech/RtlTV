"""Тесты каталога спутников, доплера и шумоподавителя звука режима «Спутники»."""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtltv import satcat, satpass  # noqa: E402
from rtltv.radio import MODES  # noqa: E402
from rtltv.satrx import SpectralNR  # noqa: E402


def test_catalog_consistent():
    seen = set()
    for s in satcat.CATALOG:
        assert s.norad not in seen
        seen.add(s.norad)
        assert s.cat in satcat.CATEGORIES and s.txs
        for t in s.txs:
            assert 29e6 < t.freq < 2.5e9
            assert t.mode in ("LRPT", "LRIT", "IQ") or t.mode in MODES
            if t.is_band:
                assert t.mode in ("USB", "LSB") and t.hi - t.freq <= 100e3
    for cat in satcat.CATEGORIES:
        if cat != "other":                      # «Свои / другие» — только добавленные пользователем
            assert satcat.norads([cat])
    assert satcat.BY_NORAD[25544].txs[0].freq == 145.8e6


def test_doppler_sign_and_size():
    tles, _ = satpass.load_tle(offline=True)
    if 25544 not in tles:
        return
    o = satpass.Orbit(tles[25544])
    t = time.time()
    # для МКС на 437,8 МГц доплер не превышает ±10,5 кГц
    d = o.doppler(t + np.arange(0, 6000, 60), 437.8e6, 55.64, 37.39)
    assert np.nanmax(np.abs(d)) < 10.5e3
    # знак: приближается (скорость сближения < 0) → частота выше
    _az, _el, _rng, rr = o.look(t, 55.64, 37.39)
    assert np.sign(o.doppler(t, 437.8e6, 55.64, 37.39)) == -np.sign(rr)


def test_spectral_nr():
    fs = 48000
    rng = np.random.default_rng(0)
    t = np.arange(fs * 2) / fs
    # «речь»: тон 800 Гц пачками 0,3 с с паузами 0,2 с (стационарный тон NR честно считает шумом)
    tone = 0.3 * np.sin(2 * np.pi * 800 * t) * ((t % 0.5) < 0.3)
    noise = 0.05 * rng.standard_normal(t.size)
    nr = SpectralNR()
    out = np.concatenate([nr.process((tone + noise)[k:k + 4800]) for k in range(0, t.size, 4800)])
    d = 0                                           # задержки нет: выдаём уже сложенную половину окна
    ref = tone[: out.size - d]
    y = out[d:]
    seg = slice(fs * 3 // 4, None)                  # после установления оценки шума
    err_in = np.std(noise[: out.size - d][seg])
    err_out = np.std(y[seg] - ref[seg])
    assert err_out < err_in * 0.8
    assert abs(np.std(y[seg]) / np.std(ref[seg]) - 1) < 0.25


def test_user_sats_roundtrip():
    s = satcat.Sat(99999, "Мой кубсат", "other", "#fff",
                   [satcat.Tx("Маяк", 437.1e6, "CW"), satcat.Tx("Данные", 437.2e6, "IQ", decoder="iq"),
                    satcat.Tx("Снимки", 137.1e6, "LRPT")], tle=("1 x", "2 y"))
    d = s.to_dict()
    s2 = satcat.Sat.from_dict(d)
    assert s2.norad == 99999 and s2.user and s2.tle == ("1 x", "2 y")
    assert [t.kind for t in s2.txs] == ["voice", "iq", "lrpt"]
    assert [t.listenable for t in s2.txs] == [True, False, False]
    n0 = len(satcat.CATALOG)
    satcat.set_user([d])
    try:
        assert len(satcat.CATALOG) == n0 + 1 and 99999 in satcat.BY_NORAD
        assert 99999 in satcat.norads()
    finally:
        satcat.set_user([])
    assert len(satcat.CATALOG) == n0 and 99999 not in satcat.BY_NORAD
    # «Электро-Л» — в общем списке приёма, но не в каталоге пролётов
    assert 67756 in {x.norad for x in satcat.all_sats()} and 67756 not in satcat.norads()


def test_gallery_meta(tmp_path):
    from rtltv.modes import sat_gallery as G
    p = tmp_path / "meteor_M2-4_20261024_101500_rgb221.png"
    p.write_bytes(b"\x89PNG")
    G.write_meta(str(p), dict(sat="Метеор-М №2-4", freq=137.9e6, modulation="LRPT OQPSK", symbol_rate=72000,
                              frames=900, frames_ok=880, rs_pct=97.8, lines=1200, product="композит 221",
                              source="live"))
    rows = G.scan({"meteor": str(tmp_path)})
    assert len(rows) == 1
    m = rows[0][2]
    assert m["sat"] == "Метеор-М №2-4" and "saved" in m
    txt = G.tech_text(m)
    assert "LRPT" in txt and "880/900" in txt and "1200 строк" in txt
    # без JSON — метаданные из имени файла
    q = tmp_path / "meteor_M2-3_20261024_101500_ch1.png"
    q.write_bytes(b"\x89PNG")
    m2 = {r[0]: r[2] for r in G.scan({"meteor": str(tmp_path)})}[str(q)]
    assert m2["sat"] == "Метеор-М №2-3" and m2["product"] == "ch1"


def test_spec_pacer_even():
    from rtltv.satrx import SpecPacer
    sp = SpecPacer(fps=50)
    fs = 1024000
    iq = (np.random.default_rng(0).standard_normal(131072) * (1 + 0j)).astype(np.complex64)
    t0 = 1000.0
    sp.push(iq, 137.7e6, fs, 4096, t0)
    times = [x[0] for x in sp.q]
    assert len(times) == 6                                   # 128 мс блока → 6 кадров
    assert np.allclose(np.diff(times), 0.128 / 6, atol=1e-3)
    assert sp.take(t0) is not None and sp.take(t0)[0] == 1   # в момент t0 — только первый
    assert sp.take(t0 + 0.2)[0] == 6                         # позже — последний наступивший


def test_orbit_track_full_revolution():
    tles, _ = satpass.load_tle(offline=True)
    if 25544 not in tles:
        return
    o = satpass.Orbit(tles[25544])
    assert 88 < o.period_min < 96
    pts = o.orbit_track(time.time(), 1.0, 1.0, 240)
    lons = np.array([p[1] for p in pts])
    assert len(pts) > 200 and (lons.max() - lons.min()) > 300  # два витка обходят всю долготу
