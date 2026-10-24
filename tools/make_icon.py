"""Собрать rtltv/assets/sdr4noob.ico (иконка EXE) из логотипа сайта rtltv/assets/sdr4noob.svg.

    py -3.12 tools/make_icon.py

Нужны PySide6 (рисует SVG) и Pillow (пишет ICO с размерами 16…256)."""
import io
import os
import sys

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SVG = os.path.join(ROOT, "rtltv", "assets", "sdr4noob.svg")
ICO = os.path.join(ROOT, "rtltv", "assets", "sdr4noob.ico")
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def render(size):
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(SVG).render(p)
    p.end()
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA")


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication(sys.argv)
    imgs = [render(s) for s in SIZES]
    imgs[-1].save(ICO, format="ICO", sizes=[(s, s) for s in SIZES], append_images=imgs[:-1])
    print(ICO, os.path.getsize(ICO), "байт")
    del app


if __name__ == "__main__":
    main()
