package io.github.mikeileech.sdr4noob.sdr

import io.github.mikeileech.sdr4noob.dsp.TvTestSignal

/**
 * Тестовый эфир режима ТВ без приёмника (как на ПК): аналоговая станция с испытательной таблицей на 583,25 МГц
 * (ДМВ 35) и мультиплекс DVB-T2 на 546 МГц (ДМВ 30). Отсчёты в реальном темпе; частоту можно перестраивать.
 */
class TvDemoSource(private var fs: Int = 2_400_000) : SdrSource {
    override val label = "Тестовый сигнал ТВ (без приёмника)"
    override val gains = IntArray(0)
    override val sampleRates get() = io.github.mikeileech.sdr4noob.sdr.TV_RATES

    private var sig = TvTestSignal(fs.toDouble())
    private var center = 546_000_000L
    private var t0 = 0L
    private var produced = 0L

    override fun open(sampleRate: Int, frequencyHz: Long) { setSampleRate(sampleRate); center = frequencyHz }
    override fun start() { t0 = System.nanoTime(); produced = 0 }

    override fun read(re: FloatArray, im: FloatArray, maxSamples: Int): Int {
        val due = (System.nanoTime() - t0) / 1000 * fs / 1_000_000L
        if (due - produced > fs) produced = due - maxSamples
        val lag = due - produced
        if (lag < maxSamples) { Thread.sleep(maxOf(1L, (maxSamples - lag) * 1000L / fs)); return 0 }
        val raw = sig.render(center.toDouble(), maxSamples, 30.0)
        val (r, i) = TvTestSignal.u8ToIq(raw)
        System.arraycopy(r, 0, re, 0, maxSamples); System.arraycopy(i, 0, im, 0, maxSamples)
        produced += maxSamples
        return maxSamples
    }

    override fun setFrequency(hz: Long) { center = hz }
    override fun setSampleRate(rate: Int) { if (rate != fs) { fs = rate; sig = TvTestSignal(fs.toDouble()) } }
    override fun setGain(tenthsDb: Int) {}
    override fun close() {}
}

/** Полосы обзора режима ТВ (Tv.RATES): не уже 2 МГц, иначе обход мультиплекса не сходится. */
val TV_RATES = io.github.mikeileech.sdr4noob.dsp.Tv.RATES
