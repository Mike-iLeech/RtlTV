package io.github.mikeileech.sdr4noob.dsp

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * fsIn → fsOut рациональным полифазным КИХ без швов между блоками — как StreamResampler в rtltv/devices.py:
 * L/M = fsOut/fsIn (несократимая дробь), фильтр firwin(K·L, 0,9/max(L, M), окно Кайзера β = 8) · L.
 * Нужен для приёмников со своими частотами дискретизации (HackRF ≥ 2 МГц, Airspy 2,5/3/6/10 МГц), когда
 * радиорежиму нужна частота, кратная 240 кГц.
 */
class StreamResampler(fsIn: Int, fsOut: Int, private val k: Int = 24) {
    val up: Int
    val down: Int
    private val h: FloatArray

    init {
        val g = gcd(fsOut.toLong(), fsIn.toLong())
        up = (fsOut / g).toInt(); down = (fsIn / g).toInt()
        require(up <= 1000 && down <= 1000) { "слишком сложное отношение частот $fsOut/$fsIn" }   // noqa: i18n — проверка программиста
        val n = k * up
        val fc = 0.9 / maxOf(up, down) / 2          // в долях частоты дискретизации (после повышения)
        val m = (n - 1) / 2.0
        val beta = 8.0
        val i0b = besselI0(beta)
        val hh = DoubleArray(n) { i ->
            val t = i - m
            val sinc = if (abs(t) < 1e-12) 2 * fc else sin(2 * PI * fc * t) / (PI * t)
            val r = 2.0 * i / (n - 1) - 1.0
            sinc * besselI0(beta * sqrt(maxOf(0.0, 1 - r * r))) / i0b
        }
        val s = hh.sum()
        h = FloatArray(n) { (hh[it] / s * up).toFloat() }
    }

    // история входа: абсолютные индексы [bufStart, bufStart + bufLen)
    private var bufR = FloatArray(4096); private var bufI = FloatArray(4096)
    private var bufStart = -(k.toLong())            // начальные нули — как задержка фильтра
    private var bufLen = k
    private var next = 0L                           // номер следующего выходного отсчёта

    /** Сколько выходных отсчётов даст [n] входных (примерно, ±1). */
    fun outFor(n: Int) = (n.toLong() * up / down).toInt()

    /** Обработать [n] входных отсчётов; выход — в [outR]/[outI] (размер ≥ outFor(n) + 2). Возвращает число выходных. */
    fun process(inR: FloatArray, inI: FloatArray, n: Int, outR: FloatArray, outI: FloatArray): Int {
        if (bufLen + n > bufR.size) { val c = maxOf(bufR.size * 2, bufLen + n); bufR = bufR.copyOf(c); bufI = bufI.copyOf(c) }
        System.arraycopy(inR, 0, bufR, bufLen, n); System.arraycopy(inI, 0, bufI, bufLen, n)
        bufLen += n
        val end = bufStart + bufLen                  // первый ещё не пришедший индекс
        var cnt = 0
        while (true) {
            val nu = next * down                     // время выхода в повышенной частоте
            val i0 = nu / up                         // самый новый вход, нужный для этого выхода
            if (i0 >= end) break
            val ph = (nu % up).toInt()
            var sr = 0f; var si = 0f
            var j = 0
            while (j < k) {
                val idx = (i0 - j - bufStart).toInt()
                if (idx >= 0) { val c = h[ph + j * up]; sr += c * bufR[idx]; si += c * bufI[idx] }
                j++
            }
            outR[cnt] = sr; outI[cnt] = si
            cnt++
            next++
        }
        // старше (самый старый нужный следующему выходу) — не нужно
        val keepFrom = (next * down / up) - k + 1
        val drop = (keepFrom - bufStart).toInt().coerceIn(0, bufLen)
        if (drop > 0) {
            System.arraycopy(bufR, drop, bufR, 0, bufLen - drop); System.arraycopy(bufI, drop, bufI, 0, bufLen - drop)
            bufLen -= drop; bufStart += drop
        }
        return cnt
    }

    companion object {
        fun gcd(a: Long, b: Long): Long = if (b == 0L) a else gcd(b, a % b)

        /** Модифицированная функция Бесселя I0 (ряд) — для окна Кайзера. */
        fun besselI0(x: Double): Double {
            var s = 1.0; var t = 1.0; var k = 1
            val q = x * x / 4
            while (t > 1e-12 * s) { t *= q / (k.toDouble() * k); s += t; k++ }
            return s
        }
    }
}
