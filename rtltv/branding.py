"""Название, идея и логотип SDR4NOOB (логотип — тот же, что на сайте: rtltv/assets/sdr4noob.svg)."""
import os

from . import NAME, SITE_URL, TAGLINE  # noqa: F401 — реэкспорт для удобства

__all__ = ["NAME", "SITE_URL", "TAGLINE", "app_icon", "LOGO_SVG", "LOGO_ICO"]

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
LOGO_SVG = os.path.join(ASSETS, "sdr4noob.svg")
LOGO_ICO = os.path.join(ASSETS, "sdr4noob.ico")

_icon = None


def app_icon():
    """QIcon логотипа во всех размерах (SVG рисуется под нужный размер; ICO — запасной вариант)."""
    global _icon
    if _icon is None:
        from PySide6.QtGui import QIcon
        _icon = QIcon(LOGO_SVG) if os.path.exists(LOGO_SVG) else QIcon(LOGO_ICO)
        if _icon.isNull() and os.path.exists(LOGO_ICO):
            _icon = QIcon(LOGO_ICO)
    return _icon
