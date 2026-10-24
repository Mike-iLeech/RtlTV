"""Движок: источник -> очередь -> поток обработки (видео / звук / мультиплекс / поиск) -> сигналы Qt."""
import queue
import threading
import time

import numpy as np
from PySide6.QtCore import QObject, Signal

from .analog import AnalogVideoDecoder, FmSoundDecoder
from .audio import AudioOut
from .digital import MuxAnalyzer, psd_db
from . import devices
from .standards import STANDARDS

SOUND_TUNE = 0.25e6  # несущая звука на -250 кГц от центра приёмника


class Engine(QObject):
    frame = Signal(object)        # dict кадра аналогового ТВ
    spectrum = Signal(object)     # dict(center, fs, psd)
    digital = Signal(object)      # итог измерения мультиплекса
    scan_progress = Signal(int, int, float)
    scan_done = Signal(object)
    opened = Signal(object)
    status = Signal(str)
    error = Signal(str)
    audio_level = Signal(float)

    def __init__(self):
        super().__init__()
        self.q = queue.Queue(maxsize=40)
        self.src = None
        self.worker = None
        self.alive = False
        self.lock = threading.Lock()
        self.fs = 2560000
        self.mode = "analog"
        self.sound = False
        self.std = STANDARDS["DK"]
        self.video_carrier = 583.25e6
        self.mux_center = 546e6
        self.mux_bw = 8e6
        self.gain = 300
        self.vdec = None
        self.fm = None
        self.mux = None
        self.mux_cycle = 0
        self.scan = None
        self.field_swap = False
        self.audio = AudioOut()
        self._last_spec = 0.0
        self.nfft = 1024
        self._drops = 0

    # ---------- управление (из потока интерфейса) ----------
    def start(self, kind, index=0):
        """kind — идентификатор источника ("rtl:0", "rtltcp:host:port", "test", ...) или старое ("rtl", 0)."""
        self.stop()
        self.alive = True
        self.src = devices.make_source(devices.source_id(kind, index), self._sink, self.fs, self._center(),
                                       std_key=self.std.key)
        self.src.gain = self.gain
        self._rebuild()
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()
        self.src.start()
        self._apply_mode()

    def stop(self):
        self.alive = False
        if self.src:
            self.src.stop()
            self.src.join(timeout=2)
            self.src = None
        if self.worker:
            # поток обработки сам выходит по alive=False (очередь читается с таймаутом);
            # блокирующий put(None) здесь вешал интерфейс, когда очередь была полна
            self.worker.join(timeout=2)
            self.worker = None
        self.audio.stop()
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    def running(self):
        return self.src is not None and self.src.is_alive()

    def cmd(self, name, value=None):
        if self.src:
            self.src.command(name, value)

    def set_mode(self, mode, sound):
        with self.lock:
            self.mode, self.sound = mode, sound
            self._rebuild()
        self._apply_mode()

    def set_standard(self, key):
        with self.lock:
            self.std = STANDARDS[key]
            self._rebuild()
        self.cmd("std", key)
        self._apply_mode()

    def set_video_carrier(self, hz):
        with self.lock:
            self.video_carrier = float(hz)
            self._rebuild()
        self._apply_mode()

    def set_mux(self, center, bw):
        with self.lock:
            self.mux_center, self.mux_bw = float(center), float(bw)
            self._rebuild()
        self._apply_mode()

    def set_rate(self, fs):
        with self.lock:
            self.fs = int(fs)
            self._rebuild()
        self.cmd("rate", int(fs))
        self._apply_mode()

    def set_gain(self, tenths):
        self.gain = tenths
        self.cmd("gain", tenths)

    def set_field_swap(self, on):
        self.field_swap = on
        if self.vdec:
            self.vdec.field_swap = on

    def start_scan(self, kind, chans):
        with self.lock:
            items, meta = [], []
            for ch in chans:
                f = ch.video + self.std.tune_offset if kind == "analog" else ch.center
                items.append((f, 65536))
                meta.append(ch)
            self.scan = {"kind": kind, "meta": meta, "res": []}
        self.cmd("plan", {"tag": "scan", "items": items, "repeat": False})

    def cancel_scan(self):
        if self.scan:
            self.scan = None
            self._apply_mode()

    # ---------- внутреннее ----------
    def _center(self):
        if self.mode == "digital":
            return self.mux_center
        if self.sound:
            return self.video_carrier + self.std.sound_offset + SOUND_TUNE
        return self.video_carrier + self.std.tune_offset

    def _rebuild(self):
        self.vdec = AnalogVideoDecoder(self.fs, self.std)
        self.vdec.field_swap = self.field_swap
        self.fm = FmSoundDecoder(self.fs, -SOUND_TUNE)
        self.mux = MuxAnalyzer(self.fs, self.mux_center, self.mux_bw)
        self.mux_cycle = 0

    def _apply_mode(self):
        if not self.src:
            return
        if self.mode == "analog" and self.sound:
            self.audio.start()
            if self.audio.error:
                self.error.emit("Звук: " + self.audio.error)
                self.audio.error = None
        else:
            self.audio.stop()
        if self.mode == "digital":
            self.cmd("freq", int(self.mux_center))
            self.cmd("plan", {"tag": "mux", "items": self.mux.plan(True), "repeat": True})
        else:
            self.cmd("plan", None)
            self.cmd("freq", int(self._center()))

    def _sink(self, kind, *args):
        item = (kind, args)
        try:
            self.q.put_nowait(item)
        except queue.Full:
            # не успеваем — выкидываем старое, декодер видео начнёт заново
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
            self._drops += 1
            self.q.put_nowait(("reset", (None,)))
            try:
                self.q.put_nowait(item)
            except queue.Full:
                pass

    def _loop(self):
        while self.alive:
            try:
                item = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            kind, args = item
            try:
                with self.lock:
                    self._handle(kind, args)
            except Exception as e:  # noqa: BLE001
                self.error.emit(f"Обработка: {e}")

    def _emit_spectrum(self, center, iq, force=False):
        now = time.perf_counter()
        if not force and now - self._last_spec < 0.06:
            return
        self._last_spec = now
        n = min(iq.size, max(16384, 4 * self.nfft))
        self.spectrum.emit({"center": center, "fs": self.fs, "psd": psd_db(iq[:n], self.nfft)})

    def _handle(self, kind, args):
        if kind == "opened":
            self.opened.emit(args[0])
        elif kind == "error":
            self.error.emit(args[0])
        elif kind == "reset":
            self.vdec.reset()
        elif kind == "stream":
            freq, iq = args
            if self.mode != "analog" or abs(freq - self._center()) > 1:
                return
            self._emit_spectrum(freq, iq)
            if self.sound:
                a = self.fm.process(iq)
                self.audio.push(a)
                if a.size:
                    self.audio_level.emit(float(np.sqrt(np.mean(a * a))))
            else:
                for fr in self.vdec.feed(iq):
                    self.frame.emit(fr)
        elif kind == "plan":
            tag, i, n, f, iq = args
            if tag == "mux" and self.mode == "digital" and self.scan is None:
                res = self.mux.add(f, iq)
                if res is not None:
                    self.digital.emit(res)
            elif tag == "scan" and self.scan is not None:
                self._scan_step(i, n, f, iq)
        elif kind == "plan_done":
            if args[0] == "scan" and self.scan is not None:
                res = self.scan["res"]
                self.scan = None
                self.scan_done.emit(res)
                self._apply_mode()

    def _scan_step(self, i, n, f, iq):
        ch = self.scan["meta"][i]
        p = psd_db(iq, 4096)
        bins = (np.arange(4096) - 2048) * (self.fs / 4096)
        if self.scan["kind"] == "analog":
            off = -self.std.tune_offset
            win = np.abs(bins - off) < 60e3
            noise = float(np.median(p))
            snr = float(p[win].max() - noise)
            self.scan["res"].append({"ch": ch, "kind": "analog", "value": snr, "found": snr > 15})
        else:
            win = (np.abs(bins) > 0.1e6) & (np.abs(bins) < 1.1e6)
            self.scan["res"].append({"ch": ch, "kind": "digital", "value": float(np.median(p[win])), "found": None})
            if i == n - 1:
                vals = np.array([r["value"] for r in self.scan["res"]])
                ref = float(np.percentile(vals, 25))
                for r in self.scan["res"]:
                    r["value"] -= ref
                    r["found"] = r["value"] > 3.0
        self.scan_progress.emit(i + 1, n, f)

