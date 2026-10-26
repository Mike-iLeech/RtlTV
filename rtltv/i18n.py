"""Язык интерфейса: русский (исходный) и английский.

Как пользоваться в коде:
    from .i18n import tr, trn, N_, num
    QLabel(tr("Частота"))                                  # простая строка
    tr("Найдено {n} мест, всего {size}", n=3, size=s)      # вместо f-строки — именованные подстановки
    trn(n, "{n} борт", "{n} борта", "{n} бортов")          # множественное число: 1 / 2–4 / 5+
    THEMES = {N_("Тёмная"): ...}; tr(name)                 # пометить строку при импорте, перевести при показе
    num(105.3, 1)                                          # «105,3» по-русски, «105.3» по-английски

Ключ перевода — сама русская строка. Английские строки лежат в rtltv/locale/en/*.json ({"русская": "english"};
для trn — {"<форма one>": ["one", "other"]}). Файлы разбиты по модулям, при загрузке сливаются в один словарь.
Строки без перевода показываются по-русски, а тест tests/test_i18n.py не даёт им попасть в main.

Почему не Qt Linguist (.ts/.qm): половина строк живёт вне QObject (декодеры, справочники, обновлятор), для
.qm нужен шаг сборки lrelease и контексты по классам, а JSON проверяется обычным pytest без Qt и читается в diff.
Стандартные кнопки Qt (Yes/No, Cancel в QFileDialog…) переводит штатный qtbase_*.qm — install_qt_translator().

Язык выбирается в «Настройках» (app.json "language": "auto" | "ru" | "en"); "auto" — язык Windows.
tr() вызывается при построении окна, поэтому смена языка — пересоздание окон (Launcher.relaunch), а на уровне
модуля строки только помечаются N_() и переводятся при показе.
"""
import glob
import json
import os
import re

LANGS = {"ru": "Русский", "en": "English"}  # noqa: i18n — название языка всегда на нём самом
# пока английский перевод не готов целиком, «как в системе» = русский, а выбор языка в настройках скрыт;
# RTLTV_LANG=en включает английский принудительно (тесты, проверка перевода)
ENGLISH_READY = False
# языки системы, для которых по умолчанию русский интерфейс (остальным — английский)
_RU_SYSTEM = ("ru", "be", "kk", "ky", "tg")

LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")

_lang = None
_cat = None
_qt_translators = []


def system_language():
    """Язык Windows (интерфейса): 'ru' для русскоязычных систем, иначе 'en'."""
    code = ""
    try:
        from PySide6.QtCore import QLocale
        langs = QLocale.system().uiLanguages()
        code = (langs[0] if langs else QLocale.system().name()).lower()
    except Exception:  # noqa: BLE001 — без Qt (установщик --configure) смотрим окружение
        import locale
        code = (locale.getlocale()[0] or os.environ.get("LANG", "")).lower()
    return "ru" if code[:2] in _RU_SYSTEM or code.startswith("russian") else "en"


def configured():
    """Выбор пользователя: 'auto' | 'ru' | 'en'."""
    from . import settings
    v = settings.load("app", {}).get("language", "auto")
    return v if v in LANGS or v == "auto" else "auto"


def resolve(choice=None):
    """Какой язык включить при выборе choice ('auto' | 'ru' | 'en')."""
    env = os.environ.get("RTLTV_LANG", "").lower()
    if env in LANGS:
        return env
    if not ENGLISH_READY:
        return "ru"
    choice = configured() if choice is None else choice
    return choice if choice in LANGS else system_language()


def selectable():
    """Показывать ли выбор языка (до готовности перевода — только при RTLTV_LANG)."""
    return ENGLISH_READY or os.environ.get("RTLTV_LANG", "").lower() in LANGS


def language():
    """Текущий язык интерфейса: 'ru' или 'en'."""
    global _lang
    if _lang is None:
        _lang = resolve()
    return _lang


def set_language(code):
    """Переключить язык в этом процессе (новые окна строятся уже на нём)."""
    global _lang, _cat
    _lang = code if code in LANGS else "ru"
    _cat = None
    if _qt_translators:
        install_qt_translator()


def catalog(lang="en"):
    """Словарь переводов {русская строка: перевод} из rtltv/locale/<lang>/*.json."""
    out = {}
    for path in sorted(glob.glob(os.path.join(LOCALE_DIR, lang, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                out.update(json.load(f))
        except (OSError, ValueError):
            pass
    return out


def _table():
    global _cat
    if _cat is None:
        _cat = catalog(language()) if language() != "ru" else {}
    return _cat


def N_(text):
    """Пометить строку для перевода, не переводя (на уровне модуля; переводится потом через tr(var))."""
    return text


def tr(text, /, **kw):
    """Перевести русскую строку на язык интерфейса и подставить {имена} из kw."""
    s = _table().get(text, text) if language() != "ru" else text
    if isinstance(s, list):  # ключ множественной формы, вызванный как tr — берём «other»
        s = s[-1]
    return s.format(**kw) if kw else s


def _ru_form(n):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return 0
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return 1
    return 2


def trn(n, one, few, many, /, **kw):
    """Множественное число: trn(5, "{n} борт", "{n} борта", "{n} бортов") → «5 бортов» / «5 aircraft».
    В английском словаре ключ — форма one, значение — [one, other]."""
    kw.setdefault("n", n)
    if language() == "ru":
        s = (one, few, many)[_ru_form(n)]
    else:
        forms = _table().get(one)
        if isinstance(forms, list) and forms:
            s = forms[0] if abs(n) == 1 else forms[-1]
        else:
            s = (one, few, many)[_ru_form(n)]
    return s.format(**kw)


def num(x, nd=1):
    """Число с nd знаками после запятой: десятичный разделитель — запятая по-русски, точка по-английски."""
    s = f"{x:.{nd}f}"
    return s.replace(".", ",") if language() == "ru" else s


def dec(s):
    """Поправить десятичный разделитель в уже готовой строке с числом ('105.3' → '105,3' по-русски)."""
    s = str(s)
    return re.sub(r"(?<=\d)\.(?=\d)", ",", s) if language() == "ru" else s


def install_qt_translator(app=None):
    """Перевод стандартных кнопок и диалогов Qt (qtbase_ru.qm из PySide6) на язык интерфейса."""
    from PySide6.QtCore import QCoreApplication, QLibraryInfo, QTranslator
    app = app or QCoreApplication.instance()
    if app is None:
        return
    for t in _qt_translators:
        app.removeTranslator(t)
    _qt_translators.clear()
    if language() == "en":
        return
    import PySide6
    t = QTranslator(app)
    for path in (QLibraryInfo.path(QLibraryInfo.TranslationsPath),
                 os.path.join(os.path.dirname(PySide6.__file__), "translations")):   # в сборке PyInstaller
        if t.load(f"qtbase_{language()}", path):
            app.installTranslator(t)
            _qt_translators.append(t)
            return
