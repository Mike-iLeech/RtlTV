package io.github.mikeileech.sdr4noob.sdr

import android.content.Context
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager

/** Какой драйвер нужен приёмнику. */
enum class DriverKind(val title: String, val builtIn: Boolean) {
    RTL("RTL2832U — встроенный драйвер (librtlsdr)", true),
    HACKRF("HackRF — драйвер в планах", false),
    AIRSPY("Airspy — драйвер в планах", false),
    AIRSPY_HF("Airspy HF+ — драйвер в планах", false),
    SDRPLAY("SDRplay — нужен закрытый драйвер производителя", false),
    LIME("LimeSDR — драйвер в планах", false),
    PLUTO("ADALM-Pluto — драйвер в планах", false),
    FUNCUBE("FUNcube — драйвер в планах", false),
}

data class UsbModel(val vid: Int, val pid: Int?, val name: String, val kind: DriverKind)

/**
 * Известные USB радиоприёмники (SDR). Тот же список, что KNOWN_USB в rtltv/devices.py, плюс все
 * DVB-T донглы на RTL2832U из таблицы librtlsdr. VID/PID приёмников со встроенным драйвером — также
 * в res/xml/usb_receivers.xml (Android сам предлагает открыть SDR4NOOB при подключении).
 *
 * Как добавить драйвер другого приёмника: библиотеку на libusb собрать в CMakeLists.txt с
 * -include usb_fd_shim.h (она откроется по дескриптору без правок) или взять её функцию открытия
 * по fd (airspy_open_fd), сделать класс SdrSource и вернуть его в [Receivers.sourceFor].
 */
object UsbReceivers {
    private val rtlIds = listOf(
        0x0bda to 0x2832, 0x0bda to 0x2838, 0x0413 to 0x6680, 0x0413 to 0x6f0f, 0x0458 to 0x707f,
        0x0ccd to 0x00a9, 0x0ccd to 0x00b3, 0x0ccd to 0x00b4, 0x0ccd to 0x00b5, 0x0ccd to 0x00b7,
        0x0ccd to 0x00b8, 0x0ccd to 0x00b9, 0x0ccd to 0x00c0, 0x0ccd to 0x00c6, 0x0ccd to 0x00d3,
        0x0ccd to 0x00d7, 0x0ccd to 0x00e0, 0x1554 to 0x5020, 0x15f4 to 0x0131, 0x15f4 to 0x0133,
        0x185b to 0x0620, 0x185b to 0x0650, 0x185b to 0x0680, 0x1b80 to 0xd393, 0x1b80 to 0xd394,
        0x1b80 to 0xd395, 0x1b80 to 0xd397, 0x1b80 to 0xd398, 0x1b80 to 0xd39d, 0x1b80 to 0xd3a4,
        0x1b80 to 0xd3a8, 0x1b80 to 0xd3af, 0x1b80 to 0xd3b0, 0x1d19 to 0x1101, 0x1d19 to 0x1102,
        0x1d19 to 0x1103, 0x1d19 to 0x1104, 0x1f4d to 0xa803, 0x1f4d to 0xb803, 0x1f4d to 0xc803,
        0x1f4d to 0xd286, 0x1f4d to 0xd803,
    )

    val models: List<UsbModel> = rtlIds.map { (v, p) ->
        UsbModel(v, p, if (v == 0x0bda) "RTL-SDR (RTL2832U)" else "DVB-T донгл на RTL2832U", DriverKind.RTL)
    } + listOf(
        UsbModel(0x1D50, 0x6089, "HackRF One", DriverKind.HACKRF),
        UsbModel(0x1D50, 0x604B, "HackRF Jawbreaker", DriverKind.HACKRF),
        UsbModel(0x1D50, 0xCC15, "rad1o (HackRF)", DriverKind.HACKRF),
        UsbModel(0x1D50, 0x60A1, "Airspy R2 / Mini", DriverKind.AIRSPY),
        UsbModel(0x03EB, 0x800C, "Airspy HF+", DriverKind.AIRSPY_HF),
        UsbModel(0x1DF7, null, "SDRplay RSP", DriverKind.SDRPLAY),
        UsbModel(0x0403, 0x601F, "LimeSDR Mini", DriverKind.LIME),
        UsbModel(0x1D50, 0x6108, "LimeSDR-USB", DriverKind.LIME),
        UsbModel(0x0456, 0xB673, "ADALM-Pluto (PlutoSDR)", DriverKind.PLUTO),
        UsbModel(0x04D8, 0xFB56, "FUNcube Dongle Pro", DriverKind.FUNCUBE),
        UsbModel(0x04D8, 0xFB31, "FUNcube Dongle Pro+", DriverKind.FUNCUBE),
    )

    fun model(vid: Int, pid: Int): UsbModel? =
        models.firstOrNull { it.vid == vid && it.pid == pid } ?: models.firstOrNull { it.vid == vid && it.pid == null }

    /** Подключённые USB-приёмники, которые мы узнаём. */
    fun attached(context: Context): List<Pair<UsbDevice, UsbModel>> {
        val um = context.getSystemService(Context.USB_SERVICE) as UsbManager
        return um.deviceList.values.mapNotNull { d -> model(d.vendorId, d.productId)?.let { d to it } }
            .sortedBy { it.first.deviceName }
    }
}
