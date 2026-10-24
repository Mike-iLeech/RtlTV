"""Тесты не должны читать и портить настройки пользователя (%APPDATA%/SDR4NOOB/*.json) и переносить его
папки RtlTV → SDR4NOOB (RTLTV_NO_MIGRATE):
до импорта rtltv подменяем APPDATA на временную папку. Кэши в LOCALAPPDATA (TLE, карты) — общие."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="rtltv-tests-")
os.environ["APPDATA"] = _tmp
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ["RTLTV_MUTE"] = "1"
os.environ["RTLTV_NO_MIGRATE"] = "1"
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")  # схема в справке: без GPU в offscreen
from PySide6.QtCore import QCoreApplication, Qt  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)  # до первого QApplication — для QtWebEngine
