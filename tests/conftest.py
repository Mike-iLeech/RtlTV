"""Тесты не должны читать и портить настройки пользователя (%APPDATA%/RtlTV/*.json):
до импорта rtltv подменяем APPDATA на временную папку. Кэши в LOCALAPPDATA (TLE, карты) — общие."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="rtltv-tests-")
os.environ["APPDATA"] = _tmp
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ["RTLTV_MUTE"] = "1"
