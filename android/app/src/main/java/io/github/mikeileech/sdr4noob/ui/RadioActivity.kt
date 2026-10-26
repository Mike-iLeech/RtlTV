package io.github.mikeileech.sdr4noob.ui

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.view.WindowManager
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.SeekBar
import android.widget.Spinner
import android.widget.TextView
import io.github.mikeileech.sdr4noob.dsp.Modulation
import io.github.mikeileech.sdr4noob.dsp.RadioDemod
import io.github.mikeileech.sdr4noob.radio.RadioEngine
import io.github.mikeileech.sdr4noob.sdr.Receivers
import io.github.mikeileech.sdr4noob.sdr.SdrException
import java.util.Locale
import kotlin.math.roundToLong

/** Радио и спектр: WFM (стерео), NFM, AM; водопад; настройка касанием — как радиорежим настольной версии. */
class RadioActivity : Activity() {
    companion object {
        const val EXTRA_SOURCE = "source"
        private val PRESETS = listOf(
            "FM вещание" to (101_000_000L to Modulation.WFM),
            "Любители 2 м" to (145_500_000L to Modulation.NFM),
            "PMR446" to (446_006_250L to Modulation.NFM),
            "LPD 433" to (433_075_000L to Modulation.NFM),
            "Авиа (АМ)" to (127_000_000L to Modulation.AM),
            "СВ радио (АМ)" to (999_000L to Modulation.AM),
        )
    }

    private lateinit var prefs: android.content.SharedPreferences
    private var engine: RadioEngine? = null
    private lateinit var spectrum: SpectrumView
    private lateinit var freqText: TextView
    private lateinit var infoText: TextView
    private lateinit var statusText: TextView
    private lateinit var gainBar: SeekBar
    private lateinit var gainAuto: CheckBox
    private lateinit var gainText: TextView
    private lateinit var squelchBar: SeekBar
    private lateinit var modeSpin: Spinner
    private lateinit var biasBox: CheckBox
    private lateinit var stereoBox: CheckBox
    private val ui = Handler(Looper.getMainLooper())
    private var sourceId = Receivers.DEMO
    private var mode = Modulation.WFM

    private val ticker = object : Runnable {
        override fun run() {
            updateStatus()
            ui.postDelayed(this, 200)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        prefs = getSharedPreferences("sdr4noob", MODE_PRIVATE)
        sourceId = intent.getStringExtra(EXTRA_SOURCE) ?: Receivers.DEMO
        mode = runCatching { Modulation.valueOf(prefs.getString("mode", "WFM")!!) }.getOrDefault(Modulation.WFM)
        title = Receivers.describe(this, sourceId)
        buildUi()
        startEngine()
    }

    override fun onResume() { super.onResume(); ui.post(ticker) }
    override fun onPause() { super.onPause(); ui.removeCallbacks(ticker) }

    override fun onDestroy() {
        engine?.stop()   // снимает listener — поздние onError/onOpened сюда уже не придут
        engine = null
        super.onDestroy()
    }

    private val alive get() = !isFinishing && !isDestroyed

    private fun buildUi() {
        val root = Ui.vbox(this, 8)
        root.setBackgroundColor(Ui.BG)

        val top = Ui.hbox(this)
        top.addView(Ui.button(this, "−") { step(-1) }, Ui.lp(Ui.dp(this, 52)))
        freqText = Ui.text(this, "", 26f, Ui.ACCENT, bold = true).apply {
            textAlignment = View.TEXT_ALIGNMENT_CENTER
            setOnClickListener { askFrequency() }
        }
        top.addView(freqText, Ui.lp(0, weight = 1f))
        top.addView(Ui.button(this, "+") { step(+1) }, Ui.lp(Ui.dp(this, 52)))
        root.addView(top)

        val row = Ui.hbox(this)
        modeSpin = Spinner(this).apply {
            adapter = ArrayAdapter(this@RadioActivity, android.R.layout.simple_spinner_dropdown_item,
                Modulation.values().map { it.title })
            setSelection(mode.ordinal)
            onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
                override fun onItemSelected(p: AdapterView<*>?, v: View?, pos: Int, id: Long) {
                    val m = Modulation.values()[pos]
                    if (m != mode) setMode(m)
                }
                override fun onNothingSelected(p: AdapterView<*>?) {}
            }
        }
        row.addView(modeSpin, Ui.lp(0, weight = 1f))
        stereoBox = CheckBox(this).apply {
            text = "Стерео"; setTextColor(0xFFFFFFFF.toInt())
            isChecked = prefs.getBoolean("stereo", true)
            setOnCheckedChangeListener { _, on -> engine?.setStereo(on); prefs.edit().putBoolean("stereo", on).apply() }
        }
        row.addView(stereoBox)
        root.addView(row)

        infoText = Ui.text(this, "", 13f, Ui.DIM)
        root.addView(infoText)

        spectrum = SpectrumView(this)
        root.addView(spectrum, Ui.lp(h = 0, weight = 1f, top = Ui.dp(this, 4)))

        // усиление
        val g = Ui.hbox(this)
        gainText = Ui.text(this, "Усиление", 13f).apply { minWidth = Ui.dp(this@RadioActivity, 110) }
        g.addView(gainText)
        gainBar = SeekBar(this).apply { max = 1 }
        g.addView(gainBar, Ui.lp(0, weight = 1f))
        gainAuto = CheckBox(this).apply { text = "Авто"; setTextColor(0xFFFFFFFF.toInt()) }
        g.addView(gainAuto)
        root.addView(g)

        // громкость и шумодав
        val v = Ui.hbox(this)
        v.addView(Ui.text(this, "Громкость", 13f).apply { minWidth = Ui.dp(this@RadioActivity, 110) })
        val vol = SeekBar(this).apply {
            max = 100; progress = prefs.getInt("volume", 80)
            setOnSeekBarChangeListener(seek { p -> engine?.volume = p / 100f; prefs.edit().putInt("volume", p).apply() })
        }
        v.addView(vol, Ui.lp(0, weight = 1f))
        root.addView(v)
        val s = Ui.hbox(this)
        s.addView(Ui.text(this, "Шумодав", 13f).apply { minWidth = Ui.dp(this@RadioActivity, 110) })
        squelchBar = SeekBar(this).apply {
            max = 100; progress = prefs.getInt("squelch", 0)
            setOnSeekBarChangeListener(seek { p -> engine?.setSquelch(squelchDb(p)); prefs.edit().putInt("squelch", p).apply() })
        }
        s.addView(squelchBar, Ui.lp(0, weight = 1f))
        root.addView(s)

        val b = Ui.hbox(this)
        biasBox = CheckBox(this).apply {
            text = "Питание антенны (bias-T)"; setTextColor(0xFFFFFFFF.toInt()); visibility = View.GONE
            setOnCheckedChangeListener { _, on -> engine?.setBiasTee(on) }
        }
        b.addView(biasBox, Ui.lp(0, weight = 1f))
        b.addView(Ui.button(this, "Станции…") { choosePreset() })
        root.addView(b)

        statusText = Ui.text(this, "Подключение…", 12f, Ui.DIM)
        root.addView(statusText)
        setContentView(root)
    }

    private fun seek(f: (Int) -> Unit) = object : SeekBar.OnSeekBarChangeListener {
        override fun onProgressChanged(sb: SeekBar?, p: Int, user: Boolean) { if (user) f(p) }
        override fun onStartTrackingTouch(sb: SeekBar?) {}
        override fun onStopTrackingTouch(sb: SeekBar?) {}
    }

    /** 0 — шумодав выключен; дальше −120 … −20 dBFS. */
    private fun squelchDb(p: Int) = if (p == 0) -200f else -120f + p

    private fun startEngine() {
        val src = try {
            Receivers.sourceFor(this, sourceId)
        } catch (e: SdrException) {
            fail(e.message ?: "Ошибка приёмника"); return
        }
        // по умолчанию 1,2 МГц обзора: вдвое меньше нагрузка и расход батареи; до 2,88 МГц — в меню
        val rate = prefs.getInt("rate", 1_200_000).takeIf { it in RadioDemod.RADIO_RATES } ?: 1_200_000
        val freq = prefs.getLong("freq_${mode.name}", defaultFreq(mode))
        val e = RadioEngine(src, rate, freq, mode)
        e.volume = prefs.getInt("volume", 80) / 100f
        e.setStereo(prefs.getBoolean("stereo", true))
        e.setSquelch(squelchDb(prefs.getInt("squelch", 0)))
        prefs.getInt("ppm", 0).takeIf { it != 0 }?.let(e::setPpm)
        e.listener = object : RadioEngine.Listener {
            override fun onOpened(engine: RadioEngine) = runOnUiThread { opened(engine) }
            override fun onSpectrum() = spectrum.postInvalidate()
            override fun onError(message: String) = runOnUiThread { fail(message) }
        }
        engine = e
        spectrum.engine = e
        applyModeUi()
        spectrum.onTuned = { f -> saveFreq(f); updateFreq() }
        updateFreq()
        e.start()
    }

    private fun opened(e: RadioEngine) {
        if (!alive || e !== engine) return
        val gains = e.gains
        val savedGain = prefs.getInt("gain", -1)
        if (gains.isEmpty()) {
            gainBar.isEnabled = false; gainAuto.isEnabled = false; gainText.text = "Усиление —"
        } else {
            gainBar.max = gains.size - 1
            val idx = if (savedGain < 0) gains.size * 2 / 3 else gains.indexOfFirst { it >= savedGain }.coerceAtLeast(0)
            gainBar.progress = idx
            gainAuto.isChecked = prefs.getBoolean("gain_auto", false)
            applyGain(gains)
            gainBar.setOnSeekBarChangeListener(seek { applyGain(gains) })
            gainAuto.setOnCheckedChangeListener { _, _ -> applyGain(gains) }
        }
        biasBox.visibility = if (e.hasBiasTee) View.VISIBLE else View.GONE
        statusText.text = e.label
    }

    private fun applyGain(gains: IntArray) {
        val e = engine ?: return
        val auto = gainAuto.isChecked
        gainBar.isEnabled = !auto
        val t = gains[gainBar.progress.coerceIn(0, gains.size - 1)]
        e.setGain(if (auto) -1 else t)
        gainText.text = if (auto) "Усиление: авто" else String.format(Locale.ROOT, "Усиление %.1f dB", t / 10.0).replace('.', ',')
        prefs.edit().putInt("gain", t).putBoolean("gain_auto", auto).apply()
    }

    private fun fail(message: String) {
        if (!alive) return
        statusText.text = message
        AlertDialog.Builder(this)
            .setTitle("Приёмник")
            .setMessage(message)
            .setPositiveButton("К выбору приёмника") { _, _ -> finish() }
            .setNegativeButton("Остаться", null)
            .show()
    }

    private fun defaultFreq(m: Modulation) = when (m) {
        Modulation.WFM -> 101_000_000L
        Modulation.NFM -> 145_500_000L
        Modulation.AM -> 127_000_000L
    }

    private fun setMode(m: Modulation, freq: Long? = null) {
        mode = m
        prefs.edit().putString("mode", m.name).apply()
        if (modeSpin.selectedItemPosition != m.ordinal) modeSpin.setSelection(m.ordinal)
        val e = engine ?: return
        e.setMode(m)
        val f = freq ?: prefs.getLong("freq_${m.name}", defaultFreq(m))
        e.tune(f)
        saveFreq(f)
        applyModeUi()
        updateFreq()
    }

    private fun applyModeUi() {
        spectrum.snapHz = when (mode) { Modulation.WFM -> 100_000L; Modulation.NFM -> 6_250L; Modulation.AM -> 1_000L }
        spectrum.channelBw = mode.defaultBw
        stereoBox.visibility = if (mode == Modulation.WFM) View.VISIBLE else View.GONE
    }

    private fun step(dir: Int) {
        val e = engine ?: return
        val f = e.vfoHz + dir * mode.step
        e.tune(f); saveFreq(f); updateFreq()
    }

    private fun saveFreq(f: Long) { prefs.edit().putLong("freq_${mode.name}", f).apply() }

    private fun updateFreq() {
        engine?.let { freqText.text = Ui.mhz(it.vfoHz) }
    }

    private fun askFrequency() {
        val e = engine ?: return
        val edit = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
            setText(String.format(Locale.ROOT, "%.4f", e.vfoHz / 1e6))
            selectAll()
        }
        AlertDialog.Builder(this)
            .setTitle("Частота, МГц")
            .setView(edit)
            .setPositiveButton("Настроить") { _, _ ->
                val mhz = edit.text.toString().replace(',', '.').toDoubleOrNull()
                if (mhz != null && mhz in 0.5..1766.0) {
                    val f = (mhz * 1e6).roundToLong()
                    e.tune(f); saveFreq(f); updateFreq()
                }
            }
            .setNegativeButton("Отмена", null)
            .show()
    }

    private fun choosePreset() {
        AlertDialog.Builder(this)
            .setTitle("Куда настроиться")
            .setItems(PRESETS.map { "${it.first} — ${Ui.mhz(it.second.first)}" }.toTypedArray()) { _, i ->
                val (f, m) = PRESETS[i].second
                if (m != mode) setMode(m, f) else { engine?.tune(f); saveFreq(f); updateFreq() }
            }
            .show()
    }

    private fun updateStatus() {
        val e = engine ?: return
        if (!e.running) return
        val st = when {
            mode != Modulation.WFM -> ""
            e.stereoLock -> " · СТЕРЕО"
            else -> " · моно"
        }
        val sq = if (!e.squelchOpen) " · шумодав закрыт" else ""
        infoText.text = String.format(Locale.ROOT, "Уровень %.0f dBFS", e.levelDb) + st + sq +
            " · полоса " + Ui.mhz(e.sampleRate.toLong()).replace(" МГц", " МГц обзора")
        if (e.dropped > 0) statusText.text = "${e.label} · потеряно отсчётов: ${e.dropped}"
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menu.add(0, 1, 0, "Полоса обзора…")
        menu.add(0, 2, 0, "Поправка частоты (ppm)…")
        menu.add(0, 3, 0, "Справка")
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        when (item.itemId) {
            1 -> {
                val rates = RadioDemod.RADIO_RATES
                AlertDialog.Builder(this).setTitle("Полоса обзора (частота дискретизации)")
                    .setItems(rates.map { String.format(Locale.ROOT, "%.2f МГц", it / 1e6).replace('.', ',') }.toTypedArray()) { _, i ->
                        prefs.edit().putInt("rate", rates[i]).apply()
                        engine?.setSampleRate(rates[i])
                    }.show()
            }
            2 -> {
                val edit = EditText(this).apply {
                    inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_SIGNED
                    setText(prefs.getInt("ppm", 0).toString())
                }
                AlertDialog.Builder(this).setTitle("Поправка кварца, ppm").setView(edit)
                    .setPositiveButton("OK") { _, _ ->
                        val p = edit.text.toString().toIntOrNull() ?: 0
                        prefs.edit().putInt("ppm", p).apply(); engine?.setPpm(p)
                    }.setNegativeButton("Отмена", null).show()
            }
            3 -> startActivity(Intent(this, GuideActivity::class.java).putExtra(GuideActivity.EXTRA_PAGE, "guide/mode-radio.html"))
            else -> return super.onOptionsItemSelected(item)
        }
        return true
    }
}
