"""Встроенное руководство («вики») для начинающего радиолюбителя.

Страницы — HTML в rtltv/guide/*.html (UTF-8), картинки-схемы — SVG в rtltv/guide/img/ с цветами темы
вместо {{fg}}, {{accent}}… Слева дерево разделов и поиск по всем страницам, справа QTextBrowser.
show_guide(page, parent) открывает единственное окно на нужной странице («radio», «antennas#v-dipole»).
В сборке PyInstaller папку надо положить как данные: --add-data "rtltv\\guide;rtltv\\guide".
Интерактивные страницы с JavaScript лежат в подпапках (guide/pipeline/index.html): QTextBrowser их не
показывает, поэтому они открываются здесь же во встроенном браузере QtWebEngine (модуль грузится при первом
открытии схемы). Без QtWebEngine в сборке (или с RTLTV_NO_WEBENGINE=1) — во внешнем браузере как file://.
"""
import html
import logging
import os
import re
import sys

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (QColor, QDesktopServices, QImage, QKeySequence, QPainter, QShortcut, QTextCursor,
                           QTextDocument)
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                               QSplitter, QStackedWidget, QTextBrowser, QTextEdit, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import settings
from .themes import THEMES, qss

log = logging.getLogger(__name__)

# (страница, заголовок) или (заголовок группы, [страницы])
TOC = [
    ("index", "Содержание"),
    ("Первые шаги", [
        ("quickstart", "Быстрый старт"),
        ("bands", "Что можно принимать"),
        ("legal", "Закон и этика эфира"),
    ]),
    ("Основы радиоприёма", [
        ("pipeline", "Путь радиосигнала"),
        ("sdr", "Приёмники SDR"),
        ("decibels", "Единицы и децибелы"),
        ("gain", "Усиление и АРУ"),
        ("antennas", "Антенны и кабели"),
        ("modulation", "Виды модуляции"),
        ("spectrum", "Спектр и водопад"),
        ("satellites", "Доплер и спутники"),
    ]),
    ("Режимы SDR4NOOB", [
        ("mode-tv", "ТВ"),
        ("mode-radio", "Радио"),
        ("mode-sstv", "SSTV"),
        ("mode-sat", "Спутники"),
        ("mode-adsb", "Самолёты (ADS-B)"),
        ("mode-aprs", "APRS"),
    ]),
    ("Помощь", [
        ("troubleshooting", "Помехи и неполадки"),
        ("glossary", "Словарь терминов"),
    ]),
]

# окно режима → страница (если у класса нет своего GUIDE_PAGE)
MODE_PAGES = {"MainWindow": "mode-tv", "RadioWindow": "mode-radio", "SstvWindow": "mode-sstv",
              "SatWindow": "mode-sat", "AdsbWindow": "mode-adsb", "AprsWindow": "mode-aprs"}
# короткие имена, которые удобно передавать в show_guide
ALIASES = {"tv": "mode-tv", "radio": "mode-radio", "sstv": "mode-sstv", "sat": "mode-sat",
           "adsb": "mode-adsb", "aprs": "mode-aprs", "home": "index"}


def guide_dir():
    """Папка страниц: рядом с модулем; в сборке PyInstaller — внутри sys._MEIPASS."""
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guide")
    base = getattr(sys, "_MEIPASS", None)
    if base:
        frozen = os.path.join(base, "rtltv", "guide")
        if os.path.isdir(frozen):
            return frozen
    return here


def pages():
    """Все страницы по порядку оглавления: [(id, заголовок)]."""
    out = []
    for key, val in TOC:
        if isinstance(val, list):
            out.extend(val)
        else:
            out.append((key, val))
    return out


def page_title(pid):
    return dict(pages()).get(pid, pid)


def page_for(window):
    """Страница руководства для окна режима."""
    return getattr(window, "GUIDE_PAGE", None) or MODE_PAGES.get(type(window).__name__, "index")


def page_path(pid):
    return os.path.join(guide_dir(), pid + ".html")


def read_page(pid):
    with open(page_path(pid), encoding="utf-8") as f:
        return f.read()


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def page_text(src):
    """Видимый текст страницы (без разметки, svg и стилей) — для поиска."""
    src = re.sub(r"(?is)<(style|script|svg)\b.*?</\1>", " ", src)
    src = re.sub(r'(?is)<p class="crumbs">.*?</p>|<title>.*?</title>', " ", src)
    src = re.sub(r"(?is)<br\s*/?>|</(p|li|tr|td|th|h\d|div)>", " ", src)
    text = _WS.sub(" ", html.unescape(_TAG.sub(" ", src))).strip()
    return re.sub(r" ([.,;:!?»)])", r"\1", text)


# единицы в руководстве пишутся по-английски (dBm), но искать можно и по-русски (дБм) — и наоборот
_DB_PAIRS = [("дбмкв", "dbµv"), ("дбfs", "dbfs"), ("дбм", "dbm"), ("дбi", "dbi"), ("дбd", "dbd"), ("дбc", "dbc"),
             ("дб", "db")]


def word_variants(word):
    """Слово запроса и его написания с «дБ…» ↔ «dB…»: ['дбм', 'dbm']."""
    w = word.lower()
    ru2en, en2ru = w, w
    for ru, en in _DB_PAIRS:
        ru2en = ru2en.replace(ru, en)
    for ru, en in _DB_PAIRS:
        en2ru = en2ru.replace(en, ru)
    if "dbuv" in w:
        ru2en = ru2en.replace("dbuv", "dbµv")
    out = []
    for v in (w, ru2en, en2ru):
        if v not in out:
            out.append(v)
    return out


def search(query, texts=None):
    """Поиск по всем страницам: все слова запроса должны встретиться. -> [(id, число совпадений, отрывок)]."""
    groups = [word_variants(w) for w in query.lower().split() if w]
    if not groups:
        return []
    if texts is None:
        texts = {pid: page_text(read_page(pid)) for pid, _t in pages() if os.path.exists(page_path(pid))}
    found = []
    for pid, _title in pages():
        text = texts.get(pid, "")
        low = text.lower()
        if not all(any(v in low for v in vs) for vs in groups):
            continue
        n = sum(max(low.count(v) for v in vs) for vs in groups)
        first = next(v for v in groups[0] if v in low)
        i = low.find(first)
        a, b = max(0, i - 60), min(len(text), i + len(first) + 90)
        snip = ("…" if a else "") + text[a:b] + ("…" if b < len(text) else "")
        found.append((pid, n, snip))
    found.sort(key=lambda r: -r[1])
    return found


def doc_css(t):
    """Стили документа под цвета темы (QTextDocument понимает подмножество CSS)."""
    return f"""
    body {{ color: {t['fg']}; font-family: 'Segoe UI', Arial, sans-serif; }}
    a {{ color: {t['accent']}; text-decoration: none; }}
    h1 {{ color: {t['accent']}; font-size: x-large; margin-top: 4px; margin-bottom: 8px; }}
    h2 {{ color: {t['line']}; font-size: large; margin-top: 18px; margin-bottom: 6px; }}
    h3 {{ color: {t['fg']}; font-size: medium; margin-top: 14px; margin-bottom: 4px; }}
    p, li {{ margin-top: 3px; margin-bottom: 3px; }}
    table.t {{ border-collapse: collapse; border-color: {t['grid']}; border-style: solid; margin-top: 6px;
               margin-bottom: 6px; }}
    table.t th {{ background-color: {t['panel2']}; color: {t['fg']}; border-color: {t['grid']};
                  border-style: solid; padding: 4px; font-weight: bold; }}
    table.t td {{ border-color: {t['grid']}; border-style: solid; padding: 4px; }}
    table.note {{ background-color: {t['panel2']}; border-color: {t['accent']}; border-style: solid;
                  margin-top: 8px; margin-bottom: 8px; }}
    table.warn {{ background-color: {t['panel2']}; border-color: #d9822b; border-style: solid;
                  margin-top: 8px; margin-bottom: 8px; }}
    table.btn {{ background-color: {t['accent']}; margin-top: 10px; margin-bottom: 4px; }}
    table.btn a {{ color: {t['bg']}; font-size: large; }}
    code, .mono {{ font-family: Consolas, 'Courier New', monospace; color: {t['lcd']}; }}
    .dim {{ color: {t['dim']}; }}
    .k {{ font-family: Consolas, monospace; background-color: {t['panel2']}; color: {t['fg']}; }}
    .crumbs {{ color: {t['dim']}; }}
    """


def interactive_path(url):
    """Путь к интерактивной странице (HTML в подпапке руководства, например pipeline/index.html) или None.
    Такие страницы написаны для браузера (JavaScript, Canvas), а не для QTextBrowser."""
    if not url.isLocalFile():
        return None
    path = os.path.abspath(url.toLocalFile())
    if not path.lower().endswith((".html", ".htm")):
        return None
    root = os.path.normcase(os.path.abspath(guide_dir()))
    norm = os.path.normcase(path)
    if norm.startswith(root + os.sep) and os.path.dirname(norm) != root:
        return path
    return None


def open_in_browser(path):
    """Открыть локальную страницу в системном браузере (file://). Отдельная функция — чтобы подменять в тестах."""
    return QDesktopServices.openUrl(QUrl.fromLocalFile(path))


_WEB = None


def web_engine():
    """(QtWebEngineWidgets, QtWebEngineCore) или None, если модуля нет в сборке или он отключён
    RTLTV_NO_WEBENGINE=1 — тогда интерактивные страницы открываются в системном браузере."""
    global _WEB
    if os.environ.get("RTLTV_NO_WEBENGINE") == "1":
        return None
    if _WEB is None:
        try:
            from PySide6 import QtWebEngineCore, QtWebEngineWidgets
            _WEB = (QtWebEngineWidgets, QtWebEngineCore)
        except ImportError as e:  # сборка без QtWebEngine
            log.warning("QtWebEngine недоступен (%s): схема откроется в браузере", e)
            _WEB = False
    return _WEB or None


def is_dark(t):
    """Тёмная ли тема приложения (по яркости фона) — для ?theme= интерактивной страницы."""
    return QColor(t["bg"]).lightnessF() < 0.5


def web_nav(url):
    """Куда ведёт переход со встроенной интерактивной страницы:
    ("web", None) — остаётся во встроенном браузере (сама схема, её якоря, data:/about:);
    ("guide", "antennas#lna") — обычная страница руководства, открывается в окне справки;
    ("external", QUrl) — http(s)/mailto в системный браузер; ("block", None) — остальное не открывать."""
    scheme = url.scheme().lower()
    if scheme in ("http", "https", "mailto"):
        return "external", url
    if scheme in ("data", "about", "blob"):
        return "web", None
    if not url.isLocalFile():
        return "block", None
    if interactive_path(url):
        return "web", None
    path = os.path.normcase(os.path.abspath(url.toLocalFile()))
    root = os.path.normcase(os.path.abspath(guide_dir()))
    if os.path.dirname(path) == root and path.endswith(".html"):
        pid = os.path.splitext(os.path.basename(url.toLocalFile()))[0]
        return "guide", pid + ("#" + url.fragment() if url.fragment() else "")
    return "block", None


def make_web_view(owner):
    """Встроенный браузер для интерактивных страниц (QWebEngineView) или None без QtWebEngine.
    Профиль без диска (кэш и куки только в памяти); переходы на обычные страницы руководства — в окно справки."""
    mods = web_engine()
    if not mods:
        return None
    QtWebEngineWidgets, QtWebEngineCore = mods
    QWebEnginePage, QWebEngineProfile = QtWebEngineCore.QWebEnginePage, QtWebEngineCore.QWebEngineProfile

    class GuideWebPage(QWebEnginePage):
        def acceptNavigationRequest(self, url, _nav_type, is_main):
            if not is_main:
                return True
            kind, arg = web_nav(url)
            if kind == "guide":
                QTimer.singleShot(0, lambda: owner.open_page(arg))
            elif kind == "external":
                QDesktopServices.openUrl(arg)
            return kind == "web"

        def javaScriptConsoleMessage(self, level, msg, line, src):
            if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
                log.warning("схема: %s (%s:%s)", msg, os.path.basename(QUrl(src).toLocalFile() or src), line)

        def createWindow(self, _type):  # window.open и target=_blank — не плодим окна
            return None

    view = QtWebEngineWidgets.QWebEngineView()
    profile = QWebEngineProfile(view)  # без имени — off-the-record
    page = GuideWebPage(profile, view)
    WA = QtWebEngineCore.QWebEngineSettings.WebAttribute
    page.settings().setAttribute(WA.LocalContentCanAccessRemoteUrls, False)
    page.settings().setAttribute(WA.PluginsEnabled, False)
    view.setPage(page)

    def crashed(status, code):
        if status != QWebEnginePage.RenderProcessTerminationStatus.NormalTerminationStatus:
            log.warning("схема: процесс страницы завершился (%s, код %s) — перезагрузка", status, code)
            QTimer.singleShot(500, view.reload)
    page.renderProcessTerminated.connect(crashed)
    return view


def render_svg(data, t, width=0):
    """SVG со шаблонами цветов темы -> QImage. width — желаемая ширина в точках (0 — как в файле)."""
    from PySide6.QtSvg import QSvgRenderer
    txt = data
    for k, v in t.items():
        if isinstance(v, str):
            txt = txt.replace("{{" + k + "}}", v)
    r = QSvgRenderer(QByteArray(txt.encode("utf-8")))
    if not r.isValid():
        return QImage()
    size = r.defaultSize()
    if size.isEmpty():
        size = QSize(600, 300)
    if width:
        size = QSize(int(width), int(width * size.height() / max(1, size.width())))
    scale = 2  # чёткость на мониторах с масштабом 150–200 %
    img = QImage(size * scale, QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    r.render(p)
    p.end()
    img.setDevicePixelRatio(scale)
    return img


class GuideBrowser(QTextBrowser):
    """QTextBrowser, который читает страницы в UTF-8 и перекрашивает SVG-схемы под тему."""

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setOpenLinks(True)
        self.setOpenExternalLinks(True)   # http(s) — в системный браузер
        self._img_cache = {}
        self._img_cap = 0                 # ширина, под которую ужаты схемы на открытой странице
        self._reflow = QTimer(self)
        self._reflow.setSingleShot(True)
        self._reflow.setInterval(200)
        self._reflow.timeout.connect(self._refit_images)

    def img_cap(self):
        return max(280, self.viewport().width() - 40)

    def _fit_images(self, text):
        """Схемы не шире окна: QTextDocument не знает max-width, поэтому ужимаем атрибут width."""
        cap = self._img_cap = self.img_cap()
        return re.sub(r'(<img\b[^>]*\bwidth=")(\d+)"', lambda m: f'{m.group(1)}{min(int(m.group(2)), cap)}"', text)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._img_cap and "<img" in self.toHtml() and abs(self.img_cap() - self._img_cap) > 24:
            self._reflow.start()

    def _refit_images(self):
        bar = self.verticalScrollBar()
        frac = bar.value() / max(1, bar.maximum())
        self.reload()
        bar.setValue(int(frac * bar.maximum()))

    def doSetSource(self, url, kind=QTextDocument.UnknownResource):
        """Интерактивные страницы (pipeline/index.html) — во встроенный браузер окна (или внешний), остальное —
        как обычно. В историю QTextBrowser они не попадают: «Назад» со схемы возвращает на страницу со ссылкой."""
        path = interactive_path(url)
        if path:
            if os.path.exists(path):
                self.owner.show_interactive(path, url.fragment())
            return
        super().doSetSource(url, kind)

    def loadResource(self, kind, url):
        path = url.toLocalFile() if url.isLocalFile() else ""
        if not path and url.isRelative():
            path = os.path.join(guide_dir(), url.path())
        if kind == QTextDocument.HtmlResource and path.endswith(".html"):
            try:
                with open(path, encoding="utf-8") as f:
                    return self._fit_images(f.read())
            except OSError:
                name = html.escape(os.path.basename(path))
                return f"<h1>Страница не найдена</h1><p>{name}</p><p><a href='index.html'>Содержание</a></p>"
        if kind == QTextDocument.ImageResource and path.endswith(".svg"):
            key = (path, self.owner.theme_name)
            if key not in self._img_cache:
                try:
                    with open(path, encoding="utf-8") as f:
                        self._img_cache[key] = render_svg(f.read(), self.owner.theme)
                except OSError:
                    self._img_cache[key] = QImage()
            return self._img_cache[key]
        return super().loadResource(kind, url)

    def clear_cache(self):
        self._img_cache.clear()


class GuideWindow(QWidget):
    """Окно руководства: дерево разделов и поиск слева, страница справа."""

    ZOOM_MIN, ZOOM_MAX = 8, 20

    def __init__(self, page=None, parent=None):
        super().__init__(parent, Qt.Window)
        self.setAttribute(Qt.WA_QuitOnClose, False)  # не держит приложение, когда окна режимов закрыты
        self.setWindowTitle("SDR4NOOB — руководство")
        self.resize(1180, 780)
        self.st = settings.load("guide", {"zoom": 11})
        self.theme_name, self.theme = "Тёмная", THEMES["Тёмная"]
        self._texts = None
        self._query_words = []

        # ---- панель кнопок
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.b_back = QPushButton("◀ Назад")
        self.b_back.setToolTip("Назад (Alt+←)")
        self.b_fwd = QPushButton("Вперёд ▶")
        self.b_fwd.setToolTip("Вперёд (Alt+→)")
        self.b_home = QPushButton("⌂ Содержание")
        self.b_home.setToolTip("Первая страница руководства (Alt+Home)")
        self.b_zm = QPushButton("A−")
        self.b_zm.setToolTip("Мельче (Ctrl+−)")
        self.b_zp = QPushButton("A+")
        self.b_zp.setToolTip("Крупнее (Ctrl++)")
        for b in (self.b_zm, self.b_zp):
            b.setFixedWidth(40)
        self.title = QLabel("")
        self.title.setObjectName("dim")
        bar.addWidget(self.b_back)
        bar.addWidget(self.b_fwd)
        bar.addWidget(self.b_home)
        bar.addSpacing(10)
        bar.addWidget(self.title, 1)
        bar.addWidget(self.b_zm)
        bar.addWidget(self.b_zp)

        # ---- слева: поиск + дерево / результаты
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(4)
        self.q = QLineEdit()
        self.q.setPlaceholderText("Поиск по руководству (Ctrl+F)…")
        self.q.setClearButtonEnabled(True)
        lv.addWidget(self.q)
        self.stack = QStackedWidget()
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.items = {}
        for key, val in TOC:
            if isinstance(val, list):
                grp = QTreeWidgetItem([key])
                grp.setFlags(Qt.ItemIsEnabled)
                f = grp.font(0)
                f.setBold(True)
                grp.setFont(0, f)
                self.tree.addTopLevelItem(grp)
                for pid, title in val:
                    it = QTreeWidgetItem([title])
                    it.setData(0, Qt.UserRole, pid)
                    grp.addChild(it)
                    self.items[pid] = it
                grp.setExpanded(True)
            else:
                it = QTreeWidgetItem([val])
                it.setData(0, Qt.UserRole, key)
                self.tree.addTopLevelItem(it)
                self.items[key] = it
        self.results = QListWidget()
        self.results.setWordWrap(True)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.stack.addWidget(self.tree)
        self.stack.addWidget(self.results)
        lv.addWidget(self.stack, 1)
        self.found_lbl = QLabel("")
        self.found_lbl.setObjectName("dim")
        self.found_lbl.setWordWrap(True)
        lv.addWidget(self.found_lbl)

        self.view = GuideBrowser(self)
        self.view.setSearchPaths([guide_dir()])
        # справа: страница руководства или интерактивная схема (QWebEngineView создаётся при первом открытии)
        self.web = None
        self._web_theme = None
        self._web_fwd = False      # «Вперёд» со страницы вернёт на схему, с которой ушли «Назад»
        self._web_from = None      # страница, с которой открыта схема
        self.pages = QStackedWidget()
        self.pages.addWidget(self.view)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(self.pages)
        split.setStretchFactor(1, 1)
        split.setSizes([270, 900])
        split.setChildrenCollapsible(False)
        left.setMinimumWidth(200)

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 8)
        v.setSpacing(6)
        v.addLayout(bar)
        v.addWidget(split, 1)

        # ---- сигналы
        self.b_back.clicked.connect(self.go_back)
        self.b_fwd.clicked.connect(self.go_forward)
        self.b_home.clicked.connect(lambda: self.open_page("index"))
        self.b_zm.clicked.connect(lambda: self.zoom(-1))
        self.b_zp.clicked.connect(lambda: self.zoom(+1))
        self.view.backwardAvailable.connect(lambda _on: self._sync_nav())
        self.view.forwardAvailable.connect(lambda _on: self._sync_nav())
        self.view.sourceChanged.connect(self._source_changed)
        self.tree.itemClicked.connect(self._tree_clicked)
        self.tree.itemActivated.connect(self._tree_clicked)
        self.q.textChanged.connect(self._search)
        self.q.returnPressed.connect(self._open_first_result)
        self.results.itemClicked.connect(self._result_clicked)
        self.results.itemActivated.connect(self._result_clicked)
        self.b_back.setEnabled(False)
        self.b_fwd.setEnabled(False)
        for key, fn in (("Alt+Left", self.go_back), ("Alt+Right", self.go_forward),
                        ("Alt+Home", lambda: self.open_page("index")), ("Ctrl+F", self._focus_search),
                        ("Ctrl+=", lambda: self.zoom(+1)), ("Ctrl++", lambda: self.zoom(+1)),
                        ("Ctrl+-", lambda: self.zoom(-1)), ("F3", self._find_next), ("Escape", self._escape)):
            QShortcut(QKeySequence(key), self, activated=fn)

        self.apply_theme()
        self._apply_zoom()
        self.open_page(page or "index")

    # ---------- оформление ----------
    def apply_theme(self, name=None):
        name = name or settings.load("app", {}).get("theme") or "Тёмная"
        self.theme_name = name if name in THEMES else "Тёмная"
        t = self.theme = THEMES[self.theme_name]
        self.setStyleSheet(qss(t) + f"""
            QTextBrowser {{ background: {t['panel']}; border: 1px solid {t['grid']}; border-radius: 4px;
                           padding: 6px 10px; }}
            QTreeWidget, QListWidget {{ background: {t['panel']}; border: 1px solid {t['grid']}; }}
            QTreeWidget::item {{ padding: 3px 2px; }}
            QListWidget::item {{ padding: 4px 2px; border-bottom: 1px solid {t['grid']}; }}
            QTreeWidget::item:selected {{ background: {t['accent']}; color: {t['bg']}; }}
        """)
        self.view.clear_cache()
        self.view.document().setDefaultStyleSheet(doc_css(t))
        if not self.view.source().isEmpty():
            self.view.reload()
        if self.web is not None:
            self.web.page().setBackgroundColor(QColor(t["bg"]))
            if self._web_theme is not None and self._web_theme != is_dark(t) and not self.web.url().isEmpty():
                url = self.web.url()
                url.setQuery(self._theme_query())
                self.web.load(url)

    def zoom(self, d):
        self.st["zoom"] = max(self.ZOOM_MIN, min(self.ZOOM_MAX, int(self.st.get("zoom", 11)) + d))
        settings.save("guide", self.st)
        self._apply_zoom()

    def _apply_zoom(self):
        f = self.view.font()
        f.setPointSizeF(float(self.st.get("zoom", 11)))
        self.view.setFont(f)
        self.view.document().setDefaultFont(f)
        if self.web is not None:
            self.web.setZoomFactor(float(self.st.get("zoom", 11)) / 11)

    # ---------- интерактивная схема ----------
    def _theme_query(self):
        self._web_theme = is_dark(self.theme)
        return "theme=" + ("dark" if self._web_theme else "light")

    def web_shown(self):
        return self.web is not None and self.pages.currentWidget() is self.web

    def show_interactive(self, path, frag=""):
        """Открыть интерактивную страницу (pipeline/index.html) справа вместо текста. Та же страница не
        перезагружается — положение и ползунки сохраняются; без QtWebEngine — системный браузер."""
        if self.web is None:
            self.web = make_web_view(self)
            if self.web is None:
                return open_in_browser(path)
            self.web.page().setBackgroundColor(QColor(self.theme["bg"]))
            self.web.setZoomFactor(float(self.st.get("zoom", 11)) / 11)
            self.pages.addWidget(self.web)
        url = QUrl.fromLocalFile(path)
        url.setQuery(self._theme_query())
        cur = self.web.url()
        same = (cur.isLocalFile() and os.path.normcase(cur.toLocalFile()) == os.path.normcase(url.toLocalFile())
                and cur.query() == url.query())
        if frag:
            url.setFragment(frag)
        if not same:
            self.web.load(url)
        elif frag:  # блок схемы по якорю: страница сама откроет его и прокрутит (обработчик hashchange)
            self.web.page().runJavaScript("location.hash = " + repr("#" + frag))
        self._web_fwd = False
        self._web_from = self.current_page()
        self._set_web_active(True)
        self.pages.setCurrentWidget(self.web)
        self._web_title()
        self._sync_nav()
        self.raise_()
        self.activateWindow()
        return True

    def _web_title(self):
        title = page_title(self.current_page())
        self.title.setText(f"{title} — интерактивная схема")
        self.setWindowTitle(f"SDR4NOOB — руководство: {title} (схема)")

    def _set_web_active(self, on):
        """Скрытая схема не тратит процессор и молчит: таймеры и анимации заморожены, звук выключен."""
        if self.web is None:
            return
        from PySide6.QtWebEngineCore import QWebEnginePage
        page = self.web.page()
        page.setAudioMuted(not on)
        if on:
            page.setLifecycleState(QWebEnginePage.LifecycleState.Active)
        elif not self.web.isVisible() and page.lifecycleState() == QWebEnginePage.LifecycleState.Active:
            page.setLifecycleState(QWebEnginePage.LifecycleState.Frozen)

    def _show_text(self):
        if self.web_shown():
            self.pages.setCurrentWidget(self.view)
            self._set_web_active(False)
            self._source_changed(self.view.source())
        self._sync_nav()

    def go_back(self):
        """«Назад»: со схемы — на страницу, откуда её открыли; иначе — по истории страниц."""
        if self.web_shown():
            self._show_text()
            self._web_fwd = True
            self._sync_nav()
        else:
            self.view.backward()

    def go_forward(self):
        if not self.web_shown() and self._web_fwd and not self.view.isForwardAvailable():
            self._web_fwd = False
            self._set_web_active(True)
            self.pages.setCurrentWidget(self.web)
            self._web_title()
            self._sync_nav()
        else:
            self.view.forward()

    def _sync_nav(self):
        web = self.web_shown()
        self.b_back.setEnabled(web or self.view.isBackwardAvailable())
        self.b_fwd.setEnabled(not web and (self.view.isForwardAvailable() or self._web_fwd))

    def hideEvent(self, e):
        super().hideEvent(e)
        self._set_web_active(False)

    def showEvent(self, e):
        super().showEvent(e)
        if self.web_shown():
            self._set_web_active(True)

    # ---------- навигация ----------
    def open_page(self, page):
        """page: «antennas», «antennas#v-dipole», «antennas.html#v-dipole» или «radio» (псевдоним)."""
        pid, _, frag = str(page or "index").partition("#")
        pid = pid[:-5] if pid.endswith(".html") else pid
        pid = ALIASES.get(pid, pid)
        if not os.path.exists(page_path(pid)):
            pid = "index"
        url = QUrl.fromLocalFile(page_path(pid))
        if frag:
            url.setFragment(frag)
        self._show_text()
        self._web_fwd = False
        self.view.setSource(url)
        self.raise_()
        self.activateWindow()

    def current_page(self):
        return self._pid_of(self.view.source())

    @staticmethod
    def _pid_of(url):
        return os.path.splitext(os.path.basename(url.toLocalFile()))[0]

    def _source_changed(self, url):
        # source() при переходе на якорь другой страницы обновляется позже сигнала — берём url из сигнала
        pid = self._pid_of(url)
        if self._web_fwd and pid != self._web_from:  # ушли по ссылке на другую страницу — «Вперёд» уже не на схему
            self._web_fwd = False
            self._sync_nav()
        if self.web_shown():
            self._web_title()
        else:
            self.title.setText(page_title(pid))
            self.setWindowTitle(f"SDR4NOOB — руководство: {page_title(pid)}")
        it = self.items.get(pid)
        if it is not None:
            self.tree.blockSignals(True)
            self.tree.setCurrentItem(it)
            self.tree.blockSignals(False)
        self._highlight()

    def _tree_clicked(self, item, _col=0):
        pid = item.data(0, Qt.UserRole)
        if pid:
            self.open_page(pid)

    # ---------- поиск ----------
    def texts(self):
        if self._texts is None:
            self._texts = {}
            for pid, _t in pages():
                try:
                    self._texts[pid] = page_text(read_page(pid))
                except OSError:
                    self._texts[pid] = ""
        return self._texts

    def _focus_search(self):
        self.q.setFocus()
        self.q.selectAll()

    def _escape(self):
        if self.q.text():
            self.q.clear()
        else:
            self.close()

    def _search(self, text):
        text = text.strip()
        self._query_words = [v for w in text.lower().split() if w for v in word_variants(w)]
        self.results.clear()
        if len(text) < 2:
            self.stack.setCurrentWidget(self.tree)
            self.found_lbl.setText("")
            self._highlight()
            return
        found = search(text, self.texts())
        for pid, n, snip in found:
            it = QListWidgetItem(f"{page_title(pid)}  ({n})\n{snip}")
            it.setData(Qt.UserRole, pid)
            it.setToolTip(snip)
            self.results.addItem(it)
        self.stack.setCurrentWidget(self.results)
        self.found_lbl.setText(f"Найдено страниц: {len(found)}" if found else "Ничего не найдено")
        self._highlight()

    def _open_first_result(self):
        if self.results.count():
            self._result_clicked(self.results.item(0))

    def _result_clicked(self, item):
        pid = item.data(Qt.UserRole)
        if pid != self.current_page():
            self.open_page(pid)
        else:
            self._highlight()
        self._find_next(from_top=True)

    def _highlight(self):
        """Подсветить слова запроса на открытой странице."""
        sels = []
        if self._query_words:
            doc = self.view.document()
            col = QColor(self.theme["accent"])
            col.setAlpha(110)
            for w in self._query_words:
                cur = QTextCursor(doc)
                while True:
                    cur = doc.find(w, cur)
                    if cur.isNull():
                        break
                    s = QTextEdit.ExtraSelection()
                    s.cursor = cur
                    s.format.setBackground(col)
                    sels.append(s)
                    if len(sels) > 500:
                        break
        self.view.setExtraSelections(sels)
        return len(sels)

    def _find_next(self, from_top=False):
        if not self._query_words:
            return False
        if from_top:
            self.view.moveCursor(QTextCursor.Start)
        for w in self._query_words:  # первое слово запроса в любом из написаний (дБм / dBm)
            if self.view.find(w):
                return True
        self.view.moveCursor(QTextCursor.Start)
        return any(self.view.find(w) for w in self._query_words)


_window = None


def selftest_interactive(timeout=40.0):
    """Проверка сборки (SDR4NOOB.exe --selftest-guide): схема грузится во встроенном браузере и её JavaScript
    запускается. -> код выхода: 0 — работает, 1 — нет QtWebEngine или страница не ожила за timeout с."""
    import time
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    path = os.path.join(guide_dir(), "pipeline", "index.html")
    w = GuideWindow("pipeline")
    if not w.show_interactive(path) or w.web is None:
        log.error("самопроверка справки: QtWebEngine недоступен")
        return 1
    box = []
    end = time.time() + timeout
    while time.time() < end and box != [True]:
        box.clear()
        w.web.page().runJavaScript("!!(window.SDR4NOOB_PIPELINE && document.documentElement.getAttribute('data-theme'))",
                                   0, box.append)
        t = time.time() + 0.5
        while time.time() < t and not box:
            app.processEvents()
            time.sleep(0.01)
    ok = box == [True]
    (log.info if ok else log.error)("самопроверка справки: схема %s", "работает" if ok else "не загрузилась")
    w.close()
    w.deleteLater()
    return 0 if ok else 1


def show_guide(page=None, parent=None):
    """Открыть руководство (одно окно на приложение) на странице page. Возвращает окно."""
    global _window
    w = _window
    try:
        if w is not None:
            w.isVisible()  # объект мог быть удалён Qt
    except RuntimeError:
        w = None
    if w is None:
        w = _window = GuideWindow(page)
        w.destroyed.connect(_forget)
        if parent is not None:
            try:
                g = parent.frameGeometry()
                w.move(g.center().x() - w.width() // 2, max(0, g.center().y() - w.height() // 2))
            except Exception:  # noqa: BLE001
                pass
    else:
        w.apply_theme()
        if page:
            w.open_page(page)
    w.show()
    w.raise_()
    w.activateWindow()
    return w


def _forget(*_a):
    global _window
    _window = None
