"""Тесты SSTV: кодер → эфирные искажения → потоковый декодер.

Искажения: белый шум (SNR по всей полосе), сдвиг частоты ±50 Гц, ошибка частоты
дискретизации ±0,1 %, подача блоками по 2400 отсчётов (50 мс при 48 кГц).

Запуск таблицей: py -3.12 tests/test_sstv.py [папка_для_png]
"""
import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtltv import sstv  # noqa: E402

FS = 48000
BLOCK = 2400
SNR_DB = 12.0
MODE_NAMES = list(sstv.MODES)


def card(w, h):
    """Испытательная картинка: цветные полосы, серая растяжка, круг на фоне."""
    y, x = np.mgrid[0:h, 0:w]
    img = np.zeros((h, w, 3), np.float32)
    bars = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0),
            (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0)]
    b = x * 8 // w
    top = y < h // 3
    for i, c in enumerate(bars):
        img[(b == i) & top] = c
    mid = (y >= h // 3) & (y < h // 2)
    img[mid] = (x[mid] / (w - 1) * 255.0)[:, None]
    low = y >= h // 2
    img[low] = np.stack([x[low] / w * 120, 60 + y[low] / h * 80, 180 - x[low] / w * 60], -1)
    circ = ((x - w / 2) ** 2 + (y - 0.75 * h) ** 2) < (h / 7) ** 2
    img[circ] = (230, 200, 60)
    return img.astype(np.uint8)


def psnr(a, b):
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return 10 * np.log10(255.0 ** 2 / max(mse, 1e-9))


def air(mode, k, snr_db=SNR_DB, seed=0, img=None):
    """Сигнал «из эфира»: тишина, передача, тишина + шум."""
    m = sstv.MODES[mode]
    if img is None:
        img = card(m["width"], m["height"])
    clk = 1.0 + (0.001 if k % 2 == 0 else -0.001)     # ошибка тактовой ±0,1 %
    shift = 50.0 if k % 2 == 0 else -50.0              # расстройка ±50 Гц
    sig = sstv.encode(img, mode, fs=FS * clk, freq_offset=shift)
    sig = np.concatenate([np.zeros(FS // 2, np.float32), sig, np.zeros(FS, np.float32)])
    rng = np.random.default_rng(seed + k)
    ps = 0.5 ** 2 / 2
    sig = sig + rng.normal(0, np.sqrt(ps / 10 ** (snr_db / 10)), sig.size).astype(np.float32)
    return img, sig, clk


def run(dec, sig, block=BLOCK):
    events = []
    for i in range(0, sig.size, block):
        events += dec.process(sig[i:i + block])
    return events


@pytest.mark.parametrize("mode", MODE_NAMES)
def test_mode_roundtrip(mode):
    k = MODE_NAMES.index(mode)
    img, sig, clk = air(mode, k)
    dec = sstv.SstvDecoder(FS)
    ev = run(dec, sig)
    vis = [e for e in ev if e["type"] == "vis"]
    assert vis and vis[0]["mode"] == mode
    assert abs(vis[0]["offset"] - (50.0 if k % 2 == 0 else -50.0)) < 15
    done = [e for e in ev if e["type"] == "done"]
    assert len(done) == 1 and done[0]["complete"]
    out = done[0]["image"]
    assert out.shape == img.shape and out.dtype == np.uint8
    p = psnr(out, img)
    assert p >= 20.0, f"{mode}: PSNR {p:.1f} дБ"
    # потоковые строки покрывают всю картинку
    rows = {e["y"] for e in ev if e["type"] == "line"}
    assert rows == set(range(img.shape[0]))
    # наклон: оценка тактовой ~ ±1000 ppm
    assert abs(done[0]["slant_ppm"] - (clk - 1.0) * 1e6) < 150


def test_vis_codes_unique():
    codes = [m["vis"] for m in sstv.MODES.values()]
    assert len(codes) == len(set(codes))


def test_force_mode_without_vis():
    """Приём с середины передачи без VIS: автостарт по синхроимпульсам."""
    mode = "PD 120"
    m = sstv.MODES[mode]
    img, sig, _ = air(mode, 0, snr_db=15)
    skip_lines = 20
    t_cut = 0.5 + 0.91 + skip_lines * m["line"] + 0.3 * m["line"]
    sig = sig[int(t_cut * FS * 1.001):]
    dec = sstv.SstvDecoder(FS)
    dec.force_mode = mode
    ev = run(dec, sig) + dec.flush()
    assert not [e for e in ev if e["type"] == "vis"]
    starts = [e for e in ev if e["type"] == "start"]
    assert starts and starts[0]["manual"]
    fin = [e for e in ev if e["type"] in ("done", "lost")]
    assert fin
    out = fin[0]["image"]
    rows = fin[0]["lines"]
    first = (skip_lines + 1) * 2
    assert rows >= m["height"] - first - 4
    n = min(rows, m["height"] - first) - 2
    assert psnr(out[:n], img[first:first + n]) >= 20.0


def test_start_manual():
    mode = "Martin M1"
    m = sstv.MODES[mode]
    img, sig, _ = air(mode, 1, snr_db=20)
    sig = sig[int((0.5 + 0.91) * FS * 0.999) + 2000:]   # без VIS, с начала 1-й строки
    dec = sstv.SstvDecoder(FS)
    dec.start_manual(mode)
    ev = run(dec, sig) + dec.flush()
    fin = [e for e in ev if e["type"] in ("done", "lost")]
    assert fin
    out = fin[0]["image"]
    # первая строка могла уйти до старта — ищем сдвиг 0..1
    best = max(psnr(out[:m["height"] - 2], img[s:s + m["height"] - 2]) for s in (0, 1))
    assert best >= 20.0


def test_lost_on_cut():
    mode = "Scottie S1"
    m = sstv.MODES[mode]
    _, sig, _ = air(mode, 0, snr_db=20)
    cut = int((0.5 + 0.91 + 0.3 * m["lines"] * m["line"]) * FS)
    sig = np.concatenate([sig[:cut], np.random.default_rng(5).normal(0, 0.1, 6 * FS)])
    ev = run(sstv.SstvDecoder(FS), sig.astype(np.float32))
    lost = [e for e in ev if e["type"] == "lost"]
    assert lost and 0.2 * m["height"] < lost[0]["lines"] < 0.4 * m["height"]


def test_noise_only_no_events():
    rng = np.random.default_rng(3)
    ev = run(sstv.SstvDecoder(FS), rng.normal(0, 0.3, 60 * FS).astype(np.float32))
    assert ev == []


def test_two_images_back_to_back():
    a = sstv.encode(card(320, 240), "Robot 36", FS)
    b = sstv.encode(card(320, 256), "Martin M2", FS)
    sig = np.concatenate([a, np.zeros(FS // 3, np.float32), b, np.zeros(FS, np.float32)])
    ev = run(sstv.SstvDecoder(FS), sig)
    done = [e["mode"] for e in ev if e["type"] == "done"]
    assert done == ["Robot 36", "Martin M2"]


def test_decode_file_44100(tmp_path):
    from scipy.io import wavfile
    img = card(320, 240)
    sig = sstv.encode(img, "Robot 72", 44100)
    sig = np.concatenate([np.zeros(22050, np.float32), sig, np.zeros(44100, np.float32)])
    path = tmp_path / "r72.wav"
    wavfile.write(str(path), 44100, (sig * 32767).astype(np.int16))
    imgs = sstv.decode_file(str(path))
    assert len(imgs) == 1 and psnr(imgs[0], img) >= 22.0


def test_level():
    dec = sstv.SstvDecoder(FS)
    t = np.arange(FS // 10) / FS
    dec.process((0.5 * np.sin(2 * np.pi * 1900 * t)).astype(np.float32))
    lv = dec.level()
    assert abs(lv["freq"] - 1900) < 5 and lv["state"] == "idle"


def main(outdir=None):
    print(f"{'режим':16s} {'VIS':>14s} {'PSNR':>6s} {'аудио,с':>8s} {'декод,с':>8s} {'x реал':>7s}")
    for k, mode in enumerate(MODE_NAMES):
        img, sig, _ = air(mode, k)
        dec = sstv.SstvDecoder(FS)
        t0 = time.perf_counter()
        ev = run(dec, sig)
        dt = time.perf_counter() - t0
        vis = next((e["mode"] for e in ev if e["type"] == "vis"), "-")
        done = next((e for e in ev if e["type"] == "done"), None)
        p = psnr(done["image"], img) if done else float("nan")
        dur = sig.size / FS
        print(f"{mode:16s} {vis:>14s} {p:6.1f} {dur:8.1f} {dt:8.2f} {dur / dt:7.0f}")
        if outdir and done is not None and mode in ("PD 120", "Robot 36", "Scottie S1"):
            from PIL import Image
            name = mode.replace(" ", "_")
            Image.fromarray(np.concatenate([img, done["image"]], 1)).save(
                os.path.join(outdir, f"sstv_{name}.png"))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
