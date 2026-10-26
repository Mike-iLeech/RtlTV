package io.github.mikeileech.sdr4noob.sdr

import android.content.Context
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbManager

/** JNI к встроенному драйверу RTL2832U (src/main/cpp/sdr4noob_jni.c). */
object RtlSdrNative {
    init { System.loadLibrary("sdr4noob") }

    /** Хэндл (0 — ошибка, код в err[0]). Хэндл — указатель: на arm64 он бывает «отрицательным». */
    external fun open(fd: Int, err: IntArray): Long
    external fun close(h: Long)
    external fun start(h: Long): Int
    external fun stop(h: Long)
    external fun read(h: Long, out: ByteArray, len: Int, timeoutMs: Int): Int
    external fun flush(h: Long)
    external fun dropped(h: Long): Long
    external fun lost(h: Long): Boolean
    external fun setSampleRate(h: Long, rate: Int): Int
    external fun setCenterFreq(h: Long, hz: Long): Int
    external fun setGain(h: Long, tenths: Int): Int
    external fun gains(h: Long): IntArray
    external fun setPpm(h: Long, ppm: Int): Int
    external fun setBiasTee(h: Long, on: Boolean): Int
    external fun setAgc(h: Long, on: Boolean): Int
    external fun setDirectSampling(h: Long, mode: Int): Int
    external fun tunerType(h: Long): Int
    external fun usbStrings(h: Long): String
}

/**
 * Приёмник на RTL2832U через USB Host API: устройство открывает Android (после разрешения
 * пользователя), а встроенный librtlsdr получает его файловый дескриптор — root и отдельное
 * приложение-драйвер не нужны.
 */
class RtlUsbSource(private val context: Context, private val device: UsbDevice) : SdrSource {
    private var conn: UsbDeviceConnection? = null
    private var h = 0L
    private var buf = ByteArray(0)
    override var label: String = "RTL-SDR"
        private set
    override var gains: IntArray = IntArray(0)
        private set
    override val hasBiasTee = true
    override val droppedSamples: Long get() = if (h != 0L) RtlSdrNative.dropped(h) / 2 else 0

    override fun open(sampleRate: Int, frequencyHz: Long) {
        val um = context.getSystemService(Context.USB_SERVICE) as UsbManager
        if (!um.hasPermission(device)) throw SdrException("Нет разрешения на доступ к приёмнику по USB")
        val c = um.openDevice(device) ?: throw SdrException("Android не открыл USB-устройство (отключено?)")
        val err = IntArray(1)
        val handle = RtlSdrNative.open(c.fileDescriptor, err)
        if (handle == 0L) {
            c.close()
            throw SdrException("Драйвер RTL2832U не открыл приёмник (код ${err[0]}). Переподключите его к USB.")
        }
        conn = c
        h = handle
        val tuner = TUNERS[RtlSdrNative.tunerType(h)] ?: "?"
        val (manuf, product) = RtlSdrNative.usbStrings(h).split('|').let { it.getOrElse(0) { "" } to it.getOrElse(1) { "" } }
        val name = listOf(manuf, product).filter { it.isNotBlank() }.joinToString(" ").ifBlank { "RTL2832U" }
        label = "$name ($tuner)"
        gains = RtlSdrNative.gains(h)
        check(RtlSdrNative.setSampleRate(h, sampleRate), "частоту дискретизации $sampleRate")
        check(RtlSdrNative.setCenterFreq(h, frequencyHz), "частоту ${frequencyHz} Гц")
    }

    private fun check(r: Int, what: String) {
        if (r < 0) throw SdrException("Приёмник не принял $what (код $r)")
    }

    override fun start() {
        if (RtlSdrNative.start(h) != 0) throw SdrException("Не удалось запустить поток чтения USB")
    }

    override fun read(re: FloatArray, im: FloatArray, maxSamples: Int): Int {
        val bytes = maxSamples * 2
        if (buf.size < bytes) buf = ByteArray(bytes)
        val n = RtlSdrNative.read(h, buf, bytes, 500)
        if (n < 0) throw SdrException(if (RtlSdrNative.lost(h)) "Приёмник отключён от USB" else "Поток чтения остановлен")
        return U8.convert(buf, n, re, im)
    }

    override fun setFrequency(hz: Long) {
        RtlSdrNative.setCenterFreq(h, hz)
        RtlSdrNative.flush(h)
    }

    override fun setSampleRate(rate: Int) {
        RtlSdrNative.setSampleRate(h, rate)
        RtlSdrNative.flush(h)
    }

    override fun setGain(tenthsDb: Int) { RtlSdrNative.setGain(h, tenthsDb) }
    override fun setPpm(ppm: Int) { RtlSdrNative.setPpm(h, ppm) }
    override fun setBiasTee(on: Boolean) { RtlSdrNative.setBiasTee(h, on) }

    override fun close() {
        if (h != 0L) {
            RtlSdrNative.close(h)
            h = 0L
        }
        conn?.close()
        conn = null
    }
}
