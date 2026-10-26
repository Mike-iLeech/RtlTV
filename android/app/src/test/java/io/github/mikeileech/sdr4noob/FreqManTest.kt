package io.github.mikeileech.sdr4noob

import io.github.mikeileech.sdr4noob.radio.FreqEntry
import io.github.mikeileech.sdr4noob.radio.FreqMan
import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.File

/** Перенос test_freqman_csv_xml_roundtrip (tests/test_radio.py) + совместимость с файлами настольной версии. */
class FreqManTest {
    private val ent = listOf(
        FreqMan.normalize("Радио Москвы", 105.3e6, "WFM", 200000.0, "FM"),
        FreqMan.normalize("PMR 1", 446.00625e6, "NFM", null, "Рации"),
        FreqMan.normalize("CW", 7.03e6, "CW", 500.0, null),
    )

    // эталон: rtltv.freqman.export_csv / export_sdrsharp_xml для тех же записей
    private val pyCsv = "Название;Частота, Гц;Режим;Полоса, Гц;Группа\r\nРадио Москвы;105300000;WFM;200000;FM\r\n" +
        "PMR 1;446006250;NFM;12500;Рации\r\nCW;7030000;CW;500;Мои\r\n"
    private val pyXml = "<?xml version='1.0' encoding='utf-8'?>\n<ArrayOfMemoryEntry xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\" " +
        "xmlns:xsd=\"http://www.w3.org/2001/XMLSchema\"><MemoryEntry><IsFavourite>true</IsFavourite><Name>Радио Москвы</Name>" +
        "<GroupName>FM</GroupName><Frequency>105300000</Frequency><DetectorType>WFM</DetectorType><Shift>0</Shift>" +
        "<FilterBandwidth>200000</FilterBandwidth></MemoryEntry><MemoryEntry><IsFavourite>true</IsFavourite><Name>PMR 1</Name>" +
        "<GroupName>Рации</GroupName><Frequency>446006250</Frequency><DetectorType>NFM</DetectorType><Shift>0</Shift>" +
        "<FilterBandwidth>12500</FilterBandwidth></MemoryEntry><MemoryEntry><IsFavourite>true</IsFavourite><Name>CW</Name>" +
        "<GroupName>Мои</GroupName><Frequency>7030000</Frequency><DetectorType>CW</DetectorType><Shift>0</Shift>" +
        "<FilterBandwidth>500</FilterBandwidth></MemoryEntry></ArrayOfMemoryEntry>"

    @Test fun normalizeLikeDesktop() {
        assertEquals(FreqEntry("PMR 1", 446006250.0, "NFM", 12500.0, "Рации"), ent[1])
        assertEquals("Мои", ent[2].group)
        assertEquals(4000.0, FreqMan.normalize("x", 1.0, "usb", 9000.0, "").bw, 0.0)
        assertEquals("AM", FreqMan.normalize("x", 1.0, "DSB", null, "").mode)
    }

    @Test fun csvSameBytesAsDesktop() {
        assertEquals(pyCsv, FreqMan.exportCsv(ent))
        assertEquals(ent, FreqMan.importCsv("﻿" + pyCsv))
    }

    @Test fun xmlSameAsDesktop() {
        assertEquals(pyXml, FreqMan.exportSdrSharpXml(ent))
        assertEquals(ent, FreqMan.importSdrSharpXml(pyXml.toByteArray()))
        assertEquals(ent, FreqMan.importAny(pyXml.toByteArray()))
    }

    @Test fun englishCsvAndMerge() {
        val got = FreqMan.importAny("name,frequency,mode,bandwidth,group\nAir,125.5 MHz,AM,8330,Avia\nATIS,128000000,am,,Avia\n".toByteArray())
        assertEquals(listOf(Triple(125.5e6, "AM", 8330.0), Triple(128e6, "AM", 10000.0)), got.map { Triple(it.freq, it.mode, it.bw) })
        val list = ent.toMutableList()
        assertEquals(2, FreqMan.merge(list, got))
        assertEquals(0, FreqMan.merge(list, got))
    }

    @Test fun quotedFieldsAndFreqUnits() {
        val e = listOf(FreqMan.normalize("Станция; \"ночная\"", 101.2e6, "WFM", null, "FM"))
        assertEquals(e, FreqMan.importCsv(FreqMan.exportCsv(e)))
        assertEquals(101.2e6, FreqMan.parseFreq("101,2"), 1e-3)
        assertEquals(7.03e6, FreqMan.parseFreq("7,03 МГц"), 1e-3)
        assertEquals(8330.0, FreqMan.parseFreq("8330", guessMhz = false), 0.0)
    }

    @Test fun storeAndLoad() {
        val f = File.createTempFile("freqman", ".json")
        FreqMan.save(f, ent)
        FreqMan.save(f, ent)   // повторная запись поверх
        assertEquals(ent, FreqMan.load(f))
        f.delete()
    }
}
