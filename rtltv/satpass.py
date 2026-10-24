"""Орбиты спутников: TLE с celestrak.org (кэш, офлайн), SGP4, подспутниковая точка, трасса,
пролёты над домом (AOS/LOS/максимум), азимут/возвышение, доплеровский сдвиг.

Кэш TLE: %LOCALAPPDATA%\\RtlTV\\tle\\<группа>.txt, обновление не чаще раза в сутки;
без сети — работаем по кэшу любой давности.
"""
import math
import os
import time
import urllib.request
from dataclasses import dataclass

import numpy as np
from sgp4.api import Satrec
from . import paths

TLE_DIR = paths.service("tle")
TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"
TLE_URL_CATNR = "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=tle"
GROUPS = ("weather", "stations", "amateur")
MAX_AGE = 24 * 3600
USER_AGENT = "RtlTV/1.1 (personal SDR receiver; Windows)"

C_LIGHT = 299792.458          # км/с
R_EARTH = 6378.137            # км (WGS-84)
F_EARTH = 1 / 298.257223563
OMEGA_E = 7.2921150e-5        # рад/с

# спутники режима: NORAD → (подпись, цвет, частота Гц, примечание)
TRACKED = {
    57166: ("Метеор-М №2-3", "#ffb84d", 137.9e6, "LRPT 137,900 МГц, 72k OQPSK"),
    59051: ("Метеор-М №2-4", "#ff6a4d", 137.9e6, "LRPT 137,900 МГц, 72k OQPSK"),
    25544: ("МКС", "#7fd1ff", 145.8e6, "SSTV/голос 145,800 МГц — режим SSTV"),
}


@dataclass
class Tle:
    name: str
    norad: int
    line1: str
    line2: str

    @property
    def epoch_unix(self):
        s = Satrec.twoline2rv(self.line1, self.line2)
        return (s.jdsatepoch + s.jdsatepochF - 2440587.5) * 86400.0


def parse_tle(text):
    """Текст в формате 3LE/2LE → {norad: Tle}."""
    out = {}
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    i = 0
    while i < len(lines) - 1:
        if lines[i].startswith("1 ") and lines[i + 1].startswith("2 "):
            name, l1, l2, step = None, lines[i], lines[i + 1], 2
        elif i < len(lines) - 2 and lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
            name, l1, l2, step = lines[i].strip(), lines[i + 1], lines[i + 2], 3
        else:
            i += 1
            continue
        i += step
        try:
            norad = int(l1[2:7])
            Satrec.twoline2rv(l1, l2)
        except (ValueError, RuntimeError):
            continue
        out[norad] = Tle(name or f"NORAD {norad}", norad, l1, l2)
    return out


def cache_time(groups=GROUPS):
    """Когда последний раз скачивались TLE (unix-время самого свежего файла кэша) или None."""
    ts = [os.path.getmtime(_cache_path(g)) for g in groups if os.path.exists(_cache_path(g))]
    return max(ts) if ts else None


def _cache_path(group):
    return os.path.join(TLE_DIR, f"{group}.txt")


def _download(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("ascii", "replace")


def _read_group(url, path, force, offline, timeout):
    """Текст TLE группы и пометка о происхождении."""
    age = time.time() - os.path.getmtime(path) if os.path.exists(path) else None
    if not offline and (force or age is None or age > MAX_AGE):
        try:
            text = _download(url, timeout)
            if parse_tle(text):
                with open(path, "w", encoding="ascii", newline="\n") as f:
                    f.write(text)
                return text, "обновлено"
        except OSError:
            pass
    if age is None:
        return None, "нет данных"
    with open(path, encoding="ascii", errors="replace") as f:
        return f.read(), f"кэш {age / 3600:.0f} ч"


def load_tle(groups=GROUPS, force=False, offline=False, timeout=10.0, norads=()):
    """Возвращает ({norad: Tle}, сообщение о состоянии). norads — досылать по номеру, если нет в группах."""
    os.makedirs(TLE_DIR, exist_ok=True)
    tles = {}
    notes = []
    for g in groups:
        text, note = _read_group(TLE_URL.format(group=g), _cache_path(g), force, offline, timeout)
        notes.append(f"{g}: {note}")
        if text:
            tles.update(parse_tle(text))
    for n in list(TRACKED) + [n for n in norads if n not in TRACKED]:   # недостающие — по номеру NORAD
        if n not in tles:
            text, _ = _read_group(TLE_URL_CATNR.format(norad=n), _cache_path(f"catnr_{n}"), force, offline,
                                  timeout)
            if text:
                tles.update(parse_tle(text))
    return tles, "; ".join(notes)


# ---------------------------------------------------------------- геометрия

def jd_from_unix(t):
    t = np.asarray(t, np.float64)
    jd = t / 86400.0 + 2440587.5
    jd0 = np.floor(jd - 0.5) + 0.5
    return jd0, jd - jd0


def gmst(jd, fr):
    """Звёздное время Гринвича (рад), IAU-82."""
    tut = (jd + fr - 2451545.0) / 36525.0
    g = 67310.54841 + (876600.0 * 3600 + 8640184.812866) * tut + 0.093104 * tut ** 2 - 6.2e-6 * tut ** 3
    return np.radians((g % 86400.0) / 240.0) % (2 * np.pi)


def geodetic_to_ecef(lat, lon, h_km=0.0):
    la, lo = np.radians(lat), np.radians(lon)
    e2 = F_EARTH * (2 - F_EARTH)
    n = R_EARTH / np.sqrt(1 - e2 * np.sin(la) ** 2)
    return np.array([(n + h_km) * np.cos(la) * np.cos(lo), (n + h_km) * np.cos(la) * np.sin(lo),
                     (n * (1 - e2) + h_km) * np.sin(la)])


def ecef_to_geodetic(x, y, z):
    e2 = F_EARTH * (2 - F_EARTH)
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1 - e2))
    for _ in range(5):
        n = R_EARTH / np.sqrt(1 - e2 * np.sin(lat) ** 2)
        h = p / np.cos(lat) - n
        lat = np.arctan2(z, p * (1 - e2 * n / (n + h)))
    n = R_EARTH / np.sqrt(1 - e2 * np.sin(lat) ** 2)
    h = p / np.cos(lat) - n
    return np.degrees(lat), np.degrees(lon), h


@dataclass
class Pass:
    norad: int
    name: str
    aos: float          # unix
    los: float
    tca: float
    max_el: float
    az_aos: float
    az_los: float
    az_tca: float

    @property
    def duration(self):
        return self.los - self.aos


class Orbit:
    def __init__(self, tle):
        self.tle = tle
        self.sat = Satrec.twoline2rv(tle.line1, tle.line2)

    def ecef(self, t):
        """t — unix-время (скаляр или массив) → (r км, v км/с) в ECEF, формы (3, N)."""
        t = np.atleast_1d(np.asarray(t, np.float64))
        jd, fr = jd_from_unix(t)
        e, r, v = self.sat.sgp4_array(jd, fr)
        r = r.T
        v = v.T
        g = gmst(jd, fr)
        c, s = np.cos(g), np.sin(g)
        x = c * r[0] + s * r[1]
        y = -s * r[0] + c * r[1]
        vx = c * v[0] + s * v[1] + OMEGA_E * y
        vy = -s * v[0] + c * v[1] - OMEGA_E * x
        re = np.vstack([x, y, r[2]])
        ve = np.vstack([vx, vy, v[2]])
        bad = e != 0
        if np.any(bad):
            re[:, bad] = np.nan
            ve[:, bad] = np.nan
        return re, ve

    def subpoint(self, t):
        r, _ = self.ecef(t)
        lat, lon, h = ecef_to_geodetic(r[0], r[1], r[2])
        if np.ndim(t) == 0:
            return float(lat[0]), float(lon[0]), float(h[0])
        return lat, lon, h

    @property
    def period_min(self):
        """Период обращения, мин (из среднего движения TLE)."""
        no = self.sat.no_kozai          # рад/мин
        return 2 * math.pi / no if no > 0 else 100.0

    def orbit_track(self, t0, before=1.0, after=1.0, points=240):
        """Трасса на before/after оборотов назад/вперёд от t0 (для геостационара — одна точка)."""
        p = self.period_min
        if p > 1300:                    # геостационар/геосинхронный: трасса вырождается в точку
            return self.track(t0, 0, 0, 60)
        step = max(10.0, p * 60 * (before + after) / points)
        return self.track(t0, p * before, p * after, step)

    def track(self, t0, before_min=50, after_min=100, step_s=30):
        ts = t0 + np.arange(-before_min * 60, after_min * 60 + 1, step_s)
        lat, lon, _ = self.subpoint(ts)
        ok = np.isfinite(lat)
        return list(zip(lat[ok].tolist(), lon[ok].tolist()))

    def look(self, t, lat, lon, h_km=0.0):
        """→ (азимут °, возвышение °, дальность км, скорость сближения км/с (+ удаляется))."""
        r, v = self.ecef(t)
        o = geodetic_to_ecef(lat, lon, h_km)[:, None]
        d = r - o
        la, lo = math.radians(lat), math.radians(lon)
        sl, cl, so, co = math.sin(la), math.cos(la), math.sin(lo), math.cos(lo)
        e = -so * d[0] + co * d[1]
        n = -sl * co * d[0] - sl * so * d[1] + cl * d[2]
        u = cl * co * d[0] + cl * so * d[1] + sl * d[2]
        rng = np.sqrt(e * e + n * n + u * u)
        el = np.degrees(np.arctan2(u, np.hypot(e, n)))
        az = np.degrees(np.arctan2(e, n)) % 360
        rr = np.sum(d * v, axis=0) / rng
        if np.ndim(t) == 0:
            return float(az[0]), float(el[0]), float(rng[0]), float(rr[0])
        return az, el, rng, rr

    def doppler(self, t, freq, lat, lon, h_km=0.0):
        """Сдвиг частоты, Гц (принимаемая = freq + сдвиг)."""
        _az, _el, _rng, rr = self.look(t, lat, lon, h_km)
        return -freq * rr / C_LIGHT

    def passes(self, lat, lon, t0=None, hours=24.0, min_el=0.0, h_km=0.0):
        t0 = time.time() if t0 is None else t0
        step = 20.0
        # окно с запасом 30 мин после конца, чтобы пролёт на границе не обрезался
        ts = t0 - 20 * 60 + np.arange(0, hours * 3600 + 50 * 60, step)
        az, el, _, _ = self.look(ts, lat, lon, h_km)
        el = np.nan_to_num(el, nan=-90.0)
        up = el > 0
        out = []
        i = 0
        n = ts.size
        while i < n:
            if not up[i]:
                i += 1
                continue
            j = i
            while j < n and up[j]:
                j += 1
            aos = self._edge(ts[i - 1] if i > 0 else ts[i], ts[i], lat, lon, h_km, rising=True)
            los = self._edge(ts[j - 1], ts[j] if j < n else ts[j - 1], lat, lon, h_km, rising=False)
            fine = np.arange(aos, los, 1.0) if los - aos > 2 else np.array([aos, los])
            faz, fel, _, _ = self.look(fine, lat, lon, h_km)
            k = int(np.nanargmax(fel))
            mx = float(fel[k])
            if j < n and i > 0 and mx >= min_el and los > t0 and aos < t0 + hours * 3600:
                out.append(Pass(self.tle.norad, self.tle.name, float(aos), float(los), float(fine[k]), mx,
                                float(faz[0]), float(faz[-1]), float(faz[k])))
            i = j
        return out

    def _edge(self, ta, tb, lat, lon, h_km, rising):
        if tb <= ta:
            return ta
        for _ in range(12):
            m = 0.5 * (ta + tb)
            _, el, _, _ = self.look(m, lat, lon, h_km)
            if (el > 0) == rising:
                tb = m
            else:
                ta = m
        return 0.5 * (ta + tb)


def visibility_circle(lat, lon, alt_km=830.0, min_el=0.0, n=120):
    """Граница зоны, где спутник на высоте alt_km виден с дома выше min_el (точки lat, lon)."""
    e = math.radians(min_el)
    lam = math.acos(R_EARTH * math.cos(e) / (R_EARTH + alt_km)) - e    # угловой радиус
    la, lo = math.radians(lat), math.radians(lon)
    pts = []
    for k in range(n + 1):
        b = 2 * math.pi * k / n
        la2 = math.asin(math.sin(la) * math.cos(lam) + math.cos(la) * math.sin(lam) * math.cos(b))
        lo2 = lo + math.atan2(math.sin(b) * math.sin(lam) * math.cos(la), math.cos(lam) - math.sin(la) * math.sin(la2))
        pts.append((math.degrees(la2), (math.degrees(lo2) + 540) % 360 - 180))
    return pts


def all_passes(orbits, lat, lon, t0=None, hours=24.0, min_el=0.0):
    out = []
    for o in orbits:
        try:
            out.extend(o.passes(lat, lon, t0, hours, min_el))
        except (ValueError, FloatingPointError):
            continue
    out.sort(key=lambda p: p.aos)
    return out
