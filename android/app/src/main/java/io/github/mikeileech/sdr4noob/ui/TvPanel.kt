package io.github.mikeileech.sdr4noob.ui

import io.github.mikeileech.sdr4noob.i18n.num
import io.github.mikeileech.sdr4noob.i18n.tr
import android.app.Activity
import android.content.Context
import android.content.SharedPreferences
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Rect
import android.graphics.RectF
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.LinearLayout
import android.widget.Spinner
import io.github.mikeileech.sdr4noob.dsp.AnalogFrame
import io.github.mikeileech.sdr4noob.dsp.MuxResult
import io.github.mikeileech.sdr4noob.dsp.Tv
import io.github.mikeileech.sdr4noob.dsp.TvChannel
import io.github.mikeileech.sdr4noob.dsp.TvMode
import io.github.mikeileech.sdr4noob.radio.RadioEngine
import io.github.mikeileech.sdr4noob.radio.TvWorker
import kotlin.math.roundToInt

private fun f1(x: Double) = num(x, 1)

/** Спектр мультиплекса (весь канал, собранный из перестроек) или кадр аналогового ТВ. */
class TvView(ctx: Context) : View(ctx) {
    var mux: MuxResult? = null
        set(v) { field = v; bmp = null; invalidate() }
    private var bmp: Bitmap? = null
    var hint = tr("Выберите канал и режим")
        set(v) { field = v; invalidate() }
    private val d = resources.displayMetrics.density
    private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Ui.ACCENT; style = Paint.Style.STROKE; strokeWidth = 1.5f * d }
    private val grid = Paint().apply { color = 0xFF2A323C.toInt(); strokeWidth = 1f }
    private val txt = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Ui.DIM; textSize = 11f * d }
    private val pBmp = Paint(Paint.FILTER_BITMAP_FLAG)

    fun showFrame(f: AnalogFrame) {
        val px = IntArray(f.width * f.height) {
            val v = (f.image[it].coerceIn(0f, 1f) * 255).toInt()
            Color.rgb(v, v, v)
        }
        mux = null                    // сеттер сбрасывает кадр — новый ставится после
        bmp = Bitmap.createBitmap(px, f.width, f.height, Bitmap.Config.ARGB_8888)
        invalidate()
    }

    override fun onDraw(c: Canvas) {
        c.drawColor(0xFF101418.toInt())
        bmp?.let { b ->
            // кадр 4:3 вписан в окно
            val s = minOf(width.toFloat() / (b.height * 4f / 3f), height.toFloat() / b.height)
            val w = b.height * 4f / 3f * s; val h = b.height * s
            c.drawBitmap(b, Rect(0, 0, b.width, b.height), RectF((width - w) / 2, (height - h) / 2, (width + w) / 2, (height + h) / 2), pBmp)
            return
        }
        val m = mux
        if (m == null || m.psd.isEmpty()) {
            txt.textAlign = Paint.Align.CENTER
            c.drawText(hint, width / 2f, height / 2f, txt)
            txt.textAlign = Paint.Align.LEFT
            return
        }
        var lo = Float.MAX_VALUE; var hi = -Float.MAX_VALUE
        for (v in m.psd) { if (v < lo) lo = v; if (v > hi) hi = v }
        lo = (Math.floor(lo / 10.0) * 10).toFloat(); hi = (Math.ceil(hi / 10.0) * 10).toFloat().coerceAtLeast(lo + 20)
        val f0 = m.freqs.first(); val f1 = m.freqs.last()
        for (db in generateSequence(lo) { it + 10 }.takeWhile { it <= hi }) {
            val y = height - (db - lo) / (hi - lo) * height
            c.drawLine(0f, y, width.toFloat(), y, grid)
            if (y > 14 * d && y < height - 16 * d) c.drawText("${db.roundToInt()} dB", 4 * d, y - 2 * d, txt)   // не на подписях частот
        }
        val path = Path()
        for (i in m.psd.indices) {
            val x = ((m.freqs[i] - f0) / (f1 - f0) * width).toFloat()
            val y = height - (m.psd[i] - lo) / (hi - lo) * height
            if (i == 0) path.moveTo(x, y) else path.lineTo(x, y)
        }
        c.drawPath(path, line)
        c.drawText(tr("{f} МГц", "f" to num(f0 / 1e6, 2)), 4 * d, height - 4 * d, txt)
        txt.textAlign = Paint.Align.RIGHT
        c.drawText(tr("{f} МГц", "f" to num(f1 / 1e6, 2)), width - 4 * d, height - 4 * d, txt)
        txt.textAlign = Paint.Align.LEFT
    }
}

/**
 * Панель режима «Телевидение» (как окно ТВ на ПК): измеритель цифрового мультиплекса DVB-T/T2 (обход канала
 * перестройками, C/N, уровень, неравномерность, режим OFDM и защитный интервал), аналоговая картинка
 * (чёрно-белая: полоса телефона меньше полосы видеосигнала) и поиск каналов по сетке.
 */
class TvPanel(private val act: Activity, private val prefs: SharedPreferences, private val toast: (String) -> Unit) {
    private val ui = Handler(Looper.getMainLooper())
    val view: LinearLayout = Ui.vbox(act, 0)
    private val tv = TvView(act)
    private val info = Ui.text(act, "", 13f).apply { maxLines = 6 }
    private val planSpin = Spinner(act)
    private val chSpin = Spinner(act)
    private val plans = Tv.PLANS.values.toList()
    private var chans: List<TvChannel> = emptyList()
    private var worker: TvWorker? = null
    private var engine: RadioEngine? = null
    private var mode: String = prefs.getString("tv_job", "measure")!!.takeIf { it != "scan" } ?: "measure"
    @Volatile private var alive = false
    private val posted = java.util.concurrent.atomic.AtomicBoolean(false)
    private var scanStarted = false

    init {
        view.addView(tv, Ui.lp(h = 0, weight = 1f))
        view.addView(info)
        val r0 = Ui.hbox(act)
        planSpin.adapter = ArrayAdapter(act, android.R.layout.simple_spinner_dropdown_item, plans.map { tr(it.name) })
        planSpin.setSelection(plans.indexOfFirst { it.key == prefs.getString("tv_plan", "RU") }.coerceAtLeast(0))
        r0.addView(planSpin, Ui.lp(0, weight = 1f))
        r0.addView(chSpin, Ui.lp(0, weight = 1f))
        view.addView(r0)
        val r1 = Ui.hbox(act)
        r1.addView(Ui.button(act, tr("Цифровое")) { run("measure") }, Ui.lp(0, weight = 1f))
        r1.addView(Ui.button(act, tr("Аналоговое")) { run("analog") }, Ui.lp(0, weight = 1f))
        r1.addView(Ui.button(act, tr("Поиск каналов")) { run("scan") }, Ui.lp(0, weight = 1f))
        view.addView(r1)
        planSpin.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(p: AdapterView<*>?, v: View?, pos: Int, id: Long) {
                if (plans[pos].key != prefs.getString("tv_plan", "RU")) prefs.edit().putString("tv_plan", plans[pos].key).apply()
                fillChannels()
            }
            override fun onNothingSelected(p: AdapterView<*>?) {}
        }
        chSpin.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(p: AdapterView<*>?, v: View?, pos: Int, id: Long) {
                val ch = chans.getOrNull(pos) ?: return
                if (ch.name != prefs.getString("tv_channel", null)) {
                    prefs.edit().putString("tv_channel", ch.name).apply()
                    run(if (mode == "scan") "measure" else mode)      // после поиска — измерить выбранный канал
                }
            }
            override fun onNothingSelected(p: AdapterView<*>?) {}
        }
        fillChannels()
    }

    private val plan get() = plans[planSpin.selectedItemPosition.coerceAtLeast(0)]
    private val std get() = Tv.STANDARDS.getValue(plan.std)
    private val channel get() = chans.getOrNull(chSpin.selectedItemPosition)

    private fun fillChannels() {
        chans = plan.channels
        chSpin.adapter = ArrayAdapter(act, android.R.layout.simple_spinner_dropdown_item,
            chans.map { it.title + " · " + tr("{f} МГц", "f" to num(it.center / 1e6, 2)) })
        // по умолчанию — ДМВ 30 (546 МГц): там мультиплекс тестового эфира и первый мультиплекс во многих городах
        val want = prefs.getString("tv_channel", null)
        val idx = chans.indexOfFirst { it.name == want }.takeIf { it >= 0 } ?: chans.indexOfFirst { Math.abs(it.center - 546e6) < 1e5 }
        chSpin.setSelection(idx.coerceAtLeast(0))
    }

    /** Центр, с которого начинать (до первой команды измерителя). */
    fun startFreq(): Long = (channel?.center ?: 546e6).toLong()

    fun start(e: RadioEngine) {
        stop()
        alive = true
        // обновления из потока измерителя (на каждый кадр аналога) прореживаются: одно в очереди интерфейса
        val w = TvWorker(e, { if (posted.compareAndSet(false, true)) ui.postDelayed({ posted.set(false); if (alive) refresh() }, 100) },
            { msg -> ui.post { if (alive) toast(msg) } })
        worker = w; engine = e
        e.iqTap = w::feed
    }

    /** Приёмник открыт — начать выбранное измерение. */
    fun opened() = run(mode)

    fun stop() {
        alive = false
        engine?.iqTap = null; engine = null
        worker?.stop(); worker = null
    }

    private fun run(m: String) {
        val w = worker ?: return
        val ch = channel ?: return
        mode = m
        if (m != "scan") prefs.edit().putString("tv_job", m).apply()      // поиск при следующем открытии не повторять
        tv.mux = null
        when (m) {
            "analog" -> { tv.hint = tr("Аналоговое ТВ: ожидание кадра на {ch}…", "ch" to ch.title); w.analog(ch, std) }
            "scan" -> { tv.hint = tr("Поиск каналов…"); scanStarted = false; askScan(w) }
            else -> { tv.hint = tr("Измерение мультиплекса {ch}: обход канала…", "ch" to ch.title); w.measure(ch) }
        }
        info.text = ""
    }

    private fun askScan(w: TvWorker) {
        android.app.AlertDialog.Builder(act).setTitle(tr("Поиск каналов — что искать"))
            .setItems(arrayOf(tr("Цифровые мультиплексы (DVB-T/T2)"), tr("Аналоговые станции"))) { _, i ->
                scanStarted = true
                w.scan(if (i == 0) TvMode.DIGITAL else TvMode.ANALOG, chans, std)
            }.setOnCancelListener { run("measure") }.show()
    }

    private fun refresh() {
        val w = worker ?: return
        when (mode) {
            "analog" -> w.frame?.let { f ->
                tv.showFrame(f)
                info.text = tr("Синхронизация: строки {h} %, кадры {v} · уровень {lvl} dB · С/Ш {snr} dB",
                    "h" to (f.hlock * 100).roundToInt(), "v" to (if (f.vlock) tr("есть") else tr("нет")),
                    "lvl" to f1(f.levelDb), "snr" to f1(f.snrDb)) + "\n" +
                    tr("Картинка чёрно-белая: полоса приёмника меньше полосы видеосигнала, цветовая поднесущая не помещается.")
            }
            "scan" -> {
                if (!scanStarted) return
                val p = w.scanProgress
                val res = w.scan
                val found = res.filter { it.found == true }
                info.text = (if (p != null) tr("Поиск: канал {i} из {n}", "i" to p.first + 1, "n" to p.second) + "\n"
                    else tr("Поиск завершён.") + " ") +
                    (if (found.isEmpty()) (if (p == null) tr("Ничего не найдено.") else "")
                    else tr("Найдено: {list}", "list" to found.joinToString(", ") { "${it.ch.title} (${f1(it.value)} dB)" }))
                tv.hint = if (p != null) tr("Поиск каналов…") + " ${p.first + 1}/${p.second}" else tr("Выберите канал из найденных и режим")
            }
            else -> w.mux?.let { m ->
                tv.mux = m
                val o = m.ofdm
                val ofdm = when {
                    o == null -> tr("анализ OFDM…")
                    o.found -> "OFDM ${o.mode}" + (o.guard?.let { tr(", защитный интервал {g}", "g" to it) } ?: "") +
                        (o.freqOffsetHz?.let { tr(" · уход частоты {f} кГц", "f" to f1(it / 1e3)) } ?: "")
                    else -> tr("OFDM не найден")
                }
                info.text = when {
                    m.present -> tr("Мультиплекс есть: C/N {cn} dB · уровень {lvl} dB · неравномерность {flat} dB",
                        "cn" to f1(m.cnDb), "lvl" to f1(m.levelDb), "flat" to f1(m.flatnessDb)) + "\n$ofdm"
                    m.other -> tr("В канале сигнал, но не цифровой мультиплекс (аналоговая станция?)") + "\n$ofdm"
                    else -> tr("Мультиплекса нет: только шум") + "\n$ofdm"
                } + "\n" + tr("Для приёма C/N нужно не меньше ~20 dB (DVB-T2); неравномерность > 6 dB — отражения или помеха.")
            }
        }
    }
}
