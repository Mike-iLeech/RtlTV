"""Навигация по спектру: обзорная полоса всего диапазона приёмника и вертикальные ручки справа (как в SDR#)."""
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QLabel, QSlider, QToolTip, QVBoxLayout, QWidget

from . import bandplan


class BandOverview(QWidget):
    """Весь диапазон приёмника на одной полосе (логарифмическая шкала частот).

    Сверху — классы волн (километровые … дециметровые), ниже — кто занимает диапазон (вещание, авиа,
    любители, сотовая…), рамка — что сейчас видно на спектре. Щелчок или перетаскивание — перейти
    на частоту (сигнал jumpRequested), подсказка под курсором — частота, длина волны и владелец."""

    jumpRequested = Signal(float)

    F_LO, F_HI = 0.1e6, 2000e6       # шкала; приёмник реально берёт ~0,5…1766 МГц
    RX_LO, RX_HI = 0.5e6, 1766e6

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)
        self.setMinimumHeight(64)
        self.setMaximumHeight(64)
        self.view = None           # (f0, f1) — что видно на основном спектре
        self.vfo = None
        self.colors = dict(bg="#15181d", grid="#2f3542", fg="#d8dee9", accent="#ffd84d")
        self._lanes = bandplan.lanes(bandplan.ALLOCATIONS)
        self._nl = 1 + max(i for i, _ in self._lanes)
        self._drag = False
        self.setToolTip("")

    def set_theme(self, t):
        self.colors = dict(bg=t["bg"], grid=t["grid"], fg=t["fg"], accent=t.get("accent", "#ffd84d"))
        self.update()

    def set_view(self, f0, f1, vfo=None):
        if self.view != (f0, f1) or self.vfo != vfo:
            self.view = (f0, f1)
            self.vfo = vfo
            self.update()

    # ------------------------------------------------------------ геометрия
    def _bar(self):
        return QRectF(6, 2, self.width() - 12, self.height() - 4)

    def _x(self, f):
        r = self._bar()
        return r.left() + bandplan.log_pos(f, self.F_LO, self.F_HI) * r.width()

    def _f(self, x):
        r = self._bar()
        return bandplan.log_freq((x - r.left()) / max(r.width(), 1), self.F_LO, self.F_HI)

    # ------------------------------------------------------------ мышь
    def _jump(self, x):
        f = min(max(self._f(x), self.RX_LO), self.RX_HI)
        self.jumpRequested.emit(f)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = True
            self._jump(e.position().x())

    def mouseMoveEvent(self, e):
        if self._drag and e.buttons() & Qt.LeftButton:
            self._jump(e.position().x())
        f = self._f(e.position().x())
        QToolTip.showText(e.globalPosition().toPoint(), bandplan.describe(f), self)

    def mouseReleaseEvent(self, _e):
        self._drag = False

    def leaveEvent(self, _e):
        QToolTip.hideText()

    # ------------------------------------------------------------ рисование
    def paintEvent(self, _e):
        p = QPainter(self)
        c = self.colors
        p.fillRect(self.rect(), QColor(c["bg"]))
        r = self._bar()
        f = QFont(self.font())
        f.setPointSizeF(7.5)
        p.setFont(f)
        fm = p.fontMetrics()
        # 1) классы волн
        wy, wh = r.top(), 14
        for i, (f0, f1, name, common, lam) in enumerate(bandplan.WAVE_CLASSES):
            x0, x1 = self._x(f0), self._x(f1)
            if x1 <= r.left() or x0 >= r.right():
                continue
            x0, x1 = max(x0, r.left()), min(x1, r.right())
            col = QColor(c["grid"])
            col.setAlpha(110 if i % 2 else 60)
            p.fillRect(QRectF(x0, wy, x1 - x0, wh), col)
            for text in (f"{common} · {name} ({lam})", f"{common} · {name}", common):
                if fm.horizontalAdvance(text) + 6 < x1 - x0:
                    p.setPen(QColor(c["fg"]))
                    p.drawText(QRectF(x0, wy, x1 - x0, wh), Qt.AlignCenter, text)
                    break
        # 2) кто занимает
        ly = wy + wh + 2
        lh = max(8.0, (r.bottom() - 12 - ly) / self._nl)
        for lane, (f0, f1, short, _desc, kind) in self._lanes:
            x0, x1 = self._x(f0), max(self._x(f1), self._x(f0) + 2)
            y = ly + lane * lh
            col = QColor(bandplan.KINDS[kind][0])
            col.setAlpha(170)
            p.fillRect(QRectF(x0, y, x1 - x0, lh - 1), col)
            if fm.horizontalAdvance(short) + 4 < x1 - x0 and lh >= 10:
                p.setPen(QColor("#101010"))
                p.drawText(QRectF(x0, y, x1 - x0, lh - 1), Qt.AlignCenter, short)
        # 3) шкала частот
        p.setPen(QColor(c["fg"]))
        for fv, txt in ((0.5e6, "0,5"), (1e6, "1"), (3e6, "3"), (10e6, "10"), (30e6, "30"), (100e6, "100"),
                        (300e6, "300"), (1000e6, "1000 МГц")):
            x = self._x(fv)
            p.setPen(QPen(QColor(c["grid"]), 1))
            p.drawLine(QPointF(x, r.bottom() - 11), QPointF(x, r.bottom() - 8))
            p.setPen(QColor(c["fg"]))
            p.drawText(QRectF(x - 40, r.bottom() - 11, 80, 11), Qt.AlignCenter, txt)
        # недоступное приёмнику — затемнить
        shade = QColor(0, 0, 0, 120)
        p.fillRect(QRectF(r.left(), r.top(), self._x(self.RX_LO) - r.left(), r.height()), shade)
        p.fillRect(QRectF(self._x(self.RX_HI), r.top(), r.right() - self._x(self.RX_HI), r.height()), shade)
        # 4) что видно сейчас
        if self.view:
            x0, x1 = self._x(self.view[0]), self._x(self.view[1])
            if x1 - x0 < 4:
                m = (x0 + x1) / 2
                x0, x1 = m - 2, m + 2
            acc = QColor(c["accent"])
            p.setPen(QPen(acc, 1.5))
            fill = QColor(acc)
            fill.setAlpha(50)
            p.setBrush(fill)
            p.drawRect(QRectF(x0, r.top(), x1 - x0, r.height() - 1))
        if self.vfo:
            x = self._x(self.vfo)
            p.setPen(QPen(QColor(c["accent"]), 1))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))


class _VSlider(QWidget):
    """Ручка-пилюля: скруглённая плашка с подписью сверху, вертикальным ползунком и значением снизу.
    Зеркалит обычный QSlider панели (двигаешь любой — меняется второй)."""

    def __init__(self, title, master, fmt, tip):
        super().__init__()
        self.master = master
        self.fmt = fmt
        self.colors = dict(bg="#1b2028", grid="#2f3542", fg="#d8dee9", accent="#4fb3ff")
        self.setAttribute(Qt.WA_StyledBackground, False)
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 7, 4, 7)
        v.setSpacing(3)
        self.title = QLabel(title)
        self.title.setAlignment(Qt.AlignHCenter)
        f = self.title.font()
        f.setPointSizeF(8)
        f.setBold(True)
        self.title.setFont(f)
        v.addWidget(self.title)
        self.s = QSlider(Qt.Vertical)
        self.s.setRange(master.minimum(), master.maximum())
        self.s.setValue(master.value())
        self.s.setToolTip(tip)
        self.title.setToolTip(tip)
        self.setToolTip(tip)
        self.s.setMinimumHeight(28)
        v.addWidget(self.s, 1, Qt.AlignHCenter)
        self.val = QLabel()
        self.val.setAlignment(Qt.AlignHCenter)
        fv = self.val.font()
        fv.setPointSizeF(7.5)
        self.val.setFont(fv)
        v.addWidget(self.val)
        self.setFixedWidth(62)
        self.s.valueChanged.connect(self._mine)
        master.valueChanged.connect(self._theirs)
        self._show(master.value())

    def set_theme(self, t):
        self.colors = dict(bg=t.get("panel", t["bg"]), grid=t["grid"], fg=t["fg"], accent=t.get("accent", "#4fb3ff"))
        self.title.setStyleSheet(f"color: {self.colors['accent']};")
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        bg = QColor(self.colors["bg"])
        bg.setAlpha(235)
        p.setBrush(bg)
        p.setPen(QPen(QColor(self.colors["grid"]), 1))
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        rad = min(r.width() / 2, 16.0)
        p.drawRoundedRect(r, rad, rad)

    def _show(self, x):
        self.val.setText(self.fmt(x))

    def _mine(self, x):
        if self.master.value() != x:
            self.master.setValue(x)
        self._show(x)

    def _theirs(self, x):
        if self.s.value() != x:
            self.s.blockSignals(True)
            self.s.setValue(x)
            self.s.blockSignals(False)
        self._show(x)


class SpectrumSideBar(QWidget):
    """Столбик из ручек-пилюль справа от спектра (одна над другой). knobs: [(заголовок, QSlider, формат, подсказка)].

    Каждая ручка — копия существующего регулятора на панели: двигаешь любую — меняется вторая."""

    def __init__(self, knobs):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(5, 4, 4, 4)
        v.setSpacing(6)
        self.knobs = []
        for title, master, fmt, tip in knobs:
            k = _VSlider(title, master, fmt, tip)
            self.knobs.append(k)
            v.addWidget(k, 1)

    def set_theme(self, t):
        for k in self.knobs:
            k.set_theme(t)
