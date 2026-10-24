"""Панели окна «Радио»: сворачиваемая секция (как левые панели SDR#) и менеджер частот с группами."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLineEdit, QMessageBox, QPushButton,
                               QSpinBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import freqman
from ..radio import MODES

BUILTIN_GROUP = "Диапазоны (встроенные)"


class Section(QGroupBox):
    """Секция левой колонки: щелчок по заголовку сворачивает и разворачивает её.
    Содержимое — в self.body (свой layout задаёт вызывающий)."""

    toggled = Signal(bool)

    def __init__(self, title, expanded=True, tip=""):
        super().__init__()
        self._title = title
        self.body = QWidget()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.body)
        self.expanded = True
        self.setToolTip(tip or "Щёлкните по заголовку, чтобы свернуть или развернуть")
        self.set_expanded(expanded)

    def set_expanded(self, on):
        self.expanded = bool(on)
        self.body.setVisible(self.expanded)
        self.setTitle(("▾ " if self.expanded else "▸ ") + self._title)
        # свёрнутая секция — только строка заголовка, без пустой рамки
        self.setMaximumHeight(16777215 if self.expanded else 25)

    def mousePressEvent(self, e):
        if e.position().y() <= 26:
            self.set_expanded(not self.expanded)
            self.toggled.emit(self.expanded)
            e.accept()
            return
        super().mousePressEvent(e)


class EntryDialog(QDialog):
    def __init__(self, parent, entry, groups):
        super().__init__(parent)
        self.setWindowTitle("Частота")
        f = QFormLayout(self)
        self.name = QLineEdit(entry.get("name", ""))
        f.addRow("Название:", self.name)
        self.freq = QDoubleSpinBox()
        self.freq.setDecimals(6)
        self.freq.setRange(0.1, 2000.0)
        self.freq.setSuffix(" МГц")
        self.freq.setValue(float(entry.get("freq", 100e6)) / 1e6)
        f.addRow("Частота:", self.freq)
        self.mode = QComboBox()
        self.mode.addItems(list(MODES))
        self.mode.setCurrentText(entry.get("mode", "NFM"))
        f.addRow("Режим:", self.mode)
        self.bw = QSpinBox()
        self.bw.setRange(100, 250000)
        self.bw.setSuffix(" Гц")
        self.bw.setValue(int(entry.get("bw") or MODES[self.mode.currentText()][0]))
        self.mode.currentTextChanged.connect(lambda m: self.bw.setValue(MODES[m][0]))
        f.addRow("Полоса:", self.bw)
        self.group = QComboBox()
        self.group.setEditable(True)
        self.group.addItems(sorted(set(groups) | {freqman.DEFAULT_GROUP}))
        self.group.setCurrentText(entry.get("group") or freqman.DEFAULT_GROUP)
        self.group.setToolTip("Группа — папка в менеджере частот. Можно вписать новую.")
        f.addRow("Группа:", self.group)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def entry(self):
        return freqman.normalize({"name": self.name.text(), "freq": self.freq.value() * 1e6,
                                  "mode": self.mode.currentText(), "bw": self.bw.value(),
                                  "group": self.group.currentText()})


class FreqManager(QWidget):
    """Дерево «группа → частоты». Двойной щелчок — настроиться. Встроенные диапазоны не редактируются."""

    tuneRequested = Signal(dict)
    changed = Signal()

    def __init__(self, entries, builtin, current=None):
        super().__init__()
        self.entries = [freqman.normalize(e) for e in entries]
        self.builtin = builtin          # [(название, частота, режим)]
        self.current = current          # () -> словарь текущей настройки (для «+ Текущая»)
        self.collapsed = set()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по названию или частоте…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refill)
        v.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Название", "МГц", "Режим"])
        self.tree.setRootIsDecorated(True)
        self.tree.setMinimumHeight(200)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        h = self.tree.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tree.itemDoubleClicked.connect(self._pick)
        self.tree.itemCollapsed.connect(lambda it: self.collapsed.add(it.data(0, Qt.UserRole + 1)))
        self.tree.itemExpanded.connect(lambda it: self.collapsed.discard(it.data(0, Qt.UserRole + 1)))
        self.tree.setToolTip("Двойной щелчок — перейти на частоту с её режимом и полосой")
        v.addWidget(self.tree)
        row = QHBoxLayout()
        for text, tip, fn in (("+ Текущая", "Запомнить текущую частоту, режим и полосу", self.add_current),
                              ("Изменить", "Изменить выбранную запись", self.edit),
                              ("Удалить", "Удалить выбранную запись (встроенные не удаляются)", self.remove)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b)
        v.addLayout(row)
        row2 = QHBoxLayout()
        b = QPushButton("Импорт…")
        b.setToolTip("Добавить частоты из CSV (Excel) или из менеджера частот SDR# (Frequencies.xml)")
        b.clicked.connect(self.import_file)
        row2.addWidget(b)
        b = QPushButton("Экспорт…")
        b.setToolTip("Сохранить свои частоты в CSV (Excel) или XML формата SDR#")
        b.clicked.connect(self.export_file)
        row2.addWidget(b)
        v.addLayout(row2)
        self.refill()

    def groups(self):
        return sorted({e["group"] for e in self.entries})

    def refill(self):
        q = self.search.text().strip().lower()
        self.tree.clear()

        def match(name, f):
            return not q or q in name.lower() or q.replace(",", ".") in f"{f / 1e6:.4f}"

        groups = {}
        for i, e in enumerate(self.entries):
            if match(e["name"], e["freq"]):
                groups.setdefault(e["group"], []).append((i, e))
        order = sorted(groups)
        for g in order:
            self._add_group(g, [(e["name"], e["freq"], e["mode"], ("user", i)) for i, e in groups[g]])
        bi = [(n, f, m, ("builtin", None)) for n, f, m in self.builtin if match(n, f)]
        if bi:
            self._add_group(BUILTIN_GROUP, bi)

    def _add_group(self, g, items):
        top = QTreeWidgetItem([f"{g} ({len(items)})", "", ""])
        top.setData(0, Qt.UserRole, None)
        top.setData(0, Qt.UserRole + 1, g)
        top.setFlags(top.flags() & ~Qt.ItemIsSelectable)
        f = top.font(0)
        f.setBold(True)
        top.setFont(0, f)
        self.tree.addTopLevelItem(top)
        for name, freq, mode, ref in sorted(items, key=lambda x: x[1]):
            it = QTreeWidgetItem([name, f"{freq / 1e6:.4f}", mode])
            it.setData(0, Qt.UserRole, (ref, {"name": name, "freq": freq, "mode": mode}))
            it.setToolTip(0, f"{name}\n{freq / 1e6:.6f} МГц, {mode}")
            top.addChild(it)
        top.setExpanded(g not in self.collapsed)

    def _selected(self):
        it = self.tree.currentItem()
        d = it.data(0, Qt.UserRole) if it else None
        return d

    def _pick(self, it, _col=0):
        d = it.data(0, Qt.UserRole)
        if not d:
            return
        ref, e = d
        if ref[0] == "user":
            e = dict(self.entries[ref[1]])
        self.tuneRequested.emit(e)

    def add_current(self):
        cur = self.current() if self.current else {}
        dlg = EntryDialog(self, cur, self.groups())
        if dlg.exec():
            self.entries.append(dlg.entry())
            self.refill()
            self.changed.emit()

    def edit(self):
        d = self._selected()
        if not d:
            return
        ref, e = d
        if ref[0] != "user":
            # встроенный диапазон: правка = копия в свою группу
            e = freqman.normalize(dict(e, group=freqman.DEFAULT_GROUP))
            dlg = EntryDialog(self, e, self.groups())
            if dlg.exec():
                self.entries.append(dlg.entry())
                self.refill()
                self.changed.emit()
            return
        dlg = EntryDialog(self, self.entries[ref[1]], self.groups())
        if dlg.exec():
            self.entries[ref[1]] = dlg.entry()
            self.refill()
            self.changed.emit()

    def remove(self):
        d = self._selected()
        if not d:
            return
        ref, _e = d
        if ref[0] != "user":
            QMessageBox.information(self, "Менеджер частот", "Встроенные диапазоны не удаляются.")
            return
        del self.entries[ref[1]]
        self.refill()
        self.changed.emit()

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Импорт частот", "",
                                              "Частоты (*.csv *.txt *.xml);;CSV (*.csv *.txt);;SDR# (*.xml)")
        if not path:
            return
        try:
            new = freqman.import_any(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Импорт частот", f"Не удалось прочитать файл:\n{e}")
            return
        n = freqman.merge(self.entries, new)
        self.refill()
        self.changed.emit()
        QMessageBox.information(self, "Импорт частот", f"Добавлено записей: {n} (в файле {len(new)}).")

    def export_file(self):
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт частот", "frequencies.csv",
                                              "CSV для Excel (*.csv);;SDR# Frequencies.xml (*.xml)")
        if not path:
            return
        try:
            freqman.export_any(path, self.entries)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Экспорт частот", f"Не удалось сохранить файл:\n{e}")
