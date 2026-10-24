"""RTL-SDR без пропусков отсчётов: отдельный поток, который только читает USB.

Прежний источник (RtlSource) между вызовами rtlsdr_read_sync копировал блок, переводил его в комплексные
числа и отдавал в очередь — несколько миллисекунд, а если GIL занят обработкой, то и больше. RTL2832
в это время не опрашивается, его FIFO переполняется: на 2,4 MS/s было 3–5 пропусков в секунду — щелчки
в звуке, срыв RDS, скачки фазы пилот-тона, «рваный» водопад.

Здесь поток чтения делает только read_sync в заранее выделенные буферы по кругу (GIL на время чтения
отпущен) и кладёт номер буфера в очередь; перевод в комплексные числа — в потоке источника.
Асинхронный режим librtlsdr (rtlsdr_read_async) пробовали: пропусков нет, но под Windows отмена чтения
изредка роняет libusb (access violation / assert при закрытии), а этот способ так же надёжен, как прежний.

Команды (частота, усиление, ppm, bias-T) выполняет сам поток чтения между чтениями — librtlsdr
по-прежнему вызывается только из одного потока. Блоки, прочитанные до перестройки, отбрасываются.
Планы перестроек (сканер DMR, read_at) — с приостановкой потока чтения.
"""
import ctypes as C
import queue
import threading

from . import rtlsdr
from .source import RtlSource, stream_block_bytes


class RtlStreamSource(RtlSource):
    NBUF = 16

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.q = queue.Queue()
        self.dev_cmds = queue.Queue()
        self.want = 0            # номер настройки, данные которой нужны (меняет поток источника)
        self.cur = 0             # номер настройки, с которой сейчас читает поток чтения
        self.lost = 0            # блоки, перезаписанные раньше, чем их забрали (обработка не успевала)
        self._rd = None
        self._rd_run = False
        self._pause = False
        self._paused = threading.Event()
        self._bufs = []
        self._seq = 0
        self._nbytes = 0
        self._err = None

    # ---------- поток чтения ----------
    def _start_reader(self):
        self._nbytes = stream_block_bytes(self.fs)
        self._bufs = [(C.c_ubyte * self._nbytes)() for _ in range(self.NBUF)]
        self._seq = 0
        self._rd_run = True
        self._pause = False
        self._paused.clear()
        self.cur = self.want
        self.dev.reset_buffer()
        self._rd = threading.Thread(target=self._reader, daemon=True, name="rtl-reader")
        self._rd.start()

    def _stop_reader(self):
        if self._rd is None:
            return
        self._rd_run = False
        self._rd.join(timeout=3)
        self._rd = None
        self._flush()

    def _pause_reader(self):
        """Остановить чтение (поток ждёт), чтобы источник сам работал с донглом (планы перестроек)."""
        if self._rd is None or self._paused.is_set():
            return
        self._pause = True
        self._paused.wait(3)
        self._flush()

    def _resume_reader(self):
        if self._rd is None or not self._paused.is_set():
            return
        self.cur = self.want
        self.dev.reset_buffer()
        self._pause = False

    def _reader(self):
        L = self.dev.L
        dev = self.dev.dev
        got = C.c_int(0)
        nb = self._nbytes
        try:
            while self._rd_run:
                if self._pause:
                    self._paused.set()
                    while self._pause and self._rd_run:
                        threading.Event().wait(0.005)
                    self._paused.clear()
                    continue
                if not self.dev_cmds.empty():
                    self._dev_commands()
                i = self._seq % self.NBUF
                r = L.rtlsdr_read_sync(dev, C.addressof(self._bufs[i]), nb, C.byref(got))
                if r == -7:   # LIBUSB_ERROR_TIMEOUT: управление отвечает, а поток данных молчит
                    raise rtlsdr.RtlSdrError(rtlsdr.TIMEOUT_HINT)
                if r < 0:
                    raise rtlsdr.RtlSdrError(f"Чтение USB: ошибка {r} (донгл отключён?)")
                self.q.put((self.cur, self._seq, got.value))
                self._seq += 1
        except Exception as e:  # noqa: BLE001
            self._err = e
            self.q.put(None)

    def _dev_commands(self):
        d = self.dev
        while True:
            try:
                name, value, tag = self.dev_cmds.get_nowait()
            except queue.Empty:
                return
            if name == "freq":
                d.set_center_freq(int(value))
                self._tuned = int(value)
            elif name == "gain":
                d.set_gain(value)
            elif name == "rtl_agc":
                d.set_rtl_agc(bool(value))
            elif name == "ppm":
                d.set_ppm(int(value))
            elif name == "bias":
                d.set_bias_tee(bool(value))
            d.reset_buffer()
            if tag is not None:
                self.cur = tag

    def _flush(self):
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return

    def close(self):
        self.running = False
        if self.dev:
            self._stop_reader()
        super().close()

    # ---------- команды (поток источника) ----------
    def apply(self, name, value):
        if self._rd is None or self._paused.is_set():
            if name == "freq":
                self.want += 1
            return super().apply(name, value)
        if name == "rate":
            self._stop_reader()
            self.want += 1
            return super().apply(name, value)
        tag = None
        if name == "freq":
            self.freq = int(value)
            self.want += 1
            tag = self.want
        elif name == "gain":
            self.gain = value
        self.dev_cmds.put((name, value, tag))

    # ---------- чтение (поток источника) ----------
    def read_at(self, freq, nsamples, settle):
        self._pause_reader()
        self.want += 1
        return super().read_at(freq, nsamples, settle)

    def read_stream(self):
        if self._rd is None:
            if self._tuned != self.freq:
                self.dev.set_center_freq(self.freq)
                self._tuned = self.freq
            self._start_reader()
        elif self._paused.is_set():
            if self._tuned != self.freq:
                self.dev.set_center_freq(self.freq)
                self._tuned = self.freq
            self.want += 1
            self._resume_reader()
        while self.running:
            try:
                item = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                raise self._err or rtlsdr.RtlSdrError("Чтение USB остановилось")
            tag, seq, n = item
            if self._seq - seq >= self.NBUF - 1:
                self.lost += 1           # буфер уже перезаписан: обработка отстала больше чем на ~0,7 с
                continue
            raw = bytes(memoryview(self._bufs[seq % self.NBUF])[:n])
            if tag != self.want or self._seq - seq >= self.NBUF:
                continue
            return rtlsdr.u8_to_iq(raw)
        return rtlsdr.u8_to_iq(b"")
