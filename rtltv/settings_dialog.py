"""Окно «Настройки»: язык интерфейса, где хранить записи, снимки, звук, журналы и служебные файлы, обновления."""
import os
import subprocess
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QGroupBox,
                               QScrollArea, QWidget,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressDialog, QPushButton,
                               QVBoxLayout)

from . import i18n, paths, settings
from .i18n import tr


def _fmt_size(b):
    for unit, k in ((tr("ГБ"), 1e9), (tr("МБ"), 1e6), (tr("КБ"), 1e3)):
        if b >= k:
            return f"{i18n.num(b / k, 1)} {unit}"
    return tr("{n} Б", n=b)


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("SDR4NOOB — настройки"))
        self.resize(820, 700)
        self.setMinimumSize(640, 560)
        self.app_s = settings.load("app", {})
        outer = QVBoxLayout(self)
        # содержимое — в прокрутке: на низком экране (1366×768) группы не налезают друг на друга
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        self.lang = None
        if i18n.selectable():
            # второе название — на другом языке: найти переключатель, даже если текущий язык непонятен
            g0 = QGroupBox(tr("Язык интерфейса") + (" / Language" if i18n.language() == "ru"
                                                     else " / Язык интерфейса"))  # noqa: i18n
            g0l = QHBoxLayout(g0)
            self.lang = QComboBox()
            sys_name = i18n.LANGS[i18n.system_language()]
            self.lang.addItem(tr("Как в системе ({language})", language=sys_name), "auto")
            for code, name in i18n.LANGS.items():
                self.lang.addItem(name, code)
            self.lang.setCurrentIndex(max(0, self.lang.findData(i18n.configured())))
            g0l.addWidget(self.lang)
            note = QLabel(tr("Окна откроются заново на выбранном языке, текущий режим — тоже."))
            note.setObjectName("dim")
            g0l.addWidget(note, 1)
            v.addWidget(g0)

        g = QGroupBox(tr("Папка данных"))
        gl = QVBoxLayout(g)
        hint = QLabel(tr("Сюда SDR4NOOB складывает всё, что вы получаете: записи пролётов, снимки, записи звука, "
                         "журналы. Можно выбрать любой диск, например большой второй диск для записей IQ (они весят "
                         "до 2 ГБ за пролёт)."))
        hint.setWordWrap(True)
        hint.setObjectName("dim")
        gl.addWidget(hint)
        row = QHBoxLayout()
        self.path = QLineEdit(paths.data_root())
        self.path.setToolTip(tr("Корневая папка данных SDR4NOOB"))
        b_pick = QPushButton(tr("Выбрать…"))
        b_pick.clicked.connect(self._pick)
        b_open = QPushButton(tr("Открыть"))
        b_open.clicked.connect(lambda: self._open(self.path.text()))
        b_def = QPushButton(tr("По умолчанию"))
        b_def.setToolTip(paths.DEFAULT_DATA)
        b_def.clicked.connect(lambda: self.path.setText(paths.DEFAULT_DATA))
        row.addWidget(self.path, 1)
        row.addWidget(b_pick)
        row.addWidget(b_open)
        row.addWidget(b_def)
        gl.addLayout(row)
        self.cb_service = QCheckBox(tr("Служебные файлы тоже здесь (кэш карт, орбиты, история самолётов, "
                                       "справочник бортов, драйверы, журнал программы)"))
        self.cb_service.setChecked(paths.service_in_data())
        self.cb_service.setToolTip(tr("Если выключено — служебные файлы в {path}", path=paths.LOCAL))
        gl.addWidget(self.cb_service)
        v.addWidget(g)

        g2 = QGroupBox(tr("Что где будет лежать"))
        g2l = QVBoxLayout(g2)
        self.layout_lbl = QLabel()
        self.layout_lbl.setWordWrap(True)
        self.layout_lbl.setTextFormat(Qt.RichText)
        g2l.addWidget(self.layout_lbl)
        settings_note = QLabel(tr("Настройки программы всегда хранятся в {path} — по ним SDR4NOOB находит папку "
                                  "данных.", path=settings.DIR))
        settings_note.setObjectName("dim")
        settings_note.setWordWrap(True)
        g2l.addWidget(settings_note)
        v.addWidget(g2)

        g3 = QGroupBox(tr("Файлы в прежних местах"))
        g3l = QVBoxLayout(g3)
        self.legacy_lbl = QLabel()
        self.legacy_lbl.setWordWrap(True)
        self.legacy_lbl.setTextFormat(Qt.RichText)
        g3l.addWidget(self.legacy_lbl)
        self.btn_move = QPushButton(tr("Перенести их в папку данных…"))
        self.btn_move.clicked.connect(self._move)
        g3l.addWidget(self.btn_move)
        v.addWidget(g3)

        g4 = QGroupBox(tr("Обновления"))
        g4l = QHBoxLayout(g4)
        g4l.addWidget(QLabel(tr("Проверять новую версию на GitHub:")))
        self.upd = QComboBox()
        for key, name in (("daily", tr("раз в день")), ("weekly", tr("раз в неделю")), ("never", tr("никогда"))):
            self.upd.addItem(name, key)
        self.upd.setCurrentIndex(max(0, self.upd.findData(self.app_s.get("update_check", "daily"))))
        g4l.addWidget(self.upd)
        b_now = QPushButton(tr("Проверить сейчас"))
        b_now.clicked.connect(lambda: __import__("rtltv.update_ui", fromlist=["x"]).check_now(self))
        g4l.addWidget(b_now)
        g4l.addStretch(1)
        v.addWidget(g4)
        v.addStretch(1)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText(tr("Сохранить"))
        bb.button(QDialogButtonBox.Cancel).setText(tr("Отмена"))
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

        self.path.textChanged.connect(self._refresh)
        self.cb_service.toggled.connect(self._refresh)
        self._refresh()
        # по содержимому, но не выше экрана (ноутбук 1366×768: кнопки «Сохранить/Отмена» должны быть видны)
        h = max(700, body.sizeHint().height() + bb.sizeHint().height() + 40)
        scr = self.screen().availableGeometry() if self.screen() is not None else None
        if scr is not None and scr.height() > 0:
            h = min(h, scr.height() - 40)
        self.resize(820, h)

    # ---------------------------------------------------------------- вид
    def _root(self):
        return os.path.abspath(os.path.expandvars(self.path.text().strip() or paths.DEFAULT_DATA))

    def _refresh(self):
        root = self._root()
        rows = [(paths.IQ, tr("сырые записи пролётов спутников (cu8)")),
                (os.path.join(paths.PICS, paths.PICS_TV), tr("снимки кадров ТВ (F12)")),
                (os.path.join(paths.PICS, paths.PICS_METEOR), tr("снимки «Метеор-М»")),
                (os.path.join(paths.PICS, paths.PICS_ELEKTRO), tr("снимки «Электро-Л»")),
                (os.path.join(paths.PICS, paths.PICS_SSTV), tr("картинки SSTV")),
                (paths.AUDIO, tr("записи радио (WAV)")),
                (os.path.join(paths.LOGS, paths.LOGS_ADSB), tr("журналы ADS-B (CSV, KML)")),
                (os.path.join(paths.LOGS, paths.LOGS_APRS), tr("журналы пакетов APRS"))]
        service = os.path.join(root, paths.SERVICE) if self.cb_service.isChecked() else paths.LOCAL
        html = "<table cellspacing='3'>" + "".join(
            f"<tr><td><b>{root}\\{sub}</b></td><td>&nbsp;— {what}</td></tr>" for sub, what in rows)
        html += f"<tr><td><b>{service}</b></td><td>&nbsp;— {tr('служебные файлы')}</td></tr></table>"
        self.layout_lbl.setText(html)
        self.items = paths.legacy_items(root, self.cb_service.isChecked())
        if self.items:
            total = sum(i[2] for i in self.items)
            lines = "".join(f"<li>{src} → {dst} ({_fmt_size(size)})</li>" for src, dst, size, _f in self.items)
            self.legacy_lbl.setText(
                i18n.trn(len(self.items), "Найдено {n} место, всего {size}:", "Найдено {n} места, всего {size}:",
                         "Найдено {n} мест, всего {size}:", size=_fmt_size(total))
                + f"<ul>{lines}</ul>" + tr("Перенос не обязателен: старые файлы просто останутся там, где были."))
            self.btn_move.setEnabled(True)
        else:
            self.legacy_lbl.setText(tr("В прежних местах ничего не найдено — переносить нечего."))
            self.btn_move.setEnabled(False)

    def _pick(self):
        d = QFileDialog.getExistingDirectory(self, tr("Папка данных SDR4NOOB"), self._root())
        if d:
            # если выбрали непустую «чужую» папку — данные кладём в её подпапку SDR4NOOB, чтобы не мусорить
            if os.path.basename(os.path.normpath(d)).lower() != "rtltv" and os.listdir(d):
                d = os.path.join(d, "SDR4NOOB")
            self.path.setText(os.path.normpath(d))

    def _open(self, d):
        d = os.path.abspath(d)
        os.makedirs(d, exist_ok=True)
        os.startfile(d)  # noqa: S606 — только Windows

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
            QMessageBox.warning(self, "SDR4NOOB", tr("В папку нельзя писать:\n{path}\n\n{error}", path=root, error=e))
            return False

    def _move(self):
        root = self._root()
        if not self._check_root(root):
            return
        total = sum(i[2] for i in self.items)
        same_drive = all(os.path.splitdrive(src)[0].lower() == os.path.splitdrive(root)[0].lower()
                         for src, *_ in self.items)
        msg = tr("Перенести {size} в\n{path}?\n\n"
                 "Файлы перемещаются (не копируются): из прежних мест они исчезнут. Совпадающие имена не "
                 "перезаписываются — к новому имени добавится номер.", size=_fmt_size(total), path=root)
        if not same_drive:
            msg += "\n\n" + tr("Папка на другом диске — перенос займёт время (копирование).")
        if QMessageBox.question(self, tr("Перенос файлов"), msg) != QMessageBox.Yes:
            return
        dlg = QProgressDialog(tr("Перенос файлов…"), None, 0, len(self.items), self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.show()

        def prog(i, n):
            dlg.setValue(i)
            QApplication.processEvents()
        moved, errors = paths.move_items(self.items, prog)
        dlg.close()
        if errors:
            QMessageBox.warning(self, tr("Перенос файлов"),
                                tr("Перенесено: {n}. Ошибки:", n=moved) + "\n" + "\n".join(errors[:10]))
        else:
            QMessageBox.information(self, tr("Перенос файлов"), tr("Готово, перенесено: {n}.", n=moved))
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
        lang_new = None
        if self.lang is not None:
            app_s["language"] = self.lang.currentData()
            lang_new = i18n.resolve(app_s["language"])
            if lang_new == i18n.language():
                lang_new = None
        settings.save("app", app_s)
        self.accept()
        if changed and QMessageBox.question(
                self.parentWidget(), "SDR4NOOB",
                tr("Новые папки начнут использоваться после перезапуска программы.\nПерезапустить сейчас?")) \
                == QMessageBox.Yes:
            restart()
            return
        if lang_new:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: __import__("rtltv.launcher", fromlist=["x"]).switch_language(lang_new))


def restart():
    """Перезапустить SDR4NOOB (EXE или «py -m rtltv») и закрыть текущий процесс."""
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
