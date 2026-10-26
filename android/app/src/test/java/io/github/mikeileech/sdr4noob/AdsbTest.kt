package io.github.mikeileech.sdr4noob

import io.github.mikeileech.sdr4noob.dsp.Adsb
import io.github.mikeileech.sdr4noob.dsp.AdsbDemo
import io.github.mikeileech.sdr4noob.dsp.AdsbDemodulator
import io.github.mikeileech.sdr4noob.dsp.AdsbFrame
import io.github.mikeileech.sdr4noob.dsp.AdsbProcessor
import io.github.mikeileech.sdr4noob.dsp.AdsbTracker
import io.github.mikeileech.sdr4noob.dsp.HistEntry
import io.github.mikeileech.sdr4noob.radio.AircraftInfo
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.sin

/**
 * ADS-B / Mode S — перенос rtltv/adsb.py и офлайн-части rtltv/aircraft_info.py. Эталоны посчитаны Python-модулем
 * (CRC, разбор, CPR, кодеры, расшифровка, демодулятор на одном и том же детерминированном IQ); проверки
 * tests/test_adsb.py перенесены (случайные числа — java.util.Random, поэтому там сверка по смыслу, не побитно).
 */
class AdsbTest {
    private fun dec(h: String) = Adsb.decodeHex(h)
    private fun near(exp: Double, got: Double?, tol: Double) {
        assertNotNull(got); assertTrue("ожидалось $exp, получено $got", abs(exp - got!!) <= tol)
    }
    private fun d(p: Pair<Double, Double>, q: Pair<Double, Double>) = Adsb.distanceKm(p.first, p.second, q.first, q.second)

    // ---------------------------------------------------------------- эталоны (как в Python)
    @Test fun crcAndSyndromeMatchPython() {
        val ref = listOf(
            Triple("8D4840D6202CC371C32CE0576098", 0x576098, 0), Triple("8D40621D58C382D690C8AC2863A7", 0x2863A7, 0),
            Triple("8D40621D58C386435CC412692AD6", 0x692AD6, 0), Triple("8D485020994409940838175B284F", 0x5B284F, 0),
            Triple("8DA05F219B06B6AF189400CBC33F", 0xCBC33F, 0), Triple("8DA05629EA21485CBF3F8CADAEEB", 0xADAEEB, 0),
            Triple("A0001838CA3E51F0A8000047A36A", 0xA8C227, 0xEF614D), Triple("5D4840D6A0F7E0", 0xF8740F, 0x5883EF),
            Triple("02E197B00179C3", 0x4A613D, 0x4B18FE), Triple("28000A3D6AC5C9", 0xB292F5, 0xD8573C))
        for ((h, crc, syn) in ref) {
            val b = Adsb.fromHex(h)
            assertEquals(h, crc, Adsb.crc24(b, b.size - 3))
            assertEquals(h, syn, Adsb.syndrome(b))
        }
        assertEquals(listOf(3749354, 10367465, 199374, 99687, 16774153, 1),
            listOf(0, 5, 55, 56, 87, 111).map { Adsb.CONTRIB[it] })
    }

    @Test fun crcAndIdentification() {
        val (m, syn) = dec("8D4840D6202CC371C32CE0576098")
        assertEquals(0, syn)
        assertEquals(17, m.df); assertEquals(0x4840D6, m.icao)
        assertEquals(4, m.tc); assertEquals("KLM1023", m.callsign); assertNull(m.category)
        assertEquals(false, m.onGround)
        assertEquals("8D4840D6202CC371C32CE0576098", m.raw)
    }

    @Test fun crcDetectsAndLocatesSingleError() {
        val b = Adsb.fromHex("8D4840D6202CC371C32CE0576098")
        b[6] = (b[6].toInt() xor 0x10).toByte()
        val s = Adsb.syndrome(b)
        assertNotEquals(0, s)
        assertEquals(6 * 8 + 3, Adsb.FIX1[s])   // одиночная ошибка исправима: номер бита
    }

    @Test fun airbornePositionGlobalAndLocal() {
        val (e, se) = dec("8D40621D58C382D690C8AC2863A7")
        val (o, so) = dec("8D40621D58C386435CC412692AD6")
        assertEquals(0, se); assertEquals(0, so)
        assertEquals(38000, e.alt); assertEquals(38000, o.alt)
        val ec = e.cpr!!; val oc = o.cpr!!
        assertEquals(listOf(0, 93000, 51372), listOf(ec.odd, ec.lat, ec.lon)); assertFalse(ec.surface)
        assertEquals(listOf(1, 74158, 50194), listOf(oc.odd, oc.lat, oc.lon))
        val g0 = Adsb.cprGlobal(ec.lat, ec.lon, oc.lat, oc.lon, newestOdd = false)!!
        assertEquals(52.2572021484375, g0.first, 1e-12); assertEquals(3.91937255859375, g0.second, 1e-12)
        val g1 = Adsb.cprGlobal(ec.lat, ec.lon, oc.lat, oc.lon, newestOdd = true)!!
        assertEquals(52.26578017412606, g1.first, 1e-12); assertEquals(3.938912527901786, g1.second, 1e-12)
        val l0 = Adsb.cprLocal(ec.lat, ec.lon, 0, 52.258, 3.918)
        assertEquals(52.2572021484375, l0.first, 1e-12); assertEquals(3.91937255859375, l0.second, 1e-12)
        val l1 = Adsb.cprLocal(oc.lat, oc.lon, 1, 52.258, 3.918)
        assertEquals(52.26578017412606, l1.first, 1e-12); assertEquals(3.9389125279017776, l1.second, 1e-12)
    }

    @Test fun cprNlEncodeAndGeodesyMatchPython() {
        assertEquals(listOf(59, 58, 42, 36, 29, 2, 2, 1),
            listOf(0.0, 10.47047130, 45.0, 52.2572, 59.95459277, 86.9, 87.0, 88.0).map { Adsb.cprNl(it) })
        assertEquals(38356 to 58753, Adsb.cprEncode(55.7558, 37.6173, false))
        assertEquals(18056 to 45057, Adsb.cprEncode(55.7558, 37.6173, true))
        assertEquals(90852 to 39711, Adsb.cprEncode(55.9726, 37.4146, true, surface = true))
        assertEquals(45875 to 76022, Adsb.cprEncode(-33.9, 151.2, false))
        val p = Adsb.destination(55.7558, 37.6173, 40.0, 180.0)
        assertEquals(56.981451414607555, p.first, 1e-10); assertEquals(39.526944264684744, p.second, 1e-10)
        assertEquals(2213.312995180802, Adsb.distanceKm(55.7558, 37.6173, 52.2572, 3.9194), 1e-8)
        assertEquals(274.04192062375336, Adsb.bearingDeg(55.7558, 37.6173, 52.2572, 3.9194), 1e-9)
    }

    @Test fun velocityGroundSpeed() {
        val (m, syn) = dec("8D485020994409940838175B284F")
        assertEquals(0, syn); assertEquals(19, m.tc)
        assertEquals(159.20113064925135, m.gs!!, 1e-12)
        assertEquals(182.8803775528476, m.track!!, 1e-12)
        assertEquals(-832, m.vr); assertEquals(550, m.gnssDiff)
    }

    @Test fun velocityAirspeed() {
        val (m, syn) = dec("8DA05F219B06B6AF189400CBC33F")
        assertEquals(0, syn)
        assertEquals(243.984375, m.heading!!, 1e-12)
        assertEquals(375, m.tas); assertNull(m.ias); assertEquals(-2304, m.vr)
    }

    @Test fun targetStateTc29() {
        val (m, syn) = dec("8DA05629EA21485CBF3F8CADAEEB")
        assertEquals(0, syn); assertEquals(29, m.tc)
        assertEquals(16992, m.selAlt); assertEquals(1012.8, m.qnh!!, 1e-9); assertEquals(66.796875, m.selHdg!!, 1e-12)
    }

    @Test fun shortFramesAndDf20() {
        val (m20, syn20) = dec("A0001838CA3E51F0A8000047A36A")
        assertEquals(20, m20.df); assertEquals(38000, m20.alt); assertEquals(syn20, m20.icao)  // адрес = синдром
        assertEquals(0xEF614D, m20.icao); assertEquals(false, m20.onGround)
        val m11 = dec("5D4840D6A0F7E0").first
        assertEquals(11, m11.df); assertEquals(0x4840D6, m11.icao); assertEquals(false, m11.onGround)
        val m0 = dec("02E197B00179C3").first
        assertEquals(0, m0.df); assertEquals(0x4B18FE, m0.icao); assertEquals(37000, m0.alt); assertNull(m0.onGround)
        val m5 = dec("28000A3D6AC5C9").first
        assertEquals(5, m5.df); assertEquals(0xD8573C, m5.icao); assertEquals("3307", m5.squawk)
        assertEquals(false, m5.onGround)
    }

    @Test fun noPositionTc0Altitude() {
        // TC=0: борт без координат (подавление GNSS), но с барометрической высотой — так в живом эфире
        val fr = Adsb.makeDf17(0x151E04, Adsb.encodeAc12(35000.0).toLong() shl 36)
        val m = Adsb.decodeFrame(fr)
        assertEquals(0, m.tc); assertEquals(35000, m.alt); assertTrue(m.noPos)
        val t = AdsbTracker(home = 55.75 to 37.62)
        val a = t.apply(m, now = 1.0)!!
        assertTrue(a.noPos); assertNull(a.lat); assertEquals(35000, a.alt)
    }

    @Test fun gillham() {
        // все 1280 допустимых кодов Гиллхэма дают каждую высоту от −1200 до 126 700 футов с шагом 100 ровно один раз
        val alts = (0 until 0x8000).filter { it and 0x8888 == 0 }.mapNotNull { Adsb.gillhamToFt(it) }
        assertEquals(1280, alts.size)
        assertEquals((-1200 until 126800 step 100).toList(), alts.sorted())
        // через 13-битное поле AC (DF4/20, Q=0, M=0) и 12-битное (позиция ADS-B) — те же высоты
        for (h in 0 until 0x8000 step 7) {
            if (h and 0x8888 != 0 || Adsb.gillhamToFt(h) == null) continue
            val ac13 = Adsb.hexToId13(h)
            assertEquals(Adsb.gillhamToFt(h), Adsb.decodeAc13(ac13))
            val ac12 = ((ac13 shr 1) and 0xFC0) or (ac13 and 0x3F)
            assertEquals(Adsb.gillhamToFt(h), Adsb.decodeAc12(ac12))
        }
    }

    @Test fun encodersMatchPython() {
        assertEquals(3730, Adsb.encodeSquawk("7421"))
        assertEquals("7700", Adsb.decodeSquawk(Adsb.encodeSquawk("7700")))
        assertEquals(5533, Adsb.encodeAc13(33725.0)); assertEquals(2896, Adsb.encodeAc12(35000.0))
        assertEquals(13356, Adsb.decodeAc13(0x1FB7 and 0x10.inv() or 0x40))   // M-бит: метры → футы
        assertEquals(9856448268283168L, Adsb.meIdent("AFL1124"))
        assertEquals(43066954111013888L, Adsb.meVelocity(420.0, 135.0, -1280.0))
        assertEquals(24957833985437697L, Adsb.meAirbornePos(55.7558, 37.6173, 33000.0, true))
        assertEquals(16354165039602463L, Adsb.meSurfacePos(55.9726, 37.4146, true, 12.0, 90.0))
        assertEquals("8D155ABC23046331C72D2084444A", Adsb.hex(Adsb.makeDf17(0x155ABC, Adsb.meIdent("AFL1124"))))
        assertEquals("5D155ABC756EA7", Adsb.hex(Adsb.makeDf11(0x155ABC)))
        assertEquals("2000159DBB8528", Adsb.hex(Adsb.makeDf4(0x155ABC, 33725.0)))
        assertEquals("28000E92669971", Adsb.hex(Adsb.makeDf5(0x155ABC, "7421")))
        assertEquals("8D155ABC99012AA5585400E49563",
            Adsb.hex(Adsb.makeDf17(0x155ABC, Adsb.meVelocity(420.0, 135.0, -1280.0))))
        assertEquals("8C1556E23A1A06C5C89B1F9F51C4",
            Adsb.hex(Adsb.makeDf17(0x1556E2, Adsb.meSurfacePos(55.9726, 37.4146, true, 12.0, 90.0), ca = 4)))
    }

    @Test fun roundtripEncoders() {
        val icao = 0x155ABC
        var fr = Adsb.makeDf17(icao, Adsb.meIdent("AFL1124", 4, 3))
        var m = Adsb.decodeFrame(fr)
        assertEquals(0, Adsb.syndrome(fr)); assertEquals("AFL1124", m.callsign); assertTrue(m.category!!.startsWith("A3"))
        fr = Adsb.makeDf17(icao, Adsb.meVelocity(420.0, 135.0, -1280.0))
        m = Adsb.decodeFrame(fr)
        near(420.0, m.gs, 1.0); near(135.0, m.track, 0.3); assertEquals(-1280, m.vr)
        fr = Adsb.makeDf4(icao, 33725.0)
        assertEquals(icao, Adsb.syndrome(fr)); assertEquals(33725, Adsb.decodeFrame(fr).alt)
        fr = Adsb.makeDf5(icao, "7421")
        assertEquals(icao, Adsb.syndrome(fr)); assertEquals("7421", Adsb.decodeFrame(fr).squawk)
        fr = Adsb.makeDf11(icao)
        assertEquals(0, Adsb.syndrome(fr)); assertEquals(icao, Adsb.decodeFrame(fr).icao)
    }

    @Test fun cprRoundtripMany() {
        val rng = java.util.Random(1)
        var ok = 0
        repeat(300) {
            val lat = -80 + 160 * rng.nextDouble(); val lon = -179 + 358 * rng.nextDouble()
            val e = Adsb.cprEncode(lat, lon, false)
            val o = Adsb.cprEncode(lat, lon, true)
            // на границе широтных зон пара неоднозначна — так и должно быть
            val r = Adsb.cprGlobal(e.first, e.second, o.first, o.second, newestOdd = true) ?: return@repeat
            assertTrue(d(lat to lon, r) < 0.02)
            val r2 = Adsb.cprLocal(e.first, e.second, 0, lat + 0.5, lon - 0.5)
            assertTrue(d(lat to lon, r2) < 0.02)
            ok++
        }
        assertTrue(ok > 250)
    }

    @Test fun surfacePositionLocal() {
        val lat = 55.9726; val lon = 37.4146  // Шереметьево
        val fr = Adsb.makeDf17(0x1556E2, Adsb.meSurfacePos(lat, lon, true, 12.0, 90.0), ca = 4)
        val m = Adsb.decodeFrame(fr)
        assertEquals(true, m.onGround); assertEquals(7, m.tc); assertEquals(90.0, m.track!!, 0.0); assertEquals(12.0, m.gs!!, 0.0)
        val c = m.cpr!!
        assertEquals(listOf(1, 90852, 39711), listOf(c.odd, c.lat, c.lon)); assertTrue(c.surface)
        val r = Adsb.cprLocal(c.lat, c.lon, c.odd, 55.7558, 37.6173, surface = true)
        assertEquals(55.97259521484375, r.first, 1e-12); assertEquals(37.41460561752319, r.second, 1e-12)
        assertTrue(d(lat to lon, r) < 0.01)
    }

    @Test fun envelopeMatchesPython() {
        val fr = Adsb.fromHex("8D4840D6202CC371C32CE0576098")
        val e2 = Adsb.renderEnvelope(fr, 2_000_000, 0.37)
        assertEquals(242, e2.size); assertEquals(116.0, e2.sum().toDouble(), 1e-4)
        val p2 = floatArrayOf(0.7f, 0.3f, 0.7f, 0.3f, 0f, 0f, 0f, 0.7f, 0.3f, 0.7f, 0.3f, 0f)
        for (i in p2.indices) assertEquals(p2[i], e2[i], 0f)
        val e24 = Adsb.renderEnvelope(fr, 2_400_000, 0.55)
        assertEquals(290, e24.size); assertEquals(139.2, e24.sum().toDouble(), 1e-4)
        val p24 = floatArrayOf(0.5f, 0.7f, 0.1f, 1f, 0.1f, 0f, 0f, 0f, 0.1f, 1f, 0.1f, 0.7f)
        for (i in p24.indices) assertEquals(p24[i], e24[i], 0f)
    }

    @Test fun describeMatchesPython() {
        val ref = listOf(
            "8D4840D6202CC371C32CE0576098" to ("позывной KLM1023" to "позывной KLM1023"),
            "8D40621D58C382D690C8AC2863A7" to ("высота 11 582 м, CPR чётный (ждём пару)" to
                "высота 38 000 фут, CPR чётный (ждём пару)"),
            "8D40621D58C386435CC412692AD6" to ("высота 11 582 м, CPR нечётный (ждём пару)" to
                "высота 38 000 фут, CPR нечётный (ждём пару)"),
            "8D485020994409940838175B284F" to ("скорость 295 км/ч, путевой угол 183°, вертикальная -4.2 м/с" to
                "скорость 295 км/ч, путевой угол 183°, вертикальная -832 фут/мин"),
            "8DA05F219B06B6AF189400CBC33F" to ("скорость 694 км/ч TAS, курс 244°, вертикальная -11.7 м/с" to
                "скорость 694 км/ч TAS, курс 244°, вертикальная -2304 фут/мин"),
            "8DA05629EA21485CBF3F8CADAEEB" to ("автопилот: высота 5 179 м, курс 67°, QNH 1012.8 гПа" to
                "автопилот: высота 16 992 фут, курс 67°, QNH 1012.8 гПа"),
            "A0001838CA3E51F0A8000047A36A" to ("высота 11 582 м" to "высота 38 000 фут"),
            "5D4840D6A0F7E0" to ("ответ на общий запрос (DF11)" to "ответ на общий запрос (DF11)"),
            "02E197B00179C3" to ("высота 11 278 м" to "высота 37 000 фут"),
            "28000A3D6AC5C9" to ("сквок 3307" to "сквок 3307"))
        for ((h, r) in ref) {
            val m = dec(h).first
            assertEquals(h, r.first, Adsb.describe(m))
            assertEquals(h, r.second, Adsb.describe(m, metric = false))
        }
        var m = dec("8D4840D6202CC371C32CE0576098").first
        assertEquals("DF17/TC4", Adsb.msgType(m))
        m = Adsb.decodeFrame(Adsb.makeDf17(0x151E04, Adsb.encodeAc12(35000.0).toLong() shl 36))
        assertEquals("высота 10 668 м, нет координат (TC0)", Adsb.describe(m))
        assertEquals("высота 35 000 фут, нет координат (TC0)", Adsb.describe(m, metric = false))
        assertEquals("сквок 0730", Adsb.describe(Adsb.decodeFrame(Adsb.makeDf5(1, "0730"), 1)))
        assertEquals("сквок 7700 (бедствие)", Adsb.describe(Adsb.decodeFrame(Adsb.makeDf5(1, "7700"), 1)))
        m = dec("8D40621D58C382D690C8AC2863A7").first
        assertEquals("высота 11 582 м, позиция 52.2572, 3.9194", Adsb.describe(m, pos = 52.2572 to 3.9194))
        assertEquals("ответ на общий запрос (DF11)", Adsb.describe(Adsb.decodeFrame(Adsb.makeDf11(5))))
        assertEquals("DF4", Adsb.msgType(Adsb.decodeFrame(Adsb.makeDf4(1, 1000.0), 1)))
        // форматирование как в Python: округление к чётному, отрицательный ноль, разряды
        assertEquals("-0.0", Adsb.pyF(-0.01, 1, plus = true)); assertEquals("+0", Adsb.pyF(0.0, 0, plus = true))
        assertEquals("2", Adsb.pyF(2.5, 0)); assertEquals("1 234 568", Adsb.pyF(1234567.5, 0, group = true))
    }

    // ---------------------------------------------------------------- таблица бортов
    @Test fun trackerPositionAndStale() {
        val t = AdsbTracker(home = 52.0 to 4.0)
        listOf("8D40621D58C382D690C8AC2863A7", "8D40621D58C386435CC412692AD6").forEachIndexed { i, h ->
            t.apply(Adsb.decodeFrame(Adsb.fromHex(h)), now = 100.0 + i)
        }
        val a = t.planes.getValue(0x40621D)
        assertEquals(52.26578017412606, a.lat!!, 1e-12); assertEquals(3.938912527901786, a.lon!!, 1e-12)
        assertEquals(29.84608229203597, a.dist!!, 1e-9)
        assertEquals("pair", a.posSrc); assertTrue(a.confirmed); assertEquals(38000, a.alt)
        // DF4 от неизвестного борта борт не создаёт
        assertNull(t.apply(Adsb.decodeFrame(Adsb.makeDf4(0x123456, 1000.0)), now = 101.0))
        t.expire(now = 101.0 + Adsb.STALE_S + 1)
        assertTrue(t.planes.isEmpty())
    }

    @Test fun trackerKeepAndHistory() {
        // борт держится keepS (null — весь сеанс), а история сеанса не теряет никого
        val hist = HashMap<Int, HistEntry>()
        val t = AdsbTracker(home = 55.75 to 37.62, keepS = 15 * 60.0, history = hist)
        t.apply(Adsb.decodeFrame(Adsb.makeDf17(0x151E04, Adsb.encodeAc12(35000.0).toLong() shl 36)), now = 0.0)
        t.apply(Adsb.decodeFrame(Adsb.makeDf17(0x151E04, Adsb.meIdent("SDM6569"))), now = 1.0)
        t.apply(Adsb.decodeFrame(Adsb.makeDf17(0x151E04, Adsb.encodeAc12(34000.0).toLong() shl 36)), now = 2.0)
        t.expire(now = 300.0)                       // 5 мин тишины: давно не слышен, но в таблице
        assertTrue(0x151E04 in t.planes)
        assertFalse(0x151E04 in t.known(now = 300.0))   // для «адрес/чётность» уже не используется
        t.expire(now = 2.0 + 15 * 60 + 1)
        assertTrue(t.planes.isEmpty())
        val h = hist.getValue(0x151E04)
        assertEquals("SDM6569", h.callsign); assertEquals(3, h.msgs); assertEquals(34000, h.altMin); assertEquals(35000, h.altMax)
        assertTrue(h.noPos); assertFalse(h.hadPos)
        val t2 = AdsbTracker(keepS = null)
        t2.apply(Adsb.decodeFrame(Adsb.makeDf11(0x123456)), now = 0.0)
        t2.expire(now = 1e6)
        assertTrue(0x123456 in t2.planes)
        // борт из прошлого сеанса услышан снова — запись продолжается и больше не «прошлая»
        val old = HistEntry(0x4840D6, "4840D6", 0.0).also { it.prev = true; it.msgs = 1 }
        val t3 = AdsbTracker(history = hashMapOf(0x4840D6 to old))
        t3.apply(Adsb.decodeFrame(Adsb.makeDf11(0x4840D6)), now = 2000.0)
        assertFalse(old.prev); assertEquals(2, old.msgs)
    }

    @Test fun singleCprFromHome() {
        // одиночное воздушное CPR (только чётные) декодируется относительно приёмника, пара его подтверждает
        val home = 55.7558 to 37.6173
        val p = Adsb.destination(home.first, home.second, 40.0, 180.0)
        val t = AdsbTracker(home = home)
        val m = Adsb.decodeFrame(Adsb.makeDf17(0x151111, Adsb.meAirbornePos(p.first, p.second, 33000.0, false)))
        val a = t.apply(m, now = 1.0)!!
        assertEquals("single", a.posSrc); assertFalse(a.confirmed)
        assertTrue(d(p, a.lat!! to a.lon!!) < 0.05)
        assertTrue(m.posNote!!.contains("одиночному"))
        t.apply(Adsb.decodeFrame(Adsb.makeDf17(0x151111, Adsb.meAirbornePos(p.first, p.second, 33000.0, true))), now = 1.5)
        assertEquals("pair", a.posSrc); assertTrue(a.confirmed)
        // дальше 300 км одиночное сообщение от приёмника не декодируем (неоднозначно)
        val far = Adsb.destination(home.first, home.second, 40.0, 450.0)
        val t2 = AdsbTracker(home = home)
        val b = t2.apply(Adsb.decodeFrame(Adsb.makeDf17(0x152222, Adsb.meAirbornePos(far.first, far.second, 33000.0, false))),
            now = 1.0)!!
        assertTrue(b.lat == null || d(far, b.lat!! to b.lon!!) < 0.05)
    }

    @Test fun rejectFarAndJumpWithReason() {
        val home = 55.7558 to 37.6173
        var t = AdsbTracker(home = home)
        val far = Adsb.destination(home.first, home.second, 250.0, 1240.0)
        var a = t.planes[0]
        var m = Adsb.decodeFrame(Adsb.makeDf11(1))
        for ((i, odd) in listOf(false, true).withIndex()) {
            m = Adsb.decodeFrame(Adsb.makeDf17(0x153333, Adsb.meAirbornePos(far.first, far.second, 35000.0, odd)))
            a = t.apply(m, now = 10.0 + i)
        }
        a!!
        assertNull(a.lat); assertTrue(a.suspect); assertTrue(a.rejected >= 1); assertTrue(t.rejected >= 1)
        assertTrue(a.rawNote!!.contains("км от приёмника") && a.rawNote!!.contains("подмена GNSS"))
        assertTrue(d(far, a.rawLat!! to a.rawLon!!) < 0.1)
        assertTrue(Adsb.describe(m).contains("позиция отброшена"))
        // скачок: подтверждённая позиция, через 2 с — на 85 км в сторону
        val p0 = Adsb.destination(home.first, home.second, 90.0, 100.0)
        t = AdsbTracker(home = home)
        for ((i, odd) in listOf(false, true).withIndex())
            a = t.apply(Adsb.decodeFrame(Adsb.makeDf17(0x154444, Adsb.meAirbornePos(p0.first, p0.second, 35000.0, odd))),
                now = 20.0 + i)
        a!!
        assertTrue(a.confirmed)
        val p1 = Adsb.destination(p0.first, p0.second, 0.0, 85.0)
        m = Adsb.decodeFrame(Adsb.makeDf17(0x154444, Adsb.meAirbornePos(p1.first, p1.second, 35000.0, false)))
        t.apply(m, now = 23.0)
        assertTrue(m.posNote, m.posNote!!.startsWith("скачок 85 км за 2 с")); assertTrue(a.suspect)
        assertTrue(d(p0, a.lat!! to a.lon!!) < 0.1)          // прежняя позиция сохранилась
        // одно и то же «новое место» JUMP_ACCEPT раз подряд — принимаем
        for (k in 0 until Adsb.JUMP_ACCEPT)
            t.apply(Adsb.decodeFrame(Adsb.makeDf17(0x154444, Adsb.meAirbornePos(p1.first, p1.second, 35000.0, k % 2 == 1))),
                now = 24.0 + k)
        assertTrue(d(p1, a.lat!! to a.lon!!) < 0.1); assertFalse(a.suspect)
    }

    @Test fun historyTrackThinning() {
        // история хранит трек каждого борта (прореженный, с потолком)
        val home = 55.7558 to 37.6173
        val hist = HashMap<Int, HistEntry>()
        val t = AdsbTracker(home = home, keepS = 60.0, history = hist)
        var p = Adsb.destination(home.first, home.second, 0.0, 50.0)
        var now = 0.0
        for (k in 0 until 4000) {                   // ~1,1 ч полёта 360 км/ч, сообщение раз в секунду
            p = Adsb.destination(p.first, p.second, 90.0, 0.1)
            t.apply(Adsb.decodeFrame(Adsb.makeDf17(0x155555, Adsb.meAirbornePos(p.first, p.second, 30000.0, k % 2 == 1))),
                now = now)
            now += 1.0
        }
        val h = hist.getValue(0x155555)
        assertTrue(h.track.size in 51..AdsbTracker.HIST_TRACK_MAX)   // не каждая точка, но и не пусто
        assertTrue(d(h.track.last().lat to h.track.last().lon, p) < 1.5)
        t.expire(now = now + 3600)                  // из таблицы удалён — в истории остался
        assertTrue(t.planes.isEmpty()); assertTrue(hist.getValue(0x155555).track.isNotEmpty())
    }

    @Test fun levelState() {
        assertEquals("норма", AdsbProcessor.levelState(0.0, -10.0, -15.0))
        assertEquals("перегрузка", AdsbProcessor.levelState(0.0, -1.0, -15.0))
        assertEquals("перегрузка", AdsbProcessor.levelState(0.01, -10.0, -15.0))
        assertEquals("мало", AdsbProcessor.levelState(0.0, -30.0, -35.0))
        assertEquals("мало", AdsbProcessor.levelState(0.0, null, null))
        val p = AdsbProcessor()
        val src = AdsbDemo(seed = 3)
        src.open()
        val re = FloatArray(131072); val im = FloatArray(131072)
        for (k in 0 until 20) {
            src.render(131072, re, im)
            p.process(re, im, now = 1000 + k * 0.0655)
        }
        val st = p.levelStats()
        assertNotNull(st.framesP90); assertTrue(st.noiseDb > -40 && st.noiseDb < 0)
        assertTrue(st.state, st.state == "норма" || st.state == "перегрузка")
        assertEquals(st, p.levelStats())   // блоков не было — прежняя сводка
    }

    // ---------------------------------------------------------------- демодулятор
    /** Детерминированный IQ (тот же расчёт в ref-скрипте Python): кадры с дробными сдвигами + шум от LCG. */
    private fun synth(frames: List<ByteArray>, fs: Int, amps: DoubleArray, sigma: Double, n: Int, seed: Long):
        Pair<FloatArray, FloatArray> {
        val re = DoubleArray(n); val im = DoubleArray(n)
        var pos = 500
        for ((k, fr) in frames.withIndex()) {
            val frac = ((k * 37) % 100) / 100.0
            val env = Adsb.renderEnvelope(fr, fs, frac)
            val ph = 0.7 * k
            val a = amps[k % amps.size]
            val c = cos(ph); val s = sin(ph)
            for (i in env.indices) { re[pos + i] += a * env[i] * c; im[pos + i] += a * env[i] * s }
            pos += env.size + 60 + (k * 53) % 240
        }
        var x = seed
        for (i in 0 until n) {
            for (comp in arrayOf(re, im)) {
                var s = 0.0
                repeat(3) {
                    x = (x * 1664525 + 1013904223) and 0xFFFFFFFFL
                    s += x / 4294967296.0 * 2 - 1
                }
                comp[i] += sigma * s
            }
        }
        return FloatArray(n) { re[it].toFloat() } to FloatArray(n) { im[it].toFloat() }
    }

    private fun feedChunks(d: AdsbDemodulator, re: FloatArray, im: FloatArray, chunk: Int, known: Set<Int>): List<AdsbFrame> {
        val out = ArrayList<AdsbFrame>()
        var i = 0
        while (i < re.size) {
            val n = minOf(chunk, re.size - i)
            out += d.feed(re.copyOfRange(i, i + n), im.copyOfRange(i, i + n), n, known)
            i += n
        }
        return out
    }

    private val refFrames: List<ByteArray> by lazy {
        val bad = Adsb.fromHex("8D4840D6202CC371C32CE0576098").also { it[7] = (it[7].toInt() xor 0x08).toByte() }
        val bad2 = Adsb.makeDf17(0x777777, Adsb.meIdent("BAD1")).also { it[8] = (it[8].toInt() xor 0x01).toByte() }
        val base = listOf(Adsb.fromHex("8D4840D6202CC371C32CE0576098"), Adsb.makeDf11(0x4840D6), Adsb.makeDf4(0x4840D6, 33725.0),
            Adsb.makeDf5(0x4840D6, "7421"), Adsb.fromHex("8D485020994409940838175B284F"),
            Adsb.fromHex("8D40621D58C382D690C8AC2863A7"), Adsb.fromHex("8D40621D58C386435CC412692AD6"), bad,
            Adsb.fromHex("A0001838CA3E51F0A8000047A36A"), bad2, Adsb.makeDf11(0x777777))
        base + base + base
    }

    private fun checkAgainstPython(fs: Int, chunk: Int, n: Int, ref: List<String>, corrected: Int) {
        val (re, im) = synth(refFrames, fs, doubleArrayOf(0.5, 0.2, 0.1, 0.3), 0.03, n, 12345)
        val d = AdsbDemodulator(fs)
        val got = feedChunks(d, re, im, chunk, setOf(0x4840D6, 0xEF614D))
        val exp = ref.map { it.split(" ") }
        assertEquals(exp.map { it[0] }, got.map { Adsb.hex(it.frame) })
        for ((e, g) in exp.zip(got)) {
            assertEquals(e[1], g.icao?.let { "%06X".format(it) } ?: "None")
            assertEquals(e[2].toDouble(), g.signal, 2e-3)
            assertEquals(e[3].toInt(), g.fixed)
        }
        assertEquals(corrected, d.corrected)
    }

    @Test fun demodulatorMatchesPython2M() {
        checkAgainstPython(2_000_000, 7000, 40000, listOf(
            "8D4840D6202CC371C32CE0576098 None -5.4830 0", "28000E923B831B 4840D6 -10.0122 0",
            "8D485020994409940838175B284F None -6.3133 0", "8D4840D6202CC371C32CE0576098 None -10.8057 1",
            "A0001838CA3E51F0A8000047A36A EF614D -5.3471 0", "8D4840D6202CC371C32CE0576098 None -8.9715 0",
            "5D4840D6F8740F None -5.7364 0", "8D40621D58C382D690C8AC2863A7 None -6.0517 0",
            "A0001838CA3E51F0A8000047A36A EF614D -9.2984 0", "5D4840D6F8740F None -9.4579 0",
            "2000159DE69F42 4840D6 -5.6208 0", "8D40621D58C382D690C8AC2863A7 None -10.0312 0",
            "8D40621D58C386435CC412692AD6 None -5.7075 0", "5D777777224C09 None -6.2704 0"), corrected = 1)
    }

    @Test fun demodulatorMatchesPython2400k() {
        checkAgainstPython(2_400_000, 8192, 48000, listOf(
            "8D4840D6202CC371C32CE0576098 None -6.0169 0", "8D485020994409940838175B284F None -5.9193 0",
            "8D4840D6202CC371C32CE0576098 None -10.8743 1", "A0001838CA3E51F0A8000047A36A EF614D -6.0194 0",
            "8D4840D6202CC371C32CE0576098 None -10.4232 0", "5D4840D6F8740F None -5.9770 0",
            "8D40621D58C382D690C8AC2863A7 None -6.2957 0", "A0001838CA3E51F0A8000047A36A EF614D -10.0278 0",
            "5D4840D6F8740F None -10.0480 0", "2000159DE69F42 4840D6 -5.6769 0",
            "8D40621D58C382D690C8AC2863A7 None -8.8209 0", "8D40621D58C386435CC412692AD6 None -6.3342 0",
            "8D4840D6202CC371C32CE0576098 None -11.8112 1", "5D777777224C09 None -5.8732 0"), corrected = 2)
    }

    /** Как _iq_with в tests/test_adsb.py: кадры со случайным дробным сдвигом и фазой + гауссов шум. */
    private fun iqWith(frames: List<ByteArray>, amp: Double, sigma: Double, rng: java.util.Random, fs: Int = 2_000_000):
        Pair<FloatArray, FloatArray> {
        val n = 20000
        val re = FloatArray(n); val im = FloatArray(n)
        var pos = 500
        for (fr in frames) {
            val env = Adsb.renderEnvelope(fr, fs, rng.nextDouble())
            val ph = rng.nextDouble() * 2 * Math.PI
            for (i in env.indices) { re[pos + i] += (amp * env[i] * cos(ph)).toFloat(); im[pos + i] += (amp * env[i] * sin(ph)).toFloat() }
            pos += env.size + 50 + rng.nextInt(250)
        }
        for (i in 0 until n) { re[i] += (rng.nextGaussian() * sigma).toFloat(); im[i] += (rng.nextGaussian() * sigma).toFloat() }
        return re to im
    }

    @Test fun syntheticIqDecoding() {
        val rng = java.util.Random(7)
        val icao = 0x4840D6
        val one = listOf(Adsb.fromHex("8D4840D6202CC371C32CE0576098"), Adsb.makeDf11(icao))
        val frames = (0 until 20).flatMap { one }
        val d = AdsbDemodulator()
        val got = ArrayList<AdsbFrame>()
        repeat(3) {
            val (re, im) = iqWith(frames, 0.4, 0.035, rng)
            got += d.feed(re, im, known = setOf(icao))
        }
        val hexes = frames.map { Adsb.hex(it) }.toSet()
        val ok = got.count { Adsb.hex(it.frame) in hexes }
        assertTrue("принято $ok", ok >= 0.85 * 3 * frames.size)
        assertTrue(got.all { Adsb.hex(it.frame) in hexes })   // ложных кадров нет
    }

    @Test fun noiseOnlyNoFalseFrames() {
        val rng = java.util.Random(3)
        val d = AdsbDemodulator()
        var bad = 0
        val re = FloatArray(131072); val im = FloatArray(131072)
        repeat(20) {
            for (i in re.indices) { re[i] = (rng.nextGaussian() * 0.05).toFloat(); im[i] = (rng.nextGaussian() * 0.05).toFloat() }
            bad += d.feed(re, im).size
        }
        assertEquals(0, bad)
    }

    @Test fun blockBoundary() {
        // кадр, разрезанный границей блоков, всё равно декодируется (хвост переносится)
        val fr = Adsb.fromHex("8D4840D6202CC371C32CE0576098")
        val (re, im) = iqWith(listOf(fr), 0.5, 0.02, java.util.Random(5))
        val d = AdsbDemodulator()
        val got = d.feed(re.copyOfRange(0, 560), im.copyOfRange(0, 560)) +
            d.feed(re.copyOfRange(560, re.size), im.copyOfRange(560, im.size))
        assertEquals(listOf(Adsb.hex(fr)), got.map { Adsb.hex(it.frame) })
    }

    @Test fun rate2400k() {
        val fr = Adsb.fromHex("8D4840D6202CC371C32CE0576098")
        val (re, im) = iqWith(List(20) { fr }, 0.5, 0.02, java.util.Random(11), fs = 2_400_000)
        val got = AdsbDemodulator(2_400_000).feed(re, im)
        assertTrue(got.count { Adsb.hex(it.frame) == Adsb.hex(fr) } >= 12)
    }

    @Test fun demoSourceEndToEnd() {
        val src = AdsbDemo(seed = 2, nplanes = 10)
        src.open()
        // борт с подменой GNSS — на 100 км от приёмника, чтобы был слышен при любом зерне (дальше ~200 км демо
        // ослабляет сигнал ниже порога)
        src.planes.first { it.spoof }.let {
            val q = Adsb.destination(src.home.first, src.home.second, 120.0, 100.0)
            it.lat = q.first; it.lon = q.second
        }
        val p = AdsbProcessor(home = src.home)
        val re = FloatArray(131072); val im = FloatArray(131072)
        for (k in 0 until 200) {  // ≈13 с эфира
            src.render(131072, re, im)
            p.process(re, im, now = 1000.0 + k * 0.0655)
        }
        val truth = src.planes.associateBy { it.icao }
        assertTrue(truth.keys.containsAll(p.tracker.planes.keys))          // ни одного ложного борта
        val planes = p.tracker.planes.values
        val nopos = planes.filter { truth.getValue(it.icao).noPos }
        assertTrue(nopos.all { it.lat == null })
        assertTrue(nopos.count { it.noPos && it.alt != null } >= 2)
        assertEquals(p.msgs, p.drainLog().size); assertTrue(p.drainLog().isEmpty())
        assertTrue(planes.size >= 8)
        val withPos = planes.filter { it.lat != null }
        assertTrue(withPos.size >= 7)
        val spoof = planes.filter { truth.getValue(it.icao).spoof }
        assertTrue(spoof.map { "${it.hex} rej=${it.rejected} susp=${it.suspect} src=${it.posSrc} msgs=${it.msgs} " +
            "note=${it.rawNote}" }.toString() + " всего ${src.planes.count { it.spoof }}",
            spoof.isNotEmpty() && spoof[0].rejected > 0 && spoof[0].suspect)   // подмена GNSS замечена
        val even = planes.filter { truth.getValue(it.icao).evenOnly }
        assertTrue(even.isNotEmpty() && even[0].lat != null)                // только чётные CPR — позиция есть
        for (a in withPos) {
            val pl = truth.getValue(a.icao)
            if (pl.spoof) continue
            assertTrue(Adsb.distanceKm(a.lat!!, a.lon!!, pl.lat, pl.lon) < 2.0)
            if (!pl.ground) assertTrue(abs(a.alt!! - pl.alt) < 300)
        }
        assertEquals(p.tracker.planes.size, p.tracker.history.size)
        // доля принятых зависит от того, сколько бортов далеко (слабый сигнал): у Python при зёрнах 2…5 — 0,43…0,66
        assertTrue("принято ${p.msgs} из ${src.sent}", p.msgs > 0.4 * src.sent)
    }

    // ---------------------------------------------------------------- aircraft_info (офлайн)
    @Test fun countryByIcaoOffline() {
        val cases = mapOf(0x151E04 to "ru", 0x4840D6 to "nl", 0x89618C to "ae", 0xA0F1B2 to "us", 0x3C6444 to "de",
            0x400F00 to "gb", 0x4B8E01 to "tr", 0x780A3B to "cn", 0x7C1234 to "au", 0x683123 to "kz", 0x510123 to "by",
            0x508123 to "ua", 0x896180 to "ae", 0xC01234 to "ca", 0x06A123 to "qa", 0x600900 to "az")
        for ((icao, cc) in cases) assertEquals("%06X".format(icao), cc, AircraftInfo.countryByIcao(icao).first)
        assertEquals("ru" to "Россия", AircraftInfo.countryByIcao(0x151E04))
        assertEquals("nl" to "Нидерланды", AircraftInfo.countryByIcao(0x4840D6))
        assertEquals(null to null, AircraftInfo.countryByIcao(0x000001))       // вне выделенных блоков
        assertEquals(188, AircraftInfo.ICAO_RANGES.size); assertEquals(91, AircraftInfo.AIRLINES.size)
    }

    @Test fun airlineAndPrivateOffline() {
        assertEquals(Triple("AFL", "Аэрофлот", "ru"), AircraftInfo.airlineByCallsign("AFL1124"))
        assertEquals("Россия", AircraftInfo.airlineByCallsign("SDM6569")!!.second)
        assertEquals(Triple("XYZ", null, null), AircraftInfo.airlineByCallsign("XYZ123"))
        assertNull(AircraftInfo.airlineByCallsign("RA67221")); assertTrue(AircraftInfo.looksLikeRegistration("RA67221"))
        assertTrue(AircraftInfo.looksLikeRegistration("N123AB")); assertFalse(AircraftInfo.looksLikeRegistration("AFL31"))
        assertFalse(AircraftInfo.looksLikeRegistration("FV6555")); assertFalse(AircraftInfo.looksLikeRegistration("SU1124"))
        var c = AircraftInfo.card(0x151E04, "SDM6569")
        assertEquals("ru", c.cc); assertEquals("Россия", c.airline); assertEquals(false, c.isPrivate)
        assertEquals("151E04", c.hex); assertEquals("SDM", c.airlineCode)
        c = AircraftInfo.card(0x151F00, "RA67221")
        assertEquals(true, c.isPrivate); assertNull(c.airline)
        c = AircraftInfo.card(0x896180, null)
        assertEquals("ОАЭ", c.country); assertNull(c.isPrivate)
    }
}
