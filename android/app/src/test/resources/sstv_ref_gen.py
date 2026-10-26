"""Эталоны для SstvTest.kt: py -3.12 android/app/src/test/resources/sstv_ref_gen.py (PYTHONUTF8=1) → sstv_ref.txt рядом."""
import sys, os
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), *[".."] * 5))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import numpy as np
from rtltv import sstv
from test_sstv import card, psnr, run

FS = 48000
out = []
for name, m in sstv.MODES.items():
    chans = ";".join(f"{k}:{o!r}:{d!r}" for k, o, d in m["channels"])
    out.append(f'MODE|{name}|{m["vis"]}|{m["width"]}|{m["height"]}|{m["line"]!r}|{m["sync"]!r}|{m["first_sync"]!r}|{m["duration"]!r}|{m["lines"]}|{chans}|{m["sep"]}')

# цвет
vals = [(0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 0, 255), (12, 200, 77), (230, 200, 60)]
for r, g, b in vals:
    a = np.array([[[r, g, b]]], np.float32)
    y, u, v = sstv._rgb_to_ycc(a)
    out.append(f"YCC|{r},{g},{b}|{float(y[0,0])!r}|{float(u[0,0])!r}|{float(v[0,0])!r}")
for y, u, v in [(16, 128, 128), (235, 128, 128), (81, 90, 240), (145, 54, 34), (41, 240, 110), (128, 60, 200), (200, 200, 30)]:
    rgb = sstv._ycc_to_rgb(np.array([y], np.float32), np.array([u], np.float32), np.array([v], np.float32))[0]
    out.append(f"RGB|{y},{u},{v}|{rgb[0]},{rgb[1]},{rgb[2]}")

# кодер: длина и контрольные отсчёты
for name in sstv.MODES:
    m = sstv.MODES[name]
    sig = sstv.encode(card(m["width"], m["height"]), name, FS)
    idx = [1000, 20000, 50000, len(sig) // 2, len(sig) - 7]
    out.append(f"ENC|{name}|{len(sig)}|" + ",".join(f"{float(sig[i])!r}" for i in idx)
               + f"|{float(np.sum(sig.astype(np.float64) ** 2))!r}")

# декодер: такт +0,1 %, расстройка +50 Гц, без шума
for name in sstv.MODES:
    m = sstv.MODES[name]
    img = card(m["width"], m["height"])
    sig = sstv.encode(img, name, fs=FS * 1.001, freq_offset=50.0)
    sig = np.concatenate([np.zeros(FS // 2, np.float32), sig, np.zeros(FS, np.float32)])
    ev = run(sstv.SstvDecoder(FS), sig)
    vis = [e for e in ev if e["type"] == "vis"][0]
    done = [e for e in ev if e["type"] == "done"][0]
    im = done["image"].astype(np.float64)
    H, W = im.shape[:2]
    grid = []
    for gy in range(4):
        for gx in range(4):
            blk = im[gy * H // 4:(gy + 1) * H // 4, gx * W // 4:(gx + 1) * W // 4]
            grid += [round(float(blk[..., c].mean()), 2) for c in range(3)]
    out.append(f"DEC|{name}|{vis['offset']}|{done['slant_ppm']}|{done['offset']}|{psnr(done['image'], img):.3f}|"
               + ",".join(str(g) for g in grid))
    print(name, file=sys.stderr, flush=True)

open(os.path.join(os.path.dirname(__file__), "sstv_ref.txt"), "w", encoding="utf-8").write("\n".join(out) + "\n")
