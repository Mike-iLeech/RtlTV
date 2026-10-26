"""Настройки режимов: <osplat.config_dir()>/<имя>.json (Windows: %APPDATA%/SDR4NOOB; ТВ хранит свои в settings.json).

До версии 3.0 программа называлась RtlTV и хранила настройки в %APPDATA%/RtlTV — при первом запуске
они копируются сюда (старая папка остаётся нетронутой: можно вернуться на прежнюю версию)."""
import json
import os
import shutil

from . import osplat

DIR = osplat.config_dir()
OLD_DIR = os.path.join(osplat.config_base(), "RtlTV")


def _migrate():
    if os.environ.get("RTLTV_NO_MIGRATE") or os.path.exists(DIR) or not os.path.isdir(OLD_DIR):
        return
    try:
        shutil.copytree(OLD_DIR, DIR)
    except OSError:
        pass


_migrate()


def load(name, defaults):
    s = dict(defaults)
    try:
        with open(os.path.join(DIR, name + ".json"), encoding="utf-8") as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    return s


def save(name, data):
    try:
        os.makedirs(DIR, exist_ok=True)
        with open(os.path.join(DIR, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
