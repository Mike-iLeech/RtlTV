"""Устройства SDR: список источников, фабрика источника по идентификатору, драйверы (DLL) пользователя.

Идентификаторы источников (значения комбобоксов «Источник» во всех режимах):
  "rtl:N"             RTL-SDR через librtlsdr: встроенную или выбранную в «Режим → Устройства…»
  "rtltcp:host:port"  сетевой rtl_tcp (и совместимые серверы: rsp_tcp для SDRplay и т. п.)
  "hackrf:N"          HackRF One через hackrf.dll, загруженную пользователем
  "airspy:N"          Airspy R2 / Mini через airspy.dll, загруженную пользователем
  "sdrplay:N"         не поддерживается напрямую (API v3 не реализован) — см. rsp_tcp
  "test"              тестовый генератор

Все источники совместимы с source.SourceBase: те же команды ("freq", "rate", "gain", "ppm", "bias",
"plan", ...), тот же формат sink ("opened", info), ("stream", freq, iq), ("plan", ...), отсчёты complex64.
Усиление везде — десятые дБ или None (АРУ); info["gains"] — доступные значения в десятых дБ.
Если устройство не умеет запрошенную частоту дискретизации, поток пересчитывается (StreamResampler).

Драйверы пользователя лежат в %LOCALAPPDATA%\\RtlTV\\drivers\\ (каталог добавляется в поиск DLL).
"""
import collections
import ctypes as C
import math
import os
import shutil
import socket
import struct
import threading
import time
from fractions import Fraction

import numpy as np

from . import rtlsdr, settings
from .source import BLOCK_BYTES, R828D_GAINS, SourceBase, TestSource
from . import paths

DRIVERS_DIR = paths.service("drivers")
BLOCK = BLOCK_BYTES // 2          # отсчётов в блоке потока — как у RtlSource (≈51 мс при 2,56 МГц)
ZADIG_URL = "https://zadig.akeo.ie"


class DeviceError(RuntimeError):
    pass


class DriverError(DeviceError):
    pass


# ======================================================================
#  Драйверы (DLL): проверка, импорт в папку драйверов, поиск
# ======================================================================
MACHINES = {0x8664: "x64", 0x14C: "x86 (32 бита)", 0xAA64: "ARM64", 0x1C4: "ARM", 0x200: "Itanium"}

HACKRF_FUNCS = ["hackrf_init", "hackrf_device_list", "hackrf_device_list_open", "hackrf_device_list_free",
                "hackrf_close", "hackrf_set_freq", "hackrf_set_sample_rate", "hackrf_set_lna_gain",
                "hackrf_set_vga_gain", "hackrf_set_amp_enable", "hackrf_start_rx", "hackrf_stop_rx"]
AIRSPY_FUNCS = ["airspy_list_devices", "airspy_open_sn", "airspy_close", "airspy_get_samplerates",
                "airspy_set_samplerate", "airspy_set_sample_type", "airspy_set_freq", "airspy_set_linearity_gain",
                "airspy_set_lna_agc", "airspy_set_mixer_agc", "airspy_start_rx", "airspy_stop_rx"]
RTL_FUNCS = ["rtlsdr_get_device_count", "rtlsdr_open", "rtlsdr_close", "rtlsdr_set_center_freq",
             "rtlsdr_set_sample_rate", "rtlsdr_read_sync", "rtlsdr_reset_buffer", "rtlsdr_get_tuner_gains"]

# вид: (название, функция-признак, обязательные функции, поддерживается ли RtlTV)
DRIVER_KINDS = {
    "rtl": ("RTL-SDR (librtlsdr)", "rtlsdr_open", RTL_FUNCS, True),
    "hackrf": ("HackRF (libhackrf)", "hackrf_init", HACKRF_FUNCS, True),
    "airspy": ("Airspy R2/Mini (libairspy)", "airspy_open", AIRSPY_FUNCS, True),
    "airspyhf": ("Airspy HF+ (libairspyhf)", "airspyhf_open", ["airspyhf_open"], False),
    "sdrplay": ("SDRplay API", "sdrplay_api_Open", ["sdrplay_api_Open"], False),
}
DEP_MARKERS = {"libusb_init": "libusb-1.0", "pthread_create": "pthreads"}
DEP_NAMES = ("libusb", "pthread", "libwinpthread", "libgcc", "libstdc++", "msvcp", "vcruntime", "libfftw")
PREFERRED_NAMES = {"hackrf": ("hackrf.dll", "libhackrf.dll"), "airspy": ("airspy.dll", "libairspy.dll")}

UNSUPPORTED_HINT = {
    "sdrplay": "Прямая работа с SDRplay API v3 в RtlTV не реализована. Запустите сервер rsp_tcp "
               "(совместим с rtl_tcp) и добавьте его кнопкой «Добавить сетевой rtl_tcp…».",
    "airspyhf": "Airspy HF+ в RtlTV не поддерживается: его частоты дискретизации (до 0,9 MS/s) меньше, "
                "чем нужно режимам (от 0,96 MS/s).",
}


def pe_info(path):
    """Разбор заголовка PE без загрузки DLL: разрядность и имена экспортируемых функций."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise DriverError(f"Не удалось прочитать {path}: {e}") from e
    try:
        if data[:2] != b"MZ":
            raise ValueError
        off = struct.unpack_from("<I", data, 0x3C)[0]
        if data[off:off + 4] != b"PE\0\0":
            raise ValueError
        machine, nsec, _t, _p, _n, opt_size, chars = struct.unpack_from("<HHIIIHH", data, off + 4)
        opt = off + 24
        magic = struct.unpack_from("<H", data, opt)[0]
        exp_rva = struct.unpack_from("<I", data, opt + (112 if magic == 0x20B else 96))[0]
        secs = [struct.unpack_from("<8xIIII", data, opt + opt_size + 40 * i) for i in range(nsec)]
    except (ValueError, struct.error):
        raise DriverError(f"{os.path.basename(path)} — не DLL Windows (нет заголовка PE)") from None

    def at(rva):
        for vsize, va, rsize, rptr in secs:
            if va <= rva < va + max(vsize, rsize):
                return rva - va + rptr
        raise DriverError(f"{os.path.basename(path)}: повреждена таблица экспорта")

    exports = set()
    if exp_rva:
        try:
            e = at(exp_rva)
            n_names, _funcs, names = struct.unpack_from("<III", data, e + 24)
            base = at(names)
            for i in range(min(n_names, 100000)):
                p = at(struct.unpack_from("<I", data, base + 4 * i)[0])
                exports.add(data[p:data.index(b"\0", p)].decode("latin-1"))
        except (struct.error, ValueError):
            pass
    return {"machine": MACHINES.get(machine, f"0x{machine:04x}"), "x64": machine == 0x8664,
            "dll": bool(chars & 0x2000), "exports": exports}


def classify(exports, name=""):
    """Вид DLL по экспорту: 'rtl'/'hackrf'/'airspy'/... или 'dep' (зависимость) или None."""
    for kind, (_title, marker, _need, _ok) in DRIVER_KINDS.items():
        if marker in exports:
            return kind
    if any(m in exports for m in DEP_MARKERS) or name.lower().startswith(DEP_NAMES):
        return "dep"
    return None


def kind_title(kind):
    if kind == "dep":
        return "зависимость драйвера"
    return DRIVER_KINDS[kind][0] if kind in DRIVER_KINDS else "неизвестная DLL"


def check_dll(path):
    """Проверить DLL до копирования. Возвращает (вид, pe_info) или бросает DriverError с понятной причиной."""
    name = os.path.basename(path)
    if not os.path.isfile(path):
        raise DriverError(f"Файл не найден: {path}")
    if not name.lower().endswith(".dll"):
        raise DriverError(f"{name}: нужен файл .dll")
    info = pe_info(path)
    if not info["dll"]:
        raise DriverError(f"{name} — это программа (EXE), а не библиотека DLL")
    if not info["x64"]:
        raise DriverError(f"{name} собрана для {info['machine']}, а RtlTV — 64-битное приложение. "
                          "Нужна 64-битная (x64) сборка этой DLL.")
    kind = classify(info["exports"], name)
    if kind is None:
        raise DriverError(f"{name} не похожа на драйвер SDR: в ней нет функций librtlsdr, libhackrf, "
                          "libairspy, SDRplay API и это не известная зависимость (libusb, pthreads).")
    if kind in DRIVER_KINDS:
        title, _m, need, ok = DRIVER_KINDS[kind]
        missing = [f for f in need if f not in info["exports"]]
        if missing:
            raise DriverError(f"{name} ({title}) — неполная или слишком старая версия: нет функций "
                              + ", ".join(missing[:5]))
        if not ok:
            raise DriverError(f"{name}: {title}. {UNSUPPORTED_HINT.get(kind, 'Не поддерживается.')}")
    return kind, info


_dll_dir_handles = {}


def ensure_dll_dir(path=None):
    """Добавить папку драйверов в поиск зависимых DLL (os.add_dll_directory)."""
    path = os.path.abspath(path or DRIVERS_DIR)
    if path in _dll_dir_handles or not os.path.isdir(path):
        return
    try:
        _dll_dir_handles[path] = os.add_dll_directory(path)
    except (AttributeError, OSError):
        _dll_dir_handles[path] = None
    os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")


def try_load(path):
    """Пробная загрузка DLL: None — успешно, иначе текст ошибки."""
    ensure_dll_dir(os.path.dirname(path))
    try:
        C.CDLL(os.path.abspath(path))
        return None
    except OSError as e:
        return (f"{e}. Скорее всего, рядом нет зависимостей драйвера (libusb-1.0.dll, pthreadVC2.dll) — "
                "загрузите их тоже.")


def import_dll(src, drivers_dir=None):
    """Скопировать DLL пользователя в папку драйверов и проверить, что она загружается.

    Возвращает {"kind", "title", "name", "path", "error"}; "error" — None или почему DLL не загружается
    (файл при этом остаётся: зависимости можно догрузить следующим шагом).
    """
    ddir = drivers_dir or DRIVERS_DIR
    kind, _info = check_dll(src)
    os.makedirs(ddir, exist_ok=True)
    name = os.path.basename(src)
    dst = os.path.join(ddir, name)
    if os.path.abspath(src).lower() != os.path.abspath(dst).lower():
        try:
            shutil.copy2(src, dst)
        except PermissionError as e:
            raise DriverError(f"{name} уже загружена приложением и занята — перезапустите RtlTV "
                              "и загрузите файл снова.") from e
        except OSError as e:
            raise DriverError(f"Не удалось скопировать {name} в {ddir}: {e}") from e
    err = try_load(dst) if kind in DRIVER_KINDS else None
    _scan_cache.clear()
    return {"kind": kind, "title": kind_title(kind), "name": name, "path": dst, "error": err}


_scan_cache = {}


def driver_dlls(drivers_dir=None):
    """DLL в папке драйверов: [{"name", "path", "kind", "title", "error"}]."""
    ddir = drivers_dir or DRIVERS_DIR
    out = []
    try:
        names = sorted(n for n in os.listdir(ddir) if n.lower().endswith(".dll"))
    except OSError:
        return out
    for n in names:
        p = os.path.join(ddir, n)
        try:
            key = (p, os.path.getmtime(p))
        except OSError:
            continue
        if key not in _scan_cache:
            try:
                kind, _info = check_dll(p)
                ent = {"kind": kind, "error": None}
            except DriverError as e:
                ent = {"kind": None, "error": str(e)}
            _scan_cache[key] = ent
        ent = dict(_scan_cache[key], name=n, path=p)
        ent["title"] = kind_title(ent["kind"])
        out.append(ent)
    return out


def find_driver(kind, drivers_dir=None):
    """Путь к DLL нужного вида в папке драйверов или None."""
    found = [d for d in driver_dlls(drivers_dir) if d["kind"] == kind]
    if not found:
        return None
    pref = PREFERRED_NAMES.get(kind, ())
    found.sort(key=lambda d: (d["name"].lower() not in pref, d["name"].lower()))
    return found[0]["path"]


# ======================================================================
#  Настройки: settings "app" -> "devices"
# ======================================================================
def load_dev_settings():
    d = settings.load("app", {}).get("devices") or {}
    return {"rtl_dll": d.get("rtl_dll"), "rtltcp": [str(x) for x in d.get("rtltcp") or []],
            "sources": d.get("sources") or []}


def save_dev_settings(d):
    app = settings.load("app", {})
    app["devices"] = d
    settings.save("app", app)


def rtl_dll_path(d=None):
    """Выбранная пользователем librtlsdr (путь) или None — встроенная."""
    d = d or load_dev_settings()
    name = d.get("rtl_dll")
    if not name:
        return None
    p = os.path.join(DRIVERS_DIR, os.path.basename(name))
    return p if os.path.isfile(p) else None


def apply_rtl_driver(d=None):
    path = rtl_dll_path(d)
    if path:
        ensure_dll_dir()
    rtlsdr.set_dll_override(path)
    return path


# ======================================================================
#  Потоковый пересчёт частоты дискретизации
# ======================================================================
class StreamResampler:
    """fs_in -> fs_out рациональным полифазным КИХ (scipy upfirdn) без швов между блоками."""

    def __init__(self, fs_in, fs_out, taps_per_phase=24):
        from scipy.signal import firwin
        fr = Fraction(int(round(fs_out)), int(round(fs_in))).limit_denominator(1000)
        self.up, self.down = fr.numerator, fr.denominator
        self.fs_in = float(fs_in)
        self.fs_out = self.fs_in * self.up / self.down
        self.K = int(taps_per_phase)
        L, M = self.up, self.down
        h = firwin(self.K * L, 0.9 / max(L, M), window=("kaiser", 8.0)) * L
        self.h = h.astype(np.float32)
        self.reset()

    def reset(self):
        pre = self.K - 1 + self.down
        self.buf = np.zeros(pre, np.complex64)
        self.buf_start = -pre   # абсолютный номер входного отсчёта buf[0]
        self.T = 0              # абсолютный индекс (в повышенной частоте) следующего выходного отсчёта

    def _first_needed(self):
        M = self.down
        return ((self.T // self.up - (self.K - 1)) // M) * M  # кратно M: выход ложится на сетку upfirdn

    def process(self, x):
        from scipy.signal import upfirdn
        L, M = self.up, self.down
        buf = np.concatenate((self.buf, np.asarray(x, np.complex64)))
        a = self._first_needed()
        xb = buf[a - self.buf_start:]
        skip = (self.T - a * L) // M
        last = (len(xb) * L - 1) // M
        if last < skip:
            self.buf, self.buf_start = xb, a
            return np.zeros(0, np.complex64)
        y = upfirdn(self.h, xb, L, M)[skip:last + 1]
        self.T += y.size * M
        a2 = self._first_needed()
        self.buf = buf[a2 - self.buf_start:].copy()
        self.buf_start = a2
        return y.astype(np.complex64, copy=False)


# ======================================================================
#  Усиление: общие десятые дБ -> родные ступени
# ======================================================================
HACKRF_GAINS = [g * 10 for g in range(0, 117, 2)]      # МШУ 0–40 (шаг 8) + ПЧ 0–62 (шаг 2) + усилитель 14 дБ
HACKRF_DEFAULT = (False, 24, 20)                         # у HackRF нет АРУ: «АРУ» = среднее усиление
AIRSPY_GAINS = [i * 20 for i in range(22)]               # ступени linearity 0–21, условно по 2 дБ


def hackrf_split(tenths):
    """(усилитель, МШУ дБ, ПЧ дБ) для суммарного усиления в десятых дБ; None — средние значения."""
    if tenths is None:
        return HACKRF_DEFAULT
    g = int(min(max(round(tenths / 10.0), 0), 116))
    amp = g > 102
    if amp:
        g -= 14
    lna = min(40, (g // 2) // 8 * 8)
    vga = min(62, (g - lna) // 2 * 2)
    if g - lna - vga > 0:
        lna = min(40, lna + 8 * math.ceil((g - lna - vga) / 8))
        vga = min(62, max(0, (g - lna) // 2 * 2))
    return amp, lna, vga


def airspy_step(tenths):
    return int(min(max(round(tenths / 20.0), 0), 21))


# ======================================================================
#  rtl_tcp (сеть)
# ======================================================================
RTLTCP_GAINS = {
    1: [-10, 15, 40, 65, 90, 115, 140, 165, 190, 215, 240, 290, 340, 420],
    2: [-99, -40, 71, 179, 192],
    3: [-99, -73, -65, -63, -60, -58, -54, 58, 61, 63, 65, 67, 68, 70, 71, 179, 181, 182, 184, 186, 188, 191, 197],
    4: [0],
    5: R828D_GAINS,
    6: R828D_GAINS,
}
CMD_FREQ, CMD_RATE, CMD_GAIN_MODE, CMD_GAIN, CMD_PPM, CMD_AGC, CMD_BIAS = 0x01, 0x02, 0x03, 0x04, 0x05, 0x08, 0x0E


def parse_hostport(text, default_port=1234):
    text = str(text).strip()
    if text.startswith("["):                       # [::1]:1234
        host, _, rest = text[1:].partition("]")
        port = rest.lstrip(":") or default_port
    elif text.count(":") == 1:
        host, port = text.split(":")
    else:
        host, port = text, default_port
    try:
        port = int(port)
    except ValueError:
        raise DeviceError(f"Неверный порт в «{text}»") from None
    if not host or not 0 < port < 65536:
        raise DeviceError(f"Нужен адрес вида хост:порт, например 192.168.1.10:1234 (получено «{text}»)")
    return host, port


def _rtltcp_connect(host, port, timeout):
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        raise DeviceError(f"rtl_tcp: не удалось открыть соединение с {host}:{port}: {e}") from e
    try:
        head = b""
        while len(head) < 12:
            part = s.recv(12 - len(head))
            if not part:
                raise DeviceError(f"rtl_tcp {host}:{port}: сервер закрыл соединение (занят другим клиентом?)")
            head += part
    except socket.timeout:
        s.close()
        raise DeviceError(f"rtl_tcp {host}:{port}: нет ответа — не удалось открыть поток (это точно rtl_tcp?)") \
            from None
    except DeviceError:
        s.close()
        raise
    if head[:4] != b"RTL0":
        s.close()
        raise DeviceError(f"{host}:{port} — не сервер rtl_tcp: не удалось открыть поток (заголовок {head[:4]!r})")
    tuner, ngains = struct.unpack(">II", head[4:12])
    return s, tuner, ngains


def probe_rtltcp(host, port, timeout=3.0):
    """Проверка сервера: {"tuner": имя, "gains": число ступеней}. Бросает DeviceError."""
    s, tuner, ngains = _rtltcp_connect(host, port, timeout)
    s.close()
    return {"tuner": rtlsdr.TUNER_NAMES.get(tuner, f"тип {tuner}"), "gains": ngains}


class RtlTcpSource(SourceBase):
    """Клиент rtl_tcp: команды по 5 байт (код + uint32 BE), поток u8 IQ как у донгла."""
    HARDWARE = True
    TIMEOUT = 5.0

    def __init__(self, sink, fs, freq, host="127.0.0.1", port=1234):
        super().__init__(sink, fs, freq)
        self.host, self.port = host, int(port)
        self.sock = None
        self._buf = bytearray(BLOCK_BYTES)
        self._odd = 0
        self._tuned = None

    def _send(self, cmd, value):
        self.sock.sendall(struct.pack(">BI", cmd, int(value) & 0xFFFFFFFF))

    def open(self):
        self.sock, tuner, ngains = _rtltcp_connect(self.host, self.port, self.TIMEOUT)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        self.sock.settimeout(self.TIMEOUT)
        self.gains = list(RTLTCP_GAINS.get(tuner) or R828D_GAINS)
        self._send(CMD_RATE, self.fs)
        self._tune(self.freq)
        self._set_gain(self.gain)
        self.sink("opened", {"label": f"rtl_tcp {self.host}:{self.port}",
                             "tuner": rtlsdr.TUNER_NAMES.get(tuner, f"тип {tuner}"), "gains": self.gains,
                             "driver": "сеть: rtl_tcp"})

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _set_gain(self, tenths):
        if tenths is None:
            self._send(CMD_GAIN_MODE, 0)
        else:
            self._send(CMD_GAIN_MODE, 1)
            self._send(CMD_GAIN, int(tenths))

    def _tune(self, hz):
        self._send(CMD_FREQ, int(hz))
        self._tuned = int(hz)

    def apply(self, name, value):
        if name == "freq":
            self.freq = int(value)
            self._tune(self.freq)
        elif name == "rate":
            self.fs = int(value)
            self._send(CMD_RATE, self.fs)
        elif name == "gain":
            self.gain = value
            self._set_gain(value)
        elif name == "rtl_agc":
            self._send(CMD_AGC, 1 if value else 0)
        elif name == "ppm":
            self._send(CMD_PPM, int(value))
        elif name == "bias":
            self._send(CMD_BIAS, 1 if value else 0)
        else:
            return
        self._drop_pending()

    def _drop_pending(self):
        """Выбросить уже пришедшие (старые) отсчёты, сохранив чётность I/Q."""
        self.sock.setblocking(False)
        try:
            for _ in range(64):
                n = self.sock.recv_into(self._buf)
                if not n:
                    break
                self._odd ^= n & 1
        except (BlockingIOError, InterruptedError):
            pass
        finally:
            self.sock.settimeout(self.TIMEOUT)
        if self._odd:
            self._recv(1)

    def _recv(self, nbytes):
        if len(self._buf) < nbytes:
            self._buf = bytearray(nbytes)
        mv = memoryview(self._buf)[:nbytes]
        got = 0
        while got < nbytes:
            try:
                n = self.sock.recv_into(mv[got:])
            except socket.timeout:
                raise DeviceError(f"rtl_tcp {self.host}:{self.port}: нет данных {self.TIMEOUT:.0f} с") from None
            if not n:
                raise DeviceError(f"rtl_tcp {self.host}:{self.port}: сервер закрыл соединение")
            got += n
        self._odd ^= nbytes & 1
        return bytes(mv)

    def read_at(self, freq, nsamples, settle):
        if self._tuned != int(freq):
            self._tune(int(freq))
        self._drop_pending()
        if settle:
            self._recv(32768)
        return rtlsdr.u8_to_iq(self._recv(2 * int(nsamples)))

    def read_stream(self):
        if self._tuned != self.freq:
            self._tune(self.freq)
            self._drop_pending()
        return rtlsdr.u8_to_iq(self._recv(BLOCK_BYTES))


# ======================================================================
#  Устройства с потоком через callback (HackRF, Airspy)
# ======================================================================
class _CallbackSource(SourceBase):
    """Общее для драйверов, которые сами зовут callback со свежими отсчётами в своём потоке."""
    HARDWARE = True
    MAX_QUEUE_S = 1.0
    SETTLE_S = 0.012

    def __init__(self, sink, fs, freq, index=0, lib_path=None):
        super().__init__(sink, fs, freq)
        self.index = int(index)
        self.lib_path = lib_path
        self.L = None
        self.dev = C.c_void_p()
        self._q = collections.deque()
        self._qn = 0
        self._qlock = threading.Lock()
        self._ev = threading.Event()
        self._pend = np.zeros(0, np.complex64)
        self._skip = 0
        self.native_fs = None
        self.rs = None
        self.ppm = 0
        self.drops = 0
        self._tuned = None
        self._streaming = False

    # --- вызывается из потока драйвера ---
    def _push(self, arr):
        with self._qlock:
            self._q.append(arr)
            self._qn += arr.size
            lim = int((self.native_fs or 1e7) * self.MAX_QUEUE_S)
            while self._qn > lim and len(self._q) > 1:
                self._qn -= self._q.popleft().size
                self.drops += 1
        self._ev.set()

    # --- поток источника ---
    def _flush(self):
        with self._qlock:
            self._q.clear()
            self._qn = 0
        self._pend = np.zeros(0, np.complex64)
        if self.rs is not None:
            self.rs.reset()
        self._skip = int((self.native_fs or 0) * self.SETTLE_S)

    def _pull(self):
        waited = 0.0
        while True:
            with self._qlock:
                if self._q:
                    a = self._q.popleft()
                    self._qn -= a.size
                    return a
                self._ev.clear()
            if not self.running:
                return None
            if not self._ev.wait(0.2):
                waited += 0.2
                if waited >= 3.0:
                    raise DeviceError(f"{self.NAME}: устройство не отдаёт данные (3 с) — USB отключён?")

    def _read(self, n):
        parts, have = [self._pend], self._pend.size
        while have < n:
            a = self._pull()
            if a is None:
                break
            if self._skip:
                k = min(self._skip, a.size)
                self._skip -= k
                a = a[k:]
            if self.rs is not None:
                a = self.rs.process(a)
            parts.append(a)
            have += a.size
        cat = np.concatenate(parts) if len(parts) > 1 else parts[0]
        self._pend = cat[n:].copy()
        return cat[:n]

    def _set_rate(self, fs):
        self.fs = int(fs)
        native = int(self._choose_native(self.fs))
        self._dev_set_rate(native)
        self.native_fs = native
        self.rs = None if native == self.fs else StreamResampler(native, self.fs)
        self._flush()

    def _tune(self, hz):
        self._dev_set_freq(int(round(hz / (1.0 + self.ppm * 1e-6))))
        self._tuned = int(hz)
        self._flush()

    def _rx_start(self):
        if not self._streaming:
            self._dev_start()
            self._streaming = True

    def _rx_stop(self):
        if self._streaming:
            self._streaming = False
            self._dev_stop()

    def open(self):
        self._dev_open()
        self._set_rate(self.fs)
        self._tune(self.freq)
        self._dev_set_gain(self.gain)
        self._rx_start()
        info = {"label": self.label, "tuner": self.tuner, "gains": self.gains,
                "driver": f"{os.path.basename(self.lib_path or '')}",
                "native_fs": self.native_fs}
        if self.rs is not None:
            info["note"] = f"поток {self.native_fs / 1e6:g} MS/s пересчитывается в {self.fs / 1e6:g} MS/s"
        self.sink("opened", info)

    def close(self):
        try:
            if self.dev:
                self._rx_stop()
        except Exception:  # noqa: BLE001
            pass
        finally:
            if self.dev:
                self._dev_close()
                self.dev = C.c_void_p()

    def apply(self, name, value):
        if name == "freq":
            self.freq = int(value)
            self._tune(self.freq)
        elif name == "rate":
            self._rx_stop()
            self._set_rate(int(value))
            self._rx_start()
        elif name == "gain":
            self.gain = value
            self._dev_set_gain(value)
        elif name == "ppm":
            self.ppm = int(value)
            self._tune(self.freq)
        elif name == "bias":
            self._dev_set_bias(bool(value))

    def read_at(self, freq, nsamples, settle):
        if self._tuned != int(freq):
            self._tune(int(freq))
        else:
            self._flush()
            if not settle:
                self._skip = 0
        return self._read(int(nsamples))

    def read_stream(self):
        if self._tuned != self.freq:
            self._tune(self.freq)
        return self._read(BLOCK)


# ---------------------------------------------------------------- HackRF
class HackRfTransfer(C.Structure):
    _fields_ = [("device", C.c_void_p), ("buffer", C.POINTER(C.c_ubyte)), ("buffer_length", C.c_int),
                ("valid_length", C.c_int), ("rx_ctx", C.c_void_p), ("tx_ctx", C.c_void_p)]


class HackRfDeviceList(C.Structure):
    _fields_ = [("serial_numbers", C.POINTER(C.c_char_p)), ("usb_board_ids", C.POINTER(C.c_int)),
                ("usb_device_index", C.POINTER(C.c_int)), ("devicecount", C.c_int),
                ("usb_devices", C.POINTER(C.c_void_p)), ("usb_devicecount", C.c_int)]


HACKRF_CB = C.CFUNCTYPE(C.c_int, C.POINTER(HackRfTransfer))
AIRSPY_SAMPLE_FLOAT32_IQ = 0

_backend_libs = {}
_backend_lock = threading.Lock()


def _missing_dll(kind):
    title = DRIVER_KINDS[kind][0]
    names = " или ".join(PREFERRED_NAMES[kind])
    deps = "libusb-1.0.dll и pthreadVC2.dll"
    return DriverError(f"Драйвер {title} не загружен: нет {names} в {DRIVERS_DIR}. Откройте «Режим → "
                       f"Устройства…» → «Загрузить драйвер (DLL)…» и выберите {names} (вместе с {deps}).")


def _open_backend(kind, path, setup):
    path = path or find_driver(kind)
    if not path:
        raise _missing_dll(kind)
    path = os.path.abspath(path)
    with _backend_lock:
        if path in _backend_libs:
            return _backend_libs[path], path
        ensure_dll_dir(os.path.dirname(path))
        try:
            L = C.CDLL(path)
        except OSError as e:
            raise DriverError(f"{os.path.basename(path)} не загрузилась: {e}. Нужна 64-битная DLL и её "
                              "зависимости (libusb-1.0.dll, pthreadVC2.dll) в той же папке.") from e
        need = DRIVER_KINDS[kind][2]
        missing = [f for f in need if not hasattr(L, f)]
        if missing:
            raise DriverError(f"{os.path.basename(path)}: нет функций {', '.join(missing[:5])}")
        setup(L)
        _backend_libs[path] = L
        return L, path


def _setup_hackrf(L):
    vp, i32, u32, u8 = C.c_void_p, C.c_int, C.c_uint32, C.c_uint8
    sigs = {
        "hackrf_init": ([], i32), "hackrf_device_list": ([], C.POINTER(HackRfDeviceList)),
        "hackrf_device_list_open": ([C.POINTER(HackRfDeviceList), i32, C.POINTER(vp)], i32),
        "hackrf_device_list_free": ([C.POINTER(HackRfDeviceList)], None),
        "hackrf_close": ([vp], i32), "hackrf_set_freq": ([vp, C.c_uint64], i32),
        "hackrf_set_sample_rate": ([vp, C.c_double], i32), "hackrf_set_lna_gain": ([vp, u32], i32),
        "hackrf_set_vga_gain": ([vp, u32], i32), "hackrf_set_amp_enable": ([vp, u8], i32),
        "hackrf_start_rx": ([vp, HACKRF_CB, vp], i32), "hackrf_stop_rx": ([vp], i32),
    }
    opt = {"hackrf_set_antenna_enable": ([vp, u8], i32), "hackrf_error_name": ([i32], C.c_char_p),
           "hackrf_compute_baseband_filter_bw": ([u32], u32),
           "hackrf_set_baseband_filter_bandwidth": ([vp, u32], i32),
           "hackrf_board_id_read": ([vp, C.POINTER(u8)], i32), "hackrf_board_id_name": ([i32], C.c_char_p)}
    for table, required in ((sigs, True), (opt, False)):
        for name, (args, res) in table.items():
            if required or hasattr(L, name):
                f = getattr(L, name)
                f.argtypes, f.restype = args, res
    r = L.hackrf_init()
    if r != 0:
        raise DriverError(f"hackrf_init: ошибка {r}")


def _setup_airspy(L):
    vp, i32, u32, u8 = C.c_void_p, C.c_int, C.c_uint32, C.c_uint8
    sigs = {
        "airspy_list_devices": ([C.POINTER(C.c_uint64), i32], i32),
        "airspy_open_sn": ([C.POINTER(vp), C.c_uint64], i32), "airspy_close": ([vp], i32),
        "airspy_get_samplerates": ([vp, C.POINTER(u32), u32], i32), "airspy_set_samplerate": ([vp, u32], i32),
        "airspy_set_sample_type": ([vp, i32], i32), "airspy_set_freq": ([vp, u32], i32),
        "airspy_set_linearity_gain": ([vp, u8], i32), "airspy_set_lna_agc": ([vp, u8], i32),
        "airspy_set_mixer_agc": ([vp, u8], i32), "airspy_start_rx": ([vp, vp, vp], i32),
        "airspy_stop_rx": ([vp], i32),
    }
    opt = {"airspy_init": ([], i32), "airspy_set_rf_bias": ([vp, u8], i32),
           "airspy_error_name": ([i32], C.c_char_p), "airspy_set_vga_gain": ([vp, u8], i32)}
    for table, required in ((sigs, True), (opt, False)):
        for name, (args, res) in table.items():
            if required or hasattr(L, name):
                f = getattr(L, name)
                f.argtypes, f.restype = args, res
    if hasattr(L, "airspy_init"):
        L.airspy_init()


def load_hackrf(path=None):
    return _open_backend("hackrf", path, _setup_hackrf)[0]


def load_airspy(path=None):
    return _open_backend("airspy", path, _setup_airspy)[0]


def list_hackrf(path=None):
    L = load_hackrf(path)
    lst = L.hackrf_device_list()
    if not lst:
        return []
    try:
        d = lst.contents
        out = []
        for i in range(d.devicecount):
            sn = d.serial_numbers[i] if d.serial_numbers else None
            sn = sn.decode("latin-1", "replace").lstrip("0") if sn else ""
            out.append(f"HackRF #{i}" + (f" (s/n …{sn[-8:]})" if sn else ""))
        return out
    finally:
        L.hackrf_device_list_free(lst)


def list_airspy(path=None):
    L = load_airspy(path)
    n = L.airspy_list_devices(None, 0)
    if n <= 0:
        return []
    arr = (C.c_uint64 * n)()
    n = L.airspy_list_devices(arr, n)
    return [f"Airspy #{i} (s/n {arr[i]:016X})" for i in range(max(n, 0))]


class HackRfSource(_CallbackSource):
    NAME = "HackRF"
    MIN_FS, MAX_FS = 2_000_000, 20_000_000

    def _err(self, r, what):
        if r != 0:
            name = self.L.hackrf_error_name(r).decode() if hasattr(self.L, "hackrf_error_name") else str(r)
            raise DeviceError(f"HackRF, {what}: {name} ({r})")

    def _choose_native(self, fs):
        if fs >= self.MIN_FS:
            return min(fs, self.MAX_FS)
        return fs * math.ceil(self.MIN_FS / fs)

    def _dev_open(self):
        self.L, self.lib_path = _open_backend("hackrf", self.lib_path, _setup_hackrf)
        L = self.L
        lst = L.hackrf_device_list()
        count = lst.contents.devicecount if lst else 0
        try:
            if self.index >= count:
                raise DeviceError(f"Не удалось открыть HackRF #{self.index}: подключено устройств — {count}. "
                                  "Проверьте кабель и драйвер WinUSB («Режим → Устройства…»).")
            r = L.hackrf_device_list_open(lst, self.index, C.byref(self.dev))
            if r != 0:
                raise DeviceError(f"Не удалось открыть HackRF #{self.index} (код {r}): устройство занято "
                                  "другой программой или не привязан WinUSB.")
        finally:
            if lst:
                L.hackrf_device_list_free(lst)
        self.label, self.tuner = "HackRF", "MAX2837/MAX2839"
        if hasattr(L, "hackrf_board_id_read") and hasattr(L, "hackrf_board_id_name"):
            bid = C.c_uint8(0)
            if L.hackrf_board_id_read(self.dev, C.byref(bid)) == 0:
                nm = L.hackrf_board_id_name(bid.value)
                if nm:
                    self.label = nm.decode("latin-1", "replace")
        self.gains = HACKRF_GAINS

    def _dev_close(self):
        self.L.hackrf_close(self.dev)

    def _dev_set_rate(self, native):
        self._err(self.L.hackrf_set_sample_rate(self.dev, float(native)), "частота дискретизации")
        if hasattr(self.L, "hackrf_set_baseband_filter_bandwidth") and \
                hasattr(self.L, "hackrf_compute_baseband_filter_bw"):
            bw = self.L.hackrf_compute_baseband_filter_bw(int(native * 0.75))
            self.L.hackrf_set_baseband_filter_bandwidth(self.dev, bw)

    def _dev_set_freq(self, hz):
        if not 1_000_000 <= hz <= 6_000_000_000:
            raise DeviceError(f"HackRF: частота {hz / 1e6:.3f} МГц вне диапазона 1–6000 МГц")
        self._err(self.L.hackrf_set_freq(self.dev, int(hz)), "частота")

    def _dev_set_gain(self, tenths):
        amp, lna, vga = hackrf_split(tenths)
        self._err(self.L.hackrf_set_amp_enable(self.dev, 1 if amp else 0), "усилитель")
        self._err(self.L.hackrf_set_lna_gain(self.dev, lna), "усиление МШУ")
        self._err(self.L.hackrf_set_vga_gain(self.dev, vga), "усиление ПЧ")

    def _dev_set_bias(self, on):
        if hasattr(self.L, "hackrf_set_antenna_enable"):
            self._err(self.L.hackrf_set_antenna_enable(self.dev, 1 if on else 0), "питание антенны")

    def _on_rx(self, tp):
        try:
            t = tp.contents
            n = t.valid_length & ~1
            if n > 0:
                raw = C.string_at(t.buffer, n)
                a = np.frombuffer(raw, np.int8).astype(np.float32)
                a *= 1.0 / 128.0
                self._push(a.view(np.complex64))
        except Exception:  # noqa: BLE001 — исключение в callback драйвера нельзя пропускать наружу
            pass
        return 0

    def _dev_start(self):
        self._cb = HACKRF_CB(self._on_rx)
        self._err(self.L.hackrf_start_rx(self.dev, self._cb, None), "запуск приёма")

    def _dev_stop(self):
        self.L.hackrf_stop_rx(self.dev)


# ---------------------------------------------------------------- Airspy
class AirspyTransfer(C.Structure):
    _fields_ = [("device", C.c_void_p), ("ctx", C.c_void_p), ("samples", C.c_void_p), ("sample_count", C.c_int),
                ("dropped_samples", C.c_uint64), ("sample_type", C.c_int)]


AIRSPY_CB = C.CFUNCTYPE(C.c_int, C.POINTER(AirspyTransfer))


class AirspySource(_CallbackSource):
    NAME = "Airspy"

    def _err(self, r, what):
        if r != 0:
            name = self.L.airspy_error_name(r).decode() if hasattr(self.L, "airspy_error_name") else str(r)
            raise DeviceError(f"Airspy, {what}: {name} ({r})")

    def _choose_native(self, fs):
        rates = sorted(self.rates) or [fs]
        up = [r for r in rates if r >= fs]
        return up[0] if up else rates[-1]

    def _dev_open(self):
        self.L, self.lib_path = _open_backend("airspy", self.lib_path, _setup_airspy)
        L = self.L
        n = L.airspy_list_devices(None, 0)
        if self.index >= max(n, 0):
            raise DeviceError(f"Не удалось открыть Airspy #{self.index}: подключено устройств — {max(n, 0)}.")
        arr = (C.c_uint64 * n)()
        L.airspy_list_devices(arr, n)
        r = L.airspy_open_sn(C.byref(self.dev), arr[self.index])
        if r != 0:
            raise DeviceError(f"Не удалось открыть Airspy #{self.index} (код {r}): устройство занято "
                              "другой программой или не привязан WinUSB.")
        self._err(L.airspy_set_sample_type(self.dev, AIRSPY_SAMPLE_FLOAT32_IQ), "формат отсчётов")
        cnt = C.c_uint32(0)
        L.airspy_get_samplerates(self.dev, C.byref(cnt), 0)
        buf = (C.c_uint32 * max(cnt.value, 1))()
        if cnt.value:
            L.airspy_get_samplerates(self.dev, buf, cnt.value)
        self.rates = [int(buf[i]) for i in range(cnt.value)] or [2_500_000, 10_000_000]
        self.label, self.tuner = f"Airspy (s/n {arr[self.index]:016X})", "R820T2"
        self.gains = AIRSPY_GAINS

    def _dev_close(self):
        self.L.airspy_close(self.dev)

    def _dev_set_rate(self, native):
        self._err(self.L.airspy_set_samplerate(self.dev, int(native)), "частота дискретизации")

    def _dev_set_freq(self, hz):
        if not 24_000_000 <= hz <= 1_800_000_000:
            raise DeviceError(f"Airspy: частота {hz / 1e6:.3f} МГц вне диапазона 24–1800 МГц")
        self._err(self.L.airspy_set_freq(self.dev, int(hz)), "частота")

    def _dev_set_gain(self, tenths):
        if tenths is None:
            self._err(self.L.airspy_set_lna_agc(self.dev, 1), "АРУ")
            self._err(self.L.airspy_set_mixer_agc(self.dev, 1), "АРУ смесителя")
        else:
            self.L.airspy_set_lna_agc(self.dev, 0)
            self.L.airspy_set_mixer_agc(self.dev, 0)
            self._err(self.L.airspy_set_linearity_gain(self.dev, airspy_step(tenths)), "усиление")

    def _dev_set_bias(self, on):
        if hasattr(self.L, "airspy_set_rf_bias"):
            self._err(self.L.airspy_set_rf_bias(self.dev, 1 if on else 0), "питание антенны")

    def _on_rx(self, tp):
        try:
            t = tp.contents
            if t.sample_count > 0 and t.samples:
                raw = C.string_at(t.samples, t.sample_count * 8)
                self._push(np.frombuffer(raw, np.complex64))
        except Exception:  # noqa: BLE001
            pass
        return 0

    def _dev_start(self):
        self._cb = AIRSPY_CB(self._on_rx)
        self._err(self.L.airspy_start_rx(self.dev, C.cast(self._cb, C.c_void_p), None), "запуск приёма")

    def _dev_stop(self):
        self.L.airspy_stop_rx(self.dev)


class _FailSource(SourceBase):
    """Источник, который сразу сообщает об ошибке (неподдерживаемое устройство) — тем же путём, что и обычные."""

    def __init__(self, sink, fs, freq, message):
        super().__init__(sink, fs, freq)
        self.message = message

    def open(self):
        raise DeviceError(self.message)

    def close(self):
        pass


# ======================================================================
#  USB: что подключено и какой драйвер Windows привязан
# ======================================================================
KNOWN_USB = [
    # (vid, pid или None = любой, модель, вид бэкенда)
    (0x0BDA, 0x2838, "RTL-SDR (RTL2832U)", "rtl"),
    (0x0BDA, 0x2832, "RTL-SDR (RTL2832U)", "rtl"),
    (0x1D50, 0x6089, "HackRF One", "hackrf"),
    (0x1D50, 0x604B, "HackRF Jawbreaker", "hackrf"),
    (0x1D50, 0xCC15, "rad1o (HackRF)", "hackrf"),
    (0x1D50, 0x60A1, "Airspy R2 / Mini", "airspy"),
    (0x03EB, 0x800C, "Airspy HF+", "airspyhf"),
    (0x1DF7, None, "SDRplay RSP", "sdrplay"),
    (0x0403, 0x601F, "LimeSDR Mini", "lime"),
    (0x1D50, 0x6108, "LimeSDR-USB", "lime"),
    (0x0456, 0xB673, "ADALM-Pluto (PlutoSDR)", "pluto"),
    (0x04D8, 0xFB56, "FUNcube Dongle Pro", "funcube"),
    (0x04D8, 0xFB31, "FUNcube Dongle Pro+", "funcube"),
]


def known_model(vid, pid):
    for v, p, name, kind in KNOWN_USB:
        if v == vid and (p is None or p == pid):
            return name, kind
    return None


def _parse_ids(inst):
    up = inst.upper()
    try:
        vid = int(up.split("VID_")[1][:4], 16)
        pid = int(up.split("PID_")[1][:4], 16)
    except (IndexError, ValueError):
        return None
    mi = None
    if "&MI_" in up:
        try:
            mi = int(up.split("&MI_")[1][:2], 16)
        except ValueError:
            mi = None
    return vid, pid, mi


def _usb_raw_setupapi():
    """[(instance_id, parent_id, service, description)] для присутствующих USB-устройств (SetupAPI, без админа)."""
    from ctypes import wintypes as W
    sa = C.WinDLL("setupapi", use_last_error=True)
    cm = C.WinDLL("cfgmgr32")

    class DevInfo(C.Structure):
        _fields_ = [("cbSize", W.DWORD), ("ClassGuid", C.c_byte * 16), ("DevInst", W.DWORD),
                    ("Reserved", C.c_void_p)]

    sa.SetupDiGetClassDevsW.argtypes = [C.c_void_p, W.LPCWSTR, W.HWND, W.DWORD]
    sa.SetupDiGetClassDevsW.restype = C.c_void_p
    sa.SetupDiEnumDeviceInfo.argtypes = [C.c_void_p, W.DWORD, C.POINTER(DevInfo)]
    sa.SetupDiEnumDeviceInfo.restype = W.BOOL
    sa.SetupDiGetDeviceInstanceIdW.argtypes = [C.c_void_p, C.POINTER(DevInfo), W.LPWSTR, W.DWORD,
                                               C.POINTER(W.DWORD)]
    sa.SetupDiGetDeviceInstanceIdW.restype = W.BOOL
    sa.SetupDiGetDeviceRegistryPropertyW.argtypes = [C.c_void_p, C.POINTER(DevInfo), W.DWORD, C.POINTER(W.DWORD),
                                                     C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
    sa.SetupDiGetDeviceRegistryPropertyW.restype = W.BOOL
    sa.SetupDiDestroyDeviceInfoList.argtypes = [C.c_void_p]
    cm.CM_Get_Parent.argtypes = [C.POINTER(W.DWORD), W.DWORD, W.ULONG]
    cm.CM_Get_Device_IDW.argtypes = [W.DWORD, W.LPWSTR, W.ULONG, W.ULONG]

    h = sa.SetupDiGetClassDevsW(None, "USB", None, 0x2 | 0x4)  # DIGCF_PRESENT | DIGCF_ALLCLASSES
    if not h or h == C.c_void_p(-1).value:
        raise OSError("SetupDiGetClassDevs не сработала")

    def prop(di, code):
        buf = C.create_unicode_buffer(512)
        if sa.SetupDiGetDeviceRegistryPropertyW(h, C.byref(di), code, None, buf, C.sizeof(buf), None):
            return buf.value
        return ""

    out = []
    try:
        i = 0
        while True:
            di = DevInfo()
            di.cbSize = C.sizeof(DevInfo)
            if not sa.SetupDiEnumDeviceInfo(h, i, C.byref(di)):
                break
            i += 1
            buf = C.create_unicode_buffer(512)
            if not sa.SetupDiGetDeviceInstanceIdW(h, C.byref(di), buf, 512, None):
                continue
            inst = buf.value
            if "VID_" not in inst.upper():
                continue
            parent = ""
            pdev = W.DWORD(0)
            if cm.CM_Get_Parent(C.byref(pdev), di.DevInst, 0) == 0:
                pb = C.create_unicode_buffer(512)
                if cm.CM_Get_Device_IDW(pdev.value, pb, 512, 0) == 0:
                    parent = pb.value
            out.append((inst, parent, prop(di, 4), prop(di, 0x0C) or prop(di, 0)))  # SERVICE, FRIENDLYNAME/DESC
    finally:
        sa.SetupDiDestroyDeviceInfoList(h)
    return out


def _driver_state(kind, svc):
    """(готово ли, текст состояния, что делать) для вида устройства и службы интерфейса 0."""
    s = (svc or "").lower()
    zadig = f"Привяжите WinUSB через Zadig ({ZADIG_URL}): Options → List All Devices → нужное устройство → WinUSB."
    if kind == "rtl":
        if s == "winusb":
            return True, "WinUSB", "Готово: работает со встроенным драйвером RtlTV."
        if not s:
            return False, "драйвер не установлен", zadig + " Для RTL-SDR выберите «Bulk-In, Interface (Interface 0)»."
        return False, f"{svc} (драйвер DVB-T/другой программы)", \
            zadig + " Для RTL-SDR выберите «Bulk-In, Interface (Interface 0)»."
    if kind in ("hackrf", "airspy"):
        dll = find_driver(kind)
        need_dll = "" if dll else (f" Загрузите {PREFERRED_NAMES[kind][0]} кнопкой «Загрузить драйвер (DLL)…» "
                                   "(вместе с libusb-1.0.dll и pthreadVC2.dll).")
        if s == "winusb":
            return bool(dll), "WinUSB", ("Готово." if dll else "Драйвер Windows в порядке." + need_dll)
        return False, svc or "драйвер не установлен", zadig + need_dll
    if kind == "sdrplay":
        return False, svc or "драйвер не установлен", UNSUPPORTED_HINT["sdrplay"]
    if kind == "airspyhf":
        return False, svc or "драйвер не установлен", UNSUPPORTED_HINT["airspyhf"]
    if kind == "lime":
        return False, svc or "драйвер не установлен", "LimeSDR напрямую в RtlTV не поддерживается."
    if kind == "pluto":
        return False, svc or "драйвер не установлен", "PlutoSDR (libiio) в RtlTV не поддерживается."
    if kind == "funcube":
        return False, svc or "USB-аудио", "FUNcube Dongle работает как звуковая карта — в RtlTV не поддерживается."
    return False, svc or "—", ""


def usb_devices(raw=None):
    """Найденные SDR на USB: [{"vid", "pid", "model", "kind", "service", "services", "ready", "state", "hint"}]."""
    if raw is None:
        try:
            raw = _usb_raw_setupapi()
        except (OSError, AttributeError):
            return []
    groups = {}
    for inst, parent, svc, desc in raw:
        ids = _parse_ids(inst)
        if not ids:
            continue
        vid, pid, mi = ids
        km = known_model(vid, pid)
        if not km:
            continue
        key = parent.upper() if mi is not None and parent else inst.upper()
        g = groups.setdefault(key, {"vid": vid, "pid": pid, "model": km[0], "kind": km[1], "ifaces": {},
                                    "main": None, "instance": key})
        if mi is None:
            g["main"] = (svc, desc)
        else:
            g["ifaces"][mi] = (svc, desc)
    out = []
    for g in groups.values():
        main_svc = g["main"][0] if g["main"] else ""
        svc0 = g["ifaces"].get(0, (main_svc, ""))[0] if g["ifaces"] else main_svc
        ready, state, hint = _driver_state(g["kind"], svc0)
        services = sorted({s for s, _d in list(g["ifaces"].values()) + ([g["main"]] if g["main"] else []) if s})
        out.append({"vid": g["vid"], "pid": g["pid"], "model": g["model"], "kind": g["kind"], "service": svc0,
                    "services": services, "ready": ready, "state": state, "hint": hint,
                    "instance": g["instance"]})
    out.sort(key=lambda d: (d["kind"], d["instance"]))
    return out


# ======================================================================
#  Список источников и фабрика
# ======================================================================
TEST_LABEL = "Тестовый генератор (без антенны)"


def available_sources(test_label=TEST_LABEL, include_test=True):
    """[(идентификатор, подпись)] для комбобоксов «Источник». Ошибки драйверов — строками с id "none"."""
    d = load_dev_settings()
    apply_rtl_driver(d)
    out, errors = [], []
    try:
        for i in range(rtlsdr.device_count()):
            out.append((f"rtl:{i}", f"RTL-SDR #{i}: {rtlsdr.device_label(i)}"))
    except Exception as e:  # noqa: BLE001
        errors.append(("none", f"Драйвер RTL-SDR: {e}"))
    for kind, lister in (("hackrf", list_hackrf), ("airspy", list_airspy)):
        if not find_driver(kind):
            continue
        try:
            for i, label in enumerate(lister()):
                out.append((f"{kind}:{i}", label))
        except Exception as e:  # noqa: BLE001
            errors.append(("none", f"{DRIVER_KINDS[kind][0]}: {e}"))
    for hp in d["rtltcp"]:
        out.append((f"rtltcp:{hp}", f"rtl_tcp {hp} (сеть)"))
    out += errors[:1] if out else errors
    if include_test:
        out.append(("test", test_label))
    return out


def describe(sid):
    kind, _, rest = str(sid).partition(":")
    return {"rtl": f"RTL-SDR #{rest}", "rtltcp": f"rtl_tcp {rest}", "hackrf": f"HackRF #{rest}",
            "airspy": f"Airspy #{rest}", "sdrplay": f"SDRplay #{rest}"}.get(kind, str(sid))


def fill_source_combo(combo, current, test_label=TEST_LABEL, keep_missing=True, include_test=True):
    """Заполнить QComboBox источниками и выбрать current (если его нет — добавить строку «(не найден)»)."""
    combo.clear()
    for sid, label in available_sources(test_label, include_test):
        combo.addItem(label, sid)
    idx = combo.findData(current)
    if idx < 0 and keep_missing and current and ":" in str(current):
        combo.insertItem(0, f"{describe(current)} (не найден)", current)
        idx = 0
    combo.setCurrentIndex(max(idx, 0))


def source_id(kind, index=0):
    """Совместимость со старым вызовом start(kind, index): ("rtl", 0) -> "rtl:0"; полный id — как есть."""
    kind = str(kind)
    if ":" in kind or kind in ("test", "none"):
        return kind
    return f"{kind}:{int(index or 0)}"


def make_source(sid, sink, fs, freq, **test_kw):
    """Источник по идентификатору. test_kw (std_key, generator) передаются только тестовому генератору.

    Ошибки открытия приходят как обычно — в sink("error", текст) из потока источника.
    """
    sid = str(sid or "none")
    kind, _, rest = sid.partition(":")
    if kind == "test":
        return TestSource(sink, fs, freq, **test_kw)
    try:
        if kind == "rtl":
            apply_rtl_driver()
            # отдельный поток чтения USB: прежний RtlSource терял отсчёты 3–5 раз/с (щелчки, срыв RDS и LRPT)
            from .rtl_stream import RtlStreamSource
            return RtlStreamSource(sink, fs, freq, index=int(rest or 0))
        if kind == "rtltcp":
            host, port = parse_hostport(rest)
            return RtlTcpSource(sink, fs, freq, host, port)
        if kind == "hackrf":
            return HackRfSource(sink, fs, freq, int(rest or 0))
        if kind == "airspy":
            return AirspySource(sink, fs, freq, int(rest or 0))
    except (ValueError, DeviceError) as e:
        return _FailSource(sink, fs, freq, f"Не удалось открыть источник «{sid}»: {e}")
    if kind == "sdrplay":
        return _FailSource(sink, fs, freq, "Не удалось открыть SDRplay: " + UNSUPPORTED_HINT["sdrplay"])
    if kind == "none":
        return _FailSource(sink, fs, freq, "Не удалось открыть устройство: нет подходящего источника.")
    return _FailSource(sink, fs, freq, f"Не удалось открыть источник: неизвестный идентификатор «{sid}»")


def wait_opened(src, timeout=5.0):
    """Для проверок: запустить источник и дождаться "opened"/"error" (sink оборачивается)."""
    got = {}
    ev = threading.Event()
    inner = src.sink

    def sink(kind, *args):
        if kind in ("opened", "error") and not ev.is_set():
            got["kind"], got["data"] = kind, args[0]
            ev.set()
        inner(kind, *args)

    src.sink = sink
    src.start()
    t0 = time.time()
    ev.wait(timeout)
    return got.get("kind"), got.get("data"), time.time() - t0
