package io.github.mikeileech.sdr4noob.dsp

import kotlin.math.PI
import kotlin.math.ceil
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * APRS: AFSK Bell 202 (1200 бод, 1200/2200 Гц) — перенос rtltv/aprs.py (разделы «кодер AFSK» и «демодулятор»).
 *
 * Демодулятор (по мотивам Dire Wolf):
 *  * звук NFM 48 кГц → несколько «входных профилей» (разные полосовые фильтры и окна корреляторов);
 *  * в каждом профиле — квадратурные корреляторы на 1200 и 2200 Гц, амплитуды тонов,
 *    по желанию — АРУ каждого тона по пикам/провалам (выравнивает пред-/деакцентирование);
 *  * на каждый профиль несколько «срезателей» с разным весом тона паузы (space gain);
 *  * у каждого срезателя своя цифровая ФАПЧ тактов по переходам, NRZI, снятие бит-стаффинга,
 *    поиск флагов HDLC, проверка FCS;
 *  * кадр с плохой FCS можно спасти исправлением одного или двух соседних битов (по синдрому CRC)
 *    — только если адреса AX.25 и поле управления выглядят правильно;
 *  * одинаковые кадры от разных срезателей сливаются в один (с числом «услышавших» декодеров).
 *
 * Весь расчёт — в double, как в numpy (иначе срезатели расходятся с Python на шумном сигнале).
 * Кодер AFSK + AX.25 — для тестов и тестового эфира.
 */
object Afsk {
    const val FS = 48000
    const val BAUD = 1200
    const val MARK = 1200.0
    const val SPACE = 2200.0

    private val FLAG = intArrayOf(0, 1, 1, 1, 1, 1, 1, 0)

    /** Байты кадра с FCS → биты HDLC: флаги, данные младшим битом вперёд с бит-стаффингом, флаги. */
    fun hdlcBits(frameFcs: ByteArray, preamble: Int = 40, postamble: Int = 4): IntArray {
        val out = ArrayList<Int>(frameFcs.size * 10 + (preamble + postamble) * 8)
        repeat(preamble) { for (b in FLAG) out.add(b) }
        var ones = 0
        for (byte in frameFcs) {
            val v = byte.toInt() and 0xFF
            for (k in 0 until 8) {
                val b = (v ushr k) and 1
                out.add(b)
                if (b != 0) {
                    ones++
                    if (ones == 5) { out.add(0); ones = 0 }
                } else ones = 0
            }
        }
        repeat(postamble) { for (b in FLAG) out.add(b) }
        return out.toIntArray()
    }

    /** NRZI: 0 — смена тона, 1 — тон тот же. Возвращает уровни (1 — метка 1200 Гц). */
    fun nrzi(bits: IntArray, level: Int = 1): IntArray {
        var acc = level.toLong()
        return IntArray(bits.size) { i ->
            if (bits[i] == 0) acc++
            Math.floorMod(acc, 2L).toInt()
        }
    }

    /**
     * Уровни NRZI → звук AFSK с непрерывной фазой.
     * fs может быть дробной: так моделируется ошибка тактовой передатчика (звук «как будто» fs).
     * spaceGain — относительная громкость тона 2200 Гц (пред-/деакцентирование тракта).
     */
    fun afsk(levels: IntArray, fs: Double = FS.toDouble(), baud: Int = BAUD, mark: Double = MARK, space: Double = SPACE,
             amp: Double = 0.5, spaceGain: Double = 1.0, shift: Double = 0.0, phase: Double = 0.0): FloatArray {
        val n = ceil(levels.size * fs / baud).toInt()
        val step = baud / fs
        val out = FloatArray(n)
        var acc = 0.0
        for (i in 0 until n) {
            val k = min((i * step).toLong(), (levels.size - 1).toLong()).toInt()
            val lv = levels[k] != 0
            acc += (if (lv) mark else space) + shift
            val ph = phase + 2 * PI * acc / fs
            val a = (if (lv) 1.0 else spaceGain) * amp
            out[i] = (a * sin(ph)).toFloat()
        }
        return out
    }

    /** Кадр (без FCS) → звук одного пакета. */
    fun encodeFrameAudio(frame: ByteArray, fs: Double = FS.toDouble(), preamble: Int = 40, postamble: Int = 4,
                         amp: Double = 0.5, spaceGain: Double = 1.0, shift: Double = 0.0, phase: Double = 0.0): FloatArray =
        afsk(nrzi(hdlcBits(Ax25.addFcs(frame), preamble, postamble)), fs = fs, amp = amp, spaceGain = spaceGain,
            shift = shift, phase = phase)

    /** TNC2-строка → звук одного пакета. */
    fun encodeFrameAudio(tnc2: String, fs: Double = FS.toDouble()) = encodeFrameAudio(Ax25.tnc2Frame(tnc2), fs)

    /** Несколько пакетов подряд с паузами (секунды) → звук float. */
    fun encodePackets(frames: List<ByteArray>, fs: Double = FS.toDouble(), gap: Double = 0.5, lead: Double = 0.3,
                      preamble: Int = 40, postamble: Int = 4, amp: Double = 0.5, spaceGain: Double = 1.0,
                      shift: Double = 0.0): FloatArray {
        val parts = ArrayList<FloatArray>()
        parts.add(FloatArray((lead * fs).toInt()))
        for (fr in frames) {
            parts.add(encodeFrameAudio(fr, fs, preamble, postamble, amp, spaceGain, shift))
            parts.add(FloatArray((gap * fs).toInt()))
        }
        val out = FloatArray(parts.sumOf { it.size })
        var p = 0
        for (a in parts) { System.arraycopy(a, 0, out, p, a.size); p += a.size }
        return out
    }

    /** Python: float // float (CPython float_floor_div). */
    internal fun floorDiv(vx: Double, wx: Double): Double {
        var mod = vx % wx
        var div = (vx - mod) / wx
        if (mod != 0.0) {
            if ((wx < 0) != (mod < 0)) { mod += wx; div -= 1.0 }
        }
        return if (div != 0.0) {
            var fd = floor(div)
            if (div - fd > 0.5) fd += 1.0
            fd
        } else Math.copySign(0.0, vx / wx)
    }
}

/** Профиль входа: полоса предфильтра (или null), окно коррелятора в битах, форма окна, АРУ в битах, веса паузы. */
data class AfskProfile(val band: Pair<Double, Double>?, val winBits: Double, val window: String, val agcBits: Int,
                       val gains: DoubleArray)

/** Принятый кадр: байты без FCS, время конца кадра (с), исправленных битов, сколько срезателей его приняли. */
class AprsRxFrame(val data: ByteArray, var t: Double, var fixed: Int, var decoders: Int) {
    override fun toString() = "AprsRxFrame(t=$t, fixed=$fixed, decoders=$decoders, ${data.size} байт)"
}

/** Корреляторы метки/паузы одного профиля: звук → амплитуды тонов на сниженной частоте. */
internal class AfskFrontEnd(fs: Double, private val dec: Int, baud: Int, mark: Double, space: Double,
                            band: Pair<Double, Double>?, winBits: Double, window: String, agcBits: Int) {
    private val pre: DoubleArray? = band?.let { Firwin.bandpass(81, it.first, it.second, fs) }
    private val preTail = DoubleArray(if (pre == null) 0 else pre.size - 1)
    private val lpLen = max(3, Math.rint(winBits * fs / baud).toInt())
    private val lp: DoubleArray
    private val tailRe = Array(2) { DoubleArray(lpLen - 1) }
    private val tailIm = Array(2) { DoubleArray(lpLen - 1) }
    private val w = doubleArrayOf(2 * PI * mark / fs, 2 * PI * space / fs)
    private val ph = doubleArrayOf(0.0, 0.0)
    private var n = 0L
    private val agc = if (agcBits != 0) Math.rint(agcBits * fs / dec / baud).toInt() else 0
    private val hist = Array(2) { DoubleArray(max(0, agc - 1)) }

    init {
        val L = lpLen
        val win = if (window == "box") DoubleArray(L) { 1.0 }
        else DoubleArray(L) { i -> val M = L + 2; 0.5 + 0.5 * cos(PI * (1 - M + 2 * (i + 1)) / (M - 1)) }  // np.hanning(L+2)[1:-1]
        val s = win.sum()
        lp = DoubleArray(L) { win[it] / s }
    }

    /** Возвращает (метка, пауза) — амплитуды на частоте fs/dec. */
    fun process(xIn: DoubleArray): Pair<DoubleArray, DoubleArray> {
        var x = xIn
        if (pre != null) {
            val p = pre.size
            val buf = DoubleArray(p - 1 + x.size)
            System.arraycopy(preTail, 0, buf, 0, p - 1)
            System.arraycopy(x, 0, buf, p - 1, x.size)
            System.arraycopy(buf, buf.size - (p - 1), preTail, 0, p - 1)
            val y = DoubleArray(x.size)
            for (i in y.indices) {
                var acc = 0.0
                for (j in 0 until p) acc += buf[i + j] * pre[p - 1 - j]
                y[i] = acc
            }
            x = y
        }
        val nx = x.size
        val start = Math.floorMod(-n, dec.toLong()).toInt()
        n += nx
        val nOut = if (start >= nx) 0 else (nx - 1 - start) / dec + 1
        val res = Array(2) { DoubleArray(nOut) }
        val L = lpLen
        val yRe = DoubleArray(L - 1 + nx)
        val yIm = DoubleArray(L - 1 + nx)
        for (k in 0 until 2) {
            val p0 = ph[k]; val wk = w[k]
            ph[k] = PyNum.mod(p0 + wk * nx, 2 * PI)
            System.arraycopy(tailRe[k], 0, yRe, 0, L - 1)
            System.arraycopy(tailIm[k], 0, yIm, 0, L - 1)
            for (i in 0 until nx) {
                val a = p0 + wk * i
                // x·exp(−j·φ) = x·cos φ − j·x·sin φ
                yRe[L - 1 + i] = x[i] * cos(a)
                yIm[L - 1 + i] = -(x[i] * sin(a))
            }
            System.arraycopy(yRe, yRe.size - (L - 1), tailRe[k], 0, L - 1)
            System.arraycopy(yIm, yIm.size - (L - 1), tailIm[k], 0, L - 1)
            val o = res[k]
            var i = start
            var q = 0
            while (i < nx) {
                var sr = 0.0; var si = 0.0
                for (j in 0 until L) {
                    val c = lp[L - 1 - j]
                    sr += yRe[i + j] * c; si += yIm[i + j] * c
                }
                o[q++] = Math.hypot(sr, si)
                i += dec
            }
        }
        if (agc > 1 && nOut > 0) {
            val a = agc
            for (k in 0 until 2) {
                val v = res[k]
                val buf = DoubleArray(a - 1 + nOut)
                System.arraycopy(hist[k], 0, buf, 0, a - 1)
                System.arraycopy(v, 0, buf, a - 1, nOut)
                System.arraycopy(buf, buf.size - (a - 1), hist[k], 0, a - 1)
                val r = DoubleArray(nOut)
                for (i in 0 until nOut) {
                    var hi = buf[i]; var lo = buf[i]
                    for (j in i + 1 until i + a) { val z = buf[j]; if (z > hi) hi = z; if (z < lo) lo = z }
                    r[i] = (v[i] - lo) / (hi - lo + 1e-9)
                }
                res[k] = r
            }
        }
        return res[0] to res[1]
    }
}

/** Срезатель + ФАПЧ тактов + NRZI + HDLC. Кадры (биты, время) копятся в [frames]. */
internal class AfskSlicer(private val T: Double, val gain: Double) {
    private var nextT = T / 2
    private var level = false
    private var prevLevel = false
    private var lastD = 0.0
    private var ones = 0
    private var buf = ByteArrayList()
    private var hunting = true
    val frames = ArrayList<Pair<ByteArray, Double>>()

    fun busy() = !hunting && buf.size >= 24

    fun process(d: DoubleArray, t0: Long) {
        if (d.isEmpty()) return
        val taus = ArrayList<Double>()
        var prev = lastD
        var prevPos = prev > 0
        for (i in d.indices) {
            val cur = d[i]
            val pos = cur > 0
            if (pos != prevPos) taus.add((t0 - 1 + i).toDouble() + prev / (prev - cur))
            prev = cur; prevPos = pos
        }
        lastD = d[d.size - 1]
        val half = 0.5 * T
        var nt = nextT
        var lvl = level
        for (tau in taus) {
            if (tau > nt) {
                val k = ceil((tau - nt) / T).toInt()
                run(lvl, k, nt)
                nt += k * T
            }
            lvl = !lvl
            nt -= (if (hunting) 0.5 else 0.26) * ((nt - tau) - half)
        }
        val tEnd = (t0 + d.size - 1).toDouble()
        if (nt <= tEnd) {
            val k = Afsk.floorDiv(tEnd - nt, T).toInt() + 1
            run(lvl, k, nt)
            nt += k * T
        }
        nextT = nt
        level = lvl
    }

    /** k тактовых отсчётов одного уровня: первый бит NRZI по смене уровня, остальные — единицы. */
    private fun run(lvl: Boolean, kIn: Int, t: Double) {
        var k = kIn
        val same = lvl == prevLevel
        prevLevel = lvl
        if (same) {
            ones++
            if (ones >= 7) abort() else buf.add(1)
        } else {
            val o = ones
            ones = 0
            if (o == 6) flag(t)
            else if (o != 5 && o < 7) {          // 5 единиц + 0 — вставленный бит, его выбрасываем
                buf.add(0)
                if (buf.size > MAXBITS) abort()
            }
        }
        k -= 1
        if (k > 0) {
            if (ones + k >= 7) {
                if (ones < 7) abort()
                ones += k
            } else {
                repeat(k) { buf.add(1) }
                ones += k
            }
        }
    }

    private fun abort() {
        buf.clear()
        hunting = true
        ones = max(ones, 7)
    }

    private fun flag(t: Double) {
        if (!hunting) {
            val n = buf.size - 7
            if (n >= Ax25.MIN_FRAME * 8 && n % 8 == 0) frames.add(buf.copyOf(n) to t)
        }
        buf.clear()
        hunting = false
    }

    /** Динамический массив битов без упаковки в объекты. */
    class ByteArrayList {
        private var a = ByteArray(1024)
        var size = 0; private set
        fun add(v: Int) { if (size == a.size) a = a.copyOf(a.size * 2); a[size++] = v.toByte() }
        fun clear() { size = 0 }
        fun copyOf(n: Int) = a.copyOf(n)
    }

    companion object { const val MAXBITS = Ax25.MAX_FRAME * 8 + 8 }
}

/**
 * Потоковый приёмник APRS: [process] (звук) → список принятых кадров [AprsRxFrame].
 */
class AfskDemodulator(val fs: Double = Afsk.FS.toDouble(), val baud: Int = Afsk.BAUD, private val mark: Double = Afsk.MARK,
                      private val space: Double = Afsk.SPACE, private val profiles: List<AfskProfile> = PROFILES,
                      val fixBits: Boolean = true) {
    val dec = max(1, Math.rint(fs / (baud * 8)).toInt())
    val fsd = fs / dec
    private lateinit var fronts: List<Pair<AfskFrontEnd, List<AfskSlicer>>>
    private lateinit var slicers: List<AfskSlicer>
    private var nOut = 0L            // отсчётов на сниженной частоте
    private var recent = LinkedHashMap<String, Double>()
    var framesTotal = 0; private set
    var crcErrors = 0; private set
    var fixedTotal = 0; private set
    var level = 0.0; private set

    init { build() }

    private fun build() {
        val T = fsd / baud
        fronts = profiles.map { p ->
            AfskFrontEnd(fs, dec, baud, mark, space, p.band, p.winBits, p.window, p.agcBits) to
                p.gains.map { AfskSlicer(T, it) }
        }
        slicers = fronts.flatMap { it.second }
        nOut = 0; recent = LinkedHashMap(); framesTotal = 0; crcErrors = 0; fixedTotal = 0; level = 0.0
    }

    val nDecoders get() = slicers.size

    /** «Несущая данных»: хотя бы один срезатель сейчас внутри кадра. */
    fun busy() = slicers.any { it.busy() }

    /** Сброс в начальное состояние (в Python reset() пересоздаёт объект с параметрами по умолчанию; здесь — с теми же). */
    fun reset() = build()

    fun process(xf: FloatArray, len: Int = xf.size): List<AprsRxFrame> {
        if (len == 0) return emptyList()
        val x = DoubleArray(len) { xf[it].toDouble() }
        var ss = 0.0
        for (v in x) ss += v * v
        level = sqrt(ss / len)
        val t0 = nOut
        var nout = -1
        for ((fe, sl) in fronts) {
            val (m, s) = fe.process(x)
            nout = m.size
            for (z in sl) {
                val g = z.gain
                z.process(DoubleArray(m.size) { m[it] - g * s[it] }, t0)
            }
        }
        nOut += max(0, nout)
        val found = LinkedHashMap<String, AprsRxFrame>()
        for (z in slicers) {
            if (z.frames.isEmpty()) continue
            for ((bits, t) in z.frames) {
                var raw: ByteArray? = packBits(bits)
                var fixed = 0
                if (!Ax25.checkFcs(raw!!)) {
                    if (!fixBits) { crcErrors++; continue }
                    val r = Ax25.tryFix(raw)
                    raw = r.first; fixed = r.second
                    if (raw == null) { crcErrors++; continue }
                }
                val data = raw.copyOf(raw.size - 2)
                val fr = Ax25.decode(data)
                if (fr == null || (fixed != 0 && !Ax25.isAprs(fr))) continue
                val key = String(data, Charsets.ISO_8859_1)
                val e = found[key]
                if (e == null) found[key] = AprsRxFrame(data, t / fsd, fixed, 1)
                else {
                    e.decoders++
                    e.fixed = min(e.fixed, fixed)
                    e.t = min(e.t, t / fsd)
                }
            }
            z.frames.clear()
        }
        val out = ArrayList<AprsRxFrame>()
        val now = nOut / fsd
        recent = LinkedHashMap(recent.filter { now - it.value < 3.0 })
        for ((key, e) in found.entries.sortedBy { it.value.t }) {
            // тот же кадр от другого срезателя кончается почти в тот же миг; повтор передачи — позже
            val r = recent[key]
            if (r != null && kotlin.math.abs(e.t - r) < 0.25) continue
            recent[key] = e.t
            framesTotal++
            if (e.fixed != 0) fixedTotal++
            out.add(e)
        }
        return out
    }

    companion object {
        /** профили: (полоса предфильтра или null, окно коррелятора в битах, форма окна, АРУ в битах, веса паузы) */
        val PROFILES = listOf(
            AfskProfile(null, 1.0, "box", 12, doubleArrayOf(0.55, 0.75, 1.0, 1.33, 1.8)),
            AfskProfile(900.0 to 2500.0, 1.6, "hann", 12, doubleArrayOf(0.7, 1.0, 1.43)),
            AfskProfile(500.0 to 2900.0, 1.0, "box", 0, doubleArrayOf(0.32, 0.56, 1.0, 1.8, 3.2)),
        )

        /** Биты младшим вперёд → байты (np.packbits(bitorder="little")). */
        fun packBits(bits: ByteArray): ByteArray {
            val out = ByteArray((bits.size + 7) / 8)
            for (i in bits.indices) if (bits[i].toInt() != 0) out[i / 8] = (out[i / 8].toInt() or (1 shl (i % 8))).toByte()
            return out
        }

        /** Весь звук сразу (блоками, как в потоке) → список кадров. */
        fun decodeAudio(x: FloatArray, fs: Double = Afsk.FS.toDouble(), block: Int = 4800, fixBits: Boolean = true): List<AprsRxFrame> {
            val dem = AfskDemodulator(fs, fixBits = fixBits)
            val out = ArrayList<AprsRxFrame>()
            var i = 0
            while (i < x.size) {
                val n = min(block, x.size - i)
                out.addAll(dem.process(x.copyOfRange(i, i + n)))
                i += n
            }
            return out
        }
    }
}
