package io.github.mikeileech.sdr4noob.dsp

import java.nio.charset.Charset
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * RDS (IEC 62106) — перенос rtltv/rds.py: поднесущая 57 кГц (3 × пилот), BPSK 1187,5 бит/с, двухфазный код
 * с дифференциальным предкодированием; группа = 4 блока по 26 бит. Декодируются PI, PS, RT, PTY, TP/TA, часы.
 */
object RdsCode {
    const val BITRATE = 1187.5
    const val POLY = 0x5B9
    const val A = 0x0FC; const val B = 0x198; const val C = 0x168; const val CP = 0x350; const val D = 0x1B4
    private val off2idx = mapOf(A to 0, B to 1, C to 2, CP to 2, D to 3)
    fun offIdx(off: Int): Int? = off2idx[off]
    fun offOk(idx: Int, off: Int) = when (idx) { 0 -> off == A; 1 -> off == B; 2 -> off == C || off == CP; else -> off == D }

    val PTY_NAMES = arrayOf("—", "Новости", "Текущие события", "Информация", "Спорт", "Образование", "Драма", "Культура",
        "Наука", "Разное", "Поп-музыка", "Рок-музыка", "Лёгкая музыка", "Лёгкая классика",
        "Классика", "Другая музыка", "Погода", "Финансы", "Детям", "Общество", "Религия", "Звонки в эфир",
        "Путешествия", "Досуг", "Джаз", "Кантри", "Национальная музыка", "Ретро", "Фолк",
        "Документальное", "Тест тревоги", "Тревога")

    /** Проверочное слово (без смещения) для всех 65536 значений данных блока. */
    val CRC: IntArray by lazy {
        IntArray(65536) { d ->
            var reg = d.toLong() shl 10
            for (i in 25 downTo 10) if ((reg shr i) and 1L == 1L) reg = reg xor (POLY.toLong() shl (i - 10))
            (reg and 0x3FF).toInt()
        }
    }

    fun checkword(data: Int, off: Int) = CRC[data and 0xFFFF] xor off

    private val cp1251: Charset = Charset.forName("windows-1251")

    /** Символ RDS: ASCII как есть; от 0xC0 — по cp1251 (так шлют многие станции в России). */
    fun char(b: Int): Char = when {
        b in 0x20 until 0x7F -> b.toChar()
        b >= 0xC0 -> String(byteArrayOf(b.toByte()), cp1251).firstOrNull() ?: '?'
        b == 0 || b == 0x0D -> ' '
        else -> '?'
    }

    fun bytes(s: String, n: Int): IntArray {
        val b = s.toByteArray(cp1251)
        return IntArray(n) { if (it < b.size) b[it].toInt() and 0xFF else 0x20 }
    }
}

/** Состояние RDS для интерфейса. */
data class RdsState(val synced: Boolean, val pi: Int?, val ps: String, val rt: String, val pty: Int?,
                    val ptyName: String, val tp: Boolean, val ta: Boolean, val ct: IntArray?)

class RdsDecoder(val fs: Int = 240_000) {
    private val dfac = (fs / 24_000.0).roundToInt()
    private val rate = fs.toDouble() / dfac
    private val T = rate / RdsCode.BITRATE                 // отсчётов на бит
    private val dec = ComplexDecimator(dfac, 2600.0, fs.toDouble(), 32)
    private val sqRe = Biquad.lowpass(12.0, rate)
    private val sqIm = Biquad.lowpass(12.0, rate)
    private val half = (T / 2).roundToInt()
    private val mf = FloatArray(2 * half) { if (it < half) 1f / (2 * half) else -1f / (2 * half) }
    private var mfHist = FloatArray(2 * half - 1)
    private var ncoPh = 0.0
    private val ncoW = 2 * PI * 57_000.0 / fs

    private var bufR = FloatArray(0); private var bufI = FloatArray(0)
    private var bbR = FloatArray(0); private var bbI = FloatArray(0)

    // состояние (reset)
    private var lastAng = 0.0
    private var ybuf = DoubleArray(0)
    private var absN = 0L
    private var nextAbs = 0.0
    private var clkPh: Double? = null
    private var clkRate = 0.0
    private var clkMag = 0.0
    private var prevRaw = 0
    private var reg = 0
    private var nbits = 0L
    var synced = false; private set
    private var candN = -1L; private var candIdx = 0
    private var left = 0
    private var expect = 0
    private val errs = ArrayDeque<Boolean>()
    private val grpData = IntArray(4); private val grpOff = IntArray(4); private val grpHas = BooleanArray(4)
    var pi: Int? = null; private set
    var pty: Int? = null; private set
    var tp = false; private set
    var ta = false; private set
    private val psBuf = CharArray(8) { ' ' }
    private val psSeen = BooleanArray(4)
    var ps = ""; private set
    private val rtBuf = CharArray(64) { ' ' }
    private val rtSeen = BooleanArray(16)
    private var rtAb = -1
    var rt = ""; private set
    var ct: IntArray? = null; private set
    var groupsOk = 0; private set
    @Volatile var changed = true

    fun reset() {
        lastAng = 0.0; ybuf = DoubleArray(0); absN = 0; nextAbs = 0.0; clkPh = null; clkRate = 0.0; clkMag = 0.0
        prevRaw = 0; reg = 0; nbits = 0; synced = false; candN = -1; left = 0; expect = 0; errs.clear()
        grpHas.fill(false); pi = null; pty = null; tp = false; ta = false
        psBuf.fill(' '); psSeen.fill(false); ps = ""; rtBuf.fill(' '); rtSeen.fill(false); rtAb = -1; rt = ""
        ct = null; groupsOk = 0; changed = true
    }

    fun state(): RdsState {
        val txt = String(rtBuf)
        val end = txt.indexOf('\r')
        return RdsState(synced, pi, ps.ifEmpty { String(psBuf).trim() },
            rt.ifEmpty { (if (end >= 0) txt.substring(0, end) else txt).trim() },
            pty, pty?.let { RdsCode.PTY_NAMES[it] } ?: "", tp, ta, ct)
    }

    /**
     * [mpx] — MPX на частоте fs (1.0 = 75 кГц), [n] отсчётов; [c57Re]/[c57Im] — несущая 57 кГц от фазы пилота
     * (exp(j·3·фаза пилота)) или null — тогда свободный генератор.
     */
    fun process(mpx: FloatArray, n: Int, c57Re: FloatArray?, c57Im: FloatArray?) {
        if (n == 0) return
        if (bufR.size < n) { bufR = FloatArray(n); bufI = FloatArray(n); bbR = FloatArray(n / dfac + 2); bbI = FloatArray(n / dfac + 2) }
        if (c57Re == null || c57Im == null) {
            val cw = cos(ncoW); val sw = sin(ncoW)
            var c = cos(ncoPh); var s = sin(ncoPh)
            for (i in 0 until n) {
                bufR[i] = (mpx[i] * c).toFloat(); bufI[i] = (-mpx[i] * s).toFloat()
                val nc = c * cw - s * sw; s = c * sw + s * cw; c = nc
            }
        } else {
            for (i in 0 until n) { bufR[i] = mpx[i] * c57Re[i]; bufI[i] = -mpx[i] * c57Im[i] }
        }
        ncoPh = (ncoPh + ncoW * n) % (2 * PI)
        val m = dec.process(bufR, bufI, n, bbR, bbI)
        if (m == 0) return
        // фаза несущей: угол квадрата сигнала (BPSK) после узкого ФНЧ, без разрывов между блоками
        val y = DoubleArray(m)
        var ang = lastAng
        for (i in 0 until m) {
            val a = bbR[i].toDouble(); val b = bbI[i].toDouble()
            val zr = sqRe.step(a * a - b * b); val zi = sqIm.step(2 * a * b)
            val raw = atan2(zi, zr)
            var d = raw - ang
            d -= 2 * PI * floor((d + PI) / (2 * PI))
            ang += d
            val ca = cos(-0.5 * ang); val sa = sin(-0.5 * ang)
            y[i] = a * ca - b * sa
        }
        lastAng = ang
        // согласованный фильтр двухфазного символа
        val hl = mfHist.size
        val ext = DoubleArray(hl + m)
        for (i in 0 until hl) ext[i] = mfHist[i].toDouble()
        for (i in 0 until m) ext[hl + i] = y[i]
        val yf = DoubleArray(m)
        for (i in 0 until m) {
            var s = 0.0
            // свёртка: y[n] = Σ h[k]·x[n−k]
            for (k in mf.indices) s += mf[k] * ext[hl + i - k]
            yf[i] = s
        }
        for (i in 0 until hl) mfHist[i] = ext[m + i].toFloat()
        ybuf = ybuf + yf
        timing(yf)
    }

    /** Тактовая синхронизация по спектральной линии y² на частоте битов (как _timing в rds.py). */
    private fun timing(ynew: DoubleArray) {
        var cr = 0.0; var ci = 0.0
        for (i in ynew.indices) {
            val nn = absN + i
            val ph = -2 * PI * (nn.toDouble() % T) / T
            val v = ynew[i] * ynew[i]
            cr += v * cos(ph); ci += v * sin(ph)
        }
        absN += ynew.size
        val mag = sqrt(cr * cr + ci * ci)
        clkMag = if (clkMag == 0.0) mag else 0.9 * clkMag + 0.1 * mag
        if (mag > 0) {
            val a = atan2(ci, cr)
            val prev = clkPh
            if (prev == null) clkPh = a else {
                val pred = prev + clkRate
                var e = (a - pred + PI) % (2 * PI)
                if (e < 0) e += 2 * PI
                e -= PI
                val w = min(1.0, mag / (clkMag + 1e-20))
                clkPh = pred + 0.35 * w * e
                clkRate += 0.04 * w * e
            }
        }
        var n0 = (-(clkPh ?: 0.0) / (2 * PI) * T) % T
        if (n0 < 0) n0 += T
        val yb = ybuf
        val base = absN - yb.size
        val out = ArrayList<Double>()
        while (true) {
            val want = nextAbs
            val k = Math.round((want - n0) / T).toDouble()
            var p = n0 + k * T
            if (abs(p - want) > T / 2) p = want
            var i = p - base
            if (i < 0) { i = 0.0; p = base.toDouble() }
            if (i + 1 >= yb.size) break
            val i0 = i.toInt()
            val fr = i - i0
            out.add(yb[i0] * (1 - fr) + yb[i0 + 1] * fr)
            nextAbs = p + T
        }
        var keep = maxOf(4, (yb.size - maxOf(0.0, nextAbs - base - 4)).toInt())
        keep = min(keep, yb.size)
        ybuf = yb.copyOfRange(yb.size - keep, yb.size)
        for (v in out) {
            val b = if (v > 0) 1 else 0
            val bit = b xor prevRaw    // дифференциальное декодирование
            prevRaw = b
            bit(bit)
        }
    }

    private fun bit(bit: Int) {
        reg = ((reg shl 1) or bit) and 0x3FFFFFF
        nbits++
        val crc = RdsCode.CRC
        if (!synced) {
            val off = crc[reg ushr 10] xor (reg and 0x3FF)
            val idx = RdsCode.offIdx(off) ?: return
            if (candN >= 0) {
                val dist = nbits - candN
                if (dist % 26 == 0L && dist in 1..(26 * 4) && (candIdx + (dist / 26).toInt()) % 4 == idx) {
                    synced = true
                    errs.clear()
                    grpHas.fill(false)
                    store(idx, reg ushr 10, off)
                    expect = (idx + 1) % 4
                    left = 26
                    return
                }
            }
            candN = nbits; candIdx = idx
            return
        }
        left--
        if (left > 0) return
        left = 26
        val idx = expect
        expect = (idx + 1) % 4
        var w = reg
        var okOff = -1
        val off = crc[w ushr 10] xor (w and 0x3FF)
        if (RdsCode.offOk(idx, off)) okOff = off else {
            for (b in 0 until 26) {   // исправление одиночной ошибки
                val w2 = w xor (1 shl b)
                val off2 = crc[w2 ushr 10] xor (w2 and 0x3FF)
                if (RdsCode.offOk(idx, off2)) { w = w2; okOff = off2; break }
            }
        }
        errs.addLast(okOff < 0)
        if (errs.size > 50) errs.removeFirst()
        if (okOff < 0) {
            grpHas[idx] = false
            if (errs.count { it } > 20) { synced = false; candN = -1 }
        } else store(idx, w ushr 10, okOff)
        if (idx == 3) { group(); grpHas.fill(false) }
    }

    private fun store(idx: Int, data: Int, off: Int) { grpData[idx] = data; grpOff[idx] = off; grpHas[idx] = true }

    private fun group() {
        if (grpHas[0]) setPi(grpData[0]) else if (grpHas[2] && grpOff[2] == RdsCode.CP) setPi(grpData[2])
        if (!grpHas[1]) return
        val bv = grpData[1]
        val gtype = bv ushr 12; val ver = (bv ushr 11) and 1
        set(((bv ushr 10) and 1) == 1, tp) { tp = it }
        val p = (bv ushr 5) and 31
        if (pty != p) { pty = p; changed = true }
        groupsOk++
        if (gtype == 0 && grpHas[3]) {
            set(((bv ushr 4) and 1) == 1, ta) { ta = it }
            val addr = bv and 3
            val dv = grpData[3]
            psBuf[2 * addr] = RdsCode.char(dv ushr 8); psBuf[2 * addr + 1] = RdsCode.char(dv and 0xFF)
            psSeen[addr] = true
            if (psSeen.all { it }) {
                val s = String(psBuf)
                if (s != ps) { ps = s; changed = true }
                psSeen.fill(false)
            }
        } else if (gtype == 2) {
            val ab = (bv ushr 4) and 1
            if (rtAb >= 0 && ab != rtAb) { rtBuf.fill(' '); rtSeen.fill(false) }
            rtAb = ab
            val addr = bv and 15
            val chars: IntArray
            val pos: Int
            if (ver == 0) {
                if (!grpHas[2] || !grpHas[3]) return
                chars = intArrayOf(grpData[2] ushr 8, grpData[2] and 0xFF, grpData[3] ushr 8, grpData[3] and 0xFF)
                pos = addr * 4
            } else {
                if (!grpHas[3]) return
                chars = intArrayOf(grpData[3] ushr 8, grpData[3] and 0xFF)
                pos = addr * 2
            }
            for ((i, ch) in chars.withIndex()) if (pos + i < 64) rtBuf[pos + i] = if (ch == 0x0D) '\r' else RdsCode.char(ch)
            rtSeen[addr] = true
            val seg = if (ver == 0) 4 else 2
            val txt = String(rtBuf, 0, 16 * seg)
            val end = txt.indexOf('\r')
            val nSeg = if (end >= 0) end / seg + 1 else 16
            if ((0 until nSeg).all { rtSeen[it] }) {
                val s = (if (end >= 0) txt.substring(0, end) else txt).trim()
                if (s != rt) { rt = s; changed = true }
            }
        } else if (gtype == 4 && ver == 0 && grpHas[2] && grpHas[3]) {
            val cv = grpData[2]; val dv = grpData[3]
            val mjd = ((bv and 3) shl 15) or (cv ushr 1)
            val hour = ((cv and 1) shl 4) or (dv ushr 12)
            val minute = (dv ushr 6) and 0x3F
            val off = (dv and 0x1F) * 30 * (if ((dv ushr 5) and 1 == 1) -1 else 1)
            if (hour < 24 && minute < 60 && mjd > 50000) {
                val y = ((mjd - 15078.2) / 365.25).toInt()
                val mo = ((mjd - 14956.1 - (y * 365.25).toInt()) / 30.6001).toInt()
                val day = mjd - 14956 - (y * 365.25).toInt() - (mo * 30.6001).toInt()
                val k = if (mo == 14 || mo == 15) 1 else 0
                val t = hour * 60 + minute + off
                val v = intArrayOf(1900 + y + k, mo - 1 - k * 12, day, Math.floorMod(t / 60, 24), Math.floorMod(t, 60))
                if (!v.contentEquals(ct)) { ct = v; changed = true }
            }
        }
    }

    private fun setPi(v: Int) { if (pi != v) { pi = v; changed = true } }
    private inline fun set(v: Boolean, old: Boolean, f: (Boolean) -> Unit) { if (v != old) { f(v); changed = true } }
}

/** Кодер RDS (для тестов и тестового эфира): цикл групп → двухфазные символы ±1 (как RdsEncoder в rds.py). */
class RdsEncoder(groups: List<IntArray>) {
    private val sym: FloatArray

    init {
        val bits = ArrayList<Int>()
        for (g in groups) for (j in 0 until 4) {
            val data = g[2 * j]; val off = g[2 * j + 1]
            val word = (data shl 10) or RdsCode.checkword(data, off)
            for (i in 0 until 26) bits.add((word ushr (25 - i)) and 1)
        }
        fun diff(prev0: Int): IntArray { var p = prev0; return IntArray(bits.size) { p = p xor bits[it]; p } }
        var d = diff(0)
        if (d.last() != 0) d = d + diff(d.last())
        sym = FloatArray(d.size) { d[it] * 2f - 1f }
    }

    /** Значение символа в момент [t] секунд. */
    fun value(t: Double): Float {
        val x = t * RdsCode.BITRATE
        val k = floor(x).toLong()
        val s = sym[Math.floorMod(k, sym.size.toLong()).toInt()]
        return if (x - k >= 0.5) -s else s
    }

    companion object {
        /** Группы 0A (PS) ×4 + 2A (RT) [+ 4A часы]: каждая группа — 4 пары (данные, смещение). */
        fun groups(pi: Int = 0x7201, ps: String = "SDR4NOOB", rt: String = "Тестовый эфир SDR4NOOB", pty: Int = 10,
                   ct: IntArray? = null): List<IntArray> {
            val out = ArrayList<IntArray>()
            val psB = RdsCode.bytes(ps, 8)
            for (addr in 0 until 4) {
                val b = (1 shl 10) or (pty shl 5) or (1 shl 3) or (1 shl 2) or addr
                out.add(intArrayOf(pi, RdsCode.A, b, RdsCode.B, 0xE0CD, RdsCode.C, (psB[2 * addr] shl 8) or psB[2 * addr + 1], RdsCode.D))
            }
            var rtS = rt.take(64)
            if (rtS.length < 64) rtS += "\r"
            val nSeg = (rtS.length + 3) / 4
            val rtB = RdsCode.bytes(rtS, nSeg * 4)
            for (addr in 0 until nSeg) {
                val b = (2 shl 12) or (1 shl 10) or (pty shl 5) or addr
                out.add(intArrayOf(pi, RdsCode.A, b, RdsCode.B, (rtB[4 * addr] shl 8) or rtB[4 * addr + 1], RdsCode.C,
                    (rtB[4 * addr + 2] shl 8) or rtB[4 * addr + 3], RdsCode.D))
            }
            if (ct != null) {
                val (year, month, day, hour, minute) = ct.toList()
                val k = if (month <= 2) 1 else 0
                val mjd = 14956 + day + ((year - 1900 - k) * 365.25).toInt() + ((month + 1 + k * 12) * 30.6001).toInt()
                val b = (4 shl 12) or (1 shl 10) or (pty shl 5) or ((mjd ushr 15) and 3)
                val c = ((mjd and 0x7FFF) shl 1) or (hour ushr 4)
                val d = ((hour and 15) shl 12) or (minute shl 6)
                out.add(intArrayOf(pi, RdsCode.A, b, RdsCode.B, c, RdsCode.C, d, RdsCode.D))
            }
            return out
        }
    }
}
