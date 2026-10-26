package io.github.mikeileech.sdr4noob.lrpt

import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.sqrt

/** CCSDS-пакет: APID, счётчик, данные (после 6-байтного заголовка). */
class Packet(val apid: Int, val seq: Int, val payload: ByteArray)

/** VCDU (1020 байт без ASM, используются первые 892) → CCSDS-пакеты VCID 5. */
class Demux(private val vcid: Int = 5) {
    private var pending: ByteArray? = null
    private var lastCount: Int? = null
    var scid: Int? = null
        private set
    var packets = 0
        private set

    fun push(b: ByteArray): List<Packet> {
        val res = ArrayList<Packet>()
        val b0 = b[0].toInt() and 0xFF
        val b1 = b[1].toInt() and 0xFF
        val ver = b0 shr 6
        val sc = ((b0 and 0x3F) shl 2) or (b1 shr 6)
        val vc = b1 and 0x3F
        if (ver != 1 || vc != vcid) return res
        scid = sc
        val count = ((b[2].toInt() and 0xFF) shl 16) or ((b[3].toInt() and 0xFF) shl 8) or (b[4].toInt() and 0xFF)
        val lc = lastCount
        if (lc != null && count != ((lc + 1) and 0xFFFFFF)) pending = null
        lastCount = count
        val fhp = ((b[8].toInt() and 0x07) shl 8) or (b[9].toInt() and 0xFF)
        val zone = b.copyOfRange(10, 10 + 882)
        val pd = pending
        if (pd != null) {
            if (fhp == 0x7FF) {
                pending = parse(cat(pd, zone), res, true)
                return res
            }
            parse(cat(pd, zone.copyOf(minOf(fhp, zone.size))), res, false)
            pending = null
        }
        if (fhp == 0x7FF || fhp >= 882) return res
        pending = parse(zone.copyOfRange(fhp, zone.size), res, true)
        return res
    }

    private fun parse(buf: ByteArray, res: MutableList<Packet>, partialOk: Boolean): ByteArray? {
        var i = 0
        val n = buf.size
        while (n - i >= 6) {
            val ln = (((buf[i + 4].toInt() and 0xFF) shl 8) or (buf[i + 5].toInt() and 0xFF)) + 1
            val tot = 6 + ln
            if (n - i < tot) break
            val apid = ((buf[i].toInt() and 0x07) shl 8) or (buf[i + 1].toInt() and 0xFF)
            val seq = ((buf[i + 2].toInt() and 0x3F) shl 8) or (buf[i + 3].toInt() and 0xFF)
            if (apid != 2047) {
                res.add(Packet(apid, seq, buf.copyOfRange(i + 6, i + tot)))
                packets++
            }
            i += tot
        }
        return if (partialOk) buf.copyOfRange(i, n) else null
    }
}

/** Полутоновый снимок: px — height×width байт (0..255 без знака). */
class LrptGray(val width: Int, val height: Int, val px: ByteArray) {
    operator fun get(y: Int, x: Int): Int = px[y * width + x].toInt() and 0xFF
}

/** RGB-композит из трёх каналов. */
class LrptRgb(val width: Int, val height: Int, val r: ByteArray, val g: ByteArray, val b: ByteArray) {
    /** Пиксели ARGB для Bitmap. */
    fun argb(): IntArray = IntArray(width * height) {
        (0xFF shl 24) or ((r[it].toInt() and 0xFF) shl 16) or ((g[it].toInt() and 0xFF) shl 8) or (b[it].toInt() and 0xFF)
    }
}

/** МКУ (JPEG-подобное сжатие МСУ-МР): стандартные таблицы Хаффмана JPEG, DCT 8×8, таблица квантования по qf. */
object Mcu {
    private val QT = intArrayOf(16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
        14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
        18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
        49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99)

    /** позиция в зигзаге для каждой естественной позиции */
    private val ZZ = intArrayOf(0, 1, 5, 6, 14, 15, 27, 28, 2, 4, 7, 13, 16, 26, 29, 42,
        3, 8, 12, 17, 25, 30, 41, 43, 9, 11, 18, 24, 31, 40, 44, 53,
        10, 19, 23, 32, 39, 45, 52, 54, 20, 22, 33, 38, 46, 51, 55, 60,
        21, 34, 37, 47, 50, 56, 59, 61, 35, 36, 48, 49, 57, 58, 62, 63)

    private val DC_BITS = intArrayOf(0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0)
    private val DC_VALS = IntArray(12) { it }
    private val AC_BITS = intArrayOf(0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D)
    private val AC_VALS = intArrayOf(
        0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06, 0x13, 0x51, 0x61, 0x07,
        0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08, 0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0,
        0x24, 0x33, 0x62, 0x72, 0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
        0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49,
        0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69,
        0x6A, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
        0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7,
        0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5,
        0xC6, 0xC7, 0xC8, 0xC9, 0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
        0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8,
        0xF9, 0xFA)

    /** символ → (код, длина) */
    private fun huffCodes(bits: IntArray, vals: IntArray): Map<Int, Pair<Int, Int>> {
        val codes = LinkedHashMap<Int, Pair<Int, Int>>()
        var code = 0
        var k = 0
        for (ln in 1..16) {
            repeat(bits[ln - 1]) {
                codes[vals[k]] = Pair(code, ln)
                k++
                code++
            }
            code = code shl 1
        }
        return codes
    }

    val DC_CODES = huffCodes(DC_BITS, DC_VALS)
    val AC_CODES = huffCodes(AC_BITS, AC_VALS)

    /** 16-битная таблица: индекс = следующие 16 бит → длина кода (0 — нет кода) и символ. */
    private class Lut(codes: Map<Int, Pair<Int, Int>>) {
        val len = IntArray(65536)
        val sym = IntArray(65536)

        init {
            for ((v, cl) in codes) {
                val (code, n) = cl
                val lo = code shl (16 - n)
                val hi = lo + (1 shl (16 - n))
                for (i in lo until hi) { len[i] = n; sym[i] = v }
            }
        }
    }

    private val dcLut by lazy { Lut(DC_CODES) }
    private val acLut by lazy { Lut(AC_CODES) }

    fun qtable(qf0: Int): DoubleArray {
        val qf = qf0.toDouble()
        val f = if (qf >= 20 && qf < 50) 5000.0 / qf else 200.0 - 2.0 * qf
        return DoubleArray(64) { maxOf(floor(f / 100.0 * QT[it] + 0.5), 1.0) }
    }

    private val M = Array(8) { u ->
        DoubleArray(8) { x -> (if (u == 0) sqrt(0.5) else 1.0) / 2.0 * cos((2 * x + 1) * u * PI / 16) }
    }

    /** Сжатые данные 14 МКУ (data[off:]) → (8×112 байт, число декодированных МКУ). */
    fun decode(data: ByteArray, off: Int, qf: Int): Pair<ByteArray, Int> {
        val dl = dcLut
        val al = acLut
        val nbytes = data.size - off
        val nb = nbytes * 8
        // 16 бит, начиная с бита pos (за концом — нули)
        fun w(pos: Int): Int {
            val byte = pos shr 3
            var v = 0
            for (k in 0 until 3) {
                val bi = byte + k
                v = (v shl 8) or (if (bi < nbytes) data[off + bi].toInt() and 0xFF else 0)
            }
            return (v ushr (8 - (pos and 7))) and 0xFFFF
        }
        var pos = 0
        var dc = 0
        val blocks = ArrayList<IntArray>()
        for (blk in 0 until 14) {
            if (pos >= nb) break
            var cw = w(pos)
            var n = dl.len[cw]
            if (n == 0) break
            val cat = dl.sym[cw]
            pos += n
            var v: Int
            if (cat != 0) {
                if (pos + cat > nb) break
                v = w(pos) ushr (16 - cat)
                if (v < (1 shl (cat - 1))) v -= (1 shl cat) - 1
                pos += cat
            } else v = 0
            dc += v
            val coef = IntArray(64)
            coef[0] = dc
            var k = 1
            var bad = false
            while (k < 64) {
                if (pos >= nb) { bad = true; break }
                cw = w(pos)
                n = al.len[cw]
                if (n == 0) { bad = true; break }
                val rs = al.sym[cw]
                pos += n
                if (rs == 0x00) break           // EOB
                val run = rs shr 4
                val size = rs and 15
                if (size == 0) { k += 16; continue }   // ZRL (F0)
                k += run
                if (pos + size > nb) { bad = true; break }
                var a = w(pos) ushr (16 - size)
                if (a < (1 shl (size - 1))) a -= (1 shl size) - 1
                pos += size
                if (k < 64) coef[k] = a
                k++
            }
            if (bad) break
            blocks.add(coef)
        }
        val out = ByteArray(8 * 112)
        if (blocks.isEmpty()) return Pair(out, 0)
        val qt = qtable(qf)
        val f = DoubleArray(64)
        for ((bi, z) in blocks.withIndex()) {
            for (i in 0 until 64) f[i] = z[ZZ[i]].toDouble() * qt[i]
            for (r in 0 until 8) for (c in 0 until 8) {
                var s = 0.0
                for (v in 0 until 8) for (u in 0 until 8) s += M[v][r] * f[v * 8 + u] * M[u][c]
                val px = (Math.rint(s) + 128).coerceIn(0.0, 255.0).toInt()
                out[r * 112 + bi * 8 + c] = px.toByte()
            }
        }
        return Pair(out, blocks.size)
    }
}

/** CCSDS-пакеты APID 64–69 → каналы МСУ-МР. */
class Imager {
    private data class Key(val base: Int, val mcun: Int)

    private val seg = LinkedHashMap<Int, LinkedHashMap<Key, ByteArray>>().also { m -> for (a in Lrpt.APIDS) m[a] = LinkedHashMap() }
    private var rollover = 0
    private var lastSeq: Int? = null
    var count = 0
        private set
    var qf = 0
        private set
    var version = 0
        private set
    var firstTime: Long? = null
        private set
    var lastTime: Long? = null
        private set

    fun push(apid: Int, seq: Int, p: ByteArray) {
        val sg = seg[apid] ?: return
        if (p.size < 15) return
        val mcun = p[8].toInt() and 0xFF
        if (p[9].toInt() != 0 || p[10].toInt() != 0 || (p[11].toInt() and 0xFF) != 0xFF || (p[12].toInt() and 0xFF) != 0xF0 ||
            mcun % 14 != 0 || mcun > 182) return
        val ls = lastSeq
        if (ls != null && seq < ls && ls - seq > 8192) rollover += 16384
        lastSeq = seq
        val q = p[13].toInt() and 0xFF
        val (img, n) = Mcu.decode(p, 14, q)
        if (n == 0) return
        val base = seq + rollover - mcun / 14
        sg[Key(base, mcun)] = img
        count++
        qf = q
        var ms = 0L
        for (i in 2 until 6) ms = (ms shl 8) or (p[i].toLong() and 0xFF)
        if (firstTime == null) firstTime = ms
        lastTime = ms
        version++
    }

    fun channels(): List<Int> = Lrpt.APIDS.filter { seg[it]!!.isNotEmpty() }

    fun lines(): Int {
        val l = layout() ?: return 0
        return (l.n1 - l.n0 + 1) * 8
    }

    private class Layout(val phase0: Int, val cyc: Int, val n0: Int, val n1: Int)

    private fun layout(): Layout? {
        val chans = channels()
        if (chans.isEmpty()) return null
        val cyc = 14 * chans.size + 1      // цикл передачи: по 14 пакетов на канал + телеметрия (обычно 43)
        val cnt = HashMap<Int, Int>()
        for (k in seg[chans[0]]!!.keys) cnt.merge(Math.floorMod(k.base, cyc), 1, Int::plus)
        // самая частая фаза; при равенстве — меньшая (в Python — порядок множества)
        val phase0 = cnt.entries.sortedWith(compareBy<Map.Entry<Int, Int>>({ -it.value }, { it.key })).first().key
        var n0 = Int.MAX_VALUE
        var n1 = Int.MIN_VALUE
        for (a in chans) for (k in seg[a]!!.keys) {
            val ln = Math.floorDiv(k.base - phase0, cyc)
            if (ln < n0) n0 = ln
            if (ln > n1) n1 = ln
        }
        if (n1 - n0 > 3000) n0 = n1 - 3000      // мусор в счётчиках — ограничим размер
        return Layout(phase0, cyc, n0, n1)
    }

    fun image(apid: Int): LrptGray? {
        val l = layout() ?: return null
        val sg = seg[apid]
        if (sg.isNullOrEmpty()) return null
        val h = (l.n1 - l.n0 + 1) * 8
        val img = ByteArray(h * Lrpt.WIDTH)
        for ((k, s) in sg) {
            val ln = Math.floorDiv(k.base - l.phase0, l.cyc) - l.n0
            if (ln < 0) continue
            val x = (k.mcun / 14) * 112
            for (r in 0 until 8) System.arraycopy(s, r * 112, img, (ln * 8 + r) * Lrpt.WIDTH + x, 112)
        }
        return LrptGray(Lrpt.WIDTH, h, img)
    }

    fun allImages(): Map<Int, LrptGray> = LinkedHashMap<Int, LrptGray>().also { m -> for (a in channels()) m[a] = image(a)!! }
}

object LrptView {
    /** combo — строка из трёх цифр каналов (1..6), например "123", "125", "221". */
    fun composite(images: Map<Int, LrptGray>, combo: String): LrptRgb? {
        val ch = ArrayList<LrptGray>()
        for (c in combo) {
            val a = 63 + (c - '0')
            ch.add(images[a] ?: return null)
        }
        val h = ch.minOf { it.height }
        val w = ch[0].width
        return LrptRgb(w, h, ch[0].px.copyOf(w * h), ch[1].px.copyOf(w * h), ch[2].px.copyOf(w * h))
    }

    /** Снимок для показа: канал ("1".."6", gray) или композит ("221", "125", "123", rgb). */
    class Preview(val key: String, val gray: LrptGray?, val rgb: LrptRgb?)

    fun preview(images: Map<Int, LrptGray>, key0: String, doStretch: Boolean = true): Preview {
        var key = key0
        if (images.isEmpty()) return Preview(key, null, null)
        if (key.length == 3) {
            var arr = composite(images, key)
            if (arr == null) {
                val have = images.keys.sorted().joinToString("") { (it - 63).toString() }
                var alt: String? = if ("125".all { it in have }) "125" else null
                if (alt == null && have.length >= 3) alt = "${have[1]}${have[1]}${have[0]}"
                arr = if (alt != null) composite(images, alt) else null
                key = alt ?: key
            }
            val a = arr
            if (a != null && doStretch) {
                fun st(p: ByteArray) = stretch(LrptGray(a.width, a.height, p)).px
                arr = LrptRgb(a.width, a.height, st(a.r), st(a.g), st(a.b))
            }
            return Preview(key, null, arr)
        }
        var g = images[63 + key.toInt()]
        if (g != null && doStretch) g = stretch(g)
        return Preview(key, g, null)
    }

    /** Растяжка уровней по процентилям 0,5/99,5 (по прореженной выборке, без нулей). */
    fun stretch(img: LrptGray): LrptGray {
        val size = img.px.size
        if (size == 0) return img
        val step = maxOf(1, size / 200000)
        val hist = IntArray(256)
        var i = 0
        while (i < size) {
            val v = img.px[i].toInt() and 0xFF
            if (v > 0) hist[v]++
            i += step
        }
        val nz = hist.sum()
        if (nz < 100) return img
        fun at(idx: Int): Double {
            var acc = 0
            for (v in 1 until 256) { acc += hist[v]; if (idx < acc) return v.toDouble() }
            return 255.0
        }
        fun pct(q: Double): Double {
            val vi = q * (nz - 1)
            val lo = floor(vi).toInt()
            val hi = minOf(lo + 1, nz - 1)
            val t = vi - lo
            val a = at(lo)
            val b = at(hi)
            val d = b - a
            return if (t >= 0.5) b - d * (1 - t) else a + d * t
        }
        val lo = pct(0.5 / 100)
        val hi = pct(99.5 / 100)
        if (hi <= lo) return img
        val k = 255.0 / (hi - lo)
        val lut = ByteArray(256) { ((it - lo) * k).coerceIn(0.0, 255.0).toInt().toByte() }
        return LrptGray(img.width, img.height, ByteArray(size) { lut[img.px[it].toInt() and 0xFF] })
    }
}
