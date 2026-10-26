package io.github.mikeileech.sdr4noob

import io.github.mikeileech.sdr4noob.dsp.Agc
import io.github.mikeileech.sdr4noob.dsp.Modulation
import io.github.mikeileech.sdr4noob.dsp.RadioDemod
import io.github.mikeileech.sdr4noob.dsp.RdsCode
import io.github.mikeileech.sdr4noob.dsp.RdsDecoder
import io.github.mikeileech.sdr4noob.dsp.RdsEncoder
import io.github.mikeileech.sdr4noob.sdr.DemoSource
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.log10
import kotlin.math.sin

/** RDS, SSB/CW, АРУ — перенос rtltv/rds.py и rtltv/radio.py; эталоны — значения из Python-модулей. */
class RadioQ1Test {
    private fun tone(x: FloatArray, n0: Int, n: Int, f: Double, fs: Double): Double {
        var re = 0.0; var im = 0.0
        for (i in 0 until n) { val w = 2 * PI * f * i / fs; re += x[n0 + i] * cos(w); im -= x[n0 + i] * sin(w) }
        return (re * re + im * im) / n / n
    }

    /** Эталон: rtltv.rds.crc_table() и checkword(0x7A01, A/B/C/C'/D) в Python. */
    @Test fun rdsCrcMatchesPython() {
        val t = RdsCode.CRC
        assertArrayEquals(intArrayOf(0, 441, 150, 283, 205, 129),
            intArrayOf(t[0], t[1], t[0x1234], t[0x7A01], t[0xFFFF], t[0xE0CD]))
        assertArrayEquals(intArrayOf(487, 131, 115, 587, 175),
            intArrayOf(RdsCode.A, RdsCode.B, RdsCode.C, RdsCode.CP, RdsCode.D).map { RdsCode.checkword(0x7A01, it) }.toIntArray())
    }

    @Test fun rdsCharsCp1251() {
        assertEquals('A', RdsCode.char(0x41))
        assertEquals('Т', RdsCode.char(0xD2))
        assertEquals(' ', RdsCode.char(0x0D))
    }

    /** Кодер → MPX 240 кГц с пилотом → декодер: PS, RT, PTY, PI и часы, при свободном генераторе 57 кГц. */
    @Test fun rdsRoundTrip() {
        val fs = 240_000
        val enc = RdsEncoder(RdsEncoder.groups(0x7A01, "SDR4NOOB", "Тестовый эфир SDR4NOOB", pty = 10, ct = intArrayOf(2026, 10, 26, 12, 34)))
        val dec = RdsDecoder(fs)
        val block = fs / 20
        val mpx = FloatArray(block)
        var n = 0L
        repeat(20 * 8) {   // 8 с
            for (i in 0 until block) {
                val t = (n + i).toDouble() / fs
                val p = 2 * PI * 19_000 * t
                mpx[i] = (0.1 * sin(p) + 0.05 * enc.value(t) * sin(3 * p) + 0.3 * sin(2 * PI * 1000 * t)).toFloat()
            }
            n += block
            dec.process(mpx, block, null, null)
        }
        val st = dec.state()
        assertTrue("синхронизация", st.synced)
        assertEquals(0x7A01, st.pi)
        assertEquals("SDR4NOOB", st.ps)
        assertEquals("Тестовый эфир SDR4NOOB", st.rt)
        assertEquals(10, st.pty)
        assertEquals("Поп-музыка", st.ptyName)
        assertArrayEquals(intArrayOf(2026, 10, 26, 12, 34), st.ct)
    }

    /** Тестовый эфир 101,0 МГц: стерео и RDS через полную цепочку демодулятора (8 с в реальном темпе). */
    @Test fun rdsFromDemoStation() {
        val fs = 1_200_000
        val src = DemoSource()
        src.open(fs, 101_000_000L + 150_000)
        src.start()
        val d = RadioDemod(fs, Modulation.WFM).apply { offsetHz = -150_000.0 }
        val block = fs / 20
        val re = FloatArray(block); val im = FloatArray(block)
        val aud = FloatArray(2 * (block / 5 + 64))
        var got = 0
        while (got < fs * 8) { val k = src.read(re, im, block); if (k > 0) { d.process(re, im, k, aud); got += k } }
        assertTrue(d.stereoLock)
        assertEquals("SDR4NOOB", d.rds.state().ps)
        assertEquals("Тестовый эфир SDR4NOOB: музыка в стерео", d.rds.state().rt)
    }

    private fun ssbOut(mode: Modulation, toneOffset: Double): FloatArray {
        val fs = 960_000
        val n = fs
        val re = FloatArray(n); val im = FloatArray(n)
        for (i in 0 until n) {
            val ph = 2 * PI * (-100_000.0 + toneOffset) * i / fs
            re[i] = (0.1 * cos(ph)).toFloat(); im[i] = (0.1 * sin(ph)).toFloat()
        }
        val d = RadioDemod(fs, mode).apply { offsetHz = -100_000.0 }
        val out = ArrayList<Float>()
        val block = fs / 20
        val aud = FloatArray(2 * (block / 5 + 64))
        var pos = 0
        while (pos + block <= n) {
            val k = d.process(re.copyOfRange(pos, pos + block), im.copyOfRange(pos, pos + block), block, aud)
            for (i in 0 until k) out.add(aud[2 * i])
            pos += block
        }
        return out.toFloatArray()
    }

    /** Уровень звука без АРУ (постоянное усиление). */
    private fun rawLevel(mode: Modulation, toneOffset: Double): Double {
        val fs = 960_000
        val n = fs / 2
        val re = FloatArray(n); val im = FloatArray(n)
        for (i in 0 until n) {
            val ph = 2 * PI * (-100_000.0 + toneOffset) * i / fs
            re[i] = (0.1 * cos(ph)).toFloat(); im[i] = (0.1 * sin(ph)).toFloat()
        }
        val d = RadioDemod(fs, mode).apply { offsetHz = -100_000.0; agc.on = false; agc.manualDb = 0.0 }
        val aud = FloatArray(2 * (n / 5 + 64))
        val k = d.process(re, im, n, aud)
        var p = 0.0
        for (i in k / 2 until k) p += aud[2 * i] * aud[2 * i].toDouble()
        return p / (k - k / 2) + 1e-30
    }

    @Test fun usbPassesUpperAndRejectsLower() {
        val up = ssbOut(Modulation.USB, 1000.0)
        val m = up.size
        val pUp = tone(up, m / 2, m / 2 - 1, 1000.0, 48_000.0)
        val dn = ssbOut(Modulation.USB, -1000.0)
        val pDn = tone(dn, m / 2, m / 2 - 1, 1000.0, 48_000.0)
        assertTrue("тон 1 кГц над несущей слышен", pUp > 1e-3)
        assertTrue("тон под несущей подавлен: ${10 * log10(pDn / pUp)} dB", 10 * log10(pDn / pUp) < -30)
        val ratio = rawLevel(Modulation.USB, -1000.0) / rawLevel(Modulation.USB, 1000.0)
        assertTrue("до АРУ: ${10 * log10(ratio)} dB", 10 * log10(ratio) < -30)
    }

    @Test fun lsbIsMirrorOfUsb() {
        val dn = ssbOut(Modulation.LSB, -1000.0)
        val m = dn.size
        val pDn = tone(dn, m / 2, m / 2 - 1, 1000.0, 48_000.0)
        assertTrue(pDn > 1e-3)
        // АРУ поднимает и остаток подавленной полосы, поэтому сравниваем уровни на одном и том же входе
        val up = ssbOut(Modulation.LSB, 1000.0)
        val pUp = tone(up, m / 2, m / 2 - 1, 1000.0, 48_000.0)
        val upRaw = rawLevel(Modulation.LSB, 1000.0); val dnRaw = rawLevel(Modulation.LSB, -1000.0)
        assertTrue("до АРУ тон над несущей подавлен: ${10 * log10(upRaw / dnRaw)} dB", 10 * log10(upRaw / dnRaw) < -30)
        assertTrue(pUp <= pDn * 1.5)
    }

    @Test fun cwGivesTone() {
        val out = ssbOut(Modulation.CW, 0.0)   // несущая точно на частоте → тон cwTone 700 Гц
        val m = out.size
        val p700 = tone(out, m / 2, m / 2 - 1, 700.0, 48_000.0)
        val p1400 = tone(out, m / 2, m / 2 - 1, 1400.0, 48_000.0)
        assertTrue(p700 > 1e-3)
        assertTrue(p1400 < p700 * 1e-3)
    }

    @Test fun agcLevelsAndDelays() {
        val agc = Agc(48_000)
        val x = FloatArray(48_000) { (0.01 * sin(2 * PI * 500 * it / 48_000)).toFloat() }
        val out = FloatArray(48_000 + 96)
        val k = agc.process(x, x.size, out)
        assertEquals(48_000, k)
        var pk = 0f
        for (i in k / 2 until k) pk = maxOf(pk, abs(out[i]))
        assertEquals("слабый сигнал поднят до цели 0,5", 0.5f, pk, 0.05f)
        // хвост, не кратный 48 отсчётам, ждёт следующего блока
        assertEquals(0, agc.process(x, 47, out))
        assertEquals(48, agc.process(x, 1, out))
    }

    @Test fun bandwidthClampedToModeRange() {
        val d = RadioDemod(960_000, Modulation.USB)
        d.setBandwidth(10_000)
        assertEquals(4_000, d.bandwidth)
        d.setMode(Modulation.NFM, 1_000)
        assertEquals(5_000, d.bandwidth)
    }
}
