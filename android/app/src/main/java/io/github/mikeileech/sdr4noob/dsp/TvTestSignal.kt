package io.github.mikeileech.sdr4noob.dsp

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.hypot
import kotlin.math.ln
import kotlin.math.pow
import kotlin.math.sin
import kotlin.math.sqrt

/** xorshift64* → равномерное [0,1) → нормальное (Бокс — Мюллер, косинусная ветвь) — как Noise в генераторах эталонов. */
class XorShiftNoise(seed: Long) {
    private var x = seed * -7046029254386353131L + 1   // 0x9E3779B97F4A7C15
    fun next(): Long {
        x = x xor (x ushr 12); x = x xor (x shl 25); x = x xor (x ushr 27)
        return x * 0x2545F4914F6CDD1DL
    }
    fun uni() = (next() ushr 11) * (1.0 / 9007199254740992.0)
    fun gauss(): Double {
        val u1 = 1.0 - uni()
        val u2 = uni()
        return sqrt(-2.0 * ln(u1)) * cos(2.0 * PI * u2)
    }
    /** rng.choice([-1, 1]) — по старшему биту. */
    fun sign(): Int = if ((next() ushr 63) == 0L) -1 else 1
}

/** БПФ в double произвольного размера (Bluestein через степень двойки) — для генератора OFDM. */
class FftAny(val n: Int) {
    private val m = run { var v = 2; while (v < 2 * n - 1) v *= 2; v }
    private val cr = DoubleArray(n); private val ci = DoubleArray(n)
    private val br = DoubleArray(m); private val bi = DoubleArray(m)
    private val cosT = DoubleArray(m / 2); private val sinT = DoubleArray(m / 2)
    private val rev = IntArray(m)

    init {
        for (i in 0 until m / 2) { cosT[i] = cos(2 * PI * i / m); sinT[i] = -sin(2 * PI * i / m) }
        val bits = Integer.numberOfTrailingZeros(m)
        for (i in 0 until m) rev[i] = if (bits == 0) 0 else Integer.reverse(i) ushr (32 - bits)
        for (k in 0 until n) {
            val a = PI * ((k.toLong() * k) % (2L * n)) / n
            cr[k] = cos(a); ci[k] = sin(a)      // chirp exp(+iπk²/N)
        }
        for (k in 0 until n) {
            br[k] = cr[k]; bi[k] = -ci[k]
            if (k > 0) { br[m - k] = cr[k]; bi[m - k] = -ci[k] }
        }
        fft(br, bi, false)
    }

    private fun fft(re: DoubleArray, im: DoubleArray, inverse: Boolean) {
        val s = if (inverse) -1.0 else 1.0
        for (i in 0 until m) {
            val j = rev[i]
            if (j > i) { var t = re[i]; re[i] = re[j]; re[j] = t; t = im[i]; im[i] = im[j]; im[j] = t }
        }
        var size = 2
        while (size <= m) {
            val half = size / 2; val step = m / size
            var start = 0
            while (start < m) {
                var k = 0
                for (j in start until start + half) {
                    val wr = cosT[k]; val wi = s * sinT[k]
                    val l = j + half
                    val tr = re[l] * wr - im[l] * wi; val ti = re[l] * wi + im[l] * wr
                    re[l] = re[j] - tr; im[l] = im[j] - ti; re[j] += tr; im[j] += ti
                    k += step
                }
                start += size
            }
            size *= 2
        }
    }

    /** Обратное ДПФ с множителем 1/N (как numpy.fft.ifft), на месте. */
    fun inverse(re: DoubleArray, im: DoubleArray) {
        val ar = DoubleArray(m); val ai = DoubleArray(m)
        for (k in 0 until n) {
            ar[k] = re[k] * cr[k] - im[k] * ci[k]
            ai[k] = re[k] * ci[k] + im[k] * cr[k]
        }
        fft(ar, ai, false)
        for (i in 0 until m) {
            val r = ar[i] * br[i] - ai[i] * bi[i]; val q = ar[i] * bi[i] + ai[i] * br[i]
            ar[i] = r; ai[i] = q
        }
        fft(ar, ai, true)
        for (k in 0 until n) {
            val r = ar[k] / m; val q = ai[k] / m
            re[k] = (r * cr[k] - q * ci[k]) / n
            im[k] = (r * ci[k] + q * cr[k]) / n
        }
    }
}

/**
 * Тестовый эфир для режима ТВ — перенос rtltv/testsignal.py: аналоговая станция (испытательная таблица + FM-звук)
 * на 583,25 МГц и мультиплекс DVB-T2 32K, GI 1/16 на 546 МГц. Отдаёт сырые байты, как настоящий донгл (u8 IQ).
 * Отличие от настольной: случайные числа — [XorShiftNoise] (а не numpy default_rng), чтобы эталоны сверялись до отсчёта.
 */
class TvTestSignal(fs: Double, stdKey: String = "DK", seed: Long = 1) {
    companion object {
        const val TEST_ANALOG_VC = 583.25e6   // ДМВ 35, D/K
        const val TEST_MUX_CENTER = 546e6     // ДМВ 30
        const val TEST_MUX_TU = 3584e-6       // DVB-T2 32K, 8 МГц
        const val TEST_MUX_GI = 1.0 / 16
        val MELODY = doubleArrayOf(523.25, 659.25, 783.99, 1046.5, 783.99, 659.25)
        private val LUT = FloatArray(256) { (it - 127.4f) / 128f }

        /** u8 IQ → re/im (как rtlsdr.u8_to_iq / SdrSource). */
        fun u8ToIq(raw: ByteArray): Pair<FloatArray, FloatArray> {
            val n = raw.size / 2
            return FloatArray(n) { LUT[raw[2 * it].toInt() and 0xFF] } to FloatArray(n) { LUT[raw[2 * it + 1].toInt() and 0xFF] }
        }

        /** Испытательная таблица (яркость 0..1), rows×cols построчно — make_card. */
        fun makeCard(rows: Int, cols: Int): FloatArray {
            val img = FloatArray(rows * cols) { 0.5f }
            val gx = cols / 16.0; val gy = rows / 12.0
            val gxf = gx.toFloat(); val gyf = gy.toFloat()
            val gxh = (gx / 2).toFloat(); val gyh = (gy / 2).toFloat()
            val gxt = (gx / 2 - 1.2).toFloat(); val gyt = (gy / 2 - 1.2).toFloat()
            fun fmod(a: Float, b: Float): Float { val r = a % b; return if (r != 0f && (r < 0) != (b < 0)) r + b else r }
            for (yi in 0 until rows) for (xi in 0 until cols) {
                val x = xi.toFloat(); val y = yi.toFloat()
                if (abs(fmod(x, gxf) - gxh) > gxt || abs(fmod(y, gyf) - gyh) > gyt) img[yi * cols + xi] = 1f
            }
            val top = (rows * 0.30).toInt(); val bot = (rows * 0.55).toInt()
            for (yi in top until bot) for (xi in 0 until cols) {
                val bar = floor(xi.toFloat() / cols * 8f) / 7f
                img[yi * cols + xi] = 1f - bar
            }
            val g0 = (rows * 0.60).toInt(); val g1 = (rows * 0.70).toInt()
            for (yi in g0 until g1) for (xi in 0 until cols) img[yi * cols + xi] = xi.toFloat() / (cols - 1)
            val b0 = (rows * 0.72).toInt(); val b1 = (rows * 0.80).toInt()
            for ((i, period) in intArrayOf(16, 8, 5, 3).withIndex()) {
                val c0 = (cols * (0.15 + 0.18 * i)).toInt(); val c1 = (cols * (0.15 + 0.18 * i + 0.15)).toInt()
                for (yi in b0 until b1) for (xi in c0 until c1) {
                    img[yi * cols + xi] = fmod(floor(xi.toFloat() / period), 2f)
                }
            }
            val cy = rows / 2.0; val cx = cols / 2.0
            val cyf = cy.toFloat(); val cxf = cx.toFloat()
            val asp = (rows.toDouble() / cols).toFloat()
            val rad = (rows * 0.44).toFloat()
            for (yi in 0 until rows) for (xi in 0 until cols) {
                val x = xi.toFloat(); val y = yi.toFloat()
                val rr = hypot(((x - cxf) * asp * 4f / 3f).toDouble(), (y - cyf).toDouble()).toFloat()
                if (abs(rr - rad) < 1.6f) img[yi * cols + xi] = 1f
                if (abs(x - cxf) < 1.5f || abs(y - cyf) < 1.0f) img[yi * cols + xi] = 0f
            }
            return img
        }
    }

    val fs = fs
    val std: TvStandard = Tv.STANDARDS.getValue(stdKey)
    private var n = 0L
    var clockErr = 0.4e-6
    private val grid = 1024
    private val cardRows = 2 * std.visCount
    private val cardCols = 640
    private val card = makeCard(cardRows, cardCols)
    private val frames = LinkedHashMap<Long, FloatArray>()
    private var fmPhase = 0.0
    private var ofdmR = DoubleArray(0); private var ofdmI = DoubleArray(0)
    private var ofdmFc: Double? = null
    val rng = XorShiftNoise(seed)
    private var ofdmFft: FftAny? = null

    /** Композитный видеосигнал кадра (амплитуда огибающей, негативная модуляция), L×grid построчно. */
    private fun composite(frameNo: Long): FloatArray {
        frames[frameNo]?.let { return it }
        val g = grid; val l = std.lines
        val comp = FloatArray(l * g) { 0.75f }
        val a0 = Math.rint(std.activeStartUs / 64.0 * g).toInt()
        val a1 = Math.rint((std.activeStartUs + std.activeUs) / 64.0 * g).toInt()
        val cd = card.copyOf()
        val rows = cardRows; val cols = cardCols
        // движущийся квадрат: видно, что картинка живая
        val t = frameNo / 25.0
        val bx = ((0.5 + 0.38 * sin(t * 1.3)) * (cols - 60)).toInt()
        val by = ((0.5 + 0.38 * cos(t * 0.9)) * (rows - 60)).toInt()
        for (y in by until minOf(by + 60, rows)) for (x in bx until minOf(bx + 60, cols)) cd[y * cols + x] = 1f - cd[y * cols + x]
        val num = a1 - a0
        val step = (cols - 1).toDouble() / (num - 1)
        val cidx = IntArray(num) { if (it == num - 1) cols - 1 else (it * step).toInt() }
        for ((f, start) in listOf(0 to std.vis1, 1 to std.vis2)) {
            for (i in 0 until std.visCount) {
                val srcRow = f + 2 * i
                val ln = start + i
                for (c in 0 until num) comp[ln * g + a0 + c] = 0.75f - 0.625f * cd[srcRow * cols + cidx[c]]
            }
        }
        val hs = Math.rint(4.7 / 64.0 * g).toInt()
        val eq = Math.rint(2.35 / 64.0 * g).toInt()
        val h2 = g / 2
        val bw = h2 - hs
        val nb = std.nBroad; val ne = std.nEq
        val hh = 2 * l
        val kind = ByteArray(hh)   // 0 норм., 1 широкий, 2 уравнивающий
        for (base in intArrayOf(0, l)) {
            for (i in base until base + nb) kind[i] = 1
            for (i in base + nb until base + nb + ne) kind[i] = 2
            val s = (base + l - ne) % hh
            for (i in s until minOf(s + ne, hh)) kind[i] = 2
        }
        for (h in 0 until hh) {
            val ln = h / 2; val off = (h % 2) * h2
            if (kind[h].toInt() == 0) {
                if (h % 2 == 0) for (c in 0 until hs) comp[ln * g + c] = 1f
                continue
            }
            for (c in off until off + h2) comp[ln * g + c] = 0.75f
            val e = if (kind[h].toInt() == 1) bw else eq
            for (c in off until off + e) comp[ln * g + c] = 1f
        }
        // ограничение полосы (~2 МГц), чтобы не было наложения спектра: np.convolve(..., ones(8)/8, "same")
        val nn = comp.size
        val flat = FloatArray(nn)
        for (i in 0 until nn) {
            var s = 0f
            for (j in 0 until 8) { val k = i + 3 - j; if (k in 0 until nn) s += comp[k] * 0.125f }
            flat[i] = s
        }
        if (frames.size > 4) frames.clear()
        frames[frameNo] = flat
        return flat
    }

    private fun ofdm(fc: Double, count: Int, outR: DoubleArray, outI: DoubleArray, gain: Double) {
        if (ofdmFc != fc) { ofdmR = DoubleArray(0); ofdmI = DoubleArray(0); ofdmFc = fc }
        val lu = Math.rint(TEST_MUX_TU * fs).toInt()
        val lg = Math.rint(lu * TEST_MUX_GI).toInt()
        val fft = ofdmFft?.takeIf { it.n == lu } ?: FftAny(lu).also { ofdmFft = it }
        val v = 1.0 / (lu * (1.0 / fs))
        val occ = BooleanArray(lu) { k ->
            val kk = if (k <= (lu - 1) / 2) k else k - lu
            abs(kk * v + (fc - TEST_MUX_CENTER)) < 3.8e6
        }
        val m = occ.count { it }
        val bufR = ArrayList<DoubleArray>(); val bufI = ArrayList<DoubleArray>()
        bufR += ofdmR; bufI += ofdmI
        var have = ofdmR.size
        val sq = sqrt(lu.toDouble())
        val s2 = sqrt(2.0)
        while (have < count) {
            val xr = DoubleArray(lu); val xi = DoubleArray(lu)
            val reS = IntArray(m) { rng.sign() }
            val imS = IntArray(m) { rng.sign() }
            var j = 0
            for (k in 0 until lu) if (occ[k]) {
                xr[k] = (reS[j] / s2).toFloat().toDouble(); xi[k] = (imS[j] / s2).toFloat().toDouble(); j++
            }
            fft.inverse(xr, xi)
            val sr = DoubleArray(lg + lu); val si = DoubleArray(lg + lu)
            for (i in 0 until lu) {
                val r = (xr[i] * sq).toFloat().toDouble(); val q = (xi[i] * sq).toFloat().toDouble()
                sr[lg + i] = r; si[lg + i] = q
                if (i >= lu - lg) { sr[i - (lu - lg)] = r; si[i - (lu - lg)] = q }
            }
            bufR += sr; bufI += si
            have += sr.size
        }
        val allR = DoubleArray(have); val allI = DoubleArray(have)
        var o = 0
        for (b in bufR.indices) {
            System.arraycopy(bufR[b], 0, allR, o, bufR[b].size)
            System.arraycopy(bufI[b], 0, allI, o, bufI[b].size)
            o += bufR[b].size
        }
        ofdmR = allR.copyOfRange(count, have); ofdmI = allI.copyOfRange(count, have)
        for (i in 0 until count) { outR[i] += allR[i] * gain; outI[i] += allI[i] * gain }
    }

    /** [nsamples] отсчётов эфира на частоте [fc] при усилении [gainDb] (30 дБ — номинал) → u8 IQ. */
    fun render(fc: Double, nsamples: Int, gainDb: Double): ByteArray {
        val t = DoubleArray(nsamples) { (n + it).toDouble() / fs * (1 + clockErr) }
        n += nsamples
        val g = 10.0.pow((gainDb - 30.0) / 20.0)
        val xr = DoubleArray(nsamples); val xi = DoubleArray(nsamples)
        val ns = 0.012 * g
        for (i in 0 until nsamples) xr[i] = rng.gauss()
        for (i in 0 until nsamples) xi[i] = rng.gauss()
        for (i in 0 until nsamples) { xr[i] *= ns; xi[i] *= ns }
        val half = fs / 2
        val off = TEST_ANALOG_VC - fc
        if (abs(off) < half + 0.8e6) {
            val l = std.lines; val gr = grid
            val w = 2 * PI * off
            val ag = 0.25 * g
            var curNo = Long.MIN_VALUE; var comp = FloatArray(0)
            for (i in 0 until nsamples) {
                val lt = t[i] * std.lineFreq
                val fno = floor(lt / l).toLong()
                if (fno != curNo) { comp = composite(fno); curNo = fno }
                val idx = ((lt - fno * l) * gr).toLong().coerceIn(0L, (comp.size - 1).toLong()).toInt()
                val env = comp[idx].toDouble()
                val a = w * t[i]
                xr[i] += env * cos(a) * ag; xi[i] += env * sin(a) * ag
            }
        }
        val soff = TEST_ANALOG_VC + std.soundOffset - fc
        if (abs(soff) < half) {
            val note = MELODY[((t[0] / 0.35).toInt()) % MELODY.size]
            val fg = 0.05 * g
            var cum = 0.0
            var ph = fmPhase
            for (i in 0 until nsamples) {
                val inst = soff + 30e3 * sin(2 * PI * note * t[i])
                cum += 2 * PI * inst / fs
                ph = fmPhase + cum
                xr[i] += cos(ph) * fg; xi[i] += sin(ph) * fg
            }
            fmPhase = PyNum.mod(ph, 2 * PI)
        }
        if (abs(TEST_MUX_CENTER - fc) < 3.8e6 + half) ofdm(fc, nsamples, xr, xi, 0.05 * g)
        val out = ByteArray(2 * nsamples)
        for (i in 0 until nsamples) {
            out[2 * i] = Math.rint((xr[i].toFloat() * 128f + 127.4f).toDouble()).coerceIn(0.0, 255.0).toInt().toByte()
            out[2 * i + 1] = Math.rint((xi[i].toFloat() * 128f + 127.4f).toDouble()).coerceIn(0.0, 255.0).toInt().toByte()
        }
        return out
    }
}
