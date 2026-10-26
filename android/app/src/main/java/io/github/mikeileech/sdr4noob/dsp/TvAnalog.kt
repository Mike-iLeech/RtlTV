package io.github.mikeileech.sdr4noob.dsp

import kotlin.math.PI
import kotlin.math.atan2
import kotlin.math.ceil
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.floor
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin
import kotlin.math.sqrt

/** Кадр аналогового ТВ: яркость [image] (0 = чёрный, 1 = белый) размером [height]×[width] построчно. */
class AnalogFrame(
    val image: FloatArray,
    val width: Int,
    val height: Int,
    val hlock: Double,
    val vlock: Boolean,
    val levelDb: Double,
    val snrDb: Double,
)

/**
 * Аналоговое ТВ: AM-VSB видео с негативной модуляцией — перенос AnalogVideoDecoder из rtltv/analog.py.
 * Огибающая → строчная/кадровая синхронизация → кадр яркости.
 *
 * Полоса (до 2,88 МГц) меньше полосы видеосигнала (6 МГц), поэтому картинка чёрно-белая и примерно 130 точек
 * по горизонтали: цветовая поднесущая (4,4 МГц) в окно не попадает. Приёмник настраивают на несущую изображения
 * + [TvStandard.tuneOffset] (см. [Tv.receiverCenter]).
 */
class AnalogVideoDecoder(fs: Double, val std: TvStandard) {
    companion object {
        const val OUT_W = 480

        /** Кольцевая свёртка профиля с прямоугольником ширины w: позиция максимума и сама свёртка (_box_peak). */
        internal fun boxPeak(prof: FloatArray, w: Int): Pair<Int, DoubleArray> {
            val n = prof.size
            val cs = DoubleArray(n + w)
            for (i in 0 until n + w - 1) cs[i + 1] = cs[i] + prof[i % n].toDouble()
            val box = DoubleArray(n) { (cs[w + it] - cs[it]) / w }
            var k = 0
            for (i in 1 until n) if (box[i] > box[k]) k = i
            return k to box
        }
    }

    val fs = fs
    val nominal = fs / std.lineFreq
    var fieldSwap = false
    var period = nominal
        private set
    private var buf = FloatArray(0)
    private var bufLen = 0
    private var pos = 0.0
    var hlock = 0.0
        private set
    var vlock = false
        private set
    private var vscore: DoubleArray? = null

    fun reset() {
        period = nominal
        buf = FloatArray(0); bufLen = 0
        pos = 0.0
        hlock = 0.0
        vlock = false
        vscore = null
    }

    /** Подать блок IQ; возвращает готовые кадры (может быть пусто). */
    fun feed(re: FloatArray, im: FloatArray, len: Int = re.size): List<AnalogFrame> {
        if (bufLen + len > buf.size) buf = buf.copyOf(max(bufLen + len, buf.size * 3 / 2 + 1024))
        for (i in 0 until len) buf[bufLen + i] = sqrt(re[i] * re[i] + im[i] * im[i])
        bufLen += len
        val out = ArrayList<AnalogFrame>()
        val l = std.lines
        while (bufLen >= pos + (l + 3) * period + 2) {
            out += frame()
            val drop = pos.toInt() - 8
            if (drop > 0) {
                System.arraycopy(buf, drop, buf, 0, bufLen - drop)
                bufLen -= drop
                pos -= drop
            }
        }
        return out
    }

    private fun sample(idx: Double): Float {
        var i0 = floor(idx).toLong()
        val fr = (idx - i0).toFloat()
        i0 = min(max(i0, 0L), (bufLen - 2).toLong())
        val i = i0.toInt()
        return buf[i] * (1f - fr) + buf[i + 1] * fr
    }

    private fun frame(): AnalogFrame {
        val p = period
        val l = std.lines
        val np = Math.rint(p).toInt()
        val colstep = p / np
        val starts = DoubleArray(l) { pos + it * p }
        val s = FloatArray(l * np)
        for (r in 0 until l) for (c in 0 until np) s[r * np + c] = sample(starts[r] + c * colstep)

        val sorted = DoubleArray(s.size) { s[it].toDouble() }.also { it.sort() }
        val tip = max(TvDigital.percentile(sorted, 99.0).toFloat().toDouble(), 1e-6)
        val thr = (0.875 * tip).toFloat()
        val sync = BooleanArray(s.size) { s[it] > thr }

        // строчная синхронизация: где по всем строкам чаще всего стоит импульс 4,7 мкс
        val w = max(2, Math.rint(std.hsyncUs * 1e-6 * fs / colstep).toInt())
        fun colMean(r0: Int, r1: Int) = FloatArray(np) { c ->
            var cnt = 0
            for (r in r0 until r1) if (sync[r * np + c]) cnt++
            (cnt.toDouble() / (r1 - r0)).toFloat()
        }
        val (k, box) = boxPeak(colMean(0, l), w)
        val q = box[k] - TvDigital.median(box)
        hlock = q
        val errCols = if (k <= np / 2) k else k - np
        val err = errCols * colstep

        if (q > 0.4) {
            val half = l / 2
            val kt = boxPeak(colMean(0, half), w).first
            val kb = boxPeak(colMean(half, l), w).first
            val d = Math.floorMod(kb - kt + np / 2, np) - np / 2
            val slope = d * colstep / half
            period += 0.5 * slope + 0.02 * err / l
            period = min(max(period, nominal * 0.998), nominal * 1.002)
        }

        // строки заново по найденным фронтам строчных импульсов
        val line0 = DoubleArray(l) { starts[it] + err }
        val sal = FloatArray(l * np)
        for (r in 0 until l) for (c in 0 until np) sal[r * np + c] = sample(line0[r] + c * colstep)

        // кадровая синхронизация: шаблон начала поля 1 (две строки широких импульсов, перед ними уравнивающие)
        // и через полкадра — строка «уравнивающий | широкий» поля 2
        val hN = np / 2
        val a0 = (0.25 * hN).toInt(); val a1 = (0.8 * hN).toInt()
        fun rowMean(r: Int, c0: Int, c1: Int): Double {
            if (c1 <= c0) return Double.NaN
            var cnt = 0
            for (c in c0 until c1) if (sal[r * np + c] > thr) cnt++
            return cnt.toDouble() / (c1 - c0)
        }
        val m1 = DoubleArray(l) { rowMean(it, a0, a1) }
        val m2 = DoubleArray(l) { rowMean(it, hN + a0, hN + a1) }
        val fh = l / 2
        val score = DoubleArray(l) { i ->
            m1[i] + m2[i] + m1[(i + 1) % l] + m2[(i + 1) % l] + (1 - m2[(i - 1 + l) % l]) +
                m2[(i + fh) % l] + (1 - m1[(i + fh) % l])
        }
        // «маховик»: оценка копится по кадрам, кадр сдвигаем, только если другая строка явно лучше
        var vs = vscore?.takeIf { it.size == l } ?: DoubleArray(l)
        vs = if (q > 0.3) DoubleArray(l) { 0.6 * vs[it] + 0.4 * score[it] } else DoubleArray(l) { 0.9 * vs[it] }
        var r = 0
        for (i in 1 until l) if (vs[i] > vs[r]) r = i
        var shift: Int? = null
        if (r != 0 && vs[r] > 4.8 && vs[r] > vs[0] + 0.6) {
            shift = r
            vs = DoubleArray(l) { vs[(it + r) % l] }
        } else if (vs[0] > 4.3) {
            shift = 0
        }
        vscore = vs

        val vc = std.visCount
        val bp0 = (5.8e-6 * fs / colstep).toInt()
        val bp1 = max(bp0 + 1, (9.8e-6 * fs / colstep).toInt())
        val tipEnd = min(max(2, w - 1), np)
        var tsum = 0.0; var tn = 0
        var psum = 0.0; var pn = 0
        val pe = min(bp1, np)
        for (i in 0 until vc) {
            val row = (std.vis1 + i) * np
            for (c in 1 until tipEnd) { tsum += sal[row + c]; tn++ }
            for (c in bp0 until pe) { psum += sal[row + c]; pn++ }
        }
        val tipv = (tsum / tn).toFloat().toDouble()
        var blank = (psum / pn).toFloat().toDouble()
        var pvar = 0.0
        for (i in 0 until vc) {
            val row = (std.vis1 + i) * np
            for (c in bp0 until pe) { val d = sal[row + c] - blank; pvar += d * d }
        }
        val noise = sqrt(pvar / pn).toFloat().toDouble()
        if (tipv - blank < 0.08 * tipv) blank = 0.75 * tipv
        val span = 2.5 * (tipv - blank)

        val ow = OUT_W
        val cols = DoubleArray(ow) { (std.activeStartUs + it * (std.activeUs / ow)) * 1e-6 * fs }
        val img = FloatArray(2 * vc * ow)
        val bl = blank.toFloat(); val sp = span.toFloat()
        for (i in 0 until vc) {
            val a = line0[std.vis1 + i]; val b = line0[std.vis2 + i]
            val ra = if (fieldSwap) b else a
            val rb = if (fieldSwap) a else b
            for (c in 0 until ow) {
                img[(2 * i) * ow + c] = (bl - sample(ra + cols[c])) / sp
                img[(2 * i + 1) * ow + c] = (bl - sample(rb + cols[c])) / sp
            }
        }

        pos += l * p + (if (q > 0.5) 0.3 else 0.6) * err
        if (shift != null && shift != 0) {
            pos += shift * p
            vlock = false
        } else {
            vlock = shift == 0
        }

        val snr = 20 * log10(max(tipv - blank, 1e-6) / max(noise, 1e-6))
        return AnalogFrame(img, ow, 2 * vc, q, vlock, 20 * log10(tip), if (q > 0.3) snr else 0.0)
    }
}

/**
 * FM-звук аналогового ТВ (девиация ±50 кГц, предыскажения 50 мкс) → 48 кГц моно — перенос FmSoundDecoder
 * из rtltv/analog.py. [carrierOffset] — где несущая звука относительно центра приёмника (обычно −[Tv.SOUND_TUNE]).
 */
class FmSoundDecoder(fs: Double, carrierOffset: Double, deemph: Double = 50e-6) {
    companion object { const val OUT_RATE = 48000 }

    private val w = -2 * PI * carrierOffset / fs
    private var ph = 0.0
    val d1 = max(1, floor(fs / 200e3).toInt())
    val fs1 = fs / d1
    private val h1 = firwinD(8 * d1 + 1, 110e3, fs)
    private val z1r = DoubleArray(h1.size - 1)   // последние входы (старые → новые)
    private val z1i = DoubleArray(h1.size - 1)
    private var p1 = 0
    private var prevR = 1.0
    private var prevI = 0.0
    val d2 = max(1, floor(fs1 / 40e3).toInt())
    val fs2 = fs1 / d2
    private val h2 = firwinD(6 * d2 + 1, 15e3, fs1)
    private val z2 = DoubleArray(h2.size - 1)
    private var p2 = 0
    private val deA = 1 - exp(-1 / (fs2 * deemph))
    private var zde = 0.0
    private var dc = 0.0
    private val ratio = fs2 / OUT_RATE
    private var t = 0.0
    private var last = 0.0

    /** scipy.signal.firwin(taps, cutoff, fs=fs) в double: окно Хэмминга, сумма = 1. */
    private fun firwinD(taps: Int, cutoff: Double, fs: Double): DoubleArray {
        val fc = cutoff / fs
        val m = (taps - 1) / 2.0
        val h = DoubleArray(taps) { i ->
            val tt = i - m
            val sinc = if (tt == 0.0) 2 * fc else sin(2 * PI * fc * tt) / (PI * tt)
            sinc * (0.54 - 0.46 * cos(2 * PI * i / (taps - 1)))
        }
        val s = h.sum()
        return DoubleArray(taps) { h[it] / s }
    }

    fun process(re: FloatArray, im: FloatArray, len: Int = re.size): FloatArray {
        val n = len
        val h = h1
        val nh = h.size
        val hist = nh - 1
        // смеситель и КИХ1 с прореживанием d1 (считаем только нужные выходы)
        val xr = DoubleArray(hist + n); val xi = DoubleArray(hist + n)
        System.arraycopy(z1r, 0, xr, 0, hist); System.arraycopy(z1i, 0, xi, 0, hist)
        for (i in 0 until n) {
            val a = ph + w * i
            val c = cos(a); val s = sin(a)
            val r = re[i].toDouble(); val q = im[i].toDouble()
            xr[hist + i] = r * c - q * s
            xi[hist + i] = r * s + q * c
        }
        ph = PyNum.mod(ph + w * n, 2 * PI)
        System.arraycopy(xr, n, z1r, 0, hist); System.arraycopy(xi, n, z1i, 0, hist)
        val yIdx = ArrayList<Int>()
        run { var i = p1; while (i < n) { yIdx += i; i += d1 } }
        p1 = Math.floorMod(p1 - n, d1)
        if (yIdx.isEmpty()) return FloatArray(0)
        val ny = yIdx.size
        val yr = DoubleArray(ny); val yi = DoubleArray(ny)
        for ((j, i) in yIdx.withIndex()) {
            var sr = 0.0; var si = 0.0
            val base = hist + i
            for (k in 0 until nh) { sr += h[k] * xr[base - k]; si += h[k] * xi[base - k] }
            yr[j] = sr; yi[j] = si
        }
        // частотный детектор
        val gain = fs1 / (2 * PI * 50e3)
        val dem = DoubleArray(ny)
        var pr = prevR; var pi = prevI
        for (j in 0 until ny) {
            val cr = yr[j]; val ci = yi[j]
            dem[j] = atan2(ci * pr - cr * pi, cr * pr + ci * pi) * gain
            pr = cr; pi = ci
        }
        prevR = pr; prevI = pi
        // КИХ2 с прореживанием d2
        val hh = h2
        val nh2 = hh.size
        val hist2 = nh2 - 1
        val ext = DoubleArray(hist2 + ny)
        System.arraycopy(z2, 0, ext, 0, hist2)
        System.arraycopy(dem, 0, ext, hist2, ny)
        System.arraycopy(ext, ny, z2, 0, hist2)
        val aIdx = ArrayList<Int>()
        run { var i = p2; while (i < ny) { aIdx += i; i += d2 } }
        p2 = Math.floorMod(p2 - ny, d2)
        val a = DoubleArray(aIdx.size)
        for ((j, i) in aIdx.withIndex()) {
            var s = 0.0
            val base = hist2 + i
            for (k in 0 until nh2) s += hh[k] * ext[base - k]
            a[j] = s
        }
        // коррекция предыскажений: y[n] = A·x[n] − (A − 1)·y[n−1]
        for (j in a.indices) {
            val y = deA * a[j] + zde
            zde = -(deA - 1) * y
            a[j] = y
        }
        // убрать постоянную составляющую (расстройка несущей звука)
        if (a.isNotEmpty()) {
            dc = 0.995 * dc + 0.005 * a.average()
            for (j in a.indices) a[j] -= dc
        }
        // линейная интерполяция на 48 кГц
        val data = DoubleArray(a.size + 1)
        data[0] = last
        System.arraycopy(a, 0, data, 1, a.size)
        if (data.size < 2) return FloatArray(0)
        val span = (data.size - 1).toDouble()
        val cnt = if (span > t) ceil((span - t) / ratio).toInt() else 0
        val out = FloatArray(cnt)
        for (i in 0 until cnt) {
            val ps = t + i * ratio
            val i0 = min(floor(ps).toInt(), data.size - 1)
            out[i] = if (i0 >= data.size - 1) data[data.size - 1].toFloat()
            else (data[i0] + (data[i0 + 1] - data[i0]) * (ps - i0)).toFloat()
        }
        t = if (cnt > 0) t + (cnt - 1) * ratio + ratio - span else t - span
        last = data[data.size - 1]
        return out
    }
}
