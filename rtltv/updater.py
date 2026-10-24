"""Автообновление с GitHub Releases (github.com/Mike-iLeech/RtlTV).

Проверка: GET api.github.com/repos/<REPO>/releases/latest (без токена — репозиторий публичный),
сравнение версии тега (vX.Y.Z) с __version__. Обновление (только для собранного EXE):
скачать архив RtlTV-<версия>-win64.zip → сверить SHA-256 (поле digest у ассета GitHub или файл .sha256)
→ распаковать во временную папку → после выхода программы PowerShell-скрипт заменяет файлы
программы (RtlTV.exe и _internal; прежняя версия сохраняется как *.old и восстанавливается при ошибке)
и запускает новую версию. Данные и настройки (папка данных, %APPDATA%\\RtlTV) не трогаются.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

from . import __version__, paths, settings

REPO = "Mike-iLeech/RtlTV"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
UA = f"RtlTV/{__version__} (+https://github.com/{REPO})"
INTERVALS = {"daily": 24 * 3600, "weekly": 7 * 24 * 3600, "never": None}


class UpdateError(RuntimeError):
    pass


def parse_version(s):
    """'v2.0.1' / '2.0.1-beta' → (2, 0, 1). Нечисловые хвосты отбрасываются."""
    nums = re.findall(r"\d+", str(s).split("-")[0])
    return tuple(int(x) for x in nums[:3]) + (0,) * (3 - len(nums[:3]))


def is_newer(remote, local=__version__):
    return parse_version(remote) > parse_version(local)


def frozen():
    return bool(getattr(sys, "frozen", False))


def app_dir():
    return os.path.dirname(os.path.abspath(sys.executable)) if frozen() else None


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — только https api.github.com/github.com
        return r.read()


def check_latest(timeout=15):
    """Сведения о последнем релизе: {version, tag, notes, url, asset: {name, url, size, sha256}} или None."""
    try:
        data = json.loads(_get(API_LATEST, timeout).decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"Не удалось проверить обновления: {e}") from e
    tag = data.get("tag_name") or ""
    ver = ".".join(str(x) for x in parse_version(tag))
    asset = None
    sha_url = None
    for a in data.get("assets") or []:
        name = a.get("name", "")
        if name.lower().endswith("-win64.zip"):
            digest = a.get("digest") or ""
            asset = {"name": name, "url": a.get("browser_download_url"), "size": a.get("size", 0),
                     "sha256": digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None}
        elif name.lower().endswith(".zip.sha256"):
            sha_url = a.get("browser_download_url")
    if asset and not asset["sha256"] and sha_url:
        try:
            asset["sha256"] = _get(sha_url, timeout).decode("ascii", "ignore").split()[0].lower()
        except Exception:  # noqa: BLE001
            pass
    return {"version": ver, "tag": tag, "notes": data.get("body") or "", "url": data.get("html_url") or RELEASES_PAGE,
            "asset": asset, "published": data.get("published_at") or ""}


# ---------------------------------------------------------------- расписание проверок

def _app():
    return settings.load("app", {})


def check_interval():
    return _app().get("update_check", "daily")


def due(now=None):
    """Пора ли проверять (по настройке «daily/weekly/never» и времени прошлой проверки)."""
    s = _app()
    step = INTERVALS.get(s.get("update_check", "daily"), INTERVALS["daily"])
    if step is None:
        return False
    now = time.time() if now is None else now
    return now - float(s.get("update_last", 0)) >= step


def mark_checked(now=None):
    s = _app()
    s["update_last"] = time.time() if now is None else now
    settings.save("app", s)


def skipped(version):
    return _app().get("update_skip") == version


def skip(version):
    s = _app()
    s["update_skip"] = version
    settings.save("app", s)


# ---------------------------------------------------------------- загрузка и установка

def download(info, progress=None, cancel=None):
    """Скачать архив релиза в %LOCALAPPDATA%\\RtlTV\\updates, проверить SHA-256. Возвращает путь к zip."""
    a = info.get("asset")
    if not a or not a.get("url"):
        raise UpdateError("В релизе нет архива для Windows (RtlTV-*-win64.zip).")
    if not a.get("sha256"):
        raise UpdateError("Не удалось получить контрольную сумму архива — без неё обновление не ставится. "
                          "Скачайте архив вручную со страницы релиза.")
    d = os.path.join(paths.LOCAL, "updates")
    os.makedirs(d, exist_ok=True)
    for old in os.listdir(d):   # прежние скачанные архивы больше не нужны
        if old.lower().endswith((".zip", ".zip.part")) and old != a["name"]:
            _rm(os.path.join(d, old))
    path = os.path.join(d, a["name"])
    req = urllib.request.Request(a["url"], headers={"User-Agent": UA})
    h = hashlib.sha256()
    done = 0
    try:
        with urllib.request.urlopen(req, timeout=30) as r, open(path + ".part", "wb") as f:  # noqa: S310
            total = int(r.headers.get("Content-Length") or a.get("size") or 0)
            while True:
                if cancel and cancel():
                    raise UpdateError("Загрузка отменена.")
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except UpdateError:
        _rm(path + ".part")
        raise
    except Exception as e:  # noqa: BLE001
        _rm(path + ".part")
        raise UpdateError(f"Не удалось скачать обновление: {e}") from e
    got = h.hexdigest()
    if got != a["sha256"]:
        _rm(path + ".part")
        raise UpdateError("Контрольная сумма архива не совпала — файл повреждён, обновление отменено.")
    os.replace(path + ".part", path)
    return path


def _rm(p):
    try:
        os.remove(p)
    except OSError:
        pass


def prepare(zip_path):
    """Распаковать архив во временную папку и найти в нём папку с RtlTV.exe."""
    if not zipfile.is_zipfile(zip_path):
        raise UpdateError("Скачанный файл — не архив zip.")
    dest = tempfile.mkdtemp(prefix="rtltv-update-")
    try:
        with zipfile.ZipFile(zip_path) as z:
            for m in z.namelist():   # защита от путей вида ../../
                p = os.path.normpath(os.path.join(dest, m))
                if not p.startswith(os.path.normpath(dest) + os.sep) and p != os.path.normpath(dest):
                    raise UpdateError("В архиве недопустимые пути — обновление отменено.")
            z.extractall(dest)
        for root, _dirs, files in os.walk(dest):
            if "RtlTV.exe" in files and os.path.isdir(os.path.join(root, "_internal")):
                return root
        raise UpdateError("В архиве нет RtlTV.exe с папкой _internal.")
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def temp_root(new_dir):
    """Корневая папка распаковки (rtltv-update-*) — установщик удалит её после себя."""
    d = os.path.normpath(new_dir)
    tmp = os.path.normcase(os.path.normpath(tempfile.gettempdir()))
    while os.path.dirname(d) != d and os.path.normcase(os.path.dirname(d)) != tmp:
        d = os.path.dirname(d)
    return d if os.path.basename(d).startswith("rtltv-update-") else ""


INSTALL_PS1 = r"""
param([int]$ProcId, [string]$New, [string]$App, [string]$Log, [string]$Tmp = '', [switch]$NoStart)
$ErrorActionPreference = 'Stop'
function L($m) { try { Add-Content -LiteralPath $Log -Value ((Get-Date -Format s) + ' ' + $m) -Encoding UTF8 } catch {} }
$int = Join-Path $App '_internal'; $intOld = Join-Path $App '_internal.old'
$exe = Join-Path $App 'RtlTV.exe'; $exeOld = Join-Path $App 'RtlTV.exe.old'
$movedInt = $false; $movedExe = $false
function Rollback {
  L "откат к прежней версии"
  if ($script:movedInt) {
    if (Test-Path -LiteralPath $int) { Remove-Item -LiteralPath $int -Recurse -Force -ErrorAction Continue }
    Rename-Item -LiteralPath $intOld -NewName '_internal' -ErrorAction Continue
  }
  if ($script:movedExe) {
    if (Test-Path -LiteralPath $exe) { Remove-Item -LiteralPath $exe -Force -ErrorAction Continue }
    Rename-Item -LiteralPath $exeOld -NewName 'RtlTV.exe' -ErrorAction Continue
  }
}
$ok = $false
try {
  L "ожидание выхода RtlTV (PID $ProcId)"
  $p = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
  if ($p -and -not $p.WaitForExit(120000)) {
    L "RtlTV не закрылась за 2 минуты — файлы не тронуты, обновление отложено"
    exit 2
  }
  Start-Sleep -Milliseconds 700
  if (Test-Path -LiteralPath $intOld) { Remove-Item -LiteralPath $intOld -Recurse -Force }
  if (Test-Path -LiteralPath $exeOld) { Remove-Item -LiteralPath $exeOld -Force }
  Rename-Item -LiteralPath $int -NewName '_internal.old'; $movedInt = $true
  Rename-Item -LiteralPath $exe -NewName 'RtlTV.exe.old'; $movedExe = $true
  Copy-Item -LiteralPath (Join-Path $New '_internal') -Destination $int -Recurse -Force
  Copy-Item -LiteralPath (Join-Path $New 'RtlTV.exe') -Destination $exe -Force
  Get-ChildItem -LiteralPath $New -File | Where-Object { $_.Name -ne 'RtlTV.exe' } |
    ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $App -Force }
  Remove-Item -LiteralPath $intOld -Recurse -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $exeOld -Force -ErrorAction SilentlyContinue
  L "обновление установлено, запуск"
  $ok = $true
} catch {
  L ("ошибка: " + $_)
  Rollback
}
if ($Tmp -and (Test-Path -LiteralPath $Tmp)) { Remove-Item -LiteralPath $Tmp -Recurse -Force -ErrorAction SilentlyContinue }
if (-not $NoStart -and (Test-Path -LiteralPath $exe)) { Start-Process -FilePath $exe -WorkingDirectory $App }
if (-not $ok) { exit 1 }
"""


def install_and_restart(new_dir):
    """Запустить установщик и вернуть True. Вызывающий должен сразу закрыть программу:
    установщик ждёт её выхода до 2 минут, а если не дождётся — файлы не трогает."""
    if not frozen():
        raise UpdateError("Обновление файлов работает только в собранной версии (RtlTV.exe).")
    app = app_dir()
    test = os.path.join(app, ".rtltv_write_test")
    try:
        with open(test, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test)
    except OSError as e:
        raise UpdateError(f"Нет прав на запись в папку программы ({app}): {e}. "
                          "Запустите обновление от имени администратора или скачайте архив вручную.") from e
    d = os.path.join(paths.LOCAL, "updates")
    os.makedirs(d, exist_ok=True)
    ps1 = os.path.join(d, "install_update.ps1")
    with open(ps1, "w", encoding="utf-8-sig") as f:
        f.write(INSTALL_PS1)
    log = os.path.join(d, "install_update.log")
    flags = 0x08000000 | 0x00000008   # CREATE_NO_WINDOW | DETACHED_PROCESS
    subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1,
                      "-ProcId", str(os.getpid()), "-New", new_dir, "-App", app, "-Log", log, "-Tmp", temp_root(new_dir)],
                     creationflags=flags, close_fds=True)
    return True
