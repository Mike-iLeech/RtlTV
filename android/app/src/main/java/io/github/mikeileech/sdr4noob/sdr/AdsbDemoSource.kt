package io.github.mikeileech.sdr4noob.sdr

import io.github.mikeileech.sdr4noob.dsp.AdsbDemo

/**
 * Тестовый эфир ADS-B без приёмника (как демо в окне «Самолёты» на ПК): борта вокруг Москвы на 1090 МГц,
 * отсчёты в реальном темпе. Частота и полоса фиксированы — 1090 МГц, 2,4 МГц.
 */
class AdsbDemoSource : SdrSource {
    companion object { const val FS = 2_400_000 }

    override val label = "Тестовый сигнал ADS-B (без приёмника)"
    override val gains = IntArray(0)
    override val fixedRate: Int get() = FS
    override val fixedCenter: Long get() = 1_090_000_000L
    override val sampleRates get() = intArrayOf(FS)
    override val retunable get() = false

    private val demo = AdsbDemo(FS)
    private var t0 = 0L
    private var produced = 0L

    override fun open(sampleRate: Int, frequencyHz: Long) { demo.open() }
    override fun start() { t0 = System.nanoTime(); produced = 0 }

    override fun read(re: FloatArray, im: FloatArray, maxSamples: Int): Int {
        val due = (System.nanoTime() - t0) / 1000 * FS / 1_000_000L
        if (due - produced > FS) produced = due - maxSamples
        val lag = due - produced
        if (lag < maxSamples) { Thread.sleep(maxOf(1L, (maxSamples - lag) * 1000L / FS)); return 0 }
        demo.render(maxSamples, re, im)
        produced += maxSamples
        return maxSamples
    }

    override fun setFrequency(hz: Long) {}
    override fun setSampleRate(rate: Int) {}
    override fun setGain(tenthsDb: Int) {}
    override fun close() {}
}
