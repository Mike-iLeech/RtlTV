package io.github.mikeileech.sdr4noob.sdr

import java.io.DataInputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException

/**
 * Клиент сетевого rtl_tcp (как RtlTcpSource в rtltv/devices.py): заголовок «RTL0» + тип тюнера +
 * число ступеней усиления, затем поток u8 IQ; команды — 5 байт (код + uint32 big-endian).
 * Приёмник может стоять на другом компьютере или в отдельном приложении-драйвере на телефоне.
 */
class RtlTcpSource(private val host: String, private val port: Int) : SdrSource {
    companion object {
        private const val CMD_FREQ = 1
        private const val CMD_RATE = 2
        private const val CMD_GAIN_MODE = 3
        private const val CMD_GAIN = 4
        private const val CMD_PPM = 5
        private const val CMD_AGC = 8
        private const val CMD_BIAS = 14

        // ступени усиления по типу тюнера, десятые dB (librtlsdr)
        private val R82XX = intArrayOf(0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229, 254, 280,
            297, 328, 338, 364, 372, 386, 402, 421, 434, 439, 445, 480, 496)
        private val GAINS = mapOf(
            1 to intArrayOf(-10, 15, 40, 65, 90, 115, 140, 165, 190, 215, 240, 290, 340, 420),
            2 to intArrayOf(-99, -40, 71, 179, 192), 3 to intArrayOf(-99, -73, -65, -63, -60, -58, -54, 58,
                61, 63, 65, 67, 68, 70, 71, 179, 181, 182, 184, 186, 188, 191, 197),
            5 to R82XX, 6 to R82XX)

        /** «хост:порт», «[::1]:1234» или просто «хост» (порт 1234). */
        fun parse(text: String, defaultPort: Int = 1234): Pair<String, Int> {
            val t = text.trim()
            val (h, p) = when {
                t.startsWith("[") -> t.substring(1).substringBefore("]") to t.substringAfter("]").removePrefix(":")
                t.count { it == ':' } == 1 -> t.substringBefore(":") to t.substringAfter(":")
                else -> t to ""
            }
            val port = if (p.isEmpty()) defaultPort else p.toIntOrNull()
                ?: throw SdrException("Неверный порт в «$text»")
            if (h.isEmpty() || port !in 1..65535)
                throw SdrException("Нужен адрес вида хост:порт, например 192.168.1.10:1234")
            return h to port
        }
    }

    private var sock: Socket? = null
    private var inp: InputStream? = null
    private var out: OutputStream? = null
    private var buf = ByteArray(0)
    private var odd = false   // в буфере остался нечётный байт (I без Q)
    private var pendingByte: Byte = 0
    override var label = "rtl_tcp $host:$port"
        private set
    override var gains = IntArray(0)
        private set
    override val hasBiasTee = true

    override fun open(sampleRate: Int, frequencyHz: Long) {
        val s = Socket()
        try {
            s.connect(InetSocketAddress(host, port), 5000)
            s.tcpNoDelay = true
            s.receiveBufferSize = 1 shl 20
            s.soTimeout = 5000
            val din = DataInputStream(s.getInputStream())
            val head = ByteArray(12)
            try { din.readFully(head) } catch (e: IOException) {
                throw SdrException("rtl_tcp $host:$port: сервер закрыл соединение (занят другим клиентом?)", e)
            }
            if (String(head, 0, 4, Charsets.US_ASCII) != "RTL0")
                throw SdrException("$host:$port — это не сервер rtl_tcp")
            val tuner = ((head[4].toInt() and 0xFF) shl 24) or ((head[5].toInt() and 0xFF) shl 16) or
                ((head[6].toInt() and 0xFF) shl 8) or (head[7].toInt() and 0xFF)
            gains = GAINS[tuner] ?: R82XX
            label = "rtl_tcp $host:$port (${TUNERS[tuner] ?: "тюнер $tuner"})"
            sock = s; inp = din; out = s.getOutputStream()
        } catch (e: SdrException) {
            s.close(); throw e
        } catch (e: IOException) {
            s.close(); throw SdrException("rtl_tcp: не удалось подключиться к $host:$port — ${e.message}", e)
        }
        send(CMD_RATE, sampleRate.toLong())
        send(CMD_FREQ, frequencyHz)
    }

    private fun send(cmd: Int, value: Long) {
        val v = value.toInt()
        val b = byteArrayOf(cmd.toByte(), (v ushr 24).toByte(), (v ushr 16).toByte(), (v ushr 8).toByte(), v.toByte())
        try { out?.write(b); out?.flush() } catch (e: IOException) { throw SdrException("rtl_tcp: связь потеряна", e) }
    }

    override fun start() {}

    override fun read(re: FloatArray, im: FloatArray, maxSamples: Int): Int {
        val want = maxSamples * 2
        if (buf.size < want) buf = ByteArray(want)
        var have = 0
        if (odd) { buf[0] = pendingByte; have = 1; odd = false }
        // дочитываем до полного блока (как приёмник по USB): мелкие порции гоняли бы обработку впустую
        var total = have
        while (total < want) {
            val n = try {
                inp!!.read(buf, total, want - total)
            } catch (e: SocketTimeoutException) {
                break
            } catch (e: IOException) {
                throw SdrException("rtl_tcp $host:$port: связь потеряна", e)
            }
            if (n < 0) throw SdrException("rtl_tcp $host:$port: сервер закрыл соединение")
            total += n
        }
        if (total % 2 == 1) { pendingByte = buf[total - 1]; odd = true; total-- }
        return U8.convert(buf, total, re, im)
    }

    override fun setFrequency(hz: Long) = send(CMD_FREQ, hz)
    override fun setSampleRate(rate: Int) = send(CMD_RATE, rate.toLong())
    override fun setGain(tenthsDb: Int) {
        if (tenthsDb < 0) send(CMD_GAIN_MODE, 0) else { send(CMD_GAIN_MODE, 1); send(CMD_GAIN, tenthsDb.toLong()) }
    }
    override fun setPpm(ppm: Int) = send(CMD_PPM, ppm.toLong())
    override fun setBiasTee(on: Boolean) = send(CMD_BIAS, if (on) 1 else 0)

    override fun close() {
        try { sock?.close() } catch (_: IOException) {}
        sock = null
    }
}
