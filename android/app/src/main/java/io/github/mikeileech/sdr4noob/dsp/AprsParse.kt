package io.github.mikeileech.sdr4noob.dsp

import io.github.mikeileech.sdr4noob.i18n.I18n
import io.github.mikeileech.sdr4noob.i18n.N_
import io.github.mikeileech.sdr4noob.i18n.tr
import kotlin.math.abs
import kotlin.math.asin
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.ln
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Разбор APRS — перенос rtltv/aprs.py (разделы «APRS: вспомогательное», «классы станций и символы»,
 * «текст по-русски», «кодеры полей»): позиции (несжатые, сжатые, Mic-E), объекты и элементы, статус,
 * сообщения, телеметрия, погода, NMEA, сторонний трафик; символ, комментарий, высота, курс/скорость,
 * путь (digipeater, МКС RS0ISS/ARISS); расшифровка по-русски; кодеры позиций для тестов и тестового эфира.
 *
 * Поле info разбирается как строка latin-1 (байт = символ), как в Python; тексты — через [Ax25.smartText].
 * Пакет — [AprsPacket]: поле null = ключа нет в словаре Python.
 */

/** Погода (единицы СИ). Целые поля — как int в Python (направление ветра, влажность, освещённость). */
data class AprsWx(
    var windDir: Int? = null,
    var windMs: Double? = null,
    var gustMs: Double? = null,
    var tempC: Double? = null,
    var rain1hMm: Double? = null,
    var rain24hMm: Double? = null,
    var rainMidnightMm: Double? = null,
    var humidity: Int? = null,
    var pressureHpa: Double? = null,
    var lumWm2: Int? = null,
    var snowMm: Double? = null,
) {
    fun isEmpty() = windDir == null && windMs == null && gustMs == null && tempC == null && rain1hMm == null &&
        rain24hMm == null && rainMidnightMm == null && humidity == null && pressureHpa == null && lumWm2 == null &&
        snowMm == null

    /** dict.update: заданные поля [o] перекрывают свои. */
    fun update(o: AprsWx) {
        o.windDir?.let { windDir = it }; o.windMs?.let { windMs = it }; o.gustMs?.let { gustMs = it }
        o.tempC?.let { tempC = it }; o.rain1hMm?.let { rain1hMm = it }; o.rain24hMm?.let { rain24hMm = it }
        o.rainMidnightMm?.let { rainMidnightMm = it }; o.humidity?.let { humidity = it }
        o.pressureHpa?.let { pressureHpa = it }; o.lumWm2?.let { lumWm2 = it }; o.snowMm?.let { snowMm = it }
    }
}

/** Телеметрия: номер, аналоговые каналы (null — не число), биты. base91 — из комментария «|ss11|» (целые). */
data class AprsTelemetry(val seq: String, val analog: List<Double?>, val bits: String?, val base91: Boolean = false)

/** PHG: мощность, высота антенны, усиление, направление (null — круговая). */
data class AprsPhg(val powerW: Int, val heightM: Double, val gainDb: Int, val dir: Int?)

/** Сообщение: адресат, вид (msg/ack/rej/bulletin/nws/tlm_parm/…), номер, текст. */
data class AprsMessage(val to: String, var kind: String = "msg", var id: String? = null, var text: String = "")

/** Разобранный пакет APRS (аналог dict из aprs.parse). */
data class AprsPacket(
    val src: String,
    val dest: String,
    val path: List<Pair<String, Boolean>>,
    var type: String = "other",
    var comment: String = "",
    var raw: String = "",
    var via: String = "",
    var viaIss: Boolean = false,
    var messaging: Boolean? = null,
    var text: String? = null,
    var lat: Double? = null,
    var lon: Double? = null,
    var symTable: String? = null,
    var symCode: String? = null,
    var compressed: Boolean? = null,
    var ambiguity: Int? = null,
    var altM: Double? = null,
    var telemetry: AprsTelemetry? = null,
    var wx: AprsWx? = null,
    var course: Double? = null,
    var speedKmh: Double? = null,
    var dfBearing: Int? = null,
    var phg: AprsPhg? = null,
    var rangeKm: Double? = null,
    var dfs: String? = null,
    var micE: Boolean = false,
    var miceMsg: String? = null,
    var radio: String? = null,
    var timestamp: String? = null,
    var name: String? = null,
    var alive: Boolean? = null,
    var status: String? = null,
    var message: AprsMessage? = null,
    var gateway: String? = null,
    var thirdParty: Boolean = false,
    var outerPath: List<Pair<String, Boolean>>? = null,
    var kind: String = "other",
    var symbol: String? = null,
) {
    /** p.get("wx") в Python истинно только для непустого словаря. */
    val hasWx get() = wx?.isEmpty() == false
}

object Aprs {
    /** позывные и псевдонимы МКС (RS0ISS — digipeater на борту, ARISS/APRSAT — псевдонимы пути) */
    val ISS_CALLS = setOf("RS0ISS", "NA1SS", "OR4ISS", "DP0ISS", "IR0ISS", "ARISS", "APRSAT")

    // ------------------------------------------------------------------ срезы строк как в Python

    private fun String.sl(a: Int, b: Int = Int.MAX_VALUE): String {
        val n = length
        var s = if (a < 0) a + n else a
        var e = if (b < 0) b + n else b
        s = s.coerceIn(0, n); e = e.coerceIn(0, n)
        return if (s >= e) "" else substring(s, e)
    }

    private fun String.ch(i: Int): String = sl(i, i + 1)

    // ------------------------------------------------------------------ base91, координаты, время

    fun b91Decode(s: String): Long? {
        var v = 0L
        for (ch in s) {
            val c = ch.code - 33
            if (c !in 0..90) return null
            v = v * 91 + c
        }
        return v
    }

    fun b91Encode(v0: Long, n: Int): String {
        var v = v0
        val out = CharArray(n)
        for (i in 0 until n) {
            out[n - 1 - i] = (Math.floorMod(v, 91L) + 33).toInt().toChar()
            v = Math.floorDiv(v, 91L)
        }
        return String(out)
    }

    private fun parseLat(s: String): Pair<Double?, Int> {
        if (s.length != 8 || s[4] != '.' || s[7] !in "NnSs") return null to 0
        val amb = s.count { it == ' ' }
        val d = s.sl(0, 7).replace(' ', '0')
        val deg = PyNum.int(d.sl(0, 2)) ?: return null to 0
        val mins = PyNum.float(d.sl(2, 7)) ?: return null to 0
        if (deg > 90 || mins >= 60) return null to 0
        val v = deg + mins / 60
        return (if (s[7] in "Ss") -v else v) to amb
    }

    private fun parseLon(s: String): Double? {
        if (s.length != 9 || s[5] != '.' || s[8] !in "EeWw") return null
        val d = s.sl(0, 8).replace(' ', '0')
        val deg = PyNum.int(d.sl(0, 3)) ?: return null
        val mins = PyNum.float(d.sl(3, 8)) ?: return null
        if (deg > 180 || mins >= 60) return null
        val v = deg + mins / 60
        return if (s[8] in "Ww") -v else v
    }

    /** Отметка времени APRS → текст по-русски или null. */
    private fun ts(s: String): String? {
        if (s.length < 7 || !PyStr.isDigits(s.sl(0, 6))) return null
        val v = s.sl(0, 6); val z = s[6]
        // «²» проходит isdigit(), но не int(): Python здесь падает с ValueError, у нас — «нет отметки времени»
        val day = if (z in "zZ/") PyNum.int(v.sl(0, 2)) ?: return null else 0L
        return when (z) {
            'z', 'Z' -> tr("{day}-е число, {hh}:{mm} UTC", "day" to day, "hh" to v.sl(2, 4), "mm" to v.sl(4, 6))
            '/' -> tr("{day}-е число, {hh}:{mm} (местное)", "day" to day, "hh" to v.sl(2, 4), "mm" to v.sl(4, 6))
            'h' -> "${v.sl(0, 2)}:${v.sl(2, 4)}:${v.sl(4, 6)} UTC"
            else -> null
        }
    }

    private const val TABLE_CHARS = "/\\ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghij"
    private val WX_LEN = mapOf('c' to 3, 's' to 3, 'g' to 3, 't' to 3, 'r' to 3, 'p' to 3, 'P' to 3, 'h' to 2,
        'b' to 5, 'L' to 3, 'l' to 3, '#' to 3)

    /** Поля погоды «g005t077r000…» → (погода, остаток). */
    private fun wxFields(r: String, positional: Boolean): Pair<AprsWx, String> {
        val wx = AprsWx()
        var i = 0
        while (i < r.length && r[i] in WX_LEN) {
            val k = r[i]
            val n = WX_LEN.getValue(k)
            val v = r.sl(i + 1, i + 1 + n)
            if (v.length < n) break
            if (v.trim('.', ' ').isEmpty()) { i += 1 + n; continue }
            val value = PyNum.int(v) ?: break
            i += 1 + n
            when (k) {
                'c' -> wx.windDir = value.toInt()
                's' -> if (positional) wx.snowMm = value * 25.4 else wx.windMs = value * 0.44704
                'g' -> wx.gustMs = value * 0.44704
                't' -> wx.tempC = ((value - 32) * 5).toDouble() / 9
                'r' -> wx.rain1hMm = value * 0.254
                'p' -> wx.rain24hMm = value * 0.254
                'P' -> wx.rainMidnightMm = value * 0.254
                'h' -> wx.humidity = if (value == 0L) 100 else value.toInt()
                'b' -> wx.pressureHpa = value.toDouble() / 10
                'L' -> wx.lumWm2 = value.toInt()
                'l' -> wx.lumWm2 = (value + 1000).toInt()
            }
        }
        return wx to r.sl(i)
    }

    private val RE_ALT = Regex("/A=(-?\\d{5,6})")
    private val RE_DAO = Regex("!([Ww])([0-9])([0-9])!")
    private val RE_TLM = Regex("\\|([!-{]{4,14})\\|")

    private fun cut(s: String, r: IntRange) = s.substring(0, r.first) + s.substring(r.last + 1)

    /** Высота «/A=001234», уточнение «!W12!», телеметрия «|ss11|» из комментария. */
    private fun altAndExtras(rest0: String, p: AprsPacket): String {
        var rest = rest0
        RE_ALT.find(rest)?.let { m ->
            p.altM = PyNum.int(m.groupValues[1])!! * 0.3048
            rest = cut(rest, m.range)
        }
        val dao = RE_DAO.find(rest)
        if (dao != null && p.lat != null) {
            val dl = dao.groupValues[2].toInt() * 0.001 / 60
            val dn = dao.groupValues[3].toInt() * 0.001 / 60
            p.lat = p.lat!! + if (p.lat!! >= 0) dl else -dl
            p.lon = p.lon!! + if (p.lon!! >= 0) dn else -dn
            rest = cut(rest, dao.range)
        }
        val tm = RE_TLM.find(rest)
        if (tm != null && tm.groupValues[1].length % 2 == 0) {
            val g = tm.groupValues[1]
            val vals = (g.indices step 2).map { b91Decode(g.sl(it, it + 2)) }
            if (null !in vals) {
                p.telemetry = AprsTelemetry(vals[0].toString(), vals.subList(1, min(6, vals.size)).map { it!!.toDouble() },
                    null, base91 = true)
                rest = cut(rest, tm.range)
            }
        }
        return rest
    }

    private val RE_CSE = Regex("^(\\d{3}|\\.{3}| {3})/(\\d{3}|\\.{3}| {3})")
    private val RE_DF = Regex("^/(\\d{3})/(\\d{3})")
    private val RE_PHG = Regex("^PHG(\\d)(\\d)(\\d)(\\d)")
    private val RE_RNG = Regex("^RNG(\\d{4})")
    private val RE_DFS = Regex("^DFS(\\d)(\\d)(\\d)(\\d)")

    /** Расширение данных после несжатой позиции: курс/скорость, PHG, RNG, DFS. */
    private fun dataExt(rest0: String, p: AprsPacket): String {
        var rest = rest0
        val m = RE_CSE.find(rest)
        if (m != null) {
            val c = m.groupValues[1]; val s = m.groupValues[2]
            if (p.symCode == "_") {
                val wx = p.wx ?: AprsWx().also { p.wx = it }
                if (PyStr.isDigits(c)) wx.windDir = c.toInt()
                if (PyStr.isDigits(s)) wx.windMs = s.toInt() * 0.44704
            } else {
                if (PyStr.isDigits(c) && c.toInt() in 1..360) p.course = (c.toInt() % 360).toDouble()
                if (PyStr.isDigits(s)) p.speedKmh = s.toInt() * 1.852
            }
            rest = rest.sl(7)
            // DF-отчёт: «/BRG/NRQ»
            val m2 = RE_DF.find(rest)
            if (m2 != null && p.symCode == "\\") {
                p.dfBearing = m2.groupValues[1].toInt()
                rest = rest.sl(8)
            }
        } else {
            val ph = RE_PHG.find(rest)
            if (ph != null) {
                val (pw, h, g, d) = ph.groupValues.drop(1).map { it.toInt() }
                p.phg = AprsPhg(pw * pw, 10 * (1 shl h) * 0.3048, g, if (d == 0) null else d * 45)
                rest = rest.sl(7)
            } else {
                val rn = RE_RNG.find(rest)
                if (rn != null) {
                    p.rangeKm = rn.groupValues[1].toInt() * 1.609344
                    rest = rest.sl(7)
                } else {
                    val df = RE_DFS.find(rest)
                    if (df != null) {
                        p.dfs = df.value
                        rest = rest.sl(7)
                    }
                }
            }
        }
        if (p.symCode == "_") {
            val (wx, r) = wxFields(rest, positional = true)
            rest = r
            (p.wx ?: AprsWx().also { p.wx = it }).update(wx)
        }
        return rest
    }

    /** Позиция (несжатая 19 символов или сжатая 13) в начале s → остаток (комментарий) или null. */
    private fun position(s: String, p: AprsPacket): String? {
        if (s.length >= 19 && (s[0] in '0'..'9' || s[0] == ' ')) {
            val (lat, amb) = parseLat(s.sl(0, 8))
            val lon = parseLon(s.sl(9, 18))
            if (lat == null || lon == null) return null
            p.lat = lat; p.lon = lon; p.symTable = s.ch(8); p.symCode = s.ch(18); p.compressed = false
            if (amb != 0) p.ambiguity = amb
            return dataExt(s.sl(19), p)
        }
        if (s.length >= 10 && s[0] in TABLE_CHARS && s[0] !in '0'..'9') {
            val y = b91Decode(s.sl(1, 5))
            val x = b91Decode(s.sl(5, 9))
            if (y == null || x == null) return null
            val lat = 90 - y / 380926.0
            val lon = -180 + x / 190463.0
            if (!(lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180)) return null
            var tab = s[0]
            if (tab in 'a'..'j') tab = (tab.code - 'a'.code + '0'.code).toChar()
            p.lat = lat; p.lon = lon; p.symTable = tab.toString(); p.symCode = s.ch(9); p.compressed = true
            var rest = s.sl(10)
            if (s.length >= 13) {
                val c = s[10]; val sp = s[11]; val t = s[12]
                rest = s.sl(13)
                if (c != ' ') {
                    val tt = t.code - 33
                    if ((tt and 0x18) == 0x10) {
                        val v = b91Decode("$c$sp")
                        if (v != null) p.altM = 1.002.pow(v.toDouble()) * 0.3048
                    } else if (c == '{') {
                        p.rangeKm = 2 * 1.08.pow((sp.code - 33).toDouble()) * 1.609344
                    } else if (c in '!'..'z') {
                        val crs = (c.code - 33) * 4
                        if (p.symCode == "_") {
                            (p.wx ?: AprsWx().also { p.wx = it }).update(
                                AprsWx(windDir = crs, windMs = (1.08.pow((sp.code - 33).toDouble()) - 1) * 0.44704))
                        } else {
                            if (crs != 0) p.course = (crs % 360).toDouble()
                            p.speedKmh = (1.08.pow((sp.code - 33).toDouble()) - 1) * 1.852
                        }
                    }
                }
            }
            if (p.symCode == "_") {
                val (wx, r) = wxFields(rest, positional = true)
                rest = r
                (p.wx ?: AprsWx().also { p.wx = it }).update(wx)
            }
            return rest
        }
        return null
    }

    /** стандартные сообщения Mic-E (N_ — переводятся через tr при разборе пакета) */
    val MICE_STD = mapOf(7 to N_("Не на службе"), 6 to N_("В пути"), 5 to N_("На службе"), 4 to N_("Возвращаюсь"),
        3 to N_("Занят"), 2 to N_("Особое"), 1 to N_("Приоритет"), 0 to N_("ЭКСТРЕННОЕ"))
    val MICE_RADIO = mapOf("_ " to "Yaesu VX-8", "_\"" to "Yaesu FTM-350", "_#" to "Yaesu VX-8G", "_\$" to "Yaesu FT1D",
        "_%" to "Yaesu FTM-400DR", "_)" to "Yaesu FTM-100D", "_(" to "Yaesu FT2D", "_0" to "Yaesu FT3D",
        "_3" to "Yaesu FT5D", "_1" to "Yaesu FTM-300D", "_4" to "Yaesu FTM-500D", "|3" to "Byonics TinyTrack3",
        "|4" to "Byonics TinyTrack4", "(5" to "Anytone D578UV", "(8" to "Anytone D878UV",
        ":4" to "SCS GmbH Tracker", "^v" to "HinzTec anyfrog")

    private val RE_MICE_ALT = Regex("^([!-{]{3})\\}")

    private fun mice(dest: String, s: String, p: AprsPacket): String? {
        val base = Ax25.splitCall(dest).first
        if (base.length < 6 || s.length < 9) return null
        val digits = IntArray(6); val bits = IntArray(3)
        var custom = false; var std = false
        for (i in 0 until 6) {
            val ch = base[i]
            val d: Int; val bit: Int
            when {
                ch in '0'..'9' -> { d = ch.code - 48; bit = 0 }
                ch in 'A'..'J' -> { d = ch.code - 65; bit = 1; custom = true }
                ch in 'P'..'Y' -> { d = ch.code - 80; bit = 1; if (i < 3) std = true }
                ch == 'K' -> { d = 0; bit = 1; custom = true }
                ch == 'L' -> { d = 0; bit = 0 }
                ch == 'Z' -> { d = 0; bit = 1; if (i < 3) std = true }
                else -> return null
            }
            digits[i] = d
            if (i < 3) bits[i] = bit
        }
        val lat = (digits[0] * 10 + digits[1]) + ((digits[2] * 10 + digits[3]) + (digits[4] * 10 + digits[5]) / 100.0) / 60
        val north = base[3] in 'P'..'Z'
        val lonOff = if (base[4] in 'P'..'Z') 100 else 0
        val west = base[5] in 'P'..'Z'
        val b = IntArray(6) { s[1 + it].code - 28 }
        var d = b[0] + lonOff
        if (d in 180..189) d -= 80 else if (d in 190..199) d -= 190
        var m = b[1]
        if (m >= 60) m -= 60
        val h = b[2]
        if (!(d in 0..180 && m in 0..59 && h in 0..99 && lat <= 90)) return null
        val lon = d + (m + h / 100.0) / 60
        var speed = b[3] * 10 + Math.floorDiv(b[4], 10)
        if (speed >= 800) speed -= 800
        var course = Math.floorMod(b[4], 10) * 100 + b[5]
        if (course >= 400) course -= 400
        p.lat = if (north) lat else -lat; p.lon = if (west) -lon else lon
        p.symCode = s.ch(7); p.symTable = s.ch(8); p.compressed = false; p.micE = true
        if (course in 1..360) p.course = (course % 360).toDouble()
        if (speed != 0) p.speedKmh = speed * 1.852
        val idx = bits[0] * 4 + bits[1] * 2 + bits[2]
        p.miceMsg = when {
            custom && std -> tr("неизвестно")
            custom -> tr("Своё сообщение {n}", "n" to 7 - idx)
            else -> tr(MICE_STD.getValue(idx))
        }
        var rest = s.sl(9)
        var radio: String? = null
        val r0 = rest.sl(0, 1)
        if (r0 == ">" || r0 == "]") {
            val kenwood = rest[0] == '>'
            rest = rest.sl(1)
            if (rest.endsWith("=")) {
                radio = if (kenwood) "Kenwood TH-D72" else "Kenwood TM-D710"
                rest = rest.sl(0, -1)
            } else if (rest.endsWith("^") && kenwood) {
                radio = "Kenwood TH-D74"
                rest = rest.sl(0, -1)
            } else {
                radio = if (kenwood) "Kenwood TH-D7A" else "Kenwood TM-D700"
            }
        } else if (r0 == "`" || r0 == "'") {
            rest = rest.sl(1)
            if (rest.length >= 2 && rest.sl(-2) in MICE_RADIO) {
                radio = MICE_RADIO.getValue(rest.sl(-2))
                rest = rest.sl(0, -2)
            }
        }
        if (radio != null) p.radio = radio
        RE_MICE_ALT.find(rest)?.let {
            p.altM = (b91Decode(it.groupValues[1])!! - 10000).toDouble()
            rest = rest.sl(4)
        }
        return rest
    }

    private fun nmea(s: String, p: AprsPacket): Boolean {
        val f = s.split("*")[0].split(",")
        val lat: String; val ns: String; val lon: String; val ew: String; val spd: String; val crs: String
        if (f[0].sl(3) == "RMC" && f.size >= 9 && f[2] == "A") {
            lat = f[3]; ns = f[4]; lon = f[5]; ew = f[6]; spd = f[7]; crs = f[8]
        } else if (f[0].sl(3) == "GGA" && f.size >= 10) {
            lat = f[2]; ns = f[3]; lon = f[4]; ew = f[5]
            spd = ""; crs = ""
            if (f[9].isNotEmpty()) p.altM = PyNum.float(f[9]) ?: return false
        } else return false
        val la0 = PyNum.int(lat.sl(0, 2)) ?: return false
        val la1 = PyNum.float(lat.sl(2)) ?: return false
        val lo0 = PyNum.int(lon.sl(0, 3)) ?: return false
        val lo1 = PyNum.float(lon.sl(3)) ?: return false
        val la = la0 + la1 / 60
        val lo = lo0 + lo1 / 60
        p.lat = if (ns == "S") -la else la; p.lon = if (ew == "W") -lo else lo; p.symTable = "/"; p.symCode = "/"
        if (spd.isNotEmpty()) p.speedKmh = (PyNum.float(spd) ?: throw NumberFormatException(spd)) * 1.852
        if (crs.isNotEmpty()) p.course = PyNum.mod(PyNum.float(crs) ?: throw NumberFormatException(crs), 360.0)
        return true
    }

    private val RE_ITEM = Regex("^\\)([^!_]{3,9})([!_])")

    /** Кадр (от [Ax25.decode]/[Ax25.parseTnc2]) → пакет APRS. */
    fun parse(fr: Ax25Frame): AprsPacket {
        val p = AprsPacket(fr.src, fr.dest, fr.path.toList())
        p.raw = Ax25.tnc2(fr)
        val (via, iss) = viaInfo(p.path, p.src)
        p.via = via; p.viaIss = iss
        val info = fr.info
        if (!Ax25.isAprs(fr) && fr.pid != null && fr.pid != 0xF0) {
            p.type = "ax25"
            p.text = Ax25.smartText(info)
            return finish(p)
        }
        val s = String(info, Charsets.ISO_8859_1)
        if (s.isEmpty()) {
            p.type = "empty"
            return finish(p)
        }
        val c = s[0]
        var rest: String? = null
        if (c in "!=") {
            p.messaging = c == '='
            if (s.sl(1, 2) == "!") {                   // Ultimeter: «!!» — сырые данные метеостанции
                p.type = "other"
                p.text = s
            } else {
                rest = position(s.sl(1), p)
                p.type = if (rest != null) "position" else "other"
            }
        } else if (c in "/@") {
            p.messaging = c == '@'
            p.timestamp = ts(s.sl(1, 8))
            rest = position(s.sl(8), p)
            p.type = if (rest != null) "position" else "other"
        } else if (c in "`'\u001c\u001d") {
            rest = mice(fr.dest, s, p)
            p.type = if (rest != null) "mic-e" else "other"
        } else if (c == ';' && s.length >= 18) {
            p.name = PyStr.rstrip(Ax25.smartText(s.sl(1, 10)))
            p.alive = s[10] == '*'
            p.timestamp = ts(s.sl(11, 18))
            rest = position(s.sl(18), p)
            p.type = if (rest != null) "object" else "other"
        } else if (c == ')') {
            val m = RE_ITEM.find(s)
            if (m != null) {
                p.name = PyStr.rstrip(Ax25.smartText(m.groupValues[1]))
                p.alive = m.groupValues[2] == "!"
                rest = position(s.sl(m.range.last + 1), p)
            }
            p.type = if (rest != null) "item" else "other"
        } else if (c == '>') {
            var t = s.sl(1)
            val tsv = ts(t.sl(0, 7))
            if (tsv != null) {
                p.timestamp = tsv
                t = t.sl(7)
            }
            p.type = "status"
            p.status = PyStr.strip(Ax25.smartText(t))
        } else if (c == ':' && s.length >= 11 && s[10] == ':') {
            message(s, p)
        } else if (c == 'T' && s.startsWith("T#")) {
            val parts = s.sl(2).split(",")
            val an = parts.drop(1).take(5).map { PyNum.float(it) }
            val bits = if (parts.size > 6) parts[6].sl(0, 8) else null
            p.type = "telemetry"
            p.telemetry = AprsTelemetry(parts[0], an, bits)
            if (parts.size > 6 && parts[6].length > 8) p.comment = PyStr.strip(Ax25.smartText(parts[6].sl(8)))
        } else if (c == '_' && s.length >= 9 && PyStr.isDigits(s.sl(1, 9))) {
            p.type = "weather"
            p.timestamp = "${s.sl(3, 5)}.${s.sl(1, 3)} ${s.sl(5, 7)}:${s.sl(7, 9)}"
            val (wx, rest2) = wxFields(s.sl(9), positional = false)
            p.wx = wx
            p.comment = PyStr.strip(Ax25.smartText(rest2))
        } else if (c == '$') {
            p.type = if (nmeaSafe(s, p)) "nmea" else "other"
            p.text = s
        } else if (c == '}') {
            val inner = Ax25.parseTnc2(s.sl(1))
            if (inner != null) {
                val q = parse(inner)
                q.gateway = p.src
                q.thirdParty = true
                q.raw = p.raw
                q.via = p.via; q.viaIss = p.viaIss
                q.outerPath = p.path
                return q
            }
            p.text = s
        } else if (c == '<') {
            p.type = "capabilities"
            p.text = Ax25.smartText(s.sl(1))
        } else if (c == '?') {
            p.type = "query"
            p.text = Ax25.smartText(s.sl(1))
        } else {
            val k = s.sl(0, 41).indexOf('!')
            if (k > 0) {
                rest = position(s.sl(k + 1), p)
                if (rest != null) {
                    p.type = "position"
                    p.comment = PyStr.strip(Ax25.smartText(s.sl(0, k)))
                }
            }
            if (rest == null) p.text = Ax25.smartText(s)
        }
        if (rest != null) {
            val r = altAndExtras(rest, p)
            val txt = PyStr.strip(Ax25.smartText(r))
            p.comment = if (p.type == "mic-e") txt else PyStr.strip(p.comment + " " + txt)
        }
        if (p.hasWx && p.type == "position") p.type = "weather"
        return finish(p)
    }

    /** В Python ошибка float() скорости/курса NMEA поднимается наружу из parse; здесь — пакет «other». */
    private fun nmeaSafe(s: String, p: AprsPacket) = try { nmea(s, p) } catch (e: NumberFormatException) { false }

    private val PYWS = "[ \\t\\n\\r\\u000B\\u000C\\u001C-\\u001F\\u0085\\u00A0]"
    private val RE_ACK = Regex("(ack|rej)([A-Za-z0-9}]{1,7})$PYWS*")

    private fun message(s: String, p: AprsPacket) {
        val addressee = PyStr.strip(s.sl(1, 10))
        var text = s.sl(11)
        p.type = "message"
        val msg = AprsMessage(addressee)
        val m = RE_ACK.matchEntire(text)
        if (m != null) {
            msg.kind = m.groupValues[1]
            msg.id = m.groupValues[2].split("}")[0]
            text = ""
        } else {
            val k = text.lastIndexOf('{')
            if (k >= 0 && text.length - k <= 9) {
                msg.id = PyStr.strip(text.sl(k + 1).split("}")[0])
                text = text.sl(0, k)
            }
            msg.kind = "msg"
            for ((pref, kind) in listOf("PARM." to "parm", "UNIT." to "unit", "EQNS." to "eqns", "BITS." to "bits")) {
                if (text.startsWith(pref)) { msg.kind = "tlm_$kind"; break }
            }
            if (addressee.startsWith("BLN")) msg.kind = "bulletin"
            else if (addressee.startsWith("NWS")) msg.kind = "nws"
        }
        msg.text = PyStr.rstrip(Ax25.smartText(text))
        p.message = msg
    }

    // ------------------------------------------------------------------ классы станций и символы

    /** названия символов (N_ — переводятся в symbolName) */
    val SYM_PRIMARY = mapOf(
        "!" to N_("Полиция"), "#" to "Digipeater", "\$" to N_("Телефон"), "%" to N_("DX-кластер"), "&" to N_("КВ-шлюз"),
        "'" to N_("Малый самолёт"), "(" to N_("Мобильный спутник"), ")" to N_("Инвалидная коляска"), "*" to N_("Снегоход"),
        "+" to N_("Красный Крест"), "," to N_("Скауты"), "-" to N_("Дом"), "." to N_("Крестик"), "/" to N_("Точка"), ":" to N_("Пожар"),
        ";" to N_("Лагерь"), "<" to N_("Мотоцикл"), "=" to N_("Поезд"), ">" to N_("Автомобиль"), "?" to N_("Сервер"), "@" to N_("Ураган"),
        "A" to N_("Аптечка"), "B" to "BBS", "C" to N_("Каноэ"), "E" to N_("Событие"), "F" to N_("Трактор"), "G" to N_("Квадрат"),
        "H" to N_("Гостиница"), "I" to "TCP/IP", "K" to N_("Школа"), "L" to N_("Компьютер"), "M" to "MacAPRS", "N" to N_("Станция NTS"),
        "O" to N_("Аэростат"), "P" to N_("Полиция"), "R" to N_("Дом на колёсах"), "S" to N_("Шаттл"), "T" to "SSTV", "U" to N_("Автобус"),
        "V" to "ATV", "W" to N_("Метеослужба"), "X" to N_("Вертолёт"), "Y" to N_("Яхта"), "Z" to "WinAPRS", "[" to N_("Пешеход"),
        "\\" to N_("Пеленг (DF)"), "]" to N_("Почта"), "^" to N_("Самолёт"), "_" to N_("Метеостанция"), "`" to N_("Спутниковая антенна"),
        "a" to N_("Скорая помощь"), "b" to N_("Велосипед"), "c" to N_("Пункт управления"), "d" to N_("Пожарная часть"), "e" to N_("Лошадь"),
        "f" to N_("Пожарная машина"), "g" to N_("Планер"), "h" to N_("Больница"), "i" to N_("Остров (IOTA)"), "j" to N_("Джип"),
        "k" to N_("Грузовик"), "l" to N_("Ноутбук"), "m" to N_("Mic-E репитер"), "n" to N_("Узел"), "o" to N_("Штаб"), "p" to N_("Собака"),
        "q" to N_("Квадрат"), "r" to N_("Репитер"), "s" to N_("Судно"), "t" to N_("Стоянка грузовиков"), "u" to N_("Фура"), "v" to N_("Фургон"),
        "w" to N_("Водоочистка"), "x" to "X-APRS", "y" to N_("Дом с антенной"), "z" to N_("Убежище"),
    )
    val SYM_ALT = mapOf(
        "#" to "Digipeater", "&" to N_("Шлюз (I-Gate)"), "-" to N_("Дом (КВ)"), ">" to N_("Автомобиль"), "_" to N_("Метеостанция"),
        "S" to N_("Спутник"), "n" to N_("Треугольник"), "O" to N_("Ракета/зонд"), "^" to N_("Самолёт"), "k" to N_("Внедорожник"),
        "u" to N_("Грузовик"), "v" to N_("Фургон"), "s" to N_("Катер"), "0" to N_("Круг (IRLP/EchoLink)"), "a" to "ARES",
        "L" to N_("Маяк"), "W" to N_("Метеостанция"), "j" to N_("Работы на дороге"), "!" to N_("Авария"), "[" to N_("Пешеход"),
    )

    /** вид станции: подпись (N_ — показывать через tr) и цвет на карте */
    val KINDS = linkedMapOf(
        "iss" to (N_("МКС / спутник") to "#ff5cf0"),
        "car" to (N_("Транспорт") to "#ffb84d"),
        "person" to (N_("Пешеход") to "#ff8c69"),
        "home" to (N_("Дом") to "#7fdc7f"),
        "wx" to (N_("Метеостанция") to "#4fd1ff"),
        "digi" to ("Digipeater" to "#c39bff"),
        "igate" to (N_("Шлюз I-Gate") to "#a77bff"),
        "air" to (N_("Воздушное судно") to "#ffd84d"),
        "boat" to (N_("Судно") to "#4f8cff"),
        "object" to (N_("Объект") to "#c8c8c8"),
        "other" to (N_("Прочее") to "#e8e8e8"),
    )
    private val SYM_KIND = mapOf(
        ">" to "car", "k" to "car", "u" to "car", "v" to "car", "j" to "car", "U" to "car", "R" to "car", "<" to "car",
        "a" to "car", "f" to "car", "!" to "car", "P" to "car", "=" to "car", "b" to "car", "*" to "car", "F" to "car",
        "[" to "person", "e" to "person", "p" to "person",
        "-" to "home", "y" to "home", "H" to "home",
        "_" to "wx", "W" to "wx", "@" to "wx",
        "#" to "digi", "r" to "digi", "m" to "digi",
        "&" to "igate", "I" to "igate",
        "'" to "air", "^" to "air", "X" to "air", "g" to "air", "O" to "air",
        "s" to "boat", "Y" to "boat", "C" to "boat",
    )

    fun symbolName(table: String?, code: String?): String {
        if (table.isNullOrEmpty() || code.isNullOrEmpty()) return ""
        if (table == "/") return SYM_PRIMARY[code]?.let { tr(it) } ?: tr("символ /{code}", "code" to code)
        var name = SYM_ALT[code]?.let { tr(it) } ?: tr("символ \\{code}", "code" to code)
        if (table != "\\") name += " [$table]"
        return name
    }

    fun stationKind(p: AprsPacket): String {
        val base = Ax25.splitCall(p.src).first
        val code = p.symCode; val table = p.symTable
        if (base in ISS_CALLS || (code == "S" && table != null && table != "/")) return "iss"
        if (p.type == "object" || p.type == "item") return "object"
        if (p.hasWx || p.type == "weather") return "wx"
        if (!code.isNullOrEmpty() && !table.isNullOrEmpty()) {
            if (table != "/" && code == "&") return "igate"
            return SYM_KIND[code] ?: "other"
        }
        return "other"
    }

    private val RE_GENERIC = Regex("(WIDE|TRACE|RELAY|TCPIP|RFONLY)\\d?(-\\d+)?\\n?")
    private val RE_ALIAS = Regex("(WIDE|TRACE|RELAY)\\d?(-\\d+)?\\n?")

    /** Путь → (подпись, через МКС ли). Учитываются только digipeater с битом H. */
    fun viaInfo(path: List<Pair<String, Boolean>>, src: String = ""): Pair<String, Boolean> {
        val used = path.filter { it.second }.map { it.first }
        val viaIss = used.any { Ax25.splitCall(it).first in ISS_CALLS }
        if (viaIss) return tr("через МКС") to true
        if (Ax25.splitCall(src).first in ISS_CALLS) return tr("МКС (борт)") to true
        if (used.isNotEmpty()) {
            val last = used.last()
            if (RE_GENERIC.matches(last)) {
                val named = used.filter { !RE_ALIAS.matches(it) }
                return (if (named.isNotEmpty()) tr("через {call}", "call" to named.last()) else tr("через ретранслятор")) to false
            }
            return tr("через {call}", "call" to last) to false
        }
        return tr("напрямую") to false
    }

    private fun finish(p: AprsPacket): AprsPacket {
        p.kind = stationKind(p)
        if (!p.symCode.isNullOrEmpty()) p.symbol = symbolName(p.symTable, p.symCode)
        return p
    }

    /** TNC2-строка → разобранный пакет (для тестов и журналов). */
    fun parseLine(text: String): AprsPacket? = Ax25.parseTnc2(text)?.let { parse(it) }

    /** Байты кадра без FCS → разобранный пакет или null. */
    fun parseFrame(data: ByteArray): AprsPacket? = Ax25.decode(data)?.let { parse(it) }

    // ------------------------------------------------------------------ текст по-русски

    /** Как Python (PyNum.fixed), с запятой по-русски; свой, а не i18n.num — округление как в Python. */
    private fun num(v: Double, nd: Int = 1) = PyNum.fixed(v, nd).let { if (I18n.language == "ru") it.replace(".", ",") else it }

    fun fmtLat(v: Double): String {
        val d = abs(v).toInt()
        val m = (abs(v) - d) * 60
        return if (v >= 0) tr("{deg}°{min}′ с. ш.", "deg" to d, "min" to num(m, 3))
        else tr("{deg}°{min}′ ю. ш.", "deg" to d, "min" to num(m, 3))
    }

    fun fmtLon(v: Double): String {
        val d = abs(v).toInt()
        val m = (abs(v) - d) * 60
        return if (v >= 0) tr("{deg}°{min}′ в. д.", "deg" to d, "min" to num(m, 3))
        else tr("{deg}°{min}′ з. д.", "deg" to d, "min" to num(m, 3))
    }

    /** QTH-локатор (Maidenhead), 6 знаков. */
    fun locator(lat0: Double, lon0: Double): String {
        val lon = lon0 + 180
        val lat = lat0 + 90
        val a = "" + ('A' + Afsk.floorDiv(lon, 20.0).toInt()) + ('A' + Afsk.floorDiv(lat, 10.0).toInt())
        val b = Afsk.floorDiv(PyNum.mod(lon, 20.0), 2.0).toInt().toString() + PyNum.mod(lat, 10.0).toInt().toString()
        val c = "" + ('a' + (PyNum.mod(lon, 2.0) * 12).toInt()) + ('a' + (PyNum.mod(lat, 1.0) * 24).toInt())
        return a + b + c
    }

    fun distanceKm(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val r = 6371.0
        val p1 = Math.toRadians(lat1); val p2 = Math.toRadians(lat2)
        val dp = p2 - p1
        val dl = Math.toRadians(lon2 - lon1)
        val h = sin(dp / 2).pow(2) + cos(p1) * cos(p2) * sin(dl / 2).pow(2)
        return 2 * r * asin(min(1.0, sqrt(h)))
    }

    fun bearingDeg(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val p1 = Math.toRadians(lat1); val p2 = Math.toRadians(lat2)
        val dl = Math.toRadians(lon2 - lon1)
        val y = sin(dl) * cos(p2)
        val x = cos(p1) * sin(p2) - sin(p1) * cos(p2) * cos(dl)
        return PyNum.mod(Math.toDegrees(atan2(y, x)), 360.0)
    }

    fun wxText(wx: AprsWx): String {
        val out = ArrayList<String>()
        if (wx.windDir != null || wx.windMs != null) {
            var t = ""
            wx.windDir?.let { t += " $it°" }
            wx.windMs?.let { t += " " + tr("{v} м/с", "v" to num(it)) }
            wx.gustMs?.let { t += tr(", порывы {v} м/с", "v" to num(it)) }
            out.add(tr("ветер {wind}", "wind" to t.removePrefix(" ")))
        }
        wx.tempC?.let { out.add("${num(it)} °C") }
        wx.humidity?.let { out.add(tr("влажность {v} %", "v" to it)) }
        wx.pressureHpa?.let { out.add(tr("давление {v} гПа", "v" to num(it))) }
        wx.rain1hMm?.let { out.add(tr("осадки за час {v} мм", "v" to num(it))) }
        wx.rain24hMm?.let { out.add(tr("за сутки {v} мм", "v" to num(it))) }
        wx.rainMidnightMm?.let { out.add(tr("с полуночи {v} мм", "v" to num(it))) }
        wx.snowMm?.let { out.add(tr("снег {v} мм", "v" to num(it, 0))) }
        wx.lumWm2?.let { out.add(tr("освещённость {v} Вт/м²", "v" to it)) }
        return out.joinToString(", ")
    }

    private fun posText(p: AprsPacket): String {
        val out = arrayListOf("${fmtLat(p.lat!!)}, ${fmtLon(p.lon!!)}")
        if (!p.symbol.isNullOrEmpty()) out.add(p.symbol!!)
        val mv = ArrayList<String>()
        p.speedKmh?.let { mv.add(tr("{v} км/ч", "v" to PyNum.fixed(it, 0))) }
        p.course?.let { mv.add(tr("курс {v}°", "v" to PyNum.fixed(it, 0))) }
        if (mv.isNotEmpty()) out.add(mv.joinToString(", "))
        p.altM?.let { out.add(tr("высота {v} м", "v" to PyNum.fixed(it, 0))) }
        p.phg?.let { g ->
            out.add(tr("мощность {power} Вт, антенна {height} м, {gain} dB", "power" to g.powerW,
                "height" to PyNum.fixed(g.heightM, 0), "gain" to g.gainDb) +
                (if (g.dir == null) "" else tr(", направл. {dir}°", "dir" to g.dir)))
        }
        p.rangeKm?.let { if (it != 0.0) out.add(tr("радиус {v} км", "v" to PyNum.fixed(it, 0))) }
        return out.joinToString(" · ")
    }

    /** Расшифровка пакета по-русски (одна строка). */
    fun describe(p: AprsPacket): String {
        val t = p.type
        val parts = ArrayList<String>()
        if (p.thirdParty) parts.add(tr("Сторонний пакет через шлюз {gateway}", "gateway" to p.gateway))
        when (t) {
            "position" -> parts.add(tr("Позиция: {pos}", "pos" to posText(p)))
            "mic-e" -> {
                parts.add(tr("Позиция (Mic-E): {pos}", "pos" to posText(p)))
                if (!p.miceMsg.isNullOrEmpty()) parts.add(tr("статус «{status}»", "status" to p.miceMsg))
                if (!p.radio.isNullOrEmpty()) parts.add(tr("радио {radio}", "radio" to p.radio))
            }
            "object", "item" -> {
                val pos = posText(p)
                parts.add(when {
                    t == "object" && p.alive == true -> tr("Объект «{name}» (активен): {pos}", "pos" to pos, "name" to p.name)
                    t == "object" -> tr("Объект «{name}» (удалён): {pos}", "pos" to pos, "name" to p.name)
                    p.alive == true -> tr("Элемент «{name}» (активен): {pos}", "pos" to pos, "name" to p.name)
                    else -> tr("Элемент «{name}» (удалён): {pos}", "pos" to pos, "name" to p.name)
                })
            }
            "weather" -> {
                if (p.lat != null) parts.add(tr("Метеостанция: {lat}, {lon}", "lat" to fmtLat(p.lat!!), "lon" to fmtLon(p.lon!!)))
                else parts.add(tr("Погода"))
                parts.add(wxText(p.wx ?: AprsWx()).ifEmpty { tr("нет данных") })
            }
            "status" -> parts.add(tr("Статус: «{status}»", "status" to (p.status ?: "")))
            "message" -> {
                val m = p.message!!
                val k = m.kind
                when {
                    k == "ack" -> parts.add(tr("Подтверждение №{id} для {to}", "id" to m.id, "to" to m.to))
                    k == "rej" -> parts.add(tr("Отказ №{id} для {to}", "id" to m.id, "to" to m.to))
                    k == "bulletin" -> parts.add(tr("Бюллетень {to}: «{text}»", "to" to m.to, "text" to m.text))
                    k.startsWith("tlm_") -> {
                        val names = mapOf("tlm_parm" to tr("названия каналов"), "tlm_unit" to tr("единицы"),
                            "tlm_eqns" to tr("коэффициенты"), "tlm_bits" to tr("биты"))
                        parts.add(tr("Описание телеметрии ({what}): {text}", "what" to names.getValue(k), "text" to m.text))
                    }
                    !m.id.isNullOrEmpty() ->
                        parts.add(tr("Сообщение для {to}: «{text}» (№{id})", "to" to m.to, "id" to m.id, "text" to m.text))
                    else -> parts.add(tr("Сообщение для {to}: «{text}»", "to" to m.to, "text" to m.text))
                }
            }
            "telemetry" -> {
                val tl = p.telemetry!!
                val an = tl.analog.joinToString(", ") { if (it == null) "—" else PyNum.g(it) }
                parts.add(if (!tl.bits.isNullOrEmpty())
                    tr("Телеметрия №{seq}: {values}; биты {bits}", "seq" to tl.seq, "bits" to tl.bits, "values" to an)
                else tr("Телеметрия №{seq}: {values}", "seq" to tl.seq, "values" to an))
            }
            "nmea" -> parts.add("NMEA GPS: " + posText(p))
            "capabilities" -> parts.add(tr("Возможности станции: {text}", "text" to (p.text ?: "")))
            "query" -> parts.add(tr("Запрос: {text}", "text" to (p.text ?: "")))
            "ax25" -> parts.add(tr("Кадр AX.25 (не APRS): {text}", "text" to (p.text ?: "")))
            "empty" -> parts.add(tr("Пустой пакет"))
            else -> parts.add(tr("Данные: «{text}»", "text" to (p.text ?: "")))
        }
        if (t != "weather" && p.hasWx) parts.add(tr("погода: {wx}", "wx" to wxText(p.wx!!)))
        if (t != "telemetry" && p.telemetry != null) {
            val tl = p.telemetry!!
            parts.add(tr("телеметрия: {values}", "values" to tl.analog.joinToString(", ") {
                when {
                    it == null -> "None"
                    tl.base91 -> it.toLong().toString()
                    else -> it.toString()
                }
            }))
        }
        if (!p.timestamp.isNullOrEmpty()) parts.add(tr("время {time}", "time" to p.timestamp))
        if (p.comment.isNotEmpty()) parts.add(tr("«{comment}»", "comment" to p.comment))
        return parts.joinToString(" · ")
    }

    // ------------------------------------------------------------------ кодеры полей (тесты, эфир)

    /** Несжатая позиция «!DDMM.mmN/DDDMM.mmE>…» (с ts — «@DDHHMMz…»). */
    fun encodePosition(lat: Double, lon: Double, table: String = "/", code: String = ">", comment: String = "",
                       messaging: Boolean = false, ts: String? = null): String {
        val la = abs(lat); val lo = abs(lon)
        var dla = la.toInt(); var dlo = lo.toInt()
        var mla = PyNum.round((la - dla) * 60, 2)
        var mlo = PyNum.round((lo - dlo) * 60, 2)
        if (mla >= 60) { dla += 1; mla = 0.0 }
        if (mlo >= 60) { dlo += 1; mlo = 0.0 }
        val s = dla.toString().padStart(2, '0') + PyNum.fixed(mla, 2).padStart(5, '0') + (if (lat >= 0) "N" else "S") +
            table + dlo.toString().padStart(3, '0') + PyNum.fixed(mlo, 2).padStart(5, '0') + (if (lon >= 0) "E" else "W") + code
        if (!ts.isNullOrEmpty()) return (if (messaging) "@" else "/") + ts + s + comment
        return (if (messaging) "=" else "!") + s + comment
    }

    fun encodeCompressed(lat: Double, lon: Double, table: String = "/", code: String = ">", course: Double? = null,
                         speedKn: Double? = null, altFt: Double? = null, comment: String = ""): String {
        val y = Math.rint(380926 * (90 - lat)).toLong()
        val x = Math.rint(190463 * (180 + lon)).toLong()
        var s = table + b91Encode(y, 4) + b91Encode(x, 4) + code
        if (altFt != null) {
            val v = Math.rint(ln(maxOf(altFt, 1.0)) / ln(1.002)).toLong()
            s += b91Encode(v, 2) + (33 + 0x30).toChar()
        } else if (course != null && speedKn != null) {
            s += (33 + Math.floorMod(Math.rint(course / 4).toLong(), 90L)).toInt().toChar()
            s += (33 + Math.rint(ln(speedKn + 1) / ln(1.08)).toInt()).toChar()
            s += (33 + 0x26).toChar()
        } else {
            s += "  " + (33 + 0x20).toChar()
        }
        return "!" + s + comment
    }

    /** Mic-E → (поле назначения, поле данных) для makeFrame. msg — номер стандартного сообщения 0…7. */
    fun encodeMice(lat: Double, lon: Double, speedKn: Double = 0.0, course: Double = 0.0, table: String = "/",
                   code: String = ">", msg: Int = 6, comment: String = "", altM: Double? = null,
                   radio: String? = null): Pair<String, String> {
        val tm = Math.rint(abs(lat) * 6000).toLong()
        val dg = (tm / 6000).toString().padStart(2, '0') + ((tm % 6000) / 100).toString().padStart(2, '0') +
            (tm % 100).toString().padStart(2, '0')
        val tl = Math.rint(abs(lon) * 6000).toLong()
        val ldeg = (tl / 6000).toInt(); val lmin = ((tl % 6000) / 100).toInt(); val lhun = (tl % 100).toInt()
        val off = ldeg < 10 || ldeg >= 100
        val flags = listOf((msg shr 2) and 1 != 0, (msg shr 1) and 1 != 0, msg and 1 != 0, lat >= 0, off, lon < 0)
        val dest = StringBuilder()
        for (i in 0 until min(dg.length, 6)) dest.append(((if (flags[i]) 'P'.code else '0'.code) + (dg[i] - '0')).toChar())
        val d = when {
            ldeg < 10 -> ldeg + 90
            ldeg < 100 -> ldeg
            ldeg < 110 -> ldeg - 20
            else -> ldeg - 100
        }
        val m = if (lmin < 10) lmin + 60 else lmin
        val speed = Math.rint(speedKn).toInt()
        val crs = Math.floorMod(Math.rint(course).toInt(), 360)
        var sp = Math.floorDiv(speed, 10)
        if (sp < 20) sp += 80
        val dc = Math.floorMod(speed, 10) * 10 + crs / 100
        val se = crs % 100
        var info = "`" + intArrayOf(d, m, lhun, sp, dc, se).map { (it + 28).toChar() }.joinToString("") + code + table
        if (!radio.isNullOrEmpty()) info = info.sl(0, 9) + radio[0]
        if (altM != null) info += b91Encode(Math.rint(altM).toLong() + 10000, 3) + "}"
        info += comment
        if (radio != null && radio.length > 1) info += radio.substring(1)
        return dest.toString() to info
    }
}
