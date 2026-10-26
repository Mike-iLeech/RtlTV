"""Окно «Настройки»: где хранить записи, снимки, звук, журналы и служебные файлы."""
import os
import subprocess
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressDialog, QPushButton,
                               QVBoxLayout)

from . import osplat, paths, settings


def _fmt_size(b):
    for unit, k in (("ГБ", 1e9), ("МБ", 1e6), ("КБ", 1e3)):
        if b >= k:
            return f"{b / k:.1f} {unit}".replace(".", ",")
    return f"{b} Б"


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SDR4NOOB — настройки")
        self.resize(820, 700)
        self.setMinimumSize(640, 560)
        self.app_s = settings.load("app", {})
        v = QVBoxLayout(self)

        g = QGroupBox("Папка данных")
        gl = QVBoxLayout(g)
        hint = QLabel("Сюда SDR4NOOB складывает всё, что вы получаете: записи пролётов, снимки, записи звука, журналы. "
                      "Можно выбрать любой диск, например большой второй диск для записей IQ (они весят до 2 ГБ за пролёт).")
        hint.setWordWrap(True)
        hint.setObjectName("dim")
        gl.addWidget(hint)
        row = QHBoxLayout()
        self.path = QLineEdit(paths.data_root())
        self.path.setToolTip("Корневая папка данных SDR4NOOB")
        b_pick = QPushButton("Выбрать…")
        b_pick.clicked.connect(self._pick)
        b_open = QPushButton("Открыть")
        b_open.clicked.connect(lambda: self._open(self.path.text()))
        b_def = QPushButton("По умолчанию")
        b_def.setToolTip(paths.DEFAULT_DATA)
        b_def.clicked.connect(lambda: self.path.setText(paths.DEFAULT_DATA))
        row.addWidget(self.path, 1)
        row.addWidget(b_pick)
        row.addWidget(b_open)
        row.addWidget(b_def)
        gl.addLayout(row)
        self.cb_service = QCheckBox("Служебные файлы тоже здесь (кэш карт, орбиты, история самолётов, справочник "
                                    "бортов, драйверы, журнал программы)")
        self.cb_service.setChecked(paths.service_in_data())
        self.cb_service.setToolTip(f"Если выключено — служебные файлы в {paths.LOCAL}")
        gl.addWidget(self.cb_service)
        v.addWidget(g)

        g2 = QGroupBox("Что где будет лежать")
        g2l = QVBoxLayout(g2)
        self.layout_lbl = QLabel()
        self.layout_lbl.setWordWrap(True)
        self.layout_lbl.setTextFormat(Qt.RichText)
        g2l.addWidget(self.layout_lbl)
        settings_note = QLabel(f"Настройки программы всегда хранятся в {settings.DIR} — по ним SDR4NOOB находит папку данных.")
        settings_note.setObjectName("dim")
        settings_note.setWordWrap(True)
        g2l.addWidget(settings_note)
        v.addWidget(g2)

        g3 = QGroupBox("Файлы в прежних местах")
        g3l = QVBoxLayout(g3)
        self.legacy_lbl = QLabel()
        self.legacy_lbl.setWordWrap(True)
        self.legacy_lbl.setTextFormat(Qt.RichText)
        g3l.addWidget(self.legacy_lbl)
        self.btn_move = QPushButton("Перенести их в папку данных…")
        self.btn_move.clicked.connect(self._move)
        g3l.addWidget(self.btn_move)
        v.addWidget(g3)

        g4 = QGroupBox("Обновления")
        g4l = QHBoxLayout(g4)
        g4l.addWidget(QLabel("Проверять новую версию на GitHub:"))
        self.upd = QComboBox()
        for key, name in (("daily", "раз в день"), ("weekly", "раз в неделю"), ("never", "никогда")):
            self.upd.addItem(name, key)
        self.upd.setCurrentIndex(max(0, self.upd.findData(self.app_s.get("update_check", "daily"))))
        g4l.addWidget(self.upd)
        b_now = QPushButton("Проверить сейчас")
        b_now.clicked.connect(lambda: __import__("rtltv.update_ui", fromlist=["x"]).check_now(self))
        g4l.addWidget(b_now)
        g4l.addStretch(1)
        v.addWidget(g4)
        v.addStretch(1)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("Сохранить")
        bb.button(QDialogButtonBox.Cancel).setText("Отмена")
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        self.path.textChanged.connect(self._refresh)
        self.cb_service.toggled.connect(self._refresh)
        self._refresh()

    # ---------------------------------------------------------------- вид
    def _root(self):
        return os.path.abspath(os.path.expandvars(self.path.text().strip() or paths.DEFAULT_DATA))

    def _refresh(self):
        root = self._root()
        rows = [(paths.IQ, "сырые записи пролётов спутников (cu8)"),
                (os.path.join(paths.PICS, paths.PICS_TV), "снимки кадров ТВ (F12)"),
                (os.path.join(paths.PICS, paths.PICS_METEOR), "снимки «Метеор-М»"),
                (os.path.join(paths.PICS, paths.PICS_ELEKTRO), "снимки «Электро-Л»"),
                (os.path.join(paths.PICS, paths.PICS_SSTV), "картинки SSTV"),
                (paths.AUDIO, "записи радио (WAV)"),
                (os.path.join(paths.LOGS, paths.LOGS_ADSB), "журналы ADS-B (CSV, KML)"),
                (os.path.join(paths.LOGS, paths.LOGS_APRS), "журналы пакетов APRS")]
        service = os.path.join(root, paths.SERVICE) if self.cb_service.isChecked() else paths.LOCAL
        html = "<table cellspacing='3'>" + "".join(
            f"<tr><td><b>{root}\\{sub}</b></td><td>&nbsp;— {what}</td></tr>" for sub, what in rows)
        html += f"<tr><td><b>{service}</b></td><td>&nbsp;— служебные файлы</td></tr></table>"
        self.layout_lbl.setText(html)
        self.items = paths.legacy_items(root, self.cb_service.isChecked())
        if self.items:
            total = sum(i[2] for i in self.items)
            lines = "".join(f"<li>{src} → {dst} ({_fmt_size(size)})</li>" for src, dst, size, _f in self.items)
            self.legacy_lbl.setText(f"Найдено {len(self.items)} мест, всего {_fmt_size(total)}:<ul>{lines}</ul>"
                                    "Перенос не обязателен: старые файлы просто останутся там, где были.")
            self.btn_move.setEnabled(True)
        else:
            self.legacy_lbl.setText("В прежних местах ничего не найдено — переносить нечего.")
            self.btn_move.setEnabled(False)

    def _pick(self):
        d = QFileDialog.getExistingDirectory(self, "Папка данных SDR4NOOB", self._root())
        if d:
            # если выбрали непустую «чужую» папку — данные кладём в её подпапку SDR4NOOB, чтобы не мусорить
            if os.path.basename(os.path.normpath(d)).lower() != "rtltv" and os.listdir(d):
                d = os.path.join(d, "SDR4NOOB")
            self.path.setText(os.path.normpath(d))

    def _open(self, d):
        d = os.path.abspath(d)
        os.makedirs(d, exist_ok=True)
        osplat.open_path(d)

    # ---------------------------------------------------------------- действия
    def _check_root(self, root):
        try:
            os.makedirs(root, exist_ok=True)
            test = os.path.join(root, ".rtltv_write_test")
            with open(test, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(test)
            return True
        except OSError as e:
            QMessageBox.warning(self, "SDR4NOOB", f"В папку нельзя писать:\n{root}\n\n{e}")
            return False

    def _move(self):
        root = self._root()
        if not self._check_root(root):
            return
        total = sum(i[2] for i in self.items)
        same_drive = all(os.path.splitdrive(src)[0].lower() == os.path.splitdrive(root)[0].lower()
                         for src, *_ in self.items)
        msg = (f"Перенести {_fmt_size(total)} в\n{root}?\n\n"
               "Файлы перемещаются (не копируются): из прежних мест они исчезнут. Совпадающие имена не "
               "перезаписываются — к новому имени добавится номер.")
        if not same_drive:
            msg += "\n\nПапка на другом диске — перенос займёт время (копирование)."
        if QMessageBox.question(self, "Перенос файлов", msg) != QMessageBox.Yes:
            return
        dlg = QProgressDialog("Перенос файлов…", None, 0, len(self.items), self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.show()

        def prog(i, n):
            dlg.setValue(i)
            QApplication.processEvents()
        moved, errors = paths.move_items(self.items, prog)
        dlg.close()
        if errors:
            QMessageBox.warning(self, "Перенос файлов", f"Перенесено: {moved}. Ошибки:\n" + "\n".join(errors[:10]))
        else:
            QMessageBox.information(self, "Перенос файлов", f"Готово, перенесено: {moved}.")
        self._refresh()

    def _save(self):
        root = self._root()
        if not self._check_root(root):
            return
        old_root, old_service = paths.data_root(), paths.service_in_data()
        changed = (os.path.normcase(root) != os.path.normcase(old_root)) or (self.cb_service.isChecked() != old_service)
        app_s = settings.load("app", {})
        if os.path.normcase(root) != os.path.normcase(old_root):
            app_s["data_dir_prev"] = old_root
        app_s["data_dir"] = None if os.path.normcase(root) == os.path.normcase(paths.DEFAULT_DATA) else root
        app_s["service_in_data"] = self.cb_service.isChecked()
        app_s["update_check"] = self.upd.currentData()
        settings.save("app", app_s)
        self.accept()
        if changed and QMessageBox.question(
                self.parentWidget(), "SDR4NOOB",
                "Новые папки начнут использоваться после перезапуска программы.\nПерезапустить сейчас?") == QMessageBox.Yes:
            restart()


def restart():
    """Перезапустить SDR4NOOB (EXE или «py -m rtltv») и закрыть текущий процесс."""
    from . import updater_linux
    if updater_linux.appimage_path():   # не файл внутри монтирования AppImage — оно исчезнет вместе с нами
        updater_linux.restart(updater_linux.appimage_path())
    else:
        if getattr(sys, "frozen", False):
            args = [sys.executable]
        else:
            args = [sys.executable, "-m", "rtltv"]
        subprocess.Popen(args, cwd=os.getcwd(), close_fds=True)
    app = QApplication.instance()
    for w in app.topLevelWidgets():
        w.close()
    app.quit()


def show_settings(parent=None):
    SettingsDialog(parent).exec()
