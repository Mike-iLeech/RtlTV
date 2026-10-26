package io.github.mikeileech.sdr4noob.radio

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.ParcelFileDescriptor
import android.provider.MediaStore
import io.github.mikeileech.sdr4noob.i18n.tr
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
class WavWriter(private val ch: FileChannel, val rate: Int, val channels: Int,
                /** Запись без разрывов (IQ): если диск не успел, остановиться, а не склеивать куски с дырами. */
                private val strict: Boolean = false,
                private val onClose: () -> Unit = {}) {
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
        if (!alive) return
        if (!queue.offer(b)) {
            dropped.addAndGet(n.toLong())
            if (strict && failed == null) { failed = tr("память телефона не успевает за потоком IQ — уменьшите полосу обзора"); close() }
        }
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
                if (bytes + b.remaining() > LIMIT) { failed = tr("файл дорос до предела WAV (4 ГБ)"); break }
                while (b.hasRemaining()) bytes += ch.write(b)
            }
        } catch (e: Throwable) {
            failed = if (failed != null) failed else if (e.message?.contains("ENOSPC") == true || e.message?.contains("space", true) == true)
                tr("нет места на телефоне") else tr("ошибка записи: {error}", "error" to (e.message ?: e.javaClass.simpleName))
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
    const val AUDIO = "Звук"          // как paths.AUDIO в настольной версии  // noqa: i18n
    const val IQ = "Звук/IQ"  // noqa: i18n

    class Out(val channel: FileChannel, val shownPath: String, val finish: () -> Unit)

    // ---------------- своя папка данных (выбрана в «Настройках» через системный выбор папки) ----------------
    private const val PREF_TREE = "data_tree"

    /** Своя папка данных (дерево SAF) или null — стандартные «Загрузки»/«Изображения». */
    fun tree(context: Context): Uri? =
        context.getSharedPreferences("sdr4noob", Context.MODE_PRIVATE).getString(PREF_TREE, null)?.let(Uri::parse)
            ?.takeIf { t -> context.contentResolver.persistedUriPermissions.any { it.uri == t && it.isWritePermission } }

    fun setTree(context: Context, uri: Uri?) {
        context.getSharedPreferences("sdr4noob", Context.MODE_PRIVATE).edit().putString(PREF_TREE, uri?.toString()).apply()
    }

    /** Как папку показывать человеку: последняя часть пути дерева (например «primary:Radio» → «Radio»). */
    fun treeName(uri: Uri): String =
        android.provider.DocumentsContract.getTreeDocumentId(uri).let { id ->
            id.substringAfter(':').ifEmpty { if (id.startsWith("primary")) tr("корень памяти телефона") else tr("корень карты памяти") }
        }

    /** Найти или создать вложенную папку [path] ("SDR4NOOB/Звук/IQ") в дереве; документ папки. */
    @Synchronized       // два одновременных сохранения не должны создать «SDR4NOOB (1)»
    private fun safDir(context: Context, tree: Uri, path: String): Uri {
        val cr = context.contentResolver
        var doc = android.provider.DocumentsContract.buildDocumentUriUsingTree(tree, android.provider.DocumentsContract.getTreeDocumentId(tree))
        for (seg in path.split('/').filter { it.isNotEmpty() }) {
            val id = android.provider.DocumentsContract.getDocumentId(doc)
            val children = android.provider.DocumentsContract.buildChildDocumentsUriUsingTree(tree, id)
            var found: Uri? = null
            cr.query(children, arrayOf(android.provider.DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                android.provider.DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                android.provider.DocumentsContract.Document.COLUMN_MIME_TYPE), null, null, null)?.use { c ->
                while (c.moveToNext()) if (c.getString(1) == seg && c.getString(2) == android.provider.DocumentsContract.Document.MIME_TYPE_DIR) {
                    found = android.provider.DocumentsContract.buildDocumentUriUsingTree(tree, c.getString(0)); break
                }
            }
            doc = found ?: android.provider.DocumentsContract.createDocument(cr, doc, android.provider.DocumentsContract.Document.MIME_TYPE_DIR, seg)
                ?: throw IllegalStateException(tr("не удалось создать папку {name} в {folder}", "name" to seg, "folder" to treeName(tree)))
        }
        return doc
    }

    private fun safCreate(context: Context, tree: Uri, sub: String, name: String, mime: String): Pair<Uri, String> {
        val dir = safDir(context, tree, "$ROOT/$sub")
        val uri = android.provider.DocumentsContract.createDocument(context.contentResolver, dir, mime, name)
            ?: throw IllegalStateException(tr("не удалось создать файл в {folder}", "folder" to treeName(tree)))
        // провайдер мог переименовать при совпадении имени («name (1).png») — показать настоящее имя
        val real = runCatching {
            context.contentResolver.query(uri, arrayOf(android.provider.DocumentsContract.Document.COLUMN_DISPLAY_NAME), null, null, null)
                ?.use { c -> if (c.moveToFirst()) c.getString(0) else null }
        }.getOrNull() ?: name
        return uri to "${treeName(tree)}/$ROOT/$sub/$real"
    }

    /** Своя папка недоступна (карту вынули, папку удалили): файл всё равно сохраняется — в стандартную. */
    private fun fallbackNote(shown: String) = tr("{path} (своя папка недоступна — сохранено в стандартную)", "path" to shown)

    fun open(context: Context, sub: String, name: String, mime: String = "audio/x-wav"): Out {
        var safFailed = false
        tree(context)?.let { t ->
            try {
                val (uri, shown) = safCreate(context, t, sub, name, mime)
                val pfd: ParcelFileDescriptor? = try { context.contentResolver.openFileDescriptor(uri, "rw") } catch (e: Exception) { null }
                if (pfd == null) {
                    runCatching { android.provider.DocumentsContract.deleteDocument(context.contentResolver, uri) }   // пустой файл не оставлять
                    throw IllegalStateException(tr("не удалось открыть файл в {folder}", "folder" to treeName(t)))
                }
                return Out(FileOutputStream(pfd.fileDescriptor).channel, shown) { runCatching { pfd.close() } }
            } catch (e: Exception) { safFailed = true }
        }
        val out = openStandard(context, sub, name, mime)
        return if (safFailed) Out(out.channel, fallbackNote(out.shownPath), out.finish) else out
    }

    private fun openStandard(context: Context, sub: String, name: String, mime: String): Out {
        if (Build.VERSION.SDK_INT >= 29) {
            val cr = context.contentResolver
            val v = ContentValues().apply {
                put(MediaStore.MediaColumns.DISPLAY_NAME, name)
                put(MediaStore.MediaColumns.MIME_TYPE, mime)
                put(MediaStore.MediaColumns.RELATIVE_PATH, "${Environment.DIRECTORY_DOWNLOADS}/$ROOT/$sub")
                put(MediaStore.MediaColumns.IS_PENDING, 1)
            }
            val uri: Uri = cr.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v)
                ?: throw IllegalStateException(tr("MediaStore не создал файл"))
            val pfd: ParcelFileDescriptor = cr.openFileDescriptor(uri, "rw")
                ?: throw IllegalStateException(tr("MediaStore не открыл файл"))
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

    // ---------------- картинки (SSTV, позже — спутники) ----------------
    const val SSTV = "SSTV"           // как paths.PICS_SSTV в настольной версии
    const val METEOR = "Метеор"       // как paths.PICS_METEOR  // noqa: i18n

    /** Где лежат картинки — для подсказок: Pictures/SDR4NOOB/SSTV (или своя папка данных). */
    fun picturesPath(sub: String) = "${Environment.DIRECTORY_PICTURES}/$ROOT/$sub"

    /** Где окажутся файлы [sub] — для «Настроек» и подсказок. */
    fun whereLabel(context: Context, sub: String, picture: Boolean): String =
        tree(context)?.let { "${treeName(it)}/$ROOT/$sub" }
            ?: if (Build.VERSION.SDK_INT >= 29) (if (picture) picturesPath(sub) else "${Environment.DIRECTORY_DOWNLOADS}/$ROOT/$sub")
            else "Android/data/${context.packageName}/files/" + (if (picture) "Pictures/" else "") + "$ROOT/$sub"

    /** Имя картинки, как в окне SSTV на ПК: sstv_20261024_101500_Martin_M1.png (местное время). */
    fun sstvName(mode: String, now: Date = Date()) =
        "sstv_" + stamp("yyyyMMdd_HHmmss", false, now) + "_" + mode.map { if (it.isLetterOrDigit()) it else '_' }.joinToString("") + ".png"

    /** Сохранить PNG в «Изображения/SDR4NOOB/[sub]»; возвращает путь для показа. */
    fun savePng(context: Context, sub: String, name: String, bmp: android.graphics.Bitmap): String {
        var safFailed = false
        tree(context)?.let { t ->
            try {
                val (uri, shown) = safCreate(context, t, sub, name, "image/png")
                try {
                    (context.contentResolver.openOutputStream(uri) ?: throw IllegalStateException(tr("файл не открылся"))).use {
                        if (!bmp.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, it)) throw IllegalStateException(tr("PNG не записан"))
                    }
                } catch (e: Exception) {
                    runCatching { android.provider.DocumentsContract.deleteDocument(context.contentResolver, uri) }
                    throw e
                }
                return shown
            } catch (e: Exception) { safFailed = true }      // снимок пролёта не повторить — сохранить в стандартную
        }
        val p = savePngStandard(context, sub, name, bmp)
        return if (safFailed) fallbackNote(p) else p
    }

    private fun savePngStandard(context: Context, sub: String, name: String, bmp: android.graphics.Bitmap): String {
        if (Build.VERSION.SDK_INT >= 29) {
            val cr = context.contentResolver
            val v = ContentValues().apply {
                put(MediaStore.MediaColumns.DISPLAY_NAME, name)
                put(MediaStore.MediaColumns.MIME_TYPE, "image/png")
                put(MediaStore.MediaColumns.RELATIVE_PATH, picturesPath(sub))
                put(MediaStore.MediaColumns.IS_PENDING, 1)
            }
            val uri = cr.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, v)
                ?: throw IllegalStateException(tr("MediaStore не создал файл"))
            try {
                (cr.openOutputStream(uri) ?: throw IllegalStateException(tr("MediaStore не открыл файл"))).use {
                    if (!bmp.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, it)) throw IllegalStateException(tr("PNG не записан"))
                }
            } catch (e: Exception) {
                runCatching { cr.delete(uri, null, null) }
                throw e
            }
            cr.update(uri, ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0) }, null, null)
            return "${picturesPath(sub)}/$name"
        }
        val dir = picturesDir(context, sub).apply { mkdirs() }
        val f = File(dir, name)
        FileOutputStream(f).use { if (!bmp.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, it)) throw IllegalStateException(tr("PNG не записан")) }
        return f.path
    }

    private fun picturesDir(context: Context, sub: String) =
        File(context.getExternalFilesDir(Environment.DIRECTORY_PICTURES) ?: context.filesDir, "$ROOT/$sub")

    /** Последние (до 60) картинки этой программы в «Изображения/SDR4NOOB/[sub]»: (имя, адрес), новые первыми. */
    fun listPictures(context: Context, sub: String): List<Pair<String, Uri>> {
        tree(context)?.let { t -> runCatching { listSaf(context, t, sub) }.getOrNull()?.let { return it } }
        return listStandard(context, sub)
    }

    private fun listSaf(context: Context, t: Uri, sub: String): List<Pair<String, Uri>> {
        run {
            val dir = safDir(context, t, "$ROOT/$sub")
            val id = android.provider.DocumentsContract.getDocumentId(dir)
            val out = ArrayList<Triple<String, Uri, Long>>()
            context.contentResolver.query(android.provider.DocumentsContract.buildChildDocumentsUriUsingTree(t, id),
                arrayOf(android.provider.DocumentsContract.Document.COLUMN_DOCUMENT_ID, android.provider.DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                    android.provider.DocumentsContract.Document.COLUMN_LAST_MODIFIED), null, null, null)?.use { c ->
                while (c.moveToNext()) {
                    val n = c.getString(1) ?: continue
                    if (n.endsWith(".png")) out += Triple(n, android.provider.DocumentsContract.buildDocumentUriUsingTree(t, c.getString(0)), c.getLong(2))
                }
            }
            return out.sortedByDescending { it.third }.take(60).map { it.first to it.second }
        }
    }

    private fun listStandard(context: Context, sub: String): List<Pair<String, Uri>> {
        if (Build.VERSION.SDK_INT >= 29) {
            val out = ArrayList<Pair<String, Uri>>()
            val col = MediaStore.Images.Media.EXTERNAL_CONTENT_URI
            context.contentResolver.query(col, arrayOf(MediaStore.MediaColumns._ID, MediaStore.MediaColumns.DISPLAY_NAME),
                "${MediaStore.MediaColumns.RELATIVE_PATH} = ?", arrayOf(picturesPath(sub) + "/"),
                "${MediaStore.MediaColumns.DATE_ADDED} DESC")?.use { c ->
                while (c.moveToNext() && out.size < 60)
                    out += c.getString(1) to android.content.ContentUris.withAppendedId(col, c.getLong(0))
            }
            return out
        }
        return (picturesDir(context, sub).listFiles { f -> f.name.endsWith(".png") } ?: emptyArray())
            .sortedByDescending { it.lastModified() }.take(60).map { it.name to Uri.fromFile(it) }
    }
}
