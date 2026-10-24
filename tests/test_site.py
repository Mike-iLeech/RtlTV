"""Сайт GitHub Pages (docs/): tools/build_site.py собирает его без ошибок, все внутренние ссылки, якоря и
картинки ведут на существующие файлы, все страницы руководства попали на сайт и в поисковый индекс."""
import importlib.util
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")


def _load():
    spec = importlib.util.spec_from_file_location("build_site", os.path.join(ROOT, "tools", "build_site.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bs = _load()
_BUILT = {}


def _build():
    if "made" not in _BUILT:
        _BUILT["made"] = bs.build(verbose=False)
    return _BUILT["made"]


def _read(rel):
    with open(os.path.join(DOCS, *rel.split("/")), encoding="utf-8") as f:
        return f.read()


def test_build_and_links():
    made = _build()
    assert "index.html" in made and "install.html" in made
    assert os.path.exists(os.path.join(DOCS, ".nojekyll"))
    assert bs.check_links() == []


def test_all_guide_pages_on_site():
    made = _build()
    pages = bs.guide_pages(bs.guide_toc())
    files = {f[:-5] for f in os.listdir(os.path.join(ROOT, "rtltv", "guide")) if f.endswith(".html")}
    assert {pid for pid, _t in pages} == files
    for pid, _t in pages:
        assert f"guide/{pid}.html" in made
        src = _read(f"guide/{pid}.html")
        assert '<html lang="ru">' in src and '<meta charset="utf-8">' in src
        assert 'name="viewport"' in src
        assert "{{" not in src and "<a name=" not in src
        assert "\r" not in src
    # меню слева повторяет оглавление руководства по порядку
    nav = re.findall(r'<li><a href="guide/([\w-]+)\.html"', _read("index.html"))
    assert nav == [pid for pid, _t in pages]


def test_svg_variants_without_placeholders():
    _build()
    img = os.path.join(DOCS, "guide", "img")
    for name in os.listdir(os.path.join(ROOT, "rtltv", "guide", "img")):
        if name.endswith(".svg"):
            for out in (name, name[:-4] + "-dark.svg"):
                with open(os.path.join(img, out), encoding="utf-8") as f:
                    assert "{{" not in f.read(), out


def test_search_index():
    made = _build()
    data = json.loads(_read("assets/search-index.js").split("=", 1)[1].strip().rstrip(";"))
    assert {d["u"] for d in data} == set(made)
    assert all(d["t"] and len(d["x"]) > 200 for d in data)
    text = " ".join(d["x"] for d in data).lower()
    for word in ("zadig", "метеор", "dmr", "доплер"):
        assert word in text


def test_no_external_resources():
    """Без CDN: стили, скрипты и картинки только локальные."""
    _build()
    for dp, _dn, fn in os.walk(DOCS):
        for f in fn:
            if f.endswith(".html"):
                with open(os.path.join(dp, f), encoding="utf-8") as fh:
                    src = fh.read()
                assert not re.search(r"""<(script|img|source|iframe)\b[^>]*(src|srcset)=["']https?:""", src), f
                assert not re.search(r"""<link\b[^>]*rel="(stylesheet|icon|preload)"[^>]*href=["']https?:""",
                                     src), f


def test_screens_small():
    shots = os.path.join(DOCS, "img", "screens")
    names = [f for f in os.listdir(shots) if f.endswith(".webp")]
    assert len(names) >= 10
    for f in names:
        assert os.path.getsize(os.path.join(shots, f)) <= bs.SCREEN_MAX_BYTES, f
        assert bs.image_size(os.path.join(shots, f)), f


def test_interactive_pipeline_on_site():
    """Интерактивная схема копируется на сайт как есть, на неё ведут кнопка в «Пути радиосигнала» и главная."""
    _build()
    src_dir = os.path.join(ROOT, "rtltv", "guide", "pipeline")
    dst_dir = os.path.join(DOCS, "guide", "pipeline")
    for name in os.listdir(src_dir):
        with open(os.path.join(src_dir, name), "rb") as a, open(os.path.join(dst_dir, name), "rb") as b:
            assert a.read() == b.read(), name
    page = _read("guide/pipeline.html")
    assert re.search(r'<a class="btn big" href="pipeline/index.html">', page)
    assert 'href="guide/pipeline/index.html"' in _read("index.html")
    assert "guide/pipeline/index.html" in _read("sitemap.xml")
    assert bs.interactive_dirs() == ["pipeline"]
