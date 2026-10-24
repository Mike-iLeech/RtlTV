"""Телевизионные стандарты и сетки каналов."""
from dataclasses import dataclass


@dataclass(frozen=True)
class TvStandard:
    key: str
    name: str
    lines: int            # строк в кадре
    line_freq: float      # Гц
    sound_offset: float   # несущая звука относительно несущей изображения, Гц
    active_start_us: float
    active_us: float
    hsync_us: float
    vis1: int             # первая видимая строка поля 1 (от начала кадровых импульсов поля 1)
    vis2: int             # первая видимая строка поля 2
    vis_count: int        # видимых строк в поле
    n_broad: int          # полустрок с широкими кадровыми импульсами
    n_eq: int             # полустрок с уравнивающими импульсами
    tune_offset: float = 0.5e6  # центр приёмника выше несущей изображения (уводит DC с несущей)


STANDARDS = {
    "DK": TvStandard("DK", "SECAM/PAL D/K — Россия, СНГ", 625, 15625.0, 6.5e6,
                     10.5, 52.0, 4.7, 22, 335, 288, 5, 5),
    "BG": TvStandard("BG", "PAL B/G — Европа", 625, 15625.0, 5.5e6,
                     10.5, 52.0, 4.7, 22, 335, 288, 5, 5),
    "I": TvStandard("I", "PAL I — Великобритания", 625, 15625.0, 6.0e6,
                    10.5, 52.0, 4.7, 22, 335, 288, 5, 5),
    "M": TvStandard("M", "NTSC M — США, Япония", 525, 15734.264, 4.5e6,
                    10.9, 52.6, 4.7, 17, 279, 240, 6, 6),
}


@dataclass(frozen=True)
class Channel:
    name: str
    video: float      # несущая изображения (аналог), Гц
    center: float     # центр канала (цифровой мультиплекс), Гц
    bandwidth: float  # ширина канала, Гц


def _ch(name, vc_mhz, center_off, bw):
    vc = vc_mhz * 1e6
    return Channel(name, vc, vc + center_off, bw)


def _plan_ru():
    vhf = [49.75, 59.25, 77.25, 85.25, 93.25, 175.25, 183.25, 191.25, 199.25, 207.25, 215.25, 223.25]
    out = [_ch(f"R{i + 1} (МВ)", f, 2.75e6, 8e6) for i, f in enumerate(vhf)]
    out += [_ch(f"{n} (ДМВ)", 471.25 + 8 * (n - 21), 2.75e6, 8e6) for n in range(21, 70)]
    return out


def _plan_eu():
    out = [_ch("E2", 48.25, 2.25e6, 7e6), _ch("E3", 55.25, 2.25e6, 7e6), _ch("E4", 62.25, 2.25e6, 7e6)]
    out += [_ch(f"E{n}", 175.25 + 7 * (n - 5), 2.25e6, 7e6) for n in range(5, 13)]
    out += [_ch(f"{n} (UHF)", 471.25 + 8 * (n - 21), 2.75e6, 8e6) for n in range(21, 70)]
    return out


def _plan_uk():
    return [_ch(f"{n} (UHF)", 471.25 + 8 * (n - 21), 2.75e6, 8e6) for n in range(21, 69)]


def _plan_us():
    vhf = {2: 55.25, 3: 61.25, 4: 67.25, 5: 77.25, 6: 83.25}
    out = [_ch(f"{n} (VHF)", f, 1.75e6, 6e6) for n, f in vhf.items()]
    out += [_ch(f"{n} (VHF)", 175.25 + 6 * (n - 7), 1.75e6, 6e6) for n in range(7, 14)]
    out += [_ch(f"{n} (UHF)", 471.25 + 6 * (n - 14), 1.75e6, 6e6) for n in range(14, 37)]
    return out


PLANS = {
    "RU": ("Россия / СНГ (R1–R12, 21–69)", "DK", _plan_ru),
    "EU": ("Европа (E2–E12, 21–69)", "BG", _plan_eu),
    "UK": ("Великобритания (21–68)", "I", _plan_uk),
    "US": ("США (2–36)", "M", _plan_us),
}


def channels(plan_key):
    return PLANS[plan_key][2]()
