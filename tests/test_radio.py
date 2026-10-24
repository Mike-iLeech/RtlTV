"""Режим «Радио»: демодуляторы на синтетике (стерео, ОСШ, швы блоков, АРУ, шумодав, бланкер, SSB),
RDS, файлы IQ, менеджер частот, поток чтения RTL-SDR (на подставном донгле), окно offscreen."""
import ctypes as C
import math
import time

import numpy as np
import pytest

from rtltv import freqman, iqfile, rds
from rtltv.radio import AUDIO_RATE, Agc, FrontEnd, RadioDemod, s_units, spectrum_db

FS = 2400000
OFF = 300e3
rng = np.random.default_rng(5)


# ---------------------------------------------------------------- вспомогательное
def tone_db(a, f, fs=AUDIO_RATE, skip=0.3):
    """(амплитуда тона f, SINAD дБ в полосе 100 Гц … 15 кГц)."""
    a = np.asarray(a, np.float64)[int(skip * fs):]
    w = np.hanning(a.size)
    P = np.abs(np.fft.rfft(a * w)) ** 2
    fr = np.fft.rfftfreq(a.size, 1 / fs)
    band = (fr > 100) & (fr < 15000)
    k = np.abs(fr - f) < 25
    sig = P[k].sum()
    amp = 2 * math.sqrt(sig / (a.size * np.sum(w * w)))     # Парсеваль: энергия главного лепестка окна
    return amp, 10 * math.log10(sig / max(P[band].sum() - sig, 1e-30))


def run(d, iq, blk=131072):
    out = [d.process(iq[i:i + blk]) for i in range(0, iq.size, blk)]
    return np.concatenate([o for o in out if o.size])


def fm(mod_hz, off=OFF, cnr=None, amp=0.3):
    ph = np.cumsum(2 * np.pi * (off + mod_hz) / FS)
    x = amp * np.exp(1j * ph)
    if cnr is not None:
        x = x + noise(x.size, amp, cnr, 200e3)
    return x.astype(np.complex64)


def noise(n, amp, cnr, bw):
    p = amp ** 2 / 10 ** (cnr / 10) * FS / bw
    return (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * math.sqrt(p / 2)


def t_axis(sec):
    return np.arange(int(FS * sec)) / FS


# ---------------------------------------------------------------- WFM
def test_wfm_stereo_separation_and_snr():
    t = t_axis(1.5)
    L = 0.4 * np.sin(2 * np.pi * 1000 * t)
    mpx = 0.45 * L + 0.45 * L * np.sin(2 * np.pi * 38000 * t) + 0.09 * np.sin(2 * np.pi * 19000 * t)
    d = RadioDemod(FS, "WFM")
    d.offset = OFF
    a = run(d, fm(75000 * mpx))
    assert d.stereo_lock and 0.07 < d.pilot_level < 0.11
    aL, sinad = tone_db(a[:, 0], 1000)
    aR, _ = tone_db(a[:, 1], 1000)
    sep = 20 * math.log10(aL / aR)
    print(f"  разделение {sep:.1f} дБ, SINAD {sinad:.1f} дБ")
    assert sep > 45          # было ~34 дБ: не учитывалось ослабление 38 кГц частотным дискриминатором
    assert sinad > 45


def test_wfm_mono_without_pilot_and_noise():
    t = t_axis(1.5)
    mpx = 0.45 * np.sin(2 * np.pi * 1000 * t)
    d = RadioDemod(FS, "WFM")
    d.offset = OFF
    a = run(d, fm(75000 * mpx, cnr=20))
    assert not d.stereo_lock
    amp, sinad = tone_db(a[:, 0], 1000)
    assert sinad > 40 and np.allclose(a[:, 0], a[:, 1])


def test_wfm_noisy_stereo_lock_holds():
    t = t_axis(1.5)
    L = 0.4 * np.sin(2 * np.pi * 1000 * t)
    mpx = 0.45 * L + 0.45 * L * np.sin(2 * np.pi * 38000 * t) + 0.09 * np.sin(2 * np.pi * 19000 * t)
    d = RadioDemod(FS, "WFM")
    d.offset = OFF
    locks = []
    iq = fm(75000 * mpx, cnr=12)
    for i in range(0, iq.size, 120000):
        d.process(iq[i:i + 120000])
        locks.append(d.stereo_lock)
    assert all(locks[4:]), locks      # захват не «мигает» на шумном сигнале


@pytest.mark.parametrize("mode", ["NFM", "AM", "USB", "CW"])
def test_block_size_does_not_matter(mode):
    """Звук не зависит от того, как поток порезан на блоки (нет щелчков на стыках)."""
    t = t_axis(1.0)
    if mode == "NFM":
        iq = fm(3000 * np.sin(2 * np.pi * 1000 * t), cnr=25)
    else:
        env = (1 + 0.5 * np.sin(2 * np.pi * 700 * t)) if mode == "AM" else 1.0
        iq = (0.05 * env * np.exp(2j * np.pi * (OFF + (900 if mode == "USB" else 0)) * t)).astype(np.complex64)
    d1, d2 = RadioDemod(FS, mode), RadioDemod(FS, mode)
    d1.offset = d2.offset = OFF
    a1, a2 = run(d1, iq, 131072), run(d2, iq, 50000)
    n = min(a1.size, a2.size) - 2000
    assert np.max(np.abs(a1[:n] - a2[:n])) < 1e-4


# ---------------------------------------------------------------- NFM
def test_nfm_level_and_adjacent_channel():
    t = t_axis(1.0)
    want = fm(3000 * np.sin(2 * np.pi * 1000 * t), cnr=25)
    adj = fm(3000 * np.sin(2 * np.pi * 400 * t), off=OFF + 12500, amp=3.0)   # сосед +20 дБ
    d = RadioDemod(FS, "NFM")
    d.offset = OFF
    amp, sinad = tone_db(run(d, want + adj), 1000)
    assert 0.6 < amp < 0.8 and sinad > 25


# ---------------------------------------------------------------- AM, АРУ
def test_am_carrier_onset_no_thump():
    """Борт выходит в эфир: прежде детектор подстраивал постоянную составляющую ~50 с — хлопок ×10."""
    t = t_axis(2.0)
    on = (t > 1.0).astype(float)
    x = on * (1 + 0.5 * np.sin(2 * np.pi * 800 * t)) * 0.05 * np.exp(2j * np.pi * OFF * t) + noise(t.size, 0.05, 25, 10e3)
    d = RadioDemod(FS, "AM")
    d.offset = OFF
    a = run(d, x.astype(np.complex64))
    onset = np.abs(a[int(1.0 * AUDIO_RATE):int(1.15 * AUDIO_RATE)]).max()
    steady = np.abs(a[int(1.5 * AUDIO_RATE):int(1.9 * AUDIO_RATE)]).max()
    assert onset < 1.3 * steady and steady < 0.7


def test_agc_levels_threshold_and_smoothness():
    agc = Agc(threshold_db=-80, decay_ms=200)
    outs = []
    for lev in (1e-3, 1e-2, 1e-1):
        x = lev * np.sin(2 * np.pi * 1000 * np.arange(AUDIO_RATE) / AUDIO_RATE)
        y = agc(x)
        outs.append(np.abs(y[-AUDIO_RATE // 4:]).max())
    assert all(0.45 < o < 0.55 for o in outs), outs     # 40 дБ разницы входа -> одна громкость
    # ниже порога не вытягивает до полной громкости
    agc2 = Agc(threshold_db=-40)
    y = agc2(1e-4 * np.sin(2 * np.pi * 1000 * np.arange(AUDIO_RATE) / AUDIO_RATE))
    assert np.abs(y[-4800:]).max() < 0.02
    # скачок громкости: усиление меняется плавно (нет ступенек между кусками по 1 мс)
    agc3 = Agc()
    s1k = np.sin(2 * np.pi * 1000 * np.arange(19200) / AUDIO_RATE)
    x = s1k * np.concatenate((0.001 * np.ones(4800), 0.1 * np.ones(4800), 0.001 * np.ones(9600)))
    y = agc3(x)
    assert np.abs(np.diff(y)).max() < 0.08 and np.abs(y).max() <= 0.96
    # выключенная АРУ — постоянное усиление
    agc4 = Agc()
    agc4.on = False
    agc4.manual_db = 20
    y = agc4(0.01 * np.ones(4800))
    assert abs(y[-100] - 0.1) < 1e-9


def test_ssb_opposite_sideband_and_cw_tone():
    t = t_axis(1.0)
    res = {}
    for f in (1000, -1000):
        d = RadioDemod(FS, "USB")
        d.offset = OFF
        d.agc.on = False
        d.agc.manual_db = 0
        res[f] = np.sqrt(np.mean(run(d, (0.05 * np.exp(2j * np.pi * (OFF + f) * t)).astype(np.complex64))[9600:] ** 2))
    assert 20 * math.log10(res[1000] / res[-1000]) > 45
    d = RadioDemod(FS, "CW")
    d.offset = OFF
    d.cw_tone = 600.0
    a = run(d, (0.05 * np.exp(2j * np.pi * OFF * t)).astype(np.complex64))[9600:]
    fr = np.fft.rfftfreq(a.size, 1 / AUDIO_RATE)
    assert abs(fr[np.argmax(np.abs(np.fft.rfft(a)))] - 600) < 3


def test_bandwidth_change_keeps_state():
    d = RadioDemod(FS, "NFM")
    agc_obj = d.agc
    d.set_bandwidth(20000)
    assert d.agc is agc_obj and abs(d.bw - 20000) < 1
    d.set_mode("NFM", 8000)        # тот же режим — только полоса, без пересборки
    assert abs(d.bw - 8000) < 1


# ---------------------------------------------------------------- шумодав, бланкер, IQ
def test_squelch_closes_and_opens_smoothly():
    t = t_axis(2.0)
    on = (t > 1.0).astype(float)
    x = on * fm(3000 * np.sin(2 * np.pi * 1000 * t), cnr=None, amp=0.05) + noise(t.size, 0.05, 30, 12.5e3)
    d = RadioDemod(FS, "NFM")
    d.offset = OFF
    d.squelch_db = -45.0
    a = run(d, x.astype(np.complex64))
    assert np.abs(a[int(0.2 * AUDIO_RATE):int(0.9 * AUDIO_RATE)]).max() < 1e-3      # закрыт: тишина
    assert np.sqrt(np.mean(a[int(1.3 * AUDIO_RATE):] ** 2)) > 0.3                   # открыт
    edge = a[int(0.95 * AUDIO_RATE):int(1.2 * AUDIO_RATE)]
    assert np.abs(np.diff(edge)).max() < 0.5                                        # без щелчка


def test_noise_blanker_removes_impulses():
    """Импульсная помеха (искрение, зажигание): 100 импульсов/с по 4 мкс, в 60 раз сильнее станции."""
    t = t_axis(1.0)
    iq = ((1 + 0.5 * np.sin(2 * np.pi * 1000 * t)) * 0.05 * np.exp(2j * np.pi * OFF * t)
          + noise(t.size, 0.05, 50, 10e3)).astype(np.complex64)
    for h in rng.integers(0, iq.size - 20, 100):
        iq[h:h + 10] += 3.0 * np.exp(1j * rng.uniform(0, 6.28))
    res = {}
    for nb in (False, True):
        fe = FrontEnd()
        fe.nb = nb
        d = RadioDemod(FS, "AM")
        d.offset = OFF
        out = [d.process(fe.process(iq[i:i + 120000], FS)) for i in range(0, iq.size, 120000)]
        res[nb] = tone_db(np.concatenate(out), 1000)[1]
    print(f"  SINAD без бланкера {res[False]:.1f}, с бланкером {res[True]:.1f} дБ")
    assert res[True] > res[False] + 6


def test_frontend_dc_and_iq_correction():
    t = t_axis(0.5)
    sig = 0.3 * np.exp(2j * np.pi * 200e3 * t)
    bad = (sig.real + 1j * 1.15 * (sig.imag * math.cos(0.08) + sig.real * math.sin(0.08)) + 0.05 + 0.03j)
    bad = (bad + noise(t.size, 0.3, 60, FS)).astype(np.complex64)
    fe = FrontEnd()
    fe.iq = True
    out = [fe.process(bad[i:i + 120000], FS) for i in range(0, bad.size, 120000)]
    x = np.concatenate(out[-4:])

    def image_db(z):
        P = np.abs(np.fft.fft(z[:65536] * np.hanning(65536))) ** 2
        f = np.fft.fftfreq(65536, 1 / FS)
        return 10 * math.log10(P[np.argmin(np.abs(f - 200e3))] / P[np.argmin(np.abs(f + 200e3))])

    assert image_db(bad[-262144:]) < 25 and image_db(x) > 45
    assert abs(np.mean(x)) < 0.002


# ---------------------------------------------------------------- RDS
def _rds_iq(sec, pilot=True, cnr=None, phase=0.0, rate_err=0.0, ps="RADIO 1", rt="Hello RDS world 123"):
    t = t_axis(sec)
    g = rds.make_groups(0x7A12, ps, rt, pty=10, ct=(2026, 10, 24, 11, 30))
    enc = rds.RdsEncoder(g)
    from scipy import signal
    tb = np.arange(int(240000 * sec)) / 240000
    bb = signal.filtfilt(signal.firwin(255, 2400, fs=240000), [1.0], enc.value(tb, rds.BITRATE * (1 + rate_err)))
    bb = np.repeat(bb, 10)[:t.size]
    L = 0.4 * np.sin(2 * np.pi * 1000 * t)
    R = 0.4 * np.sin(2 * np.pi * 400 * t)
    mpx = 0.45 * (L + R) + 0.45 * (L - R) * np.sin(2 * np.pi * 38000 * t) + 0.04 * bb * np.sin(
        2 * np.pi * 57000 * t + phase)
    if pilot:
        mpx += 0.09 * np.sin(2 * np.pi * 19000 * t)
    return fm(75000 * mpx, cnr=cnr)


@pytest.mark.parametrize("pilot,cnr,phase,rate_err", [(True, None, 0.0, 0.0), (True, 18, 1.3, 0.0),
                                                      (False, 25, 0.4, 0.0), (True, 25, 0.0, 3e-4)])
def test_rds_decodes_ps_rt_pi(pilot, cnr, phase, rate_err):
    d = RadioDemod(FS, "WFM")
    d.offset = OFF
    run(d, _rds_iq(5, pilot, cnr, phase, rate_err))
    st = d.rds.state()
    assert st["pi"] == 0x7A12 and st["ps"].strip() == "RADIO 1" and st["rt"] == "Hello RDS world 123", st
    assert st["pty_name"] == "Поп-музыка" and st["ct"] == (2026, 10, 24, 11, 30)
    assert st["bad"] <= 0.1 * st["ok"]


def test_rds_crc_and_cyrillic():
    for data in (0, 0x1234, 0xFFFF, 0x7A12):
        for off in ("A", "B", "C", "D"):
            word = (data << 10) | rds.checkword(data, off)
            assert int(rds.crc_table()[word >> 10]) ^ (word & 0x3FF) == rds.OFFSETS[off]
    assert rds.rds_char("Ж".encode("cp1251")[0]) == "Ж" and rds.rds_char(0x41) == "A"
    d = rds.RdsDecoder()
    d.reset()
    assert d.state()["ps"] == "" and not d.state()["synced"]


# ---------------------------------------------------------------- спектр, S-метр
def test_spectrum_dbfs_independent_of_fft_size():
    t = np.arange(65536) / FS
    x = (0.5 * np.exp(2j * np.pi * 75e3 * t)).astype(np.complex64)      # точно в бине при любом БПФ
    for n in (1024, 4096, 16384):
        assert abs(spectrum_db(x, n, dbfs=True).max() - 20 * math.log10(0.5)) < 0.3
    assert s_units(-93, 145e6) == "S9" and s_units(-73, 7e6) == "S9" and s_units(-63, 145e6) == "S9+30"
    assert s_units(-111, 145e6) == "S6"


# ---------------------------------------------------------------- файлы IQ
def test_iq_record_and_playback(tmp_path):
    t = np.arange(48000) / FS
    x = (0.4 * np.exp(2j * np.pi * 123e3 * t)).astype(np.complex64)
    for fmt, tol in (("wav", 1e-4), ("cu8", 0.01)):
        path = iqfile.file_name(str(tmp_path), 101.2e6, FS, fmt)
        rec = iqfile.IqRecorder(path, FS, fmt)
        rec.write(x[:20000])
        rec.write(x[20000:])
        rec.close()
        assert iqfile.parse_name(path) == (FS, 101.2e6)
        r = iqfile.IqReader(path)
        assert r.fs == FS and r.center == 101.2e6 and r.total == x.size
        y = r.read(x.size)
        assert np.max(np.abs(y - x)) < tol
        y2 = r.read(10)                    # по кругу
        assert np.max(np.abs(y2 - x[:10])) < tol
        r.close()
    assert iqfile.parse_name("SDRSharp_20201010_101010Z_101200000Hz_IQ.wav") == (None, 101.2e6)
    assert iqfile.parse_name("rec_2.048MSPS_433.92MHz.cu8") == (2048000, 433.92e6)
    assert iqfile.target_rate(2048000, [960000, 2400000]) == 2400000


def test_iq_file_source_resamples(tmp_path):
    fs_in = 2048000
    t = np.arange(fs_in // 2) / fs_in
    x = (0.4 * np.exp(2j * np.pi * 200e3 * t)).astype(np.complex64)
    path = str(tmp_path / f"t_{fs_in}sps_100000000Hz.cf32")
    x.tofile(path)
    got = []
    src = iqfile.IqFileSource(lambda kind, *a: got.append((kind, a)), path, [960000, 2400000])
    assert src.fs == 2400000 and src.freq == 100000000
    src.open()
    src.t_next = -1e9                      # без задержек реального темпа
    y = np.concatenate([src.read_stream() for _ in range(8)])
    src.close()
    P = np.abs(np.fft.fft(y[4096:4096 + 65536])) ** 2
    f = np.fft.fftfreq(65536, 1 / 2400000)
    assert abs(f[np.argmax(P)] - 200e3) < 100


# ---------------------------------------------------------------- менеджер частот
def test_freqman_csv_xml_roundtrip(tmp_path):
    ent = [freqman.normalize({"name": "Радио Москвы", "freq": 105.3e6, "mode": "WFM", "bw": 200000, "group": "FM"}),
           freqman.normalize({"name": "PMR 1", "freq": 446.00625e6, "mode": "NFM", "group": "Рации"}),
           freqman.normalize({"name": "CW", "freq": 7.03e6, "mode": "CW", "bw": 500})]
    p = str(tmp_path / "f.csv")
    freqman.export_csv(p, ent)
    assert freqman.import_csv(p) == ent
    x = str(tmp_path / "f.xml")
    freqman.export_sdrsharp_xml(x, ent)
    assert freqman.import_sdrsharp_xml(x) == ent
    p2 = tmp_path / "en.csv"
    p2.write_text("name,frequency,mode,bandwidth,group\nAir,125.5 MHz,AM,8330,Avia\nATIS,128000000,am,,Avia\n",
                  encoding="utf-8")
    got = freqman.import_any(str(p2))
    assert [(e["freq"], e["mode"], e["bw"]) for e in got] == [(125.5e6, "AM", 8330.0), (128e6, "AM", 10000.0)]
    assert freqman.merge(ent, got) == 2 and freqman.merge(ent, got) == 0


# ---------------------------------------------------------------- поток чтения RTL-SDR (подставной донгл)
class _FakeL:
    def __init__(self, dev):
        self.dev = dev

    def rtlsdr_read_sync(self, _dev, addr, n, pgot):
        time.sleep(0.002)
        C.memset(addr, self.dev.tuned & 0xFF, n)      # содержимое блока = метка текущей частоты
        pgot._obj.value = n
        return 0


class _FakeDev:
    def __init__(self):
        self.tuned = 0
        self.L = _FakeL(self)
        self.dev = None
        self.gains = [0, 100]
        self.label, self.tuner = "fake", "fake"

    def set_center_freq(self, f):
        time.sleep(0.003)
        self.tuned = int(f) // 1000000

    def set_sample_rate(self, fs):
        pass

    def set_gain(self, g):
        pass

    def reset_buffer(self):
        pass

    def read_bytes(self, n):
        return bytes([self.tuned & 0xFF]) * n

    def close(self):
        pass


def test_rtl_stream_source_retune_tags():
    """После перестройки источник не отдаёт блоки, прочитанные на старой частоте."""
    from rtltv.rtl_stream import RtlStreamSource
    from rtltv import rtlsdr
    src = RtlStreamSource(lambda *a: None, 960000, 100e6)
    src.dev = _FakeDev()
    src.dev.set_center_freq(100e6)
    src._tuned = int(100e6)
    lut = np.round(rtlsdr._LUT * 128 + 127.4).astype(int)
    try:
        for f in (101e6, 102e6, 103e6):
            src.apply("freq", f)
            for _ in range(5):
                iq = src.read_stream()
                v = int(lut[int(np.argmin(np.abs(rtlsdr._LUT - iq.view(np.float32)[0])))])
                assert v == int(f) // 1000000, (f, v)
        # план перестроек (сканер): чтение приостанавливается и возобновляется
        src.read_at(110e6, 1024, True)
        src.apply("freq", 104e6)
        iq = src.read_stream()
        assert int(lut[int(np.argmin(np.abs(rtlsdr._LUT - iq.view(np.float32)[0])))]) == 104
    finally:
        src.running = False
        src._stop_reader()
    assert src.lost == 0


# ---------------------------------------------------------------- звук
def test_audio_latency_control():
    from rtltv.radio_audio import RadioAudioOut, stretch
    a = np.sin(np.arange(2400) / 10).astype(np.float32)
    assert stretch(a, 1.005).shape[0] == 2412 and stretch(np.stack((a, a), 1), 0.995).shape == (2388, 2)
    out = RadioAudioOut()
    out.stream = object()            # «открытое» устройство без звуковой карты
    out.channels, out.rate = 2, 48000
    for _ in range(20):              # блоки приходят быстрее, чем уходят
        out.push(np.zeros((2400, 2), np.float32))
    assert out.buffered() <= RadioAudioOut.HARD + 0.06
    assert out.adjust < 0
    out.stream = None


# ---------------------------------------------------------------- виджет спектра
def test_spectrum_view_zoom_and_ticks():
    from PySide6.QtWidgets import QApplication
    from rtltv.widgets import SpectrumView, mhz_decimals, nice_step
    QApplication.instance() or QApplication([])
    assert nice_step(2.4e6, 1100) == 250000 and mhz_decimals(250000) == 2 and mhz_decimals(1e6) == 1
    assert nice_step(37500, 1100) == 5000 and mhz_decimals(5000) == 3
    v = SpectrumView()
    v.resize(900, 400)
    f = 100e6 + (np.arange(4096) - 2048) * (2.4e6 / 4096)
    v.zoom, v.zoom_center = 4.0, 100.3e6
    v._apply(f, np.zeros(4096))
    span = v.freqs[-1] - v.freqs[0]
    assert abs(span - 2.4e6 / 4) < 2e3 and v.freqs[0] < 100.3e6 < v.freqs[-1]
    v.zoom_center = 101.19e6                   # у края полосы — окно прижимается к краю
    v._apply(f, np.zeros(4096))
    assert v.freqs[-1] <= f[-1] and abs(v.freqs[-1] - f[-1]) < 1e3


# ---------------------------------------------------------------- окно offscreen
def test_radio_window_offscreen():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from rtltv.modes.radio_window import RadioWindow
    w = RadioWindow({"source": "test", "shot": "unused.png", "shot_delay": 9999})
    errors = []
    w.engine.error.connect(errors.append)
    w.resize(1280, 720)
    w.show()

    def pump(sec, until=None):
        t0 = time.time()
        while time.time() - t0 < sec:
            app.processEvents()
            time.sleep(0.005)
            if until and until():
                return True
        return False

    try:
        assert pump(10, lambda: w.rds_ps.text().strip() == "SDR4NOOB"), w.rds_ps.text()
        assert "Тестовый эфир" in w.rds_rt.text() and w.stereo_lbl.text() == "СТЕРЕО"
        # перетаскивание края полосы меняет ширину, полосы — частоту
        w._edge_drag(1, w.fdisp.value + 60000)
        assert abs(w.engine.demod.bw - 120000) < 200
        f0 = w.fdisp.value
        w.cb_snap.setChecked(False)
        w._band_drag(25000)
        assert abs(w.fdisp.value - f0 - 25000) < 2
        w.cb_lock.setChecked(True)
        w._spectrum_click(f0 + 500000)
        assert abs(w.fdisp.value - f0 - 25000) < 2       # замок частоты
        w.cb_lock.setChecked(False)
        w.zoom.setValue(30)
        assert abs(w.spec.zoom - 8) < 0.01
        for m in ("NFM", "AM", "USB", "LSB", "CW", "DMR", "WFM"):
            w._set_mode(m)
            pump(0.3)
            assert w.engine.demod.mode == m
            assert w.sections["rds"].isVisible() == (m == "WFM")
        # менеджер частот: переход и запись
        w.fm.tuneRequested.emit({"name": "x", "freq": 446.00625e6, "mode": "NFM", "bw": 12500, "group": "g"})
        assert w._mode() == "NFM" and abs(w.fdisp.value - 446.00625e6) < 1
        w.sections["agc"].set_expanded(True)
        w.cb_agc_on.setChecked(False)
        assert not w.engine.demod.agc.on
        assert not errors, errors
        # ничего не вылезает за пределы окна на 1280×720
        side = w.sections["src"].parentWidget()
        assert side.minimumSizeHint().width() <= 410
    finally:
        w.close()
        pump(0.3)


def test_smeter_calibration_offset():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from rtltv.modes.radio_window import RadioWindow
    w = RadioWindow({"source": "test"})
    try:
        w.cb_agc.setChecked(False)
        w.scal.setValue(0.0)
        w._on_level(-40.0, 0.0, False)
        raw = w._last_dbm_raw
        assert raw is not None and "dBm" in w.lvl.text
        w.scal.setValue(12.5)                       # поправка сохраняется в настройки режима
        assert w.s["smeter_cal"] == 12.5
        w._on_level(-40.0, 0.0, False)
        assert abs(w._last_dbm_raw - raw) < 1e-9    # «сырой» уровень от поправки не зависит
    finally:
        w.close()
