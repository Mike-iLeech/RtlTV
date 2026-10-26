package io.github.mikeileech.sdr4noob.sdr

import io.github.mikeileech.sdr4noob.dsp.StreamResampler
import io.github.mikeileech.sdr4noob.lrpt.Lrpt
import io.github.mikeileech.sdr4noob.lrpt.LrptGray
import io.github.mikeileech.sdr4noob.lrpt.LrptSynth

/**
 * Бесконечный сигнал «Метеора» LRPT для тестового эфира (как SynthSource на ПК): картинка из трёх каналов
 * идёт полосами по 8 строк — пакеты CCSDS → VCDU → CADU → OQPSK 72k (16 отсчётов на символ, 1,152 МГц) →
 * пересчёт в частоту дискретизации приёмника [fs]. Отдаёт комплексные отсчёты по запросу.
 */
class LrptDemoGen(private val fs: Int) {
    companion object {
        private const val SPS = 16
        private const val BASE = Lrpt.SYMBOL_RATE * SPS        // 1 152 000
        private const val LINES = 480                         // высота картинки до повтора

        /** Картинка трёх каналов (64, 65, 66) — общая для всех генераторов, считается один раз. */
        private val IMAGES: Map<Int, LrptGray> by lazy { (0 until 3).associate { 64 + it to LrptSynth.synthImage(LINES, it) } }
    }

    private val mod = LrptSynth.Modulator(SPS, oqpsk = true, nrzm = true)
    private val rs = if (fs != BASE) StreamResampler(BASE, fs) else null
    private var line = 0
    private var seq = 0
    private var count = 0
    private var bufR = FloatArray(0); private var bufI = FloatArray(0)
    private var have = 0

    /** Следующие [n] отсчётов (амплитуда ≈ 1) в [re]/[im]. */
    fun next(n: Int, re: FloatArray, im: FloatArray) {
        while (have < n) produce()
        System.arraycopy(bufR, 0, re, 0, n); System.arraycopy(bufI, 0, im, 0, n)
        System.arraycopy(bufR, n, bufR, 0, have - n); System.arraycopy(bufI, n, bufI, 0, have - n)
        have -= n
    }

    private fun produce() {
        // полоса 8 строк: пакеты всех каналов → VCDU → CADU → модулятор
        val y = (line * 8) % LINES
        val strip = IMAGES.mapValues { (_, g) -> LrptGray(g.width, 8, g.px.copyOfRange(y * g.width, (y + 8) * g.width)) }
        val pk = LrptSynth.synthPackets(strip, line0 = line, seq0 = seq)
        seq += pk.size; line++
        val vc = LrptSynth.packetsToVcdus(pk, count0 = count)
        count += vc.size
        val iq = mod.modulate(LrptSynth.vcdusToCadus(vc))
        val m = iq.size / 2
        val r = FloatArray(m) { iq[2 * it] }; val i = FloatArray(m) { iq[2 * it + 1] }
        val rr: FloatArray; val ii: FloatArray; val k: Int
        val s = rs
        if (s == null) { rr = r; ii = i; k = m } else {
            rr = FloatArray(s.outFor(m) + 4); ii = FloatArray(rr.size)
            k = s.process(r, i, m, rr, ii)
        }
        if (have + k > bufR.size) { bufR = bufR.copyOf(maxOf(2 * bufR.size, have + k)); bufI = bufI.copyOf(bufR.size) }
        System.arraycopy(rr, 0, bufR, have, k); System.arraycopy(ii, 0, bufI, have, k)
        have += k
    }
}
