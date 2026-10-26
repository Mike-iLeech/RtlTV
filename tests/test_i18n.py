"""Перевод интерфейса (rtltv/i18n.py): ни одной русской строки мимо tr(), у каждого ключа есть английский перевод.

Правило для кода (проверяется разбором AST всех rtltv/**/*.py):
  * кириллица в строковом литерале допустима только как аргумент tr()/trn()/N_(), в docstring, в сообщении
    журнала (log.info(...) и т. п. — журнал читает разработчик) или в строке с комментарием «# noqa: i18n»
    (данные, которые не показываются: разбор ввода, имена файлов прежних версий и т. п.);
  * f-строка с кириллицей запрещена — её ключ менялся бы от значения; вместо неё tr("… {x} …", x=...);
  * у каждого ключа есть перевод в rtltv/locale/en/*.json без кириллицы и с теми же {подстановками};
  * в словаре нет ключей, которых больше нет в коде.
Модули из PENDING ещё не переведены (перевод идёт по частям) — к концу работы список пуст.
"""
import ast
import glob
import os
import re
import string

import pytest

from rtltv import i18n

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rtltv")
CYR = re.compile("[А-Яа-яЁё]")
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
TR_FUNCS = {"tr": (0,), "N_": (0,), "trn": (1, 2, 3)}

# ещё не переведённые модули (путь от rtltv/, через «/»)
PENDING = {
    "adsb.py", "aircraft_info.py", "app.py", "aprs.py", "audio.py", "bandplan.py", "devices.py",
    "devices_dialog.py", "diag.py", "dmr.py", "dmrscan.py", "elektro.py", "engine.py", "freqman.py",
    "guide_window.py", "help_radio.py", "iqfile.py", "linux_setup.py", "lrpt.py", "mapview.py", "osplat.py", "paths.py",
    "radiotest.py", "rds.py", "rtl_stream.py", "rtlsdr.py", "satcat.py", "satpass.py",
    "satrx.py", "source.py", "spectrum_nav.py", "sstv.py", "standards.py", "update_ui.py",
    "updater.py", "updater_linux.py", "widgets.py",
    "modes/adsb_window.py", "modes/aprs_window.py", "modes/dmr_scan_panel.py", "modes/radio_panels.py",
    "modes/radio_window.py", "modes/sat_gallery.py", "modes/sat_userdlg.py", "modes/sat_widgets.py",
    "modes/sat_window.py", "modes/sstv_window.py",
}


def _sources():
    for path in sorted(glob.glob(os.path.join(ROOT, "**", "*.py"), recursive=True)):
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        with open(path, encoding="utf-8") as f:
            yield rel, f.read()


def _call_name(node):
    f = node.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


LOGGERS = {"log", "logger", "logging", "_log", "LOG"}


def _is_log_call(node):
    """log.info(…), logging.getLogger(…).warning(…) — но не QMessageBox.warning(…) (это окно для пользователя)."""
    f = node.func
    if not (isinstance(f, ast.Attribute) and f.attr in LOG_METHODS):
        return False
    obj = f.value
    if isinstance(obj, ast.Name):
        return obj.id in LOGGERS
    if isinstance(obj, ast.Attribute):          # self.log.info(…)
        return obj.attr in LOGGERS
    return isinstance(obj, ast.Call) and _call_name(obj) == "getLogger"


def scan(src):
    """(нарушения [(строка, текст)], ключи перевода [(строка, ключ)]) для исходника модуля."""
    tree = ast.parse(src)
    lines = src.splitlines()
    ok = set()       # id() литералов, которым кириллица разрешена
    keys = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ok.add(id(body[0].value))
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name in TR_FUNCS:
                for i in TR_FUNCS[name]:
                    if i < len(node.args) and isinstance(node.args[i], ast.Constant) \
                            and isinstance(node.args[i].value, str):
                        ok.add(id(node.args[i]))
                        keys.append((node.args[i].lineno, node.args[i].value, name, i))
            elif _is_log_call(node):
                for a in node.args:
                    for sub in ast.walk(a):
                        ok.add(id(sub))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            if any(isinstance(v, ast.Constant) and CYR.search(str(v.value)) for v in node.values) \
                    and id(node) not in ok:
                for v in node.values:
                    ok.add(id(v))
                if "noqa: i18n" not in lines[node.lineno - 1]:
                    bad.append((node.lineno, "f-строка: " + "".join(
                        str(v.value) if isinstance(v, ast.Constant) else "{…}" for v in node.values)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and CYR.search(node.value) \
                and id(node) not in ok:
            end = getattr(node, "end_lineno", node.lineno)
            if not any("noqa: i18n" in lines[n - 1] for n in range(node.lineno, end + 1)):
                bad.append((node.lineno, node.value))
    return bad, keys


def _fields(s):
    return {f for _t, f, _s, _c in string.Formatter().parse(s) if f is not None}


SOURCES = dict(_sources())


def test_pending_list_is_current():
    """PENDING — только существующие модули, где ещё правда есть непереведённое (список только сокращается)."""
    for rel in sorted(PENDING):
        assert rel in SOURCES, f"{rel}: нет такого модуля — убрать из PENDING"
        assert scan(SOURCES[rel])[0], f"{rel} уже переведён — убрать из PENDING"


@pytest.mark.parametrize("rel", sorted(r for r in SOURCES if r not in PENDING))
def test_no_untranslated_strings(rel):
    bad, _keys = scan(SOURCES[rel])
    assert not bad, f"{rel}: строки мимо tr():\n" + "\n".join(f"  {n}: {t!r}" for n, t in bad[:40])


def _all_keys():
    keys = {}
    for rel, src in SOURCES.items():
        if rel in PENDING:
            continue
        for line, key, func, idx in scan(src)[1]:
            keys.setdefault(key, []).append((rel, line, func, idx))
    return keys


def test_every_key_translated():
    en = i18n.catalog("en")
    missing, wrong = [], []
    for key, uses in sorted(_all_keys().items()):
        if not CYR.search(key):
            continue  # чисто латинская строка («SDR4NOOB», «dB») — перевод не нужен
        rel, line, func, idx = uses[0]
        if func == "trn" and idx != 1:
            continue  # формы few/many: перевод лежит у формы one
        if key not in en:
            missing.append(f"  {rel}:{line}  {key!r}")
            continue
        val = en[key]
        vals = val if isinstance(val, list) else [val]
        if func == "trn" and not (isinstance(val, list) and len(val) == 2):
            wrong.append(f"  {key!r}: для trn нужен список [one, other]")
        for v in vals:
            if not isinstance(v, str) or CYR.search(v):
                wrong.append(f"  {key!r} → {v!r}: в переводе кириллица")
            elif _fields(v) != _fields(key) and not (func == "trn" and _fields(v) <= _fields(key) | {"n"}):
                wrong.append(f"  {key!r} → {v!r}: подстановки {_fields(key)} ≠ {_fields(v)}")
    assert not missing, "нет перевода в rtltv/locale/en/*.json:\n" + "\n".join(missing[:60])
    assert not wrong, "\n".join(wrong[:60])


def test_plural_forms_have_same_fields():
    """trn(n, one, few, many): у всех трёх русских форм одни и те же {подстановки} (иначе KeyError только по-русски)."""
    bad = []
    for rel, src in SOURCES.items():
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call) and _call_name(node) == "trn" and len(node.args) >= 4:
                forms = [a.value for a in node.args[1:4] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                if len(forms) == 3 and len({frozenset(_fields(f)) for f in forms}) != 1:
                    bad.append(f"  {rel}:{node.lineno}: {forms}")
    assert not bad, "\n".join(bad)


def test_no_stale_catalog_keys():
    """В словаре нет мёртвых ключей (строку в коде поменяли — перевод надо поправить там же)."""
    used = set()
    for rel, src in SOURCES.items():  # включая PENDING: модуль могут переводить по частям
        used.update(k for _l, k, _f, _i in scan(src)[1])
    stale = sorted(set(i18n.catalog("en")) - used)
    assert not stale, "ключи без использования в коде:\n" + "\n".join(f"  {k!r}" for k in stale[:60])


def test_catalog_files_are_valid_json():
    import json
    for path in glob.glob(os.path.join(i18n.LOCALE_DIR, "*", "*.json")):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, dict) and data, path


def test_scan_catches_bare_and_fstrings():
    """Положительный контроль самого сканера."""
    src = ('"""Модуль."""\nfrom rtltv.i18n import tr\n'
           'a = "Привет"\nb = tr("Мир")\nc = f"Частота {x}"\nlog.info("журнал %s", 1)\n'
           'd = "данные"  # noqa: i18n\ne = trn(n, "{n} борт", "{n} борта", "{n} бортов")\n'
           'QMessageBox.warning(None, "Ой", f"Ошибка {e}")\nlogging.getLogger("x").warning("журнал")\n'
           'self.log.error("журнал")\n')
    bad, keys = scan(src)
    assert sorted(n for n, _t in bad) == [3, 5, 9, 9]
    assert [k for _l, k, _f, _i in keys] == ["Мир", "{n} борт", "{n} борта", "{n} бортов"]


# ------------------------------------------------------------------ сам модуль i18n
@pytest.fixture
def lang():
    old = i18n.language()
    yield i18n.set_language
    i18n.set_language(old)


def test_tr_and_plural_forms(lang):
    lang("ru")
    assert [i18n.trn(n, "{n} борт", "{n} борта", "{n} бортов") for n in (1, 2, 5, 11, 21, 104, 112)] == \
        ["1 борт", "2 борта", "5 бортов", "11 бортов", "21 борт", "104 борта", "112 бортов"]
    assert i18n.num(105.3, 1) == "105,3" and i18n.dec("1.5 МГц") == "1,5 МГц"
    assert i18n.tr("Ключ {x}", x=5) == "Ключ 5"
    lang("en")
    assert i18n.num(105.3, 1) == "105.3" and i18n.dec("1.5") == "1.5"
    assert i18n.tr("Нет такого ключа") == "Нет такого ключа"   # без перевода — исходная строка, не падение


def test_english_catalog_used(lang, monkeypatch):
    monkeypatch.setattr(i18n, "catalog", lambda _l="en": {"Привет": "Hello", "{n} борт": ["{n} aircraft", "{n} aircraft"],
                                                          "{n} файл": ["{n} file", "{n} files"]})
    lang("en")
    assert i18n.tr("Привет") == "Hello"
    assert i18n.trn(1, "{n} файл", "{n} файла", "{n} файлов") == "1 file"
    assert i18n.trn(3, "{n} файл", "{n} файла", "{n} файлов") == "3 files"
    assert i18n.trn(3, "{n} борт", "{n} борта", "{n} бортов") == "3 aircraft"


def test_resolve_choice(monkeypatch):
    monkeypatch.delenv("RTLTV_LANG", raising=False)
    monkeypatch.setattr(i18n, "ENGLISH_READY", True)
    monkeypatch.setattr(i18n, "system_language", lambda: "en")
    assert i18n.resolve("auto") == "en" and i18n.resolve("ru") == "ru"
    monkeypatch.setattr(i18n, "system_language", lambda: "ru")
    assert i18n.resolve("auto") == "ru" and i18n.resolve("en") == "en"
    monkeypatch.setenv("RTLTV_LANG", "en")
    assert i18n.resolve() == "en"
    assert i18n.resolve("ru") == "ru"          # явный выбор в «Настройках» сильнее переменной окружения
    monkeypatch.delenv("RTLTV_LANG")
    monkeypatch.setattr(i18n, "ENGLISH_READY", False)
    assert i18n.resolve() == "ru"              # при запуске, пока перевод не готов, — русский
