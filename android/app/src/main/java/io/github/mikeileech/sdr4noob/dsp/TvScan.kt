package io.github.mikeileech.sdr4noob.dsp

import kotlin.math.abs

/** Результат поиска по одному каналу; [found] для цифрового поиска выставляет [TvScan.finishDigital]. */
class TvScanResult(val ch: TvChannel, val kind: TvMode, var value: Double, var found: Boolean?)

/**
 * Поиск станций по сетке каналов — перенос Engine.start_scan / _scan_step (rtltv/engine.py).
 * Каждый канал — один захват [CAPTURE] отсчётов; оценка по спектру [NFFT] точек.
 * Аналог: пик несущей изображения (±60 кГц у −tuneOffset) над медианой спектра, найдено при > 15 дБ.
 * Цифра: медиана спектра в 0,1…1,1 МГц от центра; в конце значения отсчитываются от 25-го процентиля всех каналов,
 * найдено при > 3 дБ (шум у разных частот разный, поэтому абсолютный порог не годится).
 */
object TvScan {
    const val CAPTURE = 65536
    const val NFFT = 4096

    fun items(kind: TvMode, chans: List<TvChannel>, std: TvStandard): List<MuxHop> =
        chans.map { MuxHop(if (kind == TvMode.ANALOG) it.video + std.tuneOffset else it.center, CAPTURE) }

    private fun bin(i: Int, fs: Double) = (i - NFFT / 2) * (fs / NFFT)

    fun analogSnr(re: FloatArray, im: FloatArray, fs: Double, std: TvStandard, len: Int = re.size): Double {
        val p = TvDigital.psdDb(re, im, len, NFFT)
        val off = -std.tuneOffset
        val noise = TvDigital.median(DoubleArray(NFFT) { p[it].toDouble() })
        var mx = Double.NEGATIVE_INFINITY
        for (i in 0 until NFFT) if (abs(bin(i, fs) - off) < 60e3) mx = maxOf(mx, p[i].toDouble())
        return mx - noise
    }

    fun digitalLevel(re: FloatArray, im: FloatArray, fs: Double, len: Int = re.size): Double {
        val p = TvDigital.psdDb(re, im, len, NFFT)
        val v = ArrayList<Double>()
        for (i in 0 until NFFT) { val a = abs(bin(i, fs)); if (a > 0.1e6 && a < 1.1e6) v += p[i].toDouble() }
        return TvDigital.median(v.toDoubleArray())
    }

    /** Один шаг поиска (_scan_step): добавляет результат в [res]; на последнем цифровом шаге нормирует всё. */
    fun step(res: MutableList<TvScanResult>, kind: TvMode, ch: TvChannel, i: Int, n: Int,
             re: FloatArray, im: FloatArray, fs: Double, std: TvStandard, len: Int = re.size) {
        if (kind == TvMode.ANALOG) {
            val snr = analogSnr(re, im, fs, std, len)
            res += TvScanResult(ch, kind, snr, snr > 15)
        } else {
            res += TvScanResult(ch, kind, digitalLevel(re, im, fs, len), null)
            if (i == n - 1) finishDigital(res)
        }
    }

    fun finishDigital(res: List<TvScanResult>) {
        if (res.isEmpty()) return
        val ref = TvDigital.percentile(DoubleArray(res.size) { res[it].value }.also { it.sort() }, 25.0)
        for (r in res) { r.value -= ref; r.found = r.value > 3.0 }
    }
}
