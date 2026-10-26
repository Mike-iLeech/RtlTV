package io.github.mikeileech.sdr4noob.radio

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.ParcelFileDescriptor
import android.provider.MediaStore
import java.io.File
import java.io.FileOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.channels.FileChannel
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.roundToInt

/**
 * WAV 16 бит PCM в произвольный канал с перемоткой (файл или документ MediaStore). Всё, что касается диска,
 * делает отдельный поток записи: заголовок, данные, размеры в заголовке и закрытие — поток приёма и интерфейс
 * диск не ждут. Если диск не успевает, лишние блоки отбрасываются ([dropped]); если запись не удалась (нет места,
 * карта извлечена) или файл дорос до предела WAV в 4 ГБ — поток останавливается, причина — в [failed],
 * приложение при этом не падает.
 */
class WavWriter(private val ch: FileChannel, val rate: Int, val channels: Int, private val onClose: () -> Unit = {}) {
    companion object {
        /** Предел формата WAV: размер в заголовке — 32 бита. */
        const val LIMIT = 0xFFFFFFFFL - 44
    }

    private val queue = ArrayBlockingQueue<ByteBuffer>(64)
    private val stopMark = ByteBuffer.allocate(0)
    @Volatile var bytes = 0L; private set
    /** Почему запись остановилась сама (null — пишет или закрыта штатно). */
    @Volatile var failed: String? = null; private set
    val dropped = AtomicLong()
    @Volatile private var closing = false
    private val thread = Thread(::run, "sdr4noob-wav").apply { start() }

    private fun header(dataBytes: Long): ByteBuffer {
        val b = ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN)
        val size = dataBytes.coerceAtMost(LIMIT)
        b.put("RIFF".toByteArray(Charsets.US_ASCII)).putInt((36 + size).toInt()).put("WAVE".toByteArray(Charsets.US_ASCII))
        b.put("fmt ".toByteArray(Charsets.US_ASCII)).putInt(16).putShort(1).putShort(channels.toShort()).putInt(rate)
            .putInt(rate * channels * 2).putShort((channels * 2).toShort()).putShort(16)
        b.put("data".toByteArray(Charsets.US_ASCII)).putInt(size.toInt())
        b.flip()
        return b
    }

    val alive get() = thread.isAlive && !closing && failed == null

    private fun offer(b: ByteBuffer, n: Int) {
        if (!alive || !queue.offer(b)) dropped.addAndGet(n.toLong())
    }

    /** Отсчёты float ±1 (чередование каналов), [n] — число значений (а не кадров). */
    fun write(x: FloatArray, n: Int) {
        if (!alive) return
        val b = ByteBuffer.allocate(n * 2).order(ByteOrder.LITTLE_ENDIAN)
        for (i in 0 until n) b.putShort((x[i].coerceIn(-1f, 1f) * 32767f).roundToInt().toShort())
        b.flip()
        offer(b, n)
    }

    /** Комплексные отсчёты (I — левый канал, Q — правый), как IqRecorder в rtltv/iqfile.py. */
    fun writeIq(re: FloatArray, im: FloatArray, n: Int) {
        if (!alive) return
        val b = ByteBuffer.allocate(n * 4).order(ByteOrder.LITTLE_ENDIAN)
        for (i in 0 until n) {
            b.putShort((re[i].coerceIn(-1f, 1f) * 32767f).roundToInt().toShort())
            b.putShort((im[i].coerceIn(-1f, 1f) * 32767f).roundToInt().toShort())
        }
        b.flip()
        offer(b, n)
    }

    val seconds get() = bytes.toDouble() / (rate * channels * 2)

    private fun run() {
        try {
            ch.position(0)
            ch.write(header(0))
            while (true) {
                val b = queue.take()
                if (b === stopMark) break
                if (bytes + b.remaining() > LIMIT) { failed = "файл дорос до предела WAV (4 ГБ)"; break }
                while (b.hasRemaining()) bytes += ch.write(b)
            }
        } catch (e: Throwable) {
            failed = if (e.message?.contains("ENOSPC") == true || e.message?.contains("space", true) == true)
                "нет места на телефоне" else "ошибка записи: ${e.message ?: e.javaClass.simpleName}"
        } finally {
            queue.clear()
            runCatching { ch.write(header(bytes), 0) }
            runCatching { ch.force(true) }
            runCatching { ch.close() }
            runCatching { onClose() }
        }
    }

    /** Закончить запись, не дожидаясь диска: остаток очереди допишет поток записи. */
    fun close() {
        if (closing) return
        closing = true
        if (!queue.offer(stopMark)) { queue.clear(); queue.offer(stopMark) }   // очередь полна — хвост отбрасываем
    }

    /** Дождаться окончания записи (для тестов и выхода). */
    fun await(ms: Long = 10_000) { thread.join(ms) }
}

/**
 * Куда пишутся записи: «Загрузки/SDR4NOOB/…» (видно в любом файловом менеджере и на ПК по USB) — через
 * MediaStore на Android 10+, без разрешений; на Android 7–9 — папка программы на карте памяти
 * (Android/data/…/files/SDR4NOOB/…), тоже без разрешений.
 */
object Storage {
    const val ROOT = "SDR4NOOB"
    const val AUDIO = "Звук"          // как paths.AUDIO в настольной версии
    const val IQ = "Звук/IQ"

    class Out(val channel: FileChannel, val shownPath: String, val finish: () -> Unit)

    fun open(context: Context, sub: String, name: String, mime: String = "audio/x-wav"): Out {
        if (Build.VERSION.SDK_INT >= 29) {
            val cr = context.contentResolver
            val v = ContentValues().apply {
                put(MediaStore.MediaColumns.DISPLAY_NAME, name)
                put(MediaStore.MediaColumns.MIME_TYPE, mime)
                put(MediaStore.MediaColumns.RELATIVE_PATH, "${Environment.DIRECTORY_DOWNLOADS}/$ROOT/$sub")
                put(MediaStore.MediaColumns.IS_PENDING, 1)
            }
            val uri: Uri = cr.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v)
                ?: throw IllegalStateException("MediaStore не создал файл")
            val pfd: ParcelFileDescriptor = cr.openFileDescriptor(uri, "rw")
                ?: throw IllegalStateException("MediaStore не открыл файл")
            val chan = FileOutputStream(pfd.fileDescriptor).channel
            return Out(chan, "${Environment.DIRECTORY_DOWNLOADS}/$ROOT/$sub/$name") {
                runCatching { pfd.close() }
                cr.update(uri, ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0) }, null, null)
            }
        }
        val dir = File(context.getExternalFilesDir(null) ?: context.filesDir, "$ROOT/$sub").apply { mkdirs() }
        val f = File(dir, name)
        return Out(java.io.RandomAccessFile(f, "rw").channel, f.path) {}
    }

    private fun stamp(fmt: String, utc: Boolean, now: Date) =
        SimpleDateFormat(fmt, Locale.ROOT).apply { if (utc) timeZone = TimeZone.getTimeZone("UTC") }.format(now)

    /** Имя записи звука, как в радиорежиме ПК: radio_101.2000MHz_20261024_101500.wav (местное время). */
    fun audioName(freqHz: Long, now: Date = Date()) =
        String.format(Locale.ROOT, "radio_%.4fMHz_", freqHz / 1e6) + stamp("yyyyMMdd_HHmmss", false, now) + ".wav"

    /** Имя записи IQ, как iqfile.file_name: SDR4NOOB_20261024_101500Z_101200000Hz_2400000sps_IQ.wav (UTC). */
    fun iqName(centerHz: Long, fs: Int, now: Date = Date()) =
        "SDR4NOOB_" + stamp("yyyyMMdd_HHmmss", true, now) + "Z_${centerHz}Hz_${fs}sps_IQ.wav"
}
