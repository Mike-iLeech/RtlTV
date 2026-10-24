"""Декодер DMR (ETSI TS 102 361, Tier I/II) для режима «Радио».

Вход — мгновенная частота (выход FM-дискриминатора) в Гц, 48 кГц. Внутри:
  * согласованный RRC-фильтр (α=0,2), 4FSK 4800 Бод (±648/±1944 Гц);
  * поиск синхрослов BS/MS/Direct (voice/data, 48 бит), оценка смещения частоты и девиации
    по самому синхрослову, автоопределение полярности, «маховик» TDMA (30 мс на пакет)
    с подстройкой тактовой частоты по каждому найденному синхрослову;
  * CACH/TACT (номер слота), Slot Type — Golay(20,8), BPTC(196,96) с Hamming(15,11)/(13,9),
    RS(12,9) с масками CRC (LC header/terminator), CSBK и заголовки данных (CRC-CCITT),
    EMB — QR(16,7,6), встроенный LC в кадрах B–E (Hamming(16,11,4) + CRC5);
  * голос: 3×72 бита AMBE+2 на пакет -> деперемежение как в DSD -> mbelib
    (mbe_processAmbe3600x2450Framef, там же Golay и снятие скремблирования) -> 8 кГц -> 48 кГц.

Шифрованные вызовы (бит Privacy в LC, PI-заголовок или PI в EMB) звук не выдают.

Здесь же генератор синтетического DMR-сигнала (test_signal) — для тестов и rtltv/radiotest.py.
"""
import ctypes as C
import os
import sys
import time

import numpy as np
from scipy.signal import firwin, lfilter, resample_poly

FS = 48000
SPS = 10                 # отсчётов на символ (48000 / 4800)
BURST_SYM = 132          # символов в пакете (264 бита)
SLOT_SYM = 144           # символов на слот TDMA (30 мс): CACH 12 + пакет 132
CACH_SYM = 12
SYNC_OFF = 54            # номер первого символа синхрослова внутри пакета
SPB = SLOT_SYM * SPS     # отсчётов на слот = 1440

DEV1 = 648.0             # девиация символа ±1, Гц
DEV3 = 1944.0            # девиация символа ±3, Гц

# --------------------------------------------------------------------------------------
# Синхрослова (voice; data — их инверсия по символам)
# --------------------------------------------------------------------------------------
FAM_BS, FAM_MS, FAM_DM1, FAM_DM2 = 0, 1, 2, 3
FAM_NAMES = {FAM_BS: "BS", FAM_MS: "MS", FAM_DM1: "DM", FAM_DM2: "DM"}
_SYNC_HEX = {FAM_BS: 0x755FD7DF75F7, FAM_MS: 0x7F7D5DD57DFD,
             FAM_DM1: 0x5D577F7757FF, FAM_DM2: 0x7DFFD5F55D5F}
_LEVEL_OF_DIBIT = np.array([1, 3, -1, -3], dtype=np.float32)   # 00->+1, 01->+3, 10->-1, 11->-3
_DIBIT_OF_IDX = np.array([3, 2, 0, 1], dtype=np.uint8)         # digitize(u, [-2,0,2]) -> дибит


def _int_bits(v, n):
    return [(v >> (n - 1 - i)) & 1 for i in range(n)]


def _bits_int(bits):
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def _bits_bytes(bits):
    return bytes(_bits_int(bits[i:i + 8]) for i in range(0, len(bits), 8))


def _bytes_bits(data):
    out = []
    for b in data:
        out.extend(_int_bits(b, 8))
    return out


def _sync_signs(fam):
    bits = _int_bits(_SYNC_HEX[fam], 48)
    dib = [bits[2 * i] * 2 + bits[2 * i + 1] for i in range(24)]
    return _LEVEL_OF_DIBIT[dib] / 3.0


_SYNC_MAT = np.stack([_sync_signs(f) for f in (FAM_BS, FAM_MS, FAM_DM1, FAM_DM2)], axis=1)  # 24×4

# --------------------------------------------------------------------------------------
# Коды
# --------------------------------------------------------------------------------------


def _polymod(val, g, deg):
    """Остаток от деления многочлена val на g (степень deg) над GF(2)."""
    for bit in range(val.bit_length() - 1, deg - 1, -1):
        if val & (1 << bit):
            val ^= g << (bit - deg)
    return val


def _golay2087(m):
    """Golay(20,8): 8 бит данных + 11 бит остатка по g=0xC75 + бит общей чётности."""
    r = _polymod(m << 11, 0xC75, 11)
    p = (bin(m).count("1") + bin(r).count("1")) & 1
    return (m << 12) | (r << 1) | p


def _qr1676(m):
    """QR(16,7,6) для EMB: 7 бит + 8 бит остатка по g=0x139 + бит общей чётности."""
    r = _polymod(m << 8, 0x139, 8)
    p = (bin(m).count("1") + bin(r).count("1")) & 1
    return (m << 9) | (r << 1) | p


_GOLAY2087_TAB = [_golay2087(m) for m in range(256)]
_QR1676_TAB = [_qr1676(m) for m in range(128)]


def _nearest(tab, word, maxd):
    best, bd = -1, 99
    for m, cw in enumerate(tab):
        d = (cw ^ word).bit_count()
        if d < bd:
            best, bd = m, d
            if d == 0:
                break
    return (best, bd) if bd <= maxd else (-1, bd)


def _ham15113(d):
    return [d[0] ^ d[1] ^ d[2] ^ d[3] ^ d[5] ^ d[7] ^ d[8],
            d[1] ^ d[2] ^ d[3] ^ d[4] ^ d[6] ^ d[8] ^ d[9],
            d[2] ^ d[3] ^ d[4] ^ d[5] ^ d[7] ^ d[9] ^ d[10],
            d[0] ^ d[1] ^ d[2] ^ d[4] ^ d[6] ^ d[7] ^ d[10]]


def _ham1393(d):
    return [d[0] ^ d[1] ^ d[3] ^ d[5] ^ d[6],
            d[0] ^ d[1] ^ d[2] ^ d[4] ^ d[6] ^ d[7],
            d[0] ^ d[1] ^ d[2] ^ d[3] ^ d[5] ^ d[7] ^ d[8],
            d[0] ^ d[2] ^ d[4] ^ d[5] ^ d[8]]


def _ham16114(d):
    return _ham15113(d) + [d[0] ^ d[2] ^ d[5] ^ d[6] ^ d[8] ^ d[9] ^ d[10]]


def _ham743(d):
    return [d[0] ^ d[1] ^ d[2], d[1] ^ d[2] ^ d[3], d[0] ^ d[1] ^ d[3]]


def _syndrome_table(fn, k, n):
    """Синдром -> номер ошибочного бита для систематического кода (k данных, n всего)."""
    tab = {}
    for pos in range(n):
        e = [0] * n
        e[pos] = 1
        p = fn(e[:k])
        s = tuple(p[i] ^ e[k + i] for i in range(n - k))
        tab[s] = pos
    return tab


_SYN15 = _syndrome_table(_ham15113, 11, 15)
_SYN13 = _syndrome_table(_ham1393, 9, 13)
_SYN16 = _syndrome_table(_ham16114, 11, 16)


def _fix(word, fn, k, syn):
    """Исправляет одну ошибку на месте. Возвращает True, если слово (стало) корректно."""
    p = fn(word[:k])
    s = tuple(p[i] ^ word[k + i] for i in range(len(p)))
    if not any(s):
        return True
    pos = syn.get(s)
    if pos is None:
        return False
    word[pos] ^= 1
    return True


# --- GF(256), RS(12,9) ---
_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_x = 1
for _i in range(255):
    _GF_EXP[_i] = _x
    _GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]


def _gmul(a, b):
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _gdiv(a, b):
    if a == 0:
        return 0
    return _GF_EXP[(_GF_LOG[a] - _GF_LOG[b]) % 255]


_RS_G = [1, 14, 56, 64]   # x^3 + 14x^2 + 56x + 64 = (x+α)(x+α²)(x+α³)


def _rs129_parity(msg):
    r = [0, 0, 0]
    for b in msg:
        f = b ^ r[0]
        r = [r[1] ^ _gmul(f, _RS_G[1]), r[2] ^ _gmul(f, _RS_G[2]), _gmul(f, _RS_G[3])]
    return r


def _rs129_check(cw):
    """cw — 12 байт. Возвращает исправленные 12 байт или None."""
    syn = []
    for j in (1, 2, 3):
        a = _GF_EXP[j]
        s = 0
        for b in cw:
            s = _gmul(s, a) ^ b
        syn.append(s)
    if not any(syn):
        return list(cw)
    s1, s2, s3 = syn
    if s1 == 0 or s2 == 0:
        return None
    x = _gdiv(s2, s1)
    e = _gdiv(s1, x)
    if _gmul(e, _gmul(x, _gmul(x, x))) != s3:
        return None
    k = _GF_LOG[x]
    if k > 11:
        return None
    out = list(cw)
    out[11 - k] ^= e
    return out


def _crc_ccitt(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc ^ 0xFFFF


def _crc_ok(block, mask):
    """12-байтный блок с CRC-CCITT в последних двух байтах (под маской)."""
    got = ((block[10] << 8) | block[11]) ^ mask
    c = _crc_ccitt(block[:10])
    return got == c or got == (c ^ 0xFFFF)


# --- BPTC(196,96) ---
_BPTC_DATA_POS = list(range(4, 12)) + [p for r in range(1, 9) for p in range(1 + 15 * r, 12 + 15 * r)]
_BPTC_PERM = [(a * 181) % 196 for a in range(196)]      # deint[a] = raw[perm[a]]


def _bptc_encode(bits96):
    m = [0] * 196
    for p, b in zip(_BPTC_DATA_POS, bits96):
        m[p] = int(b)
    for r in range(9):
        row = m[1 + 15 * r: 16 + 15 * r]
        m[12 + 15 * r: 16 + 15 * r] = _ham15113(row)
    for c in range(15):
        col = [m[1 + c + 15 * r] for r in range(9)]
        for i, p in enumerate(_ham1393(col)):
            m[1 + c + 15 * (9 + i)] = p
    raw = [0] * 196
    for a in range(196):
        raw[_BPTC_PERM[a]] = m[a]
    return raw


def _bptc_decode(raw):
    """Возвращает (биты без исправления, биты после Hamming) — по 96 бит."""
    m = [int(raw[p]) for p in _BPTC_PERM]
    plain = [m[p] for p in _BPTC_DATA_POS]
    for _ in range(2):
        for c in range(15):
            col = [m[1 + c + 15 * r] for r in range(13)]
            if _fix(col, _ham1393, 9, _SYN13):
                for r in range(13):
                    m[1 + c + 15 * r] = col[r]
        for r in range(9):
            row = m[1 + 15 * r: 16 + 15 * r]
            if _fix(row, _ham15113, 11, _SYN15):
                m[1 + 15 * r: 16 + 15 * r] = row
    return plain, [m[p] for p in _BPTC_DATA_POS]


# --- встроенный LC (128 бит на 4 пакета B–E) ---
_EMB_DATA_POS = (list(range(0, 11)) + list(range(16, 27)) + list(range(32, 42)) + list(range(48, 58))
                 + list(range(64, 74)) + list(range(80, 90)) + list(range(96, 106)))
_EMB_CRC_POS = [42, 58, 74, 90, 106]
_EMB_PERM = [(a * 16) % 127 for a in range(127)] + [127]   # data[perm[a]] = raw[a]


def _emb_lc_encode(lc9):
    d = [0] * 128
    for p, b in zip(_EMB_DATA_POS, _bytes_bits(lc9)):
        d[p] = b
    crc = sum(lc9) % 31
    for i, p in enumerate(_EMB_CRC_POS):
        d[p] = (crc >> (4 - i)) & 1
    for r in range(7):
        d[16 * r + 11: 16 * r + 16] = _ham16114(d[16 * r: 16 * r + 11])
    for c in range(16):
        d[112 + c] = 0
        for r in range(7):
            d[112 + c] ^= d[16 * r + c]
    return [d[_EMB_PERM[a]] for a in range(128)]


def _emb_lc_decode(raw):
    d = [0] * 128
    for a in range(128):
        d[_EMB_PERM[a]] = int(raw[a])

    def extract(dd):
        lc = _bits_bytes([dd[p] for p in _EMB_DATA_POS])
        crc = _bits_int([dd[p] for p in _EMB_CRC_POS])
        return lc if sum(lc) % 31 == crc else None

    lc = extract(d)
    if lc is not None:
        return lc
    for r in range(7):
        row = d[16 * r: 16 * r + 16]
        if not _fix(row, _ham16114, 11, _SYN16):
            return None
        d[16 * r: 16 * r + 16] = row
    for c in range(16):
        par = 0
        for r in range(8):
            par ^= d[16 * r + c]
        if par:
            return None
    return extract(d)


# --- AMBE+2: перестановка 72 бит в ambe_fr[4][24] (таблицы DSD) ---
_RW = [0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1,
       0, 1, 0, 1, 0, 2, 0, 2, 0, 2, 0, 2, 0, 2, 0, 2, 0, 2]
_RX = [23, 10, 22, 9, 21, 8, 20, 7, 19, 6, 18, 5, 17, 4, 16, 3, 15, 2,
       14, 1, 13, 0, 12, 10, 11, 9, 10, 8, 9, 7, 8, 6, 7, 5, 6, 4]
_RY = [0, 2, 0, 2, 0, 2, 0, 2, 0, 3, 0, 3, 1, 3, 1, 3, 1, 3,
       1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3]
_RZ = [5, 3, 4, 2, 3, 1, 2, 0, 1, 13, 0, 12, 22, 11, 21, 10, 20, 9,
       19, 8, 18, 7, 17, 6, 16, 5, 15, 4, 14, 3, 13, 2, 12, 1, 11, 0]
_FR_HI = np.array(_RW) * 24 + np.array(_RX)    # индекс в плоском ambe_fr[96] для старшего бита дибита
_FR_LO = np.array(_RY) * 24 + np.array(_RZ)


def ambe_deinterleave(bits72):
    fr = np.zeros(96, dtype=np.int8)
    b = np.asarray(bits72, dtype=np.int8)
    fr[_FR_HI] = b[0::2]
    fr[_FR_LO] = b[1::2]
    return fr


def _golay2312_parity(m12):
    return _polymod(m12 << 11, 0xC75, 11)


def ambe_encode(ambe_d):
    """49 бит параметров AMBE+2 -> 72 бита в эфирном порядке (обратное к mbelib + DSD)."""
    d = [int(x) for x in ambe_d]
    fr = [[0] * 24 for _ in range(4)]
    for i in range(12):
        fr[0][23 - i] = d[i]
    c0 = _bits_int(d[0:12])
    p = _golay2312_parity(c0)
    for j in range(11):
        fr[0][1 + j] = (p >> j) & 1
    fr[0][0] = sum(fr[0][1:]) & 1
    for i in range(12):
        fr[1][22 - i] = d[12 + i]
    p = _golay2312_parity(_bits_int(d[12:24]))
    for j in range(11):
        fr[1][j] = (p >> j) & 1
    pr = [16 * c0]
    for i in range(1, 24):
        pr.append((173 * pr[i - 1] + 13849) % 65536)
    for k, j in enumerate(range(22, -1, -1), start=1):
        fr[1][j] ^= pr[k] // 32768
    for i in range(11):
        fr[2][10 - i] = d[24 + i]
    for i in range(14):
        fr[3][13 - i] = d[35 + i]
    out = []
    for i in range(36):
        out.append(fr[_RW[i]][_RX[i]])
        out.append(fr[_RY[i]][_RZ[i]])
    return out


def ambe_params(b0, b1=0, b2=16, b3=0, b4=0):
    """Собирает 49 бит ambe_d из основных индексов (раскладка как в mbe_decodeAmbe2450Parms)."""
    d = [0] * 49
    for vals, poss in ((b0, [0, 1, 2, 3, 37, 38, 39]), (b1, [4, 5, 6, 7, 35]),
                       (b2, [8, 9, 10, 11, 36]), (b3, [12, 13, 14, 15, 16, 17, 18, 19, 40]),
                       (b4, [20, 21, 22, 23, 41, 42, 43])):
        n = len(poss)
        for i, p in enumerate(poss):
            d[p] = (vals >> (n - 1 - i)) & 1
    return d


# --------------------------------------------------------------------------------------
# mbelib через ctypes
# --------------------------------------------------------------------------------------
_mbe = None


def _mbe_path():
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "rtltv", "driver", "mbelib.dll")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "driver", "mbelib.dll")


def mbelib():
    """Загружает mbelib.dll (или None, если её нет)."""
    global _mbe
    if _mbe is not None:
        return _mbe or None
    path = _mbe_path()
    try:
        L = C.CDLL(path)
        vp = C.c_void_p
        L.mbe_initMbeParms.argtypes = [vp, vp, vp]
        L.mbe_initMbeParms.restype = None
        L.mbe_processAmbe3600x2450Framef.argtypes = [vp, vp, vp, vp, vp, vp, vp, vp, vp, C.c_int]
        L.mbe_processAmbe3600x2450Framef.restype = None
        _mbe = L
    except OSError:
        _mbe = False
        return None
    return _mbe


_MBE_PARMS_SIZE = 1200    # sizeof(mbe_parms) = 1164


class AmbeVoice:
    """Состояние синтезатора одного слота."""
    calls = 0              # счётчик вызовов mbelib (для тестов)

    def __init__(self):
        self.L = mbelib()
        self._p = [C.create_string_buffer(_MBE_PARMS_SIZE) for _ in range(3)]
        self._fr = (C.c_char * 96)()
        self._d = (C.c_char * 49)()
        self._out = (C.c_float * 160)()
        self._e1 = C.c_int(0)
        self._e2 = C.c_int(0)
        self._es = C.create_string_buffer(64)
        self.errs = 0
        self.reset()

    @property
    def ok(self):
        return self.L is not None

    def reset(self):
        if self.L is not None:
            self.L.mbe_initMbeParms(*self._p)

    def decode(self, bits72):
        """72 бита из эфира -> 160 отсчётов float32 8 кГц (≈ ±1)."""
        if self.L is None:
            return np.zeros(160, np.float32)
        C.memmove(self._fr, ambe_deinterleave(bits72).tobytes(), 96)
        self.L.mbe_processAmbe3600x2450Framef(
            self._out, C.byref(self._e1), C.byref(self._e2), self._es, self._fr, self._d,
            self._p[0], self._p[1], self._p[2], 3)
        AmbeVoice.calls += 1
        self.errs = self._e2.value
        return np.clip(np.frombuffer(self._out, np.float32) * (1.0 / 8000.0), -1.0, 1.0)

    def last_params(self):
        return list(bytes(self._d))


# --------------------------------------------------------------------------------------
# Разбор LC / CSBK
# --------------------------------------------------------------------------------------
_FLCO_NAMES = {0: "групповой", 3: "индивидуальный", 4: "псевдоним 1", 5: "псевдоним 2",
               6: "псевдоним 3", 7: "псевдоним 4", 8: "GPS"}
_DT_NAMES = {0: "PI-заголовок", 1: "LC-заголовок", 2: "Терминатор", 3: "CSBK", 4: "MBC-заголовок",
             5: "MBC-продолжение", 6: "Заголовок данных", 7: "Данные 1/2", 8: "Данные 3/4",
             9: "Idle", 10: "Данные 1/1", 11: "USB-данные"}
_CSBK_NAMES = {0x04: "запрос индивидуального вызова", 0x05: "ответ на запрос", 0x07: "синхронизация каналов",
               0x38: "активация ретранслятора", 0x3D: "преамбула"}
_CSBK_T3 = {0x19: "Aloha (Tier III)", 0x1C: "Ahoy (Tier III)", 0x28: "C_BCAST (Tier III)"}
CSBK_REPEAT_S = 30.0      # одинаковые CSBK (маяки управляющего канала) в журнал не чаще раза в 30 с


def _parse_lc(lc):
    flco = lc[0] & 0x3F
    return {"flco": flco, "fid": lc[1], "so": lc[2],
            "dst": (lc[3] << 16) | (lc[4] << 8) | lc[5],
            "src": (lc[6] << 16) | (lc[7] << 8) | lc[8],
            "group": flco == 0, "private": flco == 3,
            "encrypted": bool(lc[2] & 0x40), "emergency": bool(lc[2] & 0x80)}


def _make_lc(src, dst, group=True, so=0, fid=0):
    return bytes([0 if group else 3, fid, so, (dst >> 16) & 255, (dst >> 8) & 255, dst & 255,
                  (src >> 16) & 255, (src >> 8) & 255, src & 255])


# --------------------------------------------------------------------------------------
# Декодер
# --------------------------------------------------------------------------------------


def _rrc(sps=SPS, alpha=0.2, span=8):
    t = np.arange(-span * sps // 2, span * sps // 2 + 1) / sps
    h = np.empty_like(t)
    for i, x in enumerate(t):
        if abs(x) < 1e-9:
            h[i] = 1 - alpha + 4 * alpha / np.pi
        elif abs(abs(x) - 1 / (4 * alpha)) < 1e-9:
            h[i] = alpha / np.sqrt(2) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha))
                                         + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
        else:
            h[i] = (np.sin(np.pi * x * (1 - alpha)) + 4 * alpha * x * np.cos(np.pi * x * (1 + alpha))) / \
                   (np.pi * x * (1 - (4 * alpha * x) ** 2))
    return h


_H_RX = _rrc()
_H_RX = (_H_RX / _H_RX.sum()).astype(np.float64)


class _Slot:
    def __init__(self, n):
        self.n = n
        self.voice = AmbeVoice()
        self.clear()
        self.cc = None
        self.last = ""
        self.last_t = 0.0
        self.csbk_seen = {}

    def clear(self):
        self.active = False
        self.announced = False
        self.src = 0
        self.dst = 0
        self.group = True
        self.encrypted = False
        self.pi = False           # был PI-заголовок перед вызовом
        self.vf = None            # номер кадра в суперкадре (0=A … 5=F), None — нет голоса
        self.missed_a = 0
        self.bad_emb = 0
        self.idle = 0
        self.frags = None
        self.sf = 0
        self.lc_known = False


class DmrDecoder:
    """Потоковый декодер DMR. Вызывать process() блоками любой длины (обычно ~50 мс)."""

    HUNT_THR = 0.82        # порог нормированной корреляции синхрослова при поиске
    TRACK_THR = 0.62       # порог при сопровождении (в ожидаемом месте)
    LOST_BURSTS = 24       # столько пакетов без единого подтверждения -> потеря синхронизации

    def __init__(self, fs=48000):
        self.fs = int(fs)
        self.slot_filter = 0
        self._zi = np.zeros(len(_H_RX) - 1)
        self._y = np.zeros(0, np.float32)
        self._y0 = 0                       # абсолютный номер отсчёта self._y[0]
        self._n_in = 0                     # сколько отсчётов 48 кГц пришло всего
        self._search = 0                   # с какого абсолютного отсчёта искать синхро
        self._pol = 1                      # полярность дискриминатора
        self._slots = {1: _Slot(1), 2: _Slot(2)}
        self._events = []
        self._audio = []                   # очередь звука 48 кГц
        self._audio_len = 0
        self._playing = False
        self._audio_slot = None
        self._up = firwin(121, 3500, fs=48000) * 6.0
        self._up_zi = np.zeros(len(self._up) - 1)
        self._unlock()
        self.sync_level = 0.0
        self.stats = {"bursts": 0, "syncs": 0, "voice": 0, "lc_ok": 0, "csbk": 0, "emb_ok": 0}

    # ---------------------------------------------------------------- публичный интерфейс
    def process(self, freq_hz):
        x = np.asarray(freq_hz, dtype=np.float32)
        n_out = len(x)
        if self.fs != FS and len(x):
            x = resample_poly(x, FS, self.fs).astype(np.float32)
        if len(x):
            y, self._zi = lfilter(_H_RX, 1.0, x.astype(np.float64), zi=self._zi)
            self._y = np.concatenate([self._y, y.astype(np.float32)])
            self._n_in += len(x)
        self._run()
        self._trim()
        ev, self._events = self._events, []
        return self._pull_audio(n_out), ev

    def status(self):
        slots = {}
        for n, s in self._slots.items():
            slots[n] = {"active": s.active, "src": s.src, "dst": s.dst, "group": s.group,
                        "private": not s.group, "encrypted": s.encrypted, "cc": s.cc,
                        "last": s.last, "voice_errs": s.voice.errs if s.active else 0}
        return {"locked": self._confirmed, "mode": FAM_NAMES.get(self._fam, "") if self._confirmed else "",
                "sync": int(round(100 * self.sync_level)) if self._confirmed else 0,
                "polarity": self._pol, "offset_hz": float(self._dc) if self._confirmed else 0.0,
                "deviation_hz": float(self._scale) if self._confirmed else 0.0,
                "mbelib": mbelib() is not None, "audio_slot": self._audio_slot, "slots": slots}

    def reset(self):
        self.__init__(self.fs)

    # ---------------------------------------------------------------- служебное
    def _unlock(self):
        self._locked = False
        self._confirmed = False
        self._next = 0.0
        self._spb = float(SPB)
        self._dc = 0.0
        self._scale = DEV3
        self._fam = FAM_BS
        self._slot_next = 1
        self._tc_vote = 0
        self._tc_fresh = True
        self._first_sync = None
        self._miss = 0
        self._since_sync = 0
        self._tent_pol = self._pol

    def _trim(self):
        keep_from = (self._next - 3 * SPB) if self._locked else (self._search - 3 * SPB)
        cut = int(keep_from) - self._y0
        if cut > 0:
            self._y = self._y[cut:]
            self._y0 += cut

    def _end(self):
        return self._y0 + len(self._y)

    def _sample(self, pos):
        q = np.asarray(pos, dtype=np.float64) - self._y0
        i = np.floor(q).astype(np.int64)
        f = q - i
        i = np.clip(i, 0, len(self._y) - 2)
        return self._y[i] * (1 - f) + self._y[i + 1] * f

    def _corr_at(self, center):
        """Нормированная корреляция синхрослова в точке center (первый символ синхро)."""
        v = self._sample(center + np.arange(24) * (self._spb / SLOT_SYM))
        v = v - v.mean()
        nv = np.sqrt((v * v).sum()) + 1e-9
        c = (v @ _SYNC_MAT) / (nv * np.sqrt(24.0))
        return c, v

    def _levels(self, center, fam, sign):
        """dc, размах (Гц на символ ±3) и качество подгонки по синхрослову."""
        v = self._sample(center + np.arange(24) * (self._spb / SLOT_SYM))
        s = _SYNC_MAT[:, fam] * sign
        hi, lo = v[s > 0].mean(), v[s < 0].mean()
        dc = 0.5 * (hi + lo)
        scale = 0.5 * (hi - lo)
        if scale <= 1:
            return dc, scale, 9.0
        fit = float(np.mean(np.abs(v - (dc + s * scale)))) / scale
        return dc, scale, fit

    def _run(self):
        guard = 8
        while True:
            end = self._end()
            if not self._locked:
                if not self._hunt(end):
                    return
                continue
            if self._next + SYNC_OFF * SPS + SPB // 2 + 24 * SPS + guard > end:
                return
            self._track_burst()

    # ---------------------------------------------------------------- поиск синхро
    def _hunt(self, end):
        lo = max(self._search, self._y0 + (CACH_SYM + SYNC_OFF) * SPS + 2)
        hi = end - (BURST_SYM - SYNC_OFF) * SPS - 12
        if hi - lo < 4:
            return False
        a = int(lo - self._y0)
        b = int(hi - self._y0)
        seg = self._y[a: b + 23 * SPS + 1].astype(np.float64)
        win = np.lib.stride_tricks.sliding_window_view(seg, 23 * SPS + 1)[:, ::SPS]
        win = win[: b - a]
        v = win - win.mean(axis=1, keepdims=True)
        nv = np.sqrt((v * v).sum(axis=1)) + 1e-9
        c = (v @ _SYNC_MAT) / (nv[:, None] * np.sqrt(24.0))
        ac = np.abs(c).max(axis=1)
        cand = np.flatnonzero(ac >= self.HUNT_THR)
        while len(cand):
            k = int(cand[0])
            # локальный максимум в пределах символа
            k2 = min(len(ac), k + SPS)
            k = k + int(np.argmax(ac[k:k2]))
            frac = 0.0
            if 0 < k < len(ac) - 1:
                y1, y2, y3 = ac[k - 1], ac[k], ac[k + 1]
                den = y1 - 2 * y2 + y3
                if den < 0:
                    frac = float(np.clip(0.5 * (y1 - y3) / den, -0.5, 0.5))
            fam = int(np.argmax(np.abs(c[k])))
            sign = 1 if c[k, fam] > 0 else -1
            center = lo + k + frac
            if self._try_lock(center, fam, sign, float(ac[k])):
                return True
            cand = cand[cand > k + SPS]
        self._search = hi
        return False

    def _try_lock(self, center, fam, sign, corr):
        dc, scale, fit = self._levels(center, fam, sign)
        if not (600.0 < scale < 3600.0) or fit > 0.45:
            return False
        start = center - SYNC_OFF * SPS
        self._locked = True
        self._confirmed = False
        self._fam = fam
        self._dc, self._scale = dc, scale
        self._next = start
        self._spb = float(SPB)
        self._miss = 0
        self._since_sync = 0
        self.sync_level = corr
        # Версия полярности: данные проверяются сразу (Golay), голос — по следующим EMB
        bits = self._burst_bits(start, self._pol)
        kind_p = "voice" if sign * self._pol > 0 else "data"
        if kind_p == "data" and self._slot_type(bits[1], 1) is not None:
            self._tent_pol = self._pol
        else:
            bits_n = self._burst_bits(start, -self._pol)
            if kind_p == "voice" and self._slot_type(bits_n[1], 1) is not None:
                self._tent_pol = -self._pol
            else:
                self._tent_pol = self._pol if kind_p == "voice" else -self._pol
        self._first_sync = (fam, sign)
        return True

    # ---------------------------------------------------------------- сопровождение
    def _burst_bits(self, start, pol):
        tsym = self._spb / SLOT_SYM
        v = self._sample(start + np.arange(-CACH_SYM, BURST_SYM) * tsym)
        u = pol * (v - self._dc) * (3.0 / self._scale)
        dib = _DIBIT_OF_IDX[np.digitize(u, (-2.0, 0.0, 2.0))]
        bits = np.empty(2 * len(dib), np.uint8)
        bits[0::2] = dib >> 1
        bits[1::2] = dib & 1
        return bits[:2 * CACH_SYM], bits[2 * CACH_SYM:]

    def _slot_type(self, burst, maxd=3):
        w = _bits_int(burst[98:108]) << 10 | _bits_int(burst[156:166])
        m, d = _nearest(_GOLAY2087_TAB, w, maxd)
        if m < 0:
            return None
        return m >> 4, m & 15

    def _wide_sync(self, pe):
        """Поиск синхрослова в пределах ±полслота от ожидаемого места (после скачка тактов)."""
        half = SPB // 2
        a = int(pe - half - self._y0)
        b = int(pe + half - self._y0)
        a = max(a, 0)
        b = min(b, len(self._y) - 23 * SPS - 2)
        if b - a < 2 * SPS:
            return (0.0, 0, 0, 0)
        seg = self._y[a: b + 23 * SPS + 1].astype(np.float64)
        win = np.lib.stride_tricks.sliding_window_view(seg, 23 * SPS + 1)[:, ::SPS][: b - a]
        v = win - win.mean(axis=1, keepdims=True)
        nv = np.sqrt((v * v).sum(axis=1)) + 1e-9
        c = (v @ _SYNC_MAT) / (nv[:, None] * np.sqrt(24.0))
        ac = np.abs(c).max(axis=1)
        for k in np.argsort(ac)[::-1][:3]:
            if ac[k] < self.HUNT_THR:
                break
            fam = int(np.argmax(np.abs(c[k])))
            sign = 1 if c[k, fam] > 0 else -1
            pos = self._y0 + a + int(k)
            _dc, scale, fit = self._levels(pos, fam, sign)
            if 600.0 < scale < 3600.0 and fit <= 0.45:
                return (float(ac[k]), float(pos - pe), fam, sign)
        return (0.0, 0, 0, 0)

    def _track_burst(self):
        start = self._next
        tsym = self._spb / SLOT_SYM
        sync = None
        first = self._first_sync
        if first is not None:
            self._first_sync = None
            sync = first
        else:
            pe = start + SYNC_OFF * tsym
            offs = np.arange(-4, 5, dtype=np.float64)
            best = (0.0, 0, 0, 0)
            for o in offs:
                c, _ = self._corr_at(pe + o)
                ca = np.abs(c)
                ca[np.arange(4) != self._fam] -= 0.2      # другое семейство — только при сильной корреляции
                f = int(np.argmax(ca))
                if ca[f] > best[0]:
                    best = (abs(c[f]), o, f, 1 if c[f] > 0 else -1)
            if best[0] < self.TRACK_THR and self._since_sync >= 2:
                best = self._wide_sync(pe)
            if best[0] >= self.TRACK_THR:
                corr, o, fam, sign = best
                # уточнение по параболе
                cm, _ = self._corr_at(pe + o - 1)
                cp, _ = self._corr_at(pe + o + 1)
                y1, y2, y3 = abs(cm[fam]), corr, abs(cp[fam])
                den = y1 - 2 * y2 + y3
                frac = float(np.clip(0.5 * (y1 - y3) / den, -0.5, 0.5)) if den < 0 else 0.0
                err = o + frac
                dc, scale, fit = self._levels(pe + err, fam, sign)
                if 400.0 < scale < 4000.0 and fit < 0.6:
                    start += err
                    if abs(err) > 6:
                        self._spb = float(SPB)          # скачок (пропуск отсчётов) — такт заново
                    elif self._since_sync > 0:
                        self._spb += 0.2 * err / self._since_sync
                        self._spb = float(np.clip(self._spb, SPB * 0.999, SPB * 1.001))
                    self._dc += 0.5 * (dc - self._dc)
                    self._scale += 0.5 * (scale - self._scale)
                    self.sync_level += 0.3 * (corr - self.sync_level)
                    self._since_sync = 0
                    sync = (fam, sign)
        pol = self._tent_pol if not self._confirmed else self._pol
        cach, burst = self._burst_bits(start, pol)
        valid = self._handle_burst(cach, burst, sync, pol)
        if sync is not None and first is None:
            valid = True       # синхрослово снова там, где ожидали
        self.stats["bursts"] += 1
        if sync is not None:
            self.stats["syncs"] += 1
        self._since_sync += 1
        self._next = start + self._spb
        if valid:
            self._miss = 0
            if not self._confirmed:
                self._confirmed = True
                self._pol = pol
        else:
            self._miss += 1
            if sync is None:
                self.sync_level *= 0.97
            lim = self.LOST_BURSTS if self._confirmed else 7
            if self._miss >= lim or self._since_sync > 2 * self.LOST_BURSTS:
                self._lose_sync()

    def _lose_sync(self):
        was = self._confirmed
        for s in self._slots.values():
            if s.active:
                self._call_end(s, None, "конец вызова (потеря синхронизации)")
            s.vf = None
        self._search = int(self._next)
        self._unlock()
        self.sync_level = 0.0
        if was:
            self._emit("sync_lost", 0, None, text="потеря синхронизации DMR")

    # ---------------------------------------------------------------- пакеты
    def _handle_burst(self, cach, burst, sync, pol):
        # номер слота: CACH (BS), тип синхрослова (DM), иначе — чередование
        if sync is not None:
            self._fam = sync[0]
        slot_n = self._slot_next
        if self._fam == FAM_BS:
            tact = [int(cach[i]) for i in (0, 4, 8, 12, 14, 18, 22)]
            ok = _ham743(tact[:4]) == tact[4:]
            tc = 2 if tact[1] else 1
            if ok and self._tc_fresh:
                self._tc_fresh = False
                slot_n = tc
            self._tc_vote += (2 if ok else 1) * (1 if tc == slot_n else -1)
            self._tc_vote = max(-6, min(6, self._tc_vote))
            if self._tc_vote < -2:
                slot_n = 3 - slot_n
                self._tc_vote = 2
        elif sync is not None and sync[0] in (FAM_DM1, FAM_DM2):
            slot_n = 1 if sync[0] == FAM_DM1 else 2
        self._slot_next = 3 - slot_n
        st = self._slots[slot_n]
        confirmed = self._confirmed
        if sync is not None:
            kind = "voice" if sync[1] * pol > 0 else "data"
            if kind == "data":
                return self._on_data(st, burst, confirmed)
            st.missed_a = 0
            return self._on_voice(st, burst, True, confirmed) or confirmed
        if st.vf is not None:
            if st.vf < 5:
                return self._on_voice(st, burst, False, confirmed)
            if st.missed_a < 2 and st.bad_emb < 3:
                st.missed_a += 1
                self._on_voice(st, burst, True, confirmed)
                return False
            st.vf = None
        if st.active:
            st.idle += 1
            if st.idle >= 4:
                self._call_end(st, None, "конец вызова (нет голоса)")
        return False

    def _on_data(self, st, burst, confirmed):
        r = self._slot_type(burst, 3 if confirmed else 1)
        if r is None:
            return False
        cc, dt = r
        st.cc = cc
        st.last = _DT_NAMES.get(dt, f"тип {dt}")
        st.last_t = time.monotonic()
        st.vf = None
        info = [int(b) for b in burst[0:98]] + [int(b) for b in burst[166:264]]
        if dt in (0, 1, 2, 3, 4, 6):
            plain, fixed = _bptc_decode(info)
            if dt in (1, 2):
                mask = (0x96, 0x96, 0x96) if dt == 1 else (0x99, 0x99, 0x99)
                lc = None
                for cand in (plain, fixed):
                    blk = list(_bits_bytes(cand))
                    for i in range(3):
                        blk[9 + i] ^= mask[i]
                    good = _rs129_check(blk)
                    if good is not None:
                        lc = bytes(good[:9])
                        break
                if lc is not None:
                    self.stats["lc_ok"] += 1
                    info_lc = _parse_lc(lc)
                    if dt == 1:
                        self._on_header(st, info_lc)
                    else:
                        st.last = "Терминатор"
                        if st.active:
                            self._call_end(st, info_lc, None)
            elif dt in (0, 3, 4, 6):
                mask = {0: 0x6969, 3: 0xA5A5, 4: 0xAAAA, 6: 0xCCCC}[dt]
                blk = None
                for cand in (plain, fixed):
                    b = list(_bits_bytes(cand))
                    if _crc_ok(b, mask):
                        blk = b
                        break
                if blk is not None:
                    if dt == 0:
                        st.pi = True
                        st.encrypted = st.encrypted or st.active
                        if confirmed:
                            self._emit("data", st.n, st.cc, text=f"PI-заголовок: шифрование, алгоритм {blk[0]}, "
                                       f"ключ {blk[2]}", encrypted=True)
                    elif dt == 3:
                        self.stats["csbk"] += 1
                        self._on_csbk(st, blk, confirmed)
                    elif dt == 6 and confirmed:
                        grp = bool(blk[0] & 0x80)
                        dst = (blk[2] << 16) | (blk[3] << 8) | blk[4]
                        src = (blk[5] << 16) | (blk[6] << 8) | blk[7]
                        self._emit("data", st.n, st.cc, src=src, dst=dst, group=grp,
                                   text=f"передача данных {src} → {'TG ' if grp else ''}{dst}")
        if dt == 9 and st.active:
            st.idle += 1
            if st.idle >= 2:
                self._call_end(st, None, "конец вызова (без терминатора)")
        return True

    def _on_csbk(self, st, b, confirmed):
        op, fid = b[0] & 0x3F, b[1]
        dst = (b[4] << 16) | (b[5] << 8) | b[6]
        src = (b[7] << 16) | (b[8] << 8) | b[9]
        key = bytes(b[:10])
        now = time.monotonic()
        st.last = "CSBK"
        if not confirmed:
            return
        seen = st.csbk_seen
        if now - seen.get(key, -1e9) < CSBK_REPEAT_S:
            return
        if len(seen) > 64:
            seen.clear()
        seen[key] = now
        if fid == 0 and op in _CSBK_NAMES:
            name = _CSBK_NAMES[op]
            if op == 0x3D:
                grp = bool(b[2] & 0x40)
                text = f"CSBK {name}: {src} → {'TG ' if grp else ''}{dst}, блоков {b[3]}"
                self._emit("csbk", st.n, st.cc, src=src, dst=dst, group=grp, text=text)
                return
            text = f"CSBK {name}: {src} → {dst}"
        elif fid == 0 and op in _CSBK_T3:
            src = dst = 0
            text = f"CSBK {_CSBK_T3[op]}: {key[2:].hex().upper()}"
        else:
            src = dst = 0
            text = f"CSBK 0x{op:02X} FID 0x{fid:02X}: {key[2:].hex().upper()}"
        self._emit("csbk", st.n, st.cc, src=src, dst=dst, group=False, text=text)

    def _on_header(self, st, lc):
        if st.active and st.lc_known and (lc["src"], lc["dst"]) == (st.src, st.dst):
            return
        if st.active:
            self._call_end(st, None, "конец вызова")
        self._call_begin(st)
        self._apply_lc(st, lc)

    def _apply_lc(self, st, lc):
        if lc["flco"] not in (0, 3):
            return
        st.src, st.dst, st.group = lc["src"], lc["dst"], lc["group"]
        st.encrypted = st.encrypted or lc["encrypted"]
        st.lc_known = True
        if not st.announced and self._confirmed:
            self._announce(st)

    def _announce(self, st):
        st.announced = True
        kind = "групповой" if st.group else "индивидуальный"
        who = f"{st.src or '?'} → {'TG ' if st.group else ''}{st.dst or '?'}"
        enc = ", шифрован" if st.encrypted else ""
        self._emit("call_start", st.n, st.cc, src=st.src, dst=st.dst, group=st.group,
                   encrypted=st.encrypted, text=f"{kind} вызов {who}{enc}")

    def _call_begin(self, st):
        pi = st.pi
        st.clear()
        st.active = True
        st.encrypted = pi
        st.voice.reset()

    def _call_end(self, st, lc, why):
        if lc is not None and lc["flco"] in (0, 3):
            st.src, st.dst, st.group = lc["src"], lc["dst"], lc["group"]
            st.encrypted = st.encrypted or lc["encrypted"]
        junk = not st.announced and not st.lc_known and st.sf < 2
        if (self._confirmed or st.announced) and not junk:
            if not st.announced:
                self._announce(st)
            who = f"{st.src or '?'} → {'TG ' if st.group else ''}{st.dst or '?'}"
            self._emit("call_end", st.n, st.cc, src=st.src, dst=st.dst, group=st.group,
                       encrypted=st.encrypted, text=f"{why or 'конец вызова'} {who}")
        info = (st.src, st.dst, st.group, st.encrypted)
        st.clear()
        st.src, st.dst, st.group, st.encrypted = info
        if self._audio_slot == st.n:
            self._audio_slot = None

    def _on_voice(self, st, burst, is_sync, confirmed):
        valid = False
        if not st.active:
            self._call_begin(st)
        st.idle = 0
        if is_sync:
            st.vf = 0
            st.sf += 1
            st.last = "Голос"
            if st.sf >= 3 and not st.announced and confirmed:
                self._announce(st)
        else:
            st.vf += 1
            emb = (_bits_int(burst[108:116]) << 8) | _bits_int(burst[148:156])
            m, _d = _nearest(_QR1676_TAB, emb, 1 if confirmed else 0)
            if m >= 0:
                cc, pi, lcss = m >> 3, (m >> 2) & 1, m & 3
                if st.cc is None or cc == st.cc or not st.lc_known:
                    valid = True
                    st.bad_emb = 0
                    st.cc = cc
                    self.stats["emb_ok"] += 1
                    if pi and _d == 0 and not st.encrypted:
                        st.encrypted = True
                    frag = [int(b) for b in burst[116:148]]
                    if lcss == 1:
                        st.frags = [frag]
                    elif lcss == 3 and st.frags is not None and len(st.frags) < 3:
                        st.frags.append(frag)
                    elif lcss == 2 and st.frags is not None and len(st.frags) == 3:
                        st.frags.append(frag)
                        lc = _emb_lc_decode(sum(st.frags, []))
                        st.frags = None
                        if lc is not None:
                            info = _parse_lc(lc)
                            if info["flco"] in (0, 3):
                                self._apply_lc(st, info)
                    elif lcss != 0:
                        st.frags = None
                else:
                    st.bad_emb += 1
            else:
                st.bad_emb += 1
            if st.bad_emb >= 4:
                self._call_end(st, None, "конец вызова (сигнал пропал)")
                st.vf = None
                return False
        self.stats["voice"] += 1
        if confirmed or valid:
            self._voice_audio(st, burst)
        return valid

    def _voice_audio(self, st, burst):
        if self.slot_filter not in (0, st.n) or st.encrypted:
            return
        other = self._slots[3 - st.n]
        if self._audio_slot is None or (self._audio_slot != st.n and not other.active):
            self._audio_slot = st.n
        if self._audio_slot != st.n:
            return
        frames = (burst[0:72], np.concatenate([burst[72:108], burst[156:192]]), burst[192:264])
        pcm = np.concatenate([st.voice.decode(f) for f in frames])
        self._push_audio(pcm)

    # ---------------------------------------------------------------- звук
    def _push_audio(self, pcm8):
        z = np.zeros(len(pcm8) * 6)
        z[::6] = pcm8
        y, self._up_zi = lfilter(self._up, 1.0, z, zi=self._up_zi)
        y = y.astype(np.float32)
        if self.fs != FS:
            y = resample_poly(y, self.fs, FS).astype(np.float32)
        self._audio.append(y)
        self._audio_len += len(y)
        cap = int(0.6 * self.fs)
        while self._audio_len > cap and len(self._audio) > 1:
            self._audio_len -= len(self._audio.pop(0))

    def _pull_audio(self, n):
        out = np.zeros(n, np.float32)
        if not self._playing and self._audio_len >= int(0.12 * self.fs):
            self._playing = True
        if not self._playing:
            if self._audio_len and not any(s.active for s in self._slots.values()):
                self._playing = True      # хвост вызова — доиграть
            else:
                return out
        pos = 0
        while pos < n and self._audio:
            a = self._audio[0]
            k = min(n - pos, len(a))
            out[pos:pos + k] = a[:k]
            pos += k
            if k == len(a):
                self._audio.pop(0)
            else:
                self._audio[0] = a[k:]
            self._audio_len -= k
        if not self._audio:
            self._playing = False
        return out

    def _emit(self, typ, slot, cc, src=0, dst=0, group=True, encrypted=False, text=""):
        st = self._slots.get(slot)
        if st is not None and typ in ("call_start", "call_end"):
            encrypted = encrypted or st.encrypted
        self._events.append({"type": typ, "slot": slot, "cc": cc if cc is not None else -1,
                             "src": int(src), "dst": int(dst), "group": bool(group),
                             "encrypted": bool(encrypted), "text": text})


# --------------------------------------------------------------------------------------
# Генератор (кодер) DMR
# --------------------------------------------------------------------------------------
_SYNC_BITS = {f: _int_bits(h, 48) for f, h in _SYNC_HEX.items()}
_SYNC_BITS_DATA = {f: [1 - b if i % 2 == 0 else b for i, b in enumerate(_int_bits(h, 48))]
                   for f, h in _SYNC_HEX.items()}
# инверсия символа ±3: 01 <-> 11, т.е. меняется только старший бит дибита


def _cach(slot, at=1):
    tact = [at, 1 if slot == 2 else 0, 0, 0]
    tact += _ham743(tact)
    bits = [0] * 24
    for p, b in zip((0, 4, 8, 12, 14, 18, 22), tact):
        bits[p] = b
    return bits


def data_burst(cc, dt, payload96, fam=FAM_BS):
    info = _bptc_encode(payload96)
    st = _int_bits(_golay2087((cc << 4) | dt), 20)
    return info[:98] + st[:10] + _SYNC_BITS_DATA[fam] + st[10:] + info[98:]


def lc_burst(cc, lc9, terminator=False, fam=FAM_BS):
    mask = 0x99 if terminator else 0x96
    par = _rs129_parity(lc9)
    blk = list(lc9) + [p ^ mask for p in par]
    return data_burst(cc, 2 if terminator else 1, _bytes_bits(blk), fam)


def idle_burst(cc, fam=FAM_BS):
    blk = [0xFF, 0x83, 0xDF, 0x17, 0x32, 0x09, 0x4E, 0x1C, 0xEF, 0x0A, 0x65, 0x00]
    return data_burst(cc, 9, _bytes_bits(blk), fam)


def csbk_burst(cc, blk10, fam=FAM_BS):
    crc = _crc_ccitt(blk10) ^ 0xA5A5
    return data_burst(cc, 3, _bytes_bits(list(blk10) + [crc >> 8, crc & 255]), fam)


def voice_burst(frames, idx, cc, emb_frag=None, lcss=0, pi=0, fam=FAM_BS):
    """frames — 3 кадра по 72 бита, idx — 0 (A, синхро) … 5 (F)."""
    v = list(frames[0]) + list(frames[1]) + list(frames[2])
    if idx == 0:
        mid = _SYNC_BITS[fam]
    else:
        emb = _int_bits(_qr1676((cc << 3) | (pi << 2) | lcss), 16)
        mid = emb[:8] + list(emb_frag if emb_frag is not None else [0] * 32) + emb[8:]
    return v[:108] + mid + v[108:]


def voice_call(cc, lc9, ambe_frames, superframes, headers=2, terms=2, fam=FAM_BS):
    """Пакеты одного слота для вызова: заголовки, суперкадры A–F со встроенным LC, терминаторы."""
    out = [lc_burst(cc, lc9, False, fam) for _ in range(headers)]
    emb = _emb_lc_encode(lc9)
    k = 0
    for _ in range(superframes):
        for idx in range(6):
            fr = [ambe_frames[(k + j) % len(ambe_frames)] for j in range(3)]
            k += 3
            if 1 <= idx <= 4:
                lcss = (1, 3, 3, 2)[idx - 1]
                out.append(voice_burst(fr, idx, cc, emb[32 * (idx - 1): 32 * idx], lcss, 0, fam))
            else:
                out.append(voice_burst(fr, idx, cc, None, 0, 0, fam))
    out += [lc_burst(cc, lc9, True, fam) for _ in range(terms)]
    return out


def bs_stream(slot1, slot2):
    """Чередует пакеты слотов 1/2 в поток BS с CACH. Возвращает биты."""
    n = max(len(slot1), len(slot2))
    bits = []
    for i in range(n):
        for s, lst in ((1, slot1), (2, slot2)):
            bits += _cach(s) + list(lst[i] if i < len(lst) else idle_burst(1))
    return bits


def modulate(bits, fs=FS, offset_hz=0.0, ppm=0.0):
    """Биты -> мгновенная частота (Гц) 4FSK с RRC-формированием."""
    b = np.asarray(bits, dtype=np.int64)
    dib = b[0::2] * 2 + b[1::2]
    lev = _LEVEL_OF_DIBIT[dib] * (DEV3 / 3.0)
    up = np.zeros(len(lev) * SPS)
    up[::SPS] = lev
    h = _rrc()
    g = np.convolve(h, _H_RX)
    h = h / g.max()
    f = np.convolve(up, h)[(len(h) - 1) // 2:][:len(up)]
    if ppm:
        t = np.arange(int(len(f) / (1 + ppm * 1e-6))) * (1 + ppm * 1e-6)
        f = np.interp(t, np.arange(len(f)), f)
    if fs != FS:
        f = resample_poly(f, fs, FS)
    return (f + offset_hz).astype(np.float32)


def _tone_frames(b0, n=6, b2=17):
    """Набор AMBE-кадров «тон/гласная» с небольшим вибрато по высоте."""
    return [ambe_encode(ambe_params(b0 + (i % 3) - 1, 0, b2, 0, 0)) for i in range(n)]


def test_signal(fs=FS):
    """Синтетический эфир DMR (BS, CC=1), ~11 с, зацикливаемый.

    Слот 1: групповой вызов 2501234 → TG 91; пауза; слот 2: индивидуальный 2507777 → 2501234.
    В свободном слоте — Idle-пакеты, между вызовами — немодулированная несущая.
    """
    cc = 1
    idle = [idle_burst(cc) for _ in range(200)]
    pause = np.zeros(int(1.0 * FS), np.float32)
    parts = []
    for slot, lc, b0 in ((1, _make_lc(2501234, 91, True), 40), (2, _make_lc(2507777, 2501234, False), 25)):
        call = voice_call(cc, lc, _tone_frames(b0), superframes=10)
        lead = idle[:4]
        hang = idle[:8]
        busy = lead + call + hang
        free = idle[:len(busy)]
        s1, s2 = (busy, free) if slot == 1 else (free, busy)
        parts.append(modulate(bs_stream(s1, s2)))
        parts.append(pause)
    f = np.concatenate(parts)
    if fs != FS:
        f = resample_poly(f, fs, FS).astype(np.float32)
    return f.astype(np.float32)
