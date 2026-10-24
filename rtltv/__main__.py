import argparse
import importlib
import sys

from PySide6.QtWidgets import QApplication

from .launcher import MODES, Launcher


def main():
    ap = argparse.ArgumentParser(prog="RtlTV", description="Приёмник на RTL-SDR: ТВ, радио, SSTV, спутники, ADS-B")
    ap.add_argument("--mode", choices=[m[0] for m in MODES], help="сразу открыть режим, минуя выбор")
    ap.add_argument("--test", action="store_true", help="тестовый эфир вместо донгла")
    ap.add_argument("--digital", action="store_true", help="ТВ: запуск в цифровом режиме")
    ap.add_argument("--shot", help="сохранить снимок окна через --shot-delay секунд и выйти")
    ap.add_argument("--shot-delay", type=float, default=8.0)
    ap.add_argument("--freq", type=float, help="ТВ: частота, МГц")
    ap.add_argument("--auto", action="store_true", help="Спутники: сразу включить автоприём по расписанию")
    a = ap.parse_args()
    from . import diag
    diag.setup()
    app = QApplication(sys.argv)
    app.setApplicationName("RtlTV")
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
