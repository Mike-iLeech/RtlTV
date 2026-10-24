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
            if "/" in target:  # интерактивная страница в подпапке (pipeline/index.html) — открывается в браузере
                if not os.path.isfile(os.path.join(gw.guide_dir(), *target.split("/"))):
                    bad.append(f"{pid}: {href} — нет файла")
                continue
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


def test_pipeline_page_in_toc():
    """«Путь радиосигнала» — в разделе «Основы радиоприёма», со схемой и кнопкой интерактивной страницы."""
    groups = {key: [pid for pid, _t in val] for key, val in gw.TOC if isinstance(val, list)}
    assert "pipeline" in groups["Основы радиоприёма"]
    assert gw.page_title("pipeline") == "Путь радиосигнала"
    src = gw.read_page("pipeline")
    assert 'src="img/pipeline.svg"' in src
    assert re.search(r'<table class="btn"[^>]*>\s*<tr>\s*<td>\s*<a href="pipeline/index.html">', src)
    assert '<a href="pipeline.html">' in gw.read_page("index")
    texts = {pid: gw.page_text(s) for pid, s in _all().items()}
    assert "pipeline" in [pid for pid, _n, _s in gw.search("фидер МШУ", texts)]


def test_interactive_page_offline():
    """Интерактивная схема работает из файла: всё своё рядом, без CDN и внешних шрифтов."""
    base = os.path.join(gw.guide_dir(), "pipeline")
    with open(os.path.join(base, "index.html"), encoding="utf-8") as f:
        src = f.read()
    assert '<meta charset="utf-8">' in src and 'name="viewport"' in src
    assert not re.search(r"""(?<![\w-])(src|href)=["'](https?:)?//""", src)
    for ref in re.findall(r"""(?<![\w-])(?:src|href)=["']([^"'#:]+)["']""", src):   # не data-src и т. п.
        assert os.path.isfile(os.path.normpath(os.path.join(base, ref))), ref
    for name in ("app.js", "style.css"):
        with open(os.path.join(base, name), encoding="utf-8") as f:
            txt = f.read()
        assert "http://" not in txt.replace("http://www.w3.org/2000/svg", "") and "https://" not in txt, name
        assert "@import" not in txt and "fonts.googleapis" not in txt
    for anchor in re.findall(r'href="#([\w-]+)"', src):
        assert f'id="{anchor}"' in src, anchor


def test_interactive_link_without_webengine_opens_browser(monkeypatch):
    """Без QtWebEngine ссылка на pipeline/index.html не грузится в QTextBrowser, а уходит в системный браузер."""
    monkeypatch.setattr(gw.settings, "save", lambda *_a: None)
    monkeypatch.setattr(gw, "web_engine", lambda: None)
    opened = []
    monkeypatch.setattr(gw, "open_in_browser", lambda path: opened.append(path) or True)
    from PySide6.QtCore import QUrl
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    w = gw.GuideWindow("pipeline")
    w.show()
    app.processEvents()
    assert w.current_page() == "pipeline"
    src = w.view.source()
    hist = w.view.backwardHistoryCount()
    w.view.setSource(src.resolved(QUrl("pipeline/index.html")))
    app.processEvents()
    want = os.path.join(gw.guide_dir(), "pipeline", "index.html")
    assert [os.path.normcase(p) for p in opened] == [os.path.normcase(want)]
    assert w.view.source() == src and w.current_page() == "pipeline"  # окно осталось на своей странице
    assert w.view.backwardHistoryCount() == hist  # в историю не попало
    # настоящий щелчок мышью по кнопке-ссылке на странице
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    opened.clear()
    w.resize(1100, 760)
    app.processEvents()
    cur = w.view.document().find("Открыть интерактивную схему")
    assert not cur.isNull()
    w.view.setTextCursor(cur)
    w.view.ensureCursorVisible()
    app.processEvents()
    r = w.view.cursorRect(cur)
    pos = QPoint(r.left() - 40, r.center().y())
    QTest.mouseClick(w.view.viewport(), Qt.LeftButton, Qt.NoModifier, pos)
    app.processEvents()
    assert [os.path.normcase(p) for p in opened] == [os.path.normcase(want)]
    assert w.current_page() == "pipeline"
    # обычные страницы и картинки — не «интерактивные»
    assert gw.interactive_path(QUrl.fromLocalFile(gw.page_path("sdr"))) is None
    assert gw.interactive_path(QUrl.fromLocalFile(os.path.join(gw.guide_dir(), "img", "pipeline.svg"))) is None
    assert gw.interactive_path(QUrl("https://example.org/pipeline/index.html")) is None
    w.close()


def _wait(app, cond, secs=20):
    import time
    t = time.time() + secs
    while not cond() and time.time() < t:
        app.processEvents()
        time.sleep(0.01)
    return cond()


def _js(app, page, code, secs=10):
    box = []
    page.runJavaScript(code, 0, box.append)
    assert _wait(app, lambda: box, secs), code
    return box[0]


def test_interactive_page_embedded(monkeypatch):
    """Схема открывается в самом окне справки (QtWebEngine): тема приложения, «Назад»/«Вперёд», ссылки со схемы
    на обычные страницы — в окно справки, внешние — в браузер; скрытая схема заморожена и без звука."""
    if gw.web_engine() is None:
        import pytest
        pytest.skip("нет QtWebEngine")
    monkeypatch.setattr(gw.settings, "save", lambda *_a: None)
    monkeypatch.setattr(gw.settings, "load", lambda name, default=None: {"theme": "Светлая"} if name == "app" else dict(default or {}))
    opened = []
    monkeypatch.setattr(gw, "open_in_browser", lambda path: opened.append(path) or True)
    from PySide6.QtCore import QUrl
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    w = gw.GuideWindow("pipeline")
    w.resize(1100, 760)
    w.show()
    app.processEvents()
    hist = w.view.backwardHistoryCount()
    w.view.setSource(w.view.source().resolved(QUrl("pipeline/index.html")))
    app.processEvents()
    assert opened == [] and w.web_shown()
    assert w.view.backwardHistoryCount() == hist and w.current_page() == "pipeline"
    assert "интерактивная схема" in w.title.text() and w.b_back.isEnabled()
    page = w.web.page()
    assert _wait(app, lambda: _js(app, page, "!!window.SDR4NOOB_PIPELINE") is True)
    assert _js(app, page, "document.documentElement.getAttribute('data-theme')") == "light"
    assert "theme=light" in w.web.url().query()
    # переход на блок схемы по якорю — без перезагрузки, блок раскрывается
    _js(app, page, "window.__keep = 1")
    w.show_interactive(gw.interactive_path(w.web.url()), "b-fft")
    assert _wait(app, lambda: _js(app, page, "document.getElementById('b-fft').open") is True)
    assert _js(app, page, "window.__keep") == 1
    # «Назад» — на страницу со ссылкой, схема заморожена и без звука; «Вперёд» — обратно на схему
    w.go_back()
    app.processEvents()
    assert not w.web_shown() and w.current_page() == "pipeline" and w.b_fwd.isEnabled()
    assert page.isAudioMuted() and page.lifecycleState() == QWebEnginePage.LifecycleState.Frozen
    w.go_forward()
    app.processEvents()
    assert w.web_shown() and not page.isAudioMuted()
    assert page.lifecycleState() == QWebEnginePage.LifecycleState.Active
    assert _js(app, page, "window.__keep") == 1  # та же страница, состояние на месте
    # ссылка со схемы на обычную страницу руководства открывается в окне справки
    _js(app, page, "location.href = '../antennas.html#lna'")
    assert _wait(app, lambda: not w.web_shown())
    assert w.current_page() == "antennas" and w.view.source().fragment() == "lna"
    # смена темы приложения перезагружает схему в тёмной теме
    w.view.setSource(QUrl.fromLocalFile(os.path.join(gw.guide_dir(), "pipeline", "index.html")))
    app.processEvents()
    assert w.web_shown()
    w.apply_theme("Тёмная")
    assert _wait(app, lambda: _js(app, page, "document.documentElement.getAttribute('data-theme')") == "dark")
    # масштаб A+/A− действует и на схему
    w.st["zoom"] = 13
    w._apply_zoom()
    assert abs(w.web.zoomFactor() - 13 / 11) < 1e-6
    assert opened == []
    w.close()
    w.release_web()
    assert w.web is None and not w.web_shown()


def test_interactive_navigation_cases(monkeypatch):
    """Навигация между схемой и страницами: «Назад» со страницы, открытой ссылкой со схемы, — на схему;
    «Вперёд» на схему важнее истории; поиск по той же странице показывает текст; закрытое окно — схема заморожена."""
    if gw.web_engine() is None:
        import pytest
        pytest.skip("нет QtWebEngine")
    monkeypatch.setattr(gw.settings, "save", lambda *_a: None)
    monkeypatch.setattr(gw, "open_in_browser", lambda path: True)
    from PySide6.QtCore import QUrl
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    ip = os.path.join(gw.guide_dir(), "pipeline", "index.html")
    w = gw.GuideWindow("pipeline")
    w.resize(1100, 760)
    w.show()
    app.processEvents()
    w.show_interactive(ip)
    page = w.web.page()
    assert _wait(app, lambda: _js(app, page, "!!window.SDR4NOOB_PIPELINE") is True)
    assert w.title.text() == "Путь радиосигнала — интерактивная схема"
    # ссылка со схемы в словарь → «Назад» снова на схему → «Вперёд» снова в словарь
    _js(app, page, "location.href = '../glossary.html#t-fspl'")
    assert _wait(app, lambda: not w.web_shown() and w.current_page() == "glossary")
    assert w.b_back.isEnabled()
    w.go_back()
    app.processEvents()
    assert w.web_shown() and w.b_fwd.isEnabled()
    w.go_forward()
    app.processEvents()
    assert not w.web_shown() and w.current_page() == "glossary" and w.view.source().fragment() == "t-fspl"
    # ссылка → «Назад» (схема) → «Назад» — на страницу, с которой открыта схема, а не на словарь
    w.go_back()
    app.processEvents()
    assert w.web_shown()
    w.go_back()
    app.processEvents()
    assert not w.web_shown() and w.current_page() == "pipeline" and w.b_fwd.isEnabled()
    w.go_forward()
    app.processEvents()
    assert w.web_shown()
    # A → B → «Назад» на A → схема с A → «Назад» → «Вперёд» ведёт на схему, а не на B
    w.open_page("sdr")
    w.open_page("decibels")
    w.go_back()
    app.processEvents()
    assert w.current_page() == "sdr" and w.view.isForwardAvailable()
    w.view.setSource(QUrl.fromLocalFile(ip))
    app.processEvents()
    assert w.web_shown()
    w.go_back()
    app.processEvents()
    assert not w.web_shown() and w.current_page() == "sdr"
    w.go_forward()
    app.processEvents()
    assert w.web_shown()
    # поиск: результат на странице, с которой открыта схема, — показывается текст
    w.q.setText("АЦП")
    app.processEvents()
    it = next(w.results.item(i) for i in range(w.results.count()) if w.results.item(i).data(gw.Qt.UserRole) == "sdr")
    w._result_clicked(it)
    app.processEvents()
    assert not w.web_shown() and w.current_page() == "sdr"
    w.q.clear()
    # закрытое окно: схема заморожена и без звука; открыли снова — ожила
    w.show_interactive(ip)
    app.processEvents()
    w.close()
    assert _wait(app, lambda: page.lifecycleState() == QWebEnginePage.LifecycleState.Frozen, 5)
    assert page.isAudioMuted()
    w.show()
    app.processEvents()
    assert page.lifecycleState() == QWebEnginePage.LifecycleState.Active and not page.isAudioMuted()
    w.close()
    w.release_web()


def test_selftest_guide_exits_cleanly():
    """SDR4NOOB --selftest-guide (шаг CI для собранного EXE): схема оживает, процесс выходит с кодом 0 — без
    падения при выходе (профиль QtWebEngine должен пережить страницу)."""
    if gw.web_engine() is None:
        import pytest
        pytest.skip("нет QtWebEngine")
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    r = subprocess.run([sys.executable, "-m", "rtltv", "--selftest-guide"], cwd=root, env=env, timeout=120,
                       capture_output=True)
    assert r.returncode == 0, (r.returncode, r.stderr[-2000:])


def test_web_nav_rules():
    """Куда ведут переходы со встроенной схемы."""
    from PySide6.QtCore import QUrl
    base = os.path.join(gw.guide_dir(), "pipeline", "index.html")
    assert gw.web_nav(QUrl.fromLocalFile(base)) == ("web", None)
    u = QUrl.fromLocalFile(base)
    u.setFragment("b-air")
    assert gw.web_nav(u) == ("web", None)
    u = QUrl.fromLocalFile(gw.page_path("decibels"))
    u.setFragment("dbm")
    assert gw.web_nav(u) == ("guide", "decibels#dbm")
    assert gw.web_nav(QUrl.fromLocalFile(gw.page_path("legal"))) == ("guide", "legal")
    kind, url = gw.web_nav(QUrl("https://example.org/"))
    assert kind == "external" and url.host() == "example.org"
    assert gw.web_nav(QUrl("data:text/plain,1"))[0] == "web"
    assert gw.web_nav(QUrl.fromLocalFile(r"C:\Windows\win.ini")) == ("block", None)
    assert gw.web_nav(QUrl("ftp://example.org/x"))[0] == "block"


def test_pipeline_terms_dictionary():
    """Словарь сокращений интерактивной схемы: каждый термин ведёт в словарь руководства и, если указано,
    на существующий блок страницы; написания не повторяются."""
    import json
    base = os.path.join(gw.guide_dir(), "pipeline")
    with open(os.path.join(base, "terms.js"), encoding="utf-8") as f:
        src = f.read()
    terms = json.loads(src[src.index("["):src.rindex("]") + 1])
    with open(os.path.join(base, "index.html"), encoding="utf-8") as f:
        page = f.read()
    assert '<script src="terms.js"></script>' in page
    gloss = _anchors(gw.read_page("glossary"))
    words = []
    for t in terms:
        assert t["full"] and t["tip"], t["id"]
        if t["id"].startswith("f-"):  # формулы: ссылка проставлена вручную в «Формулах на одной странице»
            assert 'data-term="%s"' % t["id"] in page, t["id"]
        else:
            assert t["w"], t["id"]
        assert t["gl"] in gloss, f"{t['id']}: нет якоря {t['gl']} в glossary.html"
        if t["href"].startswith("#"):
            assert re.search(r'\sid="%s"' % re.escape(t["href"][1:]), page), f"{t['id']}: нет {t['href']}"
        elif t["href"]:  # страница руководства: ../decibels.html#dbm
            target, _, frag = t["href"][3:].partition("#")
            assert t["href"].startswith("../") and frag in _anchors(gw.read_page(target[:-5])), t["href"]
        words += t["w"]
    assert len(words) == len(set(words))
    for must in ("ЭИИМ", "МШУ", "АЦП", "ПЧ", "IQ", "БПФ", "RBW", "VBW", "RSSI", "С/Ш", "КШ", "SINAD", "ENOB", "ЭДС",
                 "КПД", "FSPL", "ДН", "AM", "FM", "FSK", "QPSK", "DMR", "AMBE", "CACH", "ADS-B", "PPM", "IARU", "ЦАП",
                 "VFO", "PMR", "LRPT", "MRC"):
        assert must in words, must


def test_search_db_synonyms():
    """Единицы в руководстве по-английски, но поиск находит и «дБм», и «dBm»."""
    texts = {pid: gw.page_text(src) for pid, src in _all().items()}
    assert "дБ" not in " ".join(texts.values())
    for q in ("дБмкВ", "dBµV", "дБм", "dBm", "дБFS", "dbfs"):
        assert "decibels" in [pid for pid, _n, _s in gw.search(q, texts)], q
    assert gw.word_variants("дБм") == ["дбм", "dbm"]
