"""Тесты сканера DMR (rtltv/dmrscan.py) и панели (rtltv/modes/dmr_scan_panel.py).

Запуск: py -3.12 tests/test_dmrscan.py   (или py -3.12 -m pytest tests/test_dmrscan.py)
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rtltv import dmr as D  # noqa: E402
from rtltv import dmrscan, radiotest, rtlsdr  # noqa: E402

FS = 2400000


def _fm_iq(freq48, offset, n, fs=FS, rng=None, amp=0.2):
    """Мгновенная частота @48 кГц -> комплексная ЧМ на fs со сдвигом offset."""
    t48 = np.arange(len(freq48)) / 48000
    t = np.arange(n) / fs
    f = np.interp(t % t48[-1], t48, freq48)
    ph = 2 * np.pi * np.cumsum(offset + f) / fs
    return amp * np.exp(1j * (ph + (rng.uniform(0, 6.28) if rng is not None else 0)))


def _fsk(rate, dev3, nsym, rng):
    """Случайная 4FSK без синхрослов: rate Бод, крайние уровни ±dev3."""
    sps = 48000 // rate
    lev = rng.choice([-3, -1, 1, 3], nsym) * (dev3 / 3)
    up = np.repeat(lev, sps).astype(np.float64)
    k = np.ones(sps // 2) / (sps // 2)
    return np.convolve(up, k, mode="same")


def _scan(sc, render, passes):
    for _ in range(passes):
        for i, (f, n) in enumerate(sc.plan_items()):
            sc.process(f, render(f, n), i)
    return {int(r["freq"]): r for r in sc.snapshot() if r["hits"]}


def test_plan():
    w = dmrscan.plan_windows(400e6, 470e6, FS)
    span = 0.8 * FS
    assert w[0] - span / 2 <= 400e6 + 10e3 and w[-1] + span / 2 >= 470e6 - 10e3
    assert all(abs(((c - dmrscan.DC_SHIFT) / dmrscan.GRID) - round((c - dmrscan.DC_SHIFT) / dmrscan.GRID)) < 1e-6
               for c in w)
    assert dmrscan.dwell_samples(2048000, 0.35) * 48000 % 2048000 == 0
    assert len(dmrscan.plan_windows(446.0e6, 446.2e6, FS)) == 1


def test_radiotest_pmr446():
    gen = radiotest.factory(FS)
    sc = dmrscan.DmrScanner(FS, 446.0e6, 446.2e6)
    t = time.perf_counter()
    rows = _scan(sc, lambda f, n: rtlsdr.u8_to_iq(gen.render(f, n, 30.0)), 10)
    dt = time.perf_counter() - t
    r = rows.get(446100000)
    assert r and r["kind"] == "dmr" and r["cc"] == [1] and r["mode"] == "BS", rows
    assert any("2501234" in s or "2507777" in s for s in r["ids"]), r["ids"]
    assert "голос" in r["types"]
    a = rows.get(446006250)
    assert a and a["kind"] == "analog", a
    print(f"  тестовый эфир PMR446: 10 проходов, обработка {sc.busy_s:.2f} с (всего с генератором {dt:.1f} с); "
          f"446,1: {dmrscan.describe(r)}; IDs {r['ids']}")


def test_classes():
    """Одно окно: DMR (MS, один слот), P25-подобная 4FSK 4800, 2400 Бод, аналог ЧМ, несущая, WFM."""
    rng = np.random.default_rng(1)
    lc = D._make_lc(7654321, 777, True)
    call = D.voice_call(5, lc, D._tone_frames(30), 3, fam=D.FAM_MS)
    bits = []
    for b in call:
        bits += list(rng.integers(0, 2, 24)) + list(b) + list(rng.integers(0, 2, 288))
    dmr_ms = D.modulate(bits)
    t48 = np.arange(48000 * 2) / 48000
    voice = 2500 * np.sin(2 * np.pi * 700 * t48) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t48))
    wfm = 60000 * np.sin(2 * np.pi * 1000 * t48)
    base = 439.0e6
    sig = {
        438.6e6: dmr_ms,
        438.75e6: _fsk(4800, 1800, 20000, rng),
        438.9e6: _fsk(2400, 1050, 10000, rng),
        439.1e6: voice,
        439.25e6: np.zeros(1000),
        439.5e6: wfm,
    }

    def render(f, nn):
        x = (rng.standard_normal(nn) + 1j * rng.standard_normal(nn)) * 0.003
        for fc, fm in sig.items():
            x += _fm_iq(fm, fc - f, nn, rng=rng)
        return x.astype(np.complex64)

    sc = dmrscan.DmrScanner(FS, base - 0.5e6, base + 0.6e6, dwell=0.6)
    assert len(sc.centers) == 1
    rows = _scan(sc, render, 3)
    kinds = {f: r["kind"] for f, r in rows.items()}
    assert kinds.get(438600000) == "dmr", kinds
    assert rows[438600000]["cc"] == [5] and rows[438600000]["mode"] == "MS"
    assert kinds.get(438750000) == "digital", kinds
    assert kinds.get(438900000) == "narrow", kinds
    assert kinds.get(439100000) == "analog", kinds
    assert kinds.get(439250000) == "carrier", kinds
    assert any(k == "wide" and 439.48e6 < f < 439.52e6 for f, k in kinds.items()), kinds
    print("  классы:", {f / 1e6: dmrscan.describe(r) for f, r in rows.items()})


def test_panel_with_engine():
    from PySide6.QtWidgets import QApplication, QScrollArea
    from rtltv.modes.dmr_scan_panel import DmrScanPanel
    from rtltv.modes.radio_window import RadioEngine

    app = QApplication.instance() or QApplication([])
    eng = RadioEngine()
    eng.fs = FS
    eng.center = 446.1e6
    eng.audio.start = lambda: None          # без звуковой карты
    eng.audio.stop = lambda: None
    panel = DmrScanPanel(eng)
    scroll = QScrollArea()
    scroll.setWidget(panel)
    scroll.setWidgetResizable(True)
    got = []
    panel.tuneRequested.connect(lambda f, m: got.append((f, m)))
    # без приёма — вежливый отказ
    panel.btn.click()
    assert not panel.scanning() and "включите приём" in panel.info.text()
    eng.start("test", 0, 300)
    try:
        t0 = time.time()
        while not eng.running() and time.time() - t0 < 3:
            app.processEvents()
            time.sleep(0.02)
        panel.band.setCurrentIndex(1)       # PMR446
        panel.btn.click()
        assert panel.scanning()
        t0 = time.time()
        found = False
        while time.time() - t0 < 25 and not found:
            app.processEvents()
            time.sleep(0.05)
            found = any(r["kind"] == "dmr" and abs(r["freq"] - 446.1e6) < 1 for r in panel._rows)
        assert found, panel._rows
        for w in (330, 380, 420):
            scroll.resize(w, 900)
            scroll.show()
            app.processEvents()
            assert panel.minimumSizeHint().width() <= w - 20, (w, panel.minimumSizeHint())
            vis = [c for c in panel.findChildren(QWidget_cls()) if c.isVisible() and c.parent() is panel.findChild(
                QGroupBox_cls())]
            for c in vis:
                g = c.geometry()
                assert g.right() <= panel.width(), (w, c, g)
            for i, a in enumerate(vis):
                for b in vis[i + 1:]:
                    if a.geometry().intersects(b.geometry()):
                        raise AssertionError(f"наложение при ширине {w}: {a} {b}")
        row = next(i for i in range(panel.table.rowCount())
                   if abs(panel.table.item(i, 0).data(256) - 446.1e6) < 1)
        panel._pick(row, 0)
        assert got and abs(got[-1][0] - 446.1e6) < 1 and got[-1][1] == "DMR", got
        assert not panel.scanning()          # выбор частоты останавливает сканер
        assert eng.plan_handler is None
    finally:
        panel.shutdown()
        eng.stop()


def QWidget_cls():
    from PySide6.QtWidgets import QWidget
    return QWidget


def QGroupBox_cls():
    from PySide6.QtWidgets import QGroupBox
    return QGroupBox


def main():
    tests = [(k, v) for k, v in globals().items() if k.startswith("test_") and callable(v)]
    bad = 0
    for name, fn in tests:
        t = time.perf_counter()
        try:
            fn()
            print(f"OK   {name} ({time.perf_counter() - t:.2f} с)")
        except Exception as e:  # noqa: BLE001
            bad += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {e!r}")
    print("Итог:", "все тесты прошли" if not bad else f"упало {bad} из {len(tests)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
