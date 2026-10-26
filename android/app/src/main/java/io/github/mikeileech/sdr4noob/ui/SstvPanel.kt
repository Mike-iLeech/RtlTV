package io.github.mikeileech.sdr4noob.ui

import io.github.mikeileech.sdr4noob.i18n.N_
import io.github.mikeileech.sdr4noob.i18n.num
import io.github.mikeileech.sdr4noob.i18n.tr
import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Rect
import android.graphics.RectF
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.LinearLayout
import android.widget.Spinner
import android.widget.TextView
import io.github.mikeileech.sdr4noob.dsp.Sstv
import io.github.mikeileech.sdr4noob.dsp.SstvEvent
import io.github.mikeileech.sdr4noob.dsp.SstvLevel
import io.github.mikeileech.sdr4noob.radio.RadioEngine
import io.github.mikeileech.sdr4noob.radio.SstvWorker
import io.github.mikeileech.sdr4noob.radio.Storage
import kotlin.concurrent.thread

/** Картинка SSTV: вписана в окно с сохранением пропорций, при приёме — линия текущей строки. */
class SstvView(ctx: Context) : View(ctx) {
    var bitmap: Bitmap? = null
        set(v) { field = v; invalidate() }
    /** Доля принятых строк (0…1) или null, если картинка не принимается. */
    var progress: Float? = null
        set(v) { field = v; invalidate() }
    private val paint = Paint(Paint.FILTER_BITMAP_FLAG)
    private val line = Paint().apply { color = Ui.ACCENT; strokeWidth = 3f }
    private val hint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Ui.DIM; textAlign = Paint.Align.CENTER }
    private val dst = RectF()

    override fun onDraw(c: Canvas) {
        c.drawColor(0xFF101418.toInt())
        val b = bitmap
        if (b == null) {
            hint.textSize = 14f * resources.displayMetrics.density
            c.drawText(tr("Здесь появится картинка SSTV"), width / 2f, height / 2f, hint)
            return
        }
        val s = minOf(width.toFloat() / b.width, height.toFloat() / b.height)
        val w = b.width * s; val h = b.height * s
        dst.set((width - w) / 2, (height - h) / 2, (width + w) / 2, (height + h) / 2)
        c.drawBitmap(b, Rect(0, 0, b.width, b.height), dst, paint)
        progress?.let { p -> val y = dst.top + h * p.coerceIn(0f, 1f); c.drawLine(dst.left, y, dst.right, y, line) }
    }
}

/**
 * Панель SSTV в окне радио — как окно SSTV настольной версии: декодер слушает звук радиорежима (USB на КВ,
 * NFM для МКС), режим определяется по VIS-коду или задаётся вручную, принятые картинки сохраняются в
 * «Изображения/SDR4NOOB/SSTV».
 */
class SstvPanel(private val act: Activity, private val prefs: android.content.SharedPreferences,
                private val toast: (String) -> Unit) {
    companion object {
        /** Значение «авто» в списке режимов и в настройке sstv_mode (сохраняется по-русски, показывается через tr). */
        val AUTO = N_("Авто (VIS)")
        /** Названия частот помечены N_ — показывать через tr(it.first). */
        val PRESETS = listOf(
            Triple(N_("КВ 20 м — 14,230 МГц (USB), главная частота SSTV"), 14_230_000L, "USB"),
            Triple(N_("КВ 20 м — 14,233 МГц (USB)"), 14_233_000L, "USB"),
            Triple(N_("КВ 40 м — 7,171 МГц (LSB)"), 7_171_000L, "LSB"),
            Triple(N_("КВ 80 м — 3,730 МГц (LSB)"), 3_730_000L, "LSB"),
            Triple(N_("КВ 15 м — 21,340 МГц (USB)"), 21_340_000L, "USB"),
            Triple(N_("КВ 10 м — 28,680 МГц (USB)"), 28_680_000L, "USB"),
            Triple(N_("МКС — 145,800 МГц (FM), сеансы ARISS"), 145_800_000L, "NFM"),
            Triple(N_("2 м — 144,500 МГц (FM)"), 144_500_000L, "NFM"),
        )
    }

    private val ui = Handler(Looper.getMainLooper())
    val view: LinearLayout = Ui.vbox(act, 0)
    private val image = SstvView(act)
    private val status = Ui.text(act, tr("Ожидание VIS-кода…"), 13f, Ui.DIM)
    private val modeSpin = Spinner(act)
    private val modes = listOf(AUTO) + Sstv.MODES.keys
    private var worker: SstvWorker? = null
    private var engine: RadioEngine? = null
    private var bmp: Bitmap? = null
    private var curMode: String? = null
    private var lineText = "—"
    @Volatile private var alive = false

    init {
        view.addView(image, Ui.lp(h = 0, weight = 1f))
        view.addView(status)
        val r1 = Ui.hbox(act)
        modeSpin.adapter = ArrayAdapter(act, android.R.layout.simple_spinner_dropdown_item, modes.map { if (it == AUTO) tr(it) else it })
        modeSpin.setSelection(modes.indexOf(prefs.getString("sstv_mode", AUTO)).coerceAtLeast(0))
        modeSpin.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(p: AdapterView<*>?, v: View?, pos: Int, id: Long) {
                prefs.edit().putString("sstv_mode", modes[pos]).apply()
                worker?.setForceMode(modes[pos].takeIf { it != AUTO })
            }
            override fun onNothingSelected(p: AdapterView<*>?) {}
        }
        r1.addView(modeSpin, Ui.lp(0, weight = 1f))
        r1.addView(Ui.button(act, tr("Начать сейчас")) { manual() }, Ui.lp(0, weight = 1f))
        r1.addView(Ui.button(act, tr("Сброс")) { reset() }, Ui.lp(0, weight = 0.6f))
        view.addView(r1)
        val r2 = Ui.hbox(act)
        r2.addView(Ui.button(act, tr("Картинки…")) { gallery() }, Ui.lp(0, weight = 1f))
        r2.addView(Ui.button(act, tr("Из WAV-файла…")) { pickWav() }, Ui.lp(0, weight = 1f))
        view.addView(r2)
    }

    val running get() = worker != null

    /** Включить декодер на звуке движка [e]. */
    fun start(e: RadioEngine) {
        stop()
        alive = true
        val w = SstvWorker({ ev, lv -> ui.post { if (alive) onEvents(ev, lv) } }, { msg -> ui.post { if (alive) toast(msg) } })
        w.setForceMode(modes[modeSpin.selectedItemPosition.coerceAtLeast(0)].takeIf { it != AUTO })
        worker = w; engine = e
        e.audioTap = w::feed
    }

    fun stop() {
        alive = false
        engine?.audioTap = null
        engine = null
        worker?.stop(); worker = null
    }

    private fun manual() {
        val m = modes[modeSpin.selectedItemPosition.coerceAtLeast(0)]
        if (m == AUTO) { toast(tr("Выберите конкретный режим SSTV")); return }
        worker?.startManual(m)
    }

    private fun reset() {
        worker?.reset()
        curMode = null; lineText = "—"
        image.progress = null
        status.text = tr("Ожидание VIS-кода…")
    }

    private fun onEvents(events: List<SstvEvent>, lv: SstvLevel) {
        for (ev in events) when (ev) {
            is SstvEvent.Vis -> toast("VIS: ${ev.mode}")
            is SstvEvent.Start -> {
                curMode = ev.mode
                bmp = Bitmap.createBitmap(ev.width, ev.height, Bitmap.Config.ARGB_8888).apply { eraseColor(0xFF000000.toInt()) }
                image.bitmap = bmp; image.progress = 0f
                lineText = tr("строка {y} / {h}", "y" to 0, "h" to ev.height)
            }
            is SstvEvent.Line -> bmp?.let { b ->
                if (ev.y in 0 until b.height && b.width == ev.width) {
                    b.setPixels(ev.rgb, 0, ev.width, 0, ev.y, minOf(ev.width, ev.rgb.size), 1)
                    image.progress = (ev.y + 1f) / b.height
                    lineText = tr("строка {y} / {h}", "y" to ev.y + 1, "h" to b.height)
                    image.invalidate()
                }
            }
            is SstvEvent.Done -> {
                val b = Bitmap.createBitmap(ev.image, ev.width, ev.height, Bitmap.Config.ARGB_8888)
                bmp = b; image.bitmap = b; image.progress = null
                save(b, ev.mode + if (ev.complete) "" else " (неполная)")  // noqa: i18n
                lineText = tr("готово")
            }
            is SstvEvent.Lost -> {
                image.progress = null
                if (ev.lines > 0) {
                    val b = Bitmap.createBitmap(ev.image, ev.width, ev.height, Bitmap.Config.ARGB_8888)
                    bmp = b; image.bitmap = b
                    save(b, ev.mode + " (неполная)")  // noqa: i18n
                }
                lineText = tr("сигнал потерян")
            }
        }
        val mode = lv.mode ?: curMode
        val tone = if (lv.freq > 0) tr("{f} Гц · синхро {s}", "f" to num(lv.freq, 0), "s" to num(lv.sync, 2)) else "—"
        status.text = (if (mode != null) "$mode · $lineText" else tr("Ожидание VIS-кода…")) + " · " + tr("тон {t}", "t" to tone)
    }

    private fun save(b: Bitmap, mode: String) {
        val name = Storage.sstvName(mode)
        thread(name = "sstv-save") {
            val msg = try { tr("Картинка сохранена: {path}", "path" to Storage.savePng(act, Storage.SSTV, name, b)) }
            catch (e: Exception) { tr("Картинка не сохранена: {err}", "err" to e.message) }
            ui.post { if (!act.isDestroyed) toast(msg) }
        }
    }

    private fun gallery() {
        thread(name = "sstv-list") {
            val items = runCatching { Storage.listPictures(act, Storage.SSTV) }.getOrDefault(emptyList())
            ui.post {
                if (act.isDestroyed) return@post
                if (items.isEmpty()) { toast(tr("Принятых картинок пока нет")); return@post }
                AlertDialog.Builder(act).setTitle(tr("Картинки SSTV ({path})", "path" to Storage.whereLabel(act, Storage.SSTV, true)))
                    .setItems(items.map { it.first }.toTypedArray()) { _, i -> open(items[i].second) }
                    .setNegativeButton(tr("Закрыть"), null).show()
            }
        }
    }

    private fun open(uri: Uri) {
        thread(name = "sstv-open") {
            val b = runCatching { act.contentResolver.openInputStream(uri)?.use { android.graphics.BitmapFactory.decodeStream(it) } }.getOrNull()
            ui.post {
                if (act.isDestroyed) return@post
                if (b == null) { toast(tr("Картинку не удалось открыть")); return@post }
                image.progress = null; image.bitmap = b
            }
        }
    }

    var onPickWav: (() -> Unit)? = null
    private fun pickWav() { onPickWav?.invoke() }

    /** Декодировать выбранный WAV-файл (в фоне) и сохранить найденные картинки. */
    fun decodeWav(uri: Uri) {
        val mode = modes[modeSpin.selectedItemPosition.coerceAtLeast(0)].takeIf { it != AUTO }
        status.text = tr("Декодирование файла…")
        thread(name = "sstv-wav") {
            val res = runCatching { act.contentResolver.openInputStream(uri)!!.buffered(1 shl 16).use { SstvWorker.decodeWav(it, mode) } }
            ui.post {
                if (act.isDestroyed) return@post
                res.onFailure { status.text = tr("Файл не декодирован: {err}", "err" to it.message); return@post }
                val imgs = res.getOrThrow()
                for (ev in imgs) {
                    val (px, w, h, m) = when (ev) {
                        is SstvEvent.Done -> Quad(ev.image, ev.width, ev.height, ev.mode)
                        is SstvEvent.Lost -> Quad(ev.image, ev.width, ev.height, ev.mode + " (неполная)")  // noqa: i18n
                        else -> continue
                    }
                    val b = Bitmap.createBitmap(px, w, h, Bitmap.Config.ARGB_8888)
                    image.progress = null; image.bitmap = b
                    save(b, "файл $m")  // noqa: i18n
                }
                status.text = tr("Из файла получено картинок: {n}", "n" to imgs.size)
            }
        }
    }

    private data class Quad(val px: IntArray, val w: Int, val h: Int, val mode: String)
}
