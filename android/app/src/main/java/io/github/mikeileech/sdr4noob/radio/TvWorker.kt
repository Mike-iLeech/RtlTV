package io.github.mikeileech.sdr4noob.radio

import io.github.mikeileech.sdr4noob.dsp.AnalogFrame
import io.github.mikeileech.sdr4noob.dsp.AnalogVideoDecoder
import io.github.mikeileech.sdr4noob.dsp.MuxAnalyzer
import io.github.mikeileech.sdr4noob.dsp.MuxResult
import io.github.mikeileech.sdr4noob.dsp.Tv
import io.github.mikeileech.sdr4noob.dsp.TvChannel
import io.github.mikeileech.sdr4noob.dsp.TvMode
import io.github.mikeileech.sdr4noob.dsp.TvScan
import io.github.mikeileech.sdr4noob.dsp.TvScanResult
import io.github.mikeileech.sdr4noob.dsp.TvStandard
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import kotlin.math.roundToLong

/**
 * Измеритель ТВ на своём потоке (как Engine режима ТВ на ПК): сырой IQ от движка через [feed]; сам перестраивает
 * приёмник ([RadioEngine.setCenter]) по плану обхода мультиплекса или поиска каналов. Итоги — в @Volatile полях,
 * [onUpdate] вызывается из потока измерителя после каждого нового итога.
 */
class TvWorker(private val engine: RadioEngine, private val onUpdate: () -> Unit, private val onError: (String) -> Unit = {}) {
    private sealed class Job {
        class Measure(val ch: TvChannel) : Job()
        class Analog(val ch: TvChannel, val std: TvStandard) : Job()
        class Scan(val kind: TvMode, val chans: List<TvChannel>, val std: TvStandard) : Job()
    }

    private val queue = ArrayBlockingQueue<Pair<FloatArray, FloatArray>>(48)
    @Volatile private var running = true
    @Volatile private var job: Job? = null
    @Volatile private var jobSerial = 0

    @Volatile var mux: MuxResult? = null; private set
    @Volatile var frame: AnalogFrame? = null; private set
    @Volatile var scan: List<TvScanResult> = emptyList(); private set
    /** Поиск: (сделано, всего); null — поиск не идёт. */
    @Volatile var scanProgress: Pair<Int, Int>? = null; private set
    @Volatile var hops = 0; private set
    private val thread = Thread({ loop() }, "tv").apply { isDaemon = true; start() }   // последним: поля уже заданы

    fun feed(re: FloatArray, im: FloatArray, n: Int) {
        if (!running || n <= 0 || job == null) return
        queue.offer(re.copyOf(n) to im.copyOf(n))           // не успевает — блок отбрасывается
    }

    fun measure(ch: TvChannel) = set(Job.Measure(ch))
    fun analog(ch: TvChannel, std: TvStandard) = set(Job.Analog(ch, std))
    fun scan(kind: TvMode, chans: List<TvChannel>, std: TvStandard) = set(Job.Scan(kind, chans, std))
    fun stopJob() = set(null)

    @Synchronized private fun set(j: Job?) { job = j; jobSerial++; queue.clear() }

    fun stop() { running = false; thread.interrupt() }

    private class Changed : Exception()

    private fun loop() {
        var reported = false
        while (running) {
            try {
                val serial = jobSerial
                when (val j = job) {
                    null -> { queue.poll(250, TimeUnit.MILLISECONDS) }
                    is Job.Measure -> runMeasure(j, serial)
                    is Job.Analog -> runAnalog(j, serial)
                    is Job.Scan -> runScan(j, serial)
                }
            } catch (_: InterruptedException) {
                break
            } catch (_: Changed) {
            } catch (e: Exception) {
                if (!reported) { reported = true; onError("ТВ: сбой измерителя (${e.javaClass.simpleName}), измерение начато заново") }
                try { Thread.sleep(200) } catch (_: InterruptedException) { break }
            }
        }
    }

    private fun check(serial: Int) { if (serial != jobSerial || !running) throw Changed() }

    /** Следующий блок IQ текущей задачи (ждёт, пока задача не сменится). */
    private fun next(serial: Int): Pair<FloatArray, FloatArray> {
        while (true) {
            check(serial)
            queue.poll(250, TimeUnit.MILLISECONDS)?.let { return it }
        }
    }

    /** Перестроить приёмник на [f] и дождаться отсчётов уже с новой частоты (+2 блока на переходный процесс). */
    private fun retune(f: Double, serial: Int) {
        val hz = f.roundToLong()
        engine.setCenter(hz)
        queue.clear()
        val t0 = System.nanoTime()
        while (engine.centerHz != hz) {
            next(serial)
            if (System.nanoTime() - t0 > 3_000_000_000L) break     // источник с фиксированным центром (запись)
        }
        repeat(2) { next(serial) }
    }

    private fun capture(n: Int, serial: Int): Pair<FloatArray, FloatArray> {
        val re = FloatArray(n); val im = FloatArray(n)
        var have = 0
        while (have < n) {
            val (r, i) = next(serial)
            val k = minOf(r.size, n - have)
            System.arraycopy(r, 0, re, have, k); System.arraycopy(i, 0, im, have, k)
            have += k
        }
        return re to im
    }

    /** Полоса уже 2 МГц (запись IQ со своей частотой) — измерять нечем: сказать один раз и ждать другой задачи. */
    private fun rateOk(serial: Int): Boolean {
        if (engine.sampleRate >= Tv.RATES.min()) return true
        onError("Полоса ${engine.sampleRate / 1e6} МГц мала для ТВ: нужна не уже 2,4 МГц (у записи IQ полоса своя)")
        while (true) next(serial)
    }

    private fun runMeasure(j: Job.Measure, serial: Int) {
        rateOk(serial)
        val fs = engine.sampleRate.toDouble()
        val an = MuxAnalyzer(fs, j.ch.center, j.ch.bandwidth)
        mux = null; hops = 0
        while (true) {
            for (h in an.plan(withCp = true)) {
                retune(h.freq, serial)
                val (re, im) = capture(h.samples, serial)
                hops++
                an.add(h.freq, re, im)?.let { mux = it; onUpdate() }
            }
        }
    }

    private fun runAnalog(j: Job.Analog, serial: Int) {
        rateOk(serial)
        val fs = engine.sampleRate.toDouble()
        retune(Tv.receiverCenter(TvMode.ANALOG, false, j.std, j.ch.video, j.ch.center), serial)
        val dec = AnalogVideoDecoder(fs, j.std)
        frame = null
        while (true) {
            val (re, im) = next(serial)
            val fr = dec.feed(re, im)
            if (fr.isNotEmpty()) { frame = fr.last(); onUpdate() }
        }
    }

    private fun runScan(j: Job.Scan, serial: Int) {
        rateOk(serial)
        val fs = engine.sampleRate.toDouble()
        val items = TvScan.items(j.kind, j.chans, j.std)
        val res = ArrayList<TvScanResult>()
        scan = emptyList()
        try {
            for ((i, h) in items.withIndex()) {
                scanProgress = i to items.size; onUpdate()
                retune(h.freq, serial)
                val (re, im) = capture(h.samples, serial)
                TvScan.step(res, j.kind, j.chans[i], i, items.size, re, im, fs, j.std)
                // копии: finishDigital на последнем шаге меняет значения, а интерфейс читает список
                scan = res.map { TvScanResult(it.ch, it.kind, it.value, it.found) }
            }
        } finally {
            scanProgress = null
        }
        onUpdate()
        synchronized(this) { if (serial == jobSerial) job = null }     // новая задача, поставленная только что, не теряется
    }
}
