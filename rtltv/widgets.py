"""Виджеты: экран ТВ, спектр с водопадом, частотное табло с колёсиком по разрядам, индикатор уровня."""
import time

import numpy as np
from PySide6.QtCore import QByteArray, QDataStream, QIODevice, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from .themes import gradient_lut


class VideoView(QWidget):
    doubleClicked = Signal()

    def __init__(self):
        super().__init__()
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.img = None
        self._arr = None
        self.osd = ""
        self.osd_until = 0.0
        self.status = "Нет сигнала"
        self.overlay = ""
        self.aspect = 4 / 3

    def set_rgb(self, rgb):
        self._arr = np.ascontiguousarray(rgb)
        h, w, _ = self._arr.shape
        self.img = QImage(self._arr.data, w, h, 3 * w, QImage.Format_RGB888)
        self.update()

    def clear(self, status):
        self.img = None
        self.status = status
        self.update()

    def show_osd(self, text, secs=3.0):
        self.osd = text
        self.osd_until = time.time() + secs
        self.update()

    def mouseDoubleClickEvent(self, _e):
        self.doubleClicked.emit()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000"))
        W, H = self.width(), self.height()
        if H * self.aspect > W:
            w, h = W, W / self.aspect
        else:
            w, h = H * self.aspect, H
        r = QRectF((W - w) / 2, (H - h) / 2, w, h)
        if self.img is not None:
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawImage(r, self.img)
        else:
            p.fillRect(r, QColor("#0a1a6a"))
            p.setPen(QColor("#ffffff"))
            f = QFont(self.font())
            f.setPointSizeF(max(14, h / 18))
            p.setFont(f)
            p.drawText(r, Qt.AlignCenter, self.status)
        f = QFont(self.font())
        f.setBold(True)
        f.setPointSizeF(max(12, h / 22))
        p.setFont(f)
        if self.osd and time.time() < self.osd_until:
            p.setPen(QColor(0, 0, 0, 200))
            p.drawText(r.adjusted(18, 14, 0, 0), Qt.AlignLeft | Qt.AlignTop, self.osd)
            p.setPen(QColor("#5dff5d"))
            p.drawText(r.adjusted(16, 12, 0, 0), Qt.AlignLeft | Qt.AlignTop, self.osd)
        if self.overlay and self.img is not None:
            f.setPointSizeF(max(9, h / 40))
            f.setBold(False)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 170))
            p.drawText(r.adjusted(0, 0, -10, -8), Qt.AlignRight | Qt.AlignBottom, self.overlay)


def _lut32(lut):
    """Палитра 256×3 (uint8) -> 256 пикселей RGB32 (0xFFRRGGBB)."""
    lut = lut.astype(np.uint32)
    return (0xFF000000 | (lut[:, 0] << 16) | (lut[:, 1] << 8) | lut[:, 2]).astype(np.uint32)


def nice_step(span, px, min_px=110):
    """Шаг сетки по частоте 1-2-2,5-5 × 10^n: подписи не ближе min_px пикселей."""
    target = span / max(1.0, px / min_px)
    if target <= 0:
        return 1.0
    mag = 10 ** np.floor(np.log10(target))
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= target:
            return float(m * mag)
    return float(10 * mag)


def mhz_decimals(step_hz):
    """Сколько знаков после запятой нужно, чтобы подписи в МГц с шагом step_hz различались."""
    v = step_hz / 1e6
    for d in range(0, 7):
        if abs(round(v, d) - v) < 1e-9:
            return max(1, d)
    return 6


class SpectrumView(QWidget):
    """Спектр (верх) и водопад (низ). Клик — настроиться на частоту, колесо — шаг частоты.

    Необязательное (включается, только если подключены соответствующие сигналы или заданы атрибуты):
    zoom/zoom_center — масштаб по частоте вокруг zoom_center; Shift+колесо -> zoomWheel;
    перетаскивание края подсвеченной полосы bands[0] -> bandEdgeDragged(сторона 0/1, частота),
    перетаскивание внутри полосы -> bandDragged(сдвиг, Гц); wf_offset/wf_contrast/wf_speed/split —
    яркость, контраст и скорость водопада, доля высоты под спектр."""

    tuneRequested = Signal(float)
    wheelStep = Signal(int)
    ctrlWheelStep = Signal(int)     # Ctrl+колесо (например, ширина полосы)
    panRequested = Signal(float)    # перетаскивание: сдвинуть центр на столько Гц (плавная прокрутка)
    zoomWheel = Signal(int)         # Shift+колесо (масштаб); без подписчиков — как обычное колесо
    bandEdgeDragged = Signal(int, float)   # край полосы демодулятора: 0 — левый, 1 — правый; частота
    bandDragged = Signal(float)     # полосу демодулятора тянут мышью: сдвиг VFO, Гц

    def __init__(self, waterfall=True):
        super().__init__()
        self.setMinimumHeight(150)
        self.setMouseTracking(True)
        self.waterfall = waterfall
        self.freqs = None
        self.psd = None
        self.avg = None
        self.peak = None
        self.markers = []   # (частота, цвет, подпись)
        self.bands = []     # (f0, f1, цвет)
        self.lo, self.hi = -60.0, 0.0
        self.wf = None
        self.wf_img = None
        self.lut = gradient_lut([(0, "#000010"), (1, "#ffffff")])
        self.lut32 = _lut32(self.lut)
        self.colors = dict(bg="#15181d", grid="#2f3542", line="#7fd1ff", fill="#1e5d8a", fg="#d8dee9")
        self.hover = None
        self.smooth = 0.5
        self.show_peak = True
        self.waterfall_on = True
        self.auto_range = True
        self.pan_enabled = False
        # QObject.receivers(SignalInstance) в PySide6 не работает — окна включают возможности флагами
        self.band_drag_enabled = False   # перетаскивание краёв/самой полосы (bandEdgeDragged, bandDragged)
        self.zoom_enabled = False        # Shift+колесо — масштаб (zoomWheel)   # перетаскивание сдвигает полосу (panRequested) — включает окно, которому это нужно
        self.man_lo, self.man_hi = -60.0, 0.0
        self._pending = None
        self._disp = None   # (xs, ys_avg, ys_peak) в пикселях — кэш для рисования
        self.zoom = 1.0
        self.zoom_center = None
        self.full_span = None       # (f0, f1) всей полосы до масштабирования
        self.wf_offset = 0.0        # яркость водопада: сдвиг нижней границы, дБ
        self.wf_contrast = 1.0      # контраст водопада: во сколько раз уже диапазон, чем у спектра
        self.wf_speed = 1.0         # строк водопада на кадр спектра (0,5 — каждый второй кадр)
        self._wf_acc = 0.0
        self.split = 0.55           # доля высоты под спектр (при включённом водопаде)
        self._drag_kind = None
        # данные только запоминаются, а перерисовка идёт таймером: не копится очередь и нет «отставания»
        self._timer = QTimer(self)
        # точный таймер: обычный в Windows округляется до ~15,6 мс, и «30 к/с» превращались в 21
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self._consume)
        self.set_fps(20)

    def set_fps(self, fps):
        self.fps = max(1, int(fps))
        self._timer.start(int(1000 / self.fps))

    def set_theme(self, t):
        self.colors = dict(bg=t["bg"], grid=t["grid"], line=t["line"], fill=t["fill"], fg=t["fg"])
        self.lut = gradient_lut(t["cmap"])
        self.lut32 = _lut32(self.lut)
        self.wf = None
        self.update()

    def set_data(self, freqs, psd):
        """Дёшево: только запомнить последний спектр (вызывается с любой частотой)."""
        self._pending = (freqs, psd)

    def _consume(self):
        if self._pending is None or not self.isVisible():
            return
        freqs, psd = self._pending
        self._pending = None
        self._apply(freqs, psd)

    def _apply(self, freqs, psd):
        self.full_span = (float(freqs[0]), float(freqs[-1]))
        if self.zoom > 1.0 and freqs.size > 32:
            # масштаб: показываем 1/zoom полосы вокруг zoom_center (не выходя за края)
            half = (freqs[-1] - freqs[0]) / self.zoom / 2
            zc = self.zoom_center if self.zoom_center is not None else 0.5 * (freqs[0] + freqs[-1])
            zc = min(max(zc, freqs[0] + half), freqs[-1] - half)
            i0 = int(np.searchsorted(freqs, zc - half))
            n = max(16, int(round(freqs.size / self.zoom)))
            i0 = min(max(i0, 0), freqs.size - n)
            freqs, psd = freqs[i0:i0 + n], psd[i0:i0 + n]
        # сжатие до ширины в пикселях (максимум по группе — пики не теряются)
        w = max(64, int(self._plot_rect().width()))
        if psd.size > w:
            edges = np.linspace(0, psd.size, w + 1).astype(np.int64)
            psd = np.maximum.reduceat(psd, edges[:-1])
            freqs = freqs[edges[:-1]]
        if (self.freqs is not None and self.freqs.size != freqs.size and self.avg is not None
                and abs(self.freqs[0] - freqs[0]) < 2 * (freqs[1] - freqs[0])
                and abs(self.freqs[-1] - freqs[-1]) < 2 * (self.freqs[1] - self.freqs[0])):
            # изменилась только ширина окна (число точек) — пересчитываем усреднение под новый размер,
            # водопад не сбрасываем (его ширину подгонит _push_wf)
            self.avg = np.interp(freqs, self.freqs, self.avg)
            self.peak = np.interp(freqs, self.freqs, self.peak)
            self.freqs = freqs
        span_same = (self.freqs is not None and self.freqs.size == freqs.size
                     and abs((self.freqs[-1] - self.freqs[0]) - (freqs[-1] - freqs[0])) < 1)
        if span_same and abs(self.freqs[0] - freqs[0]) > 1:
            # центр сдвинулся (прокрутка): сдвигаем водопад и усреднение, а не сбрасываем
            step = (freqs[-1] - freqs[0]) / max(freqs.size - 1, 1)
            k = int(round((freqs[0] - self.freqs[0]) / step))
            if abs(k) < freqs.size:
                self.avg = np.roll(self.avg, -k)
                self.peak = np.roll(self.peak, -k)
                if k > 0:
                    self.avg[-k:] = psd[-k:]
                    self.peak[-k:] = psd[-k:]
                elif k < 0:
                    self.avg[:-k] = psd[:-k]
                    self.peak[:-k] = psd[:-k]
                if self.wf is not None:
                    kw = int(round(k * self.wf.shape[1] / freqs.size))
                    self.wf = np.roll(self.wf, -kw, axis=1)
                    if kw > 0:
                        self.wf[:, -kw:] = self.lut32[0]
                    elif kw < 0:
                        self.wf[:, :-kw] = self.lut32[0]
            else:
                self.avg, self.peak, self.wf = psd.copy(), psd.copy(), None
        if self.freqs is None or self.freqs.size != freqs.size or not span_same:
            self.avg = psd.copy()
            self.peak = psd.copy()
            self.wf = None
        else:
            self.avg = self.smooth * self.avg + (1 - self.smooth) * psd
            self.peak = np.maximum(self.peak - 0.3, psd)
        self.freqs = freqs
        self.psd = psd
        if self.auto_range:
            lo, hi = float(np.percentile(self.avg, 3)) - 6, float(self.avg.max()) + 6
            self.lo = 0.9 * self.lo + 0.1 * lo
            self.hi = 0.9 * self.hi + 0.1 * max(hi, lo + 30)
        else:
            self.lo, self.hi = self.man_lo, max(self.man_hi, self.man_lo + 5)
        if self.waterfall and self.waterfall_on:
            self._wf_acc += self.wf_speed
            while self._wf_acc >= 1.0:
                self._wf_acc -= 1.0
                self._push_wf()
        self._disp = None
        self.update()

    def _draw_fill(self, p, r, ys):
        """Полупрозрачная градиентная заливка под линией спектра: маска «ниже линии» × градиент (uint32)."""
        w, h = int(r.width()), int(r.height())
        if w < 2 or h < 2:
            return
        top = np.interp(np.arange(w), np.linspace(0, w - 1, ys.size), ys - r.top()).astype(np.int32)
        key = (w, h, self.colors["fill"])
        if getattr(self, "_grad_key", None) != key:
            fc = QColor(self.colors["fill"])
            a = np.linspace(255, 40, h)
            # ARGB32 premultiplied: 0xAARRGGBB
            self._grad = ((a.astype(np.uint32) << 24) | ((fc.red() * a / 255).astype(np.uint32) << 16)
                          | ((fc.green() * a / 255).astype(np.uint32) << 8)
                          | (fc.blue() * a / 255).astype(np.uint32))[:, None]
            self._rows = np.arange(h, dtype=np.int32)[:, None]
            self._grad_key = key
        self._fill_buf = np.ascontiguousarray((self._rows >= top[None, :]) * self._grad, dtype=np.uint32)
        qi = QImage(self._fill_buf.data, w, h, 4 * w, QImage.Format_ARGB32_Premultiplied)
        p.drawImage(QPointF(r.left(), r.top()), qi)

    def _poly(self, xs, ys):
        """QPolygonF из массивов numpy без цикла Python: через QDataStream (число точек + пары double)."""
        n = int(min(len(xs), len(ys)))
        buf = np.empty(n * 2, ">f8")
        buf[0::2] = xs[:n]
        buf[1::2] = ys[:n]
        ba = QByteArray(np.array([n], ">u4").tobytes() + buf.tobytes())
        poly = QPolygonF()
        QDataStream(ba, QIODevice.ReadOnly) >> poly
        return poly

    def _plot_rect(self):
        h = self.height()
        top_h = h * (self.split if (self.waterfall and self.waterfall_on) else 1.0)
        return QRectF(46, 6, self.width() - 52, top_h - 24)

    def _push_wf(self):
        r = self._plot_rect()
        w = max(16, int(r.width()))
        rows = max(16, int(self.height() - r.bottom() - 22))
        if self.wf is None or self.wf.shape[1] != w or self.wf.shape[0] != rows:
            # 32-битные пиксели (RGB32): Qt рисует их без пересчёта формата при каждой перерисовке
            new = np.empty((rows, w), np.uint32)
            new[:] = self.lut32[0]
            if self.wf is not None:
                # размер окна изменился: сохраняем историю — растягиваем по ширине, по высоте берём сколько влезет
                old_w = self.wf.shape[1]
                cols = np.minimum((np.arange(w) * old_w / w).astype(np.int64), old_w - 1)
                keep = min(rows, self.wf.shape[0])
                new[:keep] = self.wf[:keep][:, cols]
            self.wf = new
        x = np.linspace(0, self.psd.size - 1, w)
        line = np.interp(x, np.arange(self.psd.size), self.psd)
        lo = self.lo + self.wf_offset
        rng = max(self.hi - self.lo, 1) / max(self.wf_contrast, 0.05)
        v = np.clip((line - lo) / rng * 255, 0, 255).astype(np.uint8)
        self.wf[1:] = self.wf[:-1]
        self.wf[0] = self.lut32[v]
        self.wf_img = QImage(self.wf.data, w, rows, 4 * w, QImage.Format_RGB32)

    def _x(self, f, r):
        f0, f1 = self.freqs[0], self.freqs[-1]
        return r.left() + (f - f0) / (f1 - f0) * r.width()

    def _f(self, x, r):
        f0, f1 = self.freqs[0], self.freqs[-1]
        return f0 + (x - r.left()) / r.width() * (f1 - f0)

    def _band_hit(self, pos):
        """Что под курсором: ("edge", 0|1) — край полосы, ("band", None) — внутри неё, None — ничего."""
        if self.freqs is None or not self.bands:
            return None
        r = self._plot_rect()
        if not (r.top() <= pos.y() <= r.bottom()):
            return None
        b0, b1 = self.bands[0][0], self.bands[0][1]
        x0, x1 = sorted((self._x(b0, r), self._x(b1, r)))
        if self.band_drag_enabled:
            for side, x in ((0, x0), (1, x1)):
                if abs(pos.x() - x) <= 5:
                    return ("edge", side)
        if self.band_drag_enabled and x0 < pos.x() < x1:
            return ("band", None)
        return None

    def mousePressEvent(self, e):
        # щелчок — настроиться; перетаскивание (если кто-то слушает panRequested) — плавно сдвинуть полосу
        self._press = e.position()
        self._dragged = False
        self._drag_kind = self._band_hit(e.position()) if e.button() == Qt.LeftButton else None

    def mouseMoveEvent(self, e):
        self.hover = e.position()
        press = getattr(self, "_press", None)
        if press is None and (self.band_drag_enabled or self.band_drag_enabled):
            hit = self._band_hit(e.position())
            self.setCursor(Qt.SizeHorCursor if hit and hit[0] == "edge" else
                           Qt.OpenHandCursor if hit else Qt.ArrowCursor)
        if press is not None and e.buttons() & Qt.LeftButton and self.freqs is not None:
            dx = e.position().x() - press.x()
            kind = getattr(self, "_drag_kind", None)
            if kind is not None and (abs(dx) > 2 or self._dragged):
                r = self._plot_rect()
                self._dragged = True
                if kind[0] == "edge":
                    self.bandEdgeDragged.emit(kind[1], self._f(e.position().x(), r))
                else:
                    hz_per_px = (self.freqs[-1] - self.freqs[0]) / max(r.width(), 1)
                    self.bandDragged.emit(dx * hz_per_px)
                    self._press = e.position()
            elif abs(dx) > 4 or self._dragged:
                if self.pan_enabled:  # receivers(SignalInstance) в PySide6 не работает — явный флаг
                    r = self._plot_rect()
                    hz_per_px = (self.freqs[-1] - self.freqs[0]) / max(r.width(), 1)
                    self.panRequested.emit(-dx * hz_per_px)
                    self._press = e.position()
                self._dragged = True
        self.update()

    def mouseReleaseEvent(self, e):
        press = getattr(self, "_press", None)
        self._press = None
        self._drag_kind = None
        if press is None or self._dragged or self.freqs is None:
            return
        r = self._plot_rect()
        if r.left() <= e.position().x() <= r.right():
            self.tuneRequested.emit(self._f(e.position().x(), r))

    def leaveEvent(self, _e):
        self.hover = None
        self.update()

    def wheelEvent(self, e):
        ad = e.angleDelta()
        dy = ad.y() if ad.y() != 0 else ad.x()   # с Shift Windows присылает горизонтальную прокрутку
        if dy == 0:
            return
        d = 1 if dy > 0 else -1
        if e.modifiers() & Qt.ControlModifier:
            self.ctrlWheelStep.emit(d)
        elif e.modifiers() & Qt.ShiftModifier and self.zoom_enabled:
            self.zoomWheel.emit(d)
        else:
            self.wheelStep.emit(d)

    def paintEvent(self, _e):
        p = QPainter(self)
        c = self.colors
        p.fillRect(self.rect(), QColor(c["bg"]))
        r = self._plot_rect()
        p.setPen(QPen(QColor(c["grid"]), 1))
        p.drawRect(r)
        if self.freqs is None:
            p.setPen(QColor(c["fg"]))
            p.drawText(r, Qt.AlignCenter, "Спектр появится после запуска")
            return
        f = QFont(self.font())
        f.setPointSizeF(8)
        p.setFont(f)
        span = self.hi - self.lo
        step = 10 if span < 80 else 20
        db = np.ceil(self.lo / step) * step
        while db < self.hi:
            y = r.bottom() - (db - self.lo) / span * r.height()
            p.setPen(QPen(QColor(c["grid"]), 1, Qt.DotLine))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor(c["fg"]))
            p.drawText(QRectF(0, y - 8, 42, 16), Qt.AlignRight | Qt.AlignVCenter, f"{db:.0f}")
            db += step
        f0, f1 = self.freqs[0], self.freqs[-1]
        fstep = nice_step(f1 - f0, r.width())
        dec = mhz_decimals(fstep)
        ft = np.ceil(f0 / fstep) * fstep
        while ft <= f1:
            x = self._x(ft, r)
            p.setPen(QPen(QColor(c["grid"]), 1, Qt.DotLine))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setPen(QColor(c["fg"]))
            p.drawText(QRectF(x - 50, r.bottom() + 2, 100, 16), Qt.AlignCenter, f"{ft / 1e6:.{dec}f}")
            ft += fstep
        for b0, b1, col in self.bands:
            x0, x1 = sorted((self._x(b0, r), self._x(b1, r)))
            x0, x1 = max(x0, r.left()), min(x1, r.right())
            if x1 <= x0:
                continue
            qc = QColor(col)
            qc.setAlpha(40)
            p.fillRect(QRectF(x0, r.top(), x1 - x0, r.height()), qc)
        n = self.avg.size
        xs = r.left() + np.arange(n) / max(n - 1, 1) * r.width()
        ys = r.bottom() - np.clip((self.avg - self.lo) / span, 0, 1) * r.height()
        line = self._poly(xs, ys)
        # заливка под спектром — картинкой из numpy (растеризация многоугольника в Qt на тысячах точек
        # съедала до 70 % потока интерфейса); линии — перо ровно 1 px (быстрый путь без построения контура)
        self._draw_fill(p, r, ys)
        p.setBrush(Qt.NoBrush)
        lp = QPen(QColor(c["line"]), 1)
        lp.setCosmetic(True)
        p.setPen(lp)
        p.drawPolyline(line)
        if self.show_peak:
            pk = r.bottom() - np.clip((self.peak - self.lo) / span, 0, 1) * r.height()
            pc = QColor(c["line"])
            pc.setAlpha(80)
            pp = QPen(pc, 1)
            pp.setCosmetic(True)
            p.setPen(pp)
            p.drawPolyline(self._poly(xs, pk))
        for fm, col, label in self.markers:
            if f0 <= fm <= f1:
                x = self._x(fm, r)
                p.setPen(QPen(QColor(col), 1.5, Qt.DashLine))
                p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
                p.setPen(QColor(col))
                p.drawText(QRectF(x + 3, r.top() + 2, 120, 14), Qt.AlignLeft, label)
        if self.hover is not None and r.contains(self.hover):
            fh = self._f(self.hover.x(), r)
            p.setPen(QPen(QColor(c["fg"]), 1, Qt.DotLine))
            p.drawLine(QPointF(self.hover.x(), r.top()), QPointF(self.hover.x(), r.bottom()))
            hz_px = (f1 - f0) / max(r.width(), 1)
            hd = int(min(6, max(3, np.ceil(-np.log10(max(hz_px, 1e-3) / 1e6)))))
            p.drawText(QRectF(self.hover.x() + 4, r.bottom() - 18, 160, 16), Qt.AlignLeft,
                       f"{fh / 1e6:.{hd}f} МГц")
        if self.waterfall and self.waterfall_on and self.wf_img is not None:
            top = r.bottom() + 20
            p.drawImage(QRectF(r.left(), top, r.width(), self.height() - top - 2), self.wf_img)


class FreqDisplay(QWidget):
    """Табло частоты «583.250.000 Гц»: колесо над разрядом меняет именно его, клик сверху/снизу — ±1."""

    changed = Signal(float)

    def __init__(self, lo=24e6, hi=1766e6):
        super().__init__()
        self.lo, self.hi = lo, hi
        self.value = 583.25e6
        self.color = "#7fffd4"
        self.dim = "#335544"
        self.bg = "#000"
        self._rects = []
        self.setMinimumHeight(52)
        self.setToolTip("Колесо мыши над цифрой — изменить этот разряд. Щелчок по верху/низу цифры — ±1.")

    def set_theme(self, t):
        self.color = t["lcd"]
        c = QColor(t["lcd"])
        c.setAlpha(60)
        self.dim = c.name(QColor.HexArgb)
        self.bg = t["panel2"]
        self.update()

    def set_value(self, v, emit=False):
        v = float(min(max(v, self.lo), self.hi))
        if abs(v - self.value) < 0.5:
            return
        self.value = v
        self.update()
        if emit:
            self.changed.emit(v)

    def _text(self):
        return f"{int(round(self.value)):>10,}".replace(",", ".")

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(self.bg))
        f = QFont("Consolas")
        f.setPixelSize(int(self.height() * 0.62))
        f.setBold(True)
        p.setFont(f)
        fm = QFontMetricsF(f)
        txt = self._text()
        unit = " Гц"
        uf = QFont(f)
        uf.setPixelSize(int(self.height() * 0.3))
        total = fm.horizontalAdvance(txt) + QFontMetricsF(uf).horizontalAdvance(unit)
        x = (self.width() - total) / 2
        y = (self.height() + fm.capHeight()) / 2
        self._rects = []
        digits_right = sum(ch.isdigit() for ch in txt)
        lead = True
        for ch in txt:
            w = fm.horizontalAdvance(ch)
            if ch.isdigit():
                digits_right -= 1
                if ch != "0":
                    lead = False
                self._rects.append((QRectF(x, 0, w, self.height()), 10 ** digits_right))
            if ch == " ":
                x += w
                continue
            p.setPen(QColor(self.dim if lead else self.color))
            p.drawText(QPointF(x, y), ch)
            x += w
        p.setFont(uf)
        p.setPen(QColor(self.color))
        p.drawText(QPointF(x, y), unit)

    def _step_at(self, pos):
        for rect, step in self._rects:
            if rect.contains(pos):
                return step
        return None

    def wheelEvent(self, e):
        step = self._step_at(e.position()) or 1000
        self.set_value(self.value + (step if e.angleDelta().y() > 0 else -step), emit=True)

    def mousePressEvent(self, e):
        step = self._step_at(e.position())
        if step:
            self.set_value(self.value + (step if e.position().y() < self.height() / 2 else -step), emit=True)


class LevelMeter(QWidget):
    def __init__(self, lo=0.0, hi=40.0, unit="дБ"):
        super().__init__()
        self.lo, self.hi, self.unit = lo, hi, unit
        self.value = lo
        self.shown = lo
        self.text = ""
        self.setMinimumHeight(22)
        self.bg = "#252a33"
        self.fg = "#d8dee9"

    def set_theme(self, t):
        self.bg = t["panel2"]
        self.fg = t["fg"]
        self.update()

    def set_value(self, v, text=None):
        self.value = v
        self.shown = 0.6 * self.shown + 0.4 * v
        self.text = text if text is not None else f"{v:.1f} {self.unit}"
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        r = QRectF(self.rect()).adjusted(0, 0, -1, -1)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self.bg))
        p.drawRoundedRect(r, 4, 4)
        frac = min(max((self.shown - self.lo) / (self.hi - self.lo), 0), 1)
        g = QLinearGradient(r.left(), 0, r.right(), 0)
        g.setColorAt(0, QColor("#d0342c"))
        g.setColorAt(0.4, QColor("#e8c53a"))
        g.setColorAt(1, QColor("#3ad06a"))
        p.setBrush(g)
        p.drawRoundedRect(QRectF(r.left(), r.top(), r.width() * frac, r.height()), 4, 4)
        p.setPen(QColor(self.fg))
        p.drawText(r, Qt.AlignCenter, self.text)


class SpectrumControls(QWidget):
    """Настройки спектра и водопада для одного или нескольких SpectrumView:
    частота обновления, разрешение БПФ, усреднение, пики, водопад, шкала (авто/вручную).
    nfft_setter(n) — куда передать размер БПФ (обычно движок: engine.nfft = n).
    Настройки запоминаются в settings «app» → spectrum[key]."""

    FPS = [(5, "5 /с"), (10, "10 /с"), (15, "15 /с"), (20, "20 /с"), (30, "30 /с")]
    NFFT = [(1024, "1024"), (2048, "2048"), (4096, "4096"), (8192, "8192"), (16384, "16384")]

    WF_SPEED = [(0.5, "медленно"), (1.0, "обычно"), (2.0, "быстро"), (3.0, "очень быстро")]

    def __init__(self, views, nfft_setter=None, key="default", nfft_default=4096, extended=False):
        super().__init__()
        self.extended = extended
        from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QSlider, QSpinBox
        from . import settings
        self._settings = settings
        self.views = list(views)
        self.nfft_setter = nfft_setter
        self.key = key
        app_s = settings.load("app", {})
        st = (app_s.get("spectrum") or {}).get(key, {})
        self.st = {"fps": 15, "nfft": nfft_default, "smooth": 50, "peak": True, "waterfall": True,
                   "auto": True, "lo": -70, "hi": 0, "wf_off": 0, "wf_con": 100, "wf_speed": 1.0, "split": 55}
        self.st.update(st)
        f = QFormLayout(self)
        f.setContentsMargins(0, 0, 0, 0)
        self.fps = QComboBox()
        for v, n in self.FPS:
            self.fps.addItem(n, v)
        self.fps.setCurrentIndex(max(0, self.fps.findData(self.st["fps"])))
        self.fps.setToolTip("Сколько раз в секунду перерисовываются спектр и водопад. Меньше — меньше нагрузка на ПК.")
        f.addRow("Обновление:", self.fps)
        self.nfft = QComboBox()
        for v, n in self.NFFT:
            self.nfft.addItem(n + " точек", v)
        self.nfft.setCurrentIndex(max(0, self.nfft.findData(self.st["nfft"])))
        self.nfft.setToolTip("Разрешение спектра: больше точек — тоньше видны узкие сигналы, но выше нагрузка.")
        self.nfft.setEnabled(nfft_setter is not None)
        f.addRow("Разрешение:", self.nfft)
        self.smooth = QSlider(Qt.Horizontal)
        self.smooth.setRange(0, 95)
        self.smooth.setValue(int(self.st["smooth"]))
        self.smooth.setToolTip("Усреднение спектра: больше — спокойнее линия, но медленнее реакция.")
        f.addRow("Усреднение:", self.smooth)
        row = QHBoxLayout()
        self.cb_peak = QCheckBox("Пики")
        self.cb_peak.setChecked(bool(self.st["peak"]))
        self.cb_peak.setToolTip("Линия пикового удержания над спектром")
        self.cb_wf = QCheckBox("Водопад")
        self.cb_wf.setChecked(bool(self.st["waterfall"]))
        row.addWidget(self.cb_peak)
        row.addWidget(self.cb_wf)
        f.addRow(row)
        row2 = QHBoxLayout()
        self.cb_auto = QCheckBox("Шкала авто")
        self.cb_auto.setChecked(bool(self.st["auto"]))
        self.cb_auto.setToolTip("Автоподбор шкалы дБ. Снимите, чтобы задать границы вручную (контраст водопада).")
        self.lo = QSpinBox()
        self.lo.setRange(-160, 40)
        self.lo.setValue(int(self.st["lo"]))
        self.lo.setSuffix(" дБ")
        self.hi = QSpinBox()
        self.hi.setRange(-150, 60)
        self.hi.setValue(int(self.st["hi"]))
        self.hi.setSuffix(" дБ")
        row2.addWidget(self.cb_auto)
        row2.addWidget(self.lo)
        row2.addWidget(self.hi)
        f.addRow(row2)
        sliders = [self.smooth, self.lo, self.hi]
        combos = [self.fps, self.nfft]
        if extended:
            self.wf_off = QSlider(Qt.Horizontal)
            self.wf_off.setRange(-40, 40)
            self.wf_off.setValue(int(self.st["wf_off"]))
            self.wf_off.setToolTip("Яркость водопада (как Offset в SDR#): влево — ярче, слабые сигналы заметнее; "
                                   "вправо — темнее, виден только сильный эфир.")
            f.addRow("Водопад, яркость:", self.wf_off)
            self.wf_con = QSlider(Qt.Horizontal)
            self.wf_con.setRange(25, 400)
            self.wf_con.setValue(int(self.st["wf_con"]))
            self.wf_con.setToolTip("Контраст водопада (как Contrast в SDR#): вправо — уже диапазон цветов, "
                                   "сигналы резче выделяются на фоне шума.")
            f.addRow("Водопад, контраст:", self.wf_con)
            self.wf_speed = QComboBox()
            for v, n in self.WF_SPEED:
                self.wf_speed.addItem(n, v)
            self.wf_speed.setCurrentIndex(max(0, self.wf_speed.findData(float(self.st["wf_speed"]))))
            self.wf_speed.setToolTip("Скорость водопада: строк на кадр спектра. Медленно — длиннее история, "
                                     "быстро — лучше видны короткие посылки.")
            f.addRow("Скорость водопада:", self.wf_speed)
            self.split = QSlider(Qt.Horizontal)
            self.split.setRange(20, 85)
            self.split.setValue(int(self.st["split"]))
            self.split.setToolTip("Какую часть высоты занимает спектр (остальное — водопад)")
            f.addRow("Высота спектра:", self.split)
            sliders += [self.wf_off, self.wf_con, self.split]
            combos.append(self.wf_speed)
        for w in combos:
            w.currentIndexChanged.connect(self._changed)
        for w in sliders:
            w.valueChanged.connect(self._changed)
        for w in (self.cb_peak, self.cb_wf, self.cb_auto):
            w.toggled.connect(self._changed)
        self._apply()

    def add_view(self, v):
        self.views.append(v)
        self._apply()

    def _changed(self, *_):
        self.st.update(fps=self.fps.currentData(), nfft=self.nfft.currentData(), smooth=self.smooth.value(),
                       peak=self.cb_peak.isChecked(), waterfall=self.cb_wf.isChecked(),
                       auto=self.cb_auto.isChecked(), lo=self.lo.value(), hi=self.hi.value())
        if self.extended:
            self.st.update(wf_off=self.wf_off.value(), wf_con=self.wf_con.value(),
                           wf_speed=self.wf_speed.currentData(), split=self.split.value())
        app_s = self._settings.load("app", {})
        sp = app_s.get("spectrum") or {}
        sp[self.key] = self.st
        app_s["spectrum"] = sp
        self._settings.save("app", app_s)
        self._apply()

    def _apply(self):
        st = self.st
        self.lo.setEnabled(not st["auto"])
        self.hi.setEnabled(not st["auto"])
        for v in self.views:
            v.set_fps(st["fps"])
            v.smooth = st["smooth"] / 100.0
            v.show_peak = bool(st["peak"])
            if v.waterfall_on != bool(st["waterfall"]):
                v.waterfall_on = bool(st["waterfall"])
                v.wf = None
            v.auto_range = bool(st["auto"])
            v.man_lo, v.man_hi = float(st["lo"]), float(st["hi"])
            if self.extended:
                v.wf_offset = float(st["wf_off"])
                v.wf_contrast = float(st["wf_con"]) / 100.0
                v.wf_speed = float(st["wf_speed"])
                if abs(v.split - st["split"] / 100.0) > 1e-6:
                    v.split = st["split"] / 100.0
                    v.wf = None
            v.update()
        if self.nfft_setter is not None:
            self.nfft_setter(int(st["nfft"]))
