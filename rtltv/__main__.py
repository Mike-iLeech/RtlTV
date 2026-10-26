import argparse
import importlib
import sys



def configure(data_dir=None, updates=None):
    """Записать выбор из установщика в настройки (app.json) и выйти. Окон не открывает."""
    import os
    from . import paths, settings
    s = settings.load("app", {})
    if data_dir:
        root = os.path.abspath(os.path.expandvars(data_dir))
        old = paths.data_root()
        if os.path.normcase(root) != os.path.normcase(old):
            s["data_dir_prev"] = old
        s["data_dir"] = None if os.path.normcase(root) == os.path.normcase(paths.DEFAULT_DATA) else root
        try:
            os.makedirs(root, exist_ok=True)
        except OSError:
            pass
    if updates in ("daily", "weekly", "never"):
        s["update_check"] = updates
    settings.save("app", s)
    return 0


def main():
    if "--configure" in sys.argv:
        cp = argparse.ArgumentParser(prog="SDR4NOOB --configure")
        cp.add_argument("--configure", action="store_true")
        cp.add_argument("--data-dir")
        cp.add_argument("--updates", choices=["daily", "weekly", "never"])
        c, _rest = cp.parse_known_args()
        sys.exit(configure(c.data_dir, c.updates))
    from PySide6.QtWidgets import QApplication
    from .launcher import MODES, Launcher
    ap = argparse.ArgumentParser(prog="SDR4NOOB", description="SDR4NOOB — всё в одном для начинающего радиолюбителя: ТВ, радио, SSTV, спутники, ADS-B, APRS на RTL-SDR")
    ap.add_argument("--mode", choices=[m[0] for m in MODES], help="сразу открыть режим, минуя выбор")
    ap.add_argument("--test", action="store_true", help="тестовый эфир вместо донгла")
    ap.add_argument("--digital", action="store_true", help="ТВ: запуск в цифровом режиме")
    ap.add_argument("--shot", help="сохранить снимок окна через --shot-delay секунд и выйти")
    ap.add_argument("--shot-delay", type=float, default=8.0)
    ap.add_argument("--freq", type=float, help="ТВ: частота, МГц")
    ap.add_argument("--auto", action="store_true", help="Спутники: сразу включить автоприём по расписанию")
    ap.add_argument("--selftest-guide", action="store_true", help=argparse.SUPPRESS)  # проверка сборки в CI
    a = ap.parse_args()
    from . import diag
    diag.setup()
    from . import osplat
    osplat.use_bundled_portaudio()   # Linux без libportaudio2 — встроенная (до первого import sounddevice)
    from . import updater
    updater.cleanup_legacy_exe()   # RtlTV.exe от 3.0 и ярлыки на него — после обновления с 3.0
    if osplat.IS_LINUX:
        # справка открывает только свои локальные страницы (guide_window.web_nav), а песочница Chromium в AppImage
        # не запускается без setuid chrome-sandbox / непривилегированных пространств имён (Ubuntu 24.04)
        import os
        os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    from PySide6.QtCore import QCoreApplication, Qt
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)  # до QApplication — нужно QtWebEngine (схема в справке)
    app = QApplication(sys.argv)
    app.setApplicationName("SDR4NOOB")
    if osplat.IS_LINUX:
        app.setDesktopFileName("sdr4noob")   # значок окна в Wayland — из sdr4noob.desktop
    from .branding import app_icon
    app.setWindowIcon(app_icon())   # иконка всех окон и панели задач
    if a.selftest_guide:            # встроенный браузер справки собран и оживает (код выхода 0)
        from .guide_window import selftest_interactive
        sys.exit(selftest_interactive())
    from PySide6.QtCore import QTimer
    beat = QTimer()
    beat.timeout.connect(diag.heartbeat)
    beat.start(500)
    opts = {"shot": a.shot, "shot_delay": a.shot_delay}
    if a.test:
        opts["source"] = "test"
    if a.digital:
        opts["mode"] = "digital"
    if a.auto:
        opts["auto"] = True
    launcher = Launcher(opts)
    if a.mode or a.digital or a.freq:
        key = a.mode or "tv"
        entry = next(m for m in MODES if m[0] == key)
        cls = getattr(importlib.import_module(entry[1]), entry[2])
        o = dict(opts)
        o["theme"] = launcher.app_s.get("theme", "Тёмная")
        w = cls(o)
        w.switchRequested.connect(launcher._back)
        launcher.current = w
        if a.freq and hasattr(w, "_set_freq"):
            w._set_freq(a.freq * 1e6)
        w.show()
    elif a.shot:
        from PySide6.QtCore import QTimer
        launcher.show()
        QTimer.singleShot(int(a.shot_delay * 1000), lambda: (launcher.grab().save(a.shot), app.quit()))
    else:
        launcher.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
