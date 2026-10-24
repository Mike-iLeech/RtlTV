"""RDS (Radio Data System, IEC 62106): декодер с комплексного сигнала MPX ЧМ-вещания и кодер для тестов.

Поднесущая 57 кГц (3 × пилот), BPSK, 1187,5 бит/с, двухфазное (Манчестер) кодирование с
дифференциальным предкодированием. Группа = 4 блока по 26 бит (16 данных + 10 проверочных со
смещением A/B/C/C'/D). Декодируются PI, PS (название станции), RT (радиотекст), PTY, TP/TA, часы (CT).

Демодуляция векторная (numpy): перенос 57 кГц в ноль (от фазы пилота, а без пилота — свободным
генератором), ФНЧ и прореживание до 24 кГц, фаза несущей по квадрату сигнала, согласованный фильтр
двухфазного символа, тактовая синхронизация по максимуму энергии. Побитовая часть (синдромы, группы)
— на Python: всего 1187 бит/с.
"""
import math

import numpy as np
from scipy import signal

BITRATE = 1187.5
POLY = 0x5B9                       # x^10 + x^8 + x^7 + x^5 + x^4 + x^3 + 1
OFFSETS = {"A": 0x0FC, "B": 0x198, "C": 0x168, "Cp": 0x350, "D": 0x1B4}
OFF2IDX = {0x0FC: 0, 0x198: 1, 0x168: 2, 0x350: 2, 0x1B4: 3}
IDX2OFF = {0: (0x0FC,), 1: (0x198,), 2: (0x168, 0x350), 3: (0x1B4,)}

PTY_NAMES = ["—", "Новости", "Текущие события", "Информация", "Спорт", "Образование", "Драма", "Культура",
             "Наука", "Разное", "Поп-музыка", "Рок-музыка", "Лёгкая музыка", "Лёгкая классика",
             "Классика", "Другая музыка", "Погода", "Финансы", "Детям", "Общество", "Религия", "Звонки в эфир",
             "Путешествия", "Досуг", "Джаз", "Кантри", "Национальная музыка", "Ретро", "Фолк",
             "Документальное", "Тест тревоги", "Тревога"]

_CRC = None


def crc_table():
    """Проверочное слово (без смещения) для всех 65536 значений данных блока."""
    global _CRC
    if _CRC is None:
        d = np.arange(65536, dtype=np.int64)
        reg = d << 10
        for i in range(25, 9, -1):
            hit = (reg >> i) & 1
            reg ^= hit * (POLY << (i - 10))
        _CRC = (reg & 0x3FF).astype(np.int64)
    return _CRC


def checkword(data, off):
    return int(crc_table()[data & 0xFFFF]) ^ OFFSETS[off]


def rds_char(b):
    """Символ RDS: ASCII как есть; остальное — по кодовой странице cp1251 (так шлют многие станции в России)."""
    if 0x20 <= b < 0x7F:
        return chr(b)
    if b >= 0xC0:
        try:
            return bytes([b]).decode("cp1251")
        except UnicodeDecodeError:
            return "?"
    return " " if b in (0, 0x0D) else "?"


class RdsDecoder:
    def __init__(self, fs=240000):
        from .radio import Decimator, Fir, Nco, Sos
        self.fs = fs
        self.D = int(round(fs / 24000))
        self.rate = fs / self.D
        self.T = self.rate / BITRATE                     # отсчётов на бит
        self.nco = Nco(57000.0, fs)
        self.dec = Decimator(self.D, 2600.0 / (self.rate / 2), 32)
        self.sq_lp = Sos(signal.butter(2, 12.0, fs=self.rate, output="sos"))
        half = int(round(self.T / 2))
        self.mf = Fir(np.concatenate((np.ones(half), -np.ones(half))) / (2 * half))
        self._Fir = Fir
        self.reset()

    def reset(self):
        self.last_ang = 0.0
        self.ybuf = np.zeros(0)
        self.abs_n = 0                  # абсолютный номер следующего отсчёта на 24 кГц
        self.next_abs = 0.0             # абсолютная позиция следующего отсчёта бита
        self.clk_ph = None
        self.clk_rate = 0.0
        self.clk_mag = 0.0
        self.prev_raw = 0
        self.reg = 0
        self.nbits = 0
        self.synced = False
        self.cand = None                # (номер бита, индекс блока) последнего найденного блока при поиске
        self.left = 0
        self.expect = 0
        self.errs = []
        self.group = [None] * 4
        self.pi = None
        self.pty = None
        self.tp = self.ta = False
        self.music = None
        self.ps_buf = [" "] * 8
        self.ps_seen = [False] * 4
        self.ps = ""
        self.rt_buf = [" "] * 64
        self.rt_seen = [False] * 16
        self.rt_ab = None
        self.rt = ""
        self.ct = None
        self.groups_ok = 0
        self.blocks_ok = 0
        self.blocks_bad = 0
        self.changed = True

    # ---------- демодуляция ----------
    def process(self, mpx, c57=None):
        """mpx — вещественный MPX (1.0 = 75 кГц) на частоте fs; c57 — exp(j·фаза 57 кГц) от пилота или None."""
        if mpx.size == 0:
            return
        if c57 is None:
            c57 = self.nco(mpx.size)
        else:
            self.nco.ph = (self.nco.ph + self.nco.w * mpx.size) % (2 * math.pi)
        bb = self.dec(mpx * np.conj(c57))
        if bb.size == 0:
            return
        # фаза несущей: угол квадрата сигнала (BPSK) после узкого ФНЧ, без разрывов между блоками
        z2 = self.sq_lp(bb * bb)
        ang = np.unwrap(np.concatenate(([self.last_ang], np.angle(z2))))[1:]
        self.last_ang = float(ang[-1])
        r = np.real(bb * np.exp(-0.5j * ang))
        y = self.mf(r)
        self.ybuf = np.concatenate((self.ybuf, y))
        self._timing(y)

    def _timing(self, ynew):
        """Тактовая синхронизация: у y² есть спектральная линия на частоте битов; её фаза (сглаженная по
        времени) даёт момент максимума отклика согласованного фильтра. Отсчёты берутся строго по одному
        на бит, следующий — в пределах ±T/2 от ожидаемого (без пропусков и повторов)."""
        T = self.T
        n = self.abs_n + np.arange(ynew.size)
        ph = np.exp(-2j * np.pi * np.fmod(n, T) / T)
        c = complex(np.sum(ynew * ynew * ph))
        self.abs_n += ynew.size
        # петля 2-го порядка по фазе тактовой линии: следит и за уходом частоты битов (у станций
        # встречается до 3·10⁻⁴ — без слежения за частотой отсчёты «уезжают» за 1–2 с)
        mag = abs(c)
        self.clk_mag = mag if self.clk_mag == 0 else 0.9 * self.clk_mag + 0.1 * mag
        if mag > 0:
            if self.clk_ph is None:
                self.clk_ph = float(np.angle(c))
            else:
                pred = self.clk_ph + self.clk_rate
                e = (float(np.angle(c)) - pred + np.pi) % (2 * np.pi) - np.pi
                w = min(1.0, mag / (self.clk_mag + 1e-20))
                self.clk_ph = pred + 0.35 * w * e
                self.clk_rate += 0.04 * w * e
        n0 = (-(self.clk_ph or 0.0) / (2 * np.pi) * T) % T
        yb = self.ybuf
        base = self.abs_n - yb.size              # абсолютный номер yb[0]
        out = []
        while True:
            want = self.next_abs
            # ближайшая к ожидаемой точка сетки n0 + kT
            k = round((want - n0) / T)
            p = n0 + k * T
            if abs(p - want) > T / 2:
                p = want
            i = p - base
            if i < 0:
                i = 0.0
                p = float(base)
            if i + 1 >= yb.size:
                break
            i0 = int(i)
            fr = i - i0
            out.append(yb[i0] * (1 - fr) + yb[i0 + 1] * fr)
            self.next_abs = p + T
        keep = max(4, int(yb.size - max(0.0, self.next_abs - base - 4)))
        keep = min(keep, yb.size)
        self.ybuf = yb[yb.size - keep:]
        raw = (np.array(out) > 0).astype(np.int64)
        for b in raw:
            bit = int(b) ^ self.prev_raw     # дифференциальное декодирование
            self.prev_raw = int(b)
            self._bit(bit)

    # ---------- блоки и группы ----------
    def _bit(self, bit):
        self.reg = ((self.reg << 1) | bit) & 0x3FFFFFF
        self.nbits += 1
        crc = crc_table()
        if not self.synced:
            off = int(crc[self.reg >> 10]) ^ (self.reg & 0x3FF)
            idx = OFF2IDX.get(off)
            if idx is None:
                return
            if self.cand is not None:
                n0, i0 = self.cand
                dist = self.nbits - n0
                if dist % 26 == 0 and 0 < dist <= 26 * 4 and (i0 + dist // 26) % 4 == idx:
                    self.synced = True
                    self.errs = []
                    self.group = [None] * 4
                    self._store(idx, self.reg >> 10, off)
                    self.expect = (idx + 1) % 4
                    self.left = 26
                    return
            self.cand = (self.nbits, idx)
            return
        self.left -= 1
        if self.left > 0:
            return
        self.left = 26
        idx = self.expect
        self.expect = (idx + 1) % 4
        w = self.reg
        ok_off = None
        off = int(crc[w >> 10]) ^ (w & 0x3FF)
        if off in IDX2OFF[idx]:
            ok_off = off
        else:
            # исправление одиночной ошибки
            for b in range(26):
                w2 = w ^ (1 << b)
                off2 = int(crc[w2 >> 10]) ^ (w2 & 0x3FF)
                if off2 in IDX2OFF[idx]:
                    w, ok_off = w2, off2
                    break
        self.errs.append(ok_off is None)
        if len(self.errs) > 50:
            self.errs.pop(0)
        if ok_off is None:
            self.blocks_bad += 1
            self.group[idx] = None
            if sum(self.errs) > 20:
                self.synced = False
                self.cand = None
        else:
            self._store(idx, w >> 10, ok_off)
        if idx == 3:
            self._group()
            self.group = [None] * 4

    def _store(self, idx, data, off):
        self.blocks_ok += 1
        self.group[idx] = (data, off)

    def _group(self):
        g = self.group
        a, b, c, d = g
        if a is not None:
            self._set("pi", a[0])
        elif c is not None and c[1] == OFFSETS["Cp"]:
            self._set("pi", c[0])
        if b is None:
            return
        bv = b[0]
        gtype, ver = bv >> 12, (bv >> 11) & 1
        self._set("tp", bool((bv >> 10) & 1))
        self._set("pty", (bv >> 5) & 31)
        self.groups_ok += 1
        if gtype == 0 and d is not None:
            self._set("ta", bool((bv >> 4) & 1))
            self._set("music", bool((bv >> 3) & 1))
            addr = bv & 3
            dv = d[0]
            self.ps_buf[2 * addr] = rds_char(dv >> 8)
            self.ps_buf[2 * addr + 1] = rds_char(dv & 0xFF)
            self.ps_seen[addr] = True
            if all(self.ps_seen):
                self._set("ps", "".join(self.ps_buf))
                self.ps_seen = [False] * 4
        elif gtype == 2:
            ab = (bv >> 4) & 1
            if self.rt_ab is not None and ab != self.rt_ab:
                self.rt_buf = [" "] * 64
                self.rt_seen = [False] * 16
            self.rt_ab = ab
            addr = bv & 15
            if ver == 0:
                chars = []
                for blk in (c, d):
                    if blk is None:
                        return
                    chars += [blk[0] >> 8, blk[0] & 0xFF]
                pos = addr * 4
            else:
                if d is None:
                    return
                chars = [d[0] >> 8, d[0] & 0xFF]
                pos = addr * 2
            for i, ch in enumerate(chars):
                if pos + i < 64:
                    self.rt_buf[pos + i] = "\r" if ch == 0x0D else rds_char(ch)
            self.rt_seen[addr] = True
            seg = 4 if ver == 0 else 2
            txt = "".join(self.rt_buf[:16 * seg])
            end = txt.find("\r")
            n_seg = end // seg + 1 if end >= 0 else 16
            if all(self.rt_seen[:n_seg]):
                self._set("rt", (txt[:end] if end >= 0 else txt).strip())
        elif gtype == 4 and ver == 0 and c is not None and d is not None:
            cv, dv = c[0], d[0]
            mjd = ((bv & 3) << 15) | (cv >> 1)
            hour = ((cv & 1) << 4) | (dv >> 12)
            minute = (dv >> 6) & 0x3F
            off = (dv & 0x1F) * 30 * (-1 if (dv >> 5) & 1 else 1)
            if hour < 24 and minute < 60 and mjd > 50000:
                y_ = int((mjd - 15078.2) / 365.25)
                m_ = int((mjd - 14956.1 - int(y_ * 365.25)) / 30.6001)
                day = mjd - 14956 - int(y_ * 365.25) - int(m_ * 30.6001)
                k = 1 if m_ in (14, 15) else 0
                year, month = 1900 + y_ + k, m_ - 1 - k * 12
                t = hour * 60 + minute + off
                self._set("ct", (year, month, day, (t // 60) % 24, t % 60))

    def _set(self, name, val):
        if getattr(self, name) != val:
            setattr(self, name, val)
            self.changed = True

    # ---------- для интерфейса ----------
    def state(self):
        txt = "".join(self.rt_buf)
        end = txt.find("\r")
        return {
            "synced": self.synced, "pi": self.pi, "ps": self.ps or "".join(self.ps_buf).strip(),
            "ps_done": bool(self.ps), "rt": self.rt or (txt[:end] if end >= 0 else txt).strip(),
            "pty": self.pty, "pty_name": PTY_NAMES[self.pty] if self.pty is not None else "",
            "tp": self.tp, "ta": self.ta, "music": self.music, "ct": self.ct,
            "groups": self.groups_ok, "ok": self.blocks_ok, "bad": self.blocks_bad,
        }


# ======================================================================
#  Кодер (для тестов и тестового эфира)
# ======================================================================
def _chars(s, n):
    b = s.encode("cp1251", "replace")[:n].ljust(n, b" ")
    return list(b)


def make_groups(pi=0x7201, ps="SDR4NOOB", rt="Тестовый эфир SDR4NOOB", pty=10, ct=None):
    """Цикл групп: 4 × 0A (PS) + 2A (RT) + при желании 4A (часы). Каждая группа — [(данные, смещение)] × 4."""
    groups = []
    ps_b = _chars(ps, 8)
    for addr in range(4):
        b = (0 << 12) | (0 << 11) | (1 << 10) | (pty << 5) | (0 << 4) | (1 << 3) | (1 << 2) | addr
        groups.append([(pi, "A"), (b, "B"), (0xE0CD, "C"), ((ps_b[2 * addr] << 8) | ps_b[2 * addr + 1], "D")])
    rt_s = rt[:64]
    if len(rt_s) < 64:
        rt_s += "\r"
    n_seg = (len(rt_s) + 3) // 4
    rt_b = _chars(rt_s, n_seg * 4)
    for addr in range(n_seg):
        b = (2 << 12) | (0 << 11) | (1 << 10) | (pty << 5) | (0 << 4) | addr
        c = (rt_b[4 * addr] << 8) | rt_b[4 * addr + 1]
        d = (rt_b[4 * addr + 2] << 8) | rt_b[4 * addr + 3]
        groups.append([(pi, "A"), (b, "B"), (c, "C"), (d, "D")])
    if ct is not None:
        year, month, day, hour, minute = ct
        k = 1 if month <= 2 else 0
        mjd = 14956 + day + int((year - 1900 - k) * 365.25) + int((month + 1 + k * 12) * 30.6001)
        b = (4 << 12) | (1 << 10) | (pty << 5) | ((mjd >> 15) & 3)
        c = ((mjd & 0x7FFF) << 1) | (hour >> 4)
        d = ((hour & 15) << 12) | (minute << 6)
        groups.append([(pi, "A"), (b, "B"), (c, "C"), (d, "D")])
    return groups


def group_bits(groups):
    out = []
    for g in groups:
        for data, off in g:
            word = (data << 10) | checkword(data, off)
            out += [(word >> (25 - i)) & 1 for i in range(26)]
    return np.array(out, np.uint8)


def diff_encode(bits, prev=0):
    out = np.empty(bits.size, np.uint8)
    for i, b in enumerate(bits):
        prev ^= int(b)
        out[i] = prev
    return out


class RdsEncoder:
    """Поток символов RDS: value(t) — двухфазный сигнал ±1 в моменты времени t (секунды, массив).
    Цикл групп повторяется; длина цикла подогнана так, чтобы дифференциальный код сходился на стыке."""

    def __init__(self, groups):
        bits = group_bits(groups)
        d = diff_encode(bits)
        if d[-1] != 0:
            d = np.concatenate((d, diff_encode(bits, int(d[-1]))))
        self.sym = d.astype(np.float32) * 2 - 1

    def value(self, t, rate=BITRATE):
        x = t * rate
        k = np.floor(x).astype(np.int64)
        half = (x - k) >= 0.5
        s = self.sym[k % self.sym.size]
        return np.where(half, -s, s)


def rds_baseband(groups, fs, seconds, shaped=True):
    """Готовый сигнал RDS (до умножения на 57 кГц) длительностью seconds — для тестов."""
    t = np.arange(int(fs * seconds)) / fs
    x = RdsEncoder(groups).value(t)
    if shaped:
        x = signal.filtfilt(signal.firwin(255, 2400, fs=fs), [1.0], x)
    return x
