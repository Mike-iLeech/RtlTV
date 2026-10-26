"""SDR4NOOB — всё в одном для начинающего радиолюбителя: любой USB радиоприёмник (SDR) — ТВ, радио, SSTV,
спутники, ADS-B, APRS. Внутреннее имя пакета — rtltv (историческое: проект начинался как RtlTV)."""
from .i18n import N_

__version__ = "3.3.1"

NAME = "SDR4NOOB"
TAGLINE = N_("Всё в одном для начинающего радиолюбителя")  # на экран — tr(TAGLINE)
REPO = "Mike-iLeech/SDR4NOOB"
SITE_URL = "https://mike-ileech.github.io/SDR4NOOB/"


def site_url():
    """Сайт на языке интерфейса (английская версия — в подпапке en/)."""
    from .i18n import language
    return SITE_URL + ("en/" if language() == "en" else "")
