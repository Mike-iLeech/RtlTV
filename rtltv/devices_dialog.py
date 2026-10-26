"""Диалог «Устройства…»: какие SDR подключены, какой драйвер Windows к ним привязан, загрузка DLL драйверов
и сетевые серверы rtl_tcp. Драйверы Windows диалог не меняет и ничего не скачивает — только подсказывает."""
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFileDialog, QGroupBox,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QListWidget, QMessageBox,
                               QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from . import devices, osplat

INTRO_WIN = ("SDR4NOOB работает с USB радиоприёмниками (SDR). Для RTL-SDR драйвер встроен; для других приёмников "
             "загрузите DLL их драйвера "
             "(HackRF: hackrf.dll; Airspy R2/Mini: airspy.dll; обоим нужны libusb-1.0.dll и pthreadVC2.dll) — "
             "файлы копируются в папку драйверов SDR4NOOB. Приёмник на другом компьютере подключается как "
             "сетевой rtl_tcp. Драйвер Windows (WinUSB) ставится программой Zadig: "
             f'<a href="{devices.ZADIG_URL}">{devices.ZADIG_URL}</a> — SDR4NOOB его не меняет.')
INTRO_UNIX = ("SDR4NOOB работает с USB радиоприёмниками (SDR). Для приёмников на RTL2832U (RTL-SDR и совместимые) "
              "драйвер встроен. "
              "Для HackRF и Airspy R2/Mini SDR4NOOB берёт системную библиотеку "
              + ("(brew install hackrf airspy)" if osplat.IS_MAC else "(пакеты libhackrf0, libairspy0)")
              + f" или файл {osplat.LIB_EXT}, загруженный сюда. Приёмник на другом компьютере подключается "
              "как сетевой rtl_tcp.")
INTRO = INTRO_WIN if osplat.IS_WIN else INTRO_UNIX


class DevicesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Устройства")
        self.resize(900, 640)
        self.changed = False
        self.d = devices.load_dev_settings()
        v = QVBoxLayout(self)
        intro = QLabel(INTRO)
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        intro.setOpenExternalLinks(True)
        v.addWidget(intro)

        g = QGroupBox("Подключённые USB-приёмники")
        gl = QVBoxLayout(g)
        self.usb = QTreeWidget()
        self.usb.setColumnCount(4)
        self.usb.setHeaderLabels(["Устройство", "VID:PID", "Драйвер Windows" if osplat.IS_WIN else "Состояние",
                                  "Что делать"])
        self.usb.setRootIsDecorated(False)
        self.usb.setWordWrap(True)
        self.usb.setSelectionMode(QAbstractItemView.NoSelection)
        h = self.usb.header()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        gl.addWidget(self.usb)
        v.addWidget(g, 2)

        g2 = QGroupBox("Драйверы (DLL) в папке SDR4NOOB" if osplat.IS_WIN else "Драйверы в папке SDR4NOOB")
        g2l = QVBoxLayout(g2)
        self.dlls = QListWidget()
        g2l.addWidget(self.dlls)
        row = QHBoxLayout()
        row.addWidget(QLabel("Драйвер RTL-SDR:"))
        self.rtl_combo = QComboBox()
        self.rtl_combo.activated.connect(self._rtl_choice)
        row.addWidget(self.rtl_combo, 1)
        b_dir = QPushButton("Открыть папку")
        b_dir.clicked.connect(self._open_dir)
        row.addWidget(b_dir)
        g2l.addLayout(row)
        v.addWidget(g2, 1)

        g3 = QGroupBox("Сетевые rtl_tcp и итоговый список источников")
        g3l = QHBoxLayout(g3)
        tcp_col = QVBoxLayout()
        tcp_col.addWidget(QLabel("Серверы rtl_tcp:"))
        self.tcp = QListWidget()
        tcp_col.addWidget(self.tcp)
        b_del = QPushButton("Убрать из списка")
        b_del.clicked.connect(self._remove_tcp)
        tcp_col.addWidget(b_del)
        g3l.addLayout(tcp_col, 1)
        src_col = QVBoxLayout()
        src_col.addWidget(QLabel("Источники в окнах режимов:"))
        self.sources = QListWidget()
        src_col.addWidget(self.sources)
        g3l.addLayout(src_col, 1)
        v.addWidget(g3, 1)

        bb = QHBoxLayout()
        self.b_load = QPushButton("Загрузить драйвер (DLL)…" if osplat.IS_WIN else "Загрузить драйвер…")
        self.b_load.clicked.connect(self.load_dll)
        self.b_tcp = QPushButton("Добавить сетевой rtl_tcp…")
        self.b_tcp.clicked.connect(self.add_tcp)
        self.b_refresh = QPushButton("Обновить")
        self.b_refresh.clicked.connect(self.refresh)
        btns = [self.b_load, self.b_tcp, self.b_refresh]
        if osplat.IS_LINUX:   # права на USB и драйвер ТВ-тюнера ядра — один раз, через pkexec
            self.b_access = QPushButton("Настроить доступ…")
            self.b_access.setToolTip("Правило udev для USB-приёмников и запрет драйвера ТВ-тюнера ядра "
                                     "(dvb_usb_rtl28xxu). Нужен пароль администратора.")
            self.b_access.clicked.connect(self.setup_access)
            btns.insert(0, self.b_access)
        for b in btns:
            bb.addWidget(b)
        bb.addStretch(1)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        bb.addWidget(close)
        v.addLayout(bb)
        self.refresh()

    # ---------- заполнение ----------
    def refresh(self):
        self.d = devices.load_dev_settings()
        self.usb.clear()
        found = devices.usb_devices()
        for dev in found:
            it = QTreeWidgetItem([dev["model"], f"{dev['vid']:04x}:{dev['pid']:04x}", dev["state"], dev["hint"]])
            it.setToolTip(3, dev["hint"])
            it.setToolTip(0, dev["instance"])
            self.usb.addTopLevelItem(it)
        if not found:
            self.usb.addTopLevelItem(QTreeWidgetItem(["Приёмников SDR на USB не найдено", "", "", ""]))

        self.dlls.clear()
        rtl_dlls = []
        for ent in devices.driver_dlls():
            text = f"{ent['name']} — {ent['title']}"
            if ent["error"]:
                text += f" — не подходит: {ent['error']}"
            self.dlls.addItem(text)
            if ent["kind"] == "rtl":
                rtl_dlls.append(ent["name"])
        if not self.dlls.count():
            self.dlls.addItem(f"Пока пусто ({devices.DRIVERS_DIR})")
        self.rtl_combo.clear()
        self.rtl_combo.addItem("Встроенная librtlsdr (rtl-sdr-blog)", None)
        for n in rtl_dlls:
            self.rtl_combo.addItem(f"Загруженная: {n}", n)
        idx = self.rtl_combo.findData(self.d.get("rtl_dll")) if self.d.get("rtl_dll") else 0
        self.rtl_combo.setCurrentIndex(max(idx, 0))

        self.tcp.clear()
        for hp in self.d["rtltcp"]:
            self.tcp.addItem(hp)
        self.sources.clear()
        srcs = devices.available_sources(refresh=True)   # «Устройства…» — всегда свежий опрос USB
        for _sid, label in srcs:
            self.sources.addItem(label)
        self.d["sources"] = [[sid, label] for sid, label in srcs]
        devices.save_dev_settings(self.d)

    # ---------- действия ----------
    def load_dll(self):
        files, _f = QFileDialog.getOpenFileNames(self, "Драйвер устройства", "", osplat.lib_filter())
        if not files:
            return
        done, problems, new_rtl = [], [], []
        for path in files:
            try:
                res = devices.import_dll(path)
            except devices.DriverError as e:
                problems.append(str(e))
                continue
            if res["error"]:
                problems.append(f"{res['name']} скопирована, но не загружается: {res['error']}")
            else:
                done.append(f"{res['name']} — {res['title']}")
            if res["kind"] == "rtl":
                new_rtl.append(res["name"])
        self.changed = True
        self.refresh()
        if new_rtl and QMessageBox.question(
                self, "Драйвер RTL-SDR",
                f"Использовать {new_rtl[0]} вместо встроенной librtlsdr во всех режимах?") == QMessageBox.Yes:
            self._set_rtl(new_rtl[0])
        msg = ""
        if done:
            msg += "Загружено:\n" + "\n".join(done)
        if problems:
            msg += ("\n\n" if msg else "") + "Не получилось:\n" + "\n".join(problems)
        (QMessageBox.warning if problems else QMessageBox.information)(self, "Драйверы", msg)

    def _rtl_choice(self, _i):
        self._set_rtl(self.rtl_combo.currentData())

    def _set_rtl(self, name):
        self.d = devices.load_dev_settings()
        self.d["rtl_dll"] = name
        devices.save_dev_settings(self.d)
        devices.apply_rtl_driver(self.d)
        self.changed = True
        self.refresh()

    def add_tcp(self):
        text, ok = QInputDialog.getText(self, "Сетевой rtl_tcp",
                                        "Адрес сервера rtl_tcp (хост:порт), например 192.168.1.10:1234:",
                                        text="127.0.0.1:1234")
        if not ok or not text.strip():
            return
        try:
            host, port = devices.parse_hostport(text)
        except devices.DeviceError as e:
            QMessageBox.warning(self, "rtl_tcp", str(e))
            return
        hp = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        try:
            info = devices.probe_rtltcp(host, port)
            note = f"Сервер {hp} отвечает: тюнер {info['tuner']}, ступеней усиления {info['gains']}."
        except devices.DeviceError as e:
            if QMessageBox.question(self, "rtl_tcp", f"{e}\n\nВсё равно добавить {hp}?") != QMessageBox.Yes:
                return
            note = None
        self.d = devices.load_dev_settings()
        if hp not in self.d["rtltcp"]:
            self.d["rtltcp"].append(hp)
        devices.save_dev_settings(self.d)
        self.changed = True
        self.refresh()
        if note:
            QMessageBox.information(self, "rtl_tcp", note)

    def _remove_tcp(self):
        it = self.tcp.currentItem()
        if not it:
            return
        self.d = devices.load_dev_settings()
        self.d["rtltcp"] = [x for x in self.d["rtltcp"] if x != it.text()]
        devices.save_dev_settings(self.d)
        self.changed = True
        self.refresh()

    def setup_access(self):
        from . import linux_setup
        what = ("SDR4NOOB установит два файла (нужен пароль администратора):\n\n"
                f"• {linux_setup.RULES_PATH} — доступ к USB-приёмникам для вошедшего пользователя;\n"
                f"• {linux_setup.BLACKLIST_PATH} — ядро перестанет считать приёмник на RTL2832U "
                "ТВ-тюнером (драйвер dvb_usb_rtl28xxu).\n\nПродолжить?")
        if linux_setup.is_configured():
            what = "Доступ уже настроен. Установить файлы заново?\n\n" + what
        if QMessageBox.question(self, "Доступ к USB", what) != QMessageBox.Yes:
            return
        import threading
        from PySide6.QtCore import QTimer
        res = {}

        def work():   # pkexec ждёт пароль в своём окне — окно программы при этом не замирает
            try:
                res["r"] = linux_setup.run_setup()
            except Exception as e:  # noqa: BLE001
                res["r"] = (False, f"Настройка не удалась: {e}")
        th = threading.Thread(target=work, daemon=True, name="usb-access-setup")
        self.b_access.setEnabled(False)
        th.start()
        timer = QTimer(self)

        def check():
            if th.is_alive():
                return
            timer.stop()
            self.b_access.setEnabled(True)
            ok, msg = res.get("r", (False, "Настройка не удалась."))
            (QMessageBox.information if ok else QMessageBox.warning)(self, "Доступ к USB", msg)
            self.refresh()
        timer.timeout.connect(check)
        timer.start(200)
        return th

    def _open_dir(self):
        os.makedirs(devices.DRIVERS_DIR, exist_ok=True)
        osplat.open_path(devices.DRIVERS_DIR)
