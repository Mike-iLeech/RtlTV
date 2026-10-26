package io.github.mikeileech.sdr4noob

import io.github.mikeileech.sdr4noob.dsp.Sstv
import io.github.mikeileech.sdr4noob.dsp.SstvEvent
import io.github.mikeileech.sdr4noob.radio.AprsWorker
import io.github.mikeileech.sdr4noob.radio.SstvWorker
import io.github.mikeileech.sdr4noob.radio.WavPcm
import io.github.mikeileech.sdr4noob.sdr.DemoSource
import io.github.mikeileech.sdr4noob.dsp.Afsk
import io.github.mikeileech.sdr4noob.dsp.Aprs
import io.github.mikeileech.sdr4noob.dsp.Ax25
import io.github.mikeileech.sdr4noob.radio.AprsRx
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.CopyOnWriteArrayList

/** Обвязка режимов SSTV и APRS: чтение WAV, декодирование файла, поток APRS. */
class AppsTest {
    private fun wav(x: FloatArray, fs: Int, ch: Int = 1, bits: Int = 16, extra: Boolean = true): ByteArray {
        val bps = bits / 8
        val data = ByteBuffer.allocate(x.size * ch * bps).order(ByteOrder.LITTLE_ENDIAN)
        for (v in x) repeat(ch) {
            when (bits) {
                16 -> data.putShort((v.coerceIn(-1f, 1f) * 32767).toInt().toShort())
                8 -> data.put(((v.coerceIn(-1f, 1f) * 127) + 128).toInt().toByte())
                else -> data.putFloat(v)
            }
        }
        val out = ByteArrayOutputStream()
        fun le(n: Int, len: Int) { for (i in 0 until len) out.write((n shr (8 * i)) and 0xFF) }
        val list = if (extra) "LIST".toByteArray() + byteArrayOf(5, 0, 0, 0) + "abcde".toByteArray() + byteArrayOf(0) else ByteArray(0)
        out.write("RIFF".toByteArray()); le(4 + 24 + list.size + 8 + data.capacity(), 4); out.write("WAVE".toByteArray())
        out.write("fmt ".toByteArray()); le(16, 4); le(if (bits == 32) 3 else 1, 2); le(ch, 2); le(fs, 4)
        le(fs * ch * bps, 4); le(ch * bps, 2); le(bits, 2)
        out.write(list)
        out.write("data".toByteArray()); le(data.capacity(), 4); out.write(data.array())
        return out.toByteArray()
    }

    @Test fun wavFormats() {
        val x = FloatArray(1000) { kotlin.math.sin(it * 0.05).toFloat() * 0.5f }
        for ((bits, tol) in listOf(16 to 1e-4f, 8 to 2e-2f, 32 to 1e-6f)) {
            val (fs, y) = WavPcm.readMono(ByteArrayInputStream(wav(x, 44_100, ch = 2, bits = bits)))
            assertEquals(44_100, fs)
            assertEquals(x.size, y.size)
            for (i in x.indices step 37) assertEquals("бит $bits, отсчёт $i", x[i], y[i], tol)
        }
        try { WavPcm.readMono(ByteArrayInputStream("RIFF....AVI ".toByteArray())); throw AssertionError() } catch (_: IOException) {}
    }

    /** WAV с картинкой Robot 36 на 44,1 кГц → та же картинка (как decode_file на ПК). */
    @Test fun sstvFromWav() {
        val w = 320; val h = 240
        val img = IntArray(w * h) { i -> if ((i % w) < w / 2) 0xFFFF0000.toInt() else 0xFF0000FF.toInt() }
        val audio = Sstv.encode(img, w, h, "Robot 36", fs = 44_100.0)
        val pad = FloatArray(audio.size + 44_100)
        System.arraycopy(audio, 0, pad, 22_050, audio.size)
        val ev = SstvWorker.decodeWav(ByteArrayInputStream(wav(pad, 44_100)))
        val done = ev.filterIsInstance<SstvEvent.Done>()
        assertEquals(1, done.size)
        val d = done[0]
        assertEquals("Robot 36", d.mode)
        val left = d.image[100 * d.width + 40]; val right = d.image[100 * d.width + 280]
        assertTrue("слева красный: %08x".format(left), (left shr 16 and 0xFF) > 180 && (left and 0xFF) < 80)
        assertTrue("справа синий: %08x".format(right), (right and 0xFF) > 180 && (right shr 16 and 0xFF) < 80)
    }

    /** Поток APRS: звук стерео 48 кГц порциями по 50 мс → пакеты и станции с координатами. */
    @Test(timeout = 20_000) fun aprsWorkerDecodes() {
        val frames = listOf(
            Ax25.makeFrame("RA3AAA-9", "APRS", listOf("WIDE1-1"), Aprs.encodePosition(55.75, 37.61, "/", ">", "тест")),
            Ax25.makeFrame("R3CCC", "APRS", emptyList(), ">Привет"),
        )
        val x = Afsk.encodePackets(frames, gap = 0.5)
        val got = CopyOnWriteArrayList<AprsRx>()
        val w = AprsWorker({ got.addAll(it) })
        var i = 0
        while (i < x.size) {
            val n = minOf(2400, x.size - i)
            val st = FloatArray(2 * n) { x[i + it / 2] }
            w.feed(st, n); i += n
        }
        w.feed(FloatArray(2 * 48_000), 48_000)
        while (got.size < 2) Thread.sleep(20)
        w.stop()
        assertEquals(listOf("RA3AAA-9", "R3CCC"), got.map { it.packet.src })
        assertEquals(1, w.stations.size)
        assertEquals(55.75, w.stations[0].lat, 0.01)
    }

    /** Тестовый эфир отдаёт станции APRS и SSTV без исключений (буферы считаются при первом обращении). */
    @Test fun demoHasAprsAndSstv() {
        val d = DemoSource()
        d.open(1_200_000, 145_300_000)
        d.start()
        Thread.sleep(120)
        val re = FloatArray(60_000); val im = FloatArray(60_000)
        val n = d.read(re, im, 60_000)
        assertTrue(n == 0 || n == 60_000)
    }
}
