"""SSTV: потоковый декодер и кодер (чистые numpy/scipy, без GUI и без донгла).

Вход декодера — звук float32 моно после SSB/FM-демодулятора. Цепочка:
  перенос на 1700 Гц → ФНЧ (комплексный, ±1100 Гц) → прореживание до ~12 кГц →
  квадратурный частотный детектор (угол z[n]·conj(z[n-1])) → трек частоты.
По треку: детектор VIS (лидер 1900, старт-бит 1200, биты 1100/1300, чётность),
поиск строчных синхроимпульсов 1200 Гц, робастная линейная подгонка позиций синхро
(наклон = разница тактовых частот), коррекция расстройки по лидеру и синхро,
усреднение частоты по длительности пикселя.

Пример:
    dec = SstvDecoder(48000)
    for block in blocks:
        for ev in dec.process(block):
            ...
"""
from __future__ import annotations

import math

import numpy as np
from scipy.signal import firwin, lfilter

__all__ = ["MODES", "SstvDecoder", "encode", "decode_file", "mode_duration"]

F_SYNC = 1200.0
F_BLACK = 1500.0
F_WHITE = 2300.0
F_LEADER = 1900.0
F_BIT1 = 1100.0
F_BIT0 = 1300.0
F_SPAN = F_WHITE - F_BLACK

# ---------------------------------------------------------------- режимы ----

_CHANNELS = ("R", "G", "B", "Y", "Y0", "Y1", "U", "V", "C")


def _martin(ch):
    return [("sync", 4.862, F_SYNC), ("gap", 0.572, F_BLACK),
            ("G", ch, None), ("gap", 0.572, F_BLACK),
            ("B", ch, None), ("gap", 0.572, F_BLACK),
            ("R", ch, None), ("gap", 0.572, F_BLACK)]


def _scottie(ch):
    return [("gap", 1.5, F_BLACK), ("G", ch, None), ("gap", 1.5, F_BLACK),
            ("B", ch, None), ("sync", 9.0, F_SYNC), ("gap", 1.5, F_BLACK),
            ("R", ch, None)]


def _pd(ch):
    return [("sync", 20.0, F_SYNC), ("gap", 2.08, F_BLACK),
            ("Y0", ch, None), ("V", ch, None), ("U", ch, None), ("Y1", ch, None)]


_ROBOT36 = [("sync", 9.0, F_SYNC), ("gap", 3.0, F_BLACK), ("Y", 88.0, None),
            ("sepC", 4.5, None), ("gap", 1.5, F_LEADER), ("C", 44.0, None)]
_ROBOT72 = [("sync", 9.0, F_SYNC), ("gap", 3.0, F_BLACK), ("Y", 138.0, None),
            ("gap", 4.5, F_BLACK), ("gap", 1.5, F_LEADER), ("V", 69.0, None),
            ("gap", 4.5, F_WHITE), ("gap", 1.5, F_BLACK), ("U", 69.0, None)]
_WRAASE180 = [("sync", 5.5225, F_SYNC), ("gap", 0.5, F_BLACK),
              ("R", 235.0, None), ("G", 235.0, None), ("B", 235.0, None)]


def _mk(name, vis, w, h, layout, color, prefix=(), rows_per_line=1):
    """Собирает описание режима из раскладки строки (длительности в мс)."""
    layout = [(k, d / 1000.0, f) for k, d, f in layout]
    prefix = [(k, d / 1000.0, f) for k, d, f in prefix]
    t = 0.0
    sync_at = None
    starts = []
    for k, d, f in layout:
        starts.append(t)
        if k == "sync" and sync_at is None:
            sync_at = t
        t += d
    line = t
    chans, sep = [], None
    for (k, d, f), st in zip(layout, starts):
        if k in _CHANNELS:
            chans.append((k, st - sync_at, d))
        elif k == "sepC":
            sep = (st - sync_at, d)
    lines = h // rows_per_line
    pre = sum(d for _, d, _ in prefix)
    return {
        "name": name, "vis": vis, "width": w, "height": h, "color": color,
        "line": line, "sync": next(d for k, d, _ in layout if k == "sync"),
        "lines": lines, "rows_per_line": rows_per_line,
        "layout": layout, "prefix": prefix,
        "channels": chans, "sep": sep,
        "first_sync": pre + sync_at,
        "duration": 0.91 + pre + lines * line,
    }


MODES: dict = {}
for _m in (
    _mk("Robot 36", 8, 320, 240, _ROBOT36, "r36"),
    _mk("Robot 72", 12, 320, 240, _ROBOT72, "r72"),
    _mk("Martin M1", 44, 320, 256, _martin(146.432), "rgb"),
    _mk("Martin M2", 40, 320, 256, _martin(73.216), "rgb"),
    _mk("Scottie S1", 60, 320, 256, _scottie(138.24), "rgb", [("sync", 9.0, F_SYNC)]),
    _mk("Scottie S2", 56, 320, 256, _scottie(88.064), "rgb", [("sync", 9.0, F_SYNC)]),
    _mk("Scottie DX", 76, 320, 256, _scottie(345.6), "rgb", [("sync", 9.0, F_SYNC)]),
    _mk("PD 50", 93, 320, 256, _pd(91.52), "pd", rows_per_line=2),
    _mk("PD 90", 99, 320, 256, _pd(170.24), "pd", rows_per_line=2),
    _mk("PD 120", 95, 640, 496, _pd(121.6), "pd", rows_per_line=2),
    _mk("PD 160", 98, 512, 400, _pd(195.584), "pd", rows_per_line=2),
    _mk("PD 180", 96, 640, 496, _pd(183.04), "pd", rows_per_line=2),
    _mk("PD 240", 97, 640, 496, _pd(244.48), "pd", rows_per_line=2),
    _mk("PD 290", 94, 800, 616, _pd(228.8), "pd", rows_per_line=2),
    _mk("Wraase SC2-180", 55, 320, 256, _WRAASE180, "rgb"),
):
    MODES[_m["name"]] = _m
del _m

_VIS2MODE = {m["vis"]: name for name, m in MODES.items()}


def mode_duration(mode: str) -> float:
    """Полная длительность передачи (VIS + картинка), с."""
    return MODES[mode]["duration"]


# ------------------------------------------------------------- цвет ---------

def _rgb_to_ycc(img):
    r, g, b = img[..., 0], img[..., 1], img[..., 2]
    y = 16.0 + 0.256788 * r + 0.504129 * g + 0.097906 * b
    u = 128.0 - 0.148223 * r - 0.290993 * g + 0.439216 * b
    v = 128.0 + 0.439216 * r - 0.367788 * g - 0.071427 * b
    return y, u, v


def _ycc_to_rgb(y, u, v):
    y = 1.164383 * (np.asarray(y, np.float32) - 16.0)
    u = np.asarray(u, np.float32) - 128.0
    v = np.asarray(v, np.float32) - 128.0
    r = y + 1.596027 * v
    g = y - 0.391762 * u - 0.812968 * v
    b = y + 2.017232 * u
    return np.clip(np.stack([r, g, b], -1) + 0.5, 0, 255).astype(np.uint8)


def _fit_image(image, w, h):
    """Приводит картинку к размеру режима (ближайший сосед), float32 0..255, RGB."""
    img = np.asarray(image)
    if img.ndim == 2:
        img = np.stack([img] * 3, -1)
    img = img[..., :3]
    if img.dtype != np.uint8 and img.size and float(np.nanmax(img)) <= 1.0:
        img = img * 255.0
    img = img.astype(np.float32)
    if img.shape[:2] != (h, w):
        yi = (np.arange(h) * img.shape[0] / h).astype(int)
        xi = (np.arange(w) * img.shape[1] / w).astype(int)
        img = img[yi][:, xi]
    return np.clip(img, 0, 255)


# ------------------------------------------------------------- кодер --------

def encode(image_rgb: np.ndarray, mode: str, fs=48000, freq_offset: float = 0.0,
           amplitude: float = 0.5) -> np.ndarray:
    """Кодирует картинку (H×W×3, uint8 или float 0..1/0..255) в звук SSTV float32.

    Передача: лидер 1900 Гц 300 мс, разрыв 1200 Гц 10 мс, лидер 300 мс, VIS
    (старт-бит, 7 бит младшим вперёд, чётность, стоп-бит по 30 мс), картинка.
    Картинка другого размера масштабируется. freq_offset — сдвиг всех частот, Гц
    (имитация расстройки SSB). Фаза непрерывна.
    """
    m = MODES[mode]
    fs = float(fs)
    w, h = m["width"], m["height"]
    img = _fit_image(image_rgb, w, h)
    color = m["color"]
    vals = {}
    if color == "rgb":
        vals = {"R": img[..., 0], "G": img[..., 1], "B": img[..., 2]}
    else:
        y, u, v = _rgb_to_ycc(img)
        if color == "pd":
            vals = {"Y0": y[0::2], "Y1": y[1::2],
                    "U": (u[0::2] + u[1::2]) / 2, "V": (v[0::2] + v[1::2]) / 2}
        elif color == "r72":
            vals = {"Y": y, "U": u, "V": v}
        else:  # r36: чётные строки несут R-Y, нечётные B-Y (среднее по паре)
            vp = (v[0::2] + v[1::2]) / 2
            up = (u[0::2] + u[1::2]) / 2
            c = np.empty_like(y)
            c[0::2], c[1::2] = vp, up
            vals = {"Y": y, "C": c}
    nl = m["lines"]

    # заголовок: (частота, длительность)
    code = m["vis"]
    bits = [(code >> i) & 1 for i in range(7)]
    head = [(F_LEADER, 0.3), (F_SYNC, 0.01), (F_LEADER, 0.3), (F_SYNC, 0.03)]
    head += [(F_BIT1 if b else F_BIT0, 0.03) for b in bits]
    head += [(F_BIT1 if sum(bits) % 2 else F_BIT0, 0.03), (F_SYNC, 0.03)]
    head += [(f, d) for _, d, f in m["prefix"]]
    t_starts, freqs, t = [], [], 0.0
    for f, d in head:
        t_starts.append(t)
        freqs.append(f)
        t += d
    t_img = t

    # шаблон строки: относительные времена кусков и столбцы частот
    rel, cols = [], []
    t = 0.0
    for k, d, f in m["layout"]:
        if k in _CHANNELS:
            rel.append(t + np.arange(w) * (d / w))
            cols.append(F_BLACK + vals[k] * (F_SPAN / 255.0))
        else:
            rel.append(np.array([t]))
            if k == "sepC":
                col = np.where(np.arange(nl) % 2 == 0, F_BLACK, F_WHITE)
            else:
                col = np.full(nl, f)
            cols.append(col[:, None])
        t += d
    rel = np.concatenate(rel)
    fl = np.concatenate([np.broadcast_to(c, (nl, c.shape[1])) for c in cols], axis=1)
    tl = t_img + np.arange(nl)[:, None] * m["line"] + rel[None, :]
    t_end = t_img + nl * m["line"]

    t_all = np.concatenate([np.array(t_starts), tl.ravel(), [t_end]])
    f_all = np.concatenate([np.array(freqs), fl.ravel()]).astype(np.float64) + freq_offset
    bounds = np.round(t_all * fs).astype(np.int64)
    counts = np.diff(bounds)
    finst = np.repeat(f_all, counts)
    out = np.empty(finst.size, np.float32)
    k = 2.0 * math.pi / fs
    ph0 = 0.0
    step = 1 << 20
    for i in range(0, finst.size, step):
        ph = ph0 + k * np.cumsum(finst[i:i + step])
        out[i:i + step] = amplitude * np.sin(ph)
        ph0 = float(ph[-1]) % (2 * math.pi)
    return out


# ------------------------------------------------------------- декодер ------

class _Demod:
    """Комплексный перенос + ФНЧ + прореживание + квадратурный частотный детектор."""

    def __init__(self, fs, fc=1700.0, cutoff=1100.0):
        self.fs = float(fs)
        if self.fs < 6000:
            raise ValueError("частота дискретизации слишком мала для SSTV")
        self.D = max(1, int(round(self.fs / 12000.0)))
        self.fs_int = self.fs / self.D
        self.fc = fc
        ntaps = max(31, int(round(self.fs / 48000.0 * 96)) | 1)
        self.h = firwin(ntaps, cutoff, fs=self.fs)
        self.reset()

    def reset(self):
        self.zi = np.zeros(len(self.h) - 1, complex)
        self.n = 0
        self.prev = 0j

    def process(self, x):
        n = len(x)
        if n == 0:
            return np.zeros(0, np.float32), np.zeros(0, np.float32)
        idx = self.n + np.arange(n, dtype=np.float64)
        mix = np.exp(-2j * math.pi * self.fc / self.fs * idx)
        y, self.zi = lfilter(self.h, 1.0, x * mix, zi=self.zi)
        start = (-self.n) % self.D
        self.n += n
        yd = y[start::self.D]
        if len(yd) == 0:
            return np.zeros(0, np.float32), np.zeros(0, np.float32)
        z = np.concatenate(([self.prev], yd))
        self.prev = yd[-1]
        f = np.angle(z[1:] * np.conj(z[:-1])) * (self.fs_int / (2 * math.pi)) + self.fc
        return f.astype(np.float32), np.abs(yd).astype(np.float32)


class _Buf:
    """Растущий буфер трека частоты с абсолютной индексацией."""

    def __init__(self):
        self.a = np.zeros(1 << 16, np.float32)
        self.n = 0
        self.base = 0

    @property
    def end(self):
        return self.base + self.n

    def append(self, x):
        need = self.n + len(x)
        if need > len(self.a):
            na = np.zeros(max(need, 2 * len(self.a)), np.float32)
            na[:self.n] = self.a[:self.n]
            self.a = na
        self.a[self.n:need] = x
        self.n = need

    def trim(self, keep_from):
        k = int(min(max(keep_from - self.base, 0), self.n))
        if k <= 0:
            return
        self.a[:self.n - k] = self.a[k:self.n]
        self.n -= k
        self.base += k

    def view(self, lo, hi, fill=F_BLACK):
        """Отрезок [lo, hi) в абсолютных индексах; вне буфера — fill."""
        lo, hi = int(lo), int(hi)
        if lo >= self.base and hi <= self.end:
            return self.a[lo - self.base:hi - self.base]
        out = np.full(max(hi - lo, 0), fill, np.float32)
        s, e = max(lo, self.base), min(hi, self.end)
        if e > s:
            out[s - lo:e - lo] = self.a[s - self.base:e - self.base]
        return out

    def mean(self, lo, hi):
        lo, hi = int(round(lo)), int(round(hi))
        if hi <= lo:
            hi = lo + 1
        return float(np.mean(self.view(lo, hi)))


class SstvDecoder:
    """Потоковый декодер SSTV.

    События process():
      {"type": "vis", "mode": имя, "code": int, "offset": Гц}
      {"type": "start", "mode", "width", "height", "manual": bool}
      {"type": "line", "y": int, "rgb": np.uint8[W, 3]}  — по мере приёма
      {"type": "done", "mode", "image": np.uint8[H, W, 3], "complete": bool,
       "lines": принято строк, "slant_ppm", "offset"}
      {"type": "lost", "mode", "image": частичная картинка, "lines"}
    """

    SYNC_Q = 0.45          # порог качества синхроимпульса (0..1)
    IDLE_KEEP_S = 4.5      # сколько истории держать в простое

    def __init__(self, fs=48000):
        self.fs = float(fs)
        self.force_mode = None   # имя режима: автостарт по синхроимпульсам без VIS
        self._dm = _Demod(self.fs)
        self.fs_int = self._dm.fs_int
        self.reset()

    # ----------------------------------------------------------- публичное
    def reset(self):
        """Сброс в исходное состояние (буфер, фильтры, текущая картинка)."""
        self._dm.reset()
        self._buf = _Buf()
        self._state = "idle"
        self._m = None
        self._vis_scan = 0
        self._auto_next = 0
        self._auto_floor = 0
        self._pending = None
        self._last_q = 0.0
        self._off = 0.0
        self._last_f = np.zeros(0, np.float32)
        self._last_amp = 0.0

    def start_manual(self, mode):
        """Ручной старт: ищет строчную синхронизацию режима в ближайших строках
        (с захватом последней прошедшей строки) и начинает картинку без VIS."""
        if mode not in MODES:
            raise KeyError(mode)
        ev = []
        if self._state == "image":
            ev += self._finish(lost=True)
        T = MODES[mode]["line"] * self.fs_int
        self._state = "pending"
        self._pending = {"mode": mode, "p_lo": max(self._buf.base, self._buf.end - int(T)),
                         "tries": 0}
        self._pending_events = ev

    def process(self, audio) -> list:
        """Принимает блок звука, возвращает список событий."""
        x = np.asarray(audio, dtype=np.float64).ravel()
        f, amp = self._dm.process(x)
        if len(amp):
            self._last_amp = float(np.sqrt(np.mean(amp.astype(np.float64) ** 2)))
        self._last_f = f[-int(0.03 * self.fs_int):] if len(f) else self._last_f
        self._buf.append(f)
        ev = []
        if getattr(self, "_pending_events", None):
            ev += self._pending_events
            self._pending_events = []
        while True:
            vis = self._scan_vis()
            if vis is None:
                break
            name, e, off, code = vis
            if self._state == "image":
                ev += self._finish(lost=True)
            self._pending = None
            ev.append({"type": "vis", "mode": name, "code": code, "offset": round(off, 1)})
            t_img = e + 0.300 * self.fs_int
            ev += self._begin(name, t_img + MODES[name]["first_sync"] * self.fs_int, off, False)
            ev += self._run_image()
        if self._state == "pending":
            ev += self._try_pending()
        if self._state == "idle" and self.force_mode in MODES:
            ev += self._try_autostart()
        if self._state == "image":
            ev += self._run_image()
        if self._state == "idle":
            keep = self._buf.end - int(self.IDLE_KEEP_S * self.fs_int)
            if keep - self._buf.base > int(self.fs_int):
                self._buf.trim(keep)
        return ev

    def flush(self) -> list:
        """Конец потока: незаконченная картинка отдаётся как done (complete=False)."""
        if self._state == "image" and self._line > 0:
            return self._finish(lost=False, force_done=True)
        return []

    def level(self) -> dict:
        """Для индикатора: частота тона, уверенность синхронизации и состояние."""
        f = self._last_f
        freq = float(np.median(f)) if len(f) else 0.0
        d = {"freq": freq, "amp": self._last_amp, "sync": max(0.0, min(1.0, self._last_q)),
             "state": self._state, "mode": None, "line": 0, "lines": 0,
             "offset": self._off, "slant_ppm": 0.0}
        if self._state == "image":
            m = self._m
            d.update(mode=m["name"], line=self._line * m["rows_per_line"],
                     lines=m["height"], slant_ppm=(self._b / self._b_nom - 1.0) * 1e6)
        elif self._state == "pending":
            d["mode"] = self._pending["mode"]
        return d

    # ------------------------------------------------------------- VIS
    def _scan_vis(self):
        B, fsI = self._buf, self.fs_int
        L1 = int(round(0.200 * fsI))
        L2 = int(round(0.010 * fsI))
        s0 = int(round(0.004 * fsI))
        s1 = int(round(0.026 * fsI))
        h = int(round(0.005 * fsI))
        tail = int(round(0.340 * fsI))
        kmin = max(self._vis_scan, B.base + L1 + h + 2)
        kmax = B.end - tail
        if kmax < kmin:
            return None
        step = max(1, int(round(0.001 * fsI)))
        ks = np.arange(kmin, kmax + 1, step)
        lo = kmin - L1 - h - 1
        seg = B.view(lo, kmax + s1 + 2)
        c = np.concatenate(([0.0], np.cumsum(seg, dtype=np.float64)))
        kr = ks - lo
        m_lead = (c[kr - L2] - c[kr - L1]) / (L1 - L2)
        m_st = (c[kr + s1] - c[kr + s0]) / (s1 - s0)
        pts = np.arange(-L1 + h, -L2 - h + 1, 2 * h)
        P = kr[:, None] + pts[None, :]
        bx = (c[P + h] - c[P - h]) / (2 * h)
        bad = (np.abs(bx - m_lead[:, None]) > 70.0).sum(1)
        dlt = m_st - m_lead
        cond = (np.abs(m_lead - F_LEADER) <= 160) & (bad <= 2) & (dlt < -560) & (dlt > -840)
        skip_to = -1
        for ci in np.nonzero(cond)[0]:
            k = int(ks[ci])
            if k < skip_to:
                continue
            res = self._try_vis(k, float(m_lead[ci]))
            if res is not None:
                self._vis_scan = int(res[1] + 0.300 * fsI)
                return res
            skip_to = k + int(0.010 * fsI)
        self._vis_scan = int(kmax + step)
        return None

    def _try_vis(self, k, mlead):
        B, fsI = self._buf, self.fs_int
        off = mlead - F_LEADER
        r0 = k - int(0.006 * fsI)
        seg = B.view(r0, k + int(0.014 * fsI)).astype(np.float64)
        e = r0 + float(np.clip((seg - (mlead - 700.0)) / 700.0, 0.0, 1.0).sum())
        bit = 0.030 * fsI
        g = 0.004 * fsI
        st = B.mean(e + g, e + bit - g) - off
        if abs(st - F_SYNC) > 90:
            return None
        v = [B.mean(e + i * bit + g, e + (i + 1) * bit - g) - off for i in range(1, 10)]
        data, par, stop = v[:7], v[7], v[8]
        if any(not (1010.0 < x < 1390.0) for x in data + [par]) or stop > 1280.0:
            return None
        bits = [1 if x < F_SYNC else 0 for x in data]
        p = 1 if par < F_SYNC else 0
        if (sum(bits) + p) % 2:
            return None
        code = sum(b << i for i, b in enumerate(bits))
        name = _VIS2MODE.get(code)
        if name is None:
            return None
        return name, e, off, code

    # --------------------------------------------------------- синхро
    def _sync_scores(self, lo, n, Ls, Lp):
        """Оценки «синхроимпульс начинается в lo+p», p = 0..n-1 (нормированы 0..1)."""
        seg = self._buf.view(lo, lo + n + Ls + Lp).astype(np.float64)
        g = np.clip(((F_SYNC + 150.0 + self._off) - seg) / 150.0, -1.0, 1.0)
        c = np.concatenate(([0.0], np.cumsum(g)))
        p = np.arange(n)
        s = (c[p + Ls] - c[p]) - (c[p + Ls + Lp] - c[p + Ls])
        return s / (Ls + Lp)

    def _sync_search(self, center, w):
        lo = int(math.floor(center - w))
        n = int(2 * w) + 2
        s = self._sync_scores(lo, n, self._Ls, self._Lp)
        k = int(np.argmax(s))
        q = float(s[k])
        p = lo + k
        # уточнение по заднему фронту (синхро 1200 → гашение 1500 есть во всех
        # режимах; передний фронт зависит от содержимого предыдущего канала)
        Lw = max(2, int(round(min(0.0015, 0.3 * self._m["sync"]) * self.fs_int)))
        r0 = p + self._Ls - Lw
        seg = self._buf.view(r0, r0 + 2 * Lw).astype(np.float64)
        v = np.clip((seg - (F_SYNC + self._off)) / (F_BLACK - F_SYNC), 0.0, 1.0)
        edge = r0 + float((1.0 - v).sum())
        return edge - self._m["sync"] * self.fs_int * (self._b / self._b_nom), q

    def _find_anchor(self, T, p_lo, nsync, Ls, Lp):
        n = int(T)
        total = n + int(math.ceil((nsync - 1) * T)) + 2
        s = self._sync_scores(p_lo, total, Ls, Lp)
        comb = np.zeros(n)
        for j in range(nsync):
            o = int(round(j * T))
            comb += s[o:o + n]
        p = int(np.argmax(comb))
        qs = [float(s[p + int(round(j * T))]) for j in range(nsync)]
        return p_lo + p, qs

    def _try_pending(self):
        pd = self._pending
        m = MODES[pd["mode"]]
        T = m["line"] * self.fs_int
        Ls = int(round(m["sync"] * self.fs_int))
        Lp = int(round(0.002 * self.fs_int))
        ev = []
        while self._state == "pending":
            need = pd["p_lo"] + 3 * T + Ls + Lp + 4
            if self._buf.end < need:
                break
            p, qs = self._find_anchor(T, pd["p_lo"], 3, Ls, Lp)
            if min(qs) >= 0.3 or pd["tries"] >= 4:
                self._pending = None
                ev += self._begin(pd["mode"], p, self._off, True)
                ev += self._run_image()
            else:
                pd["p_lo"] += int(T)
                pd["tries"] += 1
        return ev

    def _try_autostart(self):
        m = MODES[self.force_mode]
        T = m["line"] * self.fs_int
        Ls = int(round(m["sync"] * self.fs_int))
        Lp = int(round(0.002 * self.fs_int))
        B = self._buf
        if B.end < self._auto_next:
            return []
        self._auto_next = B.end + int(T / 2)
        p_lo = int(B.end - 3 * T - Ls - Lp - 4)
        if p_lo < max(B.base, self._auto_floor):
            return []
        p, qs = self._find_anchor(T, p_lo, 3, Ls, Lp)
        if min(qs) < 0.5 or sum(qs) / 3 < 0.65:
            return []
        return self._begin(self.force_mode, p, self._off, True) + self._run_image()

    # ---------------------------------------------------------- картинка
    def _begin(self, mode, first_sync, off, manual):
        m = MODES[mode]
        fsI = self.fs_int
        self._m = m
        self._state = "image"
        self._manual = manual
        self._off = float(off)
        T = m["line"] * fsI
        self._b_nom = T
        self._a0 = float(first_sync)
        self._a, self._b = self._a0, T
        self._Ls = int(round(m["sync"] * fsI))
        self._Lp = int(round(0.002 * fsI))
        self._w0 = max(0.012, 0.03 * m["line"]) * fsI
        self._w1 = max(0.004, 0.015 * m["line"]) * fsI
        offs = [o for _, o, _ in m["channels"]]
        ends = [o + d for _, o, d in m["channels"]]
        self._chan_end = max(ends) * fsI
        self._miss_lim = max(6, int(math.ceil(3.0 / m["line"])))
        n = m["lines"]
        self._pos = np.zeros(n)
        self._q = np.zeros(n)
        self._good = np.zeros(n, bool)
        self._line = 0
        self._miss = 0
        self._ngood = 0
        self._last_good = -1
        W, H = m["width"], m["height"]
        c = m["color"]
        if c == "rgb":
            self._P = {"rgb": np.zeros((H, W, 3), np.float32)}
        elif c == "pd":
            self._P = {"Y": np.full((H, W), 16, np.float32),
                       "U": np.full((H // 2, W), 128, np.float32),
                       "V": np.full((H // 2, W), 128, np.float32)}
        elif c == "r72":
            self._P = {k: np.full((H, W), 16 if k == "Y" else 128, np.float32) for k in "YUV"}
        else:
            self._P = {"Y": np.full((H, W), 16, np.float32),
                       "C": np.full((H, W), 128, np.float32),
                       "ct": np.full(H, -1, np.int8)}
        keep = self._a0 + min(0.0, min(offs)) * fsI - 0.05 * fsI - self._w0
        self._buf.trim(int(keep))
        return [{"type": "start", "mode": mode, "width": W, "height": H, "manual": manual}]

    def _fit(self, upto=None):
        idx = np.nonzero(self._good[:upto])[0]
        bn = self._b_nom
        if len(idx) == 0:
            return self._a0, bn
        pos = self._pos[idx]
        if len(idx) == 1 or idx[-1] - idx[0] < 2:
            return float(np.median(pos - idx * bn)), bn
        wt = np.clip(self._q[idx], 0.05, 1.0)
        y0 = pos[0]
        tol = 0.0005 * self.fs_int
        mask = np.ones(len(idx), bool)
        b, a = bn, float(np.median(pos - idx * bn))
        for _ in range(5):
            if mask.sum() < 2 or np.ptp(idx[mask]) < 2:
                break
            b, a0 = np.polyfit(idx[mask], pos[mask] - y0, 1, w=wt[mask])
            a = a0 + y0
            res = pos - (a + b * idx)
            mad = float(np.median(np.abs(res[mask])))
            new = np.abs(res) <= max(4.0 * 1.4826 * mad, tol)
            if new.sum() < 2 or (new == mask).all():
                break
            mask = new
        if abs(b / bn - 1.0) > 0.02:
            b = bn
            a = float(np.median(pos - idx * bn))
        return float(a), float(b)

    def _box(self, bounds):
        """Средние трека частоты по интервалам [bounds[j], bounds[j+1])."""
        lo = int(math.floor(bounds[0])) - 1
        hi = int(math.ceil(bounds[-1])) + 2
        seg = self._buf.view(lo, hi, F_BLACK + self._off)
        c = np.concatenate(([0.0], np.cumsum(seg, dtype=np.float64)))
        x = bounds - lo
        i = np.clip(np.floor(x).astype(np.int64), 0, len(c) - 2)
        fr = x - i
        C = c[i] + (c[i + 1] - c[i]) * fr
        return np.diff(C) / np.maximum(np.diff(bounds), 1e-9)

    def _decode_line(self, S, r):
        m, fsI = self._m, self.fs_int
        W = m["width"]
        out = {}
        k255 = 255.0 / F_SPAN
        for kind, o, d in m["channels"]:
            st = S + o * fsI * r
            bounds = st + np.arange(W + 1) * (d * fsI * r / W)
            f = self._box(bounds)
            out[kind] = np.clip((f - self._off - F_BLACK) * k255, 0.0, 255.0)
        if m["sep"] is not None:
            so, sd = m["sep"]
            st = S + so * fsI * r
            fm = self._buf.mean(st + 0.15 * sd * fsI, st + 0.85 * sd * fsI) - self._off
            out["ct"] = 1 if fm > F_LEADER else 0
        return out

    def _store(self, i, v):
        """Кладёт строку в плоскости, возвращает готовые строки изображения."""
        c, P = self._m["color"], self._P
        if c == "rgb":
            P["rgb"][i] = np.stack([v["R"], v["G"], v["B"]], -1)
            return [i]
        if c == "pd":
            P["Y"][2 * i], P["Y"][2 * i + 1] = v["Y0"], v["Y1"]
            P["U"][i], P["V"][i] = v["U"], v["V"]
            return [2 * i, 2 * i + 1]
        if c == "r72":
            P["Y"][i], P["U"][i], P["V"][i] = v["Y"], v["U"], v["V"]
            return [i]
        P["Y"][i], P["C"][i], P["ct"][i] = v["Y"], v["C"], v["ct"]
        return [i - 1, i] if i % 2 == 1 else []

    def _row_rgb(self, y):
        c, P = self._m["color"], self._P
        if c == "rgb":
            return np.clip(P["rgb"][y] + 0.5, 0, 255).astype(np.uint8)
        if c == "pd":
            return _ycc_to_rgb(P["Y"][y], P["U"][y // 2], P["V"][y // 2])
        if c == "r72":
            return _ycc_to_rgb(P["Y"][y], P["U"][y], P["V"][y])
        k = y - y % 2
        pair = [k, min(k + 1, len(P["ct"]) - 1)]
        ct = [int(P["ct"][j]) for j in pair]
        if ct[0] == ct[1] or -1 in ct:
            ct = [0, 1]           # по чётности: чётная R-Y, нечётная B-Y
        vrow = P["C"][pair[ct.index(0)]]
        urow = P["C"][pair[ct.index(1)]]
        return _ycc_to_rgb(P["Y"][y], urow, vrow)

    def _image(self, rows=None):
        m = self._m
        H = m["height"]
        img = np.stack([self._row_rgb(y) for y in range(H)])
        if rows is not None and rows < H:
            img[rows:] = 0
        return img

    def _run_image(self):
        ev = []
        m, B = self._m, self._buf
        while self._state == "image":
            i = self._line
            pred = self._a + self._b * i
            w = self._w0 if self._ngood == 0 else self._w1
            need = pred + max(w + self._Ls + self._Lp, self._chan_end * 1.03) + 8
            if B.end < need:
                break
            pos, q = self._sync_search(pred, w)
            self._last_q = q
            if q >= self.SYNC_Q:
                self._pos[i], self._q[i], self._good[i] = pos, q, True
                self._ngood += 1
                self._miss = 0
                self._last_good = i
                ls = self._Ls
                fm = B.mean(pos + 0.25 * ls, pos + 0.75 * ls)
                meas = float(np.clip(fm - F_SYNC, -250.0, 250.0))
                self._off += 0.15 * (meas - self._off)
            else:
                self._miss += 1
            self._a, self._b = self._fit(i + 1)
            S = self._a + self._b * i
            vals = self._decode_line(S, self._b / self._b_nom)
            for y in self._store(i, vals):
                ev.append({"type": "line", "y": y, "rgb": self._row_rgb(y)})
            self._line = i + 1
            if self._line >= m["lines"]:
                ev += self._finish(lost=False)
            elif self._miss >= self._miss_lim:
                ev += self._finish(lost=True)
        return ev

    def _finish(self, lost, force_done=False):
        """Завершение картинки: перерасчёт всех строк по общей подгонке синхро."""
        m = self._m
        n_dec = self._line
        if lost:
            n_dec = self._last_good + 1
        ev = []
        if n_dec > 0:
            self._a, self._b = self._fit(n_dec)
            r = self._b / self._b_nom
            for i in range(n_dec):
                self._store(i, self._decode_line(self._a + self._b * i, r))
        rows = n_dec * m["rows_per_line"]
        complete = n_dec >= m["lines"]
        img = self._image(rows)
        info = {"mode": m["name"], "image": img, "lines": rows,
                "slant_ppm": round((self._b / self._b_nom - 1.0) * 1e6, 1),
                "offset": round(self._off, 1)}
        if complete or force_done or (lost and n_dec >= 0.8 * m["lines"]):
            ev.append(dict(type="done", complete=complete, **info))
        else:
            ev.append(dict(type="lost", **info))
        self._auto_floor = int(self._a + self._b * max(n_dec, self._line))
        self._state = "idle"
        self._last_q = 0.0
        return ev


# ------------------------------------------------------------- файлы --------

def decode_file(path, block_s: float = 0.05, force_mode=None) -> list:
    """Декодирует WAV-файл любой частоты дискретизации, возвращает список
    картинок np.uint8[H, W, 3] (законченные и частичные)."""
    from scipy.io import wavfile
    fs, data = wavfile.read(path)
    x = np.asarray(data)
    if x.ndim > 1:
        x = x.astype(np.float64).mean(axis=1)
    if x.dtype == np.uint8:
        x = (x.astype(np.float64) - 128.0) / 128.0
    elif np.issubdtype(x.dtype, np.integer):
        x = x.astype(np.float64) / float(np.iinfo(x.dtype).max)
    else:
        x = x.astype(np.float64)
    if fs < 6000:
        from scipy.signal import resample_poly
        x = resample_poly(x, 12000, fs)
        fs = 12000
    dec = SstvDecoder(fs)
    dec.force_mode = force_mode
    out = []
    n = max(1, int(fs * block_s))
    for i in range(0, len(x), n):
        for ev in dec.process(x[i:i + n]):
            if ev["type"] in ("done", "lost"):
                out.append(ev["image"])
    for ev in dec.process(np.zeros(int(fs * 0.5))) + dec.flush():
        if ev["type"] in ("done", "lost"):
            out.append(ev["image"])
    return out
