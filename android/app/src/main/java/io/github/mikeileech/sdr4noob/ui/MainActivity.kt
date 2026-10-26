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
                    else if (dev != null) toastDialog("Без разрешения Android не даст программе доступ к приёмнику. " +
                        "Нажмите на приёмник ещё раз и разрешите доступ.")
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
        root.addView(Ui.text(this, "Всё в одном для начинающего радиолюбителя · версия ${BuildConfig.VERSION_NAME}", 13f, Ui.DIM))
        root.addView(Ui.text(this, "Подключите любой USB радиоприёмник (SDR) через OTG-переходник. " +
            "Для приёмников на RTL2832U (например, RTL-SDR Blog V4), HackRF и Airspy драйверы встроены — ничего ставить не нужно. " +
            "Без приёмника можно попробовать тестовый сигнал.", 14f), Ui.lp(top = Ui.dp(this, 10)))

        updateBox = Ui.vbox(this, 0)
        root.addView(updateBox, Ui.lp(top = Ui.dp(this, 8)))

        val dev = Ui.card(this)
        dev.addView(Ui.text(this, "Приёмник", 17f, bold = true))
        selectedText = Ui.text(this, "", 14f, Ui.ACCENT)
        dev.addView(selectedText, Ui.lp(top = Ui.dp(this, 4)))
        devicesBox = Ui.vbox(this, 0)
        dev.addView(devicesBox, Ui.lp(top = Ui.dp(this, 6)))
        val row = Ui.hbox(this)
        row.addView(Ui.button(this, "Сетевой rtl_tcp…") { askTcp() }, Ui.lp(0, weight = 1f))
        row.addView(Ui.button(this, "Тестовый сигнал") { select(Receivers.DEMO) }, Ui.lp(0, weight = 1f))
        dev.addView(row)
        dev.addView(Ui.button(this, "Файл IQ… (запись с ПК или телефона)") {
            startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*"), REQ_IQ_FILE)
        })
        root.addView(dev, Ui.lp(top = Ui.dp(this, 12)))

        val modes = Ui.card(this)
        modes.addView(Ui.text(this, "Режимы", 17f, bold = true))
        modes.addView(Ui.button(this, "📻  Радио и спектр: FM с RDS, NFM, AM, SSB, CW, DMR, водопад") { openRadio() }, Ui.lp(top = Ui.dp(this, 6)))
        for ((name, plan) in listOf(
            "📺  Телевидение (измеритель DVB-T2, аналог)" to "в планах",
            "🖼  SSTV — картинки с МКС и любителей" to "в планах",
            "🛰  Спутники: пролёты, «Метеор» LRPT" to "в планах",
            "✈  Самолёты ADS-B на карте" to "в планах",
            "📍  APRS — метки любителей на карте" to "в планах",
        )) {
            val b = Ui.button(this, "$name — $plan") {}
            b.isEnabled = false
            modes.addView(b)
        }
        modes.addView(Ui.text(this, "Остальные режимы есть в версии для Windows и переносятся на Android по очереди.",
            12f, Ui.DIM), Ui.lp(top = Ui.dp(this, 4)))
        root.addView(modes, Ui.lp(top = Ui.dp(this, 12)))

        val more = Ui.hbox(this)
        more.addView(Ui.button(this, "Справка для начинающих") { startActivity(Intent(this, GuideActivity::class.java)) },
            Ui.lp(0, weight = 1f))
        more.addView(Ui.button(this, "Об Android-версии") {
            startActivity(Intent(this, GuideActivity::class.java).putExtra(GuideActivity.EXTRA_PAGE, "android/index.html"))
        }, Ui.lp(0, weight = 1f))
        root.addView(more, Ui.lp(top = Ui.dp(this, 12)))

        val scroll = ScrollView(this)
        scroll.setBackgroundColor(Ui.BG)
        scroll.addView(root)
        setContentView(scroll)
    }

    private fun refreshDevices() {
        devicesBox.removeAllViews()
        val list = UsbReceivers.attached(this)
        if (list.isEmpty()) {
            devicesBox.addView(Ui.text(this, "USB-приёмников не найдено. Подключите приёмник через OTG-переходник " +
                "(на некоторых телефонах OTG нужно включить в настройках).", 13f, Ui.DIM))
        }
        for ((d, m) in list) {
            val status = if (m.kind.builtIn) "встроенный драйвер" else m.kind.title.substringAfter(" — ")
            val b = Ui.button(this, "🔌  ${m.name} · $status") { chooseUsb(d) }
            b.gravity = Gravity.START or Gravity.CENTER_VERTICAL
            devicesBox.addView(b)
        }
        // выбранный USB-приёмник отключили — показываем, но открыть не получится
        selectedText.text = "Выбран: " + Receivers.describe(this, selected)
    }

    private fun chooseUsb(d: UsbDevice) {
        val m = UsbReceivers.model(d.vendorId, d.productId)
        if (m != null && !m.kind.builtIn) {
            toastDialog("${m.name}: ${m.kind.title}.\n\nВ Android-версии пока работают приёмники на RTL2832U, HackRF, " +
                "Airspy R2/Mini и сетевой rtl_tcp. Остальные USB радиоприёмники (SDR) поддерживаются в версии для Windows.")
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
            .setTitle("Сетевой rtl_tcp")
            .setMessage("Адрес сервера rtl_tcp (хост:порт). Приёмник подключён к другому компьютеру, " +
                "на котором запущен rtl_tcp -a 0.0.0.0.")
            .setView(edit)
            .setPositiveButton("Подключить") { _, _ ->
                val t = edit.text.toString()
                try {
                    RtlTcpSource.parse(t)
                    prefs.edit().putString("tcp", t).apply()
                    select(Receivers.tcp(t)); openRadio()
                } catch (e: SdrException) { toastDialog(e.message ?: "") }
            }
            .setNegativeButton("Отмена", null)
            .show()
    }

    private fun select(id: String) {
        selected = id
        prefs.edit().putString("source", id).apply()
        if (::selectedText.isInitialized) selectedText.text = "Выбран: " + Receivers.describe(this, id)
    }

    private fun openRadio() {
        startActivity(Intent(this, RadioActivity::class.java).putExtra(RadioActivity.EXTRA_SOURCE, selected))
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
        AlertDialog.Builder(this).setMessage(msg).setPositiveButton("Понятно", null).show()
    }

    private fun showUpdate(info: UpdateCheck.Info) {
        updateBox.removeAllViews()
        val c = Ui.card(this)
        c.addView(Ui.text(this, "Вышла версия ${info.version}", 15f, Ui.ACCENT, bold = true))
        c.addView(Ui.button(this, "Скачать APK (${info.sizeMb} МБ)") {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(info.apkUrl)))
        })
        updateBox.addView(c)
    }
}
