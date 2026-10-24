"""Настройки режимов: %APPDATA%/RtlTV/<имя>.json (ТВ хранит свои в settings.json)."""
import json
import os

DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "RtlTV")


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
