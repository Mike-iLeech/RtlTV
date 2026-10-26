package io.github.mikeileech.sdr4noob.dsp

import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.sin
import kotlin.math.sqrt
import kotlin.math.tan

/**
 * Фильтры и прореживание — перенос блоков rtltv/radio.py (Decimator, Fir, Sos, deemph) на Kotlin.
 * Все блоки потоковые: результат не зависит от того, как поток порезан на блоки.
 * Комплексный сигнал хранится двумя массивами (re, im) — без объектов на отсчёт.
 */
object Firwin {
    /** КИХ ФНЧ окном Хэмминга, срез [cutoffHz] при частоте [fs]; коэффициенты нормированы (сумма = 1) — как scipy.signal.firwin. */
    fun lowpass(taps: Int, cutoffHz: Double, fs: Double): FloatArray {
        require(taps % 2 == 1) { "нечётное число отводов" }
        val fc = cutoffHz / fs  // доля частоты дискретизации
        val m = (taps - 1) / 2.0
        val h = DoubleArray(taps)
        for (i in 0 until taps) {
            val t = i - m
            val sinc = if (t == 0.0) 2 * fc else sin(2 * PI * fc * t) / (PI * t)
            val w = 0.54 - 0.46 * cos(2 * PI * i / (taps - 1))
            h[i] = sinc * w
        }
        val s = h.sum()
        return FloatArray(taps) { (h[it] / s).toFloat() }
    }

    /**
     * КИХ полосовой фильтр окном Хэмминга [f1Hz]…[f2Hz] — как scipy.signal.firwin(taps, (f1, f2), pass_zero=False, fs=fs):
     * h = r·sinc(r·m) − l·sinc(l·m) (частоты в долях Найквиста), умножение на окно, нормировка на единичное усиление
     * в центре полосы ((l + r) / 2). Коэффициенты в double (для APRS, где расчёт идёт в double, как в numpy).
     */
    fun bandpass(taps: Int, f1Hz: Double, f2Hz: Double, fs: Double): DoubleArray {
        val nyq = fs / 2
        val l = f1Hz / nyq
        val r = f2Hz / nyq
        val alpha = 0.5 * (taps - 1)
        fun sinc(x: Double) = if (x == 0.0) 1.0 else sin(PI * x) / (PI * x)
        val h = DoubleArray(taps)
        for (i in 0 until taps) {
            val m = i - alpha
            val w = 0.54 - 0.46 * cos(2 * PI * i / (taps - 1))
            h[i] = (r * sinc(r * m) - l * sinc(l * m)) * w
        }
        val sf = 0.5 * (l + r)
        var s = 0.0
        for (i in 0 until taps) s += h[i] * cos(PI * (i - alpha) * sf)
        for (i in 0 until taps) h[i] /= s
        return h
    }
}

/** Потоковый комплексный КИХ-дециматор: считаются только нужные выходные отсчёты. */
class ComplexDecimator(val factor: Int, private val h: FloatArray) {
    private val n = h.size
    private var hRe = FloatArray(n - 1)
    private var hIm = FloatArray(n - 1)
    private var bufRe = FloatArray(0)
    private var bufIm = FloatArray(0)
    private var phase = 0   // смещение первого выходного отсчёта в следующем блоке

    constructor(factor: Int, cutoffHz: Double, fs: Double, tapsPerPhase: Int = 16) :
        this(factor, if (factor > 1) Firwin.lowpass(factor * tapsPerPhase + 1, cutoffHz, fs) else floatArrayOf(1f))

    /** Возвращает число выходных отсчётов в [outRe]/[outIm] (размер не меньше inLen / factor + 1). */
    fun process(inRe: FloatArray, inIm: FloatArray, inLen: Int, outRe: FloatArray, outIm: FloatArray): Int {
        if (factor == 1 && n == 1) {
            System.arraycopy(inRe, 0, outRe, 0, inLen); System.arraycopy(inIm, 0, outIm, 0, inLen)
            return inLen
        }
        val total = n - 1 + inLen
        if (bufRe.size < total) { bufRe = FloatArray(total); bufIm = FloatArray(total) }
        val br = bufRe; val bi = bufIm
        System.arraycopy(hRe, 0, br, 0, n - 1); System.arraycopy(hIm, 0, bi, 0, n - 1)
        System.arraycopy(inRe, 0, br, n - 1, inLen); System.arraycopy(inIm, 0, bi, n - 1, inLen)
        var k = 0
        var pos = phase
        val hh = h
        while (pos + n <= total) {
            var sr = 0f; var si = 0f
            var j = 0
            while (j < n) {
                val c = hh[j]
                sr += c * br[pos + j]; si += c * bi[pos + j]
                j++
            }
            outRe[k] = sr; outIm[k] = si
            k++
            pos += factor
        }
        phase = pos - (total - (n - 1))
        System.arraycopy(br, total - (n - 1), hRe, 0, n - 1); System.arraycopy(bi, total - (n - 1), hIm, 0, n - 1)
        return k
    }

    fun reset() { hRe.fill(0f); hIm.fill(0f); phase = 0 }
}

/** Потоковый вещественный КИХ-дециматор. */
class RealDecimator(val factor: Int, private val h: FloatArray) {
    private val n = h.size
    private val hist = FloatArray(n - 1)
    private var buf = FloatArray(0)
    private var phase = 0

    constructor(factor: Int, cutoffHz: Double, fs: Double, tapsPerPhase: Int) :
        this(factor, Firwin.lowpass(factor * tapsPerPhase + 1, cutoffHz, fs))

    fun process(x: FloatArray, len: Int, out: FloatArray): Int {
        val total = n - 1 + len
        if (buf.size < total) buf = FloatArray(total)
        val b = buf
        System.arraycopy(hist, 0, b, 0, n - 1)
        System.arraycopy(x, 0, b, n - 1, len)
        var k = 0
        var pos = phase
        val hh = h
        while (pos + n <= total) {
            var s = 0f
            var j = 0
            while (j < n) { s += hh[j] * b[pos + j]; j++ }
            out[k++] = s
            pos += factor
        }
        phase = pos - (total - (n - 1))
        System.arraycopy(b, total - (n - 1), hist, 0, n - 1)
        return k
    }
}

/** Биквад (прямая форма II транспонированная), коэффициенты в double — устойчив и на 40 Гц при 240 кГц. */
class Biquad(private val b0: Double, private val b1: Double, private val b2: Double,
             private val a1: Double, private val a2: Double) {
    private var z1 = 0.0
    private var z2 = 0.0

    fun step(x: Double): Double {
        val y = b0 * x + z1
        z1 = b1 * x - a1 * y + z2
        z2 = b2 * x - a2 * y
        return y
    }

    fun process(x: FloatArray, len: Int) {
        for (i in 0 until len) x[i] = step(x[i].toDouble()).toFloat()
    }

    fun reset() { z1 = 0.0; z2 = 0.0 }

    companion object {
        /** Баттерворт 2-го порядка (Q = 1/√2), билинейное преобразование. */
        fun lowpass(fc: Double, fs: Double, q: Double = 1 / sqrt(2.0)): Biquad {
            val k = tan(PI * fc / fs)
            val norm = 1 / (1 + k / q + k * k)
            val b0 = k * k * norm
            return Biquad(b0, 2 * b0, b0, 2 * (k * k - 1) * norm, (1 - k / q + k * k) * norm)
        }

        fun highpass(fc: Double, fs: Double, q: Double = 1 / sqrt(2.0)): Biquad {
            val k = tan(PI * fc / fs)
            val norm = 1 / (1 + k / q + k * k)
            return Biquad(norm, -2 * norm, norm, 2 * (k * k - 1) * norm, (1 - k / q + k * k) * norm)
        }
    }
}

/** Цепочка биквадов: Баттерворт 4-го порядка = два звена с Q 0,541 и 1,307. */
class BiquadChain(private val stages: List<Biquad>) {
    fun process(x: FloatArray, len: Int) { for (s in stages) s.process(x, len) }

    companion object {
        private val Q4 = doubleArrayOf(0.5411961, 1.3065630)
        fun lowpass4(fc: Double, fs: Double) = BiquadChain(Q4.map { Biquad.lowpass(fc, fs, it) })
        fun highpass2(fc: Double, fs: Double) = BiquadChain(listOf(Biquad.highpass(fc, fs)))
    }
}

/** Деэмфазис ЧМ-вещания: однополюсный ФНЧ с постоянной [tau] (50 мкс в Европе, 75 мкс в США). */
class Deemphasis(tau: Double, fs: Double) {
    private val a = exp(-1 / (fs * tau)).toFloat()
    private var y = 0f
    fun process(x: FloatArray, len: Int) {
        var s = y
        val b = 1 - a
        for (i in 0 until len) { s = b * x[i] + a * s; x[i] = s }
        y = s
    }
}

/** Перенос частоты с непрерывной фазой (вращающийся фазор с перенормировкой). */
class Mixer(private val fs: Double) {
    private var pr = 1.0
    private var pi = 0.0

    fun process(re: FloatArray, im: FloatArray, len: Int, shiftHz: Double) {
        if (shiftHz == 0.0) return
        val w = 2 * PI * shiftHz / fs
        // фазор во float, перенормировка каждые 1024 отсчёта (ошибка амплитуды за это время < 1e-4)
        val cw = cos(w).toFloat(); val sw = sin(w).toFloat()
        var r = pr.toFloat(); var i = pi.toFloat()
        var n = 0
        while (n < len) {
            val end = minOf(len, n + 1024)
            while (n < end) {
                val xr = re[n]; val xi = im[n]
                re[n] = xr * r - xi * i
                im[n] = xr * i + xi * r
                val nr = r * cw - i * sw
                i = r * sw + i * cw
                r = nr
                n++
            }
            val g = 1f / sqrt(r * r + i * i)
            r *= g; i *= g
        }
        pr = r.toDouble(); pi = i.toDouble()
    }
}
