"""Каталог спутников режима «Спутники»: метео, МКС, любительские FM, SSB/CW-транспондеры, телеметрия.

Частоты — по SatNOGS DB (db.satnogs.org, выгрузка 23.10.2026) и AMSAT. Статус — на октябрь 2026 г.
по данным SatNOGS (на орбите / сошёл) и сообщениям AMSAT: работа любительских аппаратов часто
зависит от освещённости и расписания операторов — «активен» не гарантирует сигнал на каждом витке.
"""
from dataclasses import dataclass, field

CATEGORIES = {
    "meteo": "Метео",
    "iss": "МКС",
    "fm": "FM-ретрансляторы",
    "ssb": "SSB/CW-транспондеры",
    "tlm": "Телеметрия",
    "other": "Свои / другие",
}

# режимы демодуляции для прослушивания (RadioDemod) и цифровые декодеры приёма
LISTEN_MODES = ("NFM", "WFM", "AM", "USB", "LSB", "CW")
DECODERS = {
    "voice": "голос / звук",
    "lrpt": "LRPT («Метеор-М», снимки)",
    "lrit": "LRIT («Электро-Л», снимки)",
    "iq": "только приём и запись IQ",
}


@dataclass
class Tx:
    label: str
    freq: float            # частота вниз (для транспондера — нижний край полосы), Гц
    mode: str              # режим демодуляции RadioDemod: NFM, USB, LSB, CW, AM; LRPT — приём снимков
    hi: float = 0.0        # верхний край полосы транспондера, Гц
    bw: float = 0.0        # полоса канала, Гц (0 — по умолчанию режима)
    note: str = ""
    decoder: str = ""      # voice | lrpt | lrit | iq; пусто — по режиму

    @property
    def kind(self):
        if self.decoder:
            return self.decoder
        if self.mode == "LRPT":
            return "lrpt"
        if self.mode == "LRIT":
            return "lrit"
        return "voice" if self.mode in LISTEN_MODES else "iq"

    @property
    def listenable(self):
        return self.mode in LISTEN_MODES

    def to_dict(self):
        return dict(label=self.label, freq=self.freq, mode=self.mode, hi=self.hi, bw=self.bw, note=self.note,
                    decoder=self.decoder)

    @property
    def is_band(self):
        return self.hi > self.freq

    @property
    def center(self):
        return (self.freq + self.hi) / 2 if self.is_band else self.freq


@dataclass
class Sat:
    norad: int
    name: str
    cat: str
    color: str
    txs: list = field(default_factory=list)
    status: str = "активен"
    note: str = ""
    user: bool = False     # добавлен пользователем
    tle: tuple = ()        # (строка 1, строка 2) — TLE, введённый вручную

    def to_dict(self):
        return dict(norad=self.norad, name=self.name, cat=self.cat, color=self.color, status=self.status,
                    note=self.note, tle=list(self.tle), txs=[t.to_dict() for t in self.txs])

    @staticmethod
    def from_dict(d):
        txs = [Tx(t.get("label", ""), float(t["freq"]), t.get("mode", "NFM"), float(t.get("hi") or 0),
                  float(t.get("bw") or 0), t.get("note", ""), t.get("decoder", "")) for t in d.get("txs", [])]
        return Sat(int(d["norad"]), d.get("name") or f"NORAD {d['norad']}", d.get("cat", "other"),
                   d.get("color", "#e0e0e0"), txs, d.get("status", "добавлен вручную"), d.get("note", ""), True,
                   tuple(d.get("tle") or ()))


def _mhz(x):
    return x * 1e6


CATALOG = [
    # --- метео
    Sat(57166, "Метеор-М №2-3", "meteo", "#ffb84d",
        [Tx("LRPT (снимки)", _mhz(137.900), "LRPT", note="72k OQPSK"),
         Tx("LRPT, резервная частота", _mhz(137.100), "LRPT", note="72k OQPSK")],
        "активен", "антенна раскрылась не полностью — сигнал слабее №2-4"),
    Sat(59051, "Метеор-М №2-4", "meteo", "#ff6a4d",
        [Tx("LRPT (снимки)", _mhz(137.900), "LRPT", note="72k OQPSK"),
         Tx("LRPT, резервная частота", _mhz(137.100), "LRPT", note="72k OQPSK")], "активен"),
    # --- МКС
    Sat(25544, "МКС", "iss", "#7fd1ff",
        [Tx("Голос / SSTV", _mhz(145.800), "NFM", bw=15000, note="SSTV — режим SSTV"),
         Tx("APRS", _mhz(145.825), "NFM", bw=15000, note="пакеты AFSK 1200"),
         Tx("Кросс-бэнд репитер", _mhz(437.800), "NFM", bw=15000, note="вверх 145,990 (67 Гц), при включении")],
        "активна", "SSTV/голос 145,800 МГц — режим SSTV"),
    # --- FM
    Sat(27607, "SO-50 (SaudiSat-1C)", "fm", "#9be37a",
        [Tx("FM-ретранслятор", _mhz(436.795), "NFM", bw=15000, note="вверх 145,850 (67 Гц)")], "активен"),
    Sat(43678, "PO-101 (Diwata-2)", "fm", "#c9f27a",
        [Tx("FM-ретранслятор", _mhz(145.900), "NFM", bw=15000, note="вверх 437,500")],
        "по расписанию", "включают по объявлениям операторов"),
    Sat(43017, "AO-91 (Fox-1B)", "fm", "#7ae3c3",
        [Tx("FM-ретранслятор", _mhz(145.960), "NFM", bw=15000, note="вверх 435,250 (без тона)")],
        "только на солнце", "аккумулятор деградировал — работает на освещённой части витка"),
    Sat(61781, "AO-123 (ASRTU-1)", "fm", "#e3d77a",
        [Tx("FM-ретранслятор", _mhz(435.400), "NFM", bw=15000, note="вверх 145,850")], "активен"),
    Sat(22825, "AO-27 (EYESAT-1)", "fm", "#a7a7a7",
        [Tx("FM-ретранслятор", _mhz(436.795), "NFM", bw=15000, note="вверх 145,850")],
        "нерегулярно", "аппарату 30+ лет, включается эпизодически"),
    # --- SSB/CW
    Sat(44909, "RS-44 (DOSAAF-85)", "ssb", "#f27ab8",
        [Tx("Транспондер (инверт.)", _mhz(435.610), "USB", hi=_mhz(435.670), note="вверх 145,935–145,995"),
         Tx("CW-маяк", _mhz(435.605), "CW")], "активен"),
    Sat(24278, "FO-29 (JAS-2)", "ssb", "#d07af2",
        [Tx("Транспондер (инверт.)", _mhz(435.800), "USB", hi=_mhz(435.900), note="вверх 145,900–146,000"),
         Tx("CW-маяк", _mhz(435.795), "CW")], "по расписанию", "включает JARL, обычно по выходным"),
    Sat(39444, "AO-73 (FUNcube-1)", "ssb", "#7a9bf2",
        [Tx("Транспондер (инверт.)", _mhz(145.950), "USB", hi=_mhz(145.970), note="вверх 435,130–435,150"),
         Tx("Телеметрия BPSK 1200", _mhz(145.935), "USB")],
        "в тени Земли", "транспондер работает в тени, телеметрия — на солнце"),
    Sat(43803, "JO-97 (JY1Sat)", "ssb", "#b0b0f2",
        [Tx("Транспондер (инверт.)", _mhz(145.855), "USB", hi=_mhz(145.875), note="вверх 435,100–435,120"),
         Tx("Телеметрия BPSK 1200", _mhz(145.840), "USB")], "нерегулярно"),
    # --- телеметрия
    Sat(40908, "LilacSat-2 (CAS-3H)", "tlm", "#f2c27a",
        [Tx("Телеметрия BPSK 9600 / CW", _mhz(437.200), "USB"), Tx("Телеметрия GFSK 4800", _mhz(437.225), "NFM")],
        "активен"),
    Sat(43880, "UWE-4", "tlm", "#7af2e0",
        [Tx("Телеметрия FSK 9600", _mhz(435.600), "NFM", bw=20000)], "активен"),
    Sat(59112, "SONATE-2", "tlm", "#f27a7a",
        [Tx("SSTV (Martin M1)", _mhz(145.880), "NFM", bw=15000), Tx("CW-маяк", _mhz(145.840), "CW"),
         Tx("Телеметрия GMSK 9600", _mhz(437.025), "NFM", bw=20000)], "активен"),
]

# «Электро-Л» (геостационар, LRIT 1691 МГц): в каталоге для «Приёма со спутника», пролётов у них нет
GEO = [
    Sat(67756, "Электро-Л №5", "meteo", "#ffd84d", [Tx("LRIT (снимки)", _mhz(1691.0), "LRIT", note="BPSK 294k")],
        "активен", "76° в. д."),
    Sat(44903, "Электро-Л №3", "meteo", "#ffd84d", [Tx("LRIT (снимки)", _mhz(1691.0), "LRIT", note="BPSK 294k")],
        "перегоняется", "с 76° в. д. на 14,5° з. д."),
    Sat(55506, "Электро-Л №4", "meteo", "#ffd84d", [Tx("LRIT (снимки)", _mhz(1691.0), "LRIT", note="BPSK 294k")],
        "активен", "165,8° в. д. — из России не виден"),
    Sat(41105, "Электро-Л №2", "meteo", "#ffd84d", [Tx("LRIT (снимки)", _mhz(1691.0), "LRIT", note="BPSK 294k")],
        "заменяется №3", "14,5° з. д."),
]
GEO_NORADS = {s.norad for s in GEO}

BUILTIN = list(CATALOG)
USER = []
BY_NORAD = {}


def _rebuild():
    CATALOG[:] = BUILTIN + [u for u in USER if u.norad not in {b.norad for b in BUILTIN}]
    BY_NORAD.clear()
    for s in CATALOG + GEO:
        BY_NORAD[s.norad] = s


def set_user(sats):
    """Свои спутники (список Sat или dict из настроек) — добавляются в каталог."""
    USER[:] = [s if isinstance(s, Sat) else Sat.from_dict(s) for s in sats]
    _rebuild()


def all_sats():
    """Все спутники для выбора приёма: каталог + «Электро-Л»."""
    return CATALOG + GEO


def norads(cats=None):
    return [s.norad for s in CATALOG if cats is None or s.cat in cats]


_rebuild()
