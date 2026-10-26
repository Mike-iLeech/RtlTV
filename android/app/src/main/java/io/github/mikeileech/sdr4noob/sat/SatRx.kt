package io.github.mikeileech.sdr4noob.sat

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.ln
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin
import kotlin.math.sqrt

/*
 * Приём со спутника — то из rtltv/satrx.py и rtltv/modes/sat_window.py, что не зависит от Qt и устройства:
 * план приёма (частота настройки, полоса, доплер), пересчёт доплера в потоке DSP ([DopplerTracker], как в RxWorker),
 * автоприём «Метеоров» по расписанию пролётов ([AutoRxScheduler]), равномерная выдача кадров спектра ([SpecPacer]),
 * приёмник без декодера с уровнем и «созвездием» ([IqReceiver]) и шумоподавитель звука ([SpectralNR]).
 *
 * Комплексный сигнал — два массива (re, im), как в остальном DSP Android-версии.
 */

// ---------------------------------------------------------------- БПФ (double)

/** Комплексное БПФ в double: степень двойки — по основанию 2, иначе прямое ДПФ. [inverse] — без деления на n. */
internal object DFft {
    fun transform(re: DoubleArray, im: DoubleArray, inverse: Boolean = false) {
        val n = re.size
        if (n <= 1) return
        if (n and (n - 1) != 0) { dft(re, im, inverse); return }
        var j = 0
        for (i in 1 until n) {
            var bit = n shr 1
            while (j and bit != 0) { j = j xor bit; bit = bit shr 1 }
            j = j xor bit
            if (i < j) {
                var t = re[i]; re[i] = re[j]; re[j] = t
                t = im[i]; im[i] = im[j]; im[j] = t
            }
        }
        val sgn = if (inverse) 1.0 else -1.0
        var len = 2
        while (len <= n) {
            val half = len / 2
            val ang = sgn * 2 * PI / len
            for (k in 0 until half) {
                val wr = cos(ang * k)
                val wi = sin(ang * k)
                var s = 0
                while (s < n) {
                    val a = s + k
                    val b = a + half
                    val tr = re[b] * wr - im[b] * wi
                    val ti = re[b] * wi + im[b] * wr
                    re[b] = re[a] - tr; im[b] = im[a] - ti
                    re[a] += tr; im[a] += ti
                    s += len
                }
            }
            len *= 2
        }
    }

    private fun dft(re: DoubleArray, im: DoubleArray, inverse: Boolean) {
        val n = re.size
        val sgn = if (inverse) 1.0 else -1.0
        val outR = DoubleArray(n)
        val outI = DoubleArray(n)
        for (k in 0 until n) {
            var sr = 0.0
            var si = 0.0
            for (t in 0 until n) {
                val a = sgn * 2 * PI * ((k.toLong() * t) % n) / n
                val c = cos(a)
                val s = sin(a)
                sr += re[t] * c - im[t] * s
                si += re[t] * s + im[t] * c
            }
            outR[k] = sr; outI[k] = si
        }
        outR.copyInto(re); outI.copyInto(im)
    }
}

// ---------------------------------------------------------------- план приёма

/**
 * Параметры приёма выбранного передатчика (как start_rx в sat_window.py): [center] — частота настройки
 * (ниже сигнала на ПЧ, чтобы уйти от пика постоянной составляющей), [doppler] — поправлять ли доплер
 * (у геостационара и демо-генератора — нет), [iqBw] — полоса [IqReceiver], [halfBand] — полуширина полосы
 * на спектре, [recordAlways] — IQ пишется всегда (нет декодера снимков).
 */
data class RxPlan(
    val kind: String, val freq: Double, val fs: Int, val center: Double, val doppler: Boolean,
    val iqBw: Double, val halfBand: Double, val recordAlways: Boolean,
) {
    companion object {
        /** Сдвиг настройки ниже сигнала для LRPT/голоса/IQ (sat_window.IF_OFFSET). */
        const val IF_OFFSET = 200e3
        /** То же для LRIT «Электро-Л» (elektro.IF_OFFSET) и его частота дискретизации (elektro.LIVE_RATE). */
        const val LRIT_IF_OFFSET = 400e3
        const val LRIT_RATE = 2_048_000
        const val LRPT_SYMBOL_RATE = 72_000
        const val LRIT_SYMBOL_RATE = 294_000
        /** Частоты дискретизации, на которых работает приёмник LRPT. */
        val LRPT_RATES = intArrayOf(1_024_000, 1_200_000, 1_440_000)

        /** Частота демо-генератора (SynthSource.FS). */
        fun synthRate(kind: String) = if (kind == "lrit") LRIT_SYMBOL_RATE * 8 else LRPT_SYMBOL_RATE * 16

        /**
         * [rate] — выбранная частота дискретизации (для LRPT вне [LRPT_RATES] берётся 1 024 000, для LRIT — всегда
         * [LRIT_RATE]); [test] — демо-генератор; [dopplerOn] — галочка «доплер».
         */
        fun of(sat: Sat, tx: Tx, rate: Int, test: Boolean = false, dopplerOn: Boolean = true): RxPlan {
            val kind = tx.kind
            val geo = sat.norad in SatCatalog.GEO_NORADS
            val freq = tx.center
            var fs: Int
            val ifo: Double
            if (kind == "lrit") {
                fs = LRIT_RATE; ifo = LRIT_IF_OFFSET
            } else {
                fs = if (kind == "lrpt" && rate !in LRPT_RATES) 1_024_000 else rate
                ifo = IF_OFFSET
            }
            if (test) fs = synthRate(if (kind == "lrit") "lrit" else "lrpt")
            val bw = if (tx.isBand) tx.hi - tx.freq + 10e3 else if (tx.bw != 0.0) tx.bw else 30e3
            val half = if (tx.isBand) (tx.hi - tx.freq) / 2 + 30e3
            else when (kind) { "lrpt" -> 60e3; "lrit" -> 220e3; else -> 15e3 }
            return RxPlan(kind, freq, fs, freq - ifo, dopplerOn && !geo && !test, bw, half,
                kind == "iq" || kind == "voice")
        }
    }
}

// ---------------------------------------------------------------- доплер

/**
 * Пересчёт доплера в потоке DSP (как RxWorker._process): не чаще раза в 0,5 с по времени [update];
 * смещение сигнала от центра настройки = sig − center + доплер. При ошибке орбиты (NaN) доплер = 0.
 */
class DopplerTracker(
    val center: Double,
    val sig: Double,
    private val orbit: Orbit?,
    private val lat: Double,
    private val lon: Double,
    private val hKm: Double = 0.0,
    @Volatile var enabled: Boolean = true,
) {
    @Volatile var dopplerHz = 0.0
        private set
    @Volatile var offsetHz = sig - center
        private set
    private var lastT = 0.0

    /** Время [t] (unix) → новое смещение, Гц, если пора пересчитать, иначе null. */
    fun update(t: Double): Double? {
        if (abs(t - lastT) <= 0.5) return null
        lastT = t
        var d = 0.0
        if (enabled && orbit != null) {
            d = try { orbit.doppler(t, sig, lat, lon, hKm) } catch (e: ArithmeticException) { 0.0 }
            if (!d.isFinite()) d = 0.0
        }
        dopplerHz = d
        offsetHz = sig - center + d
        return offsetHz
    }
}

// ---------------------------------------------------------------- автоприём

/**
 * Автоприём «Метеоров» по расписанию (sat_window._auto_check): приём начинается, когда идёт пролёт «Метеора»
 * с максимумом не ниже minEl (с AOS − 30 с до LOS − 60 с), и останавливается через 20 с после LOS.
 */
class AutoRxScheduler(private val meteors: Set<Int> = SatCatalog.METEORS) {
    sealed class Action {
        object None : Action()
        data class Start(val pass: Pass) : Action()
        data class Stop(val pass: Pass) : Action()
    }

    /** Пролёт, на котором идёт автоприём, или null. */
    var current: Pass? = null
        private set

    /**
     * Проверка раз в секунду: [enabled] — кнопка автоприёма, [busy] — уже идёт приём/прослушивание/декодирование файла.
     * Start — начать приём pass.norad (передатчик 0); Stop — остановить (пролёт закончился).
     */
    fun check(now: Double, enabled: Boolean, passes: List<Pass>, minEl: Double, busy: Boolean): Action {
        if (!enabled) return Action.None
        current?.let { p ->
            if (now > p.los + 20) {
                current = null
                return Action.Stop(p)
            }
            return Action.None
        }
        if (busy) return Action.None
        for (p in passes) {
            if (p.norad in meteors && p.maxEl >= minEl && p.aos - 30 <= now && now <= p.los - 60) {
                current = p
                return Action.Start(p)
            }
        }
        return Action.None
    }

    /** Приём остановлен (вручную или по Stop). */
    fun stopped() { current = null }

    /** Автоприём выключен: true — надо остановить идущий автоприём. */
    fun disable(): Boolean = (current != null).also { current = null }
}

// ---------------------------------------------------------------- спектр

/**
 * Кадры спектра для интерфейса с равномерной выдачей (satrx.SpecPacer).
 * Блок IQ длиной T делится на k ≈ T·fps частей; каждая даёт свой кадр со своим временем выдачи.
 * [push] — в потоке приёма, [take] — в потоке интерфейса (последний наступивший кадр).
 */
class SpecPacer(fps: Double = 50.0) {
    class Frame(val n: Int, val freqs: DoubleArray, val psd: FloatArray)

    private class Item(val t: Double, val frame: Frame)

    private val dt = 1.0 / fps
    private val q = ArrayDeque<Item>()
    private val win = HashMap<Int, DoubleArray>()
    var count = 0
        private set
    private var lastRel = 0.0
    @Volatile var snap: Frame? = null
        private set

    @Synchronized
    fun push(re: FloatArray, im: FloatArray, len: Int, center: Double, fs: Double, nfft: Int, now: Double) {
        val n = nfft
        val tBlock = len / fs
        val k = max(1, min(min(12, Math.rint(tBlock / dt).toInt()), len / n))
        val part = len / k
        val w = win.getOrPut(n) { DoubleArray(n) { if (n == 1) 1.0 else 0.5 - 0.5 * cos(2 * PI * it / (n - 1)) }.also { a ->
            for (i in a.indices) a[i] = a[i].toFloat().toDouble() } }   // np.hanning(n).astype(float32)
        val freqs = DoubleArray(n) { i ->
            val kk = if (i < n / 2) i - n / 2 + n else i - n / 2      // fftshift(fftfreq)
            val f = if (kk < (n + 1) / 2) kk else kk - n
            center + f * (1.0 / (n * (1.0 / fs)))   // как np.fft.fftfreq: k · (1 / (n·d))
        }
        var t = min(max(now, lastRel), now + tBlock)
        val step = tBlock / k
        val xr = DoubleArray(n)
        val xi = DoubleArray(n)
        for (j in 0 until k) {
            val a = j * part
            if (a + n > len) break
            for (i in 0 until n) {
                xr[i] = (re[a + i] * w[i].toFloat()).toDouble()
                xi[i] = (im[a + i] * w[i].toFloat()).toDouble()
            }
            DFft.transform(xr, xi)
            val psd = FloatArray(n)
            val nn = n.toDouble() * n
            for (i in 0 until n) {
                val s = (i + n / 2) % n        // fftshift
                psd[i] = (10 * log10((xr[s] * xr[s] + xi[s] * xi[s]) / nn + 1e-12)).toFloat()
            }
            count++
            if (q.size >= 64) q.removeFirst()
            q.addLast(Item(t, Frame(count, freqs, psd)))
            t += step
        }
        lastRel = t
    }

    /** Очередь кадров (время выдачи, кадр) — для тестов. */
    @Synchronized
    internal fun pending(): List<Pair<Double, Frame>> = q.map { it.t to it.frame }

    @Synchronized
    fun take(now: Double): Frame? {
        while (q.isNotEmpty() && q.first().t <= now) snap = q.removeFirst().frame
        return snap
    }
}

// ---------------------------------------------------------------- приёмник без декодера

/**
 * Приёмник без декодера (satrx.IqReceiver): перенос на частоту спутника, ФНЧ (firwin 129, Хэмминг),
 * прореживание, «созвездие» (точки IQ) и уровень; шум — медиана спектра блока, пересчитанная на полосу фильтра.
 * Блок обрабатывается целиком (свёртка «valid», без хвоста прошлого блока) — как в Python.
 */
class IqReceiver(fs: Double, bw: Double = 40e3) {
    val fs = fs
    val bw = bw
    val dec = max(1, (fs / (4 * bw)).toInt())
    private val h: DoubleArray = firwin(129, bw / 2, fs)
    @Volatile var offset = 0.0
        private set
    private var nco = 0.0
    /** Последние точки «созвездия» (x, y, x, y …), нормированы на уровень, ×0,7. */
    @Volatile var lastCons = FloatArray(0)
        private set
    @Volatile var levelDb = -120.0
        private set
    @Volatile var noiseDb = -120.0
        private set
    var nblocks = 0
        private set

    fun setOffset(hz: Double) { offset = hz }

    fun feed(re: FloatArray, im: FloatArray, len: Int) {
        val n = len
        val w = -2 * PI * offset / fs
        val m = min(n, 65536)
        val s0 = n - m
        // перенос (только та часть блока, что идёт в фильтр)
        val xr = FloatArray(m)
        val xi = FloatArray(m)
        for (i in 0 until m) {
            val ph = nco + w * (s0 + i)
            val c = cos(ph).toFloat()
            val s = sin(ph).toFloat()
            val a = re[s0 + i]; val b = im[s0 + i]
            xr[i] = a * c - b * s
            xi[i] = a * s + b * c
        }
        nco = pmod(nco + w * n, 2 * PI)
        val taps = h.size
        val valid = m - taps + 1
        if (valid > 0) {
            val ny = (valid + dec - 1) / dec
            val yr = DoubleArray(ny)
            val yi = DoubleArray(ny)
            var p = 0.0
            for (q in 0 until ny) {
                val o = q * dec
                var sr = 0.0
                var si = 0.0
                // свёртка: y[o] = Σ h[k]·x[o + taps − 1 − k]
                for (k in 0 until taps) {
                    val idx = o + taps - 1 - k
                    sr += h[k] * xr[idx]; si += h[k] * xi[idx]
                }
                yr[q] = sr; yi[q] = si
                p += sr * sr + si * si
            }
            p = p / ny + 1e-20
            levelDb = 10 * log10(p)
            val segN = min(n, 8192)
            val fr = DoubleArray(segN) { re[it].toDouble() }
            val fi = DoubleArray(segN) { im[it].toDouble() }
            DFft.transform(fr, fi)
            val pw = DoubleArray(segN) { fr[it] * fr[it] + fi[it] * fi[it] }
            pw.sort()
            val med = if (segN % 2 == 1) pw[segN / 2] else 0.5 * (pw[segN / 2 - 1] + pw[segN / 2])
            val pn = med / segN / ln(2.0)
            noiseDb = 10 * log10(pn * bw / fs + 1e-20)
            val c0 = max(0, ny - 800)
            val sp = sqrt(p)
            val cons = FloatArray(2 * (ny - c0))
            for (q in c0 until ny) {
                cons[2 * (q - c0)] = (yr[q] / sp).toFloat() * 0.7f
                cons[2 * (q - c0) + 1] = (yi[q] / sp).toFloat() * 0.7f
            }
            lastCons = cons
        }
        nblocks++
    }

    val snrDb: Double get() = levelDb - noiseDb

    companion object {
        /** scipy.signal.firwin(taps, cutoff, fs=fs) в double: окно Хэмминга, нормировка на единичное усиление в нуле. */
        fun firwin(taps: Int, cutoffHz: Double, fs: Double): DoubleArray {
            val c = cutoffHz / (fs / 2)
            val alpha = 0.5 * (taps - 1)
            val h = DoubleArray(taps) { i ->
                val m = i - alpha
                val x = c * m
                val sinc = if (x == 0.0) 1.0 else sin(PI * x) / (PI * x)
                c * sinc * (0.54 - 0.46 * cos(2 * PI * i / (taps - 1)))
            }
            val s = h.sum()
            for (i in h.indices) h[i] /= s
            return h
        }
    }
}

// ---------------------------------------------------------------- шумоподавитель звука

/**
 * Потоковый шумоподавитель (satrx.SpectralNR): спектральное вычитание, окно [n] (sqrt-Ханн), шаг n/2.
 * Шум оценивается «минимальной статистикой» (минимум сглаженного спектра за 6 отрезков по 47 окон);
 * выход отстаёт от входа на буфер до [n] отсчётов. Для моно-звука.
 */
class SpectralNR(val n: Int = 512, val strength: Double = 1.6, val floor: Double = 0.12) {
    private val h = n / 2
    private val nb = n / 2 + 1
    private val win = FloatArray(n) { sqrt(0.5 - 0.5 * cos(2 * PI * it / n)).toFloat() }   // float32, как в numpy
    private var inbuf = FloatArray(0)
    private var ola = FloatArray(n)
    private var noise: DoubleArray? = null
    private var g = DoubleArray(nb)
    private var ps = DoubleArray(nb)
    private var curMin = DoubleArray(nb)
    private val mins = ArrayList<DoubleArray>()
    private var nfr = 0

    fun process(x: FloatArray, len: Int = x.size): FloatArray {
        val buf = FloatArray(inbuf.size + len)
        inbuf.copyInto(buf)
        x.copyInto(buf, inbuf.size, 0, len)
        var pos = 0
        val out = ArrayList<Float>()
        val re = DoubleArray(n)
        val im = DoubleArray(n)
        while (buf.size - pos >= n) {
            for (i in 0 until n) { re[i] = (buf[pos + i] * win[i]).toDouble(); im[i] = 0.0 }
            DFft.transform(re, im)
            val p = DoubleArray(nb) { re[it] * re[it] + im[it] * im[it] }
            if (noise == null) {
                ps = p.copyOf()
                curMin = p.copyOf()
                mins.clear()
                nfr = 0
                noise = p.copyOf()
                g = DoubleArray(nb) { 1.0 }
            }
            for (k in 0 until nb) {
                ps[k] = 0.8 * ps[k] + 0.2 * p[k]
                curMin[k] = min(curMin[k], ps[k])
            }
            nfr++
            if (nfr % 47 == 0) {
                mins.add(curMin)
                while (mins.size > 6) mins.removeAt(0)
                curMin = ps.copyOf()
                val nz = DoubleArray(nb) { k -> 1.5 * mins.minOf { it[k] } }
                noise = nz
            }
            val nz = noise!!
            for (k in 0 until nb) {
                val gk = max(1.0 - strength * nz[k] / (p[k] + 1e-12), floor)
                g[k] = 0.5 * g[k] + 0.5 * gk
            }
            // обратное вещественное БПФ спектра X·g (эрмитово продолжение; мнимые части DC и Найквиста отбрасываются)
            val yr = DoubleArray(n)
            val yi = DoubleArray(n)
            for (k in 0 until nb) { yr[k] = re[k] * g[k]; yi[k] = im[k] * g[k] }
            yi[0] = 0.0
            yi[n / 2] = 0.0
            for (k in 1 until n / 2) { yr[n - k] = yr[k]; yi[n - k] = -yi[k] }
            DFft.transform(yr, yi, inverse = true)
            for (i in 0 until n) ola[i] += (yr[i] / n).toFloat() * win[i]
            for (i in 0 until h) out.add(ola[i])
            val nola = FloatArray(n)
            ola.copyInto(nola, 0, h, n)
            ola = nola
            pos += h
        }
        inbuf = buf.copyOfRange(pos, buf.size)
        return out.toFloatArray()
    }
}
