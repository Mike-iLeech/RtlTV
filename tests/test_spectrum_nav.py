import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QSlider

from rtltv import bandplan
from rtltv.spectrum_nav import BandOverview, SpectrumSideBar
from rtltv.widgets import SpectrumControls, SpectrumView


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _mouse(w, kind, x, y, buttons=Qt.LeftButton):
    btn = Qt.LeftButton if kind != QMouseEvent.MouseMove else Qt.NoButton
    e = QMouseEvent(kind, QPointF(x, y), w.mapToGlobal(QPointF(x, y)), btn, buttons, Qt.NoModifier)
    {QMouseEvent.MouseButtonPress: w.mousePressEvent, QMouseEvent.MouseMove: w.mouseMoveEvent,
     QMouseEvent.MouseButtonRelease: w.mouseReleaseEvent}[kind](e)


def test_wavelength_and_classes():
    assert bandplan.fmt_wavelength(100e6) == "3,00 м"
    assert bandplan.fmt_wavelength(1090e6) == "28 см"
    assert bandplan.fmt_wavelength(200e3) == "1,5 км"
    assert bandplan.wave_class(145e6)[0] == "метровые"
    assert bandplan.wave_class(433e6)[0] == "дециметровые"
    assert bandplan.wave_class(7e6)[0] == "декаметровые"
    names = [a[2] for a in bandplan.at(101e6)]
    assert "FM" in names
    assert bandplan.at(446.1e6)[0][2] == "PMR446"          # сначала самое узкое
    assert "λ" in bandplan.describe(137.9e6) and "Метеор" in bandplan.describe(137.9e6)


def test_lanes_do_not_overlap():
    for lane in {i for i, _ in bandplan.lanes(bandplan.ALLOCATIONS)}:
        items = sorted(a for i, a in bandplan.lanes(bandplan.ALLOCATIONS) if i == lane)
        for a, b in zip(items, items[1:]):
            assert a[1] <= b[0]


def test_log_scale_roundtrip():
    for f in (0.5e6, 7e6, 145e6, 1090e6):
        assert bandplan.log_freq(bandplan.log_pos(f, 0.1e6, 2e9), 0.1e6, 2e9) == pytest.approx(f, rel=1e-9)


def test_overview_click_jumps(app):
    ov = BandOverview()
    ov.resize(800, 64)
    got = []
    ov.jumpRequested.connect(got.append)
    x = ov._x(145e6)
    _mouse(ov, QMouseEvent.MouseButtonPress, x, 30)
    _mouse(ov, QMouseEvent.MouseButtonRelease, x, 30, Qt.NoButton)
    assert got and got[0] == pytest.approx(145e6, rel=0.01)
    _mouse(ov, QMouseEvent.MouseButtonPress, 1, 30)            # левее диапазона приёмника — край 0,5 МГц
    assert got[-1] == BandOverview.RX_LO
    ov.set_view(100e6, 102e6, 101e6)
    ov.grab()                                                   # рисование без ошибок


def _view(app):
    v = SpectrumView(waterfall=True)
    v.resize(800, 500)
    freqs = np.linspace(100e6, 102e6, 2048)
    psd = np.random.default_rng(0).normal(-70, 2, 2048)
    for _ in range(3):
        v._apply(freqs, psd)
    return v


def test_split_drag(app):
    v = _view(app)
    r = v._plot_rect()
    y = r.bottom() + 10
    assert v._split_hit(QPointF(300, y))
    hist = v.wf.copy()
    got = []
    tuned = []
    v.splitChanged.connect(got.append)
    v.tuneRequested.connect(tuned.append)
    _mouse(v, QMouseEvent.MouseButtonPress, 300, y)
    _mouse(v, QMouseEvent.MouseMove, 300, 150, Qt.LeftButton)
    _mouse(v, QMouseEvent.MouseButtonRelease, 300, 150, Qt.NoButton)
    assert got and got[0] == pytest.approx((150 + 8) / 500, abs=0.01)
    assert not tuned                                            # тянули границу — не перестройка
    # под курсором граница теперь там, куда её отпустили
    assert abs(v._plot_rect().bottom() + 10 - 150) < 2
    v._apply(v.freqs, v.psd)
    assert v.wf is not None and v.wf.shape[1] == hist.shape[1]   # водопад не сброшен
    v.split = 0.55


def test_range_db_and_alloc(app):
    v = _view(app)
    v.auto_range = False
    v.man_lo, v.man_hi = -100, 0
    v.range_db = 40
    v._apply(v.freqs, v.psd)
    assert v.hi - v.lo == pytest.approx(40)
    v.show_alloc = True
    v.grab()


def test_sidebar_mirrors_sliders(app, tmp_path, monkeypatch):
    v = _view(app)
    ctl = SpectrumControls([v], key="t_nav", extended=True)
    master = QSlider(Qt.Horizontal)
    master.setRange(0, 60)
    bar = SpectrumSideBar([("Зум", master, str, ""), ("Диап.", ctl.range, str, "")])
    bar.knobs[0].s.setValue(30)
    assert master.value() == 30
    master.setValue(10)
    assert bar.knobs[0].s.value() == 10 and bar.knobs[0].val.text() == "10"
    bar.knobs[1].s.setValue(50)
    assert v.range_db == 50 and ctl.st["range"] == 50
    v.splitChanged.emit(0.4)                                    # перетащили границу — ползунок «Высота» следом
    assert ctl.split.value() == 40 and ctl.st["split"] == 40


def test_range_applies_immediately(app):
    v = _view(app)
    v.auto_range = True
    v.range_db = 0
    v._apply(v.freqs, v.psd)
    v.range_db = 40
    v.refresh_range()                       # без нового кадра спектра
    assert v.hi - v.lo == pytest.approx(40)
    lo_before = v.lo
    v.range_db = 90
    v.refresh_range()
    assert v.hi - v.lo == pytest.approx(90) and v.lo == pytest.approx(lo_before, abs=3)


def test_waterfall_lens(app):
    v = _view(app)
    for _ in range(40):
        v._apply(v.freqs, v.psd)
    wr = v._wf_rect()
    pos = QPointF(wr.left() + wr.width() * 0.25, wr.top() + 10)
    f_before = v._f_at(pos)
    for _ in range(5):
        v._wf_zoom(pos, 1, vertical=False)
    v._wf_zoom(pos, 1, vertical=True)
    assert v.wf_zoomed() and v.wf_zx > 2 and v.wf_zy > 1
    assert v._f_at(pos) == pytest.approx(f_before, rel=1e-4)      # точка под курсором на месте
    src = v._wf_src()
    assert src.left() >= 0 and src.right() <= v.wf_img.width() + 1e-6
    v._wf_pan(40, 0)
    assert v._wf_src().left() <= src.left()
    v.grab()
    v.wf_reset()
    assert not v.wf_zoomed()


def test_controls_flush_on_hide(app):
    from rtltv import settings
    v = _view(app)
    ctl = SpectrumControls([v], key="t_flush", extended=True)
    ctl.show()
    ctl.range.setValue(77)
    assert ctl._save_timer.isActive()
    ctl.hide()                                   # закрытие окна раньше 0,4 с — значение не теряется
    assert not ctl._save_timer.isActive()
    assert settings.load("app", {})["spectrum"]["t_flush"]["range"] == 77


def test_lens_pan_clamped(app):
    v = _view(app)
    for _ in range(30):
        v._apply(v.freqs, v.psd)
    wr = v._wf_rect()
    for _ in range(4):
        v._wf_zoom(wr.center(), 1, vertical=False)
    for _ in range(50):
        v._wf_pan(-100, 0)                       # далеко за правый край
    left = v._wf_src().left()
    v._wf_pan(100, 0)
    assert v._wf_src().left() < left             # обратно едет сразу, без «мёртвой зоны»
