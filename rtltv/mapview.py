"""Карта: тайлы OpenStreetMap (кэш на диске) + слои объектов (самолёты, спутники, точки, линии).

API для режимов:
    m = MapView()
    m.set_center(lat, lon, zoom=None)
    m.set_home(lat, lon, rings_km=(50, 100, 200))   # дом + кольца дальности
    m.set_items("planes", [ {key, lat, lon, kind, heading, label, color, trail} ])
        kind: "plane" | "sat" | "point";  heading — градусы (0 = север), trail — [(lat, lon), ...]
    m.set_tracks("orbits", [ {points: [(lat, lon), ...], color, width, dash} ])
    m.set_selected(key)
    сигналы: clicked(lat, lon), itemClicked(key)
"""
import math
import os

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import QWidget
from . import paths

TILE = 256
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "RtlTV/1.1 (personal SDR receiver; Windows)"
CACHE_DIR = paths.service("tiles")


def lonlat_to_px(lon, lat, z):
    n = TILE * (2 ** z)
    lat = max(min(lat, 85.0511), -85.0511)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def px_to_lonlat(x, y, z):
    n = TILE * (2 ** z)
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon, lat


def _plane_path(size):
    s = size
    pts = [(0, -s), (0.12 * s, -0.55 * s), (0.12 * s, -0.15 * s), (0.95 * s, 0.25 * s), (0.95 * s, 0.4 * s),
           (0.12 * s, 0.18 * s), (0.1 * s, 0.7 * s), (0.38 * s, 0.9 * s), (0.38 * s, 1.0 * s), (0, 0.9 * s)]
    full = pts + [(-x, y) for x, y in reversed(pts)]
    return QPolygonF([QPointF(x, y) for x, y in full])


class MapView(QWidget):
    clicked = Signal(float, float)
    itemClicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 200)
        self.setMouseTracking(True)
        self.zoom = 6
        self.cx, self.cy = lonlat_to_px(37.62, 55.75, self.zoom)  # Москва по умолчанию
        self.tiles = {}
        self.pending = set()
        self.failed = set()
        self.items = {}
        self.tracks = {}
        self.home = None
        self.rings = ()
        self.selected = None
        self._drag = None
        self._moved = False
        self.colors = dict(bg="#1d2128", fg="#d8dee9", accent="#4fb3ff", label_bg="#000000")
        self.dim_tiles = True
        self.net = QNetworkAccessManager(self)
        self.net.finished.connect(self._tile_loaded)
        os.makedirs(CACHE_DIR, exist_ok=True)
        self._plane = _plane_path(9)
        self._retry = QTimer(self)
        self._retry.timeout.connect(self._clear_failed)
        self._retry.start(30000)

    # ---------- API ----------
    def set_theme(self, t):
        self.colors = dict(bg=t["bg"], fg=t["fg"], accent=t["accent"], label_bg=t["panel"])
        self.dim_tiles = t.get("bg", "#000").lower() < "#808080"
        self.update()

    def set_center(self, lat, lon, zoom=None):
        if zoom is not None:
            self.zoom = int(max(2, min(18, zoom)))
        self.cx, self.cy = lonlat_to_px(lon, lat, self.zoom)
        self.update()

    def center(self):
        lon, lat = px_to_lonlat(self.cx, self.cy, self.zoom)
        return lat, lon

    def set_home(self, lat, lon, rings_km=(50, 100, 200)):
        self.home = (lat, lon)
        self.rings = tuple(rings_km)
        self.update()

    def set_items(self, layer, items):
        self.items[layer] = list(items)
        self.update()

    def set_tracks(self, layer, tracks):
        self.tracks[layer] = list(tracks)
        self.update()

    def set_selected(self, key):
        self.selected = key
        self.update()

    # ---------- тайлы ----------
    def _cache_path(self, z, x, y):
        return os.path.join(CACHE_DIR, str(z), str(x), f"{y}.png")

    def _tile(self, z, x, y):
        key = (z, x, y)
        img = self.tiles.get(key)
        if img is not None:
            return img
        path = self._cache_path(z, x, y)
        if os.path.exists(path):
            img = QImage(path)
            if not img.isNull():
                if len(self.tiles) > 600:
                    self.tiles.clear()
                self.tiles[key] = img
                return img
        if key not in self.pending and key not in self.failed and len(self.pending) < 12:
            self.pending.add(key)
            req = QNetworkRequest(QUrl(TILE_URL.format(z=z, x=x, y=y)))
            req.setRawHeader(b"User-Agent", USER_AGENT.encode())
            req.setAttribute(QNetworkRequest.User, key)
            self.net.get(req)
        return None

    def _tile_loaded(self, reply):
        key = reply.request().attribute(QNetworkRequest.User)
        self.pending.discard(key)
        if reply.error() == QNetworkReply.NoError:
            data = bytes(reply.readAll())
            img = QImage.fromData(data)
            if not img.isNull():
                self.tiles[key] = img
                path = self._cache_path(*key)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                try:
                    with open(path, "wb") as f:
                        f.write(data)
                except OSError:
                    pass
                self.update()
        else:
            self.failed.add(key)
        reply.deleteLater()

    def _clear_failed(self):
        if self.failed:
            self.failed.clear()
            self.update()

    # ---------- геометрия ----------
    def to_screen(self, lat, lon):
        x, y = lonlat_to_px(lon, lat, self.zoom)
        n = TILE * (2 ** self.zoom)
        dx = x - self.cx
        if dx > n / 2:
            x -= n
        elif dx < -n / 2:
            x += n
        return QPointF(x - self.cx + self.width() / 2, y - self.cy + self.height() / 2)

    def to_latlon(self, sx, sy):
        x = self.cx + sx - self.width() / 2
        y = self.cy + sy - self.height() / 2
        lon, lat = px_to_lonlat(x, y, self.zoom)
        return lat, ((lon + 180) % 360) - 180

    # ---------- мышь ----------
    def mousePressEvent(self, e):
        self._drag = (e.position(), self.cx, self.cy)
        self._moved = False

    def mouseMoveEvent(self, e):
        if self._drag and e.buttons() & Qt.LeftButton:
            p0, cx, cy = self._drag
            d = e.position() - p0
            if abs(d.x()) + abs(d.y()) > 3:
                self._moved = True
            self.cx = cx - d.x()
            self.cy = min(max(cy - d.y(), 0), TILE * (2 ** self.zoom))
            self.update()

    def mouseReleaseEvent(self, e):
        if self._drag and not self._moved:
            key = self._hit(e.position())
            if key:
                self.itemClicked.emit(key)
            else:
                lat, lon = self.to_latlon(e.position().x(), e.position().y())
                self.clicked.emit(lat, lon)
        self._drag = None

    def wheelEvent(self, e):
        old = self.zoom
        nz = max(2, min(18, old + (1 if e.angleDelta().y() > 0 else -1)))
        if nz == old:
            return
        # зум к курсору
        pos = e.position()
        lat, lon = self.to_latlon(pos.x(), pos.y())
        self.zoom = nz
        x, y = lonlat_to_px(lon, lat, nz)
        self.cx = x - (pos.x() - self.width() / 2)
        self.cy = y - (pos.y() - self.height() / 2)
        self.update()

    def _hit(self, pos):
        best, bd = None, 14.0
        for items in self.items.values():
            for it in items:
                base = self.to_screen(it["lat"], it["lon"])
                d = min(math.hypot(base.x() + dx - pos.x(), base.y() - pos.y()) for dx in (self._copies(base) or [0]))
                if d < bd:
                    best, bd = it.get("key"), d
        return best

    # ---------- рисование ----------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(self.colors["bg"]))
        z = self.zoom
        n = 2 ** z
        w, h = self.width(), self.height()
        x0 = self.cx - w / 2
        y0 = self.cy - h / 2
        tx0, ty0 = int(math.floor(x0 / TILE)), int(math.floor(y0 / TILE))
        tx1, ty1 = int(math.floor((x0 + w) / TILE)), int(math.floor((y0 + h) / TILE))
        for ty in range(ty0, ty1 + 1):
            if ty < 0 or ty >= n:
                continue
            for tx in range(tx0, tx1 + 1):
                img = self._tile(z, tx % n, ty)
                rx, ry = tx * TILE - x0, ty * TILE - y0
                if img is not None:
                    p.drawImage(QRectF(rx, ry, TILE, TILE), img)
                else:
                    p.fillRect(QRectF(rx, ry, TILE, TILE), QColor(self.colors["bg"]).lighter(115))
        if self.dim_tiles:
            p.fillRect(self.rect(), QColor(0, 0, 0, 95))
        self._draw_home(p)
        for tracks in self.tracks.values():
            for tr in tracks:
                self._draw_track(p, tr)
        for items in self.items.values():
            for it in items:
                for dx in self._copies(self.to_screen(it["lat"], it["lon"])):
                    self._draw_item(p, it, dx)
        # копирайт OSM обязателен
        f = QFont(self.font())
        f.setPointSizeF(7.5)
        p.setFont(f)
        p.setPen(QColor(self.colors["fg"]))
        p.drawText(QRectF(0, h - 16, w - 6, 14), Qt.AlignRight, "© участники OpenStreetMap")

    def _draw_home(self, p):
        if not self.home:
            return
        c = self.to_screen(*self.home)
        acc = QColor(self.colors["accent"])
        lat = self.home[0]
        mpp = 156543.03392 * math.cos(math.radians(lat)) / (2 ** self.zoom)
        f = QFont(self.font())
        f.setPointSizeF(8)
        p.setFont(f)
        for km in self.rings:
            r = km * 1000 / mpp
            if r < 15 or r > 6000:
                continue
            pen = QPen(acc, 1.6, Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, r, r)
            p.drawText(QPointF(c.x() + 3, c.y() - r - 3), f"{km} км")
        p.setPen(QPen(QColor("#000"), 1))
        p.setBrush(acc)
        p.drawEllipse(c, 5, 5)

    def _draw_track(self, p, tr):
        """Трасса разворачивается по долготе непрерывно и рисуется во всех видимых копиях мира
        (при мелком масштабе окно шире одного «мира» — иначе трассы обрываются у краёв)."""
        pts = tr.get("points") or []
        if len(pts) < 2:
            return
        pen = QPen(QColor(tr.get("color", "#ffd84d")), tr.get("width", 1.5))
        if tr.get("dash"):
            pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        n = TILE * (2 ** self.zoom)
        ox, oy = self.width() / 2 - self.cx, self.height() / 2 - self.cy
        path = QPainterPath()
        prev = None
        for lat, lon in pts:
            x, y = lonlat_to_px(lon, lat, self.zoom)
            if prev is not None:
                while x - prev > n / 2:
                    x -= n
                while x - prev < -n / 2:
                    x += n
            if prev is None:
                path.moveTo(x + ox, y + oy)
            else:
                path.lineTo(x + ox, y + oy)
            prev = x
        p.setBrush(Qt.NoBrush)
        br = path.boundingRect()
        view = QRectF(-20, -20, self.width() + 40, self.height() + 40)
        k0 = int(math.floor((view.left() - br.right()) / n))
        k1 = int(math.ceil((view.right() - br.left()) / n))
        for k in range(k0, k1 + 1):
            if br.translated(k * n, 0).intersects(view):
                p.drawPath(path.translated(k * n, 0))

    def _copies(self, s):
        """Сдвиги (в пикселях) копий мира, в которых точка s видна в окне."""
        n = TILE * (2 ** self.zoom)
        out = []
        for k in range(-3, 4):
            x = s.x() + k * n
            if -60 <= x <= self.width() + 60:
                out.append(k * n)
        return out

    def _draw_item(self, p, it, dx=0.0):
        s = self.to_screen(it["lat"], it["lon"]) + QPointF(dx, 0)
        if not QRectF(-50, -50, self.width() + 100, self.height() + 100).contains(s):
            return
        col = QColor(it.get("color", "#ffd84d"))
        trail = it.get("trail") or []
        if len(trail) > 1:
            c2 = QColor(col)
            c2.setAlpha(140)
            p.setPen(QPen(c2, 1.5))
            path = QPainterPath(self.to_screen(*trail[0]) + QPointF(dx, 0))
            for lat, lon in trail[1:]:
                path.lineTo(self.to_screen(lat, lon) + QPointF(dx, 0))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        sel = it.get("key") is not None and it.get("key") == self.selected
        kind = it.get("kind", "point")
        p.save()
        p.translate(s)
        if kind == "plane":
            p.rotate(it.get("heading") or 0.0)
            p.setPen(QPen(QColor("#ffffff") if sel else QColor("#000000"), 1.5 if sel else 0.8))
            p.setBrush(col)
            p.drawPolygon(self._plane)
        elif kind == "sat":
            p.setPen(QPen(QColor("#000"), 1))
            p.setBrush(col)
            p.drawRect(QRectF(-4, -4, 8, 8))
            p.drawLine(QPointF(-11, 0), QPointF(-4, 0))
            p.drawLine(QPointF(4, 0), QPointF(11, 0))
            p.drawRect(QRectF(-15, -3, 5, 6))
            p.drawRect(QRectF(10, -3, 5, 6))
        else:
            p.setPen(QPen(QColor("#000"), 1))
            p.setBrush(col)
            p.drawEllipse(QPointF(0, 0), 4.5, 4.5)
        p.restore()
        label = it.get("label")
        if label:
            f = QFont(self.font())
            f.setPointSizeF(8)
            f.setBold(sel)
            p.setFont(f)
            lines = str(label).split("\n")
            tw = max(p.fontMetrics().horizontalAdvance(t) for t in lines) + 6
            th = p.fontMetrics().height() * len(lines) + 2
            r = QRectF(s.x() + 12, s.y() - th / 2, tw, th)
            bg = QColor(self.colors["label_bg"])
            bg.setAlpha(190)
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(r, 3, 3)
            p.setPen(QColor("#ffffff") if sel else QColor(self.colors["fg"]))
            p.drawText(r.adjusted(3, 1, 0, 0), Qt.AlignLeft | Qt.AlignTop, label)
