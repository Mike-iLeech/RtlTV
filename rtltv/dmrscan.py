"""Сканер DMR: обход диапазона окнами по ~2,4 МГц, поиск активных каналов и проверка DMR-синхрослов.

На каждом окне один большой БПФ даёт сразу все каналы окна:
  * мощность по сетке 6,25 кГц (покрывает и сетку 12,5 кГц, и PMR446/dPMR со сдвигом 6,25);
  * для активных каналов — выделение канала прямо в частотной области (вырезка бинов ±24 кГц
    и обратный БПФ = канал 48 кГц), FM-дискриминатор и декодер DMR (rtltv.dmr.DmrDecoder);
  * если DMR-синхрослов нет — грубая классификация: аналоговая ЧМ, цифровой 4FSK 4800 Бод
    (P25/NXDN 9600 и т. п.), узкополосный 2400 Бод (dPMR/NXDN 4800), широкополосный, несущая.
Повторные проходы копят статистику по каналам (CC, слоты, тип, последние ID, сколько раз слышен).
"""
import math
import time

import numpy as np

from .dmr import DmrDecoder

GRID = 6250.0            # сетка поиска, Гц
CH_FS = 48000            # частота дискретизации выделенного канала
DC_SHIFT = 3125.0        # центр окна смещён на полшага сетки, чтобы «ноль» донгла не лёг на канал

BANDS = [
    ("70 см любительский 430–440 МГц", 430.0e6, 440.0e6),
    ("PMR446 446,0–446,2 МГц", 446.0e6, 446.2e6),
    ("Служебные 400–470 МГц", 400.0e6, 470.0e6),
    ("2 м любительский 144–146 МГц", 144.0e6, 146.0e6),
    ("Служебные 136–174 МГц", 136.0e6, 174.0e6),
]

# примеры каналов, пойманных в живом эфире при отладке (в другом городе будут свои)
KNOWN = [
    (440.3875e6, "DMR-ретранслятор, CC12; слышен групповой вызов 599 → TG 27; маяк каждые ~20 с"),
    (440.0875e6, "DMR Tier III, управляющий канал (CSBK Aloha/C_BCAST), CC0 — непрерывно"),
    (438.8125e6, "DMR-ретранслятор, CC12"),
    (438.05e6, "DMR-ретранслятор, CC1"),
    (446.65e6, "DMR (Motorola, CSBK FID 0x10 — похоже на Capacity Plus), CC3"),
]

KIND_NAMES = {"dmr": "DMR", "analog": "аналог ЧМ", "digital": "другой цифровой", "narrow": "узкий цифровой",
              "wide": "широкополосный", "carrier": "несущая", "known": "известный"}


def plan_windows(lo, hi, fs):
    """Центры окон перестройки, чтобы полезные ±40 % полосы покрыли [lo, hi]."""
    span = 0.8 * fs
    lo, hi = float(min(lo, hi)), float(max(lo, hi))
    if hi - lo <= span:
        return [round((lo + hi) / 2 / GRID) * GRID + DC_SHIFT]
    n = int(math.ceil((hi - lo) / span))
    step = (hi - lo) / n
    return [round((lo + step * (k + 0.5)) / GRID) * GRID + DC_SHIFT for k in range(n)]


def dwell_samples(fs, dwell):
    """Длина блока: кратна fs/gcd(fs, 48000), чтобы вырезка бинов давала ровно 48 кГц."""
    g = math.gcd(int(fs), CH_FS)
    q = int(fs) // g
    k = max(1, int(round(dwell * fs / q)))
    return k * q


def _taper(m, bin_hz, flat=7500.0, edge=10500.0):
    f = (np.arange(m) - m // 2) * bin_hz
    a = np.abs(f)
    w = np.ones(m)
    t = (a > flat) & (a < edge)
    w[t] = 0.5 + 0.5 * np.cos(np.pi * (a[t] - flat) / (edge - flat))
    w[a >= edge] = 0.0
    return w


def _baud_line(fm, rate):
    """Сила спектральной линии на частоте символов (признак цифровой ЧМ-модуляции)."""
    d = np.diff(fm.astype(np.float64))
    e = d * d
    e = e - e.mean()
    n = len(e)
    if n < 4096:
        return 0.0
    sp = np.abs(np.fft.rfft(e * np.hanning(n)))
    hz = CH_FS / n
    k = int(round(rate / hz))
    lo, hi = max(1, k - int(400 / hz)), min(len(sp) - 1, k + int(400 / hz))
    peak = sp[max(1, k - 3): k + 4].max()
    ref = np.median(sp[lo:hi]) + 1e-12
    return float(peak / ref)


class DmrScanner:
    def __init__(self, fs, lo, hi, dwell=0.35, max_check=12):
        self.fs = int(fs)
        self.lo, self.hi = float(min(lo, hi)), float(max(lo, hi))
        self.centers = plan_windows(self.lo, self.hi, self.fs)
        self.nsamp = dwell_samples(self.fs, dwell)
        self.max_check = max_check
        self.entries = {}
        self.passes = 0
        self.windows_done = 0
        self.last_freq = None
        self.busy_s = 0.0
        for f, note in KNOWN:
            self._entry(f).update(kind="known", label="известный", note=note)

    # ------------------------------------------------------------------ план
    def plan_items(self):
        return [(int(c), int(self.nsamp)) for c in self.centers]

    def _entry(self, f):
        key = int(round(f / GRID) * GRID)
        e = self.entries.get(key)
        if e is None:
            e = {"freq": float(key), "kind": "", "label": "", "note": "", "cc": [], "slots": [], "types": [],
                 "mode": "", "ids": [], "hits": 0, "dmr_hits": 0, "first": 0.0, "last": 0.0, "level_db": -200.0,
                 "encrypted": False, "in_band": self.lo - 1 <= key <= self.hi + 1}
            self.entries[key] = e
        return e

    # ------------------------------------------------------------------ обработка окна
    def process(self, freq, iq, i=None):
        """Один блок плана (центр freq, IQ complex64). Возвращает список наблюдений этого окна."""
        t0 = time.perf_counter()
        if i == 0 or (i is None and freq == self.centers[0]):
            self.passes += 1
        self.windows_done += 1
        self.last_freq = freq
        x = np.asarray(iq, dtype=np.complex64)
        n = len(x)
        if n < 4096:
            return []
        x = x - x.mean()
        X = np.fft.fftshift(np.fft.fft(x))
        P = (X.real ** 2 + X.imag ** 2).astype(np.float64)
        bin_hz = self.fs / n
        cs = np.concatenate([[0.0], np.cumsum(P)])
        half = self.fs * 0.4
        g0 = math.ceil(max(self.lo, freq - half) / GRID)
        g1 = math.floor(min(self.hi, freq + half) / GRID)
        if g1 < g0:
            return []
        grid = np.arange(g0, g1 + 1) * GRID
        off = grid - freq
        k = n // 2 + off / bin_hz
        w = 3000.0 / bin_hz
        a = np.clip(np.round(k - w).astype(np.int64), 0, n)
        b = np.clip(np.round(k + w).astype(np.int64), 0, n)
        pw = (cs[b] - cs[a]) / np.maximum(b - a, 1)
        noise = float(np.median(pw)) + 1e-20
        rel = 10 * np.log10(pw / noise + 1e-20)
        act = rel > 10.0
        act &= np.abs(off) > 4000.0           # остаток «нуля» донгла
        obs = []
        # группируем соседние активные ячейки: ширина сигнала и его пик
        runs = []
        j = 0
        while j < len(act):
            if act[j]:
                s = j
                while j + 1 < len(act) and act[j + 1]:
                    j += 1
                runs.append((s, j))
            j += 1
        runs.sort(key=lambda r: -rel[r[0]: r[1] + 1].max())
        m = n * CH_FS // self.fs
        taper = None
        for s, e in runs[: self.max_check]:
            width = (e - s + 1)
            pk = s + int(np.argmax(rel[s: e + 1]))
            # центр сигнала — по «центру тяжести» мощности в ячейках серии
            ww = pw[s: e + 1] - noise
            fc = float((grid[s: e + 1] * ww).sum() / ww.sum()) if width > 1 else float(grid[pk])
            level = float(rel[pk])
            if width >= 4:
                obs.append(self._note(fc, "wide", level, "широкополосный (≥25 кГц)"))
                continue
            kc = int(round(n // 2 + (fc - freq) / bin_hz))
            lo_k, hi_k = kc - m // 2, kc - m // 2 + m
            seg = np.zeros(m, np.complex128)
            sa, sb = max(lo_k, 0), min(hi_k, n)
            seg[sa - lo_k: sb - lo_k] = X[sa:sb]
            if taper is None:
                taper = _taper(m, bin_hz)
            y = np.fft.ifft(np.fft.ifftshift(seg * taper))
            fm = (np.angle(y[1:] * np.conj(y[:-1])) * (CH_FS / (2 * np.pi))).astype(np.float32)
            obs.append(self._classify(fc, fm, width, level))
        self.busy_s += time.perf_counter() - t0
        return obs

    def _classify(self, fc, fm, width, level):
        dec = DmrDecoder(CH_FS)
        _audio, events = dec.process(fm)
        st = dec.status()
        if st["locked"] and dec.stats["syncs"] >= 1:
            return self._note_dmr(fc, dec, st, events, level)
        dev = float(np.std(fm))
        if dev < 150.0:
            return self._note(fc, "carrier", level, "несущая без модуляции")
        l48 = _baud_line(fm, 4800.0)
        l24 = _baud_line(fm, 2400.0)
        if l48 > 8.0 and l48 > l24:
            return self._note(fc, "digital", level, "цифровой 4FSK 4800 Бод без DMR-синхры (P25/NXDN?)")
        if l24 > 8.0:
            return self._note(fc, "narrow", level, "узкополосный цифровой 2400 Бод (dPMR/NXDN?)")
        return self._note(fc, "analog", level, "аналоговая ЧМ (или другой цифровой)")

    def _base(self, f, level):
        e = self._entry(f)
        now = time.time()
        e["hits"] += 1
        e["first"] = e["first"] or now
        e["last"] = now
        e["level_db"] = level
        return e

    def _note(self, f, kind, level, label):
        e = self._base(f, level)
        if e["kind"] != "dmr" or e["dmr_hits"] == 0:
            e["kind"], e["label"] = kind, label
        return {"freq": e["freq"], "kind": kind, "label": label, "level_db": level}

    def _note_dmr(self, f, dec, st, events, level):
        f = f + float(st.get("offset_hz", 0.0))
        e = self._base(f, level)
        e["dmr_hits"] += 1
        e["kind"] = "dmr"
        e["mode"] = st.get("mode", "")
        types = []
        voice = dec.stats["voice"] > 0
        for n, s in st["slots"].items():
            if s.get("cc") not in (None, -1) and s["cc"] not in e["cc"]:
                e["cc"].append(s["cc"])
            if s.get("active") or s.get("last"):
                if n not in e["slots"] and (s.get("active") or s.get("last") not in ("", "Idle")):
                    e["slots"].append(n)
            if s.get("active") and (s.get("src") or s.get("dst")):
                self._add_id(e, s["src"], s["dst"], s["group"])
            if s.get("encrypted") and s.get("active"):
                e["encrypted"] = True
        for ev in events:
            if ev["type"] in ("call_start", "call_end", "data") and (ev["src"] or ev["dst"]):
                self._add_id(e, ev["src"], ev["dst"], ev["group"])
            if ev.get("encrypted"):
                e["encrypted"] = True
        if voice:
            types.append("голос")
        if dec.stats["csbk"] >= 3 and not voice:
            types.append("управляющий канал")
        elif dec.stats["csbk"]:
            types.append("CSBK")
        if any(ev["type"] == "data" for ev in events):
            types.append("данные")
        if not types:
            lasts = {s.get("last") for s in st["slots"].values()}
            types.append("маяк/дежурный" if lasts <= {"Idle", "", "Терминатор"} else "служебные пакеты")
        for t in types:
            if t not in e["types"]:
                e["types"].append(t)
        e["now"] = types
        e["label"] = " · ".join(types)
        return {"freq": e["freq"], "kind": "dmr", "label": e["label"], "level_db": level,
                "cc": list(e["cc"]), "mode": e["mode"], "events": events}

    @staticmethod
    def _add_id(e, src, dst, group):
        s = f"{src or '?'} → {'TG ' if group else ''}{dst or '?'}"
        if s in e["ids"]:
            e["ids"].remove(s)
        e["ids"].insert(0, s)
        del e["ids"][5:]

    # ------------------------------------------------------------------ результат
    def snapshot(self):
        rows = []
        for e in self.entries.values():
            if e["hits"] == 0 and e["kind"] != "known":
                continue
            r = dict(e)
            for k in ("cc", "slots", "types", "ids"):
                r[k] = list(e[k])
            rows.append(r)
        rows.sort(key=lambda r: r["freq"])
        return rows

    def progress(self):
        return {"pass": self.passes, "windows": len(self.centers), "done": self.windows_done,
                "freq": self.last_freq, "busy_s": self.busy_s,
                "dmr": sum(1 for e in self.entries.values() if e["kind"] == "dmr" and e["dmr_hits"])}


def describe(row):
    """Короткое описание строки для интерфейса: (что слышно, подробности)."""
    kind = row["kind"]
    if kind == "dmr":
        head = "DMR"
        if row["cc"]:
            head += " CC" + "/".join(str(c) for c in row["cc"])
        if row["mode"]:
            head += f" ({row['mode']})"
        text = f"{head} · {row['label']}"
        if row.get("encrypted"):
            text += " · шифр"
    elif kind == "known":
        text = "известный: " + row["note"]
    else:
        text = row["label"] or KIND_NAMES.get(kind, kind)
    return text
