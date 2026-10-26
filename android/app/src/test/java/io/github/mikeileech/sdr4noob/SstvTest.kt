package io.github.mikeileech.sdr4noob

import io.github.mikeileech.sdr4noob.dsp.Sstv
import io.github.mikeileech.sdr4noob.dsp.SstvDecoder
import io.github.mikeileech.sdr4noob.dsp.SstvEvent
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Random
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * SSTV — перенос rtltv/sstv.py и проверок tests/test_sstv.py.
 * Эталоны (src/test/resources/sstv_ref.txt) посчитаны Python-модулем скриптом sstv_ref_gen.py рядом:
 * тайминги и VIS режимов, цветовые формулы, отсчёты кодера, результат Python-декодера на чистом сигнале.
 */
class SstvTest {
    companion object {
        const val FS = 48000.0
        const val BLOCK = 2400
        const val SNR_DB = 12.0
        val MODE_NAMES = Sstv.MODES.keys.toList()

        val REF: List<List<String>> by lazy {
            SstvTest::class.java.getResourceAsStream("/sstv_ref.txt")!!.bufferedReader(Charsets.UTF_8)
                .readLines().filter { it.isNotBlank() }.map { it.split("|") }
        }

        fun ref(kind: String) = REF.filter { it[0] == kind }

        /** Испытательная картинка (как card в test_sstv.py): цветные полосы, серая растяжка, круг на фоне. */
        fun card(w: Int, h: Int): IntArray {
            val bars = arrayOf(intArrayOf(255, 255, 255), intArrayOf(255, 255, 0), intArrayOf(0, 255, 255),
                intArrayOf(0, 255, 0), intArrayOf(255, 0, 255), intArrayOf(255, 0, 0), intArrayOf(0, 0, 255),
                intArrayOf(0, 0, 0))
            val img = IntArray(w * h)
            fun u8(v: Double) = v.toFloat().toInt()
            for (y in 0 until h) for (x in 0 until w) {
                var r = 0; var g = 0; var b = 0
                if (y < h / 3) { val c = bars[x * 8 / w]; r = c[0]; g = c[1]; b = c[2] }
                else if (y < h / 2) { val v = u8(x.toDouble() / (w - 1) * 255.0); r = v; g = v; b = v }
                else {
                    r = u8(x.toDouble() / w * 120); g = u8(60 + y.toDouble() / h * 80); b = u8(180 - x.toDouble() / w * 60)
                }
                val dx = x - w / 2.0; val dy = y - 0.75 * h
                if (dx * dx + dy * dy < (h / 7.0).pow(2)) { r = 230; g = 200; b = 60 }
                img[y * w + x] = Sstv.argb(r, g, b)
            }
            return img
        }

        fun ch(p: Int, c: Int) = (p shr (16 - 8 * c)) and 0xFF

        fun psnr(a: IntArray, b: IntArray, n: Int = a.size, offA: Int = 0, offB: Int = 0): Double {
            var se = 0.0
            for (i in 0 until n) for (c in 0..2) { val d = (ch(a[offA + i], c) - ch(b[offB + i], c)).toDouble(); se += d * d }
            val mse = se / (3.0 * n)
            return 10 * log10(255.0 * 255.0 / max(mse, 1e-9))
        }

        /** Сигнал «из эфира»: тишина, передача (такт ±0,1 %, расстройка ±50 Гц), тишина + шум. */
        fun air(mode: String, k: Int, snrDb: Double? = SNR_DB, seed: Long = 0): Triple<IntArray, FloatArray, Double> {
            val m = Sstv.MODES.getValue(mode)
            val img = card(m.width, m.height)
            val clk = 1.0 + (if (k % 2 == 0) 0.001 else -0.001)
            val shift = if (k % 2 == 0) 50.0 else -50.0
            val enc = Sstv.encode(img, m.width, m.height, mode, FS * clk, shift)
            val sig = FloatArray(FS.toInt() / 2 + enc.size + FS.toInt())
            System.arraycopy(enc, 0, sig, FS.toInt() / 2, enc.size)
            if (snrDb != null) {
                val rng = Random(seed + k)
                val sd = sqrt(0.5 * 0.5 / 2 / 10.0.pow(snrDb / 10))
                for (i in sig.indices) sig[i] += (rng.nextGaussian() * sd).toFloat()
            }
            return Triple(img, sig, clk)
        }

        fun run(dec: SstvDecoder, sig: FloatArray, block: Int = BLOCK): List<SstvEvent> {
            val ev = ArrayList<SstvEvent>()
            var i = 0
            while (i < sig.size) { ev += dec.process(sig, i, min(block, sig.size - i)); i += block }
            return ev
        }
    }

    @Test fun modesMatchPython() {
        val rows = ref("MODE")
        assertEquals(MODE_NAMES, rows.map { it[1] })
        for (r in rows) {
            val m = Sstv.MODES.getValue(r[1])
            assertEquals(r[2].toInt(), m.vis)
            assertEquals(r[3].toInt(), m.width)
            assertEquals(r[4].toInt(), m.height)
            assertEquals(r[5].toDouble(), m.line, 1e-12)
            assertEquals(r[6].toDouble(), m.sync, 1e-12)
            assertEquals(r[7].toDouble(), m.firstSync, 1e-12)
            assertEquals(r[8].toDouble(), m.duration, 1e-9)
            assertEquals(r[9].toInt(), m.lines)
            assertEquals(r[8].toDouble(), Sstv.modeDuration(m.name), 1e-9)
            val chans = r[10].split(";").map { it.split(":") }
            assertEquals(chans.map { it[0] }, m.channels.map { it.kind })
            for ((c, k) in chans.zip(m.channels)) {
                assertEquals(c[1].toDouble(), k.off, 1e-12)
                assertEquals(c[2].toDouble(), k.dur, 1e-12)
            }
            if (r[11] == "None") assertEquals(null, m.sep)
            else {
                val s = r[11].trim('(', ')').split(",").map { it.trim().toDouble() }
                assertEquals(s[0], m.sep!![0], 1e-12); assertEquals(s[1], m.sep!![1], 1e-12)
            }
        }
    }

    @Test fun visCodesUnique() {
        val codes = Sstv.MODES.values.map { it.vis }
        assertEquals(codes.size, codes.toSet().size)
        for ((name, m) in Sstv.MODES) assertEquals(name, Sstv.VIS2MODE[m.vis])
    }

    @Test fun colorMatchesPython() {
        for (r in ref("YCC")) {
            val (cr, cg, cb) = r[1].split(",").map { it.toFloat() }
            val ycc = Sstv.rgbToYcc(cr, cg, cb)
            for (i in 0..2) assertEquals(r[1], r[2 + i].toDouble(), ycc[i].toDouble(), 1e-4)
        }
        for (r in ref("RGB")) {
            val (y, u, v) = r[1].split(",").map { it.toFloat() }
            val p = Sstv.yccToRgb(y, u, v)
            assertEquals(r[1], r[2], "${ch(p, 0)},${ch(p, 1)},${ch(p, 2)}")
        }
    }

    @Test fun encoderMatchesPython() {
        for (r in ref("ENC")) {
            val m = Sstv.MODES.getValue(r[1])
            val sig = Sstv.encode(card(m.width, m.height), m.width, m.height, m.name, FS)
            assertEquals(r[1], r[2].toInt(), sig.size)
            val idx = intArrayOf(1000, 20000, 50000, sig.size / 2, sig.size - 7)
            val want = r[3].split(",").map { it.toDouble() }
            for ((j, i) in idx.withIndex()) assertEquals("${r[1]} [$i]", want[j], sig[i].toDouble(), 2e-4)
            var e = 0.0
            for (v in sig) e += v.toDouble() * v
            assertEquals(r[1], r[4].toDouble(), e, r[4].toDouble() * 1e-5)
        }
    }

    @Test fun encoderVisAndScaling() {
        // картинка другого размера масштабируется; тишины нет — сразу лидер 1900 Гц
        val sig = Sstv.encode(card(64, 48), 64, 48, "Robot 36", FS)
        assertEquals(Math.rint(Sstv.modeDuration("Robot 36") * FS).toInt(), sig.size)
        // первые 300 мс — 1900 Гц: сверяем с синусом
        for (i in 0 until 1000) assertEquals(0.5 * sin(2 * PI * 1900 * (i + 1) / FS), sig[i].toDouble(), 1e-5)
    }

    /**
     * Kotlin-декодер на чистом сигнале (такт +0,1 %, +50 Гц) даёт ту же картинку, что Python-декодер:
     * PSNR до сотых дБ, наклон и расстройка до десятых, средние блоков 4×4 по каналам — до округления эталона.
     */
    @Test fun decoderMatchesPython() {
        for (r in ref("DEC")) {
            val mode = r[1]
            val m = Sstv.MODES.getValue(mode)
            val (img, sig, _) = air(mode, 0, snrDb = null)
            val ev = run(SstvDecoder(FS), sig)
            val vis = ev.filterIsInstance<SstvEvent.Vis>().single()
            assertEquals(mode, vis.mode)
            assertEquals(mode, r[2].toDouble(), vis.offset, 0.15)
            val done = ev.filterIsInstance<SstvEvent.Done>().single()
            assertTrue(done.complete)
            assertEquals(mode, r[3].toDouble(), done.slantPpm, 0.15)
            assertEquals(mode, r[4].toDouble(), done.offset, 0.15)
            val p = psnr(done.image, img)
            assertEquals(mode, r[5].toDouble(), p, 0.01)
            val grid = r[6].split(",").map { it.toDouble() }
            var g = 0
            var maxD = 0.0
            for (gy in 0 until 4) for (gx in 0 until 4) {
                val s = DoubleArray(3); var n = 0
                for (y in gy * m.height / 4 until (gy + 1) * m.height / 4)
                    for (x in gx * m.width / 4 until (gx + 1) * m.width / 4) {
                        val px = done.image[y * m.width + x]
                        for (c in 0..2) s[c] += ch(px, c).toDouble()
                        n++
                    }
                for (c in 0..2) { maxD = max(maxD, abs(s[c] / n - grid[g])); g++ }
            }
            assertTrue("$mode: средние блоков отличаются от Python на $maxD", maxD < 0.05)
        }
    }

    // ---------------------------------------------- проверки tests/test_sstv.py

    @Test fun modeRoundtripAllModes() {
        for ((k, mode) in MODE_NAMES.withIndex()) {
            val (img, sig, clk) = air(mode, k)
            val ev = run(SstvDecoder(FS), sig)
            val vis = ev.filterIsInstance<SstvEvent.Vis>()
            assertTrue(mode, vis.isNotEmpty() && vis[0].mode == mode)
            assertEquals(mode, if (k % 2 == 0) 50.0 else -50.0, vis[0].offset, 15.0)
            val done = ev.filterIsInstance<SstvEvent.Done>()
            assertTrue(mode, done.size == 1 && done[0].complete)
            val m = Sstv.MODES.getValue(mode)
            assertEquals(m.width * m.height, done[0].image.size)
            val p = psnr(done[0].image, img)
            assertTrue("$mode: PSNR $p дБ", p >= 20.0)
            // потоковые строки покрывают всю картинку
            val rows = ev.filterIsInstance<SstvEvent.Line>().map { it.y }.toSet()
            assertEquals(mode, (0 until m.height).toSet(), rows)
            // наклон: оценка тактовой ~ ±1000 ppm
            assertEquals(mode, (clk - 1.0) * 1e6, done[0].slantPpm, 150.0)
        }
    }

    /** Приём с середины передачи без VIS: автостарт по синхроимпульсам. */
    @Test fun forceModeWithoutVis() {
        val mode = "PD 120"
        val m = Sstv.MODES.getValue(mode)
        val (img, sig0, _) = air(mode, 0, snrDb = 15.0)
        val skip = 20
        val tCut = 0.5 + 0.91 + skip * m.line + 0.3 * m.line
        val sig = sig0.copyOfRange((tCut * FS * 1.001).toInt(), sig0.size)
        val dec = SstvDecoder(FS)
        dec.forceMode = mode
        val ev = run(dec, sig) + dec.flush()
        assertTrue(ev.none { it is SstvEvent.Vis })
        val starts = ev.filterIsInstance<SstvEvent.Start>()
        assertTrue(starts.isNotEmpty() && starts[0].manual)
        val fin = ev.filter { it is SstvEvent.Done || it is SstvEvent.Lost }
        assertTrue(fin.isNotEmpty())
        val (out, rows) = when (val f = fin[0]) {
            is SstvEvent.Done -> f.image to f.lines
            is SstvEvent.Lost -> f.image to f.lines
            else -> error("")
        }
        val first = (skip + 1) * 2
        assertTrue("строк $rows", rows >= m.height - first - 4)
        val n = min(rows, m.height - first) - 2
        assertTrue(psnr(out, img, n * m.width, 0, first * m.width) >= 20.0)
    }

    @Test fun startManual() {
        val mode = "Martin M1"
        val m = Sstv.MODES.getValue(mode)
        val (img, sig0, _) = air(mode, 1, snrDb = 20.0)
        val sig = sig0.copyOfRange(((0.5 + 0.91) * FS * 0.999).toInt() + 2000, sig0.size)  // без VIS, с начала 1-й строки
        val dec = SstvDecoder(FS)
        dec.startManual(mode)
        assertEquals("pending", dec.level().state)
        assertEquals(mode, dec.level().mode)
        val ev = run(dec, sig) + dec.flush()
        val fin = ev.filter { it is SstvEvent.Done || it is SstvEvent.Lost }
        assertTrue(fin.isNotEmpty())
        val out = (fin[0] as? SstvEvent.Done)?.image ?: (fin[0] as SstvEvent.Lost).image
        // первая строка могла уйти до старта — ищем сдвиг 0..1
        val n = (m.height - 2) * m.width
        val best = (0..1).maxOf { s -> psnr(out, img, n, 0, s * m.width) }
        assertTrue("PSNR $best", best >= 20.0)
    }

    @Test fun lostOnCut() {
        val mode = "Scottie S1"
        val m = Sstv.MODES.getValue(mode)
        val (_, sig0, _) = air(mode, 0, snrDb = 20.0)
        val cut = ((0.5 + 0.91 + 0.3 * m.lines * m.line) * FS).toInt()
        val rng = Random(5)
        val sig = sig0.copyOf(cut + 6 * FS.toInt())
        for (i in cut until sig.size) sig[i] = (rng.nextGaussian() * 0.1).toFloat()
        val lost = run(SstvDecoder(FS), sig).filterIsInstance<SstvEvent.Lost>()
        assertTrue(lost.isNotEmpty())
        assertTrue("строк ${lost[0].lines}", lost[0].lines > 0.2 * m.height && lost[0].lines < 0.4 * m.height)
    }

    @Test fun noiseOnlyNoEvents() {
        val rng = Random(3)
        val sig = FloatArray(60 * FS.toInt()) { (rng.nextGaussian() * 0.3).toFloat() }
        assertEquals(emptyList<SstvEvent>(), run(SstvDecoder(FS), sig))
    }

    @Test fun twoImagesBackToBack() {
        val a = Sstv.encode(card(320, 240), 320, 240, "Robot 36", FS)
        val b = Sstv.encode(card(320, 256), 320, 256, "Martin M2", FS)
        val gap = FS.toInt() / 3
        val sig = FloatArray(a.size + gap + b.size + FS.toInt())
        System.arraycopy(a, 0, sig, 0, a.size)
        System.arraycopy(b, 0, sig, a.size + gap, b.size)
        val done = run(SstvDecoder(FS), sig).filterIsInstance<SstvEvent.Done>().map { it.mode }
        assertEquals(listOf("Robot 36", "Martin M2"), done)
    }

    /** Как test_decode_file_44100 (без WAV): весь звук 44,1 кГц через decodeAll. */
    @Test fun decodeAll44100() {
        val img = card(320, 240)
        val enc = Sstv.encode(img, 320, 240, "Robot 72", 44100.0)
        val sig = FloatArray(22050 + enc.size + 44100)
        // как WAV int16: квантование до 1/32767
        for (i in enc.indices) sig[22050 + i] = ((enc[i] * 32767).toInt().toShort() / 32767.0).toFloat()
        val imgs = SstvDecoder.decodeAll(sig, 44100.0)
        assertEquals(1, imgs.size)
        assertTrue(psnr((imgs[0] as SstvEvent.Done).image, img) >= 22.0)
    }

    @Test fun level() {
        val dec = SstvDecoder(FS)
        val n = FS.toInt() / 10
        dec.process(FloatArray(n) { (0.5 * sin(2 * PI * 1900 * it / FS)).toFloat() })
        val lv = dec.level()
        assertEquals(1900.0, lv.freq, 5.0)
        assertEquals("idle", lv.state)
        assertFalse(lv.amp <= 0.0)
    }

    @Test fun progressAndFlush() {
        // обрыв посреди картинки: flush отдаёт незаконченную картинку как Done(complete = false)
        val m = Sstv.MODES.getValue("Robot 36")
        val enc = Sstv.encode(card(320, 240), 320, 240, "Robot 36", FS)
        val dec = SstvDecoder(FS)
        val half = (enc.size * 0.6).toInt()
        val ev = run(dec, enc.copyOf(half))
        val lines = ev.filterIsInstance<SstvEvent.Line>()
        assertTrue(lines.isNotEmpty())
        assertTrue(lines.all { it.mode == "Robot 36" && it.height == m.height && it.rgb.size == m.width })
        val lv = dec.level()
        assertEquals("image", lv.state)
        assertEquals("Robot 36", lv.mode)
        assertTrue(lv.line in 1 until m.height)
        val fin = dec.flush().single() as SstvEvent.Done
        assertFalse(fin.complete)
        assertTrue(fin.lines in 1 until m.height)
        assertEquals("idle", dec.level().state)
    }
}
