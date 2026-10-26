package io.github.mikeileech.sdr4noob.radio

import io.github.mikeileech.sdr4noob.dsp.Adsb
import io.github.mikeileech.sdr4noob.dsp.AdsbLevels
import io.github.mikeileech.sdr4noob.dsp.AdsbProcessor
import io.github.mikeileech.sdr4noob.dsp.AdsbTracker
import io.github.mikeileech.sdr4noob.dsp.AfskDemodulator
import io.github.mikeileech.sdr4noob.dsp.Aprs
import io.github.mikeileech.sdr4noob.dsp.AprsPacket
import io.github.mikeileech.sdr4noob.i18n.tr
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit

/** Принятый пакет APRS для журнала: время приёма (мс), разбор, строка описания. */
class AprsRx(val timeMs: Long, val packet: AprsPacket, val text: String, val fixed: Int)

/** Станция APRS на карте: последняя позиция и путь (как слой станций в окне APRS на ПК). */
class AprsStation(val name: String, val lat: Double, val lon: Double, val symbol: String?, val last: AprsRx,
                  val track: List<Pair<Double, Double>>, val count: Int)

/**
 * Декодер APRS на своём потоке (как окно APRS на ПК): звук NFM 48 кГц от движка через [feed], AFSK 1200
 * → кадры AX.25 → разбор. Новые пакеты уходят в [onPackets] из потока декодера; станции с координатами —
 * в [stations] (готовая копия, читать можно из любого потока).
 */
class AprsWorker(private val onPackets: (List<AprsRx>) -> Unit, private val onError: (String) -> Unit = {}) {
    private val queue = ArrayBlockingQueue<FloatArray>(200)
    @Volatile private var running = true
    @Volatile var stations: List<AprsStation> = emptyList(); private set
    @Volatile var total = 0; private set
    private val byName = LinkedHashMap<String, AprsStation>()
    private val thread = Thread({ loop() }, "aprs").apply { isDaemon = true; start() }

    fun feed(stereo: FloatArray, frames: Int) {
        if (!running || frames <= 0) return
        queue.offer(FloatArray(frames) { (stereo[2 * it] + stereo[2 * it + 1]) * 0.5f })
    }

    fun stop() { running = false; thread.interrupt() }

    private fun loop() {
        var dem = AfskDemodulator(48_000.0)
        var reported = false
        while (running) {
            try {
                val x = queue.poll(250, TimeUnit.MILLISECONDS) ?: continue
                val got = ArrayList<AprsRx>()
                for (f in dem.process(x)) {
                    // испорченный кадр не должен останавливать приём остальных
                    val p = try { Aprs.parseFrame(f.data) } catch (_: Exception) { null } ?: continue
                    got += AprsRx(System.currentTimeMillis(), p, Aprs.describe(p), f.fixed)
                }
                if (got.isEmpty()) continue
                total += got.size
                for (rx in got) note(rx)
                stations = byName.values.sortedByDescending { it.last.timeMs }
                onPackets(got)
            } catch (_: InterruptedException) {
                break
            } catch (e: Exception) {
                dem = AfskDemodulator(48_000.0)      // сбой демодулятора не роняет приложение: начать заново
                if (!reported) { reported = true; onError(tr("APRS: сбой декодера ({error}), декодер перезапущен", "error" to e.javaClass.simpleName)) }
            }
        }
    }

    private fun note(rx: AprsRx) {
        val p = rx.packet
        val lat = p.lat ?: return
        val lon = p.lon ?: return
        val name = p.name?.takeIf { it.isNotBlank() } ?: p.src    // объекты и элементы — под своим именем
        val old = byName[name]
        val track = (old?.track ?: emptyList()).let { t -> if (t.lastOrNull() == lat to lon) t else (t + (lat to lon)).takeLast(100) }
        byName[name] = AprsStation(name, lat, lon, p.symbol, rx, track, (old?.count ?: 0) + 1)
    }
}

/** Борт для интерфейса — копия, сделанная в потоке декодера. */
class PlaneRow(val icao: Int, val hex: String, val callsign: String?, val alt: Int?, val gs: Double?, val track: Double?,
               val vr: Int?, val squawk: String?, val lat: Double?, val lon: Double?, val dist: Double?, val msgs: Int,
               val ageS: Double, val onGround: Boolean, val suspect: Boolean, val trail: List<Pair<Double, Double>>)

/**
 * Декодер ADS-B на своём потоке (как поток AdsbProcessor на ПК): IQ 2,4 МГц от движка через [feed]
 * (копия блока; если поток не успевает — блок отбрасывается), борта — в [planes] после каждой порции.
 */
class AdsbWorker(fs: Int, home: Pair<Double, Double>, private val onError: (String) -> Unit = {}) {
    private val queue = ArrayBlockingQueue<Pair<FloatArray, FloatArray>>(40)
    @Volatile private var running = true
    @Volatile var planes: List<PlaneRow> = emptyList(); private set
    @Volatile var levels: AdsbLevels? = null; private set
    @Volatile var dropped = 0L; private set
    val proc = AdsbProcessor(fs, home)
    /** Частота дискретизации потока (запись IQ может задать свою). Поток декодера применит её сам. */
    @Volatile var rate = fs
    private val thread = Thread({ loop() }, "adsb").apply { isDaemon = true; start() }   // последним: поля уже заданы

    fun feed(re: FloatArray, im: FloatArray, n: Int) {
        if (!running || n <= 0) return
        if (!queue.offer(re.copyOf(n) to im.copyOf(n))) dropped++
    }

    fun setHome(lat: Double, lon: Double) = proc.setHome(lat, lon)

    fun stop() { running = false; thread.interrupt() }

    private fun loop() {
        var lastPub = 0L
        var fs = rate
        var reported = false
        var fails = 0
        while (running) {
            try {
                val b = queue.poll(250, TimeUnit.MILLISECONDS)
                if (rate != fs) { fs = rate; proc.setRate(fs) }
                if (b != null) { proc.process(b.first, b.second, b.first.size); fails = 0 }
                val now = System.nanoTime()
                if (now - lastPub > 500_000_000L) { lastPub = now; publish() }
            } catch (_: InterruptedException) {
                break
            } catch (e: Exception) {
                // сбой на одном блоке не роняет приложение; таблица бортов сохраняется. Сбои подряд — состояние
                // демодулятора испорчено: пересоздать его
                if (++fails >= 5) { fails = 0; runCatching { proc.setRate(fs) } }
                if (!reported) { reported = true; onError(tr("ADS-B: сбой декодера ({error}), блок пропущен", "error" to e.javaClass.simpleName)) }
            }
        }
    }

    private fun publish() {
        val now = AdsbTracker.nowS()
        planes = proc.snapshot().map { a ->
            PlaneRow(a.icao, a.hex, a.callsign, a.alt, a.gs, a.track ?: a.heading, a.vr, a.squawk, a.lat, a.lon, a.dist,
                a.msgs, now - a.last, a.onGround == true, a.suspect, a.trail.map { it.lat to it.lon })
        }.sortedWith(compareBy({ it.ageS > Adsb.STALE_S }, { it.dist ?: 1e9 }))
        levels = runCatching { proc.levelStats() }.getOrNull()
    }
}
