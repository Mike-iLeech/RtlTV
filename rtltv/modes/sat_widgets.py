"""Виджеты режима «Спутники»: созвездие, снимок с прокруткой, диалог параметров записи IQ, панель с прокруткой."""
import os
import re

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QLabel, QScrollArea,
                               QSizePolicy, QSpinBox, QVBoxLayout, QWidget)

from .. import lrpt


class PanelScroll(QScrollArea):
    """Прокрутка панели только по вертикали; ширина подстраивается под минимальную ширину содержимого
    (пересчёт при каждой смене раскладки — после применения темы/шрифтов минимум меняется)."""

    def __init__(self, inner, min_width, max_width):
        super().__init__()
        self._min, self._max = min_width, max_width
        self.setWidget(inner)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        inner.installEventFilter(self)
        self._fit()

    def _fit(self):
        need = self.widget().minimumSizeHint().width() + self.verticalScrollBar().sizeHint().width() + 6
        self.setMinimumWidth(max(self._min, need))
        self.setMaximumWidth(max(self._max, need))

    def eventFilter(self, obj, e):
        if obj is self.widget() and e.type() in (QEvent.LayoutRequest, QEvent.Polish, QEvent.StyleChange):
            self._fit()
        return False


def scroll_panel(widgets, min_width=300, max_width=400):
    """Вертикальная панель в QScrollArea без горизонтальной прокрутки."""
    inner = QWidget()
    v = QVBoxLayout(inner)
    v.setContentsMargins(4, 4, 4, 4)
    for w in widgets:
        v.addWidget(w)
    v.addStretch(1)
    # длинные пункты списков не должны распирать панель: ширина по минимуму, текст — в подсказке
    for cb in inner.findChildren(QComboBox):
        cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        cb.setMinimumContentsLength(10)
    return PanelScroll(inner, min_width, max_width)


def wrap_label(text=""):
    lb = QLabel(text)
    lb.setWordWrap(True)
    lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lb


class ConstellationView(QWidget):
    """Созвездие как гистограмма плотности (QImage 128×128 с послесвечением), квадратная область,
    масштаб по размеру виджета. Точки принимаются как есть (numpy N×2), рисование — один drawImage."""

    N = 128
    RANGE = 1.6

    def __init__(self):
        super().__init__()
        self.setMinimumSize(150, 150)
        sp = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.bg, self.fg, self.dot = "#15181d", "#2f3542", "#7fd1ff"
        self.acc = np.zeros((self.N, self.N), np.float32)
        self.img = None
        self._lut = None
        self._pending = None
        self._last_id = None
        self._build_lut()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return w

    def sizeHint(self):
        return QSize(220, 220)

    def _build_lut(self):
        b, d = QColor(self.bg), QColor(self.dot)
        k = np.linspace(0, 1, 256)[:, None] ** 0.6
        c0 = np.array([b.red(), b.green(), b.blue()], np.float32)
        c1 = np.array([d.red(), d.green(), d.blue()], np.float32)
        lut = c0 * (1 - k) + np.minimum(255, c1 * 1.25) * k
        self._lut = np.clip(lut, 0, 255).astype(np.uint8)

    def set_theme(self, t):
        self.bg, self.fg, self.dot = t["bg"], t["grid"], t["line"]
        self._build_lut()
        self.update()

    def set_points(self, pts):
        """Дёшево: запомнить точки; гистограмма считается при отрисовке (не чаще кадров экрана)."""
        if pts is None or id(pts) == self._last_id:
            return
        self._last_id = id(pts)
        self._pending = pts
        self.update()

    def _accumulate(self):
        pts = self._pending
        self._pending = None
        if pts is None or not len(pts):
            return
        n, r = self.N, self.RANGE
        ix = np.clip(((pts[:, 0] + r) * (n / (2 * r))).astype(np.int32), 0, n - 1)
        iy = np.clip(((r - pts[:, 1]) * (n / (2 * r))).astype(np.int32), 0, n - 1)
        h = np.bincount(iy * n + ix, minlength=n * n).reshape(n, n).astype(np.float32)
        self.acc *= 0.6                         # послесвечение
        self.acc += h
        v = np.clip(self.acc * (255.0 / max(1.0, float(np.percentile(self.acc[self.acc > 0], 98))
                                            if np.any(self.acc > 0) else 1.0)), 0, 255).astype(np.uint8)
        rgb = np.ascontiguousarray(self._lut[v])
        self._rgb = rgb
        self.img = QImage(rgb.data, n, n, 3 * n, QImage.Format_RGB888)

    def paintEvent(self, _e):
        if self._pending is not None:
            self._accumulate()
        p = QPainter(self)
        s = min(self.width(), self.height())
        r = QRectF((self.width() - s) / 2, (self.height() - s) / 2, s, s)
        p.fillRect(r, QColor(self.bg))
        if self.img is not None:
            p.drawImage(r, self.img)
        p.setPen(QPen(QColor(self.fg), 1))
        p.drawRect(r.adjusted(0, 0, -1, -1))
        c = r.center()
        p.drawLine(QPointF(r.left(), c.y()), QPointF(r.right(), c.y()))
        p.drawLine(QPointF(c.x(), r.top()), QPointF(c.x(), r.bottom()))


class ImageScroll(QScrollArea):
    """Снимок на всю ширину (или целиком по высоте) с вертикальной прокруткой."""

    def __init__(self, text="Снимков пока нет."):
        super().__init__()
        self.lbl = QLabel(text)
        self.lbl.setAlignment(Qt.AlignCenter)
        self.lbl.setWordWrap(True)
        self.setWidget(self.lbl)
        self.setWidgetResizable(True)
        self.setAlignment(Qt.AlignCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.qimg = None
        self.arr = None
        self.fit = False

    def set_text(self, text):
        self.qimg = None
        self.lbl.setPixmap(QPixmap())
        self.lbl.setText(text)

    def set_array(self, arr, follow=False, fit=False):
        if arr is None:
            return
        a = np.ascontiguousarray(arr)
        self.arr = a
        self.fit = fit
        if a.ndim == 2:
            self.qimg = QImage(a.data, a.shape[1], a.shape[0], a.shape[1], QImage.Format_Grayscale8)
        else:
            self.qimg = QImage(a.data, a.shape[1], a.shape[0], a.shape[1] * 3, QImage.Format_RGB888)
        self._render()
        if follow:
            QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))

    def _render(self):
        if self.qimg is None:
            return
        vw = max(120, self.viewport().width() - 2)
        vh = max(120, self.viewport().height() - 2)
        pm = QPixmap.fromImage(self.qimg)
        if self.fit:
            pm = pm.scaled(vw, vh, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        else:
            pm = pm.scaledToWidth(vw, Qt.SmoothTransformation)
        self.lbl.setPixmap(pm)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._render()


class FileDialog(QDialog):
    """Параметры записи IQ: формат, частота дискретизации, модуляция (для LRPT)."""

    def __init__(self, parent, path, lrpt_mod=True, default_fs=1024000):
        super().__init__(parent)
        self.setWindowTitle("Декодировать запись IQ")
        f = QFormLayout(self)
        f.addRow(wrap_label(os.path.basename(path)))
        self.fmt = QComboBox()
        for k, name in (("wav", "WAV, 2 канала (I/Q)"), ("cu8", "cu8 / raw — 8 бит без знака (rtl_sdr)"),
                        ("cs8", "cs8 — 8 бит со знаком"), ("cs16", "cs16 — 16 бит со знаком"),
                        ("cf32", "cf32 — float32")):
            self.fmt.addItem(name, k)
        g = lrpt.guess_format(path)
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(g)))
        f.addRow("Формат:", self.fmt)
        self.fs = QSpinBox()
        self.fs.setRange(150000, 20000000)
        self.fs.setSingleStep(1000)
        self.fs.setSuffix(" Гц")
        m = re.search(r"(\d{5,8})\s*(?:hz|sps)", os.path.basename(path), re.I)
        self.fs.setValue(int(m.group(1)) if m else default_fs)
        if g == "wav":
            try:
                self.fs.setValue(lrpt.wav_info(path)[0])
            except (OSError, ValueError):
                pass
        f.addRow("Частота дискретизации:", self.fs)
        self.mod = QComboBox()
        if lrpt_mod:
            self.mod.addItem("Авто (OQPSK и QPSK)", None)
            self.mod.addItem("OQPSK 72k — «Метеор-М» №2-3/№2-4", True)
            self.mod.addItem("QPSK 72k — старый «Метеор-М» №2", False)
        else:
            self.mod.addItem("BPSK 294k — «Электро-Л» LRIT", None)
        f.addRow("Модуляция:", self.mod)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Декодировать")
        bb.button(QDialogButtonBox.Cancel).setText("Отмена")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
