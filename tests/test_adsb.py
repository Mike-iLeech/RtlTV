"""Тесты декодера ADS-B: эталонные сообщения («The 1090MHz Riddle»), CPR, кодирование ↔ декодирование,
синтетический IQ с шумом и дробными сдвигами импульсов, демо-генератор целиком.

Запуск:  py -3.12 -m pytest tests/test_adsb.py -q   (или просто py -3.12 tests/test_adsb.py)
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtltv import adsb  # noqa: E402


def approx(a, b, tol):
    return abs(a - b) <= tol


# ---------------------------------------------------------------- эталоны
def test_crc_and_identification():
    m, syn = adsb.decode_hex("8D4840D6202CC371C32CE0576098")
    assert syn == 0
    assert m["df"] == 17 and m["icao"] == 0x4840D6
    assert m["tc"] == 4 and m["callsign"] == "KLM1023"


def test_crc_detects_error():
    b = bytearray.fromhex("8D4840D6202CC371C32CE0576098")
    b[6] ^= 0x10
    assert adsb.syndrome(b) != 0
    assert adsb.syndrome(b) in adsb._FIX1  # одиночная ошибка исправима


def test_airborne_position_global():
    e, se = adsb.decode_hex("8D40621D58C382D690C8AC2863A7")
    o, so = adsb.decode_hex("8D40621D58C386435CC412692AD6")
    assert se == 0 and so == 0
    assert e["alt"] == 38000 and o["alt"] == 38000
    assert e["cpr"][0] == 0 and o["cpr"][0] == 1
    lat, lon = adsb.cpr_global(e["cpr"][1:3], o["cpr"][1:3], newest_odd=False)
    assert approx(lat, 52.25720, 1e-4) and approx(lon, 3.91937, 1e-4)


def test_airborne_position_local():
    e, _ = adsb.decode_hex("8D40621D58C382D690C8AC2863A7")
    lat, lon = adsb.cpr_local(e["cpr"][1], e["cpr"][2], 0, 52.258, 3.918)
    assert approx(lat, 52.25720, 1e-4) and approx(lon, 3.91937, 1e-4)


def test_velocity_ground_speed():
    m, syn = adsb.decode_hex("8D485020994409940838175B284F")
    assert syn == 0 and m["tc"] == 19
    assert approx(m["gs"], 159.20, 0.01)
    assert approx(m["track"], 182.88, 0.01)
    assert m["vr"] == -832


def test_velocity_airspeed():
    m, syn = adsb.decode_hex("8DA05F219B06B6AF189400CBC33F")
    assert syn == 0
    assert approx(m["heading"], 243.98, 0.01)
    assert m["tas"] == 375 and m["vr"] == -2304


def test_target_state_tc29():
    m, syn = adsb.decode_hex("8DA05629EA21485CBF3F8CADAEEB")
    assert syn == 0 and m["tc"] == 29
    assert m["sel_alt"] == 16992 and approx(m["qnh"], 1012.8, 0.01) and approx(m["sel_hdg"], 66.8, 0.1)


def test_no_position_tc0_altitude():
    """TC=0: борт без координат (подавление GNSS), но с барометрической высотой — так в живом эфире."""
    fr = adsb.make_df17(0x151E04, adsb.encode_ac12(35000) << 36)
    m = adsb.decode_frame(fr)
    assert m["tc"] == 0 and m["alt"] == 35000 and m["no_pos"]
    t = adsb.Tracker(home=(55.75, 37.62))
    a = t.apply(m, now=1.0)
    assert a.no_pos and a.lat is None and a.alt == 35000


def test_df20_altitude_address_parity():
    m, syn = adsb.decode_hex("A0001838CA3E51F0A8000047A36A")
    assert m["df"] == 20 and m["alt"] == 38000
    assert m["icao"] == syn  # адрес = синдром (поле «адрес/чётность»)


def test_gillham():
    """Код Гиллхэма (Mode C без Q-бита): все 1280 допустимых кодов дают каждую высоту
    от −1200 до 126 700 футов с шагом 100 ровно один раз."""
    alts = [adsb.gillham_to_ft(h) for h in range(0x8000) if not (h & 0x8888)]
    alts = [x for x in alts if x is not None]
    assert len(alts) == 1280 and sorted(alts) == list(range(-1200, 126800, 100))
    # через 13-битное поле AC (DF4/20, Q=0, M=0) и 12-битное (позиция ADS-B) — те же высоты
    for h in range(0, 0x8000, 7):
        if h & 0x8888 or adsb.gillham_to_ft(h) is None:
            continue
        ac13 = adsb._hex_to_id13(h)
        assert adsb.decode_ac13(ac13) == adsb.gillham_to_ft(h)
        ac12 = ((ac13 >> 1) & 0xFC0) | (ac13 & 0x3F)
        assert adsb.decode_ac12(ac12) == adsb.gillham_to_ft(h)


# ---------------------------------------------------------------- кодирование ↔ декодирование
def test_roundtrip_encoders():
    icao = 0x155ABC
    fr = adsb.make_df17(icao, adsb.me_ident("AFL1124", 4, 3))
    m = adsb.decode_frame(fr)
    assert adsb.syndrome(fr) == 0 and m["callsign"] == "AFL1124" and m["category"].startswith("A3")
    fr = adsb.make_df17(icao, adsb.me_velocity(420.0, 135.0, -1280))
    m = adsb.decode_frame(fr)
    assert approx(m["gs"], 420, 1.0) and approx(m["track"], 135, 0.3) and m["vr"] == -1280
    fr = adsb.make_df4(icao, 33725)
    assert adsb.syndrome(fr) == icao and adsb.decode_frame(fr)["alt"] == 33725
    fr = adsb.make_df5(icao, "7421")
    assert adsb.syndrome(fr) == icao and adsb.decode_frame(fr)["squawk"] == "7421"
    fr = adsb.make_df11(icao)
    assert adsb.syndrome(fr) == 0 and adsb.decode_frame(fr)["icao"] == icao


def test_cpr_roundtrip_many():
    rng = np.random.default_rng(1)
    for _ in range(300):
        lat, lon = rng.uniform(-80, 80), rng.uniform(-179, 179)
        e = adsb.cpr_encode(lat, lon, False)
        o = adsb.cpr_encode(lat, lon, True)
        r = adsb.cpr_global(e, o, newest_odd=True)
        if r is None:  # на границе широтных зон пара неоднозначна — так и должно быть
            continue
        assert adsb.distance_km(lat, lon, *r) < 0.02
        r2 = adsb.cpr_local(e[0], e[1], 0, lat + 0.5, lon - 0.5)
        assert adsb.distance_km(lat, lon, *r2) < 0.02


def test_surface_position_local():
    lat, lon = 55.9726, 37.4146  # Шереметьево
    fr = adsb.make_df17(0x1556E2, adsb.me_surface_pos(lat, lon, True, 12.0, 90.0), ca=4)
    m = adsb.decode_frame(fr)
    assert m["on_ground"] and m["tc"] == 7 and approx(m["track"], 90, 3) and approx(m["gs"], 12, 0.6)
    r = adsb.cpr_local(m["cpr"][1], m["cpr"][2], m["cpr"][0], 55.7558, 37.6173, surface=True)
    assert adsb.distance_km(lat, lon, *r) < 0.01


def test_tracker_position_and_stale():
    t = adsb.Tracker(home=(52.0, 4.0))
    for i, h in enumerate(("8D40621D58C382D690C8AC2863A7", "8D40621D58C386435CC412692AD6")):
        t.apply(adsb.decode_frame(bytes.fromhex(h)), now=100.0 + i)
    a = t.planes[0x40621D]
    assert approx(a.lat, 52.2658, 1e-3) and approx(a.lon, 3.9389, 1e-3) and a.alt == 38000
    assert a.dist is not None and 20 < a.dist < 40
    # DF4 от неизвестного борта борт не создаёт
    assert t.apply(adsb.decode_frame(adsb.make_df4(0x123456, 1000)), now=101.0) is None
    t.expire(now=101.0 + adsb.STALE_S + 1)
    assert not t.planes


def test_tracker_keep_and_history():
    """Борт держится keep_s (None — весь сеанс), а история сеанса не теряет никого."""
    hist = {}
    t = adsb.Tracker(home=(55.75, 37.62), keep_s=15 * 60, history=hist)
    t.apply(adsb.decode_frame(adsb.make_df17(0x151E04, adsb.encode_ac12(35000) << 36)), now=0.0)
    t.apply(adsb.decode_frame(adsb.make_df17(0x151E04, adsb.me_ident("SDM6569"))), now=1.0)
    t.apply(adsb.decode_frame(adsb.make_df17(0x151E04, adsb.encode_ac12(34000) << 36)), now=2.0)
    t.expire(now=300.0)                     # 5 мин тишины: давно не слышен, но в таблице
    assert 0x151E04 in t.planes
    assert 0x151E04 not in t.known(now=300.0)  # для «адрес/чётность» уже не используется
    t.expire(now=2.0 + 15 * 60 + 1)
    assert not t.planes
    h = hist[0x151E04]
    assert h["callsign"] == "SDM6569" and h["msgs"] == 3 and h["alt_min"] == 34000 and h["alt_max"] == 35000
    assert h["no_pos"] and not h["had_pos"]
    t2 = adsb.Tracker(keep_s=None)
    t2.apply(adsb.decode_frame(adsb.make_df11(0x123456)), now=0.0)
    t2.expire(now=1e6)
    assert 0x123456 in t2.planes


def test_single_cpr_from_home():
    """Одиночное воздушное CPR (только чётные) декодируется относительно приёмника, пара его подтверждает."""
    home = (55.7558, 37.6173)
    lat, lon = adsb.destination(home[0], home[1], 40.0, 180.0)
    t = adsb.Tracker(home=home)
    m = adsb.decode_frame(adsb.make_df17(0x151111, adsb.me_airborne_pos(lat, lon, 33000, False)))
    a = t.apply(m, now=1.0)
    assert a.pos_src == "single" and not a.confirmed
    assert adsb.distance_km(lat, lon, a.lat, a.lon) < 0.05
    assert "одиночному" in m["pos_note"]
    t.apply(adsb.decode_frame(adsb.make_df17(0x151111, adsb.me_airborne_pos(lat, lon, 33000, True))), now=1.5)
    assert a.pos_src == "pair" and a.confirmed
    # дальше 300 км одиночное сообщение от приёмника не декодируем (неоднозначно)
    far = adsb.destination(home[0], home[1], 40.0, 450.0)
    t2 = adsb.Tracker(home=home)
    b = t2.apply(adsb.decode_frame(adsb.make_df17(0x152222, adsb.me_airborne_pos(*far, 33000, False))), now=1.0)
    assert b.lat is None or adsb.distance_km(*far, b.lat, b.lon) < 0.05


def test_reject_far_and_jump_with_reason():
    home = (55.7558, 37.6173)
    t = adsb.Tracker(home=home)
    far = adsb.destination(home[0], home[1], 250.0, 1240.0)
    for i, odd in enumerate((False, True)):
        m = adsb.decode_frame(adsb.make_df17(0x153333, adsb.me_airborne_pos(*far, 35000, odd)))
        a = t.apply(m, now=10.0 + i)
    assert a.lat is None and a.suspect and a.rejected >= 1 and t.rejected >= 1
    assert "км от приёмника" in a.raw_note and "подмена GNSS" in a.raw_note
    assert adsb.distance_km(*far, a.raw_lat, a.raw_lon) < 0.1
    assert "позиция отброшена" in adsb.describe(m)
    # скачок: подтверждённая позиция, через 2 с — на 85 км в сторону
    p0 = adsb.destination(home[0], home[1], 90.0, 100.0)
    t = adsb.Tracker(home=home)
    for i, odd in enumerate((False, True)):
        a = t.apply(adsb.decode_frame(adsb.make_df17(0x154444, adsb.me_airborne_pos(*p0, 35000, odd))), now=20.0 + i)
    assert a.confirmed
    p1 = adsb.destination(*p0, 0.0, 85.0)
    m = adsb.decode_frame(adsb.make_df17(0x154444, adsb.me_airborne_pos(*p1, 35000, False)))
    t.apply(m, now=23.0)
    assert m["pos_note"].startswith("скачок 85 км за 2 с") and a.suspect
    assert adsb.distance_km(*p0, a.lat, a.lon) < 0.1          # прежняя позиция сохранилась
    # одно и то же «новое место» JUMP_ACCEPT раз подряд — принимаем
    for k in range(adsb.JUMP_ACCEPT):
        t.apply(adsb.decode_frame(adsb.make_df17(0x154444, adsb.me_airborne_pos(*p1, 35000, bool(k % 2)))),
                now=24.0 + k)
    assert adsb.distance_km(*p1, a.lat, a.lon) < 0.1 and not a.suspect


def test_history_track_thinning_and_export(tmp_path=None):
    """История хранит трек каждого борта (прореженный, с потолком), экспорт в KML и CSV."""
    import tempfile
    home = (55.7558, 37.6173)
    hist = {}
    t = adsb.Tracker(home=home, keep_s=60, history=hist)
    lat, lon = adsb.destination(home[0], home[1], 0.0, 50.0)
    now = 0.0
    for k in range(4000):                       # ~1,1 ч полёта 360 км/ч, сообщение раз в секунду
        lat, lon = adsb.destination(lat, lon, 90.0, 0.1)
        fr = adsb.make_df17(0x155555, adsb.me_airborne_pos(lat, lon, 30000, bool(k % 2)))
        t.apply(adsb.decode_frame(fr), now=now)
        now += 1.0
    h = hist[0x155555]
    assert 50 < len(h["track"]) <= adsb.HIST_TRACK_MAX      # не каждая точка, но и не пусто
    assert adsb.distance_km(h["track"][-1][0], h["track"][-1][1], lat, lon) < 1.5
    t.expire(now=now + 3600)                    # из таблицы удалён — в истории остался
    assert not t.planes and hist[0x155555]["track"]
    from rtltv.modes.adsb_window import AdsbWindow
    d = tmp_path or tempfile.mkdtemp()
    snap = [dict(x, track=list(x["track"]), raw=list(x["raw"])) for x in hist.values()]
    kml = os.path.join(str(d), "h.kml")
    assert AdsbWindow._write_history(kml, snap) == 1
    txt = open(kml, encoding="utf-8").read()
    assert "<LineString>" in txt and txt.count(",") > 100
    csvp = os.path.join(str(d), "h.csv")
    AdsbWindow._write_history(csvp, snap)
    assert len(open(csvp, encoding="utf-8-sig").read().splitlines()) == len(h["track"]) + 1


def test_country_by_icao_offline():
    from rtltv.aircraft_info import country_by_icao
    cases = {0x151E04: "ru", 0x4840D6: "nl", 0x89618C: "ae", 0xA0F1B2: "us", 0x3C6444: "de", 0x400F00: "gb",
             0x4B8E01: "tr", 0x780A3B: "cn", 0x7C1234: "au", 0x683123: "kz", 0x510123: "by", 0x508123: "ua",
             0x896180: "ae", 0xC01234: "ca", 0x06A123: "qa", 0x600900: "az"}
    for icao, cc in cases.items():
        assert country_by_icao(icao)[0] == cc, hex(icao)
    assert country_by_icao(0x151E04)[1] == "Россия"
    assert country_by_icao(0x000001) == (None, None)       # вне выделенных блоков


def test_airline_and_private_offline():
    from rtltv.aircraft_info import AircraftInfo, airline_by_callsign, looks_like_registration
    assert airline_by_callsign("AFL1124") == ("AFL", "Аэрофлот", "ru")
    assert airline_by_callsign("SDM6569")[1] == "Россия"
    assert airline_by_callsign("XYZ123") == ("XYZ", None, None)
    assert airline_by_callsign("RA67221") is None and looks_like_registration("RA67221")
    assert looks_like_registration("N123AB") and not looks_like_registration("AFL31")
    assert not looks_like_registration("FV6555") and not looks_like_registration("SU1124")
    import tempfile
    info = AircraftInfo(cache_dir=tempfile.mkdtemp(), online=False)
    c = info.card(0x151E04, "SDM6569")
    assert c["cc"] == "ru" and c["airline"] == "Россия" and c["private"] is False and not c["pending"]
    c = info.card(0x151F00, "RA67221")
    assert c["private"] is True and c["airline"] is None
    c = info.card(0x896180, None)
    assert c["country"] == "ОАЭ" and c["private"] is None


def test_aircraft_info_cache_merges_online_data():
    """Онлайн-ответ (как от api.adsbdb.com) из кэша дополняет карточку: рег., тип, маршрут."""
    import tempfile
    import time as _t
    from rtltv.aircraft_info import AircraftInfo
    d = tempfile.mkdtemp()
    info = AircraftInfo(cache_dir=d, online=False)
    info.ac["151E04"] = {"t": _t.time(), "ok": True, "reg": "RA-73220", "type": "A319 111", "icao_type": "A319",
                         "manufacturer": "Airbus", "owner": "Rossiya Russian Airlines", "op_code": "SDM",
                         "src": "adsbdb.com"}
    info.cs["SDM6569"] = {"t": _t.time(), "ok": True, "airline": "Rossiya", "origin": {"iata": "LED", "city": "СПб"},
                          "dest": {"iata": "SVO", "city": "Москва"}, "src": "adsbdb.com"}
    info.dirty = True
    info.save()
    info2 = AircraftInfo(cache_dir=d, online=False)
    c = info2.card(0x151E04, "SDM6569")
    assert c["reg"] == "RA-73220" and c["icao_type"] == "A319" and c["dest"]["iata"] == "SVO"
    assert c["airline"] == "Россия"                          # офлайн-название по-русски приоритетнее


def test_history_save_load_and_retention():
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "h.json")
    hist = {}
    t = adsb.Tracker(home=(55.75, 37.62), history=hist)
    t.apply(adsb.decode_frame(adsb.make_df17(0x151E04, adsb.me_ident("SDM6569"))), now=1000.0)
    old = adsb.Tracker(history={})
    old.apply(adsb.decode_frame(adsb.make_df11(0x4840D6)), now=1000.0 - 10 * 86400)
    adsb.save_history(list(hist.values()) + list(old.history.values()), path)
    got = adsb.load_history(path, keep_days=7, now=1000.0)
    assert set(got) == {0x151E04} and got[0x151E04]["prev"] and got[0x151E04]["callsign"] == "SDM6569"
    assert set(adsb.load_history(path, keep_days=0, now=1000.0)) == {0x151E04, 0x4840D6}
    # борт из прошлого сеанса услышан снова — запись продолжается и больше не «прошлая»
    t2 = adsb.Tracker(history=got)
    t2.apply(adsb.decode_frame(adsb.make_df11(0x151E04)), now=2000.0)
    assert "prev" not in got[0x151E04] and got[0x151E04]["msgs"] == 2
    assert adsb.load_history(os.path.join(os.path.dirname(path), "нет.json")) == {}


def test_level_state():
    base = {"clip": 0.0, "frames_max": -10.0, "frames_p90": -15.0}
    assert adsb.level_state(base) == "норма"
    assert adsb.level_state(dict(base, frames_max=-1.0)) == "перегрузка"
    assert adsb.level_state(dict(base, clip=0.01)) == "перегрузка"
    assert adsb.level_state(dict(base, frames_p90=-35.0, frames_max=-30.0)) == "мало"
    assert adsb.level_state(dict(base, frames_p90=None, frames_max=None)) == "мало"
    p = adsb.AdsbProcessor()
    src = adsb.DemoSource(lambda *a: None, seed=3)
    src.open()
    for k in range(20):
        p.process(src.render(131072), now=1000 + k * 0.0655)
    st = p.level_stats()
    assert st["frames_p90"] is not None and -40 < st["noise_db"] < 0 and st["state"] in ("норма", "перегрузка")


def test_describe_russian():
    m = adsb.decode_frame(bytes.fromhex("8D4840D6202CC371C32CE0576098"))
    assert adsb.describe(m) == "позывной KLM1023" and adsb.msg_type(m) == "DF17/TC4"
    m = adsb.decode_frame(bytes.fromhex("8DA05F219B06B6AF189400CBC33F"))
    d = adsb.describe(m)
    assert "скорость 694 км/ч TAS, курс 244°" in d and "вертикальная -11.7 м/с" in d
    m = adsb.decode_frame(adsb.make_df17(0x151E04, adsb.encode_ac12(35000) << 36))
    assert adsb.describe(m) == "высота 10 668 м, нет координат (TC0)"
    assert adsb.describe(m, metric=False) == "высота 35 000 фут, нет координат (TC0)"
    assert adsb.describe(adsb.decode_frame(adsb.make_df5(1, "0730"), 1)) == "сквок 0730"
    m = adsb.decode_frame(bytes.fromhex("8D40621D58C382D690C8AC2863A7"))
    assert adsb.describe(m, pos=(52.2572, 3.9194)).endswith("позиция 52.2572, 3.9194")
    assert "ждём пару" in adsb.describe(m)
    assert adsb.describe(adsb.decode_frame(adsb.make_df11(5))) == "ответ на общий запрос (DF11)"


# ---------------------------------------------------------------- синтетический IQ
def _iq_with(frames, amp, sigma, rng, fs=2_000_000):
    n = 20000
    x = np.zeros(n, np.complex64)
    pos = 500
    for fr in frames:
        env = adsb.render_envelope(fr, fs, rng.random())
        x[pos:pos + env.size] += amp * env * np.exp(1j * rng.uniform(0, 2 * np.pi))
        pos += env.size + int(rng.integers(50, 300))
    x += (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * sigma
    return x


def test_synthetic_iq_decoding():
    rng = np.random.default_rng(7)
    icao = 0x4840D6
    frames = [bytes.fromhex("8D4840D6202CC371C32CE0576098"), adsb.make_df11(icao)] * 20
    d = adsb.Demodulator()
    got = []
    for _ in range(3):
        got += d.feed(_iq_with(frames, 0.4, 0.035, rng), known={icao})
    ok = sum(1 for f in got if f[0] in frames)
    assert ok >= 0.85 * 3 * len(frames), ok
    assert all(f[0] in frames for f in got)  # ложных кадров нет


def test_synthetic_noise_only_no_false_frames():
    rng = np.random.default_rng(3)
    d = adsb.Demodulator()
    bad = 0
    for _ in range(20):
        x = (rng.standard_normal(131072) + 1j * rng.standard_normal(131072)).astype(np.complex64) * 0.05
        bad += len(d.feed(x))
    assert bad == 0


def test_block_boundary():
    """Кадр, разрезанный границей блоков, всё равно декодируется (хвост переносится)."""
    rng = np.random.default_rng(5)
    fr = bytes.fromhex("8D4840D6202CC371C32CE0576098")
    x = _iq_with([fr], 0.5, 0.02, rng)
    d = adsb.Demodulator()
    got = d.feed(x[:560]) + d.feed(x[560:])
    assert [g[0] for g in got] == [fr]


def test_rate_2400k():
    rng = np.random.default_rng(11)
    fr = bytes.fromhex("8D4840D6202CC371C32CE0576098")
    x = _iq_with([fr] * 20, 0.5, 0.02, rng, fs=2_400_000)
    got = adsb.Demodulator(2_400_000).feed(x)
    assert sum(1 for g in got if g[0] == fr) >= 12


def test_demo_source_end_to_end():
    src = adsb.DemoSource(lambda *a: None, seed=2, planes=10)
    src.open()
    p = adsb.AdsbProcessor(home=src.home)
    for k in range(200):  # ≈13 с эфира
        p.process(src.render(131072), now=1000.0 + k * 0.0655)
    truth = {pl.icao: pl for pl in src.planes}
    assert set(p.tracker.planes) <= set(truth)          # ни одного ложного борта
    nopos = [a for a in p.tracker.planes.values() if truth[a.icao].no_pos]
    assert all(a.lat is None for a in nopos)
    assert sum(1 for a in nopos if a.no_pos and a.alt) >= 2
    assert len(p.drain_log()) == p.msgs and not p.drain_log()
    assert len(p.tracker.planes) >= 8
    with_pos = [a for a in p.tracker.planes.values() if a.lat is not None]
    assert len(with_pos) >= 7
    spoof = [a for a in p.tracker.planes.values() if truth[a.icao].spoof]
    assert spoof and spoof[0].rejected > 0 and spoof[0].suspect      # подмена GNSS замечена
    even = [a for a in p.tracker.planes.values() if truth[a.icao].even_only]
    assert even and even[0].lat is not None                           # только чётные CPR — позиция есть
    for a in with_pos:
        pl = truth[a.icao]
        if pl.spoof:
            continue
        assert adsb.distance_km(a.lat, a.lon, pl.lat, pl.lon) < 2.0
        if not pl.ground:
            assert abs(a.alt - pl.alt) < 300
    assert len(p.tracker.history) == len(p.tracker.planes)
    assert p.msgs > 0.5 * src.sent


if __name__ == "__main__":
    import time
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            t = time.perf_counter()
            fn()
            n += 1
            print(f"OK  {name}  {time.perf_counter() - t:.2f} с")
    print(f"Все {n} тестов прошли")


def _plane(i, now, **kw):
    a = {"hex": f"AB{i:04X}", "lat": None, "lon": None, "suspect": False, "last": now, "alt": 10000 + i * 100,
         "track": 90.0, "heading": None, "callsign": f"TST{i}", "on_ground": False, "signal": -10.0,
         "raw_lat": None, "raw_lon": None, "raw_note": ""}
    a.update(kw)
    return a


def test_overlay_list_scroll_survives_refresh(monkeypatch):
    """Список «Без координат» поверх карты: высота ровно под строки (без полосы прокрутки при ≤10), при >10 —
    прокрутка, которая НЕ сбрасывается обновлением раз в 0,5 с; колесо не уходит карте."""
    import time as _t
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication
    from rtltv import settings
    from rtltv.modes.adsb_window import AdsbWindow
    monkeypatch.setattr(settings, "save", lambda *_a: None)
    app = QApplication.instance() or QApplication([])
    w = AdsbWindow({"source": "test", "theme": "Тёмная", "shot": "x"})
    w.timer.stop()
    w.resize(1300, 820)
    w.show()
    app.processEvents()
    now = _t.time()
    lw = w.ovl_list
    w._update_overlay([_plane(i, now) for i in range(4)], now)
    app.processEvents()
    assert lw.count() == 4 and lw.verticalScrollBar().maximum() == 0   # всё влезло — прокрутки нет
    assert lw.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
    planes = [_plane(i, now) for i in range(25)]
    w._update_overlay(planes, now)
    app.processEvents()
    sb = lw.verticalScrollBar()
    assert lw.count() == 25 and sb.maximum() > 0
    first = lw.item(0)
    sb.setValue(sb.maximum() // 2)
    pos = sb.value()
    for _ in range(3):   # обновления по таймеру — прокрутка на месте, строки те же объекты
        w._update_overlay(planes, now + 1)
        app.processEvents()
    assert sb.value() == pos and lw.item(0) is first
    sb.setValue(sb.maximum())
    ev = QWheelEvent(QPointF(10, 10), QPointF(lw.mapToGlobal(QPoint(10, 10))), QPoint(0, 0), QPoint(0, -120),
                     Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    zoom = w.map.zoom
    QApplication.sendEvent(lw.viewport(), ev)
    assert ev.isAccepted() and w.map.zoom == zoom   # у края списка карта не масштабируется
    w._update_overlay(planes[:3], now)   # список сократился — без прокрутки и пустых строк
    app.processEvents()
    assert lw.count() == 3 and lw.verticalScrollBar().maximum() == 0
    w.close()


def test_overlay_header_row_drops_plane_color():
    """Строки списка переиспользуются: бывшая строка борта, ставшая заголовком группы, теряет цвет высоты."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from rtltv.modes.adsb_window import _OverlayList
    _app = QApplication.instance() or QApplication([])  # noqa: F841 — нужен для виджета
    lw = _OverlayList()
    lw.set_rows([("AFL1  3 000 м", "AB0001", "#ff0000", "", False)])
    assert lw.item(0).data(Qt.ForegroundRole) is not None and lw.item(0).flags() & Qt.ItemIsSelectable
    lw.set_rows([("Подозрительные (1)", None, None, "", True), ("AFL1  3 000 м", "AB0001", "#ff0000", "", False)])
    head = lw.item(0)
    assert head.data(Qt.ForegroundRole) is None and head.font().bold() and not head.flags() & Qt.ItemIsSelectable
    assert head.data(Qt.UserRole) is None and lw.item(1).data(Qt.UserRole) == "AB0001"
