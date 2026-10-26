"""Ужать Qt WebEngine в собранном dist\\SDR4NOOB (встроенный браузер нужен только схеме в справке).

PyInstaller вместе с QtWebEngineWidgets забирает весь QML (папка qml\\ и десятки DLL Qt Quick 3D, Quick
Controls, Graphs…) и отладочные ресурсы Chromium — это сотни мегабайт, которые SDR4NOOB не использует:
  * языки Chromium (qtwebengine_locales\\*.pak) — оставить ru и en-US (без своего языка берётся en-US);
  * *.debug.pak и qtwebengine_devtools_resources.pak — отладка и инструменты разработчика;
  * PySide6\\qml и plugins\\qmltooling — QML не используется;
  * Qt6*.dll, которые после этого не импортирует ни один оставшийся модуль (импорты из PE-заголовков, по кругу);
    в Linux так же — libQt6*.so.6 по записям NEEDED заголовков ELF (readelf из binutils).
Проверка результата — SDR4NOOB.exe --selftest-guide (схема грузится) и обычный запуск.

    py -3.12 tools/prune_webengine.py [dist\\SDR4NOOB]
"""
import os
import shutil
import sys

KEEP_LOCALES = {"ru.pak", "en-US.pak"}
DROP_FILES = ("qtwebengine_devtools_resources.pak",)
DROP_DIRS = (("PySide6", "qml"), ("PySide6", "plugins", "qmltooling"),
             ("PySide6", "Qt", "qml"), ("PySide6", "Qt", "plugins", "qmltooling"))   # раскладка Linux
PE_EXT = (".exe", ".dll", ".pyd")


def _size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    return sum(os.path.getsize(os.path.join(dp, f)) for dp, _dn, fn in os.walk(path) for f in fn)


def _imports(path):
    """Имена DLL из таблицы импорта (обычной и отложенной), в нижнем регистре."""
    import pefile
    try:
        pe = pefile.PE(path, fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                                               pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
    except (pefile.PEFormatError, OSError):
        return set()
    out = set()
    for attr in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
        for entry in getattr(pe, attr, []):
            out.add(entry.dll.decode("ascii", "replace").lower())
    pe.close()
    return out


def _is_elf(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def _elf_needed(path):
    """Имена из NEEDED (readelf -d) — библиотеки, которые ELF-файл подгружает при загрузке."""
    import subprocess
    try:
        out = subprocess.run(["readelf", "-d", path], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return {line.split("[", 1)[1].split("]", 1)[0] for line in out.splitlines() if "(NEEDED)" in line and "[" in line}


def _prune_elf(base, drop, log):
    """Linux: libQt6*.so*, которые не нужны ни одному оставшемуся ELF (по кругу). Возвращает имена убранных."""
    elfs = [os.path.join(dp, f) for dp, _dn, fn in os.walk(base) for f in fn
            if not os.path.islink(os.path.join(dp, f)) and _is_elf(os.path.join(dp, f))]
    needed = {}
    for p in elfs:
        n = _elf_needed(p)
        if n is None:
            log("readelf не найден — лишние библиотеки Qt не убираются")
            return []
        needed[p] = n
    removed = []
    while True:
        used = set().union(*needed.values()) if needed else set()
        extra = [p for p in needed if os.path.basename(p).startswith("libQt6") and ".so" in os.path.basename(p)
                 and os.path.basename(p) not in used]
        if not extra:
            break
        for p in extra:
            drop(p)
            needed.pop(p)
            removed.append(os.path.basename(p))
    return removed


def prune(dist, log=print):
    """Ужать dist. -> освобождено байт."""
    internal = os.path.join(dist, "_internal")
    base = internal if os.path.isdir(internal) else dist
    freed = 0

    def drop(path):
        nonlocal freed
        freed += _size(path)
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)

    for dp, _dn, fn in os.walk(base):
        for name in fn:
            if (os.path.basename(dp) == "qtwebengine_locales" and name.endswith(".pak") and name not in KEEP_LOCALES) \
                    or name.endswith(".debug.pak") or name in DROP_FILES:
                drop(os.path.join(dp, name))
    for parts in DROP_DIRS:
        p = os.path.join(base, *parts)
        if os.path.isdir(p):
            drop(p)
    # Qt6*.dll, на которые больше никто не ссылается; удаление одной может освободить следующие
    qtdir = os.path.join(base, "PySide6")
    pe_files = [os.path.join(dp, f) for dp, _dn, fn in os.walk(dist) for f in fn if f.lower().endswith(PE_EXT)]
    imports = {p: _imports(p) for p in pe_files}
    removed = []
    while True:
        used = set().union(*imports.values()) if imports else set()
        extra = [p for p in imports if os.path.dirname(p) == qtdir and os.path.basename(p).lower().startswith("qt6")
                 and p.lower().endswith(".dll") and os.path.basename(p).lower() not in used]
        if not extra:
            break
        for p in extra:
            drop(p)
            imports.pop(p)
            removed.append(os.path.basename(p))
    if sys.platform.startswith("linux"):
        removed += _prune_elf(base, drop, log)
    log(f"Qt WebEngine: убрано лишних библиотек Qt {len(removed)}, освобождено {freed / 1e6:.0f} МБ")
    return freed


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                              "dist", "SDR4NOOB")
    prune(root)
