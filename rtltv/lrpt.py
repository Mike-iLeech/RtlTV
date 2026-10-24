"""Декодер «Метеор-М» LRPT: IQ → снимки МСУ-МР.

Параметры (2026 г., по SatDump resources/pipelines/Meteor-M.json и наблюдениям SatNOGS):
  * «Метеор-М» №2-3 (NORAD 57166) и №2-4 (NORAD 59051): 137,900 МГц (резерв 137,100 МГц),
    72 000 символ/с, OQPSK, RRC α = 0,5, свёрточный код k=7 r=1/2 (0x4F/0x6D), после Витерби —
    дифференциальное декодирование NRZ-M, ASM 1ACFFC1D, кадр CADU 1024 байта,
    дерандомизация CCSDS, RS(255,223) ×4 (обычный, не дуальный базис).
  * старый «Метеор-М» №2 (до 2022 г., архивные записи): 72k QPSK без NRZ-M.
  * режим 80k с перемежением сейчас не используется (здесь не реализован).

Цепочка: перенос частоты (доплер) → RRC + децимация (numpy) → [C] АРУ, Костас, Гарднер →
мягкие символы → [C] Витерби (8 гипотез фазы/сдвига, автоопределение NRZ-M) → ASM →
дерандомизация → [C] RS → VCDU (VCID 5) → M_PDU → CCSDS-пакеты → APID 64–69 →
декомпрессия МКУ (JPEG-подобная, стандартные таблицы Хаффмана) → каналы и RGB-композиты.
"""
import ctypes as C
import os
import re
import struct
import sys

import numpy as np
from scipy import signal as sps

SYMBOL_RATE = 72000
ASM = 0x1ACFFC1D
FRAME_BYTES = 1024
FRAME_BITS = FRAME_BYTES * 8
WIDTH = 1568            # 196 МКУ × 8 пикселей
APIDS = (64, 65, 66, 67, 68, 69)

SATS = {
    # имя: NORAD, частота, модуляция, скорость
    "METEOR-M2 3": dict(norad=57166, freq=137.9e6, alt=137.1e6, oqpsk=True, rs=72000),
    "METEOR-M2 4": dict(norad=59051, freq=137.9e6, alt=137.1e6, oqpsk=True, rs=72000),
}


# ---------------------------------------------------------------- DLL

def _dll_path():
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "rtltv", "driver", "lrpt_dsp.dll")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "driver", "lrpt_dsp.dll")


_lib = None


def lib():
    global _lib
    if _lib is not None:
        return _lib
    path = _dll_path()
    if not os.path.exists(path):
        raise RuntimeError(f"Не найдена библиотека декодера: {path} (соберите tools\\build_lrpt_dsp.bat)")
    L = C.CDLL(path)
    vp, i32, f64 = C.c_void_p, C.c_int, C.c_double
    L.demod_size.argtypes, L.demod_size.restype = [], i32
    L.demod_init.argtypes, L.demod_init.restype = [vp, f64, i32, f64, f64, f64], vp
    L.demod_set_costas.argtypes, L.demod_set_costas.restype = [vp, f64], None
    L.demod_set_freq.argtypes, L.demod_set_freq.restype = [vp, f64], None
    L.demod_state.argtypes, L.demod_state.restype = [vp, vp], None
    L.demod_process.argtypes, L.demod_process.restype = [vp, vp, i32, vp, vp, i32], i32
    L.conv_encode.argtypes, L.conv_encode.restype = [vp, i32, vp, i32], i32
    L.viterbi27.argtypes, L.viterbi27.restype = [vp, i32, vp, vp], i32
    L.rs_encode.argtypes, L.rs_encode.restype = [vp, vp, i32], None
    L.rs_decode.argtypes, L.rs_decode.restype = [vp, i32], i32
    _lib = L
    return L


def _p(a):
    return a.ctypes.data_as(C.c_void_p)


# ---------------------------------------------------------------- базовые блоки

def rrc_taps(sps_, alpha=0.5, span=12):
    """Корень из приподнятого косинуса, sps_ — отсчётов на символ (может быть дробным)."""
    n = int(span * sps_) | 1
    t = (np.arange(n) - n // 2) / sps_
    h = np.zeros(n)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-9:
            h[i] = 1 - alpha + 4 * alpha / np.pi
        elif abs(abs(4 * alpha * ti) - 1) < 1e-9:
            h[i] = alpha / np.sqrt(2) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha))
                                         + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
        else:
            h[i] = (np.sin(np.pi * ti * (1 - alpha)) + 4 * alpha * ti * np.cos(np.pi * ti * (1 + alpha))) / \
                   (np.pi * ti * (1 - (4 * alpha * ti) ** 2))
    return (h / np.sqrt(np.sum(h ** 2))).astype(np.float32)


def pn_sequence(n=FRAME_BYTES - 4):
    """Псевдослучайная последовательность CCSDS: a[k+8] = a[k+7] ^ a[k+5] ^ a[k+3] ^ a[k], начало — 8 единиц.
    Даёт FF 48 0E C0 9A …"""
    nb = 8 * n
    a = np.zeros(nb + 8, np.uint8)
    a[:8] = 1
    for k in range(nb):
        a[k + 8] = a[k + 7] ^ a[k + 5] ^ a[k + 3] ^ a[k]
    return np.packbits(a[:nb])


_PN = None


def pn():
    global _PN
    if _PN is None:
        _PN = pn_sequence()
    return _PN


ASM_BITS = np.array([(ASM >> (31 - i)) & 1 for i in range(32)], np.uint8)
ASM_PM = (ASM_BITS.astype(np.int16) * 2 - 1)


def nrzm_decode(bits, prev=0):
    """NRZ-M → NRZ-L: d[i] = e[i] xor e[i-1]."""
    out = np.empty_like(bits)
    out[0] = bits[0] ^ prev
    out[1:] = bits[1:] ^ bits[:-1]
    return out


def nrzm_encode(bits, prev=0):
    return (np.bitwise_xor.accumulate(np.concatenate([[prev], bits]).astype(np.uint8))[1:]).astype(np.uint8)


def viterbi(soft):
    """soft — int8 (I,Q,...) где >0 = 1; возвращает биты uint8."""
    soft = np.ascontiguousarray(soft, np.int8)
    n = soft.size // 2
    out = np.empty(n, np.uint8)
    dec = np.empty(max(n, 1), np.uint64)
    lib().viterbi27(_p(soft), n, _p(out), _p(dec))
    return out


def conv_encode(bits, state=0):
    bits = np.ascontiguousarray(bits, np.uint8)
    out = np.empty(bits.size * 2, np.uint8)
    st = lib().conv_encode(_p(bits), bits.size, _p(out), state)
    return out, st


def rs_decode(cw, dual=False):
    """cw — 255 байт (изменяется на месте). Возвращает число исправленных байт или -1."""
    return lib().rs_decode(_p(cw), 1 if dual else 0)


def rs_encode(data223, dual=False):
    data223 = np.ascontiguousarray(data223, np.uint8)
    par = np.empty(32, np.uint8)
    lib().rs_encode(_p(data223), _p(par), 1 if dual else 0)
    return par


def _transform(soft, hyp):
    """Снятие неоднозначности: 0:(a,b) 1:(-b,a) 2:(a,-b) 3:(b,a); 4..7 — то же со сдвигом на 1 мягкий бит."""
    s = soft[1:] if hyp >= 4 else soft
    s = s[: s.size // 2 * 2].astype(np.int16)
    a, b = s[0::2], s[1::2]
    k = hyp & 3
    if k == 1:
        a, b = -b, a
    elif k == 2:
        b = -b
    elif k == 3:
        a, b = b, a
    out = np.empty(a.size * 2, np.int8)
    out[0::2] = np.clip(a, -127, 127)
    out[1::2] = np.clip(b, -127, 127)
    return out


def _asm_hits(bits, max_err=3):
    """Позиции ASM (прямого и инверсного): [(pos, inverted)]."""
    if bits.size < 32:
        return []
    v = bits.astype(np.int16) * 2 - 1
    c = np.correlate(v, ASM_PM, "valid")
    thr = 32 - 2 * max_err
    pos = np.nonzero(np.abs(c) >= thr)[0]
    return [(int(p), bool(c[p] < 0)) for p in pos]


# ---------------------------------------------------------------- демодулятор

class Demodulator:
    """IQ (complex64, fs) → мягкие символы. Перенос частоты, RRC+децимация в numpy, петли — в C."""

    MODES = {"qpsk": 0, "oqpsk": 1, "bpsk": 2}

    def __init__(self, fs, rs=SYMBOL_RATE, oqpsk=True, costas_bw=0.006, timing_bw=0.002, alpha=0.5, mode=None):
        self.fs = float(fs)
        self.rs = float(rs)
        self.mode = mode or ("oqpsk" if oqpsk else "qpsk")
        self.oqpsk = self.mode == "oqpsk"
        self.bpsk = self.mode == "bpsk"
        self.up = 1
        while self.fs * self.up < 4 * self.rs:
            self.up += 1
        fsu = self.fs * self.up
        self.down = max(1, int(fsu // (4 * self.rs)))
        self.fs2 = fsu / self.down
        self.sps = self.fs2 / self.rs
        self.taps = rrc_taps(fsu / self.rs, alpha, 12) * self.up
        self.tail = np.zeros(self.taps.size - 1, np.complex64)
        self.dphase = 0
        self.offset_hz = 0.0
        self._nco = 0.0
        L = lib()
        self._mem = np.zeros(L.demod_size() + 64, np.uint8)
        self.h = L.demod_init(_p(self._mem), self.sps, self.MODES[self.mode], costas_bw, timing_bw,
                              2 * np.pi * 8000.0 / self.fs2)
        self._st = np.zeros(6, np.float64)
        self.mixed = []          # последние ~0,4 с после переноса частоты (для грубой оценки)
        self.mixed_n = 0
        self.nsym = 0

    def set_offset(self, hz):
        """Частота сигнала относительно центра настройки (доплер + поправка), Гц."""
        self.offset_hz = float(hz)

    def set_residual(self, hz):
        lib().demod_set_freq(self.h, 2 * np.pi * hz / self.fs2)

    def set_costas_bw(self, bw):
        lib().demod_set_costas(self.h, bw)

    def state(self):
        lib().demod_state(self.h, _p(self._st))
        f, ph, om, agc, amp, lock = self._st
        return dict(residual_hz=f * self.fs2 / (2 * np.pi), omega=om, agc=agc, amp=amp,
                    lock=float(np.clip(lock / 0.92, 0.0, 1.0)))

    def filter(self, iq):
        """Перенос частоты, RRC, ресемплинг до fs2. Возвращает complex64."""
        iq = np.asarray(iq, np.complex64)
        n = iq.size
        if self.offset_hz:
            w = -2 * np.pi * self.offset_hz / self.fs
            ph = self._nco + w * np.arange(n)
            iq = iq * np.exp(1j * ph).astype(np.complex64)
            self._nco = float((self._nco + w * n) % (2 * np.pi))
        self.mixed.append(iq)
        self.mixed_n += iq.size
        while len(self.mixed) > 1 and self.mixed_n - self.mixed[0].size >= self.fs * 0.4:
            self.mixed_n -= self.mixed.pop(0).size
        if self.up > 1:
            u = np.zeros(n * self.up, np.complex64)
            u[:: self.up] = iq
            iq = u
        x = np.concatenate([self.tail, iq])
        y = sps.oaconvolve(x, self.taps, mode="valid").astype(np.complex64)
        self.tail = x[-(self.taps.size - 1):].copy()
        out = y[self.dphase:: self.down]
        self.dphase = (self.dphase - y.size) % self.down
        return np.ascontiguousarray(out)

    def process(self, iq, want_cons=True):
        return self.process_fs2(self.filter(iq), want_cons)

    def process_fs2(self, x, want_cons=True):
        x = np.ascontiguousarray(x, np.complex64)
        maxs = int(x.size / self.sps) + 16
        soft = np.empty(maxs * 2, np.int8)
        cons = np.empty(maxs * 2, np.float32) if want_cons else None
        n = lib().demod_process(self.h, _p(x), x.size, _p(soft), _p(cons) if want_cons else None, maxs)
        self.nsym += n
        ns = n if self.bpsk else 2 * n
        return soft[:ns], (cons[: 2 * n].reshape(-1, 2) if want_cons else None)


def coarse_offset(x, fs, rs=SYMBOL_RATE, max_hz=10000.0):
    """Грубая оценка частоты сигнала (Гц) по симметрии спектра и SNR (дБ).

    x — отсчёты ДО согласованного фильтра (иначе фильтр, стоящий на нуле, тянет оценку к нулю).
    """
    if x.size < 8192:
        return 0.0, 0.0
    nfft = 1 << int(np.ceil(np.log2(fs / 40.0)))
    nfft = min(nfft, 1 << int(np.floor(np.log2(x.size / 4))))
    f, p = sps.welch(x, fs=fs, nperseg=nfft, return_onesided=False, detrend=False)
    p = np.fft.fftshift(p)
    f = np.fft.fftshift(f)
    span = min(fs / 2, rs * 1.6)
    keep = np.abs(f) <= span
    f, p = f[keep], p[keep]
    df = f[1] - f[0]
    # уровень шума — по краям полосы анализа (там, где сигнала нет при любом доплере)
    edge = np.abs(f) >= rs * 0.5 + max_hz + 2000
    noise = float(np.median(p[edge])) if np.count_nonzero(edge) > 32 else float(np.percentile(p, 10))
    q = np.maximum(p / max(noise, 1e-30) - 1.0, 0.0)
    # сглаживание ~ 1 кГц, чтобы не цепляться за отдельные бины
    k = max(1, int(1000 / df))
    q = sps.fftconvolve(q, np.ones(k) / k, mode="same")
    r = sps.fftconvolve(q, q)  # автосвёртка: максимум при i+j = 2·центр
    lags = np.arange(r.size) * df / 2 + f[0]
    ok = np.abs(lags) <= max_hz
    if not np.any(ok):
        return 0.0, 0.0
    i = int(np.argmax(np.where(ok, r, -1)))
    c = float(lags[i])
    band = np.abs(f - c) <= rs * 0.35
    snr = 10 * np.log10(max(float(np.mean(p[band])) / max(noise, 1e-30) - 1.0, 1e-3))
    return c, float(snr)


# ---------------------------------------------------------------- кадры

class FrameDecoder:
    """Мягкие символы → CADU (1024 байта, после дерандомизации и RS).

    Поиск: 3 кадра мягких символов декодируются Витерби для 8 гипотез (поворот 0/90°, инверсия Q,
    перестановка I/Q, сдвиг на полсимвола) с NRZ-M и без; выбирается гипотеза, где ASM повторяется
    через 8192 бита. При двух пропусках ASM подряд (проскальзывание фазы Костаса) — повторный поиск
    по последним кадрам.
    """

    SEARCH_PAIRS = 3 * FRAME_BITS
    WIN = 8192           # пар на окно Витерби (один кадр)
    OVL = 384            # перекрытие окон (глубина трассировки)

    def __init__(self, nrzm=None):
        self.nrzm_forced = nrzm
        self.frames = 0
        self.frames_ok = 0
        self.rs_fixed = 0
        self.rs_dual = None
        self.recent = []         # последние результаты RS (True/False)
        self.relocks = 0
        self.reset()

    def reset(self):
        self.locked = False
        self.hyp = None
        self.nrzm = False
        self.raw = np.zeros(0, np.int8)    # мягкие до поиска
        self.soft = np.zeros(0, np.int8)   # в захвате, ещё не декодированные
        self.hist = []                     # недавно декодированные мягкие (для повторного поиска)
        self.vtail = None
        self.nrzm_prev = 0
        self.bits = np.zeros(0, np.uint8)
        self.expect = None
        self.misses = 0

    @property
    def synced(self):
        return self.locked and self.misses == 0 and self.frames > 0

    def rs_ratio(self):
        if not self.recent:
            return 0.0
        return 100.0 * sum(self.recent) / len(self.recent)

    def push(self, soft):
        out = []
        if self.locked:
            self.soft = np.concatenate([self.soft, soft])
        else:
            self.raw = np.concatenate([self.raw, soft])
        need = 2 * self.SEARCH_PAIRS + 2
        for _ in range(8):
            if not self.locked:
                if self.raw.size < need:
                    break
                if not self._search(self.raw[:need]):
                    self.raw = self.raw[need - 2 * FRAME_BITS:]
                    continue
                self.soft = self.raw[1:] if self.hyp >= 4 else self.raw
                self.raw = np.zeros(0, np.int8)
            self._run(out)
            if self.locked:
                break
        return out

    def _search(self, raw):
        best = None
        for hyp in range(8):
            t = _transform(raw, hyp)
            b = viterbi(t)[64:]
            for nz in ((False, True) if self.nrzm_forced is None else (self.nrzm_forced,)):
                bb = nrzm_decode(b) if nz else b
                hits = _asm_hits(bb)
                if len(hits) < 2:
                    continue
                pos = [p for p, _ in hits]
                good = sum(1 for p in pos for q in pos if q > p and (q - p) % FRAME_BITS == 0)
                if good and (best is None or good > best[0]):
                    best = (good, hyp, nz)
        if best is None:
            return False
        _, self.hyp, self.nrzm = best
        self.locked = True
        self.misses = 0
        self.expect = None
        self.bits = np.zeros(0, np.uint8)
        self.nrzm_prev = 0
        self.vtail = None
        self.hist = []
        return True

    def _run(self, out):
        while self.locked and self.soft.size >= 2 * self.WIN:
            take = 2 * self.WIN
            chunk = self.soft[:take]
            self.soft = self.soft[take:]
            self.hist.append(chunk)
            if len(self.hist) > 4:
                self.hist.pop(0)
            t = _transform(chunk, self.hyp & 3)
            if self.vtail is None:
                win = t
                b = viterbi(win)
                bits = b[: b.size - self.OVL]
            else:
                win = np.concatenate([self.vtail, t])
                b = viterbi(win)
                bits = b[self.OVL: b.size - self.OVL]
            self.vtail = win[-4 * self.OVL:]
            if self.nrzm:
                nb = nrzm_decode(bits, self.nrzm_prev)
                self.nrzm_prev = int(bits[-1])
                bits = nb
            self.bits = np.concatenate([self.bits, bits])
            self._deframe(out)

    def flush(self):
        """Конец файла: декодировать остаток."""
        out = []
        if self.locked and self.soft.size >= 2 * self.OVL:
            t = _transform(self.soft[: self.soft.size // 2 * 2], self.hyp & 3)
            win = t if self.vtail is None else np.concatenate([self.vtail, t])
            b = viterbi(win)
            bits = b[(0 if self.vtail is None else self.OVL):]
            if self.nrzm:
                bits = nrzm_decode(bits, self.nrzm_prev)
            self.bits = np.concatenate([self.bits, bits])
            self.soft = np.zeros(0, np.int8)
            self._deframe(out)
        return out

    def _deframe(self, out):
        while self.locked:
            if self.expect is None:
                hits = _asm_hits(self.bits, 3)
                if not hits:
                    if self.bits.size >= FRAME_BITS + 32:
                        self.bits = self.bits[-32:]
                        self._miss()
                    return
                self.expect = hits[0][0]
            p = self.expect
            if self.bits.size < p + FRAME_BITS + 16:
                if p > 16:
                    self.bits = self.bits[p - 16:]
                    self.expect = 16
                return
            # ASM ищем в окне ±16 бит от ожидаемого места (проскальзывание символа)
            lo = max(0, p - 16)
            seg = self.bits[lo: p + 16 + 32].astype(np.int16) * 2 - 1
            c = np.correlate(seg, ASM_PM, "valid")
            k = int(np.argmax(np.abs(c)))
            if abs(int(c[k])) < 32 - 2 * 6:
                self.expect = None
                self.bits = self.bits[p + FRAME_BITS - 64:]
                self._miss()
                continue
            p = lo + k
            inv = c[k] < 0
            self.misses = 0
            frame = np.packbits(self.bits[p: p + FRAME_BITS])
            if inv:
                frame ^= 0xFF
            out.append(self._finish(frame))
            self.bits = self.bits[p + FRAME_BITS - 16:]
            self.expect = 16

    def _miss(self):
        self.misses += 1
        if self.misses >= 2:
            raw = np.concatenate(self.hist[-3:] + [self.soft]) if self.hist else self.soft
            self.relocks += 1
            st = (self.frames, self.frames_ok, self.rs_fixed, self.rs_dual, self.recent, self.relocks)
            self.reset()
            self.frames, self.frames_ok, self.rs_fixed, self.rs_dual, self.recent, self.relocks = st
            self.raw = raw

    def _finish(self, frame):
        data = frame[4:].copy()
        data ^= pn()
        modes = (self.rs_dual,) if self.rs_dual is not None else (False, True)
        best = None
        for dual in modes:
            d = data.copy()
            errs = []
            for j in range(4):
                cw = np.ascontiguousarray(d[j::4])
                e = rs_decode(cw, dual)
                errs.append(e)
                if e > 0:
                    d[j::4] = cw
            if all(e >= 0 for e in errs):
                best = (d, errs, dual)
                break
            if best is None:
                best = (d, errs, dual)
        d, nerr, dual = best
        ok = all(e >= 0 for e in nerr)
        if ok and self.rs_dual is None:
            self.rs_dual = dual
        self.frames += 1
        if ok:
            self.frames_ok += 1
            self.rs_fixed += sum(nerr)
        self.recent.append(ok)
        if len(self.recent) > 50:
            self.recent.pop(0)
        return dict(data=d, ok=ok, errors=nerr)


# ---------------------------------------------------------------- VCDU / пакеты

class Demux:
    """VCDU (1020 байт без ASM) → CCSDS-пакеты VCID 5."""

    def __init__(self, vcid=5):
        self.vcid = vcid
        self.pending = None
        self.last_count = None
        self.scid = None
        self.packets = 0

    def push(self, vcdu):
        res = []
        b = vcdu
        ver = b[0] >> 6
        scid = ((b[0] & 0x3F) << 2) | (b[1] >> 6)
        vcid = b[1] & 0x3F
        if ver != 1 or vcid != self.vcid:
            return res
        self.scid = scid
        count = (int(b[2]) << 16) | (int(b[3]) << 8) | int(b[4])
        if self.last_count is not None and count != (self.last_count + 1) & 0xFFFFFF:
            self.pending = None
        self.last_count = count
        fhp = ((int(b[8]) & 0x07) << 8) | int(b[9])
        zone = bytes(b[10:10 + 882])
        if self.pending is not None:
            if fhp == 0x7FF:
                self.pending += zone
                self.pending = self._parse(self.pending, res, partial_ok=True)
                return res
            self.pending += zone[:fhp]
            self._parse(self.pending, res)
            self.pending = None
        if fhp == 0x7FF or fhp >= 882:
            return res
        self.pending = self._parse(zone[fhp:], res, partial_ok=True)
        return res

    def _parse(self, buf, res, partial_ok=False):
        i = 0
        n = len(buf)
        while n - i >= 6:
            ln = ((buf[i + 4] << 8) | buf[i + 5]) + 1
            tot = 6 + ln
            if n - i < tot:
                break
            apid = ((buf[i] & 0x07) << 8) | buf[i + 1]
            seq = ((buf[i + 2] & 0x3F) << 8) | buf[i + 3]
            if apid != 2047:
                res.append((apid, seq, buf[i + 6: i + tot]))
                self.packets += 1
            i += tot
        return buf[i:] if partial_ok else None


# ---------------------------------------------------------------- МКУ (JPEG-подобное сжатие)

_QT = np.array([16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
                14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
                18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
                49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99], np.float64)
# позиция в зигзаге для каждой естественной позиции
_ZZ = np.array([0, 1, 5, 6, 14, 15, 27, 28, 2, 4, 7, 13, 16, 26, 29, 42,
                3, 8, 12, 17, 25, 30, 41, 43, 9, 11, 18, 24, 31, 40, 44, 53,
                10, 19, 23, 32, 39, 45, 52, 54, 20, 22, 33, 38, 46, 51, 55, 60,
                21, 34, 37, 47, 50, 56, 59, 61, 35, 36, 48, 49, 57, 58, 62, 63], np.int64)

_DC_BITS = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
_DC_VALS = list(range(12))
_AC_BITS = [0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D]
_AC_VALS = [
    0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06, 0x13, 0x51, 0x61, 0x07,
    0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08, 0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0,
    0x24, 0x33, 0x62, 0x72, 0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
    0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49,
    0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69,
    0x6A, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
    0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7,
    0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5,
    0xC6, 0xC7, 0xC8, 0xC9, 0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
    0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8,
    0xF9, 0xFA]


def _huff_codes(bits, vals):
    codes = {}
    code = 0
    k = 0
    for ln in range(1, 17):
        for _ in range(bits[ln - 1]):
            codes[vals[k]] = (code, ln)
            k += 1
            code += 1
        code <<= 1
    return codes


def _huff_lut(bits, vals):
    """16-битная таблица: индекс = следующие 16 бит → (длина кода, символ); длина 0 = нет кода."""
    ln = np.zeros(65536, np.int32)
    sym = np.zeros(65536, np.int32)
    for v, (code, n) in _huff_codes(bits, vals).items():
        lo = code << (16 - n)
        hi = lo + (1 << (16 - n))
        ln[lo:hi] = n
        sym[lo:hi] = v
    return ln.tolist(), sym.tolist()


DC_CODES = _huff_codes(_DC_BITS, _DC_VALS)
AC_CODES = _huff_codes(_AC_BITS, _AC_VALS)
_DC_LUT = None
_AC_LUT = None


def _luts():
    global _DC_LUT, _AC_LUT
    if _DC_LUT is None:
        _DC_LUT = _huff_lut(_DC_BITS, _DC_VALS)
        _AC_LUT = _huff_lut(_AC_BITS, _AC_VALS)
    return _DC_LUT, _AC_LUT


def qtable(qf):
    qf = float(qf)
    f = 5000.0 / qf if 20 <= qf < 50 else 200.0 - 2.0 * qf
    t = np.floor(f / 100.0 * _QT + 0.5)
    return np.maximum(t, 1.0)


def _idct_matrix():
    m = np.zeros((8, 8))
    for u in range(8):
        cu = np.sqrt(0.5) if u == 0 else 1.0
        for x in range(8):
            m[u, x] = cu / 2.0 * np.cos((2 * x + 1) * u * np.pi / 16)
    return m


_M = _idct_matrix()


def _windows16(data):
    bits = np.unpackbits(np.frombuffer(bytes(data), np.uint8))
    nb = bits.size
    pad = np.concatenate([bits, np.zeros(32, np.uint8)]).astype(np.uint32)
    w = np.zeros(nb + 1, np.uint32)
    for k in range(16):
        w |= pad[k: k + nb + 1] << (15 - k)
    return w.tolist(), nb


def decode_mcus(data, qf):
    """Сжатые данные 14 МКУ → (массив 8×112 uint8, число декодированных МКУ)."""
    (dln, dsym), (aln, asym) = _luts()
    w, nb = _windows16(data)
    pos = 0
    dc = 0
    blocks = []
    for _ in range(14):
        if pos >= nb:
            break
        cw = w[pos]
        n = dln[cw]
        if n == 0:
            break
        cat = dsym[cw]
        pos += n
        if cat:
            if pos + cat > nb:
                break
            v = w[pos] >> (16 - cat)
            if v < (1 << (cat - 1)):
                v -= (1 << cat) - 1
            pos += cat
        else:
            v = 0
        dc += v
        coef = [0] * 64
        coef[0] = dc
        k = 1
        bad = False
        while k < 64:
            if pos >= nb:
                bad = True
                break
            cw = w[pos]
            n = aln[cw]
            if n == 0:
                bad = True
                break
            rs = asym[cw]
            pos += n
            if rs == 0x00:      # EOB
                break
            run, size = rs >> 4, rs & 15
            if size == 0:       # ZRL (F0)
                k += 16
                continue
            k += run
            if pos + size > nb:
                bad = True
                break
            v = w[pos] >> (16 - size)
            if v < (1 << (size - 1)):
                v -= (1 << size) - 1
            pos += size
            if k < 64:
                coef[k] = v
            k += 1
        if bad:
            break
        blocks.append(coef)
    out = np.zeros((8, 112), np.uint8)
    if not blocks:
        return out, 0
    z = np.array(blocks, np.float64)                 # (n, 64) в порядке зигзага
    nat = z[:, _ZZ] * qtable(qf)[None, :]            # естественный порядок
    F = nat.reshape(-1, 8, 8)
    px = np.einsum("vr,nvu,uc->nrc", _M, F, _M)
    px = np.clip(np.round(px) + 128, 0, 255).astype(np.uint8)
    for i in range(px.shape[0]):
        out[:, i * 8:(i + 1) * 8] = px[i]
    return out, px.shape[0]


class Imager:
    """CCSDS-пакеты APID 64–69 → каналы МСУ-МР."""

    def __init__(self):
        self.seg = {a: {} for a in APIDS}   # apid → {база счётчика: (8×112, mcun)}
        self.rollover = 0
        self.last_seq = None
        self.count = 0
        self.qf = 0
        self.version = 0
        self.first_time = None
        self.last_time = None

    def push(self, apid, seq, payload):
        if apid not in self.seg or len(payload) < 15:
            return
        p = payload
        mcun = p[8]
        if p[9] != 0 or p[10] != 0 or p[11] != 0xFF or p[12] != 0xF0 or mcun % 14 or mcun > 182:
            return
        if self.last_seq is not None and seq < self.last_seq and self.last_seq - seq > 8192:
            self.rollover += 16384
        self.last_seq = seq
        img, n = decode_mcus(p[14:], p[13])
        if n == 0:
            return
        base = seq + self.rollover - mcun // 14
        self.seg[apid][(base, mcun)] = img
        self.count += 1
        self.qf = p[13]
        ms = struct.unpack(">I", bytes(p[2:6]))[0]
        if self.first_time is None:
            self.first_time = ms
        self.last_time = ms
        self.version += 1

    def channels(self):
        return [a for a in APIDS if self.seg[a]]

    def lines(self):
        _, n0, n1 = self._layout()
        return 0 if n0 is None else (n1 - n0 + 1) * 8

    def _layout(self):
        chans = self.channels()
        if not chans:
            return None, None, None
        cyc = 14 * len(chans) + 1      # цикл передачи: по 14 пакетов на канал + телеметрия (обычно 43)
        ref = chans[0]
        ph = [b % cyc for (b, _m) in self.seg[ref]]
        phase0 = max(set(ph), key=ph.count)
        n0, n1 = None, None
        for a in chans:
            for (b, _m) in self.seg[a]:
                ln = (b - phase0) // cyc
                n0 = ln if n0 is None or ln < n0 else n0
                n1 = ln if n1 is None or ln > n1 else n1
        if n1 - n0 > 3000:      # мусор в счётчиках — ограничим размер
            n0 = n1 - 3000
        return (phase0, cyc), n0, n1

    def image(self, apid):
        ph, n0, n1 = self._layout()
        if ph is None or not self.seg.get(apid):
            return None
        phase0, cyc = ph
        h = (n1 - n0 + 1) * 8
        img = np.zeros((h, WIDTH), np.uint8)
        for (b, mcun), seg in self.seg[apid].items():
            ln = (b - phase0) // cyc - n0
            if ln < 0:
                continue
            x = (mcun // 14) * 112
            img[ln * 8: ln * 8 + 8, x: x + 112] = seg
        return img

    def all_images(self):
        return {a: self.image(a) for a in self.channels()}


def composite(images, combo):
    """combo — строка из трёх цифр каналов (1..6), например "123", "125", "221"."""
    ch = []
    for c in combo:
        a = 63 + int(c)
        if a not in images or images[a] is None:
            return None
        ch.append(images[a])
    h = min(x.shape[0] for x in ch)
    return np.dstack([x[:h] for x in ch])


def preview(images, key, do_stretch=True):
    """Снимок для показа: key — номер канала ("1".."6") или комбинация ("221", "125", "123").
    Если нужных каналов нет — подбирается похожий композит. Возвращает (массив, использованный ключ)."""
    if not images:
        return None, key
    if len(key) == 3:
        arr = composite(images, key)
        if arr is None:
            have = "".join(str(a - 63) for a in sorted(images))
            alt = "125" if all(c in have for c in "125") else None
            if alt is None and len(have) >= 3:
                alt = have[1] + have[1] + have[0]
            arr = composite(images, alt) if alt else None
            key = alt or key
        if arr is not None and do_stretch:
            arr = np.dstack([stretch(arr[:, :, i]) for i in range(3)])
        return arr, key
    arr = images.get(63 + int(key))
    if arr is not None and do_stretch:
        arr = stretch(arr)
    return arr, key


def stretch(img):
    """Растяжка уровней по процентилям (для показа). Процентили — по прореженной выборке (быстро)."""
    if img is None or img.size == 0:
        return img
    sub = img.ravel()[:: max(1, img.size // 200000)]
    nz = sub[sub > 0]
    if nz.size < 100:
        return img
    lo, hi = np.percentile(nz, (0.5, 99.5))
    if hi <= lo:
        return img
    lut = np.clip((np.arange(256, dtype=np.float32) - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)
    return lut[img]


# ---------------------------------------------------------------- приёмник целиком

class PskReceiver:
    """Общая часть приёмников PSK: демодулятор, грубый поиск частоты, «передачи» Костаса, кадры CADU."""

    def __init__(self, fs, rs, mode, nrzm=None, max_hz=10000.0):
        self.demod = Demodulator(fs, rs, mode=mode)
        self.frames = FrameDecoder(nrzm)
        self.max_hz = max_hz
        self.unlocked_syms = 0
        self.coarse_hz = 0.0
        self.snr_db = 0.0
        self.last_cons = np.zeros((0, 2), np.float32)
        self._acq = True
        self._wide = None
        self.dint = None             # Deinterleaver80k для режима 80k с перемежением

    def set_offset(self, hz):
        self.demod.set_offset(hz)

    def feed_frames(self, iq, want_cons=True):
        """IQ → список кадров (dict data/ok/errors)."""
        x = self.demod.filter(iq)
        if x.size == 0:
            return []
        st = self.demod.state()
        need_acq = self._acq or (not self.frames.locked and self.unlocked_syms > 2.0 * self.demod.rs)
        if need_acq and self.demod.mixed_n >= self.demod.fs * 0.3:
            c, snr = coarse_offset(np.concatenate(self.demod.mixed), self.demod.fs, self.demod.rs, self.max_hz)
            self.coarse_hz, self.snr_db = c, snr
            if snr > -3.0 and (self._acq or abs(c - st["residual_hz"]) > 300):
                self.demod.set_residual(c)
            self._acq = False
            self.unlocked_syms = 0
        wide = not self.frames.locked
        if wide != self._wide:          # «переключение передач» петли Костаса
            self._wide = wide
            self.demod.set_costas_bw(0.012 if wide else 0.005)
        n0 = self.demod.nsym
        soft, cons = self.demod.process_fs2(x, want_cons)
        st = self.demod.state()
        if st["lock"] < 0.3 and not self.frames.locked:
            self.unlocked_syms += self.demod.nsym - n0
        else:
            self.unlocked_syms = 0
        if cons is not None and cons.shape[0]:
            self.last_cons = cons[-1500:]
        if self.dint is not None:
            soft = self.dint.push(soft)
        return self.frames.push(soft)

    def finish_frames(self):
        return self.frames.flush()

    def base_stats(self):
        st = self.demod.state()
        f = self.frames
        return dict(carrier=st["lock"], residual_hz=st["residual_hz"] + self.demod.offset_hz,
                    omega=st["omega"], sync=f.synced, frames=f.frames, frames_ok=f.frames_ok,
                    rs_pct=f.rs_ratio(), rs_fixed=f.rs_fixed, snr_db=self.snr_db, nrzm=f.nrzm, hyp=f.hyp)


class Deinterleaver80k:
    """Режим «80k с перемежением» «Метеор-М» №2-2/2-3/2-4 (SatDump meteor_m2-x_lrpt_80k, meteor_decode).

    Поток мягких бит (I, Q, I, Q…) 80 000 символ/с: каждые 80 бит — маркер 0x27 (8 бит) + 72 бита данных.
    Данные прошли свёрточный перемежитель: 36 ветвей, шаг задержки 2048·36 бит. Обратная операция:
    out[j] = in[j − (35 − j mod 36)·36·2048] — постоянная задержка 35·36·2048 бит (≈ 16 с).
    Поворот фазы и сдвиг на бит определяются по маркеру ДО обратного перемежения (после него соседние
    биты приходят из разных мест потока, и снять неоднозначность уже нельзя).
    """

    STRIDE, MARK_LEN, BRANCHES, DELAY = 80, 8, 36, 2048
    MARKER = 0x27
    SPAN = 35 * 36 * 2048

    def __init__(self):
        self.mark = np.array([(self.MARKER >> (7 - i)) & 1 for i in range(8)], np.uint8)
        self.raw = np.zeros(0, np.int8)        # до захвата маркера
        self.t = np.zeros(0, np.int8)          # после поворота, выровнено по началу кадра 80 бит
        self.carry = np.zeros(0, np.int8)      # непарный мягкий бит между вызовами
        self.locked = False
        self.rot = 0
        self.data = np.zeros(0, np.int8)       # данные без маркеров (история для обратного перемежения)
        self.base = 0                          # абсолютный номер data[0]
        self.total = 0                         # всего бит данных
        self.k_next = self.SPAN                # первый выход, у которого вся история есть
        self.marker_ok = 0.0
        self.relocks = 0

    @staticmethod
    def _rot(soft, r):
        """8 вариантов неоднозначности QPSK: поворот на 90°·(r & 3), сопряжение (r & 4)."""
        s = soft[: soft.size // 2 * 2].astype(np.int16)
        a, b = s[0::2], s[1::2]
        if r & 4:
            b = -b
        for _ in range(r & 3):
            a, b = -b, a
        out = np.empty(a.size * 2, np.int8)
        out[0::2] = np.clip(a, -127, 127)
        out[1::2] = np.clip(b, -127, 127)
        return out

    def _search(self, raw):
        m = raw.size // self.STRIDE - 2
        if m < 40:
            return None
        best = None
        for r in range(8):
            bits = (self._rot(raw, r) > 0).astype(np.uint8)
            for o in range(self.STRIDE):
                blk = bits[o: o + m * self.STRIDE].reshape(m, self.STRIDE)[:, :8]
                score = int(np.count_nonzero(blk == self.mark))
                if best is None or score > best[0]:
                    best = (score, r, o)
        if best[0] < 0.85 * 8 * m:
            return None
        return best

    def push(self, soft):
        out = []
        if not self.locked:
            self.raw = np.concatenate([self.raw, soft])
            if self.raw.size < self.STRIDE * 64:
                return np.zeros(0, np.int8)
            found = self._search(self.raw[: self.STRIDE * 120])
            if found is None:
                self.raw = self.raw[-self.STRIDE * 8:] if self.raw.size > self.STRIDE * 160 else self.raw
                return np.zeros(0, np.int8)
            _score, self.rot, o = found
            even = self.raw[: self.raw.size // 2 * 2]
            self.carry = self.raw[even.size:]
            self.t = self._rot(even, self.rot)[o:]
            self.raw = np.zeros(0, np.int8)
            self.locked = True
            self.relocks += 1
        else:
            s2 = np.concatenate([self.carry, soft])
            even = s2[: s2.size // 2 * 2]
            self.carry = s2[even.size:]
            self.t = np.concatenate([self.t, self._rot(even, self.rot)])
        m = self.t.size // self.STRIDE
        if m:
            fr = self.t[: m * self.STRIDE].reshape(m, self.STRIDE)
            self.t = self.t[m * self.STRIDE:]
            ok = float(np.mean((fr[:, :8] > 0) == self.mark.astype(bool)))
            self.marker_ok = 0.8 * self.marker_ok + 0.2 * ok if self.marker_ok else ok
            if self.marker_ok < 0.7:                  # маркер потерян — ищем заново (поворот/сдвиг)
                self.locked = False
                self.marker_ok = 0.0
                self.raw = np.concatenate([fr.ravel(), self.t])
                self.t = np.zeros(0, np.int8)
                return np.zeros(0, np.int8)
            self.data = np.concatenate([self.data, fr[:, 8:].ravel()])
            self.total += m * (self.STRIDE - 8)
        if self.total > self.k_next:
            ks = np.arange(self.k_next, self.total, dtype=np.int64)
            idx = ks - (35 - ks % 36) * (36 * self.DELAY) - self.base
            out = self.data[idx]
            self.k_next = self.total
            keep = self.SPAN + 64
            if self.data.size > keep * 2:
                drop = self.data.size - keep
                self.data = self.data[drop:]
                self.base += drop
            return out
        return np.zeros(0, np.int8)


def interleave80k(coded_bits):
    """Передатчик (для тестов): перемежение и вставка маркеров. coded_bits — 0/1 после свёрточного кода,
    длина кратна 72. Возвращает биты канала (кратно 80)."""
    D = Deinterleaver80k
    x = np.asarray(coded_bits, np.uint8)
    i = np.arange(x.size, dtype=np.int64)
    src = i - (i % 36) * (36 * D.DELAY)
    sent = np.where(src >= 0, x[np.clip(src, 0, None)], 0).astype(np.uint8)
    m = sent.size // 72
    mark = np.array([(D.MARKER >> (7 - k)) & 1 for k in range(8)], np.uint8)
    fr = np.concatenate([np.tile(mark, (m, 1)), sent[: m * 72].reshape(m, 72)], axis=1)
    return fr.ravel()


class LrptReceiver(PskReceiver):
    """IQ → изображения МСУ-МР. feed() можно вызывать из потока приёма.
    mode80k=True — 80 000 символ/с с перемежением (иначе 72 000 без перемежения)."""

    def __init__(self, fs, oqpsk=True, rs=None, nrzm=None, mode80k=False):
        rs = rs or (80000 if mode80k else SYMBOL_RATE)
        super().__init__(fs, rs, "oqpsk" if oqpsk else "qpsk", nrzm)
        self.mode80k = mode80k
        if mode80k:
            self.dint = Deinterleaver80k()
        self.demux = Demux(5)
        self.imager = Imager()

    @property
    def mode_text(self):
        return f"{int(self.demod.rs / 1000)}k {'OQPSK' if self.demod.oqpsk else 'QPSK'}" + \
            (" + перемежение" if self.mode80k else "")

    def _use(self, frames):
        for fr in frames:
            if fr["ok"]:
                for apid, seq, pl in self.demux.push(fr["data"][:892]):
                    self.imager.push(apid, seq, pl)

    def feed(self, iq, want_cons=True):
        self._use(self.feed_frames(iq, want_cons))

    def finish(self):
        self._use(self.finish_frames())

    def stats(self):
        st = self.base_stats()
        st.update(packets=self.demux.packets, lines=self.imager.lines(), channels=self.imager.channels(),
                  scid=self.demux.scid, mode=self.mode_text,
                  marker=(self.dint.marker_ok if self.dint is not None else None))
        return st


class AutoLrptReceiver:
    """Автоопределение режима LRPT в живом приёме: параллельно 72k OQPSK и 80k OQPSK с перемежением
    (плюс, если нужно, QPSK), пока один не наберёт уверенных кадров; дальше работает только он."""

    def __init__(self, fs, modes=((True, False), (True, True))):
        self.rxs = [LrptReceiver(fs, oq, mode80k=m80) for oq, m80 in modes]
        self.chosen = None
        self._offset = 0.0

    @property
    def rx(self):
        return self.chosen or max(self.rxs, key=lambda r: (r.frames.frames_ok, r.demod.state()["lock"]))

    def __getattr__(self, name):        # imager, demux, frames, demod, last_cons… — у текущего лучшего
        return getattr(self.rx, name)

    def set_offset(self, hz):
        self._offset = hz
        for r in self.rxs:
            r.set_offset(hz)

    def feed(self, iq, want_cons=True):
        if self.chosen is not None:
            self.chosen.feed(iq, want_cons)
            return
        for r in self.rxs:
            r.feed(iq, want_cons)
        good = [r for r in self.rxs if r.frames.frames_ok >= 5 and r.frames.frames_ok >= 0.6 * r.frames.frames]
        if good:
            self.chosen = good[0]
            self.rxs = [self.chosen]

    def finish(self):
        for r in self.rxs:
            r.finish()

    def stats(self):
        st = self.rx.stats()
        st["mode"] = self.rx.mode_text if self.chosen else "поиск режима: " + " / ".join(r.mode_text for r in self.rxs)
        return st


# ---------------------------------------------------------------- файлы IQ

FORMATS = {"cu8": (np.uint8, 127.5, 127.5), "cs8": (np.int8, 0.0, 128.0),
           "cs16": (np.int16, 0.0, 32768.0), "cf32": (np.float32, 0.0, 1.0)}


def guess_format(path):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return {"wav": "wav", "cu8": "cu8", "raw": "cu8", "bin": "cu8", "u8": "cu8", "cs8": "cs8", "s8": "cs8",
            "cs16": "cs16", "s16": "cs16", "sc16": "cs16", "cf32": "cf32", "fc32": "cf32", "f32": "cf32",
            "iq": "cf32"}.get(ext, "cu8")


def wav_info(path):
    """(fs, формат, смещение данных, число байт) для 2-канального WAV (PCM8/16, float32)."""
    with open(path, "rb") as f:
        hdr = f.read(12)
        if hdr[:4] not in (b"RIFF", b"RF64") or hdr[8:12] != b"WAVE":
            raise ValueError("Не WAV-файл")
        fmt = None
        while True:
            ch = f.read(8)
            if len(ch) < 8:
                raise ValueError("В WAV нет блока данных")
            cid, size = ch[:4], struct.unpack("<I", ch[4:])[0]
            if cid == b"fmt ":
                d = f.read(size)
                tag, nch, rate, _br, _ba, bits = struct.unpack("<HHIIHH", d[:16])
                if tag == 0xFFFE and len(d) >= 26:
                    tag = struct.unpack("<H", d[24:26])[0]
                fmt = (tag, nch, rate, bits)
                if size & 1:
                    f.read(1)
            elif cid == b"data":
                off = f.tell()
                total = os.path.getsize(path) - off
                if size not in (0, 0xFFFFFFFF):
                    total = min(total, size)
                break
            else:
                f.seek(size + (size & 1), 1)
    if fmt is None:
        raise ValueError("В WAV нет блока fmt")
    tag, nch, rate, bits = fmt
    if nch != 2:
        raise ValueError(f"WAV должен быть 2-канальным (I/Q), а в нём каналов: {nch}")
    if tag == 3 and bits == 32:
        k = "cf32"
    elif tag == 1 and bits == 16:
        k = "cs16"
    elif tag == 1 and bits == 8:
        k = "cu8"
    else:
        raise ValueError(f"Неподдерживаемый формат WAV (тег {tag}, {bits} бит)")
    return rate, k, off, total


def iter_iq(path, fmt=None, block=262144):
    """Генератор блоков complex64 и полная длина в отсчётах (первый yield — число отсчётов)."""
    fmt = fmt or guess_format(path)
    if fmt == "wav":
        _fs, k, off, nbytes = wav_info(path)
    else:
        k, off, nbytes = fmt, 0, os.path.getsize(path)
    dt, bias, scale = FORMATS[k]
    item = np.dtype(dt).itemsize * 2
    n = nbytes // item
    mm = np.memmap(path, dtype=dt, mode="r", offset=off, shape=(n * 2,))
    yield n
    for i in range(0, n, block):
        a = np.asarray(mm[2 * i: 2 * min(n, i + block)], np.float32)
        if bias:
            a = a - bias
        a = a / scale
        yield (a[0::2] + 1j * a[1::2]).astype(np.complex64)


def offset_from_name(path, fs, freqs):
    """Смещение сигнала от центра записи по имени файла RtlTV «…_<fs>Hz_<центр>.cu8» (или None)."""
    m = re.search(r"_(\d{5,8})Hz_(\d{7,11})", os.path.basename(path))
    if not m:
        return None
    center = float(m.group(2))
    for f in freqs:
        if abs(f - center) < fs / 2 - 50e3:
            return f - center
    return None


def find_signal(path, fs, fmt=None, rs=SYMBOL_RATE, nblocks=48):
    """Поиск сигнала LRPT в записи: средний спектр по всему файлу, «полка» шириной ≈1,5·Rs.
    Возвращает (смещение от центра, Гц; превышение над шумом, дБ)."""
    if fs < 4 * rs:            # узкая запись — сигнал и так в центре
        return 0.0, 0.0
    fmt = fmt or guess_format(path)
    if fmt == "wav":
        _fs, k, off, nbytes = wav_info(path)
    else:
        k, off, nbytes = fmt, 0, os.path.getsize(path)
    dt, bias, scale = FORMATS[k]
    item = np.dtype(dt).itemsize * 2
    n = nbytes // item
    mm = np.memmap(path, dtype=dt, mode="r", offset=off, shape=(n * 2,))
    nfft = 1 << int(np.ceil(np.log2(fs / 500.0)))
    blk = nfft * 8
    if n < blk:
        return 0.0, 0.0
    acc = np.zeros(nfft)
    for st in np.linspace(0, n - blk, nblocks).astype(np.int64):
        a = (np.asarray(mm[2 * st: 2 * (st + blk)], np.float32) - bias) / scale
        x = a[0::2] + 1j * a[1::2]
        _f, p = sps.welch(x, fs=fs, nperseg=nfft, return_onesided=False, detrend=False)
        acc += p / (np.median(p) + 1e-30)
    p = np.fft.fftshift(acc)
    f = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / fs))
    df = f[1] - f[0]
    w = max(3, int(rs * 1.2 / df))
    inner = np.convolve(p, np.ones(w) / w, mode="same")
    outer = np.convolve(p, np.ones(3 * w) / (3 * w), mode="same")
    ring = (outer * 3 - inner) / 2                  # среднее по краям «полки»
    score = inner / (ring + 1e-30)
    edge = np.abs(f) <= fs / 2 - rs
    i = int(np.argmax(np.where(edge, score, 0)))
    return float(f[i]), float(10 * np.log10(max(score[i], 1e-9)))


def decode_file(path, fs=None, fmt=None, oqpsk=None, progress=None, cancel=None, offset_hz=None, mode80k=None):
    """Декодировать запись. oqpsk=None / mode80k=None — режимы перебираются параллельно (72k OQPSK, 72k QPSK,
    80k OQPSK с перемежением), пока один не победит. offset_hz=None — сигнал ищется сам. Возвращает LrptReceiver."""
    fmt = fmt or guess_format(path)
    if fmt == "wav":
        wfs = wav_info(path)[0]
        fs = fs or wfs
    if not fs:
        raise ValueError("Укажите частоту дискретизации записи")
    if offset_hz is None:
        offset_hz = offset_from_name(path, fs, (137.9e6, 137.1e6))
    if offset_hz is None:
        off, gain_db = find_signal(path, fs, fmt)
        offset_hz = off if gain_db > 1.0 and abs(off) > 8000 else 0.0
    oqs = [oqpsk] if oqpsk is not None else [True, False]
    m80 = [mode80k] if mode80k is not None else [False, True]
    modes = [(o, m) for m in m80 for o in oqs if not (m and not o)]      # 80k бывает только OQPSK
    # при «авто» демодуляторы работают параллельно, пока один не наберёт уверенных кадров
    rxs = []
    for o, m in modes:
        rx = LrptReceiver(fs, o, mode80k=m)
        rx.set_offset(offset_hz)
        rxs.append(rx)
    it = iter_iq(path, fmt)
    n = next(it)
    done = 0
    for blk in it:
        if cancel is not None and cancel():
            break
        for rx in rxs:
            rx.feed(blk, want_cons=False)
        done += blk.size
        if len(rxs) > 1:
            good = [rx for rx in rxs if rx.frames.frames_ok >= 5 and rx.frames.frames_ok >= 0.6 * rx.frames.frames]
            if good:
                rxs = good[:1]
        if progress:
            progress(done / max(n, 1), max(rxs, key=lambda r: r.frames.frames_ok))
    best = max(rxs, key=lambda r: r.frames.frames_ok)
    best.finish()
    return best


# ---------------------------------------------------------------- синтетический передатчик (тесты, демо)

def _bits_val(v, n):
    """Код значения коэффициента JPEG длиной n бит."""
    if v < 0:
        v += (1 << n) - 1
    return format(v, f"0{n}b") if n else ""


def encode_segment(strip, qf=80):
    """Полоса 8×112 → сжатые данные 14 МКУ (только DC + EOB: усреднение блоков 8×8)."""
    q0 = qtable(qf)[0]
    bits = []
    prev = 0
    for i in range(14):
        blk = strip[:, i * 8:(i + 1) * 8].astype(np.float64) - 128.0
        dc = int(round(blk.mean() * 8.0 / q0))       # F(0,0) = 8·среднее
        d = dc - prev
        prev = dc
        cat = 0 if d == 0 else int(abs(d)).bit_length()
        code, ln = DC_CODES[cat]
        bits.append(format(code, f"0{ln}b") + _bits_val(d, cat))
        code, ln = AC_CODES[0x00]                    # EOB
        bits.append(format(code, f"0{ln}b"))
    s = "".join(bits)
    s += "1" * (-len(s) % 8)
    return int(s, 2).to_bytes(len(s) // 8, "big")


def synth_image(h, seed=0):
    """Тестовая картинка h×1568: градиенты, круги, сетка."""
    y, x = np.mgrid[0:h, 0:WIDTH].astype(np.float32)
    img = 60 + 80 * (x / WIDTH) + 40 * np.sin(y / 37.0 + seed)
    for cx, cy, r in ((400, h * 0.3, 120), (1100, h * 0.6, 200), (800, h * 0.8, 90)):
        img = np.where((x - cx) ** 2 + (y - cy) ** 2 < r * r, 230 - seed * 20, img)
    img = np.where(((x // 196) % 2 == 0) & ((y // 64) % 2 == 0), img * 0.8, img)
    return np.clip(img, 0, 255).astype(np.uint8)


def synth_packets(images, line0=0, seq0=0, t0_ms=36000000, qf=80):
    """{apid: картинка h×1568} → список CCSDS-пакетов (bytes) в порядке передачи (цикл 14·N+1 пакетов)."""
    pk = []
    seq = seq0
    h = min(im.shape[0] for im in images.values()) // 8
    for ln in range(h):
        ms = t0_ms + (line0 + ln) * 1230
        for apid in sorted(images):
            strip_all = images[apid][ln * 8:(ln + 1) * 8]
            for m in range(14):
                hdr = struct.pack(">HIH", 0, ms, 0) + bytes([m * 14, 0, 0, 0xFF, 0xF0, qf])
                body = hdr + encode_segment(strip_all[:, m * 112:(m + 1) * 112], qf)
                pk.append(struct.pack(">HHH", 0x0800 | apid, 0xC000 | (seq & 0x3FFF), len(body) - 1) + body)
                seq += 1
        body = bytes(20)
        pk.append(struct.pack(">HHH", 0x0800 | 70, 0xC000 | (seq & 0x3FFF), len(body) - 1) + body)
        seq += 1
    return pk


def packets_to_vcdus(packets, scid=0, vcid=5, count0=0):
    """Пакеты → VCDU по 892 байта (заголовок 6 + вставка 2 + M_PDU 2 + 882)."""
    stream = bytearray()
    starts = []
    for p in packets:
        starts.append(len(stream))
        stream += p
    stream += bytes([0xFF]) * (-len(stream) % 882)     # заполнитель: заголовок APID 2047 (пустой пакет)
    out = []
    si = 0
    for k in range(len(stream) // 882):
        a, b = k * 882, (k + 1) * 882
        while si < len(starts) and starts[si] < a:
            si += 1
        fhp = starts[si] - a if si < len(starts) and starts[si] < b else 0x7FF
        cnt = count0 + k
        hdr = bytes([0x40 | (scid >> 2), ((scid & 3) << 6) | vcid, (cnt >> 16) & 255, (cnt >> 8) & 255, cnt & 255,
                     0, 0, 0, (fhp >> 8) & 7, fhp & 255])
        out.append(np.frombuffer(hdr + bytes(stream[a:b]), np.uint8))
    return out


def vcdus_to_cadus(vcdus, dual=False):
    asm = np.frombuffer(ASM.to_bytes(4, "big"), np.uint8)
    out = []
    for vc in vcdus:
        block = np.zeros(1020, np.uint8)
        for j in range(4):
            d = np.ascontiguousarray(vc[j::4])
            block[j::4] = np.concatenate([d, rs_encode(d, dual)])
        block ^= pn()
        out.append(np.concatenate([asm, block]))
    return out


class Modulator:
    """CADU → комплексный сигнал LRPT (OQPSK/QPSK, RRC α=0,5) с непрерывным состоянием между вызовами."""

    def __init__(self, sps_=16, oqpsk=True, nrzm=True, alpha=0.5):
        self.sps = int(sps_)
        self.oqpsk = oqpsk
        self.nrzm = nrzm
        self.h = rrc_taps(self.sps, alpha, 12)
        self.tail = np.zeros(self.h.size - 1, np.complex64)
        self.conv_state = 0
        self.nrzm_prev = 0

    def modulate(self, cadus):
        bits = np.unpackbits(np.concatenate(cadus))
        if self.nrzm:
            bits = nrzm_encode(bits, self.nrzm_prev)
            self.nrzm_prev = int(bits[-1])
        enc, self.conv_state = conv_encode(bits, self.conv_state)
        i = enc[0::2].astype(np.float32) * 2 - 1
        q = enc[1::2].astype(np.float32) * 2 - 1
        n = i.size * self.sps
        x = np.zeros(n, np.complex64)
        x[0:n:self.sps] = i
        off = self.sps // 2 if self.oqpsk else 0
        x[off:n:self.sps] += 1j * q
        y = sps.oaconvolve(x, self.h).astype(np.complex64)
        y[: self.tail.size] += self.tail
        self.tail = y[n:].copy()
        return y[:n]
