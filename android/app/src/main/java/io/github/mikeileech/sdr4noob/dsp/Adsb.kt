package io.github.mikeileech.sdr4noob.dsp

import io.github.mikeileech.sdr4noob.i18n.N_
import io.github.mikeileech.sdr4noob.i18n.tr
import java.math.BigDecimal
import java.math.RoundingMode
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.acos
import kotlin.math.asin
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.hypot
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.round
import kotlin.math.sin
import kotlin.math.sqrt

/** Поле CPR позиционного сообщения: (нечётный 0/1, широта 17 бит, долгота 17 бит, на земле) — m["cpr"] в Python. */
data class CprField(val odd: Int, val lat: Int, val lon: Int, val surface: Boolean)

/**
 * Разобранное сообщение Mode S — словарь decode_frame() из rtltv/adsb.py. Поле null — ключа в словаре нет.
 */
class AdsbMsg(val df: Int, val raw: String) {
    var icao: Int? = null
    var tc: Int? = null
    var callsign: String? = null
    var category: String? = null
    var onGround: Boolean? = null
    var gs: Double? = null
    var track: Double? = null
    var heading: Double? = null
    var ias: Int? = null
    var tas: Int? = null
    var vr: Int? = null
    var gnssDiff: Int? = null
    var alt: Int? = null
    var altGnss: Int? = null
    var cpr: CprField? = null
    var noPos = false
    var selAlt: Int? = null
    var qnh: Double? = null
    var selHdg: Double? = null
    var emergency: Int? = null
    var squawk: String? = null
    /** Пояснение трекера к позиции («позиция отброшена: …», «по одиночному сообщению …»). */
    var posNote: String? = null
}

/**
 * ADS-B / Mode S на 1090 МГц — перенос rtltv/adsb.py (без Qt и потоков): CRC-24 и синдром, поля Mode S (сквок,
 * код Гиллхэма, высоты AC12/AC13), CPR (глобальное по паре чётн./нечётн. и локальное), геодезия, разбор кадров
 * DF0/4/5/11/16/17/18/20/21, расшифровка по-русски для журнала, кодеры кадров (демо и тесты) и огибающая PPM.
 * Демодулятор — [AdsbDemodulator], таблица бортов — [AdsbTracker], поток обработки — [AdsbProcessor].
 */
object Adsb {
    const val ADSB_FREQ = 1_090_000_000L
    const val FS = 2_000_000
    const val STALE_S = 60.0          // борт «давно не слышен», если 60 с не было сообщений
    const val CPR_PAIR_S = 10.0       // чётное/нечётное сообщение для глобального CPR — не дальше 10 с друг от друга
    const val MAX_RANGE_KM = 700.0    // дальше от приёмника ADS-B на 1090 не слышно — значит, ошибка или подмена GNSS
    const val SINGLE_RANGE_KM = 300.0 // одиночное CPR декодируем от приёмника, только если результат ближе этого
    const val JUMP_ACCEPT = 3         // столько согласованных «скачков» подряд — принимаем новое место
    const val FT = 0.3048
    const val KT_KMH = 1.852

    // ---------------------------------------------------------------- CRC-24 Mode S
    private const val POLY = 0xFFF409
    private val CRC_TABLE = IntArray(256) { i ->
        var c = i shl 16
        repeat(8) { c = if (c and 0x800000 != 0) (c shl 1) xor POLY else c shl 1 }
        c and 0xFFFFFF
    }

    fun crc24(data: ByteArray, len: Int = data.size): Int {
        var crc = 0
        for (k in 0 until len) {
            val b = data[k].toInt() and 0xFF
            crc = ((crc shl 8) and 0xFFFFFF) xor CRC_TABLE[((crc shr 16) xor b) and 0xFF]
        }
        return crc
    }

    /** CRC данных XOR поле чётности. 0 — кадр целый (DF11/17/18); для DF0/4/5/16/20/21 — это ICAO. */
    fun syndrome(frame: ByteArray): Int {
        val n = frame.size
        val par = ((frame[n - 3].toInt() and 0xFF) shl 16) or ((frame[n - 2].toInt() and 0xFF) shl 8) or
            (frame[n - 1].toInt() and 0xFF)
        return crc24(frame, n - 3) xor par
    }

    /** Вклад i-го бита 112-битного кадра в синдром (у 56-битного — с 56-го). */
    val CONTRIB = IntArray(112) { i ->
        val b = ByteArray(14)
        b[i shr 3] = (0x80 ushr (i and 7)).toByte()
        syndrome(b)
    }

    /** Синдром одиночной ошибки → номер бита (биты DF 0..4 не трогаем). */
    val FIX1: Map<Int, Int> = HashMap<Int, Int>().also { for (i in 5 until 112) it[CONTRIB[i]] = i }
    val LONG_DF = intArrayOf(16, 17, 18, 19, 20, 21, 24)
    val SHORT_DF = intArrayOf(0, 4, 5, 11)
    internal val VALID_DF = intArrayOf(0, 4, 5, 11, 16, 17, 18, 20, 21)

    // ---------------------------------------------------------------- поля Mode S
    const val CHARSET = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"

    val CATEGORIES: Map<Pair<Int, Int>, String> = mapOf(
        (4 to 1) to N_("A1 лёгкий (< 7 т)"), (4 to 2) to N_("A2 малый (7–34 т)"), (4 to 3) to N_("A3 большой (34–136 т)"),
        (4 to 4) to N_("A4 большой с сильным вихрем (B757)"), (4 to 5) to N_("A5 тяжёлый (> 136 т)"),
        (4 to 6) to N_("A6 высокоманёвренный"), (4 to 7) to N_("A7 вертолёт"),
        (3 to 1) to N_("B1 планер"), (3 to 2) to N_("B2 аэростат"), (3 to 3) to N_("B3 парашютист"),
        (3 to 4) to N_("B4 дельтаплан/параплан"), (3 to 6) to N_("B6 беспилотник"), (3 to 7) to N_("B7 космический аппарат"),
        (2 to 1) to N_("C1 аварийная машина"), (2 to 3) to N_("C3 служебная машина"), (2 to 4) to N_("C4 точечное препятствие"),
        (2 to 5) to N_("C5 групповое препятствие"), (2 to 6) to N_("C6 протяжённое препятствие"),
    )

    val SQUAWK_NOTES = mapOf("7500" to N_("захват"), "7600" to N_("отказ связи"), "7700" to N_("бедствие"), "7000" to N_("ПВП"),
        "2000" to N_("ППП без кода"))

    /** 13 бит поля ID/AC (C1 A1 C2 A2 C4 A4 X B1 D1 B2 D2 B4 D4) → 0xABCD (по три бита на цифру). */
    fun id13ToHex(id13: Int): Int {
        fun b(k: Int) = (id13 shr k) and 1
        val a = (b(7) shl 2) or (b(9) shl 1) or b(11)
        val bb = (b(1) shl 2) or (b(3) shl 1) or b(5)
        val c = (b(8) shl 2) or (b(10) shl 1) or b(12)
        val d = (b(0) shl 2) or (b(2) shl 1) or b(4)
        return (a shl 12) or (bb shl 8) or (c shl 4) or d
    }

    fun hexToId13(h: Int): Int {
        val a = (h shr 12) and 7; val bb = (h shr 8) and 7; val c = (h shr 4) and 7; val d = h and 7
        var r = 0
        val pairs = intArrayOf(7, a shr 2, 9, (a shr 1) and 1, 11, a and 1, 1, bb shr 2, 3, (bb shr 1) and 1, 5, bb and 1,
            8, c shr 2, 10, (c shr 1) and 1, 12, c and 1, 0, d shr 2, 2, (d shr 1) and 1, 4, d and 1)
        for (k in pairs.indices step 2) r = r or (pairs[k + 1] shl pairs[k])
        return r
    }

    fun decodeSquawk(id13: Int): String {
        val h = id13ToHex(id13)
        return "${(h shr 12) and 7}${(h shr 8) and 7}${(h shr 4) and 7}${h and 7}"
    }

    fun encodeSquawk(code: String): Int {
        val s = code.padStart(4, '0')
        val h = ((s[0] - '0') shl 12) or ((s[1] - '0') shl 8) or ((s[2] - '0') shl 4) or (s[3] - '0')
        return hexToId13(h)
    }

    /** Код Гиллхэма (раскладка 0xABCD как у Mode A) → высота в футах или null (алгоритм dump1090). */
    fun gillhamToFt(h: Int): Int? {
        if ((h.toLong() and 0xFFFF8889L) != 0L || (h and 0x00F0) == 0) return null
        var hund = 0
        if (h and 0x0010 != 0) hund = hund xor 7
        if (h and 0x0020 != 0) hund = hund xor 3
        if (h and 0x0040 != 0) hund = hund xor 1
        if ((hund and 5) == 5) hund = hund xor 2
        if (hund > 5) return null
        var five = 0
        val tab = intArrayOf(0x0002, 0xFF, 0x0004, 0x7F, 0x1000, 0x3F, 0x2000, 0x1F, 0x4000, 0x0F,
            0x0100, 0x07, 0x0200, 0x03, 0x0400, 0x01)
        for (k in tab.indices step 2) if (h and tab[k] != 0) five = five xor tab[k + 1]
        if (five and 1 != 0) hund = 6 - hund
        return (five * 5 + hund - 13) * 100
    }

    /** Поле AC (13 бит) DF0/4/16/20 → футы. M=1 (метры) встречается крайне редко — переводим в футы. */
    fun decodeAc13(ac: Int): Int? {
        if (ac == 0) return null
        if (ac and 0x40 != 0) {  // M-бит: высота в метрах
            val n = ((ac and 0x1F80) shr 1) or (ac and 0x3F)
            return if (n != 0) round(n / FT).toInt() else null
        }
        if (ac and 0x10 != 0) {  // Q-бит: шаг 25 футов
            val n = ((ac and 0x1F80) shr 2) or ((ac and 0x20) shr 1) or (ac and 0x0F)
            return n * 25 - 1000
        }
        return gillhamToFt(id13ToHex(ac))
    }

    /** Поле высоты (12 бит) в позиционном сообщении ADS-B → футы. */
    fun decodeAc12(ac: Int): Int? {
        if (ac == 0) return null
        if (ac and 0x10 != 0) {
            val n = ((ac and 0xFE0) shr 1) or (ac and 0x0F)
            return n * 25 - 1000
        }
        return gillhamToFt(id13ToHex(((ac and 0xFC0) shl 1) or (ac and 0x3F)))
    }

    fun encodeAc12(altFt: Double): Int {
        val n = round((altFt + 1000) / 25.0).toInt().coerceIn(0, 0x7FF)
        return ((n and 0x7F0) shl 1) or 0x10 or (n and 0x0F)
    }

    fun encodeAc13(altFt: Double): Int {
        val n = round((altFt + 1000) / 25.0).toInt().coerceIn(0, 0x7FF)
        return ((n and 0x7E0) shl 2) or ((n and 0x10) shl 1) or 0x10 or (n and 0x0F)
    }

    // ---------------------------------------------------------------- CPR
    const val NZ = 15

    /** Число долготных зон NL(lat). */
    fun cprNl(lat0: Double): Int {
        val lat = abs(lat0)
        if (lat < 1e-9) return 59
        if (abs(lat - 87.0) < 1e-9) return 2
        if (lat > 87.0) return 1
        val a = 1 - cos(PI / (2 * NZ))
        val c = cos(PI / 180.0 * lat)
        val b = c * c
        return floor(2 * PI / acos(1 - a / b)).toInt()
    }

    /** Остаток как в Python (a % b для float): знак делителя. */
    fun pmod(a: Double, b: Double): Double = a - b * floor(a / b)

    /** Глобальное декодирование воздушной позиции. even/odd = (lat_cpr, lon_cpr) целыми 0..131071. */
    fun cprGlobal(evenLat: Int, evenLon: Int, oddLat: Int, oddLon: Int, newestOdd: Boolean): Pair<Double, Double>? {
        val lat0 = evenLat / 131072.0; val lon0 = evenLon / 131072.0
        val lat1 = oddLat / 131072.0; val lon1 = oddLon / 131072.0
        val dlat0 = 360.0 / 60; val dlat1 = 360.0 / 59
        val j = floor(59 * lat0 - 60 * lat1 + 0.5)
        var late = dlat0 * (pmod(j, 60.0) + lat0)
        var lato = dlat1 * (pmod(j, 59.0) + lat1)
        if (late >= 270) late -= 360
        if (lato >= 270) lato -= 360
        if (abs(late) > 90 || abs(lato) > 90) return null
        // за ≤10 с борт не смещается на 11 км: пара от разных мест (подмена GNSS) — не декодируем
        if (abs(late - lato) > 0.1) return null
        val nl = cprNl(late)
        if (nl != cprNl(lato)) return null  // пара из разных широтных зон — ждём следующую
        val lat: Double
        var lon: Double
        if (newestOdd) {
            lat = lato
            val ni = max(nl - 1, 1)
            val m = floor(lon0 * (nl - 1) - lon1 * nl + 0.5)
            lon = (360.0 / ni) * (pmod(m, ni.toDouble()) + lon1)
        } else {
            lat = late
            val ni = max(nl, 1)
            val m = floor(lon0 * (nl - 1) - lon1 * nl + 0.5)
            lon = (360.0 / ni) * (pmod(m, ni.toDouble()) + lon0)
        }
        if (lon >= 180) lon -= 360
        return lat to lon
    }

    /** Локальное декодирование относительно известной точки (до 180 миль в воздухе, 45 миль на земле). */
    fun cprLocal(latCpr: Int, lonCpr: Int, odd: Int, refLat: Double, refLon: Double,
                 surface: Boolean = false): Pair<Double, Double> {
        val span = if (surface) 90.0 else 360.0
        val i = if (odd != 0) 1 else 0
        val yz = latCpr / 131072.0; val xz = lonCpr / 131072.0
        val dlat = span / (60 - i)
        val j = floor(refLat / dlat) + floor(0.5 + pmod(refLat, dlat) / dlat - yz)
        val lat = dlat * (j + yz)
        val dlon = span / max(cprNl(lat) - i, 1)
        val m = floor(refLon / dlon) + floor(0.5 + pmod(refLon, dlon) / dlon - xz)
        var lon = dlon * (m + xz)
        lon = pmod(lon + 180, 360.0) - 180
        return lat to lon
    }

    /** → (lat_cpr, lon_cpr) 17-битные. */
    fun cprEncode(lat: Double, lon: Double, odd: Boolean, surface: Boolean = false): Pair<Int, Int> {
        val span = if (surface) 90.0 else 360.0
        val i = if (odd) 1 else 0
        val dlat = span / (60 - i)
        val yz = floor(131072 * pmod(lat, dlat) / dlat + 0.5)
        val rlat = dlat * (yz / 131072.0 + floor(lat / dlat))
        val dlon = span / max(cprNl(rlat) - i, 1)
        val xz = floor(131072 * pmod(lon, dlon) / dlon + 0.5)
        return (yz.toLong().toInt() and 0x1FFFF) to (xz.toLong().toInt() and 0x1FFFF)
    }

    // ---------------------------------------------------------------- геодезия
    private fun rad(d: Double) = d * PI / 180.0
    private fun deg(r: Double) = r * 180.0 / PI

    fun distanceKm(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val p1 = rad(lat1); val p2 = rad(lat2)
        val dp = p2 - p1; val dl = rad(lon2 - lon1)
        val s1 = sin(dp / 2); val s2 = sin(dl / 2)
        val a = s1 * s1 + cos(p1) * cos(p2) * s2 * s2
        return 2 * 6371.0 * asin(min(1.0, sqrt(a)))
    }

    fun bearingDeg(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val p1 = rad(lat1); val p2 = rad(lat2)
        val dl = rad(lon2 - lon1)
        val y = sin(dl) * cos(p2)
        val x = cos(p1) * sin(p2) - sin(p1) * cos(p2) * cos(dl)
        return pmod(deg(atan2(y, x)) + 360, 360.0)
    }

    fun destination(lat: Double, lon: Double, brg: Double, km: Double): Pair<Double, Double> {
        val d = km / 6371.0
        val p1 = rad(lat); val l1 = rad(lon); val b = rad(brg)
        val p2 = asin(sin(p1) * cos(d) + cos(p1) * sin(d) * cos(b))
        val l2 = l1 + atan2(sin(b) * sin(d) * cos(p1), cos(d) - sin(p1) * sin(p2))
        return deg(p2) to pmod(deg(l2) + 540, 360.0) - 180
    }

    // ---------------------------------------------------------------- декодирование кадра
    private fun surfaceSpeed(mov: Int): Double? = when {
        mov == 0 || mov > 124 -> null
        mov == 1 -> 0.0
        mov <= 8 -> 0.125 * (mov - 1)
        mov <= 12 -> 1.0 + 0.25 * (mov - 9)
        mov <= 38 -> 2.0 + 0.5 * (mov - 13)
        mov <= 93 -> 15.0 + (mov - 39)
        mov <= 108 -> 70.0 + 2 * (mov - 94)
        mov <= 123 -> 100.0 + 5 * (mov - 109)
        else -> 175.0
    }

    private fun decodeMe(me: Long, m: AdsbMsg) {
        fun g(a: Int, b: Int): Int = ((me shr (56 - b)) and ((1L shl (b - a + 1)) - 1)).toInt()  // биты ME с 1
        val tc = g(1, 5)
        m.tc = tc
        if (tc in 1..4) {
            val ca = g(6, 8)
            val cs = String(CharArray(8) { k -> CHARSET[g(9 + 6 * k, 14 + 6 * k)] })
            m.callsign = cs.replace("#", "").trim(' ')
            m.category = CATEGORIES[tc to ca]?.let { tr(it) } ?: if (ca != 0) "${"DCBA"[tc - 1]}$ca" else null
        } else if (tc in 5..8) {
            m.onGround = true
            surfaceSpeed(g(6, 12))?.let { m.gs = it }
            if (g(13, 13) != 0) m.track = g(14, 20) * 360.0 / 128
            m.cpr = CprField(g(22, 22), g(23, 39), g(40, 56), true)
        } else if (tc in 9..18 || tc in 20..22) {
            val alt = decodeAc12(g(9, 20))
            if (alt != null) { if (tc >= 20) m.altGnss = alt else m.alt = alt }
            m.onGround = false
            m.cpr = CprField(g(22, 22), g(23, 39), g(40, 56), false)
        } else if (tc == 19) {
            val st = g(6, 8)
            m.onGround = false
            if (st == 1 || st == 2) {
                val k = if (st == 2) 4 else 1
                val vew = g(15, 24); val vns = g(26, 35)
                if (vew != 0 && vns != 0) {
                    val vx = ((vew - 1) * k * (if (g(14, 14) != 0) -1 else 1)).toDouble()
                    val vy = ((vns - 1) * k * (if (g(25, 25) != 0) -1 else 1)).toDouble()
                    m.gs = hypot(vx, vy)
                    m.track = pmod(deg(atan2(vx, vy)) + 360, 360.0)
                }
            } else if (st == 3 || st == 4) {
                if (g(14, 14) != 0) m.heading = g(15, 24) * 360.0 / 1024
                val asp = g(26, 35)
                if (asp != 0) {
                    val v = (asp - 1) * (if (st == 4) 4 else 1)
                    if (g(25, 25) != 0) m.tas = v else m.ias = v
                }
            }
            val vr = g(38, 46)
            if (vr != 0) m.vr = (vr - 1) * 64 * (if (g(37, 37) != 0) -1 else 1)
            val d = g(50, 56)
            if (d != 0) m.gnssDiff = (d - 1) * 25 * (if (g(49, 49) != 0) -1 else 1)
        } else if (tc == 0) {
            // «нет сведений о местоположении»: борт не знает своих координат (часто — подавление GNSS),
            // но барометрическую высоту передаёт
            decodeAc12(g(9, 20))?.let { m.alt = it }
            m.noPos = true
        } else if (tc == 29 && g(6, 7) == 1) {
            // состояние цели (ADS-B v2): заданная на автопилоте высота, давление QNH, заданный курс
            val sa = g(10, 20)
            if (sa != 0) m.selAlt = (sa - 1) * 32
            val qnh = g(21, 29)
            if (qnh != 0) m.qnh = 800.0 + (qnh - 1) * 0.8
            if (g(30, 30) != 0) m.selHdg = pmod(g(31, 39) * 180.0 / 256, 360.0)
        } else if (tc == 28 && g(6, 8) == 1) {
            m.emergency = g(9, 11)
            m.squawk = decodeSquawk(g(12, 24))
        }
    }

    private fun bds20(mb: Long, m: AdsbMsg) {
        if ((mb shr 48) != 0x20L) return
        val cs = String(CharArray(8) { k -> CHARSET[((mb shr (42 - 6 * k)) and 0x3F).toInt()] })
        val st = cs.trim(' ')
        if ('#' in cs.trimEnd(' ') || st.isEmpty() || !st[0].isLetter()) return
        m.callsign = st
    }

    /** Биты a..b кадра (нумерация с 1, как в стандарте), не больше 63 штук. */
    fun bits(frame: ByteArray, a: Int, b: Int): Long {
        var v = 0L
        for (i in a - 1 until b) v = (v shl 1) or (((frame[i shr 3].toInt() shr (7 - (i and 7))) and 1).toLong())
        return v
    }

    fun hex(frame: ByteArray): String = frame.joinToString("") { "%02X".format(it.toInt() and 0xFF) }

    fun fromHex(s: String): ByteArray = ByteArray(s.length / 2) { s.substring(2 * it, 2 * it + 2).toInt(16).toByte() }

    /** Разбор кадра Mode S (7 или 14 байт). icao — адрес для кадров «адрес/чётность» (синдром). */
    fun decodeFrame(frame: ByteArray, icao: Int? = null): AdsbMsg {
        val big = frame.size * 8
        fun g(a: Int, b: Int): Int = bits(frame, a, b).toInt()
        var df = g(1, 5)
        if (df >= 24) df = 24
        val m = AdsbMsg(df, hex(frame))
        if (df == 17 || df == 18 || df == 11) {
            m.icao = g(9, 32)
            val ca = g(6, 8)
            if (df != 18) {
                if (ca == 4) m.onGround = true
                else if (ca == 5) m.onGround = false
            }
            if ((df == 17 || df == 18) && big == 112) decodeMe(bits(frame, 33, 88), m)
            return m
        }
        m.icao = icao ?: syndrome(frame)
        if (df == 0 || df == 16) {
            if (g(6, 6) != 0) m.onGround = true
            decodeAc13(g(20, 32))?.let { m.alt = it }
        } else if (df == 4 || df == 20) {
            val fs = g(6, 8)
            if (fs == 1 || fs == 3) m.onGround = true
            else if (fs == 0 || fs == 2) m.onGround = false
            decodeAc13(g(20, 32))?.let { m.alt = it }
        } else if (df == 5 || df == 21) {
            val fs = g(6, 8)
            if (fs == 1 || fs == 3) m.onGround = true
            else if (fs == 0 || fs == 2) m.onGround = false
            m.squawk = decodeSquawk(g(20, 32))
        }
        if ((df == 20 || df == 21) && big == 112) bds20(bits(frame, 33, 88), m)
        return m
    }

    /** Короткое обозначение типа: «DF17/TC19», «DF4» … */
    fun msgType(m: AdsbMsg): String = "DF${m.df}" + (m.tc?.let { "/TC$it" } ?: "")

    /**
     * Число как f"{v:.Nf}" в Python: точное двоичное значение, округление к чётному, знак у отрицательного нуля;
     * [plus] — формат «+», [group] — разряды через пробел (как _num: f"{v:,.0f}" с пробелами).
     */
    fun pyF(v: Double, d: Int, plus: Boolean = false, group: Boolean = false): String {
        var s = BigDecimal(v).abs().setScale(d, RoundingMode.HALF_EVEN).toPlainString()
        if (group) {
            val dot = s.indexOf('.')
            val ip = if (dot < 0) s else s.substring(0, dot)
            val fp = if (dot < 0) "" else s.substring(dot)
            val sb = StringBuilder()
            for (k in ip.indices) {
                if (k > 0 && (ip.length - k) % 3 == 0) sb.append(' ')
                sb.append(ip[k])
            }
            s = sb.toString() + fp
        }
        val neg = v < 0 || (v == 0.0 && 1.0 / v < 0)
        return (if (neg) "-" else if (plus) "+" else "") + s
    }

    private fun num(v: Double) = pyF(v, 0, group = true)

    /** Расшифровка сообщения по-русски для ленты журнала. */
    fun describe(m: AdsbMsg, pos: Pair<Double, Double>? = null, metric: Boolean = true): String {
        fun alt(ft: Int) = if (metric) tr("{v} м", "v" to num(ft * FT)) else tr("{v} фут", "v" to num(ft.toDouble()))
        val parts = ArrayList<String>()
        if (!m.callsign.isNullOrEmpty()) parts.add(tr("позывной {cs}", "cs" to m.callsign))
        if (!m.category.isNullOrEmpty() && (m.tc ?: 0) in 1..4) parts.add(tr("категория {cat}", "cat" to m.category))
        if (m.onGround == true && m.tc in 5..8) parts.add(tr("на земле"))
        m.alt?.let { parts.add(tr("высота {alt}", "alt" to alt(it))) }
        m.altGnss?.let { parts.add(tr("высота GNSS {alt}", "alt" to alt(it))) }
        val gs = m.gs; val track = m.track; val heading = m.heading
        if (gs != null) {
            var t = tr("скорость {v} км/ч", "v" to pyF(gs * KT_KMH, 0))
            if (track != null) t += ", " + tr("путевой угол {deg}°", "deg" to pyF(track, 0))
            parts.add(t)
        } else if (track != null) parts.add(tr("путевой угол {deg}°", "deg" to pyF(track, 0)))
        for ((k, v) in listOf("IAS" to m.ias, "TAS" to m.tas)) {
            if (v == null) continue
            var t = tr("скорость {v} км/ч {kind}", "v" to pyF(v * KT_KMH, 0), "kind" to k)
            if (heading != null) t += ", " + tr("курс {deg}°", "deg" to pyF(heading, 0))
            parts.add(t)
        }
        if (heading != null && m.ias == null && m.tas == null) parts.add(tr("курс {deg}°", "deg" to pyF(heading, 0)))
        m.vr?.let {
            parts.add(if (metric) tr("вертикальная {v} м/с", "v" to pyF(it * FT / 60, 1, plus = true))
                      else tr("вертикальная {v} фут/мин", "v" to pyF(it.toDouble(), 0, plus = true)))
        }
        m.squawk?.let { sq ->
            val note = SQUAWK_NOTES[sq]
            parts.add(if (note != null) tr("сквок {sq} ({note})", "sq" to sq, "note" to tr(note)) else tr("сквок {sq}", "sq" to sq))
        }
        m.emergency?.let { if (it != 0) parts.add(tr("аварийный статус {code}", "code" to it)) }
        if (m.selAlt != null || m.selHdg != null || m.qnh != null) {
            val ap = ArrayList<String>()
            m.selAlt?.let { ap.add(tr("высота {alt}", "alt" to alt(it))) }
            m.selHdg?.let { ap.add(tr("курс {deg}°", "deg" to pyF(it, 0))) }
            m.qnh?.let { ap.add(tr("QNH {v} гПа", "v" to pyF(it, 1))) }
            parts.add(tr("автопилот: {items}", "items" to ap.joinToString(", ")))
        }
        if (m.noPos) parts.add(tr("нет координат (TC0)"))
        val cpr = m.cpr
        if (cpr != null) {
            val note = m.posNote
            if (pos != null) parts.add(tr("позиция {pos}", "pos" to "${pyF(pos.first, 4)}, ${pyF(pos.second, 4)}") +
                (if (!note.isNullOrEmpty()) " — $note" else ""))
            else if (!note.isNullOrEmpty()) parts.add(note)
            else parts.add(if (cpr.odd != 0) tr("CPR нечётный (ждём пару)") else tr("CPR чётный (ждём пару)"))
        }
        if (parts.isEmpty()) {
            parts.add(when {
                m.df == 11 -> tr("ответ на общий запрос (DF11)")
                m.tc == 31 -> tr("эксплуатационный статус (TC31)")
                else -> tr("служебное сообщение")
            })
        }
        return parts.joinToString(", ")
    }

    /** Удобство для тестов и отладки: hex-строка → (разобранное сообщение, синдром). */
    fun decodeHex(hexstr: String): Pair<AdsbMsg, Int> {
        val b = fromHex(hexstr)
        return decodeFrame(b) to syndrome(b)
    }

    // ---------------------------------------------------------------- кодирование (демо и тесты)
    private fun putBits(dst: ByteArray, start: Int, value: Long, n: Int) {
        for (k in 0 until n) {
            if ((value shr (n - 1 - k)) and 1L != 0L) {
                val i = start + k
                dst[i shr 3] = (dst[i shr 3].toInt() or (0x80 ushr (i and 7))).toByte()
            }
        }
    }

    private fun frame(head: Long, headLen: Int, payload: Long, payloadLen: Int, parityXor: Int = 0): ByteArray {
        val nb = (headLen + payloadLen) / 8
        val out = ByteArray(nb + 3)
        putBits(out, 0, head, headLen)
        putBits(out, headLen, payload, payloadLen)
        val p = crc24(out, nb) xor parityXor
        out[nb] = (p shr 16).toByte(); out[nb + 1] = (p shr 8).toByte(); out[nb + 2] = p.toByte()
        return out
    }

    fun makeDf17(icao: Int, me: Long, ca: Int = 5): ByteArray =
        frame(((17L shl 3) or ca.toLong()) shl 24 or icao.toLong(), 32, me, 56)

    fun makeDf11(icao: Int, ca: Int = 5): ByteArray = frame((11L shl 3) or ca.toLong(), 8, icao.toLong(), 24)

    fun makeDf4(icao: Int, altFt: Double, fs: Int = 0): ByteArray =
        frame((4L shl 3) or fs.toLong(), 8, encodeAc13(altFt).toLong(), 24, icao)

    fun makeDf5(icao: Int, squawk: String, fs: Int = 0): ByteArray =
        frame((5L shl 3) or fs.toLong(), 8, encodeSquawk(squawk).toLong(), 24, icao)

    fun meIdent(callsign: String, tc: Int = 4, ca: Int = 3): Long {
        val cs = (callsign.uppercase() + "        ").substring(0, 8)
        var v = ((tc shl 3) or ca).toLong()
        for (ch in cs) {
            val k = CHARSET.indexOf(ch)
            v = (v shl 6) or (if (k > 0) k else 32).toLong()
        }
        return v
    }

    fun meAirbornePos(lat: Double, lon: Double, altFt: Double, odd: Boolean, tc: Int = 11): Long {
        val (yz, xz) = cprEncode(lat, lon, odd)
        return (tc.toLong() shl 51) or (encodeAc12(altFt).toLong() shl 36) or ((if (odd) 1L else 0L) shl 34) or
            (yz.toLong() shl 17) or xz.toLong()
    }

    fun meSurfacePos(lat: Double, lon: Double, odd: Boolean, gsKt: Double, track: Double, tc: Int = 7): Long {
        val mov = when {
            gsKt <= 0.01 -> 1
            gsKt < 1 -> 1 + round(gsKt / 0.125).toInt()
            gsKt < 2 -> 9 + ((gsKt - 1) / 0.25).toInt()
            gsKt < 15 -> 13 + ((gsKt - 2) / 0.5).toInt()
            gsKt < 70 -> 39 + (gsKt - 15).toInt()
            else -> min(123, 94 + ((gsKt - 70) / 2).toInt())
        }
        val trk = round(track * 128 / 360.0).toInt() and 0x7F
        val (yz, xz) = cprEncode(lat, lon, odd, surface = true)
        return (tc.toLong() shl 51) or (mov.toLong() shl 44) or (1L shl 43) or (trk.toLong() shl 36) or
            ((if (odd) 1L else 0L) shl 34) or (yz.toLong() shl 17) or xz.toLong()
    }

    fun meVelocity(gsKt: Double, track: Double, vrFpm: Double): Long {
        val vx = gsKt * sin(rad(track))
        val vy = gsKt * cos(rad(track))
        val vew = min(round(abs(vx)).toInt() + 1, 1023)
        val vns = min(round(abs(vy)).toInt() + 1, 1023)
        val vr = min(round(abs(vrFpm) / 64.0).toInt() + 1, 511)
        var v = (19L shl 3) or 1L
        v = (v shl 1) shl 1                          // IC, IFR
        v = (v shl 3) or 0L                          // NUCv
        v = (v shl 1) or (if (vx < 0) 1L else 0L)
        v = (v shl 10) or vew.toLong()
        v = (v shl 1) or (if (vy < 0) 1L else 0L)
        v = (v shl 10) or vns.toLong()
        v = (v shl 1) or 1L                          // источник VR — барометрический
        v = (v shl 1) or (if (vrFpm < 0) 1L else 0L)
        v = (v shl 9) or vr.toLong()
        v = v shl 2                                  // резерв
        v = v shl 8                                  // разница GNSS–баро не передаём
        return v
    }

    private val PREAMBLE = floatArrayOf(1f, 0f, 1f, 0f, 0f, 0f, 0f, 1f, 0f, 1f, 0f, 0f, 0f, 0f, 0f, 0f)

    /** Кадр → последовательность полумикросекундных слотов 0/1 (преамбула + PPM). */
    fun frameSlots(frame: ByteArray): FloatArray {
        val nb = frame.size * 8
        val out = FloatArray(16 + 2 * nb)
        PREAMBLE.copyInto(out)
        for (i in 0 until nb) {
            val b = (frame[i shr 3].toInt() shr (7 - (i and 7))) and 1
            out[16 + 2 * i] = b.toFloat()
            out[17 + 2 * i] = (1 - b).toFloat()
        }
        return out
    }

    /**
     * Огибающая кадра на частоте fs с дробным сдвигом (0..1 отсчёта): прямоугольные импульсы 0,5 мкс,
     * проинтегрированные по интервалу отсчёта, — как их видит АЦП.
     */
    fun renderEnvelope(frame: ByteArray, fs: Int, frac: Double = 0.0): FloatArray {
        val up = 10
        val perHalf = round(fs * up * 0.5e-6).toInt()
        val slots = frameSlots(frame)
        val off = (frac * up).toInt() % up
        val total = off + slots.size * perHalf + 2 * up
        val k = total / up
        val out = FloatArray(k)
        for (j in 0 until k) {
            var s = 0f
            for (q in j * up until (j + 1) * up) {
                val h = q - off
                if (h >= 0 && h < slots.size * perHalf) s += slots[h / perHalf]
            }
            out[j] = s / up
        }
        return out
    }

    /** Уровень сигнала кадра (dBFS) по 8 отсчётам импульсов преамбулы, как в Demodulator._demod. */
    internal fun levelDb(lvl: Double) = 20 * log10(max(lvl, 1e-6))
}
