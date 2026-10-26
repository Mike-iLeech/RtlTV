package io.github.mikeileech.sdr4noob

import io.github.mikeileech.sdr4noob.sdr.DriverKind
import io.github.mikeileech.sdr4noob.sdr.RtlTcpSource
import io.github.mikeileech.sdr4noob.sdr.SdrException
import io.github.mikeileech.sdr4noob.sdr.U8
import io.github.mikeileech.sdr4noob.sdr.UsbReceivers
import io.github.mikeileech.sdr4noob.update.UpdateCheck
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class MiscTest {
    @Test fun parseHostPort() {
        assertEquals("192.168.1.10" to 1234, RtlTcpSource.parse("192.168.1.10"))
        assertEquals("host" to 7373, RtlTcpSource.parse(" host:7373 "))
        assertEquals("::1" to 1234, RtlTcpSource.parse("[::1]:1234"))
        for (bad in listOf("", "h:0", "h:99999", "h:x", ":1234"))
            try { RtlTcpSource.parse(bad); throw AssertionError("принят «$bad»") } catch (_: SdrException) {}
    }

    @Test fun u8Conversion() {
        val re = FloatArray(2); val im = FloatArray(2)
        assertEquals(2, U8.convert(byteArrayOf(0, 255.toByte(), 127, 128.toByte(), 9), 5, re, im))
        assertEquals(-127.4f / 128, re[0], 1e-6f)
        assertEquals(127.6f / 128, im[0], 1e-6f)
    }

    @Test fun knownReceivers() {
        assertEquals(DriverKind.RTL, UsbReceivers.model(0x0bda, 0x2838)?.kind)
        assertEquals(DriverKind.HACKRF, UsbReceivers.model(0x1d50, 0x6089)?.kind)
        assertEquals(DriverKind.SDRPLAY, UsbReceivers.model(0x1df7, 0x3000)?.kind)
        assertNull(UsbReceivers.model(0x1234, 0x5678))
    }

    /** Фильтр USB_DEVICE_ATTACHED перечисляет ровно приёмники со встроенным драйвером. */
    @Test fun attachFilterMatchesBuiltInDrivers() {
        val xml = File("src/main/res/xml/usb_receivers.xml").readText()
        val ids = Regex("""vendor-id="(\d+)" product-id="(\d+)"""").findAll(xml)
            .map { it.groupValues[1].toInt() to it.groupValues[2].toInt() }.toSet()
        val builtIn = UsbReceivers.models.filter { it.kind.builtIn }.map { it.vid to it.pid!! }.toSet()
        assertEquals(builtIn, ids)
    }

    @Test fun versionCompare() {
        assertTrue(UpdateCheck.compare("3.10.0", "3.9.9") > 0)
        assertTrue(UpdateCheck.compare("v3.3.0", "3.3.0") == 0)
        assertTrue(UpdateCheck.compare("3.3", "3.3.1") < 0)
    }

    @Test fun updateParseFindsApk() {
        val json = """{"tag_name":"v9.0.0","assets":[
            {"name":"SDR4NOOB-9.0.0-win64.zip","size":1,"browser_download_url":"https://x/zip"},
            {"name":"SDR4NOOB-9.0.0-android.apk","size":5242880,"browser_download_url":"https://x/apk"}]}"""
        val info = UpdateCheck.parse(json, "3.3.0")
        assertNotNull(info)
        assertEquals("9.0.0", info!!.version)
        assertEquals("https://x/apk", info.apkUrl)
        assertNull(UpdateCheck.parse(json, "9.0.0"))
        assertNull(UpdateCheck.parse("""{"tag_name":"v9.1.0","assets":[{"name":"a-win64.zip"}]}""", "3.3.0"))
    }
}
