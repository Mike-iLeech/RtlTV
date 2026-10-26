package io.github.mikeileech.sdr4noob

import io.github.mikeileech.sdr4noob.radio.WavWriter
import io.github.mikeileech.sdr4noob.sdr.IqFileReader
import io.github.mikeileech.sdr4noob.sdr.IqFileSource
import io.github.mikeileech.sdr4noob.sdr.IqFormatException
import io.github.mikeileech.sdr4noob.sdr.IqName
import io.github.mikeileech.sdr4noob.sdr.LinearResampler
import io.github.mikeileech.sdr4noob.sdr.SdrException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.io.RandomAccessFile
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin

/** Воспроизведение записей IQ — перенос rtltv/iqfile.py (эталоны parse_name/target_rate — из Python). */
class IqFileTest {
    @Test fun namesLikeDesktop() {
        assertEquals(2400000 to 101200000.0, IqName.parse("SDR4NOOB_20261024_101500Z_101200000Hz_2400000sps_IQ.wav"))
        assertEquals(2400000 to 101200000.0, IqName.parse("gqrx_20240101_101.2MHz_2.4MSPS.raw"))
        assertEquals(1024000 to 100000000.0, IqName.parse("rec_1024k_100MHz.cu8"))
        assertEquals(2048000 to null, IqName.parse("x_2048000sps.cs16"))
        assertEquals(null to null, IqName.parse("abc.cf32"))
        assertEquals(2400000, IqName.targetRate(2048000))
        assertEquals(960000, IqName.targetRate(250000))
    }

    /** Пересчёт 2,048 → 2,4 МГц блоками разной длины совпадает с интерполяцией «за один раз». */
    @Test fun resamplerContinuousAcrossBlocks() {
        val step = 2048000.0 / 2400000
        val input = DoubleArray(20_000) { sin(2 * PI * 0.013 * it) }
        var p = 0
        val pull = { r: FloatArray, i: FloatArray, n: Int -> for (k in 0 until n) { r[k] = input[p].toFloat(); i[k] = -input[p].toFloat(); p++ } }
        val rs = LinearResampler(step)
        val out = ArrayList<Float>()
        for (n in intArrayOf(1, 7, 1000, 333, 4096, 5)) {
            val re = FloatArray(n); val im = FloatArray(n)
            rs.process(n, re, im, pull)
            out.addAll(re.toList())
        }
        for ((j, v) in out.withIndex()) {
            val q = j * step; val k = q.toInt(); val f = q - k
            val want = input[k] + (input[k + 1] - input[k]) * f
            assertEquals("отсчёт $j", want, v.toDouble(), 1e-5)
        }
        assertTrue("лишнего не прочитано: $p", p <= (out.size * step).toInt() + 2)
    }

    @Test fun readsOwnWavLooping() {
        val f = File.createTempFile("SDR4NOOB_x_145500000Hz_960000sps_IQ", ".wav")
        val w = WavWriter(RandomAccessFile(f, "rw").channel, 960_000, 2)
        val n = 1000
        w.writeIq(FloatArray(n) { cos(it * 0.1).toFloat() * 0.5f }, FloatArray(n) { sin(it * 0.1).toFloat() * 0.5f }, n)
        w.close(); w.await()
        val r = IqFileReader({ f.inputStream() }, f.name, f.length())
        assertEquals("s16", r.kind)
        assertEquals(960_000, r.fs)
        assertEquals(145.5e6, r.center!!, 0.0)
        assertEquals(n.toLong(), r.total)
        val re = FloatArray(2500); val im = FloatArray(2500)
        r.read(re, im, 2500)                        // по кругу: 1000 + 1000 + 500
        for (j in intArrayOf(0, 10, 999, 1000, 1010, 2499)) {
            val k = j % n
            assertEquals(cos(k * 0.1) * 0.5, re[j].toDouble(), 1e-4)
            assertEquals(sin(k * 0.1) * 0.5, im[j].toDouble(), 1e-4)
        }
        r.close(); f.delete()
    }

    @Test fun rawCu8AndErrors() {
        val f = File.createTempFile("rec_1024k_100MHz", ".cu8")
        f.writeBytes(byteArrayOf(0, 255.toByte(), 127, 128.toByte()))
        val r = IqFileReader({ f.inputStream() }, f.name, f.length())
        assertEquals(1_024_000, r.fs)
        val re = FloatArray(2); val im = FloatArray(2)
        r.read(re, im, 2)
        assertEquals(-127.4f / 128, re[0], 1e-6f)
        assertEquals(0.6f / 128, im[1], 1e-6f)
        r.close()
        try { IqFileReader({ f.inputStream() }, "noname.cu8", f.length()); throw AssertionError() } catch (_: IqFormatException) {}
        try { IqFileReader({ f.inputStream() }, "x.mp3", f.length()); throw AssertionError() } catch (_: IqFormatException) {}
        try { IqFileReader({ f.inputStream() }, "x.wav", f.length()); throw AssertionError() } catch (_: IqFormatException) {}
        f.delete()
    }

    /** Файл короче заявленного размера: ошибка, а не вечный цикл в потоке радио. */
    @Test(timeout = 5000) fun truncatedFileFailsInsteadOfHanging() {
        val f = File.createTempFile("rec_1024k_100MHz", ".cu8")
        f.writeBytes(byteArrayOf(0, 1, 2, 3))
        val r = IqFileReader({ f.inputStream() }, f.name, 4000)       // «размер» больше настоящего
        try { r.read(FloatArray(3000), FloatArray(3000), 3000); throw AssertionError() } catch (_: SdrException) {}
        r.close(); f.delete()
    }

    /** Ошибки открытия файла (удалён, нет доступа) — понятное SdrException при первом обращении движка. */
    @Test fun missingFileIsSdrException() {
        val src = IqFileSource("x.cu8") { throw java.io.FileNotFoundException("нет") }
        try { src.fixedRate; throw AssertionError() } catch (e: SdrException) { assertTrue(e.message!!.contains("не найден")) }
        assertEquals(false, src.retunable)
    }
}
