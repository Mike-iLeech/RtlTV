"""Тесты декодера «Метеор-М» LRPT: синтетика (свой модулятор QPSK/OQPSK с доплером и шумом) и запись.

Запуск:  py -3.12 -m pytest tests/test_lrpt.py -q
Запись пролёта: путь в переменной RTLTV_LRPT_SAMPLE (2-канальный WAV/IQ), иначе тест пропускается.
"""
import os
import sys
import time

import numpy as np
import pytest
from scipy import signal as sps

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtltv import lrpt  # noqa: E402

SAMPLE = os.environ.get("RTLTV_LRPT_SAMPLE", "")


# ---------------------------------------------------------------- синтетический передатчик

def make_cadus(n, rng, vcid=5, dual=False):
    """n кадров CADU со случайным содержимым (без пакетов)."""
    vcdus = []
    for k in range(n):
        vc = rng.integers(0, 256, 892).astype(np.uint8)
        vc[:10] = [0x40, vcid, (k >> 16) & 255, (k >> 8) & 255, k & 255, 0, 0, 0, 0x07, 0xFF]
        vcdus.append(vc)
    return lrpt.vcdus_to_cadus(vcdus, dual)


def modulate(cadus, sps_=16, oqpsk=True, nrzm=True):
    return lrpt.Modulator(sps_, oqpsk, nrzm).modulate(cadus)


def channel(x, fs, f0=1500.0, rate=-40.0, esn0_db=12.0, sps_=16, rng=None, phase=0.7):
    """Доплер (f0 + rate·t), случайная фаза, АБГШ с заданным Es/N0."""
    t = np.arange(x.size) / fs
    ph = 2 * np.pi * (f0 * t + 0.5 * rate * t * t) + phase
    y = x * np.exp(1j * ph).astype(np.complex64)
    p = np.mean(np.abs(x) ** 2) * sps_          # энергия на символ
    n0 = p / (10 ** (esn0_db / 10))
    sigma = np.sqrt(n0 / 2)
    rng = rng or np.random.default_rng(0)
    y = y + (sigma * (rng.standard_normal(x.size) + 1j * rng.standard_normal(x.size))).astype(np.complex64)
    return y.astype(np.complex64)


def run_rx(y, fs, oqpsk, block=131072):
    rx = lrpt.LrptReceiver(fs, oqpsk)
    for k in range(0, y.size, block):
        rx.feed(y[k:k + block])
    rx.finish()
    return rx


# ---------------------------------------------------------------- блоки

def test_pn_sequence():
    assert list(lrpt.pn()[:8]) == [0xFF, 0x48, 0x0E, 0xC0, 0x9A, 0x0D, 0x70, 0xBC]


@pytest.mark.parametrize("dual", [False, True])
def test_reed_solomon(dual):
    rng = np.random.default_rng(1)
    d = rng.integers(0, 256, 223).astype(np.uint8)
    cw = np.concatenate([d, lrpt.rs_encode(d, dual)])
    assert lrpt.rs_decode(cw.copy(), dual) == 0
    e = cw.copy()
    idx = rng.choice(255, 16, replace=False)
    e[idx] ^= rng.integers(1, 256, 16).astype(np.uint8)
    assert lrpt.rs_decode(e, dual) == 16
    assert np.array_equal(e, cw)
    e = cw.copy()
    idx = rng.choice(255, 20, replace=False)
    e[idx] ^= rng.integers(1, 256, 20).astype(np.uint8)
    assert lrpt.rs_decode(e, dual) == -1


def test_viterbi_noise():
    rng = np.random.default_rng(2)
    b = rng.integers(0, 2, 100000).astype(np.uint8)
    enc, _ = lrpt.conv_encode(b)
    soft = np.clip((enc.astype(np.float32) * 2 - 1) * 40 + rng.normal(0, 25, enc.size), -127, 127).astype(np.int8)
    raw_ber = np.mean((soft > 0) != enc)
    out = lrpt.viterbi(soft)
    ber = np.mean(out[:-64] != b[:-64])
    assert raw_ber > 0.04 and ber < 2e-4


def test_huffman_tables():
    # стандартные таблицы JPEG: 12 категорий DC, 162 символа AC
    assert len(lrpt.DC_CODES) == 12 and len(lrpt.AC_CODES) == 162
    assert lrpt.AC_CODES[0x00] == (0b1010, 4) and lrpt.AC_CODES[0xF0] == (0b11111111001, 11)


# ---------------------------------------------------------------- демодулятор + кадры на синтетике

@pytest.mark.parametrize("oqpsk,nrzm,fs_out", [(True, True, 1024000), (False, False, 1152000)])
def test_synthetic_chain(oqpsk, nrzm, fs_out):
    rng = np.random.default_rng(3)
    n = 40
    cadus = make_cadus(n, rng)
    fs = 72000 * 16
    x = modulate(cadus, 16, oqpsk, nrzm)
    y = channel(x, fs, f0=2300.0, rate=-45.0, esn0_db=9.0, rng=rng)
    if fs_out != fs:
        y = sps.resample_poly(y, 8, 9).astype(np.complex64)      # 1,152 → 1,024 МГц
    t0 = time.perf_counter()
    rx = run_rx(y, fs_out, oqpsk)
    dt = time.perf_counter() - t0
    st = rx.stats()
    dur = y.size / fs_out
    print(f"\nOQPSK={oqpsk} NRZM={nrzm} fs={fs_out}: кадров {st['frames_ok']}/{n}, RS {st['rs_pct']:.0f}%, "
          f"nrzm={st['nrzm']}, остаток {st['residual_hz']:.0f} Гц, {dur / dt:.1f}x реального времени")
    assert st["frames_ok"] >= n - 6           # первые кадры уходят на захват
    assert st["nrzm"] == nrzm
    assert dur / dt > 1.5                     # быстрее реального времени


def test_synthetic_low_snr_oqpsk():
    """Es/N0 = 4 дБ (≈ порог) — большинство кадров должно пройти RS."""
    rng = np.random.default_rng(4)
    n = 40
    cadus = make_cadus(n, rng)
    fs = 72000 * 16
    y = channel(modulate(cadus, 16, True, True), fs, f0=-1800.0, rate=30.0, esn0_db=4.0, rng=rng)
    st = run_rx(y, fs, True).stats()
    print(f"\nEs/N0 4 дБ: кадров {st['frames_ok']}/{n}, исправлено байт RS {st['rs_fixed']}")
    assert st["frames_ok"] >= n * 0.6


def test_image_codec_roundtrip():
    """Сжатие МКУ (DC) → пакеты → VCDU → разбор → картинка совпадает с усреднением 8×8."""
    imgs = {64 + k: lrpt.synth_image(64, k) for k in range(3)}
    pk = lrpt.synth_packets(imgs)
    dm, im = lrpt.Demux(), lrpt.Imager()
    for v in lrpt.packets_to_vcdus(pk):
        for a, seq, pl in dm.push(v):
            im.push(a, seq, pl)
    assert dm.packets == len(pk)
    for a, ref in imgs.items():
        out = im.image(a)
        avg = ref.reshape(8, 8, 196, 8).mean(axis=(1, 3)).repeat(8, 0).repeat(8, 1)
        assert out.shape == ref.shape
        assert np.abs(out.astype(float) - avg).mean() < 1.0


def test_synthetic_image_over_air():
    """Полная цепочка с картинкой: OQPSK + NRZ-M + доплер + шум → снимок 3 каналов."""
    rng = np.random.default_rng(5)
    imgs = {64 + k: lrpt.synth_image(96, k) for k in range(3)}
    cad = lrpt.vcdus_to_cadus(lrpt.packets_to_vcdus(lrpt.synth_packets(imgs)))
    fs = 72000 * 16
    x = np.concatenate([np.zeros(30000, np.complex64), modulate(cad), np.zeros(150000, np.complex64)])
    y = channel(x, fs, f0=900.0, rate=-20.0, esn0_db=10.0, rng=rng)
    rx = run_rx(y, fs, True)
    st = rx.stats()
    print(f"\nснимок по радио: кадров {st['frames_ok']}/{len(cad)}, строк {st['lines']}, каналы {st['channels']}")
    assert st["channels"] == [64, 65, 66]
    assert st["lines"] >= 64
    rgb = lrpt.composite(rx.imager.all_images(), "123")
    assert rgb is not None and rgb.shape[2] == 3


# ---------------------------------------------------------------- запись пролёта

@pytest.mark.skipif(not os.path.exists(SAMPLE), reason="нет записи (RTLTV_LRPT_SAMPLE)")
def test_recording():
    rx = lrpt.decode_file(SAMPLE)
    st = rx.stats()
    print(f"\nзапись: кадров {st['frames_ok']}/{st['frames']}, строк {st['lines']}, каналы {st['channels']}")
    assert st["frames_ok"] > 100
    assert st["lines"] >= 200
    imgs = rx.imager.all_images()
    assert len(imgs) >= 2
    for im in imgs.values():
        assert im.shape[1] == lrpt.WIDTH
        assert np.count_nonzero(im) > im.size * 0.5


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-s"]))


# ---------------------------------------------------------------- режим 80k с перемежением, живой путь

def _chan80k(cad, extra=400):
    bits = lrpt.nrzm_encode(np.unpackbits(np.concatenate(cad)))
    coded, _ = lrpt.conv_encode(bits)
    D = lrpt.Deinterleaver80k
    coded = np.concatenate([np.zeros(72 * 200, np.uint8), coded, np.zeros(D.SPAN + 72 * extra, np.uint8)])
    return lrpt.interleave80k(coded[: coded.size // 72 * 72])


def _oqpsk(chan, sp=4):
    i = chan[0::2].astype(np.float32) * 2 - 1
    q = chan[1::2].astype(np.float32) * 2 - 1
    n = i.size * sp
    x = np.zeros(n, np.complex64)
    x[0:n:sp] = i
    x[sp // 2:n:sp] += 1j * q
    return sps.oaconvolve(x, lrpt.rrc_taps(sp, 0.5, 12)).astype(np.complex64)[:n]


def test_80k_interleaved_and_auto():
    """80k OQPSK с перемежением (маркер 0x27 каждые 80 бит, 36 ветвей × 2048): кадры и картинка;
    AutoLrptReceiver сам выбирает режим."""
    rng = np.random.default_rng(7)
    imgs = {64 + k: lrpt.synth_image(32, k) for k in range(3)}
    cad = lrpt.vcdus_to_cadus(lrpt.packets_to_vcdus(lrpt.synth_packets(imgs)))
    x = _oqpsk(_chan80k(cad))
    fs = 320000
    t = np.arange(x.size) / fs
    y = x * np.exp(1j * (2 * np.pi * (700 * t - 15 * t * t) + 1.1)).astype(np.complex64)
    y += (0.15 * (rng.standard_normal(x.size) + 1j * rng.standard_normal(x.size))).astype(np.complex64)
    rx = lrpt.AutoLrptReceiver(fs)
    for k in range(0, y.size, 65536):
        rx.feed(y[k:k + 65536])
    rx.finish()
    st = rx.stats()
    print(f"\n80k: кадров {st['frames_ok']}/{len(cad)}, режим «{st['mode']}», строк {st['lines']}")
    assert st["frames_ok"] >= len(cad) - 1 and "перемежение" in st["mode"]
    assert st["channels"] == [64, 65, 66]


@pytest.mark.skipif(not os.path.exists(SAMPLE), reason="нет записи (RTLTV_LRPT_SAMPLE)")
def test_recording_live_path_with_tle_doppler():
    """Запись блоками через RxWorker (как поток с донгла) + доплер реального пролёта по TLE поверх сигнала;
    RxWorker снимает его по той же орбите. Захват, синхронизация, созвездие из 4 облаков."""
    from rtltv import satpass
    from rtltv.satrx import RxWorker
    tles, _ = satpass.load_tle(offline=True)
    if 59051 not in tles:
        pytest.skip("нет TLE №2-4 в кэше")
    orb = satpass.Orbit(tles[59051])
    home = (55.64, 37.39)
    p = max(orb.passes(*home, hours=24), key=lambda x: x.max_el)
    fs = lrpt.wav_info(SAMPLE)[0]
    it = lrpt.iter_iq(SAMPLE, "wav", block=131072)
    n = next(it)
    t_start = p.tca - n / fs / 2
    fed = [0]
    rx = lrpt.LrptReceiver(fs, False)
    w = RxWorker(fs, 137.85e6, 137.9e6, rx, orb, home, True, None, clock=lambda: t_start + fed[0] / fs)
    w.start()
    phase = 0.0
    for blk in it:
        d = orb.doppler(t_start + (fed[0] + np.array([0, blk.size])) / fs, 137.9e6, *home)
        ph = phase + 2 * np.pi * np.cumsum(50e3 + np.linspace(d[0], d[1], blk.size)) / fs
        phase = float(ph[-1] % (2 * np.pi))
        w.put((blk * np.exp(1j * ph)).astype(np.complex64))
        fed[0] += blk.size
        while w.q.qsize() > 4:
            time.sleep(0.005)
    while w.q.qsize():
        time.sleep(0.05)
    time.sleep(0.3)
    w.stop()
    w.join(10)
    st = rx.stats()
    ang = np.degrees(np.arctan2(w.cons_snap[:, 1], w.cons_snap[:, 0])) % 90
    print(f"\nживой путь: кадров {st['frames_ok']}/{st['frames']}, строк {st['lines']}, захват {st['carrier']:.2f}")
    assert st["frames_ok"] > 800 and st["lines"] >= 600 and st["carrier"] > 0.8
    assert np.mean(np.abs(ang - 45) < 22) > 0.9
