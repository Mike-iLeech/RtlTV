package io.github.mikeileech.sdr4noob.radio

import io.github.mikeileech.sdr4noob.dsp.Modulation
import io.github.mikeileech.sdr4noob.dsp.RadioDemod
import io.github.mikeileech.sdr4noob.dsp.SpectrumAnalyzer
import io.github.mikeileech.sdr4noob.sdr.SdrException
import io.github.mikeileech.sdr4noob.sdr.SdrSource
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.Semaphore
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
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
        /** Запись остановилась сама (нет места, предел 4 ГБ, перестройка центра для IQ). */
        fun onRecordStopped(message: String) {}
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

    /** Движок остановлен, потому что запустили другой (второе окно радио): прежнему окну пора закрыться. */
    @Volatile var preempted = false
        private set

    /**
     * Приёмник один, движок — тоже: запуск нового останавливает прежний (например, окно радио с тестовым
     * сигналом осталось под новым, открытым после подключения приёмника). Владение приёмником — семафор
     * [OWNER]: поток держит его от открытия до закрытия источника, поэтому двух владельцев не бывает,
     * даже если прежний поток закрывается дольше [CLOSE_WAIT_MS].
     */
    fun start() {
        if (running) return
        running = true
        val old = current
        if (old != null && old !== this) { old.preempted = true; old.stop() }
        current = this
        val t = Thread({
            if (!OWNER.tryAcquire(CLOSE_WAIT_MS, TimeUnit.MILLISECONDS)) {
                if (running) listener?.onError("Приёмник ещё занят прежним окном радио — попробуйте через несколько секунд.")
                running = false
            } else {
                try {
                    if (running) loop()   // вытеснили, пока ждали, — приёмник не открываем
                } finally {
                    OWNER.release()
                }
            }
        }, "sdr4noob-radio").apply { priority = Thread.MAX_PRIORITY }
        thread = t
        t.start()
    }

    /** Остановить без ожидания (UI-поток не блокируется): поток сам закроет приёмник и звук. */
    fun stop() {
        listener = null
        running = false
        thread = null
        if (current === this) current = null
    }

    /** Настроиться на станцию. В пределах полосы приёмника меняется только VFO, иначе — и центр. */
    fun tune(hz: Long) {
        vfoHz = hz   // сразу — чтобы шкала не прыгала назад до выполнения команды
        post { vfoHz = hz; tuneNow(hz); demod.resetStation() }
    }

    /** Сдвинуть центр приёмника (перетаскивание спектра), VFO остаётся на станции, если она в полосе. */
    fun setCenter(hz: Long) = post {
        if (hz != centerHz) stopIqOnRetune()
        centerHz = hz
        source.setFrequency(hz)
        val half = sampleRate / 2 - mode.defaultBw / 2
        if (abs(vfoHz - hz) > half) vfoHz = hz - offsetFor(sampleRate)
        demod.offsetHz = (vfoHz - centerHz).toDouble()
    }

    /** Режим и полоса меняются сразу (для интерфейса), демодулятор — командой в потоке приёма. */
    fun setMode(m: Modulation, bw: Int = m.defaultBw) {
        mode = m
        bandwidth = bw.coerceIn(m.minBw, m.maxBw)
        val b = bandwidth
        post {
            demod.setMode(m, b)
            tuneNow(vfoHz)
        }
    }

    /** Полоса канала, Гц (в пределах режима). */
    @Volatile var bandwidth = mode.defaultBw; private set
    fun setBandwidth(bw: Int) {
        bandwidth = bw.coerceIn(mode.minBw, mode.maxBw)
        post { demod.setBandwidth(bandwidth) }
    }

    @Volatile var agcOn = true; private set
    fun setAgc(on: Boolean) { agcOn = on; post { demod.agc.on = on } }

    /** RDS текущей станции (поля читаются из потока интерфейса — только для показа). */
    val rds get() = demod.rds

    // запись: писатели подменяются атомарно, поток приёма берёт текущий на каждый блок
    private val audioRec = AtomicReference<WavWriter?>()
    private val iqRec = AtomicReference<WavWriter?>()
    val audioRecording get() = audioRec.get()
    val iqRecording get() = iqRec.get()
    fun startAudioRec(w: WavWriter) { audioRec.getAndSet(w)?.close() }
    fun startIqRec(w: WavWriter) { iqRec.getAndSet(w)?.close() }
    fun stopAudioRec() { audioRec.getAndSet(null)?.close() }
    fun stopIqRec() { iqRec.getAndSet(null)?.close() }

    /** В потоке приёма: остановить запись, которая прервалась сама, и сообщить причину. */
    private fun checkRec(ref: AtomicReference<WavWriter?>, what: String) {
        val w = ref.get() ?: return
        val why = w.failed ?: return
        if (ref.compareAndSet(w, null)) { w.close(); listener?.onRecordStopped("Запись $what остановлена: $why") }
    }

    private fun stopIqOnRetune() {
        val w = iqRec.getAndSet(null) ?: return
        w.close()
        listener?.onRecordStopped("Запись IQ остановлена: центр приёмника перестроен (в одном файле — одна частота)")
    }

    private fun tuneNow(hz: Long) {
        val off = hz - centerHz
        if (abs(off) > sampleRate / 2 - mode.defaultBw / 2 - 20_000 || abs(off) < 15_000) {
            stopIqOnRetune()
            centerHz = hz + offsetFor(sampleRate)
            source.setFrequency(centerHz)
        }
        demod.offsetHz = (vfoHz - centerHz).toDouble()
    }

    fun setSampleRate(rate: Int) = post {
        sampleRate = rate
        source.setSampleRate(rate)
        demod = RadioDemod(rate, mode).also {
            it.stereo = stereoWanted; it.squelchDb = squelch; it.setBandwidth(bandwidth); it.agc.on = agcOn
        }
        stopIqRec()   // в записи IQ одна частота дискретизации
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
            var aud = FloatArray(2 * (block / 5 + 64))
            var dcRe = 0f; var dcIm = 0f
            var lastSpec = 0L
            while (running) {
                while (true) { (commands.poll() ?: break).invoke() }
                if (sampleRate / 20 != block) {
                    block = sampleRate / 20
                    re = FloatArray(block); im = FloatArray(block); aud = FloatArray(2 * (block / 5 + 64))
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
                iqRec.get()?.writeIq(re, im, n)      // до демодулятора: он переносит частоту на месте
                val frames = demod.process(re, im, n, aud)
                audio.write(aud, frames)
                audioRec.get()?.write(aud, frames * 2)
                checkRec(audioRec, "звука"); checkRec(iqRec, "IQ")
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
            stopAudioRec(); stopIqRec()
        }
    }

    companion object {
        private val OWNER = Semaphore(1)
        @Volatile private var current: RadioEngine? = null
        private const val CLOSE_WAIT_MS = 10_000L

        /** Станция ставится не в центр полосы, а на 1/8 левее: там нет пика постоянной составляющей. */
        fun offsetFor(rate: Int): Long = (rate / 8).toLong()
    }
}
