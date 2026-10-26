package io.github.mikeileech.sdr4noob.dsp

import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

/** Принятый кадр: байты (7 или 14), ICAO для «адрес/чётность» (иначе null), уровень dBFS, исправлено бит. */
class AdsbFrame(val frame: ByteArray, val icao: Int?, val signal: Double, val fixed: Int)

/**
 * Поиск кадров Mode S в потоке IQ — перенос Demodulator из rtltv/adsb.py. [feed] принимает блоки подряд
 * (хвост блока переносится). Работает на 2 MS/s; при другой частоте [fs] модуль IQ линейно передискретизуется
 * в 2 MS/s (как np.interp в Python) — так 2,4 MS/s (одна из частот радиорежима) обрабатывается без потерь.
 * Частоты ниже 2 MS/s не годятся: импульс 0,5 мкс там короче отсчёта.
 *
 * Преамбула ищется по парам соседних отсчётов (импульс 0,5 мкс при любом дробном сдвиге целиком в паре),
 * биты — PPM с учётом дробного сдвига (Витерби по двум состояниям), затем «как есть» и со сдвигом ±1 отсчёт.
 * CRC-24 проверяется синдромом; у DF17/18 исправляется одна ошибка в бите (только у уже известного борта).
 */
class AdsbDemodulator(fs: Int = Adsb.FS, val level: Double = 1.4, val contrast: Double = 1.3) {
    val fs: Int = fs
    private var tail = FloatArray(0)
    var skip = 0
        private set
    private var rsPos = 0.0
    private var rsLast: Float? = null
    var noise = 0.0
        private set
    var candidates = 0L
        private set
    var corrected = 0
        private set

    private fun resample(mag: FloatArray): FloatArray {
        if (fs == Adsb.FS) return mag
        val last = rsLast
        val x = if (last == null) mag else FloatArray(mag.size + 1).also { it[0] = last; mag.copyInto(it, 1) }
        if (x.isEmpty()) return x
        val ratio = fs.toDouble() / Adsb.FS
        val npos = floor((x.size - 1 - rsPos) / ratio).toInt() + 1
        if (npos <= 0) {  // блок короче шага передискретизации (в Python такого не бывает) — копим смещение
            rsPos -= (x.size - 1)
            rsLast = x[x.size - 1]
            return FloatArray(0)
        }
        val out = FloatArray(npos)
        var p = 0.0
        for (k in 0 until npos) {
            p = rsPos + k * ratio
            val j = floor(p).toInt()
            out[k] = if (j >= x.size - 1) x[x.size - 1]
                     else ((x[j + 1].toDouble() - x[j]) * (p - j) + x[j]).toFloat()
        }
        rsPos = p + ratio - (x.size - 1)
        rsLast = x[x.size - 1]
        return out
    }

    /** IQ ([re]/[im], [n] отсчётов) → список принятых кадров. [known] — адреса бортов, слышных по DF11/17/18. */
    fun feed(re: FloatArray, im: FloatArray, n: Int = re.size, known: Set<Int> = emptySet()): List<AdsbFrame> {
        val mag0 = FloatArray(n) { i -> sqrt(re[i].toDouble() * re[i] + im[i].toDouble() * im[i]).toFloat() }
        val mag = resample(mag0)
        val m = if (tail.isNotEmpty()) FloatArray(tail.size + mag.size).also { tail.copyInto(it); mag.copyInto(it, tail.size) }
                else mag
        val big = m.size - SPAN
        if (big <= 2) {
            tail = m.copyOf()
            return emptyList()
        }
        var sum = 0.0
        for (v in mag) sum += v
        val noise = if (mag.isNotEmpty()) (sum / mag.size).toFloat().toDouble() else 0.0
        this.noise = noise
        // Преамбула: импульсы 0; 1,0; 3,5; 4,5 мкс (отсчёты 0, 2, 7, 9). Импульс 0,5 мкс при любом дробном
        // сдвиге целиком попадает в пару соседних отсчётов, а паузы 4–6 и 11–15 чистые — проверяем это.
        val pair = FloatArray(m.size - 1) { m[it] + m[it + 1] }
        val s = FloatArray(big)
        val c = BooleanArray(big)
        val ctr = contrast.toFloat()
        val thr = (8 * level * noise).toFloat()
        for (i in 0 until big) {
            val p0 = pair[i]; val p2 = pair[i + 2]; val p7 = pair[i + 7]; val p9 = pair[i + 9]
            s[i] = p0 + p2 + p7 + p9
            val pmin = min(min(p0, p2), min(p7, p9))
            var qmax = m[i + 4]
            for (k in QUIET) qmax = max(qmax, m[i + k])
            c[i] = pmin > ctr * qmax && s[i] > thr
        }
        for (i in 1 until big - 1) c[i] = c[i] && s[i] >= s[i - 1] && s[i] > s[i + 1]
        c[0] = false; c[big - 1] = false
        var cnt = 0
        for (v in c) if (v) cnt++
        var out: List<AdsbFrame> = emptyList()
        if (cnt > 0) {
            val idx = IntArray(cnt)
            var q = 0
            for (i in 0 until big) if (c[i]) idx[q++] = i
            candidates += cnt
            out = demod(m, idx, known)
        }
        tail = m.copyOfRange(big, m.size)
        skip = max(0, skip - big)
        return out
    }

    private class Found(val base: Int, val bits: IntArray, val df: Int, val long: Boolean, val syn: Int)

    private fun demod(m: FloatArray, idx: IntArray, known: Set<Int>): List<AdsbFrame> {
        val found = LinkedHashMap<Int, Found>()   // индекс кандидата → (база, биты, df, long, syn)
        // быстрый отсев: по первым 8 битам (Витерби) формат DF должен быть осмысленным
        var rem = ArrayList<Int>()
        val tmp = IntArray(112)
        for (r in idx.indices) {
            bitsDfb(m, idx[r], 8, noise, tmp)
            val pdf = dfOf(tmp)
            bitsStd(m, idx[r], 5, tmp)
            val sdf = dfOf(tmp)
            if (pdf in Adsb.VALID_DF || sdf in Adsb.VALID_DF) rem.add(r)
        }
        for ((dfb, shift) in PASSES) {
            if (rem.isEmpty()) break
            val next = ArrayList<Int>()
            for (r in rem) {
                val base = idx[r] + shift
                val bits = IntArray(112)
                if (dfb) bitsDfb(m, base, 112, noise, bits) else bitsStd(m, base, 112, bits)
                val df = dfOf(bits)
                val long = df in Adsb.LONG_DF
                val syn = syndromeBits(bits, long)
                val isEs = df == 17 || df == 18
                var good = isEs && long && (syn == 0 || Adsb.FIX1.containsKey(syn))
                good = good || (df == 11 && syn < 128)
                if (known.isNotEmpty()) good = good || (df in ADDR_DF && syn in known)
                if (good) found[r] = Found(base, bits, df, long, syn) else next.add(r)
            }
            rem = next
        }
        val res = ArrayList<AdsbFrame>()
        for (f in found.values.sortedBy { it.base }) {
            val i = f.base
            if (i < skip) continue
            val nb = if (f.long) 112 else 56
            val b = f.bits.copyOf(nb)
            var fixed = 0
            var icao: Int? = null
            if (f.df == 17 || f.df == 18) {
                if (f.syn != 0) {
                    val k = Adsb.FIX1.getValue(f.syn)
                    b[k] = b[k] xor 1
                    fixed = 1
                }
            } else if (f.df != 11) {
                icao = f.syn
            }
            val frame = ByteArray(nb / 8)
            for (k in 0 until nb) if (b[k] != 0) frame[k shr 3] = (frame[k shr 3].toInt() or (0x80 ushr (k and 7))).toByte()
            val aa = ((frame[1].toInt() and 0xFF) shl 16) or ((frame[2].toInt() and 0xFF) shl 8) or (frame[3].toInt() and 0xFF)
            if (f.df == 11 && f.syn != 0 && aa !in known) continue  // IID ≠ 0 принимаем только от уже известного борта
            if (fixed != 0 && aa !in known) continue  // исправленный кадр от незнакомого адреса — скорее шум
            skip = i + 16 + 2 * nb
            corrected += fixed
            val lvl = (m[i] + m[i + 1] + m[i + 2] + m[i + 3] + m[i + 7] + m[i + 8] + m[i + 9] + m[i + 10]).toDouble() / 4.0
            res.add(AdsbFrame(frame, icao, Adsb.levelDb(lvl), fixed))
        }
        return res
    }

    companion object {
        const val SPAN = 16 + 224  // преамбула + 112 бит при 2 MS/s
        private val QUIET = intArrayOf(5, 6, 11, 12, 13, 14, 15)
        private val ADDR_DF = intArrayOf(0, 4, 5, 16, 20, 21)
        private val PASSES = listOf(true to 0, false to 0, true to -1, true to 1)

        private fun dfOf(b: IntArray) = (b[0] shl 4) or (b[1] shl 3) or (b[2] shl 2) or (b[3] shl 1) or b[4]

        /** Синдром по битам: XOR вкладов установленных битов (у короткого кадра — вклады с 56-го). */
        internal fun syndromeBits(bits: IntArray, long: Boolean): Int {
            var syn = 0
            if (long) { for (i in 0 until 112) if (bits[i] != 0) syn = syn xor Adsb.CONTRIB[i] }
            else for (i in 0 until 56) if (bits[i] != 0) syn = syn xor Adsb.CONTRIB[56 + i]
            return syn
        }

        /** PPM «как есть»: бит = чётный отсчёт больше нечётного. */
        internal fun bitsStd(m: FloatArray, base: Int, nbits: Int, out: IntArray) {
            for (j in 0 until nbits) {
                val p = base + 16 + 2 * j
                out[j] = if (m[p] > m[p + 1]) 1 else 0
            }
        }

        /**
         * PPM с учётом дробного сдвига: импульс сдвинут на долю φ отсчёта и «протекает» в соседний,
         * так что каждый отсчёт зависит от текущего и предыдущего бита. φ и амплитуду A оцениваем
         * по преамбуле, а биты ищем алгоритмом Витерби по двум состояниям (предыдущий бит 0/1).
         * Арифметика во float32, как у numpy в Python.
         */
        internal fun bitsDfb(m: FloatArray, base: Int, nbits: Int, nf: Double, out: IntArray) {
            val a = (m[base] + m[base + 1] + m[base + 2] + m[base + 3] + m[base + 7] + m[base + 8] +
                m[base + 9] + m[base + 10]) / 4.0f + 1e-9f
            val phi = ((m[base + 1] + m[base + 3] + m[base + 8] + m[base + 10]) / (4.0f * a)).coerceIn(0f, 1f)
            val a1 = a * (1 - phi); val ap = a * phi
            val nf2 = (nf * nf).toFloat()  // шум поднимает «пустые» отсчёты модуля: E|s+n| ≈ √(s² + nf²)
            fun e(v: Float) = sqrt(v * v + nf2)
            // ожидаемые (чётный, нечётный) отсчёты для пары (предыдущий бит p, бит b)
            val e00 = e(ap); val e01 = e(a1 + ap); val e10 = e(0f); val e11 = e(a1)
            val o0 = e(a1); val o1 = e(ap)
            var c0 = 1e9f   // перед данными пауза преамбулы — как после бита 1
            var c1 = 0f
            val back0 = BooleanArray(nbits); val back1 = BooleanArray(nbits)
            for (j in 0 until nbits) {
                val p = base + 16 + 2 * j
                val se = m[p]; val so = m[p + 1]
                val d0 = so - o0; val d1 = so - o1
                val oc0 = d0 * d0; val oc1 = d1 * d1
                val x00 = se - e00; val x10 = se - e10; val x01 = se - e01; val x11 = se - e11
                val n0a = c0 + (x00 * x00 + oc0); val n0b = c1 + (x10 * x10 + oc0)
                val n1a = c0 + (x01 * x01 + oc1); val n1b = c1 + (x11 * x11 + oc1)
                back0[j] = n0b < n0a
                back1[j] = n1b < n1a
                c0 = min(n0a, n0b); c1 = min(n1a, n1b)
            }
            var st = if (c1 < c0) 1 else 0
            for (j in nbits - 1 downTo 0) {
                out[j] = st
                st = if (if (st == 0) back0[j] else back1[j]) 1 else 0
            }
        }
    }
}
