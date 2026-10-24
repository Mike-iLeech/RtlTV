"""APRS: AFSK Bell 202 (1200 бод, 1200/2200 Гц) → AX.25 UI-кадры → разбор пакетов APRS.

Демодулятор (по мотивам Dire Wolf):
  * звук NFM 48 кГц → несколько «входных профилей» (разные полосовые фильтры и окна корреляторов);
  * в каждом профиле — квадратурные корреляторы на 1200 и 2200 Гц, амплитуды тонов,
    по желанию — АРУ каждого тона по пикам/провалам (выравнивает пред-/деакцентирование);
  * на каждый профиль несколько «срезателей» с разным весом тона паузы (space gain);
  * у каждого срезателя своя цифровая ФАПЧ тактов по переходам (не зависит от точной частоты
    дискретизации звуковой карты), NRZI, снятие бит-стаффинга, поиск флагов HDLC, проверка FCS;
  * кадр с плохой FCS можно спасти исправлением одного или двух соседних битов (по синдрому CRC)
    — только если адреса AX.25 и поле управления выглядят правильно;
  * одинаковые кадры от разных срезателей сливаются в один (с числом «услышавших» декодеров).

Разбор APRS: позиции (несжатые, сжатые, Mic-E), объекты и элементы, статус, сообщения,
телеметрия, погода, NMEA, сторонний трафик; символ, комментарий, высота, курс/скорость,
путь (digipeater, МКС RS0ISS/ARISS).

Кодер AFSK + AX.25 — для тестов и тестового эфира.
"""
import math
import re

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy import signal

FS = 48000
BAUD = 1200
MARK = 1200.0
SPACE = 2200.0

# позывные и псевдонимы МКС (RS0ISS — digipeater на борту, ARISS/APRSAT — псевдонимы пути)
ISS_CALLS = {"RS0ISS", "NA1SS", "OR4ISS", "DP0ISS", "IR0ISS", "ARISS", "APRSAT"}

# ======================================================================= CRC / FCS

_CRC_TAB = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0x8408 if _c & 1 else _c >> 1
    _CRC_TAB.append(_c)
GOOD_RESIDUE = 0xF0B8          # остаток CRC по кадру вместе с правильной FCS
MAX_FRAME = 340                # байт: 10 адресов + упр. + PID + 256 инфо + FCS — с запасом
MIN_FRAME = 18                 # 2 адреса + упр. + PID + FCS


def crc_run(data, crc=0xFFFF):
    tab = _CRC_TAB
    for b in data:
        crc = (crc >> 8) ^ tab[(crc ^ b) & 0xFF]
    return crc


def fcs(data):
    """FCS кадра AX.25 (CRC-16-CCITT, X.25): передаётся младшим байтом вперёд."""
    return crc_run(data) ^ 0xFFFF


def check_fcs(frame):
    return len(frame) >= 3 and crc_run(frame) == GOOD_RESIDUE


def _syndromes():
    s = [0] * (MAX_FRAME * 8 + 2)
    v = 1
    for k in range(1, len(s)):
        v = (v >> 1) ^ 0x8408 if v & 1 else v >> 1
        s[k] = v
    one, two = {}, {}
    for k in range(1, len(s)):
        one.setdefault(s[k], k)
        if k >= 2:
            two.setdefault(s[k] ^ s[k - 1], k)
    return one, two


_SYN1, _SYN2 = _syndromes()


def try_fix(frame):
    """Кадр с FCS, где CRC не сошёлся: (исправленный кадр, число исправленных битов) или (None, 0).

    CRC линеен: инверсия бита i меняет остаток на S[n-i], поэтому позицию ищем по таблице.
    """
    n = len(frame) * 8
    syn = crc_run(frame) ^ GOOD_RESIDUE
    k = _SYN1.get(syn)
    if k is not None and k <= n:
        flips = [n - k]
    else:
        k = _SYN2.get(syn)
        if k is None or k > n:
            return None, 0
        flips = [n - k, n - k + 1]
    fr = bytearray(frame)
    for i in flips:
        fr[i // 8] ^= 1 << (i % 8)
    if not check_fcs(fr):
        return None, 0
    return bytes(fr), len(flips)


# ======================================================================= AX.25

def _addr_decode(b7):
    """7 байт адреса → (позывной-SSID, бит H/C, последний ли) или None, если это не позывной."""
    chars = []
    for c in b7[:6]:
        if c & 1:
            return None
        chars.append(c >> 1)
    s = bytes(chars).decode("ascii", "replace").rstrip(" ")
    if not s or not all(("A" <= ch <= "Z") or ("0" <= ch <= "9") for ch in s):
        return None
    ssid = (b7[6] >> 1) & 0x0F
    call = s + (f"-{ssid}" if ssid else "")
    return call, bool(b7[6] & 0x80), bool(b7[6] & 1)


def split_call(call):
    c = call.rstrip("*").upper()
    base, _, ss = c.partition("-")
    try:
        ssid = int(ss) if ss else 0
    except ValueError:
        ssid = 0
    return base, max(0, min(15, ssid))


def _addr_encode(call, last=False, hbit=False):
    base, ssid = split_call(call)
    base = (base + "      ")[:6]
    out = bytearray((ord(ch) << 1) & 0xFE for ch in base)
    out.append(0x60 | (ssid << 1) | (0x80 if hbit else 0) | (1 if last else 0))
    return bytes(out)


def decode_ax25(data):
    """Кадр без FCS → dict(src, dest, path=[(позывной, использован)], control, pid, info) или None."""
    addrs = []
    i = 0
    n = len(data)
    while True:
        if i + 7 > n:
            return None
        a = _addr_decode(data[i:i + 7])
        if a is None:
            return None
        addrs.append(a)
        i += 7
        if a[2]:
            break
        if len(addrs) >= 10:
            return None
    if len(addrs) < 2 or i >= n:
        return None
    control = data[i]
    pid = None
    info = b""
    if control & 0xEF == 0x03:                 # UI (с битом P/F или без)
        if i + 1 >= n:
            return None
        pid = data[i + 1]
        info = bytes(data[i + 2:])
    return {"dest": addrs[0][0], "src": addrs[1][0], "path": [(c, h) for c, h, _ in addrs[2:]],
            "control": control, "pid": pid, "info": info}


def is_aprs_frame(fr):
    return fr is not None and fr["control"] & 0xEF == 0x03 and fr["pid"] == 0xF0


def make_frame(src, dest, path=(), info=b""):
    """UI-кадр AX.25 без FCS. path — позывные, «*» в конце = уже повторён (бит H)."""
    if isinstance(info, str):
        info = info.encode("utf-8")
    path = [p for p in path if p]
    out = bytearray(_addr_encode(dest, last=False, hbit=True))
    out += _addr_encode(src, last=not path)
    for k, p in enumerate(path):
        out += _addr_encode(p, last=k == len(path) - 1, hbit=p.endswith("*"))
    out += bytes((0x03, 0xF0))
    out += info
    return bytes(out)


def add_fcs(frame):
    f = fcs(frame)
    return bytes(frame) + bytes((f & 0xFF, f >> 8))


def parse_tnc2(text):
    """«SRC>DEST,PATH:info» → dict как у decode_ax25 (info — байты) или None."""
    if isinstance(text, bytes):
        text = text.decode("latin-1")
    head, sep, info = text.partition(":")
    if not sep or ">" not in head:
        return None
    src, _, rest = head.partition(">")
    parts = rest.split(",")
    path = [(p.rstrip("*"), p.endswith("*")) for p in parts[1:] if p]
    # в TNC2 «*» стоит только у последнего повторившего — все до него тоже использованы
    last_used = max((k for k, (_c, h) in enumerate(path) if h), default=-1)
    path = [(c, k <= last_used) for k, (c, _h) in enumerate(path)]
    try:
        info_b = info.encode("latin-1")
    except UnicodeEncodeError:
        info_b = info.encode("utf-8")
    return {"src": src.strip().upper(), "dest": parts[0].strip().upper(), "path": path,
            "control": 0x03, "pid": 0xF0, "info": info_b}


def tnc2_frame(text):
    """TNC2-строка → байты кадра без FCS (для кодера)."""
    fr = parse_tnc2(text)
    path = [c + ("*" if h else "") for c, h in fr["path"]]
    return make_frame(fr["src"], fr["dest"], path, fr["info"])


def smart_text(b):
    """Байты комментария: UTF-8, иначе cp1251 (так шлют многие станции в России), иначе latin-1."""
    if isinstance(b, str):
        b = b.encode("latin-1", "replace")
    for enc in ("utf-8", "cp1251"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("latin-1")


def tnc2(fr):
    """Кадр → строка TNC2 (звёздочка у последнего повторившего digipeater)."""
    used = [k for k, (_c, h) in enumerate(fr["path"]) if h]
    last = used[-1] if used else -1
    path = "".join("," + c + ("*" if k == last else "") for k, (c, _h) in enumerate(fr["path"]))
    info = fr["info"]
    txt = smart_text(info)
    txt = "".join(ch if ch >= " " or ch in "\t" else f"<0x{ord(ch):02x}>" for ch in txt)
    return f"{fr['src']}>{fr['dest']}{path}:{txt}"


# ======================================================================= кодер AFSK

def hdlc_bits(frame_fcs, preamble=40, postamble=4):
    """Байты кадра с FCS → биты HDLC: флаги, данные младшим битом вперёд с бит-стаффингом, флаги."""
    flag = [0, 1, 1, 1, 1, 1, 1, 0]
    out = flag * preamble
    ones = 0
    for byte in frame_fcs:
        for k in range(8):
            b = (byte >> k) & 1
            out.append(b)
            if b:
                ones += 1
                if ones == 5:
                    out.append(0)
                    ones = 0
            else:
                ones = 0
    out += flag * postamble
    return np.array(out, np.uint8)


def nrzi(bits, level=1):
    """NRZI: 0 — смена тона, 1 — тон тот же. Возвращает уровни (1 — метка 1200 Гц)."""
    flips = (np.asarray(bits) == 0).astype(np.int64)
    lv = (level + np.cumsum(flips)) % 2
    return lv.astype(np.uint8)


def afsk(levels, fs=FS, baud=BAUD, mark=MARK, space=SPACE, amp=0.5, space_gain=1.0, shift=0.0, phase=0.0):
    """Уровни NRZI → звук AFSK с непрерывной фазой.

    fs может быть дробной: так моделируется ошибка тактовой передатчика (звук «как будто» fs).
    space_gain — относительная громкость тона 2200 Гц (пред-/деакцентирование тракта).
    """
    levels = np.asarray(levels, np.uint8)
    n = int(math.ceil(levels.size * fs / baud))
    k = np.minimum((np.arange(n) * (baud / fs)).astype(np.int64), levels.size - 1)
    lv = levels[k].astype(bool)
    f = np.where(lv, mark, space) + shift
    ph = phase + 2 * np.pi * np.cumsum(f) / fs
    a = np.where(lv, 1.0, space_gain) * amp
    return (a * np.sin(ph)).astype(np.float32)


def encode_frame_audio(frame, fs=FS, preamble=40, postamble=4, **kw):
    """Кадр (без FCS) или TNC2-строка → звук одного пакета."""
    if isinstance(frame, str):
        frame = tnc2_frame(frame)
    return afsk(nrzi(hdlc_bits(add_fcs(frame), preamble, postamble)), fs=fs, **kw)


def encode_packets(frames, fs=FS, gap=0.5, lead=0.3, **kw):
    """Несколько пакетов подряд с паузами (секунды) → звук float32."""
    parts = [np.zeros(int(lead * fs), np.float32)]
    for fr in frames:
        parts.append(encode_frame_audio(fr, fs=fs, **kw))
        parts.append(np.zeros(int(gap * fs), np.float32))
    return np.concatenate(parts)


# ======================================================================= демодулятор

class _FrontEnd:
    """Корреляторы метки/паузы одного профиля: звук → амплитуды тонов на сниженной частоте."""

    def __init__(self, fs, dec, baud, mark, space, band, win_bits, window, agc_bits):
        self.fs, self.dec = fs, dec
        self.pre = signal.firwin(81, band, pass_zero=False, fs=fs) if band else None
        self.pre_tail = np.zeros(0 if self.pre is None else self.pre.size - 1)
        L = max(3, int(round(win_bits * fs / baud)))
        w = np.ones(L) if window == "box" else np.hanning(L + 2)[1:-1]
        self.lp = (w / w.sum()).astype(np.complex128)
        self.tail = np.zeros((2, L - 1), np.complex128)
        self.w = (2 * np.pi * mark / fs, 2 * np.pi * space / fs)
        self.ph = [0.0, 0.0]
        self.n = 0
        self.agc = int(round(agc_bits * fs / dec / baud)) if agc_bits else 0
        self.hist = np.zeros((2, max(0, self.agc - 1)))

    def process(self, x):
        if self.pre is not None:
            buf = np.concatenate((self.pre_tail, x))
            self.pre_tail = buf[-(self.pre.size - 1):]
            x = np.convolve(buf, self.pre, "valid")
        n = x.size
        start = (-self.n) % self.dec
        self.n += n
        out = []
        ar = np.arange(n)
        for k in range(2):
            ph = self.ph[k] + self.w[k] * ar
            self.ph[k] = float((self.ph[k] + self.w[k] * n) % (2 * np.pi))
            y = np.concatenate((self.tail[k], x * np.exp(-1j * ph)))
            self.tail[k] = y[-(self.tail.shape[1]):]
            out.append(np.abs(np.convolve(y, self.lp, "valid")[start::self.dec]))
        m, s = out
        if self.agc > 1 and m.size:
            res = []
            for k, v in enumerate((m, s)):
                buf = np.concatenate((self.hist[k], v))
                self.hist[k] = buf[-(self.agc - 1):]
                win = sliding_window_view(buf, self.agc)
                hi, lo = win.max(axis=1), win.min(axis=1)
                res.append((v - lo) / (hi - lo + 1e-9))
            m, s = res
        return m, s


class _Slicer:
    """Срезатель + ФАПЧ тактов + NRZI + HDLC. Кадры (биты, время) копятся в self.frames."""

    MAXBITS = MAX_FRAME * 8 + 8

    def __init__(self, T, gain):
        self.T = T
        self.gain = gain
        self.next_t = T / 2
        self.level = False
        self.prev_level = False
        self.last_d = 0.0
        self.ones = 0
        self.buf = []
        self.hunting = True
        self.frames = []

    def busy(self):
        return not self.hunting and len(self.buf) >= 24

    def process(self, d, t0):
        if d.size == 0:
            return
        full = np.empty(d.size + 1)
        full[0] = self.last_d
        full[1:] = d
        pos = full > 0
        idx = np.flatnonzero(pos[1:] != pos[:-1])
        a = full[idx]
        b = full[idx + 1]
        taus = (t0 - 1 + idx + a / (a - b)).tolist()
        self.last_d = float(d[-1])
        T = self.T
        half = 0.5 * T
        nt = self.next_t
        lvl = self.level
        run = self._run
        for tau in taus:
            if tau > nt:
                k = int(math.ceil((tau - nt) / T))
                run(lvl, k, nt)
                nt += k * T
            lvl = not lvl
            nt -= (0.5 if self.hunting else 0.26) * ((nt - tau) - half)
        t_end = t0 + d.size - 1
        if nt <= t_end:
            k = int((t_end - nt) // T) + 1
            run(lvl, k, nt)
            nt += k * T
        self.next_t = nt
        self.level = lvl

    def _run(self, lvl, k, t):
        """k тактовых отсчётов одного уровня: первый бит NRZI по смене уровня, остальные — единицы."""
        same = lvl == self.prev_level
        self.prev_level = lvl
        if same:
            self.ones += 1
            if self.ones >= 7:
                self._abort()
            else:
                self.buf.append(1)
        else:
            o = self.ones
            self.ones = 0
            if o == 6:
                self._flag(t)
            elif o != 5 and o < 7:          # 5 единиц + 0 — вставленный бит, его выбрасываем
                self.buf.append(0)
                if len(self.buf) > self.MAXBITS:
                    self._abort()
        k -= 1
        if k > 0:
            if self.ones + k >= 7:
                if self.ones < 7:
                    self._abort()
                self.ones += k
            else:
                self.buf.extend((1,) * k)
                self.ones += k

    def _abort(self):
        self.buf = []
        self.hunting = True
        self.ones = max(self.ones, 7)

    def _flag(self, t):
        if not self.hunting:
            n = len(self.buf) - 7
            if n >= MIN_FRAME * 8 and n % 8 == 0:
                self.frames.append((self.buf[:n], t))
        self.buf = []
        self.hunting = False


# профили: (полоса предфильтра или None, окно коррелятора в битах, форма окна, АРУ в битах, веса паузы)
PROFILES = (
    (None, 1.0, "box", 12, (0.55, 0.75, 1.0, 1.33, 1.8)),
    ((900, 2500), 1.6, "hann", 12, (0.7, 1.0, 1.43)),
    ((500, 2900), 1.0, "box", 0, (0.32, 0.56, 1.0, 1.8, 3.2)),
)


class AfskDemodulator:
    """Потоковый приёмник APRS: process(звук) → список принятых кадров.

    Кадр: dict(data=байты без FCS, t=время конца кадра, с; fixed=исправленных битов;
               decoders=сколько срезателей его приняли).
    """

    def __init__(self, fs=FS, baud=BAUD, mark=MARK, space=SPACE, profiles=PROFILES, fix_bits=True):
        self.fs = float(fs)
        self.baud = baud
        self.dec = max(1, int(round(fs / (baud * 8))))
        self.fsd = self.fs / self.dec
        T = self.fsd / baud
        self.fronts = []
        self.slicers = []
        for band, win, shape, agc, gains in profiles:
            fe = _FrontEnd(self.fs, self.dec, baud, mark, space, band, win, shape, agc)
            self.fronts.append((fe, [_Slicer(T, g) for g in gains]))
            self.slicers += self.fronts[-1][1]
        self.fix_bits = fix_bits
        self.n_out = 0            # отсчётов на сниженной частоте
        self.recent = {}
        self.frames_total = 0
        self.crc_errors = 0
        self.fixed_total = 0
        self.level = 0.0

    @property
    def n_decoders(self):
        return len(self.slicers)

    def busy(self):
        """«Несущая данных»: хотя бы один срезатель сейчас внутри кадра."""
        return any(s.busy() for s in self.slicers)

    def reset(self):
        self.__init__(self.fs, self.baud)

    def process(self, x):
        x = np.asarray(x, np.float64).ravel()
        if x.size == 0:
            return []
        self.level = float(np.sqrt(np.mean(x * x)))
        t0 = self.n_out
        nout = None
        for fe, sl in self.fronts:
            m, s = fe.process(x)
            nout = m.size
            for z in sl:
                z.process(m - z.gain * s, t0)
        self.n_out += nout or 0
        found = {}
        for z in self.slicers:
            if not z.frames:
                continue
            for bits, t in z.frames:
                raw = np.packbits(np.array(bits, np.uint8), bitorder="little").tobytes()
                fixed = 0
                if not check_fcs(raw):
                    if not self.fix_bits:
                        self.crc_errors += 1
                        continue
                    raw, fixed = try_fix(raw)
                    if raw is None:
                        self.crc_errors += 1
                        continue
                data = raw[:-2]
                fr = decode_ax25(data)
                if fr is None or (fixed and not is_aprs_frame(fr)):
                    continue
                e = found.get(data)
                if e is None:
                    found[data] = {"data": data, "t": t / self.fsd, "fixed": fixed, "decoders": 1}
                else:
                    e["decoders"] += 1
                    e["fixed"] = min(e["fixed"], fixed)
                    e["t"] = min(e["t"], t / self.fsd)
            z.frames = []
        out = []
        now = self.n_out / self.fsd
        self.recent = {k: v for k, v in self.recent.items() if now - v < 3.0}
        for data, e in sorted(found.items(), key=lambda kv: kv[1]["t"]):
            # тот же кадр от другого срезателя кончается почти в тот же миг; повтор передачи — позже
            if data in self.recent and abs(e["t"] - self.recent[data]) < 0.25:
                continue
            self.recent[data] = e["t"]
            self.frames_total += 1
            if e["fixed"]:
                self.fixed_total += 1
            out.append(e)
        return out


def decode_audio(x, fs=FS, block=4800, **kw):
    """Весь звук сразу (блоками, как в потоке) → список кадров."""
    dem = AfskDemodulator(fs, **kw)
    out = []
    x = np.asarray(x, np.float32)
    for i in range(0, x.size, block):
        out += dem.process(x[i:i + block])
    return out


# ======================================================================= APRS: вспомогательное

def b91_decode(s):
    v = 0
    for ch in s:
        c = ord(ch) - 33
        if not 0 <= c <= 90:
            return None
        v = v * 91 + c
    return v


def b91_encode(v, n):
    out = []
    for _ in range(n):
        out.append(chr(v % 91 + 33))
        v //= 91
    return "".join(reversed(out))


def _lat(s):
    if len(s) != 8 or s[4] != "." or s[7] not in "NnSs":
        return None, 0
    amb = s.count(" ")
    d = s[:7].replace(" ", "0")
    try:
        deg, mins = int(d[:2]), float(d[2:7])
    except ValueError:
        return None, 0
    if deg > 90 or mins >= 60:
        return None, 0
    v = deg + mins / 60
    return (-v if s[7] in "Ss" else v), amb


def _lon(s):
    if len(s) != 9 or s[5] != "." or s[8] not in "EeWw":
        return None
    d = s[:8].replace(" ", "0")
    try:
        deg, mins = int(d[:3]), float(d[3:8])
    except ValueError:
        return None
    if deg > 180 or mins >= 60:
        return None
    v = deg + mins / 60
    return -v if s[8] in "Ww" else v


def _ts(s):
    """Отметка времени APRS → текст по-русски или None."""
    if len(s) < 7 or not s[:6].isdigit():
        return None
    v, z = s[:6], s[6]
    if z in "zZ":
        return f"{int(v[:2])}-е число, {v[2:4]}:{v[4:6]} UTC"
    if z == "/":
        return f"{int(v[:2])}-е число, {v[2:4]}:{v[4:6]} (местное)"
    if z == "h":
        return f"{v[:2]}:{v[2:4]}:{v[4:6]} UTC"
    return None


TABLE_CHARS = set("/\\ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghij")
WX_LEN = {"c": 3, "s": 3, "g": 3, "t": 3, "r": 3, "p": 3, "P": 3, "h": 2, "b": 5, "L": 3, "l": 3, "#": 3}


def _wx_fields(r, positional):
    """Поля погоды «g005t077r000…» → (dict, остаток)."""
    wx = {}
    i = 0
    while i < len(r) and r[i] in WX_LEN:
        k = r[i]
        n = WX_LEN[k]
        v = r[i + 1:i + 1 + n]
        if len(v) < n:
            break
        if v.strip(". ") == "":
            i += 1 + n
            continue
        try:
            val = int(v)
        except ValueError:
            break
        i += 1 + n
        if k == "c":
            wx["wind_dir"] = val
        elif k == "s":
            if positional:
                wx["snow_mm"] = val * 25.4
            else:
                wx["wind_ms"] = val * 0.44704
        elif k == "g":
            wx["gust_ms"] = val * 0.44704
        elif k == "t":
            wx["temp_c"] = (val - 32) * 5 / 9
        elif k == "r":
            wx["rain_1h_mm"] = val * 0.254
        elif k == "p":
            wx["rain_24h_mm"] = val * 0.254
        elif k == "P":
            wx["rain_midnight_mm"] = val * 0.254
        elif k == "h":
            wx["humidity"] = 100 if val == 0 else val
        elif k == "b":
            wx["pressure_hpa"] = val / 10
        elif k == "L":
            wx["lum_wm2"] = val
        elif k == "l":
            wx["lum_wm2"] = val + 1000
    return wx, r[i:]


def _alt_and_extras(rest, p):
    """Высота «/A=001234», уточнение «!W12!», телеметрия «|ss11|» из комментария."""
    m = re.search(r"/A=(-?\d{5,6})", rest)
    if m:
        p["alt_m"] = int(m.group(1)) * 0.3048
        rest = rest[:m.start()] + rest[m.end():]
    m = re.search(r"!([Ww])([0-9])([0-9])!", rest)
    if m and "lat" in p:
        dl = int(m.group(2)) * 0.001 / 60
        dn = int(m.group(3)) * 0.001 / 60
        p["lat"] += dl if p["lat"] >= 0 else -dl
        p["lon"] += dn if p["lon"] >= 0 else -dn
        rest = rest[:m.start()] + rest[m.end():]
    m = re.search(r"\|([!-{]{4,14})\|", rest)
    if m and len(m.group(1)) % 2 == 0:
        vals = [b91_decode(m.group(1)[k:k + 2]) for k in range(0, len(m.group(1)), 2)]
        if None not in vals:
            p["telemetry"] = {"seq": str(vals[0]), "analog": vals[1:6], "bits": None}
            rest = rest[:m.start()] + rest[m.end():]
    return rest


def _data_ext(rest, p):
    """Расширение данных после несжатой позиции: курс/скорость, PHG, RNG, DFS."""
    m = re.match(r"(\d{3}|\.{3}| {3})/(\d{3}|\.{3}| {3})", rest)
    if m:
        c, s = m.group(1), m.group(2)
        if p.get("sym_code") == "_":
            wx = p.setdefault("wx", {})
            if c.isdigit():
                wx["wind_dir"] = int(c)
            if s.isdigit():
                wx["wind_ms"] = int(s) * 0.44704
        else:
            if c.isdigit() and 1 <= int(c) <= 360:
                p["course"] = int(c) % 360
            if s.isdigit():
                p["speed_kmh"] = int(s) * 1.852
        rest = rest[7:]
        # DF-отчёт: «/BRG/NRQ»
        m2 = re.match(r"/(\d{3})/(\d{3})", rest)
        if m2 and p.get("sym_code") == "\\":
            p["df"] = {"bearing": int(m2.group(1))}
            rest = rest[8:]
    else:
        m = re.match(r"PHG(\d)(\d)(\d)(\d)", rest)
        if m:
            pw, h, g, d = (int(v) for v in m.groups())
            p["phg"] = {"power_w": pw * pw, "height_m": 10 * 2 ** h * 0.3048, "gain_db": g,
                        "dir": None if d == 0 else d * 45}
            rest = rest[7:]
        else:
            m = re.match(r"RNG(\d{4})", rest)
            if m:
                p["range_km"] = int(m.group(1)) * 1.609344
                rest = rest[7:]
            else:
                m = re.match(r"DFS(\d)(\d)(\d)(\d)", rest)
                if m:
                    p["dfs"] = m.group(0)
                    rest = rest[7:]
    if p.get("sym_code") == "_":
        wx, rest = _wx_fields(rest, positional=True)
        p.setdefault("wx", {}).update(wx)
    return rest


def _position(s, p):
    """Позиция (несжатая 19 символов или сжатая 13) в начале s → остаток (комментарий) или None."""
    if len(s) >= 19 and (s[0].isdigit() or s[0] == " "):
        lat, amb = _lat(s[0:8])
        lon = _lon(s[9:18])
        if lat is None or lon is None:
            return None
        p.update(lat=lat, lon=lon, sym_table=s[8], sym_code=s[18], compressed=False)
        if amb:
            p["ambiguity"] = amb
        return _data_ext(s[19:], p)
    if len(s) >= 10 and s[0] in TABLE_CHARS and not s[0].isdigit():
        y = b91_decode(s[1:5])
        x = b91_decode(s[5:9])
        if y is None or x is None:
            return None
        lat = 90 - y / 380926.0
        lon = -180 + x / 190463.0
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        tab = s[0]
        if "a" <= tab <= "j":
            tab = chr(ord(tab) - ord("a") + ord("0"))
        p.update(lat=lat, lon=lon, sym_table=tab, sym_code=s[9], compressed=True)
        rest = s[10:]
        if len(s) >= 13:
            c, sp, t = s[10], s[11], s[12]
            rest = s[13:]
            if c != " ":
                tt = ord(t) - 33
                if (tt & 0x18) == 0x10:
                    v = b91_decode(c + sp)
                    if v is not None:
                        p["alt_m"] = 1.002 ** v * 0.3048
                elif c == "{":
                    p["range_km"] = 2 * 1.08 ** (ord(sp) - 33) * 1.609344
                elif "!" <= c <= "z":
                    crs = (ord(c) - 33) * 4
                    if p["sym_code"] == "_":
                        p.setdefault("wx", {}).update(wind_dir=crs,
                                                       wind_ms=(1.08 ** (ord(sp) - 33) - 1) * 0.44704)
                    else:
                        if crs:
                            p["course"] = crs % 360
                        p["speed_kmh"] = (1.08 ** (ord(sp) - 33) - 1) * 1.852
        if p["sym_code"] == "_":
            wx, rest = _wx_fields(rest, positional=True)
            p.setdefault("wx", {}).update(wx)
        return rest
    return None


MICE_STD = {7: "Не на службе", 6: "В пути", 5: "На службе", 4: "Возвращаюсь", 3: "Занят",
            2: "Особое", 1: "Приоритет", 0: "ЭКСТРЕННОЕ"}
MICE_RADIO = {"_ ": "Yaesu VX-8", '_"': "Yaesu FTM-350", "_#": "Yaesu VX-8G", "_$": "Yaesu FT1D",
              "_%": "Yaesu FTM-400DR", "_)": "Yaesu FTM-100D", "_(": "Yaesu FT2D", "_0": "Yaesu FT3D",
              "_3": "Yaesu FT5D", "_1": "Yaesu FTM-300D", "_4": "Yaesu FTM-500D", "|3": "Byonics TinyTrack3",
              "|4": "Byonics TinyTrack4", "(5": "Anytone D578UV", "(8": "Anytone D878UV",
              ":4": "SCS GmbH Tracker", "^v": "HinzTec anyfrog"}


def _mice(dest, s, p):
    base = split_call(dest)[0]
    if len(base) < 6 or len(s) < 9:
        return None
    digits, bits = [], []
    custom = std = False
    for i, ch in enumerate(base[:6]):
        if "0" <= ch <= "9":
            d, bit = ord(ch) - 48, 0
        elif "A" <= ch <= "J":
            d, bit, custom = ord(ch) - 65, 1, True
        elif "P" <= ch <= "Y":
            d, bit = ord(ch) - 80, 1
            if i < 3:
                std = True
        elif ch == "K":
            d, bit, custom = 0, 1, True
        elif ch == "L":
            d, bit = 0, 0
        elif ch == "Z":
            d, bit = 0, 1
            if i < 3:
                std = True
        else:
            return None
        digits.append(d)
        if i < 3:
            bits.append(bit)
    lat = digits[0] * 10 + digits[1] + (digits[2] * 10 + digits[3] + (digits[4] * 10 + digits[5]) / 100) / 60
    north = "P" <= base[3] <= "Z"
    lon_off = 100 if "P" <= base[4] <= "Z" else 0
    west = "P" <= base[5] <= "Z"
    b = [ord(ch) - 28 for ch in s[1:7]]
    d = b[0] + lon_off
    if 180 <= d <= 189:
        d -= 80
    elif 190 <= d <= 199:
        d -= 190
    m = b[1]
    if m >= 60:
        m -= 60
    h = b[2]
    if not (0 <= d <= 180 and 0 <= m < 60 and 0 <= h < 100 and lat <= 90):
        return None
    lon = d + (m + h / 100) / 60
    speed = b[3] * 10 + b[4] // 10
    if speed >= 800:
        speed -= 800
    course = (b[4] % 10) * 100 + b[5]
    if course >= 400:
        course -= 400
    p.update(lat=lat if north else -lat, lon=-lon if west else lon, sym_code=s[7], sym_table=s[8],
             compressed=False, mic_e=True)
    if 0 < course <= 360:
        p["course"] = course % 360
    if speed:
        p["speed_kmh"] = speed * 1.852
    idx = bits[0] * 4 + bits[1] * 2 + bits[2]
    if custom and std:
        p["mice_msg"] = "неизвестно"
    elif custom:
        p["mice_msg"] = f"Своё сообщение {7 - idx}"
    else:
        p["mice_msg"] = MICE_STD[idx]
    rest = s[9:]
    radio = None
    if rest[:1] in (">", "]"):
        kenwood = rest[0] == ">"
        rest = rest[1:]
        if rest.endswith("="):
            radio = "Kenwood TH-D72" if kenwood else "Kenwood TM-D710"
            rest = rest[:-1]
        elif rest.endswith("^") and kenwood:
            radio = "Kenwood TH-D74"
            rest = rest[:-1]
        else:
            radio = "Kenwood TH-D7A" if kenwood else "Kenwood TM-D700"
    elif rest[:1] in ("`", "'"):
        rest = rest[1:]
        if len(rest) >= 2 and rest[-2:] in MICE_RADIO:
            radio = MICE_RADIO[rest[-2:]]
            rest = rest[:-2]
    if radio:
        p["radio"] = radio
    m = re.match(r"([!-{]{3})\}", rest)
    if m:
        p["alt_m"] = float(b91_decode(m.group(1)) - 10000)
        rest = rest[4:]
    return rest


def _nmea(s, p):
    f = s.split("*")[0].split(",")
    try:
        if f[0][3:] == "RMC" and len(f) >= 9 and f[2] == "A":
            lat, ns, lon, ew, spd, crs = f[3], f[4], f[5], f[6], f[7], f[8]
        elif f[0][3:] == "GGA" and len(f) >= 10:
            lat, ns, lon, ew = f[2], f[3], f[4], f[5]
            spd = crs = ""
            if f[9]:
                p["alt_m"] = float(f[9])
        else:
            return False
        la = int(lat[:2]) + float(lat[2:]) / 60
        lo = int(lon[:3]) + float(lon[3:]) / 60
    except (ValueError, IndexError):
        return False
    p.update(lat=-la if ns == "S" else la, lon=-lo if ew == "W" else lo, sym_table="/", sym_code="/")
    if spd:
        p["speed_kmh"] = float(spd) * 1.852
    if crs:
        p["course"] = float(crs) % 360
    return True


def parse(fr):
    """Кадр (dict от decode_ax25/parse_tnc2) → пакет APRS: dict с полями разбора."""
    p = {"src": fr["src"], "dest": fr["dest"], "path": list(fr["path"]), "type": "other", "comment": ""}
    p["raw"] = tnc2(fr)
    p["via"], p["via_iss"] = via_info(p["path"], p["src"])
    info = fr["info"] or b""
    if not is_aprs_frame(fr) and fr.get("pid") is not None and fr.get("pid") != 0xF0:
        p["type"] = "ax25"
        p["text"] = smart_text(info)
        return _finish(p)
    s = info.decode("latin-1")
    if not s:
        p["type"] = "empty"
        return _finish(p)
    c = s[0]
    rest = None
    if c in "!=":
        p["messaging"] = c == "="
        if s[1:2] == "!":                       # Ultimeter: «!!» — сырые данные метеостанции
            p["type"] = "other"
            p["text"] = s
        else:
            rest = _position(s[1:], p)
            p["type"] = "position" if rest is not None else "other"
    elif c in "/@":
        p["messaging"] = c == "@"
        p["timestamp"] = _ts(s[1:8])
        rest = _position(s[8:], p)
        p["type"] = "position" if rest is not None else "other"
    elif c in "`'\x1c\x1d":
        rest = _mice(fr["dest"], s, p)
        p["type"] = "mic-e" if rest is not None else "other"
    elif c == ";" and len(s) >= 18:
        p["name"] = smart_text(s[1:10]).rstrip()
        p["alive"] = s[10] == "*"
        p["timestamp"] = _ts(s[11:18])
        rest = _position(s[18:], p)
        p["type"] = "object" if rest is not None else "other"
    elif c == ")":
        m = re.match(r"\)([^!_]{3,9})([!_])", s)
        if m:
            p["name"] = smart_text(m.group(1)).rstrip()
            p["alive"] = m.group(2) == "!"
            rest = _position(s[m.end():], p)
        p["type"] = "item" if rest is not None else "other"
    elif c == ">":
        t = s[1:]
        ts = _ts(t[:7])
        if ts:
            p["timestamp"] = ts
            t = t[7:]
        p["type"] = "status"
        p["status"] = smart_text(t).strip()
    elif c == ":" and len(s) >= 11 and s[10] == ":":
        _message(s, p)
    elif c == "T" and s.startswith("T#"):
        parts = s[2:].split(",")
        an = []
        for v in parts[1:6]:
            try:
                an.append(float(v))
            except ValueError:
                an.append(None)
        bits = parts[6][:8] if len(parts) > 6 else None
        p["type"] = "telemetry"
        p["telemetry"] = {"seq": parts[0], "analog": an, "bits": bits}
        if len(parts) > 6 and len(parts[6]) > 8:
            p["comment"] = smart_text(parts[6][8:]).strip()
    elif c == "_" and len(s) >= 9 and s[1:9].isdigit():
        p["type"] = "weather"
        p["timestamp"] = f"{s[3:5]}.{s[1:3]} {s[5:7]}:{s[7:9]}"
        wx, rest2 = _wx_fields(s[9:], positional=False)
        p["wx"] = wx
        p["comment"] = smart_text(rest2).strip()
    elif c == "$":
        p["type"] = "nmea" if _nmea(s, p) else "other"
        p["text"] = s
    elif c == "}":
        inner = parse_tnc2(s[1:])
        if inner is not None:
            q = parse(inner)
            q["gateway"] = p["src"]
            q["third_party"] = True
            q["raw"] = p["raw"]
            q["via"], q["via_iss"] = p["via"], p["via_iss"]
            q["outer_path"] = p["path"]
            return q
        p["text"] = s
    elif c == "<":
        p["type"] = "capabilities"
        p["text"] = smart_text(s[1:])
    elif c == "?":
        p["type"] = "query"
        p["text"] = smart_text(s[1:])
    else:
        k = s.find("!", 0, 41)
        if k > 0:
            rest = _position(s[k + 1:], p)
            if rest is not None:
                p["type"] = "position"
                p["comment"] = smart_text(s[:k]).strip()
        if rest is None:
            p["text"] = smart_text(s)
    if rest is not None:
        rest = _alt_and_extras(rest, p)
        txt = smart_text(rest).strip()
        if p["type"] == "mic-e":
            p["comment"] = txt
        else:
            p["comment"] = (p["comment"] + " " + txt).strip()
    if p.get("wx") and p["type"] == "position":
        p["type"] = "weather"
    return _finish(p)


def _message(s, p):
    addressee = s[1:10].strip()
    text = s[11:]
    p["type"] = "message"
    msg = {"to": addressee}
    m = re.match(r"(ack|rej)([A-Za-z0-9}]{1,7})\s*$", text)
    if m:
        msg["kind"] = m.group(1)
        msg["id"] = m.group(2).split("}")[0]
        text = ""
    else:
        k = text.rfind("{")
        if k >= 0 and len(text) - k <= 9:
            msg["id"] = text[k + 1:].split("}")[0].strip()
            text = text[:k]
        msg["kind"] = "msg"
        for pref, kind in (("PARM.", "parm"), ("UNIT.", "unit"), ("EQNS.", "eqns"), ("BITS.", "bits")):
            if text.startswith(pref):
                msg["kind"] = "tlm_" + kind
                break
        if addressee.startswith("BLN"):
            msg["kind"] = "bulletin"
        elif addressee.startswith("NWS"):
            msg["kind"] = "nws"
    msg["text"] = smart_text(text).rstrip()
    p["message"] = msg


# ======================================================================= классы станций и символы

SYM_PRIMARY = {
    "!": "Полиция", "#": "Digipeater", "$": "Телефон", "%": "DX-кластер", "&": "КВ-шлюз",
    "'": "Малый самолёт", "(": "Мобильный спутник", ")": "Инвалидная коляска", "*": "Снегоход",
    "+": "Красный Крест", ",": "Скауты", "-": "Дом", ".": "Крестик", "/": "Точка", ":": "Пожар",
    ";": "Лагерь", "<": "Мотоцикл", "=": "Поезд", ">": "Автомобиль", "?": "Сервер", "@": "Ураган",
    "A": "Аптечка", "B": "BBS", "C": "Каноэ", "E": "Событие", "F": "Трактор", "G": "Квадрат",
    "H": "Гостиница", "I": "TCP/IP", "K": "Школа", "L": "Компьютер", "M": "MacAPRS", "N": "Станция NTS",
    "O": "Аэростат", "P": "Полиция", "R": "Дом на колёсах", "S": "Шаттл", "T": "SSTV", "U": "Автобус",
    "V": "ATV", "W": "Метеослужба", "X": "Вертолёт", "Y": "Яхта", "Z": "WinAPRS", "[": "Пешеход",
    "\\": "Пеленг (DF)", "]": "Почта", "^": "Самолёт", "_": "Метеостанция", "`": "Спутниковая антенна",
    "a": "Скорая помощь", "b": "Велосипед", "c": "Пункт управления", "d": "Пожарная часть", "e": "Лошадь",
    "f": "Пожарная машина", "g": "Планер", "h": "Больница", "i": "Остров (IOTA)", "j": "Джип",
    "k": "Грузовик", "l": "Ноутбук", "m": "Mic-E репитер", "n": "Узел", "o": "Штаб", "p": "Собака",
    "q": "Квадрат", "r": "Репитер", "s": "Судно", "t": "Стоянка грузовиков", "u": "Фура", "v": "Фургон",
    "w": "Водоочистка", "x": "X-APRS", "y": "Дом с антенной", "z": "Убежище",
}
SYM_ALT = {
    "#": "Digipeater", "&": "Шлюз (I-Gate)", "-": "Дом (КВ)", ">": "Автомобиль", "_": "Метеостанция",
    "S": "Спутник", "n": "Треугольник", "O": "Ракета/зонд", "^": "Самолёт", "k": "Внедорожник",
    "u": "Грузовик", "v": "Фургон", "s": "Катер", "0": "Круг (IRLP/EchoLink)", "a": "ARES",
    "L": "Маяк", "W": "Метеостанция", "j": "Работы на дороге", "!": "Авария", "[": "Пешеход",
}

# вид станции: подпись и цвет на карте
KINDS = {
    "iss": ("МКС / спутник", "#ff5cf0"),
    "car": ("Транспорт", "#ffb84d"),
    "person": ("Пешеход", "#ff8c69"),
    "home": ("Дом", "#7fdc7f"),
    "wx": ("Метеостанция", "#4fd1ff"),
    "digi": ("Digipeater", "#c39bff"),
    "igate": ("Шлюз I-Gate", "#a77bff"),
    "air": ("Воздушное судно", "#ffd84d"),
    "boat": ("Судно", "#4f8cff"),
    "object": ("Объект", "#c8c8c8"),
    "other": ("Прочее", "#e8e8e8"),
}
_SYM_KIND = {
    ">": "car", "k": "car", "u": "car", "v": "car", "j": "car", "U": "car", "R": "car", "<": "car",
    "a": "car", "f": "car", "!": "car", "P": "car", "=": "car", "b": "car", "*": "car", "F": "car",
    "[": "person", "e": "person", "p": "person",
    "-": "home", "y": "home", "H": "home",
    "_": "wx", "W": "wx", "@": "wx",
    "#": "digi", "r": "digi", "m": "digi",
    "&": "igate", "I": "igate",
    "'": "air", "^": "air", "X": "air", "g": "air", "O": "air",
    "s": "boat", "Y": "boat", "C": "boat",
}


def symbol_name(table, code):
    if not table or not code:
        return ""
    if table == "/":
        return SYM_PRIMARY.get(code, f"символ /{code}")
    name = SYM_ALT.get(code, f"символ \\{code}")
    if table != "\\":
        name += f" [{table}]"
    return name


def station_kind(p):
    base = split_call(p.get("src", ""))[0]
    code, table = p.get("sym_code"), p.get("sym_table")
    if base in ISS_CALLS or (code == "S" and table not in (None, "/")):
        return "iss"
    if p.get("type") in ("object", "item"):
        return "object"
    if p.get("wx") or p.get("type") == "weather":
        return "wx"
    if code and table:
        if table != "/" and code == "&":
            return "igate"
        return _SYM_KIND.get(code, "other")
    return "other"


def via_info(path, src=""):
    """Путь → (подпись, через МКС ли). Учитываются только digipeater с битом H."""
    used = [c for c, h in path if h]
    iss = any(split_call(c)[0] in ISS_CALLS for c in used) or split_call(src)[0] in ISS_CALLS
    if any(split_call(c)[0] in ISS_CALLS for c in used):
        return "через МКС", True
    if iss:
        return "МКС (борт)", True
    if used:
        last = used[-1]
        if re.match(r"^(WIDE|TRACE|RELAY|TCPIP|RFONLY)\d?(-\d+)?$", last):
            named = [c for c in used if not re.match(r"^(WIDE|TRACE|RELAY)\d?(-\d+)?$", c)]
            return ("через " + named[-1]) if named else "через ретранслятор", False
        return "через " + last, False
    return "напрямую", False


def _finish(p):
    p["kind"] = station_kind(p)
    if p.get("sym_code"):
        p["symbol"] = symbol_name(p.get("sym_table"), p.get("sym_code"))
    return p


def parse_line(text):
    """TNC2-строка → разобранный пакет (для тестов и журналов)."""
    fr = parse_tnc2(text)
    return parse(fr) if fr else None


def parse_frame(data):
    """Байты кадра без FCS → разобранный пакет или None."""
    fr = decode_ax25(data)
    return parse(fr) if fr else None


# ======================================================================= текст по-русски

def _num(v, nd=1):
    return f"{v:.{nd}f}".replace(".", ",")


def fmt_lat(v):
    d = int(abs(v))
    m = (abs(v) - d) * 60
    return f"{d}°{_num(m, 3)}′ {'с.' if v >= 0 else 'ю.'} ш."


def fmt_lon(v):
    d = int(abs(v))
    m = (abs(v) - d) * 60
    return f"{d}°{_num(m, 3)}′ {'в.' if v >= 0 else 'з.'} д."


def locator(lat, lon):
    """QTH-локатор (Maidenhead), 6 знаков."""
    lon += 180
    lat += 90
    a = chr(ord("A") + int(lon // 20)) + chr(ord("A") + int(lat // 10))
    b = str(int((lon % 20) // 2)) + str(int(lat % 10))
    c = chr(ord("a") + int((lon % 2) * 12)) + chr(ord("a") + int((lat % 1) * 24))
    return a + b + c


def distance_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360


def wx_text(wx):
    out = []
    if "wind_dir" in wx or "wind_ms" in wx:
        t = "ветер"
        if "wind_dir" in wx:
            t += f" {wx['wind_dir']}°"
        if "wind_ms" in wx:
            t += f" {_num(wx['wind_ms'])} м/с"
        if "gust_ms" in wx:
            t += f", порывы {_num(wx['gust_ms'])} м/с"
        out.append(t)
    if "temp_c" in wx:
        out.append(f"{_num(wx['temp_c'])} °C")
    if "humidity" in wx:
        out.append(f"влажность {wx['humidity']} %")
    if "pressure_hpa" in wx:
        out.append(f"давление {_num(wx['pressure_hpa'])} гПа")
    if "rain_1h_mm" in wx:
        out.append(f"осадки за час {_num(wx['rain_1h_mm'])} мм")
    if "rain_24h_mm" in wx:
        out.append(f"за сутки {_num(wx['rain_24h_mm'])} мм")
    if "rain_midnight_mm" in wx:
        out.append(f"с полуночи {_num(wx['rain_midnight_mm'])} мм")
    if "snow_mm" in wx:
        out.append(f"снег {_num(wx['snow_mm'], 0)} мм")
    if "lum_wm2" in wx:
        out.append(f"освещённость {wx['lum_wm2']} Вт/м²")
    return ", ".join(out)


def _pos_text(p):
    out = [f"{fmt_lat(p['lat'])}, {fmt_lon(p['lon'])}"]
    if p.get("symbol"):
        out.append(p["symbol"])
    mv = []
    if p.get("speed_kmh") is not None:
        mv.append(f"{p['speed_kmh']:.0f} км/ч")
    if p.get("course") is not None:
        mv.append(f"курс {p['course']:.0f}°")
    if mv:
        out.append(", ".join(mv))
    if p.get("alt_m") is not None:
        out.append(f"высота {p['alt_m']:.0f} м")
    if p.get("phg"):
        g = p["phg"]
        out.append(f"мощность {g['power_w']} Вт, антенна {g['height_m']:.0f} м, {g['gain_db']} дБ"
                   + ("" if g["dir"] is None else f", направл. {g['dir']}°"))
    if p.get("range_km"):
        out.append(f"радиус {p['range_km']:.0f} км")
    return " · ".join(out)


def describe(p):
    """Расшифровка пакета по-русски (одна строка)."""
    t = p.get("type")
    parts = []
    if p.get("third_party"):
        parts.append(f"Сторонний пакет через шлюз {p.get('gateway')}")
    if t == "position":
        parts.append("Позиция: " + _pos_text(p))
    elif t == "mic-e":
        parts.append("Позиция (Mic-E): " + _pos_text(p))
        if p.get("mice_msg"):
            parts.append(f"статус «{p['mice_msg']}»")
        if p.get("radio"):
            parts.append(f"радио {p['radio']}")
    elif t in ("object", "item"):
        what = "Объект" if t == "object" else "Элемент"
        st = "активен" if p.get("alive") else "удалён"
        parts.append(f"{what} «{p.get('name')}» ({st}): " + _pos_text(p))
    elif t == "weather":
        if "lat" in p:
            parts.append(f"Метеостанция: {fmt_lat(p['lat'])}, {fmt_lon(p['lon'])}")
        else:
            parts.append("Погода")
        parts.append(wx_text(p.get("wx") or {}) or "нет данных")
    elif t == "status":
        parts.append(f"Статус: «{p.get('status', '')}»")
    elif t == "message":
        m = p["message"]
        k = m.get("kind")
        if k == "ack":
            parts.append(f"Подтверждение №{m.get('id')} для {m['to']}")
        elif k == "rej":
            parts.append(f"Отказ №{m.get('id')} для {m['to']}")
        elif k == "bulletin":
            parts.append(f"Бюллетень {m['to']}: «{m['text']}»")
        elif k and k.startswith("tlm_"):
            names = {"tlm_parm": "названия каналов", "tlm_unit": "единицы", "tlm_eqns": "коэффициенты",
                     "tlm_bits": "биты"}
            parts.append(f"Описание телеметрии ({names[k]}): {m['text']}")
        else:
            parts.append(f"Сообщение для {m['to']}: «{m['text']}»" + (f" (№{m['id']})" if m.get("id") else ""))
    elif t == "telemetry":
        tl = p["telemetry"]
        an = ", ".join("—" if v is None else (f"{v:g}") for v in tl["analog"])
        parts.append(f"Телеметрия №{tl['seq']}: {an}" + (f"; биты {tl['bits']}" if tl.get("bits") else ""))
    elif t == "nmea":
        parts.append("NMEA GPS: " + _pos_text(p))
    elif t == "capabilities":
        parts.append(f"Возможности станции: {p.get('text', '')}")
    elif t == "query":
        parts.append(f"Запрос: {p.get('text', '')}")
    elif t == "ax25":
        parts.append(f"Кадр AX.25 (не APRS): {p.get('text', '')}")
    elif t == "empty":
        parts.append("Пустой пакет")
    else:
        parts.append(f"Данные: «{p.get('text', '')}»")
    if t not in ("weather",) and p.get("wx"):
        parts.append("погода: " + wx_text(p["wx"]))
    if t != "telemetry" and p.get("telemetry"):
        tl = p["telemetry"]
        parts.append("телеметрия: " + ", ".join(str(v) for v in tl["analog"]))
    if p.get("timestamp"):
        parts.append(f"время {p['timestamp']}")
    if p.get("comment"):
        parts.append(f"«{p['comment']}»")
    return " · ".join(parts)


# ======================================================================= кодеры полей (тесты, эфир)

def encode_position(lat, lon, table="/", code=">", comment="", messaging=False, ts=None):
    """Несжатая позиция «!DDMM.mmN/DDDMM.mmE>…» (с ts — «@DDHHMMz…»)."""
    la, lo = abs(lat), abs(lon)
    dla, dlo = int(la), int(lo)
    mla = round((la - dla) * 60, 2)
    mlo = round((lo - dlo) * 60, 2)
    if mla >= 60:
        dla, mla = dla + 1, 0.0
    if mlo >= 60:
        dlo, mlo = dlo + 1, 0.0
    s = (f"{dla:02d}{mla:05.2f}{'N' if lat >= 0 else 'S'}{table}"
         f"{dlo:03d}{mlo:05.2f}{'E' if lon >= 0 else 'W'}{code}")
    if ts:
        return ("@" if messaging else "/") + ts + s + comment
    return ("=" if messaging else "!") + s + comment


def encode_compressed(lat, lon, table="/", code=">", course=None, speed_kn=None, alt_ft=None, comment=""):
    y = int(round(380926 * (90 - lat)))
    x = int(round(190463 * (180 + lon)))
    s = table + b91_encode(y, 4) + b91_encode(x, 4) + code
    if alt_ft is not None:
        v = int(round(math.log(max(alt_ft, 1.0)) / math.log(1.002)))
        s += b91_encode(v, 2) + chr(33 + 0x30)
    elif course is not None and speed_kn is not None:
        s += chr(33 + int(round(course / 4)) % 90) + chr(33 + int(round(math.log(speed_kn + 1) / math.log(1.08))))
        s += chr(33 + 0x26)
    else:
        s += "  " + chr(33 + 0x20)
    return "!" + s + comment


def encode_mice(lat, lon, speed_kn=0, course=0, table="/", code=">", msg=6, comment="", alt_m=None,
                radio=None):
    """Mic-E → (поле назначения, поле данных) для make_frame. msg — номер стандартного сообщения 0…7."""
    tm = int(round(abs(lat) * 6000))
    dg = f"{tm // 6000:02d}{(tm % 6000) // 100:02d}{tm % 100:02d}"
    tl = int(round(abs(lon) * 6000))
    ldeg, lmin, lhun = tl // 6000, (tl % 6000) // 100, tl % 100
    off = ldeg < 10 or ldeg >= 100
    bits = [(msg >> 2) & 1, (msg >> 1) & 1, msg & 1]
    flags = bits + [lat >= 0, off, lon < 0]
    dest = "".join(chr((ord("P") if f else ord("0")) + int(d)) for d, f in zip(dg, flags))
    if ldeg < 10:
        d = ldeg + 90
    elif ldeg < 100:
        d = ldeg
    elif ldeg < 110:
        d = ldeg - 20
    else:
        d = ldeg - 100
    m = lmin + 60 if lmin < 10 else lmin
    speed = int(round(speed_kn))
    course = int(round(course)) % 360
    sp = speed // 10
    if sp < 20:
        sp += 80
    dc = (speed % 10) * 10 + course // 100
    se = course % 100
    info = "`" + "".join(chr(v + 28) for v in (d, m, lhun, sp, dc, se)) + code + table
    if radio:
        info = info[:9] + radio[0]
    if alt_m is not None:
        info += b91_encode(int(round(alt_m)) + 10000, 3) + "}"
    info += comment
    if radio and len(radio) > 1:
        info += radio[1:]
    return dest, info
