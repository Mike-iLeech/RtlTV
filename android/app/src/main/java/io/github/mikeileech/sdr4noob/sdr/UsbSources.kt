package io.github.mikeileech.sdr4noob.sdr

import io.github.mikeileech.sdr4noob.i18n.dec
import io.github.mikeileech.sdr4noob.i18n.tr

import android.content.Context
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbManager
import io.github.mikeileech.sdr4noob.dsp.StreamResampler
import kotlin.math.ceil


/** JNI к libhackrf (src/main/cpp/hackrf_airspy_jni.c). */
object HackRfNative {
    init { System.loadLibrary("sdr4noob") }
    external fun open(fd: Int, err: IntArray): Long
    external fun start(h: Long): Int
    external fun stop(h: Long)
    external fun close(h: Long)
    external fun readIq(h: Long, out: FloatArray, max: Int, timeoutMs: Int): Int
    external fun flush(h: Long)
    external fun dropped(h: Long): Long
    external fun setSampleRate(h: Long, rate: Double): Int
    external fun setFreq(h: Long, hz: Long): Int
    external fun setGains(h: Long, amp: Boolean, lna: Int, vga: Int): Int
    external fun setBias(h: Long, on: Boolean): Int
    external fun boardName(h: Long): String
}

/** JNI к libairspy. */
object AirspyNative {
    init { System.loadLibrary("sdr4noob") }
    external fun open(fd: Int, err: IntArray): Long
    external fun sampleRates(h: Long): IntArray
    external fun start(h: Long): Int
    external fun stop(h: Long)
    external fun close(h: Long)
    external fun readIq(h: Long, out: FloatArray, max: Int, timeoutMs: Int): Int
    external fun flush(h: Long)
    external fun dropped(h: Long): Long
    external fun setSampleRate(h: Long, rate: Int): Int
    external fun setFreq(h: Long, hz: Long): Int
    external fun setGain(h: Long, step: Int): Int
    external fun setBias(h: Long, on: Boolean): Int
}

/** Усиление: общие десятые dB → родные ступени (как hackrf_split / airspy_step в rtltv/devices.py). */
object GainMap {
    val HACKRF_GAINS = IntArray(59) { it * 2 * 10 }          // МШУ 0–40 (шаг 8) + ПЧ 0–62 (шаг 2) + усилитель 14 dB
    val HACKRF_DEFAULT = Triple(false, 24, 20)               // у HackRF нет АРУ: «АРУ» = среднее усиление
    val AIRSPY_GAINS = IntArray(22) { it * 20 }              // ступени linearity 0–21, условно по 2 dB

    /** (усилитель, МШУ dB, ПЧ dB) для суммарного усиления в десятых dB; < 0 — средние значения. */
    fun hackrfSplit(tenths: Int): Triple<Boolean, Int, Int> {
        if (tenths < 0) return HACKRF_DEFAULT
        var g = Math.rint(tenths / 10.0).toInt().coerceIn(0, 116)   // rint — как round() в Python (к чётному)
        val amp = g > 102
        if (amp) g -= 14
        var lna = minOf(40, (g / 2) / 8 * 8)
        var vga = minOf(62, (g - lna) / 2 * 2)
        if (g - lna - vga > 0) {
            lna = minOf(40, lna + 8 * ceil((g - lna - vga) / 8.0).toInt())
            vga = minOf(62, maxOf(0, Math.floorDiv(g - lna, 2) * 2))
        }
        return Triple(amp, lna, vga)
    }

    fun airspyStep(tenths: Int) = Math.rint(tenths / 20.0).toInt().coerceIn(0, 21)
}

/**
 * Общее для приёмников со своим потоком приёма (HackRF, Airspy): родная частота дискретизации устройства
 * пересчитывается в частоту радиорежима (как _CallbackSource + StreamResampler на ПК).
 */
abstract class NativeRateSource(private val context: Context, private val device: UsbDevice) : SdrSource {
    protected var conn: UsbDeviceConnection? = null
    protected var h = 0L
    protected var fs = 0
    protected var native = 0
    private var rs: StreamResampler? = null
    private var raw = FloatArray(0)
    private var rr = FloatArray(0); private var ri = FloatArray(0)

    protected abstract val name: String
    protected abstract fun nativeOpen(fd: Int, err: IntArray): Long
    protected abstract fun chooseNative(fs: Int): Int
    protected abstract fun applyRate(native: Int): Int
    protected abstract fun nativeRead(out: FloatArray, max: Int, timeoutMs: Int): Int
    /** Диапазон перестройки устройства и сама перестройка (частота уже с поправкой ppm). */
    protected abstract val tuneRange: LongRange
    protected abstract fun nativeTune(hz: Long)
    protected abstract fun nativeFlush()
    protected abstract fun nativeStart(): Int
    protected abstract fun nativeStop()

    private var streaming = false
    /** Когда последний раз пришли отсчёты: без данных дольше [NO_DATA_MS] — ошибка, как на ПК. */
    private var lastData = 0L

    final override fun start() {
        if (nativeStart() != 0) throw SdrException(tr("{name}: не удалось запустить приём", "name" to name))
        streaming = true; lastData = System.nanoTime()
    }

    private var ppm = 0
    private var lastHz = 0L

    /** Перестройка с поправкой ppm, как _tune на ПК: hz / (1 + ppm·10⁻⁶); затем сброс старых отсчётов. */
    final override fun setFrequency(hz: Long) {
        if (hz !in tuneRange) return
        lastHz = hz
        nativeTune(Math.round(hz / (1.0 + ppm * 1e-6)))
        nativeFlush()
        resetResampler()            // история фильтра — со старой частоты (как rs.reset() в _flush на ПК)
    }

    private fun resetResampler() { if (rs != null) rs = StreamResampler(native, fs) }

    final override fun setPpm(ppm: Int) {
        this.ppm = ppm
        if (lastHz > 0) setFrequency(lastHz)
    }

    override fun open(sampleRate: Int, frequencyHz: Long) {
        val um = context.getSystemService(Context.USB_SERVICE) as UsbManager
        if (!um.hasPermission(device)) throw SdrException(tr("Нет разрешения на доступ к приёмнику по USB"))
        val c = um.openDevice(device) ?: throw SdrException(tr("Android не открыл USB-устройство (отключено?)"))
        val err = IntArray(1)
        val handle = nativeOpen(c.fileDescriptor, err)
        if (handle == 0L) {
            c.close()
            throw SdrException(tr("Драйвер {name} не открыл приёмник (код {code}). Переподключите его к USB.", "name" to name, "code" to err[0]))
        }
        conn = c; h = handle
        afterOpen()
        setSampleRate(sampleRate)
        setFrequency(frequencyHz)
        setGain(-1)
    }

    protected open fun afterOpen() {}

    override fun setSampleRate(rate: Int) {
        fs = rate
        native = chooseNative(rate)
        // на ПК частота меняется при остановленном потоке (_rx_stop → _set_rate → _rx_start): libairspy
        // сбрасывает конечную точку USB, и на ходу поток может сорваться
        val was = streaming
        if (was) { nativeStop(); streaming = false }
        val r = applyRate(native)
        if (r != 0) throw SdrException(tr("{name}: частота дискретизации {rate} МГц не принята (код {code})", "name" to name, "rate" to dec("${native / 1e6}"), "code" to r))
        if (was) start()
        rs = if (native == fs) null else StreamResampler(native, fs)
        if (h != 0L) nativeFlush()          // отсчёты старой частоты не пускать в новый пересчёт (как _flush на ПК)
    }

    /** Прочитать ≤ [maxSamples] отсчётов частоты радиорежима. */
    override fun read(re: FloatArray, im: FloatArray, maxSamples: Int): Int {
        val r = rs
        val want = if (r == null) maxSamples else ((maxSamples - 2).toLong() * r.down / r.up).toInt().coerceAtLeast(1)
        if (raw.size < 2 * want) raw = FloatArray(2 * want)
        val n = nativeRead(raw, want, 500)
        if (n < 0) throw SdrException(tr("{name}: приёмник отключён или поток остановлен", "name" to name))
        val now = System.nanoTime()
        if (n == 0) {
            if (streaming && now - lastData > NO_DATA_MS * 1_000_000L)
                throw SdrException(tr("{name}: нет данных от приёмника дольше {sec} с — переподключите его", "name" to name, "sec" to NO_DATA_MS / 1000))
            return 0
        }
        lastData = now
        if (r == null) {
            for (i in 0 until n) { re[i] = raw[2 * i]; im[i] = raw[2 * i + 1] }
            return n
        }
        if (rr.size < n) { rr = FloatArray(n); ri = FloatArray(n) }
        for (i in 0 until n) { rr[i] = raw[2 * i]; ri[i] = raw[2 * i + 1] }
        return r.process(rr, ri, n, re, im)
    }

    override fun close() {
        streaming = false
        if (h != 0L) { nativeClose(); h = 0L }
        conn?.close(); conn = null
    }

    protected abstract fun nativeClose()

    companion object { const val NO_DATA_MS = 3000L }
}

/** HackRF One / Jawbreaker / rad1o: libhackrf по дескриптору USB. */
class HackRfUsbSource(context: Context, device: UsbDevice) : NativeRateSource(context, device) {
    override val name = "HackRF"
    override var label = "HackRF"; private set
    override val gains = GainMap.HACKRF_GAINS
    override val hasBiasTee = true
    override val droppedSamples: Long get() = if (h != 0L) HackRfNative.dropped(h) else 0

    override fun nativeOpen(fd: Int, err: IntArray) = HackRfNative.open(fd, err)
    override fun afterOpen() { label = HackRfNative.boardName(h) }
    /** HackRF работает от 2 МГц: меньшие частоты — целым кратным (как _choose_native на ПК). */
    override fun chooseNative(fs: Int) = if (fs >= 2_000_000) minOf(fs, 20_000_000) else fs * ceil(2_000_000.0 / fs).toInt()
    override fun applyRate(native: Int) = HackRfNative.setSampleRate(h, native.toDouble())
    override fun nativeRead(out: FloatArray, max: Int, timeoutMs: Int) = HackRfNative.readIq(h, out, max, timeoutMs)
    override fun nativeClose() = HackRfNative.close(h)
    override fun nativeStart() = HackRfNative.start(h)
    override fun nativeStop() = HackRfNative.stop(h)

    override val tuneRange = 1_000_000L..6_000_000_000L
    override fun nativeTune(hz: Long) { HackRfNative.setFreq(h, hz) }
    override fun nativeFlush() = HackRfNative.flush(h)

    override fun setGain(tenthsDb: Int) {
        val (amp, lna, vga) = GainMap.hackrfSplit(tenthsDb)
        HackRfNative.setGains(h, amp, lna, vga)
    }

    override fun setBiasTee(on: Boolean) { HackRfNative.setBias(h, on) }
}

/** Airspy R2 / Mini: libairspy (airspy_open_fd). */
class AirspyUsbSource(context: Context, device: UsbDevice) : NativeRateSource(context, device) {
    override val name = "Airspy"
    override val label = "Airspy R2 / Mini"
    override val gains = GainMap.AIRSPY_GAINS
    override val hasBiasTee = true
    override val droppedSamples: Long get() = if (h != 0L) AirspyNative.dropped(h) else 0
    private var rates = intArrayOf(2_500_000, 10_000_000)

    override fun nativeOpen(fd: Int, err: IntArray) = AirspyNative.open(fd, err)
    override fun afterOpen() { AirspyNative.sampleRates(h).takeIf { it.isNotEmpty() }?.let { rates = it.sortedArray() } }
    /** Наименьшая родная частота не ниже нужной (как _choose_native у Airspy на ПК). */
    override fun chooseNative(fs: Int) = rates.firstOrNull { it >= fs } ?: rates.last()
    override fun applyRate(native: Int) = AirspyNative.setSampleRate(h, native)
    override fun nativeRead(out: FloatArray, max: Int, timeoutMs: Int) = AirspyNative.readIq(h, out, max, timeoutMs)
    override fun nativeClose() = AirspyNative.close(h)
    override fun nativeStart() = AirspyNative.start(h)
    override fun nativeStop() = AirspyNative.stop(h)

    override val tuneRange = 24_000_000L..1_800_000_000L
    override fun nativeTune(hz: Long) { AirspyNative.setFreq(h, hz) }
    override fun nativeFlush() = AirspyNative.flush(h)

    override fun setGain(tenthsDb: Int) { AirspyNative.setGain(h, if (tenthsDb < 0) -1 else GainMap.airspyStep(tenthsDb)) }
    override fun setBiasTee(on: Boolean) { AirspyNative.setBias(h, on) }
}
