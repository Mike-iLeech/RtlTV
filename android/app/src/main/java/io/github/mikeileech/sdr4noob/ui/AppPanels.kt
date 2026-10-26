package io.github.mikeileech.sdr4noob.ui

import android.app.Activity
import android.app.AlertDialog
import android.content.SharedPreferences
import android.os.Handler
import android.os.Looper
import android.widget.LinearLayout
import io.github.mikeileech.sdr4noob.dsp.Adsb
import io.github.mikeileech.sdr4noob.radio.AdsbWorker
import io.github.mikeileech.sdr4noob.radio.AircraftInfo
import io.github.mikeileech.sdr4noob.radio.AprsRx
import io.github.mikeileech.sdr4noob.radio.AprsWorker
import io.github.mikeileech.sdr4noob.radio.PlaneRow
import io.github.mikeileech.sdr4noob.radio.RadioEngine
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.roundToInt

/** Дом (место приёмника) для колец дальности и расстояний — общий для ADS-B и APRS, как «Моё место» на ПК. */
object Home {
    fun get(p: SharedPreferences) = p.getFloat("home_lat", 55.7558f).toDouble() to p.getFloat("home_lon", 37.6173f).toDouble()
    fun set(p: SharedPreferences, lat: Double, lon: Double) =
        p.edit().putFloat("home_lat", lat.toFloat()).putFloat("home_lon", lon.toFloat()).apply()
}

private fun fmt(x: Double, d: Int) = String.format(Locale.ROOT, "%.${d}f", x).replace('.', ',')

/**
 * Панель APRS в окне радио (как окно APRS на ПК): декодер AFSK 1200 на звуке NFM, станции с координатами — на
 * карте OpenStreetMap с путями, внизу — последние пакеты; «Журнал…» — все пакеты сеанса.
 */
class AprsPanel(private val act: Activity, private val prefs: SharedPreferences, private val toast: (String) -> Unit) {
    companion object {
        val PRESETS = listOf(
            "APRS Россия и Европа — 144,800 МГц" to 144_800_000L,
            "APRS США — 144,390 МГц" to 144_390_000L,
            "APRS Австралия — 145,175 МГц" to 145_175_000L,
            "МКС, цифровой ретранслятор — 145,825 МГц" to 145_825_000L,
        )
    }

    private val ui = Handler(Looper.getMainLooper())
    val view: LinearLayout = Ui.vbox(act, 0)
    private val map = MapView(act)
    private val status = Ui.text(act, "Ожидание пакетов APRS…", 13f, Ui.DIM)
    private val last = Ui.text(act, "", 12f).apply { maxLines = 4 }
    private val log = ArrayList<AprsRx>()
    private var worker: AprsWorker? = null
    private var engine: RadioEngine? = null
    @Volatile private var alive = false
    private val time = SimpleDateFormat("HH:mm:ss", Locale.ROOT)

    init {
        val (la, lo) = Home.get(prefs)
        map.setHome(la, lo, intArrayOf(10, 30, 60))
        map.setCenter(la, lo, 9)
        map.onItemClick = { key -> showStation(key) }
        view.addView(map, Ui.lp(h = 0, weight = 1f))
        view.addView(status)
        view.addView(last)
        val r = Ui.hbox(act)
        r.addView(Ui.button(act, "Журнал…") { showLog() }, Ui.lp(0, weight = 1f))
        r.addView(Ui.button(act, "Дом = центр карты") {
            val (a, b) = map.center(); Home.set(prefs, a, b); map.setHome(a, b, intArrayOf(10, 30, 60))
            toast("Место приёмника: ${fmt(a, 4)}, ${fmt(b, 4)}")
        }, Ui.lp(0, weight = 1f))
        view.addView(r)
    }

    fun start(e: RadioEngine) {
        stop()
        alive = true
        val w = AprsWorker({ got -> ui.post { if (alive) onPackets(got) } }, { msg -> ui.post { if (alive) toast(msg) } })
        worker = w; engine = e
        e.audioTap = w::feed
    }

    fun stop() {
        alive = false
        engine?.audioTap = null; engine = null
        worker?.stop(); worker = null
    }

    private fun onPackets(got: List<AprsRx>) {
        log.addAll(got)
        while (log.size > 500) log.removeAt(0)
        val w = worker ?: return
        val st = w.stations
        map.setItems("aprs", st.map { MapItem(it.name, it.lat, it.lon, MapItem.Kind.POINT, label = it.name, color = 0xFF4DD2FF.toInt()) })
        map.setTracks("aprs", st.filter { it.track.size > 1 }.map { MapTrack(it.track, 0xFF4DD2FF.toInt(), 1.5f) })
        status.text = "Пакетов: ${w.total} · станций на карте: ${st.size} · последний — ${time.format(Date(got.last().timeMs))}"
        last.text = log.takeLast(4).reversed().joinToString("\n") { "${it.packet.src}: ${it.text}" }
    }

    private fun showStation(name: String) {
        val s = worker?.stations?.firstOrNull { it.name == name } ?: return
        val (hl, ho) = Home.get(prefs)
        val d = io.github.mikeileech.sdr4noob.dsp.Aprs.distanceKm(hl, ho, s.lat, s.lon)
        AlertDialog.Builder(act).setTitle(s.name)
            .setMessage("${s.last.text}\n\n${fmt(s.lat, 5)}, ${fmt(s.lon, 5)} · ${fmt(d, 1)} км от вас" +
                (s.symbol?.let { "\nЗначок: $it" } ?: "") + "\nПакетов: ${s.count} · последний ${time.format(Date(s.last.timeMs))}" +
                "\n\n${s.last.packet.raw}")
            .setPositiveButton("Закрыть", null).show()
    }

    private fun showLog() {
        if (log.isEmpty()) { toast("Пакетов пока нет"); return }
        val items = log.reversed().map { "${time.format(Date(it.timeMs))}  ${it.packet.src}\n${it.text}" }.toTypedArray()
        AlertDialog.Builder(act).setTitle("Журнал APRS (${log.size})").setItems(items, null)
            .setPositiveButton("Закрыть", null).show()
    }
}

/**
 * Панель ADS-B в окне радио (как окно «Самолёты» на ПК): приёмник на 1090 МГц, 2,4 МГц обзора, декодер
 * Mode S на сыром IQ; борта — на карте со следом, ближние — списком. Касание борта — карточка.
 */
class AdsbPanel(private val act: Activity, private val prefs: SharedPreferences, private val toast: (String) -> Unit) {
    private val view0: LinearLayout = Ui.vbox(act, 0)
    val view: LinearLayout get() = view0
    private val map = MapView(act)
    private val status = Ui.text(act, "Ожидание сообщений ADS-B…", 13f, Ui.DIM)
    private val list = Ui.text(act, "", 12f).apply { maxLines = 5; typeface = android.graphics.Typeface.MONOSPACE }
    private var worker: AdsbWorker? = null
    private var engine: RadioEngine? = null
    private val ui = Handler(Looper.getMainLooper())

    init {
        val (la, lo) = Home.get(prefs)
        map.setHome(la, lo, intArrayOf(50, 100, 200))
        map.setCenter(la, lo, 7)
        map.onItemClick = { key -> map.selected = key; showPlane(key) }
        view0.addView(map, Ui.lp(h = 0, weight = 1f))
        view0.addView(status)
        view0.addView(list)
        val r = Ui.hbox(act)
        r.addView(Ui.button(act, "Борта…") { showPlanes() }, Ui.lp(0, weight = 1f))
        r.addView(Ui.button(act, "Дом = центр карты") {
            val (a, b) = map.center(); Home.set(prefs, a, b); map.setHome(a, b, intArrayOf(50, 100, 200))
            worker?.setHome(a, b)
            toast("Место приёмника: ${fmt(a, 4)}, ${fmt(b, 4)}")
        }, Ui.lp(0, weight = 1f))
        view0.addView(r)
    }

    fun start(e: RadioEngine) {
        stop()
        val w = AdsbWorker(e.sampleRate, Home.get(prefs)) { msg -> ui.post { if (worker != null) toast(msg) } }
        worker = w; engine = e
        e.iqTap = w::feed
    }

    fun stop() {
        engine?.iqTap = null; engine = null
        worker?.stop(); worker = null
    }

    private fun altM(ft: Int?) = ft?.let { (it * Adsb.FT).roundToInt() }
    private fun kmh(kt: Double?) = kt?.let { (it * 1.852).roundToInt() }

    /** Вызывается раз в ~0,5 с из потока интерфейса. */
    fun refresh() {
        val w = worker ?: return
        engine?.let { if (it.sampleRate != w.rate) w.rate = it.sampleRate }   // запись IQ задаёт свою частоту
        val rows = w.planes
        val fresh = rows.filter { it.ageS < Adsb.STALE_S }
        map.setItems("planes", fresh.filter { it.lat != null && it.lon != null }.map {
            MapItem(it.hex, it.lat!!, it.lon!!, MapItem.Kind.PLANE, heading = it.track,
                label = (it.callsign ?: it.hex) + (altM(it.alt)?.let { a -> "\n$a м" } ?: ""),
                color = if (it.suspect) 0xFFFF6B6B.toInt() else if (it.onGround) 0xFFB0B0B0.toInt() else 0xFFFFD84D.toInt(),
                trail = it.trail)
        })
        val lv = w.levels
        val state = when (lv?.state) { null -> "—"; "мало" -> "слабый сигнал"; "перегрузка" -> "перегрузка — уменьшите усиление"; else -> lv.state }
        status.text = "Бортов: ${fresh.size} (на карте ${fresh.count { it.lat != null }}) · сообщений ${w.proc.msgs} · уровень: $state"
        list.text = fresh.take(5).joinToString("\n") { line(it) }
    }

    private fun line(p: PlaneRow) = String.format(Locale.ROOT, "%-8s %s %7s %6s %7s",
        p.callsign ?: "—", p.hex, altM(p.alt)?.let { "$it м" } ?: "", kmh(p.gs)?.let { "$it км/ч" } ?: "",
        p.dist?.let { fmt(it, 0) + " км" } ?: "")

    private fun showPlane(hex: String) {
        val p = worker?.planes?.firstOrNull { it.hex == hex } ?: return
        val c = AircraftInfo.card(p.icao, p.callsign)
        val txt = buildString {
            append("ICAO ${p.hex}")
            c.country?.let { append(" · $it") }
            append('\n')
            p.callsign?.let { append("Позывной: $it") }
            c.airline?.let { append(" · $it") }
            if (c.isPrivate == true) append(" (частный или служебный борт)")
            append('\n')
            altM(p.alt)?.let { append("Высота: $it м\n") }
            kmh(p.gs)?.let { append("Скорость: $it км/ч\n") }
            p.track?.let { append("Курс: ${it.roundToInt()}°\n") }
            p.vr?.let { append("Вертикальная: ${fmt(it * Adsb.FT / 60, 1)} м/с\n") }
            p.squawk?.let { append("Сквок: $it\n") }
            p.dist?.let { append("Расстояние: ${fmt(it, 1)} км\n") }
            if (p.onGround) append("На земле\n")
            if (p.suspect) append("Координаты подозрительны (возможна подмена GNSS)\n")
            append("Сообщений: ${p.msgs}, последнее ${p.ageS.roundToInt()} с назад")
        }
        AlertDialog.Builder(act).setTitle(p.callsign ?: p.hex).setMessage(txt).setPositiveButton("Закрыть", null).show()
    }

    private fun showPlanes() {
        val rows = worker?.planes.orEmpty()
        if (rows.isEmpty()) { toast("Бортов пока нет"); return }
        AlertDialog.Builder(act).setTitle("Борта (${rows.size})")
            .setItems(rows.map { line(it) + if (it.ageS >= Adsb.STALE_S) "  (давно)" else "" }.toTypedArray()) { _, i ->
                val p = rows[i]
                if (p.lat != null && p.lon != null) map.setCenter(p.lat, p.lon)
                map.selected = p.hex
                showPlane(p.hex)
            }
            .setPositiveButton("Закрыть", null).show()
    }
}
