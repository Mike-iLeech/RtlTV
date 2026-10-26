"""Базовое окно режима: меню «Режим» (возврат к выбору режима), тема, корректное освобождение донгла.

Подкласс обязан:
  * вызвать super().__init__(opts, "Заголовок окна");
  * переопределить shutdown() — остановить свои потоки и закрыть донгл (вызывается при закрытии и смене режима);
  * при желании переопределить apply_theme(t) — перекрасить свои виджеты (QSS приложение ставит само).
opts: {"theme": имя темы, "source": "rtl:0" | "rtltcp:host:port" | "hackrf:0" | ... | "test", "shot": путь снимка окна для автотестов, ...}
"""
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QKeySequence, QPalette
from PySide6.QtWidgets import QApplication, QMainWindow

from . import audio, settings
from .i18n import tr
from .themes import DEFAULT_THEME, THEMES, qss


class ModeWindow(QMainWindow):
    switchRequested = Signal()

    def __init__(self, opts=None, title="SDR4NOOB"):
        super().__init__()
        self.opts = dict(opts or {})
        self.setWindowTitle(title)
        self.resize(1360, 860)
        self._switching = False
        m = self.menuBar().addMenu(tr("Режим"))
        act = QAction(tr("Выбрать другой режим…"), self)
        act.setShortcut(QKeySequence("Ctrl+M"))
        act.triggered.connect(self.request_switch)
        m.addAction(act)
        act_dev = QAction(tr("Устройства…"), self)
        act_dev.setToolTip(tr("Другие приёмники: HackRF, Airspy, сетевой rtl_tcp, свой драйвер RTL-SDR"))
        act_dev.triggered.connect(self.show_devices)
        m.addAction(act_dev)
        act_set = QAction(tr("Настройки…"), self)
        act_set.setShortcut(QKeySequence("Ctrl+,"))
        act_set.setToolTip(tr("Где хранить записи, снимки, звук, журналы и служебные файлы"))
        act_set.triggered.connect(lambda: __import__("rtltv.settings_dialog", fromlist=["x"]).show_settings(self))
        m.addAction(act_set)
        tm = self.menuBar().addMenu(tr("Оформление"))
        for name in THEMES:
            a = QAction(tr(name), self)
            a.triggered.connect(lambda _c=False, n=name: self.set_theme(n))
            tm.addAction(a)
        self.theme_name = self.opts.get("theme") or DEFAULT_THEME
        self.view_s = settings.load("app", {}).get("view") or {}
        self.view_menu = self.menuBar().addMenu(tr("Вид"))
        self._build_view_menu()
        self.sound_menu = self.menuBar().addMenu(tr("Звук"))
        self.sound_menu.aboutToShow.connect(self._fill_sound_menu)
        self._build_help_menu()
        QTimer.singleShot(0, self._init_audio)

    # ---------- справка: руководство (rtltv/guide_window.py) и «О программе» ----------
    GUIDE_PAGE = None  # страница руководства для F1; None — по классу окна (guide_window.MODE_PAGES)

    def _build_help_menu(self):
        hm = self.menuBar().addMenu(tr("Справка"))
        act = QAction(tr("Руководство (F1)…"), self)
        act.setShortcut(QKeySequence("F1"))  # во всех режимах F1 — общее руководство
        act.triggered.connect(self.open_guide)
        hm.addAction(act)
        upd = QAction(tr("Проверить обновления…"), self)
        upd.triggered.connect(lambda: __import__("rtltv.update_ui", fromlist=["x"]).check_now(self))
        hm.addAction(upd)
        site = QAction(tr("Сайт и вики SDR4NOOB"), self)
        site.triggered.connect(lambda: __import__("webbrowser").open(__import__("rtltv").site_url()))
        hm.addAction(site)
        about = QAction(tr("О программе"), self)
        about.triggered.connect(self.show_about)
        hm.addAction(about)

    def open_guide(self, page=None):
        from .guide_window import page_for, show_guide
        show_guide(page if isinstance(page, str) and page else page_for(self), self)

    def show_about(self):
        from PySide6.QtWidgets import QMessageBox
        from . import __version__, site_url
        from .branding import app_icon
        box = QMessageBox(self)
        box.setWindowTitle(tr("О программе"))
        box.setIconPixmap(app_icon().pixmap(72, 72))
        box.setTextFormat(Qt.RichText)
        site = site_url()
        box.setText(tr("<h3>SDR4NOOB {version}</h3><p><b>Всё в одном для начинающего радиолюбителя.</b><br>"
                       "Приёмник на RTL-SDR: ТВ, радио, SSTV, спутники, самолёты (ADS-B), APRS.</p>"
                       "<p>Руководство для начинающих — меню «Справка → Руководство» (F1).</p>"
                       "<p>Сайт и вики: <a href='{site}'>{site_name}</a><br>Исходники и релизы: "
                       "<a href='https://github.com/Mike-iLeech/SDR4NOOB'>github.com/Mike-iLeech/SDR4NOOB</a><br>"
                       "Лицензия GPL-3.0-or-later.</p>",
                       version=__version__, site=site, site_name=site.split("://", 1)[-1].rstrip("/")))
        box.exec()

    # ---------- устройства: диалог «Устройства…» и обновление списка источников ----------
    def show_devices(self):
        from .devices_dialog import DevicesDialog
        dlg = DevicesDialog(self)
        dlg.exec()
        self.devices_changed()

    def devices_changed(self):
        """Перезаполнить self.src_combo (если есть) после «Устройства…». Подкласс может переопределить."""
        combo = getattr(self, "src_combo", None)
        if combo is None:
            return
        from . import devices
        ti = combo.findData("test")
        cur = combo.currentData()
        combo.blockSignals(True)
        try:
            devices.fill_source_combo(combo, cur, test_label=combo.itemText(ti) if ti >= 0 else devices.TEST_LABEL,
                                      include_test=ti >= 0)
        finally:
            combo.blockSignals(False)

    # ---------- вид: частота обновления графиков, усреднение, БПФ ----------
    VIEW_DEFAULTS = {"fps": 20, "smooth": 0.5, "nfft": 4096, "peak": True, "waterfall": True, "video_fps": 25}

    def view_opt(self, key):
        return self.view_s.get(key, self.VIEW_DEFAULTS[key])

    def _build_view_menu(self):
        m = self.view_menu
        m.clear()

        def group(title, key, items):
            sub = m.addMenu(title)
            grp = QActionGroup(self)
            for val, name in items:
                a = QAction(name, self)
                a.setCheckable(True)
                a.setChecked(self.view_opt(key) == val)
                a.triggered.connect(lambda _c=False, k=key, v=val: self._set_view(k, v))
                grp.addAction(a)
                sub.addAction(a)

        group(tr("Частота кадров изображения (ТВ)"), "video_fps",
              [(8, tr("{n} к/с", n=8)), (12, tr("{n} к/с", n=12)), (25, tr("25 к/с (как в эфире)"))])
        note = QAction(tr("Спектр и водопад — панель «Спектр и водопад» в окне режима"), self)
        note.setEnabled(False)
        m.addAction(note)
        QTimer.singleShot(0, self.apply_view)

    def _set_view(self, key, val):
        self.view_s[key] = val
        app_s = settings.load("app", {})
        app_s["view"] = self.view_s
        settings.save("app", app_s)
        self.apply_view()

    def apply_view(self):
        self.view_changed()

    def view_changed(self):
        """Подкласс может перечитать self.view_opt(...) (например, частоту кадров ТВ)."""

    # ---------- звук: общее для всех режимов ----------
    def audio_outputs(self):
        eng = getattr(self, "engine", None)
        a = getattr(eng, "audio", None)
        return [a] if a is not None else []

    def _init_audio(self):
        outs = self.audio_outputs()
        self.sound_menu.menuAction().setVisible(bool(outs))
        if not outs:
            return
        name = settings.load("app", {}).get("audio_device")
        dev = audio.device_by_name(name)
        for a in outs:
            a.device = dev
            a.on_error = lambda msg: QTimer.singleShot(0, lambda: self.statusBar().showMessage(
                tr("{error} — выберите другое устройство в меню «Звук».", error=msg), 15000))

    def _fill_sound_menu(self):
        self.sound_menu.clear()
        outs = self.audio_outputs()
        cur = outs[0].device if outs else None
        for idx, name in audio.output_devices():
            act = QAction(name, self)
            act.setCheckable(True)
            act.setChecked(idx == cur)
            act.triggered.connect(lambda _c=False, i=idx, n=name: self._choose_audio(i, n))
            self.sound_menu.addAction(act)
        if outs and outs[0].info:
            self.sound_menu.addSeparator()
            info = QAction(tr("Сейчас: {device}", device=outs[0].info), self)
            info.setEnabled(False)
            self.sound_menu.addAction(info)

    def _choose_audio(self, idx, name):
        app_s = settings.load("app", {})
        app_s["audio_device"] = None if idx is None else name
        settings.save("app", app_s)
        for a in self.audio_outputs():
            a.set_device(idx)
            if a.stream is not None:
                self.statusBar().showMessage(tr("Звук: {device}", device=a.info), 6000)

    def showEvent(self, e):
        super().showEvent(e)
        if not getattr(self, "_themed", False):
            self._themed = True
            self.set_theme(self.theme_name)
            if self.opts.get("shot"):
                QTimer.singleShot(int(self.opts.get("shot_delay", 8) * 1000), self._shot)

    def _shot(self):
        # opts["shot"] не обнуляем: по нему окна понимают, что настройки сохранять не надо
        if self.opts.get("shot") and not getattr(self, "_shot_done", False):
            self._shot_done = True
            self.grab().save(self.opts["shot"])
            self.close()

    def set_theme(self, name):
        self.theme_name = name if name in THEMES else DEFAULT_THEME
        t = THEMES[self.theme_name]
        app_s = settings.load("app", {})
        if app_s.get("theme") != self.theme_name and not self.opts.get("shot"):
            app_s["theme"] = self.theme_name
            settings.save("app", app_s)
        app = QApplication.instance()
        app.setStyleSheet(qss(t))
        pal = app.palette()  # ссылки в QLabel/QTextBrowser рисуются цветом палитры, не QSS
        pal.setColor(QPalette.Link, QColor(t["accent"]))
        pal.setColor(QPalette.LinkVisited, QColor(t["accent"]))
        app.setPalette(pal)
        self.apply_theme(t)

    def apply_theme(self, t):
        pass

    def shutdown(self):
        pass

    def request_switch(self):
        self._switching = True
        self.close()

    def closeEvent(self, e):
        import logging
        logging.getLogger("rtltv").info("закрыто окно «%s» (смена режима: %s)", self.windowTitle(), self._switching)
        try:
            self.shutdown()
        finally:
            super().closeEvent(e)
        if self._switching:
            self.switchRequested.emit()
