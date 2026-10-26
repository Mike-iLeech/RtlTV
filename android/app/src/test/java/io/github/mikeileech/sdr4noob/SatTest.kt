package io.github.mikeileech.sdr4noob

import io.github.mikeileech.sdr4noob.sat.AutoRxScheduler
import io.github.mikeileech.sdr4noob.sat.DopplerTracker
import io.github.mikeileech.sdr4noob.sat.Geo
import io.github.mikeileech.sdr4noob.sat.IqReceiver
import io.github.mikeileech.sdr4noob.sat.Orbit
import io.github.mikeileech.sdr4noob.sat.Pass
import io.github.mikeileech.sdr4noob.sat.RxPlan
import io.github.mikeileech.sdr4noob.sat.Sat
import io.github.mikeileech.sdr4noob.sat.SatCatalog
import io.github.mikeileech.sdr4noob.sat.Sgp4
import io.github.mikeileech.sdr4noob.sat.SpecPacer
import io.github.mikeileech.sdr4noob.sat.SpectralNR
import io.github.mikeileech.sdr4noob.sat.Tle
import io.github.mikeileech.sdr4noob.sat.TleLoader
import io.github.mikeileech.sdr4noob.sat.TleText
import io.github.mikeileech.sdr4noob.sat.Tx
import io.github.mikeileech.sdr4noob.sat.allPasses
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.io.IOException
import java.nio.file.Files
import kotlin.math.abs
import kotlin.math.max

/**
 * Спутники — перенос rtltv/satpass.py, rtltv/satcat.py и части rtltv/satrx.py. Эталоны посчитаны пакетом sgp4
 * (C++-ядро Satrec) и Python-модулями (android/app/src/test/python/gen_sat_ref.py → ресурс sat/sat_ref.tsv):
 * проверочный набор Vallado SGP4-VER (околоземные и глубокий космос, резонансы, ошибки), положения 22 спутников
 * по TLE из кэша celestrak (МКС, NOAA 20/21, METOP-B, «Метеоры», любительские, «Электро-Л»), ECEF, подспутниковая
 * точка, азимут/угол места/дальность/доплер, пролёты за сутки для трёх наблюдателей, satrx (NR, IqReceiver, SpecPacer).
 */
class SatTest {
    private val ref: List<List<String>> by lazy {
        javaClass.classLoader!!.getResourceAsStream("sat/sat_ref.tsv")!!.bufferedReader(Charsets.UTF_8)
            .readLines().filter { it.isNotEmpty() }.map { it.split('\t') }
    }
    private val fixtureText: String by lazy {
        javaClass.classLoader!!.getResourceAsStream("sat/tle_fixture.txt")!!.bufferedReader(Charsets.US_ASCII).readText()
    }
    private val tles by lazy { TleText.parse(fixtureText) }

    private fun d(s: String) = s.toDouble()

    private class MaxErr(val name: String) {
        var v = 0.0
        fun add(a: Double, b: Double) { if (a.isFinite() || b.isFinite()) v = max(v, abs(a - b)) }
        override fun toString() = "$name: %.3e".format(v)
    }

    // ================================================================ SGP4

    @Test
    fun sgp4MatchesVerificationSet() {
        val pos = MaxErr("SGP4-VER положение, км")
        val vel = MaxErr("SGP4-VER скорость, км/с")
        var sat: Sgp4? = null
        var sets = 0
        var points = 0
        var deep = 0
        for (r in ref) {
            when (r[0]) {
                "VER" -> { sat = Sgp4.twoline2rv(r[1], r[2]); sets++; if (sat.method == 'd') deep++ }
                "V" -> {
                    val s = sat!!
                    val st = s.propagateMinutes(d(r[1]))
                    val err = r[2].toInt()
                    assertEquals("ошибка ${s.satnum} t=${r[1]}", err, st.error)
                    if (err == 0) {
                        for (k in 0..2) {
                            pos.add(st.r[k], d(r[3 + k]))
                            vel.add(st.v[k], d(r[6 + k]))
                        }
                        points++
                    }
                }
            }
        }
        println("$pos; $vel ($sets наборов, из них SDP4 $deep, $points точек)")
        assertTrue(sets >= 30)
        assertTrue(deep >= 15)
        assertTrue(pos.toString(), pos.v <= 1e-6)
        assertTrue(vel.toString(), vel.v <= 1e-9)
    }

    @Test
    fun tleParseAndEpoch() {
        val parsed = ref.first { it[0] == "PARSED" }
        assertEquals(parsed[1].toInt(), tles.size)
        assertEquals(parsed.drop(2), tles.map { (n, t) -> "$n:${t.name}" })
        for (r in ref.filter { it[0] == "EPOCH" }) {
            val t = tles[r[1].toInt()]!!
            assertEquals(d(r[2]), t.epochUnix, 1e-6)
        }
        for (r in ref.filter { it[0] == "PERIOD" }) {
            assertEquals(d(r[2]), Orbit(tles[r[1].toInt()]!!).periodMin, 1e-9)
        }
        // 2LE без имени, мусор между наборами, битая строка, CRLF, повтор номера
        val iss = tles[25544]!!
        val txt = "мусор\r\n${iss.line1}\r\n${iss.line2}\r\n\r\nNAME X\n1 99999U bad\n2 99999 bad\n" +
                "Meteor\n${tles[57166]!!.line1}\n${tles[57166]!!.line2}\nДубль\n${iss.line1}\n${iss.line2}\n"
        val p = TleText.parse(txt)
        assertEquals(listOf(25544, 57166), p.keys.toList())
        assertEquals("Дубль", p[25544]!!.name)
        assertEquals("Meteor", p[57166]!!.name)
        val p2 = TleText.parse("${iss.line1}\n${iss.line2}\n")
        assertEquals("NORAD 25544", p2[25544]!!.name)
    }

    @Test
    fun positionsLookDoppler() {
        val teme = MaxErr("TEME, км"); val temev = MaxErr("TEME v, км/с")
        val ecef = MaxErr("ECEF, км"); val ecefv = MaxErr("ECEF v, км/с")
        val ll = MaxErr("широта/долгота, °"); val hh = MaxErr("высота, км")
        val ang = MaxErr("азимут/угол места, °"); val rng = MaxErr("дальность, км")
        val rr = MaxErr("скорость сближения, км/с"); val dop = MaxErr("доплер, Гц")
        val orbits = HashMap<Int, Orbit>()
        val obs = ref.first { it[0] == "PASSES" }
        val lat = d(obs[2]); val lon = d(obs[3]); val h = d(obs[4])
        var n = 0
        for (r in ref.filter { it[0] == "T" }) {
            val norad = r[1].toInt()
            val o = orbits.getOrPut(norad) { Orbit(tles[norad]!!) }
            val t = d(r[2])
            assertEquals(r[3].toInt(), 0)
            val jf = Geo.jdFromUnix(t)
            val st = o.sat.propagate(jf[0], jf[1])
            for (k in 0..2) { teme.add(st.r[k], d(r[4 + k])); temev.add(st.v[k], d(r[7 + k])) }
            val e = o.ecef(t)
            for (k in 0..2) { ecef.add(e.r[k], d(r[10 + k])); ecefv.add(e.v[k], d(r[13 + k])) }
            val sp = o.subpoint(t)
            ll.add(sp[0], d(r[16])); ll.add(sp[1], d(r[17])); hh.add(sp[2], d(r[18]))
            val lk = o.look(t, lat, lon, h)
            var daz = abs(lk.az - d(r[19])); if (daz > 180) daz = 360 - daz
            ang.v = max(ang.v, daz); ang.add(lk.el, d(r[20]))
            rng.add(lk.range, d(r[21])); rr.add(lk.rangeRate, d(r[22]))
            dop.add(o.doppler(t, d(r[23]), lat, lon, h), d(r[24]))
            n++
        }
        println("$n точек: $teme; $temev; $ecef; $ecefv; $ll; $hh; $ang; $rng; $rr; $dop")
        assertTrue(n > 1000)
        assertTrue(teme.toString(), teme.v <= 1e-6)
        assertTrue(temev.toString(), temev.v <= 1e-9)
        assertTrue(ecef.toString(), ecef.v <= 1e-6)
        assertTrue(ecefv.toString(), ecefv.v <= 1e-9)
        assertTrue(ll.toString(), ll.v <= 1e-9)
        assertTrue(hh.toString(), hh.v <= 1e-6)
        assertTrue(ang.toString(), ang.v <= 1e-7)
        assertTrue(rng.toString(), rng.v <= 1e-6)
        assertTrue(rr.toString(), rr.v <= 1e-9)
        assertTrue(dop.toString(), dop.v <= 1e-3)
    }

    @Test
    fun tracksAndGeometry() {
        for (r in ref.filter { it[0] == "TRACK" }) {
            val o = Orbit(tles[r[1].toInt()]!!)
            val tr = o.orbitTrack(d(r[2]), 1.0, 1.0, 120)
            assertEquals("трасса ${r[1]}", r[3].toInt(), tr.size)
            val pts = tr.take(3) + tr.takeLast(3)
            for ((i, p) in pts.withIndex()) {
                assertEquals(d(r[4 + 2 * i]), p[0], 1e-9)
                assertEquals(d(r[5 + 2 * i]), p[1], 1e-9)
            }
        }
        for (r in ref.filter { it[0] == "GMST" }) {
            val jf = Geo.jdFromUnix(d(r[1]))
            assertEquals(d(r[2]), jf[0], 0.0)
            assertEquals(d(r[3]), jf[1], 1e-15)
            assertEquals(d(r[4]), Geo.gmst(jf[0], jf[1]), 1e-12)
        }
        for (r in ref.filter { it[0] == "GEOD" }) {
            val x = Geo.geodeticToEcef(d(r[1]), d(r[2]), d(r[3]))
            for (k in 0..2) assertEquals(d(r[4 + k]), x[k], 1e-9)
            val b = Geo.ecefToGeodetic(d(r[4]), d(r[5]), d(r[6]))
            for (k in 0..2) assertEquals(d(r[7 + k]), b[k], 1e-9)
        }
        for (r in ref.filter { it[0] == "VIS" }) {
            val pts = Geo.visibilityCircle(d(r[1]), d(r[2]), d(r[3]), d(r[4]), r[5].toInt())
            assertEquals(r[5].toInt() + 1, pts.size)
            for ((i, p) in pts.withIndex()) {
                assertEquals(d(r[6 + 2 * i]), p[0], 1e-9)
                assertEquals(d(r[7 + 2 * i]), p[1], 1e-9)
            }
        }
        for (r in ref.filter { it[0] == "GEOLOOK" }) {
            val g = Geo.geoLook(d(r[1]), d(r[2]), d(r[3]))
            for (k in 0..2) assertEquals(d(r[4 + k]), g[k], 1e-9)
        }
    }

    // ================================================================ пролёты

    @Test
    fun passesForADay() {
        val leo = tles.values.filter { it.norad !in SatCatalog.GEO_NORADS }.map { Orbit(it) }
        val tErr = MaxErr("AOS/LOS/TCA, с"); val elErr = MaxErr("максимум, °"); val azErr = MaxErr("азимуты, °")
        var i = 0
        var total = 0
        while (i < ref.size) {
            val r = ref[i]
            if (r[0] != "PASSES") { i++; continue }
            val lat = d(r[2]); val lon = d(r[3]); val h = d(r[4]); val t0 = d(r[5]); val hours = d(r[6]); val minEl = d(r[7])
            val exp = ArrayList<List<String>>()
            i++
            while (i < ref.size && ref[i][0] == "P") { exp.add(ref[i]); i++ }
            val got = leo.flatMap { it.passes(lat, lon, t0, hours, minEl, h) }
            assertEquals("число пролётов (${r[1]})", exp.size, got.size)
            for ((e, p) in exp.zip(got)) {
                assertEquals(e[1].toInt(), p.norad)
                tErr.add(p.aos, d(e[2])); tErr.add(p.los, d(e[3])); tErr.add(p.tca, d(e[4]))
                elErr.add(p.maxEl, d(e[5]))
                for ((a, b) in listOf(p.azAos to d(e[6]), p.azLos to d(e[7]), p.azTca to d(e[8]))) {
                    var da = abs(a - b); if (da > 180) da = 360 - da
                    azErr.v = max(azErr.v, da)
                }
                assertTrue(p.maxEl >= minEl)
                assertTrue(p.los > p.aos && p.tca >= p.aos && p.tca <= p.los)
            }
            total += exp.size
        }
        println("$total пролётов: $tErr; $elErr; $azErr")
        assertTrue(total > 100)
        assertTrue(tErr.toString(), tErr.v <= 1.0)
        assertTrue(elErr.toString(), elErr.v <= 1e-6)
        assertTrue(azErr.toString(), azErr.v <= 1e-6)

        val a = ref.first { it[0] == "ALLPASS" }
        val all = allPasses(leo, d(a[1]), d(a[2]), d(a[3]), d(a[4]), 0.0)
        assertEquals(a[5].toInt(), all.size)
        for ((s, p) in a.drop(6).zip(all)) {
            val (n, aos) = s.split(':')
            assertEquals(n.toInt(), p.norad)
            assertEquals(d(aos), p.aos, 1.0)
        }
    }

    // ================================================================ каталог и загрузка TLE

    @Test
    fun catalog() {
        assertEquals(15, SatCatalog.BUILTIN.size)
        assertEquals(4, SatCatalog.GEO.size)
        assertEquals(setOf(67756, 44903, 55506, 41105), SatCatalog.GEO_NORADS)
        val iss = SatCatalog.BY_NORAD[25544]!!
        assertEquals("МКС", iss.name)
        assertEquals(145.8e6, iss.txs[0].freq, 0.0)
        assertEquals("voice", iss.txs[0].kind)
        val rs44 = SatCatalog.BY_NORAD[44909]!!.txs[0]
        assertTrue(rs44.isBand)
        assertEquals((435.610e6 + 435.670e6) / 2, rs44.center, 1e-6)
        assertEquals("lrpt", SatCatalog.BY_NORAD[57166]!!.txs[0].kind)
        assertEquals("lrit", SatCatalog.BY_NORAD[67756]!!.txs[0].kind)
        assertFalse(SatCatalog.BY_NORAD[67756]!!.txs[0].listenable)
        assertEquals(listOf(57166, 59051), SatCatalog.norads(listOf("meteo")))
        assertEquals(19, SatCatalog.allSats().size)
        // свои спутники: словарь из настроек, встроенный номер не заменяется
        val u = mapOf("norad" to "99999", "name" to "", "txs" to listOf(mapOf("freq" to 437.5e6, "hi" to null, "mode" to "USB")),
            "tle" to listOf("a", "b"))
        val dup = Sat(25544, "Не МКС", "other", "#fff", user = true)
        SatCatalog.setUser(listOf(u, dup))
        try {
            val s = SatCatalog.BY_NORAD[99999]!!
            assertEquals("NORAD 99999", s.name)
            assertEquals("other", s.cat)
            assertEquals("добавлен вручную", s.status)
            assertTrue(s.user)
            assertEquals(listOf("a", "b"), s.tle)
            assertEquals(Tx("", 437.5e6, "USB"), s.txs[0])
            assertEquals("МКС", SatCatalog.BY_NORAD[25544]!!.name)
            assertEquals(16, SatCatalog.CATALOG.size)
            assertEquals(s, Sat.fromMap(s.toMap()))
        } finally {
            SatCatalog.setUser(emptyList())
        }
        assertNull(SatCatalog.BY_NORAD[99999])
    }

    @Test
    fun tleLoaderCacheAndOffline() {
        val dir = Files.createTempDirectory("tle").toFile()
        try {
            var now = 2_000_000_000.0
            val asked = ArrayList<String>()
            var online = true
            val iss = tles[25544]!!
            val met = tles[57166]!!
            val fetch = { url: String, _: Double ->
                asked.add(url)
                if (!online) throw IOException("нет сети")
                when {
                    "GROUP=stations" in url -> "ISS (ZARYA)\n${iss.line1}\n${iss.line2}\n"
                    "CATNR=57166" in url -> "METEOR-M2 3\n${met.line1}\n${met.line2}\n"
                    else -> "<html>нет</html>"
                }
            }
            val ld = TleLoader(dir, fetch) { now }
            val r1 = ld.load(groups = listOf("stations", "weather"))
            assertEquals("stations: обновлено; weather: нет данных", r1.status)
            assertEquals(setOf(25544, 57166), r1.tles.keys)   // 59051 не найден ни в группах, ни по номеру
            assertTrue(asked.any { "CATNR=59051" in it })
            assertTrue(File(dir, "stations.txt").isFile && File(dir, "catnr_57166.txt").isFile)
            File(dir, "stations.txt").setLastModified(((now - 3 * 3600) * 1000).toLong())
            asked.clear()
            val r2 = ld.load(groups = listOf("stations"))
            assertEquals("stations: кэш 3 ч", r2.status)
            assertFalse(asked.any { "GROUP=stations" in it })   // свежий кэш — без загрузки
            now += 2 * 86400
            online = false
            val r3 = ld.load(groups = listOf("stations"))
            assertEquals("stations: кэш 51 ч", r3.status)        // сети нет — кэш любой давности
            assertEquals(iss, r3.tles[25544])
            assertNotNull(ld.cacheTime(listOf("stations")))
            assertEquals("https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=tle", TleText.groupUrl("amateur"))
        } finally {
            dir.deleteRecursively()
        }
    }

    // ================================================================ приём

    @Test
    fun rxPlanDopplerAndAuto() {
        val met = SatCatalog.BY_NORAD[57166]!!
        val p = RxPlan.of(met, met.txs[0], 2_048_000)
        assertEquals("lrpt", p.kind); assertEquals(1_024_000, p.fs); assertEquals(137.9e6 - 200e3, p.center, 0.0)
        assertTrue(p.doppler); assertFalse(p.recordAlways); assertEquals(60e3, p.halfBand, 0.0)
        val el = SatCatalog.BY_NORAD[67756]!!
        val pe = RxPlan.of(el, el.txs[0], 1_024_000)
        assertEquals(2_048_000, pe.fs); assertEquals(1691e6 - 400e3, pe.center, 0.0); assertFalse(pe.doppler)
        val rs = SatCatalog.BY_NORAD[44909]!!
        val pr = RxPlan.of(rs, rs.txs[0], 1_200_000)
        assertEquals(60e3 + 10e3, pr.iqBw, 1e-6); assertEquals(60e3, pr.halfBand, 1e-6); assertTrue(pr.recordAlways)
        assertEquals(72000 * 16, RxPlan.of(met, met.txs[0], 1_024_000, test = true).fs)

        // доплер — тот же, что Orbit.doppler, пересчёт не чаще 0,5 с
        val o = Orbit(tles[57166]!!)
        val t = ref.first { it[0] == "T" && it[1] == "57166" }
        val obs = ref.first { it[0] == "PASSES" }
        val tr = DopplerTracker(p.center, p.freq, o, d(obs[2]), d(obs[3]), d(obs[4]))
        val t0 = d(t[2])
        val off = tr.update(t0)!!
        assertEquals(p.freq - p.center + o.doppler(t0, p.freq, d(obs[2]), d(obs[3]), d(obs[4])), off, 1e-9)
        assertNull(tr.update(t0 + 0.4))
        assertNotNull(tr.update(t0 + 0.6))
        tr.enabled = false
        assertEquals(200e3, tr.update(t0 + 2)!!, 0.0)

        // автоприём «Метеоров»
        val a = AutoRxScheduler()
        val pm = Pass(57166, "M", 1000.0, 1700.0, 1350.0, 40.0, 0.0, 180.0, 90.0)
        val low = Pass(59051, "M4", 900.0, 1500.0, 1200.0, 5.0, 0.0, 180.0, 90.0)
        val iss = Pass(25544, "ISS", 900.0, 1500.0, 1200.0, 80.0, 0.0, 180.0, 90.0)
        val ps = listOf(low, iss, pm)
        assertEquals(AutoRxScheduler.Action.None, a.check(960.0, true, ps, 10.0, false))
        assertEquals(AutoRxScheduler.Action.None, a.check(980.0, false, ps, 10.0, false))
        assertEquals(AutoRxScheduler.Action.None, a.check(980.0, true, ps, 10.0, true))
        assertEquals(AutoRxScheduler.Action.Start(pm), a.check(980.0, true, ps, 10.0, false))
        assertEquals(AutoRxScheduler.Action.None, a.check(1720.0, true, ps, 10.0, false))
        assertEquals(AutoRxScheduler.Action.Stop(pm), a.check(1721.0, true, ps, 10.0, false))
        assertNull(a.current)
        assertEquals(AutoRxScheduler.Action.None, a.check(1640.0 + 1.0, true, listOf(pm), 10.0, false))
        assertEquals(AutoRxScheduler.Action.Start(pm), a.check(1640.0, true, listOf(pm), 10.0, false))
        assertTrue(a.disable())
        assertFalse(a.disable())
    }

    private class Lcg(var x: Long) {
        fun next(): Double {
            x = x * 6364136223846793005L + 1442695040888963407L
            return (((x ushr 33) % 2001) - 1000) / 2048.0
        }
    }

    private val tone = doubleArrayOf(0.0, 0.375, 0.6875, 0.875, 0.9375, 0.875, 0.6875, 0.375, 0.0, -0.375, -0.6875,
        -0.875, -0.9375, -0.875, -0.6875, -0.375)

    private fun lcgIq(seed: Long, n: Int): Pair<FloatArray, FloatArray> {
        val g = Lcg(seed)
        val re = FloatArray(n); val im = FloatArray(n)
        for (i in 0 until n) {
            re[i] = (g.next() * 0.5 + 0.25 * tone[i % 16]).toFloat()
            im[i] = (g.next() * 0.5 + 0.25 * tone[(i + 4) % 16]).toFloat()
        }
        return re to im
    }

    @Test
    fun spectralNr() {
        val g = Lcg(1)
        val x = FloatArray(16000) { i -> (0.05 * g.next() + if ((i / 4000) % 2 == 0) 0.25 * tone[i % 16] else 0.0).toFloat() }
        val nr = SpectralNR()
        var p = 0
        val err = MaxErr("NR")
        for (r in ref.filter { it[0] == "NR" }) {
            val c = r[1].toInt()
            val y = nr.process(x.copyOfRange(p, p + c))
            p += c
            assertEquals("длина выхода NR", r[2].toInt(), y.size)
            for (i in y.indices) err.add(y[i].toDouble(), r[3 + i].toFloat().toDouble())
        }
        println(err)
        assertTrue(err.toString(), err.v <= 1e-5)
    }

    @Test
    fun iqReceiver() {
        val h = ref.first { it[0] == "IQH" }
        val rx = IqReceiver(d(h[1]), d(h[2]))
        assertEquals(h[3].toInt(), rx.dec)
        val hk = IqReceiver.firwin(129, d(h[2]) / 2, d(h[1]))
        for (k in hk.indices) assertEquals(d(h[4 + k]), hk[k], 1e-7)
        val lv = MaxErr("уровень/шум, dB"); val cs = MaxErr("созвездие")
        for (r in ref.filter { it[0] == "IQ" }) {
            val (re, im) = lcgIq(r[1].toLong(), r[2].toInt())
            rx.setOffset(d(r[3]))
            rx.feed(re, im, re.size)
            lv.add(rx.levelDb, d(r[4])); lv.add(rx.noiseDb, d(r[5]))
            assertEquals(r[6].toInt() * 2, rx.lastCons.size)
            for (k in 0 until 10) cs.add(rx.lastCons[k].toDouble(), d(r[7 + k]))
        }
        println("$lv; $cs")
        assertTrue(lv.toString(), lv.v <= 1e-3)
        assertTrue(cs.toString(), cs.v <= 1e-4)
    }

    @Test
    fun specPacer() {
        val sp = SpecPacer()
        val fs = 240000.0
        for ((seed, n, now) in listOf(Triple(5L, 24000, 1000.0), Triple(6L, 12000, 1000.02), Triple(7L, 36000, 1000.5))) {
            val (re, im) = lcgIq(seed, n)
            sp.push(re, im, n, 137.5e6, fs, 1024, now)
        }
        val spn = ref.first { it[0] == "SPN" }
        assertEquals(spn[1].toInt(), sp.count)
        val frames = ref.filter { it[0] == "SP" }
        val err = MaxErr("спектр, dB")
        // кадры в очереди: время выдачи, номер, частоты, PSD (первые три кадра)
        val q = sp.pending()
        assertEquals(frames.size, q.size)
        var got = 0
        for ((f, item) in frames.zip(q)) {
            val (t, fr) = item
            assertEquals(d(f[1]), t, 1e-9)
            assertEquals(f[2].toInt(), fr.n)
            assertEquals(d(f[3]), fr.freqs[0], 1e-6)
            assertEquals(d(f[4]), fr.freqs[1], 1e-6)
            assertEquals(d(f[5]), fr.freqs[1023], 1e-6)
            if (f.size > 6) for (i in 0 until 1024) err.add(fr.psd[i].toDouble(), f[6 + i].toFloat().toDouble())
            got++
        }
        assertEquals(15, got)
        println(err)
        assertTrue(err.toString(), err.v <= 0.01)
        for (r in ref.filter { it[0] == "SPTAKE" }) {
            assertEquals(r[2].toInt(), sp.take(d(r[1]))?.n ?: 0)
        }
    }

    @Test
    fun tleEpochFieldsMatchTle() {
        val t: Tle = tles[25544]!!
        val s = Sgp4.twoline2rv(t.line1, t.line2)
        assertEquals("25544", s.satnum)
        assertEquals(2461338.5, s.jdSatEpoch, 0.0)
        assertEquals(0.62845297, s.jdSatEpochF, 0.0)
        assertEquals('n', s.method)
        assertEquals('d', Sgp4.twoline2rv(tles[67756]!!.line1, tles[67756]!!.line2).method)
    }
}
