package io.github.mikeileech.sdr4noob.dsp

import io.github.mikeileech.sdr4noob.i18n.N_
import io.github.mikeileech.sdr4noob.i18n.num
import io.github.mikeileech.sdr4noob.i18n.tr
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToLong
import kotlin.math.sqrt

/**
 * Поиск DMR — перенос rtltv/dmrscan.py: обход диапазона окнами (~80 % полосы обзора), поиск активных каналов
 * по сетке 6,25 кГц и проверка DMR-синхрослов; повторные проходы копят статистику (CC, слоты, тип, ID).
 *
 * Отличие от ПК: там канал вырезается из одного большого БПФ произвольной длины; здесь мощность каналов —
 * по БПФ степени двойки, а канал 48 кГц выделяется переносом частоты и прореживанием (частоты обзора кратны
 * 48 кГц). Классификация та же: DMR, аналоговая ЧМ, 4FSK 4800 Бод (P25/NXDN), 2400 Бод (dPMR/NXDN), широкая, несущая.
 */
class DmrScanner(val fs: Int, lo0: Double, hi0: Double, dwellSec: Double = 0.35, private val maxCheck: Int = 12) {
    companion object {
        const val GRID = 6250.0
        const val CH_FS = 48_000
        const val DC_SHIFT = 3125.0
        val BANDS = listOf(
            Triple(N_("70 см любительский 430–440 МГц"), 430.0e6, 440.0e6),
            Triple(N_("PMR446 446,0–446,2 МГц"), 446.0e6, 446.2e6),
            Triple(N_("Служебные 400–470 МГц"), 400.0e6, 470.0e6),
            Triple(N_("2 м любительский 144–146 МГц"), 144.0e6, 146.0e6),
            Triple(N_("Служебные 136–174 МГц"), 136.0e6, 174.0e6),
        )
        val KIND_NAMES = mapOf("dmr" to "DMR", "analog" to N_("аналог ЧМ"), "digital" to N_("другой цифровой"),
            "narrow" to N_("узкий цифровой"), "wide" to N_("широкополосный"), "carrier" to N_("несущая"))

        /** Центры окон перестройки, чтобы полезные ±40 % полосы покрыли [lo, hi] (как plan_windows). */
        fun planWindows(lo0: Double, hi0: Double, fs: Int): List<Long> {
            val span = 0.8 * fs
            val lo = min(lo0, hi0); val hi = max(lo0, hi0)
            if (hi - lo <= span) return listOf((Math.round((lo + hi) / 2 / GRID) * GRID + DC_SHIFT).roundToLong())
            val n = ceil((hi - lo) / span).toInt()
            val step = (hi - lo) / n
            return (0 until n).map { k -> (Math.round((lo + step * (k + 0.5)) / GRID) * GRID + DC_SHIFT).roundToLong() }
        }

        private object Silent : AmbeSynth {
            override fun reset() {}
            override fun decode(fr: ByteArray, out: FloatArray): Int { out.fill(0f); return 0 }
            override val ok = false
        }
    }

    class Entry(val freq: Double, val inBand: Boolean) {
        var kind = ""; var label = ""
        val cc = ArrayList<Int>(); val slots = ArrayList<Int>(); val types = ArrayList<String>(); val ids = ArrayList<String>()
        var mode = ""; var hits = 0; var dmrHits = 0; var levelDb = -200.0; var encrypted = false
        var first = 0L; var last = 0L

        /** Короткое описание (как describe в dmrscan.py). */
        fun describe(): String = when (kind) {
            "dmr" -> {
                var head = "DMR"
                if (cc.isNotEmpty()) head += " CC" + cc.joinToString("/")
                if (mode.isNotEmpty()) head += " ($mode)"
                "$head · $label" + if (encrypted) tr(" · шифр") else ""
            }
            else -> label.ifEmpty { KIND_NAMES[kind]?.let { tr(it) } ?: kind }
        }
    }

    val lo = min(lo0, hi0)
    val hi = max(lo0, hi0)
    val centers = planWindows(this.lo, this.hi, fs)
    /** Длина окна: степень двойки около dwellSec (для БПФ мощности). */
    val nsamp: Int = Integer.highestOneBit((dwellSec * fs).toInt().coerceAtLeast(8192))
    private val entries = LinkedHashMap<Long, Entry>()
    @Volatile var passes = 0; private set
    var windowsDone = 0; private set

    private fun entry(f: Double): Entry {
        val key = (Math.round(f / GRID) * GRID).roundToLong()
        return entries.getOrPut(key) { Entry(key.toDouble(), key >= lo - 1 && key <= hi + 1) }
    }

    /** Одно окно плана (центр [freq], [n] отсчётов IQ). Возвращает описания найденного в этом окне. */
    fun process(freq: Long, re: FloatArray, im: FloatArray, n: Int, index: Int): List<String> =
        processWindow(freq, re, im, n, index).also { publish() }

    /**
     * Записи меняет только поток приёма; интерфейсу после каждого окна публикуется готовая копия — без
     * блокировок (обработка окна на телефоне длится сотни миллисекунд, интерфейс ждать не должен).
     */
    @Volatile private var published: List<Found> = emptyList()
    @Volatile private var dmrCount = 0

    private fun publish() {
        published = entries.values.filter { it.hits > 0 }.sortedBy { it.freq }
            .map { Found(it.freq, it.kind, it.describe(), it.ids.toList(), it.cc.toList(), it.dmrHits) }
        dmrCount = entries.values.count { it.kind == "dmr" && it.dmrHits > 0 }
    }

    private fun processWindow(freq: Long, re: FloatArray, im: FloatArray, n: Int, index: Int): List<String> {
        if (index == 0) passes++
        windowsDone++
        if (n < 4096) return emptyList()
        val m = Integer.highestOneBit(n)
        // мощность по БПФ (без окна, как на ПК), ноль частоты посередине
        val fr = FloatArray(m); val fi = FloatArray(m)
        var mr = 0.0; var mi = 0.0
        for (i in 0 until m) { mr += re[i]; mi += im[i] }
        mr /= m; mi /= m
        for (i in 0 until m) { fr[i] = (re[i] - mr).toFloat(); fi[i] = (im[i] - mi).toFloat() }
        Fft(m).transform(fr, fi)
        val cs = DoubleArray(m + 1)
        for (i in 0 until m) { val j = (i + m / 2) % m; cs[i + 1] = cs[i] + fr[j] * fr[j] + fi[j] * fi[j] }
        val binHz = fs.toDouble() / m
        val half = fs * 0.4
        val g0 = ceil(max(lo, freq - half) / GRID).toLong()
        val g1 = floor(min(hi, freq + half) / GRID).toLong()
        if (g1 < g0) return emptyList()
        val grid = DoubleArray((g1 - g0 + 1).toInt()) { (g0 + it) * GRID }
        val w = 3000.0 / binHz
        val pw = DoubleArray(grid.size) { i ->
            val k = m / 2 + (grid[i] - freq) / binHz
            val a = Math.round(k - w).toInt().coerceIn(0, m); val b = Math.round(k + w).toInt().coerceIn(0, m)
            (cs[b] - cs[a]) / max(b - a, 1)
        }
        val noise = pw.sorted().let { s -> if (s.size % 2 == 1) s[s.size / 2] else 0.5 * (s[s.size / 2 - 1] + s[s.size / 2]) } + 1e-20
        val rel = DoubleArray(pw.size) { 10 * log10(pw[it] / noise + 1e-20) }
        val act = BooleanArray(pw.size) { rel[it] > 10.0 && abs(grid[it] - freq) > 4000.0 }   // остаток «нуля» приёмника
        // соседние активные ячейки — один сигнал
        val runs = ArrayList<IntArray>()
        var j = 0
        while (j < act.size) {
            if (act[j]) { val s = j; while (j + 1 < act.size && act[j + 1]) j++; runs.add(intArrayOf(s, j)) }
            j++
        }
        runs.sortByDescending { r -> (r[0]..r[1]).maxOf { rel[it] } }
        val out = ArrayList<String>()
        for (r in runs.take(maxCheck)) {
            val (s, e) = r[0] to r[1]
            val width = e - s + 1
            val pk = (s..e).maxBy { rel[it] }
            var fc = grid[pk]
            if (width > 1) {
                var sw = 0.0; var sf = 0.0
                for (i in s..e) { val ww = pw[i] - noise; sw += ww; sf += grid[i] * ww }
                fc = sf / sw
            }
            val level = rel[pk]
            if (width >= 4) { out.add(note(fc, "wide", level, tr("широкополосный (≥25 кГц)"))); continue }
            out.add(classify(fc, channelFm(re, im, n, fc - freq), level))
        }
        return out
    }

    /** Канал на смещении [off] → 48 кГц → мгновенная частота, Гц. */
    private fun channelFm(re: FloatArray, im: FloatArray, n: Int, off: Double): FloatArray {
        val r = re.copyOf(n); val i = im.copyOf(n)
        Mixer(fs.toDouble()).process(r, i, n, -off)
        val dec = ComplexDecimator(fs / CH_FS, 9000.0, fs.toDouble(), 8)
        val outR = FloatArray(n / dec.factor + 2); val outI = FloatArray(n / dec.factor + 2)
        val k = dec.process(r, i, n, outR, outI)
        val fm = FloatArray(max(0, k - 1))
        for (t in 1 until k) {
            val a = outR[t]; val b = outI[t]; val c = outR[t - 1]; val d = outI[t - 1]
            fm[t - 1] = (kotlin.math.atan2(b * c - a * d, a * c + b * d) * (CH_FS / (2 * PI))).toFloat()
        }
        return fm
    }

    /** Сила спектральной линии на частоте символов (признак цифровой ЧМ). */
    private fun baudLine(fm: FloatArray, rate: Double): Double {
        if (fm.size < 4097) return 0.0
        val n = Integer.highestOneBit(fm.size - 1)
        val e = DoubleArray(n) { val d = (fm[it + 1] - fm[it]).toDouble(); d * d }
        val mean = e.average()
        val r = FloatArray(n) { ((e[it] - mean) * (0.5 - 0.5 * cos(2 * PI * it / (n - 1)))).toFloat() }
        val im = FloatArray(n)
        Fft(n).transform(r, im)
        val sp = DoubleArray(n / 2 + 1) { sqrt((r[it] * r[it] + im[it] * im[it]).toDouble()) }
        val hz = CH_FS.toDouble() / n
        val k = Math.round(rate / hz).toInt()
        val lo = max(1, k - (400 / hz).toInt()); val hi = min(sp.size - 1, k + (400 / hz).toInt())
        val peak = (max(1, k - 3) until min(sp.size, k + 4)).maxOf { sp[it] }
        val ref = sp.copyOfRange(lo, hi).sorted().let { it[it.size / 2] } + 1e-12
        return peak / ref
    }

    private fun classify(fc: Double, fm: FloatArray, level: Double): String {
        val dec = DmrDecoder { Silent }
        val (_, events) = dec.process(fm, fm.size)
        val st = dec.status()
        if (st.locked && dec.stats["syncs"]!! >= 1) return noteDmr(fc, dec, st, events, level)
        val mean = fm.average()
        val dev = sqrt(fm.sumOf { (it - mean) * (it - mean) } / max(1, fm.size))
        if (dev < 150.0) return note(fc, "carrier", level, tr("несущая без модуляции"))
        val l48 = baudLine(fm, 4800.0); val l24 = baudLine(fm, 2400.0)
        if (l48 > 8.0 && l48 > l24) return note(fc, "digital", level, tr("цифровой 4FSK 4800 Бод без DMR-синхры (P25/NXDN?)"))
        if (l24 > 8.0) return note(fc, "narrow", level, tr("узкополосный цифровой 2400 Бод (dPMR/NXDN?)"))
        return note(fc, "analog", level, tr("аналоговая ЧМ (или другой цифровой)"))
    }

    private fun base(f: Double, level: Double): Entry {
        val e = entry(f)
        val now = System.currentTimeMillis()
        e.hits++
        if (e.first == 0L) e.first = now
        e.last = now
        e.levelDb = level
        return e
    }

    private fun note(f: Double, kind: String, level: Double, label: String): String {
        val e = base(f, level)
        if (e.kind != "dmr" || e.dmrHits == 0) { e.kind = kind; e.label = label }
        return "${fmtMhz(e.freq)}: $label"
    }

    private fun noteDmr(f: Double, dec: DmrDecoder, st: DmrStatus, events: List<DmrEvent>, level: Double): String {
        val e = base(f + st.offsetHz, level)   // уточнение центра по смещению частоты, найденному декодером
        e.dmrHits++
        e.kind = "dmr"
        e.mode = st.mode
        val voice = dec.stats["voice"]!! > 0
        for ((n, s) in st.slots) {
            val cc = s.cc
            if (cc != null && cc !in e.cc) e.cc.add(cc)
            if ((s.active || s.last.isNotEmpty()) && n !in e.slots && (s.active || s.last !in listOf("", "Idle"))) e.slots.add(n)
            if (s.active && (s.src != 0 || s.dst != 0)) addId(e, s.src, s.dst, s.group)
            if (s.encrypted && s.active) e.encrypted = true
        }
        for (ev in events) {
            if (ev.type in listOf("call_start", "call_end", "data") && (ev.src != 0 || ev.dst != 0)) addId(e, ev.src, ev.dst, ev.group)
            if (ev.encrypted) e.encrypted = true
        }
        val types = ArrayList<String>()
        if (voice) types.add(tr("голос"))
        if (dec.stats["csbk"]!! >= 3 && !voice) types.add(tr("управляющий канал")) else if (dec.stats["csbk"]!! > 0) types.add("CSBK")
        if (events.any { it.type == "data" }) types.add(tr("данные"))
        if (types.isEmpty()) {
            val lasts = st.slots.values.map { it.last }.toSet()
            types.add(if (lasts.all { it in setOf("Idle", "", N_("Терминатор")) }) tr("маяк/дежурный") else tr("служебные пакеты"))
        }
        for (t in types) if (t !in e.types) e.types.add(t)
        e.label = types.joinToString(" · ")
        return "${fmtMhz(e.freq)}: ${e.describe()}"
    }

    private fun addId(e: Entry, src: Int, dst: Int, group: Boolean) {
        val s = "${if (src != 0) src else "?"} → ${if (group) "TG " else ""}${if (dst != 0) dst else "?"}"
        e.ids.remove(s)
        e.ids.add(0, s)
        while (e.ids.size > 5) e.ids.removeAt(e.ids.size - 1)
    }

    /** Готовая строка найденного — копия для интерфейса (сканер меняет свои записи в потоке приёма). */
    class Found(val freq: Double, val kind: String, val text: String, val ids: List<String>, val cc: List<Int>, val dmrHits: Int)

    /** Найденное (с хотя бы одним попаданием), по частоте — на конец последнего окна; из любого потока. */
    fun snapshot(): List<Found> = published

    /** Сколько найдено каналов DMR (для строки состояния). */
    fun dmrFound(): Int = dmrCount

    private fun fmtMhz(f: Double) = tr("{f} МГц", "f" to num(f / 1e6, 5))
}
