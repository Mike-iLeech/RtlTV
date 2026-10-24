"""Тестовый генератор эфира: аналоговая станция (испытательная таблица + FM-звук)
и мультиплекс DVB-T2 с циклическим префиксом. Отдаёт сырые байты как настоящий донгл."""
import math

import numpy as np

from .standards import STANDARDS

TEST_ANALOG_VC = 583.25e6   # ДМВ 35, D/K
TEST_MUX_CENTER = 546e6     # ДМВ 30
TEST_MUX_TU = 3584e-6       # DVB-T2 32K, 8 МГц
TEST_MUX_GI = 1 / 16

MELODY = [523.25, 659.25, 783.99, 1046.5, 783.99, 659.25]


def make_card(rows, cols):
    """Испытательная таблица (яркость 0..1)."""
    y, x = np.mgrid[0:rows, 0:cols].astype(np.float32)
    img = np.full((rows, cols), 0.5, np.float32)
    gx, gy = cols / 16.0, rows / 12.0
    grid = (np.abs((x % gx) - gx / 2) > gx / 2 - 1.2) | (np.abs((y % gy) - gy / 2) > gy / 2 - 1.2)
    img[grid] = 1.0
    top, bot = int(rows * 0.30), int(rows * 0.55)
    bar = np.floor(x[top:bot] / cols * 8) / 7.0
    img[top:bot] = 1.0 - bar
    g0, g1 = int(rows * 0.60), int(rows * 0.70)
    img[g0:g1] = x[g0:g1] / (cols - 1)
    b0, b1 = int(rows * 0.72), int(rows * 0.80)
    for i, period in enumerate((16, 8, 5, 3)):
        c0, c1 = int(cols * (0.15 + 0.18 * i)), int(cols * (0.15 + 0.18 * i + 0.15))
        img[b0:b1, c0:c1] = ((x[b0:b1, c0:c1] // period) % 2).astype(np.float32)
    cy, cx = rows / 2, cols / 2
    rr = np.hypot((x - cx) * (rows / cols) * 4 / 3, y - cy)
    ring = np.abs(rr - rows * 0.44) < 1.6
    img[ring] = 1.0
    img[(np.abs(x - cx) < 1.5) | (np.abs(y - cy) < 1.0)] = 0.0
    return img


class TestSignal:
    def __init__(self, fs, std_key="DK"):
        self.fs = float(fs)
        self.std = STANDARDS[std_key]
        self.n = 0
        self.clock_err = 0.4e-6
        self._grid = 1024
        self._card = make_card(2 * self.std.vis_count, 640)
        self._frames = {}
        self._fm_phase = 0.0
        self._ofdm_buf = np.zeros(0, np.complex64)
        self._ofdm_fc = None
        self.rng = np.random.default_rng(1)

    # --- композитный видеосигнал (амплитуда огибающей, негативная модуляция) ---
    def _composite(self, frame_no):
        if frame_no in self._frames:
            return self._frames[frame_no]
        std, G = self.std, self._grid
        L = std.lines
        comp = np.full((L, G), 0.75, np.float32)
        a0 = int(round(std.active_start_us / 64.0 * G))
        a1 = int(round((std.active_start_us + std.active_us) / 64.0 * G))
        card = self._card.copy()
        # движущийся квадрат: видно, что картинка живая
        rows, cols = card.shape
        t = frame_no / 25.0
        bx = int((0.5 + 0.38 * math.sin(t * 1.3)) * (cols - 60))
        by = int((0.5 + 0.38 * math.cos(t * 0.9)) * (rows - 60))
        card[by:by + 60, bx:bx + 60] = 1.0 - card[by:by + 60, bx:bx + 60]
        cidx = np.linspace(0, cols - 1, a1 - a0).astype(np.int64)
        for f, start in ((0, std.vis1), (1, std.vis2)):
            lines = start + np.arange(std.vis_count)
            src = card[f::2][:, cidx]
            comp[lines, a0:a1] = 0.75 - 0.625 * src
        hs = int(round(4.7 / 64.0 * G))
        eq = int(round(2.35 / 64.0 * G))
        h2 = G // 2
        bw = h2 - hs
        nb, ne = std.n_broad, std.n_eq
        H = 2 * L
        kind = np.zeros(H, np.int8)  # 0 норм., 1 широкий, 2 уравнивающий
        for base in (0, L):
            kind[base:base + nb] = 1
            kind[base + nb:base + nb + ne] = 2
            kind[(base + L - ne) % H:(base + L - ne) % H + ne] = 2
        for h in range(H):
            ln, off = h // 2, (h % 2) * h2
            if kind[h] == 0:
                if h % 2 == 0:
                    comp[ln, 0:hs] = 1.0
                continue
            comp[ln, off:off + h2] = 0.75
            comp[ln, off:off + (bw if kind[h] == 1 else eq)] = 1.0
        # ограничение полосы (~2 МГц), чтобы не было наложения спектра
        k = np.ones(8, np.float32) / 8
        flat = np.convolve(comp.ravel(), k, mode="same").astype(np.float32)
        if len(self._frames) > 4:
            self._frames.clear()
        self._frames[frame_no] = flat
        return flat

    def _analog(self, t, off):
        std, G = self.std, self._grid
        L = std.lines
        lt = t * std.line_freq
        frame_no = np.floor(lt / L).astype(np.int64)
        env = np.empty(t.size, np.float32)
        for fno in np.unique(frame_no):
            m = frame_no == fno
            comp = self._composite(int(fno))
            idx = ((lt[m] - fno * L) * G).astype(np.int64)
            np.clip(idx, 0, comp.size - 1, out=idx)
            env[m] = comp[idx]
        return env * np.exp(2j * np.pi * off * t)

    def _fm(self, t, off):
        note = MELODY[int(t[0] / 0.35) % len(MELODY)]
        inst = off + 30e3 * np.sin(2 * np.pi * note * t)
        ph = self._fm_phase + np.cumsum(2 * np.pi * inst / self.fs)
        self._fm_phase = float(ph[-1] % (2 * np.pi))
        return np.exp(1j * ph)

    def _ofdm(self, fc, n):
        if self._ofdm_fc != fc:
            self._ofdm_buf = np.zeros(0, np.complex64)
            self._ofdm_fc = fc
        Lu = int(round(TEST_MUX_TU * self.fs))
        Lg = int(round(Lu * TEST_MUX_GI))
        f = np.fft.fftfreq(Lu, 1 / self.fs) + (fc - TEST_MUX_CENTER)
        occ = np.abs(f) < 3.8e6
        parts = [self._ofdm_buf]
        have = self._ofdm_buf.size
        while have < n:
            X = np.zeros(Lu, np.complex64)
            m = int(occ.sum())
            X[occ] = (self.rng.choice([-1, 1], m) + 1j * self.rng.choice([-1, 1], m)) / math.sqrt(2)
            sym = np.fft.ifft(X) * math.sqrt(Lu)
            sym = np.concatenate((sym[-Lg:], sym)).astype(np.complex64)
            parts.append(sym)
            have += sym.size
        allb = np.concatenate(parts)
        self._ofdm_buf = allb[n:]
        return allb[:n]

    def render(self, fc, nsamples, gain_db):
        t = (self.n + np.arange(nsamples)) / self.fs * (1 + self.clock_err)
        self.n += nsamples
        g = 10 ** ((gain_db - 30.0) / 20.0)
        x = (self.rng.standard_normal(nsamples) + 1j * self.rng.standard_normal(nsamples)) * (0.012 * g)
        half = self.fs / 2
        off = TEST_ANALOG_VC - fc
        if abs(off) < half + 0.8e6:
            x += self._analog(t, off) * (0.25 * g)
        soff = TEST_ANALOG_VC + self.std.sound_offset - fc
        if abs(soff) < half:
            x += self._fm(t, soff) * (0.05 * g)
        if abs(TEST_MUX_CENTER - fc) < 3.8e6 + half:
            x += self._ofdm(fc, nsamples) * (0.05 * g)
        iq = np.empty(2 * nsamples, np.float32)
        iq[0::2] = x.real
        iq[1::2] = x.imag
        return np.clip(np.round(iq * 128 + 127.4), 0, 255).astype(np.uint8).tobytes()
