package io.github.mikeileech.sdr4noob.dsp

import io.github.mikeileech.sdr4noob.dsp.DmrCodes.ambeDeinterleave
import io.github.mikeileech.sdr4noob.dsp.DmrCodes.bitsBytes
import io.github.mikeileech.sdr4noob.dsp.DmrCodes.bitsInt
import io.github.mikeileech.sdr4noob.dsp.DmrCodes.intBits
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sin
import kotlin.math.sqrt

/** Синтезатор голоса AMBE+2 одного слота (mbelib через JNI; в тестах — заглушка). */
interface AmbeSynth {
    fun reset()
    /** ambe_fr[96] → 160 отсчётов 8 кГц (≈ ±1); возвращает число ошибок кадра. */
    fun decode(fr: ByteArray, out: FloatArray): Int
    val ok: Boolean
}

/** Событие журнала DMR (как словарь событий в rtltv/dmr.py). */
data class DmrEvent(val type: String, val slot: Int, val cc: Int, val src: Int, val dst: Int, val group: Boolean,
                    val encrypted: Boolean, val text: String)

data class DmrSlotStatus(val active: Boolean, val src: Int, val dst: Int, val group: Boolean, val encrypted: Boolean,
                         val cc: Int?, val last: String)

data class DmrStatus(val locked: Boolean, val mode: String, val sync: Int, val audioSlot: Int?, val mbelib: Boolean,
                     val slots: Map<Int, DmrSlotStatus>)

/**
 * Декодер DMR (ETSI TS 102 361, Tier I/II) — перенос DmrDecoder из rtltv/dmr.py. Вход — мгновенная частота
 * (выход ЧМ-дискриминатора) в Гц, 48 кГц: RRC (α = 0,2), 4FSK 4800 Бод, поиск синхрослов BS/MS/Direct,
 * «маховик» TDMA, CACH/TACT, Slot Type (Golay), BPTC + RS(12,9) (LC), CRC (CSBK, данные), EMB (QR) и встроенный
 * LC, голос AMBE+2 → mbelib → 8 кГц → 48 кГц. Шифрованные вызовы звук не выдают.
 */
class DmrDecoder(private val synthFactory: () -> AmbeSynth) {
    companion object {
        const val FS = 48_000
        const val SPS = 10
        const val BURST_SYM = 132
        const val SLOT_SYM = 144
        const val CACH_SYM = 12
        const val SYNC_OFF = 54
        const val SPB = SLOT_SYM * SPS
        const val DEV1 = 648.0
        const val DEV3 = 1944.0
        const val FAM_BS = 0; const val FAM_MS = 1; const val FAM_DM1 = 2; const val FAM_DM2 = 3
        val FAM_NAMES = mapOf(FAM_BS to "BS", FAM_MS to "MS", FAM_DM1 to "DM", FAM_DM2 to "DM")
        val SYNC_HEX = longArrayOf(0x755FD7DF75F7, 0x7F7D5DD57DFD, 0x5D577F7757FF, 0x7DFFD5F55D5F)
        val LEVEL_OF_DIBIT = floatArrayOf(1f, 3f, -1f, -3f)
        private val DIBIT_OF_IDX = intArrayOf(3, 2, 0, 1)
        const val HUNT_THR = 0.82
        const val TRACK_THR = 0.62
        const val LOST_BURSTS = 24
        const val CSBK_REPEAT_S = 30.0

        /** Синхрослово семейства как 24 уровня ±1/±3 /3 (как _sync_signs). */
        fun syncSigns(fam: Int): DoubleArray {
            val b = intBits(SYNC_HEX[fam], 48)
            return DoubleArray(24) { LEVEL_OF_DIBIT[b[2 * it] * 2 + b[2 * it + 1]] / 3.0 }
        }
        val SYNC_MAT: Array<DoubleArray> = Array(4) { syncSigns(it) }   // [семейство][символ]

        fun rrc(sps: Int = SPS, alpha: Double = 0.2, span: Int = 8): DoubleArray {
            val n = span * sps
            return DoubleArray(n + 1) { i ->
                val x = (i - n / 2).toDouble() / sps
                when {
                    abs(x) < 1e-9 -> 1 - alpha + 4 * alpha / PI
                    abs(abs(x) - 1 / (4 * alpha)) < 1e-9 -> alpha / sqrt(2.0) * ((1 + 2 / PI) * sin(PI / (4 * alpha)) +
                        (1 - 2 / PI) * cos(PI / (4 * alpha)))
                    else -> (sin(PI * x * (1 - alpha)) + 4 * alpha * x * cos(PI * x * (1 + alpha))) /
                        (PI * x * (1 - (4 * alpha * x) * (4 * alpha * x)))
                }
            }
        }
        val H_RX: DoubleArray = rrc().let { h -> val s = h.sum(); DoubleArray(h.size) { h[it] / s } }

        val FLCO_NAMES = mapOf(0 to "групповой", 3 to "индивидуальный")
        val DT_NAMES = mapOf(0 to "PI-заголовок", 1 to "LC-заголовок", 2 to "Терминатор", 3 to "CSBK",
            4 to "MBC-заголовок", 5 to "MBC-продолжение", 6 to "Заголовок данных", 7 to "Данные 1/2", 8 to "Данные 3/4",
            9 to "Idle", 10 to "Данные 1/1", 11 to "USB-данные")
        val CSBK_NAMES = mapOf(0x04 to "запрос индивидуального вызова", 0x05 to "ответ на запрос",
            0x07 to "синхронизация каналов", 0x38 to "активация ретранслятора", 0x3D to "преамбула")
        val CSBK_T3 = mapOf(0x19 to "Aloha (Tier III)", 0x1C to "Ahoy (Tier III)", 0x28 to "C_BCAST (Tier III)")
    }

    data class Lc(val flco: Int, val fid: Int, val so: Int, val dst: Int, val src: Int) {
        val group get() = flco == 0
        val encrypted get() = so and 0x40 != 0
    }

    private fun parseLc(lc: IntArray) = Lc(lc[0] and 0x3F, lc[1], lc[2],
        (lc[3] shl 16) or (lc[4] shl 8) or lc[5], (lc[6] shl 16) or (lc[7] shl 8) or lc[8])

    private inner class Slot(val n: Int) {
        val voice = synthFactory()
        var active = false; var announced = false; var src = 0; var dst = 0; var group = true
        var encrypted = false; var pi = false; var vf: Int? = null; var missedA = 0; var badEmb = 0; var idle = 0
        var frags: MutableList<IntArray>? = null; var sf = 0; var lcKnown = false
        var cc: Int? = null; var last = ""; var lastT = 0.0
        val csbkSeen = HashMap<String, Double>()
        fun clear() {
            active = false; announced = false; src = 0; dst = 0; group = true; encrypted = false; pi = false; vf = null
            missedA = 0; badEmb = 0; idle = 0; frags = null; sf = 0; lcKnown = false
        }
    }

    var slotFilter = 0
    private val zi = DoubleArray(H_RX.size - 1)
    private var y = FloatArray(0)
    private var yLen = 0
    private var y0 = 0L
    private var search = 0L
    private var pol = 1
    private val slots = mapOf(1 to Slot(1), 2 to Slot(2))
    private val events = ArrayList<DmrEvent>()
    private val audio = ArrayDeque<FloatArray>()
    private var audioHead = 0          // уже выданная часть audio.first()
    private var audioLen = 0
    private var playing = false
    var audioSlot: Int? = null; private set
    private val up = Firwin.lowpass(121, 3500.0, FS.toDouble()).let { h -> DoubleArray(h.size) { h[it] * 6.0 } }
    private val upHist = DoubleArray(up.size - 1)
    var syncLevel = 0.0; private set
    val stats = mutableMapOf("bursts" to 0, "syncs" to 0, "voice" to 0, "lc_ok" to 0, "csbk" to 0, "emb_ok" to 0)

    // состояние захвата (_unlock)
    private var locked = false
    private var confirmed = false
    private var next = 0.0
    private var spb = SPB.toDouble()
    private var dc = 0.0
    private var scale = DEV3
    private var fam = FAM_BS
    private var slotNext = 1
    private var tcVote = 0
    private var tcFresh = true
    private var firstSync: Pair<Int, Int>? = null
    private var miss = 0
    private var sinceSync = 0
    private var tentPol = 1

    init { unlock() }

    private fun now() = System.nanoTime() / 1e9

    /** [freq] — мгновенная частота, Гц, 48 кГц. Возвращает звук 48 кГц той же длины и события. */
    fun process(freq: FloatArray, n: Int): Pair<FloatArray, List<DmrEvent>> {
        if (n > 0) {
            // согласованный RRC-фильтр (потоковый, как lfilter с zi)
            val need = yLen + n
            if (y.size < need) y = y.copyOf(maxOf(need, y.size * 2))
            val h = H_RX; val m = h.size - 1
            val buf = DoubleArray(m + n)
            System.arraycopy(zi, 0, buf, 0, m)
            for (i in 0 until n) buf[m + i] = freq[i].toDouble()
            for (i in 0 until n) {
                var s = 0.0
                for (k in h.indices) s += h[k] * buf[m + i - k]
                y[yLen + i] = s.toFloat()
            }
            System.arraycopy(buf, n, zi, 0, m)
            yLen += n
        }
        run()
        trim()
        val ev = ArrayList(events); events.clear()
        return pullAudio(n) to ev
    }

    fun status(): DmrStatus = DmrStatus(confirmed, if (confirmed) FAM_NAMES[fam] ?: "" else "",
        if (confirmed) (100 * syncLevel).roundToInt() else 0, audioSlot, slots[1]!!.voice.ok,
        slots.mapValues { (_, s) -> DmrSlotStatus(s.active, s.src, s.dst, s.group, s.encrypted, s.cc, s.last) })

    private fun unlock() {
        locked = false; confirmed = false; next = 0.0; spb = SPB.toDouble(); dc = 0.0; scale = DEV3; fam = FAM_BS
        slotNext = 1; tcVote = 0; tcFresh = true; firstSync = null; miss = 0; sinceSync = 0; tentPol = pol
    }

    private fun trim() {
        val keepFrom = if (locked) next - 3 * SPB else (search - 3 * SPB).toDouble()
        val cut = (floor(keepFrom).toLong() - y0).toInt()
        if (cut > 0) {
            val c = min(cut, yLen)
            System.arraycopy(y, c, y, 0, yLen - c)
            yLen -= c
            y0 += c
        }
    }

    private fun end() = y0 + yLen

    private fun sample(pos: Double): Double {
        val q = pos - y0
        var i = floor(q).toLong()
        val f = q - i
        i = i.coerceIn(0, (yLen - 2).toLong())
        val ii = i.toInt()
        return y[ii] * (1 - f) + y[ii + 1] * f
    }

    /** Нормированная корреляция синхрослова в точке [center] (первый символ синхро) по 4 семействам. */
    private fun corrAt(center: Double): DoubleArray {
        val t = spb / SLOT_SYM
        val v = DoubleArray(24) { sample(center + it * t) }
        return corrVec(v)
    }

    private fun corrVec(v: DoubleArray): DoubleArray {
        val mean = v.average()
        var nv = 0.0
        for (i in v.indices) { v[i] -= mean; nv += v[i] * v[i] }
        nv = sqrt(nv) + 1e-9
        return DoubleArray(4) { f -> var s = 0.0; for (i in 0 until 24) s += v[i] * SYNC_MAT[f][i]; s / (nv * sqrt(24.0)) }
    }

    /** dc, размах и качество подгонки по синхрослову. */
    private fun levels(center: Double, fam: Int, sign: Int): Triple<Double, Double, Double> {
        val t = spb / SLOT_SYM
        val v = DoubleArray(24) { sample(center + it * t) }
        var hi = 0.0; var nh = 0; var lo = 0.0; var nl = 0
        for (i in 0 until 24) { val s = SYNC_MAT[fam][i] * sign; if (s > 0) { hi += v[i]; nh++ } else { lo += v[i]; nl++ } }
        hi /= nh; lo /= nl
        val d = 0.5 * (hi + lo)
        val sc = 0.5 * (hi - lo)
        if (sc <= 1) return Triple(d, sc, 9.0)
        var fit = 0.0
        for (i in 0 until 24) fit += abs(v[i] - (d + SYNC_MAT[fam][i] * sign * sc))
        return Triple(d, sc, fit / 24 / sc)
    }

    private fun run() {
        val guard = 8
        while (true) {
            val e = end()
            if (!locked) { if (!hunt(e)) return; continue }
            if (next + SYNC_OFF * SPS + SPB / 2 + 24 * SPS + guard > e) return
            trackBurst()
        }
    }

    /** Корреляции для окон, начинающихся с отсчётов a..b (шаг символа SPS). */
    private fun windowCorr(a: Int, b: Int): Array<DoubleArray> = Array(b - a) { k ->
        corrVec(DoubleArray(24) { y[a + k + it * SPS].toDouble() })
    }

    private fun hunt(e: Long): Boolean {
        val lo = max(search, y0 + (CACH_SYM + SYNC_OFF) * SPS + 2)
        val hi = e - (BURST_SYM - SYNC_OFF) * SPS - 12
        if (hi - lo < 4) return false
        val a = (lo - y0).toInt(); val b = (hi - y0).toInt()
        val c = windowCorr(a, b)
        val ac = DoubleArray(c.size) { k -> c[k].maxOf { abs(it) } }
        var k0 = 0
        while (true) {
            var k = -1
            for (i in k0 until ac.size) if (ac[i] >= HUNT_THR) { k = i; break }
            if (k < 0) break
            // локальный максимум в пределах символа
            val k2 = min(ac.size, k + SPS)
            var best = k
            for (i in k until k2) if (ac[i] > ac[best]) best = i
            k = best
            var frac = 0.0
            if (k > 0 && k < ac.size - 1) {
                val y1 = ac[k - 1]; val y2 = ac[k]; val y3 = ac[k + 1]
                val den = y1 - 2 * y2 + y3
                if (den < 0) frac = (0.5 * (y1 - y3) / den).coerceIn(-0.5, 0.5)
            }
            var f = 0
            for (i in 1 until 4) if (abs(c[k][i]) > abs(c[k][f])) f = i
            val sign = if (c[k][f] > 0) 1 else -1
            if (tryLock(lo + k + frac, f, sign, ac[k])) return true
            k0 = k + SPS + 1
        }
        search = hi
        return false
    }

    private fun tryLock(center: Double, f: Int, sign: Int, corr: Double): Boolean {
        val (d, sc, fit) = levels(center, f, sign)
        if (!(sc > 600.0 && sc < 3600.0) || fit > 0.45) return false
        val start = center - SYNC_OFF * SPS
        locked = true; confirmed = false; fam = f; dc = d; scale = sc; next = start; spb = SPB.toDouble()
        miss = 0; sinceSync = 0; syncLevel = corr
        // версия полярности: данные проверяются сразу (Golay), голос — по следующим EMB
        val bits = burstBits(start, pol)
        val voiceP = sign * pol > 0
        tentPol = if (!voiceP && slotType(bits.second, 1) != null) pol else {
            val bitsN = burstBits(start, -pol)
            if (voiceP && slotType(bitsN.second, 1) != null) -pol else if (voiceP) pol else -pol
        }
        firstSync = f to sign
        return true
    }

    private fun burstBits(start: Double, p: Int): Pair<IntArray, IntArray> {
        val t = spb / SLOT_SYM
        val nsym = CACH_SYM + BURST_SYM
        val bits = IntArray(2 * nsym)
        for (j in 0 until nsym) {
            val v = sample(start + (j - CACH_SYM) * t)
            val u = p * (v - dc) * (3.0 / scale)
            val idx = if (u < -2.0) 0 else if (u < 0.0) 1 else if (u < 2.0) 2 else 3
            val dib = DIBIT_OF_IDX[idx]
            bits[2 * j] = dib shr 1; bits[2 * j + 1] = dib and 1
        }
        return bits.copyOfRange(0, 2 * CACH_SYM) to bits.copyOfRange(2 * CACH_SYM, bits.size)
    }

    private fun slotType(burst: IntArray, maxd: Int = 3): Pair<Int, Int>? {
        val w = (bitsInt(burst, 98, 108) shl 10) or bitsInt(burst, 156, 166)
        val (m, _) = DmrCodes.nearest(DmrCodes.GOLAY2087_TAB, w, maxd)
        return if (m < 0) null else (m shr 4) to (m and 15)
    }

    /** Поиск синхрослова в пределах ±полслота от ожидаемого места: (корр, смещение, семейство, знак). */
    private fun wideSync(pe: Double): DoubleArray {
        val half = SPB / 2
        val a = max((pe - half - y0).toInt(), 0)
        val b = min((pe + half - y0).toInt(), yLen - 23 * SPS - 2)
        if (b - a < 2 * SPS) return doubleArrayOf(0.0, 0.0, 0.0, 0.0)
        val c = windowCorr(a, b)
        val ac = DoubleArray(c.size) { k -> c[k].maxOf { abs(it) } }
        for (k in ac.indices.sortedByDescending { ac[it] }.take(3)) {
            if (ac[k] < HUNT_THR) break
            var f = 0
            for (i in 1 until 4) if (abs(c[k][i]) > abs(c[k][f])) f = i
            val sign = if (c[k][f] > 0) 1 else -1
            val pos = (y0 + a + k).toDouble()
            val (_, sc, fit) = levels(pos, f, sign)
            if (sc > 600.0 && sc < 3600.0 && fit <= 0.45) return doubleArrayOf(ac[k], pos - pe, f.toDouble(), sign.toDouble())
        }
        return doubleArrayOf(0.0, 0.0, 0.0, 0.0)
    }

    private fun trackBurst() {
        var start = next
        val t = spb / SLOT_SYM
        var sync: Pair<Int, Int>? = null
        val first = firstSync
        if (first != null) { firstSync = null; sync = first } else {
            val pe = start + SYNC_OFF * t
            var best = doubleArrayOf(0.0, 0.0, 0.0, 0.0)
            for (oi in -4..4) {
                val o = oi.toDouble()
                val c = corrAt(pe + o)
                val ca = DoubleArray(4) { if (it != fam) abs(c[it]) - 0.2 else abs(c[it]) }   // другое семейство — только при сильной
                var f = 0
                for (i in 1 until 4) if (ca[i] > ca[f]) f = i
                if (ca[f] > best[0]) best = doubleArrayOf(abs(c[f]), o, f.toDouble(), if (c[f] > 0) 1.0 else -1.0)
            }
            if (best[0] < TRACK_THR && sinceSync >= 2) best = wideSync(pe)
            if (best[0] >= TRACK_THR) {
                val corr = best[0]; val o = best[1]; val f = best[2].toInt(); val sign = best[3].toInt()
                // уточнение по параболе
                val y1 = abs(corrAt(pe + o - 1)[f]); val y2 = corr; val y3 = abs(corrAt(pe + o + 1)[f])
                val den = y1 - 2 * y2 + y3
                val frac = if (den < 0) (0.5 * (y1 - y3) / den).coerceIn(-0.5, 0.5) else 0.0
                val err = o + frac
                val (d, sc, fit) = levels(pe + err, f, sign)
                if (sc > 400.0 && sc < 4000.0 && fit < 0.6) {
                    start += err
                    if (abs(err) > 6) spb = SPB.toDouble()          // скачок (пропуск отсчётов) — такт заново
                    else if (sinceSync > 0) spb = (spb + 0.2 * err / sinceSync).coerceIn(SPB * 0.999, SPB * 1.001)
                    dc += 0.5 * (d - dc)
                    scale += 0.5 * (sc - scale)
                    syncLevel += 0.3 * (corr - syncLevel)
                    sinceSync = 0
                    sync = f to sign
                }
            }
        }
        val p = if (!confirmed) tentPol else pol
        val (cach, burst) = burstBits(start, p)
        var valid = handleBurst(cach, burst, sync, p)
        if (sync != null && first == null) valid = true      // синхрослово снова там, где ожидали
        stats["bursts"] = stats["bursts"]!! + 1
        if (sync != null) stats["syncs"] = stats["syncs"]!! + 1
        sinceSync++
        next = start + spb
        if (valid) {
            miss = 0
            if (!confirmed) { confirmed = true; pol = p }
        } else {
            miss++
            if (sync == null) syncLevel *= 0.97
            val lim = if (confirmed) LOST_BURSTS else 7
            if (miss >= lim || sinceSync > 2 * LOST_BURSTS) loseSync()
        }
    }

    private fun loseSync() {
        val was = confirmed
        for (s in slots.values) {
            if (s.active) callEnd(s, null, "конец вызова (потеря синхронизации)")
            s.vf = null
        }
        search = next.toLong()
        unlock()
        syncLevel = 0.0
        if (was) emit("sync_lost", 0, null, text = "потеря синхронизации DMR")
    }

    // ---------------------------------------------------------------- пакеты
    private fun handleBurst(cach: IntArray, burst: IntArray, sync: Pair<Int, Int>?, p: Int): Boolean {
        if (sync != null) fam = sync.first
        var slotN = slotNext
        if (fam == FAM_BS) {
            val tact = intArrayOf(0, 4, 8, 12, 14, 18, 22).map { cach[it] }.toIntArray()
            val ok = DmrCodes.ham743(tact.copyOfRange(0, 4)).contentEquals(tact.copyOfRange(4, 7))
            val tc = if (tact[1] != 0) 2 else 1
            if (ok && tcFresh) { tcFresh = false; slotN = tc }
            tcVote += (if (ok) 2 else 1) * (if (tc == slotN) 1 else -1)
            tcVote = tcVote.coerceIn(-6, 6)
            if (tcVote < -2) { slotN = 3 - slotN; tcVote = 2 }
        } else if (sync != null && (sync.first == FAM_DM1 || sync.first == FAM_DM2)) {
            slotN = if (sync.first == FAM_DM1) 1 else 2
        }
        slotNext = 3 - slotN
        val st = slots[slotN]!!
        val conf = confirmed
        if (sync != null) {
            val voice = sync.second * p > 0
            if (!voice) return onData(st, burst, conf)
            st.missedA = 0
            return onVoice(st, burst, true, conf) || conf
        }
        val vf = st.vf
        if (vf != null) {
            if (vf < 5) return onVoice(st, burst, false, conf)
            if (st.missedA < 2 && st.badEmb < 3) {
                st.missedA++
                onVoice(st, burst, true, conf)
                return false
            }
            st.vf = null
        }
        if (st.active) {
            st.idle++
            if (st.idle >= 4) callEnd(st, null, "конец вызова (нет голоса)")
        }
        return false
    }

    private fun onData(st: Slot, burst: IntArray, conf: Boolean): Boolean {
        val r = slotType(burst, if (conf) 3 else 1) ?: return false
        val (cc, dt) = r
        st.cc = cc
        st.last = DT_NAMES[dt] ?: "тип $dt"
        st.lastT = now()
        st.vf = null
        val info = burst.copyOfRange(0, 98) + burst.copyOfRange(166, 264)
        if (dt in intArrayOf(0, 1, 2, 3, 4, 6)) {
            val (plain, fixed) = DmrCodes.bptcDecode(info)
            if (dt == 1 || dt == 2) {
                val mask = if (dt == 1) 0x96 else 0x99
                var lc: IntArray? = null
                for (cand in listOf(plain, fixed)) {
                    val blk = bitsBytes(cand)
                    for (i in 0 until 3) blk[9 + i] = blk[9 + i] xor mask
                    val good = DmrCodes.rs129Check(blk)
                    if (good != null) { lc = good.copyOfRange(0, 9); break }
                }
                if (lc != null) {
                    stats["lc_ok"] = stats["lc_ok"]!! + 1
                    val l = parseLc(lc)
                    if (dt == 1) onHeader(st, l) else {
                        st.last = "Терминатор"
                        if (st.active) callEnd(st, l, null)
                    }
                }
            } else {
                val mask = when (dt) { 0 -> 0x6969; 3 -> 0xA5A5; 4 -> 0xAAAA; else -> 0xCCCC }
                var blk: IntArray? = null
                for (cand in listOf(plain, fixed)) { val b = bitsBytes(cand); if (DmrCodes.crcOk(b, mask)) { blk = b; break } }
                if (blk != null) {
                    when {
                        dt == 0 -> {
                            st.pi = true
                            st.encrypted = st.encrypted || st.active
                            if (conf) emit("data", st.n, st.cc, text = "PI-заголовок: шифрование, алгоритм ${blk[0]}, ключ ${blk[2]}",
                                encrypted = true)
                        }
                        dt == 3 -> { stats["csbk"] = stats["csbk"]!! + 1; onCsbk(st, blk, conf) }
                        dt == 6 && conf -> {
                            val grp = blk[0] and 0x80 != 0
                            val dst = (blk[2] shl 16) or (blk[3] shl 8) or blk[4]
                            val src = (blk[5] shl 16) or (blk[6] shl 8) or blk[7]
                            emit("data", st.n, st.cc, src, dst, grp, text = "передача данных $src → ${if (grp) "TG " else ""}$dst")
                        }
                    }
                }
            }
        }
        if (dt == 9 && st.active) {
            st.idle++
            if (st.idle >= 2) callEnd(st, null, "конец вызова (без терминатора)")
        }
        return true
    }

    private fun hex(b: IntArray, from: Int, to: Int) = (from until to).joinToString("") { String.format("%02X", b[it]) }

    private fun onCsbk(st: Slot, b: IntArray, conf: Boolean) {
        val op = b[0] and 0x3F; val fid = b[1]
        var dst = (b[4] shl 16) or (b[5] shl 8) or b[6]
        var src = (b[7] shl 16) or (b[8] shl 8) or b[9]
        val key = hex(b, 0, 10)
        val t = now()
        st.last = "CSBK"
        if (!conf) return
        val seen = st.csbkSeen
        if (t - (seen[key] ?: -1e9) < CSBK_REPEAT_S) return
        if (seen.size > 64) seen.clear()
        seen[key] = t
        val text: String
        if (fid == 0 && op in CSBK_NAMES) {
            val name = CSBK_NAMES.getValue(op)
            if (op == 0x3D) {
                val grp = b[2] and 0x40 != 0
                emit("csbk", st.n, st.cc, src, dst, grp, text = "CSBK $name: $src → ${if (grp) "TG " else ""}$dst, блоков ${b[3]}")
                return
            }
            text = "CSBK $name: $src → $dst"
        } else if (fid == 0 && op in CSBK_T3) {
            src = 0; dst = 0
            text = "CSBK ${CSBK_T3[op]}: ${hex(b, 2, 10)}"
        } else {
            src = 0; dst = 0
            text = String.format("CSBK 0x%02X FID 0x%02X: ", op, fid) + hex(b, 2, 10)
        }
        emit("csbk", st.n, st.cc, src, dst, false, text = text)
    }

    private fun onHeader(st: Slot, lc: Lc) {
        if (st.active && st.lcKnown && lc.src == st.src && lc.dst == st.dst) return
        if (st.active) callEnd(st, null, "конец вызова")
        callBegin(st)
        applyLc(st, lc)
    }

    private fun applyLc(st: Slot, lc: Lc) {
        if (lc.flco != 0 && lc.flco != 3) return
        st.src = lc.src; st.dst = lc.dst; st.group = lc.group
        st.encrypted = st.encrypted || lc.encrypted
        st.lcKnown = true
        if (!st.announced && confirmed) announce(st)
    }

    private fun who(st: Slot) = "${if (st.src != 0) st.src else "?"} → ${if (st.group) "TG " else ""}${if (st.dst != 0) st.dst else "?"}"

    private fun announce(st: Slot) {
        st.announced = true
        val kind = if (st.group) "групповой" else "индивидуальный"
        emit("call_start", st.n, st.cc, st.src, st.dst, st.group, st.encrypted,
            "$kind вызов ${who(st)}${if (st.encrypted) ", шифрован" else ""}")
    }

    private fun callBegin(st: Slot) {
        val pi = st.pi
        st.clear()
        st.active = true
        st.encrypted = pi
        st.voice.reset()
    }

    private fun callEnd(st: Slot, lc: Lc?, why: String?) {
        if (lc != null && (lc.flco == 0 || lc.flco == 3)) {
            st.src = lc.src; st.dst = lc.dst; st.group = lc.group
            st.encrypted = st.encrypted || lc.encrypted
        }
        val junk = !st.announced && !st.lcKnown && st.sf < 2
        if ((confirmed || st.announced) && !junk) {
            if (!st.announced) announce(st)
            emit("call_end", st.n, st.cc, st.src, st.dst, st.group, st.encrypted, "${why ?: "конец вызова"} ${who(st)}")
        }
        val src = st.src; val dst = st.dst; val g = st.group; val enc = st.encrypted
        st.clear()
        st.src = src; st.dst = dst; st.group = g; st.encrypted = enc
        if (audioSlot == st.n) audioSlot = null
    }

    private fun onVoice(st: Slot, burst: IntArray, isSync: Boolean, conf: Boolean): Boolean {
        var valid = false
        if (!st.active) callBegin(st)
        st.idle = 0
        if (isSync) {
            st.vf = 0
            st.sf++
            st.last = "Голос"
            if (st.sf >= 3 && !st.announced && conf) announce(st)
        } else {
            st.vf = (st.vf ?: 0) + 1
            val emb = (bitsInt(burst, 108, 116) shl 8) or bitsInt(burst, 148, 156)
            val (m, d) = DmrCodes.nearest(DmrCodes.QR1676_TAB, emb, if (conf) 1 else 0)
            if (m >= 0) {
                val cc = m shr 3; val pi = (m shr 2) and 1; val lcss = m and 3
                if (st.cc == null || cc == st.cc || !st.lcKnown) {
                    valid = true
                    st.badEmb = 0
                    st.cc = cc
                    stats["emb_ok"] = stats["emb_ok"]!! + 1
                    if (pi == 1 && d == 0 && !st.encrypted) st.encrypted = true
                    val frag = burst.copyOfRange(116, 148)
                    val fr = st.frags
                    when {
                        lcss == 1 -> st.frags = mutableListOf(frag)
                        lcss == 3 && fr != null && fr.size < 3 -> fr.add(frag)
                        lcss == 2 && fr != null && fr.size == 3 -> {
                            fr.add(frag)
                            val lc = DmrCodes.embLcDecode(fr.flatMap { it.toList() }.toIntArray())
                            st.frags = null
                            if (lc != null) { val l = parseLc(lc); if (l.flco == 0 || l.flco == 3) applyLc(st, l) }
                        }
                        lcss != 0 -> st.frags = null
                    }
                } else st.badEmb++
            } else st.badEmb++
            if (st.badEmb >= 4) {
                callEnd(st, null, "конец вызова (сигнал пропал)")
                st.vf = null
                return false
            }
        }
        stats["voice"] = stats["voice"]!! + 1
        if (conf || valid) voiceAudio(st, burst)
        return valid
    }

    private fun voiceAudio(st: Slot, burst: IntArray) {
        if ((slotFilter != 0 && slotFilter != st.n) || st.encrypted) return
        val other = slots[3 - st.n]!!
        if (audioSlot == null || (audioSlot != st.n && !other.active)) audioSlot = st.n
        if (audioSlot != st.n) return
        val frames = listOf(burst.copyOfRange(0, 72), burst.copyOfRange(72, 108) + burst.copyOfRange(156, 192),
            burst.copyOfRange(192, 264))
        val pcm = FloatArray(480)
        val out = FloatArray(160)
        for ((i, f) in frames.withIndex()) {
            st.voice.decode(ambeDeinterleave(f), out)
            for (j in 0 until 160) pcm[160 * i + j] = out[j].coerceIn(-1f, 1f)
        }
        pushAudio(pcm)
    }

    // ---------------------------------------------------------------- звук
    private fun pushAudio(pcm8: FloatArray) {
        val n = pcm8.size * 6
        val m = up.size - 1
        val buf = DoubleArray(m + n)
        System.arraycopy(upHist, 0, buf, 0, m)
        for (i in pcm8.indices) buf[m + 6 * i] = pcm8[i].toDouble()
        val outA = FloatArray(n)
        for (i in 0 until n) {
            var s = 0.0
            // в окне только каждый 6-й отсчёт ненулевой — но для простоты считаем полную свёртку
            for (k in up.indices) s += up[k] * buf[m + i - k]
            outA[i] = s.toFloat()
        }
        System.arraycopy(buf, n, upHist, 0, m)
        audio.addLast(outA)
        audioLen += n
        val cap = (0.6 * FS).toInt()
        while (audioLen > cap && audio.size > 1) { audioLen -= audio.removeFirst().size - audioHead; audioHead = 0 }
    }

    private fun pullAudio(n: Int): FloatArray {
        val out = FloatArray(n)
        if (!playing && audioLen >= (0.12 * FS).toInt()) playing = true
        if (!playing) {
            if (audioLen > 0 && slots.values.none { it.active }) playing = true   // хвост вызова — доиграть
            else return out
        }
        var pos = 0
        while (pos < n && audio.isNotEmpty()) {
            val a = audio.first()
            val k = min(n - pos, a.size - audioHead)
            System.arraycopy(a, audioHead, out, pos, k)
            pos += k
            audioHead += k
            audioLen -= k
            if (audioHead == a.size) { audio.removeFirst(); audioHead = 0 }
        }
        if (audio.isEmpty()) playing = false
        return out
    }

    private fun emit(type: String, slot: Int, cc: Int?, src: Int = 0, dst: Int = 0, group: Boolean = true,
                     encrypted: Boolean = false, text: String = "") {
        val st = slots[slot]
        val enc = if (st != null && (type == "call_start" || type == "call_end")) encrypted || st.encrypted else encrypted
        events.add(DmrEvent(type, slot, cc ?: -1, src, dst, group, enc, text))
    }
}

/** Генератор синтетического DMR — для тестов и тестового эфира (как генератор в rtltv/dmr.py). */
object DmrGen {
    val SYNC_BITS = Array(4) { intBits(DmrDecoder.SYNC_HEX[it], 48) }
    /** У данных синхрослово инвертировано по символам: меняется старший бит каждого дибита. */
    val SYNC_BITS_DATA = Array(4) { f -> IntArray(48) { i -> if (i % 2 == 0) 1 - SYNC_BITS[f][i] else SYNC_BITS[f][i] } }

    fun makeLc(src: Int, dst: Int, group: Boolean = true, so: Int = 0, fid: Int = 0) = intArrayOf(if (group) 0 else 3, fid, so,
        (dst shr 16) and 255, (dst shr 8) and 255, dst and 255, (src shr 16) and 255, (src shr 8) and 255, src and 255)

    fun cach(slot: Int, at: Int = 1): IntArray {
        val tact = intArrayOf(at, if (slot == 2) 1 else 0, 0, 0)
        val full = tact + DmrCodes.ham743(tact)
        val bits = IntArray(24)
        for ((i, p) in intArrayOf(0, 4, 8, 12, 14, 18, 22).withIndex()) bits[p] = full[i]
        return bits
    }

    fun dataBurst(cc: Int, dt: Int, payload96: IntArray, fam: Int = DmrDecoder.FAM_BS): IntArray {
        val info = DmrCodes.bptcEncode(payload96)
        val st = intBits(DmrCodes.golay2087((cc shl 4) or dt).toLong(), 20)
        return info.copyOfRange(0, 98) + st.copyOfRange(0, 10) + SYNC_BITS_DATA[fam] + st.copyOfRange(10, 20) + info.copyOfRange(98, 196)
    }

    fun lcBurst(cc: Int, lc9: IntArray, terminator: Boolean = false, fam: Int = DmrDecoder.FAM_BS): IntArray {
        val mask = if (terminator) 0x99 else 0x96
        val par = DmrCodes.rs129Parity(lc9)
        return dataBurst(cc, if (terminator) 2 else 1, DmrCodes.bytesBits(lc9 + IntArray(3) { par[it] xor mask }), fam)
    }

    fun idleBurst(cc: Int, fam: Int = DmrDecoder.FAM_BS) = dataBurst(cc, 9,
        DmrCodes.bytesBits(intArrayOf(0xFF, 0x83, 0xDF, 0x17, 0x32, 0x09, 0x4E, 0x1C, 0xEF, 0x0A, 0x65, 0x00)), fam)

    fun csbkBurst(cc: Int, blk10: IntArray, fam: Int = DmrDecoder.FAM_BS): IntArray {
        val crc = DmrCodes.crcCcitt(blk10) xor 0xA5A5
        return dataBurst(cc, 3, DmrCodes.bytesBits(blk10 + intArrayOf(crc shr 8, crc and 255)), fam)
    }

    fun voiceBurst(frames: List<IntArray>, idx: Int, cc: Int, embFrag: IntArray? = null, lcss: Int = 0, pi: Int = 0,
                   fam: Int = DmrDecoder.FAM_BS): IntArray {
        val v = frames[0] + frames[1] + frames[2]
        val mid = if (idx == 0) SYNC_BITS[fam] else {
            val emb = intBits(DmrCodes.qr1676((cc shl 3) or (pi shl 2) or lcss).toLong(), 16)
            emb.copyOfRange(0, 8) + (embFrag ?: IntArray(32)) + emb.copyOfRange(8, 16)
        }
        return v.copyOfRange(0, 108) + mid + v.copyOfRange(108, 216)
    }

    fun voiceCall(cc: Int, lc9: IntArray, frames: List<IntArray>, superframes: Int, headers: Int = 2, terms: Int = 2,
                  fam: Int = DmrDecoder.FAM_BS): List<IntArray> {
        val out = ArrayList<IntArray>()
        repeat(headers) { out.add(lcBurst(cc, lc9, false, fam)) }
        val emb = DmrCodes.embLcEncode(lc9)
        var k = 0
        repeat(superframes) {
            for (idx in 0 until 6) {
                val fr = List(3) { j -> frames[(k + j) % frames.size] }
                k += 3
                if (idx in 1..4) out.add(voiceBurst(fr, idx, cc, emb.copyOfRange(32 * (idx - 1), 32 * idx),
                    intArrayOf(1, 3, 3, 2)[idx - 1], 0, fam))
                else out.add(voiceBurst(fr, idx, cc, null, 0, 0, fam))
            }
        }
        repeat(terms) { out.add(lcBurst(cc, lc9, true, fam)) }
        return out
    }

    fun bsStream(slot1: List<IntArray>, slot2: List<IntArray>): IntArray {
        val n = max(slot1.size, slot2.size)
        val bits = ArrayList<Int>()
        for (i in 0 until n) for ((s, lst) in listOf(1 to slot1, 2 to slot2)) {
            bits.addAll(cach(s).toList()); bits.addAll((if (i < lst.size) lst[i] else idleBurst(1)).toList())
        }
        return bits.toIntArray()
    }

    /** Биты → мгновенная частота (Гц) 4FSK с RRC-формированием, 48 кГц. */
    fun modulate(bits: IntArray, offsetHz: Double = 0.0): FloatArray {
        val nsym = bits.size / 2
        val upS = DoubleArray(nsym * DmrDecoder.SPS)
        for (i in 0 until nsym) upS[i * DmrDecoder.SPS] = DmrDecoder.LEVEL_OF_DIBIT[bits[2 * i] * 2 + bits[2 * i + 1]] * (DmrDecoder.DEV3 / 3.0)
        val h0 = DmrDecoder.rrc()
        // нормировка: максимум свёртки с приёмным фильтром = 1
        val g = DoubleArray(h0.size + DmrDecoder.H_RX.size - 1)
        for (i in h0.indices) for (j in DmrDecoder.H_RX.indices) g[i + j] += h0[i] * DmrDecoder.H_RX[j]
        val gmax = g.max()
        val h = DoubleArray(h0.size) { h0[it] / gmax }
        val d = (h.size - 1) / 2
        return FloatArray(upS.size) { n ->
            var s = 0.0
            val idx = n + d
            for (k in h.indices) { val j = idx - k; if (j >= 0 && j < upS.size) s += h[k] * upS[j] }
            (s + offsetHz).toFloat()
        }
    }

    /** Набор AMBE-кадров «тон/гласная» с небольшим вибрато по высоте. */
    fun toneFrames(b0: Int, n: Int = 6, b2: Int = 17) = List(n) { DmrCodes.ambeEncode(DmrCodes.ambeParams(b0 + (it % 3) - 1, 0, b2, 0, 0)) }

    /** Синтетический эфир DMR (BS, CC=1), ~11 с: групповой вызов в слоте 1, индивидуальный в слоте 2. */
    fun testSignal(): FloatArray {
        val cc = 1
        val idle = List(200) { idleBurst(cc) }
        val parts = ArrayList<FloatArray>()
        for ((slot, lc, b0) in listOf(Triple(1, makeLc(2501234, 91, true), 40), Triple(2, makeLc(2507777, 2501234, false), 25))) {
            val call = voiceCall(cc, lc, toneFrames(b0), superframes = 10)
            val busy = idle.subList(0, 4) + call + idle.subList(0, 8)
            val free = idle.subList(0, busy.size)
            parts.add(modulate(if (slot == 1) bsStream(busy, free) else bsStream(free, busy)))
            parts.add(FloatArray(FS_PAUSE))
        }
        val out = FloatArray(parts.sumOf { it.size })
        var p = 0
        for (a in parts) { System.arraycopy(a, 0, out, p, a.size); p += a.size }
        return out
    }

    private const val FS_PAUSE = DmrDecoder.FS
}
