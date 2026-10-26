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
import io.github.mikeileech.sdr4noob.i18n.N_
import io.github.mikeileech.sdr4noob.i18n.num
import io.github.mikeileech.sdr4noob.i18n.tr
import io.github.mikeileech.sdr4noob.dsp.RadioDemod
import io.github.mikeileech.sdr4noob.radio.FreqEntry
import io.github.mikeileech.sdr4noob.radio.FreqMan
import io.github.mikeileech.sdr4noob.radio.RadioEngine
import io.github.mikeileech.sdr4noob.radio.RadioService
import io.github.mikeileech.sdr4noob.radio.Storage
import io.github.mikeileech.sdr4noob.radio.WavWriter
import java.io.File
import io.github.mikeileech.sdr4noob.sdr.AdsbDemoSource
import io.github.mikeileech.sdr4noob.sdr.TvDemoSource
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
        const val APP_TV = "tv"
        const val APP_SAT = "sat"
        /** Режимы, где приёмник ведёт декодер на сыром IQ: звука и ручной настройки нет. */
        private val IQ_APPS = setOf(APP_ADSB, APP_TV)
        private const val REQ_SSTV_WAV = 14
        private const val REQ_IMPORT = 11
        private const val REQ_EXPORT = 12
        private const val REQ_NOTIFY = 13
        private val PRESETS = listOf(
            N_("FM вещание") to (101_000_000L to Modulation.WFM),
            N_("Любители 2 м") to (145_500_000L to Modulation.NFM),
            "PMR446" to (446_006_250L to Modulation.NFM),
            "LPD 433" to (433_075_000L to Modulation.NFM),
            N_("Авиа (АМ)") to (127_000_000L to Modulation.AM),
            N_("СВ радио (АМ)") to (999_000L to Modulation.AM),
            N_("Любители 20 м (USB)") to (14_230_000L to Modulation.USB),
            N_("Любители 40 м (LSB)") to (7_100_000L to Modulation.LSB),
            N_("Телеграф 40 м (CW)") to (7_030_000L to Modulation.CW),
            N_("DMR, рации PMR (446,1 МГц)") to (446_100_000L to Modulation.DMR),
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
    private var tvPanel: TvPanel? = null
    private var satPanel: SatPanel? = null
    private var satTick = 0
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
        app = intent.getStringExtra(EXTRA_APP)?.takeIf { it in listOf(APP_SSTV, APP_APRS, APP_ADSB, APP_TV, APP_SAT) }
        prefs = getSharedPreferences(if (app == null) "sdr4noob" else "sdr4noob_$app", MODE_PRIVATE)
        sourceId = intent.getStringExtra(EXTRA_SOURCE) ?: Receivers.DEMO
        val defMode = when (app) { APP_SSTV -> "USB"; APP_APRS, APP_ADSB, APP_SAT -> "NFM"; else -> "WFM" }
        mode = runCatching { Modulation.valueOf(prefs.getString("mode", defMode)!!) }.getOrDefault(Modulation.WFM)
        if (app == APP_ADSB || app == APP_APRS || app == APP_TV) mode = Modulation.NFM
        // SSTV в тестовом эфире передаётся на 145,800 МГц ЧМ (как МКС): сразу туда, пока пользователь не выбрал сам
        if (app == APP_SSTV && sourceId == Receivers.DEMO && !prefs.contains("mode")) mode = Modulation.NFM
        title = when (app) {
            APP_SSTV -> "SSTV · "; APP_APRS -> "APRS · "; APP_ADSB -> tr("Самолёты ADS-B") + " · "; APP_TV -> tr("Телевидение") + " · "; APP_SAT -> tr("Спутники") + " · "; else -> ""
        } + Receivers.describe(this, sourceId)
        freqFile = File(filesDir, "freqman.json")
        freqs = FreqMan.load(freqFile)
        buildUi()
        startEngine()
        askNotifications()
        // приём в фоне: сервис держит процесс, пока окно радио открыто (запуск — пока окно видно)
        if (engine != null) {
            serviceOn = RadioService.start(this, notifyText(), sourceId, wifi = sourceId.startsWith("tcp:"), appMode = app)
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
        sstv?.stop(); aprs?.stop(); adsb?.stop(); tvPanel?.stop(); satPanel?.stop()
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
                Modulation.values().map { tr(it.title) })
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
            text = tr("Стерео"); setTextColor(0xFFFFFFFF.toInt())
            isChecked = prefs.getBoolean("stereo", true)
            setOnCheckedChangeListener { _, on -> engine?.setStereo(on); prefs.edit().putBoolean("stereo", on).apply() }
        }
        row.addView(stereoBox)
        root.addView(row)
        if (app == APP_ADSB || app == APP_APRS || app == APP_TV) row.visibility = View.GONE     // режим задан: NFM для APRS, IQ для ADS-B

        infoText = Ui.text(this, "", 13f, Ui.DIM)
        root.addView(infoText)
        rdsText = Ui.text(this, "", 14f, Ui.ACCENT).apply { visibility = View.GONE; maxLines = 2 }
        root.addView(rdsText)

        spectrum = SpectrumView(this)
        spectrum.locked = app in IQ_APPS
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
            APP_TV -> TvPanel(this, prefs, ::toast).also { tvPanel = it }.view
            APP_SAT -> SatPanel(this, prefs, common, ::toast) { f, m, bw -> setMode(m, f, bw) }.also {
                satPanel = it
            }.view
            else -> null
        }
        panel?.let { root.addView(it, Ui.lp(h = 0, weight = if (app in IQ_APPS) 3f else 2f, top = Ui.dp(this, 4))) }

        // усиление
        val g = Ui.hbox(this)
        gainText = Ui.text(this, tr("Усиление"), 13f).apply { minWidth = Ui.dp(this@RadioActivity, 110) }
        g.addView(gainText)
        gainBar = SeekBar(this).apply { max = 1 }
        g.addView(gainBar, Ui.lp(0, weight = 1f))
        gainAuto = CheckBox(this).apply { text = tr("Авто"); setTextColor(0xFFFFFFFF.toInt()) }
        g.addView(gainAuto)
        root.addView(g)

        // громкость и шумодав
        val v = Ui.hbox(this)
        v.addView(Ui.text(this, tr("Громкость"), 13f).apply { minWidth = Ui.dp(this@RadioActivity, 110) })
        val vol = SeekBar(this).apply {
            max = 100; progress = prefs.getInt("volume", 80)
            setOnSeekBarChangeListener(seek { p -> engine?.volume = p / 100f; prefs.edit().putInt("volume", p).apply() })
        }
        v.addView(vol, Ui.lp(0, weight = 1f))
        root.addView(v)
        if (app in IQ_APPS) v.visibility = View.GONE        // ADS-B и ТВ-измеритель без звука
        val s = Ui.hbox(this)
        s.addView(Ui.text(this, tr("Шумодав"), 13f).apply { minWidth = Ui.dp(this@RadioActivity, 110) })
        squelchBar = SeekBar(this).apply {
            max = 100; progress = prefs.getInt("squelch", 0)
            setOnSeekBarChangeListener(seek { p -> engine?.setSquelch(squelchDb(p)); prefs.edit().putInt("squelch", p).apply() })
        }
        s.addView(squelchBar, Ui.lp(0, weight = 1f))
        root.addView(s)
        if (app != null) s.visibility = View.GONE     // декодерам нужен звук и в паузах — шумодав выключен (как на ПК)

        val b = Ui.hbox(this)
        biasBox = CheckBox(this).apply {
            text = tr("Питание антенны (bias-T)"); setTextColor(0xFFFFFFFF.toInt()); visibility = View.GONE
            setOnCheckedChangeListener { _, on -> engine?.setBiasTee(on) }
        }
        b.addView(biasBox, Ui.lp(0, weight = 1f))
        root.addView(b)
        val btns = Ui.hbox(this)
        btns.addView(Ui.button(this, tr("Станции…")) { choosePreset() }, Ui.lp(0, weight = 1f))
        btns.addView(Ui.button(this, tr("Мои частоты…")) { showFreqMan() }, Ui.lp(0, weight = 1f))
        recButton = Ui.button(this, tr("● Звук в WAV")) { toggleAudioRec() }
        btns.addView(recButton, Ui.lp(0, weight = 1f))
        root.addView(btns)
        if (app in IQ_APPS) { btns.visibility = View.GONE; top.visibility = View.GONE }

        statusText = Ui.text(this, tr("Подключение…"), 12f, Ui.DIM)
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
            if (app == APP_ADSB && sourceId == Receivers.DEMO) AdsbDemoSource()
            else if (app == APP_TV && sourceId == Receivers.DEMO) TvDemoSource()
            else Receivers.sourceFor(this, sourceId)
        } catch (e: SdrException) {
            fail(e.message ?: tr("Ошибка приёмника")); return
        }
        // по умолчанию 1,2 МГц обзора: вдвое меньше нагрузка и расход батареи; до 2,88 МГц — в меню
        // ADS-B: импульсы 0,5 мкс — нужна полоса от 2 МГц (2,4 МГц — ближайшая частота радиорежима), 1090 МГц
        // ТВ: обход мультиплекса кусками ±1 МГц — полоса не уже 2 МГц (Tv.RATES)
        val rate = if (app == APP_ADSB || app == APP_TV) 2_400_000
            else prefs.getInt("rate", 1_200_000).takeIf { it in RadioDemod.RADIO_RATES } ?: 1_200_000
        val freq = if (app == APP_ADSB) 1_090_000_000L else if (app == APP_TV) tvPanel!!.startFreq()
            else if (app == APP_SAT && !prefs.contains("freq_${mode.name}")) satPanel!!.startFreq() else prefs.getLong("freq_${mode.name}", defaultFreq(mode))
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
                if (alive) { toast(message); recButton.text = if (engine?.audioRecording != null) tr("■ Стоп записи") else tr("● Звук в WAV") }
            }
            override fun onScanStopped(message: String) = runOnUiThread { if (alive) toast(message) }
        }
        engine = e
        spectrum.engine = e
        applyModeUi()
        spectrum.onTuned = { f -> saveFreq(f); updateFreq() }
        updateFreq()
        sstv?.start(e); aprs?.start(e); adsb?.start(e); tvPanel?.start(e); satPanel?.start(e)
        e.start()
        if (app == APP_ADSB) e.setCenter(1_090_000_000L)     // кадры Mode S — в центре полосы, без смещения VFO
    }

    private fun opened(e: RadioEngine) {
        if (!alive || e !== engine || e.preempted) return
        val gains = e.gains
        val savedGain = prefs.getInt("gain", -1)
        if (gains.isEmpty()) {
            gainBar.isEnabled = false; gainAuto.isEnabled = false; gainText.text = tr("Усиление —")
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
        tvPanel?.opened()
        statusText.text = e.label
    }

    private fun notifyText() = "${Ui.mhz(engine?.vfoHz ?: 0)} · ${mode.name} · ${Receivers.describe(this, sourceId)}"

    private fun applyGain(gains: IntArray) {
        val e = engine ?: return
        val auto = gainAuto.isChecked
        gainBar.isEnabled = !auto
        val t = gains[gainBar.progress.coerceIn(0, gains.size - 1)]
        e.setGain(if (auto) -1 else t)
        gainText.text = if (auto) tr("Усиление: авто") else tr("Усиление {g} dB", "g" to num(t / 10.0, 1))
        prefs.edit().putInt("gain", t).putBoolean("gain_auto", auto).apply()
    }

    private fun fail(message: String) {
        if (!alive || engine?.preempted == true) return
        statusText.text = message
        AlertDialog.Builder(this)
            .setTitle(tr("Приёмник"))
            .setMessage(message)
            .setPositiveButton(tr("К выбору приёмника")) { _, _ -> finish() }
            .setNegativeButton(tr("Остаться"), null)
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
            .setTitle(tr("Частота, МГц"))
            .setView(edit)
            .setPositiveButton(tr("Настроить")) { _, _ ->
                val mhz = edit.text.toString().replace(',', '.').toDoubleOrNull()
                if (mhz != null && mhz in 0.5..1766.0) {
                    val f = (mhz * 1e6).roundToLong()
                    e.tune(f); saveFreq(f); updateFreq()
                }
            }
            .setNegativeButton(tr("Отмена"), null)
            .show()
    }

    private fun choosePreset() {
        if (app == APP_SSTV) {
            val pr = SstvPanel.PRESETS
            AlertDialog.Builder(this).setTitle(tr("Частоты SSTV")).setItems(pr.map { tr(it.first) }.toTypedArray()) { _, i ->
                val (_, f, m) = pr[i]
                val mm = Modulation.valueOf(m)
                if (mm != mode) setMode(mm, f) else { engine?.tune(f); saveFreq(f); updateFreq() }
            }.show()
            return
        }
        if (app == APP_APRS) {
            val pr = AprsPanel.PRESETS
            AlertDialog.Builder(this).setTitle(tr("Частоты APRS")).setItems(pr.map { tr(it.first) }.toTypedArray()) { _, i ->
                val f = pr[i].second
                engine?.tune(f); saveFreq(f); updateFreq()
            }.show()
            return
        }
        AlertDialog.Builder(this)
            .setTitle(tr("Куда настроиться"))
            .setItems(PRESETS.map { "${tr(it.first)} — ${Ui.mhz(it.second.first)}" }.toTypedArray()) { _, i ->
                val (f, m) = PRESETS[i].second
                if (m != mode) setMode(m, f) else { engine?.tune(f); saveFreq(f); updateFreq() }
            }
            .show()
    }

    private fun updateStatus() {
        val e = engine ?: return
        if (!e.running) return
        if (adsb != null && ++adsbTick % 3 == 0) adsb?.refresh()
        if (satPanel != null && ++satTick % 5 == 0) satPanel?.refresh()          // раз в секунду: положения и доплер
        val st = when {
            mode != Modulation.WFM -> ""
            e.stereoLock -> " · " + tr("СТЕРЕО")
            else -> " · " + tr("моно")
        }
        val sq = if (!e.squelchOpen) " · " + tr("шумодав закрыт") else ""
        infoText.text = (if (app == APP_ADSB) tr("1090 МГц") else if (app == APP_TV) Ui.mhz(e.centerHz) else tr("Уровень {l} dBFS", "l" to num(e.levelDb, 0))) + st + sq +
            " · " + tr("полоса {f} обзора", "f" to Ui.mhz(e.sampleRate.toLong()))
        e.scanner?.let { s ->
            infoText.text = tr("Поиск DMR: проход {p}, окно {w} из {n}, найдено DMR: {f}",
                "p" to s.passes, "w" to e.scanWindow + 1, "n" to s.centers.size, "f" to s.dmrFound())
            e.scanLog.peek()?.let { rdsText.text = e.scanLog.toList().takeLast(5).joinToString("\n"); rdsText.visibility = View.VISIBLE }
            return
        }
        val rec = listOfNotNull(e.audioRecording?.let { w ->
                tr("звук {s} с", "s" to w.seconds.toInt()) + (w.dropped.get().takeIf { it > 0 }?.let {
                    " (" + tr("потеряно при записи: {s} с", "s" to num(it / 2.0 / RadioDemod.AUDIO_RATE, 1)) + ")" } ?: "") },
            e.iqRecording?.let { tr("IQ {s} с", "s" to it.seconds.toInt()) }).joinToString(", ")
        statusText.text = e.label + (if (e.dropped > 0) " · " + tr("потеряно отсчётов: {n}", "n" to e.dropped) else "") +
            (if (rec.isNotEmpty()) " · ● " + tr("запись: {r}", "r" to rec) else "")
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
                (if (ev.slot > 0) tr("слот {n}", "n" to ev.slot) + ": " else "") + ev.text)
            while (dmrLog.size > 5) dmrLog.removeLast()
        }
        val st = d.status()
        val head = if (!st.locked) tr("DMR: нет синхронизации") else {
            val sl = st.slots.entries.joinToString(" · ") { (n, s) ->
                tr("слот {n}", "n" to n) + ": " + if (s.active) "${if (s.src != 0) s.src else "?"} → ${if (s.group) "TG " else ""}${if (s.dst != 0) s.dst else "?"}" +
                    (if (s.encrypted) " (" + tr("шифр.") + ")" else "") else tr(s.last).ifEmpty { "—" }
            }
            tr("DMR {mode}, синхро {sync} %, CC {cc} · {slots}", "mode" to st.mode, "sync" to st.sync,
                "cc" to (st.slots.values.firstNotNullOfOrNull { it.cc } ?: "?"), "slots" to sl) +
                (if (!st.mbelib) " · " + tr("голос: нет mbelib") else "")
        }
        rdsText.maxLines = 7
        rdsText.text = (listOf(head) + dmrLog).joinToString("\n")
        rdsText.visibility = View.VISIBLE
    }

    // ---------------- поиск DMR ----------------
    private fun startScan() {
        val e = engine ?: return
        if (!e.running) { toast(tr("Приёмник не работает")); return }
        val bands = io.github.mikeileech.sdr4noob.dsp.DmrScanner.BANDS
        AlertDialog.Builder(this).setTitle(tr("Поиск DMR — где искать"))
            .setItems(bands.map { tr(it.first) }.toTypedArray()) { _, i ->
                val (_, lo, hi) = bands[i]
                if (!e.startScan(io.github.mikeileech.sdr4noob.dsp.DmrScanner(e.sampleRate, lo, hi))) {
                    toast(tr("В записи IQ поиск невозможен: центр частоты задаёт файл")); return@setItems
                }
                toast(tr("Поиск идёт по кругу, звук на это время выключен. Результаты — меню ⋮ → «Поиск DMR: результаты…»"))
            }.show()
    }

    private fun showScanResults() {
        val e = engine ?: return
        val s = e.scanner ?: return
        val rows = s.snapshot()
        val items = rows.map { "${Ui.mhz(it.freq.toLong())} — ${it.text}" +
            (if (it.ids.isNotEmpty()) "\n   " + it.ids.joinToString(", ") else "") }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle(tr("Найдено (проход {p}, окон {w}); нажмите — слушать", "p" to s.passes, "w" to s.centers.size))
            .setItems(items) { _, i ->
                val r = rows[i]
                e.stopScan()
                setMode(if (r.kind == "dmr") Modulation.DMR else Modulation.NFM, r.freq.toLong())
            }
            .setPositiveButton(tr("Остановить поиск")) { _, _ -> e.stopScan() }
            .setNegativeButton(tr("Искать дальше"), null)
            .show()
    }

    // ---------------- запись ----------------
    private fun toggleAudioRec() {
        val e = engine ?: return
        if (e.audioRecording != null) {
            e.stopAudioRec(); recButton.text = tr("● Звук в WAV"); return
        }
        if (!e.running) { toast(tr("Приёмник не работает — записывать нечего")); return }
        var out: Storage.Out? = null
        try {
            out = Storage.open(this, Storage.AUDIO, Storage.audioName(e.vfoHz))
            e.startAudioRec(WavWriter(out.channel, RadioDemod.AUDIO_RATE, 2, onClose = out.finish))
            recButton.text = tr("■ Стоп записи")
            toast(tr("Запись звука: {path}", "path" to out.shownPath))
        } catch (ex: Exception) {
            out?.let { o -> runCatching { o.channel.close() }; runCatching { o.finish() } }
            toast(tr("Не удалось начать запись: {error}", "error" to ex.message))
        }
    }

    private fun toggleIqRec() {
        val e = engine ?: return
        if (e.iqRecording != null) { e.stopIqRec(); return }
        if (!e.running) { toast(tr("Приёмник не работает — записывать нечего")); return }
        var out: Storage.Out? = null
        try {
            out = Storage.open(this, Storage.IQ, Storage.iqName(e.centerHz, e.sampleRate))
            e.startIqRec(WavWriter(out.channel, e.sampleRate, 2, strict = true, onClose = out.finish))
            val mbMin = e.sampleRate * 4L * 60 / 1_000_000
            toast(tr("Запись IQ (≈{mb} МБ в минуту; перестройка центра её остановит): {path}", "mb" to mbMin, "path" to out.shownPath))
        } catch (ex: Exception) {
            out?.let { o -> runCatching { o.channel.close() }; runCatching { o.finish() } }
            toast(tr("Не удалось начать запись IQ: {error}", "error" to ex.message))
        }
    }

    private fun toast(s: String) = android.widget.Toast.makeText(this, s, android.widget.Toast.LENGTH_LONG).show()

    // ---------------- менеджер частот ----------------
    private fun showFreqMan() {
        val e = engine
        val sorted = freqs.sortedWith(compareBy({ it.group }, { it.freq }))
        val items = sorted.map { "${tr(it.group)} · ${it.name} — ${Ui.mhz(it.freq.toLong())} ${it.mode}" }.toTypedArray()
        val d = AlertDialog.Builder(this)
            .setTitle(if (sorted.isEmpty()) tr("Мои частоты — пока пусто") else tr("Мои частоты (долгое нажатие — удалить)"))
            .setItems(items) { _, i -> tuneTo(sorted[i]) }
            .setPositiveButton(tr("Добавить текущую")) { _, _ -> if (e != null) addCurrent(e) }
            .setNeutralButton(tr("Импорт/экспорт…")) { _, _ -> importExport() }
            .setNegativeButton(tr("Закрыть"), null)
            .create()
        d.setOnShowListener {
            d.listView?.setOnItemLongClickListener { _, _, i, _ ->
                val it = sorted[i]
                AlertDialog.Builder(this).setMessage(tr("Удалить «{name}»?", "name" to it.name))
                    .setPositiveButton(tr("Удалить")) { _, _ -> freqs.remove(it); saveFreqs(); d.dismiss() }
                    .setNegativeButton(tr("Отмена"), null).show()
                true
            }
        }
        d.show()
    }

    private fun saveFreqs() {
        try { FreqMan.save(freqFile, freqs) } catch (ex: Exception) { toast(tr("Не удалось сохранить список: {error}", "error" to ex.message)) }
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
        val group = EditText(this).apply { hint = tr("Группа (например, FM или Рации)"); setText(tr(FreqMan.DEFAULT_GROUP)) }
        val box = Ui.vbox(this, 16).apply { addView(name); addView(group) }
        AlertDialog.Builder(this).setTitle(tr("Сохранить частоту")).setView(box)
            .setPositiveButton(tr("Сохранить")) { _, _ ->
                freqs.add(FreqMan.normalize(name.text.toString(), e.vfoHz.toDouble(), mode.name, e.bandwidth.toDouble(),
                    // группа по умолчанию хранится по-русски (как на ПК), какой бы язык ни был
                    group.text.toString().let { if (it == tr(FreqMan.DEFAULT_GROUP)) FreqMan.DEFAULT_GROUP else it }))
                saveFreqs()
            }.setNegativeButton(tr("Отмена"), null).show()
    }

    private fun importExport() {
        AlertDialog.Builder(this).setTitle(tr("Мои частоты"))
            .setItems(arrayOf(tr("Импорт из CSV или XML SDR# (добавит без повторов)"), tr("Экспорт в CSV (Excel, SDR4NOOB на ПК)"),
                tr("Экспорт в XML SDR#"))) { _, i ->
                when (i) {
                    0 -> startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                        .setType("*/*"), REQ_IMPORT)
                    else -> {
                        val xml = i == 2
                        prefs.edit().putBoolean("export_xml", xml).apply()
                        startActivityForResult(Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                            .setType(if (xml) "text/xml" else "text/csv")
                            .putExtra(Intent.EXTRA_TITLE, if (xml) "Frequencies.xml" else "SDR4NOOB-частоты.csv"), REQ_EXPORT)   // noqa: i18n
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
                            if (buf.size() > limit) throw IllegalArgumentException(tr("файл больше 5 МБ — это не список частот"))
                        }
                        buf.toByteArray()
                    }
                    val n = FreqMan.merge(freqs, FreqMan.importAny(bytes))
                    saveFreqs()
                    toast(tr("Добавлено записей: {n}", "n" to n))
                }
                REQ_EXPORT -> {
                    val xml = prefs.getBoolean("export_xml", false)
                    contentResolver.openOutputStream(uri, "wt")!!.use { os ->
                        if (xml) os.write(FreqMan.exportSdrSharpXml(freqs).toByteArray())
                        else os.write(("\uFEFF" + FreqMan.exportCsv(freqs)).toByteArray())
                    }
                    toast(tr("Сохранено записей: {n}", "n" to freqs.size))
                }
            }
        } catch (ex: Throwable) {
            toast(tr("Не получилось: {error}", "error" to ex.message))
        }
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        if (app in IQ_APPS) {
            menu.add(0, 2, 0, tr("Поправка частоты (ppm)…"))
            menu.add(0, 3, 0, tr("Справка"))
            return true
        }
        menu.add(0, 1, 0, tr("Полоса обзора…"))
        menu.add(0, 4, 0, tr("Полоса канала…"))
        menu.add(0, 5, 0, tr("АРУ звука (AM, SSB, CW)")).setCheckable(true).isChecked = prefs.getBoolean("agc", true)
        menu.add(0, 6, 0, tr("Запись IQ (базовая полоса)"))
        if (app == null) menu.add(0, 7, 0, tr("Поиск DMR…"))
        menu.add(0, 2, 0, tr("Поправка частоты (ppm)…"))
        menu.add(0, 3, 0, tr("Справка"))
        return true
    }

    override fun onPrepareOptionsMenu(menu: Menu): Boolean {
        menu.findItem(6)?.title = if (engine?.iqRecording != null) tr("Остановить запись IQ") else tr("Запись IQ (базовая полоса)")
        menu.findItem(7)?.title = if (engine?.scanner != null) tr("Поиск DMR: результаты…") else tr("Поиск DMR…")
        return super.onPrepareOptionsMenu(menu)
    }

    private fun fmtBw(hz: Int) = tr("{v} кГц", "v" to if (hz >= 10_000) (hz / 1000).toString() else num(hz / 1000.0, 1))

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        when (item.itemId) {
            1 -> {
                val rates = RadioDemod.RADIO_RATES
                AlertDialog.Builder(this).setTitle(tr("Полоса обзора (частота дискретизации)"))
                    .setItems(rates.map { tr("{v} МГц", "v" to num(it / 1e6, 2)) }.toTypedArray()) { _, i ->
                        prefs.edit().putInt("rate", rates[i]).apply()
                        engine?.setSampleRate(rates[i])
                    }.show()
            }
            2 -> {
                val edit = EditText(this).apply {
                    inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_SIGNED
                    setText(prefs.getInt("ppm", 0).toString())
                }
                AlertDialog.Builder(this).setTitle(tr("Поправка кварца, ppm")).setView(edit)
                    .setPositiveButton("OK") { _, _ ->
                        val p = edit.text.toString().toIntOrNull() ?: 0
                        prefs.edit().putInt("ppm", p).apply(); engine?.setPpm(p)
                    }.setNegativeButton(tr("Отмена"), null).show()
            }
            3 -> startActivity(Intent(this, GuideActivity::class.java).putExtra(GuideActivity.EXTRA_PAGE,
                when (app) { APP_SSTV -> "guide/mode-sstv.html"; APP_APRS -> "guide/mode-aprs.html"; APP_ADSB -> "guide/mode-adsb.html"; APP_TV -> "guide/mode-tv.html"; APP_SAT -> "guide/mode-sat.html"; else -> "guide/mode-radio.html" }))
            4 -> {
                val e = engine ?: return true
                val steps = listOf(0.25, 0.5, 0.75, 1.0, 1.5, 2.0).map { (mode.defaultBw * it).toInt() }
                    .filter { it in mode.minBw..mode.maxBw }.plus(listOf(mode.minBw, mode.maxBw)).distinct().sorted()
                AlertDialog.Builder(this).setTitle(tr("Полоса канала (сейчас {bw})", "bw" to fmtBw(e.bandwidth)))
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
