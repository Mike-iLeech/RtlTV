package io.github.mikeileech.sdr4noob.sdr

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import io.github.mikeileech.sdr4noob.dsp.RadioDemod
import java.io.DataInputStream
import java.io.EOFException
import java.io.InputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Воспроизведение записей IQ — перенос rtltv/iqfile.py: WAV 8/16 бит PCM и 32 бит float (I — левый, Q — правый),
 * сырые .cu8/.raw/.bin (uint8, как rtl_sdr), .cs8, .cs16, .cf32/.cfile/.fc32. Частота дискретизации — из
 * заголовка WAV или имени файла, центральная частота — из имени (…_101200000Hz…, …_101.2MHz…).
 */
object IqName {
    val RAW_KINDS = mapOf(".cu8" to "u8", ".raw" to "u8", ".bin" to "u8", ".dat" to "u8", ".cs8" to "s8",
        ".cs16" to "s16", ".cf32" to "f32", ".cfile" to "f32", ".fc32" to "f32")

    /** (частота дискретизации | null, центральная частота | null) из имени — как parse_name. */
    fun parse(name: String): Pair<Int?, Double?> {
        var fs: Double? = null
        var center: Double? = null
        Regex("""(\d+(?:\.\d+)?)\s*(k|M)?(?:sps|SPS|S/s|Sps)""").find(name)?.let { m ->
            fs = m.groupValues[1].toDouble() * when (m.groupValues[2]) { "k" -> 1e3; "M" -> 1e6; else -> 1.0 }
        }
        Regex("""(\d+(?:\.\d+)?)\s*(k|M|G)?Hz""", RegexOption.IGNORE_CASE).find(name)?.let { m ->
            center = m.groupValues[1].toDouble() * when (m.groupValues[2].lowercase()) { "k" -> 1e3; "m" -> 1e6; "g" -> 1e9; else -> 1.0 }
        }
        if (fs == null) Regex("""[_-](\d{3,5})k(?:[_.-]|$)""").find(name)?.let { fs = it.groupValues[1].toDouble() * 1e3 }
        return fs?.toInt()?.takeIf { it > 0 } to center?.takeIf { it > 0 }
    }

    /** Ближайшая частота радиорежима не ниже fs (иначе максимальная) — как target_rate. */
    fun targetRate(fs: Int, rates: IntArray = RadioDemod.RADIO_RATES): Int =
        if (fs in rates) fs else rates.filter { it >= fs }.minOrNull() ?: rates.max()
}

class IqFormatException(msg: String) : Exception(msg)

/**
 * Потоковый пересчёт частоты линейной интерполяцией: [step] — шаг по входу на один выходной отсчёт.
 * Хранится окно ещё нужных входных отсчётов и дробная позиция выхода в нём — стыки блоков без скачков и потерь.
 */
class LinearResampler(private val step: Double) {
    private var wr = FloatArray(0); private var wi = FloatArray(0)
    private var have = 0            // отсчётов в окне
    private var pos = 0.0           // позиция следующего выхода относительно wr[0]

    /** Выдать [n] отсчётов в [re]/[im]; [pull] дочитывает входные отсчёты. */
    fun process(n: Int, re: FloatArray, im: FloatArray, pull: (FloatArray, FloatArray, Int) -> Unit) {
        val needLen = (pos + (n - 1) * step).toInt() + 2       // индексы до floor(последней позиции) + 1
        if (needLen > have) {
            if (wr.size < needLen) { wr = wr.copyOf(needLen); wi = wi.copyOf(needLen) }
            val k = needLen - have
            val tr = FloatArray(k); val ti = FloatArray(k)
            pull(tr, ti, k)
            System.arraycopy(tr, 0, wr, have, k); System.arraycopy(ti, 0, wi, have, k)
            have = needLen
        }
        for (j in 0 until n) {
            val q = pos + j * step
            val k = q.toInt(); val f = (q - k).toFloat()
            re[j] = wr[k] + (wr[k + 1] - wr[k]) * f
            im[j] = wi[k] + (wi[k + 1] - wi[k]) * f
        }
        pos += n * step
        val drop = minOf(pos.toInt(), have)                    // всё до целой позиции больше не нужно
        System.arraycopy(wr, drop, wr, 0, have - drop); System.arraycopy(wi, drop, wi, 0, have - drop)
        have -= drop
        pos -= drop
    }
}

/** Чтение файла IQ блоками по кругу. [open] открывает поток с начала файла. */
class IqFileReader(private val open: () -> InputStream, name: String, private val sizeBytes: Long) {
    val kind: String
    val fs: Int
    val center: Double?
    private val offset: Long
    val total: Long
    private val width: Int
    private var input: DataInputStream? = null
    private var pos = 0L

    init {
        val ext = name.substringAfterLast('.', "").lowercase().let { if (it.isEmpty()) "" else ".$it" }
        val (nFs, nCenter) = IqName.parse(name)
        center = nCenter
        if (ext == ".wav") {
            val w = wavInfo()
            kind = w.kind; fs = w.rate; offset = w.offset
            var len = sizeBytes - offset
            if (w.size != 0L && w.size != 0xFFFFFFFFL) len = minOf(len, w.size)
            width = when (kind) { "u8", "s8" -> 2; "s16" -> 4; else -> 8 }
            total = len / width
        } else {
            kind = IqName.RAW_KINDS[ext] ?: throw IqFormatException("Неизвестный формат файла IQ: ${ext.ifEmpty { "без расширения" }}")
            fs = nFs ?: throw IqFormatException("Не удалось определить частоту дискретизации по имени файла " +
                "(нужно, например, …_2400000sps… или …_2.4MSPS…)")
            offset = 0
            width = when (kind) { "u8", "s8" -> 2; "s16" -> 4; else -> 8 }
            total = sizeBytes / width
        }
        if (total <= 0) throw IqFormatException("Файл пуст")
    }

    private class WavInfo(val kind: String, val rate: Int, val offset: Long, val size: Long)

    private fun wavInfo(): WavInfo = DataInputStream(open().buffered()).use { s ->
        val hdr = ByteArray(12)
        try { s.readFully(hdr) } catch (_: EOFException) { throw IqFormatException("Это не WAV-файл") }
        val id = String(hdr, 0, 4, Charsets.US_ASCII)
        if ((id != "RIFF" && id != "RF64") || String(hdr, 8, 4, Charsets.US_ASCII) != "WAVE") throw IqFormatException("Это не WAV-файл")
        var off = 12L
        var fmt: IntArray? = null
        while (true) {
            val ch = ByteArray(8)
            try { s.readFully(ch) } catch (_: EOFException) { break }
            off += 8
            val cid = String(ch, 0, 4, Charsets.US_ASCII)
            val size = ByteBuffer.wrap(ch, 4, 4).order(ByteOrder.LITTLE_ENDIAN).int.toLong() and 0xFFFFFFFFL
            when (cid) {
                "fmt " -> {
                    val body = ByteArray(size.toInt().coerceIn(16, 1024))
                    s.readFully(body)
                    val b = ByteBuffer.wrap(body).order(ByteOrder.LITTLE_ENDIAN)
                    var tag = b.getShort(0).toInt() and 0xFFFF
                    val nch = b.getShort(2).toInt(); val rate = b.getInt(4); val bits = b.getShort(14).toInt()
                    if (tag == 0xFFFE && body.size >= 26) tag = b.getShort(24).toInt() and 0xFFFF   // WAVE_FORMAT_EXTENSIBLE
                    fmt = intArrayOf(tag, nch, rate, bits)
                    off += body.size
                    if (size > body.size) { s.skipBytes((size - body.size).toInt()); off += size - body.size }
                    if (size % 2 == 1L) { s.skipBytes(1); off++ }
                }
                "data" -> {
                    val f = fmt ?: throw IqFormatException("WAV без заголовка формата")
                    if (f[1] != 2) throw IqFormatException("В WAV ${f[1]} канал(а), а для IQ нужно 2 (I и Q)")
                    val kind = when {
                        f[0] == 1 && f[3] == 8 -> "u8"
                        f[0] == 1 && f[3] == 16 -> "s16"
                        f[0] == 3 && f[3] == 32 -> "f32"
                        else -> throw IqFormatException("Формат WAV не поддерживается (код ${f[0]}, ${f[3]} бит)")
                    }
                    return WavInfo(kind, f[2], off, size)
                }
                else -> { var skip = size + (size % 2); while (skip > 0) { val k = s.skip(skip); if (k <= 0) break; skip -= k }; off += size + (size % 2) }
            }
        }
        throw IqFormatException("В WAV нет данных")
    }

    val seconds get() = total.toDouble() / fs

    private fun reopen() {
        input?.close()
        val s = DataInputStream(open().buffered(1 shl 16))
        var skip = offset
        while (skip > 0) { val k = s.skip(skip); if (k <= 0) break; skip -= k }
        input = s
        pos = 0
    }

    private var raw = ByteArray(0)

    /** Прочитать [n] комплексных отсчётов (по кругу) в [re]/[im]. */
    fun read(re: FloatArray, im: FloatArray, n: Int) {
        var got = 0
        while (got < n) {
            if (input == null || pos >= total) reopen()
            val take = minOf((n - got).toLong(), total - pos).toInt()
            val bytes = take * width
            if (raw.size < bytes) raw = ByteArray(bytes)
            try { input!!.readFully(raw, 0, bytes) } catch (_: EOFException) { pos = total; continue }
            val b = ByteBuffer.wrap(raw, 0, bytes).order(ByteOrder.LITTLE_ENDIAN)
            for (i in 0 until take) {
                val j = got + i
                when (kind) {
                    "u8" -> { re[j] = ((raw[2 * i].toInt() and 0xFF) - 127.4f) / 128f; im[j] = ((raw[2 * i + 1].toInt() and 0xFF) - 127.4f) / 128f }
                    "s8" -> { re[j] = raw[2 * i] / 128f; im[j] = raw[2 * i + 1] / 128f }
                    "s16" -> { re[j] = b.getShort(4 * i) / 32768f; im[j] = b.getShort(4 * i + 2) / 32768f }
                    else -> { re[j] = b.getFloat(8 * i); im[j] = b.getFloat(8 * i + 4) }
                }
            }
            got += take
            pos += take
        }
    }

    fun close() { input?.close(); input = null }
}

/**
 * Источник «файл IQ»: блоки в реальном темпе по кругу. Частоту и полосу задаёт файл (перестройка центра
 * невозможна — VFO ходит только внутри записанной полосы). Если частота дискретизации не подходит радиорежиму,
 * поток пересчитывается линейной интерполяцией вверх до ближайшей подходящей (как target_rate на ПК).
 */
class IqFileSource(private val reader: IqFileReader, private val name: String) : SdrSource {
    companion object {
        fun open(context: Context, uri: Uri): IqFileSource {
            val cr = context.contentResolver
            var name = uri.lastPathSegment ?: "file"
            var size = -1L
            cr.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { c ->
                if (c.moveToFirst()) {
                    c.getString(0)?.let { name = it }
                    if (!c.isNull(1)) size = c.getLong(1)
                }
            }
            if (size <= 0) size = cr.openAssetFileDescriptor(uri, "r")?.use { it.length } ?: -1
            if (size <= 0) throw SdrException("Не удалось узнать размер файла")
            return try {
                IqFileSource(IqFileReader({ cr.openInputStream(uri) ?: throw SdrException("Файл не открывается") }, name, size), name)
            } catch (e: IqFormatException) { throw SdrException(e.message ?: "Неверный файл IQ", e) }
        }
    }

    override val label get() = "Файл IQ: $name"
    override val gains = IntArray(0)
    val fileRate = reader.fs
    override val fixedRate: Int = IqName.targetRate(fileRate)
    override val fixedCenter: Long = (reader.center ?: 100e6).toLong()
    override val sampleRates get() = intArrayOf(fixedRate)

    private var t0 = 0L
    private var produced = 0L
    private val resampler = if (fixedRate == fileRate) null else LinearResampler(fileRate.toDouble() / fixedRate)

    override fun open(sampleRate: Int, frequencyHz: Long) {}
    override fun start() { t0 = System.nanoTime(); produced = 0 }

    override fun read(re: FloatArray, im: FloatArray, maxSamples: Int): Int {
        val due = (System.nanoTime() - t0) / 1000 * fixedRate / 1_000_000L
        if (due - produced > fixedRate) produced = due - maxSamples
        val lag = due - produced
        if (lag < maxSamples) { Thread.sleep(maxOf(1L, (maxSamples - lag) * 1000L / fixedRate)); return 0 }
        val rs = resampler
        if (rs == null) reader.read(re, im, maxSamples) else rs.process(maxSamples, re, im) { r, i, n -> reader.read(r, i, n) }
        produced += maxSamples
        return maxSamples
    }

    override fun setFrequency(hz: Long) {}
    override fun setSampleRate(rate: Int) {}
    override fun setGain(tenthsDb: Int) {}
    override fun close() { reader.close() }
}
