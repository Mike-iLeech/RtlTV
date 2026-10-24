"""Тесты «Электро-Л» LRIT: транспорт xRIT, сборка снимков, полная цепочка BPSK на синтетике.

Публичной записи LRIT «Электро-Л» найти не удалось — проверка на своём передатчике (lrpt/elektro synth).
Запуск:  py -3.12 -m pytest tests/test_elektro.py -q
"""
import os
import sys
import tempfile
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtltv import elektro as E  # noqa: E402
from rtltv import lrpt  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication.instance() or QGuiApplication([])
    yield app


def test_crc16():
    assert E.crc16(b"123456789") == 0x29B1


def test_geo_look():
    az, el, rng = E.geo_look(55.75580, 37.61730, 76.0)       # «Электро-Л» №5 из Москвы
    assert 134 < az < 138 and 17 < el < 19 and 39000 < rng < 41000
    _az, el, _ = E.geo_look(55.75580, 37.61730, 165.8)        # №4 — за горизонтом
    assert el < 0


def _roundtrip(files):
    dm, asm = E.XritDemux(), E.Assembler()
    for v in E.packets_to_vcdus(E.lrit_packets(files, max_payload=1500)):
        for f in dm.push(v):
            asm.push(f)
    return dm, asm


def test_xrit_roundtrip_jpeg_and_raw():
    disk = E.synth_disk(240, 1)
    files = [E.make_lrit_file(disk[i * 60:(i + 1) * 60], 1, i + 1, 4) for i in range(4)]
    files += [E.make_lrit_file(disk[i * 120:(i + 1) * 120], 2, i + 1, 2, jpeg=False) for i in range(2)]
    dm, asm = _roundtrip(files)
    assert dm.nfiles == 6 and dm.crc_bad == 0
    a = asm.image(("GOMS3", "202610231200", 1))
    b = asm.image(("GOMS3", "202610231200", 2))
    assert a.shape == disk.shape and np.abs(a.astype(int) - disk).mean() < 3      # JPEG
    assert np.array_equal(b, disk)                                                 # без сжатия


def test_crc_damage_keeps_rest_of_image():
    disk = E.synth_disk(200, 2)
    files = [E.make_lrit_file(disk, 3, 1, 1, jpeg=False)]
    pk = E.lrit_packets(files, max_payload=1000)
    bad = bytearray(pk[5])
    bad[100] ^= 0xFF
    pk[5] = bytes(bad)
    dm, asm = E.XritDemux(), E.Assembler()
    for v in E.packets_to_vcdus(pk):
        for f in dm.push(v):
            asm.push(f)
    img = asm.image(("GOMS3", "202610231200", 3))
    assert dm.crc_bad == 1 and img is not None
    assert np.count_nonzero(img != disk) < 1100           # испорчен только один пакет


def test_lrit_over_air():
    x, fs, disks = E.synth_signal()
    rng = np.random.default_rng(1)
    t = np.arange(x.size) / fs
    y = (x * np.exp(2j * np.pi * (-4700 * t + 0.4))).astype(np.complex64)
    y += (0.3 * (rng.standard_normal(y.size) + 1j * rng.standard_normal(y.size))).astype(np.complex64)
    t0 = time.perf_counter()
    rx = E.LritReceiver(fs)
    for k in range(0, y.size, 131072):
        rx.feed(y[k:k + 131072])
    rx.finish()
    dt = time.perf_counter() - t0
    st = rx.stats()
    print(f"\nLRIT по эфиру: кадров {st['frames_ok']}/{st['frames']}, файлов {st['files']}, "
          f"остаток {st['residual_hz']:.0f} Гц, {y.size / fs / dt:.1f}x реального времени")
    assert st["files"] == 12 and st["images"] == 3
    for key in rx.asm.keys():
        assert rx.asm.complete(key) == (4, 4)
        assert np.abs(rx.asm.image(key).astype(int) - disks[key[2]]).mean() < 3
    rgb = rx.asm.composite("GOMS3", "202610231200", "321")
    assert rgb is not None and rgb.shape == (320, 320, 3)
    assert y.size / fs / dt > 1.5


def test_decode_cs16_file_with_offset():
    """Запись cs16 2,352 МГц, сигнал на +400 кГц от центра — файл находит его сам."""
    x, fs, disks = E.synth_signal(nseg=2, size=160, channels=(1,))
    t = np.arange(x.size) / fs
    y = x * np.exp(2j * np.pi * 400e3 * t) * 0.3
    rng = np.random.default_rng(2)
    y = y + 0.05 * (rng.standard_normal(y.size) + 1j * rng.standard_normal(y.size))
    a = np.empty(y.size * 2, np.int16)
    a[0::2] = np.clip(y.real * 20000, -32767, 32767)
    a[1::2] = np.clip(y.imag * 20000, -32767, 32767)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "elektro_2352000Hz.cs16")
        a.tofile(path)
        rx = E.decode_file(path, fs, "cs16")
    st = rx.stats()
    assert st["files"] == 2, st
    assert abs(st["residual_hz"] - 400e3) < 2000


def test_lrpt_still_ok_after_bpsk():
    """Режим BPSK не сломал QPSK: короткая синтетика LRPT."""
    rng = np.random.default_rng(3)
    vcdus = []
    for k in range(20):
        vc = rng.integers(0, 256, 892).astype(np.uint8)
        vc[:10] = [0x40, 5, 0, 0, k, 0, 0, 0, 7, 255]
        vcdus.append(vc)
    x = lrpt.Modulator(16, True, True).modulate(lrpt.vcdus_to_cadus(vcdus))
    rx = lrpt.LrptReceiver(72000 * 16, True)
    for k in range(0, x.size, 131072):
        rx.feed(x[k:k + 131072])
    rx.finish()
    assert rx.stats()["frames_ok"] >= 15


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-s"]))
