"""Обновление собранной версии для macOS (SDR4NOOB.app). Вызывается из updater.py, когда osplat.IS_MAC.

Релиз содержит SDR4NOOB-<версия>-macos-<arm64|x86_64>.zip — архив с SDR4NOOB.app, упакованный
`ditto -c -k --keepParent` (сохраняет символические ссылки фреймворков Qt, права и подпись), — и такой же .dmg
для первой установки. Обновлятор качает zip (SHA-256 сверяет updater.download), распаковывает его `ditto -x -k`
(zipfile из стандартной библиотеки теряет ссылки и биты запуска — пакет после него не запустится), а после
выхода программы bash-скрипт заменяет пакет целиком: прежний SDR4NOOB.app переименовывается в
SDR4NOOB.app.old и возвращается на место при ошибке, затем новая версия запускается через `open`.
Настройки и данные (~/Library/Application Support/SDR4NOOB, ~/Documents/SDR4NOOB) не трогаются.
"""
import os
import platform
import shutil
import subprocess
import sys
import tempfile

APP_NAME = "SDR4NOOB.app"


class MacUpdateError(RuntimeError):
    pass


def _rosetta():
    """Программа x86_64 запущена на Apple Silicon через Rosetta."""
    try:
        r = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "1"
    except (OSError, subprocess.SubprocessError):
        return False


def arch():
    """arm64 (Apple Silicon) или x86_64 (Intel). Intel-сборка под Rosetta обновляется до arm64 — родной для Mac."""
    m = platform.machine().lower()
    return "arm64" if m in ("arm64", "aarch64") or _rosetta() else "x86_64"


def asset_suffix():
    return f"-macos-{arch()}.zip"


def bundle_path(executable=None):
    """Путь к SDR4NOOB.app, из которого запущена программа (…/X.app/Contents/MacOS/SDR4NOOB), иначе None."""
    exe = os.path.abspath(executable or sys.executable)
    macos = os.path.dirname(exe)
    contents = os.path.dirname(macos)
    app = os.path.dirname(contents)
    if os.path.basename(macos) == "MacOS" and os.path.basename(contents) == "Contents" and app.endswith(".app"):
        return app
    return None


def prepare(zip_path):
    """Распаковать архив во временную папку и вернуть путь к SDR4NOOB.app в нём."""
    dest = tempfile.mkdtemp(prefix="rtltv-update-")
    try:
        r = subprocess.run(["ditto", "-x", "-k", zip_path, dest], capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise MacUpdateError(f"Архив обновления не распаковался: {r.stderr.strip() or r.returncode}")
        for root, dirs, _files in os.walk(dest):
            for d in dirs:
                if d.endswith(".app") and os.path.isfile(os.path.join(root, d, "Contents", "MacOS", "SDR4NOOB")):
                    return os.path.join(root, d)
            if root.endswith(".app"):
                dirs[:] = []   # внутрь пакетов не спускаемся
        raise MacUpdateError(f"В архиве нет {APP_NAME}.")
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise


INSTALL_SH = r"""#!/bin/bash
# Замена SDR4NOOB.app после выхода программы. Аргументы: PID новый.app текущий.app журнал [временная_папка]
PID="$1"; NEW="$2"; APP="$3"; LOG="$4"; TMP="${5:-}"
ST="${LOG%.log}.status"   # итог для программы: ok / fail <причина> — покажется при следующем запуске
L() { echo "$(date '+%Y-%m-%dT%H:%M:%S') $*" >> "$LOG" 2>/dev/null; }
F() { L "$*"; echo "fail $*" > "$ST"; }
rm -f "$ST"
L "ожидание выхода программы (PID $PID)"
for _ in $(seq 1 240); do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done
if kill -0 "$PID" 2>/dev/null; then
  F "программа не закрылась за 2 минуты — файлы не тронуты, обновление отложено"; exit 2
fi
sleep 0.7
OK=0
rm -rf "$APP.old"
if mv "$APP" "$APP.old"; then
  if ditto "$NEW" "$APP"; then
    xattr -dr com.apple.quarantine "$APP" 2>/dev/null
    rm -rf "$APP.old" 2>/dev/null || L "прежняя версия $APP.old не удалилась — её можно удалить вручную"
    L "обновление установлено"; echo ok > "$ST"; OK=1
  else
    F "не удалось записать новую версию в $APP — откат к прежней версии"
    rm -rf "$APP"; mv "$APP.old" "$APP"
  fi
else
  F "нет доступа к $APP (macOS не разрешила менять программу) — файлы не тронуты"
fi
[ -n "$TMP" ] && rm -rf "$TMP"
[ -z "$SDR4NOOB_NO_START" ] && [ -d "$APP" ] && open "$APP"
[ "$OK" = 1 ] || exit 1
"""


def temp_root(new_app):
    """Корневая папка распаковки (rtltv-update-*), которую скрипт удалит после себя."""
    d = os.path.normpath(new_app)
    while os.path.dirname(d) != d and not os.path.basename(d).startswith("rtltv-update-"):
        d = os.path.dirname(d)
    return d if os.path.basename(d).startswith("rtltv-update-") else ""


def last_install_failure(updates_dir):
    """Причина, по которой не удалось прошлое обновление (один раз; затем отметка удаляется), иначе None."""
    st = os.path.join(updates_dir, "install_update.status")
    try:
        with open(st, encoding="utf-8") as f:
            text = f.read().strip()
        os.remove(st)
    except OSError:
        return None
    return text[5:].strip() or "неизвестная ошибка" if text.startswith("fail") else None


def install_and_restart(new_app, updates_dir, executable=None):
    """Запустить скрипт замены и вернуть True. Вызывающий должен сразу закрыть программу."""
    app = bundle_path(executable)
    if not app:
        raise MacUpdateError("Обновление работает только в собранной версии (SDR4NOOB.app).")
    parent = os.path.dirname(app)
    if not os.access(parent, os.W_OK):
        raise MacUpdateError(f"Нет прав на запись в папку {parent}. Скачайте образ .dmg со страницы релиза "
                             f"и перетащите SDR4NOOB в «Программы» вручную.")
    os.makedirs(updates_dir, exist_ok=True)
    sh = os.path.join(updates_dir, "install_update.sh")
    with open(sh, "w", encoding="utf-8", newline="\n") as f:
        f.write(INSTALL_SH)
    log = os.path.join(updates_dir, "install_update.log")
    subprocess.Popen(["/bin/bash", sh, str(os.getpid()), new_app, app, log, temp_root(new_app)],
                     start_new_session=True, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True
