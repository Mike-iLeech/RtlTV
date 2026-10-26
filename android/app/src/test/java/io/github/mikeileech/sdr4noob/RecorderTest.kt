package io.github.mikeileech.sdr4noob

import io.github.mikeileech.sdr4noob.radio.Storage
import io.github.mikeileech.sdr4noob.radio.WavWriter
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.io.RandomAccessFile
import java.util.Date
import java.nio.ByteBuffer
import java.nio.ByteOrder

class RecorderTest {
    /** Разбор заголовка WAV (44 байта PCM): частота, каналы, бит, число кадров; данные — с 44-го байта. */
    private class Wav(f: File) {
        val b: ByteBuffer = ByteBuffer.wrap(f.readBytes()).order(ByteOrder.LITTLE_ENDIAN)
        val riffOk = String(b.array(), 0, 4) == "RIFF" && String(b.array(), 8, 4) == "WAVE" && b.getInt(4) == b.capacity() - 8
        val channels = b.getShort(22).toInt()
        val rate = b.getInt(24)
        val bits = b.getShort(34).toInt()
        val frames = b.getInt(40) / (channels * bits / 8)
        fun sample(i: Int) = b.getShort(44 + 2 * i)
    }

    @Test fun wavHeaderAndSamples() {
        val f = File.createTempFile("rec", ".wav")
        var closed = false
        val w = WavWriter(RandomAccessFile(f, "rw").channel, 48_000, 2) { closed = true }
        val x = FloatArray(2 * 4800) { if (it % 2 == 0) 0.5f else -2f }   // −2 обрезается до −1
        repeat(10) { w.write(x, x.size) }
        w.close(); w.await()
        assertTrue(closed)
        val w2 = Wav(f)
        assertTrue(w2.riffOk)
        assertEquals(48_000, w2.rate)
        assertEquals(2, w2.channels)
        assertEquals(16, w2.bits)
        assertEquals(48_000, w2.frames)
        assertEquals(16384.toShort(), w2.sample(0))   // 0,5 → round(16383,5)
        assertEquals((-32767).toShort(), w2.sample(1))
        f.delete()
    }

    @Test fun iqWavIsStereo16() {
        val f = File.createTempFile("iqrec", ".wav")
        val w = WavWriter(RandomAccessFile(f, "rw").channel, 1_200_000, 2)
        w.writeIq(FloatArray(1000) { 0.1f }, FloatArray(1000) { -0.1f }, 1000)
        w.close(); w.await()
        val w2 = Wav(f)
        assertEquals(1_200_000, w2.rate)
        assertEquals(1_000, w2.frames)
        assertEquals(3277.toShort(), w2.sample(0)); assertEquals((-3277).toShort(), w2.sample(1))
        f.delete()
    }

    /** Ошибка диска не роняет приложение: поток записи останавливается и сообщает причину. */
    @Test fun diskErrorIsReportedNotThrown() {
        val f = File.createTempFile("broken", ".wav")
        val ch = RandomAccessFile(f, "rw").channel
        ch.close()                                   // любая запись теперь бросает ClosedChannelException
        var closed = false
        val w = WavWriter(ch, 48_000, 2) { closed = true }
        w.await(2_000)
        assertTrue("причина записана", w.failed != null)
        w.write(FloatArray(100), 100)                // после сбоя — молча игнорируется
        w.close(); w.await()
        assertTrue(closed)
        f.delete()
    }

    @Test fun closeDoesNotWait() {
        val f = File.createTempFile("async", ".wav")
        val w = WavWriter(RandomAccessFile(f, "rw").channel, 48_000, 2)
        w.write(FloatArray(96_000), 96_000)
        val t0 = System.nanoTime()
        w.close()
        assertTrue((System.nanoTime() - t0) < 50_000_000L)
        w.await()
        assertEquals(48_000, Wav(f).frames)
        f.delete()
    }

    /** Имена как на ПК: iqfile.file_name и radio_window (разбор rtltv.iqfile.parse_name находит fs и частоту). */
    @Test fun namesLikeDesktop() {
        val t = Date(1_793_000_000_000L)   // 2026-10-26 …
        val iq = Storage.iqName(101_200_000L, 2_400_000, t)
        assertTrue(iq, Regex("""SDR4NOOB_\d{8}_\d{6}Z_101200000Hz_2400000sps_IQ\.wav""").matches(iq))
        val au = Storage.audioName(101_200_000L, t)
        assertTrue(au, Regex("""radio_101\.2000MHz_\d{8}_\d{6}\.wav""").matches(au))
    }
}
