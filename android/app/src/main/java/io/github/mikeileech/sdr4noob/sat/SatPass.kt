package io.github.mikeileech.sdr4noob.sat

import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URI
import kotlin.math.PI
import kotlin.math.acos
import kotlin.math.asin
import kotlin.math.atan2
import kotlin.math.ceil
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.hypot
import kotlin.math.sin
import kotlin.math.sqrt

/*
 * Орбиты спутников — перенос rtltv/satpass.py: TLE (разбор, кэш, загрузка через подставляемый [TleFetcher]),
 * SGP4 ([Sgp4]), TEME → ECEF по GMST, подспутниковая точка, трасса, азимут/угол места/дальность/скорость
 * сближения, доплеровский сдвиг, пролёты над домом (AOS/LOS/максимум), круг видимости.
 *
 * Время везде — unix-секунды (Double), углы — градусы, расстояния — км, скорости — км/с.
 */

/** Остаток от деления как `%` в Python (знак делителя). */
internal fun pmod(a: Double, b: Double): Double {
    var m = a % b
    if (m != 0.0 && (b < 0) != (m < 0)) m += b
    return m
}

private const val RAD = PI / 180.0
private const val DEG = 180.0 / PI

/** Набор TLE одного спутника (как satpass.Tle). */
data class Tle(val name: String, val norad: Int, val line1: String, val line2: String) {
    /** Эпоха элементов, unix-время. */
    val epochUnix: Double
        get() = Sgp4.twoline2rv(line1, line2).let { (it.jdSatEpoch + it.jdSatEpochF - 2440587.5) * 86400.0 }
}

object TleText {
    const val TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"
    const val TLE_URL_CATNR = "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=tle"
    val GROUPS = listOf("weather", "stations", "amateur")
    const val MAX_AGE = 24 * 3600.0
    const val USER_AGENT = "SDR4NOOB/1.1 (personal SDR receiver; Android)"

    fun groupUrl(group: String) = TLE_URL.replace("{group}", group)
    fun catnrUrl(norad: Int) = TLE_URL_CATNR.replace("{norad}", norad.toString())

    /** Текст 3LE/2LE → {norad: Tle} (порядок — как в тексте; повтор номера заменяет запись). Битые наборы пропускаются. */
    fun parse(text: String): LinkedHashMap<Int, Tle> {
        val out = LinkedHashMap<Int, Tle>()
        val lines = text.split(Regex("\r\n|\r|\n")).filter { it.isNotBlank() }.map { it.trimEnd() }
        var i = 0
        while (i < lines.size - 1) {
            val name: String?
            val l1: String
            val l2: String
            val step: Int
            if (lines[i].startsWith("1 ") && lines[i + 1].startsWith("2 ")) {
                name = null; l1 = lines[i]; l2 = lines[i + 1]; step = 2
            } else if (i < lines.size - 2 && lines[i + 1].startsWith("1 ") && lines[i + 2].startsWith("2 ")) {
                name = lines[i].trim(); l1 = lines[i + 1]; l2 = lines[i + 2]; step = 3
            } else {
                i += 1
                continue
            }
            i += step
            val norad = try {
                if (l1.length < 7) continue
                l1.substring(2, 7).trim().toInt().also { Sgp4.twoline2rv(l1, l2) }
            } catch (e: IllegalArgumentException) {   // в т. ч. NumberFormatException
                continue
            }
            out[norad] = Tle(name ?: "NORAD $norad", norad, l1, l2)
        }
        return out
    }
}

/** Загрузка текста по URL; исключение IOException — нет сети/ответа. На Android вызывать не из главного потока. */
fun interface TleFetcher {
    @Throws(IOException::class)
    fun fetch(url: String, timeoutS: Double): String
}

/** Загрузка по HTTP(S) с подписью программы (как urllib в satpass._download). */
object HttpTleFetcher : TleFetcher {
    override fun fetch(url: String, timeoutS: Double): String {
        val c = URI(url).toURL().openConnection() as HttpURLConnection
        try {
            c.connectTimeout = (timeoutS * 1000).toInt()
            c.readTimeout = (timeoutS * 1000).toInt()
            c.setRequestProperty("User-Agent", TleText.USER_AGENT)
            if (c.responseCode !in 200..299) throw IOException("HTTP ${c.responseCode}")
            return c.inputStream.use { String(it.readBytes(), Charsets.US_ASCII) }
        } finally {
            c.disconnect()
        }
    }
}

/**
 * Кэш TLE в папке [dir] (<группа>.txt, catnr_<номер>.txt), обновление не чаще [TleText.MAX_AGE]; без сети —
 * кэш любой давности (как satpass.load_tle). [now] — текущее unix-время (в тестах подставляется).
 */
class TleLoader(
    private val dir: File,
    private val fetcher: TleFetcher = HttpTleFetcher,
    private val now: () -> Double = { System.currentTimeMillis() / 1000.0 },
) {
    class Result(val tles: LinkedHashMap<Int, Tle>, val status: String)

    fun cachePath(group: String) = File(dir, "$group.txt")

    /** Когда последний раз скачивались TLE (unix-время самого свежего файла кэша) или null. */
    fun cacheTime(groups: List<String> = TleText.GROUPS): Double? =
        groups.map { cachePath(it) }.filter { it.isFile }.maxOfOrNull { it.lastModified() / 1000.0 }

    private fun readGroup(url: String, path: File, force: Boolean, offline: Boolean, timeout: Double): Pair<String?, String> {
        val age = if (path.isFile) now() - path.lastModified() / 1000.0 else null
        if (!offline && (force || age == null || age > TleText.MAX_AGE)) {
            try {
                val text = fetcher.fetch(url, timeout)
                if (TleText.parse(text).isNotEmpty()) {
                    path.writeText(text.replace("\r\n", "\n"), Charsets.US_ASCII)
                    return text to "обновлено"
                }
            } catch (e: IOException) {
                // нет сети — берём кэш
            }
        }
        if (age == null) return null to "нет данных"
        return path.readText(Charsets.US_ASCII) to "кэш ${Math.rint(age / 3600).toLong()} ч"
    }

    /** {norad: Tle} и строка состояния; [norads] — досылать по номеру, если нет в группах (плюс [TRACKED]). */
    fun load(groups: List<String> = TleText.GROUPS, force: Boolean = false, offline: Boolean = false,
             timeout: Double = 10.0, norads: Collection<Int> = emptyList()): Result {
        dir.mkdirs()
        val tles = LinkedHashMap<Int, Tle>()
        val notes = ArrayList<String>()
        for (g in groups) {
            val (text, note) = readGroup(TleText.groupUrl(g), cachePath(g), force, offline, timeout)
            notes.add("$g: $note")
            if (text != null) tles.putAll(TleText.parse(text))
        }
        for (n in TRACKED.keys.toList() + norads.filter { it !in TRACKED }) {
            if (n !in tles) {
                val (text, _) = readGroup(TleText.catnrUrl(n), cachePath("catnr_$n"), force, offline, timeout)
                if (text != null) tles.putAll(TleText.parse(text))
            }
        }
        return Result(tles, notes.joinToString("; "))
    }
}

/** Спутники режима: NORAD → (подпись, цвет, частота Гц, примечание) — satpass.TRACKED. */
data class Tracked(val name: String, val color: String, val freq: Double, val note: String)

val TRACKED: Map<Int, Tracked> = linkedMapOf(
    57166 to Tracked("Метеор-М №2-3", "#ffb84d", 137.9e6, "LRPT 137,900 МГц, 72k OQPSK"),
    59051 to Tracked("Метеор-М №2-4", "#ff6a4d", 137.9e6, "LRPT 137,900 МГц, 72k OQPSK"),
    25544 to Tracked("МКС", "#7fd1ff", 145.8e6, "SSTV/голос 145,800 МГц — режим SSTV"),
)

// ---------------------------------------------------------------- геометрия

object Geo {
    const val C_LIGHT = 299792.458          // км/с
    const val R_EARTH = 6378.137            // км (WGS-84)
    const val F_EARTH = 1 / 298.257223563
    const val OMEGA_E = 7.2921150e-5        // рад/с
    private const val E2 = F_EARTH * (2 - F_EARTH)

    /** unix-время → юлианская дата: (целая часть …,5; доля суток). */
    fun jdFromUnix(t: Double): DoubleArray {
        val jd = t / 86400.0 + 2440587.5
        val jd0 = floor(jd - 0.5) + 0.5
        return doubleArrayOf(jd0, jd - jd0)
    }

    /** Звёздное время Гринвича (рад), IAU-82 (как satpass.gmst). */
    fun gmst(jd: Double, fr: Double): Double {
        val tut = (jd + fr - 2451545.0) / 36525.0
        val g = 67310.54841 + (876600.0 * 3600 + 8640184.812866) * tut + 0.093104 * (tut * tut) -
                6.2e-6 * (tut * tut * tut)
        return pmod((pmod(g, 86400.0) / 240.0) * RAD, 2 * PI)
    }

    /** Геодезические координаты (°, °, км над эллипсоидом WGS-84) → ECEF, км. */
    fun geodeticToEcef(lat: Double, lon: Double, hKm: Double = 0.0): DoubleArray {
        val la = lat * RAD
        val lo = lon * RAD
        val sl = sin(la)
        val n = R_EARTH / sqrt(1 - E2 * (sl * sl))
        return doubleArrayOf((n + hKm) * cos(la) * cos(lo), (n + hKm) * cos(la) * sin(lo), (n * (1 - E2) + hKm) * sl)
    }

    /** ECEF, км → (широта °, долгота °, высота км). */
    fun ecefToGeodetic(x: Double, y: Double, z: Double): DoubleArray {
        val lon = atan2(y, x)
        val p = hypot(x, y)
        var lat = atan2(z, p * (1 - E2))
        for (k in 0 until 5) {
            val s = sin(lat)
            val n = R_EARTH / sqrt(1 - E2 * (s * s))
            val h = p / cos(lat) - n
            lat = atan2(z, p * (1 - E2 * n / (n + h)))
        }
        val s = sin(lat)
        val n = R_EARTH / sqrt(1 - E2 * (s * s))
        val h = p / cos(lat) - n
        return doubleArrayOf(lat * DEG, lon * DEG, h)
    }

    /** Граница зоны, где спутник на высоте [altKm] виден с дома выше [minEl] (точки lat, lon). */
    fun visibilityCircle(lat: Double, lon: Double, altKm: Double = 830.0, minEl: Double = 0.0, n: Int = 120): List<DoubleArray> {
        val e = minEl * RAD
        val lam = acos(R_EARTH * cos(e) / (R_EARTH + altKm)) - e
        val la = lat * RAD
        val lo = lon * RAD
        return (0..n).map { k ->
            val b = 2 * PI * k / n
            val la2 = asin(sin(la) * cos(lam) + cos(la) * sin(lam) * cos(b))
            val lo2 = lo + atan2(sin(b) * sin(lam) * cos(la), cos(lam) - sin(la) * sin(la2))
            doubleArrayOf(la2 * DEG, pmod(lo2 * DEG + 540, 360.0) - 180)
        }
    }

    /** Азимут (°), угол места (°) и дальность (км) на геостационар с долготой [satLon] — elektro.geo_look. */
    fun geoLook(lat: Double, lon: Double, satLon: Double): DoubleArray {
        val rGeo = 42164.0
        val la = lat * RAD
        val dl = (satLon - lon) * RAD
        val sx = rGeo * cos(dl)
        val sy = rGeo * sin(dl)
        val up = sx * cos(la) - R_EARTH
        val north = -sx * sin(la)
        val east = sy
        val rng = sqrt(up * up + north * north + east * east)
        val el = atan2(up, hypot(north, east)) * DEG
        val az = pmod(atan2(east, north) * DEG, 360.0)
        return doubleArrayOf(az, el, rng)
    }
}

/** Пролёт над наблюдателем (как satpass.Pass): времена — unix, углы — градусы. */
data class Pass(
    val norad: Int, val name: String,
    val aos: Double, val los: Double, val tca: Double, val maxEl: Double,
    val azAos: Double, val azLos: Double, val azTca: Double,
) {
    val duration: Double get() = los - aos
}

/** Положение и скорость в ECEF (км, км/с); при ошибке SGP4 — NaN. */
class EcefState(val r: DoubleArray, val v: DoubleArray)

/** Азимут °, угол места °, дальность км, скорость сближения км/с (+ удаляется). */
class Look(val az: Double, val el: Double, val range: Double, val rangeRate: Double)

class Orbit(val tle: Tle) {
    val sat: Sgp4 = Sgp4.twoline2rv(tle.line1, tle.line2)

    /** TEME → ECEF для unix-времени [t]. */
    fun ecef(t: Double): EcefState {
        val (jd, fr) = Geo.jdFromUnix(t).let { it[0] to it[1] }
        val st = sat.propagate(jd, fr)
        if (st.error != 0) {
            return EcefState(DoubleArray(3) { Double.NaN }, DoubleArray(3) { Double.NaN })
        }
        val r = st.r
        val v = st.v
        val g = Geo.gmst(jd, fr)
        val c = cos(g)
        val s = sin(g)
        val x = c * r[0] + s * r[1]
        val y = -s * r[0] + c * r[1]
        val vx = c * v[0] + s * v[1] + Geo.OMEGA_E * y
        val vy = -s * v[0] + c * v[1] - Geo.OMEGA_E * x
        return EcefState(doubleArrayOf(x, y, r[2]), doubleArrayOf(vx, vy, v[2]))
    }

    /** Подспутниковая точка: (широта °, долгота °, высота км). */
    fun subpoint(t: Double): DoubleArray {
        val r = ecef(t).r
        return Geo.ecefToGeodetic(r[0], r[1], r[2])
    }

    /** Период обращения, мин (из среднего движения TLE). */
    val periodMin: Double get() = sat.noKozai.let { if (it > 0) 2 * PI / it else 100.0 }

    /** Трасса на [before]/[after] оборотов назад/вперёд от [t0] (для геостационара — одна точка). */
    fun orbitTrack(t0: Double, before: Double = 1.0, after: Double = 1.0, points: Int = 240): List<DoubleArray> {
        val p = periodMin
        if (p > 1300) return track(t0, 0.0, 0.0, 60.0)
        val step = maxOf(10.0, p * 60 * (before + after) / points)
        return track(t0, p * before, p * after, step)
    }

    /** Подспутниковые точки (lat, lon) от t0 − before до t0 + after (минуты) с шагом [stepS] секунд. */
    fun track(t0: Double, beforeMin: Double = 50.0, afterMin: Double = 100.0, stepS: Double = 30.0): List<DoubleArray> {
        val start = -beforeMin * 60
        val stop = afterMin * 60 + 1
        val n = maxOf(0, ceil((stop - start) / stepS).toInt())
        val out = ArrayList<DoubleArray>(n)
        for (i in 0 until n) {
            val sp = subpoint(t0 + (start + i * stepS))
            if (sp[0].isFinite()) out.add(doubleArrayOf(sp[0], sp[1]))
        }
        return out
    }

    /** Направление с точки наблюдения (lat, lon °, высота км). */
    fun look(t: Double, lat: Double, lon: Double, hKm: Double = 0.0): Look {
        val st = ecef(t)
        val r = st.r
        val v = st.v
        val o = Geo.geodeticToEcef(lat, lon, hKm)
        val d0 = r[0] - o[0]
        val d1 = r[1] - o[1]
        val d2 = r[2] - o[2]
        val la = lat * RAD
        val lo = lon * RAD
        val sl = sin(la); val cl = cos(la); val so = sin(lo); val co = cos(lo)
        val e = -so * d0 + co * d1
        val n = -sl * co * d0 - sl * so * d1 + cl * d2
        val u = cl * co * d0 + cl * so * d1 + sl * d2
        val rng = sqrt(e * e + n * n + u * u)
        val el = atan2(u, hypot(e, n)) * DEG
        val az = pmod(atan2(e, n) * DEG, 360.0)
        val rr = (d0 * v[0] + d1 * v[1] + d2 * v[2]) / rng
        return Look(az, el, rng, rr)
    }

    /** Доплеровский сдвиг, Гц (принимаемая частота = [freq] + сдвиг). */
    fun doppler(t: Double, freq: Double, lat: Double, lon: Double, hKm: Double = 0.0): Double =
        -freq * look(t, lat, lon, hKm).rangeRate / Geo.C_LIGHT

    /** Пролёты за [hours] часов от [t0] с максимумом не ниже [minEl] (как Orbit.passes). */
    fun passes(lat: Double, lon: Double, t0: Double = System.currentTimeMillis() / 1000.0, hours: Double = 24.0,
               minEl: Double = 0.0, hKm: Double = 0.0): List<Pass> {
        val step = 20.0
        // окно с запасом 30 мин после конца, чтобы пролёт на границе не обрезался
        val base = t0 - 20 * 60
        val n = ceil((hours * 3600 + 50 * 60) / step).toInt()
        val ts = DoubleArray(n) { base + it * step }
        val up = BooleanArray(n) { val el = look(ts[it], lat, lon, hKm).el; !el.isNaN() && el > 0 }
        val out = ArrayList<Pass>()
        var i = 0
        while (i < n) {
            if (!up[i]) { i++; continue }
            var j = i
            while (j < n && up[j]) j++
            val aos = edge(if (i > 0) ts[i - 1] else ts[i], ts[i], lat, lon, hKm, rising = true)
            val los = edge(ts[j - 1], if (j < n) ts[j] else ts[j - 1], lat, lon, hKm, rising = false)
            val fine = if (los - aos > 2) DoubleArray(ceil(los - aos).toInt()) { aos + it * 1.0 } else doubleArrayOf(aos, los)
            var k = -1
            var mx = Double.NaN
            var azK = 0.0
            var az0 = Double.NaN
            var azN = Double.NaN
            for (q in fine.indices) {
                val lk = look(fine[q], lat, lon, hKm)
                if (q == 0) az0 = lk.az
                if (q == fine.size - 1) azN = lk.az
                if (!lk.el.isNaN() && (k < 0 || lk.el > mx)) { k = q; mx = lk.el; azK = lk.az }
            }
            // как np.nanargmax: угол места не посчитан ни в одной точке — ошибка (all_passes пропустит спутник)
            if (k < 0) throw ArithmeticException("угол места не определён на пролёте")
            if (j < n && i > 0 && mx >= minEl && los > t0 && aos < t0 + hours * 3600) {
                out.add(Pass(tle.norad, tle.name, aos, los, fine[k], mx, az0, azN, azK))
            }
            i = j
        }
        return out
    }

    private fun edge(ta0: Double, tb0: Double, lat: Double, lon: Double, hKm: Double, rising: Boolean): Double {
        var ta = ta0
        var tb = tb0
        if (tb <= ta) return ta
        repeat(12) {
            val m = 0.5 * (ta + tb)
            val el = look(m, lat, lon, hKm).el
            if ((el > 0) == rising) tb = m else ta = m
        }
        return 0.5 * (ta + tb)
    }
}

/** Пролёты нескольких спутников, по времени AOS (как satpass.all_passes); спутник с ошибкой расчёта пропускается. */
fun allPasses(orbits: Collection<Orbit>, lat: Double, lon: Double, t0: Double = System.currentTimeMillis() / 1000.0,
              hours: Double = 24.0, minEl: Double = 0.0): List<Pass> {
    val out = ArrayList<Pass>()
    for (o in orbits) {
        try {
            out.addAll(o.passes(lat, lon, t0, hours, minEl))
        } catch (e: ArithmeticException) {
            continue
        }
    }
    out.sortBy { it.aos }
    return out
}
