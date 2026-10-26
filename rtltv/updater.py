"""Автообновление с GitHub Releases (github.com/Mike-iLeech/SDR4NOOB; до 3.0 — Mike-iLeech/RtlTV).

Проверка: GET api.github.com/repos/<REPO>/releases/latest (без токена — репозиторий публичный),
сравнение версии тега (vX.Y.Z) с __version__. Обновление (только для собранного EXE):
скачать архив SDR4NOOB-<версия>-win64.zip → сверить SHA-256 (поле digest у ассета GitHub или файл .sha256)
→ распаковать во временную папку → после выхода программы PowerShell-скрипт заменяет файлы
программы (_internal и все *.exe из архива; прежняя версия сохраняется как *.old и восстанавливается при
ошибке) и запускает новую версию. Данные и настройки (папка данных, %APPDATA%\\SDR4NOOB) не трогаются.

Если программа поставлена установщиком (рядом лежит unins000.exe), обновление делает новый установщик
SDR4NOOB-<версия>-setup.exe в тихом режиме в ту же папку — так версия в «Приложениях» Windows остаётся верной.
Переносная копия (из zip) обновляется архивом, как описано выше.

До 3.0 программа называлась RtlTV; с 3.1 копии RtlTV.exe в архиве нет, а установщик и обновлятор удаляют
устаревший RtlTV.exe из папки программы. Старый адрес API (repos/Mike-iLeech/RtlTV) GitHub перенаправляет.
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

from . import REPO, __version__, osplat, paths, settings, updater_linux


EXE_NAMES = ("SDR4NOOB.exe", "RtlTV.exe")   # основное имя и прежнее (архивы 2.x/3.0)
STALE_EXES = ("RtlTV.exe",)                  # удаляются из папки программы, если их нет в новой версии
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
UA = f"SDR4NOOB/{__version__} (+https://github.com/{REPO})"
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
    found = {}          # "asset" — архив zip, "setup" — установщик
    sha_urls = {}
    for a in data.get("assets") or []:
        name = a.get("name", "")
        low = name.lower()
        kind = "asset" if low.endswith("-win64.zip") else "setup" if low.endswith("-setup.exe") else \
            "appimage" if low.endswith(updater_linux.asset_suffix()) else None
        if kind:
            digest = a.get("digest") or ""
            found[kind] = {"name": name, "url": a.get("browser_download_url"), "size": a.get("size", 0),
                           "sha256": digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None}
        elif low.endswith(".sha256"):
            sha_urls[name[:-len(".sha256")].lower()] = a.get("browser_download_url")
    for item in found.values():
        url = sha_urls.get(item["name"].lower())
        if not item["sha256"] and url:
            try:
                item["sha256"] = _get(url, timeout).decode("ascii", "ignore").split()[0].lower()
            except Exception:  # noqa: BLE001
                pass
    return {"version": ver, "tag": tag, "notes": data.get("body") or "", "url": data.get("html_url") or RELEASES_PAGE,
            "asset": found.get("asset"), "setup": found.get("setup"), "appimage": found.get("appimage"),
            "published": data.get("published_at") or ""}


def installed():
    """Программа поставлена установщиком (Inno Setup кладёт рядом unins000.exe)."""
    d = app_dir()
    return bool(d) and os.path.isfile(os.path.join(d, "unins000.exe"))


def writable(d):
    """Можно ли писать в папку (программы) — без этого ни архив, ни тихий установщик не обновят файлы."""
    test = os.path.join(d, ".sdr4noob_write_test")
    try:
        with open(test, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test)
        return True
    except OSError:
        return False


def update_kind(info):
    """Чем обновлять: "setup" (поставлено установщиком и он есть в релизе), "asset" (архив zip) или
    "manual" (в папку программы нельзя писать — например, её поставили в Program Files от администратора).
    В Linux: "appimage" (заменить запущенный файл AppImage) или "manual" (пакет .deb, исходники)."""
    if osplat.IS_LINUX:
        return updater_linux.update_kind({"asset": info.get("appimage")})
    d = app_dir()
    if d and not writable(d):
        return "manual"
    return "setup" if installed() and (info.get("setup") or {}).get("url") else "asset"


_RELINK_PS1 = r"""
param([string]$Old, [string]$New)
$sh = New-Object -ComObject WScript.Shell
$dirs = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'),
          [Environment]::GetFolderPath('StartMenu'))
foreach ($d in $dirs) {
  if (-not $d -or -not (Test-Path -LiteralPath $d)) { continue }
  Get-ChildItem -LiteralPath $d -Filter *.lnk -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
    try {
      $l = $sh.CreateShortcut($_.FullName)
      if ($l.TargetPath -and ($l.TargetPath -ieq $Old)) {
        $l.TargetPath = $New
        $l.WorkingDirectory = Split-Path -Parent $New
        $l.IconLocation = $New + ',0'
        $l.Save()
      }
    } catch {}
  }
}
Remove-Item -LiteralPath $Old -Force -ErrorAction SilentlyContinue
"""


def cleanup_legacy_exe():
    """Один раз после обновления с 3.0 обновлятором той версии: рядом мог остаться RtlTV.exe от 3.0 (код 3.0
    вшит в exe), а ярлыки эпохи RtlTV ведут на него и запускали бы старую версию. Перенаправляем такие ярлыки
    (рабочий стол, «Пуск») на SDR4NOOB.exe и удаляем RtlTV.exe. Работает только в собранной версии."""
    d = app_dir()
    if not d or os.path.basename(sys.executable).lower() == "rtltv.exe":
        return False
    old = os.path.join(d, "RtlTV.exe")
    if not os.path.isfile(old):
        return False
    try:
        u = os.path.join(paths.LOCAL, "updates")
        os.makedirs(u, exist_ok=True)
        ps1 = os.path.join(u, "relink_shortcuts.ps1")
        with open(ps1, "w", encoding="utf-8-sig") as f:
            f.write(_RELINK_PS1)
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1,
                          "-Old", old, "-New", sys.executable],
                         creationflags=0x08000000 | 0x00000008, close_fds=True)
        return True
    except OSError:
        return False


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

def download(info, progress=None, cancel=None, kind="asset"):
    """Скачать архив (kind="asset") или установщик (kind="setup") релиза в %LOCALAPPDATA%\\SDR4NOOB\\updates,
    проверить SHA-256. Возвращает путь к файлу."""
    a = info.get(kind)
    if not a or not a.get("url"):
        raise UpdateError("В релизе нет установщика SDR4NOOB-*-setup.exe." if kind == "setup" else
                          f"В релизе нет файла SDR4NOOB-*-{updater_linux.arch()}.AppImage." if kind == "appimage" else
                          "В релизе нет архива для Windows (SDR4NOOB-*-win64.zip).")
    if not a.get("sha256"):
        raise UpdateError("Не удалось получить контрольную сумму файла — без неё обновление не ставится. "
                          "Скачайте его вручную со страницы релиза.")
    d = os.path.join(paths.LOCAL, "updates")
    os.makedirs(d, exist_ok=True)
    for old in os.listdir(d):   # прежние скачанные архивы и установщики больше не нужны
        if old.lower().endswith((".zip", ".zip.part", "-setup.exe", "-setup.exe.part", ".appimage",
                                 ".appimage.part")) and old != a["name"]:
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
    """Распаковать архив во временную папку и найти в нём папку программы (SDR4NOOB.exe или RtlTV.exe + _internal)."""
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
            if any(n in files for n in EXE_NAMES) and os.path.isdir(os.path.join(root, "_internal")):
                return root
        raise UpdateError("В архиве нет SDR4NOOB.exe с папкой _internal.")
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
param([int]$ProcId, [string]$New, [string]$App, [string]$Log, [string]$Start = 'SDR4NOOB.exe',
      [string]$Tmp = '', [switch]$NoStart)
$ErrorActionPreference = 'Stop'
function L($m) { try { Add-Content -LiteralPath $Log -Value ((Get-Date -Format s) + ' ' + $m) -Encoding UTF8 } catch {} }
$int = Join-Path $App '_internal'; $intOld = Join-Path $App '_internal.old'
$exes = @()
$movedInt = $false; $moved = New-Object System.Collections.ArrayList
function Rollback {
  L "откат к прежней версии"
  if ($script:movedInt) {
    if (Test-Path -LiteralPath $int) { Remove-Item -LiteralPath $int -Recurse -Force -ErrorAction Continue }
    Rename-Item -LiteralPath $intOld -NewName '_internal' -ErrorAction Continue
  }
  foreach ($n in $script:moved) {
    $e = Join-Path $App $n
    if (Test-Path -LiteralPath $e) { Remove-Item -LiteralPath $e -Force -ErrorAction Continue }
    Rename-Item -LiteralPath ($e + '.old') -NewName $n -ErrorAction Continue
  }
}
$ok = $false
try {
  L "ожидание выхода программы (PID $ProcId)"
  $p = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
  if ($p -and -not $p.WaitForExit(120000)) {
    L "программа не закрылась за 2 минуты — файлы не тронуты, обновление отложено"
    exit 2
  }
  Start-Sleep -Milliseconds 700
  $exes = @(Get-ChildItem -LiteralPath $New -File -Filter '*.exe' | ForEach-Object { $_.Name })
  if ($exes.Count -eq 0) { throw "в обновлении нет exe" }
  if (Test-Path -LiteralPath $intOld) { Remove-Item -LiteralPath $intOld -Recurse -Force }
  foreach ($n in $exes) {
    $o = Join-Path $App ($n + '.old')
    if (Test-Path -LiteralPath $o) { Remove-Item -LiteralPath $o -Force }
  }
  Rename-Item -LiteralPath $int -NewName '_internal.old'; $movedInt = $true
  foreach ($n in $exes) {
    $e = Join-Path $App $n
    if (Test-Path -LiteralPath $e) { Rename-Item -LiteralPath $e -NewName ($n + '.old'); [void]$moved.Add($n) }
  }
  Copy-Item -LiteralPath (Join-Path $New '_internal') -Destination $int -Recurse -Force
  Get-ChildItem -LiteralPath $New -File | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $App -Force }
  Remove-Item -LiteralPath $intOld -Recurse -Force -ErrorAction SilentlyContinue
  foreach ($n in $moved) { Remove-Item -LiteralPath (Join-Path $App ($n + '.old')) -Force -ErrorAction SilentlyContinue }
  foreach ($n in @('RtlTV.exe')) {   # прежнее имя программы (до 3.0) — если в новой версии его нет, убрать
    if ($exes -notcontains $n) {
      Remove-Item -LiteralPath (Join-Path $App $n) -Force -ErrorAction SilentlyContinue
      Remove-Item -LiteralPath (Join-Path $App ($n + '.old')) -Force -ErrorAction SilentlyContinue
    }
  }
  L ("обновление установлено (" + ($exes -join ', ') + "), запуск $Start")
  $ok = $true
} catch {
  L ("ошибка: " + $_)
  Rollback
}
if ($Tmp -and (Test-Path -LiteralPath $Tmp)) { Remove-Item -LiteralPath $Tmp -Recurse -Force -ErrorAction SilentlyContinue }
$run = Join-Path $App $Start
if (-not (Test-Path -LiteralPath $run)) { $run = Join-Path $App 'RtlTV.exe' }
if (-not $NoStart -and (Test-Path -LiteralPath $run)) { Start-Process -FilePath $run -WorkingDirectory $App }
if (-not $ok) { exit 1 }
"""


def run_setup(setup_path):
    """Запустить скачанный установщик тихо в папку текущей программы; он сам закроет её и запустит новую
    версию (/LAUNCH=1). Вызывающий должен сразу закрыть программу."""
    if not frozen():
        raise UpdateError("Обновление установщиком работает только в собранной версии (SDR4NOOB.exe).")
    flags = 0x00000008   # DETACHED_PROCESS
    subprocess.Popen([setup_path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
                      "/DIR=" + app_dir(), "/LAUNCH=1"], creationflags=flags, close_fds=True)
    return True


def start_name(new_dir):
    """Какой exe запустить после обновления: SDR4NOOB.exe, если он есть в новой версии, иначе текущий."""
    for n in EXE_NAMES:
        if os.path.isfile(os.path.join(new_dir, n)):
            return n
    return os.path.basename(sys.executable)


def install_appimage(path):
    """Linux: поставить скачанный AppImage на место запущенного и запустить его. Вызывающий сразу закрывает программу."""
    target = updater_linux.appimage_path()
    if not target:
        raise UpdateError("Программа запущена не из AppImage — обновите её вручную со страницы релиза.")
    try:
        updater_linux.replace_appimage(path, target)
    except OSError as e:
        raise UpdateError(f"Не удалось заменить {target}: {e}") from e
    return updater_linux.restart(target)


def install_and_restart(new_dir):
    """Запустить установщик и вернуть True. Вызывающий должен сразу закрыть программу:
    установщик ждёт её выхода до 2 минут, а если не дождётся — файлы не трогает."""
    if not frozen():
        raise UpdateError("Обновление файлов работает только в собранной версии (SDR4NOOB.exe).")
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
                      "-ProcId", str(os.getpid()), "-New", new_dir, "-App", app, "-Log", log, "-Tmp", temp_root(new_dir), "-Start", start_name(new_dir)],
                     creationflags=flags, close_fds=True)
    return True
