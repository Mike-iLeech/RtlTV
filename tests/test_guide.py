"""Тесты встроенного руководства: страницы на месте, внутренние ссылки и якоря ведут куда надо,
картинки есть и рисуются, поиск находит слова, окно открывается (offscreen) и переходит по ссылкам."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rtltv import guide_window as gw  # noqa: E402

HREF = re.compile(r"""<a\s[^>]*href=["']([^"']+)["']""", re.I)
SRC = re.compile(r"""<img\s[^>]*src=["']([^"']+)["']""", re.I)
ANCHOR = re.compile(r"""<a\s[^>]*name=["']([^"']+)["']|\sid=["']([^"']+)["']""", re.I)


def _anchors(src):
    return {a or b for a, b in ANCHOR.findall(src)}


def _all():
    return {pid: gw.read_page(pid) for pid, _t in gw.pages()}


def test_pages_exist_and_utf8():
    pids = [pid for pid, _t in gw.pages()]
    assert len(pids) == len(set(pids))
    for pid in pids:
        src = gw.read_page(pid)
        assert '<meta charset="utf-8">' in src, pid
        assert "<h1>" in src, pid
        assert "\r" not in src, f"{pid}: CRLF"
    # все html в папке входят в оглавление
    files = {f[:-5] for f in os.listdir(gw.guide_dir()) if f.endswith(".html")}
    assert files == set(pids)


def test_internal_links_and_anchors():
    pages = _all()
    anchors = {pid: _anchors(src) for pid, src in pages.items()}
    bad = []
    for pid, src in pages.items():
        for href in HREF.findall(src):
            if href.startswith(("http://", "https://", "mailto:")):
                continue
            target, _, frag = href.partition("#")
            tid = target[:-5] if target.endswith(".html") else (target or pid)
            if tid not in pages:
                bad.append(f"{pid}: {href} — нет страницы")
            elif frag and frag not in anchors[tid]:
                bad.append(f"{pid}: {href} — нет якоря")
    assert not bad, "\n".join(bad)


def test_images_exist_and_render():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    t = gw.THEMES["Светлая"]
    for pid, src in _all().items():
        for img in SRC.findall(src):
            path = os.path.join(gw.guide_dir(), img)
            assert os.path.exists(path), f"{pid}: {img}"
            if not img.endswith(".svg"):      # растровые (логотип в оглавлении) — только наличие
                continue
            with open(path, encoding="utf-8") as f:
                data = f.read()
            assert "{{" in data  # цвета берутся из темы
            im = gw.render_svg(data, t)
            assert not im.isNull() and im.width() > 100, img


def test_search_finds_words():
    texts = {pid: gw.page_text(src) for pid, src in _all().items()}
    for query, page in (("V-диполь", "antennas"), ("дБмкВ", "decibels"), ("Zadig", "sdr"),
                        ("пилот-тон", "modulation"), ("сквок", "mode-adsb"), ("доплер", "satellites"),
                        ("WinUSB занят", "troubleshooting"), ("цветовой код", "mode-radio")):
        found = [pid for pid, _n, _s in gw.search(query, texts)]
        assert page in found, (query, found)
    assert gw.search("абракадабра-xyz", texts) == []
    assert gw.search("   ", texts) == []
    # разметка и svg не попадают в текст
    assert "<" not in texts["antennas"] and "{{" not in texts["antennas"]


def test_page_for_modes():
    class RadioWindow:
        pass

    class Custom:
        GUIDE_PAGE = "antennas"

    assert gw.page_for(RadioWindow()) == "mode-radio"
    assert gw.page_for(Custom()) == "antennas"
    assert gw.page_for(object()) == "index"
    for page in gw.MODE_PAGES.values():
        assert os.path.exists(gw.page_path(page))


def test_window_navigation_and_search(monkeypatch):
    monkeypatch.setattr(gw.settings, "save", lambda *_a: None)  # не трогать настройки пользователя
    from PySide6.QtCore import QUrl
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    w = gw.show_guide("radio")
    assert gw.show_guide("antennas#v-dipole") is w  # одно окно
    app.processEvents()
    assert w.current_page() == "antennas"
    assert w.tree.currentItem().data(0, 0x0100) == "antennas"
    # переход по относительной ссылке, как при щелчке
    w.view.setSource(w.view.source().resolved(QUrl("glossary.html#ru-ya")))
    app.processEvents()
    assert w.current_page() == "glossary"
    w.view.backward()
    app.processEvents()
    assert w.current_page() == "antennas"
    # поиск по всем страницам и подсветка
    w.q.setText("bias-T")
    app.processEvents()
    assert w.results.count() >= 3
    w._result_clicked(w.results.item(0))
    app.processEvents()
    assert w._highlight() > 0
    w.q.clear()
    # масштаб в пределах
    z = w.st["zoom"]
    w.zoom(+1)
    w.zoom(-1)
    assert w.st["zoom"] == z
    # смена темы перерисовывает страницу
    w.apply_theme("Светлая")
    assert "#0a66c2" in w.view.document().defaultStyleSheet()
    w.close()


def test_help_menu_in_mode_window(monkeypatch):
    monkeypatch.setattr(gw.settings, "save", lambda *_a: None)
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from rtltv.modebase import ModeWindow

    class AdsbWindow(ModeWindow):
        pass

    class RadioWindow(ModeWindow):
        def show_help(self):
            pass

    for cls, f1 in ((AdsbWindow, True), (RadioWindow, True)):  # F1 — общее руководство везде
        w = cls({"theme": "Тёмная"})
        menus = {a.text(): a.menu() for a in w.menuBar().actions()}
        assert "Справка" in menus
        acts = {a.text(): a for a in menus["Справка"].actions()}
        assert "Руководство (F1)…" in acts and "О программе" in acts
        assert (acts["Руководство (F1)…"].shortcut().toString() == "F1") is f1
        acts["Руководство (F1)…"].trigger()
        assert gw._window is not None and gw._window.current_page() == gw.page_for(w)
        gw._window.close()
        w.close()
