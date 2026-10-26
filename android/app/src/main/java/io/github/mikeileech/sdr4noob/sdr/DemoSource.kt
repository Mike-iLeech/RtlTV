package io.github.mikeileech.sdr4noob.sdr

import io.github.mikeileech.sdr4noob.dsp.Afsk
import io.github.mikeileech.sdr4noob.dsp.Aprs
import io.github.mikeileech.sdr4noob.dsp.Ax25
import io.github.mikeileech.sdr4noob.dsp.DmrGen
import io.github.mikeileech.sdr4noob.dsp.Sstv
import io.github.mikeileech.sdr4noob.dsp.RdsEncoder
import kotlin.math.PI
import kotlin.math.sin

/**
 * Тестовый эфир без приёмника (как --test в настольной версии, rtltv/radiotest.py): ЧМ-стерео с RDS,
 * узкая ЧМ, АМ, SSB (USB), телеграф, DMR (групповой и индивидуальный вызовы), APRS (144,800 МГц) и SSTV
 * (145,800 МГц, Robot 36), плюс шум. Отдаёт отсчёты в реальном темпе, поэтому звук и водопад — как вживую.
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
            Station(14_230_000, 'U', intArrayOf(700, 1200, 1900), intArrayOf(), 0.15f),     // USB: тоны над несущей
            Station(7_030_000, 'C', intArrayOf(), intArrayOf(), 0.10f),                     // телеграф
            Station(446_100_000, 'D', intArrayOf(), intArrayOf(), 0.15f),                   // DMR, BS, CC 1
            Station(144_800_000, 'P', intArrayOf(), intArrayOf(), 0.20f),                   // APRS, AFSK 1200
            Station(145_800_000, 'V', intArrayOf(), intArrayOf(), 0.20f),                   // SSTV, Robot 36
        )

        /** APRS: три станции у Москвы (позиция, объект, погода), затем тишина — ~12 с по кругу, 48 кГц. */
        private val APRS_AUDIO: FloatArray by lazy {
            val frames = listOf(
                Ax25.makeFrame("RA3AAA-9", "APRS", listOf("WIDE1-1"), Aprs.encodePosition(55.7512, 37.6184, "/", ">", "Тестовый эфир SDR4NOOB")),
                Ax25.makeFrame("UA3BBB-10", "APRS", listOf("WIDE2-1"), Aprs.encodePosition(55.8304, 37.3297, "/", "-", "дом")),
                Ax25.makeFrame("R3CCC", "APRS", emptyList(), "!5541.20N/03745.00E_090/005g010t050r000p000P000h80b10132"),
            )
            val x = Afsk.encodePackets(frames, gap = 1.0)
            x.copyOf(maxOf(x.size, 12 * 48_000))
        }

        /** SSTV: цветные полосы с сеткой, Robot 36 (~36 с), затем 10 с тишины. */
        private val SSTV_AUDIO: FloatArray by lazy {
            val w = 320; val h = 240
            val bars = intArrayOf(0xFFFFFFFF.toInt(), 0xFFFFFF00.toInt(), 0xFF00FFFF.toInt(), 0xFF00FF00.toInt(),
                0xFFFF00FF.toInt(), 0xFFFF0000.toInt(), 0xFF0000FF.toInt(), 0xFF000000.toInt())
            val img = IntArray(w * h) { i ->
                val x = i % w; val y = i / w
                if (x % 40 == 0 || y % 40 == 0) 0xFF808080.toInt() else bars[x * bars.size / w]
            }
            val x = Sstv.encode(img, w, h, "Robot 36")
            x.copyOf(x.size + 10 * 48_000)
        }


        /** Сигнал DMR — мгновенная частота, Гц, 48 кГц, ~13 с по кругу (генератор как в rtltv/dmr.py). */
        private val DMR_FREQ: FloatArray by lazy { DmrGen.testSignal() }

        /** RDS станции 101,0 МГц (PS и радиотекст — как в тестовом эфире настольной версии). */
        val RDS = RdsEncoder(RdsEncoder.groups(0x7A01, "SDR4NOOB", "Тестовый эфир SDR4NOOB: музыка в стерео", pty = 10))

        // телеграф: «CQ CQ DE SDR4NOOB» азбукой Морзе, 18 слов/мин (точка 1,2/18 с)
        private val MORSE = mapOf('A' to ".-", 'B' to "-...", 'C' to "-.-.", 'D' to "-..", 'E' to ".", 'N' to "-.",
            'O' to "---", 'Q' to "--.-", 'R' to ".-.", 'S' to "...", '4' to "....-")
        private val KEY: BooleanArray = run {
            val out = ArrayList<Boolean>()
            for (ch in "CQ CQ DE SDR4NOOB ") {
                if (ch == ' ') { repeat(4) { out.add(false) }; continue }
                for (sym in MORSE.getValue(ch)) {
                    repeat(if (sym == '-') 3 else 1) { out.add(true) }
                    out.add(false)
                }
                repeat(2) { out.add(false) }
            }
            out.toBooleanArray()
        }
        private const val DOT_S = 1.2 / 18
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
        // отдаём полными блоками, как приёмник: иначе поток обработки крутился бы без сна на крошечных порциях
        if (lag < maxSamples) {
            Thread.sleep(maxOf(1L, (maxSamples - lag) * 1000L / fs))
            return 0
        }
        val n = maxSamples
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
        // станции без нот (телеграф, DMR) — до выбора ноты: у них пустой список
        if (st.kind == 'U' || st.kind == 'C') { renderSsb(k, st, off, re, im, n, t); return }
        if (st.kind == 'D') { renderDmr(k, st, off, re, im, n, t); return }
        if (st.kind == 'P' || st.kind == 'V') { renderAudioFm(k, st, off, re, im, n, t); return }
        val step = (t / 0.4).toInt()
        val fl = st.notesL[step % st.notesL.size].toDouble()
        val fr = if (st.notesR.isEmpty()) fl else st.notesR[step % st.notesR.size].toDouble()
        val dl = inc(fl); val dr = inc(fr); val dp = inc(19_000.0)
        val rds = st.freq == 101_000_000L
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
                    var mpx = 0.45f * (l + r) * 0.5f + 0.1f * pil + 0.45f * (l - r) * 0.5f * s38
                    // RDS: двухфазные символы 1187,5 бит/с на поднесущей 57 кГц (3-я гармоника пилота), 3 %
                    if (rds) mpx += 0.03f * RDS.value(t + i.toDouble() / fs) * s(pp * 3)
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

    private fun renderDmr(k: Int, st: Station, off: Double, re: FloatArray, im: FloatArray, n: Int, t: Double) {
        val sig = DMR_FREQ
        val a = st.level
        var ph = carPh[k]
        val k48 = 48_000.0
        for (i in 0 until n) {
            val idx = (((t + i.toDouble() / fs) * k48).toLong() % sig.size).toInt()
            ph += inc(off + sig[idx])
            re[i] += a * c(ph); im[i] += a * s(ph)
        }
        carPh[k] = ph
    }

    /** Узкая ЧМ звуком из буфера 48 кГц по кругу (APRS, SSTV): девиация 5 кГц на единицу амплитуды. */
    private fun renderAudioFm(k: Int, st: Station, off: Double, re: FloatArray, im: FloatArray, n: Int, t: Double) {
        val sig = if (st.kind == 'P') APRS_AUDIO else SSTV_AUDIO
        val a = st.level
        var ph = carPh[k]
        val dc = inc(off)
        val dev = 5_000.0 / fs * 4294967296.0
        for (i in 0 until n) {
            val idx = (((t + i.toDouble() / fs) * 48_000.0).toLong() % sig.size).toInt()
            ph += dc + (sig[idx] * dev).toInt()
            re[i] += a * c(ph); im[i] += a * s(ph)
        }
        carPh[k] = ph
    }

    /** USB — сумма тонов над несущей (аналитический сигнал), телеграф — несущая по манипуляции. */
    private fun renderSsb(k: Int, st: Station, off: Double, re: FloatArray, im: FloatArray, n: Int, t: Double) {
        val a = st.level
        if (st.kind == 'U') {
            for ((j, f) in st.notesL.withIndex()) {
                val d = inc(off + f)
                var ph = tonePh[4 * k + j]
                for (i in 0 until n) { re[i] += a * c(ph); im[i] += a * s(ph); ph += d }
                tonePh[4 * k + j] = ph
            }
            return
        }
        val d = inc(off)
        var ph = carPh[k]
        for (i in 0 until n) {
            val el = ((t + i.toDouble() / fs) / DOT_S).toLong()
            if (KEY[(el % KEY.size).toInt()]) { re[i] += a * c(ph); im[i] += a * s(ph) }
            ph += d
        }
        carPh[k] = ph
    }

    override fun setFrequency(hz: Long) { center = hz }
    override fun setSampleRate(rate: Int) { fs = rate; t0 = System.nanoTime(); produced = 0 }
    override fun setGain(tenthsDb: Int) {}
    override fun close() {}
}
