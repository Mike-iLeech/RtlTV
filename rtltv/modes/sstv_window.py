"""Режим SSTV: приём картинок по радио (КВ SSB, МКС FM) с живой прорисовкой и галереей."""
import os
import queue
import time

import numpy as np
from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QScrollArea, QSizePolicy, QSlider, QSplitter, QVBoxLayout,
                               QWidget)

from .. import devices, settings
from ..modebase import ModeWindow
from ..widgets import FreqDisplay, LevelMeter, SpectrumControls, SpectrumView
from .radio_window import RadioEngine, rtlsdr_gains
from .. import paths

PRESETS = [
    ("КВ 20 м — 14,230 МГц (USB), главная частота SSTV", 14.230e6, "USB"),
    ("КВ 20 м — 14,233 МГц (USB)", 14.233e6, "USB"),
    ("КВ 40 м — 7,171 МГц (LSB)", 7.171e6, "LSB"),
    ("КВ 80 м — 3,730 МГц (LSB)", 3.730e6, "LSB"),
    ("КВ 15 м — 21,340 МГц (USB)", 21.340e6, "USB"),
    ("КВ 10 м — 28,680 МГц (USB)", 28.680e6, "USB"),
    ("МКС — 145,800 МГц (FM), сеансы ARISS", 145.800e6, "NFM"),
    ("2 м — 144,500 МГц (FM)", 144.500e6, "NFM"),
]
DEFAULTS = {"freq": 14.230e6, "demod": "USB", "gain_idx": 22, "agc": False, "volume": 30, "source": "rtl:0",
            "mode": "Авто (VIS)"}


class ImageView(QWidget):
    def __init__(self):
        super().__init__()
        self.img = None
        self.arr = None
        self.text = "Ожидание сигнала SSTV…"
        self.progress = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(320, 240)

    def set_array(self, arr):
        self.arr = np.ascontiguousarray(arr)
        h, w, _ = self.arr.shape
        self.img = QImage(self.arr.data, w, h, 3 * w, QImage.Format_RGB888)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000"))
        if self.img is None:
            p.setPen(QColor("#888"))
            p.drawText(self.rect(), Qt.AlignCenter, self.text)
            return
        W, H = self.width(), self.height()
        iw, ih = self.img.width(), self.img.height()
        k = min(W / iw, H / ih)
        r = QRectF((W - iw * k) / 2, (H - ih * k) / 2, iw * k, ih * k)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawImage(r, self.img)
        if self.progress is not None:
            y = r.top() + r.height() * self.progress
            p.setPen(QColor(255, 80, 80, 200))
            p.drawLine(int(r.left()), int(y), int(r.right()), int(y))


class SstvWindow(ModeWindow):
    sstvEvents = Signal(object)

    def __init__(self, opts=None):
        super().__init__(opts, "RtlTV — SSTV")
        self.s = settings.load("sstv", DEFAULTS)
        if self.opts.get("source"):
            self.s["source"] = self.opts["source"]
        try:
            from ..sstv import MODES as SSTV_MODES, SstvDecoder
            self.decoder = SstvDecoder(48000)
            self.sstv_modes = list(SSTV_MODES)
        except Exception as e:  # noqa: BLE001
            self.decoder = None
            self.sstv_modes = []
            self.load_error = str(e)
        self.engine = RadioEngine()
        self.engine.fs = 2400000
        self.audio_q = queue.Queue(maxsize=200)
        self.engine.taps.append(self._tap)
        self.gains = list(rtlsdr_gains())
        self.canvas = None
        self.cur_mode = None
        self.folder = paths.pictures(paths.PICS_SSTV)
        self._build()
        self.engine.spectrum.connect(self._on_spectrum)
        self.engine.level.connect(lambda db, a, _s: self.lvl.set_value(db, f"{db:.0f} дБ"))
        self.engine.error.connect(lambda m: self.statusBar().showMessage("Ошибка: " + m, 8000))
        self.sstvEvents.connect(self._on_events)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._pump)
        self.timer.start(60)
        self._load_gallery()
        QTimer.singleShot(150, self.start)

    # ================= интерфейс =================
    def _build(self):
        self.view = ImageView()
        if self.decoder is None:
            self.view.text = f"Декодер SSTV не загрузился: {getattr(self, 'load_error', '')}"
        self.gallery = QListWidget()
        self.gallery.setViewMode(QListWidget.IconMode)
        self.gallery.setIconSize(QSize(160, 120))
        self.gallery.setResizeMode(QListWidget.Adjust)
        self.gallery.setMaximumHeight(170)
        self.gallery.setFlow(QListWidget.LeftToRight)
        self.gallery.setWrapping(False)
        self.gallery.itemDoubleClicked.connect(self._open_item)
        self.spec = SpectrumView(waterfall=True)
        self.spec.setMinimumHeight(170)
        self.spec.tuneRequested.connect(lambda f: self._set_freq(round(f / 100) * 100))
        self.spec.wheelStep.connect(lambda d: self._set_freq(self.fdisp.value + d * 100))

        left = QSplitter(Qt.Vertical)
        left.addWidget(self.view)
        left.addWidget(self.gallery)
        left.addWidget(self.spec)
        left.setSizes([520, 150, 190])

        side = QWidget()
        sv = QVBoxLayout(side)
        g = QGroupBox("Приём")
        f = QFormLayout(g)
        self.fdisp = FreqDisplay(lo=0.5e6, hi=1766e6)
        self.fdisp.changed.connect(lambda v: self._set_freq(v))
        f.addRow(self.fdisp)
        self.preset = QComboBox()
        for name, _f, _m in PRESETS:
            self.preset.addItem(name)
        self.preset.setCurrentIndex(-1)
        self.preset.setPlaceholderText("выберите частоту…")
        self.preset.setToolTip("Частоты, где обычно передают SSTV. Режим демодуляции выставится сам.")
        self.preset.activated.connect(self._preset)
        f.addRow("Частоты:", self.preset)
        self.demod = QComboBox()
        self.demod.addItems(["USB", "LSB", "NFM"])
        self.demod.setCurrentText(self.s["demod"])
        self.demod.currentTextChanged.connect(self._demod_changed)
        f.addRow("Демодуляция:", self.demod)
        self.src_combo = QComboBox()
        devices.fill_source_combo(self.src_combo, self.s["source"], "Тестовый эфир (без антенны)", keep_missing=False)
        self.src_combo.activated.connect(lambda _i: self.start())
        f.addRow("Источник:", self.src_combo)
        row = QHBoxLayout()
        self.gain = QSlider(Qt.Horizontal)
        self.gain.setRange(0, len(self.gains) - 1)
        self.gain.setValue(min(self.s["gain_idx"], len(self.gains) - 1))
        self.gain.valueChanged.connect(self._gain)
        self.gain_lbl = QLabel("")
        row.addWidget(self.gain, 1)
        row.addWidget(self.gain_lbl)
        f.addRow("Усиление:", row)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 150)
        self.vol.setValue(self.s["volume"])
        self.vol.valueChanged.connect(lambda v: setattr(self.engine.audio, "volume", v / 100))
        self.engine.audio.volume = self.s["volume"] / 100
        f.addRow("Прослушка:", self.vol)
        self.lvl = LevelMeter(-110, -10, "дБ")
        f.addRow("Сигнал:", self.lvl)
        sv.addWidget(g)

        g2 = QGroupBox("Декодер SSTV")
        f2 = QFormLayout(g2)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Авто (VIS)")
        self.mode_combo.addItems(self.sstv_modes)
        self.mode_combo.setCurrentText(self.s["mode"])
        f2.addRow("Режим:", self.mode_combo)
        row2 = QHBoxLayout()
        b1 = QPushButton("Начать сейчас")
        b1.setToolTip("Начать картинку выбранным режимом без VIS-кода (если начало пропущено)")
        b1.clicked.connect(self._manual)
        b2 = QPushButton("Сброс")
        b2.clicked.connect(self._reset)
        row2.addWidget(b1)
        row2.addWidget(b2)
        f2.addRow(row2)
        self.st_mode = QLabel("—")
        self.st_line = QLabel("—")
        self.st_tone = QLabel("—")
        f2.addRow("Принимается:", self.st_mode)
        f2.addRow("Строка:", self.st_line)
        f2.addRow("Тон / синхро:", self.st_tone)
        bf = QPushButton("Декодировать WAV-файл…")
        bf.clicked.connect(self._decode_file)
        f2.addRow(bf)
        bo = QPushButton("Открыть папку снимков")
        bo.clicked.connect(lambda: (os.makedirs(self.folder, exist_ok=True), os.startfile(self.folder)))
        f2.addRow(bo)
        sv.addWidget(g2)
        note = QLabel("Подсказка: на КВ (14,230 МГц) нужна длинная проволочная антенна; V4 принимает КВ "
                      "без конвертера. МКС передаёт SSTV в дни сеансов ARISS (анонсы на ariss.org), "
                      "в остальное время — голос.")
        note.setObjectName("dim")
        note.setWordWrap(True)
        sv.addWidget(note)
        g3 = QGroupBox("Спектр и водопад")
        g3l = QVBoxLayout(g3)
        self.spec_ctl = SpectrumControls([self.spec], lambda n: setattr(self.engine, "nfft", n), key="sstv",
                                         nfft_default=4096)
        g3l.addWidget(self.spec_ctl)
        sv.addWidget(g3)
        sv.addStretch(1)
        side_scroll = QScrollArea()
        side_scroll.setWidget(side)
        side_scroll.setWidgetResizable(True)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        side_scroll.setMinimumWidth(340)
        side_scroll.setMaximumWidth(400)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(side_scroll)
        split.setStretchFactor(0, 1)
        self.setCentralWidget(split)
        self.sb = QLabel("")
        self.statusBar().addWidget(self.sb, 1)
        self.fdisp.set_value(self.s["freq"])
        self._gain()

    # ================= приём =================
    def start(self):
        src = self.src_combo.currentData()
        if src == "none":
            return
        e = self.engine
        e.configure(mode=self.demod.currentText(), squelch_db=-200.0)
        e.start(src, 0, self._gain_tenths())
        self._set_freq(self.fdisp.value)

    def shutdown(self):
        self.engine.stop()
        s = self.s
        s.update(freq=self.fdisp.value, demod=self.demod.currentText(), gain_idx=self.gain.value(),
                 volume=self.vol.value(), mode=self.mode_combo.currentText())
        if self.src_combo.currentData() != "none" and not self.opts.get("source"):
            s["source"] = self.src_combo.currentData()
        if not self.opts.get("shot"):
            settings.save("sstv", s)

    def _gain_tenths(self):
        return self.gains[min(self.gain.value(), len(self.gains) - 1)]

    def _gain(self):
        t = self._gain_tenths()
        self.gain_lbl.setText(f"{t / 10:.1f} дБ")
        self.engine.cmd("gain", t)

    def _preset(self, i):
        name, f, m = PRESETS[i]
        self.demod.setCurrentText(m)
        self._set_freq(f)

    def _demod_changed(self, m):
        self.engine.configure(mode=m, squelch_db=-200.0)
        self._set_freq(self.fdisp.value)

    def _set_freq(self, f):
        self.fdisp.set_value(f)
        f = self.fdisp.value
        e = self.engine
        fs = e.fs
        if abs(f - e.center) > fs / 2 - 0.1 * fs:
            e.set_center(max(f - 0.15 * fs, 0.5e6 + fs / 2))
        e.configure(offset=f - e.center)
        bw = e.demod.bw
        lo, hi = (f + 100, f + 100 + bw) if self.demod.currentText() == "USB" else \
            (f - 100 - bw, f - 100) if self.demod.currentText() == "LSB" else (f - bw / 2, f + bw / 2)
        self.spec.bands = [(lo, hi, "#4fb3ff")]
        self.spec.markers = [(f, "#ffd84d", f"{f / 1e6:.4f}")]

    def _on_spectrum(self, d):
        n = d["psd"].size
        freqs = d["center"] + (np.arange(n) - n // 2) * (d["fs"] / n)
        self.spec.set_data(freqs, d["psd"])

    # ================= декодер =================
    def _tap(self, mono):
        try:
            self.audio_q.put_nowait(mono.astype(np.float32))
        except queue.Full:
            pass

    def _pump(self):
        """Декодер SSTV работает в потоке интерфейса порциями — он лёгкий, а события сразу рисуются."""
        if self.decoder is None:
            return
        chunks = []
        while True:
            try:
                chunks.append(self.audio_q.get_nowait())
            except queue.Empty:
                break
        if not chunks:
            return
        try:
            ev = self.decoder.process(np.concatenate(chunks))
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"SSTV: {e}", 5000)
            return
        if ev:
            self._on_events(ev)
        lv = self.decoder.level() if hasattr(self.decoder, "level") else {}
        if lv:
            tone = lv.get("tone_hz") or lv.get("freq") or lv.get("tone")
            conf = lv.get("sync") or lv.get("confidence")
            num = (int, float)
            self.st_tone.setText((f"{tone:.0f} Гц" if isinstance(tone, num) else "—")
                                 + (f" · {conf:.2f}" if isinstance(conf, num) else ""))

    def _on_events(self, events):
        for ev in events:
            t = ev.get("type")
            if t == "vis":
                self.statusBar().showMessage(f"VIS: {ev.get('mode')}", 5000)
            elif t == "start":
                w, h = ev["width"], ev["height"]
                self.cur_mode = ev.get("mode")
                self.canvas = np.zeros((h, w, 3), np.uint8)
                self.st_mode.setText(f"{self.cur_mode} ({w}×{h})")
                self.view.progress = 0.0
                self.view.set_array(self.canvas)
            elif t == "line" and self.canvas is not None:
                y = ev["y"]
                rgb = ev.get("rgb")
                if rgb is not None and 0 <= y < self.canvas.shape[0]:
                    self.canvas[y, :rgb.shape[0]] = rgb[:self.canvas.shape[1]]
                self.view.progress = (y + 1) / self.canvas.shape[0]
                self.st_line.setText(f"{y + 1} / {self.canvas.shape[0]}")
                self.view.set_array(self.canvas)
            elif t == "image":
                self.canvas = np.array(ev["image"], np.uint8)
                self.view.set_array(self.canvas)
            elif t == "done":
                img = np.array(ev["image"], np.uint8)
                self.view.progress = None
                self.view.set_array(img)
                self._save_image(img, ev.get("mode") or self.cur_mode)
                self.st_line.setText("готово")
            elif t == "lost":
                self.view.progress = None
                if self.canvas is not None and self.canvas.any():
                    self._save_image(self.canvas, (self.cur_mode or "SSTV") + " (неполная)")
                self.st_line.setText("сигнал потерян")

    def _manual(self):
        m = self.mode_combo.currentText()
        if self.decoder is None or m.startswith("Авто"):
            self.statusBar().showMessage("Выберите конкретный режим SSTV", 4000)
            return
        self.decoder.start_manual(m)

    def _reset(self):
        if self.decoder is not None:
            self.decoder.reset()
        self.canvas = None
        self.view.progress = None
        self.st_mode.setText("—")
        self.st_line.setText("—")

    # ================= галерея =================
    def _save_image(self, img, mode):
        os.makedirs(self.folder, exist_ok=True)
        safe = "".join(c if c.isalnum() else "_" for c in str(mode))
        path = os.path.join(self.folder, time.strftime(f"sstv_%Y%m%d_%H%M%S_{safe}.png"))
        h, w, _ = img.shape
        arr = np.ascontiguousarray(img)
        QImage(arr.data, w, h, 3 * w, QImage.Format_RGB888).save(path)
        self._add_thumb(path, front=True)
        self.statusBar().showMessage(f"Картинка сохранена: {path}", 8000)

    def _add_thumb(self, path, front=False):
        pm = QPixmap(path)
        if pm.isNull():
            return
        it = QListWidgetItem(QIcon(pm.scaled(160, 120, Qt.KeepAspectRatio, Qt.SmoothTransformation)),
                             os.path.basename(path)[5:20])
        it.setData(Qt.UserRole, path)
        if front:
            self.gallery.insertItem(0, it)
        else:
            self.gallery.addItem(it)

    def _load_gallery(self):
        if not os.path.isdir(self.folder):
            return
        files = sorted((f for f in os.listdir(self.folder) if f.endswith(".png")), reverse=True)[:40]
        for f in files:
            self._add_thumb(os.path.join(self.folder, f))

    def _open_item(self, it):
        img = QImage(it.data(Qt.UserRole))
        if not img.isNull():
            img = img.convertToFormat(QImage.Format_RGB888)
            arr = np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.bytesPerLine())
            arr = arr[:, :img.width() * 3].reshape(img.height(), img.width(), 3)
            self.view.progress = None
            self.view.set_array(arr.copy())

    def _decode_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "WAV с сигналом SSTV", "", "Звук (*.wav)")
        if not path:
            return
        try:
            from ..sstv import decode_file
            images = decode_file(path)
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"Файл не декодирован: {e}", 8000)
            return
        for img in images:
            self._save_image(np.asarray(img, np.uint8), "файл")
            self.view.set_array(np.asarray(img, np.uint8))
        self.statusBar().showMessage(f"Из файла получено картинок: {len(images)}", 8000)

    def apply_theme(self, t):
        for w in (self.spec, self.fdisp, self.lvl):
            w.set_theme(t)
