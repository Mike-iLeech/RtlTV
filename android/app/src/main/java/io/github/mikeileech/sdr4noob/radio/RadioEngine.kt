package io.github.mikeileech.sdr4noob.radio

import io.github.mikeileech.sdr4noob.dsp.Modulation
import io.github.mikeileech.sdr4noob.dsp.RadioDemod
import io.github.mikeileech.sdr4noob.dsp.SpectrumAnalyzer
import io.github.mikeileech.sdr4noob.sdr.SdrException
import io.github.mikeileech.sdr4noob.sdr.SdrSource
import java.util.concurrent.ConcurrentLinkedQueue
import kotlin.math.abs

/**
 * Движок радиорежима: один поток читает приёмник, считает спектр (для водопада) и демодулирует
 * канал в звук. Все обращения к приёмнику — только из этого потока: настройки из интерфейса
 * ставятся в очередь команд и выполняются между блоками (приёмнику не нужна потокобезопасность).
 */
class RadioEngine(
    private val source: SdrSource,
    sampleRate: Int,
    vfoHz: Long,
    mode: Modulation,
    private val audio: AudioOut = AudioOut(),
) {
    interface Listener {
        fun onOpened(engine: RadioEngine) {}
        fun onSpectrum() {}
        fun onError(message: String) {}
    }

    @Volatile var listener: Listener? = null
    @Volatile var sampleRate = sampleRate; private set
    @Volatile var centerHz = vfoHz + offsetFor(sampleRate); private set
    @Volatile var vfoHz = vfoHz; private set
    @Volatile var mode = mode; private set
    @Volatile var running = false; private set
    val label get() = source.label
    val gains get() = source.gains
    val hasBiasTee get() = source.hasBiasTee

    private val commands = ConcurrentLinkedQueue<() -> Unit>()
    private var thread: Thread? = null
    private var demod = RadioDemod(sampleRate, mode)
    @Volatile private var stereoWanted = true
    @Volatile private var squelch = -200f

    // последний спектр для интерфейса
    val spectrumSize = 2048
    private val specLock = Any()
    private val specOut = FloatArray(spectrumSize)
    private var specCenter = 0L
    private var specRate = 0
    private var specSerial = 0L

    val levelDb get() = demod.levelDb
    val stereoLock get() = demod.stereoLock && demod.mode == Modulation.WFM
    val squelchOpen get() = demod.squelchOpen
    @Volatile var dropped = 0L; private set

    fun start() {
        if (running) return
        running = true
        // предыдущий движок мог ещё закрывать тот же приёмник — новый открывает его только после этого
        val prev = previous
        val t = Thread({ prev?.join(); loop() }, "sdr4noob-radio").apply { priority = Thread.MAX_PRIORITY }
        previous = t
        thread = t
        t.start()
    }

    /** Остановить без ожидания (UI-поток не блокируется): поток сам закроет приёмник и звук. */
    fun stop() {
        listener = null
        running = false
        thread = null
    }

    /** Настроиться на станцию. В пределах полосы приёмника меняется только VFO, иначе — и центр. */
    fun tune(hz: Long) {
        vfoHz = hz   // сразу — чтобы шкала не прыгала назад до выполнения команды
        post { vfoHz = hz; tuneNow(hz) }
    }

    /** Сдвинуть центр приёмника (перетаскивание спектра), VFO остаётся на станции, если она в полосе. */
    fun setCenter(hz: Long) = post {
        centerHz = hz
        source.setFrequency(hz)
        val half = sampleRate / 2 - mode.defaultBw / 2
        if (abs(vfoHz - hz) > half) vfoHz = hz - offsetFor(sampleRate)
        demod.offsetHz = (vfoHz - centerHz).toDouble()
    }

    fun setMode(m: Modulation) = post {
        mode = m
        demod.setMode(m)
        tuneNow(vfoHz)
    }

    private fun tuneNow(hz: Long) {
        val off = hz - centerHz
        if (abs(off) > sampleRate / 2 - mode.defaultBw / 2 - 20_000 || abs(off) < 15_000) {
            centerHz = hz + offsetFor(sampleRate)
            source.setFrequency(centerHz)
        }
        demod.offsetHz = (vfoHz - centerHz).toDouble()
    }

    fun setSampleRate(rate: Int) = post {
        sampleRate = rate
        source.setSampleRate(rate)
        demod = RadioDemod(rate, mode).also { it.stereo = stereoWanted; it.squelchDb = squelch }
        centerHz = vfoHz + offsetFor(rate)
        source.setFrequency(centerHz)
        demod.offsetHz = (vfoHz - centerHz).toDouble()
    }

    fun setGain(tenths: Int) = post { source.setGain(tenths) }
    fun setPpm(ppm: Int) = post { source.setPpm(ppm) }
    fun setBiasTee(on: Boolean) = post { source.setBiasTee(on) }
    fun setStereo(on: Boolean) { stereoWanted = on; post { demod.stereo = on } }
    fun setSquelch(db: Float) { squelch = db; post { demod.squelchDb = db } }
    var volume: Float
        get() = audio.volume
        set(v) { audio.volume = v }

    private fun post(cmd: () -> Unit) { commands.add(cmd) }

    /** Скопировать последний спектр в [out]; возвращает (центр, частота дискретизации, номер кадра). */
    fun copySpectrum(out: FloatArray): Triple<Long, Int, Long> = synchronized(specLock) {
        System.arraycopy(specOut, 0, out, 0, spectrumSize)
        Triple(specCenter, specRate, specSerial)
    }

    private fun loop() {
        val analyzer = SpectrumAnalyzer(spectrumSize, 4)
        val spec = FloatArray(spectrumSize)
        try {
            source.open(sampleRate, centerHz)
            if (!running) return   // закрыли, пока открывался приёмник: finally закроет его
            demod.offsetHz = (vfoHz - centerHz).toDouble()
            demod.stereo = stereoWanted
            source.start()
            listener?.onOpened(this)
            var block = sampleRate / 20
            var re = FloatArray(block); var im = FloatArray(block)
            var aud = FloatArray(2 * (block / 5 + 8))
            var dcRe = 0f; var dcIm = 0f
            var lastSpec = 0L
            while (running) {
                while (true) { (commands.poll() ?: break).invoke() }
                if (sampleRate / 20 != block) {
                    block = sampleRate / 20
                    re = FloatArray(block); im = FloatArray(block); aud = FloatArray(2 * (block / 5 + 8))
                }
                val n = source.read(re, im, block)
                if (n == 0) continue
                // удаление постоянной составляющей (пик в центре спектра у донглов с прямым преобразованием)
                var sr = 0f; var si = 0f
                var i = 0
                while (i < n) { sr += re[i]; si += im[i]; i += 4 }
                val cnt = (n + 3) / 4
                dcRe = 0.8f * dcRe + 0.2f * sr / cnt; dcIm = 0.8f * dcIm + 0.2f * si / cnt
                for (k in 0 until n) { re[k] -= dcRe; im[k] -= dcIm }

                val now = System.nanoTime()
                if (now - lastSpec > 33_000_000L && analyzer.compute(re, im, n, spec)) {
                    lastSpec = now
                    synchronized(specLock) {
                        System.arraycopy(spec, 0, specOut, 0, spectrumSize)
                        specCenter = centerHz; specRate = sampleRate; specSerial++
                    }
                    listener?.onSpectrum()
                }
                val frames = demod.process(re, im, n, aud)
                audio.write(aud, frames)
                dropped = source.droppedSamples
            }
        } catch (e: SdrException) {
            listener?.onError(e.message ?: "Ошибка приёмника")
        } catch (e: Exception) {
            listener?.onError("Сбой: ${e.javaClass.simpleName}: ${e.message}")
        } finally {
            running = false
            try { source.close() } catch (_: Exception) {}
            audio.close()
        }
    }

    companion object {
        @Volatile private var previous: Thread? = null

        /** Станция ставится не в центр полосы, а на 1/8 левее: там нет пика постоянной составляющей. */
        fun offsetFor(rate: Int): Long = (rate / 8).toLong()
    }
}
