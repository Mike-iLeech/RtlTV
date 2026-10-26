package io.github.mikeileech.sdr4noob.lrpt

import io.github.mikeileech.sdr4noob.i18n.tr
import kotlin.math.abs

/** Сводка приёма (stats() в lrpt.py). */
data class LrptStats(
    val carrier: Double, val residualHz: Double, val omega: Double, val sync: Boolean, val frames: Int,
    val framesOk: Int, val rsPct: Double, val rsFixed: Int, val snrDb: Double, val nrzm: Boolean, val hyp: Int?,
    val packets: Int = 0, val lines: Int = 0, val channels: List<Int> = emptyList(), val scid: Int? = null,
    val mode: String = "", val marker: Double? = null,
)

/** Общая часть приёмников PSK: демодулятор, грубый поиск частоты, «передачи» Костаса, кадры CADU. */
open class PskReceiver(fs: Double, rs: Double, mode: String, nrzm: Boolean? = null, private val maxHz: Double = 10000.0) {
    val demod = Demodulator(fs, rs, mode)
    val frames = FrameDecoder(nrzm)
    private var unlockedSyms = 0L
    var coarseHz = 0.0
        private set
    var snrDb = 0.0
        private set

    /** последние точки созвездия (I, Q…), до 1500 */
    @Volatile
    var lastCons = FloatArray(0)
        private set
    private var acq = true
    private var wide: Boolean? = null

    /** обратное перемежение режима 80k */
    var dint: Deinterleaver80k? = null

    fun setOffset(hz: Double) = demod.setOffset(hz)

    /** IQ (re, im) → кадры. */
    fun feedFrames(iq: FloatArray, wantCons: Boolean = true): List<Frame> {
        val x = demod.filter(iq)
        if (x.isEmpty()) return emptyList()
        var st = demod.state()
        val needAcq = acq || (!frames.locked && unlockedSyms > 2.0 * demod.rs)
        if (needAcq && demod.mixedN >= demod.fs * 0.3) {
            val all = FloatArray(demod.mixedN * 2)
            var o = 0
            for (b in demod.mixed) { System.arraycopy(b, 0, all, o, b.size); o += b.size }
            val (c, snr) = Lrpt.coarseOffset(all, demod.fs, demod.rs, maxHz)
            coarseHz = c
            snrDb = snr
            if (snr > -3.0 && (acq || abs(c - st.residualHz) > 300)) demod.setResidual(c)
            acq = false
            unlockedSyms = 0
        }
        val w = !frames.locked
        if (w != wide) {          // «переключение передач» петли Костаса
            wide = w
            demod.setCostasBw(if (w) 0.012 else 0.005)
        }
        val n0 = demod.nsym
        val out = demod.processFs2(x, wantCons)
        st = demod.state()
        if (st.lock < 0.3 && !frames.locked) unlockedSyms += demod.nsym - n0 else unlockedSyms = 0
        val cons = out.cons
        if (cons != null && cons.isNotEmpty()) lastCons = cons.copyOfRange(maxOf(0, cons.size - 3000), cons.size)
        var soft = out.soft
        dint?.let { soft = it.push(soft) }
        return frames.push(soft)
    }

    fun finishFrames(): List<Frame> = frames.flush()

    fun baseStats(): LrptStats {
        val st = demod.state()
        val f = frames
        return LrptStats(st.lock, st.residualHz + demod.offsetHz, st.omega, f.synced, f.frames, f.framesOk, f.rsRatio(),
            f.rsFixed, snrDb, f.nrzm, f.hyp)
    }

    fun close() = demod.close()
}

/**
 * Режим «80k с перемежением» (SatDump meteor_m2-x_lrpt_80k): каждые 80 бит — маркер 0x27 + 72 бита данных,
 * свёрточный перемежитель 36 ветвей × 2048·36 бит. out[j] = in[j − (35 − j mod 36)·36·2048].
 */
class Deinterleaver80k {
    companion object {
        const val STRIDE = 80
        const val MARK_LEN = 8
        const val BRANCHES = 36
        const val DELAY = 2048
        const val MARKER = 0x27
        const val SPAN = 35 * 36 * 2048
        private val EMPTY = ByteArray(0)
        val MARK = ByteArray(8) { ((MARKER ushr (7 - it)) and 1).toByte() }

        /** 8 вариантов неоднозначности QPSK: поворот на 90°·(r & 3), сопряжение (r & 4). */
        fun rot(soft: ByteArray, r: Int, from: Int = 0, len: Int = soft.size - from): ByteArray {
            val np = len / 2
            val out = ByteArray(np * 2)
            for (i in 0 until np) {
                var a = soft[from + 2 * i].toInt()
                var b = soft[from + 2 * i + 1].toInt()
                if (r and 4 != 0) b = -b
                repeat(r and 3) { val t = a; a = -b; b = t }
                out[2 * i] = a.coerceIn(-127, 127).toByte()
                out[2 * i + 1] = b.coerceIn(-127, 127).toByte()
            }
            return out
        }

        /** Передатчик (для тестов): перемежение и вставка маркеров; длина coded кратна 72. */
        fun interleave(coded: ByteArray): ByteArray {
            val n = coded.size
            val sent = ByteArray(n)
            for (i in 0 until n) {
                val src = i.toLong() - (i % 36).toLong() * (36 * DELAY)
                sent[i] = if (src >= 0) coded[src.toInt()] else 0
            }
            val m = n / 72
            val out = ByteArray(m * 80)
            for (k in 0 until m) {
                System.arraycopy(MARK, 0, out, k * 80, 8)
                System.arraycopy(sent, k * 72, out, k * 80 + 8, 72)
            }
            return out
        }
    }

    private var raw = EMPTY
    private var t = EMPTY
    private var carry = EMPTY
    var locked = false
        private set
    private var rotN = 0
    // история данных без маркеров: data[0 until dataLen] соответствует абсолютным номерам base…
    private var data = ByteArray(1 shl 16)
    private var dataLen = 0
    private var base = 0L
    private var total = 0L
    private var kNext = SPAN.toLong()
    var markerOk = 0.0
        private set
    var relocks = 0
        private set

    private fun search(r: ByteArray): Triple<Int, Int, Int>? {
        val m = r.size / STRIDE - 2
        if (m < 40) return null
        var best: Triple<Int, Int, Int>? = null
        for (rr in 0 until 8) {
            val s = rot(r, rr)
            for (o in 0 until STRIDE) {
                var score = 0
                for (row in 0 until m) {
                    val p = o + row * STRIDE
                    for (i in 0 until 8) if ((if (s[p + i] > 0) 1 else 0) == MARK[i].toInt()) score++
                }
                if (best == null || score > best.first) best = Triple(score, rr, o)
            }
        }
        if (best!!.first < 0.85 * 8 * m) return null
        return best
    }

    private fun append(src: ByteArray, from: Int, n: Int) {
        if (dataLen + n > data.size) data = data.copyOf(maxOf(data.size * 2, dataLen + n))
        System.arraycopy(src, from, data, dataLen, n)
        dataLen += n
    }

    fun push(soft: ByteArray): ByteArray {
        if (!locked) {
            raw = cat(raw, soft)
            if (raw.size < STRIDE * 64) return EMPTY
            val found = search(raw.copyOf(minOf(raw.size, STRIDE * 120)))
            if (found == null) {
                if (raw.size > STRIDE * 160) raw = raw.copyOfRange(raw.size - STRIDE * 8, raw.size)
                return EMPTY
            }
            rotN = found.second
            val o = found.third
            val even = raw.size / 2 * 2
            carry = raw.copyOfRange(even, raw.size)
            val rt = rot(raw, rotN, 0, even)
            t = rt.copyOfRange(minOf(o, rt.size), rt.size)
            raw = EMPTY
            locked = true
            relocks++
        } else {
            val s2 = cat(carry, soft)
            val even = s2.size / 2 * 2
            carry = s2.copyOfRange(even, s2.size)
            t = cat(t, rot(s2, rotN, 0, even))
        }
        val m = t.size / STRIDE
        if (m > 0) {
            var good = 0
            for (row in 0 until m) for (i in 0 until 8) {
                if ((if (t[row * STRIDE + i] > 0) 1 else 0) == MARK[i].toInt()) good++
            }
            val ok = good.toDouble() / (m * 8)
            markerOk = if (markerOk != 0.0) 0.8 * markerOk + 0.2 * ok else ok
            if (markerOk < 0.7) {                  // маркер потерян — ищем заново (поворот/сдвиг)
                locked = false
                markerOk = 0.0
                raw = t
                t = EMPTY
                return EMPTY
            }
            for (row in 0 until m) append(t, row * STRIDE + 8, STRIDE - 8)
            t = t.copyOfRange(m * STRIDE, t.size)
            total += m.toLong() * (STRIDE - 8)
        }
        if (total > kNext) {
            val n = (total - kNext).toInt()
            val out = ByteArray(n)
            for (i in 0 until n) {
                val ks = kNext + i
                val idx = ks - (35 - ks % 36) * (36 * DELAY) - base
                out[i] = data[idx.toInt()]
            }
            kNext = total
            val keep = SPAN + 64
            if (dataLen > keep * 2) {
                val drop = dataLen - keep
                System.arraycopy(data, drop, data, 0, keep)
                dataLen = keep
                base += drop
            }
            return out
        }
        return EMPTY
    }
}

/** IQ → изображения МСУ-МР. mode80k = true — 80 000 символ/с с перемежением (иначе 72 000 без перемежения). */
class LrptReceiver(fs: Double, oqpsk: Boolean = true, rs: Double? = null, nrzm: Boolean? = null, val mode80k: Boolean = false) :
    PskReceiver(fs, rs ?: (if (mode80k) 80000.0 else Lrpt.SYMBOL_RATE.toDouble()), if (oqpsk) "oqpsk" else "qpsk", nrzm) {
    val demux = Demux(5)
    val imager = Imager()

    init {
        if (mode80k) dint = Deinterleaver80k()
    }

    val modeText: String
        get() = "${(demod.rs / 1000).toInt()}k ${if (demod.oqpsk) "OQPSK" else "QPSK"}" + (if (mode80k) " + " + tr("перемежение") else "")

    private fun use(fr: List<Frame>) {
        for (f in fr) if (f.ok) for (p in demux.push(f.data.copyOf(892))) imager.push(p.apid, p.seq, p.payload)
    }

    fun feed(iq: FloatArray, wantCons: Boolean = true) = use(feedFrames(iq, wantCons))

    fun finish() = use(finishFrames())

    fun stats(): LrptStats = baseStats().copy(packets = demux.packets, lines = imager.lines(), channels = imager.channels(),
        scid = demux.scid, mode = modeText, marker = dint?.markerOk)
}

/** Автоопределение режима: параллельно 72k OQPSK и 80k OQPSK с перемежением, пока один не наберёт кадров. */
class AutoLrptReceiver(fs: Double, modes: List<Pair<Boolean, Boolean>> = listOf(Pair(true, false), Pair(true, true))) {
    var rxs: List<LrptReceiver> = modes.map { LrptReceiver(fs, it.first, mode80k = it.second) }
        private set
    var chosen: LrptReceiver? = null
        private set

    val rx: LrptReceiver
        get() = chosen ?: rxs.maxWith(compareBy<LrptReceiver>({ it.frames.framesOk }, { it.demod.state().lock }))

    fun setOffset(hz: Double) = rxs.forEach { it.setOffset(hz) }

    fun feed(iq: FloatArray, wantCons: Boolean = true) {
        chosen?.let { it.feed(iq, wantCons); return }
        for (r in rxs) r.feed(iq, wantCons)
        val good = rxs.filter { it.frames.framesOk >= 5 && it.frames.framesOk >= 0.6 * it.frames.frames }
        if (good.isNotEmpty()) {
            chosen = good[0]
            for (r in rxs) if (r !== good[0]) r.close()
            rxs = listOf(good[0])
        }
    }

    fun finish() = rxs.forEach { it.finish() }

    fun stats(): LrptStats {
        val st = rx.stats()
        return st.copy(mode = if (chosen != null) rx.modeText else tr("поиск режима: {modes}", "modes" to rxs.joinToString(" / ") { it.modeText }))
    }
}

object LrptFile {
    /** Смещение сигнала от центра записи по имени файла SDR4NOOB «…_<fs>Hz_<центр>.cu8» (или null). */
    fun offsetFromName(name: String, fs: Double, freqs: DoubleArray = doubleArrayOf(137.9e6, 137.1e6)): Double? {
        val m = Regex("""_(\d{5,8})Hz_(\d{7,11})""").find(name.substringAfterLast('/').substringAfterLast('\\')) ?: return null
        val center = m.groupValues[2].toDouble()
        for (f in freqs) if (abs(f - center) < fs / 2 - 50e3) return f - center
        return null
    }

    /**
     * Декодирование записи блоками (decode_file без поиска сигнала по файлу): oqpsk/mode80k = null — режимы
     * перебираются параллельно (72k OQPSK, 72k QPSK, 80k OQPSK с перемежением), пока один не победит.
     * blocks — блоки IQ (re, im). progress(отсчётов обработано, лучший приёмник).
     */
    fun decode(
        blocks: Sequence<FloatArray>, fs: Double, offsetHz: Double = 0.0, oqpsk: Boolean? = null, mode80k: Boolean? = null,
        cancel: (() -> Boolean)? = null, progress: ((Long, LrptReceiver) -> Unit)? = null,
    ): LrptReceiver {
        val oqs = if (oqpsk != null) listOf(oqpsk) else listOf(true, false)
        val m80 = if (mode80k != null) listOf(mode80k) else listOf(false, true)
        val modes = m80.flatMap { m -> oqs.filter { o -> !(m && !o) }.map { o -> Pair(o, m) } }   // 80k бывает только OQPSK
        var rxs = modes.map { (o, m) -> LrptReceiver(fs, o, mode80k = m).also { it.setOffset(offsetHz) } }
        var done = 0L
        for (blk in blocks) {
            if (cancel != null && cancel()) break
            for (r in rxs) r.feed(blk, wantCons = false)
            done += blk.size / 2
            if (rxs.size > 1) {
                val good = rxs.filter { it.frames.framesOk >= 5 && it.frames.framesOk >= 0.6 * it.frames.frames }
                if (good.isNotEmpty()) {
                    for (r in rxs) if (r !== good[0]) r.close()
                    rxs = listOf(good[0])
                }
            }
            progress?.invoke(done, rxs.maxBy { it.frames.framesOk })
        }
        val best = rxs.maxBy { it.frames.framesOk }
        best.finish()
        return best
    }
}
