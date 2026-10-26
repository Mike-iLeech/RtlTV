package io.github.mikeileech.sdr4noob.ui

import android.app.Activity
import android.app.AlertDialog
import android.content.SharedPreferences
import android.graphics.Color
import android.os.Handler
import android.os.Looper
import android.widget.LinearLayout
import io.github.mikeileech.sdr4noob.dsp.Modulation
import io.github.mikeileech.sdr4noob.radio.LrptWorker
import io.github.mikeileech.sdr4noob.radio.RadioEngine
import io.github.mikeileech.sdr4noob.radio.Storage
import io.github.mikeileech.sdr4noob.sat.RxPlan
import io.github.mikeileech.sdr4noob.sat.Geo
import io.github.mikeileech.sdr4noob.sat.Orbit
import io.github.mikeileech.sdr4noob.sat.Pass
import io.github.mikeileech.sdr4noob.sat.Sat
import io.github.mikeileech.sdr4noob.sat.SatCatalog
import io.github.mikeileech.sdr4noob.sat.TleLoader
import io.github.mikeileech.sdr4noob.sat.Tx
import io.github.mikeileech.sdr4noob.sat.allPasses
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.concurrent.thread
import kotlin.math.abs
import kotlin.math.roundToInt
import kotlin.math.roundToLong

/**
 * Панель «Спутники» в окне радио (как окно спутников на ПК): TLE с celestrak (кэш в телефоне, без сети — кэш
 * любой давности), положения и трасса на карте, пролёты над местом приёмника, выбор передатчика и настройка
 * с поправкой доплера раз в секунду. Голос и FM-спутники слышны сразу; снимки «Метеора» (LRPT) — отдельный декодер.
 */
class SatPanel(private val act: Activity, private val prefs: SharedPreferences, private val common: SharedPreferences,
               private val toast: (String) -> Unit, private val onTune: (Long, Modulation, Int) -> Unit) {
    private val ui = Handler(Looper.getMainLooper())
    val view: LinearLayout = Ui.vbox(act, 0)
    private val map = MapView(act)
    private val picView = SstvView(act).apply { visibility = android.view.View.GONE }
    private var lrpt: LrptWorker? = null
    private var lrptCenter = 0.0
    private var showPic = false
    private val picBtn = Ui.button(act, "Снимок") { togglePic() }
    private var saveBtn: android.view.View? = null
    private var shownPic: io.github.mikeileech.sdr4noob.radio.LrptPicture? = null
    private val now = Ui.text(act, "Загрузка орбит (TLE)…", 13f)
    private val list = Ui.text(act, "", 12f).apply { maxLines = 5; typeface = android.graphics.Typeface.MONOSPACE }
    private val time = SimpleDateFormat("dd.MM HH:mm", Locale.ROOT)
    private val hhmm = SimpleDateFormat("HH:mm:ss", Locale.ROOT)
    @Volatile private var orbits: Map<Int, Orbit> = emptyMap()
    @Volatile private var passes: List<Pass> = emptyList()
    private var tleStatus = ""
    private var sat: Sat? = null
    private var tx: Tx? = null
    private var engine: RadioEngine? = null
    private var lastTuned = 0L
    private var tick = 0
    @Volatile private var alive = true

    init {
        val (la, lo) = Home.get(common)
        map.setHome(la, lo, intArrayOf(500, 1500, 3000))
        map.setCenter(la, lo, 2)
        map.onItemClick = { key -> key.toIntOrNull()?.let { n -> SatCatalog.allSats().firstOrNull { it.norad == n }?.let(::choose) } }
        view.addView(map, Ui.lp(h = 0, weight = 1f))
        view.addView(picView, Ui.lp(h = 0, weight = 1f))
        view.addView(now)
        view.addView(list)
        val r0 = Ui.hbox(act)
        picBtn.visibility = android.view.View.GONE
        r0.addView(picBtn, Ui.lp(0, weight = 1f))
        val save = Ui.button(act, "Сохранить снимок") { savePic() }.apply { visibility = android.view.View.GONE }
        r0.addView(save, Ui.lp(0, weight = 1f))
        saveBtn = save
        view.addView(r0)
        val r = Ui.hbox(act)
        r.addView(Ui.button(act, "Спутник…") { pickSat() }, Ui.lp(0, weight = 1f))
        r.addView(Ui.button(act, "Пролёты…") { showPasses() }, Ui.lp(0, weight = 1f))
        r.addView(Ui.button(act, "Обновить TLE") { load(force = true) }, Ui.lp(0, weight = 1f))
        view.addView(r)
        load(force = false)
        prefs.getInt("sat_norad", 25544).let { n -> SatCatalog.allSats().firstOrNull { it.norad == n } }?.let { s ->
            sat = s; tx = s.txs.getOrNull(prefs.getInt("sat_tx", 0)) ?: s.txs.firstOrNull()
        }
    }

    /** Частота и режим, с которых начинать радио (выбранный передатчик, по умолчанию МКС 145,800 NFM). */
    fun startFreq(): Long = (tx?.center ?: 145.8e6).toLong()
    fun startMode(): Modulation = tx?.let { runCatching { Modulation.valueOf(it.mode) }.getOrNull() } ?: Modulation.NFM

    fun start(e: RadioEngine) {
        engine = e
        tx?.let { if (it.kind == "lrpt") ui.post { startLrpt(it) } }
    }
    fun stop() { alive = false; stopLrpt(); engine = null }

    /**
     * Снимки «Метеора»: приёмник настраивается на 200 кГц ниже сигнала (как на ПК — уводит сигнал от пика
     * постоянной составляющей), IQ 1,2 МГц идёт в декодер LRPT, доплер поправляется смещением в декодере.
     */
    private fun startLrpt(x: Tx) {
        val e = engine ?: return
        stopLrpt()
        val fs = 1_200_000
        e.setSampleRate(fs)
        lrptCenter = x.center - RxPlan.IF_OFFSET
        e.setCenter(lrptCenter.roundToLong())
        val w = LrptWorker(fs, { p -> savePicture(p, auto = true) }, { msg -> ui.post { if (alive) toast(msg) } })
        w.offsetHz = x.center - lrptCenter
        lrpt = w
        e.iqTap = w::feed
        picBtn.visibility = android.view.View.VISIBLE
        saveBtn?.visibility = android.view.View.VISIBLE
    }

    private fun stopLrpt() {
        engine?.iqTap = null
        lrpt?.stop(); lrpt = null
        picBtn.visibility = android.view.View.GONE
        saveBtn?.visibility = android.view.View.GONE
        if (showPic) togglePic()
    }

    private fun togglePic() {
        showPic = !showPic
        map.visibility = if (showPic) android.view.View.GONE else android.view.View.VISIBLE
        picView.visibility = if (showPic) android.view.View.VISIBLE else android.view.View.GONE
        picBtn.text = if (showPic) "Карта" else "Снимок"
        if (showPic) refresh()
    }

    /** Полный снимок строится в потоке декодера (картинку меняет только он), сохраняется — в фоне. */
    private fun savePic() {
        val w = lrpt ?: return
        w.requestFull { p -> if (p == null) ui.post { if (alive) toast("Снимка пока нет: кадры ещё не приняты") } else savePicture(p, auto = false) }
    }

    /** Сохранить снимок в «Изображения/SDR4NOOB/Метеор» (вызывается из потока декодера — сохраняет в фоне). */
    private fun savePicture(p: io.github.mikeileech.sdr4noob.radio.LrptPicture, auto: Boolean) {
        val name = "meteor_" + SimpleDateFormat("yyyyMMdd_HHmmss", Locale.ROOT).format(Date()) + "_${p.key}.png"
        val app = act.applicationContext       // окно могли закрыть посреди пролёта — не держать его
        thread(name = "lrpt-save") {
            val msg = try {
                (if (auto) "Снимок пролёта сохранён: " else "Снимок сохранён: ") + Storage.savePng(app, Storage.METEOR, name, bitmap(p))
            } catch (e: Throwable) { "Снимок не сохранён: ${e.message ?: e.javaClass.simpleName}" }
            // окно закрыто — сообщить всё равно (короткий тост от приложения)
            ui.post { if (alive) toast(msg) else android.widget.Toast.makeText(app, msg, android.widget.Toast.LENGTH_LONG).show() }
        }
    }

    /** Растр снимка; не хватило памяти — вдвое меньше по обеим осям (снимок важнее разрешения). */
    private fun bitmap(p: io.github.mikeileech.sdr4noob.radio.LrptPicture): android.graphics.Bitmap {
        var argb = p.argb; var w = p.width; var h = p.height
        while (true) {
            try { return android.graphics.Bitmap.createBitmap(argb, w, h, android.graphics.Bitmap.Config.ARGB_8888) }
            catch (e: OutOfMemoryError) {
                if (w < 200) throw e
                val w2 = w / 2; val h2 = h / 2; val src = argb; val sw = w
                argb = IntArray(w2 * h2) { src[(it / w2) * 2 * sw + (it % w2) * 2] }
                w = w2; h = h2
            }
        }
    }

    private fun load(force: Boolean) {
        now.text = if (force) "Обновление TLE с celestrak…" else now.text
        thread(name = "tle") {
            val res = runCatching {
                val loader = TleLoader(File(act.filesDir, "tle"))
                loader.load(force = force, norads = SatCatalog.allSats().map { it.norad })
            }
            val r = res.getOrNull()
            val orb = HashMap<Int, Orbit>()
            if (r != null) for (s in SatCatalog.allSats()) r.tles[s.norad]?.let { t -> runCatching { orb[s.norad] = Orbit(t) } }
            val (la, lo) = Home.get(common)
            val ps = runCatching {
                allPasses(orb.filterKeys { it !in SatCatalog.GEO_NORADS }.values, la, lo, hours = 36.0, minEl = 10.0)
            }.getOrDefault(emptyList())
            ui.post {
                if (!alive) return@post
                orbits = orb; passes = ps
                tick = 0                       // трасса выбранного — сразу по новым орбитам
                tleStatus = when {
                    r == null -> "TLE не загружены: ${res.exceptionOrNull()?.message}"
                    orb.isEmpty() -> "Нет TLE: нужен интернет хотя бы один раз (${r.status})"
                    else -> "Орбит: ${orb.size} · ${r.status}"
                }
                refresh()
            }
        }
    }

    private fun colorOf(s: Sat) = runCatching { Color.parseColor(s.color) }.getOrDefault(0xFFFFD84D.toInt())

    /** Раз в секунду из потока интерфейса: положения, текущий спутник, доплер. */
    fun refresh() {
        val t = System.currentTimeMillis() / 1000.0
        val (la, lo) = Home.get(common)
        val orb = orbits
        val sats = SatCatalog.allSats().filter { it.norad in orb }
        val sel = sat
        map.setItems("sats", sats.mapNotNull { s ->
            val p = runCatching { orb.getValue(s.norad).subpoint(t) }.getOrNull() ?: return@mapNotNull null
            if (!p[0].isFinite() || !p[1].isFinite()) return@mapNotNull null
            MapItem(s.norad.toString(), p[0], p[1], MapItem.Kind.SAT, label = if (s.norad == sel?.norad) s.name else null, color = colorOf(s))
        })
        map.selected = sel?.norad?.toString()
        // трасса выбранного — раз в 30 с (дорого)
        if (tick++ % 30 == 0) {
            val o = sel?.let { orb[it.norad] }
            map.setTracks("track", if (o == null) emptyList() else runCatching {
                val pts = o.track(t).filter { it[0].isFinite() && it[1].isFinite() }.map { it[0] to it[1] }
                listOf(MapTrack(pts, colorOf(sel), 1.5f, dash = true))
            }.getOrDefault(emptyList()))
        }
        val o = sel?.let { orb[it.norad] }
        val x = tx
        now.text = buildString {
            if (sel == null) { append("Выберите спутник («Спутник…» или касанием на карте)"); return@buildString }
            append(sel.name)
            x?.let { append(" · ${it.label} ${String.format(Locale.ROOT, "%.3f", it.center / 1e6).replace('.', ',')} МГц ${it.mode}") }
            if (o == null) {
                append("\nНет орбиты (TLE) для этого спутника")
                // приём LRPT и без орбиты (например, запись IQ): центр и полоса — от движка, без доплера
                val w = lrpt; val e = engine
                if (w != null && e != null && x != null) { w.rate = e.sampleRate; w.offsetHz = x.center - e.centerHz }
                lrpt?.let { appendLrpt(this, it) }
                return@buildString
            }
            val l = runCatching { o.look(t, la, lo) }.getOrNull()
            if (l != null && l.el.isFinite()) {
                append(String.format(Locale.ROOT, "\nАзимут %.0f° · угол места %.1f° · дальность %.0f км", l.az, l.el, l.range).replace('.', ','))
                if (x != null && sel.norad !in SatCatalog.GEO_NORADS) {
                    val d = o.doppler(t, x.center, la, lo)
                    append(String.format(Locale.ROOT, " · доплер %+.1f кГц", d / 1e3).replace('.', ','))
                    val w = lrpt
                    val e = engine
                    if (w != null && e != null) {
                        // центр и полосу берём у движка: их могли сменить (меню, касание спектра, запись IQ со своими)
                        w.rate = e.sampleRate
                        w.offsetHz = x.center - e.centerHz + (if (l.el > -2 && e.retunable) d else 0.0)   // у записи доплер — свой
                    } else if (l.el > -2) retune(x, d)
                }
                if (l.el < 0) append(" · за горизонтом")
            }
            lrpt?.let { appendLrpt(this, it) }
        }
        val next = passes.filter { it.los > t }.take(5)
        list.text = if (next.isEmpty()) tleStatus else next.joinToString("\n") { p ->
            String.format(Locale.ROOT, "%-14s %s–%s  макс %2.0f°",
                p.name.take(14), time.format(Date((p.aos * 1000).toLong())), hhmm.format(Date((p.los * 1000).toLong())).take(5), p.maxEl)
        }
    }

    /** Строка состояния LRPT и уменьшенный снимок (если открыт). */
    private fun appendLrpt(sb: StringBuilder, w: LrptWorker) {
        with(sb) {
                val s = w.stats
                append("\nLRPT: ")
                if (s == null) append("запуск…") else {
                    append(if (s.sync) "синхронизация есть" else "поиск сигнала")
                    append(" · кадров ${s.framesOk}/${s.frames} · строк ${s.lines}")
                    if (s.channels.isNotEmpty()) append(" · каналы " + s.channels.joinToString(",") { (it - 63).toString() })
                    append(String.format(Locale.ROOT, " · С/Ш %.1f dB", s.snrDb).replace('.', ','))
                    if (s.mode.isNotEmpty()) append(" · ${s.mode}")
                    if (w.dropped > 0) append(" · пропущено блоков ${w.dropped}: телефон не успевает")
                }
                w.wantPreview = showPic
                // растр — только для нового снимка и когда его видно (уменьшенная копия, не больше 1200 строк)
                w.preview?.takeIf { it !== shownPic && showPic }?.let { p ->
                    shownPic = p
                    picView.bitmap = android.graphics.Bitmap.createBitmap(p.argb, p.width, p.height, android.graphics.Bitmap.Config.ARGB_8888)
                }
        }
    }

    /** Подстройка на частоту с доплером: радио слышит спутник без ручной перестройки (шаг 100 Гц). */
    private fun retune(x: Tx, d: Double) {
        val f = ((x.center + d) / 100).roundToLong() * 100
        if (abs(f - lastTuned) >= 100) { lastTuned = f; engine?.tune(f) }
    }

    private fun choose(s: Sat) {
        val txs = s.txs
        if (txs.isEmpty()) { toast("${s.name}: передатчиков в каталоге нет"); return }
        val pick = { i: Int ->
            sat = s; tx = txs[i]; lastTuned = 0; tick = 0
            prefs.edit().putInt("sat_norad", s.norad).putInt("sat_tx", i).apply()
            val m = runCatching { Modulation.valueOf(txs[i].mode) }.getOrNull() ?: Modulation.NFM
            stopLrpt()
            onTune(txs[i].center.toLong(), m, if (txs[i].bw > 0) txs[i].bw.roundToInt() else m.defaultBw)
            if (txs[i].kind == "lrpt") startLrpt(txs[i])
            refresh()
        }
        if (txs.size == 1) pick(0)
        else AlertDialog.Builder(act).setTitle(s.name).setItems(txs.map {
            "${it.label} — ${String.format(Locale.ROOT, "%.3f", it.center / 1e6).replace('.', ',')} МГц ${it.mode}" +
                (if (it.note.isNotEmpty()) "\n${it.note}" else "")
        }.toTypedArray()) { _, i -> pick(i) }.show()
    }

    private fun pickSat() {
        val orb = orbits
        val sats = SatCatalog.allSats()
        val t = System.currentTimeMillis() / 1000.0
        val (la, lo) = Home.get(common)
        val names = sats.map { s ->
            val cat = SatCatalog.CATEGORIES[s.cat] ?: s.cat
            val el = orb[s.norad]?.let { o -> runCatching { o.look(t, la, lo).el }.getOrNull() }
            "${s.name} — $cat" + (el?.takeIf { it.isFinite() }?.let { if (it > 0) " · сейчас виден (${it.roundToInt()}°)" else "" } ?: " · нет TLE")
        }
        AlertDialog.Builder(act).setTitle("Спутник").setItems(names.toTypedArray()) { _, i -> choose(sats[i]) }.show()
    }

    private fun showPasses() {
        val t = System.currentTimeMillis() / 1000.0
        val ps = passes.filter { it.los > t }
        if (ps.isEmpty()) { toast(if (orbits.isEmpty()) "Нет орбит: обновите TLE (нужен интернет)" else "Пролётов выше 10° за 36 ч нет"); return }
        AlertDialog.Builder(act).setTitle("Пролёты выше 10° (36 ч)").setItems(ps.map { p ->
            String.format(Locale.ROOT, "%s  %s–%s\nмакс %.0f° на азимуте %.0f°, %d мин", p.name, time.format(Date((p.aos * 1000).toLong())),
                hhmm.format(Date((p.los * 1000).toLong())).take(5), p.maxEl, p.azTca, (p.duration / 60).roundToInt())
        }.toTypedArray()) { _, i -> SatCatalog.allSats().firstOrNull { it.norad == ps[i].norad }?.let(::choose) }.show()
    }

    @Suppress("unused") private val keep = Geo     // карта и круг видимости — Geo.visibilityCircle (позже)
}
