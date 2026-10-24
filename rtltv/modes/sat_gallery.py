"""Галерея «Снимки со спутников»: все сохранённые снимки (Метеор, Электро-Л, SSTV) с метаданными,
миниатюрами и просмотром с масштабом. Метаданные — JSON рядом с PNG (имя.png → имя.json)."""
import json
import os
import re
import shutil
import time

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                               QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import paths

PICS = paths.pictures()
DIRS = {"meteor": paths.pictures(paths.PICS_METEOR), "elektro": paths.pictures(paths.PICS_ELEKTRO),
        "sstv": paths.pictures(paths.PICS_SSTV)}
COLS = ["Снимок", "Получен", "Спутник", "Частота", "Технические данные", "Источник", "Файл"]
THUMB = 72


def meta_path(png):
    return os.path.splitext(png)[0] + ".json"


def write_meta(png, meta):
    """Записать метаданные рядом со снимком (ошибки записи не мешают приёму)."""
    try:
        m = dict(meta)
        m.setdefault("saved", time.strftime("%Y-%m-%d %H:%M:%S"))
        with open(meta_path(png), "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def read_meta(png):
    try:
        with open(meta_path(png), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _guess(png, kind):
    """Метаданные по имени файла, если JSON нет (старые снимки, SSTV)."""
    name = os.path.basename(png)
    m = {"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(png)))}
    if kind == "meteor":
        r = re.match(r"meteor_([^_]+)_(\d{8})_(\d{6})_(\w+)\.png", name)
        if r:
            m.update(sat={"M2-3": "Метеор-М №2-3", "M2-4": "Метеор-М №2-4"}.get(r.group(1), r.group(1)),
                     product=r.group(4), freq=137.9e6, modulation="LRPT")
    elif kind == "elektro":
        r = re.match(r"elektro_(GOMS\d)_(\d{12})_(\w+)\.png", name)
        if r:
            m.update(sat="Электро-Л №" + r.group(1)[-1], product=r.group(3), freq=1691e6, modulation="LRIT BPSK")
    else:
        m.update(sat="SSTV", modulation="SSTV")
    return m


def tech_text(m):
    parts = []
    for key, fmt in (("modulation", "{}"), ("symbol_rate", "{:.0f} симв/с"), ("product", "{}"),
                     ("frames", None), ("lines", "{} строк"), ("mode", "{}")):
        v = m.get(key)
        if v in (None, ""):
            continue
        if key == "frames":
            parts.append(f"кадров {m.get('frames_ok', '?')}/{v}" + (f", RS {m['rs_pct']:.0f} %" if "rs_pct" in m else ""))
        else:
            parts.append(fmt.format(v))
    return ", ".join(parts)


def scan(dirs=None):
    """[(путь, вид, метаданные)] — новые сверху."""
    out = []
    for kind, d in (dirs or DIRS).items():
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.lower().endswith((".png", ".jpg")):
                continue
            p = os.path.join(d, fn)
            m = read_meta(p) or _guess(p, kind)
            m.setdefault("time", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))))
            out.append((p, kind, m))
    out.sort(key=lambda x: x[2].get("time", ""), reverse=True)
    return out


class ImageViewer(QDialog):
    """Просмотр снимка: колесо с Ctrl — масштаб, кнопки «по окну», «1:1», «Сохранить как…»."""

    def __init__(self, parent, path, title=""):
        super().__init__(parent)
        self.setWindowTitle(title or os.path.basename(path))
        self.path = path
        self.img = QImage(path)
        self.zoom = None
        v = QVBoxLayout(self)
        h = QHBoxLayout()
        for text, fn in (("По окну", lambda: self._set(None)), ("1:1", lambda: self._set(1.0)),
                         ("+", lambda: self._step(1.25)), ("−", lambda: self._step(0.8)),
                         ("Сохранить как…", self._save_as)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            h.addWidget(b)
        h.addStretch(1)
        self.info = QLabel(f"{self.img.width()}×{self.img.height()}")
        h.addWidget(self.info)
        v.addLayout(h)
        self.sa = QScrollArea()
        self.lbl = QLabel()
        self.lbl.setAlignment(Qt.AlignCenter)
        self.sa.setWidget(self.lbl)
        self.sa.setWidgetResizable(True)
        self.sa.setAlignment(Qt.AlignCenter)
        v.addWidget(self.sa, 1)
        self.resize(1100, 800)
        self._render()

    def _scale(self):
        if self.zoom is not None:
            return self.zoom
        vw, vh = self.sa.viewport().width() - 4, self.sa.viewport().height() - 4
        return max(0.02, min(vw / max(1, self.img.width()), vh / max(1, self.img.height())))

    def _render(self):
        if self.img.isNull():
            self.lbl.setText("Не удалось открыть снимок")
            return
        k = self._scale()
        pm = QPixmap.fromImage(self.img).scaled(max(1, int(self.img.width() * k)), max(1, int(self.img.height() * k)),
                                                Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.lbl.setPixmap(pm)
        self.info.setText(f"{self.img.width()}×{self.img.height()} · {k * 100:.0f} %")

    def _set(self, z):
        self.zoom = z
        self._render()

    def _step(self, f):
        self.zoom = max(0.05, min(8.0, self._scale() * f))
        self._render()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self._step(1.15 if e.angleDelta().y() > 0 else 1 / 1.15)
        else:
            super().wheelEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.zoom is None:
            self._render()

    def _save_as(self):
        p, _ = QFileDialog.getSaveFileName(self, "Сохранить снимок как", os.path.basename(self.path),
                                           "PNG (*.png);;JPEG (*.jpg)")
        if p:
            if p.lower().endswith(".jpg"):
                self.img.save(p, "JPG", 92)
            else:
                shutil.copyfile(self.path, p)


class GalleryWidget(QWidget):
    def __init__(self, dirs=None):
        super().__init__()
        self.dirs = dirs or DIRS
        self.rows = []
        self._thumbs = {}
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        h = QHBoxLayout()
        self.lbl = QLabel("")
        self.lbl.setWordWrap(True)
        b = QPushButton("Обновить")
        b.clicked.connect(self.refresh)
        h.addWidget(self.lbl, 1)
        h.addWidget(b)
        v.addLayout(h)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(THUMB + 6)
        self.table.setIconSize(QSize(THUMB, THUMB))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setWordWrap(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        self.table.setColumnWidth(0, THUMB + 12)
        self.table.cellDoubleClicked.connect(self._open)
        v.addWidget(self.table, 1)

    def _thumb(self, path):
        key = (path, os.path.getmtime(path))
        ic = self._thumbs.get(key)
        if ic is None:
            img = QImage(path)
            if img.isNull():
                return QIcon()
            ic = QIcon(QPixmap.fromImage(img.scaled(THUMB, THUMB, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
            self._thumbs[key] = ic
        return ic

    def refresh(self):
        self.rows = scan(self.dirs)[:400]
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(self.rows))
            for r, (p, kind, m) in enumerate(self.rows):
                it = QTableWidgetItem()
                it.setIcon(self._thumb(p))
                self.table.setItem(r, 0, it)
                f = m.get("freq")
                src = m.get("source", "")
                src = {"live": "живой приём"}.get(src, src)
                cells = [m.get("time", ""), m.get("sat", kind), f"{f / 1e6:.3f} МГц".replace(".", ",") if f else "",
                         tech_text(m), src, os.path.basename(p)]
                for c, txt in enumerate(cells, 1):
                    self.table.setItem(r, c, QTableWidgetItem(str(txt)))
            if self.rows:
                for c in range(1, len(COLS) - 1):
                    self.table.resizeColumnToContents(c)
        finally:
            self.table.setUpdatesEnabled(True)
        self.lbl.setText(f"Снимков: {len(self.rows)}. Папки: " + "; ".join(self.dirs.values())
                         + ". Двойной щелчок — открыть большим.")

    def _open(self, row, _col):
        if 0 <= row < len(self.rows):
            p, _k, m = self.rows[row]
            ImageViewer(self, p, f"{m.get('sat', '')} — {os.path.basename(p)}").exec()
