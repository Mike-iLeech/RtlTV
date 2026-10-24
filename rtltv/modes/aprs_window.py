"""Режим APRS: пакеты радиолюбителей на карте — 144,800 МГц (наземная сеть) и МКС 145,825 МГц.

Звук NFM 48 кГц с RadioEngine уходит в отдельный поток декодера (rtltv.aprs), принятые пакеты
раскладываются по станциям: карта (значки по типу, треки движущихся), список станций, лента пакетов,
детали станции. Для МКС — коррекция доплера по TLE, отметка пролёта и обратный отсчёт.
"""
import math
import os
import queue
import threading
import time

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QScrollArea, QSizePolicy,
                               QSlider, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from .. import aprs, devices, settings
from ..mapview import MapView
from ..modebase import ModeWindow
from ..radiotest import RadioTestSignal, Station
from ..widgets import FreqDisplay, LevelMeter, SpectrumView
from .radio_window import RadioEngine, rtlsdr_gains
from .. import paths

F_GROUND = 144.800e6
F_ISS = 145.825e6
ISS_NORAD = 25544
HOME = (55.75580, 37.61730)
RATE = 960000
PRESETS = [
    ("ground", "144,800 МГц — APRS (Россия, Европа)", F_GROUND),
    ("iss", "145,825 МГц — МКС (ARISS), доплер по TLE", F_ISS),
    ("custom", "Своя частота", None),
]
AGES = [(600, "10 мин"), (1800, "30 мин"), (3600, "1 час"), (3 * 3600, "3 часа"), (6 * 3600, "6 часов"),
        (24 * 3600, "сутки"), (0, "все")]
DEFAULTS = {"preset": "ground", "custom_freq": 144.390e6, "gain_idx": 22, "volume": 20, "source": "rtl:0",
            "home_lat": HOME[0], "home_lon": HOME[1], "doppler": True, "log": False, "filter": "",
            "kind": "", "only_iss": False, "max_age": 0, "zoom": 9}
LOG_DIR = paths.logs(paths.LOGS_APRS)
ST_COLS = ["Позывной", "Тип", "Координаты", "Расст., км", "Последний", "Пакетов", "Путь"]
FEED_COLS = ["Время", "От", "Путь", "Расшифровка", "Пакет (сырой)"]
FEED_MAX = 800


# ======================================================================= тестовый эфир

def _test_frames(home):
    """Пакеты тестового эфира 144,800: машина по кругу (Mic-E) вперемешку со станциями всех типов."""
    lat0, lon0 = home
    others = [
        "R2ABC>APDR15,WIDE1-1:=5541.00N/03730.00E-QTH R2ABC, Москва",
        "RA3WX-13>APRS,R3DIG-1*,WIDE2-1:@231200z5539.20N/03735.40E_090/005g010t046r000p001P002h65b10132wRSW",
        "R3DIG-1>APRS:!5545.00N/03715.00E#Digipeater R3DIG-1 W1",
        "UB3XYZ-7>APRS,WIDE1-1:" + aprs.encode_compressed(55.80, 37.20, "/", "k", course=120, speed_kn=30,
                                                           comment="Грузовик, сжатая позиция"),
        "R2ABC>APRS:;LEADER   *231200z5542.00N/03740.00E>Колонна",
        "R2ABC>APRS:)AID#2!5540.50N/03738.00EA",
        "R2ABC>APRS::UA3ABC-9 :Привет, как слышно?{12",
        "UA3ABC-9>APRS::R2ABC    :ack12",
        "R2ABC>APRS:>Слушаю 144.800",
        "RA3WX-13>APRS:T#123,100,200,050,000,255,10101010",
        "R3IGT-10>APRS:!5535.00N\\03750.00E&I-Gate Москва-Юг",
        "UA3ABC-9>APRS:>В пути, QRV 145.500",
    ]
    frames = []
    for k in range(12):
        a = 2 * math.pi * k / 12
        lat = lat0 + 0.03 * math.cos(a)
        lon = lon0 + 0.05 * math.sin(a)
        course = (math.degrees(a) + 90) % 360
        dest, info = aprs.encode_mice(lat, lon, speed_kn=32, course=course, msg=6, comment="Мобильный",
                                      alt_m=180, radio="]=")
        # первые 9 знаков Mic-E — байты 28…127, комментарий — UTF-8
        frames.append(aprs.make_frame("UA3ABC-9", dest, ["WIDE1-1", "WIDE2-1"],
                                      info[:9].encode("latin-1") + info[9:].encode("utf-8")))
        frames.append(aprs.tnc2_frame(others[k]))
    return frames


def _iss_frames():
    lines = [
        "UT1FG>CQ,RS0ISS*,WIDE2-1:=5021.00N/03015.00E-Привет через МКС",
        "RS0ISS>CQ:>ARISS - International Space Station",
        "OH2XYZ-7>APRS,RS0ISS*:!6010.00N/02455.00E>Мобильный через ARISS",
        "RS0ISS>APRS,RS0ISS*::RA3ABC   :Привет с орбиты{7",
    ]
    return [aprs.tnc2_frame(t) for t in lines]


def _clip(frames, period, seed):
    """Пакеты по расписанию в петле длиной period с: звук и огибающая несущей."""
    n = int(period * 48000)
    audio = np.zeros(n, np.float32)
    env = np.zeros(n, np.float32)
    step = period / len(frames)
    rng = np.random.default_rng(seed)
    for k, fr in enumerate(frames):
        a = aprs.encode_frame_audio(fr, preamble=30, amp=0.6, space_gain=1.4)
        i0 = int((k * step + 0.4 + 0.3 * rng.random()) * 48000)
        i1 = min(n, i0 + a.size)
        audio[i0:i1] = a[:i1 - i0]
        env[max(0, i0 - 2400):min(n, i1 + 2400)] = 1.0
    return audio, env


class AprsTestSignal(RadioTestSignal):
    """Тестовый эфир + APRS: наземная сеть на 144,800 МГц и пакеты через МКС на 145,825 МГц."""

    def __init__(self, fs, std=None, home=HOME):
        super().__init__(fs, std)
        self.home = home
        self.stations.append(Station(F_GROUND, "aprs", 0.12))
        self.stations.append(Station(F_ISS, "aprs_iss", 0.05))
        self._clips = {}

    def _aprs_clip(self, kind):
        c = self._clips.get(kind)
        if c is None:
            if kind == "aprs":
                c = _clip(_test_frames(self.home), 60.0, 1)
            else:
                c = _clip(_iss_frames(), 40.0, 2)
            self._clips[kind] = c
        return c

    def _station(self, st, t, off):
        if st.kind in ("aprs", "aprs_iss"):
            audio, env = self._aprs_clip(st.kind)
            a = self._clip_at(audio, t)
            on = self._clip_at(env, t)
            return self._fm(st, t, off, 3000 * a) * on
        return super()._station(st, t, off)


def test_factory(fs, std=None):
    return AprsTestSignal(fs, std)


# ======================================================================= поток декодера

class DecoderThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.inq = queue.Queue(maxsize=400)
        self.outq = queue.Queue()
        self.demod = aprs.AfskDemodulator(48000)
        self.alive = True
        self.cpu = 0.0
        self.audio_s = 0.0
        self.busy = False
        self.err = None

    def feed(self, mono):
        try:
            self.inq.put_nowait(np.asarray(mono, np.float32).copy())
        except queue.Full:
            pass

    def run(self):
        while self.alive:
            try:
                x = self.inq.get(timeout=0.2)
            except queue.Empty:
                continue
            chunks = [x]
            while True:
                try:
                    chunks.append(self.inq.get_nowait())
                except queue.Empty:
                    break
            x = np.concatenate(chunks)
            t0 = time.perf_counter()
            try:
                frames = self.demod.process(x)
            except Exception as e:  # noqa: BLE001
                self.err = str(e)
                continue
            self.cpu = 0.9 * self.cpu + 0.1 * (time.perf_counter() - t0) / max(1e-3, x.size / 48000)
            self.audio_s += x.size / 48000
            self.busy = self.demod.busy()
            for f in frames:
                p = aprs.parse_frame(f["data"])
                if p is None:
                    continue
                p["decoders"] = f["decoders"]
                p["fixed"] = f["fixed"]
                p["rx_time"] = time.time()
                self.outq.put(p)

    def stop(self):
        self.alive = False


# ======================================================================= вспомогательное

class _NumItem(QTableWidgetItem):
    def __init__(self, text, key):
        super().__init__(text)
        self.key = key

    def __lt__(self, other):
        k2 = getattr(other, "key", None)
        if k2 is None:
            return super().__lt__(other)
        return self.key < k2


def _wrap(text, n=28):
    """Длинные слова без пробелов — с точками переноса (чтобы панель не расползалась вширь)."""
    out = []
    for w in str(text).split(" "):
        if len(w) > n:
            w = "​".join(w[i:i + n] for i in range(0, len(w), n))
        out.append(w)
    return " ".join(out)


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ago(sec):
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec} с назад"
    if sec < 3600:
        return f"{sec // 60} мин назад"
    return f"{sec // 3600} ч {sec % 3600 // 60:02d} мин назад"


def _hms(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# ======================================================================= окно

class AprsWindow(ModeWindow):
    def __init__(self, opts=None):
        super().__init__(opts, "RtlTV — APRS")
        self.s = settings.load("aprs", DEFAULTS)
        # снимок для автотестов: базовый класс обнуляет opts["shot"] до закрытия — запоминаем сразу
        self._nosave = bool(self.opts.get("shot") or self.opts.get("nosave"))
        if self.opts.get("source"):
            self.s["source"] = self.opts["source"]
        self.engine = RadioEngine()
        self.engine.set_rate(RATE)
        self.engine.generator = test_factory
        self.engine.configure(mode="NFM", squelch_db=-200.0)
        self.gains = list(rtlsdr_gains())
        self.stations = {}
        self.n_packets = 0
        self.n_iss = 0
        self.orbit = None
        self.passes = []
        self.passes_t = 0.0
        self.tle_note = "TLE: загрузка…"
        self.doppler_hz = 0.0
        self.selected = None
        self.dirty = True
        self.log_f = None
        self.decoder = DecoderThread()
        self.decoder.start()
        self.engine.taps.append(self.decoder.feed)
        self._build()
        e = self.engine
        e.spectrum.connect(self._on_spectrum)
        e.level.connect(lambda db, _a, _s: self.lvl.set_value(db, f"{db:.0f} дБ"))
        e.error.connect(lambda m: self.statusBar().showMessage("Ошибка: " + m, 8000))
        self.t_pump = QTimer(self)
        self.t_pump.timeout.connect(self._pump)
        self.t_pump.start(200)
        self.t_view = QTimer(self)
        self.t_view.timeout.connect(self._refresh)
        self.t_view.start(1000)
        self.t_iss = QTimer(self)
        self.t_iss.timeout.connect(self._iss_tick)
        self.t_iss.start(1000)
        threading.Thread(target=self._load_tle, daemon=True).start()
        if self.s.get("log"):
            self.cb_log.setChecked(True)
        QTimer.singleShot(150, self.start)

    # ================= интерфейс =================
    def _build(self):
        self.map = MapView()
        self.map.set_center(self.s["home_lat"], self.s["home_lon"], int(self.s.get("zoom", 9)))
        self.map.itemClicked.connect(self._select)
        self.map.clicked.connect(self._map_clicked)

        self.table = QTableWidget(0, len(ST_COLS))
        self.table.setHorizontalHeaderLabels(ST_COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(4, Qt.AscendingOrder)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.setStretchLastSection(True)
        for i, w in enumerate((110, 120, 150, 80, 110, 70)):
            self.table.setColumnWidth(i, w)
        self.table.itemSelectionChanged.connect(self._table_selected)

        self.feed = QTableWidget(0, len(FEED_COLS))
        self.feed.setHorizontalHeaderLabels(FEED_COLS)
        self.feed.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.feed.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.feed.verticalHeader().setVisible(False)
        self.feed.setWordWrap(False)
        fh = self.feed.horizontalHeader()
        fh.setSectionResizeMode(QHeaderView.Interactive)
        fh.setStretchLastSection(True)
        for i, w in enumerate((70, 95, 110, 430)):
            self.feed.setColumnWidth(i, w)
        self.feed.itemSelectionChanged.connect(self._feed_selected)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.table, "Станции")
        self.tabs.addTab(self.feed, "Пакеты")

        self.spec = SpectrumView(waterfall=True)
        self.spec.setMinimumHeight(180)
        self.spec.tuneRequested.connect(lambda f: self._custom_freq(round(f / 500) * 500))
        self.spec.wheelStep.connect(lambda d: self._custom_freq(self.fdisp.value + d * 500))

        left = QSplitter(Qt.Vertical)
        left.addWidget(self.map)
        left.addWidget(self.tabs)
        left.addWidget(self.spec)
        left.setStretchFactor(0, 3)
        left.setStretchFactor(1, 2)
        left.setStretchFactor(2, 1)
        left.setSizes([400, 220, 190])
        left.setChildrenCollapsible(False)

        side = QWidget()
        sv = QVBoxLayout(side)
        sv.setContentsMargins(4, 4, 4, 4)
        sv.setSpacing(6)
        sv.addWidget(self._grp_rx())
        sv.addWidget(self._grp_iss())
        sv.addWidget(self._grp_station())
        sv.addWidget(self._grp_filter())
        sv.addWidget(self._grp_home())
        sv.addWidget(self._grp_log())
        sv.addWidget(self._legend())
        sv.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidget(side)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setMinimumWidth(340)
        self.scroll.setMaximumWidth(400)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(self.scroll)
        split.setStretchFactor(0, 1)
        split.setChildrenCollapsible(False)
        self.setCentralWidget(split)
        self.sb = QLabel("")
        self.statusBar().addWidget(self.sb, 1)
        self._apply_home(save=False)

    def _label(self, text="—", dim=False):
        lb = QLabel(text)
        lb.setWordWrap(True)
        lb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lb.setMinimumWidth(10)
        if dim:
            lb.setObjectName("dim")
        return lb

    def _grp_rx(self):
        g = QGroupBox("Приём")
        f = QFormLayout(g)
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.preset = QComboBox()
        for key, name, _f in PRESETS:
            self.preset.addItem(name, key)
        self.preset.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.preset.setMinimumContentsLength(12)
        self.preset.setCurrentIndex(max(0, self.preset.findData(self.s["preset"])))
        self.preset.activated.connect(lambda _i: self._preset_changed())
        f.addRow(self.preset)
        self.fdisp = FreqDisplay(lo=24e6, hi=1766e6)
        self.fdisp.setMinimumWidth(10)
        self.fdisp.changed.connect(self._custom_freq)
        f.addRow(self.fdisp)
        self.src_combo = QComboBox()
        self.src_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.src_combo.setMinimumContentsLength(12)
        devices.fill_source_combo(self.src_combo, self.s["source"], "Тестовый эфир (без антенны)", keep_missing=False)
        self.src_combo.activated.connect(lambda _i: self.start())
        f.addRow("Источник:", self.src_combo)
        row = QHBoxLayout()
        self.gain = QSlider(Qt.Horizontal)
        self.gain.setRange(0, len(self.gains) - 1)
        self.gain.setValue(min(self.s["gain_idx"], len(self.gains) - 1))
        self.gain.valueChanged.connect(self._gain)
        self.gain_lbl = QLabel("")
        self.gain_lbl.setMinimumWidth(56)
        row.addWidget(self.gain, 1)
        row.addWidget(self.gain_lbl)
        f.addRow("Усиление:", row)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 150)
        self.vol.setValue(self.s["volume"])
        self.vol.setToolTip("Прослушка эфира: пакеты APRS звучат короткими «щелчками-трелями»")
        self.vol.valueChanged.connect(lambda v: setattr(self.engine.audio, "volume", v / 100))
        self.engine.audio.volume = self.s["volume"] / 100
        f.addRow("Громкость:", self.vol)
        self.lvl = LevelMeter(-110, -10, "дБ")
        f.addRow("Сигнал:", self.lvl)
        self.dcd = self._label("—")
        f.addRow("Декодер:", self.dcd)
        self._gain()
        return g

    def _grp_iss(self):
        g = QGroupBox("МКС (RS0ISS, 145,825 МГц)")
        v = QVBoxLayout(g)
        self.iss_next = self._label("Пролёт: —")
        self.iss_now = self._label("Сейчас: —")
        self.iss_tle = self._label(self.tle_note, dim=True)
        self.cb_doppler = QCheckBox("Коррекция доплера по TLE")
        self.cb_doppler.setChecked(bool(self.s["doppler"]))
        v.addWidget(self.iss_next)
        v.addWidget(self.iss_now)
        v.addWidget(self.cb_doppler)
        v.addWidget(self.iss_tle)
        return g

    def _grp_station(self):
        g = QGroupBox("Выбранная станция")
        v = QVBoxLayout(g)
        self.details = self._label("Щёлкните станцию на карте или в списке.")
        self.details.setTextFormat(Qt.RichText)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.details)
        b = QPushButton("Показать на карте")
        b.clicked.connect(self._center_selected)
        v.addWidget(b)
        return g

    def _grp_filter(self):
        g = QGroupBox("Фильтр")
        f = QFormLayout(g)
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.flt = QLineEdit(self.s.get("filter", ""))
        self.flt.setPlaceholderText("позывной или текст")
        self.flt.setClearButtonEnabled(True)
        self.flt.textChanged.connect(self._filter_changed)
        f.addRow("Поиск:", self.flt)
        self.flt_kind = QComboBox()
        self.flt_kind.addItem("Все типы", "")
        for k, (name, _c) in aprs.KINDS.items():
            self.flt_kind.addItem(name, k)
        self.flt_kind.setCurrentIndex(max(0, self.flt_kind.findData(self.s.get("kind", ""))))
        self.flt_kind.currentIndexChanged.connect(self._filter_changed)
        f.addRow("Тип:", self.flt_kind)
        self.flt_age = QComboBox()
        for sec, name in AGES:
            self.flt_age.addItem(name, sec)
        self.flt_age.setCurrentIndex(max(0, self.flt_age.findData(self.s.get("max_age", 0))))
        self.flt_age.currentIndexChanged.connect(self._filter_changed)
        f.addRow("Не старше:", self.flt_age)
        self.flt_iss = QCheckBox("Только через МКС")
        self.flt_iss.setChecked(bool(self.s.get("only_iss")))
        self.flt_iss.toggled.connect(self._filter_changed)
        f.addRow(self.flt_iss)
        b = QPushButton("Очистить список станций")
        b.clicked.connect(self._clear)
        f.addRow(b)
        return g

    def _grp_home(self):
        g = QGroupBox("Дом")
        f = QFormLayout(g)
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
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
        self.cb_home_click = QCheckBox("Щелчок по карте задаёт дом")
        f.addRow(self.cb_home_click)
        return g

    def _grp_log(self):
        g = QGroupBox("Журнал")
        v = QVBoxLayout(g)
        self.cb_log = QCheckBox("Писать пакеты в файл")
        self.cb_log.toggled.connect(self._log_toggled)
        v.addWidget(self.cb_log)
        b = QPushButton("Открыть папку журналов")
        b.clicked.connect(lambda: (os.makedirs(LOG_DIR, exist_ok=True), os.startfile(LOG_DIR)))
        v.addWidget(b)
        return g

    def _legend(self):
        g = QGroupBox("Обозначения")
        v = QVBoxLayout(g)
        rows = []
        for _k, (name, col) in aprs.KINDS.items():
            rows.append(f"<span style='color:{col}'>●</span> {name}")
        half = (len(rows) + 1) // 2
        html = "<table width='100%'>" + "".join(
            f"<tr><td>{rows[i]}</td><td>{rows[i + half] if i + half < len(rows) else ''}</td></tr>"
            for i in range(half)) + "</table>"
        lb = self._label(html)
        lb.setTextFormat(Qt.RichText)
        v.addWidget(lb)
        return g

    # ================= приём =================
    def _src(self):
        return self.src_combo.currentData()

    def start(self):
        src = self._src()
        if src == "none":
            return
        self.engine.start(src, 0, self._gain_tenths())
        self._preset_changed(initial=True)

    def _gain_tenths(self):
        return self.gains[min(self.gain.value(), len(self.gains) - 1)]

    def _gain(self):
        t = self._gain_tenths()
        self.gain_lbl.setText(f"{t / 10:.1f} дБ")
        self.engine.cmd("gain", t)

    def _preset_key(self):
        return self.preset.currentData()

    def _base_freq(self):
        key = self._preset_key()
        if key == "custom":
            return float(self.s["custom_freq"])
        return next(f for k, _n, f in PRESETS if k == key)

    def _preset_changed(self, initial=False):
        key = self._preset_key()
        self.engine.configure(mode="NFM", bw=16000 if key == "iss" else 12500, squelch_db=-200.0)
        self._tune(self._base_freq(), force=not initial)

    def _custom_freq(self, f):
        f = float(min(max(f, 24e6), 1766e6))
        if abs(f - self._base_freq()) < 1:
            return
        self.s["custom_freq"] = f
        self.preset.setCurrentIndex(self.preset.findData("custom"))
        self.engine.configure(mode="NFM", bw=12500, squelch_db=-200.0)
        self._tune(f)

    def _doppler_active(self):
        return (self._preset_key() == "iss" and self.cb_doppler.isChecked() and self.orbit is not None
                and self._src() != "test")

    def _tune(self, f, force=False):
        self.fdisp.set_value(f)
        e = self.engine
        if force or abs(f - e.center) > 0.35 * RATE or abs(f - e.center) < 0.05 * RATE:
            e.set_center(f - 0.2 * RATE)
        shift = self.doppler_hz if self._doppler_active() else 0.0
        e.configure(offset=f + shift - e.center)
        bw = e.demod.bw
        fr = f + shift
        self.spec.bands = [(fr - bw / 2, fr + bw / 2, "#4fb3ff")]
        self.spec.markers = [(f, "#ffd84d", f"{f / 1e6:.3f}")]

    def _on_spectrum(self, d):
        n = d["psd"].size
        freqs = d["center"] + (np.arange(n) - n // 2) * (d["fs"] / n)
        self.spec.set_data(freqs, d["psd"])

    # ================= МКС =================
    def _load_tle(self):
        try:
            from .. import satpass
            tles, note = satpass.load_tle(groups=("stations",), timeout=8.0)
            t = tles.get(ISS_NORAD)
            if t is None:
                self.tle_note = f"TLE МКС не найден ({note})"
                return
            self.orbit = satpass.Orbit(t)
            age = (time.time() - t.epoch_unix) / 86400
            self.tle_note = f"TLE МКС: эпоха {age:.1f} сут. назад; {note}"
        except Exception as e:  # noqa: BLE001
            self.tle_note = f"TLE МКС недоступен: {e}"

    def _iss_tick(self):
        self.iss_tle.setText(self.tle_note)
        o = self.orbit
        if o is None:
            return
        now = time.time()
        lat, lon = self.lat.value(), self.lon.value()
        try:
            if not self.passes or now - self.passes_t > 600 or self.passes[0].los < now:
                self.passes = [p for p in o.passes(lat, lon, now, 24.0, 0.0) if p.los > now]
                self.passes_t = now
            az, el, rng, _rr = o.look(now, lat, lon)
            self.doppler_hz = float(o.doppler(now, F_ISS, lat, lon))
            slat, slon, alt = o.subpoint(now)
        except Exception as e:  # noqa: BLE001
            self.iss_now.setText(f"Ошибка расчёта орбиты: {e}")
            return
        p = self.passes[0] if self.passes else None
        if p is not None and p.aos <= now <= p.los:
            self.iss_next.setText(f"<b style='color:#ff5cf0'>МКС в зоне!</b> До ухода {_hms(p.los - now)}, "
                                  f"макс. возвышение {p.max_el:.0f}°")
        elif p is not None:
            self.iss_next.setText(f"Пролёт: {time.strftime('%H:%M:%S', time.localtime(p.aos))} "
                                  f"(через {_hms(p.aos - now)}), макс. {p.max_el:.0f}°, "
                                  f"{p.duration / 60:.0f} мин")
        else:
            self.iss_next.setText("Пролётов в ближайшие сутки нет")
        self.iss_next.setTextFormat(Qt.RichText)
        dop = f"доплер {self.doppler_hz / 1000:+.2f} кГц"
        if self._preset_key() == "iss":
            if self._src() == "test":
                dop += " (тестовый эфир: не применяется)"
            elif not self.cb_doppler.isChecked():
                dop += " (коррекция выключена)"
            else:
                dop += " (учтён)"
        self.iss_now.setText(f"Сейчас: аз. {az:.0f}°, возв. {el:.0f}°, {rng:.0f} км; {dop}")
        if self._preset_key() == "iss":
            self._tune(F_ISS)
        self.map.set_items("iss", [{"key": "iss:orbit", "lat": slat, "lon": slon, "kind": "sat",
                                    "label": f"МКС {alt:.0f} км", "color": aprs.KINDS["iss"][1]}])
        if int(now) % 30 == 0 or "iss" not in self.map.tracks:
            from ..satpass import visibility_circle
            tr = o.track(now, before_min=30, after_min=60, step_s=30)
            foot = visibility_circle(slat, slon, alt_km=max(alt, 300.0), min_el=0.0)
            self.map.set_tracks("iss", [{"points": tr, "color": aprs.KINDS["iss"][1], "width": 1.2, "dash": True},
                                        {"points": foot, "color": "#a04a98", "width": 1.0, "dash": True}])

    # ================= пакеты =================
    def _pump(self):
        n = 0
        while n < 200:
            try:
                p = self.decoder.outq.get_nowait()
            except queue.Empty:
                break
            n += 1
            self._on_packet(p)
        d = self.decoder
        st = "<b style='color:#ffd84d'>приём пакета…</b>" if d.busy else "ожидание"
        self.dcd.setTextFormat(Qt.RichText)
        self.dcd.setText(f"{st} · {d.demod.n_decoders} срезателей · ЦП {d.cpu * 100:.0f} %")

    def _key(self, p):
        if p.get("type") in ("object", "item") and p.get("name"):
            return "obj:" + p["name"]
        return p["src"]

    def _on_packet(self, p):
        now = p.get("rx_time", time.time())
        self.n_packets += 1
        if p.get("via_iss"):
            self.n_iss += 1
        key = self._key(p)
        st = self.stations.get(key)
        if st is None:
            st = self.stations[key] = {"key": key, "call": p.get("name") if key.startswith("obj:") else p["src"],
                                       "owner": p["src"], "kind": p["kind"], "first": now, "count": 0, "iss": 0,
                                       "track": [], "lat": None, "lon": None}
        st["count"] += 1
        st["last"] = now
        st["via"] = p["via"]
        if p.get("via_iss"):
            st["iss"] += 1
        if p["kind"] != "other" or st["kind"] == "other":
            st["kind"] = p["kind"]
        for k in ("symbol", "comment", "speed_kmh", "course", "alt_m", "wx", "status", "mice_msg", "radio",
                  "telemetry", "alive"):
            if p.get(k) not in (None, "", {}):
                st[k] = p[k]
        st["last_desc"] = aprs.describe(p)
        st["last_raw"] = p["raw"]
        st["decoders"] = p.get("decoders")
        if p.get("lat") is not None:
            pos = (p["lat"], p["lon"])
            if st["lat"] is not None and aprs.distance_km(st["lat"], st["lon"], *pos) > 0.03:
                if not st["track"]:
                    st["track"].append((st["lat"], st["lon"]))
                st["track"].append(pos)
                st["track"] = st["track"][-300:]
            st["lat"], st["lon"] = pos
        self._feed_add(p, now)
        self._log(p, now)
        self.dirty = True

    def _feed_add(self, p, now):
        f = self.feed
        f.insertRow(0)
        desc = aprs.describe(p)
        extra = []
        if p.get("decoders"):
            extra.append(f"декодеров: {p['decoders']}")
        if p.get("fixed"):
            extra.append(f"исправлено битов: {p['fixed']}")
        vals = [time.strftime("%H:%M:%S", time.localtime(now)), p["src"], p["via"], desc, p["raw"]]
        for c, v in enumerate(vals):
            it = QTableWidgetItem(v)
            it.setToolTip(_wrap(p["raw"], 60) + "\n" + _wrap(desc, 60) + ("\n" + ", ".join(extra) if extra else ""))
            it.setData(Qt.UserRole, self._key(p))
            if c == 2 and p.get("via_iss"):
                it.setForeground(Qt.magenta)
            f.setItem(0, c, it)
        if f.rowCount() > FEED_MAX:
            f.setRowCount(FEED_MAX)
        f.setRowHidden(0, not self._match_feed(p))

    def _match_feed(self, p):
        txt = self.flt.text().strip().lower()
        if txt and txt not in p["raw"].lower() and txt not in aprs.describe(p).lower():
            return False
        k = self.flt_kind.currentData()
        if k and p["kind"] != k:
            return False
        if self.flt_iss.isChecked() and not p.get("via_iss"):
            return False
        return True

    def _match(self, st, now):
        txt = self.flt.text().strip().lower()
        if txt and txt not in st["call"].lower() and txt not in str(st.get("comment", "")).lower() \
                and txt not in st.get("last_raw", "").lower():
            return False
        k = self.flt_kind.currentData()
        if k and st["kind"] != k:
            return False
        if self.flt_iss.isChecked() and not st["iss"]:
            return False
        age = self.flt_age.currentData()
        if age and now - st["last"] > age:
            return False
        return True

    def _filter_changed(self, *_a):
        self.dirty = True
        for r in range(self.feed.rowCount()):
            it = self.feed.item(r, 4)
            txt = self.flt.text().strip().lower()
            hide = False
            if txt and it is not None:
                row = " ".join((self.feed.item(r, c).text() if self.feed.item(r, c) else "") for c in range(5))
                hide = txt not in row.lower()
            key = it.data(Qt.UserRole) if it is not None else None
            st = self.stations.get(key)
            if st is not None and not hide:
                k = self.flt_kind.currentData()
                hide = bool(k and st["kind"] != k) or (self.flt_iss.isChecked() and not st["iss"])
            self.feed.setRowHidden(r, hide)
        self._refresh()

    def _clear(self):
        self.stations.clear()
        self.selected = None
        self.feed.setRowCount(0)
        self.details.setText("Щёлкните станцию на карте или в списке.")
        self.dirty = True
        self._refresh()

    # ================= отображение =================
    def _dist(self, st):
        if st["lat"] is None:
            return None
        return aprs.distance_km(self.lat.value(), self.lon.value(), st["lat"], st["lon"])

    def _refresh(self):
        now = time.time()
        if self.dirty:
            self.dirty = False
            vis = [st for st in self.stations.values() if self._match(st, now)]
            self._fill_table(vis, now)
            self._fill_map(vis)
        else:
            self._update_ages(now)
        self._show_details()
        n_pos = sum(1 for s in self.stations.values() if s["lat"] is not None)
        self.sb.setText(f"Пакетов: {self.n_packets} · станций: {len(self.stations)} (с координатами {n_pos}) · "
                        f"через МКС: {self.n_iss} · исправлено FCS: {self.decoder.demod.fixed_total}"
                        + (f" · ошибка декодера: {self.decoder.err}" if self.decoder.err else ""))

    def _fill_table(self, vis, now):
        t = self.table
        sel = self.selected
        t.setSortingEnabled(False)
        t.blockSignals(True)
        t.setRowCount(len(vis))
        for r, st in enumerate(vis):
            name, col = aprs.KINDS.get(st["kind"], aprs.KINDS["other"])
            d = self._dist(st)
            coords = "—" if st["lat"] is None else f"{st['lat']:.4f}, {st['lon']:.4f}"
            cells = [
                QTableWidgetItem(st["call"]),
                QTableWidgetItem(st.get("symbol") or name),
                QTableWidgetItem(coords),
                _NumItem("—" if d is None else f"{d:.1f}", 1e9 if d is None else d),
                _NumItem(_ago(now - st["last"]), now - st["last"]),
                _NumItem(str(st["count"]), st["count"]),
                QTableWidgetItem(st["via"] + (f" (МКС ×{st['iss']})" if st["iss"] and "МКС" not in st["via"]
                                               else "")),
            ]
            for c, it in enumerate(cells):
                it.setData(Qt.UserRole, st["key"])
                if c == 1:
                    it.setToolTip(name)
                t.setItem(r, c, it)
            t.item(r, 0).setForeground(QColor(col))
        t.setSortingEnabled(True)
        if sel:
            for r in range(t.rowCount()):
                if t.item(r, 0).data(Qt.UserRole) == sel:
                    t.selectRow(r)
                    break
        t.blockSignals(False)

    def _update_ages(self, now):
        t = self.table
        for r in range(t.rowCount()):
            it = t.item(r, 4)
            st = self.stations.get(it.data(Qt.UserRole)) if it else None
            if st:
                it.setText(_ago(now - st["last"]))
                it.key = now - st["last"]

    def _fill_map(self, vis):
        items = []
        for st in vis:
            if st["lat"] is None:
                continue
            col = aprs.KINDS.get(st["kind"], aprs.KINDS["other"])[1]
            kind = "sat" if st["kind"] == "iss" else "point"
            items.append({"key": st["key"], "lat": st["lat"], "lon": st["lon"], "kind": kind, "color": col,
                          "label": st["call"], "trail": st["track"]})
        self.map.set_items("stations", items)
        self.map.set_selected(self.selected)

    def _select(self, key):
        if key not in self.stations:
            return
        self.selected = key
        self.map.set_selected(key)
        t = self.table
        t.blockSignals(True)
        for r in range(t.rowCount()):
            if t.item(r, 0) and t.item(r, 0).data(Qt.UserRole) == key:
                t.selectRow(r)
                t.scrollToItem(t.item(r, 0))
                break
        t.blockSignals(False)
        self._show_details()

    def _table_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if rows:
            it = self.table.item(rows[0].row(), 0)
            if it is not None:
                self._select(it.data(Qt.UserRole))

    def _feed_selected(self):
        rows = self.feed.selectionModel().selectedRows()
        if rows:
            it = self.feed.item(rows[0].row(), 0)
            if it is not None:
                self._select(it.data(Qt.UserRole))

    def _center_selected(self):
        st = self.stations.get(self.selected)
        if st and st["lat"] is not None:
            self.map.set_center(st["lat"], st["lon"], max(self.map.zoom, 10))

    def _show_details(self):
        st = self.stations.get(self.selected)
        if st is None:
            return
        now = time.time()
        name, col = aprs.KINDS.get(st["kind"], aprs.KINDS["other"])
        rows = [f"<b style='color:{col}; font-size:13pt'>{_esc(st['call'])}</b>"
                + (f" <span>(от {_esc(st['owner'])})</span>" if st["owner"] != st["call"] else "")]
        rows.append(f"{_esc(st.get('symbol') or name)} · {name}")
        if st["lat"] is not None:
            d = self._dist(st)
            brg = aprs.bearing_deg(self.lat.value(), self.lon.value(), st["lat"], st["lon"])
            rows.append(f"{aprs.fmt_lat(st['lat'])}, {aprs.fmt_lon(st['lon'])}")
            rows.append(f"Локатор {aprs.locator(st['lat'], st['lon'])} · от дома {d:.1f} км, азимут {brg:.0f}°")
        mv = []
        if st.get("speed_kmh") is not None:
            mv.append(f"{st['speed_kmh']:.0f} км/ч")
        if st.get("course") is not None:
            mv.append(f"курс {st['course']:.0f}°")
        if st.get("alt_m") is not None:
            mv.append(f"высота {st['alt_m']:.0f} м")
        if mv:
            rows.append(", ".join(mv))
        if st.get("wx"):
            rows.append("Погода: " + _esc(aprs.wx_text(st["wx"])))
        if st.get("mice_msg"):
            rows.append(f"Mic-E: {_esc(st['mice_msg'])}" + (f", {_esc(st['radio'])}" if st.get("radio") else ""))
        if st.get("status"):
            rows.append("Статус: " + _esc(_wrap(st["status"])))
        if st.get("comment"):
            rows.append("Комментарий: " + _esc(_wrap(st["comment"])))
        rows.append(f"Пакетов: {st['count']}" + (f", через МКС: {st['iss']}" if st["iss"] else "")
                    + f" · {_esc(st['via'])}")
        rows.append(f"Впервые: {time.strftime('%H:%M:%S', time.localtime(st['first']))}, "
                    f"последний: {_ago(now - st['last'])}")
        rows.append("<span style='color:gray'>" + _esc(_wrap(st.get("last_raw", ""), 30)) + "</span>")
        self.details.setText("<br>".join(rows))

    # ================= дом, журнал =================
    def _apply_home(self, save=True):
        lat, lon = self.lat.value(), self.lon.value()
        self.map.set_home(lat, lon, (10, 50, 100))
        self.s["home_lat"], self.s["home_lon"] = lat, lon
        self.passes = []
        self.dirty = True

    def _map_clicked(self, lat, lon):
        if self.cb_home_click.isChecked():
            self.lat.setValue(lat)
            self.lon.setValue(lon)
            self._apply_home()

    def _log_toggled(self, on):
        if self.log_f:
            self.log_f.close()
            self.log_f = None
        if on:
            try:
                os.makedirs(LOG_DIR, exist_ok=True)
                path = os.path.join(LOG_DIR, time.strftime("aprs_%Y-%m-%d.txt"))
                self.log_f = open(path, "a", encoding="utf-8", newline="\n")
                self.statusBar().showMessage(f"Журнал пакетов: {path}", 6000)
            except OSError as e:
                self.statusBar().showMessage(f"Журнал не открыт: {e}", 8000)
                self.cb_log.setChecked(False)

    def _log(self, p, now):
        if self.log_f is None:
            return
        try:
            self.log_f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}\t{p['raw']}\t"
                             f"{p['via']}\t{aprs.describe(p)}\n")
            self.log_f.flush()
        except OSError:
            pass

    # ================= прочее =================
    def apply_theme(self, t):
        for w in (self.spec, self.fdisp, self.lvl, self.map):
            w.set_theme(t)

    def shutdown(self):
        self.engine.stop()
        self.decoder.stop()
        if self.log_f:
            self.log_f.close()
            self.log_f = None
        s = self.s
        s.update(preset=self._preset_key(), gain_idx=self.gain.value(), volume=self.vol.value(),
                 doppler=self.cb_doppler.isChecked(), log=self.cb_log.isChecked(), filter=self.flt.text(),
                 kind=self.flt_kind.currentData(), only_iss=self.flt_iss.isChecked(),
                 max_age=self.flt_age.currentData(), zoom=self.map.zoom,
                 home_lat=self.lat.value(), home_lon=self.lon.value())
        if self._src() != "none" and not self.opts.get("source"):
            s["source"] = self._src()
        if not self._nosave:
            settings.save("aprs", s)
