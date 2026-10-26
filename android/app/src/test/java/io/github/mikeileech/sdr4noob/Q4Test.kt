package io.github.mikeileech.sdr4noob

import io.github.mikeileech.sdr4noob.dsp.MuxAnalyzer
import io.github.mikeileech.sdr4noob.dsp.Tv
import io.github.mikeileech.sdr4noob.dsp.TvTestSignal
import io.github.mikeileech.sdr4noob.lrpt.AutoLrptReceiver
import io.github.mikeileech.sdr4noob.lrpt.LrptCodec
import io.github.mikeileech.sdr4noob.sdr.LrptDemoGen
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Обвязка очереди 4: тестовый эфир «Метеора» и ТВ проходят через свои декодеры. */
class Q4Test {
    /** Генератор LRPT на 1,2 МГц (пересчёт из 1,152 МГц) → приёмник: кадры без ошибок, есть строки каналов 1–3. */
    @Test fun lrptDemoDecodes() {
        LrptCodec.useNative = false                       // на JVM нативной библиотеки нет
        val fs = 1_200_000
        val gen = LrptDemoGen(fs)
        val rx = AutoLrptReceiver(fs.toDouble())
        val n = fs / 20
        val re = FloatArray(n); val im = FloatArray(n)
        repeat(160) {                                     // 8 с сигнала
            gen.next(n, re, im)
            val iq = FloatArray(2 * n)
            for (i in 0 until n) { iq[2 * i] = re[i]; iq[2 * i + 1] = im[i] }
            rx.feed(iq, wantCons = false)
        }
        val st = rx.stats()
        assertTrue("кадров ${st.framesOk}/${st.frames}", st.framesOk >= 40)
        assertEquals(st.frames, st.framesOk)
        assertEquals(listOf(64, 65, 66), st.channels)
        assertTrue("строк ${st.lines}", st.lines > 0)
    }

    /** Обход мультиплекса ДМВ 30 тестового эфира ТВ на 2,4 МГц находит DVB-T2 32K, защитный интервал 1/16. */
    @Test fun tvMuxOnDemo() {
        val fs = 2_400_000
        val sig = TvTestSignal(fs.toDouble())
        val ch = Tv.channels("RU").first { Math.abs(it.center - 546e6) < 1e5 }
        val an = MuxAnalyzer(fs.toDouble(), ch.center, ch.bandwidth)
        var res = an.plan(withCp = true).mapNotNull { h ->
            val (re, im) = TvTestSignal.u8ToIq(sig.render(h.freq, h.samples, 30.0))
            an.add(h.freq, re, im)
        }.lastOrNull()
        assertTrue(res != null && res.present)
        val o = res!!.ofdm ?: an.cp!!          // длинный захват OFDM может идти последним в плане
        assertTrue(o.found)
        assertEquals("32K", o.mode)
        assertEquals("1/16", o.guard)
    }
}
