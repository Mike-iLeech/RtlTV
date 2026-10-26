package io.github.mikeileech.sdr4noob.ui

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
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
import io.github.mikeileech.sdr4noob.radio.FreqEntry
import io.github.mikeileech.sdr4noob.radio.FreqMan
import io.github.mikeileech.sdr4noob.radio.RadioEngine
import io.github.mikeileech.sdr4noob.radio.RadioService
import io.github.mikeileech.sdr4noob.radio.Storage
import io.github.mikeileech.sdr4noob.radio.WavWriter
import java.io.File
import io.github.mikeileech.sdr4noob.sdr.AdsbDemoSource
import io.github.mikeileech.sdr4noob.sdr.Receivers
import io.github.mikeileech.sdr4noob.sdr.SdrException
import java.util.Locale
import kotlin.math.roundToLong

/**
 * Радио и спектр — как радиорежим настольной версии: WFM (стерео, RDS), NFM, AM, USB/LSB/CW с АРУ; водопад,
 * настройка касанием; менеджер частот; запись звука и IQ; приём в фоне (RadioService).
 * С [EXTRA_APP] то же окно работает как режим-приложение: SSTV и APRS (декодер на звуке), ADS-B (декодер на IQ,
 * 1090 МГц) — со своими настройками (отдельный файл настроек) и панелью под спектром.
 */
class RadioActivity : Activity() {
    companion object {
        const val EXTRA_SOURCE = "source"
        const val EXTRA_APP = "app"
        const val APP_SSTV = "sstv"
        const val APP_APRS = "aprs"
        const val APP_ADSB = "adsb"
        private const val REQ_SSTV_WAV = 14
        private const val REQ_IMPORT = 11
        private const val REQ_EXPORT = 12
        private const val REQ_NOTIFY = 13
        private val PRESETS = listOf(
            "FM вещание" to (101_000_000L to Modulation.WFM),
            "Любители 2 м" to (145_500_000L to Modulation.NFM),
            "PMR446" to (446_006_250L to Modulation.NFM),
            "LPD 433" to (433_075_000L to Modulation.NFM),
            "Авиа (АМ)" to (127_000_000L to Modulation.AM),
            "СВ радио (АМ)" to (999_000L to Modulation.AM),
            "Любители 20 м (USB)" to (14_230_000L to Modulation.USB),
            "Любители 40 м (LSB)" to (7_100_000L to Modulation.LSB),
            "Телеграф 40 м (CW)" to (7_030_000L to Modulation.CW),
            "DMR, рации PMR (446,1 МГц)" to (446_100_000L to Modulation.DMR),
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
    private lateinit var rdsText: TextView
    private lateinit var recButton: android.widget.Button
    private lateinit var freqFile: File
    private var freqs = mutableListOf<FreqEntry>()
    private var serviceOn = false
    private val ui = Handler(Looper.getMainLooper())
    private var sourceId = Receivers.DEMO
    private var mode = Modulation.WFM
    private var app: String? = null
    private var sstv: SstvPanel? = null
    private var aprs: AprsPanel? = null
    private var adsb: AdsbPanel? = null
    private var adsbTick = 0

    private val ticker = object : Runnable {
        override fun run() {
            updateStatus()
            ui.postDelayed(this, 200)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        app = intent.getStringExtra(EXTRA_APP)?.takeIf { it in listOf(APP_SSTV, APP_APRS, APP_ADSB) }
        prefs = getSharedPreferences(if (app == null) "sdr4noob" else "sdr4noob_$app", MODE_PRIVATE)
        sourceId = intent.getStringExtra(EXTRA_SOURCE) ?: Receivers.DEMO
        val defMode = when (app) { APP_SSTV -> "USB"; APP_APRS, APP_ADSB -> "NFM"; else -> "WFM" }
        mode = runCatching { Modulation.valueOf(prefs.getString("mode", defMode)!!) }.getOrDefault(Modulation.WFM)
        if (app == APP_ADSB || app == APP_APRS) mode = Modulation.NFM
        title = when (app) {
            APP_SSTV -> "SSTV · "; APP_APRS -> "APRS · "; APP_ADSB -> "Самолёты ADS-B · "; else -> ""
        } + Receivers.describe(this, sourceId)
        freqFile = File(filesDir, "freqman.json")
        freqs = FreqMan.load(freqFile)
        buildUi()
        startEngine()
        askNotifications()
        // приём в фоне: сервис держит процесс, пока окно радио открыто (запуск — пока окно видно)
        if (engine != null) {
            serviceOn = RadioService.start(this, notifyText(), sourceId, wifi = sourceId.startsWith("tcp:"))
            if (serviceOn) RadioService.onStop = { if (alive) finish() }
        }
    }

    /** Android 13+: без разрешения уведомлений приём в фоне работает, но значка в шторке не видно. */
    private fun askNotifications() {
        if (Build.VERSION.SDK_INT >= 33 && !prefs.getBoolean("notify_asked", false) &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            prefs.edit().putBoolean("notify_asked", true).apply()
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIFY)
        }
    }

    override fun onResume() {
        super.onResume()
        // движок этого окна остановлен новым окном радио — это окно устарело
        if (engine?.preempted == true) { finish(); return }
        ui.post(ticker)
    }
    override fun onPause() { super.onPause(); ui.removeCallbacks(ticker) }

    override fun onDestroy() {
        val e = engine
        val owner = e != null && !e.preempted
        sstv?.stop(); aprs?.stop(); adsb?.stop()
        e?.let { it.stopAudioRec(); it.stopIqRec(); it.stop() }   // stop снимает listener
        engine = null
        // сервис общий: вытесненное окно не трогает его — им пользуется новое окно радио
        if (owner) RadioService.onStop = null
        if (serviceOn && owner) RadioService.stop(this)
        serviceOn = false
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
        if (app == APP_ADSB || app == APP_APRS) row.visibility = View.GONE     // режим задан: NFM для APRS, IQ для ADS-B

        infoText = Ui.text(this, "", 13f, Ui.DIM)
        root.addView(infoText)
        rdsText = Ui.text(this, "", 14f, Ui.ACCENT).apply { visibility = View.GONE; maxLines = 2 }
        root.addView(rdsText)

        spectrum = SpectrumView(this)
        root.addView(spectrum, Ui.lp(h = 0, weight = 1f, top = Ui.dp(this, 4)))
        val common = getSharedPreferences("sdr4noob", MODE_PRIVATE)    // место приёмника — общее для режимов
        val panel = when (app) {
            APP_SSTV -> SstvPanel(this, prefs, ::toast).also { p ->
                sstv = p
                p.onPickWav = {
                    startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                        .setType("audio/*"), REQ_SSTV_WAV)
                }
            }.view
            APP_APRS -> AprsPanel(this, common, ::toast).also { aprs = it }.view
            APP_ADSB -> AdsbPanel(this, common, ::toast).also { adsb = it }.view
            else -> null
        }
        panel?.let { root.addView(it, Ui.lp(h = 0, weight = if (app == APP_ADSB) 3f else 2f, top = Ui.dp(this, 4))) }

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
        if (app == APP_ADSB) v.visibility = View.GONE        // ADS-B без звука
        val s = Ui.hbox(this)
        s.addView(Ui.text(this, "Шумодав", 13f).apply { minWidth = Ui.dp(this@RadioActivity, 110) })
        squelchBar = SeekBar(this).apply {
            max = 100; progress = prefs.getInt("squelch", 0)
            setOnSeekBarChangeListener(seek { p -> engine?.setSquelch(squelchDb(p)); prefs.edit().putInt("squelch", p).apply() })
        }
        s.addView(squelchBar, Ui.lp(0, weight = 1f))
        root.addView(s)
        if (app != null) s.visibility = View.GONE     // декодерам нужен звук и в паузах — шумодав выключен (как на ПК)

        val b = Ui.hbox(this)
        biasBox = CheckBox(this).apply {
            text = "Питание антенны (bias-T)"; setTextColor(0xFFFFFFFF.toInt()); visibility = View.GONE
            setOnCheckedChangeListener { _, on -> engine?.setBiasTee(on) }
        }
        b.addView(biasBox, Ui.lp(0, weight = 1f))
        root.addView(b)
        val btns = Ui.hbox(this)
        btns.addView(Ui.button(this, "Станции…") { choosePreset() }, Ui.lp(0, weight = 1f))
        btns.addView(Ui.button(this, "Мои частоты…") { showFreqMan() }, Ui.lp(0, weight = 1f))
        recButton = Ui.button(this, "● Звук в WAV") { toggleAudioRec() }
        btns.addView(recButton, Ui.lp(0, weight = 1f))
        root.addView(btns)
        if (app == APP_ADSB) { btns.visibility = View.GONE; top.visibility = View.GONE }

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
            if (app == APP_ADSB && sourceId == Receivers.DEMO) AdsbDemoSource() else Receivers.sourceFor(this, sourceId)
        } catch (e: SdrException) {
            fail(e.message ?: "Ошибка приёмника"); return
        }
        // по умолчанию 1,2 МГц обзора: вдвое меньше нагрузка и расход батареи; до 2,88 МГц — в меню
        // ADS-B: импульсы 0,5 мкс — нужна полоса от 2 МГц (2,4 МГц — ближайшая частота радиорежима), 1090 МГц
        val rate = if (app == APP_ADSB) 2_400_000
            else prefs.getInt("rate", 1_200_000).takeIf { it in RadioDemod.RADIO_RATES } ?: 1_200_000
        val freq = if (app == APP_ADSB) 1_090_000_000L else prefs.getLong("freq_${mode.name}", defaultFreq(mode))
        val e = RadioEngine(src, rate, freq, mode)
        e.setBandwidth(prefs.getInt("bw_${mode.name}", mode.defaultBw))
        e.setAgc(prefs.getBoolean("agc", true))
        e.volume = prefs.getInt("volume", 80) / 100f
        e.setStereo(prefs.getBoolean("stereo", true))
        e.setSquelch(if (app != null) -200f else squelchDb(prefs.getInt("squelch", 0)))
        prefs.getInt("ppm", 0).takeIf { it != 0 }?.let(e::setPpm)
        e.listener = object : RadioEngine.Listener {
            override fun onOpened(engine: RadioEngine) = runOnUiThread { opened(engine) }
            override fun onSpectrum() = spectrum.postInvalidate()
            override fun onError(message: String) = runOnUiThread { fail(message) }
            override fun onRecordStopped(message: String) = runOnUiThread {
                if (alive) { toast(message); recButton.text = if (engine?.audioRecording != null) "■ Стоп записи" else "● Звук в WAV" }
            }
            override fun onScanStopped(message: String) = runOnUiThread { if (alive) toast(message) }
        }
        engine = e
        spectrum.engine = e
        applyModeUi()
        spectrum.onTuned = { f -> saveFreq(f); updateFreq() }
        updateFreq()
        sstv?.start(e); aprs?.start(e); adsb?.start(e)
        e.start()
        if (app == APP_ADSB) e.setCenter(1_090_000_000L)     // кадры Mode S — в центре полосы, без смещения VFO
    }

    private fun opened(e: RadioEngine) {
        if (!alive || e !== engine || e.preempted) return
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

    private fun notifyText() = "${Ui.mhz(engine?.vfoHz ?: 0)} · ${mode.name} · ${Receivers.describe(this, sourceId)}"

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
        if (!alive || engine?.preempted == true) return
        statusText.text = message
        AlertDialog.Builder(this)
            .setTitle("Приёмник")
            .setMessage(message)
            .setPositiveButton("К выбору приёмника") { _, _ -> finish() }
            .setNegativeButton("Остаться", null)
            .show()
    }

    private fun defaultFreq(m: Modulation) = if (app == APP_APRS) 144_800_000L else if (app == APP_SSTV && m == Modulation.NFM) 145_800_000L else when (m) {
        Modulation.WFM -> 101_000_000L
        Modulation.NFM -> 145_500_000L
        Modulation.AM -> 127_000_000L
        Modulation.USB -> 14_230_000L
        Modulation.LSB -> 7_100_000L
        Modulation.CW -> 7_030_000L
        Modulation.DMR -> 446_100_000L
    }

    private fun setMode(m: Modulation, freq: Long? = null, bw: Int? = null) {
        mode = m
        prefs.edit().putString("mode", m.name).apply()
        if (modeSpin.selectedItemPosition != m.ordinal) modeSpin.setSelection(m.ordinal)
        val e = engine ?: return
        e.setMode(m, bw ?: prefs.getInt("bw_${m.name}", m.defaultBw))
        val f = freq ?: prefs.getLong("freq_${m.name}", defaultFreq(m))
        e.tune(f)
        saveFreq(f)
        applyModeUi()
        updateFreq()
    }

    private fun applyModeUi() {
        spectrum.snapHz = when (mode) {
            Modulation.WFM -> 100_000L; Modulation.NFM -> 6_250L; Modulation.AM -> 1_000L
            Modulation.USB, Modulation.LSB -> 100L; Modulation.CW -> 10L; Modulation.DMR -> 6_250L
        }
        val bw = engine?.bandwidth ?: mode.defaultBw
        spectrum.channelBw = bw
        spectrum.channelShift = when (mode) {
            Modulation.USB -> (RadioDemod.SSB_LOW + bw / 2).toInt()
            Modulation.LSB -> -(RadioDemod.SSB_LOW + bw / 2).toInt()
            else -> 0
        }
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
        if (serviceOn) RadioService.update(this, notifyText())
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
        if (app == APP_SSTV) {
            val pr = SstvPanel.PRESETS
            AlertDialog.Builder(this).setTitle("Частоты SSTV").setItems(pr.map { it.first }.toTypedArray()) { _, i ->
                val (_, f, m) = pr[i]
                val mm = Modulation.valueOf(m)
                if (mm != mode) setMode(mm, f) else { engine?.tune(f); saveFreq(f); updateFreq() }
            }.show()
            return
        }
        if (app == APP_APRS) {
            val pr = AprsPanel.PRESETS
            AlertDialog.Builder(this).setTitle("Частоты APRS").setItems(pr.map { it.first }.toTypedArray()) { _, i ->
                val f = pr[i].second
                engine?.tune(f); saveFreq(f); updateFreq()
            }.show()
            return
        }
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
        if (adsb != null && ++adsbTick % 3 == 0) adsb?.refresh()
        val st = when {
            mode != Modulation.WFM -> ""
            e.stereoLock -> " · СТЕРЕО"
            else -> " · моно"
        }
        val sq = if (!e.squelchOpen) " · шумодав закрыт" else ""
        infoText.text = (if (app == APP_ADSB) "1090 МГц" else String.format(Locale.ROOT, "Уровень %.0f dBFS", e.levelDb)) + st + sq +
            " · полоса " + Ui.mhz(e.sampleRate.toLong()).replace(" МГц", " МГц обзора")
        e.scanner?.let { s ->
            infoText.text = "Поиск DMR: проход ${s.passes}, окно ${e.scanWindow + 1} из ${s.centers.size}, " +
                "найдено DMR: ${s.dmrFound()}"
            e.scanLog.peek()?.let { rdsText.text = e.scanLog.toList().takeLast(5).joinToString("\n"); rdsText.visibility = View.VISIBLE }
            return
        }
        val rec = listOfNotNull(e.audioRecording?.let { w ->
                "звук ${w.seconds.toInt()} с" + (w.dropped.get().takeIf { it > 0 }?.let { " (потеряно при записи: ${"%.1f".format(it / 2.0 / RadioDemod.AUDIO_RATE)} с)" } ?: "") },
            e.iqRecording?.let { "IQ ${it.seconds.toInt()} с" }).joinToString(", ")
        statusText.text = e.label + (if (e.dropped > 0) " · потеряно отсчётов: ${e.dropped}" else "") +
            (if (rec.isNotEmpty()) " · ● запись: $rec" else "")
        if (mode == Modulation.DMR) { showDmr(e); return }
        // RDS (только ЧМ-вещание)
        if (mode == Modulation.WFM && e.rds.synced || mode == Modulation.WFM && e.rds.ps.isNotEmpty()) {
            val r = e.rds.state()
            rdsText.text = listOf(r.ps.trim(), r.ptyName.takeIf { it.isNotEmpty() && it != "—" }, r.rt.takeIf { it.isNotEmpty() })
                .filterNotNull().filter { it.isNotEmpty() }.joinToString(" · ").let { "RDS: $it" }
            rdsText.visibility = View.VISIBLE
        } else rdsText.visibility = View.GONE
    }

    /** DMR: синхронизация, кто говорит в слотах, последние события журнала (как панель DMR на ПК). */
    private val dmrLog = ArrayDeque<String>()
    private fun showDmr(e: RadioEngine) {
        val d = e.dmr
        if (d == null) { rdsText.visibility = View.GONE; return }
        while (true) {
            val ev = e.dmrEvents.poll() ?: break
            dmrLog.addFirst("${java.text.SimpleDateFormat("HH:mm:ss", Locale.ROOT).format(java.util.Date())} " +
                (if (ev.slot > 0) "слот ${ev.slot}: " else "") + ev.text)
            while (dmrLog.size > 5) dmrLog.removeLast()
        }
        val st = d.status()
        val head = if (!st.locked) "DMR: нет синхронизации" else {
            val sl = st.slots.entries.joinToString(" · ") { (n, s) ->
                "слот $n: " + if (s.active) "${if (s.src != 0) s.src else "?"} → ${if (s.group) "TG " else ""}${if (s.dst != 0) s.dst else "?"}" +
                    (if (s.encrypted) " (шифр.)" else "") else s.last.ifEmpty { "—" }
            }
            "DMR ${st.mode}, синхро ${st.sync} %, CC ${st.slots.values.firstNotNullOfOrNull { it.cc } ?: "?"} · $sl" +
                (if (!st.mbelib) " · голос: нет mbelib" else "")
        }
        rdsText.maxLines = 7
        rdsText.text = (listOf(head) + dmrLog).joinToString("\n")
        rdsText.visibility = View.VISIBLE
    }

    // ---------------- поиск DMR ----------------
    private fun startScan() {
        val e = engine ?: return
        if (!e.running) { toast("Приёмник не работает"); return }
        val bands = io.github.mikeileech.sdr4noob.dsp.DmrScanner.BANDS
        AlertDialog.Builder(this).setTitle("Поиск DMR — где искать")
            .setItems(bands.map { it.first }.toTypedArray()) { _, i ->
                val (_, lo, hi) = bands[i]
                if (!e.startScan(io.github.mikeileech.sdr4noob.dsp.DmrScanner(e.sampleRate, lo, hi))) {
                    toast("В записи IQ поиск невозможен: центр частоты задаёт файл"); return@setItems
                }
                toast("Поиск идёт по кругу, звук на это время выключен. Результаты — меню ⋮ → «Поиск DMR: результаты…»")
            }.show()
    }

    private fun showScanResults() {
        val e = engine ?: return
        val s = e.scanner ?: return
        val rows = s.snapshot()
        val items = rows.map { "${Ui.mhz(it.freq.toLong())} — ${it.text}" +
            (if (it.ids.isNotEmpty()) "\n   " + it.ids.joinToString(", ") else "") }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle("Найдено (проход ${s.passes}, окон ${s.centers.size}); нажмите — слушать")
            .setItems(items) { _, i ->
                val r = rows[i]
                e.stopScan()
                setMode(if (r.kind == "dmr") Modulation.DMR else Modulation.NFM, r.freq.toLong())
            }
            .setPositiveButton("Остановить поиск") { _, _ -> e.stopScan() }
            .setNegativeButton("Искать дальше", null)
            .show()
    }

    // ---------------- запись ----------------
    private fun toggleAudioRec() {
        val e = engine ?: return
        if (e.audioRecording != null) {
            e.stopAudioRec(); recButton.text = "● Звук в WAV"; return
        }
        if (!e.running) { toast("Приёмник не работает — записывать нечего"); return }
        var out: Storage.Out? = null
        try {
            out = Storage.open(this, Storage.AUDIO, Storage.audioName(e.vfoHz))
            e.startAudioRec(WavWriter(out.channel, RadioDemod.AUDIO_RATE, 2, onClose = out.finish))
            recButton.text = "■ Стоп записи"
            toast("Запись звука: ${out.shownPath}")
        } catch (ex: Exception) {
            out?.let { o -> runCatching { o.channel.close() }; runCatching { o.finish() } }
            toast("Не удалось начать запись: ${ex.message}")
        }
    }

    private fun toggleIqRec() {
        val e = engine ?: return
        if (e.iqRecording != null) { e.stopIqRec(); return }
        if (!e.running) { toast("Приёмник не работает — записывать нечего"); return }
        var out: Storage.Out? = null
        try {
            out = Storage.open(this, Storage.IQ, Storage.iqName(e.centerHz, e.sampleRate))
            e.startIqRec(WavWriter(out.channel, e.sampleRate, 2, strict = true, onClose = out.finish))
            val mbMin = e.sampleRate * 4L * 60 / 1_000_000
            toast("Запись IQ (≈$mbMin МБ в минуту; перестройка центра её остановит): ${out.shownPath}")
        } catch (ex: Exception) {
            out?.let { o -> runCatching { o.channel.close() }; runCatching { o.finish() } }
            toast("Не удалось начать запись IQ: ${ex.message}")
        }
    }

    private fun toast(s: String) = android.widget.Toast.makeText(this, s, android.widget.Toast.LENGTH_LONG).show()

    // ---------------- менеджер частот ----------------
    private fun showFreqMan() {
        val e = engine
        val sorted = freqs.sortedWith(compareBy({ it.group }, { it.freq }))
        val items = sorted.map { "${it.group} · ${it.name} — ${Ui.mhz(it.freq.toLong())} ${it.mode}" }.toTypedArray()
        val d = AlertDialog.Builder(this)
            .setTitle(if (sorted.isEmpty()) "Мои частоты — пока пусто" else "Мои частоты (долгое нажатие — удалить)")
            .setItems(items) { _, i -> tuneTo(sorted[i]) }
            .setPositiveButton("Добавить текущую") { _, _ -> if (e != null) addCurrent(e) }
            .setNeutralButton("Импорт/экспорт…") { _, _ -> importExport() }
            .setNegativeButton("Закрыть", null)
            .create()
        d.setOnShowListener {
            d.listView?.setOnItemLongClickListener { _, _, i, _ ->
                val it = sorted[i]
                AlertDialog.Builder(this).setMessage("Удалить «${it.name}»?")
                    .setPositiveButton("Удалить") { _, _ -> freqs.remove(it); saveFreqs(); d.dismiss() }
                    .setNegativeButton("Отмена", null).show()
                true
            }
        }
        d.show()
    }

    private fun saveFreqs() {
        try { FreqMan.save(freqFile, freqs) } catch (ex: Exception) { toast("Не удалось сохранить список: ${ex.message}") }
    }

    private fun tuneTo(f: FreqEntry) {
        val m = FreqMan.modulation(f)
        if (m != mode) setMode(m, f.freq.toLong(), f.bw.toInt())
        else { engine?.setBandwidth(f.bw.toInt()); engine?.tune(f.freq.toLong()); saveFreq(f.freq.toLong()); updateFreq() }
        prefs.edit().putInt("bw_${m.name}", engine?.bandwidth ?: f.bw.toInt()).apply()
        applyModeUi()
    }

    private fun addCurrent(e: RadioEngine) {
        val name = EditText(this).apply {
            setText(e.rds.ps.trim().ifEmpty { Ui.mhz(e.vfoHz) })
            selectAll()
        }
        val group = EditText(this).apply { hint = "Группа (например, FM или Рации)"; setText(FreqMan.DEFAULT_GROUP) }
        val box = Ui.vbox(this, 16).apply { addView(name); addView(group) }
        AlertDialog.Builder(this).setTitle("Сохранить частоту").setView(box)
            .setPositiveButton("Сохранить") { _, _ ->
                freqs.add(FreqMan.normalize(name.text.toString(), e.vfoHz.toDouble(), mode.name, e.bandwidth.toDouble(),
                    group.text.toString()))
                saveFreqs()
            }.setNegativeButton("Отмена", null).show()
    }

    private fun importExport() {
        AlertDialog.Builder(this).setTitle("Мои частоты")
            .setItems(arrayOf("Импорт из CSV или XML SDR# (добавит без повторов)", "Экспорт в CSV (Excel, SDR4NOOB на ПК)",
                "Экспорт в XML SDR#")) { _, i ->
                when (i) {
                    0 -> startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                        .setType("*/*"), REQ_IMPORT)
                    else -> {
                        val xml = i == 2
                        prefs.edit().putBoolean("export_xml", xml).apply()
                        startActivityForResult(Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                            .setType(if (xml) "text/xml" else "text/csv")
                            .putExtra(Intent.EXTRA_TITLE, if (xml) "Frequencies.xml" else "SDR4NOOB-частоты.csv"), REQ_EXPORT)
                    }
                }
            }.show()
    }

    @Deprecated("Activity.onActivityResult")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        val uri: Uri = data?.data ?: return
        if (resultCode != RESULT_OK) return
        if (requestCode == REQ_SSTV_WAV) { sstv?.decodeWav(uri); return }
        try {
            when (requestCode) {
                REQ_IMPORT -> {
                    // список частот — килобайты; большие файлы (например, записи IQ в той же папке) не читаем
                    val limit = 5 shl 20
                    val bytes = contentResolver.openInputStream(uri)!!.use { s ->
                        val buf = java.io.ByteArrayOutputStream()
                        val chunk = ByteArray(65536)
                        while (true) {
                            val k = s.read(chunk)
                            if (k < 0) break
                            buf.write(chunk, 0, k)
                            if (buf.size() > limit) throw IllegalArgumentException("файл больше 5 МБ — это не список частот")
                        }
                        buf.toByteArray()
                    }
                    val n = FreqMan.merge(freqs, FreqMan.importAny(bytes))
                    saveFreqs()
                    toast("Добавлено записей: $n")
                }
                REQ_EXPORT -> {
                    val xml = prefs.getBoolean("export_xml", false)
                    contentResolver.openOutputStream(uri, "wt")!!.use { os ->
                        if (xml) os.write(FreqMan.exportSdrSharpXml(freqs).toByteArray())
                        else os.write(("\uFEFF" + FreqMan.exportCsv(freqs)).toByteArray())
                    }
                    toast("Сохранено записей: ${freqs.size}")
                }
            }
        } catch (ex: Throwable) {
            toast("Не получилось: ${ex.message}")
        }
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        if (app == APP_ADSB) {
            menu.add(0, 2, 0, "Поправка частоты (ppm)…")
            menu.add(0, 3, 0, "Справка")
            return true
        }
        menu.add(0, 1, 0, "Полоса обзора…")
        menu.add(0, 4, 0, "Полоса канала…")
        menu.add(0, 5, 0, "АРУ звука (AM, SSB, CW)").setCheckable(true).isChecked = prefs.getBoolean("agc", true)
        menu.add(0, 6, 0, "Запись IQ (базовая полоса)")
        menu.add(0, 7, 0, "Поиск DMR…")
        menu.add(0, 2, 0, "Поправка частоты (ppm)…")
        menu.add(0, 3, 0, "Справка")
        return true
    }

    override fun onPrepareOptionsMenu(menu: Menu): Boolean {
        menu.findItem(6)?.title = if (engine?.iqRecording != null) "Остановить запись IQ" else "Запись IQ (базовая полоса)"
        menu.findItem(7)?.title = if (engine?.scanner != null) "Поиск DMR: результаты…" else "Поиск DMR…"
        return super.onPrepareOptionsMenu(menu)
    }

    private fun fmtBw(hz: Int) = if (hz >= 10_000) "${hz / 1000} кГц" else String.format(Locale.ROOT, "%.1f кГц", hz / 1000.0).replace('.', ',')

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
            4 -> {
                val e = engine ?: return true
                val steps = listOf(0.25, 0.5, 0.75, 1.0, 1.5, 2.0).map { (mode.defaultBw * it).toInt() }
                    .filter { it in mode.minBw..mode.maxBw }.plus(listOf(mode.minBw, mode.maxBw)).distinct().sorted()
                AlertDialog.Builder(this).setTitle("Полоса канала (сейчас ${fmtBw(e.bandwidth)})")
                    .setItems(steps.map { fmtBw(it) }.toTypedArray()) { _, i ->
                        e.setBandwidth(steps[i]); prefs.edit().putInt("bw_${mode.name}", steps[i]).apply(); applyModeUi()
                    }.show()
            }
            5 -> {
                val on = !item.isChecked
                item.isChecked = on
                prefs.edit().putBoolean("agc", on).apply(); engine?.setAgc(on)
            }
            6 -> toggleIqRec()
            7 -> if (engine?.scanner != null) showScanResults() else startScan()
            else -> return super.onOptionsItemSelected(item)
        }
        return true
    }
}
