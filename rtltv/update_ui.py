"""Интерфейс автообновления: фоновая проверка при запуске, диалог «Доступна новая версия», загрузка и установка."""
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox, QProgressDialog,
                               QPushButton, QTextBrowser, QVBoxLayout)

from . import __version__, osplat, updater, updater_linux


class _Bridge(QObject):
    done = Signal(object, object)   # (сведения о релизе | None, ошибка | None)


_bridge = None
_busy = False


def _check_async(cb):
    """Проверка в фоновом потоке; cb(info, err) — в потоке интерфейса. False — проверка уже идёт."""
    global _bridge, _busy
    if _busy:
        return False
    _busy = True
    _bridge = _Bridge()

    def fin(info, err):
        global _busy
        _busy = False
        cb(info, err)
    _bridge.done.connect(fin, Qt.QueuedConnection)

    def run(b=_bridge):
        try:
            info = updater.check_latest()
            b.done.emit(info, None)
        except Exception as e:  # noqa: BLE001
            b.done.emit(None, e)
    threading.Thread(target=run, daemon=True, name="update-check").start()
    return True


def maybe_check_on_start(parent):
    """При запуске: если пора по расписанию — тихо проверить; показать окно только при новой версии."""
    if not updater.due():
        return

    def cb(info, err):
        updater.mark_checked()
        if err or not info or not updater.is_newer(info["version"]) or updater.skipped(info["version"]):
            return
        UpdateDialog(info, parent).exec()
    QTimer.singleShot(2500, lambda: _check_async(cb))


def check_now(parent):
    """Ручная проверка (меню «Справка → Проверить обновления…»)."""
    def cb(info, err):
        updater.mark_checked()
        if err:
            QMessageBox.warning(parent, "Обновления", str(err))
        elif not info or not updater.is_newer(info["version"]):
            QMessageBox.information(parent, "Обновления",
                                    f"У вас последняя версия: {__version__}."
                                    + (f"\nНа GitHub: {info['version']}." if info else ""))
        else:
            UpdateDialog(info, parent).exec()
    if not _check_async(cb):
        QMessageBox.information(parent, "Обновления", "Проверка уже идёт — подождите несколько секунд.")


class UpdateDialog(QDialog):
    def __init__(self, info, parent=None):
        super().__init__(parent)
        self.info = info
        self.setWindowTitle("Доступно обновление SDR4NOOB")
        self.resize(620, 480)
        v = QVBoxLayout(self)
        self.kind = updater.update_kind(info)
        a = info.get(self.kind) or {}
        what = {"setup": "установщик", "appimage": "файл AppImage"}.get(self.kind, "архив")
        size = f", {what} {a['size'] / 1e6:.0f} МБ".replace(".", ",") if a.get("size") else ""
        head = QLabel(f"<b>Новая версия {info['version']}</b> (у вас {__version__}){size}.")
        head.setWordWrap(True)
        v.addWidget(head)
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(info.get("notes") or "Описание изменений не указано.")
        v.addWidget(notes, 1)
        manual = (updater_linux.manual_hint(info) if osplat.IS_LINUX else
                  "Нет прав на запись в папку программы — скачайте установщик со страницы релиза и запустите его "
                  "вручную (может понадобиться запуск от имени администратора).")
        hint = QLabel(manual if self.kind == "manual" and (updater.frozen() or osplat.IS_LINUX) else "Нет прав на запись в папку программы — скачайте установщик со страницы релиза и запустите его "
                      "вручную (может понадобиться запуск от имени администратора)." if self.kind == "manual" else
                      "Будут заменены только файлы программы. Настройки, записи, снимки и журналы останутся на месте."
                      if updater.frozen() else
                      "Программа запущена из исходников — обновите их (git pull) или скачайте сборку со страницы релиза.")
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        v.addWidget(hint)
        row = QHBoxLayout()
        b_skip = QPushButton("Пропустить эту версию")
        b_skip.clicked.connect(self._skip)
        b_page = QPushButton("Страница релиза")
        b_page.clicked.connect(lambda: osplat.open_url(info.get("url") or updater.RELEASES_PAGE))
        b_later = QPushButton("Позже")
        b_later.clicked.connect(self.reject)
        row.addWidget(b_skip)
        row.addWidget(b_page)
        row.addStretch(1)
        row.addWidget(b_later)
        if updater.frozen() and self.kind != "manual" and a.get("url"):
            b_go = QPushButton("Обновить сейчас")
            b_go.setDefault(True)
            b_go.clicked.connect(self._update)
            row.addWidget(b_go)
        v.addLayout(row)

    def _skip(self):
        updater.skip(self.info["version"])
        self.reject()

    def _update(self):
        prog = QProgressDialog("Загрузка обновления…", "Отмена", 0, 100, self)
        prog.setWindowTitle("SDR4NOOB — обновление")
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)
        state = {"done": 0, "total": 0, "path": None, "err": None, "cancel": False, "finished": False}

        def run():
            try:
                state["path"] = updater.download(self.info, lambda d, t: state.update(done=d, total=t),
                                                 lambda: state["cancel"], kind=self.kind)
            except Exception as e:  # noqa: BLE001
                state["err"] = e
            state["finished"] = True
        threading.Thread(target=run, daemon=True, name="update-download").start()

        def tick():
            if prog.wasCanceled():
                state["cancel"] = True
            if state["total"]:
                prog.setValue(int(state["done"] * 100 / state["total"]))
                prog.setLabelText(f"Загрузка обновления… {state['done'] / 1e6:.0f} из {state['total'] / 1e6:.0f} МБ")
            if state["finished"]:
                timer.stop()
                prog.close()
                self._install(state)
        timer = QTimer(self)
        timer.timeout.connect(tick)
        timer.start(150)

    def _install(self, state):
        if state["err"]:
            if not state["cancel"]:
                QMessageBox.warning(self, "Обновление", str(state["err"]))
            return
        if self.kind == "appimage":
            QMessageBox.information(self, "Обновление",
                                    "Обновление скачано. SDR4NOOB заменит файл AppImage новой версией, закроется и "
                                    "запустится снова.")
            try:
                updater.install_appimage(state["path"])
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "Обновление", str(e))
                return
            self._quit()
            return
        if self.kind == "setup":
            QMessageBox.information(self, "Обновление",
                                    "Обновление скачано. SDR4NOOB сейчас закроется, установщик обновит программу "
                                    "и запустит её снова.")
            try:
                updater.run_setup(state["path"])
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "Обновление", str(e))
                return
            self._quit()
            return
        try:
            new_dir = updater.prepare(state["path"])
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Обновление", str(e))
            return
        QMessageBox.information(self, "Обновление",
                                "Обновление скачано. SDR4NOOB сейчас закроется, заменит файлы и запустится снова.")
        try:   # установщик стартует только после «OK», и программа сразу закрывается
            updater.install_and_restart(new_dir)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Обновление", str(e))
            return
        self._quit()

    def _quit(self):
        self.accept()
        app = QApplication.instance()
        for w in app.topLevelWidgets():
            w.close()
        app.quit()
