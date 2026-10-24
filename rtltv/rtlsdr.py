"""Встроенный драйвер RTL-SDR: обёртка ctypes над librtlsdr (сборка rtl-sdr-blog, libusb внутри DLL).

DLL лежит рядом с пакетом (rtltv/driver/librtlsdr.dll), поэтому ставить SDR#, Zadig-пакеты
или системный librtlsdr не нужно — достаточно, чтобы на интерфейс 0 донгла был привязан WinUSB.
"""
import ctypes as C
import os
import sys

import numpy as np


class RtlSdrError(RuntimeError):
    pass


TUNER_NAMES = {0: "неизвестен", 1: "E4000", 2: "FC0012", 3: "FC0013", 4: "FC2580",
               5: "R820T/R820T2", 6: "R828D"}

_libs = {}          # путь DLL -> загруженная библиотека
_override = None    # путь к альтернативной librtlsdr (из «Устройства…»), None — встроенная


def _dll_path():
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "rtltv", "driver", "librtlsdr.dll")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "driver", "librtlsdr.dll")


def set_dll_override(path):
    """Подменить librtlsdr для всех окон (None — вернуть встроенную). Действует на следующие открытия."""
    global _override
    _override = os.path.abspath(path) if path else None


def active_dll_path():
    return _override or _dll_path()


def lib(path=None):
    path = os.path.abspath(path) if path else active_dll_path()
    L = _libs.get(path)
    if L is not None:
        return L
    if not os.path.exists(path):
        raise RtlSdrError(f"Не найден драйвер RTL-SDR: {path}")
    try:
        L = C.CDLL(path)
    except OSError as e:
        raise RtlSdrError(f"Драйвер RTL-SDR не загрузился ({os.path.basename(path)}): {e}. "
                          "Нужна 64-битная DLL; её зависимости (libusb-1.0.dll, pthreadVC2.dll) "
                          "должны лежать рядом.") from e
    vp, u32, i32 = C.c_void_p, C.c_uint32, C.c_int
    sigs = {
        "rtlsdr_get_device_count": ([], u32),
        "rtlsdr_get_device_name": ([u32], C.c_char_p),
        "rtlsdr_get_device_usb_strings": ([u32, C.c_char_p, C.c_char_p, C.c_char_p], i32),
        "rtlsdr_open": ([C.POINTER(vp), u32], i32),
        "rtlsdr_close": ([vp], i32),
        "rtlsdr_set_sample_rate": ([vp, u32], i32),
        "rtlsdr_get_sample_rate": ([vp], u32),
        "rtlsdr_set_center_freq": ([vp, u32], i32),
        "rtlsdr_get_center_freq": ([vp], u32),
        "rtlsdr_set_freq_correction": ([vp, i32], i32),
        "rtlsdr_get_tuner_type": ([vp], i32),
        "rtlsdr_get_tuner_gains": ([vp, C.POINTER(i32)], i32),
        "rtlsdr_set_tuner_gain_mode": ([vp, i32], i32),
        "rtlsdr_set_tuner_gain": ([vp, i32], i32),
        "rtlsdr_get_tuner_gain": ([vp], i32),
        "rtlsdr_set_agc_mode": ([vp, i32], i32),
        "rtlsdr_reset_buffer": ([vp], i32),
        "rtlsdr_read_sync": ([vp, vp, i32, C.POINTER(i32)], i32),
    }
    missing = [name for name in sigs if not hasattr(L, name)]
    if missing:
        raise RtlSdrError(f"{os.path.basename(path)} — не librtlsdr: нет функций {', '.join(missing[:4])}")
    for name, (args, res) in sigs.items():
        f = getattr(L, name)
        f.argtypes = args
        f.restype = res
    if hasattr(L, "rtlsdr_set_bias_tee"):
        L.rtlsdr_set_bias_tee.argtypes = [vp, i32]
        L.rtlsdr_set_bias_tee.restype = i32
    _libs[path] = L
    return L


def device_count(path=None):
    return int(lib(path).rtlsdr_get_device_count())


def device_label(index, path=None):
    L = lib(path)
    m, p, s = (C.create_string_buffer(256) for _ in range(3))
    L.rtlsdr_get_device_usb_strings(index, m, p, s)
    text = " ".join(x.value.decode("latin-1", "replace") for x in (m, p) if x.value)
    return text or (L.rtlsdr_get_device_name(index) or b"RTL-SDR").decode("latin-1", "replace")


# u8 -> float: чередующиеся I/Q float32 сразу видятся как complex64
_LUT = ((np.arange(256, dtype=np.float32) - 127.4) / 128.0).astype(np.float32)


def u8_to_iq(raw):
    a = _LUT[np.frombuffer(raw, dtype=np.uint8)]
    return a.view(np.complex64)


class RtlSdr:
    def __init__(self, index=0, lib_path=None):
        self.L = lib(lib_path)
        self.dev = C.c_void_p()
        r = self.L.rtlsdr_open(C.byref(self.dev), index)
        if r != 0:
            raise RtlSdrError(f"Не удалось открыть RTL-SDR #{index} (код {r}). "
                              "Устройство занято другой программой или не привязан WinUSB.")
        self.index = index
        self.label = device_label(index, lib_path)
        self.tuner = TUNER_NAMES.get(self.L.rtlsdr_get_tuner_type(self.dev), "?")
        n = self.L.rtlsdr_get_tuner_gains(self.dev, None)
        arr = (C.c_int * max(n, 1))()
        if n > 0:
            self.L.rtlsdr_get_tuner_gains(self.dev, arr)
        self.gains = [int(arr[i]) for i in range(max(n, 0))]
        self._buf = None

    def close(self):
        if self.dev:
            self.L.rtlsdr_close(self.dev)
            self.dev = C.c_void_p()

    def _chk(self, r, what):
        if r < 0:
            raise RtlSdrError(f"{what}: ошибка {r}")
        return r

    def set_sample_rate(self, hz):
        self._chk(self.L.rtlsdr_set_sample_rate(self.dev, int(hz)), "Частота дискретизации")

    def set_center_freq(self, hz):
        self._chk(self.L.rtlsdr_set_center_freq(self.dev, int(hz)), "Частота настройки")

    def set_ppm(self, ppm):
        self.L.rtlsdr_set_freq_correction(self.dev, int(ppm))  # -2 = значение не изменилось

    def set_gain(self, tenths_db):
        """None — АРУ тюнера, иначе усиление в десятых дБ."""
        if tenths_db is None:
            self.L.rtlsdr_set_tuner_gain_mode(self.dev, 0)
        else:
            self.L.rtlsdr_set_tuner_gain_mode(self.dev, 1)
            self.L.rtlsdr_set_tuner_gain(self.dev, int(tenths_db))

    def set_rtl_agc(self, on):
        self.L.rtlsdr_set_agc_mode(self.dev, 1 if on else 0)

    def set_bias_tee(self, on):
        if hasattr(self.L, "rtlsdr_set_bias_tee"):
            self.L.rtlsdr_set_bias_tee(self.dev, 1 if on else 0)

    def reset_buffer(self):
        self.L.rtlsdr_reset_buffer(self.dev)

    def read_bytes(self, nbytes):
        nbytes = max(512, (int(nbytes) + 511) // 512 * 512)
        if self._buf is None or len(self._buf) < nbytes:
            self._buf = (C.c_ubyte * nbytes)()
        got = C.c_int(0)
        r = self.L.rtlsdr_read_sync(self.dev, C.addressof(self._buf), nbytes, C.byref(got))
        if r < 0:
            raise RtlSdrError(f"Чтение USB: ошибка {r} (донгл отключён?)")
        return bytes(memoryview(self._buf)[:got.value])
