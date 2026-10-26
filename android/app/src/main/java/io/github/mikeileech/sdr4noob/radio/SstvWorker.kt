package io.github.mikeileech.sdr4noob.radio

import io.github.mikeileech.sdr4noob.dsp.SstvDecoder
import io.github.mikeileech.sdr4noob.dsp.SstvEvent
import io.github.mikeileech.sdr4noob.dsp.SstvLevel
import java.io.IOException
import java.io.InputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.TimeUnit

/**
 * Декодер SSTV на своём потоке (как _tap/_pump окна SSTV на ПК): движок радио отдаёт звук 48 кГц через
 * [feed] (быстро, без блокировки), здесь он сводится в моно и идёт в [SstvDecoder]. События и индикатор
 * уходят в [onEvents] — из потока декодера, интерфейс сам переносит их в свой поток.
 */
class SstvWorker(private val onEvents: (List<SstvEvent>, SstvLevel) -> Unit) {
    private val queue = ArrayBlockingQueue<FloatArray>(100)          // ~5 с звука при блоках по 50 мс
    private val commands = ConcurrentLinkedQueue<(SstvDecoder) -> Unit>()
    @Volatile private var running = true
    @Volatile var overflow = 0L; private set
    private val thread = Thread({ loop() }, "sstv").apply { isDaemon = true; start() }

    /** Звук радиорежима: [frames] стереокадров (L, R по очереди). Вызывается из потока движка. */
    fun feed(stereo: FloatArray, frames: Int) {
        if (!running || frames <= 0) return
        val mono = FloatArray(frames) { (stereo[2 * it] + stereo[2 * it + 1]) * 0.5f }
        if (!queue.offer(mono)) overflow++
    }

    /** Режим без VIS (null — «Авто»): начинать картинку и по синхроимпульсам, если VIS пропущен. */
    fun setForceMode(mode: String?) { commands.add { it.forceMode = mode } }
    fun startManual(mode: String) { commands.add { it.startManual(mode) } }
    fun reset() { commands.add { it.reset() } }

    fun stop() { running = false; thread.interrupt() }

    private fun loop() {
        val dec = SstvDecoder(48_000.0)
        try {
            while (running) {
                val first = queue.poll(250, TimeUnit.MILLISECONDS)
                while (true) { (commands.poll() ?: break).invoke(dec) }
                if (!running) break
                if (first == null) { onEvents(emptyList(), dec.level()); continue }
                val parts = arrayListOf(first)
                queue.drainTo(parts)
                val x = if (parts.size == 1) first else FloatArray(parts.sumOf { it.size }).also { a ->
                    var p = 0
                    for (c in parts) { System.arraycopy(c, 0, a, p, c.size); p += c.size }
                }
                onEvents(dec.process(x), dec.level())
            }
        } catch (_: InterruptedException) {
        }
    }

    companion object {
        /** Декодировать звук из WAV целиком (как sstv.decode_file): принятые и оборванные картинки. */
        fun decodeWav(input: InputStream): List<SstvEvent> {
            val (fs, x) = WavPcm.readMono(input)
            if (fs < 6000) throw IOException("частота дискретизации $fs Гц слишком мала для SSTV (нужно от 6 кГц)")
            return SstvDecoder.decodeAll(x, fs.toDouble())
        }
    }
}

/** Чтение WAV со звуком в моно float: PCM 8/16/24/32 бит и float 32 бит, любые каналы (среднее). */
object WavPcm {
    /** Предел — 30 минут звука при 48 кГц: больше картинкам SSTV не нужно, а память телефона не резиновая. */
    const val MAX_SAMPLES = 48_000 * 60 * 30

    fun readMono(input: InputStream): Pair<Int, FloatArray> {
        val hdr = readFully(input, 12)
        if (String(hdr, 0, 4, Charsets.US_ASCII) != "RIFF" || String(hdr, 8, 4, Charsets.US_ASCII) != "WAVE")
            throw IOException("это не WAV-файл")
        var fmt = -1; var ch = 0; var fs = 0; var bits = 0
        while (true) {
            val ck = readFully(input, 8)
            val id = String(ck, 0, 4, Charsets.US_ASCII)
            val size = ByteBuffer.wrap(ck, 4, 4).order(ByteOrder.LITTLE_ENDIAN).int.toLong() and 0xFFFFFFFFL
            if (id == "fmt ") {
                if (size < 16) throw IOException("неверный заголовок WAV")
                val f = ByteBuffer.wrap(readFully(input, size.toInt())).order(ByteOrder.LITTLE_ENDIAN)
                fmt = f.getShort(0).toInt() and 0xFFFF
                ch = f.getShort(2).toInt() and 0xFFFF
                fs = f.getInt(4)
                bits = f.getShort(14).toInt() and 0xFFFF
                if (fmt == 0xFFFE && size >= 26) fmt = f.getShort(24).toInt() and 0xFFFF   // WAVE_FORMAT_EXTENSIBLE
                if (size % 2 == 1L) skip(input, 1)
            } else if (id == "data") {
                if (fmt < 0) throw IOException("в WAV нет описания формата")
                if (ch < 1 || fs <= 0) throw IOException("неверный заголовок WAV")
                if (!(fmt == 1 && bits in intArrayOf(8, 16, 24, 32)) && !(fmt == 3 && bits == 32))
                    throw IOException("формат WAV не поддерживается (нужен PCM или float 32 бит)")
                return fs to readData(input, size, ch, bits, fmt == 3)
            } else skip(input, size + (size and 1))
        }
    }

    private fun readData(input: InputStream, size: Long, ch: Int, bits: Int, float: Boolean): FloatArray {
        val frame = bits / 8 * ch
        // размер 0 или 0xFFFFFFFF бывает у записей, не закрытых как следует: читаем до конца файла
        var left = if (size == 0L || size == 0xFFFFFFFFL) Long.MAX_VALUE else size
        var out = FloatArray(1 shl 16)
        var n = 0
        val buf = ByteArray(frame * 4096)
        var carry = 0
        while (left > 0) {
            val r = input.read(buf, carry, minOf((buf.size - carry).toLong(), left).toInt())
            if (r <= 0) break
            left -= r
            val have = carry + r
            val frames = have / frame
            if (n.toLong() + frames > MAX_SAMPLES) throw IOException("файл длиннее 30 минут — обрежьте его")
            if (n + frames > out.size) out = out.copyOf(maxOf(out.size * 2, n + frames))
            val bb = ByteBuffer.wrap(buf, 0, frames * frame).order(ByteOrder.LITTLE_ENDIAN)
            for (k in 0 until frames) {
                var s = 0f
                for (c in 0 until ch) s += when {
                    float -> bb.float
                    bits == 8 -> ((bb.get().toInt() and 0xFF) - 128) / 128f
                    bits == 16 -> bb.short / 32768f
                    bits == 24 -> {
                        val b0 = bb.get().toInt() and 0xFF; val b1 = bb.get().toInt() and 0xFF; val b2 = bb.get().toInt()
                        ((b2 shl 16) or (b1 shl 8) or b0) / 8388608f
                    }
                    else -> bb.int / 2147483648f
                }
                out[n++] = s / ch
            }
            carry = have - frames * frame
            if (carry > 0) System.arraycopy(buf, frames * frame, buf, 0, carry)
        }
        return out.copyOf(n)
    }

    private fun readFully(input: InputStream, n: Int): ByteArray {
        if (n < 0 || n > 1 shl 20) throw IOException("повреждённый WAV")
        val b = ByteArray(n)
        var p = 0
        while (p < n) {
            val r = input.read(b, p, n - p)
            if (r <= 0) throw IOException("WAV обрывается: нет звука")
            p += r
        }
        return b
    }

    private fun skip(input: InputStream, n: Long) {
        var left = n
        while (left > 0) {
            val s = input.skip(left)
            if (s > 0) left -= s
            else if (input.read() < 0) throw IOException("WAV обрывается: нет звука")
            else left--
        }
    }
}
