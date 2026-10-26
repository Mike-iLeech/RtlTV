package io.github.mikeileech.sdr4noob.radio

import io.github.mikeileech.sdr4noob.lrpt.AutoLrptReceiver
import io.github.mikeileech.sdr4noob.lrpt.LrptStats
import io.github.mikeileech.sdr4noob.lrpt.LrptView
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit

/** Снимок для показа: ARGB, размер, какие каналы в композите. */
class LrptPicture(val argb: IntArray, val width: Int, val height: Int, val key: String)

/**
 * Приём «Метеор-М» LRPT на своём потоке (как RxWorker на ПК): сырой IQ от движка через [feed], смещение сигнала
 * от центра настройки (ПЧ + доплер) — [offsetHz]; демодулятор сам выбирает 72k или 80k с перемежением.
 * Статистика и снимок публикуются раз в 2 с.
 */
class LrptWorker(private val fs: Int, private val onError: (String) -> Unit = {}) {
    private val queue = ArrayBlockingQueue<FloatArray>(64)
    @Volatile private var running = true
    /** Где сигнал относительно центра приёмника, Гц (меняется доплером). */
    @Volatile var offsetHz = 0.0
    @Volatile var stats: LrptStats? = null; private set
    @Volatile var picture: LrptPicture? = null; private set
    @Volatile var dropped = 0L; private set
    /** Комбинация каналов композита (как на ПК: «125» — естественные цвета днём). */
    @Volatile var combo = "125"
    private val thread = Thread({ loop() }, "lrpt").apply { isDaemon = true; start() }   // последним

    fun feed(re: FloatArray, im: FloatArray, n: Int) {
        if (!running || n <= 0) return
        val iq = FloatArray(2 * n)
        for (i in 0 until n) { iq[2 * i] = re[i]; iq[2 * i + 1] = im[i] }
        if (!queue.offer(iq)) dropped++
    }

    fun stop() { running = false; thread.interrupt() }

    private fun loop() {
        var rx = AutoLrptReceiver(fs.toDouble())
        var off = Double.NaN
        var lastPub = 0L
        var reported = false
        while (running) {
            try {
                val x = queue.poll(250, TimeUnit.MILLISECONDS)
                val o = offsetHz
                if (o != off) { off = o; rx.setOffset(o) }
                if (x != null) rx.feed(x, wantCons = false)
                val now = System.nanoTime()
                if (now - lastPub > 2_000_000_000L) { lastPub = now; publish(rx) }
            } catch (_: InterruptedException) {
                break
            } catch (e: Exception) {
                // сбой декодера не роняет приложение: принятое сохраняется в последнем снимке, приём — заново
                if (!reported) { reported = true; onError("LRPT: сбой декодера (${e.javaClass.simpleName}), приём начат заново") }
                rx = AutoLrptReceiver(fs.toDouble()); off = Double.NaN
            }
        }
        runCatching { rx.finish(); publish(rx) }
    }

    private fun publish(rx: AutoLrptReceiver) {
        stats = rx.stats()
        val imgs = rx.rx.imager.allImages()
        if (imgs.isEmpty()) return
        val p = LrptView.preview(imgs, combo)
        val rgb = p.rgb
        val g = p.gray
        picture = when {
            rgb != null -> LrptPicture(rgb.argb(), rgb.width, rgb.height, p.key)
            g != null -> LrptPicture(IntArray(g.width * g.height) { val v = g.px[it].toInt() and 0xFF; (0xFF shl 24) or (v shl 16) or (v shl 8) or v },
                g.width, g.height, p.key)
            else -> null
        }
    }
}
