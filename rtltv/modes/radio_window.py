"""Режим «Радио» в духе SDR#/SDR++: спектр/водопад всей полосы с масштабом, VFO, WFM (стерео, RDS)/NFM/AM/
USB/LSB/CW/DMR, АРУ с настройками, шумоподавители, менеджер частот, запись и воспроизведение IQ."""
import os
import queue
import threading
import time
import wave

import numpy as np
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QDoubleSpinBox, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QInputDialog, QLabel, QListWidget, QMessageBox, QPushButton, QScrollArea, QSlider,
                               QSpinBox, QSplitter, QVBoxLayout, QWidget)

from .. import devices, paths, radiotest, settings
from ..audio import AudioOut
from ..help_radio import HELP_HTML, MODE_HELP, MODE_HINT, TIPS
from ..modebase import ModeWindow
from ..radio import MODES, RADIO_RATES, SSB_LOW, FrontEnd, RadioDemod, s_units, spectrum_db
from ..spectrum_nav import BandOverview, SpectrumSideBar
from ..widgets import FreqDisplay, LevelMeter, SpectrumControls, SpectrumView
from .radio_panels import FreqManager, Section

BANDS = [
    # (название, частота, режим)
    ("FM-вещание 87,5–108 МГц", 101.2e6, "WFM"),
    ("Авиа 118–137 МГц (AM)", 125.5e6, "AM"),
    ("Метеоспутники 137 МГц", 137.9e6, "NFM"),
    ("2 м любительский 144–146 МГц", 145.5e6, "NFM"),
    ("МКС 145,800 МГц (голос/SSTV)", 145.8e6, "NFM"),
    ("Морской 156–162 МГц, 16 канал", 156.8e6, "NFM"),
    ("LPD 433 МГц, 1 канал", 433.075e6, "NFM"),
    ("70 см любительский 430–440 МГц", 433.5e6, "NFM"),
    ("PMR446, 1 канал", 446.00625e6, "NFM"),
    ("DMR PMR446 (446,1–446,2 МГц)", 446.10625e6, "DMR"),
    ("КВ 40 м SSB (7,1 МГц, LSB)", 7.1e6, "LSB"),
    ("КВ 40 м телеграф (7,03 МГц)", 7.03e6, "CW"),
    ("КВ 20 м SSB (14,2 МГц, USB)", 14.2e6, "USB"),
    ("КВ 20 м SSTV (14,230 МГц)", 14.23e6, "USB"),
    ("КВ вещание 31 м (9,4–9,9 МГц, AM)", 9.6e6, "AM"),
    ("СВ/ДВ вещание (AM)", 0.999e6, "AM"),
]

DEFAULTS = {"vfo": 101.2e6, "center": 101.0e6, "mode": "WFM", "bw": {}, "rate": 2400000, "gain_idx": 20,
            "agc": False, "ppm": 0, "bias": False, "volume": 60, "squelch": -120, "stereo": True,
            "deemph": 50, "step": {}, "bookmarks": [], "source": "rtl:0", "dmr_slot": 0,
            # в духе SDR#
            "freqs": None, "agc_on": True, "agc_hang": False, "agc_thr": -100, "agc_decay": 400, "agc_slope": 0,
            "if_gain": 40, "nr": False, "nr_strength": 16, "ahp": 0, "alp": 0, "cw_tone": 700,
            "nb": False, "nb_thr": 8, "dc": True, "iqcorr": False, "snap": True, "lock": False, "zoom": 0,
            "panels": {}, "rds": True, "smeter_cal": 0, "iq_fmt": "wav", "files": [],
            "overview": True, "sidebar": True}

STEPS = [(1, "1 Гц"), (10, "10 Гц"), (100, "100 Гц"), (500, "500 Гц"), (1000, "1 кГц"), (5000, "5 кГц"),
         (6250, "6,25 кГц"), (8330, "8,33 кГц"), (9000, "9 кГц"), (10000, "10 кГц"), (12500, "12,5 кГц"),
         (25000, "25 кГц"), (50000, "50 кГц"), (100000, "100 кГц"), (200000, "200 кГц")]

HP_CHOICES = [(0, "выкл"), (100, "100 Гц"), (200, "200 Гц"), (300, "300 Гц"), (500, "500 Гц")]
LP_CHOICES = [(0, "выкл"), (2400, "2,4 кГц"), (3000, "3 кГц"), (4000, "4 кГц"), (6000, "6 кГц"),
              (10000, "10 кГц"), (15000, "15 кГц")]


class RadioEngine(QObject):
    spectrum = Signal(object)
    level = Signal(float, float, bool)       # уровень канала dB, уровень звука, стерео
    dmr = Signal(object, object)             # события, статус
    opened = Signal(object)
    error = Signal(str)
    info = Signal(object)                    # RDS, АРУ, С/Ш, бланкер, файл, запись — 10 раз в секунду

    def __init__(self, audio=None):
        super().__init__()
        self.q = queue.Queue(maxsize=30)
        self.src = None
        self.worker = None
        self.alive = False
        self.lock = threading.Lock()
        self.fs = 2400000
        self.center = 101.0e6
        self.demod = RadioDemod(self.fs, "WFM")
        self.audio = audio or AudioOut()
        self.wav = None
        self._last_spec = 0.0
        self._last_lvl = 0.0
        self.drops = 0
        self.dmr_decoder = None
        self.nfft = 4096
        self.plan_handler = None  # callback(kind, args) для блоков плана перестроек (сканеры), в потоке обработки
        self.taps = []          # функции, получающие звук (моно 48 кГц) в потоке обработки
        self.generator = radiotest.factory
        # дополнительно (включает окно «Радио»; у других режимов всё как раньше)
        self.front = None         # FrontEnd: бланкер, постоянная составляющая, коррекция IQ
        self.spec_dbfs = False    # спектр в dBFS (тон полной шкалы = 0 dB)
        self.async_rtl = False    # RTL-SDR через отдельный поток чтения USB (без пропусков отсчётов)
        self.fixed_center = False  # источник — файл IQ: центр и полоса заданы файлом
        self.iqrec = None
        self.noise_db = None      # уровень шума на бин спектра, dBFS
        self._last_vfo = None

    def start(self, kind, index, gain, file_fs=None, file_center=None):
        self.stop()
        self.alive = True
        sid = devices.source_id(kind, index)
        self.fixed_center = False
        if sid.startswith("file:"):
            from ..iqfile import IqFileSource
            self.src = IqFileSource(self._sink, sid[5:], RADIO_RATES, fs=file_fs, center=file_center)
            self.fixed_center = True
            with self.lock:
                self.center = float(self.src.freq)
            self._rebuild(int(self.src.fs))
        elif sid.startswith("rtl:") and self.async_rtl:
            from ..rtl_stream import RtlStreamSource
            devices.apply_rtl_driver()
            self.src = RtlStreamSource(self._sink, self.fs, int(self.center), index=int(sid[4:] or 0))
        else:
            self.src = devices.make_source(sid, self._sink, self.fs, int(self.center), generator=self.generator)
        self.src.gain = gain
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()
        self.src.start()
        self.audio.start()

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
        self.stop_record()
        self.stop_iq_record()

    def running(self):
        return self.src is not None and self.src.is_alive()

    def cmd(self, name, value=None):
        if self.src:
            self.src.command(name, value)

    def set_center(self, f):
        if self.fixed_center:
            return
        with self.lock:
            self.center = float(f)
        self.cmd("freq", int(f))

    def _rebuild(self, fs):
        with self.lock:
            self.fs = int(fs)
            d = self.demod
            nd = type(d)(self.fs, d.mode, d.bw)
            for k in ("offset", "squelch_db", "stereo", "cw_tone", "audio_nr", "nr_strength", "rds_on",
                      "nr_on", "dmr"):
                if hasattr(d, k):
                    setattr(nd, k, getattr(d, k))
            nd.agc = d.agc
            nd.agc.reset()
            nd.set_deemph(d.deemph_tau)
            nd.set_audio_filter(d.audio_hp, d.audio_lp)
            self.demod = nd

    def set_rate(self, fs):
        if self.fixed_center:
            return
        self._rebuild(fs)
        self.cmd("rate", int(fs))

    def configure(self, **kw):
        with self.lock:
            d = self.demod
            if "mode" in kw or "bw" in kw:
                mode = kw.get("mode", d.mode)
                if mode == d.mode and kw.get("bw"):
                    d.set_bandwidth(kw["bw"])
                else:
                    d.set_mode(mode, kw.get("bw"))
                if d.mode == "DMR" and d.dmr is None:
                    d.dmr = self._make_dmr()
            for k in ("offset", "squelch_db", "stereo", "cw_tone", "audio_nr", "nr_strength", "rds_on"):
                if k in kw:
                    setattr(d, k, kw[k])
            if "deemph_tau" in kw:
                d.set_deemph(kw["deemph_tau"])
            if "audio_hp" in kw or "audio_lp" in kw:
                d.set_audio_filter(kw.get("audio_hp"), kw.get("audio_lp"))
            if "agc" in kw:
                for k, v in kw["agc"].items():
                    setattr(d.agc, k, v)
            if "dmr_slot" in kw and d.dmr is not None:
                d.dmr.slot_filter = kw["dmr_slot"]

    def _make_dmr(self):
        try:
            from ..dmr import DmrDecoder
            self.dmr_decoder = DmrDecoder(48000)
            return self.dmr_decoder
        except Exception as e:  # noqa: BLE001
            self.error.emit(f"DMR недоступен: {e}")
            return None

    def start_record(self, path):
        self.stop_record()
        w = wave.open(path, "wb")
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        self.wav = w

    def stop_record(self):
        if self.wav:
            try:
                self.wav.close()
            finally:
                self.wav = None

    def start_iq_record(self, path, fmt="wav"):
        from ..iqfile import IqRecorder
        self.stop_iq_record()
        rec = IqRecorder(path, self.fs, fmt)
        with self.lock:
            self.iqrec = rec

    def stop_iq_record(self):
        with self.lock:
            rec, self.iqrec = self.iqrec, None
        if rec is not None:
            rec.close()

    def _sink(self, kind, *args):
        try:
            self.q.put_nowait((kind, args))
        except queue.Full:
            self.drops += 1

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
                if kind == "stream":
                    self._stream(*args)
                elif kind in ("plan", "plan_done", "reset"):
                    if self.plan_handler is not None:
                        self.plan_handler(kind, args)
                elif kind == "opened":
                    self.opened.emit(args[0])
                elif kind == "error":
                    self.error.emit(args[0])
            except Exception as e:  # noqa: BLE001
                self.error.emit(f"Обработка: {e}")

    def _stream(self, freq, iq):
        if iq.size == 0:
            return
        with self.lock:
            if abs(freq - self.center) > 1:
                return
            if self.iqrec is not None:
                self.iqrec.write(iq)
            raw = iq
            if self.front is not None:
                iq = self.front.process(iq, self.fs)
            d = self.demod
            vfo = freq + d.offset
            if self._last_vfo is None or abs(vfo - self._last_vfo) > 3000:
                d.reset_station()          # другая станция: сбросить RDS и захват стерео
            self._last_vfo = vfo
            audio = d.process(iq)
            ev = d.dmr_events
            d.dmr_events = []
        self.audio.push(audio)
        if self.taps and audio.size:
            mono = audio.mean(axis=1) if audio.ndim == 2 else audio
            for tap in self.taps:
                tap(mono)
        if self.wav is not None and audio.size:
            st = audio if audio.ndim == 2 else np.repeat(audio[:, None], 2, axis=1)
            self.wav.writeframes((np.clip(st, -1, 1) * 32767).astype("<i2").tobytes())
        now = time.perf_counter()
        if now - self._last_spec > 0.04:
            self._last_spec = now
            p = spectrum_db(iq, self.nfft, dbfs=self.spec_dbfs)
            if p is not None:
                self.noise_db = float(np.median(p))
                self.spectrum.emit({"center": freq, "fs": self.fs, "psd": p})
        if now - self._last_lvl > 0.1:
            self._last_lvl = now
            arms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
            self.level.emit(d.level_db, arms, bool(d.stereo_lock and d.mode == "WFM"))
            if d.mode == "DMR" and d.dmr is not None:
                st = d.dmr.status() if hasattr(d.dmr, "status") else {}
                self.dmr.emit(ev, st)
            inf = self._info(d)
            # перегрузка АЦП: доля отсчётов у края шкалы (u8: 0 или 255)
            inf["clip"] = float(np.mean(np.abs(raw.view(np.float32)[::8]) > 0.99))
            self.info.emit(inf)
        elif ev:
            self.dmr.emit(ev, None)

    def _info(self, d):
        inf = {"agc_db": d.agc.gain_db() if d.mode in ("AM", "USB", "LSB", "CW") else None,
               "pilot": d.pilot_level if d.mode == "WFM" else None, "sq_open": d.sq_open,
               "level_db": d.level_db}
        if d.mode == "WFM" and d.rds is not None and d.rds_on:
            inf["rds"] = d.rds.state()
        if self.noise_db is not None and self.spec_dbfs:
            # шум в полосе канала: уровень шума на бин × число бинов в полосе / ЭШП окна Блэкмана (1,73 бина)
            bins = d.bw / (self.fs / max(self.nfft, 1)) / 1.73
            inf["snr"] = d.level_db - (self.noise_db + 10 * np.log10(max(bins, 1e-3)))
        if self.front is not None:
            inf["nb"] = self.front.blanked
            inf["iq_err"] = self.front.iq_err
        src = self.src
        if src is not None and hasattr(src, "reader"):
            inf["file"] = (src.reader.position(), src.reader.seconds())
        if src is not None and hasattr(src, "lost"):
            inf["lost"] = src.lost
        if self.iqrec is not None:
            inf["iqrec"] = (self.iqrec.seconds(), self.iqrec.samples * (4 if self.iqrec.fmt == "wav" else 2))
        lat = getattr(self.audio, "latency", None)
        if lat:
            inf["latency"] = lat()
        return inf


class RadioWindow(ModeWindow):
    def __init__(self, opts=None):
        super().__init__(opts, "SDR4NOOB — Радио")
        self.s = settings.load("radio", DEFAULTS)
        for k, v in DEFAULTS.items():
            self.s.setdefault(k, v)
        if self.s.get("freqs") is None:   # закладки прежних версий -> менеджер частот, группа «Мои»
            self.s["freqs"] = [{"name": b["name"], "freq": b["freq"], "mode": b["mode"], "group": "Мои"}
                               for b in self.s.get("bookmarks") or []]
        if self.opts.get("source"):
            self.s["source"] = self.opts["source"]
        from ..radio_audio import RadioAudioOut
        self.engine = RadioEngine(audio=RadioAudioOut())
        self.engine.front = FrontEnd()
        self.engine.spec_dbfs = True
        self.engine.async_rtl = True
        self.engine.fs = int(self.s["rate"])
        self.engine.center = float(self.s["center"])
        self.gains = list(rtlsdr_gains())
        self._file = None            # (путь, fs, центр) при воспроизведении файла IQ
        self._drag_t = 0.0
        self._drag_f = 0.0
        self._build()
        e = self.engine
        e.spectrum.connect(self._on_spectrum)
        e.level.connect(self._on_level)
        e.dmr.connect(self._on_dmr)
        e.info.connect(self._on_info)
        e.error.connect(lambda m: self.statusBar().showMessage("Ошибка: " + m, 8000))
        e.opened.connect(self._on_opened)
        self._apply_dsp()
        self._sql_changed(self.sql.value())
        self._set_mode(self.s["mode"], initial=True)
        self._set_vfo(self.s["vfo"], push=False)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._status)
        self.timer.start(500)
        QTimer.singleShot(150, self.start)

    # ================= интерфейс =================
    def _build(self):
        # верх в три строки: табло и режимы / звук и уровень / RDS — помещается в 1280 px
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(6, 4, 6, 2)
        tv.setSpacing(4)
        tl = QHBoxLayout()
        tl.setSpacing(6)
        tv.addLayout(tl)
        self.btn_start = QPushButton("▶")
        self.btn_start.setCheckable(True)
        self.btn_start.setFixedWidth(44)
        self.btn_start.setToolTip("Старт/стоп приёма (пробел)")
        self.btn_start.clicked.connect(lambda on: self.start() if on else self.stop())
        tl.addWidget(self.btn_start)
        self.fdisp = FreqDisplay(lo=0.5e6, hi=1766e6)
        self.fdisp.setMinimumWidth(280)
        self.fdisp.setMaximumWidth(420)
        self.fdisp.changed.connect(lambda v: self._set_vfo(v))
        tl.addWidget(self.fdisp, 2)
        self.mode_btns = QButtonGroup(self)
        for m in MODES:
            b = QPushButton(m)
            b.setCheckable(True)
            b.setMinimumWidth(46)
            b.setMaximumWidth(64)
            b.setToolTip(MODE_HELP[m])
            b.clicked.connect(lambda _c=False, mm=m: self._set_mode(mm))
            self.mode_btns.addButton(b)
            tl.addWidget(b, 1)
        tl.addStretch(1)
        hb = QPushButton("❔ Справка")
        hb.setToolTip("Краткая справка по радио: режимы, мышь и клавиши, где искать цифровые переговоры. "
                      "Полное руководство — F1")
        hb.clicked.connect(self.show_help)
        tl.addWidget(hb)

        t2 = QHBoxLayout()
        t2.setSpacing(6)
        tv.addLayout(t2)
        t2.addWidget(QLabel("Громкость"))
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 150)
        self.vol.setValue(self.s["volume"])
        self.vol.setMinimumWidth(70)
        self.vol.setMaximumWidth(150)
        self.vol.setToolTip("Громкость (до 150 %). Устройство вывода — меню «Звук».")
        self.vol.valueChanged.connect(lambda v: setattr(self.engine.audio, "volume", v / 100))
        self.engine.audio.volume = self.s["volume"] / 100
        t2.addWidget(self.vol, 1)
        t2.addWidget(QLabel("Шумодав"))
        self.sql = QSlider(Qt.Horizontal)
        self.sql.setRange(-120, 0)
        self.sql.setValue(self.s["squelch"])
        self.sql.setMinimumWidth(70)
        self.sql.setMaximumWidth(150)
        self.sql.setToolTip(TIPS["squelch"])
        self.sql.valueChanged.connect(self._sql_changed)
        t2.addWidget(self.sql, 1)
        self.sql_lbl = QLabel("")
        self.sql_lbl.setMinimumWidth(56)
        t2.addWidget(self.sql_lbl)
        self.lvl = LevelMeter(-110, -10, "dB")
        self.lvl.setMinimumWidth(150)
        self.lvl.setMaximumWidth(230)
        self.lvl.setToolTip(TIPS["smeter"])
        t2.addWidget(self.lvl, 1)
        self.stereo_lbl = QLabel("")
        self.stereo_lbl.setMinimumWidth(60)
        t2.addWidget(self.stereo_lbl)
        self.ovl_lbl = QLabel("")
        self.ovl_lbl.setStyleSheet("color: #ff5a4f; font-weight: bold;")
        self.ovl_lbl.setToolTip(TIPS["overload"])
        t2.addWidget(self.ovl_lbl)
        self.mode_hint = QLabel("")
        self.mode_hint.setObjectName("dim")
        self.mode_hint.setMinimumWidth(10)
        t2.addWidget(self.mode_hint, 3)

        self.rds_bar = QWidget()
        rb = QHBoxLayout(self.rds_bar)
        rb.setContentsMargins(0, 0, 0, 0)
        self.rds_ps = QLabel("RDS —")
        f = QFont("Consolas")
        f.setPointSize(13)
        f.setBold(True)
        self.rds_ps.setFont(f)
        self.rds_ps.setMinimumWidth(110)
        self.rds_ps.setToolTip("Название станции (RDS PS)")
        rb.addWidget(self.rds_ps)
        self.rds_rt = QLabel("")
        self.rds_rt.setMinimumWidth(10)
        self.rds_rt.setToolTip("Радиотекст (RDS RT): песня, ведущий, новости")
        rb.addWidget(self.rds_rt, 1)
        self.rds_meta = QLabel("")
        self.rds_meta.setObjectName("dim")
        rb.addWidget(self.rds_meta)
        tv.addWidget(self.rds_bar)

        self.spec = SpectrumView(waterfall=True)
        self.spec.tuneRequested.connect(self._spectrum_click)
        self.spec.wheelStep.connect(lambda d: None if self._locked() else self._step(d))
        self.spec.ctrlWheelStep.connect(self._bw_wheel)
        self.spec.zoomWheel.connect(lambda d: self.zoom.setValue(self.zoom.value() + 5 * d))
        self.spec.panRequested.connect(self._pan)
        self.spec.pan_enabled = True
        self.spec.bandEdgeDragged.connect(self._edge_drag)
        self.spec.bandDragged.connect(self._band_drag)
        self.spec.band_drag_enabled = True
        self.spec.zoom_enabled = True
        self.spec.setToolTip(TIPS["spectrum"])
        self._pan_df = 0.0
        self._pan_timer = QTimer(self)
        self._pan_timer.setSingleShot(True)
        self._pan_timer.timeout.connect(self._apply_pan)

        side = QWidget()
        sv = QVBoxLayout(side)
        sv.setSpacing(4)
        sv.setContentsMargins(4, 4, 4, 4)
        self.sections = {}
        sv.addWidget(self._sec("src", "Источник", self._grp_source))
        sv.addWidget(self._sec("demod", "Демодулятор", self._grp_radio))
        sv.addWidget(self._sec("agc", "АРУ (автоматическая регулировка усиления)", self._grp_agc, False))
        sv.addWidget(self._sec("audio", "Звук: фильтр и шумоподавитель", self._grp_audio, False))
        sv.addWidget(self._sec("rf", "Обработка сигнала: бланкер, IQ", self._grp_rf, False))
        sv.addWidget(self._sec("rds", "RDS", self._grp_rds))
        sv.addWidget(self._sec("dmr", "DMR", self._grp_dmr))
        sv.addWidget(self._sec("dmrscan", "Поиск цифрового DMR", self._grp_dmrscan, False))
        sv.addWidget(self._sec("freqs", "Менеджер частот", self._grp_freqs))
        sv.addWidget(self._sec("rec", "Запись: звук и IQ", self._grp_record, False))
        sv.addWidget(self._sec("spec", "Спектр и водопад", self._grp_spectrum, False))
        sv.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(side)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(340)
        scroll.setMaximumWidth(410)

        # спектр: сверху обзор всего диапазона, справа вертикальные ручки (как в SDR#)
        self.overview = BandOverview()
        self.overview.jumpRequested.connect(self._overview_jump)
        self._jump_f = None
        self._jump_timer = QTimer(self)
        self._jump_timer.setSingleShot(True)
        self._jump_timer.timeout.connect(self._apply_jump)
        fmt_db = lambda v: f"{v:+d} dB" if v else "0 dB"   # noqa: E731
        self.sidebar = SpectrumSideBar([
            ("Зум", self.zoom, lambda v: f"×{2 ** (v / 10):.1f}" if v < 34 else f"×{2 ** (v / 10):.0f}",
             "Масштаб (Zoom)\n" + TIPS["zoom"]),
            ("Контр.", self.spec_ctl.wf_con, lambda v: f"{v}%",
             "Контраст водопада (Contrast)\n" + self.spec_ctl.wf_con.toolTip()),
            ("Диап.", self.spec_ctl.range, lambda v: f"{v} dB" if v else "авто",
             "Диапазон шкалы спектра (Range)\n" + self.spec_ctl.range.toolTip()),
            ("Смещ.", self.spec_ctl.wf_off, fmt_db,
             "Смещение — яркость водопада (Offset)\n" + self.spec_ctl.wf_off.toolTip()),
        ])
        spec_box = QWidget()
        sb_l = QVBoxLayout(spec_box)
        sb_l.setContentsMargins(0, 0, 0, 0)
        sb_l.setSpacing(2)
        sb_l.addWidget(self.overview)
        row_spec = QHBoxLayout()
        row_spec.setContentsMargins(0, 0, 0, 0)
        row_spec.setSpacing(0)
        row_spec.addWidget(self.spec, 1)
        row_spec.addWidget(self.sidebar)
        sb_l.addLayout(row_spec, 1)
        self.overview.setVisible(bool(self.s["overview"]))
        self.sidebar.setVisible(bool(self.s["sidebar"]))

        split = QSplitter(Qt.Horizontal)
        split.addWidget(scroll)
        split.addWidget(spec_box)
        split.setStretchFactor(1, 1)
        split.setCollapsible(0, True)
        central = QWidget()
        cv = QVBoxLayout(central)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.addWidget(top)
        cv.addWidget(split, 1)
        self.setCentralWidget(central)
        self.sb = QLabel("")
        self.statusBar().addWidget(self.sb, 1)
        for key, fn in (("Ctrl+Right", lambda: None if self._locked() else self._step(1)),
                        ("Ctrl+Left", lambda: None if self._locked() else self._step(-1)),
                        ("Ctrl+Up", lambda: self._bw_wheel(1)), ("Ctrl+Down", lambda: self._bw_wheel(-1)),
                        ("Ctrl+=", lambda: self.zoom.setValue(self.zoom.value() + 5)),
                        ("Ctrl+-", lambda: self.zoom.setValue(self.zoom.value() - 5)),
                        ("Space", self._space)):
            QShortcut(QKeySequence(key), self, activated=fn)

    def _space(self):
        """Пробел — старт/стоп, но не когда печатают в поле (поиск в менеджере частот)."""
        from PySide6.QtWidgets import QApplication, QLineEdit
        w = QApplication.focusWidget()
        if isinstance(w, QLineEdit):
            w.insert(" ")
            return
        self.btn_start.click()

    def _grp_dmrscan(self, body):
        from .dmr_scan_panel import DmrScanPanel
        lay = QVBoxLayout(body)
        lay.setContentsMargins(6, 4, 6, 6)
        self.dmr_scan = DmrScanPanel(self.engine, framed=False)
        self.dmr_scan.tuneRequested.connect(self._scan_tune)
        lay.addWidget(self.dmr_scan)

    def _sec(self, key, title, builder, expanded=True):
        sec = Section(title, self.s["panels"].get(key, expanded))
        builder(sec.body)
        sec.toggled.connect(lambda on, k=key: self.s["panels"].__setitem__(k, on))
        self.sections[key] = sec
        return sec

    def _grp_source(self, body):
        f = QFormLayout(body)
        self.src_combo = QComboBox()
        devices.fill_source_combo(self.src_combo, self.s["source"], "Тестовый эфир (без антенны)", keep_missing=False)
        for p in self.s.get("files") or []:
            if os.path.exists(p):
                self.src_combo.addItem("Файл IQ: " + os.path.basename(p), "file:" + p)
        idx = self.src_combo.findData(self.s["source"])
        if idx >= 0:
            self.src_combo.setCurrentIndex(idx)
        self.src_combo.activated.connect(lambda _i: self.start() if self.engine.running() else None)
        self.src_combo.setToolTip("Приёмник или записанный файл IQ")
        f.addRow(self.src_combo)
        row = QHBoxLayout()
        b = QPushButton("Открыть файл IQ…")
        b.setToolTip(TIPS["iqfile"])
        b.clicked.connect(self._open_iq_file)
        row.addWidget(b)
        self.file_pos = QSlider(Qt.Horizontal)
        self.file_pos.setRange(0, 1000)
        self.file_pos.setToolTip("Позиция в файле: перетащите, чтобы перемотать")
        self.file_pos.sliderReleased.connect(self._file_seek)
        row.addWidget(self.file_pos, 1)
        f.addRow(row)
        self.file_lbl = QLabel("")
        self.file_lbl.setObjectName("dim")
        self.file_lbl.setVisible(False)
        f.addRow(self.file_lbl)
        self.rate = QComboBox()
        for r in RADIO_RATES:
            self.rate.addItem(f"{r / 1e6:.3f} МГц", r)
        self.rate.setCurrentIndex(max(0, self.rate.findData(self.s["rate"])))
        self.rate.currentIndexChanged.connect(self._rate_changed)
        self.rate.setToolTip(TIPS["rate"])
        f.addRow("Полоса:", self.rate)
        row = QHBoxLayout()
        self.gain = QSlider(Qt.Horizontal)
        self.gain.setRange(0, len(self.gains) - 1)
        self.gain.setValue(min(self.s["gain_idx"], len(self.gains) - 1))
        self.gain.valueChanged.connect(self._gain_changed)
        self.gain.setToolTip(TIPS["gain"])
        self.gain_lbl = QLabel("")
        self.gain_lbl.setMinimumWidth(56)
        row.addWidget(self.gain, 1)
        row.addWidget(self.gain_lbl)
        f.addRow("Усиление:", row)
        self.cb_agc = QCheckBox("АРУ тюнера")
        self.cb_agc.setChecked(self.s["agc"])
        self.cb_agc.toggled.connect(lambda _v: self._gain_changed())
        self.cb_agc.setToolTip(TIPS["agc"])
        f.addRow(self.cb_agc)
        self.ppm = QSpinBox()
        self.ppm.setRange(-200, 200)
        self.ppm.setSuffix(" ppm")
        self.ppm.setValue(self.s["ppm"])
        self.ppm.valueChanged.connect(lambda v: self.engine.cmd("ppm", v))
        self.ppm.setToolTip(TIPS["ppm"])
        f.addRow("Коррекция:", self.ppm)
        self.cb_bias = QCheckBox("Bias-T (питание антенны)")
        self.cb_bias.setChecked(self.s["bias"])
        self.cb_bias.setToolTip(TIPS["bias"])
        self.cb_bias.toggled.connect(lambda v: self.engine.cmd("bias", v))
        f.addRow(self.cb_bias)
        row = QHBoxLayout()
        self.scal = QDoubleSpinBox()
        self.scal.setRange(-60.0, 60.0)
        self.scal.setSingleStep(0.5)
        self.scal.setDecimals(1)
        self.scal.setSuffix(" dB")
        self.scal.setValue(float(self.s.get("smeter_cal", 0)))
        self.scal.setToolTip(TIPS["smeter_cal"])
        self.scal.valueChanged.connect(lambda v: self.s.__setitem__("smeter_cal", float(v)))
        b_cal = QPushButton("Калибровать…")
        b_cal.setToolTip("Настройтесь на сигнал известного уровня и введите его — поправка посчитается сама")
        b_cal.clicked.connect(self._calibrate_smeter)
        row.addWidget(self.scal, 1)
        row.addWidget(b_cal)
        f.addRow("Поправка S-метра:", row)
        self.center_lbl = QLabel("")
        self.center_lbl.setObjectName("dim")
        f.addRow(self.center_lbl)
        self._last_dbm_raw = None
        self._gain_changed()

    def _calibrate_smeter(self):
        """Калибровка по сигналу известного уровня: поправка = известный уровень − измеренный без поправки."""
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout as _F, QLabel as _L, QDoubleSpinBox as _D
        raw = self._last_dbm_raw
        if raw is None:
            QMessageBox.information(self, "Калибровка S-метра",
                                    "Запустите приём, выключите АРУ тюнера и настройтесь на сигнал известного уровня "
                                    "(генератор, маяк, опорная станция) — тогда можно калибровать.")
            return
        d = QDialog(self)
        d.setWindowTitle("Калибровка S-метра")
        lay = _F(d)
        hint = _L("Настройтесь точно на опорный сигнал (полоса демодулятора должна его целиком охватывать) и "
                  "введите его уровень на входе приёмника. Поправка = известный уровень − измеренный.\n"
                  "Поправка зависит от частоты и усиления — калибруйте там, где чаще слушаете.")
        hint.setWordWrap(True)
        lay.addRow(hint)
        meas = _L(f"{raw:.1f} dBm (без поправки)")
        lay.addRow("Сейчас измерено:", meas)
        known = _D()
        known.setRange(-160.0, 10.0)
        known.setDecimals(1)
        known.setSuffix(" dBm")
        known.setValue(round(raw + float(self.s.get("smeter_cal", 0)), 1))
        lay.addRow("Известный уровень:", known)
        res = _L("")
        lay.addRow("Поправка будет:", res)

        def upd():
            r = self._last_dbm_raw if self._last_dbm_raw is not None else raw
            meas.setText(f"{r:.1f} dBm (без поправки)")
            res.setText(f"{known.value() - r:+.1f} dB")
        known.valueChanged.connect(lambda _v: upd())
        t = QTimer(d)
        t.timeout.connect(upd)
        t.start(300)
        upd()
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Применить")
        bb.button(QDialogButtonBox.Cancel).setText("Отмена")
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        lay.addRow(bb)
        if d.exec() == QDialog.Accepted:
            if self._last_dbm_raw is None:      # пока диалог был открыт, включили АРУ или остановили приём
                QMessageBox.warning(self, "Калибровка S-метра",
                                    "Уровень сейчас не измеряется (включена АРУ тюнера или приём остановлен) — "
                                    "поправка не изменена.")
                return
            self.scal.setValue(round(known.value() - self._last_dbm_raw, 1))

    def _grp_radio(self, body):
        f = QFormLayout(body)
        row = QHBoxLayout()
        self.bw = QSlider(Qt.Horizontal)
        self.bw.valueChanged.connect(self._bw_changed)
        self.bw.setToolTip(TIPS["bw"])
        self.bw_lbl = QLabel("")
        self.bw_lbl.setMinimumWidth(70)
        row.addWidget(self.bw, 1)
        row.addWidget(self.bw_lbl)
        f.addRow("Полоса:", row)
        self.step = QComboBox()
        for _v, n in STEPS:
            self.step.addItem(n)
        self.step.currentIndexChanged.connect(self._step_changed)
        self.step.setToolTip(TIPS["step"])
        f.addRow("Шаг:", self.step)
        row = QHBoxLayout()
        self.cb_snap = QCheckBox("Привязка к шагу")
        self.cb_snap.setChecked(self.s["snap"])
        self.cb_snap.setToolTip(TIPS["snap"])
        self.cb_snap.toggled.connect(lambda v: self.s.__setitem__("snap", v))
        row.addWidget(self.cb_snap)
        self.cb_lock = QCheckBox("Замок частоты")
        self.cb_lock.setChecked(self.s["lock"])
        self.cb_lock.setToolTip(TIPS["lock"])
        self.cb_lock.toggled.connect(self._lock_changed)
        row.addWidget(self.cb_lock)
        f.addRow(row)
        self.wfm_box = QWidget()
        wf = QFormLayout(self.wfm_box)
        wf.setContentsMargins(0, 0, 0, 0)
        self.deemph = QComboBox()
        self.deemph.addItem("50 мкс (Европа, Россия)", 50)
        self.deemph.addItem("75 мкс (США)", 75)
        self.deemph.setCurrentIndex(0 if self.s["deemph"] == 50 else 1)
        self.deemph.currentIndexChanged.connect(
            lambda _i: self.engine.configure(deemph_tau=self.deemph.currentData() * 1e-6))
        self.deemph.setToolTip(TIPS["deemph"])
        wf.addRow("Предыскажения:", self.deemph)
        self.cb_stereo = QCheckBox("Стерео")
        self.cb_stereo.setChecked(self.s["stereo"])
        self.cb_stereo.toggled.connect(lambda v: self.engine.configure(stereo=v))
        self.cb_stereo.setToolTip(TIPS["stereo"])
        wf.addRow(self.cb_stereo)
        f.addRow(self.wfm_box)
        self.cw_box = QWidget()
        cf = QFormLayout(self.cw_box)
        cf.setContentsMargins(0, 0, 0, 0)
        self.cw_tone = QSpinBox()
        self.cw_tone.setRange(300, 1500)
        self.cw_tone.setSingleStep(50)
        self.cw_tone.setSuffix(" Гц")
        self.cw_tone.setValue(int(self.s["cw_tone"]))
        self.cw_tone.setToolTip(TIPS["cw_tone"])
        self.cw_tone.valueChanged.connect(lambda v: self.engine.configure(cw_tone=float(v)))
        cf.addRow("Тон CW:", self.cw_tone)
        f.addRow(self.cw_box)
        b = QPushButton("Центрировать спектр на VFO")
        b.clicked.connect(lambda: self._recenter(force=True))
        b.setToolTip(TIPS["center"])
        f.addRow(b)

    def _grp_agc(self, body):
        f = QFormLayout(body)
        row = QHBoxLayout()
        self.cb_agc_on = QCheckBox("Включена")
        self.cb_agc_on.setChecked(self.s["agc_on"])
        self.cb_agc_on.setToolTip(TIPS["agc_on"])
        row.addWidget(self.cb_agc_on)
        self.cb_hang = QCheckBox("Удержание")
        self.cb_hang.setChecked(self.s["agc_hang"])
        self.cb_hang.setToolTip(TIPS["agc_hang"])
        row.addWidget(self.cb_hang)
        f.addRow(row)

        def slider(lo, hi, val, tip, fmt):
            r = QHBoxLayout()
            s = QSlider(Qt.Horizontal)
            s.setRange(lo, hi)
            s.setValue(int(val))
            s.setToolTip(tip)
            lbl = QLabel(fmt(int(val)))
            lbl.setMinimumWidth(62)
            s.valueChanged.connect(lambda v: lbl.setText(fmt(v)))
            r.addWidget(s, 1)
            r.addWidget(lbl)
            return s, r

        self.agc_thr, r = slider(-140, -20, self.s["agc_thr"], TIPS["agc_thr"], lambda v: f"{v} dB")
        f.addRow("Порог:", r)
        self.agc_decay, r = slider(20, 3000, self.s["agc_decay"], TIPS["agc_decay"], lambda v: f"{v} мс")
        f.addRow("Спад:", r)
        self.agc_slope, r = slider(0, 10, self.s["agc_slope"], TIPS["agc_slope"], lambda v: f"{v} dB")
        f.addRow("Наклон:", r)
        self.if_gain, r = slider(0, 100, self.s["if_gain"], TIPS["if_gain"], lambda v: f"+{v} dB")
        f.addRow("Усиление ПЧ:", r)
        self.agc_lbl = QLabel("")
        self.agc_lbl.setObjectName("dim")
        self.agc_lbl.setWordWrap(True)
        f.addRow(self.agc_lbl)
        for w in (self.cb_agc_on, self.cb_hang):
            w.toggled.connect(lambda _v: self._apply_dsp())
        for w in (self.agc_thr, self.agc_decay, self.agc_slope, self.if_gain):
            w.valueChanged.connect(lambda _v: self._apply_dsp())

    def _grp_audio(self, body):
        f = QFormLayout(body)
        row = QHBoxLayout()
        self.cb_nr = QCheckBox("Шумоподавитель")
        self.cb_nr.setChecked(self.s["nr"])
        self.cb_nr.setToolTip(TIPS["nr"])
        row.addWidget(self.cb_nr)
        self.nr_str = QSlider(Qt.Horizontal)
        self.nr_str.setRange(5, 40)
        self.nr_str.setValue(int(self.s["nr_strength"]))
        self.nr_str.setToolTip("Сила шумоподавителя: больше — тише шипение, но звук «булькает»")
        row.addWidget(self.nr_str, 1)
        f.addRow(row)
        self.ahp = QComboBox()
        for v, n in HP_CHOICES:
            self.ahp.addItem(n, v)
        self.ahp.setCurrentIndex(max(0, self.ahp.findData(self.s["ahp"])))
        self.ahp.setToolTip(TIPS["ahp"])
        f.addRow("Срез низких:", self.ahp)
        self.alp = QComboBox()
        for v, n in LP_CHOICES:
            self.alp.addItem(n, v)
        self.alp.setCurrentIndex(max(0, self.alp.findData(self.s["alp"])))
        self.alp.setToolTip(TIPS["alp"])
        f.addRow("Срез высоких:", self.alp)
        self.cb_nr.toggled.connect(lambda _v: self._apply_dsp())
        self.nr_str.valueChanged.connect(lambda _v: self._apply_dsp())
        self.ahp.currentIndexChanged.connect(lambda _i: self._apply_dsp(audio=True))
        self.alp.currentIndexChanged.connect(lambda _i: self._apply_dsp(audio=True))

    def _grp_rf(self, body):
        f = QFormLayout(body)
        row = QHBoxLayout()
        self.cb_nb = QCheckBox("Бланкер импульсов")
        self.cb_nb.setChecked(self.s["nb"])
        self.cb_nb.setToolTip(TIPS["nb"])
        row.addWidget(self.cb_nb)
        self.nb_thr = QSlider(Qt.Horizontal)
        self.nb_thr.setRange(3, 30)
        self.nb_thr.setValue(int(self.s["nb_thr"]))
        self.nb_thr.setToolTip("Порог бланкера: во сколько раз импульс выше среднего уровня эфира. "
                               "Меньше — режет больше помех, но может задевать сильные станции.")
        row.addWidget(self.nb_thr, 1)
        f.addRow(row)
        self.cb_dc = QCheckBox("Убирать пик постоянной составляющей")
        self.cb_dc.setChecked(self.s["dc"])
        self.cb_dc.setToolTip(TIPS["dc"])
        f.addRow(self.cb_dc)
        self.cb_iq = QCheckBox("Коррекция дисбаланса I/Q")
        self.cb_iq.setChecked(self.s["iqcorr"])
        self.cb_iq.setToolTip(TIPS["iqcorr"])
        f.addRow(self.cb_iq)
        self.rf_lbl = QLabel("")
        self.rf_lbl.setObjectName("dim")
        self.rf_lbl.setWordWrap(True)
        f.addRow(self.rf_lbl)
        for w in (self.cb_nb, self.cb_dc, self.cb_iq):
            w.toggled.connect(lambda _v: self._apply_dsp())
        self.nb_thr.valueChanged.connect(lambda _v: self._apply_dsp())

    def _grp_rds(self, body):
        v = QVBoxLayout(body)
        self.cb_rds = QCheckBox("Декодировать RDS")
        self.cb_rds.setChecked(self.s["rds"])
        self.cb_rds.setToolTip(TIPS["rds"])
        self.cb_rds.toggled.connect(lambda on: self.engine.configure(rds_on=on))
        v.addWidget(self.cb_rds)
        self.rds_info = QLabel("Нет данных RDS")
        self.rds_info.setWordWrap(True)
        self.rds_info.setTextFormat(Qt.RichText)
        v.addWidget(self.rds_info)

    def _grp_dmr(self, body):
        v = QVBoxLayout(body)
        row = QHBoxLayout()
        row.addWidget(QLabel("Слот:"))
        self.dmr_slot = QComboBox()
        self.dmr_slot.addItems(["оба", "1", "2"])
        self.dmr_slot.setCurrentIndex(self.s["dmr_slot"])
        self.dmr_slot.currentIndexChanged.connect(lambda i: self.engine.configure(dmr_slot=i))
        self.dmr_slot.setToolTip(TIPS["dmr_slot"])
        row.addWidget(self.dmr_slot, 1)
        v.addLayout(row)
        self.dmr_s1 = QLabel("Слот 1: —")
        self.dmr_s2 = QLabel("Слот 2: —")
        for lbl in (self.dmr_s1, self.dmr_s2):
            lbl.setWordWrap(True)
            v.addWidget(lbl)
        self.dmr_log = QListWidget()
        self.dmr_log.setMinimumHeight(140)
        self.dmr_log.setWordWrap(True)
        self.dmr_log.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.dmr_log.setToolTip("Журнал вызовов DMR: время, слот, кто → кому")
        v.addWidget(self.dmr_log)
        hint = QLabel("Цифровые переговоры обычно на любительских ретрансляторах 438–440 МГц и служебных "
                      "146–174 / 400–470 МГц. Не знаете частоту — «Поиск DMR» ниже. Подробнее — ❔ Справка.")
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        v.addWidget(hint)

    def _grp_freqs(self, body):
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 0, 0)
        self.fm = FreqManager(self.s["freqs"], BANDS, current=self._current_entry)
        self.fm.tuneRequested.connect(self._fm_pick)
        self.fm.changed.connect(lambda: self.s.__setitem__("freqs", self.fm.entries))
        v.addWidget(self.fm)

    def _grp_record(self, body):
        v = QVBoxLayout(body)
        self.btn_rec = QPushButton("● Звук в WAV")
        self.btn_rec.setCheckable(True)
        self.btn_rec.toggled.connect(self._record)
        self.btn_rec.setToolTip(TIPS["rec"])
        v.addWidget(self.btn_rec)
        row = QHBoxLayout()
        self.btn_iqrec = QPushButton("● Базовая полоса IQ")
        self.btn_iqrec.setCheckable(True)
        self.btn_iqrec.toggled.connect(self._iq_record)
        self.btn_iqrec.setToolTip(TIPS["iqrec"])
        row.addWidget(self.btn_iqrec, 1)
        self.iq_fmt = QComboBox()
        self.iq_fmt.addItem("WAV 16 бит", "wav")
        self.iq_fmt.addItem("cu8 (rtl_sdr)", "cu8")
        self.iq_fmt.setCurrentIndex(max(0, self.iq_fmt.findData(self.s["iq_fmt"])))
        self.iq_fmt.setToolTip("WAV — открывается в SDR#, SDR++, HDSDR; cu8 — вдвое меньше, как у rtl_sdr")
        row.addWidget(self.iq_fmt)
        v.addLayout(row)
        self.rec_lbl = QLabel("")
        self.rec_lbl.setObjectName("dim")
        self.rec_lbl.setWordWrap(True)
        v.addWidget(self.rec_lbl)

    def _grp_spectrum(self, body):
        gl = QVBoxLayout(body)
        row = QHBoxLayout()
        row.addWidget(QLabel("Масштаб:"))
        self.zoom = QSlider(Qt.Horizontal)
        self.zoom.setRange(0, 60)          # ×2^(v/10): 1 … 64
        self.zoom.setValue(int(self.s["zoom"]))
        self.zoom.setToolTip(TIPS["zoom"])
        self.zoom.valueChanged.connect(self._zoom_changed)
        row.addWidget(self.zoom, 1)
        self.zoom_lbl = QLabel("")
        self.zoom_lbl.setMinimumWidth(44)
        row.addWidget(self.zoom_lbl)
        gl.addLayout(row)
        self.spec_ctl = SpectrumControls([self.spec], lambda n: setattr(self.engine, "nfft", n), key="radio",
                                         nfft_default=8192, extended=True)
        gl.addWidget(self.spec_ctl)
        row = QHBoxLayout()
        self.cb_overview = QCheckBox("Обзор всего диапазона")
        self.cb_overview.setChecked(bool(self.s["overview"]))
        self.cb_overview.setToolTip("Полоса над спектром: весь диапазон приёмника 0,5–1766 МГц, классы волн "
                                    "(километровые … дециметровые) и кто где работает. Щелчок — перейти на частоту.")
        self.cb_overview.toggled.connect(lambda on: self._toggle_box("overview", self.overview, on))
        self.cb_sidebar = QCheckBox("Ручки справа")
        self.cb_sidebar.setChecked(bool(self.s["sidebar"]))
        self.cb_sidebar.setToolTip("Вертикальные ручки справа от спектра, как в SDR#: масштаб, контраст, "
                                   "диапазон шкалы, смещение (копии регуляторов этой панели)")
        self.cb_sidebar.toggled.connect(lambda on: self._toggle_box("sidebar", self.sidebar, on))
        row.addWidget(self.cb_overview)
        row.addWidget(self.cb_sidebar)
        row.addStretch(1)
        gl.addLayout(row)
        self._zoom_changed(self.zoom.value())

    # ================= приём =================
    def start(self):
        src = self.src_combo.currentData()
        if src == "none" or src is None:
            self.btn_start.setChecked(False)
            return
        is_file = str(src).startswith("file:")
        if is_file:
            path = src[5:]
            fs, center = (self._file[1], self._file[2]) if self._file and self._file[0] == path else (None, None)
            try:
                self.engine.start(src, 0, None, file_fs=fs, file_center=center)
            except Exception as e:  # noqa: BLE001
                self.btn_start.setChecked(False)
                QMessageBox.warning(self, "Файл IQ", f"Не удалось открыть файл:\n{e}")
                return
            rate = self.engine.fs
            self.rate.blockSignals(True)
            i = self.rate.findData(rate)
            if i >= 0:
                self.rate.setCurrentIndex(i)
            self.rate.blockSignals(False)
            c = self.engine.center
            vfo = self.fdisp.value
            if abs(vfo - c) > rate / 2 * 0.95:
                self.fdisp.set_value(c + rate * 0.1)
                self.s["vfo"] = self.fdisp.value
        else:
            self.engine.fs = int(self.rate.currentData())
            self.engine.fixed_center = False
            self.engine.set_rate(self.engine.fs)
            self.engine.start(src, 0, None if self.cb_agc.isChecked() else self._gain_tenths())
            self.engine.cmd("ppm", self.ppm.value())
            if self.cb_bias.isChecked():
                self.engine.cmd("bias", True)
        for w in (self.rate, self.gain, self.cb_agc, self.ppm, self.cb_bias):
            w.setEnabled(not is_file)
        if not is_file:
            self.gain.setEnabled(not self.cb_agc.isChecked())
        self.file_pos.setEnabled(is_file)
        self.btn_start.setChecked(True)
        self.btn_start.setText("■")
        self._apply_dsp()
        self._push_vfo()

    def stop(self):
        self.engine.stop()
        self.btn_start.setChecked(False)
        self.btn_start.setText("▶")
        for b in (self.btn_rec, self.btn_iqrec):
            b.blockSignals(True)
            b.setChecked(False)
            b.blockSignals(False)
        self.btn_rec.setText("● Звук в WAV")
        self.btn_iqrec.setText("● Базовая полоса IQ")

    def _open_iq_file(self):
        from .. import iqfile
        path, _ = QFileDialog.getOpenFileName(self, "Файл IQ", paths.audio_dir() if hasattr(paths, "audio_dir") else "",
                                              "Файлы IQ (" + " ".join("*" + e for e in iqfile.EXTS) + ");;Все (*)")
        if not path:
            return
        fs, center = iqfile.parse_name(path)
        if os.path.splitext(path)[1].lower() != ".wav" and not fs:
            v, ok = QInputDialog.getDouble(self, "Файл IQ", "Частота дискретизации, кГц:", 2400.0, 50.0, 20000.0, 3)
            if not ok:
                return
            fs = int(v * 1000)
        if not center:
            v, ok = QInputDialog.getDouble(self, "Файл IQ", "Центральная частота записи, МГц\n"
                                           "(в имени файла её нет):", 100.0, 0.1, 2000.0, 6)
            if not ok:
                return
            center = v * 1e6
        self._file = (path, fs, center)
        sid = "file:" + path
        i = self.src_combo.findData(sid)
        if i < 0:
            self.src_combo.addItem("Файл IQ: " + os.path.basename(path), sid)
            i = self.src_combo.count() - 1
        self.src_combo.setCurrentIndex(i)
        files = [p for p in (self.s.get("files") or []) if p != path]
        self.s["files"] = ([path] + files)[:8]
        self.start()

    def _file_seek(self):
        inf = getattr(self, "_last_file", None)
        if inf and self.engine.src is not None:
            self.engine.cmd("seek", self.file_pos.value() / 1000 * inf[1])

    def _scan_tune(self, f, mode):
        self._set_mode(mode)
        self._set_vfo(f)
        self._recenter(force=True)
        self._push_vfo()

    def shutdown(self):
        try:
            self.dmr_scan.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self.engine.stop()
        self._save()

    def _on_opened(self, info):
        if not info.get("file"):
            self.gains = info["gains"] or self.gains
            self.gain.setRange(0, len(self.gains) - 1)
            self._gain_changed()
        self.dev_info = f"{info['label']} · {info['tuner']}"

    def _apply_dsp(self, audio=False):
        """Перенести настройки АРУ, звука и обработки сигнала в движок."""
        agc_on = self.cb_agc_on.isChecked()
        self.agc_thr.setEnabled(agc_on)
        self.agc_decay.setEnabled(agc_on)
        self.agc_slope.setEnabled(agc_on)
        self.cb_hang.setEnabled(agc_on)
        self.if_gain.setEnabled(not agc_on)
        self.engine.configure(agc={"on": agc_on, "hang": self.cb_hang.isChecked(),
                                   "threshold_db": float(self.agc_thr.value()),
                                   "decay_ms": float(self.agc_decay.value()),
                                   "slope_db": float(self.agc_slope.value()),
                                   "manual_db": float(self.if_gain.value())},
                              audio_nr=self.cb_nr.isChecked(), nr_strength=self.nr_str.value() / 10.0,
                              cw_tone=float(self.cw_tone.value()), rds_on=self.cb_rds.isChecked())
        d = self.engine.demod
        if audio or (d.audio_hp, d.audio_lp) != (self.ahp.currentData(), self.alp.currentData()):
            self.engine.configure(audio_hp=self.ahp.currentData(), audio_lp=self.alp.currentData())
        fe = self.engine.front
        if fe is not None:
            fe.nb = self.cb_nb.isChecked()
            fe.nb_thr = float(self.nb_thr.value())
            fe.dc = self.cb_dc.isChecked()
            fe.iq = self.cb_iq.isChecked()

    # ================= частота =================
    def _mode(self):
        b = self.mode_btns.checkedButton()
        return b.text() if b else "WFM"

    def _set_mode(self, m, initial=False, bw=None):
        for b in self.mode_btns.buttons():
            b.setChecked(b.text() == m)
        bw_def, (lo, hi), step = MODES[m]
        bw = bw or self.s["bw"].get(m, bw_def)
        bw = min(max(bw, lo), hi)
        self.bw.blockSignals(True)
        self.bw.setRange(int(lo / 100), int(hi / 100))
        self.bw.setValue(int(round(bw / 100)))
        self.bw.setEnabled(lo != hi)
        self.bw.blockSignals(False)
        self.bw_lbl.setText(self._fmt_bw(bw))
        self.s["bw"][m] = bw
        st = self.s["step"].get(m, step)
        idx = min(range(len(STEPS)), key=lambda i: abs(STEPS[i][0] - st))
        self.step.blockSignals(True)
        self.step.setCurrentIndex(idx)
        self.step.blockSignals(False)
        self.sections["dmr"].setVisible(m == "DMR")
        self.sections["rds"].setVisible(m == "WFM")
        self.rds_bar.setVisible(m == "WFM")
        self.mode_hint.setText(MODE_HINT[m])
        self.mode_hint.setToolTip(MODE_HELP[m])
        self.wfm_box.setVisible(m == "WFM")
        self.cw_box.setVisible(m == "CW")
        if m != "WFM":
            self.stereo_lbl.setText("")
        self.engine.configure(mode=m, bw=bw)
        if not initial:
            self._push_vfo()
        else:
            self._markers()

    @staticmethod
    def _fmt_bw(bw):
        return f"{bw / 1000:.1f} кГц" if bw < 100000 else f"{bw / 1000:.0f} кГц"

    def _bw_changed(self, v):
        bw = v * 100.0
        self.s["bw"][self._mode()] = bw
        self.bw_lbl.setText(self._fmt_bw(bw))
        self.engine.configure(bw=bw)
        self._markers()

    def _step_changed(self, i):
        self.s["step"][self._mode()] = STEPS[i][0]

    def _step_hz(self):
        return STEPS[self.step.currentIndex()][0]

    def _locked(self):
        return self.cb_lock.isChecked()

    def _lock_changed(self, on):
        self.s["lock"] = on
        self.fdisp.setEnabled(not on)

    def _snap(self, f):
        if not self.cb_snap.isChecked():
            return float(round(f))
        st = self._step_hz()
        return round(f / st) * st

    def _step(self, d):
        st = self._step_hz()
        f = self.fdisp.value + d * st
        if self.cb_snap.isChecked():
            f = round(f / st) * st
        self._set_vfo(f)

    def _spectrum_click(self, f):
        if self._locked():
            return
        self._set_vfo(self._snap(f))

    def _band_drag(self, df):
        """Полосу демодулятора тянут мышью: VFO едет за курсором (с привязкой к шагу)."""
        if self._locked():
            return
        now = time.monotonic()
        if now - self._drag_t > 0.4:
            self._drag_f = self.fdisp.value
        self._drag_t = now
        self._drag_f += df
        f = self._snap(self._drag_f) if self._step_hz() <= 25000 else self._drag_f
        self._set_vfo(f)

    def _edge_drag(self, side, f):
        """Край подсвеченной полосы тянут мышью — меняется ширина полосы демодулятора."""
        if not self.bw.isEnabled():
            return
        vfo = self.fdisp.value
        m = self._mode()
        if m == "USB":
            bw = (f - vfo - SSB_LOW) if side == 1 else self.engine.demod.bw
        elif m == "LSB":
            bw = (vfo - SSB_LOW - f) if side == 0 else self.engine.demod.bw
        else:
            bw = 2 * abs(f - vfo)
        self.bw.setValue(int(round(bw / 100)))

    def _set_vfo(self, f, push=True):
        if self.engine.fixed_center:
            fs = self.engine.fs
            c = self.engine.center
            f = min(max(f, c - fs / 2 * 0.98), c + fs / 2 * 0.98)
        self.fdisp.set_value(f)
        self.s["vfo"] = self.fdisp.value
        if push:
            self._push_vfo()
        else:
            self._markers()

    def _recenter(self, force=False):
        if self.engine.fixed_center:
            return
        vfo = self.fdisp.value
        fs = self.engine.fs
        bw = self.engine.demod.bw
        margin = fs / 2 - bw / 2 - 0.05 * fs
        c0 = self.engine.center
        if force:
            # сигнал не на постоянной составляющей: центр чуть ниже VFO
            c = min(max(vfo - 0.15 * fs, 0.5e6 + fs / 2), 1766e6 - fs / 2)
        elif abs(vfo - c0) > margin:
            # плавно: подвинуть полосу ровно настолько, чтобы VFO остался у края, без прыжка на страницу
            over = (vfo - c0) - margin if vfo > c0 else (vfo - c0) + margin
            c = max(c0 + over, 0.5e6 + fs / 2)
            if abs(vfo - c) > fs / 2:          # большой скачок (закладка, ввод частоты) — как раньше
                c = min(max(vfo - 0.15 * fs, 0.5e6 + fs / 2), 1766e6 - fs / 2)
        else:
            return
        self.engine.set_center(c)
        self.s["center"] = c

    def _pan(self, df):
        """Перетаскивание спектра: копим сдвиг и перестраиваем центр не чаще 25 раз/с."""
        if self.engine.fixed_center or self._locked():
            return
        self._pan_df += df
        if not self._pan_timer.isActive():
            self._pan_timer.start(40)

    def _apply_pan(self):
        df, self._pan_df = self._pan_df, 0.0
        if abs(df) < 1:
            return
        fs = self.engine.fs
        c = max(self.engine.center + df, 0.5e6 + fs / 2)
        c = min(c, 1766e6 - fs / 2)
        self.engine.set_center(c)
        self.s["center"] = c
        vfo = self.fdisp.value
        if abs(vfo - c) > fs / 2 - self.engine.demod.bw / 2:
            # VFO ушёл за край — тянем его вместе с полосой
            edge = c + (fs / 2 - self.engine.demod.bw) * (1 if vfo > c else -1)
            self.fdisp.set_value(edge)
            self.s["vfo"] = self.fdisp.value
        self.engine.configure(offset=self.fdisp.value - c)
        self._markers()

    def _toggle_box(self, key, w, on):
        self.s[key] = bool(on)
        if getattr(self, key, None) is not None:
            w.setVisible(bool(on))

    def _overview_jump(self, f):
        """Щелчок/перетаскивание по обзорной полосе: переход на частоту (не чаще 20 раз в секунду)."""
        if self._locked():
            return
        self._jump_f = f
        if not self._jump_timer.isActive():
            self._jump_timer.start(50)

    def _apply_jump(self):
        f, self._jump_f = self._jump_f, None
        if f is None:
            return
        if self.engine.fixed_center:
            c, fs = self.engine.center, self.engine.fs
            if abs(f - c) > fs / 2:      # файл IQ: за пределы записанной полосы не уйти
                self.sb.setText("Идёт файл IQ: перейти можно только внутри записанной полосы "
                                f"{(c - fs / 2) / 1e6:.3f}–{(c + fs / 2) / 1e6:.3f} МГц")
                return
            self._set_vfo(f)
            return
        self._set_vfo(self._snap(f))

    def _overview_sync(self):
        ov = getattr(self, "overview", None)
        if ov is None:
            return
        c, fs = self.engine.center, self.engine.fs
        half = fs / 2 / max(self.spec.zoom, 1.0)
        zc = self.fdisp.value if self.spec.zoom > 1.0 else c
        zc = min(max(zc, c - fs / 2 + half), c + fs / 2 - half)
        ov.set_view(zc - half, zc + half, self.fdisp.value)

    def _bw_wheel(self, d):
        if not self.bw.isEnabled():
            return
        v = self.bw.value()
        step = max(1, int(round(v * 0.1)))
        self.bw.setValue(v + d * step)

    def _zoom_changed(self, v):
        z = 2 ** (v / 10.0)
        self.s["zoom"] = v
        self.spec.zoom = z
        self.zoom_lbl.setText(f"×{z:.1f}" if z < 10 else f"×{z:.0f}")
        self._markers()

    def _push_vfo(self):
        self._recenter()
        self.engine.configure(offset=self.fdisp.value - self.engine.center)
        self._markers()

    def _band(self):
        vfo = self.fdisp.value
        bw = self.engine.demod.bw
        m = self._mode()
        if m == "USB":
            return vfo + SSB_LOW, vfo + SSB_LOW + bw
        if m == "LSB":
            return vfo - SSB_LOW - bw, vfo - SSB_LOW
        return vfo - bw / 2, vfo + bw / 2

    def _markers(self):
        vfo = self.fdisp.value
        band = self._band()
        acc = self._accent()
        self.spec.bands = [(band[0], band[1], acc)]
        self.spec.markers = [(vfo, "#ffd84d", f"{vfo / 1e6:.4f}")]
        self.spec.zoom_center = vfo
        self.center_lbl.setText(f"Центр приёмника: {self.engine.center / 1e6:.4f} МГц"
                                + (" (файл)" if self.engine.fixed_center else ""))
        self._overview_sync()
        self.spec.update()

    def _accent(self):
        from ..themes import THEMES
        return THEMES.get(self.theme_name, THEMES["Тёмная"])["accent"]

    def _rate_changed(self):
        self.engine.set_rate(int(self.rate.currentData()))
        self._push_vfo()

    def _gain_tenths(self):
        return self.gains[min(self.gain.value(), len(self.gains) - 1)]

    def _gain_changed(self):
        agc = self.cb_agc.isChecked()
        self.gain.setEnabled(not agc)
        if agc:
            self.gain_lbl.setText("АРУ")
            self.engine.cmd("gain", None)
        else:
            t = self._gain_tenths()
            self.gain_lbl.setText(f"{t / 10:.1f} dB")
            self.engine.cmd("gain", t)

    def _sql_changed(self, v):
        self.engine.configure(squelch_db=-200.0 if v <= -120 else float(v))
        self.sql_lbl.setText("выкл" if v <= -120 else f"{v} dB")

    # ================= менеджер частот =================
    def _current_entry(self):
        return {"name": f"{self.fdisp.value / 1e6:.4f} МГц", "freq": self.fdisp.value, "mode": self._mode(),
                "bw": self.engine.demod.bw, "group": "Мои"}

    def _fm_pick(self, e):
        self._set_mode(e["mode"], bw=e.get("bw"))
        self._set_vfo(e["freq"])
        self._recenter(force=True)
        self._push_vfo()

    # ================= запись =================
    def _record(self, on):
        if on:
            folder = paths.audio_dir()
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, time.strftime(f"radio_{self.fdisp.value / 1e6:.4f}MHz_%Y%m%d_%H%M%S.wav"))
            self.engine.start_record(path)
            self.rec_lbl.setText(path)
            self.btn_rec.setText("■ Остановить звук")
        else:
            self.engine.stop_record()
            self.btn_rec.setText("● Звук в WAV")

    def _iq_record(self, on):
        from .. import iqfile
        if on:
            if not self.engine.running():
                self.btn_iqrec.blockSignals(True)
                self.btn_iqrec.setChecked(False)
                self.btn_iqrec.blockSignals(False)
                self.statusBar().showMessage("Запись IQ — во время приёма", 4000)
                return
            folder = os.path.join(paths.audio_dir(), "IQ")
            os.makedirs(folder, exist_ok=True)
            fmt = self.iq_fmt.currentData()
            self.s["iq_fmt"] = fmt
            path = iqfile.file_name(folder, self.engine.center, self.engine.fs, fmt)
            self.engine.start_iq_record(path, fmt)
            self._iq_path = path
            self.btn_iqrec.setText("■ Остановить IQ")
            self.iq_fmt.setEnabled(False)
        else:
            self.engine.stop_iq_record()
            self.btn_iqrec.setText("● Базовая полоса IQ")
            self.iq_fmt.setEnabled(True)

    # ================= события движка =================
    def _on_spectrum(self, d):
        n = d["psd"].size
        freqs = d["center"] + (np.arange(n) - n // 2) * (d["fs"] / n)
        self.spec.set_data(freqs, d["psd"])

    def _on_level(self, ch_db, audio_rms, stereo):
        txt = f"{ch_db:.0f} dBFS"
        self._last_dbm_raw = None
        if not self.cb_agc.isChecked() and not self.engine.fixed_center:
            raw = ch_db - 10.0 - self._gain_tenths() / 10.0
            self._last_dbm_raw = raw
            dbm = raw + float(self.s.get("smeter_cal", 0))
            txt = f"{s_units(dbm, self.fdisp.value)} · {dbm:.0f} dBm · {txt}"
        self.lvl.set_value(ch_db, txt)
        self.stereo_lbl.setText("СТЕРЕО" if stereo else ("МОНО" if self._mode() == "WFM" else ""))

    def _on_info(self, inf):
        clip = inf.get("clip", 0.0)
        self.ovl_lbl.setText("ПЕРЕГРУЗКА" if clip > 1e-3 else "")
        parts = []
        if inf.get("snr") is not None:
            parts.append(f"С/Ш ≈ {inf['snr']:.0f} dB")
        if inf.get("agc_db") is not None:
            parts.append(f"АРУ +{inf['agc_db']:.0f} dB")
        if inf.get("latency"):
            parts.append(f"задержка звука {inf['latency'] * 1000:.0f} мс")
        self._info_txt = " · ".join(parts)
        if inf.get("agc_db") is not None:
            self.agc_lbl.setText(f"Сейчас усиление {inf['agc_db']:+.0f} dB"
                                 + ("" if self.cb_agc_on.isChecked() else " (вручную)"))
        else:
            self.agc_lbl.setText("АРУ работает в AM, SSB и CW; в ЧМ громкость задаёт девиация")
        rf = []
        if self.cb_nb.isChecked():
            rf.append(f"бланкер вырезает {inf.get('nb', 0) * 100:.2f} % отсчётов")
        if self.cb_iq.isChecked() and inf.get("iq_err"):
            g, ph = inf["iq_err"]
            rf.append(f"дисбаланс I/Q: {g:+.2f} dB, {ph:+.2f}°")
        self.rf_lbl.setText("; ".join(rf))
        if "file" in inf:
            pos, tot = inf["file"]
            self._last_file = (pos, tot)
            if not self.file_pos.isSliderDown() and tot > 0:
                self.file_pos.setValue(int(pos / tot * 1000))
            self.file_lbl.setText(f"Файл: {pos:.1f} / {tot:.1f} с, по кругу")
            self.file_lbl.setVisible(True)
        elif self.file_lbl.isVisible():
            self.file_lbl.setVisible(False)
        if "iqrec" in inf:
            sec, nbytes = inf["iqrec"]
            self.rec_lbl.setText(f"IQ: {sec:.0f} с, {nbytes / 1e6:.0f} МБ — {getattr(self, '_iq_path', '')}")
        rds = inf.get("rds")
        if rds is not None:
            self._show_rds(rds)

    def _show_rds(self, r):
        ps = r["ps"] or ""
        self.rds_ps.setText(ps if ps else ("RDS…" if r["synced"] else "RDS —"))
        self.rds_rt.setText(r["rt"])
        meta = []
        if r["pi"] is not None:
            meta.append(f"PI {r['pi']:04X}")
        if r["pty_name"] and r["pty"]:
            meta.append(r["pty_name"])
        if r["tp"]:
            meta.append("TP")
        if r["ta"]:
            meta.append("TA")
        self.rds_meta.setText(" · ".join(meta))
        tot = r["ok"] + r["bad"]
        err = f"{r['bad'] / tot * 100:.0f} %" if tot else "—"
        ct = r.get("ct")
        lines = [f"<b>{ps or '—'}</b>", r["rt"] or "<i>радиотекста нет</i>",
                 " · ".join(meta) or "",
                 (f"Время станции: {ct[2]:02d}.{ct[1]:02d}.{ct[0]} {ct[3]:02d}:{ct[4]:02d}" if ct else ""),
                 f"Синхронизация: {'есть' if r['synced'] else 'нет'} · групп {r['groups']} · ошибок {err}"]
        self.rds_info.setText("<br>".join(x for x in lines if x))

    def _on_dmr(self, events, status):
        for ev in events or []:
            slot = ev.get("slot", "?")
            text = ev.get("text") or f"{ev.get('type')} {ev.get('src', '')}→{ev.get('dst', '')}"
            self.dmr_log.insertItem(0, f"{time.strftime('%H:%M:%S')}  TS{slot}  {text}")
        while self.dmr_log.count() > 300:
            self.dmr_log.takeItem(self.dmr_log.count() - 1)
        if status:
            slots = status.get("slots", status)
            for slot, lbl in ((1, self.dmr_s1), (2, self.dmr_s2)):
                st = slots.get(slot) or slots.get(str(slot)) or {}
                if st.get("active"):
                    kind = "группа" if st.get("group") else "частный"
                    enc = " 🔒 шифр" if st.get("encrypted") else ""
                    lbl.setText(f"Слот {slot}: 🔊 {st.get('src', '?')} → {st.get('dst', '?')} ({kind}){enc}")
                else:
                    last = st.get("last") or "—"
                    cc = st.get("cc")
                    lock = "синхр. ✓" if status.get("locked") else "нет синхр."
                    lbl.setText(f"Слот {slot}: {last}" + (f" · CC{cc}" if cc not in (None, -1) else "")
                                + (f" · {lock}" if slot == 1 else ""))

    def _status(self):
        run = self.engine.running()
        info = getattr(self, "dev_info", "")
        drops = self.engine.drops + int(getattr(self.engine.src, "lost", 0) or 0)
        drops = f" · пропуски {drops}" if drops else ""
        extra = getattr(self, "_info_txt", "")
        self.sb.setText(f"{info} · {self.engine.fs / 1e6:.3f} MS/s · центр {self.engine.center / 1e6:.4f} МГц"
                        f"{drops}" + (f" · {extra}" if extra else "") if run else "Остановлено")

    def show_help(self):
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTextBrowser
        d = QDialog(self)
        d.setWindowTitle("Радио — справка")
        d.resize(780, 720)
        v = QVBoxLayout(d)
        tb = QTextBrowser()
        tb.setOpenExternalLinks(True)
        tb.setHtml(HELP_HTML)
        v.addWidget(tb)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(d.reject)
        v.addWidget(bb)
        d.exec()

    def apply_theme(self, t):
        for w in (self.spec, self.fdisp, self.lvl, self.overview, self.sidebar):
            w.set_theme(t)
        self._markers()

    def _save(self):
        s = self.s
        s.update(mode=self._mode(), rate=int(self.rate.currentData()), gain_idx=self.gain.value(),
                 agc=self.cb_agc.isChecked(), ppm=self.ppm.value(), bias=self.cb_bias.isChecked(),
                 volume=self.vol.value(), squelch=self.sql.value(), stereo=self.cb_stereo.isChecked(),
                 deemph=self.deemph.currentData(), vfo=self.fdisp.value,
                 dmr_slot=self.dmr_slot.currentIndex(), freqs=self.fm.entries,
                 agc_on=self.cb_agc_on.isChecked(), agc_hang=self.cb_hang.isChecked(),
                 agc_thr=self.agc_thr.value(), agc_decay=self.agc_decay.value(), agc_slope=self.agc_slope.value(),
                 if_gain=self.if_gain.value(), nr=self.cb_nr.isChecked(), nr_strength=self.nr_str.value(),
                 ahp=self.ahp.currentData(), alp=self.alp.currentData(), cw_tone=self.cw_tone.value(),
                 nb=self.cb_nb.isChecked(), nb_thr=self.nb_thr.value(), dc=self.cb_dc.isChecked(),
                 iqcorr=self.cb_iq.isChecked(), snap=self.cb_snap.isChecked(), lock=self.cb_lock.isChecked(),
                 zoom=self.zoom.value(), rds=self.cb_rds.isChecked(), iq_fmt=self.iq_fmt.currentData())
        if not self.engine.fixed_center:      # при воспроизведении файла центр задаёт файл — не запоминаем
            s["center"] = self.engine.center
        if self.src_combo.currentData() not in ("none", None) and not self.opts.get("source"):
            s["source"] = self.src_combo.currentData()
        if not self.opts.get("shot"):
            settings.save("radio", s)


def rtlsdr_gains():
    from ..source import R828D_GAINS
    return R828D_GAINS
