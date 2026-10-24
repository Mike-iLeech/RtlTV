"""Карточка борта: страна регистрации по ICAO-адресу (офлайн), авиакомпания по позывному (офлайн-таблица
основных + онлайн), регистрация/тип/оператор/маршрут из открытых справочников (api.adsbdb.com, запасной —
hexdb.io). Запросы — в фоновом потоке, с ограничением частоты, таймаутами и кэшем на диске; без сети всё,
что можно определить офлайн, всё равно показывается. Флаги — маленькие PNG с flagcdn.com (кэш на диске).
"""
import bisect
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from . import paths

CACHE_DIR = paths.service_root()
USER_AGENT = "RtlTV/1.1 (personal ADS-B receiver)"
AC_URLS = ("https://api.adsbdb.com/v0/aircraft/{hex}", "https://hexdb.io/api/v1/aircraft/{hex}")
CS_URL = "https://api.adsbdb.com/v0/callsign/{cs}"
CS_URL2 = "https://hexdb.io/callsign-route?callsign={cs}"
FLAG_URL = "https://flagcdn.com/24x18/{cc}.png"
TTL_OK = 60 * 86400        # найденное — 60 дней
TTL_MISS = 3 * 86400       # «не найдено» — 3 дня, потом спросим снова
MIN_INTERVAL = 1.2         # не чаще одного запроса за 1,2 с
TIMEOUT = 6.0

# Блоки 24-битных адресов ICAO (Приложение 10 ИКАО, том III): (начало, конец, ISO-код, страна)
ICAO_RANGES = [
    (0x004000, 0x0043FF, "zw", "Зимбабве"), (0x006000, 0x006FFF, "mz", "Мозамбик"),
    (0x008000, 0x00FFFF, "za", "ЮАР"), (0x010000, 0x017FFF, "eg", "Египет"), (0x018000, 0x01FFFF, "ly", "Ливия"),
    (0x020000, 0x027FFF, "ma", "Марокко"), (0x028000, 0x02FFFF, "tn", "Тунис"),
    (0x030000, 0x0303FF, "bw", "Ботсвана"), (0x032000, 0x032FFF, "bi", "Бурунди"),
    (0x034000, 0x034FFF, "cm", "Камерун"), (0x035000, 0x0353FF, "km", "Коморы"), (0x036000, 0x036FFF, "cg", "Конго"),
    (0x038000, 0x038FFF, "ci", "Кот-д'Ивуар"), (0x03E000, 0x03EFFF, "ga", "Габон"),
    (0x040000, 0x040FFF, "et", "Эфиопия"), (0x042000, 0x042FFF, "gq", "Экваториальная Гвинея"),
    (0x044000, 0x044FFF, "gh", "Гана"), (0x046000, 0x046FFF, "gn", "Гвинея"),
    (0x048000, 0x0483FF, "gw", "Гвинея-Бисау"), (0x04A000, 0x04A3FF, "ls", "Лесото"),
    (0x04C000, 0x04CFFF, "ke", "Кения"), (0x050000, 0x050FFF, "lr", "Либерия"),
    (0x054000, 0x054FFF, "mg", "Мадагаскар"), (0x058000, 0x058FFF, "mw", "Малави"),
    (0x05A000, 0x05A3FF, "mv", "Мальдивы"), (0x05C000, 0x05CFFF, "ml", "Мали"),
    (0x05E000, 0x05E3FF, "mr", "Мавритания"), (0x060000, 0x0603FF, "mu", "Маврикий"),
    (0x062000, 0x062FFF, "ne", "Нигер"), (0x064000, 0x064FFF, "ng", "Нигерия"), (0x068000, 0x068FFF, "ug", "Уганда"),
    (0x06A000, 0x06A3FF, "qa", "Катар"), (0x06C000, 0x06CFFF, "cf", "ЦАР"), (0x06E000, 0x06EFFF, "rw", "Руанда"),
    (0x070000, 0x070FFF, "sn", "Сенегал"), (0x074000, 0x0743FF, "sc", "Сейшелы"),
    (0x076000, 0x0763FF, "sl", "Сьерра-Леоне"), (0x078000, 0x078FFF, "so", "Сомали"),
    (0x07A000, 0x07A3FF, "sz", "Эсватини"), (0x07C000, 0x07CFFF, "sd", "Судан"),
    (0x080000, 0x080FFF, "tz", "Танзания"), (0x084000, 0x084FFF, "td", "Чад"), (0x088000, 0x088FFF, "tg", "Того"),
    (0x08A000, 0x08AFFF, "zm", "Замбия"), (0x08C000, 0x08CFFF, "cd", "ДР Конго"), (0x090000, 0x090FFF, "ao", "Ангола"),
    (0x094000, 0x0943FF, "bj", "Бенин"), (0x096000, 0x0963FF, "cv", "Кабо-Верде"),
    (0x098000, 0x0983FF, "dj", "Джибути"), (0x09A000, 0x09AFFF, "gm", "Гамбия"),
    (0x09C000, 0x09CFFF, "bf", "Буркина-Фасо"), (0x09E000, 0x09E3FF, "st", "Сан-Томе и Принсипи"),
    (0x0A0000, 0x0A7FFF, "dz", "Алжир"), (0x0A8000, 0x0A8FFF, "bs", "Багамы"), (0x0AA000, 0x0AA3FF, "bb", "Барбадос"),
    (0x0AB000, 0x0AB3FF, "bz", "Белиз"), (0x0AC000, 0x0ACFFF, "co", "Колумбия"),
    (0x0AE000, 0x0AEFFF, "cr", "Коста-Рика"), (0x0B0000, 0x0B0FFF, "cu", "Куба"),
    (0x0B2000, 0x0B2FFF, "sv", "Сальвадор"), (0x0B4000, 0x0B4FFF, "gt", "Гватемала"),
    (0x0B6000, 0x0B6FFF, "gy", "Гайана"), (0x0B8000, 0x0B8FFF, "ht", "Гаити"), (0x0BA000, 0x0BAFFF, "hn", "Гондурас"),
    (0x0BC000, 0x0BC3FF, "vc", "Сент-Винсент и Гренадины"), (0x0BE000, 0x0BEFFF, "jm", "Ямайка"),
    (0x0C0000, 0x0C0FFF, "ni", "Никарагуа"), (0x0C2000, 0x0C2FFF, "pa", "Панама"),
    (0x0C4000, 0x0C4FFF, "do", "Доминиканская Республика"), (0x0C6000, 0x0C6FFF, "tt", "Тринидад и Тобаго"),
    (0x0C8000, 0x0C8FFF, "sr", "Суринам"), (0x0CA000, 0x0CA3FF, "ag", "Антигуа и Барбуда"),
    (0x0CC000, 0x0CC3FF, "gd", "Гренада"), (0x0D0000, 0x0D7FFF, "mx", "Мексика"),
    (0x0D8000, 0x0DFFFF, "ve", "Венесуэла"), (0x100000, 0x1FFFFF, "ru", "Россия"),
    (0x201000, 0x2013FF, "na", "Намибия"), (0x202000, 0x2023FF, "er", "Эритрея"),
    (0x300000, 0x33FFFF, "it", "Италия"), (0x340000, 0x37FFFF, "es", "Испания"), (0x380000, 0x3BFFFF, "fr", "Франция"),
    (0x3C0000, 0x3FFFFF, "de", "Германия"), (0x400000, 0x43FFFF, "gb", "Великобритания"),
    (0x440000, 0x447FFF, "at", "Австрия"), (0x448000, 0x44FFFF, "be", "Бельгия"),
    (0x450000, 0x457FFF, "bg", "Болгария"), (0x458000, 0x45FFFF, "dk", "Дания"),
    (0x460000, 0x467FFF, "fi", "Финляндия"), (0x468000, 0x46FFFF, "gr", "Греция"),
    (0x470000, 0x477FFF, "hu", "Венгрия"), (0x478000, 0x47FFFF, "no", "Норвегия"),
    (0x480000, 0x487FFF, "nl", "Нидерланды"), (0x488000, 0x48FFFF, "pl", "Польша"),
    (0x490000, 0x497FFF, "pt", "Португалия"), (0x498000, 0x49FFFF, "cz", "Чехия"),
    (0x4A0000, 0x4A7FFF, "ro", "Румыния"), (0x4A8000, 0x4AFFFF, "se", "Швеция"),
    (0x4B0000, 0x4B7FFF, "ch", "Швейцария"), (0x4B8000, 0x4BFFFF, "tr", "Турция"),
    (0x4C0000, 0x4C7FFF, "rs", "Сербия"), (0x4C8000, 0x4C83FF, "cy", "Кипр"), (0x4CA000, 0x4CAFFF, "ie", "Ирландия"),
    (0x4CC000, 0x4CCFFF, "is", "Исландия"), (0x4D0000, 0x4D03FF, "lu", "Люксембург"),
    (0x4D2000, 0x4D23FF, "mt", "Мальта"), (0x4D4000, 0x4D43FF, "mc", "Монако"),
    (0x500000, 0x5003FF, "sm", "Сан-Марино"), (0x501000, 0x5013FF, "al", "Албания"),
    (0x501C00, 0x501FFF, "hr", "Хорватия"), (0x502C00, 0x502FFF, "lv", "Латвия"),
    (0x503C00, 0x503FFF, "lt", "Литва"), (0x504C00, 0x504FFF, "md", "Молдова"),
    (0x505C00, 0x505FFF, "sk", "Словакия"), (0x506C00, 0x506FFF, "si", "Словения"),
    (0x507C00, 0x507FFF, "uz", "Узбекистан"), (0x508000, 0x50FFFF, "ua", "Украина"),
    (0x510000, 0x5103FF, "by", "Беларусь"), (0x511000, 0x5113FF, "ee", "Эстония"),
    (0x512000, 0x5123FF, "mk", "Северная Македония"), (0x513000, 0x5133FF, "ba", "Босния и Герцеговина"),
    (0x514000, 0x5143FF, "ge", "Грузия"), (0x515000, 0x5153FF, "tj", "Таджикистан"),
    (0x516000, 0x5163FF, "me", "Черногория"), (0x600000, 0x6003FF, "am", "Армения"),
    (0x600800, 0x600BFF, "az", "Азербайджан"), (0x601000, 0x6013FF, "kg", "Киргизия"),
    (0x601800, 0x601BFF, "tm", "Туркменистан"), (0x680000, 0x6803FF, "bt", "Бутан"),
    (0x681000, 0x6813FF, "fm", "Микронезия"), (0x682000, 0x6823FF, "mn", "Монголия"),
    (0x683000, 0x6833FF, "kz", "Казахстан"), (0x684000, 0x6843FF, "pw", "Палау"),
    (0x700000, 0x700FFF, "af", "Афганистан"), (0x702000, 0x702FFF, "bd", "Бангладеш"),
    (0x704000, 0x704FFF, "mm", "Мьянма"), (0x706000, 0x706FFF, "kw", "Кувейт"), (0x708000, 0x708FFF, "la", "Лаос"),
    (0x70A000, 0x70AFFF, "np", "Непал"), (0x70C000, 0x70C3FF, "om", "Оман"), (0x70E000, 0x70EFFF, "kh", "Камбоджа"),
    (0x710000, 0x717FFF, "sa", "Саудовская Аравия"), (0x718000, 0x71FFFF, "kr", "Республика Корея"),
    (0x720000, 0x727FFF, "kp", "КНДР"), (0x728000, 0x72FFFF, "iq", "Ирак"), (0x730000, 0x737FFF, "ir", "Иран"),
    (0x738000, 0x73FFFF, "il", "Израиль"), (0x740000, 0x747FFF, "jo", "Иордания"),
    (0x748000, 0x74FFFF, "lb", "Ливан"), (0x750000, 0x757FFF, "my", "Малайзия"),
    (0x758000, 0x75FFFF, "ph", "Филиппины"), (0x760000, 0x767FFF, "pk", "Пакистан"),
    (0x768000, 0x76FFFF, "sg", "Сингапур"), (0x770000, 0x777FFF, "lk", "Шри-Ланка"),
    (0x778000, 0x77FFFF, "sy", "Сирия"), (0x780000, 0x7BFFFF, "cn", "Китай"),
    (0x7C0000, 0x7FFFFF, "au", "Австралия"), (0x800000, 0x83FFFF, "in", "Индия"),
    (0x840000, 0x87FFFF, "jp", "Япония"), (0x880000, 0x887FFF, "th", "Таиланд"),
    (0x888000, 0x88FFFF, "vn", "Вьетнам"), (0x890000, 0x890FFF, "ye", "Йемен"),
    (0x894000, 0x894FFF, "bh", "Бахрейн"), (0x895000, 0x8953FF, "bn", "Бруней"), (0x896000, 0x896FFF, "ae", "ОАЭ"),
    (0x897000, 0x8973FF, "sb", "Соломоновы Острова"), (0x898000, 0x898FFF, "pg", "Папуа — Новая Гвинея"),
    (0x899000, 0x8993FF, "tw", "Тайвань"), (0x8A0000, 0x8A7FFF, "id", "Индонезия"),
    (0x900000, 0x9003FF, "mh", "Маршалловы Острова"), (0x901000, 0x9013FF, "ck", "Острова Кука"),
    (0x902000, 0x9023FF, "ws", "Самоа"), (0xA00000, 0xAFFFFF, "us", "США"), (0xC00000, 0xC3FFFF, "ca", "Канада"),
    (0xC80000, 0xC87FFF, "nz", "Новая Зеландия"), (0xC88000, 0xC88FFF, "fj", "Фиджи"),
    (0xC8A000, 0xC8A3FF, "nr", "Науру"), (0xC8C000, 0xC8C3FF, "lc", "Сент-Люсия"),
    (0xC8D000, 0xC8D3FF, "to", "Тонга"), (0xC8E000, 0xC8E3FF, "ki", "Кирибати"), (0xC90000, 0xC903FF, "vu", "Вануату"),
    (0xE00000, 0xE3FFFF, "ar", "Аргентина"), (0xE40000, 0xE7FFFF, "br", "Бразилия"), (0xE80000, 0xE80FFF, "cl", "Чили"),
    (0xE84000, 0xE84FFF, "ec", "Эквадор"), (0xE88000, 0xE88FFF, "py", "Парагвай"), (0xE8C000, 0xE8CFFF, "pe", "Перу"),
    (0xE90000, 0xE90FFF, "uy", "Уругвай"), (0xE94000, 0xE94FFF, "bo", "Боливия"),
]
ICAO_RANGES.sort()
_STARTS = [r[0] for r in ICAO_RANGES]

# ICAO-коды основных авиакомпаний (офлайн; остальное — из онлайн-справочника): код → (название, страна ISO)
AIRLINES = {
    "AFL": ("Аэрофлот", "ru"), "SBI": ("S7 Airlines", "ru"), "SDM": ("Россия", "ru"), "UTA": ("ЮТэйр", "ru"),
    "PBD": ("Победа", "ru"), "SVR": ("Уральские авиалинии", "ru"), "NWS": ("Northwind (Северный ветер)", "ru"),
    "AZV": ("Азимут", "ru"), "RWZ": ("Red Wings", "ru"), "AUL": ("Smartavia", "ru"), "KAR": ("Pegas Fly (Икар)", "ru"),
    "SYL": ("Якутия", "ru"), "GZP": ("Газпром авиа", "ru"), "TYA": ("Нордстар", "ru"), "DRU": ("Алроса", "ru"),
    "RLU": ("Руслайн", "ru"), "VDA": ("Волга-Днепр", "ru"), "ABW": ("AirBridgeCargo", "ru"),
    "BRU": ("Белавиа", "by"), "UZB": ("Uzbekistan Airways", "uz"), "KZR": ("Air Astana", "kz"),
    "SMR": ("Somon Air", "tj"), "TZK": ("Tajik Air", "tj"), "AHY": ("Azerbaijan Airlines", "az"),
    "TGZ": ("Georgian Airways", "ge"), "MGL": ("MIAT Mongolian Airlines", "mn"),
    "THY": ("Turkish Airlines", "tr"), "PGT": ("Pegasus", "tr"), "SXS": ("SunExpress", "tr"),
    "UAE": ("Emirates", "ae"), "FDB": ("flydubai", "ae"), "ETD": ("Etihad", "ae"), "ABY": ("Air Arabia", "ae"),
    "QTR": ("Qatar Airways", "qa"), "SVA": ("Saudia", "sa"), "GFA": ("Gulf Air", "bh"), "OMA": ("Oman Air", "om"),
    "KAC": ("Kuwait Airways", "kw"), "IRA": ("Iran Air", "ir"), "IRM": ("Mahan Air", "ir"),
    "IAW": ("Iraqi Airways", "iq"), "RJA": ("Royal Jordanian", "jo"), "MEA": ("Middle East Airlines", "lb"),
    "ELY": ("El Al", "il"), "MSR": ("EgyptAir", "eg"), "ETH": ("Ethiopian Airlines", "et"),
    "CCA": ("Air China", "cn"), "CES": ("China Eastern", "cn"), "CSN": ("China Southern", "cn"),
    "CHH": ("Hainan Airlines", "cn"), "CXA": ("Xiamen Airlines", "cn"), "CPA": ("Cathay Pacific", "hk"),
    "EVA": ("EVA Air", "tw"), "KAL": ("Korean Air", "kr"), "AAR": ("Asiana", "kr"), "JAL": ("Japan Airlines", "jp"),
    "ANA": ("All Nippon Airways", "jp"), "THA": ("Thai Airways", "th"), "HVN": ("Vietnam Airlines", "vn"),
    "VJC": ("VietJet", "vn"), "SIA": ("Singapore Airlines", "sg"), "AIC": ("Air India", "in"),
    "IGO": ("IndiGo", "in"), "PIA": ("Pakistan International", "pk"), "QFA": ("Qantas", "au"),
    "KLM": ("KLM", "nl"), "DLH": ("Lufthansa", "de"), "AFR": ("Air France", "fr"), "BAW": ("British Airways", "gb"),
    "SWR": ("Swiss", "ch"), "AUA": ("Austrian", "at"), "ITY": ("ITA Airways", "it"), "RYR": ("Ryanair", "ie"),
    "EZY": ("easyJet", "gb"), "WZZ": ("Wizz Air", "hu"), "SAS": ("SAS", "se"), "FIN": ("Finnair", "fi"),
    "LOT": ("LOT", "pl"), "CSA": ("Czech Airlines", "cz"), "TAP": ("TAP Air Portugal", "pt"), "IBE": ("Iberia", "es"),
    "VLG": ("Vueling", "es"), "NAX": ("Norwegian", "no"), "ICE": ("Icelandair", "is"), "EIN": ("Aer Lingus", "ie"),
    "BEL": ("Brussels Airlines", "be"), "AEE": ("Aegean", "gr"), "UAL": ("United Airlines", "us"),
    "AAL": ("American Airlines", "us"), "DAL": ("Delta", "us"), "ACA": ("Air Canada", "ca"),
}

AIRLINE_CS = re.compile(r"^([A-Z]{3})(\d[0-9A-Z]{0,4})$")
IATA_CS = re.compile(r"^[A-Z0-9]{2}\d{1,4}[A-Z]?$")   # рейс с IATA-кодом (FV6555, SU1124) — не частный
REG_CS = re.compile(r"^(RA|RF|EW|UR|UK|UP|4K|4L|EK|EX|EY|EZ|[A-Z])-?[0-9A-Z]{2,6}$")


def country_by_icao(icao):
    """24-битный адрес → (iso, страна) или (None, None)."""
    i = bisect.bisect_right(_STARTS, int(icao)) - 1
    if i >= 0:
        a, b, cc, name = ICAO_RANGES[i]
        if a <= icao <= b:
            return cc, name
    return None, None


def airline_by_callsign(cs):
    """Позывной → (код ИКАО, название|None, страна|None) для формата «ААА123»; иначе None."""
    cs = (cs or "").strip().upper()
    m = AIRLINE_CS.match(cs)
    if not m:
        return None
    code = m.group(1)
    name, cc = AIRLINES.get(code, (None, None))
    return code, name, cc


def looks_like_registration(cs):
    cs = (cs or "").strip().upper()
    return bool(cs) and not AIRLINE_CS.match(cs) and not IATA_CS.match(cs) and bool(REG_CS.match(cs))


class AircraftInfo:
    """Справочник: card() отвечает сразу (из кэша и офлайн-таблиц), недостающее догружает фоновый поток."""

    def __init__(self, cache_dir=CACHE_DIR, online=True):
        self.dir = cache_dir
        self.path = os.path.join(cache_dir, "aircraft_cache.json")
        self.flag_dir = os.path.join(cache_dir, "flags")
        self.online = online      # запросы карточек бортов и маршрутов
        self.net = online         # сеть вообще (флаги можно качать и для демо-бортов)
        self.lock = threading.Lock()
        self.ac, self.cs = {}, {}
        self.dirty = False
        self.q = queue.Queue()
        self.pending = set()
        self.thread = None
        self.running = False
        self.fail = 0
        self.paused_until = 0.0
        self.t_last = 0.0
        self.requests = 0
        self.last_error = None
        self._load()

    # ---------- кэш
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.ac, self.cs = d.get("ac", {}), d.get("cs", {})
        except (OSError, ValueError):
            pass

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            data = json.dumps({"ac": self.ac, "cs": self.cs}, ensure_ascii=False)
            self.dirty = False
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self.path)
        except OSError:
            pass

    @staticmethod
    def _fresh(rec):
        if not rec:
            return False
        return time.time() - rec.get("t", 0) < (TTL_OK if rec.get("ok") else TTL_MISS)

    # ---------- фон
    def start(self):
        if self.net and not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def stop(self):
        self.running = False
        self.save()

    def _want(self, kind, key):
        if not (self.net if kind == "flag" else self.online):
            return
        with self.lock:
            if (kind, key) in self.pending:
                return
            self.pending.add((kind, key))
        self.q.put((kind, key))

    def _http(self, url, raw=False):
        wait = MIN_INTERVAL - (time.time() - self.t_last)
        if wait > 0:
            time.sleep(wait)
        self.t_last = time.time()
        self.requests += 1
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None if raw else {}
            raise
        return data if raw else json.loads(data.decode("utf-8", "replace") or "{}")

    def _run(self):
        while self.running:
            try:
                kind, key = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if time.time() < self.paused_until:
                    continue  # сеть недоступна — попробуем позже (ключ снимем с ожидания)
                if kind == "ac":
                    self._fetch_ac(key)
                elif kind == "cs":
                    self._fetch_cs(key)
                elif kind == "flag":
                    self._fetch_flag(key)
                self.fail = 0
            except (OSError, ValueError, urllib.error.URLError) as e:
                self.last_error = str(e)
                self.fail += 1
                if self.fail >= 3:
                    self.paused_until = time.time() + 300
            finally:
                with self.lock:
                    self.pending.discard((kind, key))

    def _fetch_ac(self, hx):
        rec = {"t": time.time(), "ok": False}
        d = self._http(AC_URLS[0].format(hex=hx))
        a = (d.get("response") or {}) if isinstance(d, dict) else {}
        a = a.get("aircraft") if isinstance(a, dict) else None
        if a:
            rec.update(ok=True, reg=a.get("registration"), type=a.get("type"), icao_type=a.get("icao_type"),
                       manufacturer=a.get("manufacturer"), owner=a.get("registered_owner"),
                       owner_cc=(a.get("registered_owner_country_iso_name") or "").lower() or None,
                       owner_country=a.get("registered_owner_country_name"),
                       op_code=a.get("registered_owner_operator_flag_code"), src="adsbdb.com")
        else:
            h = self._http(AC_URLS[1].format(hex=hx))
            if isinstance(h, dict) and h.get("Registration"):
                rec.update(ok=True, reg=h.get("Registration"), type=h.get("Type"), icao_type=h.get("ICAOTypeCode"),
                           manufacturer=h.get("Manufacturer"), owner=h.get("RegisteredOwners"),
                           op_code=h.get("OperatorFlagCode"), src="hexdb.io")
        with self.lock:
            self.ac[hx] = rec
            self.dirty = True

    def _fetch_cs(self, cs):
        rec = {"t": time.time(), "ok": False}
        d = self._http(CS_URL.format(cs=cs))
        fr = (d.get("response") or {}) if isinstance(d, dict) else {}
        fr = fr.get("flightroute") if isinstance(fr, dict) else None
        if fr:
            al = fr.get("airline") or {}
            o, de = fr.get("origin") or {}, fr.get("destination") or {}

            def place(p):
                if not p:
                    return None
                return {"iata": p.get("iata_code"), "icao": p.get("icao_code"), "city": p.get("municipality"),
                        "name": p.get("name"), "cc": (p.get("country_iso_name") or "").lower() or None}
            rec.update(ok=True, airline=al.get("name"), airline_icao=al.get("icao"),
                       airline_cc=(al.get("country_iso") or "").lower() or None,
                       origin=place(o), dest=place(de), src="adsbdb.com")
        else:
            txt = self._http(CS_URL2.format(cs=cs), raw=True)
            txt = (txt or b"").decode("ascii", "ignore").strip()
            if re.match(r"^[A-Z]{4}-[A-Z]{4}$", txt):
                a, b = txt.split("-")
                rec.update(ok=True, origin={"icao": a}, dest={"icao": b}, src="hexdb.io")
        with self.lock:
            self.cs[cs] = rec
            self.dirty = True

    def _fetch_flag(self, cc):
        path = self.flag_path(cc, any_state=True)
        data = self._http(FLAG_URL.format(cc=cc), raw=True)
        if data and data[:4] == b"\x89PNG":
            os.makedirs(self.flag_dir, exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)

    # ---------- ответ интерфейсу
    def flag_path(self, cc, any_state=False):
        if not cc:
            return None
        p = os.path.join(self.flag_dir, f"{cc}.png")
        if any_state or os.path.exists(p):
            return p
        self._want("flag", cc)
        return None

    def card(self, icao, callsign=None):
        """Сводная карточка; пока онлайн-данных нет — ставит их в очередь и отдаёт то, что известно."""
        hx = f"{int(icao):06X}"
        cs = (callsign or "").strip().upper() or None
        cc, country = country_by_icao(icao)
        c = {"hex": hx, "callsign": cs, "cc": cc, "country": country, "reg": None, "type": None,
             "icao_type": None, "manufacturer": None, "owner": None, "airline": None, "airline_code": None,
             "airline_cc": None, "private": None, "origin": None, "dest": None, "pending": False, "src": None}
        with self.lock:
            ra = self.ac.get(hx)
            rc = self.cs.get(cs) if cs else None
        if not self._fresh(ra):
            self._want("ac", hx)
            c["pending"] = self.online
        if ra and ra.get("ok"):
            for k in ("reg", "type", "icao_type", "manufacturer", "owner"):
                c[k] = ra.get(k)
            c["src"] = ra.get("src")
            if ra.get("owner_cc") and not cc:
                c["cc"], c["country"] = ra["owner_cc"], ra.get("owner_country")
        al = airline_by_callsign(cs)
        if al:
            code, name, acc = al
            c["airline_code"], c["airline"], c["airline_cc"] = code, name, acc
            if cs and not self._fresh(rc):
                self._want("cs", cs)
                c["pending"] = c["pending"] or self.online
            if rc and rc.get("ok"):
                c["airline"] = c["airline"] or rc.get("airline")
                c["airline_cc"] = c["airline_cc"] or rc.get("airline_cc")
                c["origin"], c["dest"] = rc.get("origin"), rc.get("dest")
            if not c["airline"] and ra and ra.get("ok") and ra.get("op_code") == code:
                c["airline"] = ra.get("owner")
            c["private"] = False
        elif cs:
            # позывной не в формате авиакомпании: обычно это регистрация — частный/служебный борт,
            # если только справочник не знает оператора-авиакомпанию
            op = ra.get("op_code") if (ra and ra.get("ok")) else None
            c["private"] = (not op) if looks_like_registration(cs) else None
            if op:
                c["airline_code"] = op
                c["airline"] = AIRLINES.get(op, (ra.get("owner"), None))[0]
        return c
