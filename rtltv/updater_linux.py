"""Обновление в Linux.

AppImage (переменная APPIMAGE задаётся рантаймом AppImage): скачать SDR4NOOB-<версия>-<arch>.AppImage из
последнего релиза, сверить SHA-256 и заменить файл на месте (запущенная копия работает со смонтированного
образа, поэтому замена безопасна), затем запустить новую версию. Пакет .deb и запуск из исходников обновляются
вручную — программа только сообщает о новой версии и даёт ссылку на страницу релиза.
"""
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import urllib.request


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


def manual_hint():
    if appimage_path():
        return ("Папка с файлом AppImage недоступна для записи — скачайте новый файл со страницы релиза "
                "и замените им прежний.")
    return ("Программа установлена пакетом .deb (или запущена из исходников): скачайте новый пакет "
            f"sdr4noob_<версия>_{'amd64' if arch() == 'x86_64' else 'arm64'}.deb со страницы релиза и откройте его "
            "(или sudo apt install ./sdr4noob_….deb).")


def download_to(a, dest, ua, progress=None, cancel=None, opener=urllib.request.urlopen):
    """Скачать ассет a ({name, url, size, sha256}) в dest + '.part', сверить SHA-256 и переименовать в dest."""
    if not a.get("sha256"):
        raise RuntimeError("Не удалось получить контрольную сумму файла — без неё обновление не ставится.")
    part = dest + ".part"
    h = hashlib.sha256()
    done = 0
    try:
        req = urllib.request.Request(a["url"], headers={"User-Agent": ua})
        with opener(req, timeout=30) as r, open(part, "wb") as f:  # noqa: S310 — github.com
            total = int(r.headers.get("Content-Length") or a.get("size") or 0)
            while True:
                if cancel and cancel():
                    raise RuntimeError("Загрузка отменена.")
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if h.hexdigest() != a["sha256"].lower():
            raise RuntimeError("Контрольная сумма не совпала — файл повреждён, обновление отменено.")
        os.chmod(part, 0o755)
        os.replace(part, dest)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    return dest


def replace_appimage(new_file, target):
    """Поставить new_file на место target (через временный файл рядом с target — работает и между дисками)."""
    tmp = target + ".new"
    if os.path.abspath(os.path.dirname(new_file)) == os.path.abspath(os.path.dirname(target)):
        os.replace(new_file, tmp)
    else:
        shutil.copy2(new_file, tmp)
        os.remove(new_file)
    os.chmod(tmp, 0o755)
    os.replace(tmp, target)
    return target


def restart(target, args=()):
    """Запустить новую версию отдельным процессом; вызывающий сразу закрывает программу."""
    env = {k: v for k, v in os.environ.items() if k not in ("APPIMAGE", "APPDIR", "ARGV0", "OWD")}
    subprocess.Popen([target, *args], start_new_session=True, close_fds=True, env=env,  # noqa: S603
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True
