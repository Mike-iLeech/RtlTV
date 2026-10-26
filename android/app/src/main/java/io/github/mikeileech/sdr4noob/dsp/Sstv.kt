package io.github.mikeileech.sdr4noob.dsp

import kotlin.math.PI
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin

/**
 * SSTV — перенос rtltv/sstv.py: режимы (раскладки строк, VIS-коды, тайминги), цветовые формулы
 * и кодер тестового сигнала. Потоковый декодер — [SstvDecoder] (SstvDecoder.kt).
 *
 * Картинки — IntArray ARGB построчно (ширина × высота), без Android Bitmap: всё считается на JVM.
 */

/** Кусок раскладки строки: вид ("sync", "gap", "sepC" или канал R/G/B/Y/Y0/Y1/U/V/C), длительность (с), частота тона. */
class SstvSeg(val kind: String, val dur: Double, val freq: Double?)

/** Канал строки: вид, начало относительно синхроимпульса (с), длительность (с). */
class SstvChan(val kind: String, val off: Double, val dur: Double)

/** Описание режима SSTV (как словарь режима в sstv.py). */
class SstvMode(
    val name: String, val vis: Int, val width: Int, val height: Int,
    /** "rgb", "pd", "r72" или "r36". */
    val color: String,
    /** Длительность строки передачи, с. */
    val line: Double,
    /** Длительность строчного синхроимпульса, с. */
    val sync: Double,
    /** Строк передачи (у PD одна строка передачи несёт две строки картинки). */
    val lines: Int,
    val rowsPerLine: Int,
    val layout: List<SstvSeg>,
    val prefix: List<SstvSeg>,
    val channels: List<SstvChan>,
    /** Разделитель цветности Robot 36: (начало от синхро, длительность), иначе null. */
    val sep: DoubleArray?,
    /** Первый синхроимпульс от конца VIS (стоп-бита), с. */
    val firstSync: Double,
    /** Полная длительность передачи (VIS + картинка), с. */
    val duration: Double,
)

object Sstv {
    const val F_SYNC = 1200.0
    const val F_BLACK = 1500.0
    const val F_WHITE = 2300.0
    const val F_LEADER = 1900.0
    const val F_BIT1 = 1100.0
    const val F_BIT0 = 1300.0
    const val F_SPAN = F_WHITE - F_BLACK

    // ------------------------------------------------------------ режимы

    val CHANNELS = setOf("R", "G", "B", "Y", "Y0", "Y1", "U", "V", "C")

    private fun s(k: String, d: Double, f: Double?) = SstvSeg(k, d, f)

    private fun martin(ch: Double) = listOf(
        s("sync", 4.862, F_SYNC), s("gap", 0.572, F_BLACK),
        s("G", ch, null), s("gap", 0.572, F_BLACK),
        s("B", ch, null), s("gap", 0.572, F_BLACK),
        s("R", ch, null), s("gap", 0.572, F_BLACK))

    private fun scottie(ch: Double) = listOf(
        s("gap", 1.5, F_BLACK), s("G", ch, null), s("gap", 1.5, F_BLACK),
        s("B", ch, null), s("sync", 9.0, F_SYNC), s("gap", 1.5, F_BLACK),
        s("R", ch, null))

    private fun pd(ch: Double) = listOf(
        s("sync", 20.0, F_SYNC), s("gap", 2.08, F_BLACK),
        s("Y0", ch, null), s("V", ch, null), s("U", ch, null), s("Y1", ch, null))

    private val ROBOT36 = listOf(s("sync", 9.0, F_SYNC), s("gap", 3.0, F_BLACK), s("Y", 88.0, null),
        s("sepC", 4.5, null), s("gap", 1.5, F_LEADER), s("C", 44.0, null))
    private val ROBOT72 = listOf(s("sync", 9.0, F_SYNC), s("gap", 3.0, F_BLACK), s("Y", 138.0, null),
        s("gap", 4.5, F_BLACK), s("gap", 1.5, F_LEADER), s("V", 69.0, null),
        s("gap", 4.5, F_WHITE), s("gap", 1.5, F_BLACK), s("U", 69.0, null))
    private val WRAASE180 = listOf(s("sync", 5.5225, F_SYNC), s("gap", 0.5, F_BLACK),
        s("R", 235.0, null), s("G", 235.0, null), s("B", 235.0, null))

    /** Собирает описание режима из раскладки строки (длительности в мс) — как _mk. */
    private fun mk(name: String, vis: Int, w: Int, h: Int, layoutMs: List<SstvSeg>, color: String,
                   prefixMs: List<SstvSeg> = emptyList(), rowsPerLine: Int = 1): SstvMode {
        val layout = layoutMs.map { SstvSeg(it.kind, it.dur / 1000.0, it.freq) }
        val prefix = prefixMs.map { SstvSeg(it.kind, it.dur / 1000.0, it.freq) }
        var t = 0.0
        var syncAt: Double? = null
        val starts = DoubleArray(layout.size)
        for ((i, sg) in layout.withIndex()) {
            starts[i] = t
            if (sg.kind == "sync" && syncAt == null) syncAt = t
            t += sg.dur
        }
        val line = t
        val sa = syncAt!!
        val chans = ArrayList<SstvChan>()
        var sep: DoubleArray? = null
        for ((i, sg) in layout.withIndex()) {
            if (sg.kind in CHANNELS) chans.add(SstvChan(sg.kind, starts[i] - sa, sg.dur))
            else if (sg.kind == "sepC") sep = doubleArrayOf(starts[i] - sa, sg.dur)
        }
        val lines = h / rowsPerLine
        var pre = 0.0
        for (sg in prefix) pre += sg.dur
        return SstvMode(name, vis, w, h, color, line, layout.first { it.kind == "sync" }.dur, lines, rowsPerLine,
            layout, prefix, chans, sep, pre + sa, 0.91 + pre + lines * line)
    }

    /** Режимы в порядке sstv.MODES. */
    val MODES: Map<String, SstvMode> = LinkedHashMap<String, SstvMode>().apply {
        for (m in listOf(
            mk("Robot 36", 8, 320, 240, ROBOT36, "r36"),
            mk("Robot 72", 12, 320, 240, ROBOT72, "r72"),
            mk("Martin M1", 44, 320, 256, martin(146.432), "rgb"),
            mk("Martin M2", 40, 320, 256, martin(73.216), "rgb"),
            mk("Scottie S1", 60, 320, 256, scottie(138.24), "rgb", listOf(s("sync", 9.0, F_SYNC))),
            mk("Scottie S2", 56, 320, 256, scottie(88.064), "rgb", listOf(s("sync", 9.0, F_SYNC))),
            mk("Scottie DX", 76, 320, 256, scottie(345.6), "rgb", listOf(s("sync", 9.0, F_SYNC))),
            mk("PD 50", 93, 320, 256, pd(91.52), "pd", rowsPerLine = 2),
            mk("PD 90", 99, 320, 256, pd(170.24), "pd", rowsPerLine = 2),
            mk("PD 120", 95, 640, 496, pd(121.6), "pd", rowsPerLine = 2),
            mk("PD 160", 98, 512, 400, pd(195.584), "pd", rowsPerLine = 2),
            mk("PD 180", 96, 640, 496, pd(183.04), "pd", rowsPerLine = 2),
            mk("PD 240", 97, 640, 496, pd(244.48), "pd", rowsPerLine = 2),
            mk("PD 290", 94, 800, 616, pd(228.8), "pd", rowsPerLine = 2),
            mk("Wraase SC2-180", 55, 320, 256, WRAASE180, "rgb"),
        )) put(m.name, m)
    }

    val VIS2MODE: Map<Int, String> = MODES.values.associate { it.vis to it.name }

    /** Полная длительность передачи (VIS + картинка), с. */
    fun modeDuration(mode: String): Double = MODES.getValue(mode).duration

    // ------------------------------------------------------------ цвет

    /** RGB → Y, Cb(U), Cr(V) (BT.601, «студийный» диапазон) в float32, как _rgb_to_ycc. */
    fun rgbToYcc(r: Float, g: Float, b: Float): FloatArray {
        val y = 16.0f + 0.256788f * r + 0.504129f * g + 0.097906f * b
        val u = 128.0f - 0.148223f * r - 0.290993f * g + 0.439216f * b
        val v = 128.0f + 0.439216f * r - 0.367788f * g - 0.071427f * b
        return floatArrayOf(y, u, v)
    }

    /** Y, U, V → пиксель ARGB (непрозрачный), как _ycc_to_rgb. */
    fun yccToRgb(y0: Float, u0: Float, v0: Float): Int {
        val y = 1.164383f * (y0 - 16.0f)
        val u = u0 - 128.0f
        val v = v0 - 128.0f
        val r = y + 1.596027f * v
        val g = y - 0.391762f * u - 0.812968f * v
        val b = y + 2.017232f * u
        return argb(u8(r), u8(g), u8(b))
    }

    /** clip(x + 0.5, 0, 255).astype(uint8). */
    fun u8(x: Float): Int = (x + 0.5f).coerceIn(0f, 255f).toInt()

    fun argb(r: Int, g: Int, b: Int): Int = (0xFF shl 24) or (r shl 16) or (g shl 8) or b

    /**
     * Приводит картинку ARGB [w0]×[h0] к размеру режима (ближайший сосед), как _fit_image.
     * Результат — три плоскости R, G, B (float, 0..255) по [w]×[h].
     */
    fun fitImage(argb: IntArray, w0: Int, h0: Int, w: Int, h: Int): Array<FloatArray> {
        require(argb.size >= w0 * h0) { "картинка меньше заявленного размера" }
        val out = Array(3) { FloatArray(w * h) }
        for (y in 0 until h) {
            val yi = (y.toLong() * h0 / h).toInt()
            for (x in 0 until w) {
                val xi = (x.toLong() * w0 / w).toInt()
                val p = argb[yi * w0 + xi]
                val o = y * w + x
                out[0][o] = ((p shr 16) and 0xFF).toFloat()
                out[1][o] = ((p shr 8) and 0xFF).toFloat()
                out[2][o] = (p and 0xFF).toFloat()
            }
        }
        return out
    }

    // ------------------------------------------------------------ кодер

    /**
     * Кодирует картинку ARGB [w0]×[h0] в звук SSTV (float, как encode в sstv.py).
     *
     * Передача: лидер 1900 Гц 300 мс, разрыв 1200 Гц 10 мс, лидер 300 мс, VIS (старт-бит, 7 бит младшим
     * вперёд, чётность, стоп-бит по 30 мс), картинка. Картинка другого размера масштабируется.
     * [freqOffset] — сдвиг всех частот, Гц (имитация расстройки SSB). Фаза непрерывна.
     */
    fun encode(argb: IntArray, w0: Int, h0: Int, mode: String, fs: Double = 48000.0,
               freqOffset: Double = 0.0, amplitude: Double = 0.5): FloatArray {
        val m = MODES[mode] ?: throw IllegalArgumentException(mode)
        val w = m.width
        val h = m.height
        val img = fitImage(argb, w0, h0, w, h)
        val nl = m.lines
        // значения каналов (float32, как в numpy): vals[канал][строка передачи][x]
        val vals = HashMap<String, Array<FloatArray>>()
        fun plane(f: (Int) -> Float, rows: Int, row0: (Int) -> Int) =
            Array(rows) { r -> FloatArray(w) { x -> f(row0(r) * w + x) } }
        if (m.color == "rgb") {
            for ((c, k) in listOf("R", "G", "B").withIndex()) vals[k] = plane({ img[c][it] }, h, { it })
        } else {
            val yP = FloatArray(w * h); val uP = FloatArray(w * h); val vP = FloatArray(w * h)
            for (i in 0 until w * h) {
                val ycc = rgbToYcc(img[0][i], img[1][i], img[2][i])
                yP[i] = ycc[0]; uP[i] = ycc[1]; vP[i] = ycc[2]
            }
            when (m.color) {
                "pd" -> {
                    vals["Y0"] = plane({ yP[it] }, h / 2, { 2 * it })
                    vals["Y1"] = plane({ yP[it] }, h / 2, { 2 * it + 1 })
                    vals["U"] = Array(h / 2) { r -> FloatArray(w) { x -> (uP[2 * r * w + x] + uP[(2 * r + 1) * w + x]) / 2 } }
                    vals["V"] = Array(h / 2) { r -> FloatArray(w) { x -> (vP[2 * r * w + x] + vP[(2 * r + 1) * w + x]) / 2 } }
                }
                "r72" -> {
                    vals["Y"] = plane({ yP[it] }, h, { it })
                    vals["U"] = plane({ uP[it] }, h, { it })
                    vals["V"] = plane({ vP[it] }, h, { it })
                }
                else -> {  // r36: чётные строки несут R-Y, нечётные B-Y (среднее по паре)
                    vals["Y"] = plane({ yP[it] }, h, { it })
                    vals["C"] = Array(h) { r ->
                        val k = r - r % 2
                        val src = if (r % 2 == 0) vP else uP
                        FloatArray(w) { x -> (src[k * w + x] + src[(k + 1) * w + x]) / 2 }
                    }
                }
            }
        }

        // заголовок: (частота, длительность)
        val code = m.vis
        val bits = IntArray(7) { (code shr it) and 1 }
        val head = ArrayList<Pair<Double, Double>>()
        head += listOf(F_LEADER to 0.3, F_SYNC to 0.01, F_LEADER to 0.3, F_SYNC to 0.03)
        for (b in bits) head += (if (b == 1) F_BIT1 else F_BIT0) to 0.03
        head += (if (bits.sum() % 2 == 1) F_BIT1 else F_BIT0) to 0.03
        head += F_SYNC to 0.03
        for (sg in m.prefix) head += sg.freq!! to sg.dur
        var t = 0.0
        val tStarts = DoubleArray(head.size)
        for ((i, p) in head.withIndex()) { tStarts[i] = t; t += p.second }
        val tImg = t
        val tEnd = tImg + nl * m.line

        val total = Math.rint(tEnd * fs).toLong()
        require(total < Int.MAX_VALUE) { "передача слишком длинная" }
        val out = FloatArray(total.toInt())
        val gen = PhaseGen(out, fs, amplitude)
        val span = (F_SPAN / 255.0).toFloat()
        for (i in head.indices) gen.at(tStarts[i], head[i].first + freqOffset)
        for (i in 0 until nl) {
            val base = tImg + i * m.line
            var tr = 0.0
            for (sg in m.layout) {
                if (sg.kind in CHANNELS) {
                    val row = vals.getValue(sg.kind)[i]
                    val dx = sg.dur / w
                    for (x in 0 until w) {
                        val f = (F_BLACK.toFloat() + row[x] * span).toDouble()
                        gen.at(base + (tr + x * dx), f + freqOffset)
                    }
                } else {
                    val f = if (sg.kind == "sepC") (if (i % 2 == 0) F_BLACK else F_WHITE) else sg.freq!!
                    gen.at(base + tr, f + freqOffset)
                }
                tr += sg.dur
            }
        }
        gen.at(tEnd, 0.0)
        return out
    }

    /**
     * Генератор тона с непрерывной фазой по кускам (время начала, частота): как np.repeat + cumsum
     * в encode — фаза копится блоками по 2^20 отсчётов с приведением по модулю 2π между блоками.
     */
    private class PhaseGen(val out: FloatArray, val fs: Double, val amp: Double) {
        private val k = 2.0 * PI / fs
        private var ph0 = 0.0
        private var cs = 0.0
        private var pos = 0
        private var prevBound = -1L
        private var prevF = 0.0

        fun at(tStart: Double, f: Double) {
            val b = Math.rint(tStart * fs).toLong()
            if (prevBound >= 0) {
                val e = min(b, out.size.toLong()).toInt()
                while (pos < e) {
                    cs += prevF
                    val ph = ph0 + k * cs
                    out[pos] = (amp * sin(ph)).toFloat()
                    pos++
                    if (pos and STEP_MASK == 0) { ph0 = ph % (2 * PI); cs = 0.0 }
                }
            }
            prevBound = max(b, prevBound)
            prevF = f
        }

        companion object { const val STEP_MASK = (1 shl 20) - 1 }
    }
}
