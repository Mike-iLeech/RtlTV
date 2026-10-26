package io.github.mikeileech.sdr4noob.dsp

import io.github.mikeileech.sdr4noob.i18n.tr
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.round
import kotlin.math.sin

/** Последнее CPR-сообщение борта: (lat_cpr, lon_cpr, время, на земле). */
data class CprRec(val lat: Int, val lon: Int, val t: Double, val surface: Boolean)

/** Точка следа борта на карте (lat, lon, высота). */
data class TrailPt(val lat: Double, val lon: Double, val alt: Int?)

/** Точка трека в истории сеанса (lat, lon, высота, время). */
data class HistPt(val lat: Double, val lon: Double, val alt: Int?, val t: Double)

/** Борт в таблице — перенос Aircraft из rtltv/adsb.py. */
class Aircraft(val icao: Int, now: Double) {
    var callsign: String? = null
    var category: String? = null
    var squawk: String? = null
    var emergency: Int? = null
    var alt: Int? = null
    var altGnss: Int? = null
    var gs: Double? = null
    var track: Double? = null
    var heading: Double? = null
    var ias: Int? = null
    var tas: Int? = null
    var vr: Int? = null
    var onGround: Boolean? = null
    var selAlt: Int? = null
    var qnh: Double? = null
    var selHdg: Double? = null
    var noPos = false
    var lat: Double? = null
    var lon: Double? = null
    var posT = 0.0
    var posSrc: String? = null           // pair | local | single | surface — как получена позиция
    var confirmed = false                // позиция подтверждена парой чётн./нечётн. (или выведена из такой)
    var rejected = 0                     // сколько позиций отброшено
    var rawLat: Double? = null           // последняя отброшенная («сырая») позиция
    var rawLon: Double? = null
    var rawT = 0.0
    var rawNote: String? = null
    var suspect = false                  // последние координаты подозрительны (вероятна подмена GNSS)
    var jumpStreak = 0
    var even: CprRec? = null
    var odd: CprRec? = null
    var msgs = 0
    var first = now
    var last = now
    var signal: Double? = null
    val trail = ArrayDeque<TrailPt>()    // не больше TRAIL_MAX точек
    var dist: Double? = null
    var lastEs = 0.0
    val dfSeen = sortedSetOf<Int>()

    val hex: String get() = "%06X".format(icao)

    internal fun addTrail(p: TrailPt) {
        trail.addLast(p)
        while (trail.size > TRAIL_MAX) trail.removeFirst()
    }

    companion object { const val TRAIL_MAX = 400 }
}

/** Запись истории сеанса по борту (словарь history[icao] в Python). */
class HistEntry(val icao: Int, val hex: String, now: Double) {
    var callsign: String? = null
    var first = now
    var last = now
    var altMin: Int? = null
    var altMax: Int? = null
    var msgs = 0
    var hadPos = false
    var noPos = false
    var maxDist: Double? = null
    var suspect = false
    val track = ArrayList<HistPt>()
    val raw = ArrayList<HistPt>()
    /** Запись из прошлого сеанса (загружена с диска). */
    var prev = false
}

/**
 * Таблица бортов — перенос Tracker из rtltv/adsb.py. keepS — сколько держать борт после последнего сообщения
 * (null — весь сеанс); борт, не слышный дольше STALE_S, считается «давно не слышен», но из таблицы не пропадает.
 * history — сводка по всем бортам сеанса (не удаляется; можно передать общий словарь).
 */
class AdsbTracker(var home: Pair<Double, Double>? = null, var keepS: Double? = Adsb.STALE_S,
                  history: MutableMap<Int, HistEntry>? = null) {
    val planes = LinkedHashMap<Int, Aircraft>()
    val history: MutableMap<Int, HistEntry> = history ?: LinkedHashMap()
    var maxRange = 0.0
    var positions = 0
    var rejected = 0

    fun known(now: Double = nowS()): Set<Int> =
        planes.filterValues { now - it.lastEs < Adsb.STALE_S }.keys.toHashSet()

    fun expire(now: Double = nowS()) {
        val keep = keepS ?: return
        planes.entries.removeAll { now - it.value.last > keep }
    }

    private fun history(a: Aircraft, m: AdsbMsg, now: Double) {
        val h = history.getOrPut(a.icao) { HistEntry(a.icao, a.hex, now) }
        h.last = now
        h.msgs += 1
        h.prev = false
        if (!a.callsign.isNullOrEmpty()) h.callsign = a.callsign
        m.alt?.let { alt ->
            h.altMin = h.altMin?.let { min(it, alt) } ?: alt
            h.altMax = h.altMax?.let { max(it, alt) } ?: alt
        }
        if (a.noPos) h.noPos = true
        if (a.suspect) h.suspect = true
        if (a.suspect && a.rawT == now) thinAppend(h.raw, HistPt(a.rawLat!!, a.rawLon!!, a.alt, now), HIST_RAW_MAX)
        val lat = a.lat; val lon = a.lon
        if (lat != null && lon != null && a.posT == now) {
            thinAppend(h.track, HistPt(lat, lon, a.alt, now), HIST_TRACK_MAX)
            h.hadPos = true
            a.dist?.let { h.maxDist = max(h.maxDist ?: 0.0, it) }
        }
    }

    /** Учесть разобранное сообщение. Возвращает борт (или null). */
    fun apply(m: AdsbMsg, now: Double = nowS(), signal: Double? = null): Aircraft? {
        val icao = m.icao ?: return null
        var a = planes[icao]
        if (a == null) {
            if (m.df != 11 && m.df != 17 && m.df != 18) return null
            a = Aircraft(icao, now)
            planes[icao] = a
        }
        a.msgs += 1
        a.last = now
        a.dfSeen.add(m.df)
        if (m.df == 11 || m.df == 17 || m.df == 18) a.lastEs = now
        if (signal != null) a.signal = a.signal?.let { 0.7 * it + 0.3 * signal } ?: signal
        m.callsign?.let { if (it.isNotEmpty()) a.callsign = it }
        m.category?.let { if (it.isNotEmpty()) a.category = it }
        m.alt?.let { a.alt = it }
        m.altGnss?.let { a.altGnss = it }
        m.gs?.let { a.gs = it }
        m.track?.let { a.track = it }
        m.heading?.let { a.heading = it }
        m.ias?.let { a.ias = it }
        m.tas?.let { a.tas = it }
        m.vr?.let { a.vr = it }
        m.squawk?.let { if (it.isNotEmpty()) a.squawk = it }
        m.emergency?.let { a.emergency = it }
        m.selAlt?.let { a.selAlt = it }
        m.qnh?.let { a.qnh = it }
        m.selHdg?.let { a.selHdg = it }
        m.onGround?.let { a.onGround = it }
        if (m.noPos) a.noPos = true
        val cpr = m.cpr
        if (cpr != null) {
            a.noPos = false
            val note = position(a, cpr, now)
            if (note != null) m.posNote = note
        }
        history(a, m, now)
        return a
    }

    /** Декодировать CPR и принять или отбросить позицию. Возвращает пояснение для журнала (или null). */
    private fun position(a: Aircraft, cpr: CprField, now: Double): String? {
        val odd = cpr.odd; val latC = cpr.lat; val lonC = cpr.lon; val surface = cpr.surface
        val rec = CprRec(latC, lonC, now, surface)
        if (odd != 0) a.odd = rec else a.even = rec
        var pos: Pair<Double, Double>? = null
        var src: String? = null
        val fresh = a.lat != null && now - a.posT < Adsb.STALE_S
        val home = home
        if (surface) {
            val ref = if (fresh) a.lat!! to a.lon!! else home
            if (ref != null) {
                pos = Adsb.cprLocal(latC, lonC, odd, ref.first, ref.second, surface = true); src = "surface"
            }
        } else {
            val e = a.even; val o = a.odd
            if (e != null && o != null && !e.surface && !o.surface && abs(e.t - o.t) < Adsb.CPR_PAIR_S) {
                pos = Adsb.cprGlobal(e.lat, e.lon, o.lat, o.lon, newestOdd = odd != 0); src = "pair"
            }
            if (pos == null && fresh) {
                pos = Adsb.cprLocal(latC, lonC, odd, a.lat!!, a.lon!!); src = "local"
            }
            if (pos == null && home != null) {
                // одиночное сообщение — относительно приёмника (верно, если борт ближе ~330 км)
                val p1 = Adsb.cprLocal(latC, lonC, odd, home.first, home.second)
                if (Adsb.distanceKm(home.first, home.second, p1.first, p1.second) <= Adsb.SINGLE_RANGE_KM) {
                    pos = p1; src = "single"
                }
            }
        }
        if (pos == null) return null
        val (lat, lon) = pos
        if (home != null) {
            val d = Adsb.distanceKm(home.first, home.second, lat, lon)
            if (d > Adsb.MAX_RANGE_KM) {
                if (src == "pair" && !a.confirmed && a.lat != null) {
                    // пара опровергла позицию, взятую по одиночному сообщению от приёмника, — убираем её
                    a.lat = null; a.lon = null; a.dist = null; a.posSrc = null
                    a.trail.clear()
                }
                return reject(a, lat, lon, now, tr("позиция отброшена: {d} км от приёмника — вероятно, подмена GNSS",
                    "d" to Adsb.pyF(d, 0)))
            }
        }
        // скачок проверяем от подтверждённой позиции; одиночную (src=single) пара вправе поправить
        if (fresh && a.confirmed && src != "surface") {
            val dt = now - a.posT
            val jump = Adsb.distanceKm(a.lat!!, a.lon!!, lat, lon)
            if (jump > 5.0 + 0.45 * dt) {  // быстрее ~1600 км/ч — так самолёты не летают
                val rl = a.rawLat; val rn = a.rawLon
                if (rl != null && rn != null && now - a.rawT < 30 &&
                    Adsb.distanceKm(rl, rn, lat, lon) < 5.0 + 0.45 * (now - a.rawT)) a.jumpStreak += 1
                else a.jumpStreak = 1
                if (a.jumpStreak < Adsb.JUMP_ACCEPT) {
                    val note = tr("скачок {d} км за {t} с — отброшена", "d" to Adsb.pyF(jump, 0), "t" to Adsb.pyF(max(dt, 0.1), 0))
                    val streak = a.jumpStreak
                    reject(a, lat, lon, now, note)
                    a.jumpStreak = streak
                    return note
                }
                src = "pair"  // новое место подтверждено несколькими сообщениями подряд
            }
        }
        a.jumpStreak = 0
        a.lat = lat; a.lon = lon; a.posT = now; a.posSrc = src
        if (src == "pair") a.confirmed = true
        else if (src == "single") a.confirmed = false
        a.suspect = false
        positions += 1
        val tl = a.trail.lastOrNull()
        if (tl == null || Adsb.distanceKm(tl.lat, tl.lon, lat, lon) > 0.25) a.addTrail(TrailPt(lat, lon, a.alt))
        if (home != null) {
            val d = Adsb.distanceKm(home.first, home.second, lat, lon)
            a.dist = d
            maxRange = max(maxRange, d)
        }
        if (src == "single") return tr("по одиночному сообщению (от приёмника, ждём пару)")
        return null
    }

    private fun reject(a: Aircraft, lat: Double, lon: Double, now: Double, note: String): String {
        a.rejected += 1
        rejected += 1
        a.rawLat = lat; a.rawLon = lon; a.rawT = now; a.rawNote = note
        a.suspect = true
        return note
    }

    companion object {
        const val HIST_TRACK_MAX = 1000   // точек трека борта в истории сеанса (дальше прореживаем вдвое)
        const val HIST_RAW_MAX = 300      // точек «подозрительных» координат

        fun nowS() = System.currentTimeMillis() / 1000.0

        /** Трек для истории: точка не чаще, чем через 1 км или 60 с; при переполнении — прореживание вдвое. */
        internal fun thinAppend(pts: MutableList<HistPt>, p: HistPt, cap: Int) {
            if (pts.isNotEmpty()) {
                val q = pts[pts.size - 1]
                if (p.t - q.t < 60 && Adsb.distanceKm(q.lat, q.lon, p.lat, p.lon) < 1.0) return
            }
            pts.add(p)
            if (pts.size > cap) {
                val n = pts.size
                val last = pts[n - 1]
                val kept = (0 until n step 2).map { pts[it] }
                pts.clear(); pts.addAll(kept)
                if ((n - 1) % 2 != 0) pts.add(last)
            }
        }
    }
}

/** Сводка уровней для индикатора «мало / норма / перегрузка» (dB полной шкалы). */
data class AdsbLevels(val peakDb: Double, val clip: Double, val noiseDb: Double, val framesP90: Double?,
                      val framesMax: Double?, val framesMed: Double?, val state: String)

/** Запись ленты журнала: (время, ICAO hex, позывной, сообщение, позиция|null). */
class AdsbLogEntry(val t: Double, val hex: String, val callsign: String?, val msg: AdsbMsg,
                   val pos: Pair<Double, Double>?)

/**
 * Демодулятор → таблица бортов: перенос AdsbProcessor.process() из rtltv/adsb.py без собственного потока
 * (поток и очередь блоков — забота движка приложения). Вызывать [process] из одного потока; [drainLog],
 * [levelStats], [snapshot] — из любого (общее состояние под замком).
 */
class AdsbProcessor(fs: Int = Adsb.FS, home: Pair<Double, Double>? = null,
                    var onMessage: ((AdsbMsg, Aircraft, Double) -> Unit)? = null,
                    keepS: Double? = Adsb.STALE_S, history: MutableMap<Int, HistEntry>? = null) {
    var demod = AdsbDemodulator(fs)
        private set
    val tracker = AdsbTracker(home, keepS, history)
    private val feedLog = ArrayDeque<AdsbLogEntry>()
    private val lock = Any()
    var blocks = 0
        private set
    var msgs = 0
        private set
    val byDf = HashMap<Int, Int>()
    /** Доля времени, занятая декодером (0..1, скользящая). */
    var busy = 0.0
        private set
    // уровни АЦП и принятых кадров для индикатора «мало / норма / перегрузка»
    private var lvPeak = 0.0
    private var lvClip = 0L
    private var lvSamples = 0L
    private val lvFrames = ArrayDeque<Double>()
    private var lvLast: AdsbLevels? = null

    fun setRate(fs: Int) { demod = AdsbDemodulator(fs) }
    fun setHome(lat: Double, lon: Double) { tracker.home = lat to lon }
    fun setKeep(keepS: Double?) { tracker.keepS = keepS }

    /** Забрать накопленные с прошлого раза сообщения для ленты интерфейса. */
    fun drainLog(): List<AdsbLogEntry> = synchronized(lock) {
        val out = feedLog.toList()
        feedLog.clear()
        out
    }

    /** Сводка уровней с прошлого вызова: пик АЦП, доля отсечек, шум, уровни кадров (dB полной шкалы). */
    fun levelStats(): AdsbLevels = synchronized(lock) {
        val last = lvLast
        if (lvSamples == 0L && last != null) return last  // с прошлого вызова блоков не было — прежняя сводка
        val fr = lvFrames.sorted()
        val p90 = if (fr.isNotEmpty()) fr[(0.9 * (fr.size - 1)).toInt()] else null
        val fmax = if (fr.isNotEmpty()) fr[(0.98 * (fr.size - 1)).toInt()] else null  // «сильные кадры»: 98-й процентиль
        val med = if (fr.isNotEmpty()) fr[fr.size / 2] else null
        val clip = lvClip.toDouble() / max(lvSamples, 1L)
        val st = AdsbLevels(20 * log10(max(lvPeak, 1e-6)), clip, 20 * log10(max(demod.noise, 1e-6)), p90, fmax, med,
            levelState(clip, fmax, p90))
        lvPeak = 0.0; lvClip = 0; lvSamples = 0
        lvLast = st
        st
    }

    fun historySnapshot(): List<HistEntry> = synchronized(lock) { tracker.history.values.toList() }

    fun snapshot(): List<Aircraft> = synchronized(lock) { tracker.planes.values.toList() }

    /** Блок IQ → кадры (и обновлённая таблица). [now] — время блока, с (по умолчанию — текущее). */
    fun process(re: FloatArray, im: FloatArray, n: Int = re.size, now: Double = AdsbTracker.nowS()): List<AdsbFrame> {
        val t0 = System.nanoTime()
        var peak = lvPeak
        var clip = 0L
        for (i in 0 until n) {
            val a = abs(re[i]); val b = abs(im[i])
            if (a > peak) peak = a.toDouble()
            if (b > peak) peak = b.toDouble()
            if (a >= 0.985f) clip++   // 8-битный АЦП упёрся в 0 или 255
            if (b >= 0.985f) clip++
        }
        val frames: List<AdsbFrame>
        synchronized(lock) {
            lvPeak = peak; lvClip += clip; lvSamples += 2L * n
            frames = demod.feed(re, im, n, tracker.known(now))
            for (f in frames) {
                lvFrames.addLast(f.signal)
                while (lvFrames.size > 400) lvFrames.removeFirst()
            }
            for (f in frames) {
                val m = Adsb.decodeFrame(f.frame, f.icao)
                val a = tracker.apply(m, now, f.signal) ?: continue
                msgs += 1
                byDf[m.df] = (byDf[m.df] ?: 0) + 1
                val pos = if (m.cpr != null && a.posT == now && a.lat != null) a.lat!! to a.lon!! else null
                feedLog.addLast(AdsbLogEntry(now, a.hex, a.callsign, m, pos))
                while (feedLog.size > 20000) feedLog.removeFirst()
                try { onMessage?.invoke(m, a, now) } catch (_: Exception) {}  // журнал не должен ронять приём
            }
            tracker.expire(now)
            blocks += 1
        }
        val dt = (System.nanoTime() - t0) / 1e9
        val blk = max(n, 1).toDouble() / demod.fs
        busy = 0.9 * busy + 0.1 * (dt / blk)
        return frames
    }

    companion object {
        /**
         * «перегрузка» — сильные кадры выше −3 dBFS или отсечки АЦП; «мало» — кадры слабее −30 dBFS.
         * Значение — идентификатор состояния (его сравнивают), не текст для показа: переводить при показе.
         */
        fun levelState(clip: Double, framesMax: Double?, framesP90: Double?): String = when {
            clip > 1e-4 || (framesMax != null && framesMax > -3.0) -> "перегрузка"  // noqa: i18n
            framesP90 == null || framesP90 < -30.0 -> "мало"  // noqa: i18n
            else -> "норма"  // noqa: i18n
        }
    }
}

/**
 * Синтетический эфир 1090 МГц — перенос DemoSource из rtltv/adsb.py (без темпа реального времени и потока):
 * борта летят по маршрутам вокруг дома, передают DF17 (позывной, позиция чётная/нечётная, скорость), DF4/DF5
 * (высота, сквок) и DF11. Среди них: борта без координат (TC0), борт с подменой GNSS, борт только с чётными CPR,
 * борт на земле в аэропорту. [render] выдаёт блок IQ, уже прошедший 8-битный АЦП, как у донгла.
 * Случайные числа — java.util.Random (последовательность не совпадает с Python, поведение — то же).
 */
class AdsbDemo(var fs: Int = Adsb.FS, val home: Pair<Double, Double> = 55.7558 to 37.6173, val nplanes: Int = 14,
               seed: Long? = null, val noiseSigma: Double = 0.035, val nNoPos: Int = 3,
               val silent: Pair<Double, Double>? = 8.0 to 128.0, val nSpoof: Int = 1, val nEven: Int = 1) {
    private val rnd = if (seed != null) java.util.Random(seed) else java.util.Random()
    private val rng = if (seed != null) java.util.Random(seed * 31 + 7) else java.util.Random()
    val planes = ArrayList<Plane>()
    var tStream = 0.0
        private set
    var sent = 0
        private set

    private fun uniform(a: Double, b: Double) = a + (b - a) * rnd.nextDouble()

    inner class Plane(k: Int) {
        val icao: Int = if (k % 3 != 0) 0x150000 + rnd.nextInt(0x10000) else 0x400000 + rnd.nextInt(0x400000)
        var callsign = CALLS[k % CALLS.size]
        val squawk = String(CharArray(4) { '0' + rnd.nextInt(8) })
        var ground = false
        var lat = 0.0
        var lon = 0.0
        var track = 0.0
        var gs = 0.0
        var alt = 0.0
        var vr = 0.0
        var foff = 0.0
        var phase = 0.0
        var turn = 0.0
        val next = HashMap<String, Double>()
        var noPos = false
        var spoof = false
        var evenOnly = false
        var spoofOff = Triple(0.0, 0.0, -1e9)   // (азимут, км, время выбора)

        init {
            spawn(first = true)
            foff = uniform(-120e3, 120e3)
            phase = uniform(0.0, 2 * PI)
        }

        fun spawn(first: Boolean = false) {
            val brg = uniform(0.0, 360.0)
            val dist = if (first) uniform(15.0, 260.0) else uniform(250.0, 330.0)
            val p = Adsb.destination(home.first, home.second, brg, dist)
            lat = p.first; lon = p.second
            track = if (!first) Adsb.pmod(brg + 180 + uniform(-70.0, 70.0), 360.0) else uniform(0.0, 360.0)
            gs = uniform(250.0, 480.0)
            val alts = doubleArrayOf(uniform(3000.0, 15000.0), uniform(24000.0, 39000.0), uniform(30000.0, 41000.0))
            alt = alts[rnd.nextInt(3)]
            val vrs = doubleArrayOf(0.0, 0.0, 0.0, uniform(-2200.0, -800.0), uniform(900.0, 2500.0))
            vr = vrs[rnd.nextInt(5)]
        }

        fun step(dt: Double) {
            if (ground) {
                track = Adsb.pmod(track + 6.0 * dt, 360.0)  // рулит по кругу
            } else {
                if (rnd.nextDouble() < dt / 40) {
                    val turns = doubleArrayOf(0.0, 0.0, uniform(-1.5, 1.5))
                    turn = turns[rnd.nextInt(3)]
                }
                track = Adsb.pmod(track + turn * dt, 360.0)
                alt = max(1500.0, min(41000.0, alt + vr * dt / 60))
                if (alt == 1500.0 || alt == 41000.0) vr = 0.0
            }
            val km = gs * Adsb.KT_KMH * dt / 3600
            val p = Adsb.destination(lat, lon, track, km)
            lat = p.first; lon = p.second
            if (!ground && Adsb.distanceKm(home.first, home.second, lat, lon) > (if (evenOnly) 200 else 340)) {
                if (evenOnly) track = Adsb.bearingDeg(lat, lon, home.first, home.second)
                else spawn()
            }
        }
    }

    fun open() {
        planes.clear()
        val n0 = nplanes + nNoPos
        for (k in 0 until n0 + nSpoof + nEven) planes.add(Plane(k))
        for (p in planes.subList(nplanes, n0)) p.noPos = true
        for (p in planes.subList(n0, n0 + nSpoof)) p.spoof = true
        for (p in planes.subList(n0 + nSpoof, planes.size)) {
            p.evenOnly = true
            val q = Adsb.destination(home.first, home.second, uniform(0.0, 360.0), uniform(40.0, 150.0))
            p.lat = q.first; p.lon = q.second
        }
        val ap = AIRPORTS[0]
        val g = planes[nplanes - 1]
        g.ground = true; g.lat = ap.first; g.lon = ap.second + 0.01; g.gs = 12.0; g.alt = 0.0; g.vr = 0.0
        g.callsign = "AFL1490"
    }

    /** Подмена GNSS: 15 с борт «стоит» за 1200 км от приёмника, 15 с — скачет на 60–120 км от истины. */
    private fun spoofed(p: Plane): Pair<Double, Double> {
        val t = tStream
        if (Adsb.pmod(t, 30.0) < 15) return Adsb.destination(home.first, home.second, 250.0, 1200.0)
        if (t - p.spoofOff.third > 2.0) p.spoofOff = Triple(uniform(0.0, 360.0), uniform(60.0, 120.0), t)
        return Adsb.destination(p.lat, p.lon, p.spoofOff.first, p.spoofOff.second)
    }

    private fun frames(p: Plane, kind: String): ByteArray? {
        when (kind) {
            "pos" -> {
                if (p.noPos) return Adsb.makeDf17(p.icao, Adsb.encodeAc12(p.alt).toLong() shl 36)  // TC0: только высота
                val odd = rnd.nextDouble() < 0.5 && !p.evenOnly
                if (p.spoof) {
                    val (lat, lon) = spoofed(p)
                    return Adsb.makeDf17(p.icao, Adsb.meAirbornePos(lat, lon, p.alt, odd))
                }
                if (p.ground) return Adsb.makeDf17(p.icao, Adsb.meSurfacePos(p.lat, p.lon, odd, p.gs, p.track), ca = 4)
                return Adsb.makeDf17(p.icao, Adsb.meAirbornePos(p.lat, p.lon, p.alt, odd))
            }
            "vel" -> return if (p.ground) null else Adsb.makeDf17(p.icao, Adsb.meVelocity(p.gs, p.track, p.vr))
            "id" -> return Adsb.makeDf17(p.icao, Adsb.meIdent(p.callsign, 4, 3), ca = if (p.ground) 4 else 5)
            "df4" -> return Adsb.makeDf4(p.icao, p.alt, fs = if (p.ground) 1 else 0)
            "df5" -> return Adsb.makeDf5(p.icao, p.squawk, fs = if (p.ground) 1 else 0)
        }
        return Adsb.makeDf11(p.icao, ca = if (p.ground) 4 else 5)
    }

    /** Блок IQ из [n] отсчётов в [re]/[im] (уже через 8-битный АЦП, как у донгла). */
    fun render(n: Int, re: FloatArray, im: FloatArray) {
        val dt = n.toDouble() / fs
        val t0 = tStream
        val sr = DoubleArray(n); val si = DoubleArray(n)
        val maxlen = (130e-6 * fs).toInt() + 4
        for ((pi, p) in planes.withIndex()) {
            p.step(dt)
            val sil = silent
            val mute = pi == 0 && sil != null && sil.first <= t0 && t0 < sil.second
            val d = max(Adsb.distanceKm(home.first, home.second, p.lat, p.lon), 3.0)
            val amp = min(0.45, 0.6 * 45.0 / d)  // ближние борта ~−5 dBFS: демо показывает «норму», а не перегрузку
            for ((kind, per) in PERIODS) {
                var tn = p.next.getOrPut(kind) { t0 + uniform(0.0, per) }
                while (tn < t0 + dt) {
                    val start = ((tn - t0) * fs).toInt()
                    var fr = frames(p, kind)
                    if (mute) fr = null
                    if (fr != null && start + maxlen < n) {
                        val env = Adsb.renderEnvelope(fr, fs, rnd.nextDouble())
                        val a = amp * uniform(0.8, 1.2)
                        val ph = p.phase + 2 * PI * p.foff * tn
                        for (k in env.indices) {
                            val w = ph + 2 * PI * p.foff * (k.toDouble() / fs)
                            sr[start + k] += a * env[k] * cos(w)
                            si[start + k] += a * env[k] * sin(w)
                        }
                        sent += 1
                    } else if (fr != null) {
                        tn = t0 + dt + 1e-5
                        break
                    }
                    tn += per * uniform(0.8, 1.2)
                }
                p.next[kind] = tn
            }
        }
        tStream += dt
        for (i in 0 until n) {
            re[i] = adc(sr[i] + (rng.nextGaussian() * noiseSigma).toFloat())
            im[i] = adc(si[i] + (rng.nextGaussian() * noiseSigma).toFloat())
        }
    }

    private fun adc(x: Double): Float {
        val raw = round(127.4 + 128.0 * x).coerceIn(0.0, 255.0)
        return (raw.toFloat() - 127.4f) / 128f
    }

    companion object {
        val PERIODS = linkedMapOf("pos" to 0.5, "vel" to 0.5, "id" to 5.0, "df4" to 1.2, "df5" to 1.5, "df11" to 1.0)
        val AIRPORTS = listOf(55.9726 to 37.4146, 55.4088 to 37.9063, 55.5915 to 37.2615, 55.6031 to 38.0800)
        val CALLS = listOf("AFL1124", "SBI2057", "UTA415", "SDM6311", "AFL31", "SDM6555", "NWS4011", "SVR113", "AZV327",
            "AFL2442", "THY4KC", "UAE132", "RSY9603", "KAR245", "SBI1290", "CCA910", "AFL1650", "SBI8812",
            "UTA261", "PBD447")
    }
}
