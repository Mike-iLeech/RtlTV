package io.github.mikeileech.sdr4noob.radio

import io.github.mikeileech.sdr4noob.lrpt.AutoLrptReceiver
import io.github.mikeileech.sdr4noob.lrpt.LrptGray
import io.github.mikeileech.sdr4noob.lrpt.LrptStats
import io.github.mikeileech.sdr4noob.lrpt.LrptView
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.TimeUnit

/** Снимок: ARGB, размер, какие каналы в композите. */
class LrptPicture(val argb: IntArray, val width: Int, val height: Int, val key: String)

/**
 * Приём «Метеор-М» LRPT на своём потоке (как RxWorker на ПК): сырой IQ от движка через [feed]; где сигнал
 * относительно центра приёмника (ПЧ + доплер) — [offsetHz], частота дискретизации потока — [rate] (сменилась —
 * приём заново). Демодулятор сам выбирает 72k или 80k с перемежением.
 *
 * Каждый пролёт — свой снимок: если синхронизации нет дольше [PASS_GAP_S] после принятых кадров (пролёт кончился)
 * или снимок длиннее [MAX_LINES] строк, принятое отдаётся в [onPassEnd] полным снимком и приём начинается заново.
 * Для показа публикуется уменьшенная копия [preview] (раз в 2 с, только если [wantPreview]) — полный снимок
 * пролёта занимает десятки МБ.
 */
class LrptWorker(fs: Int, private val onPassEnd: (LrptPicture) -> Unit = {}, private val onError: (String) -> Unit = {}) {
    companion object {
        const val PASS_GAP_S = 90.0
        const val MAX_LINES = 8000
        const val PREVIEW_LINES = 1200
    }

    private val queue = ArrayBlockingQueue<FloatArray>(64)
    private val commands = ConcurrentLinkedQueue<(AutoLrptReceiver) -> Unit>()
    @Volatile private var running = true
    @Volatile var offsetHz = 0.0
    @Volatile var rate = fs
    @Volatile var wantPreview = false
    @Volatile var stats: LrptStats? = null; private set
    @Volatile var preview: LrptPicture? = null; private set
    /** Блоков, отброшенных из-за того, что декодер не успевал (телефону не хватает скорости). */
    @Volatile var dropped = 0L; private set
    @Volatile var combo = "125"
    private val thread = Thread({ loop() }, "lrpt").apply { isDaemon = true; start() }   // последним

    fun feed(re: FloatArray, im: FloatArray, n: Int) {
        if (!running || n <= 0) return
        val iq = FloatArray(2 * n)
        for (i in 0 until n) { iq[2 * i] = re[i]; iq[2 * i + 1] = im[i] }
        if (!queue.offer(iq)) dropped++
    }

    /** Полный снимок текущего пролёта — строится в потоке декодера и отдаётся в [cb] (null — снимка нет). */
    fun requestFull(cb: (LrptPicture?) -> Unit) { commands.add { rx -> cb(full(rx)) } }

    /**
     * Полный снимок; не хватило памяти — снимок вдвое меньше (приём при этом не сбрасывается: принятое
     * важнее одной попытки сохранить).
     */
    private fun full(rx: AutoLrptReceiver): LrptPicture? =
        try { picture(rx, Int.MAX_VALUE) } catch (_: OutOfMemoryError) {
            try { picture(rx, maxOf(1, rx.rx.imager.lines() / 2)) } catch (_: OutOfMemoryError) { null }
        }

    /** Отдать снимок пролёта (если кадры были), закрыть приёмник и начать новый. */
    private fun endPass(rx: AutoLrptReceiver, fs: Int): AutoLrptReceiver {
        runCatching { rx.finish() }                                   // последние кадры из буферов
        if (runCatching { rx.stats().framesOk }.getOrDefault(0) > 0) full(rx)?.let(onPassEnd)
        rx.rxs.forEach { runCatching { it.close() } }               // нативные демодуляторы — сразу, не в finalize
        preview = null
        return AutoLrptReceiver(fs.toDouble())
    }

    fun stop() { running = false; thread.interrupt() }

    private fun loop() {
        var fs = rate
        var rx = AutoLrptReceiver(fs.toDouble())
        var off = Double.NaN
        var lastPub = 0L
        var lastSync = System.nanoTime()
        var reported = false
        while (running) {
            try {
                val x = queue.poll(250, TimeUnit.MILLISECONDS)
                while (true) { (commands.poll() ?: break).invoke(rx) }
                if (rate != fs) { rx = endPass(rx, rate); fs = rate; off = Double.NaN; queue.clear(); continue }
                val o = offsetHz
                if (o != off) { off = o; rx.setOffset(o) }
                if (x != null) rx.feed(x, wantCons = false)
                val now = System.nanoTime()
                if (now - lastPub > 2_000_000_000L) {
                    lastPub = now
                    val st = rx.stats()
                    stats = st
                    if (st.sync) lastSync = now
                    val gap = (now - lastSync) / 1e9
                    if (st.framesOk > 0 && (gap > PASS_GAP_S || st.lines > MAX_LINES)) {
                        // пролёт кончился (или снимок слишком длинный): отдать целиком и начать заново
                        rx = endPass(rx, fs); off = Double.NaN; lastSync = now
                        continue
                    }
                    if (wantPreview) preview = picture(rx, PREVIEW_LINES)
                }
            } catch (_: InterruptedException) {
                break
            } catch (e: Throwable) {
                // сбой или нехватка памяти не роняют приложение: приём начинается заново
                if (!reported) { reported = true; onError("LRPT: сбой декодера (${e.javaClass.simpleName}), приём начат заново") }
                rx.rxs.forEach { runCatching { it.close() } }
                rx = AutoLrptReceiver(fs.toDouble()); off = Double.NaN; preview = null
            }
        }
        // закрыли режим или сменили спутник посреди пролёта — принятое не пропадает
        runCatching { endPass(rx, fs).rxs.forEach { it.close() } }
    }

    /** Композит (или канал) снимка, уменьшенный до [maxLines] строк прореживанием. */
    private fun picture(rx: AutoLrptReceiver, maxLines: Int): LrptPicture? {
        val all = rx.rx.imager.allImages()
        if (all.isEmpty()) return null
        val h = all.values.minOf { it.height }
        val k = maxOf(1, (h + maxLines - 1) / maxLines)
        val imgs = if (k == 1) all else all.mapValues { (_, g) -> shrink(g, k) }
        val p = LrptView.preview(imgs, combo)
        val rgb = p.rgb
        val g = p.gray
        return when {
            rgb != null -> LrptPicture(rgb.argb(), rgb.width, rgb.height, p.key)
            g != null -> LrptPicture(IntArray(g.width * g.height) { val v = g.px[it].toInt() and 0xFF; (0xFF shl 24) or (v shl 16) or (v shl 8) or v },
                g.width, g.height, p.key)
            else -> null
        }
    }

    /** Прореживание в [k] раз по обеим осям (для показа на экране телефона хватает). */
    private fun shrink(g: LrptGray, k: Int): LrptGray {
        val w = g.width / k; val h = g.height / k
        return LrptGray(w, h, ByteArray(w * h) { g.px[(it / w) * k * g.width + (it % w) * k] })
    }
}
