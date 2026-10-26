package io.github.mikeileech.sdr4noob.dsp

import io.github.mikeileech.sdr4noob.dsp.Sstv.F_BLACK
import io.github.mikeileech.sdr4noob.dsp.Sstv.F_LEADER
import io.github.mikeileech.sdr4noob.dsp.Sstv.F_SPAN
import io.github.mikeileech.sdr4noob.dsp.Sstv.F_SYNC
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.ceil
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToLong
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Потоковый декодер SSTV — перенос класса SstvDecoder из rtltv/sstv.py.
 *
 * Вход — звук float моно после демодулятора (на Android — NFM 48 кГц). Цепочка: перенос на 1700 Гц →
 * ФНЧ (комплексный, ±1100 Гц) → прореживание до ~12 кГц → квадратурный частотный детектор → трек частоты.
 * По треку: детектор VIS (лидер 1900, старт-бит 1200, биты 1100/1300, чётность), поиск строчных
 * синхроимпульсов 1200 Гц, робастная линейная подгонка позиций синхро (наклон = разница тактовых частот),
 * коррекция расстройки по лидеру и синхро, усреднение частоты по длительности пикселя.
 */

/** События декодера (как словари событий process() в sstv.py). Картинки — ARGB построчно. */
sealed class SstvEvent {
    data class Vis(val mode: String, val code: Int, val offset: Double) : SstvEvent()
    data class Start(val mode: String, val width: Int, val height: Int, val manual: Boolean) : SstvEvent()

    /** Готовая строка [y] картинки (по мере приёма): прогресс = (y + 1) / [height]. */
    class Line(val mode: String, val y: Int, val width: Int, val height: Int, val rgb: IntArray) : SstvEvent()

    /** Картинка принята (или поток кончился): [lines] — принято строк картинки. */
    class Done(val mode: String, val image: IntArray, val width: Int, val height: Int, val complete: Boolean,
               val lines: Int, val slantPpm: Double, val offset: Double) : SstvEvent()

    /** Синхронизация потеряна: частичная картинка. */
    class Lost(val mode: String, val image: IntArray, val width: Int, val height: Int,
               val lines: Int, val slantPpm: Double, val offset: Double) : SstvEvent()
}

/** Для индикатора: частота тона, уровень, уверенность синхронизации, состояние ("idle"/"pending"/"image"). */
data class SstvLevel(val freq: Double, val amp: Double, val sync: Double, val state: String, val mode: String?,
                     val line: Int, val lines: Int, val offset: Double, val slantPpm: Double)

/** round(x, 1) по-питоновски (с точностью до десятых). */
private fun round1(x: Double) = Math.rint(x * 10.0) / 10.0

/** Медиана как np.median (среднее двух средних при чётном числе). */
internal fun medianOf(a: DoubleArray): Double {
    val s = a.sortedArray()
    val n = s.size
    return if (n % 2 == 1) s[n / 2] else (s[n / 2 - 1] + s[n / 2]) / 2
}

/** Комплексный перенос + ФНЧ + прореживание + квадратурный частотный детектор (_Demod). */
internal class SstvDemod(fs: Double, private val fc: Double = 1700.0, cutoff: Double = 1100.0) {
    val fs = fs
    val D: Int
    val fsInt: Double
    private val h: DoubleArray
    private var histRe: DoubleArray
    private var histIm: DoubleArray
    private var n = 0L
    private var prevRe = 0.0
    private var prevIm = 0.0
    /** Таблица гетеродина, если период fs/НОД(fs, fc) невелик (fs и fc целые), иначе null. */
    private val mixCos: DoubleArray?
    private val mixSin: DoubleArray?

    init {
        require(fs >= 6000) { "частота дискретизации слишком мала для SSTV" }
        D = max(1, Math.rint(fs / 12000.0).toInt())
        fsInt = fs / D
        val ntaps = max(31, Math.rint(fs / 48000.0 * 96).toInt() or 1)
        h = firwin(ntaps, cutoff, fs)
        histRe = DoubleArray(ntaps - 1)
        histIm = DoubleArray(ntaps - 1)
        var period = 0
        if (fs == floor(fs) && fc == floor(fc) && fs < 1e7) {
            val a = fs.toLong(); val b = fc.toLong()
            period = (a / gcd(a, b)).toInt()
        }
        if (period in 1..200_000) {
            mixCos = DoubleArray(period) { cos(2 * PI * fc / fs * it) }
            mixSin = DoubleArray(period) { sin(2 * PI * fc / fs * it) }
        } else { mixCos = null; mixSin = null }
    }

    private fun gcd(a: Long, b: Long): Long = if (b == 0L) a else gcd(b, a % b)

    /** КИХ ФНЧ окном Хэмминга (scipy.signal.firwin), в double. */
    private fun firwin(taps: Int, cutoffHz: Double, fs: Double): DoubleArray {
        val fcn = cutoffHz / fs
        val m = (taps - 1) / 2.0
        val hh = DoubleArray(taps) { i ->
            val t = i - m
            val sinc = if (t == 0.0) 2 * fcn else sin(2 * PI * fcn * t) / (PI * t)
            sinc * (0.54 - 0.46 * cos(2 * PI * i / (taps - 1)))
        }
        val s = hh.sum()
        for (i in hh.indices) hh[i] /= s
        return hh
    }

    fun reset() { histRe.fill(0.0); histIm.fill(0.0); n = 0; prevRe = 0.0; prevIm = 0.0 }

    /** Блок звука → (трек частоты, амплитуда) на частоте [fsInt]. */
    fun process(x: FloatArray, off: Int, len: Int): Pair<FloatArray, FloatArray> {
        if (len == 0) return FloatArray(0) to FloatArray(0)
        val L = h.size
        val hl = L - 1
        val eRe = DoubleArray(hl + len)
        val eIm = DoubleArray(hl + len)
        System.arraycopy(histRe, 0, eRe, 0, hl)
        System.arraycopy(histIm, 0, eIm, 0, hl)
        val w = 2 * PI * fc / fs
        for (i in 0 until len) {
            val idx = n + i
            val c: Double; val s: Double
            if (mixCos != null) { val k = (idx % mixCos.size).toInt(); c = mixCos[k]; s = mixSin!![k] }
            else { c = cos(w * idx); s = sin(w * idx) }
            val v = x[off + i].toDouble()
            eRe[hl + i] = v * c
            eIm[hl + i] = -v * s
        }
        val start = (((-n) % D + D) % D).toInt()
        val nOut = if (start >= len) 0 else (len - 1 - start) / D + 1
        n += len
        System.arraycopy(eRe, len, histRe, 0, hl)
        System.arraycopy(eIm, len, histIm, 0, hl)
        val f = FloatArray(nOut)
        val amp = FloatArray(nOut)
        val k = fsInt / (2 * PI)
        var i = start
        for (o in 0 until nOut) {
            var yr = 0.0; var yi = 0.0
            val base = hl + i
            for (j in 0 until L) { val hj = h[j]; yr += hj * eRe[base - j]; yi += hj * eIm[base - j] }
            // угол z[n]·conj(z[n-1])
            val re = yr * prevRe + yi * prevIm
            val im = yi * prevRe - yr * prevIm
            f[o] = (atan2(im, re) * k + fc).toFloat()
            amp[o] = sqrt(yr * yr + yi * yi).toFloat()
            prevRe = yr; prevIm = yi
            i += D
        }
        return f to amp
    }
}

/** Растущий буфер трека частоты с абсолютной индексацией (_Buf). */
internal class SstvBuf {
    var a = FloatArray(1 shl 16)
    var n = 0
    var base = 0
    val end get() = base + n

    fun append(x: FloatArray) {
        val need = n + x.size
        if (need > a.size) a = a.copyOf(max(need, 2 * a.size))
        System.arraycopy(x, 0, a, n, x.size)
        n = need
    }

    fun trim(keepFrom: Int) {
        val k = min(max(keepFrom - base, 0), n)
        if (k <= 0) return
        System.arraycopy(a, k, a, 0, n - k)
        n -= k
        base += k
    }

    /** Отрезок [lo, hi) в абсолютных индексах; вне буфера — [fill]. */
    fun view(lo: Int, hi: Int, fill: Double = F_BLACK): DoubleArray {
        val out = DoubleArray(max(hi - lo, 0))
        val s = max(lo, base); val e = min(hi, end)
        for (i in out.indices) {
            val g = lo + i
            out[i] = if (g in s until e) a[g - base].toDouble() else fill.toFloat().toDouble()
        }
        return out
    }

    fun mean(lo0: Double, hi0: Double): Double {
        val lo = Math.rint(lo0).toInt()
        var hi = Math.rint(hi0).toInt()
        if (hi <= lo) hi = lo + 1
        var sum = 0.0
        val s = max(lo, base); val e = min(hi, end)
        for (g in lo until hi) sum += if (g in s until e) a[g - base].toDouble() else F_BLACK
        return sum / (hi - lo)
    }
}

/**
 * Потоковый декодер SSTV. [process] принимает блоки звука и возвращает [SstvEvent]; [level] — для индикатора.
 * [forceMode] — имя режима: автостарт по синхроимпульсам без VIS.
 */
class SstvDecoder(fs: Double = 48000.0) {
    val fs = fs
    var forceMode: String? = null
    private val dm = SstvDemod(fs)
    val fsInt = dm.fsInt

    private var buf = SstvBuf()
    var state = "idle"; private set
    private var m: SstvMode? = null
    private var visScan = 0
    private var autoNext = 0
    private var autoFloor = 0
    private class Pending(val mode: String, var pLo: Int, var tries: Int)
    private var pending: Pending? = null
    private var pendingEvents = ArrayList<SstvEvent>()
    private var lastQ = 0.0
    private var off = 0.0
    private var lastF = FloatArray(0)
    private var lastAmp = 0.0

    // состояние картинки
    private var manual = false
    private var bNom = 0.0
    private var a0 = 0.0
    private var a = 0.0
    private var b = 0.0
    private var Ls = 0
    private var Lp = 0
    private var w0 = 0.0
    private var w1 = 0.0
    private var chanEnd = 0.0
    private var missLim = 0
    private var pos = DoubleArray(0)
    private var q = DoubleArray(0)
    private var good = BooleanArray(0)
    private var line = 0
    private var miss = 0
    private var ngood = 0
    private var lastGood = -1
    /** Плоскости картинки: rgb → R, G, B; pd → Y (H×W), U, V (H/2×W); r72 → Y, U, V; r36 → Y, C + ct. */
    private var P = HashMap<String, FloatArray>()
    private var ct = IntArray(0)

    companion object {
        /** Порог качества синхроимпульса (0..1). */
        const val SYNC_Q = 0.45
        /** Сколько истории держать в простое, с. */
        const val IDLE_KEEP_S = 4.5

        /**
         * Декодирует весь звук целиком (как decode_file без чтения WAV): список картинок
         * (законченные и частичные) — события Done/Lost.
         */
        fun decodeAll(x: FloatArray, fs: Double, blockS: Double = 0.05, forceMode: String? = null): List<SstvEvent> {
            val dec = SstvDecoder(fs)
            dec.forceMode = forceMode
            val out = ArrayList<SstvEvent>()
            val n = max(1, (fs * blockS).toInt())
            var i = 0
            while (i < x.size) {
                for (ev in dec.process(x, i, min(n, x.size - i))) if (ev is SstvEvent.Done || ev is SstvEvent.Lost) out.add(ev)
                i += n
            }
            for (ev in dec.process(FloatArray((fs * 0.5).toInt())) + dec.flush())
                if (ev is SstvEvent.Done || ev is SstvEvent.Lost) out.add(ev)
            return out
        }
    }

    init { reset() }

    // ------------------------------------------------------------ публичное

    /** Сброс в исходное состояние (буфер, фильтры, текущая картинка). */
    fun reset() {
        dm.reset()
        buf = SstvBuf()
        state = "idle"
        m = null
        visScan = 0
        autoNext = 0
        autoFloor = 0
        pending = null
        lastQ = 0.0
        off = 0.0
        lastF = FloatArray(0)
        lastAmp = 0.0
    }

    /**
     * Ручной старт: ищет строчную синхронизацию режима в ближайших строках (с захватом последней прошедшей
     * строки) и начинает картинку без VIS.
     */
    fun startManual(mode: String) {
        val mm = Sstv.MODES[mode] ?: throw NoSuchElementException(mode)
        val ev = ArrayList<SstvEvent>()
        if (state == "image") ev += finish(lost = true)
        val T = mm.line * fsInt
        state = "pending"
        pending = Pending(mode, max(buf.base, buf.end - T.toInt()), 0)
        pendingEvents = ev
    }

    /** Принимает блок звука, возвращает список событий. */
    fun process(audio: FloatArray, offset: Int = 0, length: Int = audio.size - offset): List<SstvEvent> {
        val (f, amp) = dm.process(audio, offset, length)
        if (amp.isNotEmpty()) {
            var s = 0.0
            for (v in amp) s += v.toDouble() * v
            lastAmp = sqrt(s / amp.size)
        }
        if (f.isNotEmpty()) {
            val k = (0.03 * fsInt).toInt()
            lastF = f.copyOfRange(max(0, f.size - k), f.size)
        }
        buf.append(f)
        val ev = ArrayList<SstvEvent>()
        if (pendingEvents.isNotEmpty()) { ev += pendingEvents; pendingEvents = ArrayList() }
        while (true) {
            val vis = scanVis() ?: break
            if (state == "image") ev += finish(lost = true)
            pending = null
            ev += SstvEvent.Vis(vis.name, vis.code, round1(vis.off))
            val tImg = vis.e + 0.300 * fsInt
            ev += begin(vis.name, tImg + Sstv.MODES.getValue(vis.name).firstSync * fsInt, vis.off, false)
            ev += runImage()
        }
        if (state == "pending") ev += tryPending()
        if (state == "idle" && forceMode?.let { it in Sstv.MODES } == true) ev += tryAutostart()
        if (state == "image") ev += runImage()
        if (state == "idle") {
            val keep = buf.end - (IDLE_KEEP_S * fsInt).toInt()
            if (keep - buf.base > fsInt.toInt()) buf.trim(keep)
        }
        return ev
    }

    /** Конец потока: незаконченная картинка отдаётся как Done (complete = false). */
    fun flush(): List<SstvEvent> =
        if (state == "image" && line > 0) finish(lost = false, forceDone = true) else emptyList()

    /** Для индикатора: частота тона, уверенность синхронизации и состояние. */
    fun level(): SstvLevel {
        val freq = if (lastF.isNotEmpty()) medianOf(DoubleArray(lastF.size) { lastF[it].toDouble() }) else 0.0
        var lv = SstvLevel(freq, lastAmp, lastQ.coerceIn(0.0, 1.0), state, null, 0, 0, off, 0.0)
        val mm = m
        if (state == "image" && mm != null) {
            lv = lv.copy(mode = mm.name, line = line * mm.rowsPerLine, lines = mm.height, slantPpm = (b / bNom - 1.0) * 1e6)
        } else if (state == "pending") lv = lv.copy(mode = pending?.mode)
        return lv
    }

    // ------------------------------------------------------------ VIS

    private class VisHit(val name: String, val e: Double, val off: Double, val code: Int)

    private fun scanVis(): VisHit? {
        val B = buf; val fsI = fsInt
        val L1 = Math.rint(0.200 * fsI).toInt()
        val L2 = Math.rint(0.010 * fsI).toInt()
        val s0 = Math.rint(0.004 * fsI).toInt()
        val s1 = Math.rint(0.026 * fsI).toInt()
        val h = Math.rint(0.005 * fsI).toInt()
        val tail = Math.rint(0.340 * fsI).toInt()
        val kmin = max(visScan, B.base + L1 + h + 2)
        val kmax = B.end - tail
        if (kmax < kmin) return null
        val step = max(1, Math.rint(0.001 * fsI).toInt())
        val lo = kmin - L1 - h - 1
        val seg = B.view(lo, kmax + s1 + 2)
        val c = DoubleArray(seg.size + 1)
        for (i in seg.indices) c[i + 1] = c[i] + seg[i]
        // точки проверки ровности лидера: arange(-L1 + h, -L2 - h + 1, 2h)
        val pts = ArrayList<Int>()
        run { var p = -L1 + h; while (p < -L2 - h + 1) { pts += p; p += 2 * h } }
        var skipTo = -1
        var k = kmin
        while (k <= kmax) {
            val kr = k - lo
            val mLead = (c[kr - L2] - c[kr - L1]) / (L1 - L2)
            val mSt = (c[kr + s1] - c[kr + s0]) / (s1 - s0)
            val dlt = mSt - mLead
            if (abs(mLead - F_LEADER) <= 160 && dlt < -560 && dlt > -840) {
                var bad = 0
                for (p in pts) {
                    val P = kr + p
                    val bx = (c[P + h] - c[P - h]) / (2 * h)
                    if (abs(bx - mLead) > 70.0) bad++
                }
                if (bad <= 2 && k >= skipTo) {
                    val res = tryVis(k, mLead)
                    if (res != null) {
                        visScan = (res.e + 0.300 * fsI).toInt()
                        return res
                    }
                    skipTo = k + (0.010 * fsI).toInt()
                }
            }
            k += step
        }
        visScan = kmax + step
        return null
    }

    private fun tryVis(k: Int, mlead: Double): VisHit? {
        val B = buf; val fsI = fsInt
        val off = mlead - F_LEADER
        val r0 = k - (0.006 * fsI).toInt()
        val seg = B.view(r0, k + (0.014 * fsI).toInt())
        var e = r0.toDouble()
        var acc = 0.0
        for (v in seg) acc += ((v - (mlead - 700.0)) / 700.0).coerceIn(0.0, 1.0)
        e += acc
        val bit = 0.030 * fsI
        val g = 0.004 * fsI
        val st = B.mean(e + g, e + bit - g) - off
        if (abs(st - F_SYNC) > 90) return null
        val v = DoubleArray(9) { B.mean(e + (it + 1) * bit + g, e + (it + 2) * bit - g) - off }
        val par = v[7]; val stop = v[8]
        for (i in 0..7) if (!(v[i] > 1010.0 && v[i] < 1390.0)) return null
        if (stop > 1280.0) return null
        var code = 0; var ones = 0
        for (i in 0 until 7) if (v[i] < F_SYNC) { code = code or (1 shl i); ones++ }
        val p = if (par < F_SYNC) 1 else 0
        if ((ones + p) % 2 != 0) return null
        val name = Sstv.VIS2MODE[code] ?: return null
        return VisHit(name, e, off, code)
    }

    // ------------------------------------------------------------ синхро

    /** Оценки «синхроимпульс начинается в lo+p», p = 0..n-1 (нормированы 0..1). */
    private fun syncScores(lo: Int, n: Int, Ls: Int, Lp: Int): DoubleArray {
        val seg = buf.view(lo, lo + n + Ls + Lp)
        val c = DoubleArray(seg.size + 1)
        val thr = F_SYNC + 150.0 + off
        for (i in seg.indices) c[i + 1] = c[i] + ((thr - seg[i]) / 150.0).coerceIn(-1.0, 1.0)
        return DoubleArray(n) { p -> ((c[p + Ls] - c[p]) - (c[p + Ls + Lp] - c[p + Ls])) / (Ls + Lp) }
    }

    private fun argmax(s: DoubleArray): Int {
        var k = 0
        for (i in 1 until s.size) if (s[i] > s[k]) k = i
        return k
    }

    private fun syncSearch(center: Double, w: Double): Pair<Double, Double> {
        val mm = m!!
        val lo = floor(center - w).toInt()
        val n = (2 * w).toInt() + 2
        val s = syncScores(lo, n, Ls, Lp)
        val k = argmax(s)
        val qq = s[k]
        val p = lo + k
        // уточнение по заднему фронту (синхро 1200 → гашение 1500 есть во всех режимах;
        // передний фронт зависит от содержимого предыдущего канала)
        val Lw = max(2, Math.rint(min(0.0015, 0.3 * mm.sync) * fsInt).toInt())
        val r0 = p + Ls - Lw
        val seg = buf.view(r0, r0 + 2 * Lw)
        var edge = r0.toDouble()
        for (v in seg) edge += 1.0 - ((v - (F_SYNC + off)) / (F_BLACK - F_SYNC)).coerceIn(0.0, 1.0)
        return (edge - mm.sync * fsInt * (b / bNom)) to qq
    }

    private fun findAnchor(T: Double, pLo: Int, nsync: Int, Ls: Int, Lp: Int): Pair<Int, DoubleArray> {
        val n = T.toInt()
        val total = n + ceil((nsync - 1) * T).toInt() + 2
        val s = syncScores(pLo, total, Ls, Lp)
        val comb = DoubleArray(n)
        for (j in 0 until nsync) {
            val o = Math.rint(j * T).toInt()
            for (i in 0 until n) comb[i] += s[o + i]
        }
        val p = argmax(comb)
        val qs = DoubleArray(nsync) { s[p + Math.rint(it * T).toInt()] }
        return (pLo + p) to qs
    }

    private fun tryPending(): List<SstvEvent> {
        val pd = pending!!
        val mm = Sstv.MODES.getValue(pd.mode)
        val T = mm.line * fsInt
        val Ls = Math.rint(mm.sync * fsInt).toInt()
        val Lp = Math.rint(0.002 * fsInt).toInt()
        val ev = ArrayList<SstvEvent>()
        while (state == "pending") {
            val need = pd.pLo + 3 * T + Ls + Lp + 4
            if (buf.end < need) break
            val (p, qs) = findAnchor(T, pd.pLo, 3, Ls, Lp)
            if (qs.min() >= 0.3 || pd.tries >= 4) {
                pending = null
                ev += begin(pd.mode, p.toDouble(), off, true)
                ev += runImage()
            } else {
                pd.pLo += T.toInt()
                pd.tries += 1
            }
        }
        return ev
    }

    private fun tryAutostart(): List<SstvEvent> {
        val fm = forceMode!!
        val mm = Sstv.MODES.getValue(fm)
        val T = mm.line * fsInt
        val Ls = Math.rint(mm.sync * fsInt).toInt()
        val Lp = Math.rint(0.002 * fsInt).toInt()
        val B = buf
        if (B.end < autoNext) return emptyList()
        autoNext = B.end + (T / 2).toInt()
        val pLo = (B.end - 3 * T - Ls - Lp - 4).toInt()
        if (pLo < max(B.base, autoFloor)) return emptyList()
        val (p, qs) = findAnchor(T, pLo, 3, Ls, Lp)
        if (qs.min() < 0.5 || qs.sum() / 3 < 0.65) return emptyList()
        return begin(fm, p.toDouble(), off, true) + runImage()
    }

    // ------------------------------------------------------------ картинка

    private fun begin(mode: String, firstSync: Double, off0: Double, manual0: Boolean): List<SstvEvent> {
        val mm = Sstv.MODES.getValue(mode)
        val fsI = fsInt
        m = mm
        state = "image"
        manual = manual0
        off = off0
        val T = mm.line * fsI
        bNom = T
        a0 = firstSync
        a = a0; b = T
        Ls = Math.rint(mm.sync * fsI).toInt()
        Lp = Math.rint(0.002 * fsI).toInt()
        w0 = max(0.012, 0.03 * mm.line) * fsI
        w1 = max(0.004, 0.015 * mm.line) * fsI
        val offs = mm.channels.map { it.off }
        chanEnd = mm.channels.maxOf { it.off + it.dur } * fsI
        missLim = max(6, ceil(3.0 / mm.line).toInt())
        val n = mm.lines
        pos = DoubleArray(n)
        q = DoubleArray(n)
        good = BooleanArray(n)
        line = 0
        miss = 0
        ngood = 0
        lastGood = -1
        val W = mm.width; val H = mm.height
        P = HashMap()
        when (mm.color) {
            "rgb" -> for (k in listOf("R", "G", "B")) P[k] = FloatArray(H * W)
            "pd" -> {
                P["Y"] = FloatArray(H * W) { 16f }
                P["U"] = FloatArray(H / 2 * W) { 128f }
                P["V"] = FloatArray(H / 2 * W) { 128f }
            }
            "r72" -> for (k in listOf("Y", "U", "V")) P[k] = FloatArray(H * W) { if (k == "Y") 16f else 128f }
            else -> {
                P["Y"] = FloatArray(H * W) { 16f }
                P["C"] = FloatArray(H * W) { 128f }
                ct = IntArray(H) { -1 }
            }
        }
        val keep = a0 + min(0.0, offs.min()) * fsI - 0.05 * fsI - w0
        buf.trim(keep.toInt())
        return listOf(SstvEvent.Start(mode, W, H, manual0))
    }

    /** Взвешенная прямая как np.polyfit(x, y, 1, w=wt): минимизирует Σ(wt·остаток)². Возвращает (наклон, сдвиг). */
    private fun polyfit1(x: DoubleArray, y: DoubleArray, wt: DoubleArray): Pair<Double, Double> {
        var sw = 0.0; var sx = 0.0; var sy = 0.0
        for (i in x.indices) { val w2 = wt[i] * wt[i]; sw += w2; sx += w2 * x[i]; sy += w2 * y[i] }
        val mx = sx / sw; val my = sy / sw
        var sxx = 0.0; var sxy = 0.0
        for (i in x.indices) { val w2 = wt[i] * wt[i]; val dx = x[i] - mx; sxx += w2 * dx * dx; sxy += w2 * dx * (y[i] - my) }
        val slope = sxy / sxx
        return slope to (my - slope * mx)
    }

    private fun fit(upto: Int): Pair<Double, Double> {
        val idxL = ArrayList<Int>()
        for (i in 0 until min(upto, good.size)) if (good[i]) idxL += i
        val bn = bNom
        if (idxL.isEmpty()) return a0 to bn
        val idx = DoubleArray(idxL.size) { idxL[it].toDouble() }
        val ps = DoubleArray(idxL.size) { pos[idxL[it]] }
        val nn = idx.size
        val medA = { medianOf(DoubleArray(nn) { ps[it] - idx[it] * bn }) }
        if (nn == 1 || idx[nn - 1] - idx[0] < 2) return medA() to bn
        val wt = DoubleArray(nn) { q[idxL[it]].coerceIn(0.05, 1.0) }
        val y0 = ps[0]
        val tol = 0.0005 * fsInt
        var mask = BooleanArray(nn) { true }
        var bb = bn
        var aa = medA()
        for (iter in 0 until 5) {
            val sel = (0 until nn).filter { mask[it] }
            if (sel.size < 2 || sel.maxOf { idx[it] } - sel.minOf { idx[it] } < 2) break
            val (b1, a1) = polyfit1(DoubleArray(sel.size) { idx[sel[it]] },
                DoubleArray(sel.size) { ps[sel[it]] - y0 }, DoubleArray(sel.size) { wt[sel[it]] })
            bb = b1
            aa = a1 + y0
            val res = DoubleArray(nn) { ps[it] - (aa + bb * idx[it]) }
            val mad = medianOf(DoubleArray(sel.size) { abs(res[sel[it]]) })
            val lim = max(4.0 * 1.4826 * mad, tol)
            val nw = BooleanArray(nn) { abs(res[it]) <= lim }
            if (nw.count { it } < 2 || nw.contentEquals(mask)) break
            mask = nw
        }
        if (abs(bb / bn - 1.0) > 0.02) {
            bb = bn
            aa = medA()
        }
        return aa to bb
    }

    /** Средние трека частоты по интервалам [bounds[j], bounds[j+1]). */
    private fun box(bounds: DoubleArray): DoubleArray {
        val lo = floor(bounds[0]).toInt() - 1
        val hi = ceil(bounds[bounds.size - 1]).toInt() + 2
        val seg = buf.view(lo, hi, F_BLACK + off)
        val c = DoubleArray(seg.size + 1)
        for (i in seg.indices) c[i + 1] = c[i] + seg[i]
        val C = DoubleArray(bounds.size) { j ->
            val x = bounds[j] - lo
            val i = floor(x).toLong().coerceIn(0L, (c.size - 2).toLong()).toInt()
            val fr = x - i
            c[i] + (c[i + 1] - c[i]) * fr
        }
        return DoubleArray(bounds.size - 1) { j -> (C[j + 1] - C[j]) / max(bounds[j + 1] - bounds[j], 1e-9) }
    }

    private class LineVals(val ch: HashMap<String, DoubleArray>, var ct: Int = -1)

    private fun decodeLine(S: Double, r: Double): LineVals {
        val mm = m!!
        val fsI = fsInt
        val W = mm.width
        val out = LineVals(HashMap())
        val k255 = 255.0 / F_SPAN
        for (c in mm.channels) {
            val st = S + c.off * fsI * r
            val dd = c.dur * fsI * r / W
            val bounds = DoubleArray(W + 1) { st + it * dd }
            val f = box(bounds)
            out.ch[c.kind] = DoubleArray(W) { ((f[it] - off - F_BLACK) * k255).coerceIn(0.0, 255.0) }
        }
        val sep = mm.sep
        if (sep != null) {
            val st = S + sep[0] * fsI * r
            val fm = buf.mean(st + 0.15 * sep[1] * fsI, st + 0.85 * sep[1] * fsI) - off
            out.ct = if (fm > F_LEADER) 1 else 0
        }
        return out
    }

    private fun putRow(dst: FloatArray, row: Int, W: Int, v: DoubleArray) {
        val o = row * W
        for (x in 0 until W) dst[o + x] = v[x].toFloat()
    }

    /** Кладёт строку в плоскости, возвращает готовые строки изображения. */
    private fun store(i: Int, v: LineVals): List<Int> {
        val mm = m!!
        val W = mm.width
        return when (mm.color) {
            "rgb" -> {
                for (k in listOf("R", "G", "B")) putRow(P.getValue(k), i, W, v.ch.getValue(k))
                listOf(i)
            }
            "pd" -> {
                putRow(P.getValue("Y"), 2 * i, W, v.ch.getValue("Y0"))
                putRow(P.getValue("Y"), 2 * i + 1, W, v.ch.getValue("Y1"))
                putRow(P.getValue("U"), i, W, v.ch.getValue("U"))
                putRow(P.getValue("V"), i, W, v.ch.getValue("V"))
                listOf(2 * i, 2 * i + 1)
            }
            "r72" -> {
                for (k in listOf("Y", "U", "V")) putRow(P.getValue(k), i, W, v.ch.getValue(k))
                listOf(i)
            }
            else -> {
                putRow(P.getValue("Y"), i, W, v.ch.getValue("Y"))
                putRow(P.getValue("C"), i, W, v.ch.getValue("C"))
                ct[i] = v.ct
                if (i % 2 == 1) listOf(i - 1, i) else emptyList()
            }
        }
    }

    /** Строка [y] картинки в ARGB. */
    private fun rowRgb(y: Int): IntArray {
        val mm = m!!
        val W = mm.width
        val out = IntArray(W)
        when (mm.color) {
            "rgb" -> {
                val r = P.getValue("R"); val g = P.getValue("G"); val bl = P.getValue("B")
                for (x in 0 until W) { val o = y * W + x; out[x] = Sstv.argb(Sstv.u8(r[o]), Sstv.u8(g[o]), Sstv.u8(bl[o])) }
            }
            "pd" -> {
                val Y = P.getValue("Y"); val U = P.getValue("U"); val V = P.getValue("V")
                val c = y / 2
                for (x in 0 until W) out[x] = Sstv.yccToRgb(Y[y * W + x], U[c * W + x], V[c * W + x])
            }
            "r72" -> {
                val Y = P.getValue("Y"); val U = P.getValue("U"); val V = P.getValue("V")
                for (x in 0 until W) { val o = y * W + x; out[x] = Sstv.yccToRgb(Y[o], U[o], V[o]) }
            }
            else -> {
                val Y = P.getValue("Y"); val C = P.getValue("C")
                val k = y - y % 2
                val pair = intArrayOf(k, min(k + 1, ct.size - 1))
                var c0 = ct[pair[0]]; var c1 = ct[pair[1]]
                if (c0 == c1 || c0 == -1 || c1 == -1) { c0 = 0; c1 = 1 }  // по чётности: чётная R-Y, нечётная B-Y
                val vRow = if (c0 == 0) pair[0] else pair[1]
                val uRow = if (c0 == 1) pair[0] else pair[1]
                for (x in 0 until W) out[x] = Sstv.yccToRgb(Y[y * W + x], C[uRow * W + x], C[vRow * W + x])
            }
        }
        return out
    }

    private fun image(rows: Int): IntArray {
        val mm = m!!
        val W = mm.width; val H = mm.height
        val img = IntArray(W * H)
        for (y in 0 until H) {
            if (y >= rows) { java.util.Arrays.fill(img, y * W, img.size, Sstv.argb(0, 0, 0)); break }
            System.arraycopy(rowRgb(y), 0, img, y * W, W)
        }
        return img
    }

    private fun runImage(): List<SstvEvent> {
        val ev = ArrayList<SstvEvent>()
        val mm = m!!
        val B = buf
        while (state == "image") {
            val i = line
            val pred = a + b * i
            val w = if (ngood == 0) w0 else w1
            val need = pred + max(w + Ls + Lp, chanEnd * 1.03) + 8
            if (B.end < need) break
            val (p, qq) = syncSearch(pred, w)
            lastQ = qq
            if (qq >= SYNC_Q) {
                pos[i] = p; q[i] = qq; good[i] = true
                ngood += 1
                miss = 0
                lastGood = i
                val fm = B.mean(p + 0.25 * Ls, p + 0.75 * Ls)
                val meas = (fm - F_SYNC).coerceIn(-250.0, 250.0)
                off += 0.15 * (meas - off)
            } else miss += 1
            val (na, nb) = fit(i + 1)
            a = na; b = nb
            val S = a + b * i
            val vals = decodeLine(S, b / bNom)
            for (y in store(i, vals)) ev += SstvEvent.Line(mm.name, y, mm.width, mm.height, rowRgb(y))
            line = i + 1
            if (line >= mm.lines) ev += finish(lost = false)
            else if (miss >= missLim) ev += finish(lost = true)
        }
        return ev
    }

    /** Завершение картинки: перерасчёт всех строк по общей подгонке синхро. */
    private fun finish(lost: Boolean, forceDone: Boolean = false): List<SstvEvent> {
        val mm = m!!
        var nDec = line
        if (lost) nDec = lastGood + 1
        val ev = ArrayList<SstvEvent>()
        if (nDec > 0) {
            val (na, nb) = fit(nDec)
            a = na; b = nb
            val r = b / bNom
            for (i in 0 until nDec) store(i, decodeLine(a + b * i, r))
        }
        val rows = nDec * mm.rowsPerLine
        val complete = nDec >= mm.lines
        val img = image(rows)
        val slant = round1((b / bNom - 1.0) * 1e6)
        val offR = round1(off)
        if (complete || forceDone || (lost && nDec >= 0.8 * mm.lines))
            ev += SstvEvent.Done(mm.name, img, mm.width, mm.height, complete, rows, slant, offR)
        else ev += SstvEvent.Lost(mm.name, img, mm.width, mm.height, rows, slant, offR)
        autoFloor = (a + b * max(nDec, line)).toInt()
        state = "idle"
        lastQ = 0.0
        return ev
    }
}
