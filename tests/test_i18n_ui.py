"""Живая проверка перевода: окна на английском без единой русской буквы и смена языка на лету.

Статический сканер (test_i18n.py) не видит строк, собранных из данных во время работы, — здесь окна строятся
по-настоящему (offscreen) и обходятся все надписи, подсказки, пункты списков и меню."""
import re
import sys
import types

import pytest

from rtltv import i18n

CYR = re.compile("[А-Яа-яЁё]")


@pytest.fixture(autouse=True)
def _no_stale_launcher():
    """Лаунчеры тестов удаляются (deleteLater) — не оставлять на них ссылку в rtltv.launcher._launchers."""
    yield
    from rtltv import launcher
    launcher._launchers.clear()


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def english():
    old = i18n.language()
    i18n.set_language("en")
    yield
    i18n.set_language(old)


def visible_texts(root):
    """Все тексты окна: заголовок, надписи, кнопки, подсказки, группы, списки, меню (с подменю)."""
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import (QAbstractButton, QComboBox, QGroupBox, QLabel, QLineEdit, QMainWindow, QMenu,
                                   QWidget)
    out = [("windowTitle", root.windowTitle())]

    def menu(m, where):
        out.append((where, m.title()))
        for a in m.actions():
            out.append((where, a.text()))
            out.append((where + " tip", a.toolTip() if a.toolTip() != a.text().replace("&", "") else ""))
            if a.menu() is not None:
                menu(a.menu(), where + "/" + a.text())

    for w in [root] + root.findChildren(QWidget):
        if w is not root and isinstance(w, QLineEdit):
            continue  # пути и ввод пользователя
        out.append((type(w).__name__ + " tip", w.toolTip()))
        if isinstance(w, (QLabel, QAbstractButton)):
            out.append((type(w).__name__, w.text()))
        if isinstance(w, QGroupBox):
            out.append(("QGroupBox", w.title()))
        if isinstance(w, QComboBox):
            out.extend(("QComboBox", w.itemText(i)) for i in range(w.count()))
    if isinstance(root, QMainWindow):
        for a in root.menuBar().actions():
            if a.menu() is not None and a.menu() is not getattr(root, "sound_menu", None):  # там имена устройств Windows
                a.menu().aboutToShow.emit()   # меню, которые заполняются при открытии
                menu(a.menu(), a.text())
    sound = getattr(root, "sound_menu", None)
    for a in root.findChildren(QAction):
        if sound is None or a not in sound.actions():
            out.append(("QAction", a.text()))
    for m in root.findChildren(QMenu):
        out.append(("QMenu", m.title()))
    # «Русский» в списке языков и «Язык интерфейса» у переключателя — по-русски и в английском интерфейсе
    native = list(i18n.LANGS.values()) + ["Язык интерфейса"]

    def russian(t):
        t = re.sub(r"[A-Za-z]:\\[^<\n]*", "", t)  # пути Windows (папки пользователя) — не текст интерфейса
        for n in native:
            t = t.replace(n, "")
        return CYR.search(t)
    return [(k, t) for k, t in out if t and russian(t)]


def _assert_english(w):
    bad = visible_texts(w)
    assert not bad, "русский текст в английском интерфейсе:\n" + "\n".join(f"  {k}: {t!r}" for k, t in bad[:40])


@pytest.fixture
def lang_selectable(monkeypatch):
    monkeypatch.setattr(i18n, "ENGLISH_READY", True)


def test_launcher_english(qapp, english, lang_selectable):
    from rtltv.launcher import Launcher
    w = Launcher({"shot": "x"})
    try:
        _assert_english(w)
        assert w.lang is not None and w.lang.currentData() == "en"
        assert w.tiles[0].title == "TV"
    finally:
        w.deleteLater()


def test_settings_dialog_english(qapp, english, lang_selectable):
    from rtltv.settings_dialog import SettingsDialog
    d = SettingsDialog()
    try:
        _assert_english(d)
        assert [d.lang.itemData(i) for i in range(d.lang.count())] == ["auto", "ru", "en"]
    finally:
        d.deleteLater()


def test_qt_translator_follows_language(qapp):
    """Стандартные кнопки Qt: ru → en → ru снова по-русски (перевод Qt переставляется при каждой смене)."""
    from PySide6.QtWidgets import QDialogButtonBox
    old = i18n.language()
    try:
        i18n.set_language("ru")
        i18n.install_qt_translator(qapp)

        def yes():
            box = QDialogButtonBox(QDialogButtonBox.Yes)
            return box.buttons()[0].text().replace("&", "")
        assert yes() == "Да"
        i18n.set_language("en")
        assert yes() == "Yes"
        i18n.set_language("ru")
        assert yes() == "Да"
    finally:
        i18n.set_language(old)


def test_settings_save_keeps_language_unless_changed(qapp, monkeypatch, lang_selectable):
    """Сохранение «Настроек» ради другого параметра язык не переключает (выбор языка не трогали)."""
    from rtltv import launcher, settings_dialog
    calls = []
    monkeypatch.setattr(launcher, "switch_language", calls.append)
    monkeypatch.setattr(i18n, "system_language", lambda: "ru")
    old = i18n.language()
    i18n.set_language("en")
    try:
        d = settings_dialog.SettingsDialog()
        d._save()
        qapp.processEvents()
        assert calls == []
        d = settings_dialog.SettingsDialog()
        d.lang.setCurrentIndex(d.lang.findData("ru"))
        d._save()
        qapp.processEvents()
        assert calls == ["ru"]
    finally:
        i18n.set_language(old)


def test_settings_dialog_hides_language_until_ready(qapp, monkeypatch):
    monkeypatch.setattr(i18n, "ENGLISH_READY", False)
    monkeypatch.delenv("RTLTV_LANG", raising=False)
    from rtltv.settings_dialog import SettingsDialog
    d = SettingsDialog()
    assert d.lang is None
    d.deleteLater()


def test_mode_window_menus_english(qapp, english):
    from rtltv.modebase import ModeWindow
    w = ModeWindow({"shot": "x"}, "SDR4NOOB")
    try:
        _assert_english(w)
        titles = [a.text() for a in w.menuBar().actions()]
        assert titles[:3] == ["Mode", "Theme", "View"] and "Help" in titles
    finally:
        w.deleteLater()


def test_switch_language_reopens_mode(qapp, monkeypatch, lang_selectable):
    """Смена языка в окне режима: окно закрывается штатно (shutdown), лаунчер и режим открываются заново."""
    from rtltv import launcher as L
    from rtltv.modebase import ModeWindow
    log = []

    class Dummy(ModeWindow):
        def __init__(self, opts):
            super().__init__(opts, "Dummy")
            log.append(("open", i18n.language(), opts.get("source")))

        def shutdown(self):
            log.append(("shutdown", i18n.language()))

    mod = types.ModuleType("rtltv_i18n_dummy_mode")
    mod.Dummy = Dummy
    monkeypatch.setitem(sys.modules, mod.__name__, mod)
    monkeypatch.setattr(L, "MODES", L.MODES + [("dummy", mod.__name__, "Dummy", "Dummy", "", "tv")])
    old_lang = i18n.language()
    i18n.set_language("ru")
    first = L.Launcher({"shot": "x"})
    try:
        first.cb_test.setChecked(True)
        first.open_mode("dummy")
        assert first.current_key == "dummy"
        new = L.switch_language("en")
        assert i18n.language() == "en"
        assert log == [("open", "ru", "test"), ("shutdown", "en"), ("open", "en", "test")]
        assert new is not first and new.current_key == "dummy" and new.cb_test.isChecked()
        assert first.property("retired") and first.current is None
        _assert_english(new)
        new.current.close()
    finally:
        i18n.set_language(old_lang)
        for w in L._launchers + [first]:
            w.deleteLater()
        L._launchers.clear()
