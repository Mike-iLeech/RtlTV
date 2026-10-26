package io.github.mikeileech.sdr4noob

import io.github.mikeileech.sdr4noob.dsp.AmbeSynth
import io.github.mikeileech.sdr4noob.dsp.DmrCodes
import io.github.mikeileech.sdr4noob.dsp.DmrDecoder
import io.github.mikeileech.sdr4noob.dsp.DmrEvent
import io.github.mikeileech.sdr4noob.dsp.DmrGen
import io.github.mikeileech.sdr4noob.dsp.Modulation
import io.github.mikeileech.sdr4noob.dsp.RadioDemod
import io.github.mikeileech.sdr4noob.sdr.DemoSource
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.math.BigInteger

/** DMR — перенос rtltv/dmr.py; эталоны посчитаны Python-модулем (коды, пакеты, события декодера). */
class DmrTest {
    private fun hex(bits: IntArray) = "0x" + BigInteger(bits.joinToString("").ifEmpty { "0" }, 2).toString(16)

    /** Синтезатор-заглушка: запоминает кадры, которые декодер отдал на озвучку. */
    private class Fake : AmbeSynth {
        val frames = ArrayList<ByteArray>()
        override fun reset() {}
        override fun decode(fr: ByteArray, out: FloatArray): Int { frames.add(fr.copyOf()); out.fill(0.1f); return 0 }
        override val ok = true
    }

    private val lc = DmrGen.makeLc(2501234, 91, true)

    @Test fun codesMatchPython() {
        assertEquals(371206, DmrCodes.golay2087(0x5A))
        assertEquals(43586, DmrCodes.qr1676(0x55))
        assertArrayEquals(intArrayOf(203, 152, 252), DmrCodes.rs129Parity(lc))
        assertEquals(56455, DmrCodes.crcCcitt(IntArray(10) { it }))
        val b96 = IntArray(96) { (it * it + it / 3) % 2 }
        assertEquals("0x40fc8027f8781fe0019f0361f8400df0013fca00fd004bf8d", hex(DmrCodes.bptcEncode(b96)))
        assertEquals("0x30c0c030a0a05000f110c0512120012", hex(DmrCodes.embLcEncode(lc)))
        assertEquals("0x28480762e404c84448", hex(DmrCodes.ambeEncode(DmrCodes.ambeParams(40, 0, 17, 0, 0))))
    }

    @Test fun burstsMatchPython() {
        assertEquals("0x2b50db012bc14e8539025c0046dff57d75df5de3344087826a03cc03cc142822e", hex(DmrGen.lcBurst(1, lc)))
        assertEquals("0x52405d37ac531277340bd016465dff57d75df5d92a42eff5180b4e2a2e0f5490a9", hex(DmrGen.idleBurst(1)))
        val fr = DmrGen.toneFrames(40)
        assertEquals("0x6e640573b220c4c88c28480762e170a0a050074404c8444828480762f404c84448",
            hex(DmrGen.voiceBurst(fr.subList(0, 3), 2, 1, DmrCodes.embLcEncode(lc).copyOfRange(32, 64), 3)))
        assertEquals("0x264c1950b20173430d96b12c4cdff57d75df5daca6a3308089061643f1839a12a",
            hex(DmrGen.csbkBurst(1, intArrayOf(0x3D, 0, 0x40, 3, 0, 0, 91, 0x26, 0x2a, 0x72))))
        assertArrayEquals(intArrayOf(1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0), DmrGen.cach(2))
    }

    @Test fun codesCorrectErrors() {
        // RS(12,9): одна ошибка в байте исправляется
        val cw = lc + DmrCodes.rs129Parity(lc)
        val bad = cw.copyOf(); bad[4] = bad[4] xor 0x5A
        assertArrayEquals(cw, DmrCodes.rs129Check(bad))
        // BPTC: по одной ошибке в строке исправляется
        val b96 = IntArray(96) { (it * 7) % 3 % 2 }
        val raw = DmrCodes.bptcEncode(b96)
        raw[10] = raw[10] xor 1; raw[100] = raw[100] xor 1
        assertArrayEquals(b96, DmrCodes.bptcDecode(raw).second)
        // встроенный LC
        val emb = DmrCodes.embLcEncode(lc)
        emb[17] = emb[17] xor 1
        assertArrayEquals(lc, DmrCodes.embLcDecode(emb))
        assertNull(DmrCodes.embLcDecode(IntArray(128) { it % 2 }))
    }

    private fun decodeAll(sig: FloatArray, fake: MutableList<Fake>): Pair<List<DmrEvent>, DmrDecoder> {
        val d = DmrDecoder { Fake().also { fake.add(it) } }
        val ev = ArrayList<DmrEvent>()
        var i = 0
        while (i < sig.size) {
            val n = minOf(2400, sig.size - i)
            ev.addAll(d.process(sig.copyOfRange(i, i + n), n).second)
            i += n
        }
        return ev to d
    }

    /** Синтетический эфир (как test_signal в dmr.py): те же события и та же статистика, что у Python-декодера. */
    @Test fun decoderMatchesPython() {
        val fakes = ArrayList<Fake>()
        val (ev, d) = decodeAll(DmrGen.testSignal(), fakes)
        assertEquals(listOf(
            "call_start 1 2501234 91 true групповой вызов 2501234 → TG 91",
            "call_end 1 2501234 91 true конец вызова 2501234 → TG 91",
            "sync_lost 0 0 0 true потеря синхронизации DMR",
            "call_start 2 2507777 2501234 false индивидуальный вызов 2507777 → 2501234",
            "call_end 2 2507777 2501234 false конец вызова 2507777 → 2501234",
            "sync_lost 0 0 0 true потеря синхронизации DMR"),
            ev.map { "${it.type} ${it.slot} ${it.src} ${it.dst} ${it.group} ${it.text}" })
        assertEquals(mapOf("bursts" to 352, "syncs" to 204, "voice" to 120, "lc_ok" to 8, "csbk" to 0, "emb_ok" to 100), d.stats)
        // голос: на озвучку ушли именно закодированные кадры AMBE (после деперемежения)
        val sent = fakes.flatMap { it.frames }.map { it.toList() }.toSet()
        val want = (DmrGen.toneFrames(40) + DmrGen.toneFrames(25)).map { DmrCodes.ambeDeinterleave(it).toList() }.toSet()
        assertTrue("кадров: ${sent.size}", sent.isNotEmpty() && want.containsAll(sent))
    }

    /** Сдвиг частоты и инверсия полярности дискриминатора не мешают. */
    @Test fun offsetAndInvertedPolarity() {
        val sig = DmrGen.testSignal()
        val inv = FloatArray(sig.size) { -sig[it] + 300f }
        val (ev, _) = decodeAll(inv, ArrayList())
        assertEquals(2, ev.count { it.type == "call_start" })
    }

    /** Поиск DMR (перенос dmrscan.py): в окне тестового эфира у 446 МГц находится DMR на 446,1 МГц, CC 1. */
    @Test fun scannerFindsDmr() {
        val fs = 960_000
        val sc = io.github.mikeileech.sdr4noob.dsp.DmrScanner(fs, 446.0e6, 446.2e6)
        assertEquals(1, sc.centers.size)
        val center = sc.centers[0]
        val src = DemoSource()
        src.open(fs, center)
        src.start()
        val t0 = DemoSource::class.java.getDeclaredField("t0").apply { isAccessible = true }
        val re = FloatArray(sc.nsamp); val im = FloatArray(sc.nsamp)
        val block = fs / 20
        val br = FloatArray(block); val bi = FloatArray(block)
        var fill = 0
        t0.setLong(src, t0.getLong(src) - 600_000_000L)      // начать с 0,6 с сигнала — там уже идёт вызов
        while (fill < sc.nsamp) {
            t0.setLong(src, t0.getLong(src) - 50_000_000L)
            val k = src.read(br, bi, block)
            val c = minOf(k, sc.nsamp - fill)
            System.arraycopy(br, 0, re, fill, c); System.arraycopy(bi, 0, im, fill, c)
            fill += c
        }
        val found = sc.process(center, re, im, fill, 0)
        val dmr = sc.snapshot().filter { it.kind == "dmr" }
        assertEquals(found.joinToString(), 1, dmr.size)
        assertEquals(446.1e6, dmr[0].freq, 1.0)
        assertEquals(listOf(1), dmr[0].cc)
    }

    @Test fun scanPlanLikeDesktop() {
        // эталон: rtltv.dmrscan.plan_windows(430e6, 440e6, 2_400_000)
        assertEquals(listOf(430834375L, 432503125L, 434171875L, 435834375L, 437503125L, 439171875L),
            io.github.mikeileech.sdr4noob.dsp.DmrScanner.planWindows(430e6, 440e6, 2_400_000))
    }

    /** Полная цепочка: тестовый эфир 446,1 МГц → RadioDemod (DMR) → события. */
    @Test fun demoStationThroughRadioChain() {
        val fs = 960_000
        val src = DemoSource()
        src.open(fs, 446_100_000L + 120_000)
        src.start()
        val t0 = DemoSource::class.java.getDeclaredField("t0").apply { isAccessible = true }
        val d = RadioDemod(fs, Modulation.DMR).apply { offsetHz = -120_000.0 }   // без mbelib (JVM) голос — тишина
        val block = fs / 20
        val re = FloatArray(block); val im = FloatArray(block)
        val aud = FloatArray(2 * (block / 5 + 64))
        var got = 0
        while (got < fs * 14) {
            t0.setLong(src, t0.getLong(src) - 50_000_000L)   // без ожидания реального времени
            val k = src.read(re, im, block)
            if (k > 0) { d.process(re, im, k, aud); got += k }
        }
        val ev = d.dmrEvents.toList()
        assertTrue(ev.joinToString { it.text }, ev.any { it.type == "call_start" && it.src == 2501234 && it.dst == 91 })
        assertTrue(ev.any { it.type == "call_start" && it.src == 2507777 && !it.group })
    }
}
