package io.github.mikeileech.sdr4noob.ui

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.DashPathEffect
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.os.Handler
import android.os.Looper
import android.util.LruCache
import android.view.GestureDetector
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import android.view.View
import io.github.mikeileech.sdr4noob.i18n.tr
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors
import kotlin.math.PI
import kotlin.math.atan
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.ln
import kotlin.math.log2
import kotlin.math.roundToInt
import kotlin.math.sin
import kotlin.math.sinh

/** Объект на карте (как словарь set_items в rtltv/mapview.py). */
data class MapItem(
    val key: String, val lat: Double, val lon: Double, val kind: Kind = Kind.POINT,
    val heading: Double? = null, val label: String? = null, val color: Int = 0xFFFFD84D.toInt(),
    val trail: List<Pair<Double, Double>> = emptyList(),
) {
    enum class Kind { PLANE, POINT, SAT }
}

/** Линия на карте (трасса спутника, путь станции APRS). */
data class MapTrack(val points: List<Pair<Double, Double>>, val color: Int = 0xFFFFD84D.toInt(),
                    val width: Float = 1.5f, val dash: Boolean = false)

/**
 * Карта OpenStreetMap — перенос rtltv/mapview.py: тайлы скачиваются в фоне и хранятся в кэше приложения,
 * карту можно двигать пальцем и масштабировать щипком или двойным касанием. Слои: дом с кольцами дальности,
 * линии и объекты (самолёты, точки, спутники). Подпись «© участники OpenStreetMap» обязательна.
 */
class MapView(ctx: Context) : View(ctx) {
    companion object {
        const val TILE = 256
        private const val TILE_URL = "https://tile.openstreetmap.org/%d/%d/%d.png"
        private val USER_AGENT = "SDR4NOOB/${io.github.mikeileech.sdr4noob.BuildConfig.VERSION_NAME} " +
            "(Android; +https://github.com/Mike-iLeech/SDR4NOOB)"
        private val pool = Executors.newFixedThreadPool(2) { r -> Thread(r, "map-tiles").apply { isDaemon = true } }

        fun lonLatToPx(lon: Double, lat0: Double, z: Int): Pair<Double, Double> {
            val n = TILE * Math.pow(2.0, z.toDouble())
            val lat = lat0.coerceIn(-85.0511, 85.0511)
            val s = sin(Math.toRadians(lat))
            return (lon + 180.0) / 360.0 * n to (0.5 - ln((1 + s) / (1 - s)) / (4 * PI)) * n
        }

        fun pxToLonLat(x: Double, y: Double, z: Int): Pair<Double, Double> {
            val n = TILE * Math.pow(2.0, z.toDouble())
            return x / n * 360.0 - 180.0 to Math.toDegrees(atan(sinh(PI * (1 - 2 * y / n))))
        }
    }

    var onClick: ((lat: Double, lon: Double) -> Unit)? = null
    var onItemClick: ((key: String) -> Unit)? = null
    /** Карта сдвинута пальцем — режимы перестают «вести» её за объектом. */
    var onUserMove: (() -> Unit)? = null

    var zoom = 6; private set
    private var cx: Double
    private var cy: Double
    private var home: Pair<Double, Double>? = null
    private var rings = intArrayOf(50, 100, 200)
    private val items = LinkedHashMap<String, List<MapItem>>()
    private val tracks = LinkedHashMap<String, List<MapTrack>>()
    var selected: String? = null
        set(v) { field = v; invalidate() }

    private val cacheDir = File(ctx.cacheDir, "tiles")
    // ~24 МБ растров (тайл 256×256 ARGB — 256 КБ): хватает на экран и соседние масштабы
    private val mem = object : LruCache<Long, Bitmap>(96 * 256 * 1024) {
        override fun sizeOf(key: Long, value: Bitmap) = value.byteCount
    }
    private val pending = HashSet<Long>()
    private val failed = HashSet<Long>()
    private val main = Handler(Looper.getMainLooper())
    private val d = resources.displayMetrics.density

    private val pTile = Paint(Paint.FILTER_BITMAP_FLAG)
    private val pFill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val pLine = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE }
    private val pText = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.WHITE; textSize = 11f * d }
    private val plane = Path()

    init {
        val (x, y) = lonLatToPx(37.62, 55.75, zoom)          // Москва по умолчанию, как на ПК
        cx = x; cy = y
        val s = 9f * d
        val pts = listOf(0f to -s, .12f * s to -.55f * s, .12f * s to -.15f * s, .95f * s to .25f * s, .95f * s to .4f * s,
            .12f * s to .18f * s, .1f * s to .7f * s, .38f * s to .9f * s, .38f * s to 1f * s, 0f to .9f * s)
        val full = pts + pts.reversed().map { -it.first to it.second }
        plane.moveTo(full[0].first, full[0].second)
        for (p in full.drop(1)) plane.lineTo(p.first, p.second)
        plane.close()
    }

    fun setCenter(lat: Double, lon: Double, z: Int? = null) {
        if (z != null) zoom = z.coerceIn(2, 18)
        val (x, y) = lonLatToPx(lon, lat, zoom)
        cx = x; cy = y
        invalidate()
    }

    fun center(): Pair<Double, Double> = pxToLonLat(cx, cy, zoom).let { it.second to it.first }

    fun setHome(lat: Double, lon: Double, ringsKm: IntArray = rings) { home = lat to lon; rings = ringsKm; invalidate() }
    fun setItems(layer: String, list: List<MapItem>) { items[layer] = list; invalidate() }
    fun setTracks(layer: String, list: List<MapTrack>) { tracks[layer] = list; invalidate() }

    // ---------------- тайлы ----------------
    private fun key(z: Int, x: Int, y: Int) = (z.toLong() shl 50) or (x.toLong() shl 25) or y.toLong()

    private fun tile(z: Int, x: Int, y: Int): Bitmap? {
        val k = key(z, x, y)
        mem.get(k)?.let { return it }
        if (k in pending || k in failed || pending.size >= 12) return null
        pending += k
        pool.execute {
            val f = File(cacheDir, "$z/$x/$y.png")
            var bmp: Bitmap? = null
            try {
                if (f.exists() && System.currentTimeMillis() - f.lastModified() < 30L * 86_400_000) bmp = BitmapFactory.decodeFile(f.path)
                if (bmp == null) {
                    val c = URL(String.format(java.util.Locale.ROOT, TILE_URL, z, x, y)).openConnection() as HttpURLConnection
                    c.setRequestProperty("User-Agent", USER_AGENT)
                    c.connectTimeout = 8000; c.readTimeout = 10000
                    try {
                        if (c.responseCode == 200) {
                            val data = c.inputStream.use { it.readBytes() }
                            bmp = BitmapFactory.decodeByteArray(data, 0, data.size)
                            if (bmp != null) runCatching { f.parentFile?.mkdirs(); f.writeBytes(data) }
                        }
                    } finally { c.disconnect() }
                }
                // старый тайл лучше, чем никакого (нет сети)
                if (bmp == null && f.exists()) bmp = BitmapFactory.decodeFile(f.path)
            } catch (_: Throwable) {          // в том числе нехватка памяти: тайл просто не нарисуется
                bmp = null
            }
            val b = bmp
            main.post {
                pending -= k
                if (b != null) mem.put(k, b) else { failed += k; main.postDelayed({ failed -= k }, 30_000) }
                invalidate()
            }
        }
        return null
    }

    // ---------------- жесты ----------------
    private val scaler = ScaleGestureDetector(ctx, object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
        var acc = 1f
        override fun onScaleBegin(g: ScaleGestureDetector): Boolean { acc = 1f; return true }
        override fun onScale(g: ScaleGestureDetector): Boolean {
            acc *= g.scaleFactor
            val steps = log2(acc.toDouble()).roundToInt()
            if (steps != 0) { zoomAt(zoom + steps, g.focusX, g.focusY); acc /= Math.pow(2.0, steps.toDouble()).toFloat() }
            return true
        }
    })
    private val gestures = GestureDetector(ctx, object : GestureDetector.SimpleOnGestureListener() {
        override fun onDown(e: MotionEvent) = true
        override fun onScroll(e1: MotionEvent?, e2: MotionEvent, dx: Float, dy: Float): Boolean {
            val n = worldPx()
            cx = ((cx + dx) % n + n) % n
            cy = (cy + dy).coerceIn(0.0, n)
            onUserMove?.invoke()
            invalidate()
            return true
        }
        override fun onDoubleTap(e: MotionEvent): Boolean { zoomAt(zoom + 1, e.x, e.y); return true }
        override fun onSingleTapConfirmed(e: MotionEvent): Boolean {
            hit(e.x, e.y)?.let { onItemClick?.invoke(it); return true }
            val (lon, lat) = pxToLonLat(cx - width / 2.0 + e.x, cy - height / 2.0 + e.y, zoom)
            onClick?.invoke(lat, lon)
            return true
        }
    })

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(e: MotionEvent): Boolean {
        parent?.requestDisallowInterceptTouchEvent(true)
        scaler.onTouchEvent(e)
        if (!scaler.isInProgress) gestures.onTouchEvent(e)
        return true
    }

    private fun worldPx() = TILE * Math.pow(2.0, zoom.toDouble())

    fun zoomBy(steps: Int) = zoomAt(zoom + steps, width / 2f, height / 2f)

    private fun zoomAt(nz0: Int, fx: Float, fy: Float) {
        val nz = nz0.coerceIn(2, 18)
        if (nz == zoom) return
        val (lon, lat) = pxToLonLat(cx - width / 2.0 + fx, cy - height / 2.0 + fy, zoom)
        zoom = nz
        val (x, y) = lonLatToPx(lon, lat, zoom)
        cx = x - fx + width / 2.0; cy = y - fy + height / 2.0
        invalidate()
    }

    private fun toScreen(lat: Double, lon: Double): Pair<Float, Float> {
        val (x, y) = lonLatToPx(lon, lat, zoom)
        val n = worldPx()
        var sx = x - cx
        while (sx > n / 2) sx -= n
        while (sx < -n / 2) sx += n
        return (sx + width / 2.0).toFloat() to (y - cy + height / 2.0).toFloat()
    }

    private fun hit(x: Float, y: Float): String? {
        var best: String? = null; var bd = (24f * d) * (24f * d)
        for (list in items.values) for (it in list) {
            val (sx, sy) = toScreen(it.lat, it.lon)
            val dd = (sx - x) * (sx - x) + (sy - y) * (sy - y)
            if (dd < bd) { bd = dd; best = it.key }
        }
        return best
    }

    // ---------------- рисование ----------------
    override fun onDraw(c: Canvas) {
        c.drawColor(0xFF1B2128.toInt())
        val n = 1 shl zoom
        val x0 = cx - width / 2.0; val y0 = cy - height / 2.0
        val tx0 = floor(x0 / TILE).toInt(); val ty0 = floor(y0 / TILE).toInt()
        val tx1 = floor((x0 + width) / TILE).toInt(); val ty1 = floor((y0 + height) / TILE).toInt()
        val dst = RectF()
        pFill.color = 0xFF262E37.toInt(); pFill.style = Paint.Style.FILL
        for (ty in ty0..ty1) {
            if (ty < 0 || ty >= n) continue
            for (tx in tx0..tx1) {
                val rx = (tx * TILE - x0).toFloat(); val ry = (ty * TILE - y0).toFloat()
                dst.set(rx, ry, rx + TILE, ry + TILE)
                val b = tile(zoom, ((tx % n) + n) % n, ty)
                if (b != null) c.drawBitmap(b, null, dst, pTile) else c.drawRect(dst, pFill)
            }
        }
        c.drawColor(0x5A000000)                                // приглушить светлую карту под яркие значки
        drawHome(c)
        for (list in tracks.values) for (t in list) drawTrack(c, t)
        for (list in items.values) for (it in list) drawItem(c, it)
        pText.textSize = 10f * d; pText.color = Color.WHITE; pText.textAlign = Paint.Align.RIGHT
        c.drawText(tr("© участники OpenStreetMap"), width - 6f * d, height - 6f * d, pText)
        pText.textAlign = Paint.Align.LEFT
    }

    private fun drawHome(c: Canvas) {
        val h = home ?: return
        val (sx, sy) = toScreen(h.first, h.second)
        val mpp = 156543.03392 * cos(Math.toRadians(h.first)) / Math.pow(2.0, zoom.toDouble()) / d * d
        pLine.color = Ui.ACCENT; pLine.strokeWidth = 1.6f * d; pLine.pathEffect = DashPathEffect(floatArrayOf(8 * d, 6 * d), 0f)
        pText.textSize = 10f * d; pText.color = Ui.ACCENT
        for (km in rings) {
            val r = (km * 1000 / mpp).toFloat()
            if (r < 15 || r > 6000) continue
            c.drawCircle(sx, sy, r, pLine)
            c.drawText(tr("{km} км", "km" to km), sx + 3, sy - r - 3, pText)
        }
        pLine.pathEffect = null
        pFill.color = Ui.ACCENT
        c.drawCircle(sx, sy, 5f * d, pFill)
    }

    private fun drawTrack(c: Canvas, t: MapTrack) {
        if (t.points.size < 2) return
        pLine.color = t.color; pLine.strokeWidth = t.width * d
        pLine.pathEffect = if (t.dash) DashPathEffect(floatArrayOf(8 * d, 6 * d), 0f) else null
        val path = Path()
        var prev: Float? = null
        val n = worldPx().toFloat()
        for ((lat, lon) in t.points) {
            var (x, y) = toScreen(lat, lon)
            prev?.let { p -> while (x - p > n / 2) x -= n; while (x - p < -n / 2) x += n }
            if (prev == null) path.moveTo(x, y) else path.lineTo(x, y)
            prev = x
        }
        c.drawPath(path, pLine)
        pLine.pathEffect = null
    }

    private fun drawItem(c: Canvas, it: MapItem) {
        val (sx, sy) = toScreen(it.lat, it.lon)
        if (sx < -60 || sy < -60 || sx > width + 60 || sy > height + 60) return
        if (it.trail.size > 1) {
            pLine.color = (it.color and 0x00FFFFFF) or 0x8C000000.toInt(); pLine.strokeWidth = 1.5f * d
            val path = Path()
            it.trail.forEachIndexed { i, (la, lo) -> val (x, y) = toScreen(la, lo); if (i == 0) path.moveTo(x, y) else path.lineTo(x, y) }
            c.drawPath(path, pLine)
        }
        val sel = it.key == selected
        c.save(); c.translate(sx, sy)
        pFill.color = it.color
        pLine.color = if (sel) Color.WHITE else Color.BLACK; pLine.strokeWidth = (if (sel) 1.8f else 0.8f) * d
        when (it.kind) {
            MapItem.Kind.PLANE -> { c.rotate((it.heading ?: 0.0).toFloat()); c.drawPath(plane, pFill); c.drawPath(plane, pLine) }
            MapItem.Kind.SAT -> {
                val s = 4 * d
                c.drawRect(-s, -s, s, s, pFill); c.drawRect(-15 * d / 4 * 4 / 4 * 1f - 11 * d, -3 * d, -10 * d, 3 * d, pFill)
                c.drawRect(10 * d, -3 * d, 15 * d, 3 * d, pFill)
            }
            MapItem.Kind.POINT -> { c.drawCircle(0f, 0f, 4.5f * d, pFill); c.drawCircle(0f, 0f, 4.5f * d, pLine) }
        }
        c.restore()
        val label = it.label ?: return
        pText.textSize = 10f * d; pText.isFakeBoldText = sel; pText.color = Color.WHITE
        val lines = label.split('\n')
        val lh = pText.fontSpacing
        val tw = lines.maxOf { pText.measureText(it) } + 6 * d
        val th = lh * lines.size + 2 * d
        val r = RectF(sx + 12 * d, sy - th / 2, sx + 12 * d + tw, sy + th / 2)
        pFill.color = 0xBE1B2128.toInt()
        c.drawRoundRect(r, 3 * d, 3 * d, pFill)
        lines.forEachIndexed { i, s -> c.drawText(s, r.left + 3 * d, r.top + lh * (i + 1) - pText.descent() + d, pText) }
        pText.isFakeBoldText = false
    }
}
