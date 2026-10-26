package io.github.mikeileech.sdr4noob.dsp

/**
 * Коды DMR (ETSI TS 102 361) — перенос из rtltv/dmr.py: Golay(20,8), QR(16,7,6), Hamming(15,11)/(13,9)/
 * (16,11)/(7,4), RS(12,9) над GF(256), CRC-CCITT с масками, BPTC(196,96), встроенный LC (128 бит на
 * пакеты B–E), перестановка 72 бит AMBE+2 (таблицы DSD) и кодер AMBE для тестового сигнала.
 * Биты — IntArray из 0/1 (как списки битов в Python).
 */
object DmrCodes {
    fun intBits(v: Long, n: Int) = IntArray(n) { ((v shr (n - 1 - it)) and 1L).toInt() }
    fun bitsInt(b: IntArray, from: Int = 0, to: Int = b.size): Int {
        var v = 0
        for (i in from until to) v = (v shl 1) or b[i]
        return v
    }
    fun bitsBytes(b: IntArray) = IntArray(b.size / 8) { bitsInt(b, 8 * it, 8 * it + 8) }
    fun bytesBits(d: IntArray): IntArray {
        val out = IntArray(d.size * 8)
        for ((i, x) in d.withIndex()) for (j in 0 until 8) out[8 * i + j] = (x shr (7 - j)) and 1
        return out
    }

    /** Остаток от деления многочлена [v] на [g] степени [deg] над GF(2). */
    fun polymod(v0: Long, g: Long, deg: Int): Long {
        var v = v0
        for (bit in 63 - java.lang.Long.numberOfLeadingZeros(v) downTo deg) {
            if ((v shr bit) and 1L == 1L) v = v xor (g shl (bit - deg))
        }
        return v
    }

    fun golay2087(m: Int): Int {
        val r = polymod(m.toLong() shl 11, 0xC75, 11).toInt()
        val p = (Integer.bitCount(m) + Integer.bitCount(r)) and 1
        return (m shl 12) or (r shl 1) or p
    }

    fun qr1676(m: Int): Int {
        val r = polymod(m.toLong() shl 8, 0x139, 8).toInt()
        val p = (Integer.bitCount(m) + Integer.bitCount(r)) and 1
        return (m shl 9) or (r shl 1) or p
    }

    val GOLAY2087_TAB = IntArray(256) { golay2087(it) }
    val QR1676_TAB = IntArray(128) { qr1676(it) }

    /** Ближайшее кодовое слово: (сообщение, расстояние) или (−1, расстояние), если дальше [maxd]. */
    fun nearest(tab: IntArray, word: Int, maxd: Int): Pair<Int, Int> {
        var best = -1; var bd = 99
        for ((m, cw) in tab.withIndex()) {
            val d = Integer.bitCount(cw xor word)
            if (d < bd) { best = m; bd = d; if (d == 0) break }
        }
        return if (bd <= maxd) best to bd else -1 to bd
    }

    fun ham15113(d: IntArray) = intArrayOf(d[0] xor d[1] xor d[2] xor d[3] xor d[5] xor d[7] xor d[8],
        d[1] xor d[2] xor d[3] xor d[4] xor d[6] xor d[8] xor d[9],
        d[2] xor d[3] xor d[4] xor d[5] xor d[7] xor d[9] xor d[10],
        d[0] xor d[1] xor d[2] xor d[4] xor d[6] xor d[7] xor d[10])

    fun ham1393(d: IntArray) = intArrayOf(d[0] xor d[1] xor d[3] xor d[5] xor d[6],
        d[0] xor d[1] xor d[2] xor d[4] xor d[6] xor d[7],
        d[0] xor d[1] xor d[2] xor d[3] xor d[5] xor d[7] xor d[8],
        d[0] xor d[2] xor d[4] xor d[5] xor d[8])

    fun ham16114(d: IntArray) = ham15113(d) + intArrayOf(d[0] xor d[2] xor d[5] xor d[6] xor d[8] xor d[9] xor d[10])

    fun ham743(d: IntArray) = intArrayOf(d[0] xor d[1] xor d[2], d[1] xor d[2] xor d[3], d[0] xor d[1] xor d[3])

    private class Syn(val fn: (IntArray) -> IntArray, val k: Int, n: Int) {
        val tab = HashMap<Int, Int>()
        init {
            for (pos in 0 until n) {
                val e = IntArray(n); e[pos] = 1
                tab[key(e)] = pos
            }
        }
        fun key(w: IntArray): Int {
            val p = fn(w.copyOfRange(0, k))
            var s = 0
            for (i in p.indices) s = (s shl 1) or (p[i] xor w[k + i])
            return s
        }
    }
    private val SYN15 = Syn(::ham15113, 11, 15)
    private val SYN13 = Syn(::ham1393, 9, 13)
    private val SYN16 = Syn(::ham16114, 11, 16)

    /** Исправить одну ошибку на месте; true — слово (стало) корректно. */
    private fun fix(w: IntArray, syn: Syn): Boolean {
        val s = syn.key(w)
        if (s == 0) return true
        val pos = syn.tab[s] ?: return false
        w[pos] = w[pos] xor 1
        return true
    }

    // ---------- GF(256), RS(12,9) ----------
    private val GF_EXP = IntArray(512)
    private val GF_LOG = IntArray(256)
    init {
        var x = 1
        for (i in 0 until 255) {
            GF_EXP[i] = x; GF_LOG[x] = i
            x = x shl 1
            if (x and 0x100 != 0) x = x xor 0x11D
        }
        for (i in 255 until 512) GF_EXP[i] = GF_EXP[i - 255]
    }
    private fun gmul(a: Int, b: Int) = if (a == 0 || b == 0) 0 else GF_EXP[GF_LOG[a] + GF_LOG[b]]
    private fun gdiv(a: Int, b: Int) = if (a == 0) 0 else GF_EXP[Math.floorMod(GF_LOG[a] - GF_LOG[b], 255)]
    private val RS_G = intArrayOf(1, 14, 56, 64)

    fun rs129Parity(msg: IntArray): IntArray {
        var r = intArrayOf(0, 0, 0)
        for (b in msg) {
            val f = b xor r[0]
            r = intArrayOf(r[1] xor gmul(f, RS_G[1]), r[2] xor gmul(f, RS_G[2]), gmul(f, RS_G[3]))
        }
        return r
    }

    /** 12 байт → исправленные 12 байт или null. */
    fun rs129Check(cw: IntArray): IntArray? {
        val syn = IntArray(3)
        for ((i, j) in (1..3).withIndex()) {
            val a = GF_EXP[j]
            var s = 0
            for (b in cw) s = gmul(s, a) xor b
            syn[i] = s
        }
        if (syn.all { it == 0 }) return cw.copyOf()
        val (s1, s2, s3) = syn.toList()
        if (s1 == 0 || s2 == 0) return null
        val x = gdiv(s2, s1)
        val e = gdiv(s1, x)
        if (gmul(e, gmul(x, gmul(x, x))) != s3) return null
        val k = GF_LOG[x]
        if (k > 11) return null
        val out = cw.copyOf()
        out[11 - k] = out[11 - k] xor e
        return out
    }

    fun crcCcitt(data: IntArray, n: Int = data.size): Int {
        var crc = 0
        for (i in 0 until n) {
            crc = crc xor (data[i] shl 8)
            repeat(8) { crc = (if (crc and 0x8000 != 0) (crc shl 1) xor 0x1021 else crc shl 1) and 0xFFFF }
        }
        return crc xor 0xFFFF
    }

    fun crcOk(block: IntArray, mask: Int): Boolean {
        val got = ((block[10] shl 8) or block[11]) xor mask
        val c = crcCcitt(block, 10)
        return got == c || got == (c xor 0xFFFF)
    }

    // ---------- BPTC(196,96) ----------
    private val BPTC_DATA_POS = (4 until 12).toList() + (1..8).flatMap { r -> (1 + 15 * r until 12 + 15 * r).toList() }
    private val BPTC_PERM = IntArray(196) { (it * 181) % 196 }

    fun bptcEncode(bits96: IntArray): IntArray {
        val m = IntArray(196)
        for ((i, p) in BPTC_DATA_POS.withIndex()) m[p] = bits96[i]
        for (r in 0 until 9) {
            val row = m.copyOfRange(1 + 15 * r, 16 + 15 * r)
            val par = ham15113(row)
            for (i in 0 until 4) m[12 + 15 * r + i] = par[i]
        }
        for (c in 0 until 15) {
            val col = IntArray(9) { m[1 + c + 15 * it] }
            for ((i, p) in ham1393(col).withIndex()) m[1 + c + 15 * (9 + i)] = p
        }
        val raw = IntArray(196)
        for (a in 0 until 196) raw[BPTC_PERM[a]] = m[a]
        return raw
    }

    /** (биты без исправления, биты после Hamming) — по 96. */
    fun bptcDecode(raw: IntArray): Pair<IntArray, IntArray> {
        val m = IntArray(196) { raw[BPTC_PERM[it]] }
        val plain = IntArray(96) { m[BPTC_DATA_POS[it]] }
        repeat(2) {
            for (c in 0 until 15) {
                val col = IntArray(13) { m[1 + c + 15 * it] }
                if (fix(col, SYN13)) for (r in 0 until 13) m[1 + c + 15 * r] = col[r]
            }
            for (r in 0 until 9) {
                val row = m.copyOfRange(1 + 15 * r, 16 + 15 * r)
                if (fix(row, SYN15)) System.arraycopy(row, 0, m, 1 + 15 * r, 15)
            }
        }
        return plain to IntArray(96) { m[BPTC_DATA_POS[it]] }
    }

    // ---------- встроенный LC ----------
    private val EMB_DATA_POS = (0 until 11) + (16 until 27) + (32 until 42) + (48 until 58) + (64 until 74) + (80 until 90) + (96 until 106)
    private val EMB_CRC_POS = intArrayOf(42, 58, 74, 90, 106)
    private val EMB_PERM = IntArray(128) { if (it < 127) (it * 16) % 127 else 127 }

    fun embLcEncode(lc9: IntArray): IntArray {
        val d = IntArray(128)
        for ((i, b) in bytesBits(lc9).withIndex()) d[EMB_DATA_POS[i]] = b
        val crc = lc9.sum() % 31
        for ((i, p) in EMB_CRC_POS.withIndex()) d[p] = (crc shr (4 - i)) and 1
        for (r in 0 until 7) {
            val par = ham16114(d.copyOfRange(16 * r, 16 * r + 11))
            for (i in 0 until 5) d[16 * r + 11 + i] = par[i]
        }
        for (c in 0 until 16) {
            d[112 + c] = 0
            for (r in 0 until 7) d[112 + c] = d[112 + c] xor d[16 * r + c]
        }
        return IntArray(128) { d[EMB_PERM[it]] }
    }

    fun embLcDecode(raw: IntArray): IntArray? {
        val d = IntArray(128)
        for (a in 0 until 128) d[EMB_PERM[a]] = raw[a]
        fun extract(dd: IntArray): IntArray? {
            val lc = bitsBytes(IntArray(EMB_DATA_POS.size) { dd[EMB_DATA_POS[it]] })
            val crc = bitsInt(IntArray(5) { dd[EMB_CRC_POS[it]] })
            return if (lc.sum() % 31 == crc) lc else null
        }
        extract(d)?.let { return it }
        for (r in 0 until 7) {
            val row = d.copyOfRange(16 * r, 16 * r + 16)
            if (!fix(row, SYN16)) return null
            System.arraycopy(row, 0, d, 16 * r, 16)
        }
        for (c in 0 until 16) {
            var par = 0
            for (r in 0 until 8) par = par xor d[16 * r + c]
            if (par != 0) return null
        }
        return extract(d)
    }

    // ---------- AMBE+2: 72 бита ↔ ambe_fr[4][24] ----------
    private val RW = intArrayOf(0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 2, 0, 2, 0, 2, 0, 2, 0, 2, 0, 2, 0, 2)
    private val RX = intArrayOf(23, 10, 22, 9, 21, 8, 20, 7, 19, 6, 18, 5, 17, 4, 16, 3, 15, 2, 14, 1, 13, 0, 12, 10, 11, 9, 10, 8, 9, 7, 8, 6, 7, 5, 6, 4)
    private val RY = intArrayOf(0, 2, 0, 2, 0, 2, 0, 2, 0, 3, 0, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3)
    private val RZ = intArrayOf(5, 3, 4, 2, 3, 1, 2, 0, 1, 13, 0, 12, 22, 11, 21, 10, 20, 9, 19, 8, 18, 7, 17, 6, 16, 5, 15, 4, 14, 3, 13, 2, 12, 1, 11, 0)

    /** 72 бита эфира → плоский ambe_fr[96] (порядок mbelib). */
    fun ambeDeinterleave(bits72: IntArray, from: Int = 0): ByteArray {
        val fr = ByteArray(96)
        for (i in 0 until 36) {
            fr[RW[i] * 24 + RX[i]] = bits72[from + 2 * i].toByte()
            fr[RY[i] * 24 + RZ[i]] = bits72[from + 2 * i + 1].toByte()
        }
        return fr
    }

    private fun golay2312Parity(m12: Int) = polymod(m12.toLong() shl 11, 0xC75, 11).toInt()

    /** 49 бит параметров AMBE+2 → 72 бита в эфирном порядке (обратное к mbelib + DSD). */
    fun ambeEncode(d: IntArray): IntArray {
        val fr = Array(4) { IntArray(24) }
        for (i in 0 until 12) fr[0][23 - i] = d[i]
        val c0 = bitsInt(d, 0, 12)
        var p = golay2312Parity(c0)
        for (j in 0 until 11) fr[0][1 + j] = (p shr j) and 1
        fr[0][0] = (1 until 24).sumOf { fr[0][it] } and 1
        for (i in 0 until 12) fr[1][22 - i] = d[12 + i]
        p = golay2312Parity(bitsInt(d, 12, 24))
        for (j in 0 until 11) fr[1][j] = (p shr j) and 1
        val pr = IntArray(24); pr[0] = 16 * c0
        for (i in 1 until 24) pr[i] = (173 * pr[i - 1] + 13849) % 65536
        var k = 1
        for (j in 22 downTo 0) { fr[1][j] = fr[1][j] xor (pr[k] / 32768); k++ }
        for (i in 0 until 11) fr[2][10 - i] = d[24 + i]
        for (i in 0 until 14) fr[3][13 - i] = d[35 + i]
        val out = IntArray(72)
        for (i in 0 until 36) { out[2 * i] = fr[RW[i]][RX[i]]; out[2 * i + 1] = fr[RY[i]][RZ[i]] }
        return out
    }

    /** 49 бит ambe_d из основных индексов (раскладка как в mbe_decodeAmbe2450Parms). */
    fun ambeParams(b0: Int, b1: Int = 0, b2: Int = 16, b3: Int = 0, b4: Int = 0): IntArray {
        val d = IntArray(49)
        for ((v, poss) in listOf(b0 to intArrayOf(0, 1, 2, 3, 37, 38, 39), b1 to intArrayOf(4, 5, 6, 7, 35),
                b2 to intArrayOf(8, 9, 10, 11, 36), b3 to intArrayOf(12, 13, 14, 15, 16, 17, 18, 19, 40),
                b4 to intArrayOf(20, 21, 22, 23, 41, 42, 43))) {
            val n = poss.size
            for ((i, p) in poss.withIndex()) d[p] = (v shr (n - 1 - i)) and 1
        }
        return d
    }
}
