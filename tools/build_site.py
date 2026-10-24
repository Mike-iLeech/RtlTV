"""Генератор сайта SDR4NOOB для GitHub Pages (папка docs/ ветки main → https://mike-ileech.github.io/SDR4NOOB/).

Источник — встроенное руководство rtltv/guide/*.html и схемы rtltv/guide/img/*.svg; порядок и группы
разделов берутся из TOC в rtltv/guide_window.py. Сайт целиком статический и локальный: без CDN, шрифтов
и счётчиков; поиск — на клиенте по JS-индексу assets/search-index.js.

    py -3.12 tools/build_site.py                  собрать docs/
    py -3.12 tools/build_site.py --check          собрать и проверить ссылки/якоря/картинки
    py -3.12 tools/build_site.py --screens DIR    перед сборкой ужать снимки окон DIR/*.png
                                                  в docs/img/screens/*.webp (нужен Pillow)

Генератор владеет docs/index.html, install.html, 404.html, sitemap.xml, .nojekyll, docs/guide/ и
docs/assets/ — их он пересоздаёт; docs/img/ не трогает (кроме --screens).
Схемы SVG перекрашиваются под тему: img/x.svg (светлая) и img/x-dark.svg (тёмная, через <picture>).
Интерактивные страницы — подпапки руководства с index.html (rtltv/guide/pipeline/) — копируются как есть.
"""
import argparse
import ast
import html
import json
import os
import re
import shutil
import struct
import sys
from urllib.parse import unquote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDE = os.path.join(ROOT, "rtltv", "guide")
DOCS = os.path.join(ROOT, "docs")

REPO = "Mike-iLeech/SDR4NOOB"
REPO_URL = f"https://github.com/{REPO}"
DOWNLOAD_URL = f"{REPO_URL}/releases/latest"
SITE_URL = "https://mike-ileech.github.io/SDR4NOOB/"
SITE_PATH = "/SDR4NOOB/"           # путь сайта на github.io — для 404.html, которую отдают с любого адреса

OWNED = ["index.html", "install.html", "404.html", "sitemap.xml", ".nojekyll", "guide", "assets"]
SCREEN_MAX_BYTES = 300 * 1024


# ---------------------------------------------------------------- данные из кода приложения

def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def app_version():
    m = re.search(r'__version__\s*=\s*"([^"]+)"', read(os.path.join(ROOT, "rtltv", "__init__.py")))
    return m.group(1) if m else "?"


def guide_toc():
    """TOC из rtltv/guide_window.py без импорта Qt: [(id, заголовок) | (группа, [(id, заголовок)])]."""
    tree = ast.parse(read(os.path.join(ROOT, "rtltv", "guide_window.py")))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "TOC" for t in node.targets):
            return ast.literal_eval(node.value)
    raise RuntimeError("TOC не найден в guide_window.py")


def guide_pages(toc):
    out = []
    for key, val in toc:
        out.extend(val if isinstance(val, list) else [(key, val)])
    return out


def interactive_dirs():
    """Подпапки руководства с самостоятельными интерактивными страницами (index.html внутри), кроме img."""
    return sorted(d for d in os.listdir(GUIDE) if d != "img" and os.path.isfile(os.path.join(GUIDE, d, "index.html")))


def theme_colors(name):
    """Цвета темы приложения (rtltv/themes.py) — для перекраски схем: {fg: '#..', ...}."""
    src = read(os.path.join(ROOT, "rtltv", "themes.py"))
    i = src.index(f'"{name}": dict(')
    j = src.index("cmap=", i)
    return dict(re.findall(r'(\w+)="(#[0-9a-fA-F]{6})"', src[i:j]))


# ---------------------------------------------------------------- размеры картинок без Pillow

def image_size(path):
    """(ширина, высота) PNG/WebP/JPEG или None."""
    try:
        with open(path, "rb") as f:
            d = f.read(64 * 1024)
    except OSError:
        return None
    if d[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", d[16:24])
    if d[:4] == b"RIFF" and d[8:12] == b"WEBP":
        kind = d[12:16]
        if kind == b"VP8 ":
            w, h = struct.unpack("<HH", d[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if kind == b"VP8L":
            b = int.from_bytes(d[21:25], "little")
            return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
        if kind == b"VP8X":
            return int.from_bytes(d[24:27], "little") + 1, int.from_bytes(d[27:30], "little") + 1
    if d[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(d):
            if d[i] != 0xFF:
                i += 1
                continue
            marker, seg = d[i + 1], struct.unpack(">H", d[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", d[i + 5:i + 9])
                return w, h
            i += 2 + seg
    return None


# ---------------------------------------------------------------- текст для поиска

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def plain_text(src):
    src = re.sub(r"(?is)<(style|script|svg|nav|header|footer)\b.*?</\1>", " ", src)
    src = re.sub(r'(?is)<p class="crumbs">.*?</p>|<title>.*?</title>|<a class="anchor"[^>]*>.*?</a>', " ", src)
    src = re.sub(r"(?is)<br\s*/?>|</(p|li|tr|td|th|h\d|div|figcaption)>", " ", src)
    text = _WS.sub(" ", html.unescape(_TAG.sub(" ", src))).strip()
    return re.sub(r" ([.,;:!?»)])", r"\1", text)


# ---------------------------------------------------------------- страницы руководства → сайт

# Места руководства, написанные для окна программы или под конкретный компьютер: на сайте — свой текст.
SITE_FIXES = {
    "quickstart": [
        ("— на этом компьютере это уже сделано.",
         '— как это сделать, написано в разделе <a href="../install.html#zadig">«Установка и обновление»</a>.'),
    ],
    "index": [
        ("или через поле поиска слева (Ctrl+F)", "или через поиск вверху страницы"),
        (re.compile(r"(?s)<h2>Как пользоваться этим окном</h2>.*?</ul>"),
         "<h2>Руководство в программе</h2>\n<ul>\n"
         "<li>Это же руководство встроено в SDR4NOOB: кнопка «Руководство» на экране выбора режима, меню "
         "«Справка → Руководство», а <b>F1</b> в окне режима открывает страницу этого режима (в «Радио» F1 — "
         "краткая справка).</li>\n"
         "<li>В окне руководства: <span class=\"k\">Alt+←</span> / <span class=\"k\">Alt+→</span> — назад и "
         "вперёд, <span class=\"k\">Ctrl+F</span> — поиск по всем страницам, <span class=\"k\">F3</span> — "
         "следующее совпадение.</li>\n"
         "<li>На сайте поиск — вверху страницы (клавиша <span class=\"k\">/</span>).</li>\n</ul>"),
    ],
}


def _callout(m):
    return f'<div class="callout {m.group(1)}">{m.group(2).strip()}</div>'


def _cell_width(m):
    return f'<{m.group(1)}{m.group(2)} style="width:{m.group(3)}"'


def _heading_anchor(m):
    tag, hid, inner = m.group(1), m.group(2), m.group(3)
    return (f'<{tag} id="{hid}">{inner}<a class="anchor" href="#{hid}" '
            f'aria-label="Ссылка на этот раздел">#</a></{tag}>')


def _ext_link(m):
    tag = m.group(0)
    if "target=" in tag:
        return tag
    return tag[:-1] + ' target="_blank" rel="noopener">'


def _picture(m):
    attrs = m.group(1)
    src = re.search(r'src="img/([^"]+)\.svg"', attrs)
    if not src:
        return m.group(0)
    name = src.group(1)
    w = re.search(r'width="(\d+)"', attrs)
    size = ""
    svg = os.path.join(GUIDE, "img", name + ".svg")
    vb = re.search(r'viewBox="[\d.]+ [\d.]+ ([\d.]+) ([\d.]+)"', read(svg)) if os.path.exists(svg) else None
    if vb:
        vw, vh = float(vb.group(1)), float(vb.group(2))
        ww = int(w.group(1)) if w else int(vw)
        size = f' width="{ww}" height="{round(ww * vh / vw)}"'
    alt = re.search(r'alt="([^"]*)"', attrs)
    alt = alt.group(1) if alt else "Схема"
    return (f'<figure class="diagram"><picture>'
            f'<source srcset="img/{name}-dark.svg" media="(prefers-color-scheme: dark)">'
            f'<img src="img/{name}.svg" alt="{html.escape(alt)}"{size} loading="lazy"></picture></figure>')


def convert_guide(pid, src):
    """HTML страницы руководства (для QTextBrowser) → (заголовок, тело для сайта)."""
    title = html.unescape(re.search(r"(?s)<title>(.*?)</title>", src).group(1).strip())
    body = re.search(r"(?s)<body[^>]*>(.*)</body>", src).group(1).strip()
    for old, new in SITE_FIXES.get(pid, []):
        if isinstance(old, re.Pattern):
            body, n = old.subn(new, body)
        else:
            n = body.count(old)
            body = body.replace(old, new)
        if not n:
            print(f"предупреждение: {pid}: не найдено место для правки сайта: {str(old)[:60]!r}", file=sys.stderr)
    # якоря: <h2><a name="x"></a>Текст</h2> → <h2 id="x">Текст <a class="anchor" href="#x">#</a></h2>
    body = re.sub(r'(?s)<(h[1-6])><a name="([^"]+)"></a>(.*?)</\1>', _heading_anchor, body)
    body = re.sub(r'<a name="([^"]+)">', r'<a id="\1">', body)
    # кнопка-ссылка (таблица из одной ячейки с классом btn) → обычная кнопка сайта
    body = re.sub(r'(?s)<table class="btn"[^>]*>\s*<tr>\s*<td>\s*<a ([^>]*)>(.*?)</a>\s*</td>\s*</tr>\s*</table>',
                  r'<p class="cta"><a class="btn big" \1>\2</a></p>', body)
    # врезки-таблицы из одной ячейки → блоки
    body = re.sub(r'(?s)<table class="(note|warn)"[^>]*>\s*<tr>\s*<td>(.*?)</td>\s*</tr>\s*</table>',
                  _callout, body)
    # таблицы: без атрибутов оформления, в обёртке с прокруткой на телефоне
    body = re.sub(r'<table class="t"[^>]*>', '<div class="tw"><table class="t">', body)
    body = re.sub(r'(?s)(<div class="tw"><table class="t">.*?</table>)', r"\1</div>", body)
    body = re.sub(r'<(th|td)([^>]*?) width="(\d+%)"', _cell_width, body)
    body = re.sub(r"<img\b([^>]*)>", _picture, body)
    body = re.sub(r'<a\s[^>]*href="https?://[^"]*"[^>]*>', _ext_link, body)
    return title, body


def svg_variants(name, light, dark):
    data = read(os.path.join(GUIDE, "img", name))
    out = {}
    for suffix, t in (("", light), ("-dark", dark)):
        txt = data
        for k, v in t.items():
            txt = txt.replace("{{" + k + "}}", v)
        left = re.findall(r"\{\{\w+\}\}", txt)
        if left:
            raise RuntimeError(f"{name}: нет цвета для {sorted(set(left))}")
        out[name[:-4] + suffix + ".svg"] = txt
    return out


# ---------------------------------------------------------------- общий шаблон

# логотип — один на всё: rtltv/assets/sdr4noob.svg (из него же иконка EXE, см. tools/make_icon.py)
LOGO_DIR = os.path.join(ROOT, "rtltv", "assets")
ICON_SVG = open(os.path.join(LOGO_DIR, "sdr4noob.svg"), encoding="utf-8").read()



def nav_tree(toc):
    """[(группа, [(путь от корня сайта, заголовок)])] — меню слева."""
    groups = [("SDR4NOOB", [("index.html", "Главная"), ("install.html", "Установка и обновление")])]
    for key, val in toc:
        if isinstance(val, list):
            groups.append((key, [(f"guide/{pid}.html", t) for pid, t in val]))
        else:
            groups[0][1].append((f"guide/{key}.html", "Руководство: " + val.lower()))
    return groups


def render_nav(groups, current, root):
    parts = []
    for name, items in groups:
        lis = []
        for path, title in items:
            cur = ' aria-current="page" class="cur"' if path == current else ""
            lis.append(f'<li><a href="{root}{path}"{cur}>{html.escape(title)}</a></li>')
        parts.append(f'<div class="nav-group"><h2>{html.escape(name)}</h2><ul>{"".join(lis)}</ul></div>')
    return "\n".join(parts)


def page(*, title, body, current, root, groups, version, description="", prevnext=None, home=False):
    full_title = "SDR4NOOB — всё в одном для начинающего радиолюбителя" if home else f"{title} — SDR4NOOB"
    desc = html.escape(description or "SDR4NOOB — всё в одном для начинающего радиолюбителя: бесплатный приёмник на RTL-SDR для Windows: ТВ, радио, "
                                      "SSTV, спутники, самолёты ADS-B, APRS.")
    pn = ""
    if prevnext:
        prev, nxt = prevnext
        a = (f'<a class="prev" href="{root}{prev[0]}"><span>← Назад</span>{html.escape(prev[1])}</a>'
             if prev else "<span></span>")
        b = (f'<a class="next" href="{root}{nxt[0]}"><span>Дальше →</span>{html.escape(nxt[1])}</a>'
             if nxt else "<span></span>")
        pn = f'<nav class="prevnext" aria-label="Соседние страницы">{a}{b}</nav>'
    canonical = SITE_URL + ("" if current == "index.html" else current)
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(full_title)}</title>
<meta name="description" content="{desc}">
<meta name="color-scheme" content="light dark">
<link rel="canonical" href="{canonical}">
<link rel="icon" href="{root}assets/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="{root}assets/sdr4noob-256.png">
<meta property="og:title" content="{html.escape(full_title)}">
<meta property="og:description" content="{desc}">
<meta property="og:image" content="{SITE_URL}assets/sdr4noob-256.png">
<link rel="stylesheet" href="{root}assets/style.css">
<script src="{root}assets/search-index.js" defer></script>
<script src="{root}assets/site.js" defer></script>
</head>
<body data-root="{root}"{' class="home"' if home else ''}>
<a class="skip" href="#content">К содержимому</a>
<header class="top">
  <button class="menu-btn" type="button" aria-controls="sidebar" aria-expanded="false" aria-label="Разделы">☰</button>
  <a class="brand" href="{root}index.html"><img src="{root}assets/favicon.svg" alt="" width="28" height="28">SDR4NOOB<small>{version}</small></a>
  <div class="search" role="search">
    <input id="q" type="search" placeholder="Поиск по сайту  ( / )" autocomplete="off" aria-label="Поиск по сайту">
    <div id="results" class="results" role="listbox" hidden></div>
  </div>
  <nav class="links" aria-label="Ссылки">
    <a href="{root}guide/index.html">Руководство</a>
    <a href="{REPO_URL}">GitHub</a>
    <a class="btn" href="{DOWNLOAD_URL}">Скачать</a>
  </nav>
</header>
<div class="layout">
<aside id="sidebar" class="sidebar" aria-label="Разделы сайта">
{render_nav(groups, current, root)}
</aside>
<main id="content" class="content">
{body}
{pn}
</main>
</div>
<footer class="foot">
  <p>SDR4NOOB {version} · свободная программа под лицензией <a href="{REPO_URL}/blob/main/LICENSE">GPL-3.0-or-later</a> ·
  <a href="{REPO_URL}/blob/main/THIRD_PARTY.md">сторонние компоненты</a> ·
  <a href="{REPO_URL}">исходный код</a> · <a href="{REPO_URL}/issues">сообщить об ошибке</a></p>
  <p>Карты на снимках экрана — © участники OpenStreetMap (ODbL).</p>
</footer>
</body>
</html>
"""


# ---------------------------------------------------------------- собственные страницы сайта

def fig(name, caption, root=""):
    """Снимок окна из docs/img/screens с подписью; щелчок открывает полный размер."""
    path = os.path.join(DOCS, "img", "screens", name + ".webp")
    size = image_size(path)
    wh = f' width="{size[0]}" height="{size[1]}"' if size else ""
    if not size:
        print(f"предупреждение: нет снимка {path}", file=sys.stderr)
    src = f"{root}img/screens/{name}.webp"
    return (f'<figure class="shot"><a href="{src}"><img src="{src}" alt="{html.escape(caption)}"{wh} '
            f'loading="lazy"></a><figcaption>{caption}</figcaption></figure>')


def home_body():
    g = "guide/"
    return f"""
<section class="hero">
  <div>
    <h1 class="logo-title"><img src="assets/favicon.svg" alt="" width="64" height="64">SDR4NOOB</h1>
    <p class="tagline">Всё в одном для начинающего радиолюбителя</p>
    <p class="lead">Бесплатная программа для Windows, которая превращает недорогой USB-донгл RTL-SDR
    в радиоприёмник, ТВ-измеритель, станцию приёма снимков со спутников и локатор самолётов.
    Всё в одном окне, по-русски, со встроенным драйвером и подробным руководством для начинающих.</p>
    <p class="cta"><a class="btn big" href="{DOWNLOAD_URL}">Скачать для Windows</a>
    <a class="btn ghost big" href="install.html">Как установить</a>
    <a class="btn ghost big" href="{g}quickstart.html">Быстрый старт</a></p>
    <p class="dim">Windows 10/11 x64 · RTL-SDR Blog V3/V4 и другие донглы на RTL2832U · без антенны есть
    тестовый эфир для знакомства</p>
  </div>
  {fig("launcher", "Экран выбора режима. Галочка «Без антенны: тестовый эфир» включает синтетические сигналы для всех режимов.")}
</section>

<h2 id="modes">Возможности по режимам<a class="anchor" href="#modes" aria-label="Ссылка на этот раздел">#</a></h2>

<section class="mode">
<h3 id="radio">Радио</h3>
<p>Универсальный приёмник в духе SDR# и SDR++: спектр и водопад всей полосы с масштабом, настройка щелчком,
демодуляторы <b>WFM-стерео с RDS</b>, NFM, AM, USB/LSB, CW и цифровой <b>DMR с голосом</b> (номера абонентов,
слоты, цветовой код, вокодер AMBE+2 через mbelib). АРУ, шумодав, шумоподавитель звука, бланкер помех,
<b>поиск DMR</b> по диапазонам, <b>менеджер частот</b> (импорт CSV и Frequencies.xml из SDR#), запись звука в WAV,
запись и воспроизведение <b>базовой полосы IQ</b> (WAV из SDR#/SDR++, cu8 из rtl_sdr).</p>
<div class="shots two">
{fig("radio-wfm", "WFM-стерео: станция тестового эфира, RDS — название и радиотекст.")}
{fig("radio-dmr", "DMR: слоты, кто кому говорит, голос; слева — карточки настроек.")}
</div>
<p><a href="{g}mode-radio.html">Подробно о режиме «Радио» →</a></p>
</section>

<section class="mode">
<h3 id="sat">Спутники</h3>
<p>Прогноз пролётов по TLE (SGP4) на карте и в таблице с фильтрами, приём снимков <b>«Метеор-М» №2-3/№2-4</b>
(LRPT 137,9 МГц, OQPSK 72k и 80k с перемежением — режим определяется сам) с композитами и
<b>автоприёмом по расписанию</b>, запись и декодирование IQ-файлов, снимки геостационарных <b>«Электро-Л»</b>
(LRIT 1691 МГц), прослушивание <b>МКС</b> и любительских спутников с автоматической коррекцией доплера,
<b>свои спутники</b> по номеру NORAD и галерея принятых снимков.</p>
<div class="shots two">
{fig("sat-map", "Карта пролётов, зона видимости и таблица ближайших пролётов.")}
{fig("sat-rx", "Приём LRPT: спектр, водопад, созвездие QPSK, захват несущей и синхронизация кадров.")}
{fig("sat-listen", "«Слушать»: любительский спутник с доплеровской подстройкой частоты.")}
</div>
<p><a href="{g}mode-sat.html">Подробно о режиме «Спутники» →</a> ·
<a href="{g}satellites.html#meteor">«Метеор» пошагово →</a></p>
</section>

<section class="mode">
<h3 id="adsb">Самолёты (ADS-B)</h3>
<p>Приём 1090 МГц: борта на карте OpenStreetMap с курсом, высотой и следами, таблица «Борта сейчас», расшифровка
каждого сообщения, история сеанса, карточка борта с флагом страны и сведениями о типе и операторе.
Отдельный список бортов <b>без координат</b> (при подавлении GNSS) и отбраковка подменённых позиций.
Журнал в CSV/KML.</p>
<div class="shots one">{fig("adsb", "Самолёты на карте (демо-генератор): кольца дальности, список «Без координат», таблица бортов.")}</div>
<p><a href="{g}mode-adsb.html">Подробно о режиме ADS-B →</a></p>
</section>

<section class="mode">
<h3 id="tv">ТВ</h3>
<p>Два прибора в одном окне. <b>Аналоговое ТВ</b> (SECAM/PAL D/K, PAL B/G, PAL I, NTSC M; сетки каналов
России, Европы, Великобритании и США) — чёрно-белая картинка или звук, 9 палитр, снимок кадра, поиск каналов.
<b>Измеритель мультиплексов DVB-T/T2</b> для наведения антенны: канал собирается из пяти перестроек,
считаются уровень, C/N и неравномерность, по циклическому префиксу определяются режим FFT и защитный интервал.</p>
<div class="shots two">
{fig("tv-analog", "Аналоговое ТВ: испытательная таблица тестового генератора.")}
{fig("tv-digital", "Измеритель DVB-T2: «стол» мультиплекса, C/N, режим 32K, GI 1/16.")}
{fig("tv-digital-real", "Реальный эфир: мультиплекс на ДМВ 34 (578 МГц), RTL-SDR Blog V4.")}
{fig("tv-retro", "Одна из пяти цветовых схем — «Ретро (янтарь)».")}
</div>
<p><a href="{g}mode-tv.html">Подробно о режиме «ТВ» →</a></p>
</section>

<section class="mode">
<h3 id="sstv">SSTV</h3>
<p>Картинки, которые радиолюбители передают звуком: Robot 36/72, Martin M1/M2, Scottie S1/S2/DX, PD 50–290,
Wraase SC2-180. Режим определяется по VIS-коду, наклон исправляется автоматически, картинка рисуется строка
за строкой; галерея принятых и декодирование записанных WAV-файлов. Частоты КВ (14,230 МГц) и МКС (145,800 МГц).</p>
<div class="shots one">{fig("sstv", "Приём Robot 36 на тестовом эфире: картинка, режим, строка, тон.")}</div>
<p><a href="{g}mode-sstv.html">Подробно о режиме SSTV →</a></p>
</section>

<section class="mode">
<h3 id="aprs">APRS</h3>
<p>Пакеты радиолюбителей (AFSK 1200 бод) на карте: станции, треки, метеостанции, лента пакетов с расшифровкой,
фильтры, журнал. Наземная сеть 144,800 МГц и ретранслятор МКС 145,825 МГц с коррекцией доплера.</p>
<div class="shots one">{fig("aprs", "Станции APRS на карте и список пакетов (тестовый эфир).")}</div>
<p><a href="{g}mode-aprs.html">Подробно о режиме APRS →</a></p>
</section>

<section class="mode">
<h3 id="more">И ещё</h3>
<ul>
<li><b>«Устройства…»</b> — RTL-SDR со встроенным драйвером, сетевой rtl_tcp, HackRF One и Airspy R2/Mini
(через их DLL), проверка драйвера WinUSB.</li>
<li><b>«Настройки»</b> — одна папка данных для снимков, записей и журналов; перенос файлов из прежних мест.</li>
<li><b>Автообновление</b> с GitHub Releases: программа сама предложит новую версию.</li>
<li><b>Руководство</b> на 19 страниц — от «что такое SDR» и децибел до антенн и доплера; то же, что на этом сайте.</li>
</ul>
<div class="shots two">
{fig("devices", "«Устройства…»: найденные приёмники и их драйвер Windows.")}
{fig("guide", "Встроенное руководство с поиском по всем страницам.")}
</div>
</section>

<h2 id="requirements">Системные требования<a class="anchor" href="#requirements" aria-label="Ссылка на этот раздел">#</a></h2>
<ul>
<li><b>Windows 10 или 11, 64 бит.</b> Python ставить не нужно — всё внутри архива.</li>
<li><b>Приёмник:</b> RTL-SDR Blog V4 (R828D) и V3, другие донглы на RTL2832U (R820T/R820T2 и т. п.);
также HackRF One, Airspy R2/Mini и сетевой rtl_tcp.</li>
<li><b>Драйвер WinUSB</b> на донгле — ставится один раз программой <a href="https://zadig.akeo.ie/">Zadig</a>
(<a href="install.html#zadig">как</a>). Сам librtlsdr уже встроен.</li>
<li>Двухъядерный процессор и выше; для полосы 2,4 МГц и больше — порт USB 2.0 без перегруженных хабов.</li>
<li>Интернет нужен только для карт, орбит (TLE), справочника бортов и проверки обновлений.</li>
</ul>

<h2 id="install">Установка<a class="anchor" href="#install" aria-label="Ссылка на этот раздел">#</a></h2>
<ol>
<li>Скачайте архив <code>SDR4NOOB-&lt;версия&gt;-win64.zip</code> со страницы
<a href="{DOWNLOAD_URL}">последнего выпуска</a>.</li>
<li>Распакуйте его в папку, куда у вас есть права записи (например, <code>C:\\SDR4NOOB</code>).</li>
<li>Запустите <code>SDR4NOOB.exe</code>. Если донгл ещё не работал с SDR-программами — один раз поставьте WinUSB
через Zadig.</li>
<li>Дальше программа обновляется сама.</li>
</ol>
<p><a href="install.html">Подробно: установка, драйвер, обновление и удаление →</a></p>

<h2 id="quickstart">Быстрый старт<a class="anchor" href="#quickstart" aria-label="Ссылка на этот раздел">#</a></h2>
<ol>
<li>Подключите донгл с антенной из комплекта, запустите SDR4NOOB и выберите <b>«Радио»</b>.</li>
<li>Нажмите <b>WFM</b>, в «Менеджере частот» дважды щёлкните «FM-вещание 87,5–108 МГц», затем щёлкните
по «горбу» станции на спектре — зазвучит музыка, появится название (RDS).</li>
<li>Меню «Режим → Выбрать другой режим…» (Ctrl+M) → <b>«Самолёты (ADS-B)»</b> → «▶ Старт»: через несколько
секунд на карте появятся борта.</li>
<li>Нет антенны — поставьте на экране выбора галочку «Без антенны: тестовый эфир» и изучайте окна.</li>
</ol>
<p><a href="{g}quickstart.html">Первые 10 минут подробно →</a></p>

<h2 id="guide">Руководство<a class="anchor" href="#guide" aria-label="Ссылка на этот раздел">#</a></h2>
<div class="cards">
<a class="card" href="{g}quickstart.html"><b>Первые шаги</b><span>Быстрый старт, что можно принимать, закон и этика эфира.</span></a>
<a class="card" href="{g}pipeline.html"><b>Основы радиоприёма</b><span>Путь радиосигнала, SDR, децибелы, усиление, антенны, модуляция, спектр, доплер.</span></a>
<a class="card" href="{g}pipeline/index.html"><b>Интерактивная схема</b><span>Путь сигнала от микрофона до динамика: модуляция, эфир, спектр и водопад, демодуляция, уровни в дБм, ваттах и вольтах.</span></a>
<a class="card" href="{g}mode-radio.html"><b>Режимы SDR4NOOB</b><span>ТВ, Радио, SSTV, Спутники, ADS-B, APRS — каждое окно по карточкам.</span></a>
<a class="card" href="{g}troubleshooting.html"><b>Помощь</b><span>Помехи и неполадки, словарь терминов.</span></a>
</div>

<h2 id="limits">Честные ограничения<a class="anchor" href="#limits" aria-label="Ссылка на этот раздел">#</a></h2>
<ul>
<li><b>DVB-T2 картинки нет и не будет:</b> мультиплекс занимает 7,8 МГц, а RTL-SDR отдаёт не больше ~3,2 МГц.
Цифровой режим ТВ — это измеритель для наведения антенны.</li>
<li><b>Аналоговое эфирное ТВ в России выключено</b> с 2019 года; режим пригодится за рубежом, для кабельных и
любительских передач и на тестовом генераторе. Картинка <b>чёрно-белая</b> (цветовая поднесущая не помещается
в полосу), звук и изображение одновременно не принимаются.</li>
<li><b>«Метеор-М»</b> требует антенны для 137 МГц (V-диполь или QFH) на открытом месте, пролёта с возвышением от
~30° и тихого эфира без помех; комнатный штырь снимка не даст. <b>«Электро-Л»</b> — только с тарелкой и МШУ.</li>
<li>Один канал (VFO) за раз; dPMR, NXDN, TETRA, P25, D-STAR и System Fusion не декодируются; APRS 9600 бод —
тоже.</li>
<li>SDR4NOOB — только приёмник: передавать он не умеет. Слушать служебные переговоры можно, разглашать — нельзя
(<a href="{g}legal.html">подробнее</a>).</li>
</ul>

<h2 id="license">Лицензия<a class="anchor" href="#license" aria-label="Ссылка на этот раздел">#</a></h2>
<p>SDR4NOOB — свободная программа под лицензией <a href="{REPO_URL}/blob/main/LICENSE">GPL-3.0-or-later</a>.
Внутри — librtlsdr (сборка rtl-sdr-blog, GPL-2.0-or-later), mbelib (ISC), Qt/PySide6 (LGPL-3.0), numpy, scipy,
sgp4 и другие компоненты; полный список с лицензиями и источниками — в
<a href="{REPO_URL}/blob/main/THIRD_PARTY.md">THIRD_PARTY.md</a>. Декодирование голоса DMR (AMBE+2) может
затрагивать патенты — используйте эту функцию на свой страх и риск.</p>
"""


def install_body():
    g = "guide/"
    return f"""
<h1>Установка и обновление</h1>
<p>SDR4NOOB поставляется готовой сборкой для Windows: Python, Qt и драйвер librtlsdr уже внутри. Установщика нет —
архив распаковывается в любую папку.</p>

<h2 id="requirements">Что нужно<a class="anchor" href="#requirements" aria-label="Ссылка на этот раздел">#</a></h2>
<div class="tw"><table class="t">
<tr><th style="width:30%">Что</th><th>Требование</th></tr>
<tr><td>Система</td><td>Windows 10 или 11, 64 бит.</td></tr>
<tr><td>Приёмник</td><td>RTL-SDR Blog V4 или V3, другие донглы на RTL2832U. Дополнительно: HackRF One, Airspy R2/Mini
(с их DLL), любой сервер rtl_tcp в сети. Без приёмника работает тестовый эфир.</td></tr>
<tr><td>Драйвер</td><td>WinUSB на интерфейсе донгла (Zadig, один раз).</td></tr>
<tr><td>Антенна</td><td>Комплектный диполь для начала; для спутников и самолётов — см.
<a href="{g}antennas.html">«Антенны и кабели»</a>.</td></tr>
<tr><td>Интернет</td><td>Не обязателен. Нужен для подложки карт, орбит (TLE), справочника бортов и обновлений.</td></tr>
</table></div>

<h2 id="download">1. Скачать и распаковать<a class="anchor" href="#download" aria-label="Ссылка на этот раздел">#</a></h2>
<ol>
<li>Откройте <a href="{DOWNLOAD_URL}">страницу последнего выпуска</a> и скачайте
<code>SDR4NOOB-&lt;версия&gt;-win64.zip</code> (например, <code>SDR4NOOB-2.0.0-win64.zip</code>).</li>
<li>Распакуйте архив целиком (правая кнопка → «Извлечь всё…») в папку, куда у вас есть права записи:
<code>C:\\SDR4NOOB</code>, <code>D:\\Программы\\SDR4NOOB</code> и т. п. Не кладите программу в
<code>C:\\Program Files</code> — автообновлению нужно заменять файлы без прав администратора.</li>
<li>Внутри будет папка с <code>SDR4NOOB.exe</code> и служебной папкой <code>_internal</code>. Запускайте
<code>SDR4NOOB.exe</code>; при желании создайте ярлык на рабочем столе.</li>
</ol>
<div class="callout note">Программа не подписана сертификатом разработчика, поэтому при первом запуске Windows
SmartScreen может показать «Система Windows защитила ваш компьютер». Нажмите «Подробнее» → «Выполнить в любом
случае». Скачивайте SDR4NOOB только со страницы выпусков на GitHub.</div>

<h2 id="zadig">2. Драйвер WinUSB (Zadig)<a class="anchor" href="#zadig" aria-label="Ссылка на этот раздел">#</a></h2>
<p>Windows считает донгл ТВ-тюнером и ставит драйвер Realtek для DVB-T, с которым SDR-программы не работают.
Один раз замените его на WinUSB. Если донгл уже работал в SDR#, SDR++ или SatDump — этот шаг не нужен.</p>
<ol>
<li>Скачайте Zadig с официального сайта <a href="https://zadig.akeo.ie/">zadig.akeo.ie</a> и запустите.</li>
<li><b>Options → List All Devices.</b> Выберите «Bulk-In, Interface (Interface 0)» или «RTL2838UHIDIR».
USB ID должен начинаться с <code>0BDA 2838</code>.</li>
<li>Справа выберите <b>WinUSB</b> и нажмите <b>Replace Driver</b> (или Install Driver).</li>
</ol>
<div class="callout warn">Не выбирайте в Zadig другие устройства — клавиатура, мышь или звуковая карта перестанут
работать. В другом USB-порту Windows может считать донгл новым устройством — тогда замену нужно повторить.</div>
<p>Проверить результат можно в SDR4NOOB: кнопка <b>«Устройства…»</b> на экране выбора режима показывает найденные
приёмники, их VID:PID и драйвер Windows («Готово: работает со встроенным драйвером SDR4NOOB»).
Подробнее — <a href="{g}sdr.html#driver">«Драйверы: WinUSB и Zadig»</a>.</p>

<h2 id="first-run">3. Первый запуск<a class="anchor" href="#first-run" aria-label="Ссылка на этот раздел">#</a></h2>
<ul>
<li>Появится экран выбора режима: ТВ, Радио, SSTV, Спутники, Самолёты (ADS-B), APRS. Вернуться к нему —
меню «Режим → Выбрать другой режим…» (Ctrl+M).</li>
<li>Галочка <b>«Без антенны: тестовый эфир вместо донгла»</b> включает синтетические сигналы для всех режимов.</li>
<li>Кнопка <b>«Руководство»</b> открывает встроенную справку (то же, что на этом сайте), <b>F1</b> в окне режима —
страницу этого режима.</li>
<li>Дальше — <a href="{g}quickstart.html">«Быстрый старт»</a>.</li>
</ul>

<h2 id="devices">Другие приёмники<a class="anchor" href="#devices" aria-label="Ссылка на этот раздел">#</a></h2>
<ul>
<li><b>RTL-SDR</b> работает со встроенным librtlsdr (сборка rtl-sdr-blog с поддержкой V4). Ставить SDR# или
системный librtlsdr не нужно.</li>
<li><b>HackRF One</b> и <b>Airspy R2/Mini</b>: «Устройства… → Загрузить драйвер (DLL)…» — hackrf.dll или airspy.dll,
плюс libusb-1.0.dll и pthreadVC2.dll. Файлы копируются в папку драйверов SDR4NOOB.</li>
<li><b>rtl_tcp</b>: приёмник на другом компьютере — «Добавить сетевой rtl_tcp…» (адрес и порт). Подходят и
совместимые серверы, например rsp_tcp для SDRplay.</li>
</ul>

<h2 id="data">Где лежат файлы<a class="anchor" href="#data" aria-label="Ссылка на этот раздел">#</a></h2>
<div class="tw"><table class="t">
<tr><th style="width:38%">Папка</th><th>Что в ней</th></tr>
<tr><td>Папка данных (по умолчанию <code>Документы\\SDR4NOOB</code>)</td><td>Записи IQ, снимки (ТВ, «Метеор»,
«Электро-Л», SSTV), звук (WAV), журналы ADS-B и APRS. Выбирается в «Настройках»; там же — перенос файлов из
прежних мест.</td></tr>
<tr><td><code>%APPDATA%\\SDR4NOOB</code></td><td>Настройки (*.json).</td></tr>
<tr><td><code>%LOCALAPPDATA%\\SDR4NOOB</code></td><td>Кэш карт, орбиты (TLE), история самолётов, справочник бортов,
драйверы, журнал <code>sdr4noob.log</code>. По желанию — внутри папки данных («служебное тоже здесь»).</td></tr>
</table></div>

<h2 id="update">Обновление<a class="anchor" href="#update" aria-label="Ссылка на этот раздел">#</a></h2>
<ul>
<li>SDR4NOOB сам проверяет новую версию на GitHub Releases — по умолчанию раз в день (в «Настройках»: раз в день,
раз в неделю или никогда). Проверить вручную — меню «Справка → Проверить обновления…» или «Настройки →
Проверить сейчас».</li>
<li>В окне «Доступно обновление SDR4NOOB» видно описание изменений; кнопки «Обновить сейчас», «Страница релиза» и
«Пропустить эту версию».</li>
<li>«Обновить сейчас» скачивает архив, сверяет контрольную сумму SHA-256, после выхода программы заменяет файлы
(прежняя версия сохраняется и возвращается при ошибке) и запускает новую версию. Настройки и папка данных не
затрагиваются.</li>
<li>Вручную: скачайте новый архив и распакуйте поверх старой папки (программа должна быть закрыта).</li>
</ul>

<h2 id="uninstall">Удаление<a class="anchor" href="#uninstall" aria-label="Ссылка на этот раздел">#</a></h2>
<p>Закройте SDR4NOOB и удалите папку программы. Чтобы не осталось следов, удалите также <code>%APPDATA%\\SDR4NOOB</code>
и <code>%LOCALAPPDATA%\\SDR4NOOB</code>; папку данных со снимками и записями — по желанию. Драйвер WinUSB можно
оставить: им пользуются и другие SDR-программы.</p>

<h2 id="source">Из исходников<a class="anchor" href="#source" aria-label="Ссылка на этот раздел">#</a></h2>
<p>Нужен Python 3.12 для Windows x64. Инструкция по запуску, сборке EXE и тестам — в
<a href="{REPO_URL}#readme">README репозитория</a>.</p>
"""


NOT_FOUND = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Страница не найдена — SDR4NOOB</title>
<meta name="color-scheme" content="light dark">
<link rel="icon" href="{SITE_PATH}assets/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="{SITE_PATH}assets/style.css">
</head>
<body class="notfound">
<main class="content">
<h1>Страница не найдена</h1>
<p>Такой страницы на сайте SDR4NOOB нет. Возможно, она переехала.</p>
<p><a class="btn" href="{SITE_PATH}index.html">На главную</a>
<a class="btn ghost" href="{SITE_PATH}guide/index.html">Руководство</a></p>
</main>
</body>
</html>
"""


# ---------------------------------------------------------------- стили и скрипт

CSS = r"""
:root {
  --bg: #f5f7fa; --panel: #ffffff; --panel2: #e9edf3; --fg: #1d2430; --dim: #5d6878; --accent: #0a66c2;
  --accent-fg: #ffffff; --grid: #d3dae4; --code: #0a3d6b; --warn: #b8610d; --mark: #ffe58a; --shadow: 0 1px 3px rgba(0,0,0,.08);
  --top: 56px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #15181d; --panel: #1d2128; --panel2: #252a33; --fg: #d8dee9; --dim: #8a93a3; --accent: #4fb3ff;
    --accent-fg: #0b1220; --grid: #2f3542; --code: #7fffd4; --warn: #e8954a; --mark: #6b5a12; --shadow: none;
  }
}
* { box-sizing: border-box; }
html { scroll-padding-top: calc(var(--top) + 12px); -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.6 "Segoe UI", system-ui, -apple-system,
  Roboto, "Helvetica Neue", Arial, sans-serif; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
code, .mono, .k { font-family: Consolas, "Cascadia Mono", "Courier New", monospace; font-size: .92em; }
code, .mono { color: var(--code); }
.k { background: var(--panel2); border: 1px solid var(--grid); border-radius: 4px; padding: 0 .35em; white-space: nowrap; }
.dim, .crumbs { color: var(--dim); }
.crumbs { font-size: .9em; margin: 0 0 .25rem; }
mark { background: var(--mark); color: inherit; border-radius: 2px; }
.skip { position: absolute; left: -999px; top: 0; }
.skip:focus { left: 8px; top: 8px; z-index: 100; background: var(--panel); padding: 6px 10px; }

/* шапка */
.top { position: sticky; top: 0; z-index: 20; height: var(--top); display: flex; align-items: center; gap: 14px;
  padding: 0 16px; background: var(--panel); border-bottom: 1px solid var(--grid); box-shadow: var(--shadow); }
.brand { display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: 1.15rem; color: var(--fg); white-space: nowrap; }
.brand:hover { text-decoration: none; }
.brand small { font-weight: 400; font-size: .75rem; color: var(--dim); }
.menu-btn { display: none; background: none; border: 1px solid var(--grid); color: var(--fg); border-radius: 6px;
  font-size: 1.2rem; width: 40px; height: 36px; cursor: pointer; }
.search { position: relative; flex: 1; max-width: 460px; margin-left: auto; }
.search input { width: 100%; height: 36px; padding: 0 12px; border-radius: 8px; border: 1px solid var(--grid);
  background: var(--bg); color: var(--fg); font: inherit; font-size: .95rem; }
.search input:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
.results { position: absolute; top: 42px; left: 0; right: 0; max-height: 70vh; overflow: auto; background: var(--panel);
  border: 1px solid var(--grid); border-radius: 8px; box-shadow: 0 8px 24px rgba(0,0,0,.25); }
.results a { display: block; padding: 8px 12px; color: var(--fg); border-bottom: 1px solid var(--grid); }
.results a:last-child { border-bottom: 0; }
.results a b { color: var(--accent); display: block; }
.results a small { color: var(--dim); display: block; font-size: .85rem; line-height: 1.4; }
.results a.sel, .results a:hover { background: var(--panel2); text-decoration: none; }
.results .none { padding: 10px 12px; color: var(--dim); }
.links { display: flex; align-items: center; gap: 14px; white-space: nowrap; }
.btn { display: inline-block; padding: 6px 14px; border-radius: 8px; background: var(--accent); color: var(--accent-fg);
  font-weight: 600; border: 1px solid var(--accent); }
.btn:hover { text-decoration: none; filter: brightness(1.08); }
.btn.ghost { background: transparent; color: var(--accent); }
.btn.big { padding: 10px 20px; font-size: 1.05rem; margin: 0 8px 8px 0; }

/* раскладка */
.layout { display: grid; grid-template-columns: 270px minmax(0, 1fr); max-width: 1320px; margin: 0 auto; }
.sidebar { position: sticky; top: var(--top); align-self: start; height: calc(100vh - var(--top)); overflow: auto;
  padding: 18px 12px 40px 16px; border-right: 1px solid var(--grid); }
.nav-group h2 { font-size: .78rem; text-transform: uppercase; letter-spacing: .06em; color: var(--dim); margin: 16px 8px 4px; }
.nav-group:first-child h2 { margin-top: 0; }
.nav-group ul { list-style: none; margin: 0; padding: 0; }
.nav-group a { display: block; padding: 4px 8px; border-radius: 6px; color: var(--fg); font-size: .95rem; line-height: 1.35; }
.nav-group a:hover { background: var(--panel2); text-decoration: none; }
.nav-group a.cur { background: var(--accent); color: var(--accent-fg); }
.content { padding: 24px 36px 48px; max-width: 980px; min-width: 0; }
body.home .content { max-width: 1060px; }
h1 { font-size: 2rem; line-height: 1.25; margin: .2rem 0 1rem; color: var(--accent); }
h2 { font-size: 1.45rem; margin: 2.2rem 0 .6rem; padding-bottom: .25rem; border-bottom: 1px solid var(--grid); }
h3 { font-size: 1.15rem; margin: 1.6rem 0 .4rem; }
h2 .anchor, h3 .anchor { margin-left: .4em; color: var(--dim); opacity: 0; font-weight: 400; }
h2:hover .anchor, h3:hover .anchor, .anchor:focus { opacity: 1; text-decoration: none; }
p, li { margin: .45rem 0; }
ul, ol { padding-left: 1.5rem; }

/* таблицы и врезки */
.tw { overflow-x: auto; margin: .8rem 0; }
table.t { border-collapse: collapse; width: 100%; background: var(--panel); font-size: .95rem; }
table.t th, table.t td { border: 1px solid var(--grid); padding: 6px 10px; vertical-align: top; text-align: left; }
table.t th { background: var(--panel2); }
.callout { background: var(--panel); border: 1px solid var(--grid); border-left: 4px solid var(--accent);
  border-radius: 6px; padding: 12px 16px; margin: 1rem 0; }
.callout.warn { border-left-color: var(--warn); }
figure { margin: 1rem 0; }
.diagram img { max-width: 100%; height: auto; display: block; }
.shot img { width: 100%; height: auto; display: block; border-radius: 8px; border: 1px solid var(--grid); }
.shot figcaption { font-size: .88rem; color: var(--dim); margin-top: 6px; line-height: 1.4; }
.shots { display: grid; gap: 18px; margin: 1rem 0; }
.shots.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }

/* главная */
.hero { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.15fr); gap: 28px; align-items: center; margin-bottom: 1rem; }
.hero h1 { font-size: 2.2rem; }
.lead { font-size: 1.12rem; }
.logo-title { display: flex; align-items: center; gap: .6rem; }
.logo-title img { width: 64px; height: 64px; }
.tagline { font-size: 1.3rem; font-weight: 600; color: var(--accent); margin: -.4rem 0 .6rem; }
.cta { margin: 1.2rem 0 .6rem; }
.mode { border-top: 1px solid var(--grid); padding-top: .4rem; }
.mode:first-of-type { border-top: 0; }
.mode h3 { font-size: 1.3rem; color: var(--accent); }
.cards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.card { display: block; background: var(--panel); border: 1px solid var(--grid); border-radius: 10px; padding: 14px 16px; color: var(--fg); }
.card:hover { border-color: var(--accent); text-decoration: none; }
.card b { display: block; color: var(--accent); margin-bottom: 4px; }
.card span { color: var(--dim); font-size: .95rem; }
.prevnext { display: flex; justify-content: space-between; gap: 16px; margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--grid); }
.prevnext a { display: block; max-width: 48%; }
.prevnext a span { display: block; font-size: .85rem; color: var(--dim); }
.prevnext .next { text-align: right; margin-left: auto; }
.foot { border-top: 1px solid var(--grid); color: var(--dim); font-size: .88rem; padding: 18px 24px 28px; text-align: center; }
.foot p { margin: .3rem 0; }
body.notfound .content { margin: 10vh auto; text-align: center; }

/* телефон и планшет */
@media (max-width: 1100px) {
  .hero { grid-template-columns: 1fr; }
}
@media (max-width: 860px) {
  :root { --top: 104px; }
  .top { flex-wrap: wrap; height: auto; min-height: var(--top); padding: 8px 16px; gap: 8px 12px; }
  .menu-btn { display: inline-block; }
  .search { order: 3; flex-basis: 100%; max-width: none; margin: 0; }
  .links { margin-left: auto; gap: 10px; }
  .links a:not(.btn) { display: none; }
  .layout { display: block; }
  .sidebar { position: fixed; z-index: 30; top: 0; left: 0; bottom: 0; width: min(300px, 86vw); height: auto;
    background: var(--panel); transform: translateX(-105%); transition: transform .2s; box-shadow: 0 0 24px rgba(0,0,0,.35); }
  body.nav-open .sidebar { transform: none; }
  body.nav-open::after { content: ""; position: fixed; inset: 0; z-index: 25; background: rgba(0,0,0,.4); }
  .content { padding: 16px 16px 40px; }
  h1, .hero h1 { font-size: 1.6rem; }
  h2 { font-size: 1.3rem; }
  .shots.two, .cards { grid-template-columns: 1fr; }
  .prevnext { flex-direction: column; }
  .prevnext a { max-width: none; }
}
@media print {
  .top, .sidebar, .prevnext, .foot { display: none; }
  .layout { display: block; }
}
"""

JS = r"""
(function () {
  "use strict";
  var root = document.body.getAttribute("data-root") || "";
  var btn = document.querySelector(".menu-btn");
  if (btn) {
    btn.addEventListener("click", function () {
      var open = document.body.classList.toggle("nav-open");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("click", function (e) {
      if (document.body.classList.contains("nav-open") && !e.target.closest(".sidebar") && !e.target.closest(".menu-btn")) {
        document.body.classList.remove("nav-open");
        btn.setAttribute("aria-expanded", "false");
      }
    });
  }

  function norm(s) { return s.toLowerCase().replace(/ё/g, "е"); }
  function esc(s) { return s.replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; }); }
  function words(q) { return norm(q).split(/\s+/).filter(function (w) { return w.length > 0; }); }

  function search(q) {
    var ws = words(q), out = [], idx = window.SDR4NOOB_SEARCH || [];
    if (!ws.length) return out;
    idx.forEach(function (p) {
      var low = p.n || (p.n = norm(p.x)), tl = norm(p.t), score = 0;
      for (var i = 0; i < ws.length; i++) {
        var inText = low.indexOf(ws[i]) >= 0, inTitle = tl.indexOf(ws[i]) >= 0;
        if (!inText && !inTitle) return;
        score += (inTitle ? 20 : 0) + low.split(ws[i]).length - 1;
      }
      var at = low.indexOf(ws[0]), a = Math.max(0, at - 50), b = Math.min(p.x.length, at + ws[0].length + 110);
      var snip = at < 0 ? p.x.slice(0, 150) : (a ? "…" : "") + p.x.slice(a, b) + (b < p.x.length ? "…" : "");
      out.push({p: p, s: score, snip: snip});
    });
    out.sort(function (x, y) { return y.s - x.s; });
    return out.slice(0, 15);
  }

  function highlight(text, ws) {
    var h = esc(text);
    ws.forEach(function (w) {
      if (w.length < 2) return;
      var re = new RegExp("(" + w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/е/g, "[её]") + ")", "gi");
      h = h.replace(re, "<mark>$1</mark>");
    });
    return h;
  }

  var input = document.getElementById("q"), box = document.getElementById("results"), sel = -1;
  function render() {
    var q = input.value.trim();
    if (!q) { box.hidden = true; box.innerHTML = ""; return; }
    var res = search(q), ws = words(q);
    sel = res.length ? 0 : -1;
    box.innerHTML = res.length ? res.map(function (r, i) {
      return '<a role="option" href="' + root + r.p.u + "?q=" + encodeURIComponent(q) + '"' + (i === 0 ? ' class="sel"' : "") + ">" +
        "<b>" + esc(r.p.t) + "</b><small>" + highlight(r.snip, ws) + "</small></a>";
    }).join("") : '<div class="none">Ничего не найдено</div>';
    box.hidden = false;
  }
  function move(d) {
    var items = box.querySelectorAll("a");
    if (!items.length) return;
    if (sel >= 0) items[sel].classList.remove("sel");
    sel = (sel + d + items.length) % items.length;
    items[sel].classList.add("sel");
    items[sel].scrollIntoView({block: "nearest"});
  }
  if (input) {
    var t = null;
    input.addEventListener("input", function () { clearTimeout(t); t = setTimeout(render, 120); });
    input.addEventListener("focus", function () { if (input.value.trim()) render(); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") {
        var items = box.querySelectorAll("a");
        if (items.length) { e.preventDefault(); location.href = items[Math.max(0, sel)].getAttribute("href"); }
      } else if (e.key === "Escape") { box.hidden = true; input.blur(); }
    });
    document.addEventListener("click", function (e) { if (!e.target.closest(".search")) box.hidden = true; });
    document.addEventListener("keydown", function (e) {
      var tag = (document.activeElement && document.activeElement.tagName) || "";
      if (e.key === "/" && tag !== "INPUT" && tag !== "TEXTAREA") { e.preventDefault(); input.focus(); input.select(); }
    });
  }

  // переход из поиска: подсветить найденные слова на странице и прокрутить к первому
  var m = /[?&]q=([^&#]*)/.exec(location.search);
  if (m) {
    var q = decodeURIComponent(m[1].replace(/\+/g, " ")), ws = words(q).filter(function (w) { return w.length > 1; });
    if (input) input.value = q;
    var main = document.getElementById("content"), first = null;
    if (main && ws.length) {
      var re = new RegExp("(" + ws.map(function (w) { return w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/е/g, "[её]"); }).join("|") + ")", "gi");
      var walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT, null), nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      nodes.forEach(function (n) {
        if (!n.nodeValue.trim() || n.parentNode.closest("script,style,mark,.anchor")) return;
        re.lastIndex = 0;
        if (!re.test(n.nodeValue)) return;
        var span = document.createElement("span");
        span.innerHTML = esc(n.nodeValue).replace(re, "<mark>$1</mark>");
        n.parentNode.replaceChild(span, n);
        if (!first) first = span.querySelector("mark");
      });
      if (first && !location.hash) first.scrollIntoView({block: "center"});
    }
  }
})();
"""


# ---------------------------------------------------------------- сборка

def clean():
    for name in OWNED:
        p = os.path.join(DOCS, name)
        if os.path.isdir(p):
            shutil.rmtree(p)
        elif os.path.exists(p):
            os.remove(p)


def build(verbose=True):
    """Собрать сайт в docs/. Возвращает список путей созданных страниц (относительно docs/)."""
    version = app_version()
    toc = guide_toc()
    gpages = guide_pages(toc)
    groups = nav_tree(toc)
    light, dark = theme_colors("Светлая"), theme_colors("Тёмная")
    os.makedirs(DOCS, exist_ok=True)
    clean()
    write(os.path.join(DOCS, ".nojekyll"), "")
    write(os.path.join(DOCS, "assets", "style.css"), CSS.lstrip())
    write(os.path.join(DOCS, "assets", "site.js"), JS.lstrip())
    write(os.path.join(DOCS, "assets", "favicon.svg"), ICON_SVG)
    shutil.copyfile(os.path.join(LOGO_DIR, "sdr4noob-256.png"), os.path.join(DOCS, "assets", "sdr4noob-256.png"))
    for name in sorted(os.listdir(os.path.join(GUIDE, "img"))):
        if name.endswith(".svg"):
            for out, txt in svg_variants(name, light, dark).items():
                write(os.path.join(DOCS, "guide", "img", out), txt)
    # интерактивные страницы (своя вёрстка и JavaScript, напр. guide/pipeline/) — как есть
    for name in interactive_dirs():
        shutil.copytree(os.path.join(GUIDE, name), os.path.join(DOCS, "guide", name))

    order = [("index.html", "Главная"), ("install.html", "Установка и обновление")] + \
            [(f"guide/{pid}.html", t) for pid, t in gpages]
    index = []
    made = []

    def emit(path, title, body, **kw):
        root = "../" * path.count("/")
        i = [p for p, _t in order].index(path)
        prevnext = None
        if path.startswith("guide/"):
            prevnext = (order[i - 1] if i > 0 else None, order[i + 1] if i + 1 < len(order) else None)
        text = page(title=title, body=body, current=path, root=root, groups=groups, version=version,
                    prevnext=prevnext, **kw)
        write(os.path.join(DOCS, *path.split("/")), text)
        index.append({"u": path, "t": title, "x": plain_text(body)})
        made.append(path)

    emit("index.html", "Главная", home_body(), home=True)
    emit("install.html", "Установка и обновление", install_body(),
         description="Как установить SDR4NOOB: скачать архив, распаковать, поставить WinUSB через Zadig; "
                     "автообновление и удаление.")
    for pid, toc_title in gpages:
        title, body = convert_guide(pid, read(os.path.join(GUIDE, pid + ".html")))
        first_p = re.search(r"(?s)<p>(.*?)</p>", body)
        desc = plain_text(first_p.group(1))[:200] if first_p else ""
        emit(f"guide/{pid}.html", title, body, description=desc)

    write(os.path.join(DOCS, "assets", "search-index.js"),
          "window.SDR4NOOB_SEARCH = " + json.dumps(index, ensure_ascii=False, separators=(",", ":")) + ";\n")
    write(os.path.join(DOCS, "404.html"), NOT_FOUND)
    pages_all = made + [f"guide/{d}/index.html" for d in interactive_dirs()]
    urls = "".join(f"  <url><loc>{SITE_URL}{'' if p == 'index.html' else p}</loc></url>\n" for p in pages_all)
    write(os.path.join(DOCS, "sitemap.xml"),
          '<?xml version="1.0" encoding="UTF-8"?>\n'
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + urls + "</urlset>\n")
    if verbose:
        print(f"Сайт собран: {len(made)} страниц → {DOCS}")
    return made


# ---------------------------------------------------------------- проверка ссылок

_ATTR = re.compile(r"""\s(href|src|srcset)=["']([^"']*)["']""", re.I)
_IDS = re.compile(r"""\s(?:id|name)=["']([^"']+)["']""", re.I)


def check_links(docs=DOCS):
    """Все внутренние href/src/srcset в docs/*.html ведут на существующие файлы и якоря. -> [ошибки]."""
    problems = []
    ids_cache = {}

    def ids_of(path):
        if path not in ids_cache:
            ids_cache[path] = set(_IDS.findall(read(path)))
        return ids_cache[path]

    def target(base_file, url):
        if url.startswith(SITE_PATH):
            return os.path.join(docs, *unquote(url[len(SITE_PATH):]).split("/"))
        return os.path.normpath(os.path.join(os.path.dirname(base_file), unquote(url)))

    files = []
    for dp, _dn, fn in os.walk(docs):
        files += [os.path.join(dp, f) for f in fn if f.endswith(".html")]
    for f in sorted(files):
        rel = os.path.relpath(f, docs)
        src = read(f)
        for attr, val in _ATTR.findall(src):
            urls = [v.strip().split(" ")[0] for v in val.split(",")] if attr.lower() == "srcset" else [val]
            for url in urls:
                if not url or re.match(r"^(https?:|mailto:|javascript:|data:)", url, re.I):
                    continue
                path, _q, frag = url.partition("#")
                path = path.split("?")[0]
                tgt = target(f, path) if path else f
                if os.path.isdir(tgt):
                    tgt = os.path.join(tgt, "index.html")
                if not os.path.exists(tgt):
                    problems.append(f"{rel}: нет файла для {url}")
                    continue
                if frag and tgt.endswith(".html") and frag not in ids_of(tgt):
                    problems.append(f"{rel}: нет якоря #{frag} в {os.path.relpath(tgt, docs)}")
    # адреса из поискового индекса
    idx_path = os.path.join(docs, "assets", "search-index.js")
    if os.path.exists(idx_path):
        data = json.loads(read(idx_path).split("=", 1)[1].strip().rstrip(";"))
        for item in data:
            if not os.path.exists(os.path.join(docs, *item["u"].split("/"))):
                problems.append(f"search-index.js: нет страницы {item['u']}")
    return problems


# ---------------------------------------------------------------- снимки окон

def convert_screens(src_dir, dst_dir=None, max_bytes=SCREEN_MAX_BYTES, width=1600):
    """PNG из src_dir → WebP в docs/img/screens (не больше max_bytes каждый)."""
    from PIL import Image
    dst_dir = dst_dir or os.path.join(DOCS, "img", "screens")
    os.makedirs(dst_dir, exist_ok=True)
    for name in sorted(os.listdir(src_dir)):
        if not name.lower().endswith(".png"):
            continue
        im = Image.open(os.path.join(src_dir, name)).convert("RGB")
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        out = os.path.join(dst_dir, os.path.splitext(name)[0] + ".webp")
        for q in (86, 80, 74, 68, 60, 52, 45):
            im.save(out, "WEBP", quality=q, method=6)
            if os.path.getsize(out) <= max_bytes:
                break
        else:
            im = im.resize((im.width * 3 // 4, im.height * 3 // 4), Image.LANCZOS)
            im.save(out, "WEBP", quality=70, method=6)
        print(f"{name} → {os.path.basename(out)}: {os.path.getsize(out) // 1024} КБ")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Собрать сайт SDR4NOOB в docs/")
    ap.add_argument("--check", action="store_true", help="после сборки проверить ссылки, якоря и картинки")
    ap.add_argument("--screens", metavar="DIR", help="ужать снимки окон DIR/*.png в docs/img/screens/*.webp")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if a.screens:
        convert_screens(a.screens)
    build()
    if a.check:
        problems = check_links()
        for p in problems:
            print(p)
        print("Ссылки в порядке." if not problems else f"Ошибок: {len(problems)}")
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
