"""Эталоны для AprsTest.kt: считает их Python-модулем rtltv/aprs.py и пишет ресурс теста aprs_ref.tsv
(файл данных, а не исходник Kotlin: сотни разобранных пакетов не влезают в лимит размера метода JVM).

Запуск из корня репозитория:  PYTHONUTF8=1 py -3.12 android/app/src/test/python/gen_aprs_ref.py
Шум в тестах демодулятора — общий для Python и Kotlin генератор (xorshift64* + Бокс — Мюллер),
поэтому события декодера можно сверять до кадра (как в DmrTest).
"""
import math
import os
import random
import sys

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), *[".."] * 5))
sys.path.insert(0, ROOT)
from rtltv import aprs  # noqa: E402

OUT = os.path.join(ROOT, "android", "app", "src", "test", "resources", "aprs_ref.tsv")
M64 = (1 << 64) - 1
FS = 48000


class Noise:
    """xorshift64* → равномерное [0,1) → нормальное (Бокс — Мюллер, только косинусная ветвь)."""

    def __init__(self, seed):
        self.x = (seed * 0x9E3779B97F4A7C15 + 1) & M64

    def nxt(self):
        x = self.x
        x ^= x >> 12
        x ^= (x << 25) & M64
        x ^= x >> 27
        self.x = x
        return (x * 0x2545F4914F6CDD1D) & M64

    def uni(self):
        return (self.nxt() >> 11) * (1.0 / 9007199254740992.0)

    def gauss(self):
        u1 = 1.0 - self.uni()
        u2 = self.uni()
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def add_noise(x, snr_db, seed):
    xs = x[np.abs(x) > 0].astype(np.float64)
    ps = float(np.cumsum(xs * xs)[-1]) / xs.size
    sd = math.sqrt(ps / 10 ** (snr_db / 10))
    g = Noise(seed)
    n = np.array([sd * g.gauss() for _ in range(x.size)], np.float64).astype(np.float32)
    return (x + n).astype(np.float32)


def noise_only(n, sd, seed):
    g = Noise(seed)
    return np.array([sd * g.gauss() for _ in range(n)], np.float64).astype(np.float32)


LINES = [
    "UA3ABC-9>APRS,WIDE1-1,WIDE2-1:!5538.55N/03723.50E>Еду домой /A=000500",
    "R2ABC>APDR15,R3DIG-1*,WIDE2-1:=5541.00N/03730.00E-QTH Москва",
    "RA3WX-13>APRS:@231200z5539.20N/03735.40E_090/005g010t050r000p001P002h65b10132",
    "R2ABC>APRS::UA3ABC-9 :Привет, как слышно?{12",
    "UT1FG>CQ,RS0ISS*:=5021.00N/03015.00E-via ISS",
]


def frames():
    return [aprs.tnc2_frame(t) for t in LINES]


# ------------------------------------------------------------------ случаи демодулятора
# (имя, параметры кодера, snr или None, зерно шума, блок)
DEMOD = [
    ("clean", {}, None, 0, 4800),
    ("amp0.02", {"amp": 0.02}, None, 0, 4800),
    ("amp0.2", {"amp": 0.2}, None, 0, 4800),
    ("amp1.5", {"amp": 1.5}, None, 0, 4800),
    ("amp3", {"amp": 3.0}, None, 0, 4800),
    ("tilt0.5", {"space_gain": 0.5}, 8, 4, 4800),
    ("tilt2", {"space_gain": 2.0}, 8, 4, 4800),
    ("shift-60", {"shift": -60.0}, None, 0, 4800),
    ("shift60", {"shift": 60.0}, None, 0, 4800),
    ("clk0.99", {"fs": FS * 0.99}, 15, 5, 4800),
    ("clk0.995", {"fs": FS * 0.995}, 15, 5, 4800),
    ("clk1.005", {"fs": FS * 1.005}, 15, 5, 4800),
    ("clk1.01", {"fs": FS * 1.01}, 15, 5, 4800),
    ("block37", {}, 12, 6, 37),
    ("block480", {}, 12, 6, 480),
    ("block9600", {}, 12, 6, 9600),
    ("snr10s0", {}, 10, 0, 4800), ("snr10s1", {}, 10, 1, 4800), ("snr10s2", {}, 10, 2, 4800),
    ("snr3s0", {}, 3, 0, 4800), ("snr3s1", {}, 3, 1, 4800), ("snr3s2", {}, 3, 2, 4800),
    ("snr0s0", {}, 0, 0, 4800), ("snr0s1", {}, 0, 1, 4800), ("snr0s2", {}, 0, 2, 4800),
    ("snr-3s0", {}, -3, 0, 4800),
]


def demod_case(kw, snr, seed, block):
    x = aprs.encode_packets(frames(), gap=0.4, **kw)
    if snr is not None:
        x = add_noise(x, snr, seed)
    return aprs.decode_audio(x, block=block), x


# ------------------------------------------------------------------ разбор

PARSE_LINES = list(LINES) + [
    "UA3ABC-9>APRS,WIDE1-1:!5538.55N/03723.50E>088/036Еду /A=000500",
    "N0CALL>APRS:/092345z4903.50S/07201.75W-PHG5132 Дом",
    "N0CALL>APRS:!/5L!!<*e7>7P[",
    "R2ABC>APRS:;LEADER   *231200z5542.00N/03740.00E>Колонна",
    "R2ABC>APRS:;LEADER   _231200z5542.00N/03740.00E>снят",
    "R2ABC>APRS:)AID#2_5540.50N/03738.00EA",
    "R2ABC>APRS:)AID#2!5540.50N/03738.00EAпервая помощь",
    "R2ABC>APRS:>Слушаю 144.800",
    "R2ABC>APRS:>231200zQRV",
    "R2ABC>APRS::UA3ABC-9 :Привет{12",
    "UA3ABC-9>APRS::R2ABC    :ack12",
    "UA3ABC-9>APRS::R2ABC    :rej7}AB",
    "R2ABC>APRS::BLN1     :Собрание в 19:00",
    "R2ABC>APRS::NWS-WARN :Шторм",
    "R2ABC>APRS::R2ABC    :PARM.Batt,Temp",
    "R2ABC>APRS::R2ABC    :UNIT.V,C",
    "R2ABC>APRS::R2ABC    :EQNS.0,1,0",
    "R2ABC>APRS::R2ABC    :BITS.11111111,Проект",
    "R2ABC>APRS:T#123,100,200,050,000,255,10101010",
    "R2ABC>APRS:T#7,1.5,x,3,,5,1100коммент",
    "R2ABC>APRS:T#MIC,1,2",
    "RA3WX-13>APRS:@231200z5539.20N/03735.40E_090/005g010t050r000p001P002h65b10132wRSW",
    "RA3WX-13>APRS:_10231200c220s004g005t-04r000p000P000h50b09900",
    "RA3WX-13>APRS:_10231200c...s   g005t077h00L123l045#100 комментарий",
    "RA3WX-13>APRS:!5539.20N/03735.40E_.../...g...t030s010",
    "R2ABC>APRS,R3DIG-1,WIDE1*,WIDE2-1:>x",
    "R2ABC>APRS,WIDE1-1:>x",
    "R2ABC>APRS,WIDE1*,WIDE2-1:>только алиасы",
    "R2ABC>APRS,TCPIP*:>через интернет",
    "R2ABC>APRS,ARISS*:>x",
    "RS0ISS>APRS:>борт",
    "R3GW>APRS:}DL1ABC>APRS,TCPIP,R3GW*:!4903.50N/00720.00E-gate",
    "R3GW>APRS:}испорчено",
    "N0CALL>APRS:$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A",
    "N0CALL>APRS:$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47",
    "N0CALL>APRS:$GPRMC,123519,V,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A",
    "N0CALL>APRS:$GPGGA,123519,xx07.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47",
    "R2ABC>APRS:!!0000003A01E2",
    "R2ABC>APRS:<IGATE,MSG_CNT=1",
    "R2ABC>APRS:?APRS?",
    "R2ABC>APRS:!5538.  N/03723.  E>неточно",
    "R2ABC>APRS:!5538.55N/03723.50E>!W47! уточнение DAO",
    "R2ABC>APRS:!5538.55N/03723.50E>|!!!\"!#!$!%| телеметрия",
    "R2ABC>APRS:!5538.55N/03723.50E>|!!!\"!#| нечётная",
    "R2ABC>APRS:!5538.55N/03723.50E#RNG0050 digi",
    "R2ABC>APRS:!5538.55N\\03723.50E\\088/036/270/729 пеленг",
    "R2ABC>APRS:!5538.55N/03723.50E>DFS2360 dfs",
    "R2ABC>APRS:!5538.55N/03723.50E>PHG0000",
    "R2ABC>APRS:@092345h5538.55N/03723.50E>время h",
    "R2ABC>APRS:/092345/5538.55N/03723.50E>местное",
    "R2ABC>APRS:/0923455538.55N/03723.50E>плохое время",
    "R2ABC>APRS:ABCDEF!5538.55N/03723.50E>впереди текст",
    "R2ABC>APRS:просто текст",
    "R2ABC>APRS:!5538.55N/03723.50E",
    "R2ABC>APRS:!9538.55N/03723.50E>плохая широта",
    "R2ABC>APRS:!5538.55N/18123.50E>плохая долгота",
    "R2ABC>APRS:!5538.55N203723.50E&шлюз",
    "R2ABC>APRS:!5538.55NS03723.50ES спутник",
    "R2ABC>APRS:=/5L!!<*e7_ !с ветром",
    "R2ABC>APRS:=/5L!!<*e7O{?!дальность",
    "R2ABC>APRS:!a5L!!<*e7&  !",
    "R2ABC>APRS:!xx",
    "R2ABC>APRS:`",
    "R2ABC>APRS:;SHORT",
    "R2ABC>APRS:",
    "R2ABC>APRS:@12",
    "N0CALL>S32U6T:`(_fn\"Oj/]комм=",
    "N0CALL>S32U6T:`(_fn\"Oj/>TH-D74^",
    "N0CALL>S32U6T:'(_fn\"Oj/`yaesu_ ",
    "N0CALL>S32U6T:`(_fn\"Oj/'\"4T}высота_0",
    "N0CALL>SA2U6T:`(_fn\"Oj/свой",
    "N0CALL>SAZU6T:`(_fn\"Oj/смешанный",
    "N0CALL>S3XU6T:`(_fn\"Oj/",
    "N0CALL>S32U6:`(_fn\"Oj/короткий",
    "N0CALL>S3-U6T:`(_fn\"Oj/плохое",
    "N0CALL>T2SP0W:`(_fn\"Oj/юг-запад",
    "N0CALL>T2SP0W:`\x7f_fn\"Oj/плохая долгота",
    "UA3ABC-9>APRS:\x1c(_fn\"Oj/старый Mic-E",
    "ПЛОХОЙ>APRS:>кириллица в позывном",
    "без двоеточия",
    "R2ABC:нет стрелки",
]

ENC_POS = [
    ("pos1", lambda: aprs.encode_position(55.6425, 37.391667, "/", ">", "комм")),
    ("pos2", lambda: aprs.encode_position(-33.999999, -70.0001, "\\", "k", "", True)),
    ("pos3", lambda: aprs.encode_position(55.75, 37.62, "/", "_", "", False, "231200z")),
    ("pos4", lambda: aprs.encode_position(10.0049999, 9.99999, "/", "-", "", True, "231200z")),
    ("cmp1", lambda: aprs.encode_compressed(55.7, 37.6, "/", ">", course=88, speed_kn=36, comment="сжатая")),
    ("cmp2", lambda: aprs.encode_compressed(55.0, 37.0, "/", "O", alt_ft=10000)),
    ("cmp3", lambda: aprs.encode_compressed(-12.5, 120.25, "\\", "#")),
    ("cmp4", lambda: aprs.encode_compressed(49.5, -72.75, "/", ">", course=360, speed_kn=0)),
]
ENC_MICE = [
    (55.75580, 37.61730, 45, 275, "/", ">", 6, "мобильный", 150, None),
    (33.4273, -112.129, 0, 0, "/", ">", 0, "", None, None),
    (-5.5, 5.25, 12.6, 359.6, "/", "[", 7, "x", None, "]="),
    (60.0, 105.0, 120, 90, "\\", "k", 3, "", -50, "_ "),
    (0.0, 179.99, 799, 180, "/", ">", 1, "", None, ">"),
]


def fval(v):
    if v is None:
        return "None"
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, float):
        return repr(v)
    return str(v)


def fields(p):
    d = {}
    for k in ("type", "kind", "symbol", "via", "via_iss", "lat", "lon", "course", "speed_kmh", "alt_m", "comment",
              "raw", "timestamp", "name", "alive", "status", "mice_msg", "radio", "gateway", "third_party", "text",
              "sym_table", "sym_code", "compressed", "ambiguity", "messaging", "range_km", "dfs", "mic_e"):
        d[k] = fval(p.get(k))
    m = p.get("message")
    d["msg"] = "None" if m is None else "\x1f".join(str(v) for v in (m.get("to"), m.get("kind"), m.get("id"), m.get("text")))
    t = p.get("telemetry")
    d["tlm"] = "None" if t is None else "\x1f".join((t["seq"], ",".join(fval(None if v is None else float(v)) for v in t["analog"]), str(t["bits"])))
    d["wx"] = "None" if p.get("wx") is None else aprs.wx_text(p["wx"])
    g = p.get("phg")
    d["phg"] = "None" if g is None else f"{g['power_w']}|{g['height_m']!r}|{g['gain_db']}|{g['dir']}"
    d["df"] = "None" if p.get("df") is None else str(p["df"]["bearing"])
    d["path"] = ",".join(f"{c}{'*' if h else ''}" for c, h in p["path"])
    d["outer_path"] = "None" if p.get("outer_path") is None else ",".join(
        f"{c}{'*' if h else ''}" for c, h in p["outer_path"])
    d["describe"] = aprs.describe(p)
    return d


CRASHES = []


def fuzz_lines(n, seed=12345):
    rnd = random.Random(seed)
    alphabet = ("0123456789./\\!=@;:)>_`'{}|#ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz *-,^[]"
                "\x1c\x1d\x7f\xa0\xe9ПРабв\x00\t\n\x85\xb2-+eE")
    base = [ln for ln in PARSE_LINES if ":" in ln and ">" in ln.split(":")[0]]
    out = []
    CRASHES.clear()
    while len(out) < n:
        ln = rnd.choice(base)
        head, _, info = ln.partition(":")
        info = list(info)
        for _ in range(rnd.randint(1, 4)):
            op = rnd.random()
            pos = rnd.randint(0, len(info))
            if op < 0.4 and info:
                info[min(pos, len(info) - 1)] = rnd.choice(alphabet)
            elif op < 0.7:
                info.insert(pos, rnd.choice(alphabet))
            elif info:
                del info[min(pos, len(info) - 1)]
        line = head + ":" + "".join(info)
        try:
            p = aprs.parse_line(line)
            if p is not None:
                aprs.describe(p)
        except Exception as e:
            if len(CRASHES) < 60:
                CRASHES.append((line, type(e).__name__))
            continue
        out.append(line)
    return out


def esc(s):
    """Поле TSV: обратная косая → двойная, управляющие символы → \\uXXXX (Kotlin-тест разбирает обратно)."""
    out = []
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    return "".join(out)


def rx_list(out):
    return ";".join(f'{o["data"].hex()},{o["t"]!r},{o["fixed"]},{o["decoders"]}' for o in out)


def main():
    rows = []

    def w(*f):
        rows.append("\t".join(esc(str(v)) for v in f))

    for s in LINES:
        w("LINE", s)
    w("FCS123", aprs.fcs(b"123456789"))
    for f in frames():
        w("FRAMEFCS", aprs.add_fcs(f).hex())
    mf = aprs.make_frame("R2ABC-15", "APRS", ["R3DIG-1*", "WIDE2-1"], "!test")
    w("MAKE", mf.hex(), aprs.tnc2(aprs.decode_ax25(mf)))
    ff = aprs.add_fcs(aprs.tnc2_frame(LINES[0]))
    for flips in ([(5, 0x10)], [(9, 0x03)], [(9, 0x03), (20, 0x81)], [(30, 0x40), (31, 0x01)], [(0, 0x01)]):
        b = bytearray(ff)
        for i, m in flips:
            b[i] ^= m
        fx, n = aprs.try_fix(bytes(b))
        w("FIX", bytes(b).hex(), n, "None" if fx is None else fx.hex())
    from scipy import signal
    for band in ((900, 2500), (500, 2900)):
        h = signal.firwin(81, band, pass_zero=False, fs=FS)
        w("FIR", band[0], band[1], *[repr(float(v)) for v in h])
    x = aprs.encode_packets(frames(), gap=0.4)
    w("ENC", x.size, repr(float(np.sum(np.abs(x.astype(np.float64))))))
    for i in range(0, x.size, 7919):
        w("ENCV", i, repr(float(x[i])))
    hb = aprs.hdlc_bits(aprs.add_fcs(frames()[0]))
    w("HDLC", "".join(map(str, hb)))
    w("NRZI", "".join(map(str, aprs.nrzi(hb))))
    for name, kw, snr, seed, block in DEMOD:
        out, xx = demod_case(kw, snr, seed, block)
        print(name, len(out), [o["decoders"] for o in out], [o["fixed"] for o in out], file=sys.stderr)
        w("DEMOD", name, repr(kw.get("amp", 0.5)), repr(kw.get("space_gain", 1.0)), repr(kw.get("shift", 0.0)),
          repr(kw.get("fs", float(FS))), "None" if snr is None else repr(float(snr)), seed, block, xx.size,
          rx_list(out))
    f0 = aprs.tnc2_frame(LINES[0])
    w("REPEATED", rx_list(aprs.decode_audio(aprs.encode_packets([f0, f0, f0], gap=0.4))))
    w("NOISEONLY", rx_list(aprs.decode_audio(noise_only(FS * 20, 0.3, 9))))
    for name, fn in ENC_POS:
        w("ENCF", name, fn())
    for lat, lon, spd, crs, tab, code, msg, com, alt, radio in ENC_MICE:
        d, i = aprs.encode_mice(lat, lon, speed_kn=spd, course=crs, table=tab, code=code, msg=msg, comment=com,
                                alt_m=alt, radio=radio)
        w("ENCM", d, i)
    dest, info = aprs.encode_mice(55.75580, 37.61730, speed_kn=45, course=275, msg=6, comment="мобильный",
                                  alt_m=150)
    mfr = aprs.make_frame("UA3ABC-9", dest, ["WIDE1-1"], info[:9].encode("latin-1") + info[9:].encode("utf-8"))
    cp = aprs.make_frame("R2ABC", "APRS", [], ">Привет".encode("cp1251"))
    non = bytearray(aprs.make_frame("R2ABC", "BEACON", [], "hello"))
    non[15] = 0xCF                                  # PID NET/ROM — не APRS
    sabm = aprs.make_frame("R2ABC", "R3DIG", [], b"")[:14] + bytes((0x2F,))  # не UI-кадр
    for h in (mfr.hex(), cp.hex(), bytes(non).hex(), sabm.hex(), (b"\x00" * 20).hex()):
        p = aprs.parse_frame(bytes.fromhex(h))
        if p is None:
            w("PARSEF", h, "None")
        else:
            w("PARSEF", h, *[f"{k}={v}" for k, v in fields(p).items()])
    for ln in PARSE_LINES + fuzz_lines(int(os.environ.get("APRS_FUZZ", "400"))):
        p = aprs.parse_line(ln)
        if p is None:
            w("PARSE", ln, "None")
        else:
            w("PARSE", ln, *[f"{k}={v}" for k, v in fields(p).items()])
    fuzz_lines(5000, seed=777)                    # отдельный прогон — только ради строк, где Python падает
    for ln, err in CRASHES:
        w("CRASH", ln, err)
    w("LOC", aprs.locator(55.75580, 37.61730), aprs.locator(-33.9, -70.65), aprs.locator(0.0, 0.0))
    w("DIST", repr(aprs.distance_km(55.75, 37.62, 59.94, 30.31)), repr(aprs.bearing_deg(55.75, 37.62, 59.94, 30.31)))
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Сгенерировано android/app/src/test/python/gen_aprs_ref.py по rtltv/aprs.py — не править вручную.\n")
        f.write("\n".join(rows) + "\n")
    print("записано", OUT, len(rows), file=sys.stderr)


if __name__ == "__main__":
    main()


