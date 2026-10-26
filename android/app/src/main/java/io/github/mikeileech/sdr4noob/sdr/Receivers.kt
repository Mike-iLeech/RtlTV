package io.github.mikeileech.sdr4noob.sdr

import android.content.Context
import android.hardware.usb.UsbManager
import io.github.mikeileech.sdr4noob.i18n.tr

/**
 * Выбор приёмника строкой (как source_id в rtltv/devices.py):
 *   "usb:/dev/bus/usb/001/002" — USB-приёмник по имени устройства Android;
 *   "tcp:192.168.1.10:1234"    — сетевой rtl_tcp;
 *   "file:content://…"         — воспроизведение записи IQ;
 *   "demo"                     — тестовый сигнал.
 */
object Receivers {
    const val DEMO = "demo"

    fun usb(deviceName: String) = "usb:$deviceName"
    fun tcp(hostPort: String) = "tcp:$hostPort"
    fun file(uri: String) = "file:$uri"

    fun describe(context: Context, id: String): String = when {
        id == DEMO -> tr("Тестовый сигнал (без приёмника)")
        id.startsWith("tcp:") -> tr("Сетевой rtl_tcp {addr}", "addr" to id.removePrefix("tcp:"))
        id.startsWith("file:") -> tr("Файл IQ: {name}", "name" to (android.net.Uri.parse(id.removePrefix("file:")).lastPathSegment?.substringAfterLast('/') ?: tr("запись")))
        id.startsWith("usb:") -> {
            val d = usbDevice(context, id)
            d?.let { UsbReceivers.model(it.vendorId, it.productId)?.name } ?: tr("USB-приёмник (не подключён)")
        }
        else -> id
    }

    private fun usbDevice(context: Context, id: String) =
        (context.getSystemService(Context.USB_SERVICE) as UsbManager).deviceList[id.removePrefix("usb:")]

    /** Создать источник. Бросает [SdrException], если приёмник не подключён или для него нет драйвера. */
    fun sourceFor(context: Context, id: String): SdrSource = when {
        id == DEMO -> DemoSource()
        id.startsWith("tcp:") -> RtlTcpSource.parse(id.removePrefix("tcp:")).let { (h, p) -> RtlTcpSource(h, p) }
        id.startsWith("file:") -> IqFileSource.forUri(context, android.net.Uri.parse(id.removePrefix("file:")))
        id.startsWith("usb:") -> {
            val d = usbDevice(context, id) ?: throw SdrException(tr("Приёмник отключён от USB"))
            val m = UsbReceivers.model(d.vendorId, d.productId)
            when (m?.kind) {
                DriverKind.RTL -> RtlUsbSource(context.applicationContext, d)
                DriverKind.HACKRF -> HackRfUsbSource(context.applicationContext, d)
                DriverKind.AIRSPY -> AirspyUsbSource(context.applicationContext, d)
                null -> throw SdrException(tr("Неизвестное USB-устройство {id}", "id" to "%04x:%04x".format(d.vendorId, d.productId)))
                else -> throw SdrException("${m.name}: ${m.kind.title}. " +
                    tr("В Android-версии этот приёмник пока не работает — он поддерживается в версии для Windows."))
            }
        }
        else -> throw SdrException(tr("Неизвестный приёмник «{id}»", "id" to id))
    }
}
