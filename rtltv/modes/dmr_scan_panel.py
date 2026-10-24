"""Панель «Поиск цифрового радио (DMR)» для левой колонки режима «Радио».

Сканирует выбранный диапазон через план перестроек источника (engine.cmd("plan", ...)),
блоки обрабатывает в потоке обработки движка (engine.plan_handler), результаты передаёт
в интерфейс сигналом Qt. Двойной щелчок по строке — сигнал tuneRequested(частота, режим).
"""
import threading
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QProgressBar, QPushButton, QSizePolicy, QTableWidget,
                               QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from ..dmrscan import BANDS, KNOWN, DmrScanner, describe

TAG = "dmrscan"

HELP = (
    "<ol style='margin-left:-20px'>"
    "<li>Запустите приём кнопкой <b>▶</b> вверху окна — сканер работает только при включённом приёме.</li>"
    "<li>Выберите <b>диапазон</b>. В Москве чаще всего DMR слышен на <b>«70 см любительский»</b> (438–440 МГц, "
    "открытые ретрансляторы). «Свой диапазон…» — задайте границы «от/до».</li>"
    "<li><b>«На окне»</b> — сколько секунд слушать каждый кусок эфира шириной ~2 МГц: 1 с надёжнее, "
    "0,25 с быстрее.</li>"
    "<li>Нажмите <b>«Сканировать»</b>. Пока идёт поиск, звука нет: приёмник перебирает окна. "
    "Поиск идёт кругами — чем дольше, тем больше найдётся (ретрансляторы говорят не всё время).</li>"
    "<li>В таблице появятся каналы. «<b>DMR-ретранслятор, CC…</b>» — найден DMR (CC — цветовой код); "
    "«<b>голос</b>» — шёл разговор, есть номера абонентов; «<b>известный</b>» — частота из списка найденных ранее; "
    "«Когда» — сколько времени назад слышен.</li>"
    "<li><b>Двойной щелчок</b> по строке — поиск остановится, радио переключится в режим DMR на эту частоту. "
    "Когда кто-то заговорит, в панели <b>«DMR»</b> выше появятся номера «кто → кому» и пойдёт звук.</li></ol>"
    "«Только DMR» скрывает аналоговые и прочие сигналы, «Очистить» сбрасывает список.<br><br>"
    "<b>Важно:</b> шифрованные вызовы видны (номера, «шифр»), но звука у них нет. dPMR, NXDN, TETRA, P25 "
    "не декодируются — отмечаются как «другой/узкий цифровой»; аналоговые станции — «аналог ЧМ» "
    "(двойной щелчок откроет их в NFM). Служебные сети 136–174 и 400–470 МГц обычно зашифрованы."
)

DWELLS = [(0.25, "0,25 с"), (0.35, "0,35 с"), (0.6, "0,6 с"), (1.0, "1 с")]


def _mhz(f):
    return f"{f / 1e6:.4f}".rstrip("0").rstrip(".").replace(".", ",")


def _ago(t):
    if not t:
        return "—"
    d = time.time() - t
    if d < 60:
        return f"{int(d)} с"
    if d < 3600:
        return f"{int(d // 60)} мин"
    return time.strftime("%H:%M", time.localtime(t))


class DmrScanPanel(QWidget):
    tuneRequested = Signal(float, str)      # частота, Гц; режим "DMR" или "NFM"
    _update = Signal(object)

    def __init__(self, engine, parent=None, framed=True):
        super().__init__(parent)
        self.framed = framed   # False — без своей рамки (панель кладётся в сворачиваемую секцию окна)
        self.engine = engine
        self.scanner = None
        self._lock = threading.Lock()
        self._rows = []
        self._last_draw = 0.0
        self._build()
        self._update.connect(self._on_update)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._fill_known()

    # ------------------------------------------------------------------ интерфейс
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        g = QGroupBox("Поиск цифрового DMR") if self.framed else QWidget()
        gv = QVBoxLayout(g)
        if not self.framed:
            gv.setContentsMargins(0, 0, 0, 0)
        gv.setSpacing(4)

        self.help_btn = QToolButton()
        self.help_btn.setText("▾ Как пользоваться")
        self.help_btn.setCheckable(True)
        self.help_btn.setChecked(True)
        self.help_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.help_btn.setAutoRaise(True)
        self.help_btn.toggled.connect(self._toggle_help)
        gv.addWidget(self.help_btn)
        self.help = QLabel(HELP)
        self.help.setWordWrap(True)
        self.help.setTextFormat(Qt.RichText)
        self.help.setObjectName("dim")
        self.help.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        gv.addWidget(self.help)

        self.band = QComboBox()
        for name, lo, hi in BANDS:
            self.band.addItem(name, (lo, hi))
        self.band.addItem("Свой диапазон…", None)
        self.band.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.band.setMinimumContentsLength(12)
        self.band.currentIndexChanged.connect(self._band_changed)
        gv.addWidget(self.band)

        self.custom = QWidget()
        cr = QHBoxLayout(self.custom)
        cr.setContentsMargins(0, 0, 0, 0)
        self.f_lo = QDoubleSpinBox()
        self.f_hi = QDoubleSpinBox()
        for sp, val in ((self.f_lo, 438.0), (self.f_hi, 441.0)):
            sp.setRange(24.0, 1760.0)
            sp.setDecimals(3)
            sp.setSingleStep(0.1)
            sp.setValue(val)
            sp.setSuffix(" МГц")
            sp.setMinimumWidth(60)
            sp.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        cr.addWidget(QLabel("от"))
        cr.addWidget(self.f_lo, 1)
        cr.addWidget(QLabel("до"))
        cr.addWidget(self.f_hi, 1)
        self.custom.setVisible(False)
        gv.addWidget(self.custom)

        row = QHBoxLayout()
        row.addWidget(QLabel("На окне:"))
        self.dwell = QComboBox()
        self.dwell.setMinimumContentsLength(5)
        self.dwell.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        for d, name in DWELLS:
            self.dwell.addItem(name, d)
        self.dwell.setCurrentIndex(1)
        self.dwell.setToolTip("Сколько слушать каждое окно ~2 МГц. Дольше — надёжнее ловятся номера абонентов.")
        row.addWidget(self.dwell, 1)
        gv.addLayout(row)

        self.btn = QPushButton("▶ Сканировать")
        self.btn.setCheckable(True)
        self.btn.toggled.connect(self._toggle)
        gv.addWidget(self.btn)
        row = QHBoxLayout()
        self.only_dmr = QCheckBox("только DMR")
        self.only_dmr.toggled.connect(lambda _v: self._draw(force=True))
        row.addWidget(self.only_dmr, 1)
        clr = QPushButton("Очистить")
        clr.clicked.connect(self._clear)
        row.addWidget(clr)
        gv.addLayout(row)

        self.prog = QProgressBar()
        self.prog.setTextVisible(False)
        self.prog.setMaximumHeight(8)
        gv.addWidget(self.prog)
        self.info = QLabel("Сканер остановлен. Приём радио при сканировании прерывается.")
        self.info.setWordWrap(True)
        self.info.setObjectName("dim")
        self.info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        gv.addWidget(self.info)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["МГц", "Что слышно", "Когда"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setWordWrap(True)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.setMinimumHeight(220)
        self.table.setToolTip("Двойной щелчок — настроить радио на эту частоту")
        self.table.cellDoubleClicked.connect(self._pick)
        gv.addWidget(self.table, 1)
        v.addWidget(g)
        self.setMinimumWidth(300)

    def _toggle_help(self, on):
        self.help.setVisible(on)
        self.help_btn.setText(("▾ " if on else "▸ ") + "Как пользоваться")

    def _band_changed(self, _i):
        self.custom.setVisible(self.band.currentData() is None)

    def _range(self):
        d = self.band.currentData()
        if d is None:
            return self.f_lo.value() * 1e6, self.f_hi.value() * 1e6
        return d

    # ------------------------------------------------------------------ сканирование
    def _toggle(self, on):
        if on:
            self.start()
        else:
            self.stop()

    def start(self):
        if not self.engine.running():
            self.info.setText("Сначала включите приём (кнопка ▶ слева вверху), затем запустите сканирование.")
            self.btn.blockSignals(True)
            self.btn.setChecked(False)
            self.btn.blockSignals(False)
            return
        lo, hi = self._range()
        if hi - lo > 200e6:
            hi = lo + 200e6
        with self._lock:
            old = self.scanner
            self.scanner = DmrScanner(self.engine.fs, lo, hi, dwell=self.dwell.currentData())
            if old is not None:      # статистика прошлых проходов сохраняется
                for k, e in old.entries.items():
                    if e["hits"] or e["kind"] == "known":
                        self.scanner.entries[k] = e
            items = self.scanner.plan_items()
        self.prog.setRange(0, len(items))
        self.prog.setValue(0)
        self.engine.plan_handler = self._on_plan
        self.engine.cmd("plan", {"tag": TAG, "items": items, "repeat": True})
        self.btn.setText("■ Стоп")
        self.info.setText(f"Сканирую {_mhz(lo)}–{_mhz(hi)} МГц: {len(items)} окон по "
                          f"{self.scanner.fs * 0.8 / 1e6:.1f} МГц…")

    def stop(self):
        if self.engine.plan_handler == self._on_plan:
            self.engine.cmd("plan", None)
            self.engine.plan_handler = None
        self.btn.blockSignals(True)
        self.btn.setChecked(False)
        self.btn.blockSignals(False)
        self.btn.setText("▶ Сканировать")
        sc = self.scanner
        if sc is not None:
            p = sc.progress()
            self.info.setText(f"Остановлено. Проходов: {p['pass']}, найдено DMR-каналов: {p['dmr']}.")
        self.prog.setValue(0)

    def scanning(self):
        return self.engine.plan_handler == self._on_plan

    def _clear(self):
        with self._lock:
            if self.scanner is not None:
                fs, lo, hi = self.scanner.fs, self.scanner.lo, self.scanner.hi
                self.scanner = DmrScanner(fs, lo, hi)
        self._rows = []
        self._fill_known()

    def _on_plan(self, kind, args):
        """Вызывается в потоке обработки движка."""
        if kind != "plan" or not args or args[0] != TAG:
            return
        _tag, i, n, freq, iq = args
        with self._lock:
            sc = self.scanner
            if sc is None:
                return
            sc.process(freq, iq, i)
            data = {"i": i, "n": n, "rows": sc.snapshot(), "progress": sc.progress()}
        self._update.emit(data)

    def _on_update(self, d):
        if not self.scanning():
            return
        self.prog.setRange(0, d["n"])
        self.prog.setValue(d["i"] + 1)
        p = d["progress"]
        self.info.setText(f"Проход {p['pass']} · окно {d['i'] + 1}/{d['n']} · "
                          f"{_mhz(p['freq'] or 0)} МГц · DMR-каналов: {p['dmr']}")
        self._rows = d["rows"]
        self._draw()

    def _tick(self):
        if self.scanning() and not self.engine.running():
            self.stop()
            self.info.setText("Приём остановлен — сканирование прервано.")
        self._draw(force=True)

    # ------------------------------------------------------------------ таблица
    def _fill_known(self):
        sc = DmrScanner(2400000, KNOWN[0][0], KNOWN[0][0]) if self.scanner is None else self.scanner
        self._rows = sc.snapshot()
        self._draw(force=True)

    def _draw(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_draw < 0.4:
            return
        self._last_draw = now
        rows = [r for r in self._rows if r["kind"] in ("dmr", "known") or not self.only_dmr.isChecked()]
        rows.sort(key=lambda r: (r["kind"] != "dmr", r["kind"] == "known", r["freq"]))
        sel = self.table.currentRow()
        sel_f = self.table.item(sel, 0).data(Qt.UserRole) if sel >= 0 and self.table.item(sel, 0) else None
        self.table.setRowCount(len(rows))
        bold = QFont(self.table.font())
        bold.setBold(True)
        for i, r in enumerate(rows):
            text = describe(r)
            if r["kind"] == "dmr" and r["ids"]:
                text += "\n" + ", ".join(r["ids"][:3])
            if r["kind"] not in ("known",) and r["hits"]:
                text += f" · {r['hits']}×"
            tip = [f"{_mhz(r['freq'])} МГц", describe(r)]
            if r["note"]:
                tip.append("Ранее: " + r["note"])
            if r["slots"]:
                tip.append("Слоты: " + ", ".join(str(s) for s in sorted(r["slots"])))
            if r["types"]:
                tip.append("Видели: " + ", ".join(r["types"]))
            if r["ids"]:
                tip.append("Последние ID: " + "; ".join(r["ids"]))
            if r["hits"]:
                tip.append(f"Слышен {r['hits']} раз, уровень {r['level_db']:.0f} дБ над шумом")
            cells = [_mhz(r["freq"]), text, _ago(r["last"]) if r["hits"] else "—"]
            for c, val in enumerate(cells):
                it = self.table.item(i, c)
                if it is None:
                    it = QTableWidgetItem()
                    self.table.setItem(i, c, it)
                it.setText(val)
                it.setToolTip("\n".join(tip))
                it.setData(Qt.UserRole, r["freq"])
                it.setData(Qt.UserRole + 1, "DMR" if r["kind"] in ("dmr", "known") else "NFM")
                it.setFont(bold if (r["kind"] == "dmr" and c < 2) else self.table.font())
            if r["freq"] == sel_f:
                self.table.selectRow(i)
        self.table.resizeRowsToContents()

    def _pick(self, row, _col):
        it = self.table.item(row, 0)
        if it is None:
            return
        if self.scanning():
            self.stop()                 # иначе источник так и будет перестраиваться по плану
        self.tuneRequested.emit(float(it.data(Qt.UserRole)), str(it.data(Qt.UserRole + 1)))

    def shutdown(self):
        self.stop()
        self._timer.stop()
