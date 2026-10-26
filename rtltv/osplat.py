"""Платформенный слой: всё, что различается в Windows, macOS и Linux, — в одном месте.

Каталоги:
    config_dir()  настройки *.json         Win %APPDATA%\\SDR4NOOB · mac ~/Library/Application Support/SDR4NOOB
                                           · Linux $XDG_CONFIG_HOME/SDR4NOOB (~/.config/SDR4NOOB)
    local_dir()   служебное (кэш карт, TLE, драйверы, журнал, обновления)
                                           Win %LOCALAPPDATA%\\SDR4NOOB · mac …/Application Support/SDR4NOOB/Service
                                           · Linux $XDG_DATA_HOME/SDR4NOOB (~/.local/share/SDR4NOOB)
    documents_dir(), pictures_dir(), music_dir() — папки пользователя (Windows Known Folders, xdg-user-dirs в Linux)
Переменные RTLTV_CONFIG_DIR и RTLTV_LOCAL_DIR важнее всего (автотесты уводят ими настройки во временную папку).

Библиотеки: встроенные лежат в rtltv/driver — lib_file("librtlsdr") = librtlsdr.dll / librtlsdr.dylib /
librtlsdr.so; на macOS и Linux к имени без префикса добавляется «lib» (mbelib → libmbelib.so).
"""
import ctypes.util
import os
import subprocess
import sys

APP = "SDR4NOOB"

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

LIB_EXT = ".dll" if IS_WIN else ".dylib" if IS_MAC else ".so"

HOME = os.path.expanduser("~")


# ---------------------------------------------------------------- каталоги программы

def _env_dir(name):
    v = os.environ.get(name)
    return os.path.abspath(os.path.expanduser(v)) if v else None


def _xdg(name, default):
    v = os.environ.get(name)
    return v if v and os.path.isabs(v) else os.path.join(HOME, default)


def config_base():
    """Где лежат папки настроек программ (без имени программы) — для переезда со старых названий."""
    if IS_WIN:
        return os.environ.get("APPDATA") or HOME
    if IS_MAC:
        return os.path.join(HOME, "Library", "Application Support")
    return _xdg("XDG_CONFIG_HOME", ".config")


def local_base():
    """Где лежат служебные папки программ (без имени программы)."""
    if IS_WIN:
        return os.environ.get("LOCALAPPDATA") or HOME
    if IS_MAC:
        return os.path.join(HOME, "Library", "Application Support")
    return _xdg("XDG_DATA_HOME", os.path.join(".local", "share"))


def config_dir():
    return _env_dir("RTLTV_CONFIG_DIR") or os.path.join(config_base(), APP)


def local_dir():
    d = _env_dir("RTLTV_LOCAL_DIR")
    if d:
        return d
    if IS_MAC:   # настройки и служебное в одной Application Support/SDR4NOOB — служебное в подпапке
        return os.path.join(local_base(), APP, "Service")
    return os.path.join(local_base(), APP)


# ---------------------------------------------------------------- папки пользователя

def _known_folder(guid_str):
    """Путь известной папки Windows (учитывает перенос «Документов» в OneDrive и т.п.) или None."""
    try:
        import ctypes
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
    return None


def xdg_user_dir(key, config_home=None, home=None):
    """Папка из ~/.config/user-dirs.dirs (то же, что `xdg-user-dir DOCUMENTS`), или None.
    Строки вида XDG_DOCUMENTS_DIR="$HOME/Документы"; папка, равная домашней, значит «не задано»."""
    home = home or HOME
    path = os.path.join(config_home or _xdg("XDG_CONFIG_HOME", ".config"), "user-dirs.dirs")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    want = f"XDG_{key}_DIR"
    for line in lines:
        line = line.strip()
        if not line.startswith(want + "="):
            continue
        v = line[len(want) + 1:].strip()
        if len(v) >= 2 and v[0] == v[-1] == '"':
            v = v[1:-1]
        v = v.replace("\\\"", "\"")
        if v.startswith("$HOME"):
            v = home + v[5:]
        elif v.startswith("${HOME}"):
            v = home + v[7:]
        if not os.path.isabs(v):
            return None
        v = os.path.normpath(v)
        return None if v == os.path.normpath(home) else v
    return None


_WIN_FOLDERS = {"DOCUMENTS": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
                "PICTURES": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
                "MUSIC": "{4BD8D571-6D19-48D3-BE97-422220080E43}"}
_DEFAULT_NAMES = {"DOCUMENTS": "Documents", "PICTURES": "Pictures", "MUSIC": "Music"}


def user_dir(key):
    """key: DOCUMENTS / PICTURES / MUSIC."""
    p = None
    if IS_WIN:
        p = _known_folder(_WIN_FOLDERS[key])
    elif IS_LINUX:
        p = xdg_user_dir(key)
    return p or os.path.join(HOME, _DEFAULT_NAMES[key])


def documents_dir():
    return user_dir("DOCUMENTS")


def pictures_dir():
    return user_dir("PICTURES")


def music_dir():
    return user_dir("MUSIC")


# ---------------------------------------------------------------- библиотеки

def lib_file(stem):
    """Имя файла встроенной библиотеки: librtlsdr → librtlsdr.dll / .dylib / .so; mbelib → mbelib.dll /
    libmbelib.dylib / libmbelib.so."""
    if IS_WIN:
        return stem + LIB_EXT
    return (stem if stem.startswith("lib") else "lib" + stem) + LIB_EXT


def driver_dir():
    """Папка встроенных библиотек (rtltv/driver; в собранной программе — внутри _MEIPASS)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "rtltv", "driver")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "driver")


def bundled_lib(stem):
    return os.path.join(driver_dir(), lib_file(stem))


_SYSTEM_LIB_DIRS = ("/usr/local/lib", "/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu",
                    "/usr/lib64", "/usr/lib", "/opt/homebrew/lib")


def find_system_lib(names):
    """Системная библиотека по базовым именам («rtlsdr», «hackrf»…): путь/имя для ctypes.CDLL или None."""
    for n in names:
        found = ctypes.util.find_library(n)
        if found:
            return found
    if IS_WIN:
        return None
    for d in _SYSTEM_LIB_DIRS:
        for n in names:
            base = "lib" + n if not n.startswith("lib") else n
            for cand in (base + LIB_EXT, base + LIB_EXT + ".0", base + ".0" + LIB_EXT):
                p = os.path.join(d, cand)
                if os.path.isfile(p):
                    return p
    return None


def is_lib_name(name):
    """Похоже ли имя файла на библиотеку этой системы (Linux: libfoo.so и libfoo.so.0.1)."""
    low = name.lower()
    if IS_LINUX:
        return low.endswith(".so") or ".so." in low
    return low.endswith(LIB_EXT)


def lib_filter():
    """Фильтр файлового диалога для драйверов."""
    if IS_WIN:
        return "Библиотеки DLL (*.dll)"
    if IS_MAC:
        return "Библиотеки (*.dylib)"
    return "Библиотеки (*.so *.so.*)"


def driver_dep_hint():
    """Чего обычно не хватает драйверу при загрузке — текст для пользователя."""
    if IS_WIN:
        return "его зависимости (libusb-1.0.dll, pthreadVC2.dll) должны лежать в той же папке"
    if IS_MAC:
        return "у библиотеки нет нужных ей зависимостей (для HackRF/Airspy из Homebrew: brew install libusb)"
    return "нужна библиотека libusb (пакет libusb-1.0-0)"


def usb_busy_hint():
    """Почему приёмник мог не открыться — окончание сообщения об ошибке."""
    if IS_WIN:
        return "Устройство занято другой программой или не привязан WinUSB."
    if IS_MAC:
        return "Устройство занято другой программой."
    return ("Устройство занято другой программой, нет прав доступа к USB или его держит драйвер ТВ-тюнера ядра — "
            "«Режим → Устройства…» → «Настроить доступ…».")


def builtin_driver_label():
    """Как назвать встроенный драйвер RTL-SDR в строке состояния."""
    return "встроенный librtlsdr (rtl-sdr-blog)" + (", WinUSB" if IS_WIN else "")


def use_bundled_portaudio():
    """Linux: если в системе нет libportaudio2, звук (sounddevice) берёт встроенную rtltv/driver/libportaudio.so.2.
    Вызывать до первого import sounddevice — он ищет библиотеку через ctypes.util.find_library("portaudio")."""
    if not IS_LINUX:
        return None
    p = os.path.join(driver_dir(), "libportaudio.so.2")
    if not os.path.isfile(p) or ctypes.util.find_library("portaudio"):
        return None
    orig = ctypes.util.find_library

    def find_library(name):
        return p if name == "portaudio" else orig(name)
    ctypes.util.find_library = find_library
    return p


# ---------------------------------------------------------------- прочее

def child_env():
    """Окружение для сторонних программ (xdg-open, браузер): собранная PyInstaller программа в Linux ставит
    LD_LIBRARY_PATH на свою папку — чужие программы получили бы её libstdc++/glib/Qt. Возвращаем прежний."""
    env = dict(os.environ)
    if IS_LINUX and getattr(sys, "frozen", False):
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
        for k in ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QTWEBENGINEPROCESS_PATH", "QTWEBENGINE_DISABLE_SANDBOX"):
            env.pop(k, None)
    return env


def open_path(path):
    """Открыть папку, файл или адрес (http…) в системном проводнике / программе по умолчанию."""
    if IS_WIN:
        os.startfile(path)  # noqa: S606
        return
    cmd = "open" if IS_MAC else "xdg-open"
    subprocess.Popen([cmd, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,  # noqa: S603
                     start_new_session=True, env=child_env())


def open_url(url):
    """Открыть адрес в браузере. Windows и macOS — модуль webbrowser; Linux — xdg-open с чистым окружением."""
    if IS_LINUX:
        try:
            open_path(url)
            return True
        except OSError:
            pass
    import webbrowser
    return webbrowser.open(url)
