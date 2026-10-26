"""Окно режима «ТВ» SDR4NOOB."""
import json
import os
import time

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFormLayout,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QMessageBox, QProgressBar, QPushButton, QRadioButton,
                               QScrollArea, QSlider, QSpinBox, QSplitter, QStackedWidget, QVBoxLayout, QWidget)

from . import devices, osplat
from .engine import Engine
from .modebase import ModeWindow
from .standards import PLANS, STANDARDS, channels
from .themes import PALETTES, THEMES, gradient_lut
from .widgets import FreqDisplay, LevelMeter, SpectrumView, VideoView
from . import paths
from . import settings as _settings

RATES = [2048000, 2400000, 2560000, 2880000, 3200000]
STEPS = [(1e3, "1 кГц"), (10e3, "10 кГц"), (50e3, "50 кГц"), (100e3, "100 кГц"), (250e3, "250 кГц"), (1e6, "1 МГц")]
SETTINGS_DIR = _settings.DIR
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")

DEFAULTS = {
    "theme": "Тёмная", "palette": "Ч/Б", "brightness": 0, "contrast": 100, "nr": 30,
    "plan": "RU", "std": "DK", "mode": "analog", "sound": False,
    "freq_analog": 583.25e6, "freq_digital": 546e6, "step": 2,
    "gain_idx": 15, "agc": False, "rtl_agc": False, "ppm": 0, "rate": 2560000, "bias": False,
    "volume": 70, "source": "rtl:0", "field_swap": False, "autostart": True,
}

DIGITAL_NOTE = ("Картинку DVB-T2 режим ТВ не показывает: декодера DVB-T2 в программе нет, а полоса режима — "
                "до 3,2 МГц (это максимум RTL-SDR) при ширине мультиплекса 7,8 МГц. "
                "Поэтому здесь работает измеритель: канал "
                "собирается из нескольких перестроек, считаются уровень и C/N, по циклическому префиксу определяются "
                "режим FFT и защитный интервал. Удобно наводить антенну: C/N выше 15–18 dB хватит телевизору.")


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    return s


class MainWindow(ModeWindow):
    def __init__(self, opts=None):
        super().__init__(opts, "SDR4NOOB — ТВ")
        self.s = load_settings()
        for k in ("mode", "source"):
            if self.opts.get(k):
                self.s[k] = self.opts[k]
        if self.opts.get("theme"):
            self.s["theme"] = self.opts["theme"]
        self.theme_name = self.s["theme"]
        self.engine = Engine()
        self.gains = []
        self.chans = channels(self.s["plan"])
        self.acc = None
        self.last_rgb = None
        self.fps_t = time.time()
        self.fps_n = 0
        self.fps = 0.0
        self.dev_info = ""
        self.last_frame_info = None
        self._build()
        self._connect_engine()
        self._sync_mode_ui()
        self._set_freq(self.s["freq_" + self.s["mode"]], push=False)
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._update_status)
        self.status_timer.start(500)
        if self.s["autostart"] or self.opts.get("source"):
            QTimer.singleShot(200, self.start)

    # ================= построение интерфейса =================
    def _build(self):
        self.video = VideoView()
        self.video.doubleClicked.connect(self.toggle_fullscreen)
        self.dig_spec = SpectrumView(waterfall=False)
        self.dig_spec.tuneRequested.connect(lambda f: None)
        dig = QWidget()
        dl = QVBoxLayout(dig)
        dl.addWidget(self.dig_spec, 3)
        mg = QGridLayout()
        self.d_status = QLabel("—")
        self.d_status.setStyleSheet("font-size: 16pt; font-weight: bold;")
        self.d_cn = LevelMeter(0, 30, "dB")
        self.d_cn.setMinimumHeight(34)
        self.d_level = QLabel("—")
        self.d_flat = QLabel("—")
        self.d_ofdm = QLabel("—")
        self.d_off = QLabel("—")
        mg.addWidget(self.d_status, 0, 0, 1, 4)
        mg.addWidget(QLabel("C/N (сигнал/шум):"), 1, 0)
        mg.addWidget(self.d_cn, 1, 1, 1, 3)
        mg.addWidget(QLabel("Уровень в канале:"), 2, 0)
        mg.addWidget(self.d_level, 2, 1)
        mg.addWidget(QLabel("Неравномерность:"), 2, 2)
        mg.addWidget(self.d_flat, 2, 3)
        mg.addWidget(QLabel("OFDM (по префиксу):"), 3, 0)
        mg.addWidget(self.d_ofdm, 3, 1)
        mg.addWidget(QLabel("Уход частоты:"), 3, 2)
        mg.addWidget(self.d_off, 3, 3)
        note = QLabel(DIGITAL_NOTE)
        note.setObjectName("dim")
        note.setWordWrap(True)
        mg.addWidget(note, 4, 0, 1, 4)
        dl.addLayout(mg, 1)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.video)
        self.stack.addWidget(dig)

        self.spec = SpectrumView(waterfall=True)
        self.spec.tuneRequested.connect(self._spectrum_click)
        self.spec.wheelStep.connect(lambda d: self._step(d))
        self.split = QSplitter(Qt.Vertical)
        self.split.addWidget(self.stack)
        self.split.addWidget(self.spec)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([600, 240])

        panel = QWidget()
        pv = QVBoxLayout(panel)
        pv.setSpacing(4)
        pv.addWidget(self._grp_source())
        pv.addWidget(self._grp_mode())
        pv.addWidget(self._grp_freq())
        pv.addWidget(self._grp_signal())
        pv.addWidget(self._grp_picture())
        pv.addWidget(self._grp_sound())
        pv.addWidget(self._grp_scan())
        g = QGroupBox("Спектр и водопад")
        gl = QVBoxLayout(g)
        from .widgets import SpectrumControls
        self.spec_ctl = SpectrumControls([self.spec, self.dig_spec], lambda n: setattr(self.engine, "nfft", n),
                                         key="tv", nfft_default=1024)
        gl.addWidget(self.spec_ctl)
        pv.addWidget(g)
        pv.addWidget(self._grp_look())
        pv.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidget(panel)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setMinimumWidth(360)
        self.scroll.setMaximumWidth(420)

        main = QSplitter(Qt.Horizontal)
        main.addWidget(self.split)
        main.addWidget(self.scroll)
        main.setStretchFactor(0, 1)
        self.setCentralWidget(main)
        self.sb_left = QLabel("")
        self.statusBar().addWidget(self.sb_left, 1)

        for key, fn in (("PgUp", lambda: self._chan_step(1)), ("PgDown", lambda: self._chan_step(-1)),
                        ("Ctrl+Right", lambda: self._step(1)), ("Ctrl+Left", lambda: self._step(-1)),
                        ("F11", self.toggle_fullscreen), ("F12", self.snapshot), ("Escape", self._exit_fullscreen)):
            QShortcut(QKeySequence(key), self, activated=fn)

    def _grp_source(self):
        g = QGroupBox("Источник")
        lay = QVBoxLayout(g)
        row = QHBoxLayout()
        self.src_combo = QComboBox()
        self._fill_sources()
        self.btn_start = QPushButton("▶ Старт")
        self.btn_start.setCheckable(True)
        self.btn_start.clicked.connect(lambda on: self.start() if on else self.stop())
        row.addWidget(self.src_combo, 1)
        row.addWidget(self.btn_start)
        lay.addLayout(row)
        self.dev_label = QLabel("Драйвер: встроенный librtlsdr (rtl-sdr-blog)")
        self.dev_label.setObjectName("dim")
        self.dev_label.setWordWrap(True)
        lay.addWidget(self.dev_label)
        return g

    def _fill_sources(self):
        devices.fill_source_combo(self.src_combo, self.s["source"], "Тестовый генератор (без антенны)",
                                  keep_missing=False)

    def devices_changed(self):
        if not self.engine.running():
            self.s["source"] = self.src_combo.currentData() or self.s["source"]
            self._fill_sources()

    def _grp_mode(self):
        g = QGroupBox("Режим")
        lay = QVBoxLayout(g)
        self.rb_analog = QRadioButton("Аналоговое ТВ")
        self.rb_digital = QRadioButton("Цифровое ТВ (DVB-T2: измеритель)")
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.rb_analog)
        self.mode_group.addButton(self.rb_digital)
        (self.rb_digital if self.s["mode"] == "digital" else self.rb_analog).setChecked(True)
        self.rb_analog.toggled.connect(lambda _on: self._mode_changed(True))
        lay.addWidget(self.rb_analog)
        lay.addWidget(self.rb_digital)
        self.cb_sound = QCheckBox("Звук вместо картинки")
        self.cb_sound.setToolTip("Несущая звука отстоит на 6,5 МГц от изображения — в полосу режима ТВ "
                                 "(до 3,2 МГц, как у RTL-SDR) одновременно они не помещаются, "
                                 "поэтому либо картинка, либо звук.")
        self.cb_sound.setChecked(self.s["sound"])
        self.cb_sound.toggled.connect(lambda _on: self._mode_changed(False))
        lay.addWidget(self.cb_sound)
        f = QFormLayout()
        self.std_combo = QComboBox()
        for k, st in STANDARDS.items():
            self.std_combo.addItem(st.name, k)
        self.std_combo.setCurrentIndex(self.std_combo.findData(self.s["std"]))
        self.std_combo.currentIndexChanged.connect(self._std_changed)
        self.plan_combo = QComboBox()
        for k, (name, _std, _fn) in PLANS.items():
            self.plan_combo.addItem(name, k)
        self.plan_combo.setCurrentIndex(self.plan_combo.findData(self.s["plan"]))
        self.plan_combo.currentIndexChanged.connect(self._plan_changed)
        f.addRow("Стандарт:", self.std_combo)
        f.addRow("Сетка:", self.plan_combo)
        lay.addLayout(f)
        return g

    def _grp_freq(self):
        g = QGroupBox("Частота")
        lay = QVBoxLayout(g)
        self.freq_what = QLabel("")
        self.freq_what.setObjectName("dim")
        lay.addWidget(self.freq_what)
        self.fdisp = FreqDisplay()
        self.fdisp.changed.connect(lambda v: self._set_freq(v))
        lay.addWidget(self.fdisp)
        row = QHBoxLayout()
        b1 = QPushButton("◀")
        b2 = QPushButton("▶")
        b1.setToolTip("Предыдущий канал (PgDown)")
        b2.setToolTip("Следующий канал (PgUp)")
        b1.clicked.connect(lambda: self._chan_step(-1))
        b2.clicked.connect(lambda: self._chan_step(1))
        self.chan_combo = QComboBox()
        self._fill_channels()
        self.chan_combo.activated.connect(self._chan_selected)
        row.addWidget(b1)
        row.addWidget(self.chan_combo, 1)
        row.addWidget(b2)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        m = QPushButton("−")
        p = QPushButton("+")
        m.clicked.connect(lambda: self._step(-1))
        p.clicked.connect(lambda: self._step(1))
        self.step_combo = QComboBox()
        for _v, name in STEPS:
            self.step_combo.addItem(name)
        self.step_combo.setCurrentIndex(self.s["step"])
        row2.addWidget(QLabel("Шаг:"))
        row2.addWidget(self.step_combo, 1)
        row2.addWidget(m)
        row2.addWidget(p)
        lay.addLayout(row2)
        row3 = QHBoxLayout()
        self.fine = QSlider(Qt.Horizontal)
        self.fine.setRange(-500, 500)
        self.fine.setToolTip("Точная подстройка ±500 кГц от номинала канала")
        self.fine.valueChanged.connect(self._fine_changed)
        self.fine_label = QLabel("±0 кГц")
        self.fine_label.setMinimumWidth(70)
        row3.addWidget(QLabel("Точно:"))
        row3.addWidget(self.fine, 1)
        row3.addWidget(self.fine_label)
        lay.addLayout(row3)
        return g

    def _grp_signal(self):
        g = QGroupBox("Сигнал")
        lay = QVBoxLayout(g)
        row = QHBoxLayout()
        self.gain = QSlider(Qt.Horizontal)
        self.gain.setRange(0, 28)
        self.gain.setValue(self.s["gain_idx"])
        self.gain.valueChanged.connect(self._gain_changed)
        self.gain_label = QLabel("—")
        self.gain_label.setMinimumWidth(64)
        row.addWidget(QLabel("Усиление:"))
        row.addWidget(self.gain, 1)
        row.addWidget(self.gain_label)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        self.cb_agc = QCheckBox("АРУ тюнера")
        self.cb_agc.setChecked(self.s["agc"])
        self.cb_agc.toggled.connect(lambda _v: self._gain_changed())
        self.cb_rtl_agc = QCheckBox("АРУ RTL2832")
        self.cb_rtl_agc.setChecked(self.s["rtl_agc"])
        self.cb_rtl_agc.toggled.connect(lambda v: self.engine.cmd("rtl_agc", v))
        row2.addWidget(self.cb_agc)
        row2.addWidget(self.cb_rtl_agc)
        lay.addLayout(row2)
        f = QFormLayout()
        self.ppm = QSpinBox()
        self.ppm.setRange(-200, 200)
        self.ppm.setValue(self.s["ppm"])
        self.ppm.setSuffix(" ppm")
        self.ppm.valueChanged.connect(lambda v: self.engine.cmd("ppm", v))
        self.rate = QComboBox()
        for r in RATES:
            self.rate.addItem(f"{r / 1e6:.3f} МГц" + ("  (может терять отсчёты)" if r > 2.9e6 else ""), r)
        self.rate.setCurrentIndex(max(0, self.rate.findData(self.s["rate"])))
        self.rate.currentIndexChanged.connect(self._rate_changed)
        self.cb_bias = QCheckBox("Bias-T (питание 4,5 В на антенну)")
        self.cb_bias.setChecked(self.s["bias"])
        self.cb_bias.setToolTip("Только для активных антенн/МШУ! На пассивной антенне с КЗ по постоянному току "
                                "включать нельзя.")
        self.cb_bias.toggled.connect(lambda v: self.engine.cmd("bias", v))
        f.addRow("Коррекция:", self.ppm)
        f.addRow("Полоса (дискр.):", self.rate)
        lay.addLayout(f)
        lay.addWidget(self.cb_bias)
        self.meter = LevelMeter(0, 40, "dB")
        lay.addWidget(QLabel("Сигнал/шум:"))
        lay.addWidget(self.meter)
        self.sync_label = QLabel("—")
        self.sync_label.setObjectName("dim")
        lay.addWidget(self.sync_label)
        return g

    def _grp_picture(self):
        self.grp_pic = g = QGroupBox("Изображение")
        f = QFormLayout(g)
        self.bright = QSlider(Qt.Horizontal)
        self.bright.setRange(-100, 100)
        self.bright.setValue(self.s["brightness"])
        self.contrast = QSlider(Qt.Horizontal)
        self.contrast.setRange(25, 300)
        self.contrast.setValue(self.s["contrast"])
        self.nr = QSlider(Qt.Horizontal)
        self.nr.setRange(0, 90)
        self.nr.setValue(self.s["nr"])
        self.nr.setToolTip("Накопление по кадрам: меньше «снега» на слабом сигнале, но шлейф за движением")
        self.palette = QComboBox()
        self.palette.addItems(list(PALETTES))
        self.palette.setCurrentText(self.s["palette"])
        self.palette.currentTextChanged.connect(self._palette_changed)
        self.cb_swap = QCheckBox("Поменять поля местами")
        self.cb_swap.setChecked(self.s["field_swap"])
        self.cb_swap.toggled.connect(self.engine.set_field_swap)
        self.engine.set_field_swap(self.s["field_swap"])
        f.addRow("Яркость:", self.bright)
        f.addRow("Контраст:", self.contrast)
        f.addRow("Шумоподавление:", self.nr)
        f.addRow("Палитра:", self.palette)
        f.addRow(self.cb_swap)
        row = QHBoxLayout()
        b1 = QPushButton("Снимок (F12)")
        b1.clicked.connect(self.snapshot)
        b2 = QPushButton("Весь экран (F11)")
        b2.clicked.connect(self.toggle_fullscreen)
        row.addWidget(b1)
        row.addWidget(b2)
        f.addRow(row)
        self._palette_changed(self.s["palette"])
        return g

    def _grp_sound(self):
        self.grp_snd = g = QGroupBox("Звук")
        f = QFormLayout(g)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 150)
        self.vol.setValue(self.s["volume"])
        self.vol.valueChanged.connect(lambda v: setattr(self.engine.audio, "volume", v / 100))
        self.engine.audio.volume = self.s["volume"] / 100
        self.alevel = LevelMeter(-40, 0, "dB")
        f.addRow("Громкость:", self.vol)
        f.addRow("Уровень:", self.alevel)
        return g

    def _grp_scan(self):
        g = QGroupBox("Поиск каналов")
        lay = QVBoxLayout(g)
        row = QHBoxLayout()
        self.btn_scan = QPushButton("Искать")
        self.btn_scan.clicked.connect(self.scan)
        self.scan_bar = QProgressBar()
        self.scan_bar.setRange(0, 1)
        self.scan_bar.setValue(0)
        row.addWidget(self.btn_scan)
        row.addWidget(self.scan_bar, 1)
        lay.addLayout(row)
        self.scan_list = QListWidget()
        self.scan_list.setMinimumHeight(110)
        self.scan_list.setWordWrap(True)
        self.scan_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scan_list.itemDoubleClicked.connect(self._scan_pick)
        lay.addWidget(self.scan_list)
        return g

    def _grp_look(self):
        g = QGroupBox("Оформление")
        f = QFormLayout(g)
        self.theme = QComboBox()
        self.theme.addItems(list(THEMES))
        self.theme.setCurrentText(self.s["theme"])
        self.theme.currentTextChanged.connect(self.set_theme)
        f.addRow("Цветовая схема:", self.theme)
        return g

    # ================= движок =================
    def _connect_engine(self):
        e = self.engine
        e.frame.connect(self._on_frame)
        e.spectrum.connect(self._on_spectrum)
        e.digital.connect(self._on_digital)
        e.opened.connect(self._on_opened)
        e.error.connect(self._on_error)
        e.scan_progress.connect(lambda i, n, f: (self.scan_bar.setRange(0, n), self.scan_bar.setValue(i)))
        e.scan_done.connect(self._on_scan_done)
        e.audio_level.connect(lambda v: self.alevel.set_value(20 * np.log10(max(v, 1e-5))))

    def start(self):
        src = self.src_combo.currentData()
        if src == "none":
            self._on_error("Нет устройства. Выберите тестовый генератор или подключите приёмник.")
            self.btn_start.setChecked(False)
            return
        e = self.engine
        e.fs = int(self.rate.currentData())
        e.mode = self._mode()
        e.sound = self.cb_sound.isChecked()
        e.std = STANDARDS[self.std_combo.currentData()]
        self._push_freq_to_engine(rebuild=False)
        e.gain = None if self.cb_agc.isChecked() else self._gain_tenths()
        e.start(src)
        e.cmd("ppm", self.ppm.value())
        e.cmd("rtl_agc", self.cb_rtl_agc.isChecked())
        if self.cb_bias.isChecked():
            e.cmd("bias", True)
        self.btn_start.setChecked(True)
        self.btn_start.setText("■ Стоп")
        self.video.clear("Поиск синхронизации…")
        self.acc = None

    def stop(self):
        self.engine.stop()
        self.btn_start.setChecked(False)
        self.btn_start.setText("▶ Старт")
        self.video.clear("Остановлено")

    def _on_opened(self, info):
        self.gains = info["gains"]
        self.gain.setRange(0, len(self.gains) - 1)
        self.gain.setValue(min(self.s["gain_idx"], len(self.gains) - 1))
        self._gain_changed()
        self.dev_info = f"{info['label']} · тюнер {info['tuner']}"
        drv = ("Эмуляция эфира: ДМВ 35 (аналог) и ДМВ 30 (DVB-T2)" if info["label"].startswith("Тест")
               else "Драйвер: " + info.get("driver", osplat.builtin_driver_label()))
        if info.get("note"):
            drv += "; " + info["note"]
        self.dev_label.setText(f"{self.dev_info}\n{drv}")

    def _on_error(self, msg):
        self.statusBar().showMessage("Ошибка: " + msg, 10000)
        if "открыть" in msg or "USB" in msg:
            self.stop()
            QMessageBox.warning(self, "SDR4NOOB", msg)

    def _on_frame(self, fr):
        """Кадр только запоминается; выводит его таймер с выбранной частотой (без очереди и отставания)."""
        self._pending_frame = fr

    def view_changed(self):
        fps = int(self.view_opt("video_fps"))
        if not hasattr(self, "frame_timer"):
            self.frame_timer = QTimer(self)
            self.frame_timer.timeout.connect(self._show_frame)
        self.frame_timer.start(int(1000 / fps))

    def _show_frame(self):
        fr = getattr(self, "_pending_frame", None)
        if fr is None:
            return
        self._pending_frame = None
        if self.stack.currentIndex() != 0 or self.cb_sound.isChecked():
            return
        img = fr["image"]
        k = self.nr.value() / 100.0
        if self.acc is None or self.acc.shape != img.shape:
            self.acc = img.copy()
        else:
            self.acc = self.acc * k + img * (1 - k)
        v = (self.acc - 0.5) * (self.contrast.value() / 100.0) + 0.5 + self.bright.value() / 200.0
        idx = np.clip(v * 255, 0, 255).astype(np.uint8)
        self.last_rgb = self.lut[idx]
        self.video.set_rgb(self.last_rgb)
        self.last_frame_info = fr
        self.fps_n += 1
        snr = fr["snr_db"]
        self.meter.set_value(max(snr, 0), f"{snr:.1f} dB")
        ok_h = fr["hlock"] > 0.5
        self.sync_label.setText(f"Синхр.: строки {'✓' if ok_h else '✗'}  кадры {'✓' if fr['vlock'] else '✗'}"
                                f"  ·  несущая {fr['level_db']:.1f} dBFS")
        self.video.overlay = "" if ok_h else "нет синхронизации"

    def _on_spectrum(self, d):
        n = d["psd"].size
        freqs = d["center"] + (np.arange(n) - n // 2) * (d["fs"] / n)
        self.spec.set_data(freqs, d["psd"])

    def _on_digital(self, r):
        self.dig_spec.set_data(r["freqs"], r["psd"])
        self.spec.set_data(r["freqs"], r["psd"])  # внизу — тот же канал целиком, с водопадом
        if r["present"]:
            text, col = "Мультиплекс обнаружен", "#3ad06a"
        elif r["other"]:
            text, col = "В канале другой сигнал (аналоговое ТВ / несущая)", "#e8a33a"
        else:
            text, col = "Мультиплекса нет / слабый сигнал", "#e8a33a"
        self.d_status.setText(text)
        self.d_status.setStyleSheet(f"font-size: 16pt; font-weight: bold; color: {col};")
        self.d_cn.set_value(max(r["cn_db"], 0), f"{r['cn_db']:.1f} dB")
        self.meter.set_value(max(r["cn_db"], 0), f"C/N {r['cn_db']:.1f} dB")
        self.d_level.setText(f"{r['level_db']:.1f} dBFS")
        self.d_flat.setText(f"{r['flatness_db']:.1f} dB")
        o = r.get("ofdm")
        if o and o.get("found"):
            self.d_ofdm.setText(f"DVB-T2 {o['mode']}, GI {o.get('guard') or '?'}  (пик ×{o.get('peak', 0):.0f})")
            self.d_off.setText(f"{o.get('freq_offset_hz', 0):+.0f} Гц (дробная часть шага поднесущих)")
        elif o:
            self.d_ofdm.setText("не определён (мало сигнала)")
            self.d_off.setText("—")
        self.sync_label.setText(f"Уровень {r['level_db']:.1f} dBFS · C/N {r['cn_db']:.1f} dB")
        self.fps_n += 1

    # ================= режимы, частота =================
    def _mode(self):
        return "digital" if self.rb_digital.isChecked() else "analog"

    def _sync_mode_ui(self):
        digital = self._mode() == "digital"
        self.stack.setCurrentIndex(1 if digital else 0)
        self.cb_sound.setEnabled(not digital)
        self.grp_pic.setEnabled(not digital)
        self.grp_snd.setEnabled(not digital and self.cb_sound.isChecked())
        self.freq_what.setText("Центр мультиплекса" if digital else
                               ("Несущая изображения (слушаем звук)" if self.cb_sound.isChecked()
                                else "Несущая изображения"))
        self.btn_scan.setText("Искать цифровые" if digital else "Искать аналоговые")
        if not digital and self.cb_sound.isChecked():
            self.video.clear("Режим звука\n\nКартинка и звук не помещаются\nв полосу режима ТВ одновременно")

    def _mode_changed(self, switched):
        mode = self._mode()
        old = "analog" if mode == "digital" else "digital"
        if switched:
            # тот же канал в другом режиме
            ch = self._nearest(self.fdisp.value, old)
            self.s["freq_" + old] = self.fdisp.value
            new_f = (ch.center if mode == "digital" else ch.video) if ch else self.s["freq_" + mode]
            self._set_freq(new_f, push=False)
        self._sync_mode_ui()
        self.acc = None
        self.engine.set_mode(mode, self.cb_sound.isChecked())
        self._push_freq_to_engine()
        if mode == "analog" and not self.cb_sound.isChecked():
            self.video.clear("Поиск синхронизации…")

    def _std_changed(self):
        self.engine.set_standard(self.std_combo.currentData())
        self._push_freq_to_engine()

    def _plan_changed(self):
        key = self.plan_combo.currentData()
        self.chans = channels(key)
        self._fill_channels()
        std = PLANS[key][1]
        self.std_combo.setCurrentIndex(self.std_combo.findData(std))
        self._set_freq(self.chans[0].video if self._mode() == "analog" else self.chans[0].center)

    def _fill_channels(self):
        self.chan_combo.blockSignals(True)
        self.chan_combo.clear()
        for ch in self.chans:
            self.chan_combo.addItem(f"{ch.name} — {ch.video / 1e6:.2f} / {ch.center / 1e6:.1f} МГц")
        self.chan_combo.addItem("— произвольная частота —")
        self.chan_combo.blockSignals(False)

    def _nominal(self, ch, mode=None):
        mode = mode or self._mode()
        return ch.center if mode == "digital" else ch.video

    def _nearest(self, f, mode=None):
        best = min(self.chans, key=lambda c: abs(self._nominal(c, mode) - f))
        return best if abs(self._nominal(best, mode) - f) <= 0.5e6 else None

    def _set_freq(self, f, push=True):
        self.fdisp.set_value(f)
        f = self.fdisp.value
        ch = self._nearest(f)
        self.chan_combo.blockSignals(True)
        self.fine.blockSignals(True)
        if ch:
            self.chan_combo.setCurrentIndex(self.chans.index(ch))
            off = int(round((f - self._nominal(ch)) / 1e3))
            self.fine.setValue(off)
            self.fine_label.setText(f"{off:+d} кГц")
        else:
            self.chan_combo.setCurrentIndex(len(self.chans))
            self.fine.setValue(0)
            self.fine_label.setText("—")
        self.chan_combo.blockSignals(False)
        self.fine.blockSignals(False)
        self.s["freq_" + self._mode()] = f
        if push:
            self._push_freq_to_engine()
            name = ch.name if ch else "—"
            self.video.show_osd(f"{name}\n{f / 1e6:.3f} МГц")

    def _push_freq_to_engine(self, rebuild=True):
        f = self.fdisp.value
        e = self.engine
        if self._mode() == "digital":
            ch = self._nearest(f)
            bw = ch.bandwidth if ch else 8e6
            if rebuild:
                e.set_mux(f, bw)
            else:
                e.mux_center, e.mux_bw = f, bw
        else:
            if rebuild:
                e.set_video_carrier(f)
            else:
                e.video_carrier = f
        self.acc = None
        self._update_markers()

    def _update_markers(self):
        f = self.fdisp.value
        st = STANDARDS[self.std_combo.currentData()]
        if self._mode() == "digital":
            ch = self._nearest(f)
            bw = ch.bandwidth if ch else 8e6
            self.spec.markers = [(f, "#ffd84d", "центр")]
            self.spec.bands = [(f - bw / 2, f + bw / 2, self._accent())]
            self.dig_spec.markers = list(self.spec.markers)
            self.dig_spec.bands = list(self.spec.bands)
        else:
            self.spec.markers = [(f, "#ffd84d", "изображение"), (f + st.sound_offset, "#5dff5d", "звук")]
            self.spec.bands = [(f - 0.75e6, f + st.sound_offset - 0.5e6, self._accent())]
        self.spec.update()

    def _accent(self):
        return THEMES.get(self.theme_name, THEMES["Тёмная"])["accent"]

    def _chan_selected(self, i):
        if i < len(self.chans):
            self._set_freq(self._nominal(self.chans[i]))

    def _chan_step(self, d):
        ch = self._nearest(self.fdisp.value)
        i = self.chans.index(ch) if ch else 0
        i = (i + d) % len(self.chans)
        self._set_freq(self._nominal(self.chans[i]))

    def _step(self, d):
        self._set_freq(self.fdisp.value + d * STEPS[self.step_combo.currentIndex()][0])

    def _fine_changed(self, v):
        ch = self._nearest(self.fdisp.value)
        if ch is None:
            i = self.chan_combo.currentIndex()
            if i >= len(self.chans):
                return
            ch = self.chans[i]
        self.fine_label.setText(f"{v:+d} кГц")
        self._set_freq(self._nominal(ch) + v * 1e3)

    def _spectrum_click(self, f):
        if self._mode() == "analog":
            if self.cb_sound.isChecked():
                f -= STANDARDS[self.std_combo.currentData()].sound_offset
            self._set_freq(round(f / 1e3) * 1e3)
        else:
            self._set_freq(round(f / 10e3) * 10e3)

    def _gain_tenths(self):
        if not self.gains:
            return 300
        return self.gains[min(self.gain.value(), len(self.gains) - 1)]

    def _gain_changed(self):
        agc = self.cb_agc.isChecked()
        self.gain.setEnabled(not agc)
        if agc:
            self.gain_label.setText("АРУ")
            self.engine.set_gain(None)
        else:
            t = self._gain_tenths()
            self.gain_label.setText(f"{t / 10:.1f} dB")
            self.engine.set_gain(t)

    def _rate_changed(self):
        self.engine.set_rate(int(self.rate.currentData()))
        self.acc = None

    # ================= изображение, тема =================
    def _palette_changed(self, name):
        self.lut = gradient_lut(PALETTES.get(name, PALETTES["Ч/Б"]))

    def apply_theme(self, t):
        if self.theme.currentText() != self.theme_name:
            self.theme.blockSignals(True)
            self.theme.setCurrentText(self.theme_name)
            self.theme.blockSignals(False)
        for w in (self.spec, self.dig_spec, self.fdisp, self.meter, self.alevel, self.d_cn):
            w.set_theme(t)
        if hasattr(self, "theme"):
            self._update_markers()

    def snapshot(self):
        if self.last_rgb is None:
            self.statusBar().showMessage("Снимок: пока нет кадра", 4000)
            return
        folder = paths.pictures(paths.PICS_TV)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, time.strftime("SDR4NOOB_%Y%m%d_%H%M%S.png"))
        h, w, _ = self.last_rgb.shape
        img = QImage(np.ascontiguousarray(self.last_rgb).data, w, h, 3 * w, QImage.Format_RGB888)
        img.scaled(int(h * 4 / 3), h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation).save(path)
        self.statusBar().showMessage(f"Снимок сохранён: {path}", 6000)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self._exit_fullscreen()
        else:
            self.scroll.hide()
            self.spec.hide()
            self.statusBar().hide()
            self.showFullScreen()

    def _exit_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            self.scroll.show()
            self.spec.show()
            self.statusBar().show()

    # ================= поиск =================
    def scan(self):
        if not self.engine.running():
            self.statusBar().showMessage("Сначала запустите приём", 4000)
            return
        kind = self._mode()
        self.scan_list.clear()
        self.scan_bar.setRange(0, len(self.chans))
        self.scan_bar.setValue(0)
        self.btn_scan.setEnabled(False)
        self.engine.start_scan(kind, self.chans)

    def _on_scan_done(self, res):
        self.btn_scan.setEnabled(True)
        self.scan_list.clear()
        found = sorted((r for r in res if r["found"]), key=lambda r: -r["value"])
        if not found:
            self.scan_list.addItem("Ничего не найдено")
        for r in found:
            ch = r["ch"]
            if r["kind"] == "analog":
                text = f"📺 {ch.name}  {ch.video / 1e6:.2f} МГц  · несущая +{r['value']:.0f} dB"
            else:
                text = f"📡 {ch.name}  {ch.center / 1e6:.1f} МГц  · +{r['value']:.1f} dB над шумом"
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, (r["kind"], ch.video if r["kind"] == "analog" else ch.center))
            self.scan_list.addItem(it)
        self.statusBar().showMessage(f"Поиск завершён: найдено {len(found)}", 6000)

    def _scan_pick(self, item):
        data = item.data(Qt.UserRole)
        if not data:
            return
        kind, f = data
        if kind != self._mode():
            (self.rb_digital if kind == "digital" else self.rb_analog).setChecked(True)
        self._set_freq(f)

    # ================= статус и выход =================
    def _update_status(self):
        now = time.time()
        if now - self.fps_t >= 1.0:
            self.fps = self.fps_n / (now - self.fps_t)
            self.fps_n = 0
            self.fps_t = now
        run = self.engine.running()
        what = "кадров/с" if self._mode() == "analog" else "измерений/с"
        drops = f" · пропуски {self.engine._drops}" if self.engine._drops else ""
        self.sb_left.setText(f"{self.dev_info or 'устройство не открыто'} · {self.rate.currentData() / 1e6:.3f} MS/s · "
                             f"{what}: {self.fps:.1f}{drops}" if run else "Остановлено")

    def shutdown(self):
        self.engine.stop()

    def closeEvent(self, e):
        self.engine.stop()
        s = self.s
        s.update(theme=self.theme.currentText(), palette=self.palette.currentText(),
                 brightness=self.bright.value(), contrast=self.contrast.value(), nr=self.nr.value(),
                 plan=self.plan_combo.currentData(), std=self.std_combo.currentData(), mode=self._mode(),
                 sound=self.cb_sound.isChecked(), step=self.step_combo.currentIndex(),
                 gain_idx=self.gain.value(), agc=self.cb_agc.isChecked(), rtl_agc=self.cb_rtl_agc.isChecked(),
                 ppm=self.ppm.value(), rate=int(self.rate.currentData()), bias=self.cb_bias.isChecked(),
                 volume=self.vol.value(), field_swap=self.cb_swap.isChecked())
        s["freq_" + self._mode()] = self.fdisp.value
        if self.src_combo.currentData() != "none" and not self.opts.get("source"):
            s["source"] = self.src_combo.currentData()
        if not self.opts.get("no_save") and not self.opts.get("shot"):
            try:
                os.makedirs(SETTINGS_DIR, exist_ok=True)
                with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                    json.dump(s, f, ensure_ascii=False, indent=1)
            except OSError:
                pass
        super().closeEvent(e)
