"""Вывод звука: кольцевой буфер -> sounddevice.

Вход всегда 48 кГц (моно [N] или стерео [N, 2]). Устройство открывается с тем числом каналов и той
частотой, которые оно поддерживает (Bluetooth-гарнитура: 1 канал, 44,1/16 кГц), звук приводится к ним.
"""
import logging
import os
import threading

import numpy as np

IN_RATE = 48000
log = logging.getLogger("rtltv")


def device_by_name(name):
    """Индекс устройства по сохранённому названию (индексы меняются при подключении наушников)."""
    if not name:
        return None
    for i, n in output_devices():
        if n == name:
            return i
    return None


def output_devices():
    """[(индекс, название)] устройств вывода; первым — «по умолчанию» (None)."""
    out = [(None, "Системное по умолчанию")]
    try:
        import sounddevice as sd
        apis = sd.query_hostapis()
        names = [a["name"] for a in apis]
        want = "Windows WASAPI" if "Windows WASAPI" in names else "MME"  # у WASAPI полные названия
        seen = set()
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] <= 0:
                continue
            api = apis[d["hostapi"]]["name"]
            if api != want:
                continue
            key = (d["name"].strip(), api)
            if key in seen:
                continue
            seen.add(key)
            out.append((i, d["name"].strip()))
    except Exception as e:  # noqa: BLE001
        log.warning("Список звуковых устройств: %s", e)
    return out


class AudioOut:
    """Кольцевой буфер -> sounddevice. Часы донгла и звуковой карты не совпадают — лишнее отбрасываем."""

    def __init__(self):
        self.stream = None
        self.lock = threading.Lock()
        self.chunks = []
        self.size = 0
        self.volume = 0.7
        self.error = None
        self.device = None       # индекс sounddevice или None — по умолчанию
        self.channels = 2
        self.rate = IN_RATE
        self.info = ""
        self.on_error = None     # callback(текст) — показать пользователю
        self._rs_pos = 0.0
        self._rs_last = None
        self.level = 0.0

    def start(self):
        if self.stream:
            return
        self.error = None
        if os.environ.get("RTLTV_MUTE"):  # автопроверки: не шуметь в колонки пользователя
            self.info = "звук выключен (RTLTV_MUTE)"
            return
        try:
            import sounddevice as sd
            dev = sd.query_devices(self.device, "output")
            ch = max(1, min(2, int(dev["max_output_channels"])))
            rate = IN_RATE
            try:
                sd.check_output_settings(device=self.device, channels=ch, samplerate=IN_RATE, dtype="float32")
            except Exception:  # noqa: BLE001 — устройство не умеет 48 кГц: берём его родную частоту
                rate = int(dev["default_samplerate"])
            self.channels, self.rate = ch, rate
            self._rs_pos, self._rs_last = 0.0, None
            self.stream = sd.OutputStream(device=self.device, samplerate=rate, channels=ch, dtype="float32",
                                          blocksize=0, latency="high", callback=self._cb)
            self.stream.start()
            self.info = f"{dev['name'].strip()} · {ch} кан. · {rate / 1000:g} кГц"
            log.info("звук: %s", self.info)
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            self.stream = None
            self.info = "звук не открылся"
            log.error("звук не открылся (устройство %s): %s", self.device, e)
            if self.on_error:
                self.on_error(f"Звук не открылся: {e}")

    def stop(self):
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:  # noqa: BLE001
                pass
            self.stream = None
        with self.lock:
            self.chunks, self.size = [], 0

    def set_device(self, device):
        running = self.stream is not None
        self.stop()
        self.device = device
        if running:
            self.start()

    def _resample(self, a):
        """Линейная передискретизация 48 кГц -> частота устройства с сохранением фазы между блоками."""
        if self.rate == IN_RATE or a.shape[0] == 0:
            return a
        if self._rs_last is None:
            self._rs_last = a[0]
        data = np.concatenate((self._rs_last[None, :], a))
        step = IN_RATE / self.rate
        span = data.shape[0] - 1
        if span <= self._rs_pos:
            self._rs_pos -= span
            self._rs_last = data[-1]
            return a[:0]
        pos = np.arange(self._rs_pos, span, step)
        i0 = pos.astype(np.int64)
        fr = (pos - i0)[:, None].astype(np.float32)
        out = data[i0] * (1 - fr) + data[np.minimum(i0 + 1, span)] * fr
        self._rs_pos = pos[-1] + step - span
        self._rs_last = data[-1]
        return out

    def push(self, a):
        if a.size == 0:
            return
        a = np.asarray(a, np.float32)
        if a.ndim == 1:
            a = a[:, None]
        self.level = float(np.sqrt(np.mean(a * a))) * self.volume
        if not self.stream:
            return
        if self.channels == 1:
            a = a.mean(axis=1, keepdims=True)
        elif a.shape[1] == 1:
            a = np.repeat(a, 2, axis=1)
        a = self._resample(a)
        with self.lock:
            self.chunks.append(a)
            self.size += a.shape[0]
            while self.size > self.rate * 0.5 and len(self.chunks) > 1:
                self.size -= self.chunks.pop(0).shape[0]

    def _cb(self, out, frames, _time, _status):
        with self.lock:
            if self.chunks:
                buf = np.concatenate(self.chunks)
            else:
                buf = np.zeros((0, out.shape[1]), np.float32)
            take = buf[:frames]
            rest = buf[frames:]
            self.chunks = [rest] if rest.shape[0] else []
            self.size = rest.shape[0]
        out[:] = 0
        out[:take.shape[0]] = np.clip(take * self.volume, -1, 1)
