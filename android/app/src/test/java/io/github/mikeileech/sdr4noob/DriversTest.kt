package io.github.mikeileech.sdr4noob

import io.github.mikeileech.sdr4noob.dsp.StreamResampler
import io.github.mikeileech.sdr4noob.sdr.GainMap
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.sin
import kotlin.math.sqrt

/** HackRF и Airspy: раскладка усиления (эталоны — rtltv/devices.py) и пересчёт частоты дискретизации. */
class DriversTest {
    @Test fun hackrfSplitLikeDesktop() {
        // эталон: [hackrf_split(t) for t in (None, 0, 10, 95, 250, 400, 780, 1020, 1030, 1160, 2000)]
        val want = listOf(Triple(false, 24, 20), Triple(false, 0, 0), Triple(false, 8, 0), Triple(false, 0, 10),
            Triple(false, 16, 8), Triple(false, 16, 24), Triple(false, 32, 46), Triple(false, 40, 62), Triple(true, 40, 48),
            Triple(true, 40, 62), Triple(true, 40, 62))
        val got = listOf(-1, 0, 10, 95, 250, 400, 780, 1020, 1030, 1160, 2000).map { GainMap.hackrfSplit(it) }
        assertEquals(want, got)
        assertEquals(Triple(false, 0, 8), GainMap.hackrfSplit(85))     // round(8,5) = 8 — как в Python (эталон)
    }

    @Test fun airspyStepLikeDesktop() {
        // эталон: [airspy_step(t) for t in (0, 10, 29, 30, 200, 419, 420, 999)]
        assertArrayEquals(intArrayOf(0, 0, 1, 2, 10, 21, 21, 21),
            intArrayOf(0, 10, 29, 30, 200, 419, 420, 999).map { GainMap.airspyStep(it) }.toIntArray())
    }

    private fun run(fsIn: Int, fsOut: Int, f: Double, chunks: IntArray, total: Int): Pair<FloatArray, FloatArray> {
        val rs = StreamResampler(fsIn, fsOut)
        val outR = ArrayList<Float>(); val outI = ArrayList<Float>()
        var pos = 0; var c = 0
        while (pos < total) {
            val n = minOf(chunks[c++ % chunks.size], total - pos)
            val r = FloatArray(n) { cos(2 * PI * f * (pos + it) / fsIn).toFloat() }
            val i = FloatArray(n) { sin(2 * PI * f * (pos + it) / fsIn).toFloat() }
            val oR = FloatArray(rs.outFor(n) + 4); val oI = FloatArray(rs.outFor(n) + 4)
            val k = rs.process(r, i, n, oR, oI)
            for (j in 0 until k) { outR.add(oR[j]); outI.add(oI[j]) }
            pos += n
        }
        return outR.toFloatArray() to outI.toFloatArray()
    }

    /** 3 МГц (Airspy Mini) → 2,4 МГц: тон 300 кГц остаётся тоном той же частоты, амплитуда ≈ 1, без швов. */
    @Test fun resamplerKeepsToneAndIsSeamless() {
        val (r1, i1) = run(3_000_000, 2_400_000, 300_000.0, intArrayOf(150_000), 600_000)
        val (r2, i2) = run(3_000_000, 2_400_000, 300_000.0, intArrayOf(7, 4096, 333, 150_000), 600_000)
        assertEquals(480_000.0, r1.size.toDouble(), 2.0)
        val n = minOf(r1.size, r2.size)
        for (j in 0 until n) { assertEquals(r1[j], r2[j], 1e-4f); assertEquals(i1[j], i2[j], 1e-4f) }
        // после задержки фильтра: модуль ≈ 1, фаза растёт на 2π·300k/2,4M за отсчёт
        val w = 2 * PI * 300_000 / 2_400_000
        for (j in 1000 until n - 1) {
            assertEquals(1.0, sqrt((r1[j] * r1[j] + i1[j] * i1[j]).toDouble()), 0.01)
            val dphi = Math.atan2((i1[j + 1] * r1[j] - r1[j + 1] * i1[j]).toDouble(), (r1[j + 1] * r1[j] + i1[j + 1] * i1[j]).toDouble())
            assertEquals(w, dphi, 1e-3)
        }
    }

    /**
     * Наложение из-за Найквиста выхода подавлено так же, как у StreamResampler на ПК (эталон Python:
     * 1,3 МГц → −33,745 dB у края полосы, 1,45 МГц → −88,3 dB).
     */
    @Test fun resamplerRejectsAliasLikeDesktop() {
        fun level(f: Double): Double {
            val (r, i) = run(3_000_000, 2_400_000, f, intArrayOf(100_000), 300_000)
            var p = 0.0
            for (j in 1000 until r.size) p += r[j] * r[j] + i[j] * i[j]
            return 10 * Math.log10(p / (r.size - 1000))
        }
        assertEquals(-33.745, level(1_300_000.0), 0.05)
        assertTrue(level(1_450_000.0) < -80)
    }

    @Test fun hackrfNativeRates() {
        // HackRF работает от 2 МГц: 0,96 → 2,88 (×3), 1,2 → 2,4 (×2); 2,4 — как есть
        val r = StreamResampler(2_880_000, 960_000)
        assertEquals(1, r.up); assertEquals(3, r.down)
        assertTrue(abs(StreamResampler(10_000_000, 2_880_000).up - 36) == 0)
    }
}
