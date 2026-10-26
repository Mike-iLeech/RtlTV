package io.github.mikeileech.sdr4noob.ui

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Rect
import android.util.AttributeSet
import android.view.MotionEvent
import android.view.View
import io.github.mikeileech.sdr4noob.radio.RadioEngine
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToLong

/**
 * Спектр (сверху) и водопад (снизу) полосы приёмника — как в радиорежиме настольной версии.
 * Касание — настроиться на частоту (с привязкой к шагу), перетаскивание по горизонтали — сдвинуть полосу.
 */
class SpectrumView @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null) : View(context, attrs) {
    var engine: RadioEngine? = null
    /** Шаг привязки при касании, Гц. */
    var snapHz = 100_000L
    /** Полоса канала для подсветки, Гц. */
    var channelBw = 200_000
    /** Сдвиг середины полосы канала от VFO, Гц: у USB полоса над несущей, у LSB — под ней. */
    var channelShift = 0
    var onTuned: ((Long) -> Unit)? = null

    private val density = resources.displayMetrics.density
    private var spec = FloatArray(2048)
    private var smooth = FloatArray(2048)
    private var lastSerial = -1L
    private var center = 0L
    private var rate = 0
    private var floorDb = -90f

    private var wf: Bitmap? = null
    private var wfRow = 0
    private var rowPixels = IntArray(0)
    private val palette = IntArray(256) { paletteColor(it / 255f) }

    private val linePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.rgb(0x7f, 0xd1, 0xff); strokeWidth = 1.2f * density; style = Paint.Style.STROKE }
    private val fillPaint = Paint().apply { color = Color.argb(110, 0x1e, 0x5d, 0x8a); style = Paint.Style.FILL }
    private val gridPaint = Paint().apply { color = Color.argb(60, 255, 255, 255); strokeWidth = 1f }
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(200, 255, 255, 255); textSize = 11f * density }
    private val vfoPaint = Paint().apply { color = Color.rgb(255, 80, 80); strokeWidth = 1.5f * density }
    private val bandPaint = Paint().apply { color = Color.argb(50, 255, 80, 80) }
    private val bgPaint = Paint().apply { color = Color.rgb(0x15, 0x18, 0x1d) }
    private val path = Path()
    private val src = Rect()
    private val dst = Rect()

    private fun paletteColor(t: Float): Int {
        // чёрный → синий → голубой → жёлтый → красный (как в SDR#)
        val stops = arrayOf(floatArrayOf(0f, 0f, 0f, 0f), floatArrayOf(0.25f, 0f, 0f, 160f),
            floatArrayOf(0.5f, 0f, 200f, 255f), floatArrayOf(0.75f, 255f, 230f, 0f), floatArrayOf(1f, 255f, 40f, 0f))
        for (i in 1 until stops.size) {
            if (t <= stops[i][0]) {
                val a = stops[i - 1]; val b = stops[i]
                val k = (t - a[0]) / (b[0] - a[0])
                return Color.rgb((a[1] + (b[1] - a[1]) * k).toInt(), (a[2] + (b[2] - a[2]) * k).toInt(), (a[3] + (b[3] - a[3]) * k).toInt())
            }
        }
        return Color.rgb(255, 40, 0)
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        val wh = h - specHeight(h)
        if (w > 0 && wh > 0) {
            wf = Bitmap.createBitmap(min(w, 1024), max(1, wh / 2), Bitmap.Config.ARGB_8888).apply { eraseColor(Color.BLACK) }
            rowPixels = IntArray(wf!!.width)
            wfRow = 0
        }
    }

    private fun specHeight(h: Int) = (h * 0.42f).toInt()

    private fun freqToX(f: Long): Float = if (rate == 0) 0f else ((f - center).toFloat() / rate + 0.5f) * width
    private fun xToFreq(x: Float): Long = center + ((x / width - 0.5f) * rate).toLong()

    override fun onDraw(canvas: Canvas) {
        val e = engine
        val w = width; val h = height
        val sh = specHeight(h)
        canvas.drawRect(0f, 0f, w.toFloat(), h.toFloat(), bgPaint)
        if (e != null) {
            if (spec.size != e.spectrumSize) { spec = FloatArray(e.spectrumSize); smooth = FloatArray(e.spectrumSize) }
            val (c, r, serial) = e.copySpectrum(spec)
            if (serial != lastSerial && r > 0) {
                val fresh = lastSerial < 0 || c != center || r != rate
                lastSerial = serial; center = c; rate = r
                for (i in spec.indices) smooth[i] = if (fresh) spec[i] else smooth[i] * 0.5f + spec[i] * 0.5f
                updateFloor()
                addWaterfallRow()
            }
        }
        if (rate == 0) {
            canvas.drawText("Ожидание сигнала…", 16 * density, 24 * density, textPaint)
            return
        }
        // сетка частот
        val stepHz = gridStep(rate)
        var f = ((center - rate / 2) / stepHz + 1) * stepHz
        while (f < center + rate / 2) {
            val x = freqToX(f)
            canvas.drawLine(x, 0f, x, sh.toFloat(), gridPaint)
            canvas.drawText(fmtMhz(f), x + 3 * density, sh - 4 * density, textPaint)
            f += stepHz
        }
        // уровни dB
        val top = floorDb + 70f
        val bottom = floorDb - 10f
        var db = (bottom / 10).toInt() * 10f
        while (db < top) {
            val y = sh * (1 - (db - bottom) / (top - bottom))
            canvas.drawLine(0f, y, w.toFloat(), y, gridPaint)
            canvas.drawText("${db.toInt()} dB", 3 * density, y - 2 * density, textPaint)
            db += 10f
        }
        // полоса канала и VFO
        e?.let {
            val vx = freqToX(it.vfoHz)
            val bw = channelBw.toFloat() / rate * w
            val cx = freqToX(it.vfoHz + channelShift)
            canvas.drawRect(cx - bw / 2, 0f, cx + bw / 2, h.toFloat(), bandPaint)
            canvas.drawLine(vx, 0f, vx, h.toFloat(), vfoPaint)
        }
        // кривая спектра
        val n = smooth.size
        path.reset()
        path.moveTo(0f, sh.toFloat())
        for (i in 0 until n) {
            val x = i.toFloat() / (n - 1) * w
            val y = sh * (1 - (smooth[i] - bottom) / (top - bottom))
            path.lineTo(x, y.coerceIn(0f, sh.toFloat()))
        }
        path.lineTo(w.toFloat(), sh.toFloat())
        path.close()
        canvas.drawPath(path, fillPaint)
        canvas.drawPath(path, linePaint)
        // водопад: новая строка сверху
        wf?.let { bmp ->
            val bh = bmp.height
            val top0 = sh
            val rowsBelow = bh - wfRow    // строки wfRow..bh-1 — новые сверху вниз
            val scale = (h - sh).toFloat() / bh
            src.set(0, wfRow, bmp.width, bh); dst.set(0, top0, w, top0 + (rowsBelow * scale).toInt())
            canvas.drawBitmap(bmp, src, dst, null)
            src.set(0, 0, bmp.width, wfRow); dst.set(0, top0 + (rowsBelow * scale).toInt(), w, h)
            if (wfRow > 0) canvas.drawBitmap(bmp, src, dst, null)
            e?.let { canvas.drawLine(freqToX(it.vfoHz), top0.toFloat(), freqToX(it.vfoHz), h.toFloat(), vfoPaint) }
        }
    }

    private fun updateFloor() {
        val sorted = smooth.copyOf().also { it.sort() }
        val nf = sorted[sorted.size / 5]
        floorDb = if (floorDb < -150f) nf else floorDb * 0.95f + nf * 0.05f
    }

    private fun addWaterfallRow() {
        val bmp = wf ?: return
        val bw = bmp.width
        val n = spec.size
        val lo = floorDb - 5f
        val range = 55f
        for (x in 0 until bw) {
            val a = x * n / bw
            val b = max(a + 1, (x + 1) * n / bw)
            var m = -200f
            for (i in a until b) m = max(m, spec[i])
            val t = ((m - lo) / range).coerceIn(0f, 1f)
            rowPixels[x] = palette[(t * 255).toInt()]
        }
        wfRow = (wfRow - 1 + bmp.height) % bmp.height
        bmp.setPixels(rowPixels, 0, bw, 0, wfRow, bw, 1)
    }

    private fun gridStep(rate: Int): Long {
        val target = rate / 5.0
        val steps = longArrayOf(10_000, 20_000, 25_000, 50_000, 100_000, 200_000, 250_000, 500_000, 1_000_000)
        return steps.firstOrNull { it >= target } ?: 1_000_000
    }

    private fun fmtMhz(f: Long): String {
        val mhz = f / 1e6
        return if (f % 100_000 == 0L) "%.1f".format(mhz) else "%.3f".format(mhz)
    }

    // ---------- касания ----------
    private var downX = 0f
    private var downCenter = 0L
    private var dragging = false

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(ev: MotionEvent): Boolean {
        val e = engine ?: return false
        if (rate == 0) return true
        when (ev.actionMasked) {
            MotionEvent.ACTION_DOWN -> { downX = ev.x; downCenter = center; dragging = false; parent?.requestDisallowInterceptTouchEvent(true) }
            MotionEvent.ACTION_MOVE -> {
                if (!dragging && abs(ev.x - downX) > 12 * density) dragging = true
                if (dragging) {
                    val df = ((downX - ev.x) / width * rate).toLong()
                    val nc = ((downCenter + df) / 1000) * 1000
                    if (nc != center) { center = nc; e.setCenter(nc); lastSerial = -1; invalidate() }
                }
            }
            MotionEvent.ACTION_UP -> {
                if (!dragging) {
                    val f = xToFreq(ev.x)
                    val snapped = (f.toDouble() / snapHz).roundToLong() * snapHz
                    e.tune(snapped)
                    onTuned?.invoke(snapped)
                    invalidate()
                } else {
                    performClick()
                }
            }
        }
        return true
    }

    override fun performClick(): Boolean { super.performClick(); return true }
}
