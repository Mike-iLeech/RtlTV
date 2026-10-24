"""Тесты декодера DMR (rtltv/dmr.py) на синтетическом эфире.

Запуск: py -3.12 tests/test_dmr.py   (или py -3.12 -m pytest tests/test_dmr.py)
"""
import os
import sys
import time

import numpy as np
from scipy.signal import firwin, lfilter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from rtltv import dmr as D  # noqa: E402

FS = 48000


def channel(freq, snr_db=None, seed=1):
    """Частота -> FM (комплексная огибающая) + белый шум -> канальный ФНЧ -> дискриминатор."""
    if snr_db is None:
        return freq.astype(np.float32)
    rng = np.random.default_rng(seed)
    ph = 2 * np.pi * np.cumsum(freq.astype(np.float64)) / FS
    iq = np.exp(1j * ph)
    npow = (FS / 12500.0) * 10 ** (-snr_db / 10.0)          # ОСШ в полосе 12,5 кГц
    iq = iq + np.sqrt(npow / 2) * (rng.standard_normal(len(iq)) + 1j * rng.standard_normal(len(iq)))
    iq = lfilter(firwin(65, 7000, fs=FS), 1.0, iq)
    d = np.angle(iq[1:] * np.conj(iq[:-1])) * FS / (2 * np.pi)
    return np.concatenate([[0.0], d]).astype(np.float32)


def run(x, block=2400, slot_filter=0):
    dec = D.DmrDecoder()
    dec.slot_filter = slot_filter
    events, audio = [], []
    for i in range(0, len(x), block):
        a, ev = dec.process(x[i:i + block])
        audio.append(a)
        for e in ev:
            e["t"] = i / FS
            events.append(e)
    return dec, events, np.concatenate(audio) if audio else np.zeros(0, np.float32)


def starts(events):
    return [(e["slot"], e["src"], e["dst"], e["group"], e["cc"]) for e in events if e["type"] == "call_start"]


def ends(events):
    return [(e["slot"], e["src"], e["dst"]) for e in events if e["type"] == "call_end"]


EXPECT = [(1, 2501234, 91, True, 1), (2, 2507777, 2501234, False, 1)]


def bs_signal(offset=0.0, ppm=0.0, superframes=6, enc=False, cc=1, headers=2, late=0):
    """Два вызова на BS (слот 1, потом слот 2) с заданными искажениями передатчика."""
    idle = [D.idle_burst(cc) for _ in range(300)]
    bits = []
    for slot, lc in ((1, D._make_lc(2501234, 91, True, so=0x40 if enc else 0)),
                     (2, D._make_lc(2507777, 2501234, False))):
        call = D.voice_call(cc, lc, D._tone_frames(40 if slot == 1 else 25), superframes, headers=headers)
        call = call[late:]
        busy = idle[:3] + call + idle[:6]
        s1, s2 = (busy, idle[:len(busy)]) if slot == 1 else (idle[:len(busy)], busy)
        bits += D.bs_stream(s1, s2)
    f = D.modulate(bits, offset_hz=offset, ppm=ppm)
    return np.concatenate([np.zeros(3000, np.float32) + offset, f, np.zeros(9000, np.float32) + offset])


# ----------------------------------------------------------------------------- тесты
def test_codecs():
    lc = D._make_lc(2501234, 91)
    cw = list(lc) + D._rs129_parity(lc)
    assert D._rs129_check(cw) == cw
    bad = list(cw)
    bad[3] ^= 0x77
    assert D._rs129_check(bad) == cw
    bits = D._bytes_bits(cw)
    raw = D._bptc_encode(bits)
    rng = np.random.default_rng(5)
    for k in rng.choice(196, 2, replace=False):
        raw[k] ^= 1
    assert D._bptc_decode(raw)[1] == bits
    emb = D._emb_lc_encode(lc)
    emb[11] ^= 1
    assert D._emb_lc_decode(emb) == lc
    assert D._golay2087(1) & 0xFFF == 0x8EB and D._qr1676(1) == 0x273
    d = D.ambe_params(40, 0, 17, 5, 3)
    v = D.AmbeVoice()
    assert v.ok, "нет mbelib.dll"
    v.decode(D.ambe_encode(d))
    assert v.last_params() == d and v.errs == 0


def test_clean_test_signal():
    x = D.test_signal()
    assert 10 * FS < len(x) < 20 * FS
    calls0 = D.AmbeVoice.calls
    t = time.perf_counter()
    dec, ev, audio = run(x)
    dt = time.perf_counter() - t
    assert starts(ev) == EXPECT, starts(ev)
    assert ends(ev) == [e[:3] for e in EXPECT], ends(ev)
    # 2 вызова × 10 суперкадров × 6 пакетов × 3 кадра AMBE
    assert D.AmbeVoice.calls - calls0 == 2 * 10 * 6 * 3
    assert len(audio) == len(x)
    voiced = (np.abs(audio) > 1e-4).sum() / FS
    assert abs(voiced - 2 * 3.6) < 0.3, voiced
    print(f"  чистый сигнал {len(x) / FS:.1f} с: декодирование {dt:.2f} с ({len(x) / FS / dt:.0f}× быстрее "
          f"реального времени), голос {voiced:.2f} с, mbelib {D.AmbeVoice.calls - calls0} кадров")


def _impaired(offset, ppm, snr, seed):
    x = channel(bs_signal(offset=offset, ppm=ppm), snr, seed)
    dec, ev, audio = run(x)
    return dec, ev, audio, x


def test_offset_clock_noise():
    for offset, ppm, snr, seed in ((200, 50, 20, 1), (-200, -50, 20, 2), (200, -50, 16, 3), (-200, 50, 14, 4)):
        dec, ev, audio, x = _impaired(offset, ppm, snr, seed)
        st = starts(ev)
        assert st == EXPECT, (offset, ppm, snr, st)
        assert len(audio) == len(x)
        print(f"  сдвиг {offset:+d} Гц, такт {ppm:+d} ppm, ОСШ {snr} дБ: вызовы {st}, "
              f"EMB ok {dec.stats['emb_ok']}, LC ok {dec.stats['lc_ok']}")


def test_inverted_polarity():
    x = -bs_signal(offset=150, ppm=20)
    dec, ev, audio = run(channel(x, 22, 7))
    assert starts(ev) == EXPECT, starts(ev)
    assert dec.status()["polarity"] == -1 or not dec.status()["locked"]


def test_ms_direct():
    """MS-источник (без CACH): пакеты голоса через 60 мс, между ними — мусор."""
    rng = np.random.default_rng(3)
    lc = D._make_lc(1234567, 9, True)
    call = D.voice_call(7, lc, D._tone_frames(30), 5, fam=D.FAM_MS)
    bits = []
    for b in call:
        bits += list(rng.integers(0, 2, 24)) + list(b)
        bits += list(rng.integers(0, 2, 288))
    x = channel(np.concatenate([np.zeros(4000, np.float32), D.modulate(bits, offset_hz=-120, ppm=-30),
                                np.zeros(30000, np.float32)]), 20, 9)
    dec, ev, audio = run(x)
    st = starts(ev)
    assert st and st[0][1:] == (1234567, 9, True, 7), st
    assert any(e["type"] == "call_end" for e in ev)


def test_direct_mode_ts2():
    lc = D._make_lc(3101, 3102, False)
    call = D.voice_call(3, lc, D._tone_frames(30), 4, fam=D.FAM_DM2)
    bits = []
    for b in call:
        bits += [0] * 24 + list(b) + [0] * 288
    x = channel(D.modulate(bits, offset_hz=80), 24, 4)
    dec, ev, audio = run(x)
    st = starts(ev)
    assert st and st[0] == (2, 3101, 3102, False, 3), st


def test_encrypted_no_audio():
    x = bs_signal(enc=True)
    # только первый вызов (шифрованный)
    x = x[: len(x) // 2]
    dec, ev, audio = run(x)
    s = [e for e in ev if e["type"] == "call_start"]
    assert s and s[0]["encrypted"] and s[0]["src"] == 2501234
    assert np.abs(audio).max() == 0.0


def test_late_entry():
    # пропускаем заголовки и первые 4 голосовых пакета: ID берутся из встроенного LC
    x = channel(bs_signal(headers=2, late=2 + 4, superframes=5), 22, 5)
    dec, ev, audio = run(x)
    assert starts(ev) == EXPECT, starts(ev)


def test_slot_filter():
    x = D.test_signal()
    half = len(x) // 2
    _, _, a2 = run(x, slot_filter=2)
    assert np.abs(a2[: half - FS]).max() == 0.0          # первый вызов — слот 1, не слышен
    assert (np.abs(a2[half:]) > 1e-4).sum() > 3 * FS     # второй — слышен


def test_csbk():
    blk = [0x3D, 0x00, 0x40, 0x02, 0x00, 0x00, 0x5B, 0x26, 0x2A, 0x72]   # преамбула 2501234 → TG 91
    idle = [D.idle_burst(2) for _ in range(20)]
    s1 = idle[:3] + [D.csbk_burst(2, blk)] * 3 + idle[:6]
    x = D.modulate(D.bs_stream(s1, idle[:len(s1)]))
    dec, ev, audio = run(np.concatenate([x, np.zeros(9000, np.float32)]))
    c = [e for e in ev if e["type"] == "csbk"]
    assert len(c) == 1 and c[0]["src"] == 2501234 and c[0]["dst"] == 91 and c[0]["group"], c


def test_sample_drops():
    """Пропуски отсчётов (переполнение USB): захват восстанавливается без потери вызовов."""
    x = bs_signal(offset=100, ppm=10, superframes=8)
    rng = np.random.default_rng(2)
    keep, pos = [], 0
    while pos < len(x):
        n = int(rng.integers(30000, 60000))
        keep.append(x[pos:pos + n])
        pos += n + int(rng.integers(50, 900))          # выбрасываем 1–19 мс
    dec, ev, audio = run(channel(np.concatenate(keep), 22, 3))
    assert starts(ev) == EXPECT, starts(ev)
    assert not [e for e in ev if e["type"] == "sync_lost"][:-1], [e["t"] for e in ev if e["type"] == "sync_lost"]


def test_noise_no_false_events():
    rng = np.random.default_rng(11)
    # шум FM-приёмника без сигнала и с ним — немодулированная несущая
    x = channel(np.zeros(20 * FS, np.float32), -3, 12)
    x2 = (rng.standard_normal(10 * FS) * 2500).astype(np.float32)
    for sig in (x, x2):
        dec, ev, audio = run(sig)
        assert not ev, ev[:3]
        assert np.abs(audio).max() == 0.0


def main():
    tests = [(k, v) for k, v in globals().items() if k.startswith("test_") and callable(v)]
    bad = 0
    for name, fn in tests:
        t = time.perf_counter()
        try:
            fn()
            print(f"OK   {name} ({time.perf_counter() - t:.2f} с)")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"FAIL {name}: {e!r}")
    print("Итог:", "все тесты прошли" if not bad else f"упало {bad} из {len(tests)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
