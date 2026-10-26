package io.github.mikeileech.sdr4noob.lrpt

import io.github.mikeileech.sdr4noob.dsp.PyNum
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.ln
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin
import kotlin.math.sqrt

/*
 * Декодер «Метеор-М» LRPT — перенос rtltv/lrpt.py (настольная программа SDR4NOOB).
 *
 * Цепочка: перенос частоты (доплер) → RRC + децимация → [C/Kotlin] АРУ, Костас, Гарднер → мягкие символы →
 * [C/Kotlin] Витерби (8 гипотез фазы/сдвига, автоопределение NRZ-M) → ASM → дерандомизация → [C/Kotlin] RS →
 * VCDU (VCID 5) → M_PDU → CCSDS-пакеты → APID 64–69 → декомпрессия МКУ → каналы и RGB-композиты.
 *
 * Комплексные отсчёты — FloatArray (re, im, re, im…), мягкие биты — ByteArray (int8, >0 = 1), биты — ByteArray 0/1.
 */
object Lrpt {
    const val SYMBOL_RATE = 72000
    const val ASM = 0x1ACFFC1D
    const val FRAME_BYTES = 1024
    const val FRAME_BITS = FRAME_BYTES * 8
    const val WIDTH = 1568            // 196 МКУ × 8 пикселей
    val APIDS = intArrayOf(64, 65, 66, 67, 68, 69)

    data class Sat(val norad: Int, val freq: Double, val alt: Double, val oqpsk: Boolean, val rs: Int)

    val SATS = mapOf(
        "METEOR-M2 3" to Sat(57166, 137.9e6, 137.1e6, true, 72000),
        "METEOR-M2 4" to Sat(59051, 137.9e6, 137.1e6, true, 72000),
    )

    /** Корень из приподнятого косинуса, sps — отсчётов на символ (может быть дробным). */
    fun rrcTaps(sps: Double, alpha: Double = 0.5, span: Int = 12): FloatArray {
        val n = (span * sps).toInt() or 1
        val h = DoubleArray(n)
        for (i in 0 until n) {
            val ti = (i - n / 2) / sps
            h[i] = when {
                abs(ti) < 1e-9 -> 1 - alpha + 4 * alpha / PI
                abs(abs(4 * alpha * ti) - 1) < 1e-9 -> alpha / sqrt(2.0) * ((1 + 2 / PI) * sin(PI / (4 * alpha)) +
                    (1 - 2 / PI) * cos(PI / (4 * alpha)))
                else -> (sin(PI * ti * (1 - alpha)) + 4 * alpha * ti * cos(PI * ti * (1 + alpha))) /
                    (PI * ti * (1 - (4 * alpha * ti) * (4 * alpha * ti)))
            }
        }
        var e = 0.0
        for (v in h) e += v * v
        val k = sqrt(e)
        return FloatArray(n) { (h[it] / k).toFloat() }
    }

    /** Псевдослучайная последовательность CCSDS (FF 48 0E C0 9A …), n байт. */
    fun pnSequence(n: Int = FRAME_BYTES - 4): ByteArray {
        val nb = 8 * n
        val a = ByteArray(nb + 8)
        for (k in 0 until 8) a[k] = 1
        for (k in 0 until nb) a[k + 8] = (a[k + 7].toInt() xor a[k + 5].toInt() xor a[k + 3].toInt() xor a[k].toInt()).toByte()
        return packBits(a, 0, nb)
    }

    val PN: ByteArray by lazy { pnSequence() }

    val ASM_BITS = ByteArray(32) { ((ASM ushr (31 - it)) and 1).toByte() }
    private val ASM_PM = IntArray(32) { ASM_BITS[it] * 2 - 1 }

    /** Биты (0/1, старший первым) → байты. */
    fun packBits(bits: ByteArray, from: Int, n: Int): ByteArray {
        val out = ByteArray((n + 7) / 8)
        for (i in 0 until n) if (bits[from + i].toInt() and 1 != 0) out[i shr 3] = (out[i shr 3].toInt() or (0x80 ushr (i and 7))).toByte()
        return out
    }

    fun unpackBits(bytes: ByteArray): ByteArray {
        val out = ByteArray(bytes.size * 8)
        for (i in out.indices) out[i] = ((bytes[i shr 3].toInt() ushr (7 - (i and 7))) and 1).toByte()
        return out
    }

    /** NRZ-M → NRZ-L: d[i] = e[i] xor e[i-1]. */
    fun nrzmDecode(bits: ByteArray, prev: Int = 0): ByteArray {
        val out = ByteArray(bits.size)
        if (bits.isEmpty()) return out
        out[0] = (bits[0].toInt() xor prev).toByte()
        for (i in 1 until bits.size) out[i] = (bits[i].toInt() xor bits[i - 1].toInt()).toByte()
        return out
    }

    fun nrzmEncode(bits: ByteArray, prev: Int = 0): ByteArray {
        val out = ByteArray(bits.size)
        var p = prev and 1
        for (i in bits.indices) { p = p xor (bits[i].toInt() and 1); out[i] = p.toByte() }
        return out
    }

    /** soft — int8 (I,Q,...) где >0 = 1; возвращает биты 0/1. */
    fun viterbi(soft: ByteArray): ByteArray {
        val n = soft.size / 2
        val out = ByteArray(n)
        LrptCodec.viterbi27(soft, n, out)
        return out
    }

    /** Свёрточный кодер: (кодированные биты 2n, новое состояние). */
    fun convEncode(bits: ByteArray, state: Int = 0): Pair<ByteArray, Int> {
        val out = ByteArray(bits.size * 2)
        val st = LrptCodec.convEncode(bits, bits.size, out, state)
        return Pair(out, st)
    }

    /** cw — 255 байт (изменяется на месте). Возвращает число исправленных байт или -1. */
    fun rsDecode(cw: ByteArray, dual: Boolean = false): Int = LrptCodec.rsDecode(cw, dual)

    fun rsEncode(data223: ByteArray, dual: Boolean = false): ByteArray {
        val par = ByteArray(32)
        LrptCodec.rsEncode(data223, par, dual)
        return par
    }

    private fun clip127(v: Int): Byte = (if (v > 127) 127 else if (v < -127) -127 else v).toByte()

    /** Снятие неоднозначности: 0:(a,b) 1:(-b,a) 2:(a,-b) 3:(b,a); 4..7 — то же со сдвигом на 1 мягкий бит. */
    fun transform(soft: ByteArray, hyp: Int): ByteArray {
        val from = if (hyp >= 4) 1 else 0
        val np = (soft.size - from).coerceAtLeast(0) / 2
        val out = ByteArray(np * 2)
        val k = hyp and 3
        for (i in 0 until np) {
            var a = soft[from + 2 * i].toInt()
            var b = soft[from + 2 * i + 1].toInt()
            when (k) {
                1 -> { val t = a; a = -b; b = t }
                2 -> b = -b
                3 -> { val t = a; a = b; b = t }
            }
            out[2 * i] = clip127(a)
            out[2 * i + 1] = clip127(b)
        }
        return out
    }

    /** Корреляция с ASM (±1) в позиции p. */
    private fun asmCorr(bits: ByteArray, p: Int): Int {
        var c = 0
        for (i in 0 until 32) c += (bits[p + i] * 2 - 1) * ASM_PM[i]
        return c
    }

    /** Позиции ASM (прямого и инверсного): [(pos, inverted)]. */
    fun asmHits(bits: ByteArray, maxErr: Int = 3, len: Int = bits.size): List<Pair<Int, Boolean>> {
        if (len < 32) return emptyList()
        val thr = 32 - 2 * maxErr
        val res = ArrayList<Pair<Int, Boolean>>()
        for (p in 0..len - 32) {
            val c = asmCorr(bits, p)
            if (abs(c) >= thr) res.add(Pair(p, c < 0))
        }
        return res
    }

    internal fun asmCorrAt(bits: ByteArray, p: Int) = asmCorr(bits, p)

    // ---------------------------------------------------------------- грубая оценка частоты

    /**
     * Грубая оценка частоты сигнала (Гц) по симметрии спектра и SNR (dB) — coarse_offset из lrpt.py.
     * x — отсчёты ДО согласованного фильтра (re, im). Спектр — Уэлч (окно Ханна, перекрытие 1/2).
     */
    fun coarseOffset(x: FloatArray, fs: Double, rs: Double = SYMBOL_RATE.toDouble(), maxHz: Double = 10000.0): Pair<Double, Double> {
        val n = x.size / 2
        if (n < 8192) return Pair(0.0, 0.0)
        var nfft = 1 shl ceil(log2(fs / 40.0)).toInt()
        nfft = min(nfft, 1 shl floor(log2(n / 4.0)).toInt())
        val pw = welch(x, n, fs, nfft)               // уже в порядке fftshift
        val df = fs / nfft
        val f = DoubleArray(nfft) { (it - nfft / 2) * df }
        val span = min(fs / 2, rs * 1.6)
        var i0 = 0
        while (i0 < nfft && abs(f[i0]) > span) i0++
        var i1 = nfft
        while (i1 > i0 && abs(f[i1 - 1]) > span) i1--
        val m = i1 - i0
        if (m < 2) return Pair(0.0, 0.0)
        val ff = DoubleArray(m) { f[i0 + it] }
        val p = DoubleArray(m) { pw[i0 + it] }
        val edgeLim = rs * 0.5 + maxHz + 2000
        val edgeVals = ArrayList<Double>()
        for (i in 0 until m) if (abs(ff[i]) >= edgeLim) edgeVals.add(p[i])
        val noise = if (edgeVals.size > 32) median(edgeVals.toDoubleArray()) else percentile(p.copyOf(), 10.0)
        val nz = max(noise, 1e-30)
        val q = DoubleArray(m) { max(p[it] / nz - 1.0, 0.0) }
        // сглаживание ~ 1 кГц (свёртка с ones(k)/k, режим «same»)
        val dff = ff[1] - ff[0]
        val k = max(1, (1000 / dff).toInt())
        val qs = DoubleArray(m)
        val inv = 1.0 / k
        val sh = (k - 1) / 2
        for (i in 0 until m) {
            val c = i + sh
            var s = 0.0
            for (j in max(0, c - k + 1)..min(m - 1, c)) s += q[j] * inv
            qs[i] = s
        }
        // автосвёртка: максимум при i+j = 2·центр; считаем только допустимые сдвиги
        var bestI = -1
        var bestV = -1.0
        for (mm in 0..2 * m - 2) {
            val lag = mm * dff / 2 + ff[0]
            if (abs(lag) > maxHz) continue
            var s = 0.0
            for (j in max(0, mm - m + 1)..min(mm, m - 1)) s += qs[j] * qs[mm - j]
            if (s > bestV) { bestV = s; bestI = mm }
        }
        if (bestI < 0) return Pair(0.0, 0.0)
        val c = bestI * dff / 2 + ff[0]
        var sum = 0.0
        var cnt = 0
        for (i in 0 until m) if (abs(ff[i] - c) <= rs * 0.35) { sum += p[i]; cnt++ }
        val mean = if (cnt > 0) sum / cnt else Double.NaN
        val snr = 10 * log10(max(mean / nz - 1.0, 1e-3))
        return Pair(c, snr)
    }

    private fun log2(v: Double) = ln(v) / ln(2.0)

    /** Спектральная плотность Уэлча (scipy.signal.welch, двусторонняя, без детренда), порядок fftshift. */
    internal fun welch(x: FloatArray, n: Int, fs: Double, nfft: Int): DoubleArray {
        val win = DoubleArray(nfft) { 0.5 - 0.5 * cos(2 * PI * it / nfft) }
        var w2 = 0.0
        for (v in win) w2 += v * v
        val step = nfft - nfft / 2
        val nseg = (n - nfft) / step + 1
        val acc = DoubleArray(nfft)
        val re = DoubleArray(nfft)
        val im = DoubleArray(nfft)
        val fft = DFft(nfft)
        for (sgm in 0 until nseg) {
            val o = sgm * step
            for (i in 0 until nfft) {
                re[i] = x[2 * (o + i)] * win[i]
                im[i] = x[2 * (o + i) + 1] * win[i]
            }
            fft.transform(re, im)
            for (i in 0 until nfft) acc[i] += re[i] * re[i] + im[i] * im[i]
        }
        val scale = 1.0 / (fs * w2) / nseg
        val out = DoubleArray(nfft)
        for (i in 0 until nfft) out[i] = acc[(i + nfft / 2) % nfft] * scale
        return out
    }

    internal fun median(v: DoubleArray): Double {
        val s = v.sortedArray()
        val n = s.size
        return if (n % 2 == 1) s[n / 2] else (s[n / 2 - 1] + s[n / 2]) / 2
    }

    /** numpy.percentile (метод linear) — v сортируется на месте. */
    internal fun percentile(v: DoubleArray, q: Double): Double {
        v.sort()
        return lerpIdx(v, q / 100 * (v.size - 1))
    }

    internal fun lerpIdx(s: DoubleArray, idx: Double): Double {
        val lo = floor(idx).toInt().coerceIn(0, s.size - 1)
        val hi = min(lo + 1, s.size - 1)
        val t = idx - lo
        val a = s[lo]
        val b = s[hi]
        val d = b - a
        return if (t >= 0.5) b - d * (1 - t) else a + d * t
    }
}

/** Комплексное БПФ двойной точности (радикс 2, на месте). */
internal class DFft(private val n: Int) {
    private val cosT = DoubleArray(n / 2) { cos(2 * PI * it / n) }
    private val sinT = DoubleArray(n / 2) { -sin(2 * PI * it / n) }
    private val rev = IntArray(n).also { r ->
        val bits = Integer.numberOfTrailingZeros(n)
        for (i in 0 until n) r[i] = Integer.reverse(i) ushr (32 - bits)
    }

    fun transform(re: DoubleArray, im: DoubleArray) {
        for (i in 0 until n) {
            val j = rev[i]
            if (j > i) {
                var t = re[i]; re[i] = re[j]; re[j] = t
                t = im[i]; im[i] = im[j]; im[j] = t
            }
        }
        var len = 2
        while (len <= n) {
            val half = len / 2
            val step = n / len
            var i = 0
            while (i < n) {
                var k = 0
                for (j in i until i + half) {
                    val wr = cosT[k]
                    val wi = sinT[k]
                    val xr = re[j + half] * wr - im[j + half] * wi
                    val xi = re[j + half] * wi + im[j + half] * wr
                    re[j + half] = re[j] - xr
                    im[j + half] = im[j] - xi
                    re[j] += xr
                    im[j] += xi
                    k += step
                }
                i += len
            }
            len = len shl 1
        }
    }
}

/** Состояние демодулятора для показа. */
data class DemodState(val residualHz: Double, val omega: Double, val agc: Double, val amp: Double, val lock: Double)

/**
 * IQ (fs) → мягкие символы. Перенос частоты, RRC + децимация — Kotlin, петли — DemodCore (C или Kotlin).
 * mode: "qpsk", "oqpsk", "bpsk".
 */
class Demodulator(
    fs: Double, rs: Double = Lrpt.SYMBOL_RATE.toDouble(), val mode: String = "oqpsk",
    costasBw: Double = 0.006, timingBw: Double = 0.002, alpha: Double = 0.5,
) {
    val fs = fs
    val rs = rs
    val oqpsk = mode == "oqpsk"
    val bpsk = mode == "bpsk"
    val up: Int
    val down: Int
    val fs2: Double
    val sps: Double
    val taps: FloatArray
    private var tailRe: FloatArray
    private var tailIm: FloatArray
    private var dphase = 0
    var offsetHz = 0.0
        private set
    private var nco = 0.0
    private val core: DemodCore

    /** последние ~0,4 с после переноса частоты (для грубой оценки) */
    val mixed = ArrayDeque<FloatArray>()
    var mixedN = 0
        private set
    var nsym = 0L
        private set
    private val st = DoubleArray(6)

    init {
        var u = 1
        while (fs * u < 4 * rs) u++
        up = u
        val fsu = fs * up
        down = max(1, floor(fsu / (4 * rs)).toInt())
        fs2 = fsu / down
        sps = fs2 / rs
        taps = Lrpt.rrcTaps(fsu / rs, alpha, 12).let { t -> FloatArray(t.size) { t[it] * up } }
        tailRe = FloatArray(taps.size - 1)
        tailIm = FloatArray(taps.size - 1)
        val m = when (mode) { "qpsk" -> 0; "oqpsk" -> 1; "bpsk" -> 2; else -> throw IllegalArgumentException(mode) }
        core = LrptCodec.demod(sps, m, costasBw, timingBw, 2 * PI * 8000.0 / fs2)
    }

    /** Частота сигнала относительно центра настройки (доплер + поправка), Гц. */
    fun setOffset(hz: Double) { offsetHz = hz }

    fun setResidual(hz: Double) = core.setFreq(2 * PI * hz / fs2)

    fun setCostasBw(bw: Double) = core.setCostas(bw)

    fun state(): DemodState {
        core.state(st)
        return DemodState(st[0] * fs2 / (2 * PI), st[2], st[3], st[4], (st[5] / 0.92).coerceIn(0.0, 1.0))
    }

    fun close() = core.close()

    /** Перенос частоты, RRC, ресемплинг до fs2. iq — n отсчётов (re, im); возвращает (re, im) с частотой fs2. */
    fun filter(iq: FloatArray, n: Int = iq.size / 2): FloatArray {
        var x = iq
        if (offsetHz != 0.0) {
            val w = -2 * PI * offsetHz / fs
            x = FloatArray(2 * n)
            // фазор пересчитывается точно каждые 512 отсчётов, между ними — поворот
            var k = 0
            while (k < n) {
                val ph0 = nco + w * k
                var c = cos(ph0)
                var s = sin(ph0)
                val dc = cos(w)
                val ds = sin(w)
                val end = min(n, k + 512)
                for (i in k until end) {
                    val cf = c.toFloat()
                    val sf = s.toFloat()
                    val a = iq[2 * i]
                    val b = iq[2 * i + 1]
                    x[2 * i] = a * cf - b * sf
                    x[2 * i + 1] = a * sf + b * cf
                    val c2 = c * dc - s * ds
                    s = c * ds + s * dc
                    c = c2
                }
                k = end
            }
            nco = PyNum.mod(nco + w * n, 2 * PI)
        } else if (x.size != 2 * n) {
            x = iq.copyOf(2 * n)
        } else {
            x = iq.copyOf()
        }
        mixed.addLast(x)
        mixedN += n
        while (mixed.size > 1 && mixedN - mixed.first().size / 2 >= fs * 0.4) mixedN -= mixed.removeFirst().size / 2
        val m = n * up
        val nt = taps.size
        val l = nt - 1
        val bre = FloatArray(l + m)
        val bim = FloatArray(l + m)
        System.arraycopy(tailRe, 0, bre, 0, l)
        System.arraycopy(tailIm, 0, bim, 0, l)
        for (i in 0 until n) {
            bre[l + i * up] = x[2 * i]
            bim[l + i * up] = x[2 * i + 1]
        }
        val first = dphase
        val nout = if (first < m) (m - 1 - first) / down + 1 else 0
        val out = FloatArray(2 * nout)
        val h = taps
        for (o in 0 until nout) {
            val j = first + o * down
            var sr = 0f
            var si = 0f
            // y[j] = Σ x[j+k]·h[nt-1-k] (свёртка, режим «valid»)
            for (kk in 0 until nt) {
                val t = h[nt - 1 - kk]
                sr += bre[j + kk] * t
                si += bim[j + kk] * t
            }
            out[2 * o] = sr
            out[2 * o + 1] = si
        }
        dphase = Math.floorMod(dphase - m, down)
        tailRe = bre.copyOfRange(m, m + l)
        tailIm = bim.copyOfRange(m, m + l)
        return out
    }

    class Output(val soft: ByteArray, val cons: FloatArray?)

    fun process(iq: FloatArray, wantCons: Boolean = true) = processFs2(filter(iq), wantCons)

    /** x — отсчёты fs2 (re, im). soft — мягкие биты, cons — точки созвездия (I, Q…). */
    fun processFs2(x: FloatArray, wantCons: Boolean = true): Output {
        val n = x.size / 2
        val maxs = (n / sps).toInt() + 16
        val soft = ByteArray(maxs * 2)
        val cons = if (wantCons) FloatArray(maxs * 2) else null
        val ns0 = core.process(x, n, soft, cons, maxs)
        nsym += ns0
        val ns = if (bpsk) ns0 else 2 * ns0
        return Output(soft.copyOf(ns), cons?.copyOf(2 * ns0))
    }
}

/** Кадр после RS: data — 1020 байт (VCDU 892 + проверочные), ok — все 4 слова RS исправимы. */
class Frame(val data: ByteArray, val ok: Boolean, val errors: IntArray)

/**
 * Мягкие символы → CADU (1024 байта, после дерандомизации и RS) — FrameDecoder из lrpt.py.
 * Поиск: 3 кадра мягких символов декодируются Витерби для 8 гипотез с NRZ-M и без; выбирается гипотеза,
 * где ASM повторяется через 8192 бита. При двух пропусках ASM подряд — повторный поиск по последним кадрам.
 */
class FrameDecoder(private val nrzmForced: Boolean? = null) {
    companion object {
        const val SEARCH_PAIRS = 3 * Lrpt.FRAME_BITS
        const val WIN = 8192          // пар на окно Витерби (один кадр)
        const val OVL = 384           // перекрытие окон (глубина трассировки)
        private val EMPTY = ByteArray(0)
    }

    var frames = 0
    var framesOk = 0
    var rsFixed = 0
    var rsDual: Boolean? = null
    val recent = ArrayDeque<Boolean>()
    var relocks = 0

    var locked = false
        private set
    var hyp: Int? = null
        private set
    var nrzm = false
        private set
    private var raw = EMPTY
    private var soft = EMPTY
    private val hist = ArrayDeque<ByteArray>()
    private var vtail: ByteArray? = null
    private var nrzmPrev = 0
    private var bits = EMPTY
    private var expect: Int? = null
    private var misses = 0

    init { reset() }

    fun reset() {
        locked = false
        hyp = null
        nrzm = false
        raw = EMPTY
        soft = EMPTY
        hist.clear()
        vtail = null
        nrzmPrev = 0
        bits = EMPTY
        expect = null
        misses = 0
    }

    val synced get() = locked && misses == 0 && frames > 0

    fun rsRatio(): Double = if (recent.isEmpty()) 0.0 else 100.0 * recent.count { it } / recent.size

    fun push(s: ByteArray): List<Frame> {
        val out = ArrayList<Frame>()
        if (locked) soft = cat(soft, s) else raw = cat(raw, s)
        val need = 2 * SEARCH_PAIRS + 2
        for (it in 0 until 8) {
            if (!locked) {
                if (raw.size < need) break
                if (!search(raw.copyOf(need))) {
                    raw = raw.copyOfRange(need - 2 * Lrpt.FRAME_BITS, raw.size)
                    continue
                }
                soft = if (hyp!! >= 4) raw.copyOfRange(1, raw.size) else raw
                raw = EMPTY
            }
            run(out)
            if (locked) break
        }
        return out
    }

    private fun search(r: ByteArray): Boolean {
        var bestGood = 0
        var bestHyp = -1
        var bestNz = false
        for (h in 0 until 8) {
            val t = Lrpt.transform(r, h)
            val vb = Lrpt.viterbi(t)
            val b = vb.copyOfRange(64, vb.size)
            val opts = if (nrzmForced == null) booleanArrayOf(false, true) else booleanArrayOf(nrzmForced)
            for (nz in opts) {
                val bb = if (nz) Lrpt.nrzmDecode(b) else b
                val hits = Lrpt.asmHits(bb)
                if (hits.size < 2) continue
                var good = 0
                for (p in hits) for (q in hits) if (q.first > p.first && (q.first - p.first) % Lrpt.FRAME_BITS == 0) good++
                if (good > 0 && (bestHyp < 0 || good > bestGood)) { bestGood = good; bestHyp = h; bestNz = nz }
            }
        }
        if (bestHyp < 0) return false
        hyp = bestHyp
        nrzm = bestNz
        locked = true
        misses = 0
        expect = null
        bits = EMPTY
        nrzmPrev = 0
        vtail = null
        hist.clear()
        return true
    }

    private fun run(out: MutableList<Frame>) {
        while (locked && soft.size >= 2 * WIN) {
            val take = 2 * WIN
            val chunk = soft.copyOf(take)
            soft = soft.copyOfRange(take, soft.size)
            hist.addLast(chunk)
            if (hist.size > 4) hist.removeFirst()
            val t = Lrpt.transform(chunk, hyp!! and 3)
            val vt = vtail
            val win: ByteArray
            var nb: ByteArray
            if (vt == null) {
                win = t
                val b = Lrpt.viterbi(win)
                nb = b.copyOf(b.size - OVL)
            } else {
                win = cat(vt, t)
                val b = Lrpt.viterbi(win)
                nb = b.copyOfRange(OVL, b.size - OVL)
            }
            vtail = win.copyOfRange(win.size - 4 * OVL, win.size)
            if (nrzm) {
                val d = Lrpt.nrzmDecode(nb, nrzmPrev)
                nrzmPrev = nb[nb.size - 1].toInt()
                nb = d
            }
            bits = cat(bits, nb)
            deframe(out)
        }
    }

    /** Конец записи: декодировать остаток. */
    fun flush(): List<Frame> {
        val out = ArrayList<Frame>()
        if (locked && soft.size >= 2 * OVL) {
            val t = Lrpt.transform(soft.copyOf(soft.size / 2 * 2), hyp!! and 3)
            val vt = vtail
            val win = if (vt == null) t else cat(vt, t)
            val b = Lrpt.viterbi(win)
            var nb = b.copyOfRange(if (vt == null) 0 else OVL, b.size)
            if (nrzm) nb = Lrpt.nrzmDecode(nb, nrzmPrev)
            bits = cat(bits, nb)
            soft = EMPTY
            deframe(out)
        }
        return out
    }

    private fun deframe(out: MutableList<Frame>) {
        while (locked) {
            if (expect == null) {
                val hits = Lrpt.asmHits(bits, 3)
                if (hits.isEmpty()) {
                    if (bits.size >= Lrpt.FRAME_BITS + 32) {
                        bits = bits.copyOfRange(bits.size - 32, bits.size)
                        miss()
                    }
                    return
                }
                expect = hits[0].first
            }
            var p = expect!!
            if (bits.size < p + Lrpt.FRAME_BITS + 16) {
                if (p > 16) {
                    bits = bits.copyOfRange(p - 16, bits.size)
                    expect = 16
                }
                return
            }
            // ASM ищем в окне ±16 бит от ожидаемого места (проскальзывание символа)
            val lo = max(0, p - 16)
            val hiEnd = min(bits.size, p + 16 + 32)
            var bestK = 0
            var bestC = 0
            var bestAbs = -1
            for (k in 0..(hiEnd - lo - 32)) {
                val c = Lrpt.asmCorrAt(bits, lo + k)
                if (abs(c) > bestAbs) { bestAbs = abs(c); bestC = c; bestK = k }
            }
            if (bestAbs < 32 - 2 * 6) {
                expect = null
                bits = bits.copyOfRange(min(bits.size, p + Lrpt.FRAME_BITS - 64), bits.size)
                miss()
                continue
            }
            p = lo + bestK
            val inv = bestC < 0
            misses = 0
            val frame = Lrpt.packBits(bits, p, Lrpt.FRAME_BITS)
            if (inv) for (i in frame.indices) frame[i] = (frame[i].toInt() xor 0xFF).toByte()
            out.add(finish(frame))
            bits = bits.copyOfRange(p + Lrpt.FRAME_BITS - 16, bits.size)
            expect = 16
        }
    }

    private fun miss() {
        misses++
        if (misses >= 2) {
            val r = if (hist.isNotEmpty()) {
                var acc = EMPTY
                for (h in hist.toList().takeLast(3)) acc = cat(acc, h)
                cat(acc, soft)
            } else soft
            relocks++
            val st = arrayOf<Any?>(frames, framesOk, rsFixed, rsDual, relocks)
            val rec = recent.toList()
            reset()
            frames = st[0] as Int; framesOk = st[1] as Int; rsFixed = st[2] as Int
            rsDual = st[3] as Boolean?; relocks = st[4] as Int
            recent.clear(); recent.addAll(rec)
            raw = r
        }
    }

    private fun finish(frame: ByteArray): Frame {
        val data = ByteArray(Lrpt.FRAME_BYTES - 4)
        val pn = Lrpt.PN
        for (i in data.indices) data[i] = (frame[4 + i].toInt() xor pn[i].toInt()).toByte()
        val dualFixed = rsDual
        val modes = if (dualFixed != null) booleanArrayOf(dualFixed) else booleanArrayOf(false, true)
        var best: Triple<ByteArray, IntArray, Boolean>? = null
        for (dual in modes) {
            val d = data.copyOf()
            val errs = IntArray(4)
            val cw = ByteArray(255)
            for (j in 0 until 4) {
                for (i in 0 until 255) cw[i] = d[j + 4 * i]
                val e = Lrpt.rsDecode(cw, dual)
                errs[j] = e
                if (e > 0) for (i in 0 until 255) d[j + 4 * i] = cw[i]
            }
            if (errs.all { it >= 0 }) { best = Triple(d, errs, dual); break }
            if (best == null) best = Triple(d, errs, dual)
        }
        val (d, nerr, dual) = best!!
        val ok = nerr.all { it >= 0 }
        if (ok && rsDual == null) rsDual = dual
        frames++
        if (ok) {
            framesOk++
            rsFixed += nerr.sum()
        }
        recent.addLast(ok)
        if (recent.size > 50) recent.removeFirst()
        return Frame(d, ok, nerr)
    }
}

internal fun cat(a: ByteArray, b: ByteArray): ByteArray {
    if (a.isEmpty()) return b
    if (b.isEmpty()) return a
    val r = a.copyOf(a.size + b.size)
    System.arraycopy(b, 0, r, a.size, b.size)
    return r
}
