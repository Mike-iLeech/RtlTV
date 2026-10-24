"""Запись и воспроизведение базовой полосы IQ (как «IQ file» / «Baseband recorder» в SDR#).

Форматы записи: WAV 16 бит стерео (I — левый, Q — правый; имя как у SDR#:
SDR4NOOB_20261024_101500Z_101200000Hz_IQ.wav) и сырой cu8 (байты донгла, как у rtl_sdr).
Воспроизведение: WAV 8/16 бит PCM и 32 бит float, сырые .cu8/.raw/.bin (uint8), .cs8 (int8),
.cs16 (int16), .cf32/.cfile/.fc32 (float32). Частота дискретизации — из заголовка WAV или имени файла
(…_2400000sps…, …_2.4MSPS…, …_2400k…), центральная частота — из имени (…_101200000Hz…, …_101.2MHz…).
Если частота дискретизации не подходит радиорежиму (нужна кратная 240 и 48 кГц), поток пересчитывается
на ближайшую подходящую. Файл проигрывается в реальном темпе по кругу.
"""
import os
import re
import struct
import time
import wave

import numpy as np

from .source import SourceBase

RAW_KINDS = {".cu8": "u8", ".raw": "u8", ".bin": "u8", ".dat": "u8", ".cs8": "s8", ".cs16": "s16",
             ".cf32": "f32", ".cfile": "f32", ".fc32": "f32"}
EXTS = sorted(set(RAW_KINDS) | {".wav"})


def file_name(folder, center, fs, fmt="wav"):
    stamp = time.strftime("%Y%m%d_%H%M%SZ", time.gmtime())
    ext = "wav" if fmt == "wav" else "cu8"
    return os.path.join(folder, f"SDR4NOOB_{stamp}_{int(round(center))}Hz_{int(fs)}sps_IQ.{ext}")


def parse_name(path):
    """(частота дискретизации | None, центральная частота | None) из имени файла."""
    name = os.path.basename(path)
    fs = None
    center = None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k|M)?(?:sps|SPS|S/s|Sps)", name)
    if m:
        fs = float(m.group(1)) * {"k": 1e3, "M": 1e6, None: 1.0}[m.group(2)]
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k|M|G)?Hz", name, re.IGNORECASE)
    if m:
        mul = {"k": 1e3, "m": 1e6, "g": 1e9, None: 1.0}[(m.group(2) or "").lower() or None]
        center = float(m.group(1)) * mul
    if fs is None:
        m = re.search(r"[_-](\d{3,5})k(?:[_.-]|$)", name)
        if m:
            fs = float(m.group(1)) * 1e3
    return (int(fs) if fs else None), (center if center else None)


class IqRecorder:
    """Запись комплексного потока (complex64, ±1) в WAV 16 бит или cu8. Писать из потока обработки."""

    def __init__(self, path, fs, fmt="wav"):
        self.path = path
        self.fmt = fmt
        self.fs = int(fs)
        self.samples = 0
        if fmt == "wav":
            self.w = wave.open(path, "wb")
            self.w.setnchannels(2)
            self.w.setsampwidth(2)
            self.w.setframerate(self.fs)
            self.f = None
        else:
            self.w = None
            self.f = open(path, "wb", buffering=1 << 20)

    def write(self, iq):
        if iq.size == 0:
            return
        v = iq.view(np.float32)
        if self.fmt == "wav":
            self.w.writeframesraw(np.clip(np.round(v * 32767), -32768, 32767).astype("<i2").tobytes())
        else:
            self.f.write(np.clip(np.round(v * 128 + 127.4), 0, 255).astype(np.uint8).tobytes())
        self.samples += iq.size

    def seconds(self):
        return self.samples / self.fs

    def close(self):
        try:
            if self.w is not None:
                self.w.close()
            if self.f is not None:
                self.f.close()
        finally:
            self.w = self.f = None


class IqReader:
    """Чтение файла IQ блоками (complex64) по кругу."""

    def __init__(self, path, fs=None, center=None):
        self.path = path
        ext = os.path.splitext(path)[1].lower()
        n_fs, n_center = parse_name(path)
        self.center = center or n_center
        if ext == ".wav":
            self.kind, rate, self.offset, self.nbytes = _wav_info(path)
            self.fs = fs or rate
        elif ext in RAW_KINDS:
            self.kind = RAW_KINDS[ext]
            self.offset = 0
            self.nbytes = os.path.getsize(path)
            self.fs = fs or n_fs
        else:
            raise ValueError(f"Неизвестный формат файла IQ: {ext or 'без расширения'}")
        if not self.fs:
            raise ValueError("Не удалось определить частоту дискретизации — укажите её вручную")
        self.width = {"u8": 2, "s8": 2, "s16": 4, "f32": 8}[self.kind]
        self.total = self.nbytes // self.width
        if self.total <= 0:
            raise ValueError("Файл пуст")
        self.f = open(path, "rb")
        self.pos = 0

    def seconds(self):
        return self.total / self.fs

    def position(self):
        return self.pos / self.fs

    def seek(self, sec):
        self.pos = int(max(0, min(self.total - 1, sec * self.fs)))

    def read(self, n):
        out = []
        left = n
        while left > 0:
            take = min(left, self.total - self.pos)
            self.f.seek(self.offset + self.pos * self.width)
            raw = self.f.read(take * self.width)
            out.append(self._conv(raw))
            self.pos += take
            left -= take
            if self.pos >= self.total:
                self.pos = 0
        return np.concatenate(out) if len(out) > 1 else out[0]

    def _conv(self, raw):
        k = self.kind
        if k == "u8":
            a = (np.frombuffer(raw, np.uint8).astype(np.float32) - 127.4) / 128.0
        elif k == "s8":
            a = np.frombuffer(raw, np.int8).astype(np.float32) / 128.0
        elif k == "s16":
            a = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
        else:
            a = np.frombuffer(raw, "<f4").astype(np.float32)
        return a.view(np.complex64)

    def close(self):
        if self.f:
            self.f.close()
            self.f = None


def _wav_info(path):
    """(вид отсчётов, частота, смещение данных, длина данных) — PCM 8/16 и IEEE float 32, 2 канала."""
    with open(path, "rb") as f:
        hdr = f.read(12)
        if len(hdr) < 12 or hdr[:4] not in (b"RIFF", b"RF64") or hdr[8:12] != b"WAVE":
            raise ValueError("Это не WAV-файл")
        fmt = None
        while True:
            ch = f.read(8)
            if len(ch) < 8:
                break
            cid, size = ch[:4], struct.unpack("<I", ch[4:])[0]
            if cid == b"fmt ":
                body = f.read(size)
                tag, nch, rate, _br, _ba, bits = struct.unpack("<HHIIHH", body[:16])
                if tag == 0xFFFE and len(body) >= 26:     # WAVE_FORMAT_EXTENSIBLE
                    tag = struct.unpack("<H", body[24:26])[0]
                fmt = (tag, nch, rate, bits)
                if size % 2:
                    f.read(1)
            elif cid == b"data":
                if fmt is None:
                    raise ValueError("WAV без заголовка формата")
                tag, nch, rate, bits = fmt
                if nch != 2:
                    raise ValueError(f"В WAV {nch} канал(а), а для IQ нужно 2 (I и Q)")
                if tag == 1 and bits == 8:
                    kind = "u8w"
                elif tag == 1 and bits == 16:
                    kind = "s16"
                elif tag == 3 and bits == 32:
                    kind = "f32"
                else:
                    raise ValueError(f"Формат WAV не поддерживается (код {tag}, {bits} бит)")
                off = f.tell()
                total = os.path.getsize(path) - off
                if size not in (0, 0xFFFFFFFF):
                    total = min(total, size)
                return ("u8" if kind == "u8w" else kind), rate, off, total
            else:
                f.seek(size + (size % 2), 1)
    raise ValueError("В WAV нет данных")


def target_rate(fs, rates):
    """Ближайшая подходящая частота радиорежима: не ниже fs (если есть), иначе максимальная."""
    for r in rates:
        if r == fs:
            return r
    up = [r for r in rates if r >= fs]
    return min(up) if up else max(rates)


class IqFileSource(SourceBase):
    """Источник «файл IQ»: выдаёт блоки в реальном темпе, частоту и полосу задаёт файл (команды
    перестройки игнорируются). При неподходящей частоте дискретизации — пересчёт StreamResampler."""

    def __init__(self, sink, path, rates, fs=None, center=None):
        self.reader = IqReader(path, fs, center)
        self.fs_file = int(self.reader.fs)
        out = target_rate(self.fs_file, rates)
        super().__init__(sink, out, int(self.reader.center or 100e6))
        self.path = path
        self.rs = None
        if out != self.fs_file:
            from .devices import StreamResampler
            self.rs = StreamResampler(self.fs_file, out)
        self.t_next = 0.0
        self.paused = False
        self._seek = None

    def open(self):
        self.t_next = time.perf_counter()
        self.sink("opened", {"label": "Файл IQ: " + os.path.basename(self.path), "tuner": "запись",
                             "gains": self.gains, "file": True})

    def close(self):
        self.reader.close()

    def apply(self, name, value):
        if name == "seek":
            self._seek = float(value)
        elif name == "pause":
            self.paused = bool(value)
            self.t_next = time.perf_counter()

    def read_at(self, freq, nsamples, settle):
        return self.read_stream()

    def read_stream(self):
        while self.paused and self.running:
            time.sleep(0.05)
            self._drain()
        if self._seek is not None:
            self.reader.seek(self._seek)
            self._seek = None
        n = max(4096, int(self.fs_file * 0.05))
        iq = self.reader.read(n)
        if self.rs is not None:
            iq = self.rs.process(iq)
        self.t_next += n / self.fs_file
        dt = self.t_next - time.perf_counter()
        if dt > 0:
            time.sleep(dt)
        elif dt < -0.5:
            self.t_next = time.perf_counter()
        return iq
