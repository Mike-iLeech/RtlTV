"""Экран выбора режима при запуске и переключение между режимами."""
import importlib
import threading

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QApplication, QCheckBox, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from . import __version__, settings
from .themes import THEMES, qss

MODES = [
    # ключ, модуль, класс, название, описание, значок
    ("tv", "rtltv.app", "MainWindow", "ТВ",
     "Аналоговое ТВ (PAL/SECAM/NTSC) и измеритель цифровых мультиплексов DVB-T2", "tv"),
    ("radio", "rtltv.modes.radio_window", "RadioWindow", "Радио",
     "Как SDR++: WFM-стерео, NFM, AM, USB/LSB, CW и DMR с голосом; спектр, водопад, закладки", "radio"),
    ("sstv", "rtltv.modes.sstv_window", "SstvWindow", "SSTV",
     "Картинки по радио: КВ 14,230 МГц, МКС 145,800 МГц; Robot, Martin, Scottie, PD", "sstv"),
    ("sat", "rtltv.modes.sat_window", "SatWindow", "Спутники",
     "«Метеор-М» и «Электро-Л» (снимки), МКС и любительские спутники: пролёты на карте, прослушивание с доплером", "sat"),
    ("adsb", "rtltv.modes.adsb_window", "AdsbWindow", "Самолёты (ADS-B)",
     "1090 МГц: борта на карте с курсом, высотой, скоростью и позывным", "plane"),
    ("aprs", "rtltv.modes.aprs_window", "AprsWindow", "APRS", "Пакеты радиолюбителей на карте: 144,800 МГц и МКС 145,825 МГц", "radio"),
]


def warm_up():
    """Пока пользователь выбирает режим — в фоне то, на что окно режима тратило ~4 с при открытии:
    опрос USB-приёмников (devices.prefetch) и импорт scipy.signal (~1 с)."""
    from . import devices
    devices.prefetch()

    def imp():
        try:
            import scipy.signal  # noqa: F401
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=imp, name="warm-imports", daemon=True).start()

def _icon_path(kind, s):
    p = QPainterPath()
    if kind == "tv":
        p.addRoundedRect(QRectF(-s, -0.65 * s, 2 * s, 1.3 * s), 0.15 * s, 0.15 * s)
        p.moveTo(-0.4 * s, -0.65 * s)
        p.lineTo(-0.75 * s, -1.1 * s)
        p.moveTo(0.4 * s, -0.65 * s)
        p.lineTo(0.75 * s, -1.1 * s)
    elif kind == "radio":
        p.addRoundedRect(QRectF(-s, -0.45 * s, 2 * s, 1.1 * s), 0.15 * s, 0.15 * s)
        p.addEllipse(QRectF(-0.75 * s, -0.2 * s, 0.65 * s, 0.65 * s))
        p.moveTo(0.85 * s, -0.45 * s)
        p.lineTo(0.3 * s, -1.15 * s)
        for i in range(3):
            p.moveTo(0.15 * s, (-0.15 + 0.2 * i) * s)
            p.lineTo(0.8 * s, (-0.15 + 0.2 * i) * s)
    elif kind == "sstv":
        p.addRect(QRectF(-s, -0.75 * s, 2 * s, 1.5 * s))
        p.moveTo(-s, 0.5 * s)
        p.lineTo(-0.3 * s, -0.2 * s)
        p.lineTo(0.2 * s, 0.3 * s)
        p.lineTo(0.5 * s, 0.0)
        p.lineTo(s, 0.5 * s)
        p.addEllipse(QRectF(0.35 * s, -0.55 * s, 0.35 * s, 0.35 * s))
    elif kind == "sat":
        p.addRect(QRectF(-0.25 * s, -0.25 * s, 0.5 * s, 0.5 * s))
        p.addRect(QRectF(-1.1 * s, -0.2 * s, 0.7 * s, 0.4 * s))
        p.addRect(QRectF(0.4 * s, -0.2 * s, 0.7 * s, 0.4 * s))
        p.moveTo(0, 0.25 * s)
        p.lineTo(0, 0.6 * s)
        p.addEllipse(QRectF(-0.25 * s, 0.6 * s, 0.5 * s, 0.3 * s))
    else:
        pts = [(0, -1), (0.12, -0.55), (0.12, -0.15), (0.95, 0.25), (0.95, 0.4), (0.12, 0.18), (0.1, 0.7),
               (0.38, 0.9), (0.38, 1.0), (0, 0.9)]
        full = pts + [(-x, y) for x, y in reversed(pts)]
        p.moveTo(full[0][0] * s, full[0][1] * s)
        for x, y in full[1:]:
            p.lineTo(x * s, y * s)
        p.closeSubpath()
    return p


class Tile(QWidget):
    clicked = Signal()

    def __init__(self, title, desc, icon):
        super().__init__()
        self.title, self.desc, self.icon = title, desc, icon
        self.hover = False
        self.colors = THEMES["Тёмная"]
        self.setMinimumSize(250, 190)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(desc)

    def enterEvent(self, _e):
        self.hover = True
        self.update()

    def leaveEvent(self, _e):
        self.hover = False
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()

    def paintEvent(self, _e):
        t = self.colors
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        p.setPen(QPen(QColor(t["accent"] if self.hover else t["grid"]), 2 if self.hover else 1))
        p.setBrush(QColor(t["panel2"] if self.hover else t["panel"]))
        p.drawRoundedRect(r, 12, 12)
        s = min(r.width(), r.height()) * 0.16
        p.save()
        p.translate(r.center().x(), r.top() + r.height() * 0.32)
        p.setPen(QPen(QColor(t["accent"]), 2.2))
        p.setBrush(Qt.NoBrush)
        p.drawPath(_icon_path(self.icon, s))
        p.restore()
        f = QFont(self.font())
        f.setPointSizeF(15)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(t["fg"]))
        p.drawText(QRectF(r.left(), r.top() + r.height() * 0.52, r.width(), 30), Qt.AlignCenter, self.title)
        f.setPointSizeF(9)
        f.setBold(False)
        p.setFont(f)
        p.setPen(QColor(t["dim"]))
        p.drawText(QRectF(r.left() + 14, r.top() + r.height() * 0.52 + 32, r.width() - 28, r.height() * 0.4),
                   Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, self.desc)


class Launcher(QWidget):
    """Окно выбора режима. Держит текущее окно режима и возвращается после «Режим → Выбрать…»."""

    def __init__(self, opts=None):
        super().__init__()
        self.opts = dict(opts or {})
        self.app_s = settings.load("app", {"theme": "Тёмная", "last": "radio"})
        self.setWindowTitle(f"SDR4NOOB {__version__} — выбор режима")
        self.resize(900, 560)
        self.current = None
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 18, 24, 18)
        from .branding import app_icon
        top = QHBoxLayout()
        top.setSpacing(14)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(56, 56))
        top.addWidget(logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        head = QLabel("SDR4NOOB")
        head.setStyleSheet("font-size: 26pt; font-weight: bold;")
        names.addWidget(head)
        idea = QLabel("Всё в одном для начинающего радиолюбителя")
        idea.setStyleSheet("font-size: 12pt;")
        names.addWidget(idea)
        top.addLayout(names)
        top.addStretch(1)
        v.addLayout(top)
        sub = QLabel("Приёмник на RTL-SDR: ТВ, радио, цифровая связь, SSTV, спутники, самолёты, APRS. "
                     "Выберите режим — вернуться сюда можно из меню «Режим» (Ctrl+M).")
        sub.setObjectName("dim")
        v.addWidget(sub)
        grid = QGridLayout()
        grid.setSpacing(12)
        self.tiles = []
        for i, (key, _mod, _cls, title, desc, icon) in enumerate(MODES):
            t = Tile(title, desc, icon)
            t.clicked.connect(lambda k=key: self.open_mode(k))
            grid.addWidget(t, i // 3, i % 3)
            self.tiles.append(t)
        v.addLayout(grid, 1)
        self.cb_test = QCheckBox("Без антенны: тестовый эфир вместо донгла")
        self.cb_test.setChecked(self.opts.get("source") == "test")
        bottom = QHBoxLayout()
        bottom.addWidget(self.cb_test, 1)
        bottom.addWidget(QPushButton("📖 Руководство", toolTip="Руководство для начинающего радиолюбителя (F1 в окне режима)", clicked=lambda: importlib.import_module("rtltv.guide_window").show_guide("index", self)))
        self.btn_devices = QPushButton("Устройства…")
        self.btn_devices.setToolTip("Другие приёмники: HackRF, Airspy, сетевой rtl_tcp, свой драйвер RTL-SDR")
        self.btn_devices.clicked.connect(self.show_devices)
        bottom.addWidget(self.btn_devices)
        b_set = QPushButton("⚙ Настройки")
        b_set.setToolTip("Где хранить записи, снимки, звук, журналы и служебные файлы")
        b_set.clicked.connect(lambda: importlib.import_module("rtltv.settings_dialog").show_settings(self))
        bottom.addWidget(b_set)
        b_upd = QPushButton("⟳ Обновления")
        b_upd.setToolTip("Проверить новую версию на GitHub")
        b_upd.clicked.connect(lambda: importlib.import_module("rtltv.update_ui").check_now(self))
        bottom.addWidget(b_upd)
        v.addLayout(bottom)
        if not self.opts.get("shot"):
            importlib.import_module("rtltv.update_ui").maybe_check_on_start(self)
        self.apply_theme()
        warm_up()

    def apply_theme(self):
        name = self.app_s.get("theme", "Тёмная")
        t = THEMES.get(name, THEMES["Тёмная"])
        QApplication.instance().setStyleSheet(qss(t))
        for tile in self.tiles:
            tile.colors = t
            tile.update()

    def show_devices(self):
        from .devices_dialog import DevicesDialog
        DevicesDialog(self).exec()

    def _save_app(self, **kw):
        """Сохранить свои ключи поверх свежих настроек (окна режимов и «Устройства…» пишут туда же)."""
        self.app_s.update(kw)
        fresh = settings.load("app", {})
        fresh.update(kw)
        settings.save("app", fresh)

    def open_mode(self, key):
        entry = next(m for m in MODES if m[0] == key)
        import logging
        logging.getLogger("rtltv").info("открыт режим %s (тестовый эфир: %s)", key, self.cb_test.isChecked())
        try:
            cls = getattr(importlib.import_module(entry[1]), entry[2])
        except Exception as e:  # noqa: BLE001
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "SDR4NOOB", f"Режим «{entry[3]}» не загрузился:\n{e}")
            return
        self._save_app(last=key)
        from . import devices
        if not devices.has_hardware():   # приёмник могли подключить после запуска — опросить заново
            devices.scan_hardware(refresh=True)
        opts = dict(self.opts)
        opts["theme"] = self.app_s.get("theme", "Тёмная")
        if self.cb_test.isChecked():
            opts["source"] = "test"
        win = cls(opts)
        win.switchRequested.connect(self._back)
        self.current = win
        self.hide()
        win.show()

    def _back(self):
        win = self.current
        self.current = None
        if win is not None:
            self._save_app(theme=getattr(win, "theme_name", self.app_s.get("theme")))
            win.deleteLater()
        self.apply_theme()
        self.show()  # сразу, иначе приложение решит, что окон не осталось, и завершится
