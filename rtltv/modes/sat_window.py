"""Режим «Спутники»: пролёты над домом на карте, приём с любого спутника (LRPT «Метеоров», LRIT «Электро-Л»,
запись IQ остальных), галерея снимков, прослушивание любительских спутников и МКС с коррекцией доплера.

Вкладки: «Карта», «Приём со спутника», «Снимки со спутников», «Слушать», «Электро-Л».
Таблица пролётов: фильтр по категориям, у каждой строки кнопки «Принять» и «Слушать».
Свои спутники (NORAD/TLE, передатчики) — кнопка «Свои спутники…» в карточке «Дом».
"""
import os
import re
import sys
import threading
import time

import numpy as np
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QImage
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QListWidget, QMenu, QProgressBar, QPushButton, QSlider, QSpinBox, QSplitter,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from .. import devices, elektro, lrpt, satcat, satpass, settings
from ..mapview import MapView
from ..modebase import ModeWindow
from ..radiotest import RadioTestSignal, Station
from ..satrx import FileDecoder, IqReceiver, NrDemod, RxWorker, SpecPacer, SynthSource
from ..source import R828D_GAINS
from ..widgets import LevelMeter, SpectrumControls, SpectrumView
from .radio_window import RadioEngine
from .sat_gallery import DIRS as GALLERY_DIRS
from .sat_gallery import GalleryWidget, write_meta
from .sat_userdlg import UserSatsDialog
from .sat_widgets import ConstellationView, FileDialog, ImageScroll, scroll_panel, wrap_label
from .. import paths


class _ListenDemo(RadioTestSignal):
    """Демо для «Слушать»: в эфире только выбранный спутник; его частоту (с доплером) двигает окно."""

    KIND = {"NFM": "nfm", "AM": "am", "USB": "cw", "LSB": "cw", "CW": "cw", "WFM": "wfm"}

    def __init__(self, fs, mode, freq):
        super().__init__(fs)
        self.sat = Station(freq, self.KIND.get(mode, "nfm"), 0.12)
        self.stations = [self.sat]


HOME_DEFAULT = (55.7558, 37.6173)  # Москва
METEORS = (57166, 59051)
ISS = 25544
IF_OFFSET = 200e3                  # настройка ниже сигнала, чтобы уйти от пика постоянной составляющей
RATES = [1024000, 1200000, 1440000, 2048000]
LISTEN_RATE = 960000               # кратно 48 кГц (RadioDemod)
LISTEN_IF = 120e3
DEFAULTS = {
    "source": "rtl:0", "gain": 402, "ppm": 0, "bias": False, "rate": 1024000,
    "home_lat": HOME_DEFAULT[0], "home_lon": HOME_DEFAULT[1], "min_el": 20.0, "sat": 59051, "tx": 0,
    "doppler": True, "record": False, "zoom": 3, "combo": "221",
    "cats": ["meteo", "iss", "fm", "ssb", "tlm", "other"], "volume": 60, "squelch": -120, "nr": True,
    "e_sat": 67756, "e_bias": False, "user_sats": [],
}
PIC_DIR = GALLERY_DIRS["meteor"]
ELEKTRO_DIR = GALLERY_DIRS["elektro"]
IQ_DIR = paths.iq_dir()
COLS = ["Спутник", "Действия", "Начало", "Конец", "Длит.", "Макс.", "Азимуты", "Частота, режим", "Статус"]
COMBOS = [("221", "221 — естественные цвета"), ("125", "125 — ИК в синем"), ("123", "123 — видимые"),
          ("1", "Канал 1 (0,5–0,7 мкм)"), ("2", "Канал 2 (0,7–1,1 мкм)"), ("3", "Канал 3 (1,6–1,8 мкм)"),
          ("4", "Канал 4 (3,5–4,1 мкм)"), ("5", "Канал 5 (10,5–11,5 мкм)"), ("6", "Канал 6 (11,5–12,5 мкм)")]
TAB_MAP, TAB_RX, TAB_IMG, TAB_LISTEN, TAB_ELEKTRO = range(5)
KIND_TEXT = {"lrpt": "декодер LRPT (72k OQPSK или 80k с перемежением — режим определяется сам) → снимки МСУ-МР", "lrit": "декодер LRIT (BPSK 294k → снимки МСУ-ГС)",
             "iq": "без декодера: спектр, созвездие, запись IQ", "voice": "без декодера: спектр, созвездие, запись IQ "
             "(звук — вкладка «Слушать»)"}


def _fmt_t(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


def _fmt_d(t):
    lt = time.localtime(t)
    today = time.localtime()
    if lt.tm_yday == today.tm_yday and lt.tm_year == today.tm_year:
        return time.strftime("%H:%M", lt)
    return time.strftime("%d.%m %H:%M", lt)


def _fmt_dt(sec):
    sec = int(max(0, sec))
    if sec >= 3600:
        return f"{sec // 3600} ч {sec % 3600 // 60:02d} мин"
    return f"{sec // 60}:{sec % 60:02d}"


def _mhz(f):
    return f"{f / 1e6:.3f}".replace(".", ",")


def _tx_text(t):
    rng = f"{_mhz(t.freq)}–{_mhz(t.hi)}" if t.is_band else _mhz(t.freq)
    return f"{t.label}: {rng} МГц, {t.mode}"


class _Bridge(QObject):
    event = Signal(str, object)


class _Twin:
    """Несколько кнопок с одним состоянием (карточка справа и вкладка «Приём со спутника»):
    любой вызов (setChecked, setText, setEnabled, …) применяется ко всем, чтение — с первой."""

    def __init__(self, *buttons):
        self.buttons = list(buttons)

    def __getattr__(self, name):
        attr = getattr(self.buttons[0], name)
        if not callable(attr) or name.startswith(("is", "text", "toolTip")):
            return attr

        def call(*a, **kw):
            res = None
            for b in self.buttons:
                b.blockSignals(name == "setChecked")
                res = getattr(b, name)(*a, **kw)
                b.blockSignals(False)
            return res
        return call


class SatRadioEngine(RadioEngine):
    """RadioEngine для «Слушать»: спектр не одним кадром на блок, а равномерно (SpecPacer, до 30 к/с);
    интерфейс забирает последний кадр таймером — сигналов с массивами в очереди не копится."""

    def __init__(self):
        super().__init__()
        self.pacer = SpecPacer()

    def _stream(self, freq, iq):
        self._last_spec = time.perf_counter()      # родительский спектр (сигнал на каждый блок) не нужен
        super()._stream(freq, iq)
        if abs(freq - self.center) <= 1:
            self.pacer.push(iq, freq, self.fs, int(self.nfft))


class SatWindow(ModeWindow):
    tleLoaded = Signal(object, str)

    def __init__(self, opts=None):
        super().__init__(opts, "SDR4NOOB — Спутники: пролёты, приём со спутников, снимки, прослушивание")
        # потоки приёма — на Python: чаще отдаём GIL потоку интерфейса (по умолчанию 5 мс — заметные рывки)
        self._switch = sys.getswitchinterval()
        sys.setswitchinterval(0.001)
        self.s = settings.load("sat", DEFAULTS)
        for k, v in DEFAULTS.items():
            self.s.setdefault(k, v)
        if (self.s["home_lat"], self.s["home_lon"]) == HOME_DEFAULT:
            # дом ещё не задавали здесь — берём из режима ADS-B, если он там указан
            other = settings.load("adsb", {})
            if "home_lat" in other and "home_lon" in other:
                self.s["home_lat"], self.s["home_lon"] = float(other["home_lat"]), float(other["home_lon"])
        satcat.set_user(self.s.get("user_sats") or [])
        # в режиме снимка окна (автотест) снимки не сохраняем, если папка не задана явно
        self.pic_dir = self.opts.get("pic_dir") or (None if self.opts.get("shot") else PIC_DIR)
        if self.opts.get("source"):
            self.s["source"] = self.opts["source"]
        self.tles = {}
        self.orbits = {}
        self.eorbits = {}
        self.passes = []
        self.src = None              # источник «Приёма со спутника»
        self.worker = None
        self.rx = None               # dict: sat, tx, kind, freq, fs, center, stamp, auto
        self.filedec = None
        self.file_kind = None
        self.auto_pass = None
        self.images = {}
        self.images_meta = {}
        self.images_src = ""
        self._img_ver = -1
        self._e_ver = -1
        self._spec_seq = 0
        self._lspec_seq = 0
        self.rx_nfft = 4096
        self.gains = list(R828D_GAINS)
        self.engine = SatRadioEngine()          # прослушивание; меню «Звук» берёт self.engine.audio
        self.engine.fs = LISTEN_RATE
        self.engine.demod = NrDemod(LISTEN_RATE, "NFM")
        self.listen = None                      # {"sat", "tx", "center", "vfo"}
        self.erx = None                         # LritReceiver (последний результат)
        self.bridge = _Bridge()
        self.bridge.event.connect(self._on_event)
        self.tleLoaded.connect(self._on_tle)
        self._build()
        self.engine.level.connect(lambda db, _a, _s: self.l_level.set_value(db, f"{db:.0f} dB"))
        self.engine.error.connect(lambda m: self.statusBar().showMessage("Приёмник: " + m, 10000))
        self.engine.opened.connect(lambda info: self.statusBar().showMessage(
            f"Открыт: {info['label']} · тюнер {info['tuner']}", 6000))
        self._apply_home(save=False)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._ui_update)
        self.ui_timer.start(200)                # 5 раз/с: доплер прослушивания, индикаторы
        self.spec_timer = QTimer(self)
        self.spec_timer.setTimerType(Qt.PreciseTimer)
        self.spec_timer.timeout.connect(self._spec_update)
        self.spec_timer.start(15)               # спектр/созвездие: только последний кадр; рисует виджет сам
        self._load_tle(force=False)
        if self.opts.get("file"):
            QTimer.singleShot(300, lambda: self._decode_path(self.opts["file"], None, None, None, "lrpt"))
        if self.opts.get("efile"):
            QTimer.singleShot(300, lambda: self._decode_path(self.opts["efile"], None, None, None, "lrit"))
        if self.opts.get("rx"):
            QTimer.singleShot(500, lambda: self.start_rx(int(self.opts["rx"]) if str(self.opts["rx"]).isdigit()
                                                         and int(self.opts["rx"]) > 1 else None))
        if self.opts.get("erx"):
            QTimer.singleShot(500, self._elektro_toggle)
        if self.opts.get("listen"):
            QTimer.singleShot(1500, lambda: self.start_listen(int(self.opts["listen"])))
        if self.opts.get("tab") is not None:
            self.tabs.setCurrentIndex(int(self.opts["tab"]))
        if self.opts.get("auto"):
            QTimer.singleShot(800, lambda: self.btn_auto.setChecked(True))

    # ================= интерфейс =================
    def _build(self):
        self.map = MapView()
        self.map.set_center(self.s["home_lat"], self.s["home_lon"], int(self.s.get("zoom", 3)))
        self.map.clicked.connect(self._map_clicked)
        self.map.itemClicked.connect(self._sat_clicked)
        self.map_lbl = QLabel("Орбиты: загрузка TLE…", self.map)   # подпись источника данных поверх карты
        self.map_lbl.move(8, 8)
        self.map_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.map, "Карта")
        self.tabs.addTab(self._tab_rx(), "Приём со спутника")
        self.tabs.addTab(self._tab_images(), "Снимки со спутников")
        self.tabs.addTab(self._tab_listen(), "Слушать")
        self.tabs.addTab(self._tab_elektro(), "Электро-Л")
        self.tabs.currentChanged.connect(self._tab_changed)

        # фильтр категорий + таблица пролётов
        tw = QWidget()
        tv = QVBoxLayout(tw)
        tv.setContentsMargins(0, 0, 0, 0)
        fh = QHBoxLayout()
        fh.addWidget(QLabel("Показывать:"))
        self.cat_cb = {}
        for k, name in satcat.CATEGORIES.items():
            cb = QCheckBox(name)
            cb.setChecked(k in self.s.get("cats", []))
            cb.toggled.connect(self._cats_changed)
            self.cat_cb[k] = cb
            fh.addWidget(cb)
        fh.addStretch(1)
        tv.addLayout(fh)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)  # ResizeToContents пересчитывает всё на каждую ячейку
        hh.setStretchLastSection(True)
        self._table_n = -1
        self.table.cellClicked.connect(self._pass_clicked)
        self.table.setMinimumHeight(120)
        tv.addWidget(self.table, 1)

        left = QSplitter(Qt.Vertical)
        left.addWidget(self.tabs)
        left.addWidget(tw)
        left.setStretchFactor(0, 3)
        left.setStretchFactor(1, 2)
        left.setChildrenCollapsible(False)

        self.side = scroll_panel([self._grp_home(), self._grp_rx(), self._grp_actions()], 290, 380)
        for b in self._tab_btns:
            self._tab_btn_row.addWidget(b)
        self._tab_btn_row.addStretch(1)

        main = QSplitter(Qt.Horizontal)
        main.addWidget(left)
        main.addWidget(self.side)
        main.setStretchFactor(0, 1)
        main.setChildrenCollapsible(False)
        self.setCentralWidget(main)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress)

    def _tab_rx(self):
        w = QWidget()
        h = QHBoxLayout(w)
        lv = QVBoxLayout()
        lv.setContentsMargins(0, 0, 0, 0)
        self._tab_btn_row = QHBoxLayout()
        lv.addLayout(self._tab_btn_row)
        self.spec = SpectrumView(waterfall=True)
        self.spec.setMinimumHeight(160)
        lv.addWidget(self.spec, 1)
        h.addLayout(lv, 1)
        gc = QGroupBox("Созвездие")
        gcv = QVBoxLayout(gc)
        self.cons = ConstellationView()
        gcv.addWidget(self.cons)
        gi = QGroupBox("Состояние приёма")
        fl = QFormLayout(gi)
        fl.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.l_rxsat = wrap_label("Спутник не выбран")
        self.m_carrier = LevelMeter(0, 100, "%")
        self.m_rs = LevelMeter(0, 100, "%")
        self.l_sync = QLabel("—")
        self.l_info = wrap_label("—")
        fl.addRow(self.l_rxsat)
        fl.addRow("Захват несущей:", self.m_carrier)
        fl.addRow("Синхр. кадров:", self.l_sync)
        fl.addRow("Кадры без ошибок RS:", self.m_rs)
        fl.addRow(self.l_info)
        g = QGroupBox("Спектр и водопад")
        gv = QVBoxLayout(g)
        self.spec_ctl = SpectrumControls([self.spec], nfft_setter=self._set_rx_nfft, key="sat_rx")
        gv.addWidget(self.spec_ctl)
        h.addWidget(scroll_panel([gc, gi, g], 250, 320))
        return w

    def _set_rx_nfft(self, n):
        self.rx_nfft = int(n)
        if self.worker is not None:
            self.worker.nfft = int(n)

    def _tab_images(self):
        w = QSplitter(Qt.Vertical)
        top = QWidget()
        iv = QVBoxLayout(top)
        iv.setContentsMargins(0, 0, 0, 0)
        ih = QGridLayout()
        self.combo = QComboBox()
        for k, name in COMBOS:
            self.combo.addItem(name, k)
        self.combo.setCurrentIndex(max(0, self.combo.findData(self.s.get("combo", "221"))))
        self.combo.currentIndexChanged.connect(lambda _i: self._show_image(force=True))
        self.cb_stretch = QCheckBox("Растянуть уровни")
        self.cb_stretch.setChecked(True)
        self.cb_stretch.toggled.connect(lambda _v: self._show_image(force=True))
        self.btn_save = QPushButton("Сохранить PNG")
        self.btn_save.clicked.connect(lambda: self._save_images(manual=True))
        self.btn_open_dir = QPushButton("Папка снимков")
        self.btn_open_dir.clicked.connect(lambda: self._open_dir(PIC_DIR))
        self.l_img = wrap_label("Текущий приём LRPT")
        ih.addWidget(QLabel("Текущий снимок:"), 0, 0)
        ih.addWidget(self.combo, 0, 1)
        ih.addWidget(self.cb_stretch, 0, 2)
        ih.addWidget(self.btn_save, 0, 4)
        ih.addWidget(self.btn_open_dir, 0, 5)
        ih.addWidget(self.l_img, 1, 0, 1, 6)
        ih.setColumnStretch(3, 1)
        iv.addLayout(ih)
        self.view = ImageScroll("Снимок текущего приёма появится здесь. Готовые снимки — в списке ниже.")
        iv.addWidget(self.view, 1)
        w.addWidget(top)
        pd = self.opts.get("pic_dir")
        self.gallery = GalleryWidget(dict(GALLERY_DIRS, meteor=pd, elektro=pd) if pd else None)
        w.addWidget(self.gallery)
        w.setSizes([300, 300])
        w.setChildrenCollapsible(False)
        return w

    def _tab_listen(self):
        w = QWidget()
        h = QHBoxLayout(w)
        self.lspec = SpectrumView(waterfall=True)
        self.lspec.setMinimumHeight(160)
        self.lspec.wheelStep.connect(self._vfo_wheel)
        self.lspec.tuneRequested.connect(self._vfo_click)
        h.addWidget(self.lspec, 1)

        g = QGroupBox("Прослушивание")
        f = QFormLayout(g)
        f.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.l_sat = QComboBox()
        self.l_sat.currentIndexChanged.connect(self._lsat_changed)
        f.addRow("Спутник:", self.l_sat)
        self.l_tx = QComboBox()
        self.l_tx.currentIndexChanged.connect(self._ltx_changed)
        f.addRow("Передатчик:", self.l_tx)
        self.l_vfo = QDoubleSpinBox()
        self.l_vfo.setDecimals(4)
        self.l_vfo.setSuffix(" МГц")
        self.l_vfo.setSingleStep(0.0005)
        self.l_vfo.valueChanged.connect(self._vfo_changed)
        f.addRow("Частота:", self.l_vfo)
        self.l_status = wrap_label("")
        f.addRow(self.l_status)
        self.l_dopp = wrap_label("")
        f.addRow(self.l_dopp)
        self.l_count = wrap_label("")
        f.addRow(self.l_count)
        self.l_level = LevelMeter(-110, -20, "dB")
        f.addRow("Уровень:", self.l_level)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(int(self.s.get("volume", 60)))
        self.vol.valueChanged.connect(self._vol_changed)
        self.engine.audio.volume = self.vol.value() / 100
        f.addRow("Громкость:", self.vol)
        self.sql = QSlider(Qt.Horizontal)
        self.sql.setRange(-120, -20)
        self.sql.setValue(int(self.s.get("squelch", -120)))
        self.sql.valueChanged.connect(self._sql_changed)
        f.addRow("Шумоподавитель (порог):", self.sql)
        self.cb_nr = QCheckBox("Подавление шума в звуке")
        self.cb_nr.setChecked(bool(self.s.get("nr", True)))
        self.cb_nr.toggled.connect(self._nr_changed)
        f.addRow(self.cb_nr)
        self.btn_listen = QPushButton("▶ Слушать")
        self.btn_listen.setCheckable(True)
        self.btn_listen.setMinimumHeight(30)
        self.btn_listen.clicked.connect(lambda on: self.start_listen() if on else self.stop_listen())
        f.addRow(self.btn_listen)
        f.addRow(wrap_label("Слушать можно любой передатчик в режимах FM/AM/SSB/CW (маяки, телеметрию, APRS — "
                            "как звук). Колесо мыши над спектром — подстройка частоты (у транспондера — внутри "
                            "полосы); доплер по TLE добавляется автоматически."))
        gs = QGroupBox("Спектр и водопад")
        gsv = QVBoxLayout(gs)
        gsv.addWidget(SpectrumControls([self.lspec], nfft_setter=lambda n: setattr(self.engine, "nfft", int(n)),
                                       key="sat_listen"))
        h.addWidget(scroll_panel([g, gs], 260, 330))
        self._fill_listen_sats()
        return w

    def _tab_elektro(self):
        w = QWidget()
        h = QHBoxLayout(w)
        left = QSplitter(Qt.Vertical)
        top = QWidget()
        lv = QVBoxLayout(top)
        lv.setContentsMargins(0, 0, 0, 0)
        tg = QGridLayout()
        self.e_img = QComboBox()
        self.e_img.currentIndexChanged.connect(lambda _i: self._eshow())
        self.e_save = QPushButton("Сохранить PNG")
        self.e_save.clicked.connect(lambda: self._esave(manual=True))
        self.e_dir = QPushButton("Папка снимков")
        self.e_dir.clicked.connect(lambda: self._open_dir(ELEKTRO_DIR))
        tg.addWidget(QLabel("Снимок:"), 0, 0)
        tg.addWidget(self.e_img, 0, 1)
        tg.addWidget(self.e_save, 0, 2)
        tg.addWidget(self.e_dir, 0, 3)
        tg.setColumnStretch(1, 1)
        lv.addLayout(tg)
        self.e_view = ImageScroll("Снимков «Электро-Л» пока нет.\nНужна антенна-тарелка ~60–100 см на 1691 МГц "
                                  "с облучателем и МШУ (питание через bias-T).")
        lv.addWidget(self.e_view, 1)
        left.addWidget(top)
        self.e_spec = SpectrumView(waterfall=True)
        self.e_spec.setMinimumHeight(140)
        left.addWidget(self.e_spec)
        left.setSizes([400, 180])
        left.setChildrenCollapsible(False)
        h.addWidget(left, 1)

        g = QGroupBox("«Электро-Л» LRIT, 1691 МГц")
        f = QFormLayout(g)
        f.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.e_sat = QComboBox()
        for n, (name, lon) in elektro.SATS.items():
            self.e_sat.addItem(f"{name} ({abs(lon):.1f}° {'в. д.' if lon >= 0 else 'з. д.'})", n)
        self.e_sat.setCurrentIndex(max(0, self.e_sat.findData(self.s.get("e_sat", 67756))))
        self.e_sat.currentIndexChanged.connect(lambda _i: self._egeo())
        f.addRow("Спутник:", self.e_sat)
        self.e_geo = wrap_label("")
        f.addRow(self.e_geo)
        self.e_cb_bias = QCheckBox("Питание МШУ (bias-T)")
        self.e_cb_bias.setChecked(bool(self.s.get("e_bias", False)))
        f.addRow(self.e_cb_bias)
        self.e_btn = QPushButton("▶ Принимать LRIT")
        self.e_btn.setCheckable(True)
        self.e_btn.setMinimumHeight(30)
        self.e_btn.clicked.connect(lambda _on: self._elektro_toggle())
        f.addRow(self.e_btn)
        self.e_file = QPushButton("Декодировать файл LRIT…")
        self.e_file.clicked.connect(lambda: self._decode_dialog("lrit"))
        f.addRow(self.e_file)
        self.e_carrier = LevelMeter(0, 100, "%")
        self.e_rs = LevelMeter(0, 100, "%")
        f.addRow("Захват несущей:", self.e_carrier)
        f.addRow("Кадры без ошибок RS:", self.e_rs)
        self.e_info = wrap_label("—")
        f.addRow(self.e_info)
        self.e_cons = ConstellationView()
        self.e_cons.setFixedHeight(150)
        f.addRow(self.e_cons)
        self.e_files = QListWidget()
        self.e_files.setMinimumHeight(80)
        self.e_files.setMaximumHeight(140)
        f.addRow(QLabel("Принятые файлы:"))
        f.addRow(self.e_files)
        gs = QGroupBox("Спектр и водопад")
        gsv = QVBoxLayout(gs)
        gsv.addWidget(SpectrumControls([self.e_spec], nfft_setter=self._set_rx_nfft, key="sat_elektro"))
        h.addWidget(scroll_panel([g, gs], 260, 340))
        return w

    def _grp_home(self):
        g = QGroupBox("Дом (Ctrl+щелчок по карте)")
        f = QFormLayout(g)
        f.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.lat = QDoubleSpinBox()
        self.lat.setRange(-90, 90)
        self.lat.setDecimals(5)
        self.lat.setValue(self.s["home_lat"])
        self.lon = QDoubleSpinBox()
        self.lon.setRange(-180, 180)
        self.lon.setDecimals(5)
        self.lon.setValue(self.s["home_lon"])
        self.lat.editingFinished.connect(self._apply_home)
        self.lon.editingFinished.connect(self._apply_home)
        f.addRow("Широта:", self.lat)
        f.addRow("Долгота:", self.lon)
        self.min_el = QDoubleSpinBox()
        self.min_el.setRange(0, 80)
        self.min_el.setDecimals(0)
        self.min_el.setSuffix("°")
        self.min_el.setValue(self.s["min_el"])
        self.min_el.editingFinished.connect(self._min_el_changed)
        f.addRow("Мин. возвышение:", self.min_el)
        self.l_tle = wrap_label("TLE: загрузка…")
        self.l_tle.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.l_tle.setOpenExternalLinks(True)
        f.addRow(self.l_tle)
        h = QHBoxLayout()
        b = QPushButton("Обновить TLE")
        b.clicked.connect(lambda: self._load_tle(force=True))
        bu = QPushButton("Свои спутники…")
        bu.clicked.connect(self._user_sats)
        h.addWidget(b)
        h.addWidget(bu)
        f.addRow(h)
        return g

    def _grp_rx(self):
        g = QGroupBox("Приёмник")
        f = QFormLayout(g)
        f.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.src_combo = QComboBox()
        self._fill_sources()
        f.addRow("Источник:", self.src_combo)
        self.gain_combo = QComboBox()
        self._fill_gains()
        self.gain_combo.currentIndexChanged.connect(self._gain_changed)
        f.addRow("Усиление:", self.gain_combo)
        self.ppm = QSpinBox()
        self.ppm.setRange(-100, 100)
        self.ppm.setValue(int(self.s.get("ppm", 0)))
        self.ppm.setSuffix(" ppm")
        f.addRow("Поправка частоты:", self.ppm)
        self.cb_bias = QCheckBox("Питание антенны (bias-T)")
        self.cb_bias.setChecked(bool(self.s.get("bias", False)))
        f.addRow(self.cb_bias)
        return g

    def _grp_actions(self):
        g = QGroupBox("Приём со спутника")
        f = QFormLayout(g)
        f.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.sat_combo = QComboBox()
        self.sat_combo.currentIndexChanged.connect(self._rxsat_changed)
        f.addRow("Спутник:", self.sat_combo)
        self.tx_combo = QComboBox()
        self.tx_combo.currentIndexChanged.connect(self._rxtx_changed)
        f.addRow("Передатчик:", self.tx_combo)
        self.rate = QComboBox()
        for r in RATES:
            self.rate.addItem(f"{r / 1e6:.3f} МГц".replace(".", ","), r)
        self.rate.setCurrentIndex(max(0, self.rate.findData(self.s.get("rate", 1024000))))
        f.addRow("Дискретизация:", self.rate)
        self.l_rxinfo = wrap_label("")
        f.addRow(self.l_rxinfo)
        self.cb_doppler = QCheckBox("Коррекция доплера по TLE")
        self.cb_doppler.setChecked(bool(self.s.get("doppler", True)))
        self.cb_record = QCheckBox("Записывать IQ (cu8, ~2 МБ/с)")
        self.cb_record.setChecked(bool(self.s.get("record", False)))
        f.addRow(self.cb_doppler)
        f.addRow(self.cb_record)
        for b in self._make_rx_buttons():
            f.addRow(b)
        self.l_next = wrap_label("")
        f.addRow(self.l_next)
        self._fill_rx_sats()
        return g

    def _make_rx_buttons(self):
        """Кнопки приёма: по экземпляру для карточки справа и для вкладки; состояние общее (_Twin)."""
        made = []
        for _ in range(2):
            b_rx = QPushButton("▶ Принимать сейчас")
            b_rx.setCheckable(True)
            b_rx.clicked.connect(lambda on: self.start_rx() if on else self.stop_rx())
            b_auto = QPushButton("Автоприём «Метеоров» по расписанию")
            b_auto.setCheckable(True)
            b_auto.toggled.connect(self._auto_toggled_any)
            b_file = QPushButton("Декодировать файл…")
            b_file.clicked.connect(lambda: self._decode_dialog(None))
            for b in (b_rx, b_auto, b_file):
                b.setMinimumHeight(30)
            made.append((b_rx, b_auto, b_file))
        (r1, a1, f1), (r2, a2, f2) = made
        self.btn_rx, self.btn_auto, self.btn_file = _Twin(r1, r2), _Twin(a1, a2), _Twin(f1, f2)
        self._tab_btns = (r2, a2, f2)
        return r1, a1, f1

    def _auto_toggled_any(self, on):
        self.btn_auto.setChecked(on)            # вторая кнопка — без сигнала
        self._auto_toggled(on)

    def _fill_sources(self):
        devices.fill_source_combo(self.src_combo, self.s["source"], "Демо-генератор (без антенны)")

    def _fill_gains(self):
        self.gain_combo.blockSignals(True)
        self.gain_combo.clear()
        self.gain_combo.addItem("АРУ тюнера", None)
        for gv in self.gains:
            self.gain_combo.addItem(f"{gv / 10:.1f} dB".replace(".", ","), gv)
        want = self.s.get("gain", 402)
        best = min(range(1, self.gain_combo.count()), key=lambda i: abs(self.gain_combo.itemData(i) - want)) \
            if want is not None else 0
        self.gain_combo.setCurrentIndex(best)
        self.gain_combo.blockSignals(False)

    def _fill_rx_sats(self):
        cur = self.sat_combo.currentData() if self.sat_combo.count() else self.s.get("sat", 59051)
        self.sat_combo.blockSignals(True)
        self.sat_combo.clear()
        for s in satcat.all_sats():
            self.sat_combo.addItem(f"{s.name} — {satcat.CATEGORIES.get(s.cat, s.cat)}", s.norad)
        self.sat_combo.setCurrentIndex(max(0, self.sat_combo.findData(cur)))
        self.sat_combo.blockSignals(False)
        self._rxsat_changed(keep_tx=True)

    def _fill_listen_sats(self):
        cur = self.l_sat.currentData()
        self.l_sat.blockSignals(True)
        self.l_sat.clear()
        for s in satcat.CATALOG:
            if any(t.listenable for t in s.txs):
                self.l_sat.addItem(f"{s.name} — {satcat.CATEGORIES.get(s.cat, s.cat)}", s.norad)
        self.l_sat.setCurrentIndex(max(0, self.l_sat.findData(cur)))
        self.l_sat.blockSignals(False)
        self._lsat_changed()

    # ================= свои спутники =================
    def _user_sats(self):
        d = UserSatsDialog(self, list(satcat.USER))
        if d.exec() != QDialog.Accepted:
            return
        self.s["user_sats"] = [s.to_dict() for s in d.sats]
        satcat.set_user(d.sats)
        self._save()
        self._fill_rx_sats()
        self._fill_listen_sats()
        self._load_tle(force=False)
        self.statusBar().showMessage(f"Своих спутников: {len(d.sats)}", 6000)

    # ================= TLE, орбиты, пролёты =================
    def _load_tle(self, force):
        self.l_tle.setText("TLE: загрузка…")
        want = satcat.norads() + list(elektro.SATS)

        def work():
            try:
                tles, msg = satpass.load_tle(force=force, norads=want)
            except Exception as e:  # noqa: BLE001
                tles, msg = {}, f"ошибка: {e}"
            self.tleLoaded.emit(tles, msg)
        threading.Thread(target=work, daemon=True).start()

    def _on_tle(self, tles, msg):
        self.tles = dict(tles)
        for s in satcat.CATALOG:            # TLE, введённые вручную, главнее
            if s.tle:
                self.tles[s.norad] = satpass.Tle(s.name, s.norad, s.tle[0], s.tle[1])
        self.orbits = {}
        for n in satcat.BY_NORAD:
            if n in self.tles and n not in satcat.GEO_NORADS:
                try:
                    self.orbits[n] = satpass.Orbit(self.tles[n])
                except (ValueError, RuntimeError):
                    pass
        self.eorbits = {n: satpass.Orbit(self.tles[n]) for n in elektro.SATS if n in self.tles}
        ages = sorted((time.time() - self.tles[n].epoch_unix) / 3600 for n in self.orbits)
        upd = satpass.cache_time()
        miss = [s.name for s in satcat.CATALOG if s.norad not in self.orbits]
        acc = getattr(self, "_accent", "#4fb3ff")
        txt = (f"Орбиты: TLE с <a style='color:{acc}' href='https://celestrak.org'>CelesTrak (celestrak.org)</a>"
               + (f", обновлено {time.strftime('%d.%m %H:%M', time.localtime(upd))}" if upd else "")
               + (f", возраст {ages[len(ages) // 2]:.0f} ч (до {ages[-1]:.0f} ч)" if ages else "")
               + f"; расчёт SGP4. Спутников: {len(self.orbits)} из {len(satcat.CATALOG)}.")
        if miss:
            txt += f" Нет TLE: {', '.join(miss)}."
        self.l_tle.setText(txt)
        self.l_tle.setToolTip(
            "TLE — наборы элементов орбит NORAD, публикует CelesTrak (обновляет несколько раз в сутки).\n"
            "SDR4NOOB скачивает их не чаще раза в сутки или по кнопке «Обновить TLE»; без сети — кэш\n"
            "%LOCALAPPDATA%\\SDR4NOOB\\tle. Положение считается моделью SGP4: при TLE возрастом 1–3 суток\n"
            "ошибка ~1 км, время пролёта — секунды.\n\n" + msg)
        self.map_note = (f"Орбиты: TLE CelesTrak, обновлено {time.strftime('%d.%m %H:%M', time.localtime(upd))}"
                         if upd else "Орбиты: TLE CelesTrak") + (f", возраст {ages[len(ages) // 2]:.0f} ч" if ages else "") \
            + "; SGP4"
        self.map_lbl.setText(self.map_note)
        self.map_lbl.adjustSize()
        self._recalc_passes()
        self._egeo()
        self._tick()

    def _cats(self):
        return {k for k, cb in self.cat_cb.items() if cb.isChecked()}

    def _cats_changed(self):
        self.s["cats"] = sorted(self._cats())
        self._fill_table()
        self._update_map(time.time())
        self._save()

    def _recalc_passes(self):
        lat, lon = self.lat.value(), self.lon.value()
        self.passes = satpass.all_passes(list(self.orbits.values()), lat, lon, hours=36, min_el=0.0)
        self._passes_t = time.time()
        self._fill_table()

    def _fill_table(self):
        self.table.setUpdatesEnabled(False)
        try:
            self._fill_table_rows()
        finally:
            if self.table.rowCount() != self._table_n:
                self._table_n = self.table.rowCount()
                self.table.resizeColumnsToContents()
            self.table.setUpdatesEnabled(True)

    def _fill_table_rows(self):
        now = time.time()
        mn = self.min_el.value()
        cats = self._cats()
        rows = [p for p in self.passes if p.los > now and p.norad in satcat.BY_NORAD
                and satcat.BY_NORAD[p.norad].cat in cats][:60]
        self.table.setRowCount(len(rows))
        self._rows = rows
        accent = QColor("#2e7d32")
        for r, p in enumerate(rows):
            sat = satcat.BY_NORAD[p.norad]
            tx = sat.txs[0]
            freq = f"{_mhz(tx.freq)}" + (f"–{_mhz(tx.hi)}" if tx.is_band else "") + f" {tx.mode}"
            if len(sat.txs) > 1:
                freq += f" (+{len(sat.txs) - 1})"
            cells = [sat.name, "", _fmt_d(p.aos), _fmt_t(p.los), f"{p.duration / 60:.0f} мин", f"{p.max_el:.0f}°",
                     f"{p.az_aos:.0f}° → {p.az_los:.0f}°", freq, sat.status]
            low = p.max_el < mn
            for c, txt in enumerate(cells):
                if c == 1:
                    continue
                it = QTableWidgetItem(txt)
                if c == 0:
                    it.setForeground(QColor(sat.color))
                    it.setToolTip(sat.note or sat.name)
                if c == 7:
                    it.setToolTip("\n".join(_tx_text(t) for t in sat.txs))
                if c == 8 and sat.note:
                    it.setToolTip(sat.note)
                if p.aos <= now <= p.los:
                    it.setBackground(accent)
                    it.setForeground(QColor("#ffffff"))
                elif low:
                    it.setForeground(QColor("#808080"))
                self.table.setItem(r, c, it)
            self.table.setCellWidget(r, 1, self._row_buttons(sat))

    def _row_buttons(self, sat):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 0, 2, 0)
        h.setSpacing(3)
        b_rx = QPushButton("Принять")
        kinds = {t.kind for t in sat.txs}
        b_rx.setToolTip("Снимки LRPT" if "lrpt" in kinds else ("Снимки LRIT" if "lrit" in kinds else
                        "Приём: спектр, созвездие, запись IQ (декодера для этого сигнала нет)"))
        b_rx.clicked.connect(lambda _c=False, n=sat.norad: self._row_rx(n))
        b_l = QPushButton("Слушать")
        lt = [(i, t) for i, t in enumerate(sat.txs) if t.listenable]
        if not lt:
            b_l.setEnabled(False)
            b_l.setToolTip("Цифровой сигнал (LRPT/LRIT/IQ) — на слух только шум. Используйте «Принять».")
        elif len(lt) == 1:
            b_l.setToolTip("Слушать: " + _tx_text(lt[0][1]))
            b_l.clicked.connect(lambda _c=False, n=sat.norad, i=lt[0][0]: self.start_listen(n, i))
        else:
            m = QMenu(b_l)
            for i, t in lt:
                a = QAction(_tx_text(t), m)
                a.triggered.connect(lambda _c=False, n=sat.norad, i=i: self.start_listen(n, i))
                m.addAction(a)
            b_l.setMenu(m)
            b_l.setToolTip("Выберите передатчик")
        for b in (b_rx, b_l):
            b.setMinimumHeight(22)
            h.addWidget(b)
        return w

    def _row_rx(self, norad):
        self._stop_all()
        self.start_rx(norad)

    def _tick(self):
        now = time.time()
        if self.orbits and now - getattr(self, "_passes_t", 0) > 600:
            self._recalc_passes()
        elif self.orbits and now - getattr(self, "_table_t", 0) >= 30:
            # раз в 30 с (раньше условие «секунда кратна 15» срабатывало на каждом тике этой секунды
            # и таблица с кнопками перестраивалась по нескольку раз подряд — подвисания)
            self._table_t = now
            self._fill_table()
        if self.tabs.currentIndex() == TAB_MAP:
            self._update_map(now)
        self._update_next(now)
        self._auto_check(now)

    def _update_map(self, now):
        items, tracks = [], []
        lat, lon = self.lat.value(), self.lon.value()
        cats = self._cats()
        sel = getattr(self, "_sel", None)
        for n, o in self.orbits.items():
            sat = satcat.BY_NORAD.get(n)
            if sat is None or (sat.cat not in cats and str(n) != sel):
                continue
            try:
                la, lo, h = o.subpoint(now)
                _az, el, _rng, _rr = o.look(now, lat, lon)
            except (ValueError, FloatingPointError):
                continue
            if not np.isfinite(la):
                continue
            short = sat.name.split(" (")[0]
            lbl = short + (f"\n{h:.0f} км, возв. {el:.0f}°" if el > 0 or str(n) == sel else "")
            items.append(dict(key=str(n), lat=la, lon=lo, kind="sat", label=lbl, color=sat.color))
            if sat.cat in ("meteo", "iss") or sat.user or str(n) == sel:
                # трасса: виток назад и виток вперёд, без затухания; переход через 180° карта рвёт сама
                past = o.orbit_track(now, 1.0, 0.0, 120)
                fut = o.orbit_track(now, 0.0, 1.0, 120)
                wsel = 2.6 if str(n) == sel else 1.6
                tracks.append(dict(points=past, color=sat.color, width=wsel * 0.7))
                tracks.append(dict(points=fut, color=sat.color, width=wsel))
        if "meteo" in cats:
            for n, o in self.eorbits.items():
                try:
                    la, lo, _h = o.subpoint(now)
                except (ValueError, FloatingPointError):
                    continue
                if np.isfinite(la):
                    items.append(dict(key=f"e{n}", lat=la, lon=lo, kind="point", color="#ffd84d",
                                      label=elektro.SATS[n][0]))
        self.map.set_items("sats", items)
        self.map.set_tracks("orbits", tracks)

    def _tab_changed(self, i):
        if i == TAB_MAP:
            self._update_map(time.time())
        elif i == TAB_IMG:
            self.gallery.refresh()

    def _update_next(self, now):
        mn = self.min_el.value()
        cur = [p for p in self.passes if p.aos <= now <= p.los]
        nxt = [p for p in self.passes if p.aos > now and p.max_el >= mn]
        parts = []
        for p in cur:
            if p.norad in METEORS or p.norad == ISS:
                parts.append(f"<b>Сейчас:</b> {satcat.BY_NORAD[p.norad].name}, до {_fmt_t(p.los)}, "
                             f"макс. {p.max_el:.0f}°")
        nm = [p for p in nxt if p.norad in METEORS]
        if nm:
            p = nm[0]
            parts.append(f"Следующий «Метеор»: {satcat.BY_NORAD[p.norad].name} в {_fmt_d(p.aos)} "
                         f"(через {_fmt_dt(p.aos - now)}), макс. {p.max_el:.0f}°")
        ni = [p for p in self.passes if p.aos > now and p.norad == ISS]
        if ni:
            parts.append(f"МКС: {_fmt_d(ni[0].aos)}, макс. {ni[0].max_el:.0f}° — SSTV/голос 145,800 МГц "
                         f"(режим SSTV)")
        self.l_next.setText("<br>".join(parts))

    # ================= дом и карта =================
    def _apply_home(self, save=True):
        lat, lon = self.lat.value(), self.lon.value()
        self.map.set_home(lat, lon, ())
        circ = [dict(points=satpass.visibility_circle(lat, lon, 830, 0), color="#4fb3ff", width=1.2, dash=True)]
        if self.min_el.value() > 0:
            circ.append(dict(points=satpass.visibility_circle(lat, lon, 830, self.min_el.value()), color="#4fb3ff",
                             width=1.6))
        self.map.set_tracks("zone", circ)
        self.s["home_lat"], self.s["home_lon"] = lat, lon
        if self.orbits:
            self._recalc_passes()
        self._egeo()
        if save:
            self._save()

    def _min_el_changed(self):
        self.s["min_el"] = self.min_el.value()
        self._apply_home()

    def _map_clicked(self, lat, lon):
        if QApplication.keyboardModifiers() & Qt.ControlModifier:
            self.lat.setValue(lat)
            self.lon.setValue(lon)
            self._apply_home()
            self.statusBar().showMessage(f"Дом: {lat:.5f}, {lon:.5f}", 5000)

    def _sat_clicked(self, key):
        if key.startswith("e"):
            n = int(key[1:])
            self.e_sat.setCurrentIndex(self.e_sat.findData(n))
            self.tabs.setCurrentIndex(TAB_ELEKTRO)
            return
        self._sel = key
        self.map.set_selected(key)
        n = int(key)
        sat = satcat.BY_NORAD.get(n)
        if sat is not None and not self.src:
            self.sat_combo.setCurrentIndex(self.sat_combo.findData(n))
        if sat is not None and not self.listen and self.l_sat.findData(n) >= 0:
            self.l_sat.setCurrentIndex(self.l_sat.findData(n))
        o = self.orbits.get(n)
        if o and sat:
            az, el, rng, rr = o.look(time.time(), self.lat.value(), self.lon.value())
            f = sat.txs[0].center
            d = -f * rr / satpass.C_LIGHT
            self.statusBar().showMessage(
                f"{sat.name}: азимут {az:.0f}°, возвышение {el:.0f}°, дальность {rng:.0f} км, "
                f"доплер {d / 1e3:+.2f} кГц на {_mhz(f)} МГц · {sat.status}", 10000)

    def _pass_clicked(self, row, _col):
        if 0 <= row < len(getattr(self, "_rows", [])):
            self._sat_clicked(str(self._rows[row].norad))

    # ================= общее: донгл один =================
    def _stop_all(self):
        if self.listen:
            self.stop_listen()
        if self.src or self.worker:
            self.stop_rx()

    def _src_index(self):
        src = self.src_combo.currentData()
        if src == "none":
            self.statusBar().showMessage("Нет устройства: подключите донгл или выберите демо-генератор.", 8000)
            return None
        return src

    # ================= «Приём со спутника» =================
    def _rx_sat(self):
        return satcat.BY_NORAD.get(self.sat_combo.currentData())

    def _rx_tx(self):
        sat = self._rx_sat()
        i = self.tx_combo.currentData()
        return sat.txs[i] if sat is not None and i is not None and i < len(sat.txs) else None

    def _rxsat_changed(self, _i=None, keep_tx=False):
        sat = self._rx_sat()
        want = self.s.get("tx", 0) if keep_tx else 0
        self.tx_combo.blockSignals(True)
        self.tx_combo.clear()
        if sat:
            for i, t in enumerate(sat.txs):
                self.tx_combo.addItem(_tx_text(t), i)
            self.tx_combo.setCurrentIndex(max(0, min(want, self.tx_combo.count() - 1)))
        self.tx_combo.blockSignals(False)
        self._rxtx_changed()

    def _rxtx_changed(self, _i=None):
        t = self._rx_tx()
        sat = self._rx_sat()
        if t is None or sat is None:
            return
        kind = t.kind
        if kind == "lrit":
            self.rate.setCurrentIndex(self.rate.findData(elektro.LIVE_RATE))
        elif kind == "lrpt" and self.rate.currentData() not in (1024000, 1200000, 1440000):
            self.rate.setCurrentIndex(self.rate.findData(1024000))
        self.rate.setEnabled(kind != "lrit" and self.src is None)
        geo = sat.norad in satcat.GEO_NORADS
        self.cb_doppler.setEnabled(not geo)
        self.l_rxinfo.setText(
            f"Частота {_mhz(t.center)} МГц, {t.mode}. Обработка: {KIND_TEXT.get(kind, kind)}."
            + (" Геостационар — доплера нет." if geo else "")
            + (" Для этого сигнала IQ записывается всегда." if kind in ("iq", "voice") else ""))
        self.btn_file.setEnabled(kind in ("lrpt", "lrit"))
        self.btn_file.setToolTip("" if kind in ("lrpt", "lrit") else "Для этого сигнала нет декодера записей")

    def start_rx(self, norad=None, txi=None, auto=False):
        if self.src:
            return
        if self.listen:
            self.stop_listen()
        src = self._src_index()
        if src is None:
            self.btn_rx.setChecked(False)
            return
        if norad is not None and self.sat_combo.findData(norad) >= 0:
            self.sat_combo.setCurrentIndex(self.sat_combo.findData(norad))
        if txi is not None:
            self.tx_combo.setCurrentIndex(max(0, self.tx_combo.findData(txi)))
        sat, t = self._rx_sat(), self._rx_tx()
        if sat is None or t is None:
            self.btn_rx.setChecked(False)
            return
        kind = t.kind
        freq = t.center
        geo = sat.norad in satcat.GEO_NORADS
        if kind == "lrit":
            fs, ifo, synth = elektro.LIVE_RATE, elektro.IF_OFFSET, "lrit"
        else:
            fs, ifo, synth = int(self.rate.currentData()), IF_OFFSET, "lrpt"
        if src == "test":
            fs = SynthSource.FS[synth]
        center = freq - ifo
        if kind == "lrpt":
            rx = lrpt.AutoLrptReceiver(fs)          # 72k OQPSK и 80k OQPSK с перемежением — сам выберет
        elif kind == "lrit":
            rx = elektro.LritReceiver(fs)
        else:
            bw = (t.hi - t.freq + 10e3) if t.is_band else (t.bw or 30e3)
            rx = IqReceiver(fs, bw)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        record = self.cb_record.isChecked() or kind in ("iq", "voice")
        slug = re.sub(r"[^\w-]+", "_", sat.name).strip("_")
        iq_dir = self.opts.get("iq_dir") or (None if self.opts.get("shot") else IQ_DIR)   # автотест — без записи
        rec = os.path.join(iq_dir, f"{slug}_{stamp}_{fs}Hz_{int(center)}.cu8") if record and iq_dir else None
        orbit = None if (src == "test" or geo) else self.orbits.get(sat.norad)
        self.worker = RxWorker(fs, center, freq, rx, orbit, (self.lat.value(), self.lon.value()),
                               self.cb_doppler.isChecked() and not geo, rec)
        self.worker.nfft = self.rx_nfft
        self.worker.start()
        bias = self.e_cb_bias.isChecked() if kind == "lrit" else self.cb_bias.isChecked()
        self._open_source(src, fs, center, freq, synth, bias, self.gain_combo.currentData())
        self.rx = dict(sat=sat, tx=t, kind=kind, freq=freq, fs=fs, center=center, stamp=stamp, auto=auto,
                       rec=rec, t0=time.time())
        if kind == "lrpt":
            self.images = {}
            self.images_src = f"{sat.name}, {time.strftime('%d.%m.%Y %H:%M')}"
            self._img_ver = -1
        if kind == "lrit":
            self._e_ver = -1
            self.e_files.clear()
            self.e_btn.setChecked(True)
            self.e_btn.setText("■ Остановить приём")
        half = (t.hi - t.freq) / 2 + 30e3 if t.is_band else {"lrpt": 60e3, "lrit": 220e3}.get(kind, 15e3)
        for sv in (self.spec, self.e_spec):
            sv.markers = [(freq, "#ff6a4d", t.mode)]
            sv.bands = [(freq - half, freq + half, "#ff6a4d")]
        self.l_rxsat.setText(f"<b>{sat.name}</b><br>{_tx_text(t)}<br>{KIND_TEXT.get(kind, kind)}")
        self.btn_rx.setChecked(True)
        self.btn_rx.setText("■ Остановить приём")
        self._lock_controls(True)
        self.tabs.setCurrentIndex(TAB_ELEKTRO if kind == "lrit" else TAB_RX)
        self.statusBar().showMessage(
            f"Приём {sat.name} на {_mhz(freq)} МГц" + (" (автоприём)" if auto else "")
            + (f", запись IQ: {rec}" if rec else ""), 10000)
        self.s.update(source=src, sat=sat.norad, tx=self.tx_combo.currentData() or 0,
                      rate=int(self.rate.currentData()), ppm=self.ppm.value(), bias=self.cb_bias.isChecked(),
                      doppler=self.cb_doppler.isChecked(), record=self.cb_record.isChecked(),
                      e_bias=self.e_cb_bias.isChecked())
        self._save()

    def _elektro_toggle(self):
        if self.rx is not None and self.rx["kind"] == "lrit":
            self.stop_rx()
            return
        self._stop_all()
        self.start_rx(self.e_sat.currentData(), 0)

    def _open_source(self, src, fs, center, freq, kind, bias, gain):
        if src == "test":
            self.src = SynthSource(self._sink, center, freq, kind)
        else:
            self.src = devices.make_source(src, self._sink, fs, center)
            self.src.gain = gain
        self.src.start()
        if src != "test":
            self.src.command("ppm", self.ppm.value())
            if bias:
                self.src.command("bias", True)

    def _lock_controls(self, on):
        for wdg in (self.src_combo, self.rate, self.sat_combo, self.tx_combo):
            wdg.setEnabled(not on)
        if not on:
            self._rxtx_changed()

    def stop_rx(self):
        src, self.src = self.src, None
        if src:
            if getattr(src, "HARDWARE", False):
                src.command("bias", False)
            src.stop()
            src.join(3.0)
        w, self.worker = self.worker, None
        info, self.rx = self.rx, None
        if w and info:
            w.stop()
            w.join(5.0)
            st = w.rx.stats()
            kind = info["kind"]
            if kind == "lrpt":
                self.images = w.images()
                self.images_meta = self._meta(info, st, "live")
                if st["lines"] > 0:
                    self._show_image(force=True)
                    saved = self._save_images()
                    self.statusBar().showMessage(
                        f"Приём завершён: {st['lines']} строк, кадров {st['frames_ok']}/{st['frames']}; "
                        f"сохранено: {saved}", 15000)
                else:
                    self.statusBar().showMessage("Приём завершён — изображение не получено.", 10000)
            elif kind == "lrit":
                self.erx = w.rx
                self.e_meta = self._meta(info, st, "live")
                self._erefresh()
                saved = self._esave()
                self.statusBar().showMessage(f"Приём LRIT завершён: файлов {w.rx.demux.nfiles}; {saved}", 15000)
            else:
                self.statusBar().showMessage(
                    f"Приём завершён ({_fmt_dt(time.time() - info['t0'])})"
                    + (f", запись IQ: {info['rec']}" if info.get("rec") else ""), 15000)
            self.gallery.refresh()
        self.auto_pass = None
        self.btn_rx.setChecked(False)
        self.btn_rx.setText("▶ Принимать сейчас")
        self.e_btn.setChecked(False)
        self.e_btn.setText("▶ Принимать LRIT")
        self._lock_controls(False)

    def _meta(self, info, st, source):
        t = info["tx"]
        sat = info["sat"]
        rs = {"lrpt": lrpt.SYMBOL_RATE, "lrit": elektro.SYMBOL_RATE}.get(info["kind"])
        mod = {"lrpt": "LRPT OQPSK", "lrit": "LRIT BPSK"}.get(info["kind"], t.mode)
        return dict(time=time.strftime("%Y-%m-%d %H:%M:%S"), sat=sat.name, norad=sat.norad, freq=t.center,
                    modulation=mod, symbol_rate=rs, frames=st.get("frames"), frames_ok=st.get("frames_ok"),
                    rs_pct=st.get("rs_pct"), lines=st.get("lines"), source=source,
                    duration_s=round(time.time() - info.get("t0", time.time())))

    def _sink(self, kind, *args):
        if kind == "stream":
            w = self.worker
            if w:
                w.put(args[1])
        elif kind in ("opened", "error"):
            self.bridge.event.emit(kind, args[0])

    def _on_event(self, kind, data):
        if kind == "opened":
            if data.get("gains"):
                self.gains = list(data["gains"])
                self._fill_gains()
            self.statusBar().showMessage(f"Открыт: {data['label']} · тюнер {data['tuner']}", 6000)
        elif kind == "error":
            self.statusBar().showMessage("Ошибка приёмника: " + str(data), 15000)
            QTimer.singleShot(300, lambda: self.src is not None and not self.src.is_alive() and self.stop_rx())

    def _gain_changed(self):
        self.s["gain"] = self.gain_combo.currentData()
        if self.src and getattr(self.src, "HARDWARE", False):
            self.src.command("gain", self.s["gain"])
        if self.listen:
            self.engine.cmd("gain", self.s["gain"])
        self._save()

    def _spec_update(self):
        """15 мс: последний наступивший кадр спектра и свежие точки созвездия видимой вкладки.
        Только передача ссылок — считают и рисуют SpectrumView/ConstellationView по своему темпу."""
        tab = self.tabs.currentIndex()
        if self.listen and tab == TAB_LISTEN:
            snap = self.engine.pacer.take()
            if snap is not None and snap[0] != self._lspec_seq:
                self._lspec_seq = snap[0]
                self.lspec.set_data(snap[1], snap[2])
            return
        w = self.worker
        if w is None or self.rx is None:
            return
        lrit = self.rx["kind"] == "lrit"
        snap = w.take_spectrum(time.time())
        if snap is not None and snap[0] != self._spec_seq:
            self._spec_seq = snap[0]
            (self.e_spec if lrit else self.spec).set_data(snap[1], snap[2])
        if tab == (TAB_ELEKTRO if lrit else TAB_RX):
            (self.e_cons if lrit else self.cons).set_points(w.cons_snap)

    def _ui_update(self):
        w = self.worker
        if w is not None and self.rx is not None:
            snap = w.snap
            st = snap.get("stats")
            if st:
                if self.rx["kind"] == "lrit":
                    self._ui_lrit(w, snap, st)
                else:
                    self._ui_rx(w, snap, st)
        if self.listen:
            self._listen_tick()
        fd = self.filedec
        if fd is not None:
            self.progress.setValue(int(fd.progress * 100))
            st = fd.stats
            if st:
                extra = f"строк {st.get('lines', 0)}" if self.file_kind == "lrpt" else f"файлов {st.get('files', 0)}"
                self.statusBar().showMessage(
                    f"Декодирование: {fd.progress * 100:.0f} %, кадров {st.get('frames_ok', 0)}/{st.get('frames', 0)},"
                    f" {extra}")
            if not fd.is_alive():
                self.filedec = None
                self.progress.setVisible(False)
                self.btn_file.setText("Декодировать файл…")
                self.e_file.setText("Декодировать файл LRIT…")
                self._file_done(fd)

    @staticmethod
    def _adc_text(st):
        """Строка про уровень АЦП: норма / мало / перегрузка или сильная помеха."""
        sd, clip = st.get("adc_std", 0.0), st.get("adc_clip", 0.0)
        if clip > 0.05 or sd > 45:
            verdict = ("<span style='color:#ff5a4f'><b>перегрузка или сильная помеха</b></span> — снизьте усиление "
                       "до 25–30 dB; если шум не уменьшается относительно сигнала, рядом с антенной шумит прибор "
                       "(LED-лампы, блоки питания, ПК)")
        elif sd > 25:
            verdict = "<span style='color:#e8a33a'>много шума</span> — можно снизить усиление"
        elif sd < 2:
            verdict = "<span style='color:#e8a33a'>мало</span> — поднимите усиление или проверьте антенну"
        else:
            verdict = "<span style='color:#3ad06a'>норма</span>"
        return f"АЦП: разброс {sd:.0f} (норма 4–20), отсечки {clip:.2f} % — {verdict}."

    def _ui_rx(self, w, snap, st):
        kind = self.rx["kind"]
        self.m_carrier.set_value(st["carrier"] * 100, f"{st['carrier'] * 100:.0f} %")
        self.m_rs.set_value(st["rs_pct"], f"{st['rs_pct']:.0f} % · {st['frames_ok']}")
        if kind == "lrpt":
            sync = "есть" if st["sync"] else ("поиск" if st["frames"] == 0 else "потеряна")
            self.l_sync.setText(f"<b>{sync}</b>" + (" · NRZ-M" if st["nrzm"] else "") + f"<br>режим: {st.get('mode', '')}")
            resid = st["residual_hz"] - st["doppler"] - (w.sig - w.center)
            self.l_info.setText(
                f"Доплер {st['doppler'] / 1e3:+.2f} кГц, остаток {resid:+.0f} Гц. "
                f"SNR ≈ {st['snr_db']:.1f} dB, пакетов {st['packets']}, строк {st['lines']}. "
                f"Каналы: {', '.join(str(a - 63) for a in st['channels']) or '—'}. "
                f"Загрузка DSP {st['busy'] * 100:.0f} %"
                + (f", пропущено блоков {st['dropped']}" if st["dropped"] else "") + ".<br>" + self._adc_text(st))
            # снимок готовится в потоке приёма; здесь только показ
            w.preview_key = self.combo.currentData() if self.tabs.currentIndex() == TAB_IMG else None
            pv = w.preview
            if pv is not None and (pv["ver"], pv["key"]) != getattr(self, "_pv_shown", None):
                self._pv_shown = (pv["ver"], pv["key"])
                self.images = pv["images"]
                if pv["arr"] is not None:
                    self.view.set_array(pv["arr"], follow=True)
                    self.l_img.setText(f"{self.images_src} · {pv['arr'].shape[0]} строк · {pv['used']}")
        else:
            self.l_sync.setText("нет декодера")
            self.l_info.setText(
                f"Доплер {st['doppler'] / 1e3:+.2f} кГц. Уровень в полосе {st.get('level_db', 0):.1f} dB, "
                f"над шумом ≈ {st['snr_db']:.1f} dB. Идёт {_fmt_dt(st['elapsed'])}"
                + (f", запись IQ: {os.path.basename(self.rx['rec'])}" if self.rx.get("rec") else "")
                + f". Загрузка DSP {st['busy'] * 100:.0f} %.<br>" + self._adc_text(st))

    # ================= автоприём =================
    def _auto_toggled(self, on):
        self.statusBar().showMessage(
            "Автоприём включён: приём начнётся на пролётах «Метеор» выше "
            f"{self.min_el.value():.0f}° (пока окно открыто)." if on else "Автоприём выключен.", 8000)
        if not on and self.auto_pass is not None and self.src:
            self.stop_rx()

    def _auto_check(self, now):
        if not self.btn_auto.isChecked():
            return
        if self.auto_pass is not None:
            if now > self.auto_pass.los + 20:
                self.stop_rx()
            return
        if self.src or self.filedec or self.listen:
            return
        mn = self.min_el.value()
        for p in self.passes:
            if p.norad in METEORS and p.max_el >= mn and p.aos - 30 <= now <= p.los - 60:
                self.start_rx(p.norad, 0, auto=True)
                self.auto_pass = p
                break

    # ================= файлы =================
    def _decode_dialog(self, kind=None):
        if self.filedec is not None:
            self.filedec.cancelled = True
            return
        if kind is None:
            t = self._rx_tx()
            kind = t.kind if t is not None else "lrpt"
        if kind not in ("lrpt", "lrit"):
            self.statusBar().showMessage("Для этого сигнала декодера записей нет.", 6000)
            return
        title = "Запись IQ пролёта «Метеор-М»" if kind == "lrpt" else "Запись IQ «Электро-Л» LRIT"
        path, _ = QFileDialog.getOpenFileName(
            self, title, IQ_DIR if os.path.isdir(IQ_DIR) else "",
            "Записи IQ (*.wav *.raw *.cu8 *.cs8 *.cs16 *.s16 *.cf32 *.fc32 *.iq *.bin);;Все файлы (*)")
        if not path:
            return
        dlg = FileDialog(self, path, kind == "lrpt", 1024000 if kind == "lrpt" else elektro.LIVE_RATE)
        if dlg.exec() != QDialog.Accepted:
            return
        self._decode_path(path, dlg.fs.value(), dlg.fmt.currentData(), dlg.mod.currentData(), kind)

    def _decode_path(self, path, fs, fmt, oqpsk, kind):
        fmt = fmt or lrpt.guess_format(path)
        if fmt != "wav" and not fs:
            m = re.search(r"(\d{5,8})\s*hz", os.path.basename(path), re.I)
            fs = int(m.group(1)) if m else (1024000 if kind == "lrpt" else elektro.LIVE_RATE)
        fs = fs if fmt != "wav" else None
        if kind == "lrpt":
            self.filedec = FileDecoder(lrpt.decode_file, path, fs=fs, fmt=fmt, oqpsk=oqpsk)
            self.btn_file.setText("Отменить декодирование")
        else:
            self.filedec = FileDecoder(elektro.decode_file, path, fs=fs, fmt=fmt)
            self.e_file.setText("Отменить декодирование")
        self.file_kind = kind
        self.filedec.start()
        self._file_name = os.path.basename(path)
        self.progress.setValue(0)
        self.progress.setVisible(True)

    def _file_meta(self, st, kind):
        return dict(time=time.strftime("%Y-%m-%d %H:%M:%S"), sat="из файла", freq=None,
                    modulation="LRPT" if kind == "lrpt" else "LRIT BPSK",
                    symbol_rate=lrpt.SYMBOL_RATE if kind == "lrpt" else elektro.SYMBOL_RATE,
                    frames=st.get("frames"), frames_ok=st.get("frames_ok"), rs_pct=st.get("rs_pct"),
                    lines=st.get("lines"), source="файл " + self._file_name)

    def _file_done(self, fd):
        if fd.error:
            self.statusBar().showMessage("Ошибка декодирования: " + fd.error, 15000)
            return
        rx = fd.rx
        st = rx.stats()
        if self.file_kind == "lrit":
            self.erx = rx
            self.e_meta = self._file_meta(st, "lrit")
            self._erefresh()
            self.tabs.setCurrentIndex(TAB_ELEKTRO)
            saved = self._esave()
            self.statusBar().showMessage(
                f"LRIT: кадров {st['frames_ok']}/{st['frames']}, файлов {st['files']}, снимков {st['images']}"
                + (f", не поддержано (вейвлет) {st['unsupported']}" if st["unsupported"] else "") + f"; {saved}",
                20000)
            self.gallery.refresh()
            return
        self.images = rx.imager.all_images()
        self.images_meta = self._file_meta(st, "lrpt")
        if rx.demux.scid is not None:
            self.images_meta["scid"] = int(rx.demux.scid)
        self.images_src = self._file_name
        self.rx_stamp = time.strftime("%Y%m%d_%H%M%S")
        if st["lines"] > 0:
            self.tabs.setCurrentIndex(TAB_IMG)
            self._show_image(force=True)
            saved = self._save_images()
            self.gallery.refresh()
            self.statusBar().showMessage(
                f"Готово: {st['lines']} строк, кадров {st['frames_ok']}/{st['frames']} (RS {st['rs_pct']:.0f} %), "
                f"каналы {', '.join(str(a - 63) for a in st['channels'])}; сохранено: {saved}", 20000)
        else:
            self.statusBar().showMessage(
                f"Изображение не получено: кадров {st['frames_ok']}/{st['frames']}. Проверьте частоту "
                "дискретизации, формат и модуляцию.", 20000)

    # ================= снимки LRPT =================
    def _show_image(self, force=False, follow=False):
        arr, key = lrpt.preview(self.images, self.combo.currentData(), self.cb_stretch.isChecked())
        self.s["combo"] = self.combo.currentData()
        w = self.worker
        if w is not None and self.rx is not None and self.rx["kind"] == "lrpt":
            w.preview_key = self.combo.currentData()       # пересчитается в потоке приёма
            return
        if arr is None:
            if self.images:
                have = ", ".join(str(a - 63) for a in sorted(self.images))
                self.l_img.setText(f"нет нужных каналов (есть: {have})")
            return
        self.view.set_array(arr, follow=follow)
        self.l_img.setText(f"{self.images_src} · {arr.shape[0]} строк · {key}")

    def _save_images(self, manual=False):
        if not self.images:
            if manual:
                self.statusBar().showMessage("Нечего сохранять.", 5000)
            return ""
        pic_dir = (self.pic_dir or PIC_DIR) if manual else self.pic_dir
        if not pic_dir:
            return "не сохранено (режим снимка окна)"
        os.makedirs(pic_dir, exist_ok=True)
        meta = dict(self.images_meta or {})
        sat = {57166: "M2-3", 59051: "M2-4"}.get(meta.get("norad"), "file")
        base = os.path.join(pic_dir, f"meteor_{sat}_{getattr(self, 'rx_stamp', time.strftime('%Y%m%d_%H%M%S'))}")
        n = 0
        for a, im in self.images.items():
            if im is None:
                continue
            _save_png(im, f"{base}_ch{a - 63}.png")
            write_meta(f"{base}_ch{a - 63}.png", dict(meta, product=f"канал {a - 63}"))
            n += 1
        for key in ("221", "125", "123"):
            c = lrpt.composite(self.images, key)
            if c is None:
                continue
            _save_png(np.dstack([lrpt.stretch(c[:, :, i]) for i in range(3)]), f"{base}_rgb{key}.png")
            write_meta(f"{base}_rgb{key}.png", dict(meta, product=f"композит {key}"))
            n += 1
        return f"{n} PNG в {pic_dir}"

    def _open_dir(self, path):
        os.makedirs(path, exist_ok=True)
        try:
            os.startfile(path)  # noqa: S606 — открыть папку в проводнике
        except OSError:
            pass

    # ================= прослушивание =================
    def _lsat(self):
        return satcat.BY_NORAD.get(self.l_sat.currentData())

    def _ltx(self):
        sat = self._lsat()
        i = self.l_tx.currentData()
        return sat.txs[i] if sat is not None and i is not None and i < len(sat.txs) else None

    def _lsat_changed(self, _i=None):
        sat = self._lsat()
        self.l_tx.blockSignals(True)
        self.l_tx.clear()
        if sat:
            for i, t in enumerate(sat.txs):
                if t.listenable:
                    self.l_tx.addItem(_tx_text(t), i)
            self.l_status.setText(f"<b>{sat.status}</b>" + (f" — {sat.note}" if sat.note else ""))
        self.l_tx.blockSignals(False)
        self._ltx_changed()

    def _ltx_changed(self, _i=None):
        t = self._ltx()
        if t is None:
            return
        self.l_vfo.blockSignals(True)
        if t.is_band:
            self.l_vfo.setRange(t.freq / 1e6, t.hi / 1e6)
            self.l_vfo.setValue(t.center / 1e6)
        else:
            # одиночная частота: подстройка ±30 кГц (у спутников бывает уход и неточные данные)
            self.l_vfo.setRange(t.freq / 1e6 - 0.03, t.freq / 1e6 + 0.03)
            self.l_vfo.setValue(t.freq / 1e6)
        self.l_vfo.blockSignals(False)
        if self.listen:
            self._retune_listen()

    def _vfo_changed(self, _v):
        if self.listen:
            self.listen["vfo"] = self.l_vfo.value() * 1e6

    def _vfo_wheel(self, d):
        t = self._ltx()
        step = 0.0001 if t is not None and t.mode in ("USB", "LSB", "CW") else 0.0005
        self.l_vfo.setValue(self.l_vfo.value() + d * step)

    def _vfo_click(self, f):
        if self.listen:
            dop = self.listen.get("dopp", 0.0)
            self.l_vfo.setValue((f - dop) / 1e6)

    def start_listen(self, norad=None, txi=None):
        if norad is not None:
            i = self.l_sat.findData(norad)
            if i < 0:
                self.statusBar().showMessage("У этого спутника нет передатчиков, которые можно слушать.", 6000)
                return
            self.l_sat.setCurrentIndex(i)
        if txi is not None and self.l_tx.findData(txi) >= 0:
            self.l_tx.setCurrentIndex(self.l_tx.findData(txi))
        if self.src or self.worker:
            self.stop_rx()
        if self.listen:
            self.stop_listen()
        src = self._src_index()
        if src is None:
            self.btn_listen.setChecked(False)
            return
        t = self._ltx()
        sat = self._lsat()
        if t is None:
            self.btn_listen.setChecked(False)
            return
        center = t.center - LISTEN_IF
        self.engine.center = center
        self.engine.fs = LISTEN_RATE
        with self.engine.lock:
            d = NrDemod(LISTEN_RATE, t.mode, t.bw or None)
            d.nr_on = self.cb_nr.isChecked() and t.mode in ("NFM", "AM", "USB", "LSB")
            d.squelch_db = -200.0 if self.sql.value() <= -120 else float(self.sql.value())
            d.offset = LISTEN_IF
            self.engine.demod = d
        kind = "test" if src == "test" else "rtl"
        self._listen_demo = None
        if kind == "test":
            def factory(fs, _std, mode=t.mode, f0=self.l_vfo.value() * 1e6):
                self._listen_demo = _ListenDemo(fs, mode, f0)
                return self._listen_demo
            self.engine.generator = factory
        self.engine.start(src if kind == "rtl" else "test", 0, self.gain_combo.currentData())
        if kind == "rtl":
            self.engine.cmd("ppm", self.ppm.value())
            if self.cb_bias.isChecked():
                self.engine.cmd("bias", True)
        self.listen = dict(sat=sat.norad, tx=t, center=center, vfo=self.l_vfo.value() * 1e6, dopp=0.0)
        self.btn_listen.setChecked(True)
        self.btn_listen.setText("■ Остановить")
        self.l_sat.setEnabled(False)
        self.tabs.setCurrentIndex(TAB_LISTEN)
        self._listen_tick()
        self.statusBar().showMessage(f"Слушаю {sat.name}: {t.label}, {t.mode}", 8000)

    def _retune_listen(self):
        t = self._ltx()
        if not t or not self.listen:
            return
        with self.engine.lock:
            self.engine.demod.set_mode(t.mode, t.bw or None)
            self.engine.demod.nr_on = self.cb_nr.isChecked() and t.mode in ("NFM", "AM", "USB", "LSB")
        self.listen["tx"] = t
        self.listen["vfo"] = self.l_vfo.value() * 1e6
        self._recenter(t.center - LISTEN_IF)

    def _recenter(self, center):
        self.listen["center"] = center
        self.engine.set_center(center)

    def stop_listen(self):
        if self.engine.src is not None and getattr(self.engine.src, "HARDWARE", False):
            self.engine.cmd("bias", False)
        self.engine.stop()
        self.listen = None
        self.btn_listen.setChecked(False)
        self.btn_listen.setText("▶ Слушать")
        self.l_sat.setEnabled(True)
        self.l_count.setText("")

    def _listen_tick(self):
        L = self.listen
        now = time.time()
        o = self.orbits.get(L["sat"])
        f = L["vfo"]
        dop = 0.0
        txt = ""
        if o is not None:
            try:
                az, el, rng, rr = o.look(now, self.lat.value(), self.lon.value())
                dop = -f * rr / satpass.C_LIGHT
                txt = f"Азимут {az:.0f}°, возвышение {el:.0f}°, дальность {rng:.0f} км. "
            except (ValueError, FloatingPointError):
                pass
        L["dopp"] = dop
        rxf = f + dop
        demo = getattr(self, "_listen_demo", None)
        if demo is not None:
            demo.sat.freq = rxf  # демо: спутник «звучит» там, где он был бы с доплером
        off = rxf - L["center"]
        if abs(off) > LISTEN_RATE / 2 - 40e3:
            self._recenter(rxf - LISTEN_IF)
            off = LISTEN_IF
        self.engine.configure(offset=off)
        self.l_dopp.setText(f"{txt}Доплер {dop / 1e3:+.2f} кГц → приём на {rxf / 1e6:.4f} МГц".replace(".", ","))
        t = L["tx"]
        self.lspec.markers = [(rxf, "#ff6a4d", t.mode)]
        self.lspec.bands = [(t.freq + dop, t.hi + dop, "#4fb3ff")] if t.is_band else []
        cur = [p for p in self.passes if p.norad == L["sat"] and p.aos <= now <= p.los]
        nxt = [p for p in self.passes if p.norad == L["sat"] and p.aos > now]
        if cur:
            self.l_count.setText(f"<b>Пролёт идёт</b>: до LOS {_fmt_dt(cur[0].los - now)} "
                                 f"(макс. {cur[0].max_el:.0f}°)")
        elif nxt:
            self.l_count.setText(f"До AOS {_fmt_dt(nxt[0].aos - now)} ({_fmt_d(nxt[0].aos)}, "
                                 f"макс. {nxt[0].max_el:.0f}°)")
        else:
            self.l_count.setText("Пролётов в ближайшие 36 ч нет.")

    def _vol_changed(self, v):
        self.engine.audio.volume = v / 100
        self.s["volume"] = v

    def _sql_changed(self, v):
        self.engine.configure(squelch_db=-200.0 if v <= -120 else float(v))
        self.s["squelch"] = v

    def _nr_changed(self, on):
        self.s["nr"] = on
        t = self._ltx()
        self.engine.demod.nr_on = on and t is not None and t.mode in ("NFM", "AM", "USB", "LSB")

    # ================= «Электро-Л» =================
    def _egeo(self):
        if not hasattr(self, "e_sat"):
            return
        n = self.e_sat.currentData()
        name, lon = elektro.SATS[n]
        o = self.eorbits.get(n)
        src = "номинал"
        if o is not None:
            try:
                _la, lo, _h = o.subpoint(time.time())
                if np.isfinite(lo):
                    lon, src = float(lo), "по TLE"
            except (ValueError, FloatingPointError):
                pass
        az, el, rng = elektro.geo_look(self.lat.value(), self.lon.value(), lon)
        vis = "виден" if el > 5 else ("низко над горизонтом" if el > 0 else "за горизонтом — не виден")
        self.e_geo.setText(
            f"Долгота {abs(lon):.2f}° {'в. д.' if lon >= 0 else 'з. д.'} ({src}).<br>"
            f"Антенна: <b>азимут {az:.1f}°, угол места {el:.1f}°</b>, дальность {rng:.0f} км — {vis}.")
        self.s["e_sat"] = n

    def _ui_lrit(self, w, snap, st):
        self.e_carrier.set_value(st["carrier"] * 100, f"{st['carrier'] * 100:.0f} %")
        self.e_rs.set_value(st["rs_pct"], f"{st['rs_pct']:.0f} % · {st['frames_ok']}")
        sync = "есть" if st["sync"] else ("поиск" if st["frames"] == 0 else "потеряна")
        self.e_info.setText(
            f"Синхронизация кадров: <b>{sync}</b>. Остаток частоты {st['residual_hz'] - (w.sig - w.center):+.0f} Гц, "
            f"SNR ≈ {st['snr_db']:.1f} dB. Пакетов {st['packets']}, файлов {st['files']}, ошибок CRC "
            f"{st['crc_bad']}, снимков {st['images']}. Загрузка DSP {st['busy'] * 100:.0f} %.")
        if st["imgver"] != self._e_ver and time.time() - getattr(self, "_e_t", 0) > 3.0 \
                and self.tabs.currentIndex() == TAB_ELEKTRO:
            self._e_t = time.time()
            self._e_ver = st["imgver"]
            self.erx = w.rx
            with w.lock:
                self._erefresh()

    def _erefresh(self):
        rx = self.erx
        if rx is None:
            return
        cur = self.e_img.currentData()
        self.e_img.blockSignals(True)
        self.e_img.clear()
        times = sorted({(k[0], k[1]) for k in rx.asm.keys()})
        for sat, tm in times:
            if all((sat, tm, c) in rx.asm.groups for c in (1, 2, 3)):
                self.e_img.addItem(f"{sat} {tm} — цвет 321", ("rgb", sat, tm))
        for k in rx.asm.keys():
            got, tot = rx.asm.complete(k)
            self.e_img.addItem(f"{k[0]} {k[1]} — канал {k[2]} ({got}/{tot} сегм.)", ("ch",) + k)
        i = self.e_img.findData(cur) if cur is not None else -1
        self.e_img.setCurrentIndex(max(0, i))
        self.e_img.blockSignals(False)
        self.e_files.clear()
        for nm in rx.names[-60:]:
            self.e_files.addItem(nm)
        self._eshow()

    def _earray(self, key):
        rx = self.erx
        if rx is None or key is None:
            return None
        if key[0] == "rgb":
            return rx.asm.composite(key[1], key[2], "321")
        return rx.asm.image(key[1:])

    def _eshow(self):
        arr = self._earray(self.e_img.currentData())
        if arr is not None:
            self.e_view.set_array(arr, fit=True)

    def _esave(self, manual=False):
        rx = self.erx
        if rx is None or not rx.asm.groups:
            if manual:
                self.statusBar().showMessage("Нечего сохранять.", 5000)
            return "снимков нет"
        d = (self.opts.get("pic_dir") or ELEKTRO_DIR) if manual or not self.opts.get("shot") else self.opts.get("pic_dir")
        if not d:
            return "не сохранено (режим снимка окна)"
        os.makedirs(d, exist_ok=True)
        meta = dict(getattr(self, "e_meta", None) or {})
        n = 0
        for i in range(self.e_img.count()):
            key = self.e_img.itemData(i)
            arr = self._earray(key)
            if arr is None:
                continue
            prod = "композит 321" if key[0] == "rgb" else f"канал {key[3]}"
            name = f"elektro_{key[1]}_{key[2]}_" + ("rgb321" if key[0] == "rgb" else f"ch{key[3]}") + ".png"
            _save_png(arr, os.path.join(d, name))
            write_meta(os.path.join(d, name), dict(meta, product=prod, image_time=key[2],
                                                    sat=meta.get("sat") or "Электро-Л №" + key[1][-1]))
            n += 1
        return f"сохранено {n} PNG в {d}"

    # ================= служебное =================
    def _save(self):
        if not self.opts.get("no_save") and not self.opts.get("shot"):
            settings.save("sat", self.s)

    def apply_theme(self, t):
        self._accent = t["accent"]
        self.table.setStyleSheet(f"QTableWidget {{ gridline-color: {t['grid']}; }}")
        self.map_lbl.setStyleSheet(f"QLabel {{ background: {t['panel']}; color: {t['fg']}; padding: 3px 7px; "
                                   f"border-radius: 4px; border: 1px solid {t['grid']}; }}")
        self.map_lbl.adjustSize()
        self.map.set_theme(t)
        for w in (self.spec, self.lspec, self.e_spec, self.cons, self.e_cons, self.m_carrier, self.m_rs,
                  self.l_level, self.e_carrier, self.e_rs):
            w.set_theme(t)

    def shutdown(self):
        self.timer.stop()
        self.ui_timer.stop()
        self.spec_timer.stop()
        if self.filedec is not None:
            self.filedec.cancelled = True
        self.stop_listen()
        self.stop_rx()
        self.s["zoom"] = self.map.zoom
        self._save()
        sys.setswitchinterval(self._switch)


def _save_png(arr, path):
    a = np.ascontiguousarray(arr)
    if a.ndim == 2:
        QImage(a.data, a.shape[1], a.shape[0], a.shape[1], QImage.Format_Grayscale8).save(path)
    else:
        QImage(a.data, a.shape[1], a.shape[0], a.shape[1] * 3, QImage.Format_RGB888).save(path)
