"""Режим ADS-B: самолёты на карте OpenStreetMap и таблица бортов (приём Mode S / ADS-B на 1090 МГц)."""
import csv
import os
import time

import threading

from PySide6.QtCore import (QAbstractTableModel, QItemSelectionModel, QModelIndex, QObject, QSortFilterProxyModel, Qt,
                            QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QScrollArea,
                               QSizePolicy, QSpinBox, QSplitter, QTableView, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)
from xml.sax.saxutils import escape

from .. import adsb, devices, settings
from ..aircraft_info import AircraftInfo
from ..mapview import MapView, lonlat_to_px
from ..modebase import ModeWindow
from ..source import R828D_GAINS
from ..widgets import LevelMeter
from .. import osplat, paths

HOME_DEFAULT = (55.7558, 37.6173)  # Москва, Красная площадь
DEFAULTS = {
    "source": "rtl:0", "gain": 496, "rtl_agc": False, "ppm": 0, "bias": False, "rate": 2000000,
    "home_lat": HOME_DEFAULT[0], "home_lon": HOME_DEFAULT[1], "rings": "50, 100, 200, 300",
    "units": "m", "trails": True, "labels": True, "log_csv": False, "zoom": 7, "keep_min": 15,
    "show_suspect": True, "hist_map": False, "hist_days": 7, "online_info": True,
}
POS_SRC = {"pair": "по паре чётн./нечётн. сообщений", "local": "по одному сообщению от прежней позиции",
           "single": "по одиночному сообщению от приёмника (ждём пару)", "surface": "на земле, от опорной точки"}
ARROWS = "↑↗→↘↓↙←↖"


HIST_DAYS = [(1, "1 день"), (7, "7 дней"), (30, "30 дней"), (0, "всегда")]
LEVEL_COLORS = {"мало": "#e8c53a", "норма": "#3ad06a", "перегрузка": "#ff4d4d"}


def when(t):
    """Время приёма: сегодня — «16:10:05», раньше — «23.10 16:10»."""
    lt = time.localtime(t)
    if time.strftime("%Y%m%d", lt) == time.strftime("%Y%m%d"):
        return time.strftime("%H:%M:%S", lt)
    return time.strftime("%d.%m %H:%M", lt)


def num(v, d=1):
    return f"{v:.{d}f}".replace(".", ",").replace("-", "−")


def restore_selection(t, key):
    """Выделить строку с ICAO=key, НЕ прокручивая таблицу к ней (иначе список нельзя листать)."""
    sm = t.selectionModel()
    sm.clearSelection()
    if not key:
        return
    for r in range(t.rowCount()):
        it = t.item(r, 0)
        if it and it.text() == key:
            sm.select(t.model().index(r, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
            return


def smooth_scroll(v):
    v.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    v.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
    v.horizontalScrollBar().setSingleStep(12)
    v.verticalScrollBar().setSingleStep(12)


def arrow(deg):
    return "" if deg is None else ARROWS[int((deg % 360) / 45 + 0.5) % 8]


def use_raw(a, show):
    """Рисовать ли борт на последней отброшенной («сырой») позиции."""
    return bool(show and a["suspect"] and a["raw_lat"] is not None
                and (a["lat"] is None or a["raw_t"] >= a["pos_t"]))
KEEP_CHOICES = [(1, "1 мин"), (5, "5 мин"), (15, "15 мин"), (60, "60 мин"), (0, "весь сеанс")]
LOG_MAX = 5000
STALE_FG = "#7d848f"
NOPOS_HINT = ("Борта не передают координаты (вероятно, подавление GNSS) — "
              "один приёмник не может определить их положение.")
HIST_COLS = ["ICAO", "Позывной", "Первый", "Последний", "Выс. мин", "Выс. макс", "Сообщ.",
             "Координаты", "Макс. дальн., км"]
LOG_DIR = paths.logs(paths.LOGS_ADSB)
COLS = ["ICAO", "Позывной", "Страна", "Высота", "Скор., км/ч", "Курс", "Вертик.", "Сквок", "Дальн., км",
        "Сообщ.", "Сигн., dB", "Приём"]
COL_TIPS = ["Адрес ICAO (24 бита)", "Позывной рейса", "Страна регистрации (по блоку ICAO-адреса)",
            "Барометрическая высота", "Путевая скорость (или IAS/TAS)",
            "Путевой угол или курс", "Вертикальная скорость", "Код ответчика Mode A", "Дальность от приёмника",
            "Принято сообщений", "Уровень сигнала, dB полной шкалы", "Когда было последнее сообщение"]
CSV_HEAD = ["время", "ICAO", "DF", "позывной", "высота_фут", "скорость_уз", "курс", "вертик_фут_мин", "сквок",
            "широта", "долгота", "дальность_км", "сигнал_dB", "кадр"]


def alt_color(alt_ft, on_ground=False):
    """Цвет по высоте: земля — серый, далее оранжевый → жёлтый → зелёный → голубой → синий → пурпурный."""
    if on_ground:
        return "#9aa0a6"
    if alt_ft is None:
        return "#d0d0d0"
    x = min(max(alt_ft, 0), 42000) / 42000.0
    hue = (25 + x * 270) / 360.0
    c = QColor.fromHsvF(hue % 1.0, 0.85, 1.0)
    return c.name()


class _NumItem(QTableWidgetItem):
    """Ячейка с числовой сортировкой (пустые значения — в конец)."""

    def __init__(self, text, key):
        super().__init__(text)
        self.key = key

    def __lt__(self, other):
        a, b = self.key, getattr(other, "key", None)
        if a is None:
            return False
        if b is None:
            return True
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


def ago_text(sec):
    if sec < 60:
        return f"{sec:.0f} с назад"
    if sec < 3600:
        return f"{sec / 60:.0f} мин назад"
    return f"{sec / 3600:.1f} ч назад"


class _LogModel(QAbstractTableModel):
    """Лента сообщений: не больше LOG_MAX строк, добавление пачками."""
    HEAD = ["Время", "ICAO", "Позывной", "Тип", "Сообщение"]

    def __init__(self):
        super().__init__()
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEAD)

    def data(self, index, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and index.isValid():
            return self.rows[index.row()][index.column()]
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEAD[section]
        return None

    def append(self, batch):
        if not batch:
            return
        batch = batch[-LOG_MAX:]
        extra = len(self.rows) + len(batch) - LOG_MAX
        if extra > 0:
            self.beginRemoveRows(QModelIndex(), 0, extra - 1)
            del self.rows[:extra]
            self.endRemoveRows()
        n = len(self.rows)
        self.beginInsertRows(QModelIndex(), n, n + len(batch) - 1)
        self.rows.extend(batch)
        self.endInsertRows()

    def clear(self):
        self.beginResetModel()
        self.rows = []
        self.endResetModel()


class _LogFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.text = ""
        self.only = None

    def set_filter(self, text, only):
        self.text, self.only = text.strip().upper(), only
        self.invalidateFilter()

    def filterAcceptsRow(self, r, _parent):
        row = self.sourceModel().rows[r]
        if self.only is not None and row[1] != self.only:
            return False
        return not self.text or self.text in (row[1] + " " + row[2]).upper()


class _Blocks(QWidget):
    """Блоки управления: в две колонки, если хватает ширины, иначе — в одну (без горизонтальной прокрутки)."""

    def __init__(self, receiver, home, view, stats):
        super().__init__()
        self.parts = (receiver, home, view, stats)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.cols = 0
        self._arrange(2)

    def _arrange(self, cols):
        if cols == self.cols:
            return
        self.cols = cols
        for w in self.parts:
            self.grid.removeWidget(w)
        rec, home, view, stats = self.parts
        if cols == 2:
            self.grid.addWidget(rec, 0, 0, 2, 1)
            self.grid.addWidget(home, 0, 1)
            self.grid.addWidget(view, 1, 1)
            self.grid.addWidget(stats, 2, 0, 1, 2)
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 1)
        else:
            for i, w in enumerate(self.parts):
                self.grid.addWidget(w, i, 0, 1, 2)
            self.grid.setColumnStretch(1, 0)

    def need2(self):
        rec, home, view, _st = self.parts
        sp = self.grid.horizontalSpacing() if self.grid.horizontalSpacing() >= 0 else 6
        return rec.minimumSizeHint().width() + max(home.minimumSizeHint().width(),
                                                   view.minimumSizeHint().width()) + sp + 2


class _Scroll(QScrollArea):
    """Прокрутка блоков: число колонок выбирается по ширине видимой области (горизонтальной прокрутки нет)."""

    def resizeEvent(self, e):
        super().resizeEvent(e)
        b = self.widget()
        if b is not None:
            b._arrange(2 if self.viewport().width() >= b.need2() else 1)


def _chains(pts, max_km=80.0):
    """Разрезать трек там, где соседние точки дальше max_km (скачки подмены GNSS): линия через полмира не нужна."""
    out, cur = [], []
    for q in pts:
        if cur and (adsb.distance_km(cur[-1][0], cur[-1][1], q[0], q[1]) > max_km
                    or (len(q) > 3 and len(cur[-1]) > 3 and q[3] - cur[-1][3] > 1800)):
            out.append(cur)
            cur = []
        cur.append(q)
    if cur:
        out.append(cur)
    return out


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


class _Bridge(QObject):
    """События источника приходят из его потока — переносим в поток интерфейса."""
    event = Signal(str, object)


class AdsbWindow(ModeWindow):
    def __init__(self, opts=None):
        super().__init__(opts, "SDR4NOOB — ADS-B: самолёты на карте (1090 МГц)")
        self.s = settings.load("adsb", DEFAULTS)
        if self.opts.get("source"):
            self.s["source"] = self.opts["source"]
        self.src = None
        self.proc = None
        self.sel = None
        self.gains = list(R828D_GAINS)
        self.log_file = None
        self.log_writer = None
        self.log_path = None
        self._msgs_prev = 0
        self._t_prev = time.time()
        self.rate_hist = []
        self._rows = {}
        # история бортов: прошлые сеансы с диска + текущий (переживает Стоп/Старт и перезапуск)
        self.history = adsb.load_history(adsb.HISTORY_PATH, int(self.s["hist_days"]))
        self._hist_saving = False
        self.info = AircraftInfo(online=bool(self.s["online_info"]) and not self.opts.get("offline"))
        self.info.start()
        self._flags = {}
        self.log_paused = False
        self.log_pending = []
        self.bridge = _Bridge()
        self.bridge.event.connect(self._on_event)
        self._build()
        self._apply_home(save=False)
        self.map.set_center(self.s["home_lat"], self.s["home_lon"], self.s["zoom"])
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(500)
        self.hist_timer = QTimer(self)
        self.hist_timer.timeout.connect(lambda: self._save_history(background=True))
        self.hist_timer.start(120000)
        if self.opts.get("source"):
            QTimer.singleShot(200, self.start)

    # ================= интерфейс =================
    def _build(self):
        self.map = MapView()
        self.map.clicked.connect(self._map_clicked)
        self.map.itemClicked.connect(self._select)

        blocks = _Blocks(self._grp_receiver(), self._grp_home(), self._grp_view(), self._grp_stats())
        sc = self.ctrl_scroll = _Scroll()
        sc.setWidget(blocks)
        sc.setWidgetResizable(True)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setMinimumHeight(120)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(8, Qt.AscendingOrder)
        smooth_scroll(self.table)
        self.table.setAlternatingRowColors(False)
        hh = self.table.horizontalHeader()
        # не ResizeToContents: он пересчитывает ширину по всем строкам на каждую вставку ячейки —
        # при десятках бортов и обновлении 2 раза/с это занимало целое ядро
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        self._table_rows = -1
        for c, tip in enumerate(COL_TIPS):
            self.table.horizontalHeaderItem(c).setToolTip(tip)
        self.table.itemSelectionChanged.connect(self._table_selected)
        self.table.cellDoubleClicked.connect(self._table_dbl)

        self.details = QLabel("Выберите борт в таблице или на карте.")
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.RichText)
        self.details.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        dg = QGroupBox("Выбранный борт")
        dl = QVBoxLayout(dg)
        dsc = QScrollArea()
        dsc.setWidget(self.details)
        dsc.setWidgetResizable(True)
        dsc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        dsc.setFrameShape(QFrame.NoFrame)
        dl.addWidget(dsc)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.table, "Борта сейчас")
        self.tabs.addTab(self._build_log(), "Журнал сообщений")
        self.tabs.addTab(self._build_history(), "История сеанса")
        self.tabs.currentChanged.connect(lambda _i: self._refresh_history())

        vs = self.vsplit = QSplitter(Qt.Vertical)
        vs.addWidget(sc)
        vs.addWidget(self.tabs)
        vs.addWidget(dg)
        vs.setChildrenCollapsible(False)
        vs.splitterMoved.connect(lambda *_: setattr(self, "_user_split", True))
        vs.setStretchFactor(0, 0)
        vs.setStretchFactor(1, 3)
        vs.setStretchFactor(2, 1)
        self.tabs.setMinimumHeight(160)
        dg.setMinimumHeight(90)

        split = self.hsplit = QSplitter(Qt.Horizontal)
        split.addWidget(self.map)
        split.addWidget(vs)
        split.setChildrenCollapsible(False)
        vs.setMinimumWidth(360)
        self.map.setMinimumWidth(320)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)
        self._build_overlay()
        self.statusBar().showMessage("Ctrl+щелчок по карте — поставить положение приёмника; колёсико — масштаб.")

    def _build_log(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 4, 2, 2)
        row = QHBoxLayout()
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText("Поиск: ICAO или позывной")
        self.log_search.setClearButtonEnabled(True)
        self.log_search.textChanged.connect(lambda _t: self._log_filter_changed())
        self.cb_log_sel = QCheckBox("Только выбранный борт")
        self.cb_log_sel.toggled.connect(lambda _v: self._log_filter_changed())
        self.btn_pause = QPushButton("⏸ Пауза")
        self.btn_pause.setCheckable(True)
        self.btn_pause.toggled.connect(self._log_pause)
        b_clear = QPushButton("Очистить")
        b_clear.clicked.connect(self._log_clear)
        b_save = QPushButton("Сохранить в файл…")
        b_save.clicked.connect(self._log_save)
        row.addWidget(self.log_search, 1)
        row.addWidget(self.cb_log_sel)
        row.addWidget(self.btn_pause)
        row.addWidget(b_clear)
        row.addWidget(b_save)
        lay.addLayout(row)
        self.log_model = _LogModel()
        self.log_proxy = _LogFilter()
        self.log_proxy.setSourceModel(self.log_model)
        v = self.log_view = QTableView()
        v.setModel(self.log_proxy)
        v.verticalHeader().setVisible(False)
        v.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        v.verticalHeader().setDefaultSectionSize(20)
        v.setSelectionBehavior(QAbstractItemView.SelectRows)
        v.setEditTriggers(QAbstractItemView.NoEditTriggers)
        v.setWordWrap(False)
        smooth_scroll(v)
        hh = v.horizontalHeader()
        for c, wd in enumerate((92, 64, 78, 86)):
            hh.resizeSection(c, wd)
        hh.setStretchLastSection(True)
        v.clicked.connect(lambda ix: self._select(self.log_proxy.index(ix.row(), 1).data()))
        lay.addWidget(v, 1)
        self.log_info = QLabel("")
        self.log_info.setObjectName("dim")
        lay.addWidget(self.log_info)
        return w

    def _build_history(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 4, 2, 2)
        row = QHBoxLayout()
        self.cb_hist_map = QCheckBox("Показать на карте рейсы из истории")
        self.cb_hist_map.setChecked(bool(self.s["hist_map"]))
        self.cb_hist_map.setToolTip("Треки всех бортов сеанса с координатами, в том числе давно пропавших. "
                                    "Подозрительные координаты — пунктиром.")
        self.cb_hist_map.toggled.connect(self._hist_map_toggled)
        b_save = QPushButton("Сохранить историю…")
        b_save.setToolTip("CSV (точки треков) или KML (откроется в Google Earth)")
        b_save.clicked.connect(self._hist_save)
        row.addWidget(self.cb_hist_map, 1)
        row.addWidget(b_save)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Хранить историю:"))
        self.hist_days = QComboBox()
        for v, name in HIST_DAYS:
            self.hist_days.addItem(name, v)
        self.hist_days.setCurrentIndex(max(0, self.hist_days.findData(int(self.s["hist_days"]))))
        self.hist_days.currentIndexChanged.connect(self._hist_days_changed)
        row2.addWidget(self.hist_days)
        b_clear = QPushButton("Очистить историю")
        b_clear.clicked.connect(self._hist_clear)
        row2.addWidget(b_clear)
        self.hist_info = QLabel("")
        self.hist_info.setObjectName("dim")
        row2.addWidget(self.hist_info, 1)
        lay.addLayout(row2)
        lay.addWidget(self._build_hist_table(), 1)
        return w

    def _hist_days_changed(self):
        days = int(self.hist_days.currentData())
        self.s["hist_days"] = days
        self._save()
        if days:
            cut = time.time() - days * 86400
            lock = self.proc.lock if self.proc else threading.Lock()
            with lock:
                for k in [k for k, h in self.history.items() if h["last"] < cut]:
                    del self.history[k]
        self._refresh_history(force=True)

    def _hist_clear(self):
        from PySide6.QtWidgets import QMessageBox
        if QMessageBox.question(self, "ADS-B", "Удалить историю бортов всех сеансов (и файл на диске)?") \
                != QMessageBox.Yes:
            return
        lock = self.proc.lock if self.proc else threading.Lock()
        with lock:
            self.history.clear()
        try:
            os.remove(adsb.HISTORY_PATH)
        except OSError:
            pass
        self.map.set_tracks("history", [])
        self.map.set_items("history", [])
        self._refresh_history(force=True)

    def _save_history(self, background=False):
        """История — на диск (%LOCALAPPDATA%/SDR4NOOB/adsb_history.json). Демо-эфир и автотесты не сохраняем."""
        if self.opts.get("shot") or self.opts.get("no_save") or self.s.get("source") == "test":
            return
        if self._hist_saving:
            return
        snap = self._hist_snapshot()
        if not snap:
            return

        def work():
            try:
                adsb.save_history(snap)
            except OSError:
                pass
            finally:
                self._hist_saving = False
        self._hist_saving = True
        if background:
            threading.Thread(target=work, daemon=True).start()
        else:
            work()

    def _build_hist_table(self):
        t = self.hist = QTableWidget(0, len(HIST_COLS))
        t.setHorizontalHeaderLabels(HIST_COLS)
        t.verticalHeader().setVisible(False)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.SingleSelection)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSortingEnabled(True)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        t.horizontalHeader().setStretchLastSection(True)
        smooth_scroll(t)
        t.cellClicked.connect(self._hist_clicked)
        return t

    def _build_overlay(self):
        """Полупрозрачная панель поверх карты: борта без координат."""
        o = self.ovl = QFrame(self.map)
        o.setObjectName("ovl")
        lay = QVBoxLayout(o)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(3)
        self.ovl_title = QPushButton("Без координат (0)")
        self.ovl_title.setObjectName("ovlTitle")
        self.ovl_title.setFlat(True)
        self.ovl_title.setToolTip("Свернуть/развернуть список")
        self.ovl_title.clicked.connect(lambda: self.ovl_list.setVisible(not self.ovl_list.isVisible())
                                       or self._place_overlay())
        self.ovl_list = QListWidget()
        self.ovl_list.setObjectName("ovlList")
        self.ovl_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.ovl_list.itemClicked.connect(lambda it: it.data(Qt.UserRole) and self._select(it.data(Qt.UserRole)))
        self.ovl_hint = QLabel(NOPOS_HINT)
        self.ovl_hint.setObjectName("ovlHint")
        self.ovl_hint.setWordWrap(True)
        lay.addWidget(self.ovl_title, 0, Qt.AlignLeft)
        lay.addWidget(self.ovl_list)
        lay.addWidget(self.ovl_hint)
        o.setFixedWidth(340)
        o.move(10, 10)
        o.hide()

    def _place_overlay(self):
        n = self.ovl_list.count()
        rh = self.ovl_list.sizeHintForRow(0) if n else 18
        self.ovl_list.setFixedHeight(min(n, 10) * rh + 6)
        self.ovl.adjustSize()

    def showEvent(self, e):
        super().showEvent(e)
        if not getattr(self, "_laid_out", False):
            self._laid_out = True
            QTimer.singleShot(0, self._initial_sizes)
            QTimer.singleShot(150, self._initial_sizes)  # после выбора числа колонок блоков

    def _initial_sizes(self):
        """Стартовые пропорции под размер окна: карте ~половина, блокам управления — сколько им нужно."""
        w, h = self.hsplit.width(), self.vsplit.height()
        right = max(360, min(int(w * 0.5), 760))
        self.hsplit.setSizes([w - right, right])
        b = self.ctrl_scroll.widget()
        vw = self.ctrl_scroll.viewport().width()
        need = (b.heightForWidth(vw) if b.hasHeightForWidth() else b.sizeHint().height()) + 56  # запас: статистика потом вырастет на 2–3 строки
        top = min(need, int(h * 0.55))
        det = max(110, int(h * 0.2))
        self.vsplit.setSizes([top, max(160, h - top - det), det])

    def _grp_receiver(self):
        g = QGroupBox("Приёмник 1090 МГц")
        lay = QVBoxLayout(g)
        row = QHBoxLayout()
        self.src_combo = QComboBox()
        self._fill_sources()
        for_combo = self.src_combo
        for_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        for_combo.setMinimumContentsLength(10)
        self.btn_start = QPushButton("▶ Старт")
        self.btn_start.setMinimumWidth(84)
        self.btn_start.setCheckable(True)
        self.btn_start.clicked.connect(lambda on: self.start() if on else self.stop())
        row.addWidget(self.src_combo, 1)
        row.addWidget(self.btn_start)
        lay.addLayout(row)
        f = QFormLayout()
        self.gain_combo = QComboBox()
        self._fill_gains()
        self.gain_combo.currentIndexChanged.connect(self._gain_changed)
        f.addRow("Усиление:", self.gain_combo)
        self.ppm = QSpinBox()
        self.ppm.setRange(-200, 200)
        self.ppm.setSuffix(" ppm")
        self.ppm.setValue(int(self.s["ppm"]))
        self.ppm.valueChanged.connect(self._ppm_changed)
        f.addRow("Коррекция:", self.ppm)
        self.rate = QComboBox()
        self.rate.addItem("2,0 МГц (рекомендуется)", 2000000)
        self.rate.addItem("2,4 МГц (с передискретизацией)", 2400000)
        self.rate.setCurrentIndex(max(0, self.rate.findData(int(self.s["rate"]))))
        self.rate.currentIndexChanged.connect(self._rate_changed)
        f.addRow("Дискретизация:", self.rate)
        lay.addLayout(f)
        self.cb_rtl_agc = QCheckBox("АРУ RTL2832")
        self.cb_rtl_agc.setChecked(bool(self.s["rtl_agc"]))
        self.cb_rtl_agc.toggled.connect(self._rtl_agc_changed)
        self.cb_bias = QCheckBox("Bias-T (питание МШУ 4,5 В)")
        self.cb_bias.setChecked(bool(self.s["bias"]))
        self.cb_bias.setToolTip("Только для активной антенны или МШУ на 1090 МГц!")
        self.cb_bias.toggled.connect(self._bias_changed)
        lay.addWidget(self.cb_rtl_agc)
        lay.addWidget(self.cb_bias)
        self.dev_label = QLabel("Драйвер: встроенный librtlsdr")
        self.dev_label.setObjectName("dim")
        self.dev_label.setWordWrap(True)
        lay.addWidget(self.dev_label)
        lay.addStretch(1)
        return g

    def _grp_home(self):
        g = QGroupBox("Положение приёмника")
        f = QFormLayout(g)
        self.lat = QDoubleSpinBox()
        self.lat.setRange(-90, 90)
        self.lat.setDecimals(5)
        self.lat.setValue(float(self.s["home_lat"]))
        self.lon = QDoubleSpinBox()
        self.lon.setRange(-180, 180)
        self.lon.setDecimals(5)
        self.lon.setValue(float(self.s["home_lon"]))
        for w in (self.lat, self.lon):
            w.editingFinished.connect(self._apply_home)
        row = QHBoxLayout()
        row.addWidget(self.lat)
        row.addWidget(self.lon)
        f.addRow("Шир./долг.:", row)
        self.rings = QLineEdit(str(self.s["rings"]))
        self.rings.setToolTip("Радиусы колец дальности через запятую, км")
        self.rings.editingFinished.connect(self._apply_home)
        f.addRow("Кольца, км:", self.rings)
        btn = QPushButton("⌂ К приёмнику")
        btn.clicked.connect(lambda: self.map.set_center(self.lat.value(), self.lon.value()))
        btn_fit = QPushButton("✈ Все борта")
        btn_fit.setToolTip("Подобрать масштаб, чтобы на карте были видны все борта с координатами")
        btn_fit.clicked.connect(lambda: self._fit_planes(force=True))
        hint = QLabel("Ctrl+щелчок по карте — поставить дом")
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        r2 = QHBoxLayout()
        r2.addWidget(btn)
        r2.addWidget(btn_fit)
        r2.addWidget(hint, 1)
        f.addRow(r2)
        return g

    def _grp_view(self):
        g = QGroupBox("Вид и журнал")
        lay = QGridLayout(g)
        self.units = QComboBox()
        self.units.addItem("Метры, м/с", "m")
        self.units.addItem("Футы, фут/мин", "ft")
        self.units.setCurrentIndex(max(0, self.units.findData(self.s["units"])))
        self.units.currentIndexChanged.connect(self._units_changed)
        self.cb_trails = QCheckBox("Следы")
        self.cb_trails.setChecked(bool(self.s["trails"]))
        self.cb_labels = QCheckBox("Подписи")
        self.cb_labels.setChecked(bool(self.s["labels"]))
        self.cb_log = QCheckBox("Журнал CSV")
        self.cb_log.setChecked(bool(self.s["log_csv"]))
        self.cb_log.toggled.connect(self._log_toggled)
        self.cb_log.setToolTip(f"Каждое сообщение — строка в файле в папке {LOG_DIR}")
        for w in (self.cb_trails, self.cb_labels):
            w.toggled.connect(lambda _v: (self._save(), self._refresh()))
        self.keep = QComboBox()
        for v, name in KEEP_CHOICES:
            self.keep.addItem(name, v)
        self.keep.setCurrentIndex(max(0, self.keep.findData(int(self.s["keep_min"]))))
        self.keep.setToolTip("Сколько держать борт в таблице после последнего сообщения. "
                             "Не слышный дольше минуты показывается серым.")
        self.keep.currentIndexChanged.connect(self._keep_changed)
        lay.addWidget(QLabel("Единицы:"), 0, 0)
        lay.addWidget(self.units, 0, 1, 1, 2)
        lay.addWidget(QLabel("Держать в списке:"), 1, 0)
        lay.addWidget(self.keep, 1, 1, 1, 2)
        lay.addWidget(self.cb_trails, 2, 0)
        lay.addWidget(self.cb_labels, 2, 1)
        lay.addWidget(self.cb_log, 2, 2)
        self.cb_suspect = QCheckBox("Подозрительные координаты")
        self.cb_suspect.setChecked(bool(self.s["show_suspect"]))
        self.cb_suspect.setToolTip("Позиции дальше 700 км от приёмника или со скачками отбрасываются "
                                   "(обычно это подмена GNSS). С галкой такие борта рисуются на последней "
                                   "сырой позиции полупрозрачными, с «?».")
        self.cb_suspect.toggled.connect(lambda _v: (self._save(), self._refresh()))
        lay.addWidget(self.cb_suspect, 3, 0, 1, 3)
        btn = QPushButton("Папка журналов")
        btn.clicked.connect(self._open_logs)
        lay.addWidget(btn, 4, 0, 1, 3)
        return g

    def _grp_stats(self):
        g = QGroupBox("Приём и статистика")
        lay = QVBoxLayout(g)
        self.stats = QLabel("—")
        self.stats.setTextFormat(Qt.RichText)
        self.stats.setWordWrap(True)
        self.stats.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.stats.setMinimumWidth(180)
        self.meter = LevelMeter(0, 200, "сообщ./с")
        self.meter.setMinimumWidth(110)
        self.meter.setMaximumWidth(170)
        top = QHBoxLayout()
        top.addWidget(self.stats, 1)
        top.addWidget(self.meter)
        lay.addLayout(top)
        self.rx_info = QLabel("Приём: остановлен")
        self.rx_info.setWordWrap(True)
        self.rx_info.setTextFormat(Qt.RichText)
        self.rx_info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self.rx_info)
        row = QHBoxLayout()
        row.addWidget(QLabel("Уровень кадров:"))
        self.lvl = LevelMeter(-45, 0, "dBFS")
        self.lvl.setMinimumWidth(120)
        self.lvl.setToolTip("90-й процентиль уровня принятых кадров, dB от полной шкалы АЦП. "
                            "Норма: от −30 до −3 dBFS.")
        row.addWidget(self.lvl, 1)
        self.lvl_state = QLabel("—")
        self.lvl_state.setMinimumWidth(90)
        self.lvl_state.setAlignment(Qt.AlignCenter)
        row.addWidget(self.lvl_state)
        lay.addLayout(row)
        self.lvl_hint = QLabel("")
        self.lvl_hint.setObjectName("dim")
        self.lvl_hint.setWordWrap(True)
        self.lvl_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self.lvl_hint)
        return g

    def _fill_sources(self):
        devices.fill_source_combo(self.src_combo, self.s["source"], "Демо-генератор (без антенны)")

    def _fill_gains(self):
        cur = self.s["gain"]
        self.gain_combo.blockSignals(True)
        self.gain_combo.clear()
        self.gain_combo.addItem("АРУ тюнера", None)
        for gv in sorted(self.gains, reverse=True):
            self.gain_combo.addItem(f"{gv / 10:.1f} dB" + ("  (максимум)" if gv == max(self.gains) else ""), gv)
        if cur is None:
            i = 0
        else:
            i = min(range(1, self.gain_combo.count()), key=lambda k: abs(self.gain_combo.itemData(k) - cur))
        self.gain_combo.setCurrentIndex(i)
        self.gain_combo.blockSignals(False)

    # ================= приём =================
    def start(self):
        if self.src:
            return
        src = self.src_combo.currentData()
        if src == "none":
            self._error("Нет устройства. Выберите демо-генератор или подключите донгл.")
            self.btn_start.setChecked(False)
            return
        fs = int(self.rate.currentData())
        home = (self.lat.value(), self.lon.value())
        # демо-борта выдуманные — онлайн-справочник для них не дёргаем (офлайн-таблицы работают)
        self.info.online = src != "test" and bool(self.s["online_info"]) and not self.opts.get("offline")
        self.proc = adsb.AdsbProcessor(fs, home, on_message=self._log_message, keep_s=self._keep_s(),
                                       history=self.history)
        self.proc.start()
        sink = self._sink
        if src == "test":
            self.src = adsb.DemoSource(sink, fs, adsb.ADSB_FREQ, home=home)
        else:
            self.src = devices.make_source(src, sink, fs, adsb.ADSB_FREQ)
        self.src.gain = self.gain_combo.currentData()
        self.src.start()
        if src != "test":
            self.src.command("ppm", self.ppm.value())
            self.src.command("rtl_agc", self.cb_rtl_agc.isChecked())
            if self.cb_bias.isChecked():
                self.src.command("bias", True)
        if self.cb_log.isChecked():
            self._log_open()
        self._msgs_prev = 0
        self._t_prev = time.time()
        self._fitted = False
        QTimer.singleShot(2500, lambda: getattr(self, "_user_split", False) or self._initial_sizes())
        self.btn_start.setChecked(True)
        self.btn_start.setText("■ Стоп")
        self.src_combo.setEnabled(False)
        self.s["source"] = src
        self._save()

    def stop(self):
        src, self.src = self.src, None
        if src:
            src.stop()
            src.join(2.5)
        if self.proc:
            self.proc.stop()
            self.proc.join(1.0)
        self._log_close()
        self.btn_start.setChecked(False)
        self.btn_start.setText("▶ Старт")
        self.src_combo.setEnabled(True)

    def _sink(self, kind, *args):
        # поток источника: блоки — сразу в очередь декодера, остальное — в интерфейс
        if kind == "stream":
            p = self.proc
            if p:
                p.put(args[1])
        elif kind in ("opened", "error"):
            self.bridge.event.emit(kind, args[0])

    def _on_event(self, kind, data):
        if kind == "opened":
            if data.get("gains"):
                self.gains = list(data["gains"])
                self._fill_gains()
            self.dev_label.setText(f"{data['label']} · тюнер {data['tuner']}\n1090,000 МГц")
            self.statusBar().showMessage(f"Открыт: {data['label']}", 5000)
        elif kind == "error":
            self._error(data)

    def _error(self, msg):
        self.statusBar().showMessage("Ошибка: " + str(msg), 15000)
        self.dev_label.setText("Ошибка: " + str(msg))
        if self.src and not self.src.is_alive():
            self.stop()
        elif self.src is not None:
            QTimer.singleShot(300, lambda: self.src is not None and not self.src.is_alive() and self.stop())

    def _cmd(self, name, value):
        if self.src and not isinstance(self.src, adsb.DemoSource):
            self.src.command(name, value)

    def _gain_changed(self):
        self.s["gain"] = self.gain_combo.currentData()
        if self.src:
            self.src.command("gain", self.s["gain"])
        self._save()

    def _ppm_changed(self, v):
        self.s["ppm"] = v
        self._cmd("ppm", v)
        self._save()

    def _rtl_agc_changed(self, v):
        self.s["rtl_agc"] = v
        self._cmd("rtl_agc", v)
        self._save()

    def _bias_changed(self, v):
        self.s["bias"] = v
        self._cmd("bias", v)
        self._save()

    def _keep_s(self):
        v = int(self.keep.currentData())
        return None if v == 0 else v * 60.0

    def _keep_changed(self):
        self.s["keep_min"] = int(self.keep.currentData())
        if self.proc:
            self.proc.set_keep(self._keep_s())
        self._save()

    def _rate_changed(self):
        fs = int(self.rate.currentData())
        self.s["rate"] = fs
        if self.src:
            self.src.command("rate", fs)
            self.proc.set_rate(fs)
        self._save()

    # ================= дом, карта, выбор =================
    def _parse_rings(self):
        out = []
        for part in self.rings.text().replace(";", ",").split(","):
            try:
                v = float(part.strip().replace(" ", ""))
            except ValueError:
                continue
            if 0 < v <= 2000:
                out.append(int(v) if v == int(v) else v)
        return tuple(out)

    def _apply_home(self, save=True):
        lat, lon = self.lat.value(), self.lon.value()
        self.map.set_home(lat, lon, self._parse_rings())
        if self.proc:
            self.proc.set_home(lat, lon)
        self.s["home_lat"], self.s["home_lon"] = lat, lon
        self.s["rings"] = ", ".join(str(r) for r in self._parse_rings())
        if save:
            self._save()

    def _map_clicked(self, lat, lon):
        if QApplication.keyboardModifiers() & Qt.ControlModifier:
            self.lat.setValue(lat)
            self.lon.setValue(lon)
            self._apply_home()
            self.statusBar().showMessage(f"Положение приёмника: {lat:.5f}, {lon:.5f}", 5000)
        else:
            key = self._hit_hist_track(lat, lon)
            self._select(key)

    def _hit_hist_track(self, lat, lon):
        """Щелчок по линии трека истории → ICAO борта (или None)."""
        if not self.cb_hist_map.isChecked():
            return None
        m = self.map
        c = m.to_screen(lat, lon)
        best, bd = None, 7.0
        for key, pts in getattr(self, "_hist_lines", []):
            prev = None
            for la, lo in pts:
                q = m.to_screen(la, lo)
                if prev is not None:
                    d = _seg_dist(c.x(), c.y(), prev.x(), prev.y(), q.x(), q.y())
                    if d < bd:
                        best, bd = key, d
                prev = q
        return best

    def _select(self, key):
        self.sel = key
        self.map.set_selected(key)
        if self.cb_log_sel.isChecked():
            self._log_filter_changed()
        self.table.blockSignals(True)
        self.table.clearSelection()
        if key:
            for r in range(self.table.rowCount()):
                it = self.table.item(r, 0)
                if it and it.text() == key:
                    self.table.selectRow(r)
                    self.table.scrollToItem(it)
                    break
        self.table.blockSignals(False)
        self._select_hist_row(key)
        self._update_details()
        if self.cb_hist_map.isChecked():
            self._update_hist_map()

    def _select_hist_row(self, key):
        t = self.hist
        t.blockSignals(True)
        restore_selection(t, key)
        t.blockSignals(False)

    def _hist_clicked(self, r, _c):
        it = self.hist.item(r, 0)
        if not it:
            return
        key = it.text()
        self._select(key)
        h = self.history.get(int(key, 16))
        pts = (h["track"] or h["raw"]) if h else []
        if pts:
            if not self.cb_hist_map.isChecked():
                self.cb_hist_map.setChecked(True)
            self._fit_bounds([p[0] for p in pts], [p[1] for p in pts], margin=160, max_zoom=9)

    def _table_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if rows:
            it = self.table.item(rows[0].row(), 0)
            if it:
                self.sel = it.text()
                self.map.set_selected(self.sel)
                self._update_details()
                if self.cb_log_sel.isChecked():
                    self._log_filter_changed()

    def _table_dbl(self, row, _col):
        it = self.table.item(row, 0)
        if not it:
            return
        for a in self._planes:
            if a["hex"] == it.text() and a["lat"] is not None:
                self.map.set_center(a["lat"], a["lon"])

    # ================= единицы =================
    def _metric(self):
        return self.units.currentData() == "m"

    def _alt_text(self, ft, short=False):
        if ft is None:
            return "—"
        if self._metric():
            return f"{ft * adsb.FT:,.0f}".replace(",", " ") + ("" if short else " м")
        return f"{ft:,.0f}".replace(",", " ") + ("" if short else " фут")

    def _vr_text(self, fpm):
        if fpm is None:
            return "—"
        if self._metric():
            return f"{fpm * adsb.FT / 60:+.1f}"
        return f"{fpm:+.0f}"

    def _units_changed(self):
        self.s["units"] = self.units.currentData()
        self._save()
        self._refresh()

    # ================= обновление 2 раза в секунду =================
    _planes = []

    def _refresh(self):
        now = time.time()
        planes = self.proc.snapshot() if self.proc else []
        self._planes = planes
        metric = self._metric()
        self.table.horizontalHeaderItem(3).setText("Высота, м" if metric else "Высота, фут")
        self.table.horizontalHeaderItem(6).setText("Вертик., м/с" if metric else "Вертик., фут/мин")
        self._fill_table(planes, now)
        items = []
        show_sus = self.cb_suspect.isChecked()
        for a in planes:
            if use_raw(a, show_sus):
                items.append(self._suspect_item(a, now))
                continue
            if a["lat"] is None:
                continue
            stale = now - a["last"] > adsb.STALE_S
            old_pos = now - a["pos_t"] > adsb.STALE_S
            c = QColor(alt_color(a["alt"], a["on_ground"]))
            if stale or old_pos:
                c.setAlpha(110)  # давно не слышен или координаты устарели — полупрозрачный, на последней точке
            col = c.name(QColor.HexArgb)
            label = None
            if self.cb_labels.isChecked() or a["hex"] == self.sel:
                label = (a["callsign"] or a["hex"]) + "\n" + ("на земле" if a["on_ground"]
                                                              else self._alt_text(a["alt"]))
                if old_pos:
                    label += f"  ⌛ {(now - a['pos_t']) / 60:.0f} мин"
            hdg = a["track"] if a["track"] is not None else a["heading"]
            trail = [(t[0], t[1]) for t in a["trail"]] if (self.cb_trails.isChecked() or a["hex"] == self.sel) else []
            if trail:
                trail.append((a["lat"], a["lon"]))
                trail = _chains(trail)[-1]  # след — только после последнего скачка
            items.append({"key": a["hex"], "lat": a["lat"], "lon": a["lon"], "kind": "plane", "heading": hdg or 0.0,
                          "label": label, "color": col, "trail": trail})
        self.map.set_items("planes", items)
        self._items = items
        if not getattr(self, "_fitted", True) and items:
            self._fit_planes()
        if self.sel and not any(a["hex"] == self.sel for a in planes) and int(self.sel, 16) not in self.history:
            self.sel = None
            self.map.set_selected(None)
        self._update_overlay(planes, now)
        self._update_details()
        self._update_stats(planes, now)
        self._flush_log()
        self._hist_tick = getattr(self, "_hist_tick", 0) + 1
        if self._hist_tick % 4 == 0:
            self._refresh_history()

    def _fit_planes(self, force=False):
        """Если ни один борт не попал в окно карты (например, она увеличена на районе дома), —
        подобрать центр и масштаб по дому и всем бортам. force — по кнопке, всегда."""
        items = [it for it in getattr(self, "_items", []) if abs(it["lat"]) < 85]
        if not items:
            return
        self._fitted = True
        m = self.map
        if not force:
            w, h = m.width(), m.height()
            for it in items:
                pt = m.to_screen(it["lat"], it["lon"])
                if 0 <= pt.x() <= w and 0 <= pt.y() <= h:
                    return  # что-то уже видно — карту пользователя не трогаем
        lats = [it["lat"] for it in items] + [self.lat.value()]
        lons = [it["lon"] for it in items] + [self.lon.value()]
        # по возможности без «подозрительных» за тысячу км
        near = [it for it in items if adsb.distance_km(self.lat.value(), self.lon.value(),
                                                        it["lat"], it["lon"]) < adsb.MAX_RANGE_KM]
        if near:
            lats = [it["lat"] for it in near] + [self.lat.value()]
            lons = [it["lon"] for it in near] + [self.lon.value()]
        self._fit_bounds(lats, lons)

    def _fit_bounds(self, lats, lons, margin=120, max_zoom=11):
        m = self.map
        clat, clon = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
        zoom = 3
        for z in range(max_zoom, 2, -1):
            x0, y0 = lonlat_to_px(min(lons), max(lats), z)
            x1, y1 = lonlat_to_px(max(lons), min(lats), z)
            if x1 - x0 <= m.width() - margin and y1 - y0 <= m.height() - margin:
                zoom = z
                break
        m.set_center(clat, clon, zoom)

    def _suspect_item(self, a, now):
        c = QColor(alt_color(a["alt"], a["on_ground"]))
        c.setAlpha(120)
        label = None
        if self.cb_labels.isChecked() or a["hex"] == self.sel:
            label = (f"{a['callsign'] or a['hex']} ?\n{self._alt_text(a['alt'])}\nподмена GNSS?"
                     + (f"  ⌛ {(now - a['raw_t']) / 60:.0f} мин" if now - a["raw_t"] > adsb.STALE_S else ""))
        hdg = a["track"] if a["track"] is not None else a["heading"]
        return {"key": a["hex"], "lat": a["raw_lat"], "lon": a["raw_lon"], "kind": "plane", "heading": hdg or 0.0,
                "label": label, "color": c.name(QColor.HexArgb), "trail": []}

    def _update_overlay(self, planes, now):
        show_sus = self.cb_suspect.isChecked()
        suspect = [a for a in planes if a["suspect"] and (a["lat"] is None or use_raw(a, show_sus))]
        sus_keys = {a["hex"] for a in suspect}
        nopos = [a for a in planes if a["lat"] is None and a["hex"] not in sus_keys]
        if not nopos and not suspect:
            self.ovl.hide()
            return
        order = lambda a: (now - a["last"] > adsb.STALE_S, -(a["alt"] or 0))  # noqa: E731
        nopos.sort(key=order)
        suspect.sort(key=order)
        title = f"Без координат ({len(nopos)})"
        if suspect:
            title += f" · подозрительные ({len(suspect)})"
        self.ovl_title.setText(title)
        lw = self.ovl_list
        lw.blockSignals(True)
        lw.clear()
        groups = []
        if suspect:
            groups.append((f"Подозрительные координаты ({len(suspect)})", suspect))
        if nopos:
            groups.append((f"Без координат ({len(nopos)})", nopos))
        for head, group in groups:
            if len(groups) > 1:
                h = QListWidgetItem(head)
                h.setFlags(Qt.NoItemFlags)
                f = h.font()
                f.setBold(True)
                h.setFont(f)
                lw.addItem(h)
            for a in group:
                lw.addItem(self._overlay_item(a, now))
                if a["hex"] == self.sel:
                    lw.item(lw.count() - 1).setSelected(True)
        lw.blockSignals(False)
        self._place_overlay()
        self.ovl.show()
        self.ovl.raise_()

    def _overlay_item(self, a, now):
        trk = a["track"] if a["track"] is not None else a["heading"]
        name = a["callsign"] or a["hex"]
        alt = "на земле" if a["on_ground"] else self._alt_text(a["alt"])
        crs = "—" if trk is None else f"{arrow(trk)}{trk:.0f}°"
        text = f"{name:<8}  {alt:>9}  {crs:>5}"
        if a["signal"] is not None:
            text += f"  {a['signal']:.0f} dB"
        if a["suspect"] and a["raw_lat"] is not None and self.lat.value() is not None:
            d = adsb.distance_km(self.lat.value(), self.lon.value(), a["raw_lat"], a["raw_lon"])
            text += f"  ?{d:.0f} км"
        age = now - a["last"]
        if age > adsb.STALE_S:
            text += f"  ⌛ {age / 60:.0f} мин"
        it = QListWidgetItem(text)
        it.setData(Qt.UserRole, a["hex"])
        if a["suspect"] and a["raw_note"]:
            it.setToolTip(a["raw_note"])
        it.setForeground(QColor(STALE_FG) if age > adsb.STALE_S else QColor(alt_color(a["alt"], a["on_ground"])))
        return it

    def _fill_table(self, planes, now):
        t = self.table
        vpos, hpos = t.verticalScrollBar().value(), t.horizontalScrollBar().value()
        t.setUpdatesEnabled(False)
        try:
            self._fill_table_rows(t, planes, now)
        finally:
            if len(planes) != self._table_rows:  # ширина колонок — только когда меняется состав
                self._table_rows = len(planes)
                t.resizeColumnsToContents()
                for c in range(t.columnCount() - 1):
                    t.setColumnWidth(c, min(t.columnWidth(c), 170))
            t.verticalScrollBar().setValue(vpos)
            t.horizontalScrollBar().setValue(hpos)
            t.setUpdatesEnabled(True)

    def _fill_table_rows(self, t, planes, now):
        t.setSortingEnabled(False)
        t.blockSignals(True)
        t.setRowCount(len(planes))
        metric = self._metric()
        for r, a in enumerate(planes):
            alt = None if a["alt"] is None else (a["alt"] * adsb.FT if metric else a["alt"])
            vr = None if a["vr"] is None else (a["vr"] * adsb.FT / 60 if metric else a["vr"])
            gs, sp_note = a["gs"], ""
            if gs is None and (a["tas"] is not None or a["ias"] is not None):
                gs, sp_note = (a["tas"], " TAS") if a["tas"] is not None else (a["ias"], " IAS")
            gs = None if gs is None else gs * adsb.KT_KMH
            trk = a["track"] if a["track"] is not None else a["heading"]
            age = now - a["last"]
            card = self.info.card(int(a["hex"], 16), a["callsign"])
            cells = [
                (a["hex"], a["hex"]),
                (a["callsign"] or "", a["callsign"] or None),
                ((card["cc"] or "").upper(), card["country"] or ""),
                ("на земле" if a["on_ground"] else self._alt_text(a["alt"], True),
                 -1.0 if a["on_ground"] else alt),
                ("—" if gs is None else f"{gs:.0f}{sp_note}", gs),
                ("—" if trk is None else f"{trk:.0f}°", trk),
                (self._vr_text(a["vr"]), vr),
                (a["squawk"] or "", a["squawk"]),
                self._dist_cell(a),
                (str(a["msgs"]), a["msgs"]),
                ("—" if a["signal"] is None else f"{a['signal']:.1f}", a["signal"]),
                (ago_text(age), age),
            ]
            stale = age > adsb.STALE_S
            col = QColor(alt_color(a["alt"], a["on_ground"]))
            for c, (text, key) in enumerate(cells):
                it = _NumItem(text, key)
                if c in (3, 4, 5, 6, 8, 9, 10, 11):
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 2 and card["cc"]:
                    it.setIcon(self._flag_icon(card["cc"]))
                    it.setToolTip(card["country"] or "")
                if c == 1 and (card["airline"] or card["private"]):
                    it.setToolTip(card["airline"] or "частный / служебный борт")
                if stale:
                    it.setForeground(QColor(STALE_FG))
                    it.setToolTip("Давно не слышен — последние известные данные")
                elif c == 3:
                    it.setForeground(col)
                if c == 7 and a["squawk"] in ("7500", "7600", "7700"):
                    it.setForeground(QColor("#ff4040"))
                t.setItem(r, c, it)
        t.setSortingEnabled(True)
        restore_selection(t, self.sel)
        t.blockSignals(False)

    def _flag_icon(self, cc):
        ic = self._flags.get(cc)
        if ic is not None:
            return ic
        path = self.info.flag_path(cc)       # нет файла — фоновая загрузка, пока рисуем код страны
        if path:
            ic = QIcon(path)
            self._flags[cc] = ic
            return ic
        pm = QPixmap(24, 18)
        pm.fill(QColor("#3a4150"))
        pnt = QPainter(pm)
        f = QFont(self.font())
        f.setPointSizeF(6.5)
        f.setBold(True)
        pnt.setFont(f)
        pnt.setPen(QColor("#e0e6ef"))
        pnt.drawText(pm.rect(), Qt.AlignCenter, cc.upper())
        pnt.end()
        return QIcon(pm)

    def _card_html(self, icao_hex, callsign):
        c = self.info.card(int(icao_hex, 16), callsign)
        parts = []
        fp = self.info.flag_path(c["cc"]) if c["cc"] else None
        if c["country"]:
            parts.append((f"<img src='{fp}' width=24 height=18> " if fp else "") + f"<b>{c['country']}</b>")
        if c["airline"]:
            parts.append(f"<b>{c['airline']}</b>" + (f" ({c['airline_code']})" if c["airline_code"] else ""))
        elif c["private"]:
            parts.append("<b>частный / служебный</b> (позывной без кода авиакомпании)")
        elif c["airline_code"]:
            parts.append(f"оператор {c['airline_code']}")
        if c["reg"]:
            parts.append(f"рег. <b>{c['reg']}</b>")
        if c["type"] or c["icao_type"]:
            parts.append(" ".join(x for x in (c["manufacturer"], c["type"]) if x)
                         + (f" ({c['icao_type']})" if c["icao_type"] else ""))
        if c["owner"] and c["owner"] != c["airline"]:
            parts.append(f"владелец: {c['owner']}")

        def place(p):
            if not p:
                return "?"
            name = p.get("city") or p.get("name") or ""
            code = p.get("iata") or p.get("icao") or ""
            return f"{name} ({code})" if name else code
        if c["origin"] or c["dest"]:
            parts.append(f"маршрут: <b>{place(c['origin'])} → {place(c['dest'])}</b>")
        tail = ""
        if c["pending"]:
            tail = " <span style='color:gray'>· запрос справочника…</span>"
        elif c["src"]:
            tail = f" <span style='color:gray'>· {c['src']}</span>"
        return "<p style='margin-bottom:4px'>" + " · ".join(parts) + tail + "</p>" if parts else ""

    def _dist_cell(self, a):
        if a["lat"] is not None and not (a["suspect"] and a["raw_t"] >= a["pos_t"]):
            return ("—" if a["dist"] is None else f"{a['dist']:.1f}", a["dist"])
        if a["suspect"] and a["raw_lat"] is not None:
            d = adsb.distance_km(self.lat.value(), self.lon.value(), a["raw_lat"], a["raw_lon"])
            return (f"? {d:.0f}", d)
        return ("—", None)

    def _update_details(self):
        a = next((p for p in self._planes if p["hex"] == self.sel), None) if self.sel else None
        if not a:
            h = self.history.get(int(self.sel, 16)) if self.sel else None
            if h:
                lo, hi = h["alt_min"], h["alt_max"]
                alts = "—" if lo is None else f"{self._alt_text(lo)} … {self._alt_text(hi)}"
                self.details.setText(
                    self._card_html(h["hex"], h["callsign"]) +
                    f"<b>{h['callsign'] or h['hex']}</b> (ICAO {h['hex']}) — из таблицы удалён, данные из истории"
                    + (" прошлых сеансов" if h.get("prev") else " сеанса")
                    + f".<br>Слышен {when(h['first'])} – {when(h['last'])}, сообщений {h['msgs']}, высота {alts}"
                    + (f", макс. дальность {h['max_dist']:.0f} км" if h["max_dist"] else "")
                    + f", точек трека {len(h['track'])}"
                    + (f", подозрительных {len(h['raw'])}" if h["raw"] else ""))
                return
            self.details.setText("Выберите борт в таблице или на карте.")
            return
        now = time.time()
        rows = [("ICAO", a["hex"]), ("Позывной", a["callsign"] or "—"), ("Категория", a["category"] or "—")]
        sq = a["squawk"]
        if sq:
            note = adsb.SQUAWK_NOTES.get(sq)
            rows.append(("Сквок", sq + (f" ({note})" if note else "")))
        if a["on_ground"]:
            rows.append(("Состояние", "на земле"))
        rows.append(("Высота баро", self._alt_text(a["alt"]) +
                     (f" / {a['alt']:,.0f} фут".replace(",", " ") if a["alt"] is not None and self._metric() else "")))
        if a["alt_gnss"] is not None:
            rows.append(("Высота GNSS", self._alt_text(a["alt_gnss"])))
        if a["gs"] is not None:
            rows.append(("Путевая скорость", f"{a['gs'] * adsb.KT_KMH:.0f} км/ч ({a['gs']:.0f} уз)"))
        if a["ias"] is not None:
            rows.append(("Приборная (IAS)", f"{a['ias'] * adsb.KT_KMH:.0f} км/ч ({a['ias']:.0f} уз)"))
        if a["tas"] is not None:
            rows.append(("Истинная (TAS)", f"{a['tas'] * adsb.KT_KMH:.0f} км/ч ({a['tas']:.0f} уз)"))
        if a["track"] is not None:
            rows.append(("Путевой угол", f"{a['track']:.1f}°"))
        if a["heading"] is not None:
            rows.append(("Курс (магн.)", f"{a['heading']:.1f}°"))
        if a["vr"] is not None:
            rows.append(("Вертикальная", f"{a['vr'] * adsb.FT / 60:+.1f} м/с ({a['vr']:+.0f} фут/мин)"))
        if a["sel_alt"] is not None:
            rows.append(("Заданная высота", self._alt_text(a["sel_alt"])))
        if a["sel_hdg"] is not None:
            rows.append(("Заданный курс", f"{a['sel_hdg']:.0f}°"))
        if a["qnh"] is not None:
            rows.append(("Давление QNH", f"{a['qnh']:.1f} гПа"))
        if a["lat"] is None:
            rows.append(("Координаты", "не передаёт (вероятно, подавление GNSS)" if a["no_pos"]
                         else ("только подозрительные" if a["suspect"] else "ещё нет")))
        elif a["pos_src"]:
            rows.append(("Позиция", POS_SRC.get(a["pos_src"], a["pos_src"])
                         + ("" if now - a["pos_t"] < adsb.STALE_S else f", {ago_text(now - a['pos_t'])}")))
        if a["rejected"]:
            rows.append(("Отброшено позиций", f"{a['rejected']}, последняя {ago_text(now - a['raw_t'])}: "
                         f"{a['raw_note']} ({a['raw_lat']:.3f}, {a['raw_lon']:.3f})"))
        if a["lat"] is not None:
            rows.append(("Координаты", f"{a['lat']:.5f}, {a['lon']:.5f}"))
            h = (self.lat.value(), self.lon.value())
            d = adsb.distance_km(h[0], h[1], a["lat"], a["lon"])
            b = adsb.bearing_deg(h[0], h[1], a["lat"], a["lon"])
            rows.append(("От приёмника", f"{d:.1f} км, азимут {b:.0f}°"))
        rows.append(("Сообщений", f"{a['msgs']} (DF {', '.join(str(x) for x in a['df_seen'])})"))
        if a["signal"] is not None:
            rows.append(("Сигнал", f"{a['signal']:.1f} dB полной шкалы"))
        age = now - a["last"]
        rows.append(("Слышен", f"{(a['last'] - a['first']) / 60:.1f} мин, последний приём {ago_text(age)}"
                     + (" — давно не слышен" if age > adsb.STALE_S else "")))
        half = (len(rows) + 1) // 2
        cells = []
        for i in range(half):
            left = rows[i]
            right = rows[i + half] if i + half < len(rows) else ("", "")
            cells.append(f"<tr><td style='color:gray'>{left[0]}</td><td><b>{left[1]}</b></td>"
                         f"<td width=14></td><td style='color:gray'>{right[0]}</td><td><b>{right[1]}</b></td></tr>")
        self.details.setText(self._card_html(a["hex"], a["callsign"])
                             + "<table cellspacing=2>" + "".join(cells) + "</table>")

    def _update_stats(self, planes, now):
        p = self.proc
        if not p:
            self.stats.setText("Остановлено. Нажмите «Старт».")
            self.meter.set_value(0, "0 сообщ./с")
            self.rx_info.setText("Приём: остановлен")
            self.lvl.set_value(-45, "—")
            self.lvl_state.setText("—")
            self.lvl_state.setStyleSheet("")
            self.lvl_hint.setText("")
            return
        dt = max(now - self._t_prev, 1e-3)
        rate = (p.msgs - self._msgs_prev) / dt
        self._msgs_prev, self._t_prev = p.msgs, now
        self.rate_hist = (self.rate_hist + [rate])[-6:]
        r = sum(self.rate_hist) / len(self.rate_hist)
        self.meter.set_value(r, f"{r:.0f} сообщ./с")
        with_pos = sum(1 for a in planes if a["lat"] is not None)
        no_pos = sum(1 for a in planes if a["lat"] is None)
        far = max((a["dist"] for a in planes if a["dist"] is not None), default=None)
        txt = (f"Сообщений: <b>{p.msgs}</b> ({r:.0f}/с) · бортов: <b>{len(planes)}</b>, с координатами "
               f"<b>{with_pos}</b>" + (f", без координат <b>{no_pos}</b>" if no_pos else "")
               + f"<br>Дальность: сейчас <b>{'—' if far is None else f'{far:.0f} км'}</b>, "
               f"максимум за сеанс <b>{p.tracker.max_range:.0f} км</b><br>"
               f"Декодер занят {p.busy * 100:.0f}% · исправлено 1 бит: {p.demod.corrected}"
               + (f" · потеряно блоков: {p.dropped}" if p.dropped else "")
               + (f"<br>Отброшено позиций: <b>{p.tracker.rejected}</b> (далеко от приёмника или скачки — "
                  "вероятно, подмена GNSS)" if p.tracker.rejected else ""))
        if self.log_path:
            txt += f"<br>Журнал: {os.path.basename(self.log_path)}"
        self.stats.setText(txt)
        self._update_level(p, r)

    def _update_level(self, p, rate):
        st = p.level_stats()
        g = self.gain_combo.currentData()
        gain = "АРУ тюнера" if g is None else f"{num(g / 10)} dB"
        demo = isinstance(self.src, adsb.DemoSource)
        fr = st["frames_p90"]
        self.rx_info.setText(
            f"Приём: <b>1090,000 МГц</b> · {num(p.demod.fs / 1e6)} MS/s · усиление <b>{gain}</b>"
            + (" + АРУ RTL2832" if self.cb_rtl_agc.isChecked() else "") + f" · ppm {self.ppm.value()}"
            + (" · демо" if demo else "")
            + f"<br>АЦП: пик {num(st['peak_db'])} dBFS, отсечки {num(st['clip'] * 100, 2)} % · шум "
            f"{num(st['noise_db'], 0)} dBFS · кадры {'—' if fr is None else num(fr, 0) + ' dBFS'}"
            + ("" if st["frames_max"] is None else f" (макс {num(st['frames_max'], 0)})")
            + f" · {rate:.0f} сообщ./с")
        state = st["state"]
        self.lvl.set_value(fr if fr is not None else -45, "нет кадров" if fr is None else f"{num(fr, 0)} dBFS")
        self.lvl_state.setText(state)
        self.lvl_state.setStyleSheet(f"color: {LEVEL_COLORS[state]}; font-weight: bold;")
        if state == "перегрузка":
            tips = []
            if self.cb_rtl_agc.isChecked():
                tips.append("выключите «АРУ RTL2832»")
            if g is None:
                tips.append("замените «АРУ тюнера» ручным усилением")
            tips.append("снизьте усиление (например до 30–40 dB)" if g is None or g > 300 else
                        "снизьте усиление ещё на 5–10 dB")
            self.lvl_hint.setText("Перегрузка: " + ", ".join(tips) + ". Сильные кадры искажаются и "
                                  "теряются, дальние борта пропадают.")
        elif state == "мало":
            self.lvl_hint.setText("Слабый сигнал: поднимите усиление до максимума, проверьте антенну "
                                  "(для 1090 МГц — вертикальный штырь ~6,9 см) и кабель."
                                  if fr is not None else "Кадров пока нет: подождите или проверьте антенну.")
        else:
            self.lvl_hint.setText("")

    # ================= лента сообщений и история =================
    def _flush_log(self):
        if self.proc:
            entries = self.proc.drain_log()
            if entries:
                metric = self._metric()
                rows = []
                for t, hx, cs, m, pos in entries[-LOG_MAX:]:
                    rows.append((time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t * 10) % 10}",
                                 hx, cs or "", adsb.msg_type(m), adsb.describe(m, pos, metric)))
                self.log_pending.extend(rows)
                if len(self.log_pending) > LOG_MAX:
                    del self.log_pending[:-LOG_MAX]
        if self.log_paused or not self.log_pending:
            self._log_status()
            return
        sb = self.log_view.verticalScrollBar()
        at_end = sb.value() >= sb.maximum() - 2
        self.log_model.append(self.log_pending)
        self.log_pending = []
        if at_end:
            self.log_view.scrollToBottom()
        self._log_status()

    def _log_status(self):
        txt = f"Строк в ленте: {self.log_model.rowCount()} (хранится до {LOG_MAX})"
        if self.log_proxy.rowCount() != self.log_model.rowCount():
            txt += f", показано по фильтру: {self.log_proxy.rowCount()}"
        if self.log_paused:
            txt += f" · пауза, ждут показа: {len(self.log_pending)}"
        self.log_info.setText(txt)

    def _log_filter_changed(self):
        only = self.sel if self.cb_log_sel.isChecked() else None
        if self.cb_log_sel.isChecked() and not self.sel:
            only = "—"
        self.log_proxy.set_filter(self.log_search.text(), only)
        self._log_status()

    def _log_pause(self, on):
        self.log_paused = on
        self.btn_pause.setText("▶ Продолжить" if on else "⏸ Пауза")
        if not on:
            self._flush_log()
        self._log_status()

    def _log_clear(self):
        self.log_model.clear()
        self.log_pending = []
        self._log_status()

    def _log_save(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        path, _flt = QFileDialog.getSaveFileName(
            self, "Сохранить журнал сообщений", os.path.join(LOG_DIR, time.strftime("adsb_lenta_%Y%m%d_%H%M%S.txt")),
            "Текст (*.txt);;CSV (*.csv)")
        if not path:
            return
        pm = self.log_proxy
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                if path.lower().endswith(".csv"):
                    w = csv.writer(f, delimiter=";")
                    w.writerow(_LogModel.HEAD)
                    for r in range(pm.rowCount()):
                        w.writerow([pm.index(r, c).data() for c in range(5)])
                else:
                    for r in range(pm.rowCount()):
                        v = [pm.index(r, c).data() for c in range(5)]
                        f.write(f"{v[0]}  {v[1]}  {v[2]:<8}  {v[3]:<10}  {v[4]}\r\n")
            self.statusBar().showMessage(f"Журнал сохранён: {path} ({pm.rowCount()} строк)", 8000)
        except OSError as e:
            self.statusBar().showMessage(f"Не удалось сохранить: {e}", 10000)

    def _hist_snapshot(self):
        if self.proc:
            return self.proc.history_snapshot()
        return [dict(h, track=list(h["track"]), raw=list(h["raw"])) for h in self.history.values()]

    def _refresh_history(self, force=False):
        need_table = self.tabs.currentIndex() == 2 or force
        if not need_table and not self.cb_hist_map.isChecked():
            return
        hist = self._hist_snapshot()
        if self.cb_hist_map.isChecked():
            self._update_hist_map(hist)
        if not need_table:
            return
        t = self.hist
        vpos, hpos = t.verticalScrollBar().value(), t.horizontalScrollBar().value()
        prev = sum(1 for h in hist if h.get("prev"))
        self.hist_info.setText(f"бортов: {len(hist)}" + (f", из прошлых сеансов: {prev}" if prev else ""))
        t.setUpdatesEnabled(False)
        t.setSortingEnabled(False)
        grew = t.rowCount() != len(hist)
        t.setRowCount(len(hist))
        metric = self._metric()
        hd = "м" if metric else "фут"
        t.horizontalHeaderItem(4).setText(f"Высота мин, {hd}")
        t.horizontalHeaderItem(5).setText(f"Высота макс, {hd}")
        now = time.time()
        for r, h in enumerate(hist):
            def alt(v):
                if v is None:
                    return ("—", None)
                x = v * adsb.FT if metric else v
                return (f"{x:,.0f}".replace(",", " "), x)
            coords = "да" if h["had_pos"] else ("не передаёт" if h["no_pos"] else "нет")
            if h.get("suspect"):
                coords = "да + подозрительные" if h["had_pos"] else "подозрительные"
            cells = [(h["hex"], h["hex"]), (h["callsign"] or "", h["callsign"] or None),
                     (when(h["first"]), h["first"]),
                     (when(h["last"]), h["last"]),
                     alt(h["alt_min"]), alt(h["alt_max"]), (str(h["msgs"]), h["msgs"]), (coords, coords),
                     ("—" if h["max_dist"] is None else f"{h['max_dist']:.1f}", h["max_dist"])]
            for c, (text, key) in enumerate(cells):
                it = _NumItem(text, key)
                if c in (4, 5, 6, 8):
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if now - h["last"] > adsb.STALE_S:
                    it.setForeground(QColor(STALE_FG))
                if c == 0:
                    cc = self.info.card(h["icao"], h["callsign"])["cc"]
                    if cc:
                        it.setIcon(self._flag_icon(cc))
                t.setItem(r, c, it)
        t.setSortingEnabled(True)
        if grew:
            t.resizeColumnsToContents()
        t.verticalScrollBar().setValue(vpos)
        t.horizontalScrollBar().setValue(hpos)
        t.setUpdatesEnabled(True)
        self._select_hist_row(self.sel)

    def _hist_map_toggled(self, on):
        self.s["hist_map"] = on
        self._save()
        if on:
            self._update_hist_map()
        else:
            self.map.set_tracks("history", [])
            self.map.set_items("history", [])
            self._hist_lines = []

    def _update_hist_map(self, hist=None):
        """Треки всех бортов сеанса: линия цветом по высоте (отрезками), подозрительные — пунктиром,
        на последней точке — метка с позывным и временем последнего приёма."""
        if not self.cb_hist_map.isChecked():
            return
        hist = self._hist_snapshot() if hist is None else hist
        tracks, items, lines = [], [], []
        live = {it["key"] for it in getattr(self, "_items", [])}
        for h in hist:
            sel = h["hex"] == self.sel
            width = 3.2 if sel else 1.6
            pts = h["track"]
            for ch in _chains(pts):
                if len(ch) < 2:
                    continue
                seg = [ch[0]]
                col = alt_color(ch[0][2])
                for q in ch[1:]:
                    c = alt_color(q[2])
                    seg.append(q)
                    if c != col:
                        tracks.append({"points": [(x[0], x[1]) for x in seg], "color": col, "width": width})
                        seg, col = [q], c
                if len(seg) >= 2:
                    tracks.append({"points": [(x[0], x[1]) for x in seg], "color": col, "width": width})
                lines.append((h["hex"], [(x[0], x[1]) for x in ch]))
            for ch in _chains(h["raw"]):
                if len(ch) >= 2:
                    tracks.append({"points": [(x[0], x[1]) for x in ch], "color": "#ff9f40",
                                   "width": width, "dash": True})
                    lines.append((h["hex"], [(x[0], x[1]) for x in ch]))
            last = pts[-1] if pts else (h["raw"][-1] if h["raw"] else None)
            if last is None or h["hex"] in live:
                continue  # борт и так на карте — метку истории не дублируем
            label = None
            if self.cb_labels.isChecked() or sel:
                label = (f"{h['callsign'] or h['hex']}{' ?' if not pts else ''}\n"
                         f"{when(h['last'])[:-3] if len(when(h['last'])) == 8 else when(h['last'])}")
            c = QColor(alt_color(last[2]) if pts else "#ff9f40")
            c.setAlpha(200)
            items.append({"key": h["hex"], "lat": last[0], "lon": last[1], "kind": "point",
                          "label": label, "color": c.name(QColor.HexArgb)})
        self._hist_lines = lines
        self.map.set_tracks("history", tracks)
        self.map.set_items("history", items)

    def _hist_save(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        path, _f = QFileDialog.getSaveFileName(
            self, "Сохранить историю сеанса", os.path.join(LOG_DIR, time.strftime("adsb_history_%Y%m%d_%H%M%S.kml")),
            "KML для Google Earth (*.kml);;CSV (*.csv)")
        if not path:
            return
        try:
            n = self._write_history(path, self._hist_snapshot())
            self.statusBar().showMessage(f"История сохранена: {path} ({n} бортов)", 8000)
        except OSError as e:
            self.statusBar().showMessage(f"Не удалось сохранить: {e}", 10000)

    @staticmethod
    def _write_history(path, hist):
        if path.lower().endswith(".csv"):
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["ICAO", "позывной", "время", "широта", "долгота", "высота_м", "подозрительная"])
                for h in hist:
                    for kind, pts in (("", h["track"]), ("да", h["raw"])):
                        for la, lo, alt, t in pts:
                            w.writerow([h["hex"], h["callsign"] or "",
                                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)), f"{la:.5f}",
                                        f"{lo:.5f}", "" if alt is None else round(alt * adsb.FT), kind])
            return len(hist)
        out = ['<?xml version="1.0" encoding="UTF-8"?>', '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
               "<name>SDR4NOOB ADS-B — история сеанса</name>",
               '<Style id="sus"><LineStyle><color>ff409fff</color><width>2</width></LineStyle></Style>']
        n = 0
        for h in hist:
            if not h["track"] and not h["raw"]:
                continue
            n += 1
            name = escape(h["callsign"] or h["hex"])
            desc = escape(f"ICAO {h['hex']}, сообщений {h['msgs']}, "
                          f"{time.strftime('%H:%M:%S', time.localtime(h['first']))}–"
                          f"{time.strftime('%H:%M:%S', time.localtime(h['last']))}")
            c = QColor(alt_color(h["alt_max"]))
            kcol = f"ff{c.blue():02x}{c.green():02x}{c.red():02x}"
            out.append(f"<Folder><name>{name}</name>")
            for pts, style, title in ((h["track"], f"<Style><LineStyle><color>{kcol}</color><width>2</width>"
                                       "</LineStyle></Style>", name),
                                      (h["raw"], "<styleUrl>#sus</styleUrl>", name + " (подозрительные)")):
                if len(pts) < 2:
                    continue
                coords = " ".join(f"{lo:.5f},{la:.5f},{0 if alt is None else alt * adsb.FT:.0f}"
                                  for la, lo, alt, _t in pts)
                out.append(f"<Placemark><name>{title}</name><description>{desc}</description>{style}"
                           "<LineString><altitudeMode>absolute</altitudeMode>"
                           f"<coordinates>{coords}</coordinates></LineString></Placemark>")
            out.append("</Folder>")
        out.append("</Document></kml>")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out))
        return n

    # ================= журнал CSV =================
    def _log_toggled(self, on):
        self.s["log_csv"] = on
        self._save()
        if on and self.src:
            self._log_open()
        elif not on:
            self._log_close()

    def _log_open(self):
        if self.log_file or self.opts.get("shot") or self.opts.get("no_save"):
            return  # автопроверки и снимки не пишут журнал в «Документы» пользователя
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            self.log_path = os.path.join(LOG_DIR, time.strftime("adsb_%Y%m%d_%H%M%S.csv"))
            f = open(self.log_path, "w", newline="", encoding="utf-8-sig")
            w = csv.writer(f, delimiter=";")
            w.writerow(CSV_HEAD)
            self.log_writer, self.log_file = w, f
        except OSError as e:
            self.log_path = None
            self.statusBar().showMessage(f"Не удалось открыть журнал: {e}", 10000)

    def _log_close(self):
        f, self.log_file, self.log_writer = self.log_file, None, None
        if f:
            try:
                f.close()
            except OSError:
                pass
        self.log_path = None

    def _log_message(self, m, a, now):
        # вызывается из потока декодера
        w = self.log_writer
        if w is None:
            return
        def r(v, k=1):  # noqa: E306
            return "" if v is None else (round(v, k) if isinstance(v, float) else v)
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}",
                    a.hex, m["df"], m.get("callsign", ""), r(m.get("alt", m.get("alt_gnss"))), r(m.get("gs")),
                    r(m.get("track", m.get("heading"))), r(m.get("vr")), m.get("squawk", ""),
                    r(a.lat, 5) if "cpr" in m else "", r(a.lon, 5) if "cpr" in m else "",
                    r(a.dist) if "cpr" in m else "", r(a.signal), m["raw"]])

    def _open_logs(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        try:
            osplat.open_path(LOG_DIR)
        except (OSError, AttributeError):
            self.statusBar().showMessage(LOG_DIR, 10000)

    # ================= прочее =================
    def _save(self):
        if self.opts.get("shot") or self.opts.get("no_save"):
            return
        self.s["trails"] = self.cb_trails.isChecked()
        self.s["labels"] = self.cb_labels.isChecked()
        self.s["show_suspect"] = self.cb_suspect.isChecked()
        self.s["zoom"] = self.map.zoom
        settings.save("adsb", self.s)

    def apply_theme(self, t):
        self.map.set_theme(t)
        self.meter.set_theme(t)
        bg = QColor(t["panel"])
        self.ovl.setStyleSheet(
            f"#ovl {{ background: rgba({bg.red()},{bg.green()},{bg.blue()},205); border: 1px solid {t['grid']};"
            f" border-radius: 6px; }}"
            f" #ovl QLabel, #ovl QPushButton, #ovl QListWidget {{ background: transparent; border: none; }}"
            f" #ovlTitle {{ color: {t['accent']}; font-weight: bold; padding: 0; text-align: left; }}"
            f" #ovlHint {{ color: {t['dim']}; font-size: 8.5pt; }}"
            f" #ovlList {{ font-family: Consolas, monospace; font-size: 9pt; }}"
            f" #ovlList::item:selected {{ background: {t['accent']}; color: {t['bg']}; }}")

    def shutdown(self):
        self.timer.stop()
        self.hist_timer.stop()
        self.stop()
        self._save()
        self._save_history()
        self.info.stop()

