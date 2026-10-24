"""«Электро-Л» LRIT (1691 МГц): приём и декодирование снимков МСУ-ГС.

Параметры (SatDump resources/pipelines/Elektro_Arktika.json, «ELEKTRO-L LRIT»):
  1691,0 МГц, BPSK 294 000 символ/с, RRC α = 0,5; свёрточный код CCSDS k=7 r=1/2 (второй выход
  инвертирован), ASM 1ACFFC1D, CADU 1024 байта, дерандомизация CCSDS, RS(255,223) ×4.
  Транспорт — xRIT (CGMS LRIT/HRIT Global Specification): VCDU (6 байт) → M_PDU (884 байта) →
  CCSDS-пакеты с CRC-16 → TP_PDU (10 байт: номер файла, длина) → файлы LRIT с заголовками
  (0 — первичный, 1 — структура изображения, 4 — аннотация/имя, 5 — время, 128 — сегмент).
  Изображения МСУ-ГС в LRIT: JPEG (флаг сжатия 2), бывает без сжатия; вейвлет (флаг 1, HRIT) не поддержан.

Положение (2026): №2 — 14,5° з. д. (заменяется №3), №3 — перегоняется с 76° в. д. на 14,5° з. д.,
№4 — 165,8° в. д., №5 — 76° в. д. (с 2026 г. вместо №3). Текущая долгота берётся из TLE.
"""
import math
import struct

import numpy as np

from . import lrpt

LRIT_FREQ = 1691.0e6
SYMBOL_RATE = 294000
LIVE_RATE = 2048000
IF_OFFSET = 400e3            # настройка ниже сигнала: уход от пика постоянной составляющей

# NORAD: (подпись, номинальная долгота °в.д.)
SATS = {
    67756: ("Электро-Л №5", 76.0),
    44903: ("Электро-Л №3", -14.5),
    55506: ("Электро-Л №4", 165.8),
    41105: ("Электро-Л №2", -14.5),
}

R_EARTH = 6378.137
R_GEO = 42164.0


def geo_look(lat, lon, sat_lon):
    """Азимут (°, от севера по часовой), угол места (°) и дальность (км) на геостационар."""
    la, dl = math.radians(lat), math.radians(sat_lon - lon)
    # вектор на спутник в локальной системе (север, восток, вверх)
    sx = R_GEO * math.cos(dl)
    sy = R_GEO * math.sin(dl)
    up = sx * math.cos(la) - R_EARTH
    north = -sx * math.sin(la)
    east = sy
    rng = math.sqrt(up * up + north * north + east * east)
    el = math.degrees(math.atan2(up, math.hypot(north, east)))
    az = math.degrees(math.atan2(east, north)) % 360
    return az, el, rng


# ---------------------------------------------------------------- CRC и транспорт xRIT

def _crc_table():
    t = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) if c & 0x8000 else (c << 1)
        t.append(c & 0xFFFF)
    return t


_CRC = _crc_table()


def crc16(data):
    """CRC-16 CCITT (начальное 0xFFFF), как в LRIT Mission Specific Document."""
    c = 0xFFFF
    for b in data:
        c = ((c << 8) & 0xFFFF) ^ _CRC[(c >> 8) ^ b]
    return c


class XritDemux:
    """VCDU (1020 байт после RS, без ASM) → завершённые файлы LRIT (bytes)."""

    MPDU = 884

    def __init__(self):
        self.vc = {}          # vcid → {"pending": bytes|None, "count": int}
        self.files = {}       # (vcid, apid) → bytearray
        self.packets = 0
        self.crc_bad = 0
        self.nfiles = 0

    def push(self, vcdu):
        b = vcdu
        if (b[0] >> 6) != 1:
            return []
        vcid = b[1] & 0x3F
        if vcid == 63:
            return []
        st = self.vc.setdefault(vcid, {"pending": None, "count": None})
        count = (int(b[2]) << 16) | (int(b[3]) << 8) | int(b[4])
        if st["count"] is not None and count != (st["count"] + 1) & 0xFFFFFF:
            st["pending"] = None
        st["count"] = count
        fhp = ((int(b[6]) & 0x07) << 8) | int(b[7])
        zone = bytes(b[8:8 + self.MPDU])
        pkts = []
        if st["pending"] is not None:
            if fhp == 0x7FF:
                st["pending"] = self._parse(st["pending"] + zone, pkts)
                return self._handle(vcid, pkts)
            self._parse(st["pending"] + zone[:fhp], pkts, tail=False)
            st["pending"] = None
        if fhp != 0x7FF and fhp < self.MPDU:
            st["pending"] = self._parse(zone[fhp:], pkts)
        return self._handle(vcid, pkts)

    @staticmethod
    def _parse(buf, out, tail=True):
        i, n = 0, len(buf)
        while n - i >= 6:
            ln = ((buf[i + 4] << 8) | buf[i + 5]) + 1
            if n - i < 6 + ln:
                break
            apid = ((buf[i] & 0x07) << 8) | buf[i + 1]
            flags = buf[i + 2] >> 6
            if apid != 2047:
                out.append((apid, flags, buf[i + 6: i + 6 + ln]))
            i += 6 + ln
        return buf[i:] if tail else None

    def _handle(self, vcid, pkts):
        done = []
        for apid, flags, pl in pkts:
            self.packets += 1
            if len(pl) < 3:
                continue
            key = (vcid, apid)
            if crc16(pl[:-2]) != ((pl[-2] << 8) | pl[-1]):
                self.crc_bad += 1
                f = self.files.get(key)
                # середина изображения: пропускаем пакет, остальное файла сохраняем
                if not (flags == 0 and f is not None and len(f) > 16 and f[3] == 0):
                    self.files.pop(key, None)
                    continue
                f += bytes(len(pl) - 2)
                continue
            data = pl[:-2]
            if flags in (1, 3):                     # первый пакет файла: TP_PDU 10 байт
                if key in self.files:
                    done.append(bytes(self.files.pop(key)))
                self.files[key] = bytearray(data[10:])
                if flags == 3:
                    done.append(bytes(self.files.pop(key)))
            elif key in self.files:
                self.files[key] += data
                if flags == 2:
                    done.append(bytes(self.files.pop(key)))
        self.nfiles += len(done)
        return done


# ---------------------------------------------------------------- файлы LRIT

def parse_lrit(data):
    """Файл LRIT → dict: headers {тип: bytes записи}, name, file_type, body (данные после заголовков)."""
    if len(data) < 16 or data[0] != 0:
        return None
    total = struct.unpack(">I", data[4:8])[0]
    ftype = data[3]
    hdrs = {}
    i = 0
    while i + 3 <= min(total, len(data)):
        t = data[i]
        ln = (data[i + 1] << 8) | data[i + 2]
        if ln < 3:
            break
        hdrs[t] = data[i:i + ln]
        i += ln
    name = ""
    if 4 in hdrs:
        name = hdrs[4][3:].split(b"\0")[0].decode("ascii", "replace").strip()
    return dict(headers=hdrs, name=name, file_type=ftype, body=data[total:], total=total)


def image_info(f):
    h = f["headers"]
    if 1 not in h:
        return None
    r = h[1]
    info = dict(bpp=r[3], cols=(r[4] << 8) | r[5], lines=(r[6] << 8) | r[7], comp=r[8])
    if 128 in h and len(h[128]) >= 13:
        s = h[128]
        info.update(sc=(s[3] << 8) | s[4], channel=s[5] + 1, seg=(s[6] << 8) | s[7],
                    seg_start=(s[8] << 8) | s[9], seg_end=(s[10] << 8) | s[11])
    parts = f["name"].split("-")
    info["sat"] = parts[2].strip("_") if len(parts) == 8 else "?"
    info["time"] = parts[6].strip("_") if len(parts) == 8 else ""
    if "channel" not in info and len(parts) == 8:
        try:
            info["channel"] = int(parts[4].strip("_").split("_")[-1] or 0)
        except ValueError:
            info["channel"] = 0
    return info


def decode_image(f, info):
    """Пиксели сегмента → uint8 (lines, cols) или None."""
    body = f["body"]
    cols, lines, bpp, comp = info["cols"], info["lines"], info["bpp"], info["comp"]
    if comp == 2 or body[:2] == b"\xff\xd8":
        from PySide6.QtGui import QImage
        img = QImage.fromData(bytes(body), "JPG")
        if img.isNull():
            return None
        img = img.convertToFormat(QImage.Format_Grayscale8)
        w, h = img.width(), img.height()
        arr = np.frombuffer(img.constBits(), np.uint8, img.bytesPerLine() * h).reshape(h, img.bytesPerLine())
        return arr[:, :w].copy()
    if comp != 0:
        return None                      # вейвлет (HRIT) — не поддержан
    n = cols * lines
    if bpp == 8 and len(body) >= n:
        return np.frombuffer(body[:n], np.uint8).reshape(lines, cols).copy()
    if bpp == 10 and len(body) * 8 >= n * 10:
        bits = np.unpackbits(np.frombuffer(body, np.uint8))[: n * 10].reshape(n, 10)
        v = bits.astype(np.uint16) @ (1 << np.arange(9, -1, -1)).astype(np.uint16)
        return (v >> 2).astype(np.uint8).reshape(lines, cols)
    if bpp == 16 and len(body) >= 2 * n:
        v = np.frombuffer(body[:2 * n], ">u2").reshape(lines, cols)
        return (v >> 8).astype(np.uint8)
    return None


class Assembler:
    """Сегменты → изображения каналов: ключ (спутник, время, канал)."""

    def __init__(self):
        self.groups = {}      # key → {"segs": {n: arr}, "start", "end", "cols", "lines"}
        self.other = []       # имена прочих файлов (служебные, текст)
        self.version = 0
        self.unsupported = 0

    def push(self, data):
        f = parse_lrit(data)
        if f is None:
            return None
        info = image_info(f) if f["file_type"] == 0 else None
        if info is None:
            self.other.append(f["name"] or f"тип {f['file_type']}")
            return f["name"]
        arr = decode_image(f, info)
        if arr is None:
            self.unsupported += 1
            return f["name"]
        key = (info["sat"], info["time"], info.get("channel", 0))
        g = self.groups.setdefault(key, {"segs": {}, "start": info.get("seg_start", 1),
                                         "end": info.get("seg_end", 1), "cols": arr.shape[1],
                                         "lines": arr.shape[0]})
        g["segs"][info.get("seg", 1)] = arr
        self.version += 1
        return f["name"]

    def keys(self):
        return sorted(self.groups, key=lambda k: (k[1], k[2]))

    def image(self, key):
        g = self.groups.get(key)
        if not g:
            return None
        rows = []
        lo = min(g["start"], min(g["segs"]))
        hi = max(g["end"], max(g["segs"]))
        for n in range(lo, hi + 1):
            a = g["segs"].get(n)
            if a is None:
                a = np.zeros((g["lines"], g["cols"]), np.uint8)
            elif a.shape[1] != g["cols"]:
                b = np.zeros((a.shape[0], g["cols"]), np.uint8)
                w = min(a.shape[1], g["cols"])
                b[:, :w] = a[:, :w]
                a = b
            rows.append(a)
        return np.vstack(rows)

    def complete(self, key):
        g = self.groups[key]
        return len(g["segs"]), g["end"] - g["start"] + 1

    def composite(self, sat, tm, combo="321"):
        chans = [self.image((sat, tm, int(c))) for c in combo]
        if any(c is None for c in chans):
            return None
        h = min(c.shape[0] for c in chans)
        w = min(c.shape[1] for c in chans)
        return np.dstack([c[:h, :w] for c in chans])


# ---------------------------------------------------------------- приёмник

class LritReceiver(lrpt.PskReceiver):
    def __init__(self, fs):
        super().__init__(fs, SYMBOL_RATE, "bpsk", nrzm=None, max_hz=20000.0)
        self.demux = XritDemux()
        self.asm = Assembler()
        self.names = []

    def _use(self, frames):
        for fr in frames:
            if fr["ok"]:
                for data in self.demux.push(fr["data"][:892]):
                    nm = self.asm.push(data)
                    if nm:
                        self.names.append(nm)
                        if len(self.names) > 200:
                            self.names.pop(0)

    def feed(self, iq, want_cons=True):
        self._use(self.feed_frames(iq, want_cons))

    def finish(self):
        self._use(self.finish_frames())

    def stats(self):
        st = self.base_stats()
        st.update(packets=self.demux.packets, files=self.demux.nfiles, crc_bad=self.demux.crc_bad,
                  images=len(self.asm.groups), unsupported=self.asm.unsupported)
        return st


def decode_file(path, fs=None, fmt=None, progress=None, cancel=None, offset_hz=None):
    fmt = fmt or lrpt.guess_format(path)
    if fmt == "wav":
        fs = fs or lrpt.wav_info(path)[0]
    if not fs:
        raise ValueError("Укажите частоту дискретизации записи")
    if offset_hz is None:
        offset_hz = lrpt.offset_from_name(path, fs, (LRIT_FREQ,))
    if offset_hz is None:
        off, gain_db = lrpt.find_signal(path, fs, fmt, rs=SYMBOL_RATE)
        offset_hz = off if gain_db > 1.0 and abs(off) > 20000 else 0.0
    rx = LritReceiver(fs)
    rx.set_offset(offset_hz)
    it = lrpt.iter_iq(path, fmt)
    n = next(it)
    done = 0
    for blk in it:
        if cancel is not None and cancel():
            break
        rx.feed(blk, want_cons=False)
        done += blk.size
        if progress:
            progress(done / max(n, 1), rx)
    rx.finish()
    return rx


# ---------------------------------------------------------------- синтетика (тесты, демо)

def synth_disk(n=400, seed=0):
    """Условный «полный диск» n×n: Земля с облаками на чёрном фоне."""
    y, x = np.mgrid[0:n, 0:n].astype(np.float32)
    r = np.hypot(x - n / 2, y - n / 2) / (n * 0.46)
    rng = np.random.default_rng(seed)
    noise = np.zeros((n, n), np.float32)
    for k in (8, 16, 32):
        g = rng.random((k, k)).astype(np.float32)
        noise += np.kron(g, np.ones((n // k + 1, n // k + 1), np.float32))[:n, :n] / 3
    img = 60 + 120 * noise + 30 * np.cos(y / n * math.pi)
    img = np.where(r < 1.0, img, 0)
    return np.clip(img, 0, 255).astype(np.uint8)


def _jpeg(arr, quality=85):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QImage
    a = np.ascontiguousarray(arr)
    img = QImage(a.data, a.shape[1], a.shape[0], a.shape[1], QImage.Format_Grayscale8)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "JPG", quality)
    return bytes(ba.data())


def make_lrit_file(arr, channel, seg, nseg, sat=3, tm="202610231200", jpeg=True):
    body = _jpeg(arr) if jpeg else arr.tobytes()
    name = f"L-000-GOMS{sat}_-MSUGS_____-{channel:02d}_{6 + channel}_______-{seg:06d}___-{tm}-C_".encode()
    h1 = bytes([1, 0, 9, 8]) + struct.pack(">HH", arr.shape[1], arr.shape[0]) + bytes([2 if jpeg else 0])
    h4 = bytes([4]) + struct.pack(">H", 3 + len(name)) + name
    h128 = bytes([128, 0, 13]) + struct.pack(">HBHHHB", sat, channel - 1, seg, 1, nseg, 0)
    total = 16 + len(h1) + len(h4) + len(h128)
    h0 = bytes([0, 0, 16, 0]) + struct.pack(">IQ", total, len(body) * 8)
    return h0 + h1 + h4 + h128 + body


def lrit_packets(files, apid=100, counter0=0, max_payload=8190):
    """Файлы LRIT → CCSDS-пакеты (TP_PDU + CRC)."""
    out = []
    seq = 0
    for k, f in enumerate(files):
        tp = struct.pack(">HQ", (counter0 + k) & 0xFFFF, len(f) * 8) + f
        chunks = [tp[i:i + max_payload] for i in range(0, len(tp), max_payload)]
        for j, ch in enumerate(chunks):
            flag = 3 if len(chunks) == 1 else (1 if j == 0 else (2 if j == len(chunks) - 1 else 0))
            pl = ch + struct.pack(">H", crc16(ch))
            out.append(struct.pack(">HHH", apid & 0x7FF, (flag << 14) | (seq & 0x3FFF), len(pl) - 1) + pl)
            seq += 1
    return out


def packets_to_vcdus(packets, vcid=2, count0=0, scid=0x2C):
    stream = bytearray()
    starts = []
    for p in packets:
        starts.append(len(stream))
        stream += p
    stream += bytes([0xFF]) * (-len(stream) % XritDemux.MPDU)
    out = []
    si = 0
    m = XritDemux.MPDU
    for k in range(len(stream) // m):
        a, b = k * m, (k + 1) * m
        while si < len(starts) and starts[si] < a:
            si += 1
        fhp = starts[si] - a if si < len(starts) and starts[si] < b else 0x7FF
        cnt = count0 + k
        hdr = bytes([0x40 | (scid >> 2), ((scid & 3) << 6) | vcid, (cnt >> 16) & 255, (cnt >> 8) & 255,
                     cnt & 255, 0, (fhp >> 8) & 7, fhp & 255])
        out.append(np.frombuffer(hdr + bytes(stream[a:b]), np.uint8))
    return out


class BpskModulator:
    """CADU → BPSK (RRC α=0,5), свёрточный код CCSDS с инверсией второго выхода."""

    def __init__(self, sps_=8, alpha=0.5):
        self.sps = sps_
        self.h = lrpt.rrc_taps(sps_, alpha, 12)
        self.tail = np.zeros(self.h.size - 1, np.complex64)
        self.state = 0

    def modulate(self, cadus):
        from scipy import signal as sps
        bits = np.unpackbits(np.concatenate(cadus))
        enc, self.state = lrpt.conv_encode(bits, self.state)
        enc[1::2] ^= 1
        sym = enc.astype(np.float32) * 2 - 1
        n = sym.size * self.sps
        x = np.zeros(n, np.complex64)
        x[:: self.sps] = sym
        y = sps.oaconvolve(x, self.h).astype(np.complex64)
        y[: self.tail.size] += self.tail
        self.tail = y[n:].copy()
        return y[:n]


def synth_signal(nseg=4, size=320, channels=(1, 2, 3), dual=True, sat=3):
    """Полная цепочка передатчика: снимки → LRIT → пакеты → VCDU → CADU → BPSK (sps 8, fs 2,352 МГц).
    Возвращает (сигнал complex64, fs, {канал: исходный снимок})."""
    disks = {c: synth_disk(size, c) for c in channels}
    files = []
    h = size // nseg
    for c in channels:
        for s in range(nseg):
            files.append(make_lrit_file(disks[c][s * h:(s + 1) * h], c, s + 1, nseg, sat))
    fill = lrpt.vcdus_to_cadus(packets_to_vcdus([], vcid=63) or [np.frombuffer(
        bytes([0x40, 63, 0, 0, 0, 0, 7, 255]) + bytes(XritDemux.MPDU), np.uint8)] * 6, dual)
    cad = fill + lrpt.vcdus_to_cadus(packets_to_vcdus(lrit_packets(files)), dual) + fill
    mod = BpskModulator(8)
    return mod.modulate(cad), SYMBOL_RATE * 8, disks
