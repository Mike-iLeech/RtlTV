package io.github.mikeileech.sdr4noob

import io.github.mikeileech.sdr4noob.dsp.AnalogFrame
import io.github.mikeileech.sdr4noob.dsp.AnalogVideoDecoder
import io.github.mikeileech.sdr4noob.dsp.FftAny
import io.github.mikeileech.sdr4noob.dsp.FmSoundDecoder
import io.github.mikeileech.sdr4noob.dsp.MuxAnalyzer
import io.github.mikeileech.sdr4noob.dsp.MuxResult
import io.github.mikeileech.sdr4noob.dsp.OfdmEstimator
import io.github.mikeileech.sdr4noob.dsp.OfdmResult
import io.github.mikeileech.sdr4noob.dsp.RadioDemod
import io.github.mikeileech.sdr4noob.dsp.Tv
import io.github.mikeileech.sdr4noob.dsp.TvDigital
import io.github.mikeileech.sdr4noob.dsp.TvMode
import io.github.mikeileech.sdr4noob.dsp.TvScan
import io.github.mikeileech.sdr4noob.dsp.TvScanResult
import io.github.mikeileech.sdr4noob.dsp.TvTestSignal
import io.github.mikeileech.sdr4noob.dsp.XorShiftNoise
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.max
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Режим ТВ — перенос rtltv/standards.py, rtltv/digital.py, rtltv/analog.py, rtltv/testsignal.py и логики поиска
 * rtltv/engine.py. Эталоны посчитаны Python-модулями (android/app/src/test/python/gen_tv_ref.py → ресурс tv_ref.tsv):
 * сетки каналов, psd_db, mode_scores/guard_scores/detect_ofdm на синтетическом OFDM с циклическим префиксом
 * (плюс DC и паразитный тон), накопление OfdmEstimator, MuxAnalyzer по обходу тестового эфира, кадры
 * AnalogVideoDecoder, звук FmSoundDecoder и поиск каналов. Случайные числа — общий xorshift64*, поэтому входы
 * совпадают до округления float.
 */
class TvTest {
    private val rows: List<List<String>> by lazy {
        val txt = javaClass.getResourceAsStream("/tv_ref.tsv")!!.readBytes().toString(Charsets.UTF_8)
        txt.split(Regex("\r?\n")).filter { it.isNotEmpty() && !it.startsWith("#") }.map { it.split("\t") }
    }

    private fun rows(tag: String) = rows.filter { it[0] == tag }
    private fun nums(s: String) = if (s.isEmpty()) DoubleArray(0) else s.split(" ").map { it.toDouble() }.toDoubleArray()
    private fun bool(s: String) = s == "True"
    private fun dbl(s: String) = if (s == "nan") Double.NaN else s.toDouble()
    private fun close(exp: Double, act: Double, tol: Double, msg: String) {
        if (exp.isNaN()) { assertTrue("$msg: ждали nan, получили $act", act.isNaN()); return }
        assertTrue("$msg: ждали $exp, получили $act", abs(exp - act) <= tol)
    }
    private fun rel(exp: Double, act: Double, tol: Double, msg: String) = close(exp, act, tol * max(abs(exp), 1e-12), msg)

    // ------------------------------------------------------------------ стандарты и каналы

    @Test fun standardsMatchPython() {
        val st = rows("std")
        assertEquals(Tv.STANDARDS.keys.toList(), st.map { it[1] })
        for (r in st) {
            val s = Tv.STANDARDS.getValue(r[1])
            assertEquals(r[2], s.name); assertEquals(r[3].toInt(), s.lines)
            assertEquals(r[4].toDouble(), s.lineFreq, 0.0); assertEquals(r[5].toDouble(), s.soundOffset, 0.0)
            assertEquals(r[6].toDouble(), s.activeStartUs, 0.0); assertEquals(r[7].toDouble(), s.activeUs, 0.0)
            assertEquals(r[8].toDouble(), s.hsyncUs, 0.0)
            assertEquals(listOf(r[9], r[10], r[11], r[12], r[13]).map { it.toInt() }, listOf(s.vis1, s.vis2, s.visCount, s.nBroad, s.nEq))
            assertEquals(r[14].toDouble(), s.tuneOffset, 0.0)
        }
        val plans = rows("plan")
        assertEquals(Tv.PLANS.keys.toList(), plans.map { it[1] })
        for (p in plans) {
            val plan = Tv.PLANS.getValue(p[1])
            assertEquals(p[2], plan.name); assertEquals(p[3], plan.std)
            val exp = rows("ch").filter { it[1] == p[1] }
            val got = Tv.channels(p[1])
            assertEquals(exp.size, got.size)
            for ((e, c) in exp.zip(got)) {
                assertEquals(e[2], c.name)
                assertEquals(e[3].toDouble(), c.video, 0.0); assertEquals(e[4].toDouble(), c.center, 0.0)
                assertEquals(e[5].toDouble(), c.bandwidth, 0.0)
            }
        }
    }

    @Test fun tuningLogic() {
        val ru = Tv.channels("RU")
        val dk = Tv.STANDARDS.getValue("DK")
        val c30 = ru.first { it.name == "30 (ДМВ)" }
        assertEquals(c30, Tv.nearest(ru, 546.3e6, TvMode.DIGITAL))
        assertNull(Tv.nearest(ru, 546.6e6, TvMode.DIGITAL))
        assertEquals(c30, Tv.nearest(ru, 543.25e6, TvMode.ANALOG))
        assertEquals(8e6, Tv.muxBandwidth(ru, 546e6), 0.0)
        assertEquals(7e6, Tv.muxBandwidth(Tv.channels("EU"), 177.5e6), 0.0)
        assertEquals(8e6, Tv.muxBandwidth(ru, 100e6), 0.0)
        assertEquals(546e6, Tv.receiverCenter(TvMode.DIGITAL, true, dk, 583.25e6, 546e6), 0.0)
        assertEquals(583.75e6, Tv.receiverCenter(TvMode.ANALOG, false, dk, 583.25e6, 546e6), 0.0)
        assertEquals(583.25e6 + 6.5e6 + 0.25e6, Tv.receiverCenter(TvMode.ANALOG, true, dk, 583.25e6, 546e6), 0.0)
        assertEquals(583_251e3, Tv.spectrumClick(583_250_600.0, TvMode.ANALOG, false, dk), 0.0)
        assertEquals(583_250e3, Tv.spectrumClick(583_250_400.0 + 6.5e6, TvMode.ANALOG, true, dk), 0.0)
        assertEquals(546_010e3, Tv.spectrumClick(546_012_000.0, TvMode.DIGITAL, false, dk), 0.0)
        // полоса режима ТВ на Android — только частоты радиорежима, не ниже 2 МГц
        for (r in Tv.RATES) assertTrue(r in RadioDemod.RADIO_RATES && r >= 2 * TvDigital.HOP_KEEP)
    }

    // ------------------------------------------------------------------ psd_db

    private fun psdInput(n: Int, seed: Long): Pair<FloatArray, FloatArray> {
        val g = XorShiftNoise(seed)
        val nr = DoubleArray(n) { g.gauss() }
        val ni = DoubleArray(n) { g.gauss() }
        val re = FloatArray(n) { (0.01 * nr[it] + 0.3 * cos(2 * PI * 0.1 * it) + 0.02).toFloat() }
        val im = FloatArray(n) { (0.01 * ni[it] + 0.3 * sin(2 * PI * 0.1 * it) - 0.01).toFloat() }
        return re to im
    }

    @Test fun psdMatchesPython() {
        val rs = rows("psd")
        assertEquals(3, rs.size)
        for (r in rs) {
            val (re, im) = psdInput(r[1].toInt(), r[3].toLong())
            val exp = nums(r[4])
            val got = TvDigital.psdDb(re, im, re.size, r[2].toInt())
            assertEquals(exp.size, got.size)
            for (i in exp.indices) close(exp[i], got[i].toDouble(), 2e-3, "psd n=${r[1]} бин $i")
        }
    }

    // ------------------------------------------------------------------ OFDM

    private class Case(val name: String, val fs: Double, val mode: String, val gi: String?, val n: Int, val noise: Double,
                       val foff: Double, val dcR: Double, val dcI: Double, val spA: Double, val spF: Double)

    private val cases = listOf(
        Case("t2_32k_1_16", 2.4e6, "32K", "1/16", 393216, 0.7, 310.0, 0.08, 0.03, 0.2, 412e3),
        Case("t_8k_1_4", 2.88e6, "8K", "1/4", 393216, 1.0, -950.0, 0.0, 0.0, 0.0, 0.0),
        Case("t_2k_1_8", 2.4e6, "2K", "1/8", 300000, 0.5, 120.0, 0.05, 0.0, 0.1, -300e3),
        Case("t2_16k_19_128", 2.88e6, "16K", "19/128", 393216, 0.8, 40.0, 0.0, 0.0, 0.0, 0.0),
        Case("t2_32k_1_128", 2.4e6, "32K", "1/128", 393216, 0.3, 0.0, 0.0, 0.0, 0.0, 0.0),
        Case("noise", 2.4e6, "8K", null, 393216, 1.0, 0.0, 0.03, 0.0, 0.3, 250e3),
        Case("weak_8k_1_32", 2.4e6, "8K", "1/32", 393216, 3.0, 200.0, 0.0, 0.0, 0.0, 0.0),
    )

    /** Как ofdm_input в gen_tv_ref.py: QPSK во временной области + циклический префикс, уход, шум, DC, тон. */
    private fun ofdmInput(fs: Double, mode: String, gi: String?, n: Int, noise: Double, foff: Double,
                          dcR: Double, dcI: Double, spA: Double, spF: Double, seed: Long): Pair<FloatArray, FloatArray> {
        val g = XorShiftNoise(seed)
        val lu = Math.rint(TvDigital.FFT_MODES.getValue(mode) * fs).toInt()
        val lg = if (gi != null) Math.rint(lu * TvDigital.GUARDS.getValue(gi)).toInt() else 0
        val s2 = sqrt(2.0)
        val xr = DoubleArray(n + lu + lg); val xi = DoubleArray(n + lu + lg)
        var have = 0
        while (have < n) {
            val sr = DoubleArray(lu); val si = DoubleArray(lu)
            for (k in 0 until lu) { sr[k] = g.sign() / s2; si[k] = g.sign() / s2 }
            for (k in 0 until lg) { xr[have + k] = sr[lu - lg + k]; xi[have + k] = si[lu - lg + k] }
            for (k in 0 until lu) { xr[have + lg + k] = sr[k]; xi[have + lg + k] = si[k] }
            have += lg + lu
        }
        val nr = DoubleArray(n) { g.gauss() }
        val ni = DoubleArray(n) { g.gauss() }
        val w = 2 * PI * foff / fs
        val ws = 2 * PI * spF / fs
        val re = FloatArray(n); val im = FloatArray(n)
        for (i in 0 until n) {
            val c = cos(w * i); val s = sin(w * i)
            var a = xr[i] * c - xi[i] * s
            var b = xr[i] * s + xi[i] * c
            a += noise * nr[i]; b += noise * ni[i]
            a += dcR; b += dcI
            a += spA * cos(ws * i); b += spA * sin(ws * i)
            re[i] = a.toFloat(); im[i] = b.toFloat()
        }
        return re to im
    }

    private fun input(c: Case) = ofdmInput(c.fs, c.mode, c.gi, c.n, c.noise, c.foff, c.dcR, c.dcI, c.spA, c.spF, c.name.length * 7L + 1)

    private fun checkOfdm(r: List<String>, o: OfdmResult, msg: String) {
        assertEquals("$msg found", bool(r[2]), o.found)
        assertEquals("$msg mode", r[3], o.mode)
        assertEquals("$msg guard", r[4].takeIf { it != "None" }, o.guard)
        rel(r[5].toDouble(), o.rho, 2e-3, "$msg rho")
        rel(dbl(r[6]), o.peak ?: Double.NaN, 2e-3, "$msg peak")
        assertEquals("$msg captures", r[7].toInt(), o.captures)
        close(dbl(r[8]), o.freqOffsetHz ?: Double.NaN, 0.5, "$msg уход частоты")
    }

    @Test fun ofdmScoresAndDetectMatchPython() {
        for (c in cases) {
            val (re, im) = input(c)
            val (xr, xi) = TvDigital.prep(re, im)
            assertEquals(Integer.highestOneBit(c.n), xr.size)
            val ms = TvDigital.modeScores(xr, xi, c.fs)
            val expM = rows("modes").first { it[1] == c.name }[2].split(" ").associate { it.substringBefore("=") to it.substringAfter("=").toDouble() }
            assertEquals(expM.keys.toList(), ms.keys.toList())
            for ((k, v) in expM) rel(v, ms.getValue(k), 2e-3, "${c.name} mode_scores $k")
            val gr = rows("guards").first { it[1] == c.name }
            val gs = TvDigital.guardScores(xr, xi, c.fs, gr[2])
            val expG = gr[3].split(" ").associate { it.substringBefore("=") to it.substringAfter("=").split(",").map { s -> s.toDouble() } }
            assertEquals(expG.keys.toList(), gs.keys.toList())
            for ((k, v) in expG) {
                rel(v[0], gs.getValue(k).first, 2e-3, "${c.name} guard $k острота")
                if (v[0] > 5) close(v[1], gs.getValue(k).second, 1e-2, "${c.name} guard $k фаза")
            }
            val o = TvDigital.detectOfdm(re, im, c.fs)
            checkOfdm(rows("ofdm").first { it[1] == c.name }, o, c.name)
        }
        // режим, GI и уход частоты, которые и задавались (там, где Python их находит)
        val t2 = TvDigital.detectOfdm(input(cases[0]).first, input(cases[0]).second, 2.4e6)
        assertTrue(t2.found); assertEquals("32K", t2.mode); assertEquals("1/16", t2.guard)
    }

    @Test fun ofdmEstimatorAccumulates() {
        val est = OfdmEstimator(2.4e6)
        val exp = rows("ofdm_acc")
        assertEquals(4, exp.size)
        for (i in 0 until 3) {
            val (re, im) = ofdmInput(2.4e6, "8K", "1/8", 262144, 1.5, 500.0, 0.0, 0.0, 0.0, 0.0, 100L + i)
            checkOfdm(exp[i], est.add(re, im), "накопление $i")
        }
        val (re, im) = ofdmInput(2.4e6, "8K", null, 262144, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 200)
        checkOfdm(exp[3], est.add(re, im), "накопление 3 (без префикса)")
    }

    // ------------------------------------------------------------------ мультиплекс

    @Test fun muxPlanAndMetricsMatchPython() {
        val fs = 2.4e6
        for ((name, center, bw, sweeps, cycles) in listOf(
            listOf("mux", 546e6, 8e6, 2, 2), listOf("empty", 650e6, 8e6, 1, 2),
            listOf("analog", 586e6, 8e6, 1, 1), listOf("edge", 543e6, 7e6, 1, 1))) {
            name as String; center as Double; bw as Double; sweeps as Int; cycles as Int
            val ts = TvTestSignal(fs)
            val mux = MuxAnalyzer(fs, center, bw)
            val plan = mux.plan(true, sweeps)
            val expPlan = rows("mux_plan").first { it[1] == name }[2].split(" ")
                .map { it.substringBefore(":").toDouble() to it.substringAfter(":").toInt() }
            assertEquals(expPlan, plan.map { it.freq to it.samples })
            val res = ArrayList<MuxResult?>()
            repeat(cycles) {
                for (h in plan) {
                    val (re, im) = TvTestSignal.u8ToIq(ts.render(h.freq, h.samples, 30.0))
                    res += mux.add(h.freq, re, im)
                }
            }
            val e = rows("mux").first { it[1] == name }
            val got = res.filterNotNull()
            assertEquals("$name: итогов", e[2].toInt(), got.size)
            val last = got.last()
            close(e[3].toDouble(), last.cnDb, 0.02, "$name C/N")
            close(e[4].toDouble(), last.levelDb, 0.02, "$name уровень")
            close(e[5].toDouble(), last.flatnessDb, 0.02, "$name неравномерность")
            assertEquals("$name present", bool(e[6]), last.present)
            assertEquals("$name other", bool(e[7]), last.other)
            assertEquals(e[8].toInt(), last.psd.size)
            val ps = rows("mux_psd").first { it[1] == name }
            val ef = nums(ps[2]); val ep = nums(ps[3])
            for (i in ef.indices) {
                close(ef[i], last.freqs[9 * i], 1e-3, "$name частота $i")
                close(ep[i], last.psd[9 * i].toDouble(), 0.05, "$name спектр $i")
            }
            val eo = rows("mux_ofdm").firstOrNull { it[1] == name }
            if (eo == null) assertNull(last.ofdm) else checkOfdm(eo, assertNotNullAnd(last.ofdm), "$name OFDM")
        }
    }

    private fun <T> assertNotNullAnd(v: T?): T { assertNotNull(v); return v!! }

    @Test fun muxRejectsNarrowRate() {
        val r = runCatching { MuxAnalyzer(1.44e6, 546e6, 8e6) }
        assertTrue(r.isFailure)
        // обход 8 МГц: 7 перестроек по 2 МГц, у каждой берётся ±1 МГц
        val m = MuxAnalyzer(2.88e6, 546e6, 8e6)
        assertEquals((-3..3).map { it * 2e6 }, m.offsets)
        assertEquals(7 * 3 + 1, m.plan(true).size)
        assertEquals(TvDigital.CP_CAPTURE, m.plan(true).last().samples)
        assertEquals(7, m.plan(false, 1).size)
    }

    // ------------------------------------------------------------------ аналоговое ТВ

    @Test fun analogVideoMatchesPython() {
        val fs = 2.4e6
        val std = Tv.STANDARDS.getValue("DK")
        val ts = TvTestSignal(fs)
        val fc = TvTestSignal.TEST_ANALOG_VC + std.tuneOffset
        val dec = AnalogVideoDecoder(fs, std)
        val got = ArrayList<Triple<Int, AnalogFrame, Double>>()
        val blocks = List(3) { 65536 } + listOf(40000, 100000) + List(4) { 65536 }
        for ((b, n) in blocks.withIndex()) {
            val (re, im) = TvTestSignal.u8ToIq(ts.render(fc, n, 30.0))
            for (fr in dec.feed(re, im)) got += Triple(b, fr, dec.period)
        }
        val exp = rows("vframe")
        assertEquals(exp.size, got.size)
        for ((e, g) in exp.zip(got)) {
            val (b, fr, period) = g
            val k = e[1]
            assertEquals("кадр $k блок", e[2].toInt(), b)
            close(e[3].toDouble(), fr.hlock, 2e-3, "кадр $k hlock")
            assertEquals("кадр $k vlock", bool(e[4]), fr.vlock)
            close(e[5].toDouble(), fr.levelDb, 0.01, "кадр $k уровень")
            close(e[6].toDouble(), fr.snrDb, 0.05, "кадр $k С/Ш")
            rel(e[7].toDouble(), period, 1e-9, "кадр $k период строки")
            close(e[8].toDouble(), fr.image.average(), 1e-3, "кадр $k средняя яркость")
            val sub = nums(e[9])
            var i = 0; var sum = 0.0
            for (y in 0 until fr.height step 16) for (x in 0 until fr.width step 16) {
                sum += abs(sub[i] - fr.image[y * fr.width + x]); i++
            }
            assertEquals(sub.size, i)
            assertTrue("кадр $k: картинка расходится (${sum / i})", sum / i < 2e-3)
        }
        assertTrue(got.drop(1).all { it.second.vlock && it.second.hlock > 0.5 && it.second.snrDb > 10 })
    }

    @Test fun analogFieldSwapAndReset() {
        val fs = 2.4e6
        val std = Tv.STANDARDS.getValue("DK")
        fun frames(swap: Boolean): List<AnalogFrame> {
            val ts = TvTestSignal(fs)
            val dec = AnalogVideoDecoder(fs, std).also { it.fieldSwap = swap }
            val (re, im) = TvTestSignal.u8ToIq(ts.render(TvTestSignal.TEST_ANALOG_VC + std.tuneOffset, 250000, 30.0))
            return dec.feed(re, im)
        }
        val a = frames(false).last(); val b = frames(true).last()
        assertEquals(576, a.height); assertEquals(480, a.width)
        for (y in 0 until a.height step 2) for (x in 0 until a.width) {
            assertEquals(a.image[y * a.width + x], b.image[(y + 1) * a.width + x], 0f)
        }
        val dec = AnalogVideoDecoder(fs, std)
        val (re, im) = TvTestSignal.u8ToIq(TvTestSignal(fs).render(583.75e6, 200000, 30.0))
        assertTrue(dec.feed(re, im).isNotEmpty())
        dec.reset()
        assertEquals(dec.nominal, dec.period, 0.0); assertFalse(dec.vlock)
        assertTrue(dec.feed(re, im, 50000).isEmpty())
    }

    @Test fun fmSoundMatchesPython() {
        val fs = 2.4e6
        val std = Tv.STANDARDS.getValue("DK")
        val ts = TvTestSignal(fs)
        val fc = TvTestSignal.TEST_ANALOG_VC + std.soundOffset + Tv.SOUND_TUNE
        val fm = FmSoundDecoder(fs, -Tv.SOUND_TUNE)
        val out = ArrayList<Float>()
        for (n in intArrayOf(65536, 30001, 7, 65536, 12345, 65536)) {
            val (re, im) = TvTestSignal.u8ToIq(ts.render(fc, n, 30.0))
            for (v in fm.process(re, im)) out += v
        }
        val e = rows("fm").single()
        val exp = nums(e[2])
        assertEquals(e[1].toInt(), out.size)
        var mx = 0.0
        for (i in exp.indices) mx = max(mx, abs(exp[i] - out[i]))
        assertTrue("звук расходится: $mx", mx < 1e-4)
        assertEquals(12, fm.d1); assertEquals(5, fm.d2); assertEquals(40000.0, fm.fs2, 1e-9)
    }

    // ------------------------------------------------------------------ поиск каналов

    @Test fun scanMatchesPython() {
        val fs = 2.4e6
        val std = Tv.STANDARDS.getValue("DK")
        val chans = Tv.channels("RU")
        for (kind in listOf(TvMode.ANALOG, TvMode.DIGITAL)) {
            val tag = if (kind == TvMode.ANALOG) "analog" else "digital"
            val exp = rows("scan").filter { it[1] == tag }
            val sel = exp.map { e -> chans.first { it.name == e[2] } }
            val items = TvScan.items(kind, sel, std)
            val ts = TvTestSignal(fs, seed = 5)
            val res = ArrayList<TvScanResult>()
            for ((i, h) in items.withIndex()) {
                assertEquals(TvScan.CAPTURE, h.samples)
                val (re, im) = TvTestSignal.u8ToIq(ts.render(h.freq, h.samples, 30.0))
                TvScan.step(res, kind, sel[i], i, items.size, re, im, fs, std)
            }
            for ((e, r) in exp.zip(res)) {
                assertEquals(e[2], r.ch.name)
                close(e[3].toDouble(), r.value, 0.02, "поиск ${e[2]}")
                assertEquals("поиск ${e[2]}", bool(e[4]), r.found)
            }
        }
    }

    // ------------------------------------------------------------------ вспомогательное

    @Test fun fftAnyMatchesNaiveDft() {
        for (n in intArrayOf(1, 2, 3, 7, 30, 64, 101)) {
            val g = XorShiftNoise(n.toLong())
            val xr = DoubleArray(n) { g.gauss() }; val xi = DoubleArray(n) { g.gauss() }
            val er = DoubleArray(n); val ei = DoubleArray(n)
            for (k in 0 until n) for (j in 0 until n) {
                val a = 2 * PI * ((j.toLong() * k) % n) / n
                er[k] += (xr[j] * cos(a) - xi[j] * sin(a)) / n
                ei[k] += (xr[j] * sin(a) + xi[j] * cos(a)) / n
            }
            val rr = xr.copyOf(); val ri = xi.copyOf()
            FftAny(n).inverse(rr, ri)
            for (k in 0 until n) { assertEquals(er[k], rr[k], 1e-9); assertEquals(ei[k], ri[k], 1e-9) }
        }
    }

    @Test fun testCardShape() {
        val c = TvTestSignal.makeCard(576, 640)
        assertEquals(576 * 640, c.size)
        assertTrue(c.all { it in 0f..1f })
        assertEquals(0f, c[288 * 640 + 100], 0f)   // перекрестие по центру
    }
}
