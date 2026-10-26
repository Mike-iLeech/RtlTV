"""Эталоны для LrptTest.kt: считает их Python-модулем rtltv/lrpt.py (с библиотекой rtltv/driver/lrpt_dsp.dll)
и пишет ресурс теста lrpt_ref.tsv (строки «ключ<TAB>значение»).

Запуск из корня репозитория:  PYTHONUTF8=1 py -3.12 android/app/src/test/python/gen_lrpt_ref.py

Случайные данные и шум — общий для Python и Kotlin генератор на счётчике (splitmix64 от seed·2^40 + i,
нормальные — Бокс — Мюллер), поэтому синтетический сигнал в Kotlin строится тот же (с точностью до округления
float32 в свёртках) и результаты приёма можно сверять до кадра и пикселя.
"""
import hashlib
import os
import sys

import numpy as np
from scipy import signal as sps

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), *[".."] * 5))
sys.path.insert(0, ROOT)
from rtltv import lrpt  # noqa: E402

OUT = os.path.join(ROOT, "android", "app", "src", "test", "resources", "lrpt_ref.tsv")
U = np.uint64


def smix(seed, n, start=0):
    """splitmix64(seed·2^40 + i) для i = start…start+n-1 → uint64."""
    with np.errstate(over="ignore"):
        z = (U(seed) << U(40)) + np.arange(start, start + n, dtype=np.uint64)
        z = z * U(0x9E3779B97F4A7C15)
        z ^= z >> U(30)
        z = z * U(0xBF58476D1CE4E5B9)
        z ^= z >> U(27)
        z = z * U(0x94D049BB133111EB)
        z ^= z >> U(31)
    return z


def uni(seed, n):
    return (smix(seed, n) >> U(11)).astype(np.float64) * (1.0 / 9007199254740992.0)


def rbytes(seed, n):
    return (smix(seed, n) >> U(56)).astype(np.uint8)


def rbits(seed, n):
    return (smix(seed, n) >> U(63)).astype(np.uint8)


def cgauss(seed, n):
    """Комплексный нормальный шум (re, im ~ N(0,1))."""
    u = uni(seed, 2 * n)
    u1 = 1.0 - u[0::2]
    u2 = u[1::2]
    r = np.sqrt(-2.0 * np.log(u1))
    return r * np.cos(2 * np.pi * u2) + 1j * (r * np.sin(2 * np.pi * u2))


def channel(x, fs, f0, rate, esn0_db, sps_, seed, phase=0.7):
    """Доплер (f0 + rate·t), фаза, АБГШ с заданным Es/N0 — как channel() в tests/test_lrpt.py."""
    t = np.arange(x.size) / fs
    ph = 2 * np.pi * (f0 * t + 0.5 * rate * t * t) + phase
    y = x * np.exp(1j * ph).astype(np.complex64)
    p = float(np.mean(np.abs(x.astype(np.complex128)) ** 2)) * sps_
    n0 = p / (10 ** (esn0_db / 10))
    sigma = np.sqrt(n0 / 2)
    y = y + (sigma * cgauss(seed, x.size)).astype(np.complex64)
    return y.astype(np.complex64)


def make_cadus(n, seed, vcid=5, dual=False):
    vcdus = []
    raw = rbytes(seed, 892 * n)
    for k in range(n):
        vc = raw[892 * k: 892 * (k + 1)].copy()
        vc[:10] = [0x40, vcid, (k >> 16) & 255, (k >> 8) & 255, k & 255, 0, 0, 0, 0x07, 0xFF]
        vcdus.append(vc)
    return lrpt.vcdus_to_cadus(vcdus, dual)


def run_rx(y, fs, oqpsk, block=131072):
    rx = lrpt.LrptReceiver(fs, oqpsk)
    for k in range(0, y.size, block):
        rx.feed(y[k:k + block])
    rx.finish()
    return rx


rows = []


def put(k, v):
    rows.append(f"{k}\t{v}")


def ints(a):
    return " ".join(str(int(v)) for v in a)


def floats(a):
    return " ".join(repr(float(v)) for v in a)


def bitstr(a):
    return "".join("1" if v else "0" for v in a)


def hexs(a):
    return bytes(np.asarray(a, np.uint8)).hex()


def img_info(prefix, img):
    put(prefix + ".shape", f"{img.shape[0]} {img.shape[1]}")
    put(prefix + ".sha", hashlib.sha256(np.ascontiguousarray(img).tobytes()).hexdigest())
    put(prefix + ".blocks", hexs(img[::8, ::8].ravel()))


def rx_info(prefix, rx, cad=None):
    st = rx.stats()
    put(prefix + ".stats", f"{st['frames']} {st['frames_ok']} {int(st['nrzm'])} {st['hyp']} {st['rs_fixed']} "
        f"{st['packets']} {st['lines']}")
    put(prefix + ".channels", ints(st["channels"]))
    put(prefix + ".residual", repr(float(st["residual_hz"])))
    for a, im in rx.imager.all_images().items():
        img_info(f"{prefix}.img{a}", im)
    print(prefix, st["frames_ok"], "/", st["frames"], "nrzm", st["nrzm"], "hyp", st["hyp"], "lines", st["lines"])


# ---------------------------------------------------------------- генератор (сверка самого генератора)
put("smix.1", " ".join(str(int(v)) for v in smix(1, 4)))
put("gauss.2", floats(np.concatenate([[g.real, g.imag] for g in cgauss(2, 3)])))

# ---------------------------------------------------------------- базовые блоки
for s in (4.0, 16.0, 1024000 / 72000, 7.5, 3.0, 8.0):
    put(f"rrc.{s!r}", floats(lrpt.rrc_taps(s, 0.5, 12)))
put("pn", hexs(lrpt.pn()))

b = rbits(11, 300)
put("nrzm.in", bitstr(b))
put("nrzm.enc1", bitstr(lrpt.nrzm_encode(b, 1)))
put("nrzm.dec0", bitstr(lrpt.nrzm_decode(b, 0)))
put("nrzm.dec1", bitstr(lrpt.nrzm_decode(b, 1)))

b = rbits(12, 2000)
enc, st = lrpt.conv_encode(b, 0)
put("conv.in", bitstr(b))
put("conv.out0", bitstr(enc))
put("conv.st0", st)
enc, st = lrpt.conv_encode(b, 37)
put("conv.out37", bitstr(enc))
put("conv.st37", st)

b = rbits(13, 3000)
enc, _ = lrpt.conv_encode(b)
g = cgauss(14, enc.size).real
soft = np.clip((enc.astype(np.float64) * 2 - 1) * 40 + 25 * g, -127, 127).astype(np.int8)
out = np.empty(enc.size // 2, np.uint8)
dec = np.empty(enc.size // 2, np.uint64)
metric = lrpt.lib().viterbi27(lrpt._p(soft), enc.size // 2, lrpt._p(out), lrpt._p(dec))
put("vit.in", bitstr(b))
put("vit.soft", hexs(soft.view(np.uint8)))
put("vit.out", bitstr(out))
put("vit.metric", metric)
print("viterbi: сырые ошибки", float(np.mean((soft > 0) != enc)), "после", int(np.sum(out[:-64] != b[:-64])))

d = rbytes(15, 223)
put("rs.data", hexs(d))
for dual in (False, True):
    tag = "dual" if dual else "norm"
    par = lrpt.rs_encode(d, dual)
    put(f"rs.{tag}.par", hexs(par))
    cw = np.concatenate([d, par])
    pos = (smix(16, 64) % U(255)).astype(np.int64)
    pos = list(dict.fromkeys(pos.tolist()))
    vals = rbytes(17, 64) | 1
    for ne in (5, 16, 17, 20):
        e = cw.copy()
        for i in range(ne):
            e[pos[i]] ^= vals[i]
        put(f"rs.{tag}.err{ne}", hexs(e))
        f = e.copy()
        r = lrpt.rs_decode(f, dual)
        put(f"rs.{tag}.ret{ne}", r)
        put(f"rs.{tag}.fix{ne}", hexs(f))

bits = rbits(18, 5000)
for p, inv, nerr in ((100, False, 0), (1300, True, 0), (2500, False, 2), (3200, True, 3), (4000, False, 4)):
    a = lrpt.ASM_BITS.copy()
    if inv:
        a ^= 1
    a[:nerr] ^= 1
    bits[p:p + 32] = a
put("asm.bits", bitstr(bits))
put("asm.hits3", " ".join(f"{p}:{int(i)}" for p, i in lrpt._asm_hits(bits, 3)))
put("asm.hits6", " ".join(f"{p}:{int(i)}" for p, i in lrpt._asm_hits(bits, 6)))

soft = (rbytes(19, 21).astype(np.int16) - 128).astype(np.int8)
soft[0] = -128
put("tr.in", hexs(soft.view(np.uint8)))
for h in range(8):
    put(f"tr.{h}", hexs(lrpt._transform(soft, h).view(np.uint8)))

# ---------------------------------------------------------------- демодулятор (вход квантован, свёрток нет)
sym = 3000
bi = rbits(20, sym).astype(np.float64) * 2 - 1
bq = rbits(21, sym).astype(np.float64) * 2 - 1
sp = 4
h = lrpt.rrc_taps(sp, 0.5, 12).astype(np.float64)
xi = np.zeros(sym * sp)
xq = np.zeros(sym * sp)
xi[::sp] = bi
xq[sp // 2::sp] = bq
x = np.convolve(xi, h)[: sym * sp] + 1j * np.convolve(xq, h)[: sym * sp]
t = np.arange(x.size) / 288000.0
x = x * np.exp(1j * (2 * np.pi * 300.0 * t + 0.4)) * 0.37 + 0.03 * cgauss(22, x.size)
q = np.round(np.concatenate([x.real, x.imag]) * 4096).astype(np.int64)
qi, qq = q[: x.size], q[x.size:]
put("demod.in", ints(np.ravel(np.column_stack([qi, qq]))))
iq = (np.column_stack([qi, qq]).astype(np.float32) / np.float32(4096)).ravel()
L = lrpt.lib()
for mode in (0, 1, 2):
    mem = np.zeros(L.demod_size() + 64, np.uint8)
    hd = L.demod_init(lrpt._p(mem), float(sp), mode, 0.006, 0.002, 2 * np.pi * 8000.0 / 288000.0)
    if mode == 1:
        L.demod_set_freq(hd, 2 * np.pi * 250.0 / 288000.0)
    softs, conss, total = [], [], 0
    for a, z in ((0, 7000), (7000, x.size)):
        chunk = np.ascontiguousarray(iq[2 * a: 2 * z])
        ms = (z - a) // sp + 16
        so = np.zeros(ms * 2, np.int8)
        co = np.zeros(ms * 2, np.float32)
        n = L.demod_process(hd, lrpt._p(chunk), z - a, lrpt._p(so), lrpt._p(co), ms)
        softs.append(so[: (n if mode == 2 else 2 * n)])
        conss.append(co[: 2 * n])
        total += n
    stt = np.zeros(6)
    L.demod_state(hd, lrpt._p(stt))
    put(f"demod.{mode}.n", total)
    put(f"demod.{mode}.soft", hexs(np.concatenate(softs).view(np.uint8)))
    put(f"demod.{mode}.cons", floats(np.concatenate(conss)[:200]))
    put(f"demod.{mode}.state", floats(stt))

# ---------------------------------------------------------------- МКУ с коэффициентами AC


def bits_val(v, n):
    if v < 0:
        v += (1 << n) - 1
    return format(v, f"0{n}b") if n else ""


def encode_blocks(blocks):
    """Кодер МКУ с AC (для эталона декодера): blocks — список 64 коэффициентов в порядке зигзага."""
    out = []
    prev = 0
    for z in blocks:
        d = z[0] - prev
        prev = z[0]
        cat = 0 if d == 0 else int(abs(d)).bit_length()
        c, ln = lrpt.DC_CODES[cat]
        out.append(format(c, f"0{ln}b") + bits_val(d, cat))
        run = 0
        last = max([k for k in range(1, 64) if z[k] != 0], default=0)
        for k in range(1, last + 1):
            v = z[k]
            if v == 0:
                run += 1
                if run == 16:
                    c, ln = lrpt.AC_CODES[0xF0]
                    out.append(format(c, f"0{ln}b"))
                    run = 0
                continue
            size = int(abs(v)).bit_length()
            c, ln = lrpt.AC_CODES[(run << 4) | size]
            out.append(format(c, f"0{ln}b") + bits_val(v, size))
            run = 0
        if last < 63:
            c, ln = lrpt.AC_CODES[0x00]
            out.append(format(c, f"0{ln}b"))
    s = "".join(out)
    s += "1" * (-len(s) % 8)
    return int(s, 2).to_bytes(len(s) // 8, "big")


u = smix(23, 14 * 64)
blocks = []
for bk in range(14):
    z = [0] * 64
    z[0] = int(u[bk * 64] % U(41)) - 20
    for k in range(1, 64):
        r = int(u[bk * 64 + k] % U(1000))
        if r < (300 if k < 10 else 60 if k < 30 else 10):
            z[k] = int(r % 31) - 15 or 3
    if bk == 5:
        z[40] = 0
        z[1:63] = [0] * 62
        z[63] = 7                 # длинная серия нулей: ZRL ×3 + последний коэффициент (без EOB)
    blocks.append(z)
for qf in (77, 30):
    data = encode_blocks(blocks)
    img, n = lrpt.decode_mcus(data, qf)
    put(f"mcu.{qf}.data", data.hex())
    put(f"mcu.{qf}.n", n)
    put(f"mcu.{qf}.px", hexs(img.ravel()))
    cut = data[: len(data) * 2 // 3]
    img, n = lrpt.decode_mcus(cut, qf)
    put(f"mcu.{qf}.cut.n", n)
    put(f"mcu.{qf}.cut.px", hexs(img.ravel()))
for qf in (10, 20, 35, 49, 50, 80, 95, 100, 0):
    put(f"qt.{qf}", ints(lrpt.qtable(qf)))

# ---------------------------------------------------------------- растяжка уровней
v = rbytes(24, 50 * 1568)
img = np.where(v < 40, 0, 30 + (v.astype(np.int32) * 140) // 255).astype(np.uint8).reshape(50, 1568)
sub = img.ravel()[:: max(1, img.size // 200000)]
nz = sub[sub > 0]
lo, hi = np.percentile(nz, (0.5, 99.5))
put("stretch.lohi", floats([lo, hi]))
put("stretch.sha", hashlib.sha256(lrpt.stretch(img).tobytes()).hexdigest())

# ---------------------------------------------------------------- синтетический передатчик
for seed in (0, 1, 2):
    im = lrpt.synth_image(96, seed)
    put(f"synth.{seed}.sha", hashlib.sha256(im.tobytes()).hexdigest())
imgs = {64 + k: lrpt.synth_image(16, k) for k in range(3)}
pk = lrpt.synth_packets(imgs)
put("synth.packets.sha", hashlib.sha256(b"".join(pk)).hexdigest())
put("synth.packets.n", len(pk))
vc = lrpt.packets_to_vcdus(pk)
put("synth.vcdus.sha", hashlib.sha256(b"".join(bytes(v) for v in vc)).hexdigest())
cad = lrpt.vcdus_to_cadus(vc)
put("synth.cadus.sha", hashlib.sha256(b"".join(bytes(c) for c in cad)).hexdigest())
put("synth.cadus.dual.sha", hashlib.sha256(b"".join(bytes(c) for c in lrpt.vcdus_to_cadus(vc, True))).hexdigest())
m = lrpt.Modulator(4, True, True).modulate(cad[:2])
put("synth.mod.head", floats(np.column_stack([m.real, m.imag]).ravel()[:400]))
put("synth.mod.n", m.size)

# ---------------------------------------------------------------- грубая оценка частоты
cad = make_cadus(6, 30)
x = lrpt.Modulator(8, True, True).modulate(cad)[:201600]
y = channel(x, 576000.0, 2300.0, 0.0, 9.0, 8, 31)
c, snr = lrpt.coarse_offset(y, 576000.0)
put("coarse.576k", floats([c, snr]))
print("coarse", c, snr)

# ---------------------------------------------------------------- полный путь на синтетике
# (имя, кадров, sps, oqpsk, nrzm, f0, rate, Es/N0, seed)
SCEN = [
    ("oq8", 40, 8, True, True, 2300.0, -45.0, 9.0, 40),
    ("qp4", 40, 4, False, False, 2300.0, -45.0, 9.0, 41),
    ("low4", 40, 4, True, True, -1800.0, 30.0, 4.0, 42),
    ("oq3", 30, 3, True, True, 1200.0, 20.0, 10.0, 43),
]
for name, n, sp, oq, nz, f0, rate, es, seed in SCEN:
    cad = make_cadus(n, seed)
    fs = 72000.0 * sp
    x = lrpt.Modulator(sp, oq, nz).modulate(cad)
    y = channel(x, fs, f0, rate, es, sp, seed + 100)
    rx = run_rx(y, fs, oq)
    rx_info("chain." + name, rx)

# снимок по радио: 3 канала, OQPSK + NRZ-M, тишина до и после
imgs = {64 + k: lrpt.synth_image(96, k) for k in range(3)}
cad = lrpt.vcdus_to_cadus(lrpt.packets_to_vcdus(lrpt.synth_packets(imgs)))
sp = 4
fs = 72000.0 * sp
x = np.concatenate([np.zeros(7500, np.complex64), lrpt.Modulator(sp, True, True).modulate(cad),
                    np.zeros(37500, np.complex64)])
y = channel(x, fs, 900.0, -20.0, 10.0, sp, 150)
put("air.ncad", len(cad))
rx_info("air", run_rx(y, fs, True))
for a, im in imgs.items():
    avg = im.reshape(12, 8, 196, 8).mean(axis=(1, 3))
    put(f"air.avg{a}", floats(avg.ravel()[:40]))

# 80k с перемежением + автоопределение режима


def oqpsk_raw(chan, sp=4):
    i = chan[0::2].astype(np.float32) * 2 - 1
    q = chan[1::2].astype(np.float32) * 2 - 1
    n = i.size * sp
    x = np.zeros(n, np.complex64)
    x[0:n:sp] = i
    x[sp // 2:n:sp] += 1j * q
    return sps.oaconvolve(x, lrpt.rrc_taps(sp, 0.5, 12)).astype(np.complex64)[:n]


imgs = {64 + k: lrpt.synth_image(32, k) for k in range(3)}
cad = lrpt.vcdus_to_cadus(lrpt.packets_to_vcdus(lrpt.synth_packets(imgs)))
bits = lrpt.nrzm_encode(np.unpackbits(np.concatenate(cad)))
coded, _ = lrpt.conv_encode(bits)
D = lrpt.Deinterleaver80k
coded = np.concatenate([np.zeros(72 * 200, np.uint8), coded, np.zeros(D.SPAN + 72 * 400, np.uint8)])
chan = lrpt.interleave80k(coded[: coded.size // 72 * 72])
put("i80.chan.sha", hashlib.sha256(chan.tobytes()).hexdigest())
x = oqpsk_raw(chan)
fs = 320000.0
t = np.arange(x.size) / fs
y = x * np.exp(1j * (2 * np.pi * (700 * t - 15 * t * t) + 1.1)).astype(np.complex64)
y = y + (0.15 * cgauss(161, x.size)).astype(np.complex64)
rx = lrpt.AutoLrptReceiver(fs)
for k in range(0, y.size, 65536):
    rx.feed(y[k:k + 65536])
rx.finish()
put("i80.ncad", len(cad))
put("i80.mode", rx.stats()["mode"])
rx_info("i80", rx.rx)

with open(OUT, "w", encoding="utf-8", newline="\n") as f:
    f.write("# Эталоны LrptTest.kt — сгенерировано android/app/src/test/python/gen_lrpt_ref.py, не править руками\n")
    f.write("\n".join(rows) + "\n")
print("записано", OUT, len(rows), "строк")
