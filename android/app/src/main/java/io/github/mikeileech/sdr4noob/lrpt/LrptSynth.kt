package io.github.mikeileech.sdr4noob.lrpt

import kotlin.math.sin

/** Синтетический передатчик LRPT (тесты, демо) — перенос нижней части rtltv/lrpt.py. */
object LrptSynth {
    private class BitWriter {
        val sb = StringBuilder()
        fun put(code: Int, n: Int) { for (i in n - 1 downTo 0) sb.append(if ((code ushr i) and 1 != 0) '1' else '0') }
    }

    /** Код значения коэффициента JPEG длиной n бит. */
    private fun valBits(v0: Int, n: Int): Int = if (v0 < 0) v0 + (1 shl n) - 1 else v0

    /** Полоса 8×112 (strip[r*stride + x0 + c]) → сжатые данные 14 МКУ (только DC + EOB). */
    fun encodeSegment(strip: ByteArray, stride: Int, x0: Int, qf: Int = 80): ByteArray {
        val q0 = Mcu.qtable(qf)[0]
        val w = BitWriter()
        var prev = 0
        for (i in 0 until 14) {
            var s = 0.0
            for (r in 0 until 8) for (c in 0 until 8) s += (strip[r * stride + x0 + i * 8 + c].toInt() and 0xFF).toDouble() - 128.0
            val mean = s / 64
            val dc = Math.rint(mean * 8.0 / q0).toInt()     // round() в Python — банковское
            val d = dc - prev
            prev = dc
            val cat = if (d == 0) 0 else 32 - Integer.numberOfLeadingZeros(Math.abs(d))
            val (code, ln) = Mcu.DC_CODES[cat]!!
            w.put(code, ln)
            if (cat > 0) w.put(valBits(d, cat), cat)
            val (ec, el) = Mcu.AC_CODES[0x00]!!         // EOB
            w.put(ec, el)
        }
        while (w.sb.length % 8 != 0) w.sb.append('1')
        val out = ByteArray(w.sb.length / 8)
        for (i in out.indices) out[i] = w.sb.substring(i * 8, i * 8 + 8).toInt(2).toByte()
        return out
    }

    /** Тестовая картинка h×1568: градиенты, круги, сетка (float32, как numpy). */
    fun synthImage(h: Int, seed: Int = 0): LrptGray {
        val w = Lrpt.WIDTH
        val px = ByteArray(h * w)
        val circles = arrayOf(Triple(400f, (h * 0.3).toFloat(), 120f), Triple(1100f, (h * 0.6).toFloat(), 200f), Triple(800f, (h * 0.8).toFloat(), 90f))
        for (y in 0 until h) for (x in 0 until w) {
            val xf = x.toFloat()
            val yf = y.toFloat()
            var v = 60f + 80f * (xf / w) + 40f * sin((yf / 37.0f + seed).toDouble()).toFloat()
            for ((cx, cy, r) in circles) {
                val dx = xf - cx
                val dy = yf - cy
                if (dx * dx + dy * dy < r * r) v = (230 - seed * 20).toFloat()
            }
            if ((x / 196) % 2 == 0 && (y / 64) % 2 == 0) v *= 0.8f
            px[y * w + x] = v.coerceIn(0f, 255f).toInt().toByte()
        }
        return LrptGray(w, h, px)
    }

    private fun be16(v: Int) = byteArrayOf((v ushr 8).toByte(), v.toByte())

    /** {apid: картинка h×1568} → CCSDS-пакеты в порядке передачи (цикл 14·N+1 пакетов). */
    fun synthPackets(images: Map<Int, LrptGray>, line0: Int = 0, seq0: Int = 0, t0Ms: Long = 36000000, qf: Int = 80): List<ByteArray> {
        val pk = ArrayList<ByteArray>()
        var seq = seq0
        val h = images.values.minOf { it.height } / 8
        for (ln in 0 until h) {
            val ms = t0Ms + (line0 + ln) * 1230L
            for (apid in images.keys.sorted()) {
                val img = images[apid]!!
                val strip = img.px.copyOfRange(ln * 8 * img.width, (ln * 8 + 8) * img.width)
                for (m in 0 until 14) {
                    val hdr = byteArrayOf(0, 0, (ms ushr 24).toByte(), (ms ushr 16).toByte(), (ms ushr 8).toByte(), ms.toByte(), 0, 0,
                        (m * 14).toByte(), 0, 0, 0xFF.toByte(), 0xF0.toByte(), qf.toByte())
                    val body = cat(hdr, encodeSegment(strip, img.width, m * 112, qf))
                    pk.add(be16(0x0800 or apid) + be16(0xC000 or (seq and 0x3FFF)) + be16(body.size - 1) + body)
                    seq++
                }
            }
            val body = ByteArray(20)
            pk.add(be16(0x0800 or 70) + be16(0xC000 or (seq and 0x3FFF)) + be16(body.size - 1) + body)
            seq++
        }
        return pk
    }

    /** Пакеты → VCDU по 892 байта (заголовок 6 + вставка 2 + M_PDU 2 + 882). */
    fun packetsToVcdus(packets: List<ByteArray>, scid: Int = 0, vcid: Int = 5, count0: Int = 0): List<ByteArray> {
        var total = 0
        val starts = IntArray(packets.size)
        for ((i, p) in packets.withIndex()) { starts[i] = total; total += p.size }
        val padded = total + Math.floorMod(-total, 882)
        val stream = ByteArray(padded) { 0xFF.toByte() }     // заполнитель: заголовок APID 2047 (пустой пакет)
        var o = 0
        for (p in packets) { System.arraycopy(p, 0, stream, o, p.size); o += p.size }
        val out = ArrayList<ByteArray>()
        var si = 0
        for (k in 0 until padded / 882) {
            val a = k * 882
            val b = (k + 1) * 882
            while (si < starts.size && starts[si] < a) si++
            val fhp = if (si < starts.size && starts[si] < b) starts[si] - a else 0x7FF
            val cnt = count0 + k
            val hdr = byteArrayOf((0x40 or (scid shr 2)).toByte(), (((scid and 3) shl 6) or vcid).toByte(), (cnt shr 16).toByte(),
                (cnt shr 8).toByte(), cnt.toByte(), 0, 0, 0, ((fhp shr 8) and 7).toByte(), fhp.toByte())
            out.add(hdr + stream.copyOfRange(a, b))
        }
        return out
    }

    /** VCDU (892 байта) → CADU 1024 байта: RS ×4 с перемежением, рандомизация, ASM. */
    fun vcdusToCadus(vcdus: List<ByteArray>, dual: Boolean = false): List<ByteArray> {
        val asm = byteArrayOf(0x1A, 0xCF.toByte(), 0xFC.toByte(), 0x1D)
        val pn = Lrpt.PN
        return vcdus.map { vc ->
            val block = ByteArray(1020)
            for (j in 0 until 4) {
                val d = ByteArray(223) { vc[j + 4 * it] }
                val par = Lrpt.rsEncode(d, dual)
                for (i in 0 until 223) block[j + 4 * i] = d[i]
                for (i in 0 until 32) block[j + 4 * (223 + i)] = par[i]
            }
            for (i in block.indices) block[i] = (block[i].toInt() xor pn[i].toInt()).toByte()
            asm + block
        }
    }

    /** CADU → комплексный сигнал LRPT (OQPSK/QPSK, RRC α=0,5) с непрерывным состоянием между вызовами. */
    class Modulator(val sps: Int = 16, val oqpsk: Boolean = true, val nrzm: Boolean = true, alpha: Double = 0.5) {
        val h: FloatArray = Lrpt.rrcTaps(sps.toDouble(), alpha, 12)
        private var tail = FloatArray(2 * (h.size - 1))
        private var convState = 0
        private var nrzmPrev = 0

        /** Возвращает (re, im) длиной (биты/2)·sps. */
        fun modulate(cadus: List<ByteArray>): FloatArray {
            var all = ByteArray(0)
            for (c in cadus) all = cat(all, c)
            var bits = Lrpt.unpackBits(all)
            if (nrzm) {
                bits = Lrpt.nrzmEncode(bits, nrzmPrev)
                nrzmPrev = bits[bits.size - 1].toInt()
            }
            val (enc, st) = Lrpt.convEncode(bits, convState)
            convState = st
            val ns = enc.size / 2
            val n = ns * sps
            val l = h.size
            val y = FloatArray(2 * (n + l - 1))
            val off = if (oqpsk) sps / 2 else 0
            for (s in 0 until ns) {
                val iv = enc[2 * s] * 2 - 1f
                val qv = enc[2 * s + 1] * 2 - 1f
                val pi = s * sps
                val pq = s * sps + off
                for (k in 0 until l) {
                    y[2 * (pi + k)] += iv * h[k]
                    y[2 * (pq + k) + 1] += qv * h[k]
                }
            }
            for (i in tail.indices) y[i] += tail[i]
            tail = y.copyOfRange(2 * n, y.size)
            return y.copyOf(2 * n)
        }
    }
}
