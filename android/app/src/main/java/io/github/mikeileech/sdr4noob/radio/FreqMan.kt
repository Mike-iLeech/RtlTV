package io.github.mikeileech.sdr4noob.radio

import io.github.mikeileech.sdr4noob.dsp.Modulation
import org.json.JSONArray
import org.json.JSONObject
import org.w3c.dom.Element
import java.io.ByteArrayInputStream
import java.io.File
import java.util.Locale
import javax.xml.parsers.DocumentBuilderFactory
import kotlin.math.roundToLong

/**
 * Менеджер частот — перенос rtltv/freqman.py: записи {название, частота, режим, полоса, группа}, импорт и
 * экспорт в тех же форматах, что на ПК: CSV (разделитель «;», UTF-8 с BOM — открывается в Excel; при импорте
 * «,», «;» и табуляция определяются сами, столбцы — по заголовку по-русски или по-английски) и XML SDR#.
 */
data class FreqEntry(val name: String, val freq: Double, val mode: String, val bw: Double, val group: String)

object FreqMan {
    val HEADER = listOf("Название", "Частота, Гц", "Режим", "Полоса, Гц", "Группа")
    const val DEFAULT_GROUP = "Мои"
    private val COLS = mapOf(
        "name" to listOf("название", "name", "имя", "станция"),
        "freq" to listOf("частота, гц", "частота", "frequency", "freq", "freq_hz", "frequency_hz", "hz"),
        "mode" to listOf("режим", "mode", "modulation", "detectortype", "демодулятор"),
        "bw" to listOf("полоса, гц", "полоса", "bandwidth", "bw", "filterbandwidth", "bw_hz"),
        "group" to listOf("группа", "group", "groupname", "категория", "category"),
    )
    private val SDRSHARP_MODES = mapOf("WFM" to "WFM", "NFM" to "NFM", "AM" to "AM", "DSB" to "AM", "USB" to "USB",
        "LSB" to "LSB", "CW" to "CW", "RAW" to "NFM")
    /** Режимы настольной версии, которых на Android пока нет, — ближайший доступный. */
    private val ANDROID_MODE = mapOf("DMR" to "NFM")

    fun modulation(e: FreqEntry): Modulation =
        runCatching { Modulation.valueOf(ANDROID_MODE[e.mode] ?: e.mode) }.getOrDefault(Modulation.NFM)

    private fun modeRange(mode: String): Triple<Int, Int, Int> = when (mode) {
        "DMR" -> Triple(12_500, 12_500, 12_500)
        else -> Modulation.valueOf(mode).let { Triple(it.defaultBw, it.minBw, it.maxBw) }
    }

    private val ALL_MODES = Modulation.values().map { it.name } + "DMR"

    /** Запись с проверенными полями (частота — Гц, режим из списка, полоса в пределах режима). */
    fun normalize(name: String?, freq: Double?, mode: String?, bw: Double?, group: String?): FreqEntry {
        var m = (mode ?: "NFM").uppercase(Locale.ROOT).ifEmpty { "NFM" }
        if (m !in ALL_MODES) m = SDRSHARP_MODES[m] ?: "NFM"
        val f = freq ?: 0.0
        val (def, lo, hi) = modeRange(m)
        val b = if (bw != null && bw > 0) bw.coerceIn(lo.toDouble(), hi.toDouble()) else def.toDouble()
        val nm = name?.trim().orEmpty().ifEmpty { String.format(Locale.ROOT, "%.4f МГц", f / 1e6) }
        val g = group?.trim().orEmpty().ifEmpty { DEFAULT_GROUP }
        return FreqEntry(nm, f, m, b, g)
    }

    /** «101.2» → 101,2 МГц (похоже на МГц без единиц), «125.5 MHz», «8330», «7,03 МГц». */
    fun parseFreq(text: String, guessMhz: Boolean = true): Double {
        var low = text.trim().replace(" ", "").replace(" ", "").lowercase(Locale.ROOT)
        var mul = 1.0
        for ((suf, m) in listOf("ghz" to 1e9, "mhz" to 1e6, "мгц" to 1e6, "khz" to 1e3, "кгц" to 1e3, "hz" to 1.0, "гц" to 1.0)) {
            if (low.endsWith(suf)) { low = low.dropLast(suf.length); mul = m; break }
        }
        val v = low.replace(',', '.').toDoubleOrNull() ?: throw NumberFormatException(text)
        if (guessMhz && mul == 1.0 && v < 10000) mul = 1e6
        return v * mul
    }

    // ---------------- CSV ----------------
    private fun csvField(s: String, d: Char) =
        if (s.any { it == d || it == '"' || it == '\n' || it == '\r' }) "\"" + s.replace("\"", "\"\"") + "\"" else s

    /** CSV как export_csv на ПК (строки с \r\n; BOM добавляет вызывающий при записи в файл). */
    fun exportCsv(entries: List<FreqEntry>): String {
        val sb = StringBuilder()
        sb.append(HEADER.joinToString(";")).append("\r\n")
        for (e0 in entries) {
            val e = normalize(e0.name, e0.freq, e0.mode, e0.bw, e0.group)
            sb.append(listOf(e.name, e.freq.roundToLong().toString(), e.mode, e.bw.roundToLong().toString(), e.group)
                .joinToString(";") { csvField(it, ';') }).append("\r\n")
        }
        return sb.toString()
    }

    private fun parseCsv(text: String, d: Char): List<List<String>> {
        val rows = ArrayList<List<String>>()
        var row = ArrayList<String>()
        val f = StringBuilder()
        var q = false
        var i = 0
        while (i < text.length) {
            val c = text[i]
            if (q) {
                if (c == '"') { if (i + 1 < text.length && text[i + 1] == '"') { f.append('"'); i++ } else q = false }
                else f.append(c)
            } else when (c) {
                '"' -> if (f.isEmpty()) q = true else f.append(c)   // как csv в Python: кавычки — только в начале поля
                d -> { row.add(f.toString()); f.setLength(0) }
                '\r' -> {}
                '\n' -> { row.add(f.toString()); f.setLength(0); rows.add(row); row = ArrayList() }
                else -> f.append(c)
            }
            i++
        }
        if (f.isNotEmpty() || row.isNotEmpty()) { row.add(f.toString()); rows.add(row) }
        return rows.filter { r -> r.any { it.isNotBlank() } }
    }

    fun importCsv(text0: String): List<FreqEntry> {
        val text = text0.removePrefix("﻿")
        val head0 = text.lineSequence().firstOrNull().orEmpty()
        val d = listOf(';', ',', '\t').maxByOrNull { ch -> head0.count { it == ch } } ?: ';'
        val rows = parseCsv(text, d)
        if (rows.isEmpty()) return emptyList()
        val head = rows[0].map { it.trim().lowercase(Locale.ROOT) }
        var idx = HashMap<String, Int>()
        for ((key, names) in COLS) head.indexOfFirst { it in names }.takeIf { it >= 0 }?.let { idx[key] = it }
        val body = if ("freq" in idx) rows.drop(1) else {
            idx = hashMapOf("name" to 0, "freq" to 1, "mode" to 2, "bw" to 3, "group" to 4)
            rows
        }
        val out = ArrayList<FreqEntry>()
        for (r in body) {
            fun col(k: String) = idx[k]?.let { r.getOrNull(it)?.trim() }.orEmpty()
            val freq = try { parseFreq(col("freq")) } catch (_: NumberFormatException) { continue }
            if (!usable(freq)) continue
            val bw = if (col("bw").isNotEmpty()) runCatching { parseFreq(col("bw"), guessMhz = false) }.getOrDefault(0.0) else 0.0
            out.add(normalize(col("name"), freq, col("mode"), bw, col("group")))
        }
        return out
    }

    // ---------------- XML SDR# ----------------
    fun importSdrSharpXml(bytes: ByteArray): List<FreqEntry> {
        // без DTD и внешних сущностей: чужой файл не раздует память и не прочитает лишнего
        val f = DocumentBuilderFactory.newInstance()
        runCatching { f.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true) }
        f.isExpandEntityReferences = false
        val doc = f.newDocumentBuilder().parse(ByteArrayInputStream(bytes))
        val list = doc.getElementsByTagName("MemoryEntry")
        val out = ArrayList<FreqEntry>()
        for (i in 0 until list.length) {
            val m = list.item(i) as Element
            fun tx(tag: String) = m.getElementsByTagName(tag).item(0)?.textContent.orEmpty()
            val freq = tx("Frequency").toDoubleOrNull()?.takeIf(::usable) ?: continue
            out.add(normalize(tx("Name"), freq, tx("DetectorType"), tx("FilterBandwidth").toDoubleOrNull() ?: 0.0, tx("GroupName")))
        }
        return out
    }

    private fun xmlEsc(s: String) = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")   // как ElementTree

    fun exportSdrSharpXml(entries: List<FreqEntry>): String {
        val sb = StringBuilder("<?xml version='1.0' encoding='utf-8'?>\n<ArrayOfMemoryEntry " +
            "xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\" xmlns:xsd=\"http://www.w3.org/2001/XMLSchema\">")
        for (e0 in entries) {
            val e = normalize(e0.name, e0.freq, e0.mode, e0.bw, e0.group)
            sb.append("<MemoryEntry>")
            for ((tag, v) in listOf("IsFavourite" to "true", "Name" to e.name, "GroupName" to e.group,
                    "Frequency" to e.freq.roundToLong().toString(), "DetectorType" to e.mode, "Shift" to "0",
                    "FilterBandwidth" to e.bw.roundToLong().toString()))
                sb.append('<').append(tag).append('>').append(xmlEsc(v)).append("</").append(tag).append('>')
            sb.append("</MemoryEntry>")
        }
        return sb.append("</ArrayOfMemoryEntry>").toString()
    }

    /** Импорт по содержимому: XML SDR# или CSV. */
    fun importAny(bytes: ByteArray): List<FreqEntry> {
        val head = String(bytes, 0, minOf(bytes.size, 256), Charsets.UTF_8).trimStart('﻿', ' ', '\r', '\n', '\t')
        return if (head.startsWith("<")) importSdrSharpXml(bytes) else importCsv(String(bytes, Charsets.UTF_8))
    }

    /** Частота, которую можно сохранить: конечная, положительная, не больше 10 ГГц. */
    fun usable(f: Double) = f.isFinite() && f > 0 && f <= 1e10

    /** Добавить записи без повторов (та же частота с точностью 1 Гц, тот же режим и группа). */
    fun merge(existing: MutableList<FreqEntry>, new: List<FreqEntry>): Int {
        val seen = existing.map { Triple(it.freq.roundToLong(), it.mode, it.group) }.toMutableSet()
        var added = 0
        for (e in new) {
            val k = Triple(e.freq.roundToLong(), e.mode, e.group)
            if (seen.add(k)) { existing.add(e); added++ }
        }
        return added
    }

    // ---------------- хранение ----------------
    fun load(file: File): MutableList<FreqEntry> {
        if (!file.isFile) return mutableListOf()
        return runCatching {
            val a = JSONArray(file.readText())
            (0 until a.length()).map { a.getJSONObject(it) }.filter { usable(it.optDouble("freq")) }.map { o ->
                normalize(o.optString("name"), o.optDouble("freq"), o.optString("mode"),
                    o.optDouble("bw").takeIf { it.isFinite() }, o.optString("group"))
            }.toMutableList()
        }.getOrDefault(mutableListOf())
    }

    fun save(file: File, entries: List<FreqEntry>) {
        val a = JSONArray()
        for (e in entries.filter { usable(it.freq) }) a.put(JSONObject().put("name", e.name).put("freq", e.freq).put("mode", e.mode).put("bw", e.bw).put("group", e.group))
        val tmp = File(file.path + ".tmp")
        tmp.writeText(a.toString(1))
        if (!tmp.renameTo(file)) { file.delete(); tmp.renameTo(file) }
    }
}
