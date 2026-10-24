"""Где RtlTV хранит файлы. Одна корневая «папка данных» (выбирается в «Настройках»), внутри — порядок:

    Записи IQ\\            сырые записи пролётов (cu8)
    Снимки\\ТВ, Метеор, Электро-Л, SSTV
    Звук\\                 записи радио (WAV)
    Журналы\\ADS-B, APRS
    Служебное\\            кэш карт, орбиты (TLE), история самолётов, справочник бортов, драйверы, журнал
                           программы — только если включено «служебное тоже здесь», иначе %LOCALAPPDATA%\\RtlTV

Настройки (*.json) всегда лежат в %APPDATA%\\RtlTV — по ним программа и узнаёт, где папка данных.
Пути читаются при загрузке модулей, поэтому смена папки вступает в силу после перезапуска.
"""
import ctypes
import os
import shutil

from . import settings

APP = "RtlTV"


def _known_folder(guid_str, fallback):
    """Путь известной папки Windows (учитывает перенос «Документов» в OneDrive и т.п.)."""
    try:
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]
        h = guid_str.strip("{}").split("-")
        g = GUID(int(h[0], 16), int(h[1], 16), int(h[2], 16),
                 (ctypes.c_ubyte * 8)(*bytes.fromhex(h[3] + h[4])))
        p = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) == 0:
            path = p.value
            ctypes.windll.ole32.CoTaskMemFree(p)
            if path:
                return path
    except Exception:  # noqa: BLE001
        pass
    return fallback


HOME = os.path.expanduser("~")
DOCUMENTS = _known_folder("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", os.path.join(HOME, "Documents"))
PICTURES = _known_folder("{33E28130-4E1E-4676-835A-98395C3BC3BB}", os.path.join(HOME, "Pictures"))
MUSIC = _known_folder("{4BD8D571-6D19-48D3-BE97-422220080E43}", os.path.join(HOME, "Music"))
LOCAL = os.path.join(os.environ.get("LOCALAPPDATA") or HOME, APP)

DEFAULT_DATA = os.path.join(DOCUMENTS, APP)

# подпапки внутри папки данных
IQ = "Записи IQ"
PICS = "Снимки"
PICS_TV, PICS_METEOR, PICS_ELEKTRO, PICS_SSTV = "ТВ", "Метеор", "Электро-Л", "SSTV"
AUDIO = "Звук"
LOGS = "Журналы"
LOGS_ADSB, LOGS_APRS = "ADS-B", "APRS"
SERVICE = "Служебное"


def _app():
    return settings.load("app", {})


def data_root():
    d = _app().get("data_dir")
    return os.path.abspath(d) if d else DEFAULT_DATA


def service_in_data():
    return bool(_app().get("service_in_data"))


def service_root():
    return os.path.join(data_root(), SERVICE) if service_in_data() else LOCAL


def iq_dir():
    return os.path.join(data_root(), IQ)


def pictures(kind=None):
    base = os.path.join(data_root(), PICS)
    return os.path.join(base, kind) if kind else base


def audio_dir():
    return os.path.join(data_root(), AUDIO)


def logs(kind=None):
    base = os.path.join(data_root(), LOGS)
    return os.path.join(base, kind) if kind else base


def service(*parts):
    return os.path.join(service_root(), *parts)


def layout():
    """[(подпапка, что в ней)] — для окна настроек."""
    out = [(IQ, "сырые записи пролётов спутников (cu8)"),
           (os.path.join(PICS, PICS_TV), "снимки кадров ТВ (F12)"),
           (os.path.join(PICS, PICS_METEOR), "снимки «Метеор-М»"),
           (os.path.join(PICS, PICS_ELEKTRO), "снимки «Электро-Л»"),
           (os.path.join(PICS, PICS_SSTV), "картинки SSTV"),
           (AUDIO, "записи радио (WAV)"),
           (os.path.join(LOGS, LOGS_ADSB), "журналы ADS-B (CSV, KML)"),
           (os.path.join(LOGS, LOGS_APRS), "журналы пакетов APRS")]
    if service_in_data():
        out.append((SERVICE, "кэш карт, орбиты, история самолётов, справочник бортов, драйверы, журнал"))
    return out


# ---------------------------------------------------------------- перенос из старых мест

def legacy_items(root=None, with_service=None):
    """Что лежит в прежних местах и куда это переносить: [(откуда, куда, размер байт)]."""
    root = root or data_root()
    with_service = service_in_data() if with_service is None else with_service
    pairs = [
        (os.path.join(DOCUMENTS, APP, "sat-iq"), os.path.join(root, IQ)),
        (os.path.join(DOCUMENTS, APP, "meteor-iq"), os.path.join(root, IQ)),
        (os.path.join(DOCUMENTS, APP, "ADS-B"), os.path.join(root, LOGS, LOGS_ADSB)),
        (os.path.join(DOCUMENTS, APP, "aprs"), os.path.join(root, LOGS, LOGS_APRS)),
        (os.path.join(PICTURES, APP, "meteor"), os.path.join(root, PICS, PICS_METEOR)),
        (os.path.join(PICTURES, APP, "elektro"), os.path.join(root, PICS, PICS_ELEKTRO)),
        (os.path.join(PICTURES, APP, "sstv"), os.path.join(root, PICS, PICS_SSTV)),
        (os.path.join(PICTURES, APP), os.path.join(root, PICS, PICS_TV)),   # снимки ТВ лежали в корне
        (os.path.join(MUSIC, APP), os.path.join(root, AUDIO)),
    ]
    # прежняя папка данных (если пользователь уже раз менял её) — по тем же подпапкам
    old_root = _app().get("data_dir_prev")
    if old_root and os.path.abspath(old_root) != os.path.abspath(root):
        for sub, _ in layout() + [(SERVICE, "")]:
            pairs.append((os.path.join(old_root, sub), os.path.join(root, sub)))
    if with_service:
        for name in ("tiles", "tle", "drivers", "flags"):
            pairs.append((os.path.join(LOCAL, name), os.path.join(root, SERVICE, name)))
        for name in ("adsb_history.json", "aircraft_cache.json"):
            pairs.append((os.path.join(LOCAL, name), os.path.join(root, SERVICE, name)))
    out = []
    for src, dst in pairs:
        if os.path.abspath(src) == os.path.abspath(dst) or not os.path.exists(src):
            continue
        if os.path.isdir(src):
            if src == os.path.join(PICTURES, APP):   # из корня «Изображения\RtlTV» — только файлы (снимки ТВ)
                files = [f for f in os.listdir(src) if os.path.isfile(os.path.join(src, f))]
            else:
                files = os.listdir(src)
            if not files:
                continue
            size = sum(_size(os.path.join(src, f)) for f in files)
            out.append((src, dst, size, files))
        else:
            out.append((src, dst, _size(src), None))
    return out


def _size(p):
    if os.path.isfile(p):
        return os.path.getsize(p)
    total = 0
    for dp, _dn, fn in os.walk(p):
        for f in fn:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


def move_items(items, progress=None):
    """Перенести (shutil.move по файлу/подпапке; существующие файлы не перезаписываются — к имени добавляется
    суффикс). Возвращает (перенесено, ошибки)."""
    moved, errors = 0, []
    total = len(items)
    for i, (src, dst, _size_b, files) in enumerate(items):
        try:
            if files is None:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, _free_name(dst))
                moved += 1
            else:
                os.makedirs(dst, exist_ok=True)
                for f in files:
                    shutil.move(os.path.join(src, f), _free_name(os.path.join(dst, f)))
                    moved += 1
                try:
                    os.rmdir(src)   # только если опустела
                except OSError:
                    pass
        except OSError as e:
            errors.append(f"{src}: {e}")
        if progress:
            progress(i + 1, total)
    return moved, errors


def _free_name(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    k = 2
    while os.path.exists(f"{base} ({k}){ext}"):
        k += 1
    return f"{base} ({k}){ext}"
