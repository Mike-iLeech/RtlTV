"""Диагностика: журнал %LOCALAPPDATA%/SDR4NOOB/sdr4noob.log, необработанные исключения всех потоков
и «сторож» зависаний — если поток интерфейса не отвечает дольше HANG_S, в журнал пишутся стеки всех потоков."""
import faulthandler
import logging
import os
import sys
import threading
import time
import traceback
from . import paths

LOG_DIR = paths.service_root()
LOG_PATH = os.path.join(LOG_DIR, "sdr4noob.log")
HANG_S = 5.0

log = logging.getLogger("rtltv")
_beat = [time.monotonic()]
_fault_file = None


def setup():
    global _fault_file
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        if os.path.getsize(LOG_PATH) > 5_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".old")
    except OSError:
        pass
    logging.basicConfig(filename=LOG_PATH, level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(threadName)s %(levelname)s %(message)s")
    log.info("==== запуск SDR4NOOB, Python %s", sys.version.split()[0])
    # падения в DLL (librtlsdr, mbelib, lrpt_dsp) — стек Python в отдельный файл
    _fault_file = open(os.path.join(LOG_DIR, "sdr4noob-crash.log"), "a", encoding="utf-8")
    faulthandler.enable(_fault_file, all_threads=True)

    def hook(t, v, tb):
        log.error("Необработанное исключение:\n%s", "".join(traceback.format_exception(t, v, tb)))
    sys.excepthook = hook
    threading.excepthook = lambda a: log.error("Исключение в потоке %s:\n%s", a.thread.name if a.thread else "?",
                                               "".join(traceback.format_exception(a.exc_type, a.exc_value,
                                                                                  a.exc_traceback)))
    threading.Thread(target=_watchdog, name="watchdog", daemon=True).start()


def heartbeat():
    _beat[0] = time.monotonic()


def _watchdog():
    reported = False
    while True:
        time.sleep(1.0)
        late = time.monotonic() - _beat[0]
        if late > HANG_S and not reported:
            reported = True
            frames = sys._current_frames()
            names = {t.ident: t.name for t in threading.enumerate()}
            out = [f"ЗАВИСАНИЕ интерфейса: нет ответа {late:.1f} с. Стеки потоков:"]
            for ident, fr in frames.items():
                out.append(f"--- {names.get(ident, ident)} ---\n" + "".join(traceback.format_stack(fr)))
            log.error("\n".join(out))
        elif late < 1.5 and reported:
            log.info("интерфейс снова отвечает")
            reported = False
