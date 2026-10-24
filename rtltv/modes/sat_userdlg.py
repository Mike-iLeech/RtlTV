"""Диалоги «Свои спутники»: список с добавлением/правкой/удалением и редактор спутника
(NORAD → TLE с CelesTrak или TLE вручную, название, категория, передатчики)."""
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from .. import satcat, satpass

MODES = ["NFM", "WFM", "AM", "USB", "LSB", "CW", "LRPT", "LRIT", "IQ"]
COLORS = ["#e0e0e0", "#f2a07a", "#7ad0f2", "#c27af2", "#7af2a0", "#f2e07a"]


class _Fetch(QObject):
    done = Signal(object, str)


class SatEditDialog(QDialog):
    def __init__(self, parent, sat=None):
        super().__init__(parent)
        self.setWindowTitle("Свой спутник" if sat is None else f"Правка: {sat.name}")
        self.resize(720, 560)
        f = QFormLayout(self)
        f.setRowWrapPolicy(QFormLayout.WrapLongRows)
        h = QHBoxLayout()
        self.norad = QSpinBox()
        self.norad.setRange(1, 999999)
        self.norad.setValue(sat.norad if sat else 25544)
        self.btn_fetch = QPushButton("Загрузить TLE с CelesTrak")
        self.btn_fetch.clicked.connect(self._fetch)
        h.addWidget(self.norad)
        h.addWidget(self.btn_fetch)
        h.addStretch(1)
        f.addRow("Номер NORAD:", h)
        self.tle = QPlainTextEdit()
        self.tle.setPlaceholderText("Пусто — TLE досылается с CelesTrak по номеру NORAD при каждом обновлении.\n"
                                    "Или вставьте TLE вручную (2 или 3 строки).")
        self.tle.setMaximumHeight(80)
        if sat and sat.tle:
            self.tle.setPlainText("\n".join(sat.tle))
        f.addRow("TLE:", self.tle)
        self.name = QLineEdit(sat.name if sat else "")
        f.addRow("Название:", self.name)
        self.cat = QComboBox()
        for k, v in satcat.CATEGORIES.items():
            self.cat.addItem(v, k)
        self.cat.setCurrentIndex(max(0, self.cat.findData(sat.cat if sat else "other")))
        f.addRow("Категория:", self.cat)
        self.note = QLineEdit(sat.note if sat else "")
        f.addRow("Примечание:", self.note)
        self.txs = QTableWidget(0, 6)
        self.txs.setHorizontalHeaderLabels(["Название", "Частота, МГц", "До, МГц (полоса)", "Режим", "Полоса, кГц",
                                            "Декодер"])
        self.txs.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.txs.setSelectionBehavior(QAbstractItemView.SelectRows)
        f.addRow(QLabel("Передатчики (частота вниз; для транспондера — полоса «от–до»):"))
        f.addRow(self.txs)
        h2 = QHBoxLayout()
        b_add = QPushButton("Добавить передатчик")
        b_add.clicked.connect(lambda: self._add_tx())
        b_del = QPushButton("Удалить выбранный")
        b_del.clicked.connect(lambda: self.txs.removeRow(self.txs.currentRow()))
        h2.addWidget(b_add)
        h2.addWidget(b_del)
        h2.addStretch(1)
        f.addRow(h2)
        for t in (sat.txs if sat else []):
            self._add_tx(t)
        if not sat:
            self._add_tx()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        f.addRow(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Сохранить")
        bb.button(QDialogButtonBox.Cancel).setText("Отмена")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self._f = _Fetch()
        self._f.done.connect(self._fetched)
        self.result_sat = None

    def _add_tx(self, t=None):
        r = self.txs.rowCount()
        self.txs.insertRow(r)
        self.txs.setItem(r, 0, QTableWidgetItem(t.label if t else "Передатчик"))
        for c, val in ((1, t.freq / 1e6 if t else 145.8), (2, t.hi / 1e6 if t and t.hi else 0.0),
                       (4, (t.bw or 0) / 1e3 if t else 0.0)):
            sp = QDoubleSpinBox()
            sp.setDecimals(4 if c != 4 else 1)
            sp.setRange(0, 6000 if c != 4 else 2000)
            sp.setValue(val)
            self.txs.setCellWidget(r, c, sp)
        mode = QComboBox()
        mode.addItems(MODES)
        mode.setCurrentText(t.mode if t else "NFM")
        self.txs.setCellWidget(r, 3, mode)
        dec = QComboBox()
        dec.addItem("по режиму", "")
        for k, v in satcat.DECODERS.items():
            dec.addItem(v, k)
        dec.setCurrentIndex(max(0, dec.findData(t.decoder if t else "")))
        self.txs.setCellWidget(r, 5, dec)

    def _fetch(self):
        n = self.norad.value()
        self.status.setText(f"Загрузка TLE для {n}…")
        self.btn_fetch.setEnabled(False)

        def work():
            try:
                text = satpass._download(satpass.TLE_URL_CATNR.format(norad=n), 15)
                tl = satpass.parse_tle(text).get(n)
                self._f.done.emit(tl, "" if tl else "CelesTrak не вернул TLE для этого номера")
            except OSError as e:
                self._f.done.emit(None, f"нет связи с CelesTrak: {e}")
        threading.Thread(target=work, daemon=True).start()

    def _fetched(self, tl, err):
        self.btn_fetch.setEnabled(True)
        if tl is None:
            self.status.setText(err)
            return
        if not self.name.text().strip():
            self.name.setText(tl.name)
        self.status.setText(f"TLE найден: {tl.name}. Сохранять его вручную не нужно — он будет обновляться сам.")

    def _accept(self):
        tle = ()
        lines = [ln.strip() for ln in self.tle.toPlainText().splitlines() if ln.strip()]
        if lines:
            parsed = satpass.parse_tle("\n".join(lines))
            if not parsed:
                QMessageBox.warning(self, "TLE", "Не удалось разобрать TLE: нужны строки «1 …» и «2 …».")
                return
            n, tl = next(iter(parsed.items()))
            self.norad.setValue(n)
            tle = (tl.line1, tl.line2)
            if not self.name.text().strip():
                self.name.setText(tl.name)
        txs = []
        for r in range(self.txs.rowCount()):
            f = self.txs.cellWidget(r, 1).value() * 1e6
            if f <= 0:
                continue
            hi = self.txs.cellWidget(r, 2).value() * 1e6
            txs.append(satcat.Tx((self.txs.item(r, 0).text() if self.txs.item(r, 0) else "") or "Передатчик", f,
                                 self.txs.cellWidget(r, 3).currentText(), hi if hi > f else 0.0,
                                 self.txs.cellWidget(r, 4).value() * 1e3, "",
                                 self.txs.cellWidget(r, 5).currentData()))
        if not txs:
            QMessageBox.warning(self, "Передатчики", "Добавьте хотя бы один передатчик с частотой.")
            return
        n = self.norad.value()
        self.result_sat = satcat.Sat(n, self.name.text().strip() or f"NORAD {n}", self.cat.currentData(),
                                     COLORS[n % len(COLORS)], txs, "добавлен вручную", self.note.text().strip(),
                                     True, tle)
        self.accept()


class UserSatsDialog(QDialog):
    """Список своих спутников. Результат — self.sats (list[Sat])."""

    def __init__(self, parent, sats):
        super().__init__(parent)
        self.setWindowTitle("Свои спутники")
        self.resize(520, 380)
        self.sats = list(sats)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Свои спутники появляются на карте, в пролётах, в «Слушать» и «Приёме со спутника»."))
        self.lst = QListWidget()
        self.lst.itemDoubleClicked.connect(lambda _i: self._edit())
        v.addWidget(self.lst, 1)
        h = QHBoxLayout()
        for text, fn in (("Добавить…", self._add), ("Изменить…", self._edit), ("Удалить", self._del)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            h.addWidget(b)
        h.addStretch(1)
        v.addLayout(h)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Готово")
        bb.button(QDialogButtonBox.Cancel).setText("Отмена")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._fill()

    def _fill(self):
        self.lst.clear()
        for s in self.sats:
            it = QListWidgetItem(f"{s.name} (NORAD {s.norad}) — {satcat.CATEGORIES.get(s.cat, s.cat)}, "
                                 f"передатчиков {len(s.txs)}" + (", TLE вручную" if s.tle else ""))
            self.lst.addItem(it)

    def _add(self):
        d = SatEditDialog(self)
        if d.exec() == QDialog.Accepted and d.result_sat:
            self.sats = [s for s in self.sats if s.norad != d.result_sat.norad] + [d.result_sat]
            self._fill()

    def _edit(self):
        i = self.lst.currentRow()
        if not 0 <= i < len(self.sats):
            return
        d = SatEditDialog(self, self.sats[i])
        if d.exec() == QDialog.Accepted and d.result_sat:
            self.sats[i] = d.result_sat
            self._fill()

    def _del(self):
        i = self.lst.currentRow()
        if 0 <= i < len(self.sats):
            del self.sats[i]
            self._fill()
