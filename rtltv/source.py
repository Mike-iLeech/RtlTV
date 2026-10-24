"""Источники сигнала: RTL-SDR (встроенный драйвер) и тестовый генератор.

Поток чтения сам выполняет команды (частота, усиление, ...) между чтениями — так librtlsdr
никогда не вызывается из двух потоков сразу. Блоки уходят в sink(kind, *args).
"""
import os
import queue
import threading
import time

from . import rtlsdr
from .testsignal import TestSignal

BLOCK_BYTES = 262144  # 131072 отсчёта ≈ 51 мс при 2,56 МГц


def stream_block_bytes(fs):
    """Блок чтения ≈ 50 мс при любой частоте дискретизации (на 1,024 МГц фиксированный блок был 128 мс —
    спектр и созвездие обновлялись рывками)."""
    n = int(fs * 2 * 0.05) // 16384 * 16384
    return max(32768, min(BLOCK_BYTES, n))
R828D_GAINS = [0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229, 254, 280, 297,
               328, 338, 364, 372, 386, 402, 421, 434, 439, 445, 480, 496]


class SourceBase(threading.Thread):
    def __init__(self, sink, fs, freq):
        super().__init__(daemon=True)
        self.sink = sink
        self.cmds = queue.Queue()
        self.fs = int(fs)
        self.freq = int(freq)
        self.plan = None
        self.running = True
        self.gain = 300
        self.gains = R828D_GAINS

    HARDWARE = False  # True у источников с настоящим приёмником (ppm/bias-T/АРУ имеют смысл)

    def command(self, name, value=None):
        self.cmds.put((name, value))

    def stop(self):
        self.running = False

    def _drain(self):
        changed = False
        while True:
            try:
                name, value = self.cmds.get_nowait()
            except queue.Empty:
                return changed
            if name == "plan":
                self.plan = value
                changed = True
            else:
                self.apply(name, value)
                if name in ("freq", "rate"):
                    changed = True

    def apply(self, name, value):
        raise NotImplementedError

    def read_at(self, freq, nsamples, settle):
        raise NotImplementedError

    def read_stream(self):
        raise NotImplementedError

    def run(self):
        try:
            self.open()
        except Exception as e:  # noqa: BLE001 — сообщение уходит в интерфейс
            self.sink("error", str(e))
            return
        try:
            while self.running:
                if self._drain():
                    self.sink("reset", self.freq)
                if self.plan:
                    plan = self.plan
                    for i, (f, n) in enumerate(plan["items"]):
                        if not self.running or self.plan is not plan or not self.cmds.empty():
                            break
                        iq = self.read_at(f, n, True)
                        self.sink("plan", plan["tag"], i, len(plan["items"]), f, iq)
                    else:
                        if not plan.get("repeat"):
                            self.plan = None
                            self.sink("plan_done", plan["tag"])
                            self.apply("freq", self.freq)
                            self.sink("reset", self.freq)
                    continue
                iq = self.read_stream()
                self.sink("stream", self.freq, iq)
        except Exception as e:  # noqa: BLE001
            self.sink("error", str(e))
        finally:
            self.close()


class RtlSource(SourceBase):
    HARDWARE = True

    def __init__(self, sink, fs, freq, index=0, lib_path=None):
        super().__init__(sink, fs, freq)
        self.index = index
        self.lib_path = lib_path  # None — librtlsdr по умолчанию (встроенная или выбранная в «Устройства…»)
        self.dev = None
        self._tuned = None

    def open(self):
        self.dev = rtlsdr.RtlSdr(self.index, self.lib_path)
        self.gains = self.dev.gains or R828D_GAINS
        self.dev.set_sample_rate(self.fs)
        self.dev.set_center_freq(self.freq)
        self._tuned = self.freq
        self.dev.set_gain(self.gain)
        self.dev.reset_buffer()
        dll = os.path.abspath(self.lib_path or rtlsdr.active_dll_path())
        drv = ("встроенный librtlsdr (rtl-sdr-blog), WinUSB" if dll == os.path.abspath(rtlsdr._dll_path())
               else "librtlsdr пользователя: " + os.path.basename(dll))
        self.sink("opened", {"label": self.dev.label, "tuner": self.dev.tuner, "gains": self.gains,
                             "driver": drv})

    def close(self):
        if self.dev:
            self.dev.close()
            self.dev = None

    def apply(self, name, value):
        d = self.dev
        if name == "freq":
            self.freq = int(value)
            d.set_center_freq(self.freq)
            self._tuned = self.freq
        elif name == "rate":
            self.fs = int(value)
            d.set_sample_rate(self.fs)
        elif name == "gain":
            self.gain = value
            d.set_gain(value)
        elif name == "rtl_agc":
            d.set_rtl_agc(bool(value))
        elif name == "ppm":
            d.set_ppm(int(value))
        elif name == "bias":
            d.set_bias_tee(bool(value))
        d.reset_buffer()

    def read_at(self, freq, nsamples, settle):
        if self._tuned != int(freq):
            self.dev.set_center_freq(int(freq))
            self._tuned = int(freq)
        self.dev.reset_buffer()
        if settle:
            self.dev.read_bytes(32768)  # ~6 мс на успокоение синтезатора
        return rtlsdr.u8_to_iq(self.dev.read_bytes(2 * nsamples))

    def read_stream(self):
        if self._tuned != self.freq:
            self.dev.set_center_freq(self.freq)
            self._tuned = self.freq
            self.dev.reset_buffer()
        return rtlsdr.u8_to_iq(self.dev.read_bytes(stream_block_bytes(self.fs)))


class TestSource(SourceBase):
    """Генератор с реальным темпом выдачи: удобно проверять приложение без антенны."""

    def __init__(self, sink, fs, freq, std_key="DK", generator=None):
        super().__init__(sink, fs, freq)
        self.std_key = std_key
        self.factory = generator or (lambda fs_, std: TestSignal(fs_, std))  # factory(fs, std) -> .render()
        self.gen = None
        self.t_next = 0.0

    def open(self):
        self.gen = self.factory(self.fs, self.std_key)
        self.t_next = time.perf_counter()
        self.sink("opened", {"label": "Тестовый генератор", "tuner": "эмуляция R828D", "gains": self.gains})

    def close(self):
        pass

    def apply(self, name, value):
        if name == "freq":
            self.freq = int(value)
        elif name == "rate":
            self.fs = int(value)
            self.gen = self.factory(self.fs, self.std_key)
        elif name == "gain":
            self.gain = value
        elif name == "std":
            self.std_key = value
            self.gen = self.factory(self.fs, value)

    def _gain_db(self):
        return 30.0 if self.gain is None else self.gain / 10.0

    def _pace(self, n):
        self.t_next += n / self.fs
        dt = self.t_next - time.perf_counter()
        if dt > 0:
            time.sleep(dt)
        elif dt < -0.5:
            self.t_next = time.perf_counter()

    def read_at(self, freq, nsamples, settle):
        raw = self.gen.render(freq, nsamples, self._gain_db())
        self._pace(nsamples + (16384 if settle else 0))
        return rtlsdr.u8_to_iq(raw)

    def read_stream(self):
        n = BLOCK_BYTES // 2
        raw = self.gen.render(self.freq, n, self._gain_db())
        self._pace(n)
        return rtlsdr.u8_to_iq(raw)
