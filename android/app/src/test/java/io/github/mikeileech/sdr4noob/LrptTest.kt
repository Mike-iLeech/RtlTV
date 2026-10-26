package io.github.mikeileech.sdr4noob

import io.github.mikeileech.sdr4noob.lrpt.AutoLrptReceiver
import io.github.mikeileech.sdr4noob.lrpt.Deinterleaver80k
import io.github.mikeileech.sdr4noob.lrpt.Demux
import io.github.mikeileech.sdr4noob.lrpt.Frame
import io.github.mikeileech.sdr4noob.lrpt.Imager
import io.github.mikeileech.sdr4noob.lrpt.KotlinCodec
import io.github.mikeileech.sdr4noob.lrpt.KotlinDemod
import io.github.mikeileech.sdr4noob.lrpt.Lrpt
import io.github.mikeileech.sdr4noob.lrpt.LrptCodec
import io.github.mikeileech.sdr4noob.lrpt.LrptFile
import io.github.mikeileech.sdr4noob.lrpt.LrptGray
import io.github.mikeileech.sdr4noob.lrpt.LrptNative
import io.github.mikeileech.sdr4noob.lrpt.LrptReceiver
import io.github.mikeileech.sdr4noob.lrpt.LrptSynth
import io.github.mikeileech.sdr4noob.lrpt.LrptView
import io.github.mikeileech.sdr4noob.lrpt.Mcu
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.security.MessageDigest
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.ln
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * «Метеор-М» LRPT — перенос rtltv/lrpt.py и tools/lrpt_dsp.c (в JVM-тестах — Kotlin-перенос библиотеки C).
 * Эталоны посчитаны Python-модулем с настоящей библиотекой lrpt_dsp.dll (src/test/python/gen_lrpt_ref.py →
 * ресурс lrpt_ref.tsv). Случайные данные и шум — общий для Python и Kotlin генератор на счётчике (splitmix64),
 * поэтому синтетические сигналы одинаковы, и приём сверяется по числу кадров и по пикселям снимков.
 * Сценарии полного пути — из tests/test_lrpt.py настольной программы.
 */
class LrptTest {
    companion object {
        val REF: Map<String, String> by lazy {
            LrptTest::class.java.getResourceAsStream("/lrpt_ref.tsv")!!.bufferedReader(Charsets.UTF_8).readLines()
                .filter { it.isNotBlank() && !it.startsWith("#") }
                .associate { it.substringBefore('\t') to it.substringAfter('\t', "") }
        }

        fun ref(k: String): String = REF[k] ?: error("нет эталона $k")
        fun refInts(k: String): List<Long> = ref(k).split(' ').filter { it.isNotEmpty() }.map { it.toLong() }
        fun refDoubles(k: String): DoubleArray = ref(k).split(' ').filter { it.isNotEmpty() }.map { it.toDouble() }.toDoubleArray()
        fun refHex(k: String): ByteArray = hex(ref(k))
        fun refBits(k: String): ByteArray = ref(k).map { (it - '0').toByte() }.toByteArray()

        fun hex(s: String): ByteArray = ByteArray(s.length / 2) { s.substring(2 * it, 2 * it + 2).toInt(16).toByte() }
        fun sha(b: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(b).joinToString("") { "%02x".format(it) }
        fun sha(parts: List<ByteArray>): String {
            val md = MessageDigest.getInstance("SHA-256")
            for (p in parts) md.update(p)
            return md.digest().joinToString("") { "%02x".format(it) }
        }

        // ------------------------------------------------ общий генератор (как в gen_lrpt_ref.py)
        fun smix(seed: Int, i: Long): Long {
            var z = (seed.toLong() shl 40) + i
            z *= 0x9E3779B97F4A7C15uL.toLong()
            z = z xor (z ushr 30)
            z *= 0xBF58476D1CE4E5B9uL.toLong()
            z = z xor (z ushr 27)
            z *= 0x94D049BB133111EBuL.toLong()
            z = z xor (z ushr 31)
            return z
        }

        fun uni(seed: Int, i: Long): Double = (smix(seed, i) ushr 11).toDouble() * (1.0 / 9007199254740992.0)
        fun rbytes(seed: Int, n: Int) = ByteArray(n) { (smix(seed, it.toLong()) ushr 56).toByte() }
        fun rbits(seed: Int, n: Int) = ByteArray(n) { (smix(seed, it.toLong()) ushr 63).toByte() }

        /** Комплексный нормальный шум: (re, im) отсчёта i. */
        fun cgauss(seed: Int, i: Long, out: DoubleArray) {
            val u1 = 1.0 - uni(seed, 2 * i)
            val u2 = uni(seed, 2 * i + 1)
            val r = sqrt(-2.0 * ln(u1))
            out[0] = r * cos(2 * PI * u2)
            out[1] = r * sin(2 * PI * u2)
        }

        /** Доплер (f0 + rate·t), фаза, АБГШ с заданным Es/N0 (channel() из tests/test_lrpt.py). */
        fun channel(x: FloatArray, fs: Double, f0: Double, rate: Double, esn0Db: Double, sps: Int, seed: Int, phase: Double = 0.7): FloatArray {
            val n = x.size / 2
            var p = 0.0
            for (i in 0 until n) p += x[2 * i].toDouble() * x[2 * i] + x[2 * i + 1].toDouble() * x[2 * i + 1]
            p = p / n * sps
            val n0 = p / Math.pow(10.0, esn0Db / 10)
            val sigma = sqrt(n0 / 2)
            val y = FloatArray(2 * n)
            val g = DoubleArray(2)
            for (i in 0 until n) {
                val t = i / fs
                val ph = 2 * PI * (f0 * t + 0.5 * rate * t * t) + phase
                val c = cos(ph).toFloat()
                val s = sin(ph).toFloat()
                val a = x[2 * i]
                val b = x[2 * i + 1]
                cgauss(seed, i.toLong(), g)
                y[2 * i] = (a * c - b * s) + (sigma * g[0]).toFloat()
                y[2 * i + 1] = (a * s + b * c) + (sigma * g[1]).toFloat()
            }
            return y
        }

        fun makeVcdus(n: Int, seed: Int, vcid: Int = 5): List<ByteArray> {
            val raw = rbytes(seed, 892 * n)
            return (0 until n).map { k ->
                val vc = raw.copyOfRange(892 * k, 892 * (k + 1))
                val hdr = intArrayOf(0x40, vcid, (k shr 16) and 255, (k shr 8) and 255, k and 255, 0, 0, 0, 0x07, 0xFF)
                for (i in hdr.indices) vc[i] = hdr[i].toByte()
                vc
            }
        }

        fun runFrames(y: FloatArray, fs: Double, oqpsk: Boolean, block: Int = 131072): Pair<LrptReceiver, List<Frame>> {
            val rx = LrptReceiver(fs, oqpsk)
            val fr = ArrayList<Frame>()
            var k = 0
            while (k < y.size / 2) {
                val e = minOf(y.size / 2, k + block)
                fr.addAll(rx.feedFrames(y.copyOfRange(2 * k, 2 * e)))
                k = e
            }
            fr.addAll(rx.finishFrames())
            return Pair(rx, fr)
        }

        fun runRx(y: FloatArray, fs: Double, oqpsk: Boolean, block: Int = 131072): LrptReceiver {
            val rx = LrptReceiver(fs, oqpsk)
            var k = 0
            while (k < y.size / 2) {
                val e = minOf(y.size / 2, k + block)
                rx.feed(y.copyOfRange(2 * k, 2 * e))
                k = e
            }
            rx.finish()
            return rx
        }

        /** Сверка снимков с Python: SHA всего снимка и значения в узлах 8×8 (для подсказки при расхождении). */
        fun checkImages(prefix: String, images: Map<Int, LrptGray>) {
            for (a in refInts("$prefix.channels")) {
                val im = images[a.toInt()]
                assertNotNull("$prefix: нет канала $a", im)
                im!!
                val (h, w) = refInts("$prefix.img$a.shape").map { it.toInt() }
                assertEquals("$prefix.img$a высота", h, im.height)
                assertEquals(w, im.width)
                val blocks = refHex("$prefix.img$a.blocks")
                var diff = 0
                var i = 0
                for (y in 0 until h step 8) for (x in 0 until w step 8) { if (blocks[i] != im.px[y * w + x]) diff++; i++ }
                assertEquals("$prefix.img$a: узлов 8×8, отличающихся от Python", 0, diff)
                assertEquals("$prefix.img$a: SHA-256 снимка", ref("$prefix.img$a.sha"), sha(im.px))
            }
        }

        fun stats(prefix: String): List<String> = ref("$prefix.stats").split(' ')
    }

    // ---------------------------------------------------------------- генератор и базовые блоки

    @Test
    fun sharedGenerator() {
        val want = ref("smix.1").split(' ').map { java.lang.Long.parseUnsignedLong(it) }
        for (i in 0 until 4) assertEquals(want[i], smix(1, i.toLong()))
        val g = refDoubles("gauss.2")
        val o = DoubleArray(2)
        for (i in 0 until 3) {
            cgauss(2, i.toLong(), o)
            assertEquals(g[2 * i], o[0], 1e-12)
            assertEquals(g[2 * i + 1], o[1], 1e-12)
        }
    }

    @Test
    fun rrcTaps() {
        val keys = REF.keys.filter { it.startsWith("rrc.") }
        assertEquals(6, keys.size)
        for (k in keys) {
            val sps = k.removePrefix("rrc.").toDouble()
            val want = refDoubles(k)
            val got = Lrpt.rrcTaps(sps)
            assertEquals(k, want.size, got.size)
            for (i in want.indices) assertEquals("$k[$i]", want[i], got[i].toDouble(), 1e-7)
        }
    }

    @Test
    fun pnAndNrzm() {
        assertArrayEquals(refHex("pn"), Lrpt.PN)
        assertEquals(listOf(0xFF, 0x48, 0x0E, 0xC0, 0x9A, 0x0D, 0x70, 0xBC), Lrpt.PN.take(8).map { it.toInt() and 0xFF })
        val b = refBits("nrzm.in")
        assertArrayEquals(refBits("nrzm.enc1"), Lrpt.nrzmEncode(b, 1))
        assertArrayEquals(refBits("nrzm.dec0"), Lrpt.nrzmDecode(b, 0))
        assertArrayEquals(refBits("nrzm.dec1"), Lrpt.nrzmDecode(b, 1))
        assertArrayEquals(b, Lrpt.nrzmDecode(Lrpt.nrzmEncode(b, 1), 1))
    }

    @Test
    fun convEncodeAndViterbi() {
        assertFalse("в JVM-тестах нативной библиотеки нет — работает Kotlin-перенос", LrptCodec.useNative)
        val b = refBits("conv.in")
        val (e0, s0) = Lrpt.convEncode(b, 0)
        assertArrayEquals(refBits("conv.out0"), e0)
        assertEquals(ref("conv.st0").toInt(), s0)
        val (e37, s37) = Lrpt.convEncode(b, 37)
        assertArrayEquals(refBits("conv.out37"), e37)
        assertEquals(ref("conv.st37").toInt(), s37)

        val soft = refHex("vit.soft")
        val out = ByteArray(soft.size / 2)
        val metric = KotlinCodec.viterbi27(soft, out.size, out)
        assertArrayEquals("Витерби: биты как у библиотеки C", refBits("vit.out"), out)
        assertEquals("Витерби: метрика как у библиотеки C", ref("vit.metric").toInt(), metric)
        val src = refBits("vit.in")
        var raw = 0
        val (enc, _) = Lrpt.convEncode(src)
        for (i in enc.indices) if ((soft[i] > 0) != (enc[i].toInt() == 1)) raw++
        assertTrue("сырых ошибок ${raw.toDouble() / enc.size}", raw > enc.size * 0.04)
        assertArrayEquals(src.copyOf(src.size - 64), out.copyOf(out.size - 64))
    }

    @Test
    fun reedSolomon() {
        val d = refHex("rs.data")
        for (dual in listOf(false, true)) {
            val tag = if (dual) "dual" else "norm"
            val par = Lrpt.rsEncode(d, dual)
            assertArrayEquals("RS $tag: проверочные", refHex("rs.$tag.par"), par)
            val cw = d + par
            assertEquals(0, Lrpt.rsDecode(cw.copyOf(), dual))
            for (ne in listOf(5, 16, 17, 20)) {
                val e = refHex("rs.$tag.err$ne")
                val r = Lrpt.rsDecode(e, dual)
                assertEquals("RS $tag: $ne ошибок", ref("rs.$tag.ret$ne").toInt(), r)
                assertArrayEquals(refHex("rs.$tag.fix$ne"), e)
                if (r >= 0) assertArrayEquals(cw, e)
            }
        }
    }

    @Test
    fun asmSearchAndTransform() {
        val bits = refBits("asm.bits")
        for (m in listOf(3, 6)) {
            val got = Lrpt.asmHits(bits, m).joinToString(" ") { "${it.first}:${if (it.second) 1 else 0}" }
            assertEquals("ASM, до $m ошибок", ref("asm.hits$m"), got)
        }
        val s = refHex("tr.in")
        for (h in 0 until 8) assertArrayEquals("гипотеза $h", refHex("tr.$h"), Lrpt.transform(s, h))
    }

    @Test
    fun demodulatorMatchesC() {
        val q = refInts("demod.in")
        val iq = FloatArray(q.size) { q[it].toFloat() / 4096f }
        val n = iq.size / 2
        for (mode in 0..2) {
            val d = KotlinDemod(4.0, mode, 0.006, 0.002, 2 * PI * 8000.0 / 288000.0)
            if (mode == 1) d.setFreq(2 * PI * 250.0 / 288000.0)
            val soft = ArrayList<Byte>()
            val cons = ArrayList<Float>()
            var total = 0
            for ((a, z) in listOf(Pair(0, 7000), Pair(7000, n))) {
                val ms = (z - a) / 4 + 16
                val so = ByteArray(ms * 2)
                val co = FloatArray(ms * 2)
                val k = d.process(iq.copyOfRange(2 * a, 2 * z), z - a, so, co, ms)
                for (i in 0 until (if (mode == 2) k else 2 * k)) soft.add(so[i])
                for (i in 0 until 2 * k) cons.add(co[i])
                total += k
            }
            assertEquals("демодулятор $mode: число символов", ref("demod.$mode.n").toInt(), total)
            val want = refHex("demod.$mode.soft")
            assertEquals(want.size, soft.size)
            var diff = 0
            var big = 0
            for (i in want.indices) {
                val dd = abs(want[i] - soft[i])
                if (dd != 0) diff++
                if (dd > 1) big++
            }
            println("демодулятор $mode: мягких бит ${want.size}, отличий от C ${diff} (больше ±1: $big)")
            // библиотека C на ПК собрана с /fp:fast — совпадение до единицы младшего разряда, не бит в бит
            assertTrue("демодулятор $mode: отличий $diff", diff <= want.size / 200)
            assertEquals("демодулятор $mode: отличий больше ±1", 0, big)
            val wc = refDoubles("demod.$mode.cons")
            for (i in wc.indices) assertEquals("созвездие $mode[$i]", wc[i], cons[i].toDouble(), 1e-3)
            val st = DoubleArray(6)
            d.state(st)
            val ws = refDoubles("demod.$mode.state")
            assertEquals("частота", ws[0], st[0], 1e-6)
            assertEquals("интервал", ws[2], st[2], 1e-5)
            assertEquals("АРУ", ws[3], st[3], 1e-4 * ws[3])
            assertEquals("амплитуда", ws[4], st[4], 1e-4)
            assertEquals("захват", ws[5], st[5], 1e-4)
        }
    }

    // ---------------------------------------------------------------- МКУ, картинки

    @Test
    fun huffmanAndQuant() {
        assertEquals(12, Mcu.DC_CODES.size)
        assertEquals(162, Mcu.AC_CODES.size)
        assertEquals(Pair(0b1010, 4), Mcu.AC_CODES[0x00])
        assertEquals(Pair(0b11111111001, 11), Mcu.AC_CODES[0xF0])
        for (qf in listOf(10, 20, 35, 49, 50, 80, 95, 100, 0)) {
            assertEquals("qtable($qf)", refInts("qt.$qf"), Mcu.qtable(qf).map { it.toLong() })
        }
    }

    @Test
    fun mcuWithAcCoefficients() {
        for (qf in listOf(77, 30)) {
            val data = refHex("mcu.$qf.data")
            val (px, n) = Mcu.decode(data, 0, qf)
            assertEquals(ref("mcu.$qf.n").toInt(), n)
            assertArrayEquals("МКУ qf=$qf", refHex("mcu.$qf.px"), px)
            val (px2, n2) = Mcu.decode(data.copyOf(data.size * 2 / 3), 0, qf)
            assertEquals(ref("mcu.$qf.cut.n").toInt(), n2)
            assertArrayEquals("МКУ qf=$qf обрезанные", refHex("mcu.$qf.cut.px"), px2)
        }
    }

    @Test
    fun stretchLevels() {
        val v = rbytes(24, 50 * 1568)
        val px = ByteArray(v.size) { val b = v[it].toInt() and 0xFF; (if (b < 40) 0 else 30 + b * 140 / 255).toByte() }
        val out = LrptView.stretch(LrptGray(1568, 50, px))
        assertEquals(ref("stretch.sha"), sha(out.px))
    }

    @Test
    fun synthTransmitter() {
        for (seed in 0..2) assertEquals("synth_image($seed)", ref("synth.$seed.sha"), sha(LrptSynth.synthImage(96, seed).px))
        val imgs = (0 until 3).associate { 64 + it to LrptSynth.synthImage(16, it) }
        val pk = LrptSynth.synthPackets(imgs)
        assertEquals(ref("synth.packets.n").toInt(), pk.size)
        assertEquals(ref("synth.packets.sha"), sha(pk))
        val vc = LrptSynth.packetsToVcdus(pk)
        assertEquals(ref("synth.vcdus.sha"), sha(vc))
        val cad = LrptSynth.vcdusToCadus(vc)
        assertEquals(ref("synth.cadus.sha"), sha(cad))
        assertEquals(ref("synth.cadus.dual.sha"), sha(LrptSynth.vcdusToCadus(vc, true)))
        val m = LrptSynth.Modulator(4, true, true).modulate(cad.take(2))
        assertEquals(ref("synth.mod.n").toInt(), m.size / 2)
        val head = refDoubles("synth.mod.head")
        for (i in head.indices) assertEquals("модулятор[$i]", head[i], m[i].toDouble(), 2e-6)
    }

    /** test_image_codec_roundtrip: сжатие МКУ → пакеты → VCDU → разбор → картинка ≈ усреднение 8×8. */
    @Test
    fun imageCodecRoundtrip() {
        val imgs = (0 until 3).associate { 64 + it to LrptSynth.synthImage(64, it) }
        val pk = LrptSynth.synthPackets(imgs)
        val dm = Demux()
        val im = Imager()
        for (v in LrptSynth.packetsToVcdus(pk)) for (p in dm.push(v)) im.push(p.apid, p.seq, p.payload)
        assertEquals(pk.size, dm.packets)
        for ((a, src) in imgs) {
            val out = im.image(a)!!
            assertEquals(src.height, out.height)
            var err = 0.0
            for (by in 0 until 8) for (bx in 0 until 196) {
                var s = 0.0
                for (r in 0 until 8) for (c in 0 until 8) s += src[by * 8 + r, bx * 8 + c]
                val avg = s / 64
                for (r in 0 until 8) for (c in 0 until 8) err += abs(out[by * 8 + r, bx * 8 + c] - avg)
            }
            assertTrue("ошибка ${err / out.px.size}", err / out.px.size < 1.0)
        }
        val rgb = LrptView.composite(im.allImages(), "123")!!
        assertEquals(64 * 1568, rgb.argb().size)
        val pv = LrptView.preview(im.allImages(), "125")
        assertEquals("нет канала 5 — подбирается другой композит", "221", pv.key)
        assertNotNull(pv.rgb)
        assertNotNull(LrptView.preview(im.allImages(), "2").gray)
    }

    @Test
    fun coarseOffset() {
        val cad = LrptSynth.vcdusToCadus(makeVcdus(6, 30))
        val x = LrptSynth.Modulator(8, true, true).modulate(cad).copyOf(2 * 201600)
        val y = channel(x, 576000.0, 2300.0, 0.0, 9.0, 8, 31)
        val (c, snr) = Lrpt.coarseOffset(y, 576000.0)
        val w = refDoubles("coarse.576k")
        println("грубая частота: Kotlin $c Гц, SNR $snr дБ; Python ${w[0]} Гц, ${w[1]} дБ")
        assertEquals(w[0], c, 576000.0 / 16384)
        assertEquals(w[1], snr, 0.3)
    }

    // ---------------------------------------------------------------- полный путь

    private fun chain(name: String, n: Int, sp: Int, oq: Boolean, nz: Boolean, f0: Double, rate: Double, es: Double, seed: Int) {
        val vcdus = makeVcdus(n, seed)
        val fs = 72000.0 * sp
        val x = LrptSynth.Modulator(sp, oq, nz).modulate(LrptSynth.vcdusToCadus(vcdus))
        val y = channel(x, fs, f0, rate, es, sp, seed + 100)
        val t0 = System.nanoTime()
        val (rx, frames) = runFrames(y, fs, oq)
        val dt = (System.nanoTime() - t0) / 1e9
        val st = rx.stats()
        val py = stats("chain.$name")
        println("цепочка $name: кадров ${st.framesOk}/${st.frames} (Python ${py[1]}/${py[0]}), nrzm=${st.nrzm}, " +
            "гипотеза ${st.hyp}, RS исправлено ${st.rsFixed} (Python ${py[4]}), ${"%.1f".format(y.size / 2 / fs / dt)}× реального времени")
        assertEquals("$name: кадров принято", py[1].toInt(), st.framesOk)
        assertEquals("$name: кадров", py[0].toInt(), st.frames)
        assertEquals(py[2] == "1", st.nrzm)
        assertEquals(nz, st.nrzm)
        assertEquals(py[3], st.hyp.toString())
        assertEquals("$name: исправлено байт RS", py[4].toInt(), st.rsFixed)
        // принятые кадры совпадают с переданными
        for (f in frames.filter { it.ok }) {
            val k = ((f.data[2].toInt() and 255) shl 16) or ((f.data[3].toInt() and 255) shl 8) or (f.data[4].toInt() and 255)
            assertArrayEquals("$name: кадр $k", vcdus[k], f.data.copyOf(892))
        }
    }

    @Test
    fun chainOqpskNrzm8sps() = chain("oq8", 40, 8, true, true, 2300.0, -45.0, 9.0, 40)

    @Test
    fun chainQpsk4sps() = chain("qp4", 40, 4, false, false, 2300.0, -45.0, 9.0, 41)

    /** Es/N0 = 4 дБ (≈ порог) — большинство кадров проходит RS. */
    @Test
    fun chainLowSnr() {
        chain("low4", 40, 4, true, true, -1800.0, 30.0, 4.0, 42)
        assertTrue(stats("chain.low4")[1].toInt() >= 24)
    }

    /** 216 кГц < 4·Rs: ветка с повышением частоты дискретизации перед RRC. */
    @Test
    fun chainUpsampled3sps() = chain("oq3", 30, 3, true, true, 1200.0, 20.0, 10.0, 43)

    /** test_synthetic_image_over_air: OQPSK + NRZ-M + доплер + шум → снимок 3 каналов, как у Python. */
    @Test
    fun imageOverAir() {
        val imgs = (0 until 3).associate { 64 + it to LrptSynth.synthImage(96, it) }
        val cad = LrptSynth.vcdusToCadus(LrptSynth.packetsToVcdus(LrptSynth.synthPackets(imgs)))
        assertEquals(ref("air.ncad").toInt(), cad.size)
        val sp = 4
        val fs = 72000.0 * sp
        val m = LrptSynth.Modulator(sp, true, true).modulate(cad)
        val x = FloatArray(2 * 7500) + m + FloatArray(2 * 37500)
        val y = channel(x, fs, 900.0, -20.0, 10.0, sp, 150)
        val rx = runRx(y, fs, true)
        val st = rx.stats()
        val py = stats("air")
        println("снимок по радио: кадров ${st.framesOk}/${cad.size}, пакетов ${st.packets}, строк ${st.lines}, каналы ${st.channels}")
        assertEquals(py[1].toInt(), st.framesOk)
        assertEquals(py[5].toInt(), st.packets)
        assertEquals(py[6].toInt(), st.lines)
        assertEquals(listOf(64, 65, 66), st.channels)
        checkImages("air", rx.imager.allImages())
        assertNotNull(LrptView.composite(rx.imager.allImages(), "123"))
    }

    /** test_80k_interleaved_and_auto: 80k OQPSK с перемежением, AutoLrptReceiver сам выбирает режим. */
    @Test
    fun interleaved80kAuto() {
        val imgs = (0 until 3).associate { 64 + it to LrptSynth.synthImage(32, it) }
        val cad = LrptSynth.vcdusToCadus(LrptSynth.packetsToVcdus(LrptSynth.synthPackets(imgs)))
        var all = ByteArray(0)
        for (c in cad) all += c
        val (coded0, _) = Lrpt.convEncode(Lrpt.nrzmEncode(Lrpt.unpackBits(all)))
        var coded = ByteArray(72 * 200) + coded0 + ByteArray(Deinterleaver80k.SPAN + 72 * 400)
        coded = coded.copyOf(coded.size / 72 * 72)
        val chan = Deinterleaver80k.interleave(coded)
        assertEquals(ref("i80.chan.sha"), sha(chan))
        val sp = 4
        val h = Lrpt.rrcTaps(sp.toDouble())
        val ns = chan.size / 2
        val n = ns * sp
        val x = FloatArray(2 * n)
        for (s in 0 until ns) {
            val iv = chan[2 * s] * 2 - 1f
            val qv = chan[2 * s + 1] * 2 - 1f
            for (k in h.indices) {
                val pi = s * sp + k
                val pq = s * sp + sp / 2 + k
                if (pi < n) x[2 * pi] += iv * h[k]
                if (pq < n) x[2 * pq + 1] += qv * h[k]
            }
        }
        val fs = 320000.0
        val g = DoubleArray(2)
        val y = FloatArray(2 * n)
        for (i in 0 until n) {
            val t = i / fs
            val ph = 2 * PI * (700 * t - 15 * t * t) + 1.1
            val c = cos(ph).toFloat()
            val s = sin(ph).toFloat()
            cgauss(161, i.toLong(), g)
            y[2 * i] = (x[2 * i] * c - x[2 * i + 1] * s) + (0.15 * g[0]).toFloat()
            y[2 * i + 1] = (x[2 * i] * s + x[2 * i + 1] * c) + (0.15 * g[1]).toFloat()
        }
        val t0 = System.nanoTime()
        val rx = AutoLrptReceiver(fs)
        var k = 0
        while (k < n) {
            val e = minOf(n, k + 65536)
            rx.feed(y.copyOfRange(2 * k, 2 * e))
            k = e
        }
        rx.finish()
        val dt = (System.nanoTime() - t0) / 1e9
        val st = rx.stats()
        println("80k: кадров ${st.framesOk}/${cad.size}, режим «${st.mode}», строк ${st.lines}, " +
            "${"%.1f".format(n / fs / dt)}× реального времени (два приёмника параллельно, пока режим не выбран)")
        assertEquals(ref("i80.mode"), st.mode)
        assertTrue(st.framesOk >= cad.size - 1)
        assertEquals(stats("i80")[1].toInt(), st.framesOk)
        assertEquals(listOf(64, 65, 66), st.channels)
        checkImages("i80", rx.rx.imager.allImages())
    }

    // ---------------------------------------------------------------- прочее

    @Test
    fun offsetFromName() {
        assertEquals(50e3, LrptFile.offsetFromName("/x/METEOR_20261026_1024000Hz_137850000.cu8", 1024000.0)!!, 1e-6)
        assertEquals(null, LrptFile.offsetFromName("rec.cu8", 1024000.0))
        assertEquals(null, LrptFile.offsetFromName("a_250000Hz_100000000.cu8", 250000.0))
    }

    /** Копия tools/lrpt_dsp.c в src/main/cpp — без изменений (кроме шапки с пометкой источника). */
    @Test
    fun nativeCopyMatchesDesktop() {
        val root = generateSequence(File("").absoluteFile) { it.parentFile }.first { File(it, "tools/lrpt_dsp.c").isFile }
        val desk = File(root, "tools/lrpt_dsp.c").readText(Charsets.UTF_8).replace("\r\n", "\n")
        val copy = File(root, "android/app/src/main/cpp/lrpt_dsp.c").readText(Charsets.UTF_8).replace("\r\n", "\n")
        assertTrue(copy.startsWith("/* КОПИЯ tools/lrpt_dsp.c"))
        assertEquals(desk, copy.substringAfter(" */\n"))
        assertFalse(LrptNative.loaded)
    }

    /** Скорость Kotlin-переноса на JVM: Витерби k=7 и весь приёмник (поток 72 000 символ/с = 72 000 пар/с). */
    @Test
    fun speed() {
        val bits = rbits(50, 720000)
        val (enc, _) = Lrpt.convEncode(bits)
        val soft = ByteArray(enc.size) { (if (enc[it].toInt() == 1) 40 else -40).toByte() }
        val out = ByteArray(bits.size)
        KotlinCodec.viterbi27(soft.copyOf(2 * 50000), 50000, ByteArray(50000))      // прогрев JIT
        var best = Double.MAX_VALUE
        repeat(3) {
            val t0 = System.nanoTime()
            KotlinCodec.viterbi27(soft, bits.size, out)
            best = minOf(best, (System.nanoTime() - t0) / 1e9)
        }
        assertArrayEquals(bits.copyOf(bits.size - 64), out.copyOf(out.size - 64))
        val rate = bits.size / best
        println("Витерби (Kotlin, JVM): ${"%.2f".format(rate / 1e6)} Мбит/с = ${"%.0f".format(rate / 72000)}× потока 72 кбит/с")
        assertTrue(rate > 72000 * 5)

        val vcdus = makeVcdus(24, 60)
        val fs = 1152000.0
        val y = channel(LrptSynth.Modulator(16, true, true).modulate(LrptSynth.vcdusToCadus(vcdus)), fs, 1500.0, -40.0, 12.0, 16, 61)
        runRx(y.copyOf(2 * 300000), fs, true)                                          // прогрев
        val t0 = System.nanoTime()
        val rx = runRx(y, fs, true)
        val dt = (System.nanoTime() - t0) / 1e9
        val st = rx.stats()
        val rt = y.size / 2 / fs / dt
        println("приёмник целиком (Kotlin, JVM, 1,152 МГц): кадров ${st.framesOk}/24, ${"%.1f".format(rt)}× реального времени")
        assertTrue(st.framesOk >= 24 - 6)
        assertTrue(rt > 1.5)
        // доли: перенос частоты + RRC + децимация (Kotlin и на устройстве) / петли демодулятора / Витерби
        val dm = io.github.mikeileech.sdr4noob.lrpt.Demodulator(fs)
        dm.setOffset(1500.0)
        var t1 = System.nanoTime()
        val parts = ArrayList<FloatArray>()
        var k = 0
        while (k < y.size / 2) {
            val e = minOf(y.size / 2, k + 131072)
            parts.add(dm.filter(y.copyOfRange(2 * k, 2 * e)))
            k = e
        }
        val tFilt = (System.nanoTime() - t1) / 1e9
        t1 = System.nanoTime()
        var nsoft = 0
        for (p in parts) nsoft += dm.processFs2(p, false).soft.size
        val tLoop = (System.nanoTime() - t1) / 1e9
        val tVit = nsoft / 2 / rate
        println("из них: RRC+децимация ${"%.0f".format(100 * tFilt / dt)} %, петли ${"%.0f".format(100 * tLoop / dt)} %, " +
            "Витерби ≈ ${"%.0f".format(100 * tVit / dt)} % времени (RRC ${dm.taps.size} отводов, ${"%.1f".format(y.size / 2 / fs / tFilt)}× реального времени)")
    }
}
