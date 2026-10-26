"""Тесты слоя устройств (rtltv.devices): rtl_tcp, перечисление USB, загрузка DLL, HackRF/Airspy на заглушках.

Заглушки hackrf.dll / airspy.dll собираются MSVC (как tools/build_lrpt_dsp.bat) во временную папку и
генерируют синтетический тон из своего потока через callback — так проверяется весь путь
callback -> очередь -> пересчёт частоты дискретизации -> sink без настоящего железа.
Если MSVC нет, эти тесты пропускаются.

Запуск: py -3.12 -m pytest tests/test_devices.py -v
"""
import ctypes as C
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtltv import devices, rtlsdr, settings  # noqa: E402

win_only = pytest.mark.skipif(sys.platform != "win32", reason="только Windows: SetupAPI, PE, DLL")

BUILTIN = rtlsdr._dll_path()
VCVARS = r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Свои настройки и своя папка драйверов: настоящие %APPDATA%/%LOCALAPPDATA% не трогаем."""
    monkeypatch.setattr(settings, "DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(devices, "DRIVERS_DIR", str(tmp_path / "drivers"))
    devices._scan_cache.clear()
    monkeypatch.setattr(devices, "_hw_cache", None)   # кэш опроса USB — свой на тест
    yield
    rtlsdr.set_dll_override(None)


class Collector:
    def __init__(self):
        self.events = []
        self.cv = threading.Condition()

    def __call__(self, kind, *args):
        with self.cv:
            self.events.append((kind, args))
            self.cv.notify_all()

    def wait(self, pred, timeout=5.0):
        t_end = time.time() + timeout
        with self.cv:
            while True:
                hit = [e for e in self.events if pred(e)]
                if hit:
                    return hit
                left = t_end - time.time()
                if left <= 0:
                    return []
                self.cv.wait(left)

    def first(self, kind, timeout=5.0):
        hit = self.wait(lambda e: e[0] in (kind, "error"), timeout)
        assert hit, f"нет события {kind}"
        assert hit[0][0] == kind, f"ошибка источника: {hit[0][1]}"
        return hit[0][1]


def peak_hz(iq, fs):
    n = 1 << 16
    sp = np.abs(np.fft.fftshift(np.fft.fft(iq[-n:] * np.hanning(n))))
    return (np.argmax(sp) - n // 2) * fs / n


# ======================================================================
#  rtl_tcp: мини-сервер на сокете
# ======================================================================
class MiniRtlTcp(threading.Thread):
    """Сервер rtl_tcp: заголовок RTL0 + R820T, принимает 5-байтовые команды, шлёт u8 IQ с тоном."""

    def __init__(self, tone_hz=200e3, magic=b"RTL0"):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(2)
        self.port = self.srv.getsockname()[1]
        self.magic = magic
        self.tone = tone_hz
        self.fs = 2048000
        self.cmds = []
        self.alive = True
        self.clients = 0

    def run(self):
        while self.alive:
            try:
                self.srv.settimeout(0.3)
                c, _a = self.srv.accept()
            except (socket.timeout, OSError):
                continue
            self.clients += 1
            self._serve(c)

    def _serve(self, c):
        c.sendall(self.magic + struct.pack(">II", 5, 29))
        c.settimeout(0.001)
        ph, pend = 0.0, b""
        try:
            while self.alive:
                try:
                    data = c.recv(4096)
                    if not data:
                        return
                    pend += data
                    while len(pend) >= 5:
                        cmd, val = struct.unpack(">BI", pend[:5])
                        pend = pend[5:]
                        self.cmds.append((cmd, val))
                        if cmd == 0x02:
                            self.fs = val
                except socket.timeout:
                    pass
                n = 16384
                k = np.arange(n)
                z = np.exp(1j * (ph + 2 * np.pi * self.tone / self.fs * k))
                ph = float((ph + 2 * np.pi * self.tone / self.fs * n) % (2 * np.pi))
                raw = np.empty(2 * n, np.uint8)
                raw[0::2] = np.clip(np.round(127.4 + 100 * z.real), 0, 255)
                raw[1::2] = np.clip(np.round(127.4 + 100 * z.imag), 0, 255)
                c.settimeout(2.0)
                c.sendall(raw.tobytes())
                c.settimeout(0.001)
        except OSError:
            pass
        finally:
            c.close()

    def stop(self):
        self.alive = False
        self.srv.close()


@pytest.fixture
def rtltcp_server():
    s = MiniRtlTcp()
    s.start()
    yield s
    s.stop()


def test_rtltcp_stream_and_commands(rtltcp_server):
    col = Collector()
    src = devices.make_source(f"rtltcp:127.0.0.1:{rtltcp_server.port}", col, 2048000, 100_000_000)
    assert isinstance(src, devices.RtlTcpSource) and src.HARDWARE
    src.gain = 300
    src.start()
    try:
        info = col.first("opened")[0]
        assert info["tuner"].startswith("R820T") and info["gains"] == devices.R828D_GAINS
        blocks = col.wait(lambda e: e[0] == "stream" and len([x for x in col.events if x[0] == "stream"]) >= 3)
        assert blocks
        freq, iq = blocks[-1][1]
        assert freq == 100_000_000 and iq.dtype == np.complex64 and iq.size == devices.BLOCK
        assert abs(peak_hz(iq, 2048000) - 200e3) < 100
        for name, val in (("freq", 145_800_000), ("gain", None), ("gain", 402), ("ppm", -3), ("bias", True),
                          ("rtl_agc", True), ("rate", 2400000)):
            src.command(name, val)
        col.wait(lambda e: e[0] == "stream" and e[1][0] == 145_800_000)
        time.sleep(0.3)
        cmds = rtltcp_server.cmds
        assert (0x02, 2048000) in cmds and (0x01, 100_000_000) in cmds
        assert (0x01, 145_800_000) in cmds
        assert (0x03, 0) in cmds and (0x03, 1) in cmds and (0x04, 402) in cmds and (0x04, 300) in cmds
        assert (0x05, (-3) & 0xFFFFFFFF) in cmds and (0x0E, 1) in cmds and (0x08, 1) in cmds
        assert (0x02, 2400000) in cmds
        # план перестроек (как у сканеров и измерителя мультиплекса)
        src.command("plan", {"tag": "t", "items": [(150e6, 65536), (151e6, 65536)], "repeat": False})
        done = col.wait(lambda e: e[0] == "plan_done", 5)
        assert done
        plans = [e[1] for e in col.events if e[0] == "plan"]
        assert [p[3] for p in plans[-2:]] == [150e6, 151e6] and all(p[4].size == 65536 for p in plans[-2:])
    finally:
        src.stop()
        src.join(3)
    assert not src.is_alive()


def test_rtltcp_probe_and_errors(rtltcp_server):
    info = devices.probe_rtltcp("127.0.0.1", rtltcp_server.port)
    assert info["tuner"].startswith("R820T") and info["gains"] == 29
    # закрытый порт
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    with pytest.raises(devices.DeviceError, match="открыть"):
        devices.probe_rtltcp("127.0.0.1", dead, timeout=1)
    # сервер, но не rtl_tcp
    bad = MiniRtlTcp(magic=b"HTTP")
    bad.start()
    try:
        with pytest.raises(devices.DeviceError, match="не сервер rtl_tcp"):
            devices.probe_rtltcp("127.0.0.1", bad.port)
    finally:
        bad.stop()
    col = Collector()
    src = devices.make_source(f"rtltcp:127.0.0.1:{dead}", col, 2048000, 100e6)
    src.start()
    err = col.wait(lambda e: e[0] == "error")
    assert err and "открыть" in err[0][1][0]


def test_parse_hostport():
    assert devices.parse_hostport("192.168.1.10:1234") == ("192.168.1.10", 1234)
    assert devices.parse_hostport("pi.local") == ("pi.local", 1234)
    assert devices.parse_hostport("[::1]:7373") == ("::1", 7373)
    with pytest.raises(devices.DeviceError):
        devices.parse_hostport("host:99999")


# ======================================================================
#  USB
# ======================================================================
@win_only
def test_usb_this_machine_rtl_v4_winusb():
    found = devices.usb_devices()
    rtl = [d for d in found if (d["vid"], d["pid"]) == (0x0BDA, 0x2838)]
    if not rtl:
        pytest.skip("RTL-SDR 0bda:2838 к этой машине не подключён")
    assert rtl[0]["service"].lower() == "winusb" and rtl[0]["ready"]
    assert rtl[0]["kind"] == "rtl"


@win_only
def test_usb_grouping_and_hints():
    raw = [
        (r"USB\VID_0BDA&PID_2838\00000001", r"USB\ROOT_HUB30\4&1", "usbccgp", "Составное USB устройство"),
        (r"USB\VID_0BDA&PID_2838&MI_00\9&AA&0&0000", r"USB\VID_0BDA&PID_2838\00000001", "RTL2832UBDA", "Bulk-In"),
        (r"USB\VID_0BDA&PID_2838&MI_01\9&AA&0&0001", r"USB\VID_0BDA&PID_2838\00000001", "", "Bulk-In"),
        (r"USB\VID_1D50&PID_6089\0000000000000000A06063C8", r"USB\ROOT_HUB30\4&1", "WinUSB", "HackRF One"),
        (r"USB\VID_1DF7&PID_3010\5&1", r"USB\ROOT_HUB30\4&1", "", "RSP1A"),
        (r"USB\VID_046D&PID_C52B\5&2", r"USB\ROOT_HUB30\4&1", "HidUsb", "мышь"),
    ]
    found = {d["kind"]: d for d in devices.usb_devices(raw)}
    assert set(found) == {"rtl", "hackrf", "sdrplay"}
    rtl = found["rtl"]
    assert rtl["service"] == "RTL2832UBDA" and not rtl["ready"] and "Zadig" in rtl["hint"]
    assert "zadig.akeo.ie" in rtl["hint"] and "Interface 0" in rtl["hint"]
    hk = found["hackrf"]
    assert hk["state"] == "WinUSB" and not hk["ready"] and "hackrf.dll" in hk["hint"]
    assert "rsp_tcp" in found["sdrplay"]["hint"]


# ======================================================================
#  Загрузка драйверов (DLL)
# ======================================================================
@win_only
def test_import_rtl_dll_and_load_by_path(tmp_path):
    src_dir = tmp_path / "user"
    src_dir.mkdir()
    user_dll = src_dir / "rtlsdr.dll"
    shutil.copy(BUILTIN, user_dll)
    res = devices.import_dll(str(user_dll))
    assert res["kind"] == "rtl" and res["error"] is None
    assert os.path.isfile(os.path.join(devices.DRIVERS_DIR, "rtlsdr.dll"))
    L_user = rtlsdr.lib(res["path"])
    L_builtin = rtlsdr.lib(BUILTIN)
    assert L_user._handle != L_builtin._handle, "копия должна загрузиться отдельным модулем"
    assert rtlsdr.device_count(res["path"]) == rtlsdr.device_count(BUILTIN)
    # выбор этой DLL для всех окон
    d = devices.load_dev_settings()
    d["rtl_dll"] = "rtlsdr.dll"
    devices.save_dev_settings(d)
    assert os.path.abspath(devices.apply_rtl_driver()) == os.path.abspath(res["path"])
    assert os.path.abspath(rtlsdr.active_dll_path()) == os.path.abspath(res["path"])
    ents = devices.driver_dlls()
    assert [e["kind"] for e in ents] == ["rtl"]
    assert devices.find_driver("rtl") == res["path"]
    d["rtl_dll"] = None
    devices.save_dev_settings(d)
    devices.apply_rtl_driver()
    assert rtlsdr.active_dll_path() == BUILTIN


@win_only
def test_reject_32bit_and_foreign_dll(tmp_path):
    p32 = tmp_path / "librtlsdr32.dll"
    data = bytearray(open(BUILTIN, "rb").read())
    off = struct.unpack_from("<I", data, 0x3C)[0]
    struct.pack_into("<H", data, off + 4, 0x14C)       # IMAGE_FILE_MACHINE_I386
    p32.write_bytes(bytes(data))
    with pytest.raises(devices.DriverError, match="64-битн"):
        devices.import_dll(str(p32))
    k32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "kernel32.dll")
    with pytest.raises(devices.DriverError, match="не похожа на драйвер SDR"):
        devices.import_dll(k32)
    txt = tmp_path / "fake.dll"
    txt.write_text("not a dll")
    with pytest.raises(devices.DriverError, match="не DLL"):
        devices.import_dll(str(txt))
    other = tmp_path / "x.txt"
    other.write_text("x")
    with pytest.raises(devices.DriverError, match="нужен файл .dll"):
        devices.import_dll(str(other))
    assert not os.path.isdir(devices.DRIVERS_DIR) or not os.listdir(devices.DRIVERS_DIR)


def test_missing_backend_messages():
    for sid, name in (("hackrf:0", devices.PREFERRED_NAMES["hackrf"][0]),
                      ("airspy:0", devices.PREFERRED_NAMES["airspy"][0])):
        col = Collector()
        src = devices.make_source(sid, col, 2400000, 100e6)
        src.start()
        err = col.wait(lambda e: e[0] == "error")
        assert err, sid
        msg = err[0][1][0]
        assert name in msg and "Устройства" in msg and devices.DRIVERS_DIR in msg
    col = Collector()
    devices.make_source("sdrplay:0", col, 2400000, 100e6).start()
    err = col.wait(lambda e: e[0] == "error")
    assert "rsp_tcp" in err[0][1][0]
    srcs = dict(devices.available_sources())
    assert "test" in srcs and not any(k.startswith(("hackrf", "airspy")) for k in srcs)


def test_gain_mapping():
    for t in devices.HACKRF_GAINS:
        amp, lna, vga = devices.hackrf_split(t)
        assert lna % 8 == 0 and 0 <= lna <= 40 and vga % 2 == 0 and 0 <= vga <= 62
        assert lna + vga + (14 if amp else 0) == t // 10, t
    assert devices.hackrf_split(None) == devices.HACKRF_DEFAULT
    assert devices.airspy_step(0) == 0 and devices.airspy_step(420) == 21 and devices.airspy_step(999) == 21


def test_stream_resampler_seamless():
    from scipy.signal import resample_poly
    rng = np.random.default_rng(1)
    x = (rng.standard_normal(300000) + 1j * rng.standard_normal(300000)).astype(np.complex64)
    for fin, fout in ((2500000, 2400000), (2880000, 960000), (2500000, 2560000), (10000000, 3200000)):
        rs = devices.StreamResampler(fin, fout)
        assert rs.fs_out == fout
        cuts = np.cumsum(rng.integers(1000, 40000, 40))
        parts = [rs.process(p) for p in np.split(x, cuts[cuts < x.size])]
        y = np.concatenate(parts)
        # тот же фильтр одним куском: совпадение с точностью float32 => швов между блоками нет
        from scipy.signal import upfirdn
        ref = upfirdn(rs.h, x, rs.up, rs.down)
        assert np.max(np.abs(y - ref[:y.size])) < 1e-4, (fin, fout)
        assert abs(y.size - x.size * fout / fin) < rs.K + 2
        assert abs(resample_poly(x, rs.up, rs.down).size - y.size) < rs.K + 2


# ======================================================================
#  HackRF / Airspy на заглушках (MSVC)
# ======================================================================
# Заглушки без CRT и Windows SDK (как lrpt_dsp.dll): функции kernel32 передаются из Python (stub_setup),
# тон — ровно fs/16 по таблице из 16 точек (без sin/cos).
STUB_COMMON = r"""
typedef unsigned char u8; typedef signed char i8; typedef unsigned int u32; typedef unsigned long long u64;
#define EXP __declspec(dllexport)
int _fltused = 0;
typedef void* (*create_thread_t)(void*, u64, u32 (*)(void*), void*, u32, u32*);
typedef void (*sleep_t)(u32);
typedef u32 (*wait_t)(void*, u32);
typedef int (*close_t)(void*);
static create_thread_t p_create; static sleep_t p_sleep; static wait_t p_wait; static close_t p_close;
EXP void stub_setup(void* c, void* s, void* w, void* h) {
    p_create = (create_thread_t)c; p_sleep = (sleep_t)s; p_wait = (wait_t)w; p_close = (close_t)h; }
static const double COS16[16] = {1.0, 0.9238795325, 0.7071067812, 0.3826834324, 0.0, -0.3826834324,
    -0.7071067812, -0.9238795325, -1.0, -0.9238795325, -0.7071067812, -0.3826834324, 0.0, 0.3826834324,
    0.7071067812, 0.9238795325};
#define SIN16(k) COS16[((k) + 12) & 15]
static volatile int g_run; static void* g_thr; static double g_rate = 10e6;
"""

STUB_HACKRF = STUB_COMMON + r"""
typedef struct { void* device; u8* buffer; int buffer_length; int valid_length; void* rx_ctx; void* tx_ctx; } T;
typedef int (*cb_t)(T*);
typedef struct { char** sn; int* ids; int* idx; int devicecount; void** usb; int usbcount; } L;
static char sn0[] = "0000000000000000a06063c8234e925f";
static char* sns[1] = {sn0};
static int ids[1] = {2};
static L g_list;
static u64 g_freq; static cb_t g_cb;
static int g_lna = -1, g_vga = -1, g_amp = -1, g_ant = -1;
static i8 buf[262144];
static u32 worker(void* p) {
    T t; int i, k = 0;
    t.device = 0; t.buffer = (u8*)buf; t.buffer_length = sizeof(buf); t.valid_length = sizeof(buf);
    t.rx_ctx = 0; t.tx_ctx = 0;
    while (g_run) {
        for (i = 0; i < (int)sizeof(buf) / 2; i++, k = (k + 1) & 15) {
            buf[2*i] = (i8)(int)(100.0 * COS16[k]); buf[2*i+1] = (i8)(int)(100.0 * SIN16(k)); }
        if (g_cb(&t) != 0) break;
        p_sleep((u32)(1000.0 * (sizeof(buf) / 2) / g_rate));
    }
    return 0;
}
EXP int hackrf_init(void) { g_list.sn = sns; g_list.ids = ids; g_list.devicecount = 1; g_list.usbcount = 1; return 0; }
EXP L* hackrf_device_list(void) { return &g_list; }
EXP int hackrf_device_list_open(L* l, int i, void** d) { if (i >= 1) return -1000; *d = (void*)0x1234; return 0; }
EXP void hackrf_device_list_free(L* l) {}
EXP int hackrf_close(void* d) { return 0; }
EXP int hackrf_set_freq(void* d, u64 f) { g_freq = f; return 0; }
EXP int hackrf_set_sample_rate(void* d, double r) { g_rate = r; return 0; }
EXP u32 hackrf_compute_baseband_filter_bw(u32 bw) { return bw < 1750000 ? 1750000 : bw; }
EXP int hackrf_set_baseband_filter_bandwidth(void* d, u32 bw) { return 0; }
EXP int hackrf_set_lna_gain(void* d, u32 v) { g_lna = (int)v; return 0; }
EXP int hackrf_set_vga_gain(void* d, u32 v) { g_vga = (int)v; return 0; }
EXP int hackrf_set_amp_enable(void* d, u8 v) { g_amp = v; return 0; }
EXP int hackrf_set_antenna_enable(void* d, u8 v) { g_ant = v; return 0; }
EXP const char* hackrf_error_name(int e) { return "HACKRF_ERROR_STUB"; }
EXP int hackrf_board_id_read(void* d, u8* v) { *v = 2; return 0; }
EXP const char* hackrf_board_id_name(int b) { return "HackRF One"; }
EXP int hackrf_start_rx(void* d, cb_t cb, void* ctx) { g_cb = cb; g_run = 1; g_thr = p_create(0, 0, worker, 0, 0, 0); return 0; }
EXP int hackrf_stop_rx(void* d) { g_run = 0; p_wait(g_thr, 3000); p_close(g_thr); return 0; }
EXP double stub_rate(void) { return g_rate; }
EXP u64 stub_freq(void) { return g_freq; }
EXP int stub_gain(int w) { return w == 0 ? g_lna : w == 1 ? g_vga : w == 2 ? g_amp : g_ant; }
"""

STUB_AIRSPY = STUB_COMMON + r"""
typedef struct { void* device; void* ctx; void* samples; int sample_count; u64 dropped; int type; } T;
typedef int (*cb_t)(T*);
static u32 g_freq; static cb_t g_cb;
static int g_lin = -1, g_lagc = -1, g_magc = -1, g_type = -1, g_bias = -1;
static float buf[2 * 65536];
static u32 worker(void* p) {
    T t; int i, k = 0;
    t.device = 0; t.ctx = 0; t.samples = buf; t.sample_count = 65536; t.dropped = 0; t.type = 0;
    while (g_run) {
        for (i = 0; i < 65536; i++, k = (k + 1) & 15) { buf[2*i] = (float)(0.5 * COS16[k]); buf[2*i+1] = (float)(0.5 * SIN16(k)); }
        if (g_cb(&t) != 0) break;
        p_sleep((u32)(1000.0 * 65536 / g_rate));
    }
    return 0;
}
EXP int airspy_init(void) { return 0; }
EXP int airspy_list_devices(u64* s, int n) { if (s && n > 0) s[0] = 0x1234ABCD5678EF00ULL; return 1; }
EXP int airspy_open(void** d) { *d = (void*)1; return 0; }
EXP int airspy_open_sn(void** d, u64 sn) { *d = (void*)1; return 0; }
EXP int airspy_close(void* d) { return 0; }
EXP int airspy_get_samplerates(void* d, u32* b, u32 n) { if (n == 0) { b[0] = 2; return 0; } b[0] = 10000000; b[1] = 2500000; return 0; }
EXP int airspy_set_samplerate(void* d, u32 r) { g_rate = r == 0 ? 10e6 : r == 1 ? 2.5e6 : (double)r; return 0; }
EXP int airspy_set_sample_type(void* d, int t) { g_type = t; return 0; }
EXP int airspy_set_freq(void* d, u32 f) { g_freq = f; return 0; }
EXP int airspy_set_linearity_gain(void* d, u8 v) { g_lin = v; return 0; }
EXP int airspy_set_lna_agc(void* d, u8 v) { g_lagc = v; return 0; }
EXP int airspy_set_mixer_agc(void* d, u8 v) { g_magc = v; return 0; }
EXP int airspy_set_rf_bias(void* d, u8 v) { g_bias = v; return 0; }
EXP const char* airspy_error_name(int e) { return "AIRSPY_ERROR_STUB"; }
EXP int airspy_start_rx(void* d, cb_t cb, void* ctx) { g_cb = cb; g_run = 1; g_thr = p_create(0, 0, worker, 0, 0, 0); return 0; }
EXP int airspy_stop_rx(void* d) { g_run = 0; p_wait(g_thr, 3000); p_close(g_thr); return 0; }
EXP double stub_rate(void) { return g_rate; }
EXP u32 stub_freq(void) { return g_freq; }
EXP int stub_get(int w) { return w == 0 ? g_lin : w == 1 ? g_lagc : w == 2 ? g_magc : w == 3 ? g_type : g_bias; }
"""


def stub_lib(path):
    """Открыть заглушку и передать ей функции kernel32 (своих импортов у неё нет)."""
    S = C.CDLL(path)
    k = C.WinDLL("kernel32")
    S.stub_setup.argtypes = [C.c_void_p] * 4
    S.stub_setup(*(C.cast(getattr(k, n), C.c_void_p) for n in
                   ("CreateThread", "Sleep", "WaitForSingleObject", "CloseHandle")))
    S.stub_rate.restype = C.c_double
    S.stub_freq.restype = C.c_uint64
    return S


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    if not os.path.isfile(VCVARS):
        pytest.skip("нет MSVC BuildTools — заглушки DLL не собрать")
    d = tmp_path_factory.mktemp("stubs")
    out = {}
    for name, code in (("hackrf", STUB_HACKRF), ("airspy", STUB_AIRSPY)):
        (d / f"{name}_stub.c").write_text(code, encoding="ascii")
        bat = d / f"build_{name}.bat"
        bat.write_text(f'@echo off\r\ncall "{VCVARS}" >nul 2>nul\r\ncd /d "{d}"\r\n'
                       f'cl /nologo /O1 /GS- /LD /TC {name}_stub.c /Fe{name}.dll /link /NOLOGO /NODEFAULTLIB '
                       f'/NOENTRY >build_{name}.log 2>&1\r\n', encoding="ascii")
        subprocess.run(["cmd", "/c", str(bat)], check=False, timeout=180)
        dll = d / f"{name}.dll"
        if not dll.is_file():
            pytest.skip(f"заглушка {name}.dll не собралась: {(d / f'build_{name}.log').read_text(errors='replace')}")
        out[name] = str(dll)
    return out


def test_import_stub_drivers(stubs):
    for kind in ("hackrf", "airspy"):
        res = devices.import_dll(stubs[kind])
        assert res["kind"] == kind and res["error"] is None
        assert devices.find_driver(kind) == res["path"]
    srcs = dict(devices.available_sources())
    assert srcs.get("hackrf:0", "").startswith("HackRF #0")
    assert srcs.get("airspy:0", "").startswith("Airspy #0")


def test_hackrf_stub_stream_resampled(stubs):
    res = devices.import_dll(stubs["hackrf"])
    S = stub_lib(res["path"])
    col = Collector()
    src = devices.make_source("hackrf:0", col, 960000, 137_100_000)
    assert isinstance(src, devices.HackRfSource)
    src.gain = 300
    src.start()
    try:
        info = col.first("opened")[0]
        assert info["label"] == "HackRF One" and info["gains"] == devices.HACKRF_GAINS
        assert info["native_fs"] == 2880000 and "пересчитывается" in info["note"]
        assert S.stub_rate() == 2880000.0 and S.stub_freq() == 137_100_000
        assert S.stub_gain(0) + S.stub_gain(1) == 30 and S.stub_gain(2) == 0
        got = col.wait(lambda e: e[0] == "stream" and len([x for x in col.events if x[0] == "stream"]) >= 2, 8)
        freq, iq = got[-1][1]
        assert freq == 137_100_000 and iq.dtype == np.complex64 and iq.size == devices.BLOCK
        assert abs(peak_hz(iq, 960000) - 2880000 / 16) < 50
        assert 0.6 < np.median(np.abs(iq)) < 0.9
        src.command("ppm", 10)
        src.command("freq", 145_800_000)
        src.command("bias", True)
        src.command("gain", 1100)
        col.wait(lambda e: e[0] == "stream" and e[1][0] == 145_800_000, 8)
        assert abs(S.stub_freq() - 145_800_000 / (1 + 10e-6)) < 2
        assert S.stub_gain(3) == 1 and S.stub_gain(2) == 1
        src.command("rate", 2400000)
        time.sleep(0.6)
        assert S.stub_rate() == 2400000.0 and src.rs is None
    finally:
        src.stop()
        src.join(3)
    assert not src.is_alive()


def test_airspy_stub_stream_resampled(stubs):
    res = devices.import_dll(stubs["airspy"])
    S = stub_lib(res["path"])
    col = Collector()
    src = devices.make_source("airspy:0", col, 2400000, 101_200_000)
    src.gain = None
    src.start()
    try:
        info = col.first("opened")[0]
        assert info["native_fs"] == 2500000 and info["gains"] == devices.AIRSPY_GAINS
        assert S.stub_rate() == 2500000.0 and S.stub_get(3) == 0     # FLOAT32_IQ
        assert S.stub_get(1) == 1 and S.stub_get(2) == 1              # АРУ
        got = col.wait(lambda e: e[0] == "stream" and len([x for x in col.events if x[0] == "stream"]) >= 2, 8)
        iq = got[-1][1][1]
        assert iq.size == devices.BLOCK and abs(peak_hz(iq, 2400000) - 2500000 / 16) < 60
        src.command("gain", 300)
        time.sleep(0.3)
        assert S.stub_get(0) == 15 and S.stub_get(1) == 0
        src.command("freq", 10_000_000)       # вне диапазона Airspy
        err = col.wait(lambda e: e[0] == "error", 5)
        assert err and "24–1800" in err[0][1][0]
    finally:
        src.stop()
        src.join(3)


# ======================================================================
#  Окно «Устройства…» (offscreen)
# ======================================================================
def test_devices_dialog_offscreen(rtltcp_server, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if os.name == "nt":
        os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
    from PySide6.QtWidgets import QApplication, QComboBox
    from rtltv.devices_dialog import DevicesDialog
    app = QApplication.instance() or QApplication([])
    assert app is not None
    d = devices.load_dev_settings()
    d["rtltcp"] = [f"127.0.0.1:{rtltcp_server.port}"]
    devices.save_dev_settings(d)
    dlg = DevicesDialog()
    labels = [dlg.sources.item(i).text() for i in range(dlg.sources.count())]
    assert any("rtl_tcp 127.0.0.1" in t for t in labels) and labels[-1].startswith("Тестовый")
    assert dlg.usb.topLevelItemCount() >= 1 and dlg.rtl_combo.count() >= 1
    saved = devices.load_dev_settings()["sources"]
    assert [f"rtltcp:127.0.0.1:{rtltcp_server.port}", f"rtl_tcp 127.0.0.1:{rtltcp_server.port} (сеть)"] in saved
    combo = QComboBox()
    devices.fill_source_combo(combo, "rtl:7", "Демо-генератор (без антенны)")
    assert combo.itemText(0) == "RTL-SDR #7 (не найден)" and combo.currentData() == "rtl:7"
    assert combo.itemData(combo.count() - 1) == "test"
    dlg.close()


def test_hardware_scan_cached(monkeypatch):
    """Опрос USB (librtlsdr открывает донгл ради подписи, ~3 с) кэшируется: окна режимов берут готовый список,
    refresh=True и смена драйвера опрашивают заново, max_age не повторяет только что законченный опрос."""
    calls = []
    monkeypatch.setattr(devices, "_hw_cache", None)
    monkeypatch.setattr(devices, "apply_rtl_driver", lambda d=None: None)
    monkeypatch.setattr(devices, "find_driver", lambda kind, drivers_dir=None: None)
    dev = {"rtl_dll": None, "rtltcp": ["10.0.0.5:1234"]}
    monkeypatch.setattr(devices, "load_dev_settings", lambda: dict(dev))
    monkeypatch.setattr(rtlsdr, "device_count", lambda path=None: calls.append("count") or 1)
    monkeypatch.setattr(rtlsdr, "device_label", lambda i, path=None: calls.append("label") or "Blog V4")
    assert not devices.has_hardware()
    src = devices.available_sources()
    assert ("rtl:0", "RTL-SDR #0: Blog V4") in src and ("rtltcp:10.0.0.5:1234", "rtl_tcp 10.0.0.5:1234 (сеть)") in src
    assert src[-1][0] == "test" and devices.has_hardware()
    assert calls == ["count", "label"]
    devices.available_sources()                       # второе окно — из кэша
    devices.scan_hardware(refresh=True, max_age=5)    # только что опрошено — не повторять
    assert calls == ["count", "label"]
    devices.available_sources(refresh=True)           # «Устройства…» — заново
    assert calls == ["count", "label"] * 2
    dev["rtl_dll"] = "my.dll"                         # другой драйвер — кэш не годится
    devices.available_sources()
    assert len(calls) == 6
    # фоновый опрос: окно ждёт его под замком, а не опрашивает второй раз
    monkeypatch.setattr(devices, "_hw_cache", None)
    gate = threading.Event()
    monkeypatch.setattr(rtlsdr, "device_label", lambda i, path=None: gate.wait(5) and "Blog V4")
    calls.clear()
    devices.prefetch()
    time.sleep(0.2)
    t = threading.Timer(0.3, gate.set)
    t.start()
    out, _err = devices.scan_hardware()
    assert out == [("rtl:0", "RTL-SDR #0: Blog V4")] and calls == ["count"]
