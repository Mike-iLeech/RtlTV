package io.github.mikeileech.sdr4noob.lrpt

import kotlin.math.sqrt

/*
 * Перенос tools/lrpt_dsp.c на Kotlin — те же формулы и те же типы (float там, где float в C, double — где
 * double), чтобы результат совпадал с библиотекой C. На устройстве по умолчанию работает сама библиотека C
 * (src/main/cpp/lrpt_dsp.c через lrpt_jni.c), этот код — для JVM-тестов и запасной путь без нативной части.
 */

/** Демодулятор QPSK/OQPSK/BPSK: АРУ, Костас, Гарднер с кубической интерполяцией, мягкие символы int8. */
interface DemodCore {
    fun setCostas(bw: Double)
    fun setFreq(freqRad: Double)

    /** out — 6 значений: [freq рад/отсчёт, phase, interval/half, agc, amp, lock] */
    fun state(out: DoubleArray)

    /** iq — n комплексных отсчётов (re, im). Возвращает число символов (≤ maxSym). */
    fun process(iq: FloatArray, n: Int, soft: ByteArray, cons: FloatArray?, maxSym: Int): Int
    fun close() {}
}

/** sin/cos из lrpt_dsp.c (приведение к [-π, π] и ряд до x^13) — библиотека C пользуется ими, а не libm. */
internal object CMath {
    private const val PI = 3.141592653589793
    private const val TP = 6.283185307179586

    fun sin(x0: Double): Double {
        var x = x0
        if (x > PI || x < -PI) x -= TP * ((x + (if (x > 0) PI else -PI)) / TP).toLong().toDouble()
        if (x > PI / 2) x = PI - x else if (x < -PI / 2) x = -PI - x
        val x2 = x * x
        return x * (1.0 - x2 / 6.0 * (1.0 - x2 / 20.0 * (1.0 - x2 / 42.0 * (1.0 - x2 / 72.0 *
            (1.0 - x2 / 110.0 * (1.0 - x2 / 156.0))))))
    }

    fun cos(x: Double): Double = sin(x + 1.5707963267948966)
}

private fun loopGains(bw: Double): Pair<Double, Double> {
    val zeta = 0.7071
    val theta = bw / (zeta + 0.25 / zeta)
    val d = 1.0 + 2.0 * zeta * theta + theta * theta
    return Pair(4.0 * zeta * theta / d, 4.0 * theta * theta / d)
}

/** demod_t + demod_process из lrpt_dsp.c. mode: 0 — QPSK, 1 — OQPSK, 2 — BPSK. */
class KotlinDemod(sps: Double, private val mode: Int, costasBw: Double, timingBw: Double, fmaxRad: Double) : DemodCore {
    private val sps = sps
    private val half = sps / 2.0
    private var interval = half
    private var tfreq = 0.0
    private var tNext = 0.0
    private var ta = 0.0
    private var tb = 0.0
    private var ca = 0.0
    private var cb = 0.0
    private var phase = 0.0
    private var freq = 0.0
    private val fmax = fmaxRad
    private var agc = 1.0
    private val agcRate = 1e-3
    private val rr = FloatArray(4)
    private val ri = FloatArray(4)
    private var nfill = 0
    private val hr = FloatArray(3)
    private val hi = FloatArray(3)
    private var hidx = 0
    private var pendI = 0f
    private var amp = 1.0
    private var lock = 0.0

    init {
        setCostas(costasBw)
        loopGains(timingBw).let { ta = it.first; tb = it.second }
    }

    override fun setCostas(bw: Double) {
        loopGains(bw).let { ca = it.first; cb = it.second }
    }

    override fun setFreq(freqRad: Double) { freq = freqRad }

    override fun state(out: DoubleArray) {
        out[0] = freq; out[1] = phase; out[2] = interval / half; out[3] = agc; out[4] = amp; out[5] = lock
    }

    private fun cubic(y: FloatArray, mu: Float): Float {
        val c0 = y[1]
        val c1 = -y[0] / 3.0f - y[1] / 2.0f + y[2] - y[3] / 6.0f
        val c2 = (y[0] + y[2]) / 2.0f - y[1]
        val c3 = (y[3] - y[0]) / 6.0f + (y[1] - y[2]) / 2.0f
        return ((c3 * mu + c2) * mu + c1) * mu + c0
    }

    private fun costas(i: Float, q: Float) {
        val sI = if (i >= 0) 1.0f else -1.0f
        val sQ = if (q >= 0) 1.0f else -1.0f
        val n = if (amp > 1e-9) amp else 1.0
        var e = if (mode == 2) (sI * q) / n else (sI * q - sQ * i) / n
        if (e > 1.0) e = 1.0
        if (e < -1.0) e = -1.0
        freq += cb * e / sps
        if (freq > fmax) freq = fmax
        if (freq < -fmax) freq = -fmax
        phase += ca * e
        val p = i.toDouble() * i + q.toDouble() * q
        val m = if (mode == 2) {
            if (p > 1e-12) (i.toDouble() * i - q.toDouble() * q) / p else 0.0
        } else {
            if (p > 1e-12) (2.0 * Math.abs(i.toDouble()) * Math.abs(q.toDouble()) / p - 0.64) / 0.36 else 0.0
        }
        lock += 0.002 * (m - lock)
        amp += if (mode == 2) 0.002 * (Math.abs(i) - amp) else 0.002 * ((Math.abs(i) + Math.abs(q)) * 0.5 - amp)
    }

    private fun timing(e0: Double) {
        val n = if (amp > 1e-9) amp * amp else 1.0
        var e = e0 / n
        if (e > 1.0) e = 1.0
        if (e < -1.0) e = -1.0
        tfreq += tb * e
        if (tfreq > 0.01 * half) tfreq = 0.01 * half
        if (tfreq < -0.01 * half) tfreq = -0.01 * half
        interval = half - tfreq * half
        tNext -= ta * e * half
    }

    private fun clip8(v: Float): Byte = when {
        v > 127.0f -> 127
        v < -127.0f -> -127
        else -> (if (v >= 0) v + 0.5f else v - 0.5f).toLong().toInt().toByte()
    }

    override fun process(iq: FloatArray, n: Int, soft: ByteArray, cons: FloatArray?, maxSym: Int): Int {
        var nsym = 0
        for (k in 0 until n) {
            val xr = iq[2 * k] * agc.toFloat()
            val xi = iq[2 * k + 1] * agc.toFloat()
            val mag = sqrt(xr * xr + xi * xi)
            agc += agcRate * (1.0 - mag) * agc
            if (agc < 1e-6) agc = 1e-6
            if (agc > 1e6) agc = 1e6
            val c = CMath.cos(phase).toFloat()
            val s = CMath.sin(phase).toFloat()
            val yr = xr * c + xi * s
            val yi = xi * c - xr * s
            phase += freq
            if (phase > TWO_PI) phase -= TWO_PI else if (phase < -TWO_PI) phase += TWO_PI
            rr[0] = rr[1]; rr[1] = rr[2]; rr[2] = rr[3]; rr[3] = yr
            ri[0] = ri[1]; ri[1] = ri[2]; ri[2] = ri[3]; ri[3] = yi
            if (nfill < 4) { nfill++; continue }
            tNext -= 1.0
            while (tNext < 1.0) {
                var mu = tNext.toFloat()
                if (mu < 0.0f) mu = 0.0f
                val h = cubic(rr, mu)
                val g = cubic(ri, mu)
                hr[0] = hr[1]; hr[1] = hr[2]; hr[2] = h
                hi[0] = hi[1]; hi[1] = hi[2]; hi[2] = g
                hidx = hidx xor 1
                tNext += interval
                when (mode) {
                    2 -> if (hidx == 0) {
                        val e: Double = (hr[1] * (hr[2] - hr[0])).toDouble()
                        timing(e)
                        costas(h, g)
                        if (nsym < maxSym) {
                            val kk = (48.0 / (if (amp > 1e-9) amp else 1.0)).toFloat()
                            soft[nsym] = clip8(h * kk)
                            if (cons != null) {
                                cons[2 * nsym] = h / (amp + 1e-9).toFloat()
                                cons[2 * nsym + 1] = g / (amp + 1e-9).toFloat()
                            }
                            nsym++
                        }
                    }
                    0 -> if (hidx == 0) {
                        val e: Double = (hr[1] * (hr[2] - hr[0]) + hi[1] * (hi[2] - hi[0])).toDouble()
                        timing(e)
                        costas(h, g)
                        if (nsym < maxSym) {
                            val kk = (48.0 / (if (amp > 1e-9) amp else 1.0)).toFloat()
                            soft[2 * nsym] = clip8(h * kk)
                            soft[2 * nsym + 1] = clip8(g * kk)
                            if (cons != null) {
                                cons[2 * nsym] = h / (amp * 1.4142 + 1e-9).toFloat()
                                cons[2 * nsym + 1] = g / (amp * 1.4142 + 1e-9).toFloat()
                            }
                            nsym++
                        }
                    }
                    else -> if (hidx == 0) {
                        val e: Double = (hr[1] * (hr[2] - hr[0])).toDouble()
                        timing(2.0 * e)
                        pendI = h
                    } else {
                        val e: Double = (hi[1] * (hi[2] - hi[0])).toDouble()
                        timing(2.0 * e)
                        val i = pendI
                        val q = g
                        costas(i, q)
                        if (nsym < maxSym) {
                            val kk = (48.0 / (if (amp > 1e-9) amp else 1.0)).toFloat()
                            soft[2 * nsym] = clip8(i * kk)
                            soft[2 * nsym + 1] = clip8(q * kk)
                            if (cons != null) {
                                cons[2 * nsym] = i / (amp * 1.4142 + 1e-9).toFloat()
                                cons[2 * nsym + 1] = q / (amp * 1.4142 + 1e-9).toFloat()
                            }
                            nsym++
                        }
                    }
                }
            }
        }
        return nsym
    }

    private companion object {
        const val TWO_PI = 6.283185307179586
    }
}

/** Свёрточный код k=7 r=1/2, Витерби и Рид — Соломон (255,223) CCSDS из lrpt_dsp.c. */
object KotlinCodec {
    private const val POLYA = 0x4F
    private const val POLYB = 0x6D

    private fun parity(v0: Int): Int {
        var v = v0
        v = v xor (v ushr 4)
        v = v and 0xF
        return (0x6996 ushr v) and 1
    }

    /** bits: n бит (0/1) → out: 2n бит; state — 6 бит истории. Возвращает новое состояние. */
    fun convEncode(bits: ByteArray, n: Int, out: ByteArray, state: Int): Int {
        var reg = state and 0x3F
        for (i in 0 until n) {
            reg = ((reg shl 1) or (bits[i].toInt() and 1)) and 0x7F
            out[2 * i] = parity(reg and POLYA).toByte()
            out[2 * i + 1] = parity(reg and POLYB).toByte()
        }
        return reg and 0x3F
    }

    // для каждого нового состояния ns: знаки ожидаемых бит при переходе из p0 = ns>>1 и p1 = (ns>>1)|32
    private val sA0 = IntArray(64)
    private val sB0 = IntArray(64)
    private val sA1 = IntArray(64)
    private val sB1 = IntArray(64)

    init {
        for (ns in 0 until 64) {
            val bit = ns and 1
            val p0 = ns shr 1
            val p1 = (ns shr 1) or 32
            val r0 = ((p0 shl 1) or bit) and 0x7F
            val r1 = ((p1 shl 1) or bit) and 0x7F
            sA0[ns] = if (parity(r0 and POLYA) != 0) 1 else -1
            sB0[ns] = if (parity(r0 and POLYB) != 0) 1 else -1
            sA1[ns] = if (parity(r1 and POLYA) != 0) 1 else -1
            sB1[ns] = if (parity(r1 and POLYB) != 0) 1 else -1
        }
    }

    /** Мягкий Витерби (viterbi27): soft — 2·npairs int8 (>0 = 1), out — npairs бит. Возвращает метрику ×100. */
    fun viterbi27(soft: ByteArray, npairs: Int, out: ByteArray): Int {
        if (npairs <= 0) return 0
        val dec = LongArray(npairs)
        var cur = IntArray(64)
        var nxt = IntArray(64)
        var total = 0L
        val a0 = sA0; val b0 = sB0; val a1 = sA1; val b1 = sB1
        for (t in 0 until npairs) {
            val a = soft[2 * t].toInt()
            val b = soft[2 * t + 1].toInt()
            var dv = 0L
            var mx = Int.MIN_VALUE
            for (ns in 0 until 64) {
                val p0 = ns shr 1
                val v0 = cur[p0] + a0[ns] * a + b0[ns] * b
                val v1 = cur[p0 or 32] + a1[ns] * a + b1[ns] * b
                val v: Int
                if (v1 > v0) {
                    v = v1
                    dv = dv or (1L shl ns)
                } else {
                    v = v0
                }
                nxt[ns] = v
                if (v > mx) mx = v
            }
            dec[t] = dv
            if (mx > (1 shl 28) || mx < -(1 shl 28) || (t and 255) == 255) {
                for (s in 0 until 64) nxt[s] -= mx
                total += mx
            }
            val tmp = cur; cur = nxt; nxt = tmp
        }
        var best = 0
        for (s in 1 until 64) if (cur[s] > cur[best]) best = s
        total += cur[best]
        var st = best
        for (t in npairs - 1 downTo 0) {
            out[t] = (st and 1).toByte()
            val k = ((dec[t] ushr st) and 1L).toInt()
            st = (st shr 1) or (k shl 5)
        }
        return (total * 100 / npairs).toInt()
    }

    // ---------------------------------------------------------------- Рид — Соломон
    private const val NN = 255
    private const val NROOTS = 32
    private const val FCR = 112
    private const val PRIM = 11
    private const val IPRIM = 116
    private const val A0 = NN

    private val alphaTo = IntArray(256)
    private val indexOf = IntArray(256)
    private val genpoly = IntArray(NROOTS + 1)
    private val taltab = IntArray(256)
    private val tal1tab = IntArray(256)

    private fun modnn(x0: Int): Int {
        var x = x0
        while (x >= NN) {
            x -= NN
            x = (x shr 8) + (x and NN)
        }
        return x
    }

    init {
        var sr = 1
        indexOf[0] = A0
        alphaTo[A0] = 0
        for (i in 0 until NN) {
            indexOf[sr] = i
            alphaTo[i] = sr
            sr = sr shl 1
            if (sr and 256 != 0) sr = sr xor 0x187
            sr = sr and 255
        }
        genpoly[0] = 1
        var root = FCR * PRIM
        for (i in 0 until NROOTS) {
            genpoly[i + 1] = 1
            for (j in i downTo 1) {
                genpoly[j] = if (genpoly[j] != 0) genpoly[j - 1] xor alphaTo[modnn(indexOf[genpoly[j]] + root)]
                else genpoly[j - 1]
            }
            genpoly[0] = alphaTo[modnn(indexOf[genpoly[0]] + root)]
            root += PRIM
        }
        for (i in 0..NROOTS) genpoly[i] = indexOf[genpoly[i]]
        val tal = intArrayOf(0x8d, 0xef, 0xec, 0x86, 0xfa, 0x99, 0xaf, 0x7b)
        for (i in 0 until 256) {
            var v = 0
            for (j in 0 until 8) if (i and (1 shl j) != 0) v = v xor tal[7 - j]
            taltab[i] = v
            tal1tab[v] = i
        }
    }

    /** 223 байта → 32 проверочных. */
    fun rsEncode(data: ByteArray, parityOut: ByteArray, dual: Boolean) {
        val bb = IntArray(NROOTS)
        for (i in 0 until NN - NROOTS) {
            val d = data[i].toInt() and 0xFF
            val dch = if (dual) tal1tab[d] else d
            val feedback = indexOf[dch xor bb[0]]
            if (feedback != A0) {
                for (j in 1 until NROOTS) bb[j] = bb[j] xor alphaTo[modnn(feedback + genpoly[NROOTS - j])]
            }
            System.arraycopy(bb, 1, bb, 0, NROOTS - 1)
            bb[NROOTS - 1] = if (feedback != A0) alphaTo[modnn(feedback + genpoly[0])] else 0
        }
        for (i in 0 until NROOTS) parityOut[i] = (if (dual) taltab[bb[i]] else bb[i]).toByte()
    }

    /** Кодовое слово 255 байт исправляется на месте. Возвращает число исправленных байт или -1. */
    fun rsDecode(data: ByteArray, dual: Boolean): Int {
        val buf = IntArray(NN) { val v = data[it].toInt() and 0xFF; if (dual) tal1tab[v] else v }
        val lambda = IntArray(NROOTS + 1)
        val s = IntArray(NROOTS)
        val b = IntArray(NROOTS + 1)
        val t = IntArray(NROOTS + 1)
        val omega = IntArray(NROOTS + 1)
        val root = IntArray(NROOTS)
        val reg = IntArray(NROOTS + 1)
        val loc = IntArray(NROOTS)

        for (i in 0 until NROOTS) s[i] = buf[0]
        for (j in 1 until NN) {
            for (i in 0 until NROOTS) {
                s[i] = if (s[i] == 0) buf[j] else buf[j] xor alphaTo[modnn(indexOf[s[i]] + (FCR + i) * PRIM)]
            }
        }
        var synError = 0
        for (i in 0 until NROOTS) {
            synError = synError or s[i]
            s[i] = indexOf[s[i]]
        }
        if (synError == 0) return 0

        lambda[0] = 1
        for (i in 0 until NROOTS + 1) b[i] = indexOf[lambda[i]]
        var r = 0
        var el = 0
        while (++r <= NROOTS) {
            var discr = 0
            for (i in 0 until r) {
                if (lambda[i] != 0 && s[r - i - 1] != A0) discr = discr xor alphaTo[modnn(indexOf[lambda[i]] + s[r - i - 1])]
            }
            discr = indexOf[discr]
            if (discr == A0) {
                System.arraycopy(b, 0, b, 1, NROOTS)
                b[0] = A0
            } else {
                t[0] = lambda[0]
                for (i in 0 until NROOTS) {
                    t[i + 1] = if (b[i] != A0) lambda[i + 1] xor alphaTo[modnn(discr + b[i])] else lambda[i + 1]
                }
                if (2 * el <= r - 1) {
                    el = r - el
                    for (i in 0..NROOTS) b[i] = if (lambda[i] == 0) A0 else modnn(indexOf[lambda[i]] - discr + NN)
                } else {
                    System.arraycopy(b, 0, b, 1, NROOTS)
                    b[0] = A0
                }
                System.arraycopy(t, 0, lambda, 0, NROOTS + 1)
            }
        }
        var degLambda = 0
        for (i in 0 until NROOTS + 1) {
            lambda[i] = indexOf[lambda[i]]
            if (lambda[i] != A0) degLambda = i
        }
        System.arraycopy(lambda, 1, reg, 1, NROOTS)
        var count = 0
        var k = IPRIM - 1
        for (i in 1..NN) {
            var q = 1
            for (j in degLambda downTo 1) {
                if (reg[j] != A0) {
                    reg[j] = modnn(reg[j] + j)
                    q = q xor alphaTo[reg[j]]
                }
            }
            if (q == 0) {
                root[count] = i
                loc[count] = k
                if (++count == degLambda) break
            }
            k = modnn(k + IPRIM)
        }
        if (degLambda != count) return -1
        val degOmega = degLambda - 1
        for (i in 0..degOmega) {
            var tmp = 0
            for (j in i downTo 0) {
                if (s[i - j] != A0 && lambda[j] != A0) tmp = tmp xor alphaTo[modnn(s[i - j] + lambda[j])]
            }
            omega[i] = indexOf[tmp]
        }
        for (j in count - 1 downTo 0) {
            var num1 = 0
            for (i in degOmega downTo 0) {
                if (omega[i] != A0) num1 = num1 xor alphaTo[modnn(omega[i] + i * root[j])]
            }
            val num2 = alphaTo[modnn(root[j] * (FCR - 1) + NN)]
            var den = 0
            var i = (if (degLambda < NROOTS - 1) degLambda else NROOTS - 1) and 1.inv()
            while (i >= 0) {
                if (lambda[i + 1] != A0) den = den xor alphaTo[modnn(lambda[i + 1] + i * root[j])]
                i -= 2
            }
            if (den == 0) return -1
            if (num1 != 0) {
                val u = alphaTo[modnn(indexOf[num1] + indexOf[num2] + NN - indexOf[den])]
                buf[loc[j]] = buf[loc[j]] xor u
            }
        }
        for (i in 0 until NN) data[i] = (if (dual) taltab[buf[i]] else buf[i]).toByte()
        return count
    }
}
