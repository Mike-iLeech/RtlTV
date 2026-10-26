package io.github.mikeileech.sdr4noob

import io.github.mikeileech.sdr4noob.dsp.Biquad
import io.github.mikeileech.sdr4noob.dsp.ComplexDecimator
import io.github.mikeileech.sdr4noob.dsp.Fft
import io.github.mikeileech.sdr4noob.dsp.Firwin
import io.github.mikeileech.sdr4noob.dsp.Modulation
import io.github.mikeileech.sdr4noob.dsp.RadioDemod
import io.github.mikeileech.sdr4noob.dsp.RealDecimator
import io.github.mikeileech.sdr4noob.dsp.SpectrumAnalyzer
import io.github.mikeileech.sdr4noob.sdr.DemoSource
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.log10
import kotlin.math.sin
import kotlin.math.sqrt

class DspTest {
    /** Мощность тона [f] в сигнале (Гёрцель), нормирована к амплитуде: синус амплитуды A → A²/4·…; сравниваем отношения. */
    private fun tone(x: FloatArray, n0: Int, n: Int, f: Double, fs: Double): Double {
        var re = 0.0; var im = 0.0
        for (i in 0 until n) {
            val w = 2 * PI * f * i / fs
            re += x[n0 + i] * cos(w); im -= x[n0 + i] * sin(w)
        }
        return (re * re + im * im) / n / n
    }

    private fun db(a: Double, b: Double) = 10 * log10(a / b)

    @Test fun fftFindsTone() {
        val n = 1024
        val fft = Fft(n)
        val re = FloatArray(n) { cos(2 * PI * 100 * it / n).toFloat() }
        val im = FloatArray(n) { sin(2 * PI * 100 * it / n).toFloat() }
        fft.transform(re, im)
        val mag = FloatArray(n) { sqrt(re[it] * re[it] + im[it] * im[it]) }
        assertEquals(100, mag.indices.maxByOrNull { mag[it] })
        assertEquals(n.toFloat(), mag[100], 1e-2f * n)
    }

    @Test fun spectrumFullScaleToneIsZeroDb() {
        val n = 8192
        val fs = 2_400_000.0
        val f = 300_000.0
        val re = FloatArray(n) { cos(2 * PI * f * it / fs).toFloat() }
        val im = FloatArray(n) { sin(2 * PI * f * it / fs).toFloat() }
        val sa = SpectrumAnalyzer(2048, 4)
        val out = FloatArray(2048)
        assertTrue(sa.compute(re, im, n, out))
        val peak = out.indices.maxByOrNull { out[it] }!!
        assertEquals(1024 + 256, peak)   // +300 кГц из ±1,2 МГц
        assertEquals(0f, out[peak], 1.5f)
    }

    @Test fun firwinUnityGainAtDc() {
        val h = Firwin.lowpass(81, 15_000.0, 240_000.0)
        assertEquals(1.0, h.sumOf { it.toDouble() }, 1e-5)
        assertEquals(h.first(), h.last(), 1e-7f)
    }

    @Test fun complexDecimatorDoesNotDependOnBlocking() {
        val fs = 2_400_000.0
        val n = 50_000
        val re = FloatArray(n) { (sin(it * 0.013) + 0.3 * cos(it * 0.31)).toFloat() }
        val im = FloatArray(n) { (cos(it * 0.017) - 0.2 * sin(it * 0.29)).toFloat() }
        fun run(chunks: IntArray): Pair<FloatArray, FloatArray> {
            val d = ComplexDecimator(10, 108_000.0, fs, 16)
            val oR = ArrayList<Float>(); val oI = ArrayList<Float>()
            var pos = 0; var k = 0
            val bR = FloatArray(n / 10 + 2); val bI = FloatArray(n / 10 + 2)
            while (pos < n) {
                val len = minOf(chunks[k++ % chunks.size], n - pos)
                val m = d.process(re.copyOfRange(pos, pos + len), im.copyOfRange(pos, pos + len), len, bR, bI)
                for (i in 0 until m) { oR.add(bR[i]); oI.add(bI[i]) }
                pos += len
            }
            return oR.toFloatArray() to oI.toFloatArray()
        }
        val (a, b) = run(intArrayOf(n))
        val (c, d) = run(intArrayOf(7, 1013, 3, 9999, 64))
        assertEquals(n / 10, a.size)
        assertArrayEquals(a, c, 1e-5f)
        assertArrayEquals(b, d, 1e-5f)
    }

    @Test fun realDecimatorPassesAndStops() {
        val fs = 240_000.0
        val n = 48_000
        fun gain(f: Double): Double {
            val d = RealDecimator(5, 14_500.0, fs, 48)
            val x = FloatArray(n) { sin(2 * PI * f * it / fs).toFloat() }
            val out = FloatArray(n / 5 + 2)
            val m = d.process(x, n, out)
            return tone(out, 2000, m - 2000, f, fs / 5)
        }
        val pass = gain(1000.0)
        assertTrue("1 кГц проходит", abs(db(pass, 0.25)) < 0.5)
        assertTrue("пилот 19 кГц подавлен на 40 dB", db(gain(19_000.0), pass) < -40)
    }

    @Test fun biquadLowpassAttenuates() {
        val fs = 48_000.0
        val bq = Biquad.lowpass(1000.0, fs)
        val x = FloatArray(48_000) { sin(2 * PI * 8000 * it / fs).toFloat() }
        bq.process(x, x.size)
        assertTrue(db(tone(x, 4800, 40_000, 8000.0, fs), 0.25) < -30)
    }

    // ---------- демодуляторы ----------

    /** Стерео-ЧМ на смещении [offset]: L — тон [fl] (амплитуда al), R — тон [fr] (ar); [pilot] — есть ли пилот 19 кГц. */
    private fun fmStereo(fs: Int, n: Int, offset: Double, fl: Double, al: Double, fr: Double, ar: Double, pilot: Boolean):
        Pair<FloatArray, FloatArray> {
        val re = FloatArray(n); val im = FloatArray(n)
        var ph = 0.0
        for (i in 0 until n) {
            val t = i.toDouble() / fs
            val l = al * sin(2 * PI * fl * t); val r = ar * sin(2 * PI * fr * t)
            val p = 2 * PI * 19_000 * t
            val mpx = 0.45 * (l + r) + (if (pilot) 0.1 * sin(p) + 0.45 * (l - r) * sin(2 * p) else 0.0)
            ph += 2 * PI * (offset + 75_000 * mpx) / fs
            re[i] = (0.5 * cos(ph)).toFloat(); im[i] = (0.5 * sin(ph)).toFloat()
        }
        return re to im
    }

    private fun demodAll(d: RadioDemod, re: FloatArray, im: FloatArray, block: Int): FloatArray {
        val out = ArrayList<Float>()
        val aud = FloatArray(2 * (block / 5 + 16))
        var pos = 0
        while (pos + block <= re.size) {
            val r = re.copyOfRange(pos, pos + block); val q = im.copyOfRange(pos, pos + block)
            val k = d.process(r, q, block, aud)
            for (i in 0 until 2 * k) out.add(aud[i])
            pos += block
        }
        return out.toFloatArray()
    }

    private fun channel(stereo: FloatArray, ch: Int) = FloatArray(stereo.size / 2) { stereo[2 * it + ch] }

    @Test fun wfmStereoSeparatesChannels() {
        val fs = 2_400_000
        val (re, im) = fmStereo(fs, fs, -300_000.0, 1000.0, 1.0, 1000.0, 0.0, pilot = true)  // 1 с, звук только слева
        val d = RadioDemod(fs, Modulation.WFM).apply { offsetHz = -300_000.0 }
        val out = demodAll(d, re, im, fs / 20)
        assertTrue("стерео захвачено", d.stereoLock)
        val l = channel(out, 0); val r = channel(out, 1)
        val n = l.size
        val pl = tone(l, n / 2, n / 2 - 1, 1000.0, 48_000.0)
        val pr = tone(r, n / 2, n / 2 - 1, 1000.0, 48_000.0)
        val sep = db(pl, pr)
        assertTrue("разделение каналов $sep dB", sep > 25)
        assertTrue("слева громко", pl > 1e-3)
    }

    @Test fun wfmWithoutPilotIsMono() {
        val fs = 1_200_000
        val (re, im) = fmStereo(fs, fs / 2, 200_000.0, 1000.0, 1.0, 1000.0, 0.0, pilot = false)
        val d = RadioDemod(fs, Modulation.WFM).apply { offsetHz = 200_000.0 }
        val out = demodAll(d, re, im, fs / 20)
        assertFalse(d.stereoLock)
        val l = channel(out, 0); val r = channel(out, 1)
        val n = l.size
        assertEquals(1.0, tone(l, n / 2, n / 2 - 1, 1000.0, 48_000.0) / tone(r, n / 2, n / 2 - 1, 1000.0, 48_000.0), 1e-3)
    }

    @Test fun nfmDemodulatesTone() {
        val fs = 960_000
        val n = fs / 2
        val re = FloatArray(n); val im = FloatArray(n)
        var ph = 0.0
        for (i in 0 until n) {
            val t = i.toDouble() / fs
            ph += 2 * PI * (50_000 + 2500 * sin(2 * PI * 1000 * t)) / fs
            re[i] = (0.3 * cos(ph)).toFloat(); im[i] = (0.3 * sin(ph)).toFloat()
        }
        val d = RadioDemod(fs, Modulation.NFM).apply { offsetHz = 50_000.0 }
        val out = channel(demodAll(d, re, im, fs / 20), 0)
        val m = out.size
        val p1 = tone(out, m / 2, m / 2 - 1, 1000.0, 48_000.0)
        val p3 = tone(out, m / 2, m / 2 - 1, 3000.0, 48_000.0)
        assertTrue("тон 1 кГц", p1 > 1e-3)
        assertTrue("искажения малы", db(p3, p1) < -25)
    }

    @Test fun amDemodulatesTone() {
        val fs = 960_000
        val n = fs
        val re = FloatArray(n); val im = FloatArray(n)
        for (i in 0 until n) {
            val t = i.toDouble() / fs
            val a = 0.2 * (1 + 0.5 * sin(2 * PI * 700 * t))
            val ph = 2 * PI * -100_000 * t
            re[i] = (a * cos(ph)).toFloat(); im[i] = (a * sin(ph)).toFloat()
        }
        val d = RadioDemod(fs, Modulation.AM).apply { offsetHz = -100_000.0 }
        val out = channel(demodAll(d, re, im, fs / 20), 0)
        val m = out.size
        assertTrue(tone(out, m / 2, m / 2 - 1, 700.0, 48_000.0) > 1e-3)
    }

    @Test fun squelchMutesWeakChannel() {
        val fs = 960_000
        val n = fs / 2
        val re = FloatArray(n) { ((it * 7919 % 1000) / 1000f - 0.5f) * 1e-4f }
        val im = FloatArray(n) { ((it * 104729 % 1000) / 1000f - 0.5f) * 1e-4f }
        val d = RadioDemod(fs, Modulation.NFM).apply { squelchDb = -40f }
        val out = demodAll(d, re, im, fs / 20)
        assertFalse(d.squelchOpen)
        val tail = out.copyOfRange(out.size / 2, out.size)
        assertTrue(tail.all { abs(it) < 1e-6f })
    }

    /** Тестовый источник после долгой остановки потока (часы в фоне) не засыпает и не пытается догонять. */
    @Test fun demoSourceSurvivesLongStall() {
        val src = DemoSource()
        src.open(2_400_000, 101_000_000L)
        src.start()
        val f = DemoSource::class.java.getDeclaredField("t0").apply { isAccessible = true }
        f.setLong(src, System.nanoTime() - 3 * 3600 * 1_000_000_000L)   // «прошло» 3 часа
        val re = FloatArray(120_000); val im = FloatArray(120_000)
        val t0 = System.nanoTime()
        assertEquals(120_000, src.read(re, im, 120_000))
        var got = 0
        var reads = 0
        while (System.nanoTime() - t0 < 300_000_000L) {
            val k = src.read(re, im, 120_000)
            assertTrue("только полные блоки: $k", k == 0 || k == 120_000)   // не крутить обработку на мелочи
            got += k; reads++
        }
        assertTrue("источник спит, а не опрашивается вхолостую: $reads", reads < 60)
        assertTrue("темп реального времени после простоя: $got", got in 400_000..1_200_000)
    }

    @Test fun demoSourceStationIsReceivedInStereo() {
        val fs = 1_200_000
        val src = DemoSource()
        src.open(fs, 101_000_000L + 150_000)
        src.start()
        val d = RadioDemod(fs, Modulation.WFM).apply { offsetHz = -150_000.0 }
        val block = fs / 20
        val re = FloatArray(block); val im = FloatArray(block)
        val aud = FloatArray(2 * (block / 5 + 16))
        var got = 0
        val t0 = System.nanoTime()
        while (got < fs * 6 / 10 && System.nanoTime() - t0 < 10_000_000_000L) {
            val k = src.read(re, im, block)
            if (k == 0) continue
            d.process(re, im, k, aud)
            got += k
        }
        assertTrue("тестовая станция 101,0 МГц — стерео", d.stereoLock)
        assertTrue("уровень канала заметный", d.levelDb > -30f)
    }
}
