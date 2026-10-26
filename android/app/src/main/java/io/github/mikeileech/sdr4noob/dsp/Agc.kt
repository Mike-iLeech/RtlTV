package io.github.mikeileech.sdr4noob.dsp

import kotlin.math.abs
import kotlin.math.exp
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.roundToInt

/**
 * АРУ звука в духе SDR# — перенос Agc из rtltv/radio.py: огибающая по пикам кусков по 1 мс с упреждением
 * (атака раньше пика, без хлопка), спад с постоянной [decayMs], удержание, порог и наклон. Усиление между
 * кусками интерполируется — нет «ступенек». on = false — постоянное усиление [manualDb].
 * Звук задерживается на время упреждения; на выходе — только целые куски (остаток ждёт следующего блока).
 */
class Agc(private val fs: Int = 48_000, var target: Double = 0.5, var thresholdDb: Double = -100.0,
          var decayMs: Double = 400.0, var slopeDb: Double = 0.0, var hang: Boolean = false, lookaheadMs: Double = 5.0) {
    companion object { const val CH = 48 }

    var on = true
    var manualDb = 40.0
    var hangMs = 500.0
    private val dChunks = max(1, (lookaheadMs * fs / 1000 / CH).roundToInt())
    private var env = 0.0
    private var hold = 0
    private var g = 1.0
    private var gh = DoubleArray(dChunks) { 1.0 }
    private var inbuf = FloatArray(0)
    private var dbuf = FloatArray(dChunks * CH)

    fun reset() {
        env = 0.0; hold = 0; g = 1.0; gh = DoubleArray(dChunks) { 1.0 }; inbuf = FloatArray(0); dbuf = FloatArray(dChunks * CH)
    }

    fun gainDb() = 20 * log10(max(g, 1e-12))

    private fun law(e0: Double): Double {
        val thr = 10.0.pow(thresholdDb / 20)
        val e = max(e0, thr)
        var gg = target / e
        if (slopeDb > 0) gg *= (e / thr).pow(slopeDb / 20.0)
        return min(gg, 0.95 / max(e0, 1e-12))
    }

    /** Обработать [n] отсчётов [x]; результат в [out] (вернёт число отсчётов, кратно CH). */
    fun process(x: FloatArray, n: Int, out: FloatArray): Int {
        val buf = FloatArray(inbuf.size + n)
        System.arraycopy(inbuf, 0, buf, 0, inbuf.size)
        System.arraycopy(x, 0, buf, inbuf.size, n)
        val nch = buf.size / CH
        val take = nch * CH
        inbuf = buf.copyOfRange(take, buf.size)
        if (nch == 0) return 0
        val gains = DoubleArray(nch)
        if (!on) {
            gains.fill(10.0.pow(manualDb / 20))
        } else {
            val k = exp(-CH.toDouble() / fs / max(decayMs / 1000, 1e-3))
            val hangN = if (hang) (hangMs * fs / 1000 / CH).toInt() else 0
            var e = env; var h = hold
            for (i in 0 until nch) {
                var p = 0.0
                for (j in i * CH until (i + 1) * CH) p = max(p, abs(buf[j].toDouble()))
                if (p >= e) { e = p; h = hangN } else if (h > 0) h-- else e = max(p, e * k)
                gains[i] = law(e)
            }
            env = e; hold = h
        }
        // упреждение: к куску i−D применяется минимальное усиление окна [i−D, i]
        val hist = gh + gains
        val gOut = DoubleArray(nch) { i -> var m = hist[i]; for (j in 1..dChunks) m = min(m, hist[i + j]); m }
        gh = hist.copyOfRange(hist.size - dChunks, hist.size)
        // задержанный звук
        val dl = FloatArray(dbuf.size + take)
        System.arraycopy(dbuf, 0, dl, 0, dbuf.size)
        System.arraycopy(buf, 0, dl, dbuf.size, take)
        dbuf = dl.copyOfRange(take, dl.size)
        // плавная интерполяция усиления внутри куска
        var g0 = g
        for (i in 0 until nch) {
            val g1 = gOut[i]
            for (j in 0 until CH) {
                val fr = (j + 1).toDouble() / CH
                out[i * CH + j] = (dl[i * CH + j] * (g0 + (g1 - g0) * fr)).toFloat()
            }
            g0 = g1
        }
        g = gOut[nch - 1]
        return take
    }
}

/** Потоковый комплексный КИХ без прореживания (фильтр SSB). */
class ComplexFir(private var h: FloatArray) {
    private var hr = FloatArray(h.size - 1)
    private var hi = FloatArray(h.size - 1)

    fun setTaps(nh: FloatArray) {
        if (nh.size != h.size) {
            val r = FloatArray(nh.size - 1); val i = FloatArray(nh.size - 1)
            val k = min(r.size, hr.size)
            System.arraycopy(hr, hr.size - k, r, r.size - k, k); System.arraycopy(hi, hi.size - k, i, i.size - k, k)
            hr = r; hi = i
        }
        h = nh
    }

    fun process(re: FloatArray, im: FloatArray, n: Int) {
        val m = h.size - 1
        val br = FloatArray(m + n); val bi = FloatArray(m + n)
        System.arraycopy(hr, 0, br, 0, m); System.arraycopy(hi, 0, bi, 0, m)
        System.arraycopy(re, 0, br, m, n); System.arraycopy(im, 0, bi, m, n)
        val hh = h
        for (i in 0 until n) {
            var sr = 0f; var si = 0f
            // свёртка: y[i] = Σ h[k]·x[i+m−k]
            for (k in hh.indices) { val c = hh[k]; sr += c * br[i + m - k]; si += c * bi[i + m - k] }
            re[i] = sr; im[i] = si
        }
        System.arraycopy(br, n, hr, 0, m); System.arraycopy(bi, n, hi, 0, m)
    }
}
