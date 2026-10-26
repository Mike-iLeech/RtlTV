package io.github.mikeileech.sdr4noob.ui

import android.app.Activity
import android.app.AlertDialog
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import io.github.mikeileech.sdr4noob.BuildConfig
import io.github.mikeileech.sdr4noob.i18n.I18n
import io.github.mikeileech.sdr4noob.i18n.tr
import io.github.mikeileech.sdr4noob.sdr.Receivers
import io.github.mikeileech.sdr4noob.sdr.RtlTcpSource
import io.github.mikeileech.sdr4noob.sdr.SdrException
import io.github.mikeileech.sdr4noob.sdr.UsbReceivers
import io.github.mikeileech.sdr4noob.update.UpdateCheck

/**
 * Экран выбора приёмника и режима — как лаунчер настольной версии (rtltv/launcher.py).
 * Подходит любой USB радиоприёмник (SDR) через OTG-переходник; для RTL2832U (например, RTL-SDR), HackRF и
 * Airspy драйверы встроены.
 */
class MainActivity : Activity() {
    companion object {
        const val ACTION_USB_PERMISSION = "io.github.mikeileech.sdr4noob.USB_PERMISSION"
        private const val REQ_IQ_FILE = 21
    }

    private lateinit var prefs: android.content.SharedPreferences
    private lateinit var devicesBox: LinearLayout
    private lateinit var selectedText: TextView
    private lateinit var updateBox: LinearLayout
    private var selected: String = Receivers.DEMO

    private val usbReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            when (intent.action) {
                ACTION_USB_PERMISSION -> {
                    val dev = usbExtra(intent)
                    val granted = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)
                    if (dev != null && granted) { select(Receivers.usb(dev.deviceName)); openRadio() }
                    else if (dev != null) toastDialog(tr("Без разрешения Android не даст программе доступ к приёмнику. Нажмите на приёмник ещё раз и разрешите доступ."))
                }
                UsbManager.ACTION_USB_DEVICE_ATTACHED, UsbManager.ACTION_USB_DEVICE_DETACHED -> refreshDevices()
            }
        }
    }

    @Suppress("DEPRECATION")
    private fun usbExtra(intent: Intent): UsbDevice? =
        if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(UsbManager.EXTRA_DEVICE, UsbDevice::class.java)
        else intent.getParcelableExtra(UsbManager.EXTRA_DEVICE)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences("sdr4noob", MODE_PRIVATE)
        selected = prefs.getString("source", Receivers.DEMO) ?: Receivers.DEMO
        buildUi()
        val f = IntentFilter(ACTION_USB_PERMISSION).apply {
            addAction(UsbManager.ACTION_USB_DEVICE_ATTACHED); addAction(UsbManager.ACTION_USB_DEVICE_DETACHED)
        }
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(usbReceiver, f, RECEIVER_NOT_EXPORTED)
        else registerReceiver(usbReceiver, f)
        // запуск по подключению приёмника (USB_DEVICE_ATTACHED): разрешение уже выдано
        usbExtra(intent)?.let { select(Receivers.usb(it.deviceName)) }
        UpdateCheck.checkInBackground(this) { info -> runOnUiThread { showUpdate(info) } }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        usbExtra(intent)?.let { select(Receivers.usb(it.deviceName)); refreshDevices() }
    }

    override fun onResume() {
        super.onResume()
        refreshDevices()
    }

    override fun onDestroy() {
        unregisterReceiver(usbReceiver)
        super.onDestroy()
    }

    private fun buildUi() {
        val root = Ui.vbox(this, 16)
        root.setBackgroundColor(Ui.BG)
        root.addView(Ui.text(this, "SDR4NOOB", 28f, Ui.ACCENT, bold = true))
        root.addView(Ui.text(this, tr("Всё в одном для начинающего радиолюбителя") + " · " +
            tr("версия {version}", "version" to BuildConfig.VERSION_NAME), 13f, Ui.DIM))
        root.addView(Ui.text(this, tr("Подключите любой USB радиоприёмник (SDR) через OTG-переходник.") + " " +
            tr("Для приёмников на RTL2832U (например, RTL-SDR Blog V4), HackRF и Airspy драйверы встроены — ничего ставить не нужно.") + " " +
            tr("Без приёмника можно попробовать тестовый сигнал."), 14f), Ui.lp(top = Ui.dp(this, 10)))

        updateBox = Ui.vbox(this, 0)
        root.addView(updateBox, Ui.lp(top = Ui.dp(this, 8)))

        val dev = Ui.card(this)
        dev.addView(Ui.text(this, tr("Приёмник"), 17f, bold = true))
        selectedText = Ui.text(this, "", 14f, Ui.ACCENT)
        dev.addView(selectedText, Ui.lp(top = Ui.dp(this, 4)))
        devicesBox = Ui.vbox(this, 0)
        dev.addView(devicesBox, Ui.lp(top = Ui.dp(this, 6)))
        val row = Ui.hbox(this)
        row.addView(Ui.button(this, tr("Сетевой rtl_tcp…")) { askTcp() }, Ui.lp(0, weight = 1f))
        row.addView(Ui.button(this, tr("Тестовый сигнал")) { select(Receivers.DEMO) }, Ui.lp(0, weight = 1f))
        dev.addView(row)
        dev.addView(Ui.button(this, tr("Файл IQ… (запись с ПК или телефона)")) {
            startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*"), REQ_IQ_FILE)
        })
        root.addView(dev, Ui.lp(top = Ui.dp(this, 12)))

        val modes = Ui.card(this)
        modes.addView(Ui.text(this, tr("Режимы"), 17f, bold = true))
        modes.addView(Ui.button(this, tr("📻  Радио и спектр: FM с RDS, NFM, AM, SSB, CW, DMR, водопад")) { openRadio() }, Ui.lp(top = Ui.dp(this, 6)))
        modes.addView(Ui.button(this, tr("🖼  SSTV — картинки с МКС и любителей (Robot, Martin, Scottie, PD)")) { openRadio(RadioActivity.APP_SSTV) })
        modes.addView(Ui.button(this, tr("✈  Самолёты ADS-B на карте (1090 МГц)")) { openRadio(RadioActivity.APP_ADSB) })
        modes.addView(Ui.button(this, tr("📍  APRS — метки любителей на карте (144,800 МГц)")) { openRadio(RadioActivity.APP_APRS) })
        modes.addView(Ui.button(this, tr("📺  Телевидение: измеритель DVB-T/T2, аналоговая картинка, поиск каналов")) { openRadio(RadioActivity.APP_TV) })
        modes.addView(Ui.button(this, tr("🛰  Спутники: пролёты, приём с доплером, «Метеор» LRPT")) { openRadio(RadioActivity.APP_SAT) })
        modes.addView(Ui.text(this, tr("Все режимы версии для Windows. Без приёмника каждый режим можно попробовать на тестовом сигнале."),
            12f, Ui.DIM), Ui.lp(top = Ui.dp(this, 4)))
        root.addView(modes, Ui.lp(top = Ui.dp(this, 12)))

        val more = Ui.hbox(this)
        more.addView(Ui.button(this, tr("Справка для начинающих")) { startActivity(Intent(this, GuideActivity::class.java)) },
            Ui.lp(0, weight = 1f))
        more.addView(Ui.button(this, tr("Об Android-версии")) {
            startActivity(Intent(this, GuideActivity::class.java).putExtra(GuideActivity.EXTRA_PAGE, "android/index.html"))
        }, Ui.lp(0, weight = 1f))
        root.addView(more, Ui.lp(top = Ui.dp(this, 12)))
        // настройки — отдельным рядом: три кнопки в ряд на узком экране переносят подписи по слогам
        root.addView(Ui.button(this, tr("Настройки: папка данных, место приёмника, обновления")) {
            startActivity(Intent(this, SettingsActivity::class.java))
        }, Ui.lp(top = Ui.dp(this, 4)))

        // язык интерфейса: как в системе / русский / English — смена пересоздаёт экран
        root.addView(Ui.button(this, tr("Язык: {lang}", "lang" to languageName(I18n.configured(this)))) { askLanguage() },
            Ui.lp(top = Ui.dp(this, 8)))

        val scroll = ScrollView(this)
        scroll.setBackgroundColor(Ui.BG)
        scroll.addView(root)
        setContentView(scroll)
    }

    private fun refreshDevices() {
        devicesBox.removeAllViews()
        val list = UsbReceivers.attached(this)
        if (list.isEmpty()) {
            devicesBox.addView(Ui.text(this, tr("USB-приёмников не найдено. Подключите приёмник через OTG-переходник (на некоторых телефонах OTG нужно включить в настройках)."), 13f, Ui.DIM))
        }
        for ((d, m) in list) {
            val status = if (m.kind.builtIn) tr("встроенный драйвер") else m.kind.status
            val b = Ui.button(this, "🔌  ${m.name} · $status") { chooseUsb(d) }
            b.gravity = Gravity.START or Gravity.CENTER_VERTICAL
            devicesBox.addView(b)
        }
        // выбранный USB-приёмник отключили — показываем, но открыть не получится
        selectedText.text = tr("Выбран: {receiver}", "receiver" to Receivers.describe(this, selected))
    }

    private fun chooseUsb(d: UsbDevice) {
        val m = UsbReceivers.model(d.vendorId, d.productId)
        if (m != null && !m.kind.builtIn) {
            toastDialog("${m.name}: ${m.kind.title}.\n\n" + tr("В Android-версии пока работают приёмники на RTL2832U, HackRF, Airspy R2/Mini и сетевой rtl_tcp. Остальные USB радиоприёмники (SDR) поддерживаются в версии для Windows."))
            return
        }
        val um = getSystemService(USB_SERVICE) as UsbManager
        if (um.hasPermission(d)) { select(Receivers.usb(d.deviceName)); openRadio(); return }
        val flags = if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0
        val pi = PendingIntent.getBroadcast(this, 0, Intent(ACTION_USB_PERMISSION).setPackage(packageName), flags)
        um.requestPermission(d, pi)
    }

    private fun askTcp() {
        val edit = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setText(prefs.getString("tcp", "192.168.1.10:1234"))
        }
        AlertDialog.Builder(this)
            .setTitle(tr("Сетевой rtl_tcp"))
            .setMessage(tr("Адрес сервера rtl_tcp (хост:порт). Приёмник подключён к другому компьютеру, на котором запущен rtl_tcp -a 0.0.0.0."))
            .setView(edit)
            .setPositiveButton(tr("Подключить")) { _, _ ->
                val t = edit.text.toString()
                try {
                    RtlTcpSource.parse(t)
                    prefs.edit().putString("tcp", t).apply()
                    select(Receivers.tcp(t)); openRadio()
                } catch (e: SdrException) { toastDialog(e.message ?: "") }
            }
            .setNegativeButton(tr("Отмена"), null)
            .show()
    }

    private fun select(id: String) {
        selected = id
        prefs.edit().putString("source", id).apply()
        if (::selectedText.isInitialized) selectedText.text = tr("Выбран: {receiver}", "receiver" to Receivers.describe(this, id))
    }

    private fun openRadio(app: String? = null) {
        startActivity(Intent(this, RadioActivity::class.java).putExtra(RadioActivity.EXTRA_SOURCE, selected)
            .putExtra(RadioActivity.EXTRA_APP, app))
    }

    @Deprecated("Activity.onActivityResult")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        val uri = data?.data ?: return
        if (requestCode != REQ_IQ_FILE || resultCode != RESULT_OK) return
        // доступ к файлу и после перезапуска программы (последний выбранный источник запоминается)
        runCatching { contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) }
        select(Receivers.file(uri.toString()))
        openRadio()
    }

    private fun toastDialog(msg: String) {
        AlertDialog.Builder(this).setMessage(msg).setPositiveButton(tr("Понятно"), null).show()
    }

    private fun languageName(choice: String) = when (choice) {
        "ru" -> "Русский"   // noqa: i18n — название языка всегда на нём самом
        "en" -> "English"
        else -> tr("как в системе")
    }

    private fun askLanguage() {
        val choices = listOf(I18n.AUTO, "ru", "en")
        val cur = choices.indexOf(I18n.configured(this)).coerceAtLeast(0)
        AlertDialog.Builder(this)
            .setTitle(tr("Язык интерфейса"))
            .setSingleChoiceItems(choices.map { languageName(it) }.toTypedArray(), cur) { d, which ->
                d.dismiss()
                if (which != cur) {
                    I18n.choose(this, choices[which])
                    recreate()
                }
            }
            .setNegativeButton(tr("Отмена"), null)
            .show()
    }

    private fun showUpdate(info: UpdateCheck.Info) {
        updateBox.removeAllViews()
        val c = Ui.card(this)
        c.addView(Ui.text(this, tr("Вышла версия {version}", "version" to info.version), 15f, Ui.ACCENT, bold = true))
        c.addView(Ui.button(this, tr("Скачать APK ({size} МБ)", "size" to info.sizeMb)) {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(info.apkUrl)))
        })
        updateBox.addView(c)
    }
}
