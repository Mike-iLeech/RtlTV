package io.github.mikeileech.sdr4noob.dsp

import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.log10
import kotlin.math.sin

/** Комплексное БПФ по основанию 2 на месте (re, im), размер — степень двойки. */
class Fft(val n: Int) {
    private val cosT = FloatArray(n / 2)
    private val sinT = FloatArray(n / 2)
    private val rev = IntArray(n)

    init {
        require(n >= 2 && n and (n - 1) == 0) { "размер БПФ — степень двойки" }   // noqa: i18n — проверка программиста
        for (i in 0 until n / 2) {
            cosT[i] = cos(2 * PI * i / n).toFloat()
            sinT[i] = -sin(2 * PI * i / n).toFloat()
        }
        val bits = Integer.numberOfTrailingZeros(n)
        for (i in 0 until n) rev[i] = Integer.reverse(i) ushr (32 - bits)
    }

    fun transform(re: FloatArray, im: FloatArray) {
        for (i in 0 until n) {
            val j = rev[i]
            if (j > i) {
                var t = re[i]; re[i] = re[j]; re[j] = t
                t = im[i]; im[i] = im[j]; im[j] = t
            }
        }
        var size = 2
        while (size <= n) {
            val half = size / 2
            val step = n / size
            var start = 0
            while (start < n) {
                var k = 0
                for (j in start until start + half) {
                    val wr = cosT[k]; val wi = sinT[k]
                    val l = j + half
                    val tr = re[l] * wr - im[l] * wi
                    val ti = re[l] * wi + im[l] * wr
                    re[l] = re[j] - tr; im[l] = im[j] - ti
                    re[j] += tr; im[j] += ti
                    k += step
                }
                start += size
            }
            size *= 2
        }
    }
}

/**
 * Спектр мощности в dBFS (как spectrum_db(dbfs=True) в rtltv/radio.py): окно Блэкмана, усреднение
 * нескольких БПФ блока, ноль частоты посередине; незатухающий тон полной шкалы = 0 dB.
 */
class SpectrumAnalyzer(val size: Int = 2048, private val maxSegments: Int = 4) {
    private val fft = Fft(size)
    private val win = FloatArray(size) { i ->
        val a = 2 * PI * i / (size - 1)
        (0.42 - 0.5 * cos(a) + 0.08 * cos(2 * a)).toFloat()
    }
    private val norm = win.sum().toDouble().let { it * it }
    private val re = FloatArray(size)
    private val im = FloatArray(size)
    private val acc = DoubleArray(size)

    /** [out] получает [size] значений dBFS. false — блок короче одного БПФ. */
    fun compute(iqRe: FloatArray, iqIm: FloatArray, len: Int, out: FloatArray): Boolean {
        val segs = minOf(maxSegments, len / size)
        if (segs == 0) return false
        acc.fill(0.0)
        val stride = if (segs > 1) (len - size) / (segs - 1) else 0
        for (s in 0 until segs) {
            val off = s * stride
            for (i in 0 until size) { re[i] = iqRe[off + i] * win[i]; im[i] = iqIm[off + i] * win[i] }
            fft.transform(re, im)
            for (i in 0 until size) acc[i] += (re[i] * re[i] + im[i] * im[i]).toDouble()
        }
        val h = size / 2
        for (i in 0 until size) {
            val p = acc[(i + h) % size] / segs / norm
            out[i] = (10 * log10(p + 1e-20)).toFloat()
        }
        // остаток постоянной составляющей в центре — средним соседей (как в настольной версии)
        val c = h
        val fill = 0.5f * (out[c - 4] + out[c + 4])
        out[c - 1] = fill; out[c] = fill; out[c + 1] = fill
        return true
    }
}
