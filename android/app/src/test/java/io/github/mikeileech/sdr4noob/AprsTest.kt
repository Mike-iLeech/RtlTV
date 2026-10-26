package io.github.mikeileech.sdr4noob

import io.github.mikeileech.sdr4noob.dsp.AfskDemodulator
import io.github.mikeileech.sdr4noob.dsp.Afsk
import io.github.mikeileech.sdr4noob.dsp.Aprs
import io.github.mikeileech.sdr4noob.dsp.AprsPacket
import io.github.mikeileech.sdr4noob.dsp.AprsRxFrame
import io.github.mikeileech.sdr4noob.dsp.Ax25
import io.github.mikeileech.sdr4noob.dsp.Firwin
import io.github.mikeileech.sdr4noob.dsp.Modulation
import io.github.mikeileech.sdr4noob.dsp.PyNum
import io.github.mikeileech.sdr4noob.dsp.RadioDemod
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.ln
import kotlin.math.pow
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * APRS — перенос rtltv/aprs.py. Эталоны посчитаны Python-модулем (android/app/src/test/python/gen_aprs_ref.py →
 * ресурс aprs_ref.tsv): FCS и кадры, кодер AFSK, коэффициенты scipy.firwin, кадры демодулятора на чистом и
 * искажённом звуке (шум — общий для Python и Kotlin генератор, поэтому сверка до кадра и числа срезателей),
 * кодеры позиций, разбор и describe() набора строк TNC2 (включая ~400 случайно испорченных).
 */
class AprsTest {
    // ------------------------------------------------------------------ эталоны

    private class Rx(val hex: String, val t: Double, val fixed: Int, val decoders: Int)

    private fun unesc(s: String): String {
        val sb = StringBuilder()
        var i = 0
        while (i < s.length) {
            val c = s[i]
            if (c == '\\' && i + 1 < s.length) {
                if (s[i + 1] == '\\') { sb.append('\\'); i += 2; continue }
                if (s[i + 1] == 'u') { sb.append(s.substring(i + 2, i + 6).toInt(16).toChar()); i += 6; continue }
            }
            sb.append(c); i++
        }
        return sb.toString()
    }

    private val rows: List<List<String>> by lazy {
        val txt = javaClass.getResourceAsStream("/aprs_ref.tsv")!!.readBytes().toString(Charsets.UTF_8)
        txt.split("\n").filter { it.isNotEmpty() && !it.startsWith("#") }.map { ln -> ln.split("\t").map { unesc(it) } }
    }

    private fun rows(tag: String) = rows.filter { it[0] == tag }

    private fun rxList(s: String) = if (s.isEmpty()) emptyList() else s.split(";").map {
        val f = it.split(",")
        Rx(f[0], f[1].toDouble(), f[2].toInt(), f[3].toInt())
    }

    private fun ByteArray.hex() = joinToString("") { "%02x".format(it.toInt() and 0xFF) }
    private fun unhex(s: String) = ByteArray(s.length / 2) { s.substring(2 * it, 2 * it + 2).toInt(16).toByte() }

    private val lines get() = rows("LINE").map { it[1] }
    private fun frames() = lines.map { Ax25.tnc2Frame(it) }

    // ------------------------------------------------------------------ общий с Python генератор шума

    /** xorshift64* → равномерное [0,1) → нормальное (Бокс — Мюллер), как Noise в gen_aprs_ref.py. */
    private class Noise(seed: Int) {
        private var x = seed.toLong() * -7046029254386353131L + 1   // 0x9E3779B97F4A7C15
        private fun next(): Long {
            x = x xor (x ushr 12); x = x xor (x shl 25); x = x xor (x ushr 27)
            return x * 0x2545F4914F6CDD1DL
        }
        private fun uni() = (next() ushr 11) * (1.0 / 9007199254740992.0)
        fun gauss(): Double {
            val u1 = 1.0 - uni()
            val u2 = uni()
            return sqrt(-2.0 * ln(u1)) * cos(2.0 * PI * u2)
        }
    }

    private fun addNoise(x: FloatArray, snrDb: Double, seed: Int): FloatArray {
        var s = 0.0; var n = 0
        for (v in x) if (abs(v) > 0) { s += v.toDouble() * v.toDouble(); n++ }
        val sd = sqrt(s / n / 10.0.pow(snrDb / 10))
        val g = Noise(seed)
        return FloatArray(x.size) { x[it] + (sd * g.gauss()).toFloat() }
    }

    private fun checkRx(name: String, exp: List<Rx>, got: List<AprsRxFrame>) {
        val e = exp.joinToString { "${it.hex.take(16)}/${it.fixed}/${it.decoders}" }
        val g = got.joinToString { "${it.data.hex().take(16)}/${it.fixed}/${it.decoders}" }
        assertEquals("$name: кадры", e, g)
        for ((a, b) in exp.zip(got)) {
            assertEquals("$name: данные", a.hex, b.data.hex())
            assertEquals("$name: время", a.t, b.t, 1e-6)
        }
    }

    // ------------------------------------------------------------------ AX.25 / CRC

    @Test fun fcsAndFramesMatchPython() {
        assertEquals(rows("FCS123")[0][1].toInt(), Ax25.fcs("123456789".toByteArray()))
        assertEquals(0x906E, Ax25.fcs("123456789".toByteArray()))
        val exp = rows("FRAMEFCS").map { it[1] }
        assertEquals(exp, frames().map { Ax25.addFcs(it).hex() })
        for (f in frames()) assertTrue(Ax25.checkFcs(Ax25.addFcs(f)))
        val mk = rows("MAKE")[0]
        val f = Ax25.makeFrame("R2ABC-15", "APRS", listOf("R3DIG-1*", "WIDE2-1"), "!test")
        assertEquals(mk[1], f.hex())
        val fr = Ax25.decode(f)!!
        assertEquals("R2ABC-15", fr.src); assertEquals("APRS", fr.dest)
        assertEquals(listOf("R3DIG-1" to true, "WIDE2-1" to false), fr.path)
        assertEquals(mk[2], Ax25.tnc2(fr))
        assertNull(Ax25.decode(ByteArray(20)))
    }

    @Test fun tryFixMatchesPython() {
        val r = rows("FIX")
        assertEquals(5, r.size)
        for (row in r) {
            val (fx, n) = Ax25.tryFix(unhex(row[1]))
            assertEquals(row[1], row[2].toInt(), n)
            assertEquals(row[1], row[3], fx?.hex() ?: "None")
        }
    }

    // ------------------------------------------------------------------ кодер и фильтры

    @Test fun firwinBandpassMatchesScipy() {
        for (row in rows("FIR")) {
            val h = Firwin.bandpass(81, row[1].toDouble(), row[2].toDouble(), 48000.0)
            val exp = row.drop(3).map { it.toDouble() }
            assertEquals(81, exp.size)
            for (i in h.indices) assertEquals("отвод $i", exp[i], h[i], 1e-13)
        }
    }

    @Test fun encoderMatchesPython() {
        val hb = Afsk.hdlcBits(Ax25.addFcs(frames()[0]))
        assertEquals(rows("HDLC")[0][1], hb.joinToString(""))
        assertEquals(rows("NRZI")[0][1], Afsk.nrzi(hb).joinToString(""))
        val x = Afsk.encodePackets(frames(), gap = 0.4)
        val e = rows("ENC")[0]
        assertEquals(e[1].toInt(), x.size)
        assertEquals(e[2].toDouble(), x.sumOf { abs(it.toDouble()) }, 1e-6)
        for (row in rows("ENCV")) assertEquals("отсчёт ${row[1]}", row[2].toDouble(), x[row[1].toInt()].toDouble(), 2e-7)
    }

    // ------------------------------------------------------------------ кодер → демодулятор

    @Test fun demodulatorMatchesPython() {
        val fr = frames()
        val cases = rows("DEMOD")
        assertTrue(cases.size >= 20)
        val hexes = fr.map { it.hex() }.toSet()
        for (c in cases) {
            val name = c[1]
            var x = Afsk.encodePackets(fr, fs = c[5].toDouble(), gap = 0.4, amp = c[2].toDouble(),
                spaceGain = c[3].toDouble(), shift = c[4].toDouble())
            assertEquals("$name: длина звука", c[9].toInt(), x.size)
            if (c[6] != "None") x = addNoise(x, c[6].toDouble(), c[7].toInt())
            val got = AfskDemodulator.decodeAudio(x, block = c[8].toInt())
            val exp = rxList(c.getOrElse(10) { "" })
            checkRx(name, exp, got)
            // как в tests/test_aprs.py: на этих искажениях принимаются все пакеты
            if (!name.startsWith("snr0") && !name.startsWith("snr-") && !name.startsWith("snr3"))
                assertEquals("$name: принято", fr.size, got.count { it.data.hex() in hexes })
        }
    }

    @Test fun cleanSignalAllFramesNoDuplicates() {
        val fr = frames()
        val out = AfskDemodulator.decodeAudio(Afsk.encodePackets(fr, gap = 0.4))
        assertEquals(fr.map { it.hex() }, out.map { it.data.hex() })
        assertTrue(out.all { it.fixed == 0 })
        assertTrue(out.minOf { it.decoders } >= 8)
    }

    @Test fun repeatedFramesAreKept() {
        val f = frames()[0]
        val out = AfskDemodulator.decodeAudio(Afsk.encodePackets(listOf(f, f, f), gap = 0.4))
        assertEquals(3, out.size)
        checkRx("повторы", rxList(rows("REPEATED")[0].getOrElse(1) { "" }), out)
    }

    @Test fun noiseOnlyNoFrames() {
        val g = Noise(9)
        val x = FloatArray(48000 * 20) { (0.3 * g.gauss()).toFloat() }
        val out = AfskDemodulator.decodeAudio(x)
        checkRx("шум", rxList(rows("NOISEONLY")[0].getOrElse(1) { "" }), out)
        assertTrue(out.isEmpty())
    }

    @Test fun demodulatorIsStreaming() {
        // результат не зависит от нарезки потока на блоки (кроме слияния срезателей внутри блока)
        val fr = frames()
        val x = addNoise(Afsk.encodePackets(fr, gap = 0.4), 12.0, 6)
        val hexes = fr.map { it.hex() }.toSet()
        for (block in intArrayOf(1, 37, 480, 9600, x.size)) {
            val out = AfskDemodulator.decodeAudio(x, block = block)
            assertEquals("блок $block", fr.size, out.count { it.data.hex() in hexes })
        }
        val dem = AfskDemodulator()
        assertEquals(13, dem.nDecoders)
        assertTrue(dem.process(FloatArray(0)).isEmpty())
    }

    @Test fun speed() {
        // как test_speed: декодер (13 срезателей) должен быть много быстрее реального времени
        val one = addNoise(Afsk.encodePackets(frames(), gap = 0.4), 10.0, 0)
        val x = FloatArray(one.size * 4) { one[it % one.size] }
        AfskDemodulator.decodeAudio(one)                       // прогрев JIT
        val t0 = System.nanoTime()
        AfskDemodulator.decodeAudio(x)
        val dt = (System.nanoTime() - t0) / 1e9
        val dur = x.size / 48000.0
        assertTrue("декодер медленный: $dt с на $dur с звука", dt < 0.25 * dur)
    }

    @Test fun rfThroughNfm() {
        // полный путь, как test_rf_through_nfm: AFSK → ЧМ (девиация 3 кГц) на ПЧ с расстройкой → RadioDemod NFM → декодер
        val fs = 960_000
        val fr = frames()
        val a = Afsk.encodePackets(fr, gap = 0.4, amp = 0.6, spaceGain = 1.4)
        val hexes = fr.map { it.hex() }.toSet()
        for (rfOff in doubleArrayOf(-500.0, 0.0, 500.0)) {
            val d = RadioDemod(fs, Modulation.NFM).apply { offsetHz = 25000.0 }
            val dem = AfskDemodulator()
            val g = Noise(3)
            val up = fs / 48000
            val blk = fs / 10
            val re = FloatArray(blk); val im = FloatArray(blk)
            val aud = FloatArray(2 * (blk / 5 + 64))
            var ph = 0.0
            var pos = 0
            val total = a.size * up
            val out = ArrayList<AprsRxFrame>()
            while (pos < total) {
                val n = minOf(blk, total - pos)
                for (i in 0 until n) {
                    ph += 2 * PI * (25000 + rfOff + 3000 * a[(pos + i) / up]) / fs
                    re[i] = (0.1 * cos(ph) + 0.02 * g.gauss()).toFloat()
                    im[i] = (0.1 * sin(ph) + 0.02 * g.gauss()).toFloat()
                }
                ph %= 2 * PI
                val k = d.process(re, im, n, aud)
                out.addAll(dem.process(FloatArray(k) { aud[2 * it] }))
                pos += n
            }
            assertEquals("расстройка $rfOff", fr.size, out.count { it.data.hex() in hexes })
        }
    }

    // ------------------------------------------------------------------ кодеры полей

    @Test fun fieldEncodersMatchPython() {
        val exp = rows("ENCF").associate { it[1] to it[2] }
        assertEquals(exp["pos1"], Aprs.encodePosition(55.6425, 37.391667, "/", ">", "комм"))
        assertEquals(exp["pos2"], Aprs.encodePosition(-33.999999, -70.0001, "\\", "k", "", true))
        assertEquals(exp["pos3"], Aprs.encodePosition(55.75, 37.62, "/", "_", "", false, "231200z"))
        assertEquals(exp["pos4"], Aprs.encodePosition(10.0049999, 9.99999, "/", "-", "", true, "231200z"))
        assertEquals(exp["cmp1"], Aprs.encodeCompressed(55.7, 37.6, "/", ">", course = 88.0, speedKn = 36.0, comment = "сжатая"))
        assertEquals(exp["cmp2"], Aprs.encodeCompressed(55.0, 37.0, "/", "O", altFt = 10000.0))
        assertEquals(exp["cmp3"], Aprs.encodeCompressed(-12.5, 120.25, "\\", "#"))
        assertEquals(exp["cmp4"], Aprs.encodeCompressed(49.5, -72.75, "/", ">", course = 360.0, speedKn = 0.0))
        val mice = rows("ENCM").map { it[1] to it[2] }
        val got = listOf(
            Aprs.encodeMice(55.75580, 37.61730, 45.0, 275.0, "/", ">", 6, "мобильный", 150.0, null),
            Aprs.encodeMice(33.4273, -112.129, 0.0, 0.0, "/", ">", 0, "", null, null),
            Aprs.encodeMice(-5.5, 5.25, 12.6, 359.6, "/", "[", 7, "x", null, "]="),
            Aprs.encodeMice(60.0, 105.0, 120.0, 90.0, "\\", "k", 3, "", -50.0, "_ "),
            Aprs.encodeMice(0.0, 179.99, 799.0, 180.0, "/", ">", 1, "", null, ">"),
        )
        assertEquals(mice, got)
    }

    @Test fun encodersRoundTrip() {
        val p = Aprs.parseLine("UB3XYZ-7>APRS:" + Aprs.encodeCompressed(55.7, 37.6, "/", ">", course = 88.0, speedKn = 36.0,
            comment = "сжатая"))!!
        assertEquals("position", p.type); assertEquals(true, p.compressed)
        assertEquals(55.7, p.lat!!, 1e-4); assertEquals(37.6, p.lon!!, 1e-4); assertEquals(88.0, p.course!!, 4.0)
        for (msg in 0..7) {
            val (d, i) = Aprs.encodeMice(48.5, -10.25, msg = msg)
            val q = Aprs.parseLine("N0CALL>$d:$i")!!
            assertEquals("mic-e", q.type)
            assertEquals(Aprs.MICE_STD[msg], q.miceMsg)
            assertEquals(48.5, q.lat!!, 1e-4); assertEquals(-10.25, q.lon!!, 1e-4)
        }
    }

    // ------------------------------------------------------------------ разбор и расшифровка

    private fun pyStr(v: Any?): String = when (v) {
        null -> "None"
        is Boolean -> if (v) "True" else "False"
        is List<*> -> v.joinToString(",") { pyStr(it) }
        else -> v.toString()
    }

    private val numKeys = setOf("lat", "lon", "course", "speed_kmh", "alt_m", "range_km")

    private fun fields(p: AprsPacket): Map<String, Any?> {
        val path = { l: List<Pair<String, Boolean>> -> l.joinToString(",") { it.first + if (it.second) "*" else "" } }
        val m = p.message
        val t = p.telemetry
        val g = p.phg
        return linkedMapOf(
            "type" to p.type, "kind" to p.kind, "symbol" to p.symbol, "via" to p.via, "via_iss" to p.viaIss,
            "lat" to p.lat, "lon" to p.lon, "course" to p.course, "speed_kmh" to p.speedKmh, "alt_m" to p.altM,
            "comment" to p.comment, "raw" to p.raw, "timestamp" to p.timestamp, "name" to p.name, "alive" to p.alive,
            "status" to p.status, "mice_msg" to p.miceMsg, "radio" to p.radio, "gateway" to p.gateway,
            "third_party" to (if (p.thirdParty) true else null), "text" to p.text, "sym_table" to p.symTable,
            "sym_code" to p.symCode, "compressed" to p.compressed, "ambiguity" to p.ambiguity,
            "messaging" to p.messaging, "range_km" to p.rangeKm, "dfs" to p.dfs, "mic_e" to (if (p.micE) true else null),
            "msg" to (m?.let { listOf(it.to, it.kind, it.id ?: "None", it.text).joinToString("\u001f") } ?: "None"),
            "tlm" to t, "wx" to (p.wx?.let { Aprs.wxText(it) } ?: "None"),
            "phg" to g, "df" to (p.dfBearing?.toString() ?: "None"), "path" to path(p.path),
            "outer_path" to (p.outerPath?.let(path) ?: "None"), "describe" to Aprs.describe(p),
        )
    }

    private fun compare(where: String, exp: List<String>, p: AprsPacket?) {
        if (exp.size == 1 && exp[0] == "None") { assertNull(where, p); return }
        assertNotNull(where, p)
        val got = fields(p!!)
        for (kv in exp) {
            val k = kv.substringBefore('='); val e = kv.substringAfter('=')
            assertTrue("$where: нет поля $k", got.containsKey(k))
            val v = got[k]
            val msg = "$where [$k]"
            when {
                k in numKeys -> if (e == "None") assertNull(msg, v) else {
                    val ev = PyNum.float(e)!!
                    val gv = v as Double
                    if (ev.isNaN()) assertTrue(msg, gv.isNaN()) else assertEquals(msg, ev, gv, 1e-9 * maxOf(1.0, abs(ev)))
                }
                k == "tlm" -> {
                    if (e == "None") { assertNull(msg, v); continue }
                    val tl = v as io.github.mikeileech.sdr4noob.dsp.AprsTelemetry
                    val (seq, an, bits) = e.split("\u001f")
                    assertEquals(msg, seq, tl.seq); assertEquals(msg, bits, tl.bits ?: "None")
                    val ea = if (an.isEmpty()) emptyList() else an.split(",")
                    assertEquals(msg, ea.size, tl.analog.size)
                    for ((x, y) in ea.zip(tl.analog)) {
                        if (x == "None") assertNull(msg, y) else {
                            val xv = PyNum.float(x)!!
                            if (xv.isNaN()) assertTrue(msg, y!!.isNaN()) else assertEquals(msg, xv, y!!, 1e-12 * maxOf(1.0, abs(xv)))
                        }
                    }
                }
                k == "phg" -> {
                    if (e == "None") { assertNull(msg, v); continue }
                    val ph = v as io.github.mikeileech.sdr4noob.dsp.AprsPhg
                    val f = e.split("|")
                    assertEquals(msg, f[0].toInt(), ph.powerW); assertEquals(msg, f[1].toDouble(), ph.heightM, 1e-9)
                    assertEquals(msg, f[2].toInt(), ph.gainDb); assertEquals(msg, f[3], ph.dir?.toString() ?: "None")
                }
                else -> assertEquals(msg, e, pyStr(v))
            }
        }
    }

    @Test fun parseMatchesPython() {
        val r = rows("PARSE")
        assertTrue(r.size > 450)
        var fails = 0
        val errs = StringBuilder()
        for (row in r) {
            try {
                compare(row[1], row.drop(2), Aprs.parseLine(row[1]))
            } catch (e: AssertionError) {
                fails++
                if (fails <= 15) errs.append(e.message).append('\n')
            } catch (e: Exception) {
                fails++
                if (fails <= 15) errs.append("${row[1]}: $e").append('\n')
            }
        }
        if (fails > 0) fail("расхождений с Python: $fails из ${r.size}\n$errs")
    }

    @Test fun noCrashWherePythonRaises() {
        // Python на них падает (ValueError в float() скорости/курса NMEA); Kotlin отдаёт пакет «other» без исключения
        val r = rows("CRASH")
        assertTrue(r.isNotEmpty())
        for (row in r) {
            val p = Aprs.parseLine(row[1])!!
            assertEquals(row[1], "other", p.type)
            assertTrue(Aprs.describe(p).isNotEmpty())
        }
        val ts = Aprs.parseLine("R2ABC>APRS:@²92345z5538.55N/03723.50E>x")!!   // isdigit() да, int() нет
        assertNull(ts.timestamp); assertEquals("position", ts.type)
    }

    @Test fun parseFramesMatchPython() {
        for (row in rows("PARSEF")) compare(row[1], row.drop(2), Aprs.parseFrame(unhex(row[1])))
    }

    @Test fun typicalPackets() {
        val p = Aprs.parseLine("UA3ABC-9>APRS,WIDE1-1:!5538.55N/03723.50E>088/036Еду /A=000500")!!
        assertEquals("position", p.type); assertEquals("car", p.kind); assertEquals("Автомобиль", p.symbol)
        assertEquals(88.0, p.course!!, 0.0); assertEquals(36 * 1.852, p.speedKmh!!, 1e-9)
        assertEquals(500 * 0.3048, p.altM!!, 1e-9); assertEquals("Еду", p.comment)
        val m = Aprs.parseLine("R2ABC>APRS::UA3ABC-9 :Привет{12")!!.message!!
        assertEquals("UA3ABC-9", m.to); assertEquals("Привет", m.text); assertEquals("12", m.id)
        val q = Aprs.parseLine("RA3WX-13>APRS:_10231200c220s004g005t-04r000p000P000h50b09900")!!
        assertEquals("weather", q.type)
        assertTrue(Aprs.describe(q).contains("давление 990,0 гПа"))
        val t = Aprs.parseLine("R3GW>APRS:}DL1ABC>APRS,TCPIP,R3GW*:!4903.50N/00720.00E-gate")!!
        assertEquals("DL1ABC", t.src); assertEquals("R3GW", t.gateway); assertTrue(t.thirdParty)
        assertEquals("через МКС", Aprs.parseLine(lines[4])!!.via)
    }

    @Test fun geoHelpersMatchPython() {
        val loc = rows("LOC")[0]
        assertEquals(loc[1], Aprs.locator(55.75580, 37.61730))
        assertEquals(loc[2], Aprs.locator(-33.9, -70.65))
        assertEquals(loc[3], Aprs.locator(0.0, 0.0))
        val d = rows("DIST")[0]
        assertEquals(d[1].toDouble(), Aprs.distanceKm(55.75, 37.62, 59.94, 30.31), 1e-9)
        assertEquals(d[2].toDouble(), Aprs.bearingDeg(55.75, 37.62, 59.94, 30.31), 1e-9)
    }

    @Test fun pythonNumberFormatting() {
        assertEquals("0.12", PyNum.fixed(0.125, 2))     // половина — к чётному, по точному значению
        assertEquals("-0.0", PyNum.fixed(-0.01, 1))
        assertEquals("2", PyNum.fixed(2.5, 0))
        assertEquals("100", PyNum.g(100.0)); assertEquals("1.5", PyNum.g(1.5)); assertEquals("1e+06", PyNum.g(1e6))
        assertEquals("1.23457e-05", PyNum.g(1.234567e-5)); assertEquals("0.0001", PyNum.g(1e-4))
        assertEquals(360.0, PyNum.mod(-1e-20, 360.0), 0.0)
        assertEquals(-5L, PyNum.int(" -5 ")); assertNull(PyNum.int("5a")); assertEquals(10.5, PyNum.float("1_0.5")!!, 0.0)
        assertNull(PyNum.float("1.5f"))
    }
}
