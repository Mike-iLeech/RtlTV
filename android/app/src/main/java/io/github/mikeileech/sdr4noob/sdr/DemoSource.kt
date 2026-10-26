package io.github.mikeileech.sdr4noob.sdr

import kotlin.math.PI
import kotlin.math.sin

/**
 * Тестовый эфир без приёмника (как --test в настольной версии): несколько станций с ЧМ-стерео,
 * узкой ЧМ и АМ, плюс шум. Отдаёт отсчёты в реальном темпе, поэтому звук и водопад — как вживую.
 */
class DemoSource : SdrSource {
    private class Station(val freq: Long, val kind: Char, val notesL: IntArray, val notesR: IntArray, val level: Float)

    companion object {
        private const val LUT_BITS = 12
        private const val LUT_SIZE = 1 shl LUT_BITS
        private val SIN = FloatArray(LUT_SIZE) { sin(2 * PI * it / LUT_SIZE).toFloat() }
        private fun s(ph: Int) = SIN[ph ushr (32 - LUT_BITS)]
        private fun c(ph: Int) = SIN[(ph + (1 shl 30)) ushr (32 - LUT_BITS)]

        // ноты в Гц: до мажор и ля минор
        private val C = intArrayOf(523, 659, 784, 1047, 784, 659)
        private val A = intArrayOf(220, 262, 330, 262)
        private val STATIONS = listOf(
            Station(101_000_000, 'S', C, A, 0.30f),       // стерео: мелодия слева, бас справа
            Station(102_600_000, 'S', A, A, 0.08f),       // слабая моно-станция (одинаково в каналах)
            Station(104_500_000, 'S', intArrayOf(440, 494), intArrayOf(880), 0.20f),
            Station(145_500_000, 'N', intArrayOf(1000, 0, 1000, 0), intArrayOf(), 0.25f),   // узкая ЧМ: тон с паузами
            Station(145_525_000, 'N', intArrayOf(600, 800), intArrayOf(), 0.10f),
            Station(999_000, 'A', intArrayOf(400, 500, 600), intArrayOf(), 0.30f),           // АМ на СВ
        )
    }

    override val label = "Тестовый сигнал (без приёмника)"
    override val gains = IntArray(0)

    private var fs = 2_400_000
    private var center = 101_000_000L
    private var t0 = 0L
    private var produced = 0L
    private var rnd = 0x2545F491
    private val carPh = IntArray(STATIONS.size)
    private val tonePh = IntArray(STATIONS.size * 4)

    override fun open(sampleRate: Int, frequencyHz: Long) {
        fs = sampleRate; center = frequencyHz
    }

    override fun start() { t0 = System.nanoTime(); produced = 0 }

    override fun read(re: FloatArray, im: FloatArray, maxSamples: Int): Int {
        // темп реального времени
        val due = (System.nanoTime() - t0) / 1000 * fs / 1_000_000L
        // отстали больше чем на секунду (поток стоял) — не догоняем, а продолжаем с текущего момента
        if (due - produced > fs) produced = due - maxSamples
        val lag = due - produced
        if (lag <= 0) {
            val waitMs = (-lag + maxSamples / 4) * 1000L / fs
            Thread.sleep(maxOf(1L, waitMs))
            return 0
        }
        val n = minOf(lag, maxSamples.toLong()).toInt()
        noise(re, im, n)
        for ((k, st) in STATIONS.withIndex()) {
            val off = st.freq - center
            if (kotlin.math.abs(off) > fs / 2 - 60_000) continue
            render(k, st, off.toDouble(), re, im, n)
        }
        produced += n
        return n
    }

    private fun noise(re: FloatArray, im: FloatArray, n: Int) {
        var x = rnd
        for (i in 0 until n) {
            x = x xor (x shl 13); x = x xor (x ushr 17); x = x xor (x shl 5)
            val a = (x and 0xFFFF) - 32768
            val b = ((x ushr 16) and 0xFFFF) - 32768
            re[i] = a * 2.5e-7f; im[i] = b * 2.5e-7f
        }
        rnd = x
    }

    private fun inc(hz: Double) = (hz / fs * 4294967296.0).toLong().toInt()

    private fun render(k: Int, st: Station, off: Double, re: FloatArray, im: FloatArray, n: Int) {
        val t = produced.toDouble() / fs
        val step = (t / 0.4).toInt()
        val fl = st.notesL[step % st.notesL.size].toDouble()
        val fr = if (st.notesR.isEmpty()) fl else st.notesR[step % st.notesR.size].toDouble()
        val dl = inc(fl); val dr = inc(fr); val dp = inc(19_000.0)
        var pl = tonePh[4 * k]; var pr = tonePh[4 * k + 1]; var pp = tonePh[4 * k + 2]
        var cp = carPh[k]
        val dc = inc(off)
        val a = st.level
        when (st.kind) {
            'S' -> {
                val dev = 75_000.0 / fs * 4294967296.0
                for (i in 0 until n) {
                    val l = if (fl > 0) s(pl) else 0f
                    val r = if (fr > 0) s(pr) else 0f
                    val pil = s(pp)
                    val s38 = s(pp * 2)
                    val mpx = 0.45f * (l + r) * 0.5f + 0.1f * pil + 0.45f * (l - r) * 0.5f * s38
                    cp += dc + (mpx * dev).toInt()
                    re[i] += a * c(cp); im[i] += a * s(cp)
                    pl += dl; pr += dr; pp += dp
                }
            }
            'N' -> {
                val dev = 2_500.0 / fs * 4294967296.0
                for (i in 0 until n) {
                    val m = if (fl > 0) s(pl) else 0f
                    cp += dc + (m * dev).toInt()
                    re[i] += a * c(cp); im[i] += a * s(cp)
                    pl += dl
                }
            }
            else -> {
                for (i in 0 until n) {
                    val env = a * (1f + 0.6f * s(pl))
                    cp += dc
                    re[i] += env * c(cp); im[i] += env * s(cp)
                    pl += dl
                }
            }
        }
        tonePh[4 * k] = pl; tonePh[4 * k + 1] = pr; tonePh[4 * k + 2] = pp
        carPh[k] = cp
    }

    override fun setFrequency(hz: Long) { center = hz }
    override fun setSampleRate(rate: Int) { fs = rate; t0 = System.nanoTime(); produced = 0 }
    override fun setGain(tenthsDb: Int) {}
    override fun close() {}
}
