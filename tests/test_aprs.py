"""Тесты APRS: кодер AFSK/AX.25 → эфирные искажения → потоковый декодер; разбор APRS всех типов;
тестовый эфир через NFM-демодулятор; окно режима на тестовом эфире (невидимо, offscreen).

Искажения: белый шум, громкость ×0,05…×3, перекос тонов (пред-/деакцентирование ±6 дБ),
сдвиг тонов ±60 Гц, ошибка тактовой ±1 %, расстройка по ВЧ ±500 Гц (через RadioDemod).

Запуск таблицей чувствительности: py -3.12 tests/test_aprs.py
"""
import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtltv import aprs  # noqa: E402

FS = 48000

LINES = [
    "UA3ABC-9>APRS,WIDE1-1,WIDE2-1:!5538.55N/03723.50E>Еду домой /A=000500",
    "R2ABC>APDR15,R3DIG-1*,WIDE2-1:=5541.00N/03730.00E-QTH Москва",
    "RA3WX-13>APRS:@231200z5539.20N/03735.40E_090/005g010t050r000p001P002h65b10132",
    "R2ABC>APRS::UA3ABC-9 :Привет, как слышно?{12",
    "UT1FG>CQ,RS0ISS*:=5021.00N/03015.00E-via ISS",
]


def frames():
    return [aprs.tnc2_frame(t) for t in LINES]


def air(snr_db=None, seed=0, **kw):
    fr = frames()
    x = aprs.encode_packets(fr, gap=0.4, **kw)
    if snr_db is not None:
        rng = np.random.default_rng(seed)
        ps = np.mean(x[np.abs(x) > 0] ** 2)
        x = x + rng.normal(0, np.sqrt(ps / 10 ** (snr_db / 10)), x.size).astype(np.float32)
    return fr, x


def got(fr, out):
    datas = {o["data"] for o in out}
    return sum(f in datas for f in fr)


# ----------------------------------------------------------------- AX.25 / CRC

def test_fcs_and_residue():
    f = aprs.tnc2_frame(LINES[0])
    ff = aprs.add_fcs(f)
    assert aprs.check_fcs(ff)
    # известное значение: CRC-16/X-25 от «123456789» = 0x906E
    assert aprs.fcs(b"123456789") == 0x906E
    bad = bytearray(ff)
    bad[5] ^= 0x10
    assert not aprs.check_fcs(bad)
    fixed, n = aprs.try_fix(bytes(bad))
    assert fixed == ff and n == 1
    bad[9] ^= 0x03                     # два соседних бита
    bad[5] ^= 0x10
    fixed, n = aprs.try_fix(bytes(bad))
    assert fixed == ff and n == 2


def test_ax25_roundtrip():
    f = aprs.make_frame("R2ABC-15", "APRS", ["R3DIG-1*", "WIDE2-1"], "!test")
    fr = aprs.decode_ax25(f)
    assert fr["src"] == "R2ABC-15" and fr["dest"] == "APRS"
    assert fr["path"] == [("R3DIG-1", True), ("WIDE2-1", False)]
    assert aprs.tnc2(fr) == "R2ABC-15>APRS,R3DIG-1*,WIDE2-1:!test"
    assert aprs.decode_ax25(b"\x00" * 20) is None


# ----------------------------------------------------------------- кодер → декодер

def test_clean_all_frames():
    fr, x = air()
    out = aprs.decode_audio(x)
    assert got(fr, out) == len(fr)
    assert len(out) == len(fr)                      # без дублей от разных срезателей
    assert all(o["fixed"] == 0 for o in out)
    assert min(o["decoders"] for o in out) >= 8


def test_repeated_identical_frames_are_kept():
    f = aprs.tnc2_frame(LINES[0])
    out = aprs.decode_audio(aprs.encode_packets([f, f, f], gap=0.4))
    assert len(out) == 3


@pytest.mark.parametrize("snr", [10, 3, 0])
def test_noise(snr):
    total = 0
    for seed in range(3):
        fr, x = air(snr_db=snr, seed=seed)
        total += got(fr, aprs.decode_audio(x))
    assert total >= {10: 15, 3: 14, 0: 12}[snr]


@pytest.mark.parametrize("amp", [0.02, 0.2, 1.5, 3.0])
def test_volume(amp):
    fr, x = air(amp=amp)
    assert got(fr, aprs.decode_audio(x)) == len(fr)


@pytest.mark.parametrize("space_gain", [0.5, 2.0])
def test_tone_tilt(space_gain):
    """Пред-/деакцентирование: тон 2200 Гц на 6 дБ тише/громче 1200 Гц, плюс шум."""
    fr, x = air(snr_db=8, seed=4, space_gain=space_gain)
    assert got(fr, aprs.decode_audio(x)) == len(fr)


@pytest.mark.parametrize("shift", [-60.0, 60.0])
def test_tone_shift(shift):
    fr, x = air(shift=shift)
    assert got(fr, aprs.decode_audio(x)) == len(fr)


@pytest.mark.parametrize("clk", [0.99, 0.995, 1.005, 1.01])
def test_clock_error(clk):
    """Ошибка частоты дискретизации передатчика ±0,5…1 %: ФАПЧ тактов её отрабатывает."""
    fr, x = air(snr_db=15, seed=5, fs=FS * clk)
    assert got(fr, aprs.decode_audio(x)) == len(fr)


def test_block_sizes():
    fr, x = air(snr_db=12, seed=6)
    for block in (37, 480, 9600):
        assert got(fr, aprs.decode_audio(x, block=block)) == len(fr)


def test_noise_only_no_frames():
    rng = np.random.default_rng(9)
    out = aprs.decode_audio(rng.normal(0, 0.3, FS * 60).astype(np.float32))
    assert out == []


def test_speed():
    _fr, x = air(snr_db=10)
    x = np.tile(x, 4)
    t0 = time.perf_counter()
    aprs.decode_audio(x)
    dt = time.perf_counter() - t0
    assert dt < 0.25 * x.size / FS, f"декодер медленный: {dt:.2f} с на {x.size / FS:.0f} с звука"


@pytest.mark.parametrize("rf_off", [-500.0, 0.0, 500.0])
def test_rf_through_nfm(rf_off):
    """Полный путь: AFSK → FM (девиация 3 кГц) на ПЧ с расстройкой → RadioDemod NFM → декодер."""
    from rtltv.radio import RadioDemod
    fs = 960000
    fr, a = air(amp=0.6, space_gain=1.4)
    up = np.repeat(a, fs // FS).astype(np.float64)
    ph = 2 * np.pi * np.cumsum(25000 + rf_off + 3000 * up) / fs
    rng = np.random.default_rng(3)
    iq = (np.exp(1j * ph) * 0.1 + 0.02 * (rng.standard_normal(ph.size) + 1j * rng.standard_normal(ph.size)))
    iq = iq.astype(np.complex64)
    d = RadioDemod(fs, "NFM")
    d.offset = 25000.0
    dem = aprs.AfskDemodulator(FS)
    out = []
    blk = fs // 10
    for i in range(0, iq.size, blk):
        out += dem.process(d.process(iq[i:i + blk]))
    assert got(fr, out) == len(fr)


# ----------------------------------------------------------------- разбор APRS

def P(line):
    p = aprs.parse_line(line)
    assert p is not None
    return p


def test_parse_uncompressed():
    p = P("UA3ABC-9>APRS,WIDE1-1:!5538.55N/03723.50E>088/036Еду /A=000500")
    assert p["type"] == "position" and p["kind"] == "car"
    assert abs(p["lat"] - (55 + 38.55 / 60)) < 1e-6 and abs(p["lon"] - (37 + 23.5 / 60)) < 1e-6
    assert p["course"] == 88 and abs(p["speed_kmh"] - 36 * 1.852) < 1e-6
    assert abs(p["alt_m"] - 152.4) < 0.1 and p["comment"] == "Еду"
    assert p["symbol"] == "Автомобиль" and p["via"] == "напрямую"


def test_parse_south_west_timestamp_phg():
    p = P("N0CALL>APRS:/092345z4903.50S/07201.75W-PHG5132 Дом")
    assert p["lat"] < 0 and p["lon"] < 0 and p["kind"] == "home"
    assert p["timestamp"].startswith("9-е число, 23:45")
    assert p["phg"]["power_w"] == 25 and p["phg"]["gain_db"] == 3 and p["comment"] == "Дом"


def test_parse_compressed():
    s = aprs.encode_compressed(55.7, 37.6, "/", ">", course=88, speed_kn=36, comment="сжатая")
    p = P("UB3XYZ-7>APRS:" + s)
    assert p["compressed"] and abs(p["lat"] - 55.7) < 1e-4 and abs(p["lon"] - 37.6) < 1e-4
    assert abs(p["course"] - 88) <= 4 and abs(p["speed_kmh"] / 1.852 - 36) < 3
    assert p["comment"] == "сжатая"
    # эталон из спецификации APRS 1.01: «/5L!!<*e7>7P[» → 49°30′N 72°45′W, курс 88°, 36,2 уз
    q = P("N0CALL>APRS:!/5L!!<*e7>7P[")
    assert abs(q["lat"] - 49.5) < 1e-3 and abs(q["lon"] + 72.75) < 1e-3
    assert q["course"] == 88 and abs(q["speed_kmh"] / 1.852 - 36.2) < 0.2
    a = P("N0CALL>APRS:" + aprs.encode_compressed(55.0, 37.0, "/", "O", alt_ft=10000))
    assert abs(a["alt_m"] - 3048) < 10


def test_parse_mice():
    dest, info = aprs.encode_mice(55.75580, 37.61730, speed_kn=45, course=275, msg=6, comment="мобильный",
                                  alt_m=180, radio="]=")
    fr = aprs.make_frame("UA3ABC-9", dest, ["WIDE1-1"], info[:9].encode("latin-1") + info[9:].encode("utf-8"))
    p = aprs.parse_frame(fr)
    assert p["type"] == "mic-e" and p["kind"] == "car"
    assert abs(p["lat"] - 55.75580) < 2e-4 and abs(p["lon"] - 37.61730) < 2e-4
    assert abs(p["speed_kmh"] - 45 * 1.852) < 0.01 and p["course"] == 275
    assert p["mice_msg"] == "В пути" and p["radio"] == "Kenwood TM-D710"
    assert p["alt_m"] == 180 and p["comment"] == "мобильный"
    # западное полушарие, долгота > 100°, «Emergency», минуты < 10
    for lat, lon, msg in ((-33.4273, -112.1290, 0), (5.05, 105.01, 7), (40.0, 8.05, 3)):
        d, i = aprs.encode_mice(lat, lon, speed_kn=0, course=0, msg=msg)
        q = aprs.parse_line(f"N0CALL>{d}:" + i)
        assert abs(q["lat"] - lat) < 2e-4 and abs(q["lon"] - lon) < 2e-4
        assert q["mice_msg"] == aprs.MICE_STD[msg]


def test_parse_mice_reference():
    # эталон APRS 1.01 (гл. 10): назначение S32U6T → 33°25,64′ с. ш.; долгота 112°07,74′ з. д.
    p = P("N0CALL>S32U6T:`(_fn\"Oj/")
    assert abs(p["lat"] - (33 + 25.64 / 60)) < 1e-4
    assert p["lat"] > 0 and p["lon"] < 0


def test_parse_object_item():
    p = P("R2ABC>APRS:;LEADER   *231200z5542.00N/03740.00E>Колонна")
    assert p["type"] == "object" and p["name"] == "LEADER" and p["alive"] and p["kind"] == "object"
    assert p["comment"] == "Колонна"
    q = P("R2ABC>APRS:)AID#2_5540.50N/03738.00EA")
    assert q["type"] == "item" and q["name"] == "AID#2" and not q["alive"]


def test_parse_status_message_ack_telemetry():
    assert P("R2ABC>APRS:>Слушаю 144.800")["status"] == "Слушаю 144.800"
    st = P("R2ABC>APRS:>231200zQRV")
    assert st["status"] == "QRV" and st["timestamp"]
    m = P("R2ABC>APRS::UA3ABC-9 :Привет{12")["message"]
    assert m == {"to": "UA3ABC-9", "kind": "msg", "id": "12", "text": "Привет"}
    a = P("UA3ABC-9>APRS::R2ABC    :ack12")["message"]
    assert a["kind"] == "ack" and a["id"] == "12"
    assert P("R2ABC>APRS::BLN1     :Собрание в 19:00")["message"]["kind"] == "bulletin"
    assert P("R2ABC>APRS::R2ABC    :PARM.Batt,Temp")["message"]["kind"] == "tlm_parm"
    t = P("R2ABC>APRS:T#123,100,200,050,000,255,10101010")["telemetry"]
    assert t["seq"] == "123" and t["analog"] == [100, 200, 50, 0, 255] and t["bits"] == "10101010"


def test_parse_weather():
    p = P("RA3WX-13>APRS:@231200z5539.20N/03735.40E_090/005g010t050r000p001P002h65b10132wRSW")
    wx = p["wx"]
    assert p["type"] == "weather" and p["kind"] == "wx"
    assert wx["wind_dir"] == 90 and abs(wx["wind_ms"] - 2.235) < 0.01 and abs(wx["gust_ms"] - 4.47) < 0.01
    assert abs(wx["temp_c"] - 10.0) < 0.01 and wx["humidity"] == 65 and wx["pressure_hpa"] == 1013.2
    assert abs(wx["rain_24h_mm"] - 0.254) < 1e-6
    q = P("RA3WX-13>APRS:_10231200c220s004g005t-04r000p000P000h50b09900")
    assert q["type"] == "weather" and q["wx"]["wind_dir"] == 220 and q["wx"]["temp_c"] < -19
    assert "lat" not in q
    assert "давление 990,0 гПа" in aprs.describe(q)


def test_parse_path_iss_digi_thirdparty_nmea():
    p = P("UT1FG>CQ,RS0ISS*,WIDE2-1:=5021.00N/03015.00E-via ISS")
    assert p["via_iss"] and p["via"] == "через МКС"
    assert P("RS0ISS>CQ:>ARISS")["kind"] == "iss"
    assert P("R2ABC>APRS,R3DIG-1,WIDE1*,WIDE2-1:>x")["via"] == "через R3DIG-1"
    assert P("R2ABC>APRS,WIDE1-1:>x")["via"] == "напрямую"
    t = P("R3GW>APRS:}DL1ABC>APRS,TCPIP,R3GW*:!4903.50N/00720.00E-gate")
    assert t["third_party"] and t["src"] == "DL1ABC" and t["gateway"] == "R3GW" and abs(t["lat"] - 49.0583) < 1e-3
    n = P("N0CALL>APRS:$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A")
    assert n["type"] == "nmea" and abs(n["lat"] - 48.1173) < 1e-4 and abs(n["course"] - 84.4) < 1e-6


def test_parse_cp1251_and_garbage():
    fr = aprs.make_frame("R2ABC", "APRS", [], ">Привет".encode("cp1251"))
    assert aprs.parse_frame(fr)["status"] == "Привет"
    for junk in ("R2ABC>APRS:!xx", "R2ABC>APRS:`", "R2ABC>APRS:;SHORT", "R2ABC>APRS:", "R2ABC>APRS:@12"):
        p = P(junk)
        assert isinstance(aprs.describe(p), str)


def test_describe_and_geo():
    p = P("UA3ABC-9>APRS:!5538.55N/03723.50E>088/036")
    d = aprs.describe(p)
    assert "Позиция" in d and "67 км/ч" in d and "курс 88°" in d
    assert aprs.locator(55.75580, 37.61730) == "KO85ts"
    assert abs(aprs.distance_km(55.75, 37.62, 59.94, 30.31) - 634) < 5


# ----------------------------------------------------------------- тестовый эфир и окно

def _air_packets(freq, seconds):
    from rtltv.modes.aprs_window import AprsTestSignal, RATE
    from rtltv.radio import RadioDemod
    g = AprsTestSignal(RATE)
    center = freq - 0.2 * RATE
    d = RadioDemod(RATE, "NFM")
    d.offset = freq - center
    dem = aprs.AfskDemodulator(FS)
    pk = []
    n = RATE // 10
    for _ in range(int(seconds * 10)):
        raw = np.frombuffer(g.render(center, n, 30.0), np.uint8).astype(np.float32)
        iq = ((raw[0::2] - 127.4) + 1j * (raw[1::2] - 127.4)).astype(np.complex64) / 128
        for f in dem.process(d.process(iq)):
            pk.append(aprs.parse_frame(f["data"]))
    return pk


def test_test_air_ground():
    """Генератор тестового эфира режима: 144,800 через RadioDemod даёт пакеты разных типов."""
    from rtltv.modes.aprs_window import F_GROUND
    pk = _air_packets(F_GROUND, 22)
    types = {p["type"] for p in pk}
    assert len(pk) >= 7
    assert "mic-e" in types and "weather" in types and "position" in types


def test_test_air_iss():
    from rtltv.modes.aprs_window import F_ISS
    pk = _air_packets(F_ISS, 21)
    assert len(pk) >= 2 and all(p["via_iss"] for p in pk)


@pytest.fixture(scope="module")
def qapp():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"          # окна не всплывают на экране
    if os.name == "nt":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(windir, "Fonts"))  # без этого у offscreen нет шрифтов
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Segoe UI", 9))
    yield app


def test_window_on_test_air(qapp, tmp_path):
    from PySide6.QtCore import QCoreApplication
    from rtltv.modes.aprs_window import AprsWindow
    w = AprsWindow({"source": "test", "nosave": True})
    w.resize(1280, 720)
    w.show()
    t0 = time.time()
    while time.time() - t0 < 40 and len([s for s in w.stations.values() if s["lat"] is not None]) < 4:
        QCoreApplication.processEvents()
        time.sleep(0.02)
    try:
        assert w.n_packets >= 4, f"пакетов {w.n_packets}"
        assert w.table.rowCount() >= 3
        assert w.feed.rowCount() == w.n_packets
        key = next(k for k, s in w.stations.items() if s["lat"] is not None)
        w._select(key)
        assert key in w.details.text() or w.stations[key]["call"] in w.details.text()
        assert w.scroll.horizontalScrollBar().maximum() == 0
        w.flt.setText("НЕТ-ТАКОГО")
        assert w.table.rowCount() == 0
        w.flt.setText("")
        assert w.table.rowCount() >= 3
        path = tmp_path / "aprs.png"
        assert w.grab().save(str(path))
    finally:
        w.close()


def main():
    print(f"{'ОСШ, дБ':>8s} {'принято':>10s}")
    for snr in (12, 6, 3, 1, 0, -1, -2, -3):
        ok = tot = 0
        for seed in range(6):
            fr, x = air(snr_db=snr, seed=seed)
            ok += got(fr, aprs.decode_audio(x))
            tot += len(fr)
        print(f"{snr:8d} {ok:4d} из {tot:3d}")


if __name__ == "__main__":
    main()
