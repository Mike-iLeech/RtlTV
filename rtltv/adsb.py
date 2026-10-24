"""ADS-B / Mode S на 1090 МГц: демодулятор IQ, декодер сообщений, таблица бортов, демо-генератор.

Цепочка:  IQ complex64 (2,0 или 2,4 MS/s) → Demodulator.feed() → кадры 56/112 бит с проверенной CRC
          → decode_frame() → Tracker.apply() (CPR-позиции, скорость, высота, сквок, устаревание 60 с).
AdsbProcessor — поток-обработчик (очередь блоков от источника), DemoSource — синтетический эфир
с бортами, летящими по маршрутам (DF17/DF4/DF5/DF11 кодируются прямо в IQ): режим проверяется без антенны.

Поиск преамбулы и PPM-демодуляция векторизованы numpy (по всему блоку разом), CRC-24 считается
матрично в GF(2): синдром = XOR вкладов установленных битов. Для DF17/18 исправляется 1 ошибочный бит.
"""
import json
import math
import os
import queue
import random
import threading
import time
from collections import deque

import numpy as np

from . import rtlsdr
from .source import BLOCK_BYTES, SourceBase
from . import paths

ADSB_FREQ = 1_090_000_000
FS = 2_000_000
STALE_S = 60.0          # борт пропадает из таблицы, если 60 с не было сообщений
CPR_PAIR_S = 10.0       # чётное/нечётное сообщение для глобального CPR — не дальше 10 с друг от друга
MAX_RANGE_KM = 700.0    # дальше от приёмника ADS-B на 1090 не слышно — значит, ошибка или подмена GNSS
SINGLE_RANGE_KM = 300.0  # одиночное CPR декодируем от приёмника, только если результат ближе этого
JUMP_ACCEPT = 3          # столько согласованных «скачков» подряд — принимаем новое место (борт не там, где думали)
FT = 0.3048
KT_KMH = 1.852

# ---------------------------------------------------------------- CRC-24 Mode S
_POLY = 0xFFF409
_CRC_TABLE = []
for _i in range(256):
    _c = _i << 16
    for _ in range(8):
        _c = ((_c << 1) ^ _POLY) if _c & 0x800000 else (_c << 1)
    _CRC_TABLE.append(_c & 0xFFFFFF)


def crc24(data):
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFF) ^ _CRC_TABLE[((crc >> 16) ^ b) & 0xFF]
    return crc


def syndrome(frame):
    """CRC данных XOR поле чётности. 0 — кадр целый (DF11/17/18); для DF0/4/5/16/20/21 — это ICAO."""
    frame = bytes(frame)
    return crc24(frame[:-3]) ^ int.from_bytes(frame[-3:], "big")


def _bit_contrib():
    c = np.zeros(112, dtype=np.int64)
    for i in range(112):
        b = (1 << (111 - i)).to_bytes(14, "big")
        c[i] = syndrome(b)
    return c


_CONTRIB = _bit_contrib()  # вклад i-го бита 112-битного кадра в синдром (у 56-битного — с 56-го)
_CBITS = ((_CONTRIB[:, None] >> np.arange(23, -1, -1)) & 1).astype(np.float32)
_POW24 = (1 << np.arange(23, -1, -1)).astype(np.int64)
_FIX1 = {int(_CONTRIB[i]): i for i in range(5, 112)}  # синдром одиночной ошибки → номер бита (DF не трогаем)
_FIX1_KEYS = np.array(sorted(_FIX1), dtype=np.int64)
LONG_DF = (16, 17, 18, 19, 20, 21, 24)
SHORT_DF = (0, 4, 5, 11)
_VALID_DF = (0, 4, 5, 11, 16, 17, 18, 20, 21)

# ---------------------------------------------------------------- поля Mode S
CHARSET = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"

CATEGORIES = {
    (4, 1): "A1 лёгкий (< 7 т)", (4, 2): "A2 малый (7–34 т)", (4, 3): "A3 большой (34–136 т)",
    (4, 4): "A4 большой с сильным вихрем (B757)", (4, 5): "A5 тяжёлый (> 136 т)",
    (4, 6): "A6 высокоманёвренный", (4, 7): "A7 вертолёт",
    (3, 1): "B1 планер", (3, 2): "B2 аэростат", (3, 3): "B3 парашютист", (3, 4): "B4 дельтаплан/параплан",
    (3, 6): "B6 беспилотник", (3, 7): "B7 космический аппарат",
    (2, 1): "C1 аварийная машина", (2, 3): "C3 служебная машина", (2, 4): "C4 точечное препятствие",
    (2, 5): "C5 групповое препятствие", (2, 6): "C6 протяжённое препятствие",
}

SQUAWK_NOTES = {"7500": "захват", "7600": "отказ связи", "7700": "бедствие", "7000": "ПВП", "2000": "ППП без кода"}


def _id13_to_hex(id13):
    """13 бит поля ID/AC (C1 A1 C2 A2 C4 A4 X B1 D1 B2 D2 B4 D4) → 0xABCD (по три бита на цифру)."""
    b = lambda k: (id13 >> k) & 1  # noqa: E731
    a = (b(7) << 2) | (b(9) << 1) | b(11)
    bb = (b(1) << 2) | (b(3) << 1) | b(5)
    c = (b(8) << 2) | (b(10) << 1) | b(12)
    d = (b(0) << 2) | (b(2) << 1) | b(4)
    return (a << 12) | (bb << 8) | (c << 4) | d


def _hex_to_id13(h):
    a, bb, c, d = (h >> 12) & 7, (h >> 8) & 7, (h >> 4) & 7, h & 7
    r = 0
    for bit, val in ((7, a >> 2), (9, (a >> 1) & 1), (11, a & 1), (1, bb >> 2), (3, (bb >> 1) & 1), (5, bb & 1),
                     (8, c >> 2), (10, (c >> 1) & 1), (12, c & 1), (0, d >> 2), (2, (d >> 1) & 1), (4, d & 1)):
        r |= val << bit
    return r


def decode_squawk(id13):
    h = _id13_to_hex(id13)
    return f"{(h >> 12) & 7}{(h >> 8) & 7}{(h >> 4) & 7}{h & 7}"


def encode_squawk(code):
    s = str(code).zfill(4)
    h = (int(s[0]) << 12) | (int(s[1]) << 8) | (int(s[2]) << 4) | int(s[3])
    return _hex_to_id13(h)


def gillham_to_ft(h):
    """Код Гиллхэма (раскладка 0xABCD как у Mode A) → высота в футах или None (алгоритм dump1090)."""
    if (h & 0xFFFF8889) or (h & 0x00F0) == 0:
        return None
    hund = 0
    if h & 0x0010:
        hund ^= 7
    if h & 0x0020:
        hund ^= 3
    if h & 0x0040:
        hund ^= 1
    if (hund & 5) == 5:
        hund ^= 2
    if hund > 5:
        return None
    five = 0
    for mask, x in ((0x0002, 0xFF), (0x0004, 0x7F), (0x1000, 0x3F), (0x2000, 0x1F), (0x4000, 0x0F),
                    (0x0100, 0x07), (0x0200, 0x03), (0x0400, 0x01)):
        if h & mask:
            five ^= x
    if five & 1:
        hund = 6 - hund
    return (five * 5 + hund - 13) * 100


def decode_ac13(ac):
    """Поле AC (13 бит) DF0/4/16/20 → футы. M=1 (метры) встречается крайне редко — переводим в футы."""
    if ac == 0:
        return None
    if ac & 0x40:  # M-бит: высота в метрах
        n = ((ac & 0x1F80) >> 1) | (ac & 0x3F)
        return round(n / FT) if n else None
    if ac & 0x10:  # Q-бит: шаг 25 футов
        n = ((ac & 0x1F80) >> 2) | ((ac & 0x20) >> 1) | (ac & 0x0F)
        return n * 25 - 1000
    return gillham_to_ft(_id13_to_hex(ac))


def decode_ac12(ac):
    """Поле высоты (12 бит) в позиционном сообщении ADS-B → футы."""
    if ac == 0:
        return None
    if ac & 0x10:
        n = ((ac & 0xFE0) >> 1) | (ac & 0x0F)
        return n * 25 - 1000
    return gillham_to_ft(_id13_to_hex(((ac & 0xFC0) << 1) | (ac & 0x3F)))


def encode_ac12(alt_ft):
    n = int(round((alt_ft + 1000) / 25.0))
    n = max(0, min(n, 0x7FF))
    return ((n & 0x7F0) << 1) | 0x10 | (n & 0x0F)


def encode_ac13(alt_ft):
    n = int(round((alt_ft + 1000) / 25.0))
    n = max(0, min(n, 0x7FF))
    return ((n & 0x7E0) << 2) | ((n & 0x10) << 1) | 0x10 | (n & 0x0F)


# ---------------------------------------------------------------- CPR
NZ = 15


def cpr_nl(lat):
    """Число долготных зон NL(lat)."""
    lat = abs(lat)
    if lat < 1e-9:
        return 59
    if abs(lat - 87.0) < 1e-9:
        return 2
    if lat > 87.0:
        return 1
    a = 1 - math.cos(math.pi / (2 * NZ))
    b = math.cos(math.pi / 180.0 * lat) ** 2
    return int(math.floor(2 * math.pi / math.acos(1 - a / b)))


def _mod(a, b):
    return a - b * math.floor(a / b)


def cpr_global(even, odd, newest_odd):
    """Глобальное декодирование воздушной позиции. even/odd = (lat_cpr, lon_cpr) целыми 0..131071."""
    lat0, lon0 = even[0] / 131072.0, even[1] / 131072.0
    lat1, lon1 = odd[0] / 131072.0, odd[1] / 131072.0
    dlat0, dlat1 = 360.0 / 60, 360.0 / 59
    j = math.floor(59 * lat0 - 60 * lat1 + 0.5)
    late = dlat0 * (_mod(j, 60) + lat0)
    lato = dlat1 * (_mod(j, 59) + lat1)
    if late >= 270:
        late -= 360
    if lato >= 270:
        lato -= 360
    if abs(late) > 90 or abs(lato) > 90:
        return None
    if abs(late - lato) > 0.1:
        return None  # за ≤10 с борт не смещается на 11 км: пара от разных мест (подмена GNSS) — не декодируем
    nl = cpr_nl(late)
    if nl != cpr_nl(lato):
        return None  # пара из разных широтных зон — ждём следующую
    if newest_odd:
        lat = lato
        ni = max(nl - 1, 1)
        m = math.floor(lon0 * (nl - 1) - lon1 * nl + 0.5)
        lon = (360.0 / ni) * (_mod(m, ni) + lon1)
    else:
        lat = late
        ni = max(nl, 1)
        m = math.floor(lon0 * (nl - 1) - lon1 * nl + 0.5)
        lon = (360.0 / ni) * (_mod(m, ni) + lon0)
    if lon >= 180:
        lon -= 360
    return lat, lon


def cpr_local(lat_cpr, lon_cpr, odd, ref_lat, ref_lon, surface=False):
    """Локальное декодирование относительно известной точки (до 180 миль в воздухе, 45 миль на земле)."""
    span = 90.0 if surface else 360.0
    i = 1 if odd else 0
    yz, xz = lat_cpr / 131072.0, lon_cpr / 131072.0
    dlat = span / (60 - i)
    j = math.floor(ref_lat / dlat) + math.floor(0.5 + _mod(ref_lat, dlat) / dlat - yz)
    lat = dlat * (j + yz)
    dlon = span / max(cpr_nl(lat) - i, 1)
    m = math.floor(ref_lon / dlon) + math.floor(0.5 + _mod(ref_lon, dlon) / dlon - xz)
    lon = dlon * (m + xz)
    lon = ((lon + 180) % 360) - 180
    return lat, lon


def cpr_encode(lat, lon, odd, surface=False):
    span = 90.0 if surface else 360.0
    i = 1 if odd else 0
    dlat = span / (60 - i)
    yz = math.floor(131072 * _mod(lat, dlat) / dlat + 0.5)
    rlat = dlat * (yz / 131072.0 + math.floor(lat / dlat))
    dlon = span / max(cpr_nl(rlat) - i, 1)
    xz = math.floor(131072 * _mod(lon, dlon) / dlon + 0.5)
    return int(yz) & 0x1FFFF, int(xz) & 0x1FFFF


# ---------------------------------------------------------------- геодезия
def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def destination(lat, lon, brg, km):
    d = km / 6371.0
    p1, l1, b = math.radians(lat), math.radians(lon), math.radians(brg)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), ((math.degrees(l2) + 540) % 360) - 180


# ---------------------------------------------------------------- декодирование кадра
def _surface_speed(mov):
    if mov == 0 or mov > 124:
        return None
    if mov == 1:
        return 0.0
    if mov <= 8:
        return 0.125 * (mov - 1)
    if mov <= 12:
        return 1.0 + 0.25 * (mov - 9)
    if mov <= 38:
        return 2.0 + 0.5 * (mov - 13)
    if mov <= 93:
        return 15.0 + (mov - 39)
    if mov <= 108:
        return 70.0 + 2 * (mov - 94)
    if mov <= 123:
        return 100.0 + 5 * (mov - 109)
    return 175.0


def _decode_me(me, m):
    g = lambda a, b: (me >> (56 - b)) & ((1 << (b - a + 1)) - 1)  # noqa: E731  — биты ME с 1
    tc = g(1, 5)
    m["tc"] = tc
    if 1 <= tc <= 4:
        ca = g(6, 8)
        cs = "".join(CHARSET[g(9 + 6 * k, 14 + 6 * k)] for k in range(8))
        m["callsign"] = cs.replace("#", "").strip()
        m["category"] = CATEGORIES.get((tc, ca), f"{'DCBA'[tc - 1]}{ca}" if ca else None)
    elif 5 <= tc <= 8:
        m["on_ground"] = True
        gs = _surface_speed(g(6, 12))
        if gs is not None:
            m["gs"] = gs
        if g(13, 13):
            m["track"] = g(14, 20) * 360.0 / 128
        m["cpr"] = (g(22, 22), g(23, 39), g(40, 56), True)
    elif 9 <= tc <= 18 or 20 <= tc <= 22:
        alt = decode_ac12(g(9, 20))
        if alt is not None:
            m["alt_gnss" if tc >= 20 else "alt"] = alt
        m["on_ground"] = False
        m["cpr"] = (g(22, 22), g(23, 39), g(40, 56), False)
    elif tc == 19:
        st = g(6, 8)
        m["on_ground"] = False
        if st in (1, 2):
            k = 4 if st == 2 else 1
            vew, vns = g(15, 24), g(26, 35)
            if vew and vns:
                vx = (vew - 1) * k * (-1 if g(14, 14) else 1)
                vy = (vns - 1) * k * (-1 if g(25, 25) else 1)
                m["gs"] = math.hypot(vx, vy)
                m["track"] = (math.degrees(math.atan2(vx, vy)) + 360) % 360
        elif st in (3, 4):
            if g(14, 14):
                m["heading"] = g(15, 24) * 360.0 / 1024
            asp = g(26, 35)
            if asp:
                v = (asp - 1) * (4 if st == 4 else 1)
                m["tas" if g(25, 25) else "ias"] = v
        vr = g(38, 46)
        if vr:
            m["vr"] = (vr - 1) * 64 * (-1 if g(37, 37) else 1)
        d = g(50, 56)
        if d:
            m["gnss_diff"] = (d - 1) * 25 * (-1 if g(49, 49) else 1)
    elif tc == 0:
        # «нет сведений о местоположении»: борт не знает своих координат (часто — подавление GNSS),
        # но барометрическую высоту передаёт
        alt = decode_ac12(g(9, 20))
        if alt is not None:
            m["alt"] = alt
        m["no_pos"] = True
    elif tc == 29 and g(6, 7) == 1:
        # состояние цели (ADS-B v2): заданная на автопилоте высота, давление QNH, заданный курс
        sa = g(10, 20)
        if sa:
            m["sel_alt"] = (sa - 1) * 32
        qnh = g(21, 29)
        if qnh:
            m["qnh"] = 800.0 + (qnh - 1) * 0.8
        if g(30, 30):
            m["sel_hdg"] = (g(31, 39) * 180.0 / 256) % 360
    elif tc == 28 and g(6, 8) == 1:
        em = g(9, 11)
        m["emergency"] = em
        m["squawk"] = decode_squawk(g(12, 24))


def _bds20(mb, m):
    if (mb >> 48) != 0x20:
        return
    cs = "".join(CHARSET[(mb >> (42 - 6 * k)) & 0x3F] for k in range(8))
    if "#" in cs.rstrip() or not cs.strip() or not cs.strip()[0].isalpha():
        return
    m["callsign"] = cs.strip()


def decode_frame(frame, icao=None):
    """Разбор кадра Mode S (bytes 7 или 14). icao — адрес для кадров «адрес/чётность» (синдром)."""
    frame = bytes(frame)
    n = int.from_bytes(frame, "big")
    L = len(frame) * 8
    g = lambda a, b: (n >> (L - b)) & ((1 << (b - a + 1)) - 1)  # noqa: E731
    df = g(1, 5)
    if df >= 24:
        df = 24
    m = {"df": df, "raw": frame.hex().upper()}
    if df in (17, 18, 11):
        m["icao"] = g(9, 32)
        ca = g(6, 8)
        if df != 18:
            if ca == 4:
                m["on_ground"] = True
            elif ca == 5:
                m["on_ground"] = False
        if df in (17, 18) and L == 112:
            _decode_me(g(33, 88), m)
        return m
    m["icao"] = syndrome(frame) if icao is None else icao
    if df in (0, 16):
        if g(6, 6):
            m["on_ground"] = True
        alt = decode_ac13(g(20, 32))
        if alt is not None:
            m["alt"] = alt
    elif df in (4, 20):
        fs = g(6, 8)
        if fs in (1, 3):
            m["on_ground"] = True
        elif fs in (0, 2):
            m["on_ground"] = False
        alt = decode_ac13(g(20, 32))
        if alt is not None:
            m["alt"] = alt
    elif df in (5, 21):
        fs = g(6, 8)
        if fs in (1, 3):
            m["on_ground"] = True
        elif fs in (0, 2):
            m["on_ground"] = False
        m["squawk"] = decode_squawk(g(20, 32))
    if df in (20, 21) and L == 112:
        _bds20(g(33, 88), m)
    return m


def msg_type(m):
    """Короткое обозначение типа: «DF17/TC19», «DF4» …"""
    return f"DF{m['df']}" + (f"/TC{m['tc']}" if "tc" in m else "")


def _num(v):
    return f"{v:,.0f}".replace(",", " ")


def describe(m, pos=None, metric=True):
    """Расшифровка сообщения по-русски для ленты журнала."""
    def alt(ft):
        return f"{_num(ft * FT)} м" if metric else f"{_num(ft)} фут"
    parts = []
    if m.get("callsign"):
        parts.append(f"позывной {m['callsign']}")
    if m.get("category") and m.get("tc", 0) in (1, 2, 3, 4):
        parts.append(f"категория {m['category']}")
    if m.get("on_ground") and m.get("tc") in (5, 6, 7, 8):
        parts.append("на земле")
    if "alt" in m:
        parts.append(f"высота {alt(m['alt'])}")
    if "alt_gnss" in m:
        parts.append(f"высота GNSS {alt(m['alt_gnss'])}")
    if "gs" in m:
        t = f"скорость {m['gs'] * KT_KMH:.0f} км/ч"
        if "track" in m:
            t += f", путевой угол {m['track']:.0f}°"
        parts.append(t)
    elif "track" in m:
        parts.append(f"путевой угол {m['track']:.0f}°")
    for k in ("ias", "tas"):
        if k in m:
            t = f"скорость {m[k] * KT_KMH:.0f} км/ч {k.upper()}"
            if "heading" in m:
                t += f", курс {m['heading']:.0f}°"
            parts.append(t)
    if "heading" in m and "ias" not in m and "tas" not in m:
        parts.append(f"курс {m['heading']:.0f}°")
    if "vr" in m:
        parts.append(f"вертикальная {m['vr'] * FT / 60:+.1f} м/с" if metric
                     else f"вертикальная {m['vr']:+.0f} фут/мин")
    if "squawk" in m:
        note = SQUAWK_NOTES.get(m["squawk"])
        parts.append(f"сквок {m['squawk']}" + (f" ({note})" if note else ""))
    if m.get("emergency"):
        parts.append(f"аварийный статус {m['emergency']}")
    if "sel_alt" in m or "sel_hdg" in m or "qnh" in m:
        ap = []
        if "sel_alt" in m:
            ap.append(f"высота {alt(m['sel_alt'])}")
        if "sel_hdg" in m:
            ap.append(f"курс {m['sel_hdg']:.0f}°")
        if "qnh" in m:
            ap.append(f"QNH {m['qnh']:.1f} гПа")
        parts.append("автопилот: " + ", ".join(ap))
    if m.get("no_pos"):
        parts.append("нет координат (TC0)")
    if "cpr" in m:
        note = m.get("pos_note")
        if pos:
            parts.append(f"позиция {pos[0]:.4f}, {pos[1]:.4f}" + (f" — {note}" if note else ""))
        elif note:
            parts.append(note)
        else:
            parts.append(f"CPR {'нечётный' if m['cpr'][0] else 'чётный'} (ждём пару)")
    if not parts:
        if m["df"] == 11:
            parts.append("ответ на общий запрос (DF11)")
        elif m.get("tc") == 31:
            parts.append("эксплуатационный статус (TC31)")
        else:
            parts.append("служебное сообщение")
    return ", ".join(parts)


def decode_hex(hexstr):
    """Удобство для тестов и отладки: hex-строка → (разобранное сообщение, синдром)."""
    b = bytes.fromhex(hexstr)
    return decode_frame(b), syndrome(b)


# ---------------------------------------------------------------- кодирование (демо и тесты)
def _frame(head_bits, head_len, payload, payload_len, parity_xor=0):
    total = head_len + payload_len
    n = (head_bits << payload_len) | payload
    data = n.to_bytes(total // 8, "big")
    return data + (crc24(data) ^ parity_xor).to_bytes(3, "big")


def make_df17(icao, me, ca=5):
    return _frame((17 << 3 | ca) << 24 | icao, 32, me, 56)


def make_df11(icao, ca=5):
    return _frame((11 << 3 | ca), 8, icao, 24)


def make_df4(icao, alt_ft, fs=0):
    return _frame((4 << 3 | fs), 8, encode_ac13(alt_ft), 24, icao)


def make_df5(icao, squawk, fs=0):
    return _frame((5 << 3 | fs), 8, encode_squawk(squawk), 24, icao)


def me_ident(callsign, tc=4, ca=3):
    cs = (callsign.upper() + " " * 8)[:8]
    v = (tc << 3) | ca
    for ch in cs:
        k = CHARSET.find(ch)
        v = (v << 6) | (k if k > 0 else 32)
    return v


def me_airborne_pos(lat, lon, alt_ft, odd, tc=11):
    yz, xz = cpr_encode(lat, lon, odd)
    return (tc << 51) | (encode_ac12(alt_ft) << 36) | ((1 if odd else 0) << 34) | (yz << 17) | xz


def me_surface_pos(lat, lon, odd, gs_kt, track, tc=7):
    if gs_kt <= 0.01:
        mov = 1
    elif gs_kt < 1:
        mov = 1 + int(round(gs_kt / 0.125))
    elif gs_kt < 2:
        mov = 9 + int((gs_kt - 1) / 0.25)
    elif gs_kt < 15:
        mov = 13 + int((gs_kt - 2) / 0.5)
    elif gs_kt < 70:
        mov = 39 + int(gs_kt - 15)
    else:
        mov = min(123, 94 + int((gs_kt - 70) / 2))
    trk = int(round(track * 128 / 360.0)) & 0x7F
    yz, xz = cpr_encode(lat, lon, odd, surface=True)
    return ((tc << 51) | (mov << 44) | (1 << 43) | (trk << 36) | ((1 if odd else 0) << 34)
            | (yz << 17) | xz)


def me_velocity(gs_kt, track, vr_fpm):
    vx = gs_kt * math.sin(math.radians(track))
    vy = gs_kt * math.cos(math.radians(track))
    vew = min(int(round(abs(vx))) + 1, 1023)
    vns = min(int(round(abs(vy))) + 1, 1023)
    vr = min(int(round(abs(vr_fpm) / 64.0)) + 1, 511)
    v = (19 << 3) | 1
    v = (v << 1) << 1               # IC, IFR
    v = (v << 3) | 0                # NUCv
    v = (v << 1) | (1 if vx < 0 else 0)
    v = (v << 10) | vew
    v = (v << 1) | (1 if vy < 0 else 0)
    v = (v << 10) | vns
    v = (v << 1) | 1                # источник VR — барометрический
    v = (v << 1) | (1 if vr_fpm < 0 else 0)
    v = (v << 9) | vr
    v = (v << 2)                    # резерв
    v = (v << 8)                    # разница GNSS–баро не передаём
    return v


def frame_slots(frame):
    """Кадр → последовательность полумикросекундных слотов 0/1 (преамбула + PPM)."""
    pre = [1, 0, 1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0]
    bits = np.unpackbits(np.frombuffer(bytes(frame), dtype=np.uint8))
    data = np.empty(bits.size * 2, dtype=np.float32)
    data[0::2] = bits
    data[1::2] = 1 - bits
    return np.concatenate([np.array(pre, dtype=np.float32), data])


def render_envelope(frame, fs, frac=0.0):
    """Огибающая кадра на частоте fs с дробным сдвигом (0..1 отсчёта): прямоугольные импульсы 0,5 мкс,
    проинтегрированные по интервалу отсчёта, — как их видит АЦП."""
    up = 10
    per_half = int(round(fs * up * 0.5e-6))
    hr = np.repeat(frame_slots(frame), per_half)
    off = int(frac * up) % up
    hr = np.concatenate([np.zeros(off, np.float32), hr, np.zeros(2 * up, np.float32)])
    k = hr.size // up
    return hr[:k * up].reshape(k, up).mean(axis=1)


# ---------------------------------------------------------------- демодулятор
class Demodulator:
    """Поиск кадров Mode S в потоке IQ. feed() принимает блоки подряд (хвост блока переносится)."""

    SPAN = 16 + 224  # преамбула + 112 бит при 2 MS/s

    def __init__(self, fs=FS, level=1.4, contrast=1.3):
        self.fs = int(fs)
        self.level = level        # порог импульсов преамбулы относительно среднего шума
        self.contrast = contrast  # во сколько раз импульсы преамбулы выше пауз между ними
        self.tail = np.zeros(0, np.float32)
        self.skip = 0
        self._rs_pos = 0.0
        self._rs_last = None
        self.noise = 0.0
        self.candidates = 0
        self.corrected = 0

    def _resample(self, mag):
        if self.fs == FS:
            return mag
        x = mag if self._rs_last is None else np.concatenate([[self._rs_last], mag])
        ratio = self.fs / FS
        npos = int(math.floor((x.size - 1 - self._rs_pos) / ratio)) + 1
        pos = self._rs_pos + np.arange(npos) * ratio
        out = np.interp(pos, np.arange(x.size), x).astype(np.float32)
        self._rs_pos = pos[-1] + ratio - (x.size - 1)
        self._rs_last = x[-1]
        return out

    def feed(self, iq, known=()):
        """→ список (frame_bytes, icao_или_None, сигнал_dBFS, исправлено_бит)."""
        mag = np.abs(iq).astype(np.float32, copy=False)
        mag = self._resample(mag)
        m = np.concatenate([self.tail, mag]) if self.tail.size else mag
        N = m.size - self.SPAN
        if N <= 2:
            self.tail = m
            return []
        noise = float(np.mean(mag)) if mag.size else 0.0
        self.noise = noise
        # Преамбула: импульсы 0; 1,0; 3,5; 4,5 мкс (отсчёты 0, 2, 7, 9). Импульс 0,5 мкс при любом дробном
        # сдвиге целиком попадает в пару соседних отсчётов, а паузы 4–6 и 11–15 чистые — проверяем это.
        pair = m[:-1] + m[1:]
        p0, p2, p7, p9 = pair[0:N], pair[2:N + 2], pair[7:N + 7], pair[9:N + 9]
        S = p0 + p2 + p7 + p9
        pmin = np.minimum(np.minimum(p0, p2), np.minimum(p7, p9))
        qmax = m[4:N + 4]
        for k in (5, 6, 11, 12, 13, 14, 15):
            qmax = np.maximum(qmax, m[k:N + k])
        c = (pmin > self.contrast * qmax) & (S > 8 * self.level * noise)
        c[1:-1] &= (S[1:-1] >= S[:-2]) & (S[1:-1] > S[2:])
        c[0] = c[-1] = False
        idx = np.flatnonzero(c)
        out = []
        if idx.size:
            self.candidates += int(idx.size)
            out = self._demod(m, idx, known)
        self.tail = m[N:].copy()
        self.skip = max(0, self.skip - N)
        return out

    @staticmethod
    def _bits_std(m, base, nbits=112):
        pos = base[:, None] + (16 + 2 * np.arange(nbits))[None, :]
        return (m[pos] > m[pos + 1]).astype(np.uint8)

    @staticmethod
    def _bits_dfb(m, base, nbits=112, nf=0.0):
        """PPM с учётом дробного сдвига: импульс сдвинут на долю φ отсчёта и «протекает» в соседний,
        так что каждый отсчёт зависит от текущего и предыдущего бита. φ и амплитуду A оцениваем
        по преамбуле, а биты ищем алгоритмом Витерби по двум состояниям (предыдущий бит 0/1)."""
        A = (m[base] + m[base + 1] + m[base + 2] + m[base + 3] + m[base + 7] + m[base + 8]
             + m[base + 9] + m[base + 10]) / 4.0 + 1e-9
        phi = np.clip((m[base + 1] + m[base + 3] + m[base + 8] + m[base + 10]) / (4.0 * A), 0.0, 1.0)
        pos = base[:, None] + (16 + 2 * np.arange(nbits))[None, :]
        se, so = m[pos], m[pos + 1]
        K = base.size
        a1, ap = (A * (1 - phi))[:, None], (A * phi)[:, None]
        # ожидаемые (чётный, нечётный) отсчёты для пары (предыдущий бит p, бит b)
        nf2 = nf * nf  # шум поднимает «пустые» отсчёты модуля: E|s+n| ≈ √(s² + nf²)
        exp_e = {(p, b): np.sqrt((a1 * b + ap * (1 - p)) ** 2 + nf2) for p in (0, 1) for b in (0, 1)}
        exp_o = {b: np.sqrt((a1 if b == 0 else ap) ** 2 + nf2) for b in (0, 1)}
        cost = {(p, b): (se - exp_e[(p, b)]) ** 2 + (so - exp_o[b]) ** 2 for p in (0, 1) for b in (0, 1)}
        c0 = np.full(K, 1e9, np.float32)   # перед данными пауза преамбулы — как после бита 1
        c1 = np.zeros(K, np.float32)
        back = np.zeros((K, nbits, 2), np.uint8)
        for j in range(nbits):
            n0a, n0b = c0 + cost[(0, 0)][:, j], c1 + cost[(1, 0)][:, j]
            n1a, n1b = c0 + cost[(0, 1)][:, j], c1 + cost[(1, 1)][:, j]
            back[:, j, 0] = n0b < n0a
            back[:, j, 1] = n1b < n1a
            c0, c1 = np.minimum(n0a, n0b), np.minimum(n1a, n1b)
        bits = np.zeros((K, nbits), np.uint8)
        st = (c1 < c0).astype(np.uint8)
        rows = np.arange(K)
        for j in range(nbits - 1, -1, -1):
            bits[:, j] = st
            st = back[rows, j, st]
        return bits

    def _check(self, bits, kn):
        df = (bits[:, 0].astype(np.int32) << 4 | bits[:, 1] << 3 | bits[:, 2] << 2 | bits[:, 3] << 1 | bits[:, 4])
        long_ = np.isin(df, LONG_DF)
        bf = bits.astype(np.float32)
        syn_l = ((np.rint(bf @ _CBITS).astype(np.int64) & 1) @ _POW24)
        syn_s = ((np.rint(bf[:, :56] @ _CBITS[56:]).astype(np.int64) & 1) @ _POW24)
        syn = np.where(long_, syn_l, syn_s)
        is_es = (df == 17) | (df == 18)
        exact = is_es & long_ & (syn == 0)
        good = exact | (is_es & long_ & np.isin(syn, _FIX1_KEYS))
        good |= (df == 11) & (syn < 128)
        if kn is not None:
            good |= np.isin(df, (0, 4, 5, 16, 20, 21)) & np.isin(syn, kn)
        return df, long_, syn, good, exact

    def _demod(self, m, idx, known):
        kn = np.fromiter(known, dtype=np.int64) if known else None
        found = {}   # индекс кандидата → (база, биты, df, long, syn)
        # быстрый отсев: по первым 8 битам (Витерби) формат DF должен быть осмысленным
        pre = self._bits_dfb(m, idx, 8, nf=self.noise)
        pdf = (pre[:, 0].astype(np.int32) << 4 | pre[:, 1] << 3 | pre[:, 2] << 2 | pre[:, 3] << 1 | pre[:, 4])
        sd = self._bits_std(m, idx, 5)
        sdf = (sd[:, 0].astype(np.int32) << 4 | sd[:, 1] << 3 | sd[:, 2] << 2 | sd[:, 3] << 1 | sd[:, 4])
        rem = np.flatnonzero(np.isin(pdf, _VALID_DF) | np.isin(sdf, _VALID_DF))
        for kind, shift in (("dfb", 0), ("std", 0), ("dfb", -1), ("dfb", 1)):
            if not rem.size:
                break
            base = idx[rem] + shift
            bits = self._bits_dfb(m, base, nf=self.noise) if kind == "dfb" else self._bits_std(m, base)
            df, long_, syn, good, _exact = self._check(bits, kn)
            for r in np.flatnonzero(good):
                found[int(rem[r])] = (int(base[r]), bits[r], int(df[r]), bool(long_[r]), int(syn[r]))
            rem = rem[~good]
        res = []
        for k in sorted(found, key=lambda k: found[k][0]):
            i, bits, d, lng, sy = found[k]
            if i < self.skip:
                continue
            nb = 112 if lng else 56
            b = bits[:nb].copy()
            fixed = 0
            icao = None
            if d in (17, 18):
                if sy:
                    b[_FIX1[sy]] ^= 1
                    fixed = 1
            elif d != 11:
                icao = sy
            frame = np.packbits(b).tobytes()
            if d == 11 and sy and int.from_bytes(frame[1:4], "big") not in known:
                continue  # IID ≠ 0 принимаем только от уже известного борта
            if fixed and int.from_bytes(frame[1:4], "big") not in known:
                continue  # исправленный кадр от незнакомого адреса — скорее шум, чем новый борт
            self.skip = i + 16 + 2 * nb
            self.corrected += fixed
            lvl = float(m[i] + m[i + 1] + m[i + 2] + m[i + 3] + m[i + 7] + m[i + 8] + m[i + 9] + m[i + 10]) / 4.0
            sig = 20 * math.log10(max(lvl, 1e-6))
            res.append((frame, icao, sig, fixed))
        return res


# ---------------------------------------------------------------- таблица бортов
class Aircraft:
    __slots__ = ("icao", "callsign", "category", "alt", "alt_gnss", "gs", "track", "heading", "ias", "tas",
                 "vr", "squawk", "emergency", "on_ground", "lat", "lon", "pos_t", "even", "odd", "msgs",
                 "first", "last", "signal", "trail", "dist", "last_es", "df_seen", "sel_alt", "qnh", "sel_hdg",
                 "no_pos", "pos_src", "confirmed", "rejected", "raw_lat", "raw_lon", "raw_t", "raw_note",
                 "suspect", "jump_streak")

    def __init__(self, icao, now):
        self.icao = icao
        self.callsign = self.category = self.squawk = self.emergency = None
        self.alt = self.alt_gnss = self.gs = self.track = self.heading = self.ias = self.tas = self.vr = None
        self.on_ground = None
        self.sel_alt = self.qnh = self.sel_hdg = None
        self.no_pos = False
        self.lat = self.lon = None
        self.pos_t = 0.0
        self.pos_src = None           # pair | local | single | surface — как получена позиция
        self.confirmed = False        # позиция подтверждена парой чётн./нечётн. (или выведена из такой)
        self.rejected = 0             # сколько позиций отброшено
        self.raw_lat = self.raw_lon = None   # последняя отброшенная («сырая») позиция
        self.raw_t = 0.0
        self.raw_note = None
        self.suspect = False          # последние координаты борта подозрительны (вероятна подмена GNSS)
        self.jump_streak = 0
        self.even = self.odd = None   # (lat_cpr, lon_cpr, t, surface)
        self.msgs = 0
        self.first = self.last = now
        self.signal = None
        self.trail = deque(maxlen=400)
        self.dist = None
        self.last_es = 0.0
        self.df_seen = set()

    @property
    def hex(self):
        return f"{self.icao:06X}"

    def as_dict(self):
        d = {k: getattr(self, k) for k in self.__slots__ if k not in ("trail", "even", "odd", "df_seen")}
        d["hex"] = self.hex
        d["trail"] = list(self.trail)
        d["df_seen"] = sorted(self.df_seen)
        return d


HIST_TRACK_MAX = 1000   # точек трека борта в истории сеанса (дальше прореживаем вдвое)
HIST_RAW_MAX = 300      # точек «подозрительных» координат


def _thin_append(pts, p, cap):
    """Трек для истории: точка не чаще, чем через 1 км или 60 с; при переполнении — прореживание вдвое."""
    if pts:
        q = pts[-1]
        if p[3] - q[3] < 60 and distance_km(q[0], q[1], p[0], p[1]) < 1.0:
            return
    pts.append(p)
    if len(pts) > cap:
        last = pts[-1]
        pts[:] = pts[::2]
        if pts[-1] is not last:
            pts.append(last)


class Tracker:
    """Таблица бортов. keep_s — сколько держать борт после последнего сообщения (None — весь сеанс);
    борт, не слышный дольше STALE_S, считается «давно не слышен», но из таблицы не пропадает.
    history — сводка по всем бортам сеанса (не удаляется; можно передать общий словарь)."""

    def __init__(self, home=None, keep_s=STALE_S, history=None):
        self.home = home
        self.keep_s = keep_s
        self.planes = {}
        self.history = {} if history is None else history
        self.max_range = 0.0
        self.positions = 0
        self.rejected = 0

    def known(self, now=None):
        now = time.time() if now is None else now
        return {k for k, a in self.planes.items() if now - a.last_es < STALE_S}

    def expire(self, now=None):
        now = time.time() if now is None else now
        if self.keep_s is None:
            return
        for k in [k for k, a in self.planes.items() if now - a.last > self.keep_s]:
            del self.planes[k]

    def _history(self, a, m, now):
        h = self.history.get(a.icao)
        if h is None:
            h = self.history[a.icao] = {"icao": a.icao, "hex": a.hex, "callsign": None, "first": now, "last": now,
                                        "alt_min": None, "alt_max": None, "msgs": 0, "had_pos": False,
                                        "no_pos": False, "max_dist": None, "suspect": False,
                                        "track": [], "raw": []}
        h["last"] = now
        h["msgs"] += 1
        h.pop("prev", None)
        if a.callsign:
            h["callsign"] = a.callsign
        alt = m.get("alt")
        if alt is not None:
            h["alt_min"] = alt if h["alt_min"] is None else min(h["alt_min"], alt)
            h["alt_max"] = alt if h["alt_max"] is None else max(h["alt_max"], alt)
        if a.no_pos:
            h["no_pos"] = True
        if a.suspect:
            h["suspect"] = True
        if a.suspect and a.raw_t == now:
            _thin_append(h["raw"], (a.raw_lat, a.raw_lon, a.alt, now), HIST_RAW_MAX)
        if a.lat is not None and a.pos_t == now:
            _thin_append(h["track"], (a.lat, a.lon, a.alt, now), HIST_TRACK_MAX)
            h["had_pos"] = True
            if a.dist is not None:
                h["max_dist"] = max(h["max_dist"] or 0.0, a.dist)

    def apply(self, m, now=None, signal=None):
        """Учесть разобранное сообщение. Возвращает Aircraft (или None)."""
        now = time.time() if now is None else now
        icao = m.get("icao")
        if icao is None:
            return None
        a = self.planes.get(icao)
        if a is None:
            if m["df"] not in (11, 17, 18):
                return None
            a = self.planes[icao] = Aircraft(icao, now)
        a.msgs += 1
        a.last = now
        a.df_seen.add(m["df"])
        if m["df"] in (11, 17, 18):
            a.last_es = now
        if signal is not None:
            a.signal = signal if a.signal is None else 0.7 * a.signal + 0.3 * signal
        for k in ("callsign", "category", "alt", "alt_gnss", "gs", "track", "heading", "ias", "tas", "vr",
                  "squawk", "emergency", "sel_alt", "qnh", "sel_hdg"):
            v = m.get(k)
            if v is not None and v != "":
                setattr(a, k, v)
        if "on_ground" in m:
            a.on_ground = m["on_ground"]
        if m.get("no_pos"):
            a.no_pos = True
        if "cpr" in m:
            a.no_pos = False
            note = self._position(a, m["cpr"], now)
            if note:
                m["pos_note"] = note
        self._history(a, m, now)
        return a

    def _position(self, a, cpr, now):
        """Декодировать CPR и принять или отбросить позицию. Возвращает пояснение для журнала (или None)."""
        odd, lat_c, lon_c, surface = cpr
        rec = (lat_c, lon_c, now, surface)
        if odd:
            a.odd = rec
        else:
            a.even = rec
        pos, src = None, None
        fresh = a.lat is not None and now - a.pos_t < STALE_S
        if surface:
            ref = (a.lat, a.lon) if fresh else self.home
            if ref:
                pos, src = cpr_local(lat_c, lon_c, odd, ref[0], ref[1], surface=True), "surface"
        else:
            if (a.even and a.odd and not a.even[3] and not a.odd[3]
                    and abs(a.even[2] - a.odd[2]) < CPR_PAIR_S):
                pos, src = cpr_global(a.even[:2], a.odd[:2], newest_odd=bool(odd)), "pair"
            if pos is None and fresh:
                pos, src = cpr_local(lat_c, lon_c, odd, a.lat, a.lon), "local"
            if pos is None and self.home:
                # одиночное сообщение — относительно приёмника (верно, если борт ближе ~330 км)
                p1 = cpr_local(lat_c, lon_c, odd, self.home[0], self.home[1])
                if distance_km(self.home[0], self.home[1], *p1) <= SINGLE_RANGE_KM:
                    pos, src = p1, "single"
        if pos is None:
            return None
        lat, lon = pos
        if self.home:
            d = distance_km(self.home[0], self.home[1], lat, lon)
            if d > MAX_RANGE_KM:
                if src == "pair" and not a.confirmed and a.lat is not None:
                    # пара опровергла позицию, взятую по одиночному сообщению от приёмника, — убираем её
                    a.lat = a.lon = a.dist = a.pos_src = None
                    a.trail.clear()
                return self._reject(a, lat, lon, now, f"позиция отброшена: {d:.0f} км от приёмника — "
                                                      "вероятно, подмена GNSS")
        # скачок проверяем от подтверждённой позиции; одиночную (src=single) пара вправе поправить
        if fresh and a.confirmed and src != "surface":
            dt = now - a.pos_t
            jump = distance_km(a.lat, a.lon, lat, lon)
            if jump > 5.0 + 0.45 * dt:  # быстрее ~1600 км/ч — так самолёты не летают
                if a.raw_lat is not None and now - a.raw_t < 30 and \
                        distance_km(a.raw_lat, a.raw_lon, lat, lon) < 5.0 + 0.45 * (now - a.raw_t):
                    a.jump_streak += 1
                else:
                    a.jump_streak = 1
                if a.jump_streak < JUMP_ACCEPT:
                    note = f"скачок {jump:.0f} км за {max(dt, 0.1):.0f} с — отброшена"
                    streak = a.jump_streak
                    self._reject(a, lat, lon, now, note)
                    a.jump_streak = streak
                    return note
                src = "pair"  # новое место подтверждено несколькими сообщениями подряд
        a.jump_streak = 0
        a.lat, a.lon, a.pos_t, a.pos_src = lat, lon, now, src
        if src == "pair":
            a.confirmed = True
        elif src == "single":
            a.confirmed = False
        a.suspect = False
        self.positions += 1
        if not a.trail or distance_km(a.trail[-1][0], a.trail[-1][1], lat, lon) > 0.25:
            a.trail.append((lat, lon, a.alt))
        if self.home:
            a.dist = distance_km(self.home[0], self.home[1], lat, lon)
            self.max_range = max(self.max_range, a.dist)
        if src == "single":
            return "по одиночному сообщению (от приёмника, ждём пару)"
        return None

    def _reject(self, a, lat, lon, now, note):
        a.rejected += 1
        self.rejected += 1
        a.raw_lat, a.raw_lon, a.raw_t, a.raw_note = lat, lon, now, note
        a.suspect = True
        return note


def level_state(st):
    """«перегрузка» — сильные кадры выше −3 dBFS или отсечки АЦП; «мало» — кадры слабее −30 dBFS."""
    if st["clip"] > 1e-4 or (st["frames_max"] is not None and st["frames_max"] > -3.0):
        return "перегрузка"
    if st["frames_p90"] is None or st["frames_p90"] < -30.0:
        return "мало"
    return "норма"


# ---------------------------------------------------------------- история на диске
HISTORY_PATH = paths.service("adsb_history.json")
_HIST_DEFAULTS = {"callsign": None, "alt_min": None, "alt_max": None, "msgs": 0, "had_pos": False, "no_pos": False,
                  "max_dist": None, "suspect": False, "track": [], "raw": []}


def save_history(entries, path=HISTORY_PATH):
    """entries — список словарей истории (снимок). Пишем атомарно: во временный файл, затем замена."""
    data = []
    for h in entries:
        d = {k: v for k, v in h.items() if k != "prev"}
        d["track"] = [list(p) for p in h.get("track", [])]
        d["raw"] = [list(p) for p in h.get("raw", [])]
        data.append(d)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "saved": time.time(), "planes": data}, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def load_history(path=HISTORY_PATH, keep_days=7, now=None):
    """Словарь icao → запись истории прошлых сеансов (помечены prev=True); старше keep_days дней — отброшены
    (keep_days=0 — хранить всегда)."""
    now = time.time() if now is None else now
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for d in data.get("planes", []):
        try:
            if keep_days and now - float(d["last"]) > keep_days * 86400:
                continue
            h = dict(_HIST_DEFAULTS)
            h.update(d)
            h["icao"] = int(h["hex"], 16)
            h["track"] = [tuple(p) for p in h.get("track") or []]
            h["raw"] = [tuple(p) for p in h.get("raw") or []]
            h["prev"] = True
            out[h["icao"]] = h
        except (KeyError, ValueError, TypeError):
            continue
    return out


# ---------------------------------------------------------------- поток обработки
class AdsbProcessor(threading.Thread):
    """Очередь блоков IQ → демодулятор → таблица. Источник вызывает put() из своего потока."""

    def __init__(self, fs=FS, home=None, on_message=None, keep_s=STALE_S, history=None):
        super().__init__(daemon=True)
        self.q = queue.Queue(maxsize=48)
        self.demod = Demodulator(fs)
        self.tracker = Tracker(home, keep_s, history)
        self.feed_log = deque(maxlen=20000)  # (время, ICAO hex, позывной, сообщение, позиция|None)
        self.lock = threading.Lock()
        self.on_message = on_message
        self.running = True
        self.blocks = 0
        self.dropped = 0
        self.msgs = 0
        self.by_df = {}
        self.busy = 0.0       # доля времени, занятая декодером (0..1, скользящая)
        # уровни АЦП и принятых кадров для индикатора «мало / норма / перегрузка»
        self.lv_peak = 0.0
        self.lv_clip = 0
        self.lv_samples = 0
        self.lv_frames = deque(maxlen=400)
        self._t_last = None

    def set_rate(self, fs):
        try:
            self.q.put(("rate", int(fs)), timeout=2)  # не вешать интерфейс, если поток уже остановлен
        except queue.Full:
            self.demod = Demodulator(int(fs))

    def set_home(self, lat, lon):
        self.tracker.home = (lat, lon)

    def set_keep(self, keep_s):
        self.tracker.keep_s = keep_s

    def drain_log(self):
        """Забрать накопленные с прошлого раза сообщения для ленты интерфейса."""
        with self.lock:
            out = list(self.feed_log)
            self.feed_log.clear()
        return out

    def level_stats(self):
        """Сводка уровней с прошлого вызова: пик АЦП, доля отсечек, шум, уровни кадров (dB полной шкалы)."""
        if not self.lv_samples and getattr(self, "_lv_last", None):
            return self._lv_last  # с прошлого вызова блоков не было — отдаём прежнюю сводку
        fr = sorted(self.lv_frames)
        st = {
            "peak_db": 20 * math.log10(max(self.lv_peak, 1e-6)),
            "clip": self.lv_clip / max(self.lv_samples, 1),
            "noise_db": 20 * math.log10(max(self.demod.noise, 1e-6)),
            "frames_p90": fr[int(0.9 * (len(fr) - 1))] if fr else None,
            "frames_max": fr[int(0.98 * (len(fr) - 1))] if fr else None,  # «сильные кадры»: 98-й процентиль
            "frames_med": fr[len(fr) // 2] if fr else None,
        }
        st["state"] = level_state(st)
        self.lv_peak, self.lv_clip, self.lv_samples = 0.0, 0, 0
        self._lv_last = st
        return st

    def history_snapshot(self):
        with self.lock:
            return [dict(h, track=list(h["track"]), raw=list(h["raw"])) for h in self.tracker.history.values()]

    def put(self, iq):
        try:
            self.q.put_nowait(iq)
        except queue.Full:
            self.dropped += 1

    def stop(self):
        self.running = False

    def process(self, iq, now=None):
        t0 = time.perf_counter()
        now = time.time() if now is None else now
        v = np.abs(iq.view(np.float32))
        self.lv_peak = max(self.lv_peak, float(v.max()) if v.size else 0.0)
        self.lv_clip += int(np.count_nonzero(v >= 0.985))  # 8-битный АЦП упёрся в 0 или 255
        self.lv_samples += v.size
        frames = self.demod.feed(iq, self.tracker.known(now))
        for f in frames:
            self.lv_frames.append(f[2])
        with self.lock:
            for frame, icao, sig, _fixed in frames:
                m = decode_frame(frame, icao)
                a = self.tracker.apply(m, now, sig)
                if a is None:
                    continue
                self.msgs += 1
                self.by_df[m["df"]] = self.by_df.get(m["df"], 0) + 1
                pos = (a.lat, a.lon) if ("cpr" in m and a.pos_t == now) else None
                self.feed_log.append((now, a.hex, a.callsign, m, pos))
                if self.on_message:
                    try:
                        self.on_message(m, a, now)
                    except Exception:  # noqa: BLE001 — журнал не должен ронять приём
                        pass
            self.tracker.expire(now)
            self.blocks += 1
        dt = time.perf_counter() - t0
        blk = max(len(iq), 1) / self.demod.fs
        self.busy = 0.9 * self.busy + 0.1 * (dt / blk)
        return frames

    def run(self):
        while self.running:
            try:
                item = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if isinstance(item, tuple):
                if item[0] == "rate":
                    self.demod = Demodulator(item[1])
                continue
            self.process(item)

    def snapshot(self):
        with self.lock:
            return [a.as_dict() for a in self.tracker.planes.values()]


# ---------------------------------------------------------------- демо-генератор
AIRPORTS = [(55.9726, 37.4146), (55.4088, 37.9063), (55.5915, 37.2615), (55.6031, 38.0800)]
_CALLS = ["AFL1124", "SBI2057", "UTA415", "SDM6311", "AFL31", "SDM6555", "NWS4011", "SVR113", "AZV327",
          "AFL2442", "THY4KC", "UAE132", "RSY9603", "KAR245", "SBI1290", "CCA910", "AFL1650", "SBI8812",
          "UTA261", "PBD447"]


class _DemoPlane:
    def __init__(self, rnd, home, k):
        self.rnd = rnd
        self.icao = 0x150000 + rnd.randrange(0x10000) if k % 3 else rnd.randrange(0x400000, 0x800000)
        self.callsign = _CALLS[k % len(_CALLS)]
        self.squawk = "".join(str(rnd.randrange(8)) for _ in range(4))
        self.ground = False
        self.spawn(home, first=True)
        self.foff = rnd.uniform(-120e3, 120e3)
        self.phase = rnd.uniform(0, 2 * math.pi)
        self.turn = 0.0
        self.next = {}
        self.no_pos = False
        self.spoof = False
        self.even_only = False
        self.spoof_off = (0.0, 0.0, -1e9)   # (азимут, км, время выбора)

    def spawn(self, home, first=False):
        r = self.rnd
        brg = r.uniform(0, 360)
        dist = r.uniform(15, 260) if first else r.uniform(250, 330)
        self.lat, self.lon = destination(home[0], home[1], brg, dist)
        self.track = (brg + 180 + r.uniform(-70, 70)) % 360 if not first else r.uniform(0, 360)
        self.gs = r.uniform(250, 480)
        self.alt = r.choice([r.uniform(3000, 15000), r.uniform(24000, 39000), r.uniform(30000, 41000)])
        self.vr = r.choice([0, 0, 0, r.uniform(-2200, -800), r.uniform(900, 2500)])

    def step(self, dt, home):
        r = self.rnd
        if self.ground:
            self.track = (self.track + 6.0 * dt) % 360  # рулит по кругу
        else:
            if r.random() < dt / 40:
                self.turn = r.choice([0, 0, r.uniform(-1.5, 1.5)])
            self.track = (self.track + self.turn * dt) % 360
            self.alt = max(1500, min(41000, self.alt + self.vr * dt / 60))
            if self.alt in (1500, 41000):
                self.vr = 0
        km = self.gs * KT_KMH * dt / 3600
        self.lat, self.lon = destination(self.lat, self.lon, self.track, km)
        if not self.ground and distance_km(home[0], home[1], self.lat, self.lon) > (200 if self.even_only else 340):
            if self.even_only:
                self.track = bearing_deg(self.lat, self.lon, home[0], home[1])
            else:
                self.spawn(home)


class DemoSource(SourceBase):
    """Синтетический эфир 1090 МГц: борта летят по маршрутам вокруг дома, передают DF17 (позывной,
    позиция чётная/нечётная, скорость), DF4/DF5 (высота, сквок) и DF11. Темп — реальное время."""

    PERIODS = {"pos": 0.5, "vel": 0.5, "id": 5.0, "df4": 1.2, "df5": 1.5, "df11": 1.0}

    def __init__(self, sink, fs=FS, freq=ADSB_FREQ, home=(55.7558, 37.6173), planes=14, seed=None, noise=0.035,
                 no_pos=3, silent=(8.0, 128.0), spoof=1, even_only=1):
        super().__init__(sink, fs, freq)
        self.home = home
        self.nplanes = planes
        self.n_nopos = no_pos      # столько бортов передают TC0 «нет координат» (как при подавлении GNSS)
        self.silent = silent       # первый борт замолкает на этот отрезок времени эфира, с
        self.n_spoof = spoof       # борт с подменённым GNSS: координаты то за 1200 км, то скачут на ~90 км
        self.n_even = even_only    # борт, у которого слышны только чётные CPR (декодируется от приёмника)
        self.rnd = random.Random(seed)
        self.rng = np.random.default_rng(seed)
        self.noise_sigma = noise
        self.planes = []
        self.t_stream = 0.0
        self.t_next = 0.0
        self.sent = 0

    def open(self):
        n0 = self.nplanes + self.n_nopos
        self.planes = [_DemoPlane(self.rnd, self.home, k) for k in range(n0 + self.n_spoof + self.n_even)]
        for p in self.planes[self.nplanes:n0]:
            p.no_pos = True
        for p in self.planes[n0:n0 + self.n_spoof]:
            p.spoof = True
        for p in self.planes[n0 + self.n_spoof:]:
            p.even_only = True
            p.lat, p.lon = destination(self.home[0], self.home[1], self.rnd.uniform(0, 360), self.rnd.uniform(40, 150))
        ap = AIRPORTS[0]
        g = self.planes[self.nplanes - 1]
        g.ground, g.lat, g.lon, g.gs, g.alt, g.vr, g.callsign = True, ap[0], ap[1] + 0.01, 12.0, 0, 0, "AFL1490"
        self.t_next = time.perf_counter()
        self.sink("opened", {"label": "Демо-генератор ADS-B", "tuner": "эмуляция", "gains": self.gains})

    def close(self):
        pass

    def apply(self, name, value):
        if name == "rate":
            self.fs = int(value)
        elif name == "freq":
            self.freq = int(value)
        elif name == "gain":
            self.gain = value

    def _pace(self, n):
        self.t_next += n / self.fs
        dt = self.t_next - time.perf_counter()
        if dt > 0:
            time.sleep(dt)
        elif dt < -0.5:
            self.t_next = time.perf_counter()

    def _spoofed(self, p):
        """Подмена GNSS: 15 с борт «стоит» за 1200 км от приёмника, 15 с — скачет на 60–120 км от истины."""
        t = self.t_stream
        if t % 30 < 15:
            return destination(self.home[0], self.home[1], 250.0, 1200.0)
        if t - p.spoof_off[2] > 2.0:
            p.spoof_off = (self.rnd.uniform(0, 360), self.rnd.uniform(60, 120), t)
        return destination(p.lat, p.lon, p.spoof_off[0], p.spoof_off[1])

    def _frames(self, p, kind):
        if kind == "pos":
            if p.no_pos:
                return make_df17(p.icao, encode_ac12(p.alt) << 36)  # TC0: только высота
            odd = self.rnd.random() < 0.5 and not p.even_only
            if p.spoof:
                lat, lon = self._spoofed(p)
                return make_df17(p.icao, me_airborne_pos(lat, lon, p.alt, odd))
            if p.ground:
                return make_df17(p.icao, me_surface_pos(p.lat, p.lon, odd, p.gs, p.track), ca=4)
            return make_df17(p.icao, me_airborne_pos(p.lat, p.lon, p.alt, odd))
        if kind == "vel":
            if p.ground:
                return None
            return make_df17(p.icao, me_velocity(p.gs, p.track, p.vr))
        if kind == "id":
            return make_df17(p.icao, me_ident(p.callsign, 4, 3), ca=4 if p.ground else 5)
        if kind == "df4":
            return make_df4(p.icao, p.alt, fs=1 if p.ground else 0)
        if kind == "df5":
            return make_df5(p.icao, p.squawk, fs=1 if p.ground else 0)
        return make_df11(p.icao, ca=4 if p.ground else 5)

    def render(self, n):
        """Блок IQ из n отсчётов (complex64, уже через 8-битный АЦП как у донгла)."""
        fs = self.fs
        dt = n / fs
        t0 = self.t_stream
        sig = np.zeros(n, np.complex64)
        maxlen = int(130e-6 * fs) + 4
        tt = np.arange(maxlen) / fs
        for pi, p in enumerate(self.planes):
            p.step(dt, self.home)
            mute = pi == 0 and self.silent and self.silent[0] <= t0 < self.silent[1]
            d = max(distance_km(self.home[0], self.home[1], p.lat, p.lon), 3.0)
            amp = min(0.45, 0.6 * 45.0 / d)  # ближние борта ~−5 dBFS: демо показывает «норму», а не перегрузку
            for kind, per in self.PERIODS.items():
                tn = p.next.get(kind)
                if tn is None:
                    tn = p.next[kind] = t0 + self.rnd.uniform(0, per)
                while tn < t0 + dt:
                    start = int((tn - t0) * fs)
                    fr = self._frames(p, kind)
                    if mute:
                        fr = None
                    if fr is not None and start + maxlen < n:
                        env = render_envelope(fr, fs, self.rnd.random())
                        k = env.size
                        a = amp * self.rnd.uniform(0.8, 1.2)
                        ph = p.phase + 2 * math.pi * p.foff * tn
                        sig[start:start + k] += (a * env * np.exp(1j * (ph + 2 * math.pi * p.foff * tt[:k])))
                        self.sent += 1
                    elif fr is not None:
                        tn = t0 + dt + 1e-5
                        break
                    tn += per * self.rnd.uniform(0.8, 1.2)
                p.next[kind] = tn
        self.t_stream += dt
        noise = self.rng.standard_normal(2 * n, dtype=np.float32) * self.noise_sigma
        x = sig.view(np.float32) + noise
        raw = np.clip(np.rint(127.4 + 128.0 * x), 0, 255).astype(np.uint8)
        return rtlsdr.u8_to_iq(raw.tobytes())

    def read_at(self, freq, nsamples, settle):
        iq = self.render(nsamples)
        self._pace(nsamples)
        return iq

    def read_stream(self):
        n = BLOCK_BYTES // 2
        iq = self.render(n)
        self._pace(n)
        return iq
