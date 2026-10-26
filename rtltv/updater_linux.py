"""Обновление в Linux.

AppImage (переменная APPIMAGE задаётся рантаймом AppImage): скачать SDR4NOOB-<версия>-<arch>.AppImage из
последнего релиза, сверить SHA-256 (updater.download) и заменить файл на месте — имя файла остаётся прежним (запущенная копия
работает со смонтированного образа, поэтому замена безопасна), затем запустить новую версию. Пакет .deb и запуск из исходников обновляются
вручную — программа только сообщает о новой версии и даёт ссылку на страницу релиза.
"""
import os
import platform
import shutil
import subprocess
import sys


def arch():
    m = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(m, m)


def asset_suffix():
    """Окончание имени файла релиза для этой машины (без учёта регистра)."""
    return f"-{arch()}.appimage"


def appimage_path():
    """Путь к запущенному файлу AppImage или None (пакет .deb, исходники)."""
    p = os.environ.get("APPIMAGE")
    if p and getattr(sys, "frozen", False) and os.path.isfile(p):
        return os.path.abspath(p)
    return None


def can_replace(path):
    """Можно ли заменить AppImage: нужна запись в его папку (временный файл рядом и переименование)."""
    return os.access(os.path.dirname(path) or ".", os.W_OK)


def update_kind(info):
    """"appimage" — заменить файл самим; "manual" — показать ссылку на релиз."""
    p = appimage_path()
    a = (info or {}).get("asset") or {}
    if p and can_replace(p) and a.get("url"):
        return "appimage"
    return "manual"


def manual_hint(info=None):
    p = appimage_path()
    if p and can_replace(p) and not ((info or {}).get("appimage") or {}).get("url"):
        return ("В этом выпуске пока нет файла AppImage для вашей системы — проверьте обновления позже "
                "или скачайте его со страницы релиза.")
    if p:
        return ("Папка с файлом AppImage недоступна для записи — скачайте новый файл со страницы релиза "
                "и замените им прежний.")
    return ("Программа установлена пакетом .deb (или запущена из исходников): скачайте новый пакет "
            f"sdr4noob_<версия>_{'amd64' if arch() == 'x86_64' else 'arm64'}.deb со страницы релиза и откройте его "
            "(или sudo apt install ./sdr4noob_….deb).")


def replace_appimage(new_file, target):
    """Поставить new_file на место target (через временный файл рядом с target — работает и между дисками)."""
    tmp = target + ".new"
    try:
        shutil.copy2(new_file, tmp)   # копия, а не перенос: при ошибке скачанный файл остаётся
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
    except OSError:
        try:
            os.remove(tmp)   # не оставлять 200 МБ недописанного файла рядом с программой
        except OSError:
            pass
        raise
    try:
        os.remove(new_file)
    except OSError:
        pass
    return target


def restart(target, args=()):
    """Запустить новую версию отдельным процессом; вызывающий сразу закрывает программу."""
    from . import osplat
    env = {k: v for k, v in osplat.child_env().items() if k not in ("APPIMAGE", "APPDIR", "ARGV0", "OWD")}
    subprocess.Popen([target, *args], start_new_session=True, close_fds=True, env=env,  # noqa: S603
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True
