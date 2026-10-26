package io.github.mikeileech.sdr4noob.dsp

import java.util.concurrent.ConcurrentHashMap
import kotlin.math.PI
import kotlin.math.atan2
import kotlin.math.ceil
import kotlin.math.cos
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.pow
import kotlin.math.sqrt

/**
 * Цифровое ТВ: измеритель мультиплекса DVB-T/T2 — перенос rtltv/digital.py.
 *
 * Декодера DVB-T2 нет: полоса обзора Android-версии — до 2,88 МГц (радиорежим, RadioDemod.RADIO_RATES), а мультиплекс
 * шириной 7,8 МГц. Поэтому это прибор для наведения антенны: канал собирается из нескольких перестроек
 * ([MuxAnalyzer.plan] — обход с шагом 2 МГц, от каждой берётся ±1 МГц), считаются уровень, C/N, неравномерность,
 * а по циклическому префиксу одного длинного захвата — режим FFT, защитный интервал и уход частоты ([OfdmEstimator]).
 * Отсчёты IQ везде — пара массивов re/im и длина.
 */
object TvDigital {
    const val HOP_STEP = 2.0e6
    const val HOP_KEEP = 1.0e6
    const val SPEC_FFT = 1024
    /** Длина захвата для анализа OFDM в плане и порог, с которого [MuxAnalyzer.add] считает захват «длинным». */
    const val CP_CAPTURE = 393216
    const val CP_MIN = 300000
    const val HOP_CAPTURE = 32768

    /** Полезная длительность символа, 8 МГц (элементарный период 7/64 мкс). */
    val FFT_MODES: Map<String, Double> = linkedMapOf(
        "1K" to 112e-6, "2K" to 224e-6, "4K" to 448e-6, "8K" to 896e-6, "16K" to 1792e-6, "32K" to 3584e-6)
    val GUARDS: Map<String, Double> = linkedMapOf(
        "1/128" to 1.0 / 128, "1/32" to 1.0 / 32, "1/16" to 1.0 / 16, "19/256" to 19.0 / 256,
        "1/8" to 1.0 / 8, "19/128" to 19.0 / 128, "1/4" to 1.0 / 4)

    private val ffts = ConcurrentHashMap<Int, Fft>()
    internal fun fft(n: Int): Fft = ffts.getOrPut(n) { Fft(n) }
    private val hann = ConcurrentHashMap<Int, FloatArray>()

    /** np.hanning(n) (симметричное окно) в float32. */
    private fun hanning(n: Int): FloatArray = hann.getOrPut(n) {
        FloatArray(n) { i -> if (n == 1) 1f else (0.5 - 0.5 * cos(2 * PI * i / (n - 1))).toFloat() }
    }

    /**
     * Спектр мощности в дБ (psd_db): окно Ханна, среднее по целым кускам [nfft], ноль частоты посередине,
     * провал вместо пика постоянной составляющей (3 средних бина = среднее бинов c±3). Короче [nfft] — всё −120.
     */
    fun psdDb(re: FloatArray, im: FloatArray, len: Int = re.size, nfft: Int = SPEC_FFT): FloatArray {
        val segs = len / nfft
        if (segs == 0) return FloatArray(nfft) { -120f }
        val f = fft(nfft)
        val w = hanning(nfft)
        val sr = FloatArray(nfft); val si = FloatArray(nfft)
        val acc = DoubleArray(nfft)
        for (s in 0 until segs) {
            val o = s * nfft
            for (i in 0 until nfft) { sr[i] = re[o + i] * w[i]; si[i] = im[o + i] * w[i] }
            f.transform(sr, si)
            for (i in 0 until nfft) acc[i] += (sr[i] * sr[i] + si[i] * si[i]).toDouble()
        }
        val norm = segs * (nfft * 0.375)
        val h = nfft / 2
        val p = DoubleArray(nfft) { acc[(it + h) % nfft] / norm }
        val c = h
        val fill = 0.5 * (p[c - 3] + p[c + 3])
        p[c - 1] = fill; p[c] = fill; p[c + 1] = fill
        return FloatArray(nfft) { (10 * log10(p[it] + 1e-12)).toFloat() }
    }

    /** Разовая оценка OFDM по одному захвату (detect_ofdm). */
    fun detectOfdm(re: FloatArray, im: FloatArray, fs: Double, len: Int = re.size): OfdmResult =
        OfdmEstimator(fs).add(re, im, len)

    /**
     * Убрать постоянную составляющую и узкие паразитные тоны донгла (_prep): они коррелируют на любом сдвиге и
     * забивают слабую корреляцию циклического префикса. Берётся старшая степень двойки отсчётов; бины спектра,
     * которые в 6 раз выше скользящего среднего по 129 бинам (и их соседи, и бин 0), обнуляются.
     */
    fun prep(re: FloatArray, im: FloatArray, len: Int = re.size): Pair<FloatArray, FloatArray> {
        require(len >= 2) { "мало отсчётов" }
        val n = Integer.highestOneBit(len)
        val xr = re.copyOf(n); val xi = im.copyOf(n)
        val f = fft(n)
        f.transform(xr, xi)
        val p = FloatArray(n) { xr[it] * xr[it] + xi[it] * xi[it] }
        val k = 129
        val hk = k / 2
        // кольцевое скользящее среднее по k бинам с центром в бине (np.convolve(..., "same") по расширенному массиву)
        val ref = DoubleArray(n)
        var s = 0.0
        for (j in -hk..hk) s += p[Math.floorMod(j, n)]
        for (i in 0 until n) {
            ref[i] = s / k
            s += p[Math.floorMod(i + hk + 1, n)] - p[Math.floorMod(i - hk, n)]
        }
        val bad0 = BooleanArray(n) { p[it] > 6 * ref[it] }
        for (i in 0 until n) {
            val bad = i == 0 || bad0[i] || bad0[(i - 1 + n) % n] || bad0[(i + 1) % n]
            if (bad) { xr[i] = 0f; xi[i] = 0f }
        }
        // обратное БПФ через сопряжение
        for (i in 0 until n) xi[i] = -xi[i]
        f.transform(xr, xi)
        val inv = 1f / n
        for (i in 0 until n) { xr[i] *= inv; xi[i] = -xi[i] * inv }
        return xr to xi
    }

    private fun lagOf(mode: String, fs: Double): Int = Math.rint(FFT_MODES.getValue(mode) * fs).toInt()

    /** Нормированная автокорреляция на сдвиге полезной длительности символа каждого режима FFT (mode_scores). */
    fun modeScores(xr: FloatArray, xi: FloatArray, fs: Double): LinkedHashMap<String, Double> {
        val n = xr.size
        var pw = 0.0
        for (i in 0 until n) pw += (xr[i] * xr[i] + xi[i] * xi[i]).toDouble()
        pw = pw / n + 1e-12
        val out = LinkedHashMap<String, Double>()
        for (name in FFT_MODES.keys) {
            val lag = lagOf(name, fs)
            if (lag.toLong() * 4 > n) continue
            var sr = 0.0; var si = 0.0
            for (i in 0 until n - lag) {
                val a = xr[i]; val b = xi[i]; val c = xr[i + lag]; val d = xi[i + lag]
                sr += (a * c + b * d).toDouble(); si += (b * c - a * d).toDouble()
            }
            val m = (n - lag).toDouble()
            out[name] = sqrt((sr / m) * (sr / m) + (si / m) * (si / m)) / pw
        }
        return out
    }

    /**
     * Свёртка произведения x·conj(x сдвинутого на Tu) по длине символа (guard_scores): у верного защитного интервала
     * пик острее. Возвращает {GI: (острота, фаза пика)}.
     */
    fun guardScores(xr: FloatArray, xi: FloatArray, fs: Double, mode: String): LinkedHashMap<String, Pair<Double, Double>> {
        val lag = lagOf(mode, fs)
        val n = xr.size - lag
        val out = LinkedHashMap<String, Pair<Double, Double>>()
        if (n <= 0) return out
        val pr = FloatArray(n); val pi = FloatArray(n)
        for (i in 0 until n) {
            val a = xr[i]; val b = xi[i]; val c = xr[i + lag]; val d = xi[i + lag]
            pr[i] = a * c + b * d; pi[i] = b * c - a * d
        }
        val nb = 256
        for ((gname, g) in GUARDS) {
            val period = lag * (1 + g)
            if (period * 3 > n) continue
            val fr = DoubleArray(nb); val fi = DoubleArray(nb)
            for (i in 0 until n) {
                val b = ((i.toDouble() % period) / period * nb).toLong().toInt()
                fr[b] += pr[i].toDouble(); fi[b] += pi[i].toDouble()
            }
            val mag = DoubleArray(nb) { Math.hypot(fr[it], fi[it]) }
            val w = max(1, Math.rint(nb * g / (1 + g)).toInt())
            val sm = DoubleArray(nb) { i ->
                var s = 0.0
                for (j in 0 until w) s += mag[(i + j) % nb]
                s / w
            }
            var am = 0
            for (i in 1 until nb) if (mag[i] > mag[am]) am = i
            out[gname] = (sm.max() / (median(sm) + 1e-12)) to atan2(fi[am], fr[am])
        }
        return out
    }

    internal fun median(a: DoubleArray): Double {
        val s = a.sortedArray()
        val n = s.size
        return if (n % 2 == 1) s[n / 2] else 0.5 * (s[n / 2 - 1] + s[n / 2])
    }

    /** np.percentile(a, q) с линейной интерполяцией (по умолчанию numpy). */
    internal fun percentile(sorted: DoubleArray, q: Double): Double {
        val pos = q / 100.0 * (sorted.size - 1)
        val lo = kotlin.math.floor(pos).toInt()
        val hi = minOf(lo + 1, sorted.size - 1)
        val t = pos - lo
        val a = sorted[lo]; val b = sorted[hi]
        return if (t >= 0.5) b - (b - a) * (1 - t) else a + (b - a) * t
    }
}

/** Итог анализа OFDM: при found=false guard/peak/freqOffsetHz могут отсутствовать. */
data class OfdmResult(
    val found: Boolean,
    val mode: String,
    val guard: String?,
    val rho: Double,
    val peak: Double?,
    val captures: Int,
    val freqOffsetHz: Double?,
)

/** Итог измерения мультиплекса: спектр всего канала ([freqs] — абсолютные частоты), C/N, уровень, неравномерность. */
class MuxResult(
    val freqs: DoubleArray,
    val psd: FloatArray,
    val cnDb: Double,
    val levelDb: Double,
    val flatnessDb: Double,
    val present: Boolean,
    val other: Boolean,
    val ofdm: OfdmResult?,
)

/** Одна перестройка плана обхода: частота приёмника и сколько отсчётов взять. */
data class MuxHop(val freq: Double, val samples: Int)

/** Сборка канала из перестроек и метрики мультиплекса (MuxAnalyzer). */
class MuxAnalyzer(fs: Double, center: Double, bandwidth: Double) {
    val fs = fs
    val center = center
    val bandwidth = bandwidth
    val offsets: List<Double>
    private val parts = HashMap<Long, Pair<DoubleArray, FloatArray>>()
    var cp: OfdmResult? = null
        private set
    val ofdm = OfdmEstimator(fs)

    init {
        require(fs >= 2 * TvDigital.HOP_KEEP) { "полоса ${fs / 1e6} МГц меньше куска обхода ±1 МГц" }
        val span = bandwidth / 2 + 1.0e6
        val n = ceil(span / TvDigital.HOP_STEP).toInt()
        offsets = (-n..n).map { it * TvDigital.HOP_STEP }
    }

    /**
     * Несколько проходов по каналу и один длинный захват для анализа OFDM (он долгий — если делать его каждый
     * проход, спектр обновлялся бы ~1 раз/с).
     */
    fun plan(withCp: Boolean, sweeps: Int = 3): List<MuxHop> {
        val one = offsets.map { MuxHop(center + it, TvDigital.HOP_CAPTURE) }
        val items = ArrayList<MuxHop>()
        repeat(max(1, sweeps)) { items += one }
        if (withCp) items += MuxHop(center, TvDigital.CP_CAPTURE)
        return items
    }

    /** Возвращает итог, когда собран весь канал (дальше — после каждой перестройки, скользящая сборка). */
    fun add(freq: Double, re: FloatArray, im: FloatArray, len: Int = re.size): MuxResult? {
        if (len >= TvDigital.CP_MIN) {
            cp = ofdm.add(re, im, len)
            return null
        }
        val off = freq - center
        val p = TvDigital.psdDb(re, im, len)
        val nf = TvDigital.SPEC_FFT
        val fl = ArrayList<Double>(); val pl = ArrayList<Float>()
        for (i in 0 until nf) {
            val f = (i - nf / 2) * (fs / nf)
            if (kotlin.math.abs(f) <= TvDigital.HOP_KEEP) { fl += f + off; pl += p[i] }
        }
        parts[Math.rint(off).toLong()] = fl.toDoubleArray() to pl.toFloatArray()
        if (parts.size < offsets.size) return null
        val keys = parts.keys.sorted()
        val fr = keys.flatMap { parts.getValue(it).first.asList() }.toDoubleArray()
        val pr = keys.flatMap { parts.getValue(it).second.asList() }.toFloatArray()
        return metrics(fr, pr)
    }

    private fun metrics(f: DoubleArray, p: FloatArray): MuxResult {
        val hb = bandwidth / 2
        val lin = DoubleArray(p.size) { 10.0.pow(p[it] / 10.0) }
        val inb = f.indices.filter { kotlin.math.abs(f[it]) < hb - 0.25e6 }
        val outb = f.indices.filter { kotlin.math.abs(f[it]) > hb + 0.15e6 }
        val sig = inb.sumOf { lin[it] } / inb.size
        val noise = if (outb.isNotEmpty()) TvDigital.median(DoubleArray(outb.size) { lin[outb[it]] })
        else TvDigital.percentile(lin.sortedArray(), 5.0)
        val cn = if (sig > noise) 10 * log10(max(sig - noise, 1e-12) / max(noise, 1e-12)) else 0.0
        val pin = DoubleArray(inb.size) { p[inb[it]].toDouble() }
        val mean = pin.average()
        val flat = sqrt(pin.sumOf { (it - mean) * (it - mean) } / pin.size)
        val level = 10 * log10(sig * bandwidth / fs + 1e-12)
        val peaky = pin.max() - TvDigital.median(pin) > 15.0   // узкая несущая: аналоговое ТВ и т.п.
        val ofdmOk = cp?.found == true
        val present = cn > 3.0 && (ofdmOk || (flat < 5.0 && !peaky))
        return MuxResult(DoubleArray(f.size) { f[it] + center }, p, cn, level, flat, present, cn > 3.0 && !present, cp)
    }
}

/**
 * Оценка режима FFT / защитного интервала по циклическому префиксу; оценки копятся по захватам (на реальном эфире
 * с многолучёвостью один захват ненадёжен).
 */
class OfdmEstimator(val fs: Double) {
    val modes = LinkedHashMap<String, Double>()
    val guards = LinkedHashMap<Pair<String, String>, Double>()
    var n = 0
        private set

    /**
     * Режим выбирается не по величине корреляции, а по остроте пика в свёртке по символу: у DVB-T2 пилоты дают
     * корреляцию и на долях Tu (Tu/4, Tu/6...), но она ровная во времени, а циклический префикс даёт пик ровно в
     * защитном интервале.
     */
    fun add(re: FloatArray, im: FloatArray, len: Int = re.size): OfdmResult {
        val (xr, xi) = TvDigital.prep(re, im, len)
        val a = if (n > 0) 0.35 else 1.0
        n++
        val scores = TvDigital.modeScores(xr, xi, fs)
        for ((k, v) in scores) modes[k] = (1 - a) * (modes[k] ?: v) + a * v
        val thr = 5.0 / sqrt(xr.size.toDouble())
        val phases = HashMap<Pair<String, String>, Double>()
        for ((m, rho) in scores) {
            if (rho < thr) continue
            for ((g, sc) in TvDigital.guardScores(xr, xi, fs, m)) {
                val key = m to g
                guards[key] = (1 - a) * (guards[key] ?: sc.first) + a * sc.first
                phases[key] = sc.second
            }
        }
        for (key in guards.keys.toList()) {
            val s = scores[key.first]
            if (s == null || s < thr) guards[key] = guards.getValue(key) * (1 - a)
        }
        if (guards.isEmpty()) {
            val best = modes.entries.maxByOrNull { it.value }?.key ?: "?"
            return OfdmResult(false, best, null, modes[best] ?: 0.0, null, n, null)
        }
        val (key, pk) = guards.entries.maxByOrNull { it.value }!!.let { it.key to it.value }
        val ph = phases[key]
        val off = if (ph != null) -ph / (2 * PI * TvDigital.FFT_MODES.getValue(key.first)) else null
        return OfdmResult(pk > 5.0, key.first, key.second, modes[key.first] ?: 0.0, pk, n, off)
    }
}
