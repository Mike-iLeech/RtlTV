package io.github.mikeileech.sdr4noob.dsp

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin
import kotlin.math.sqrt

/** Виды модуляции радиорежима (подмножество настольной версии; SSB/CW/DMR — в планах). */
enum class Modulation(val title: String, val defaultBw: Int, val step: Int) {
    WFM("ЧМ вещание (WFM)", 200_000, 100_000),
    NFM("Узкая ЧМ (NFM)", 12_500, 12_500),
    AM("АМ", 10_000, 5_000),
}

/**
 * Канал на смещении [offsetHz] от центра приёмника → звук 48 кГц стерео (чередование L, R).
 * Цепочка как в rtltv/radio.py RadioDemod: перенос VFO в ноль → прореживание до 240 кГц →
 * WFM: дискриминатор → пилот 19 кГц «фазором» → L+R и L−R → 48 кГц → деэмфазис;
 * NFM/AM: второе прореживание до 48 кГц фильтром канала → дискриминатор / огибающая → звуковые фильтры.
 * Частота дискретизации [fs] должна быть кратна 240 кГц (RADIO_RATES).
 */
class RadioDemod(val fs: Int, mode: Modulation = Modulation.WFM) {
    companion object {
        const val AUDIO_RATE = 48_000
        const val CH_RATE = 240_000
        val RADIO_RATES = intArrayOf(960_000, 1_200_000, 1_440_000, 1_920_000, 2_400_000, 2_880_000)
        private const val PILOT = 19_000.0
    }

    init { require(fs % CH_RATE == 0) { "частота дискретизации $fs не кратна 240 кГц" } }

    var mode: Modulation = mode
        private set
    var bandwidth: Int = mode.defaultBw
        private set
    @Volatile var offsetHz = 0.0
    @Volatile var stereo = true
    @Volatile var squelchDb = -200f
    var deemphTau = 50e-6

    // измерения для интерфейса
    @Volatile var levelDb = -120f; private set
    @Volatile var stereoLock = false; private set
    @Volatile var pilotLevel = 0f; private set
    @Volatile var squelchOpen = true; private set

    private val mixer = Mixer(fs.toDouble())
    private val dec1 = ComplexDecimator(fs / CH_RATE, 108_000.0, fs.toDouble(), 16)
    private var r1 = FloatArray(0); private var i1 = FloatArray(0)
    private var r2 = FloatArray(0); private var i2 = FloatArray(0)
    private var mpx = FloatArray(0); private var dif = FloatArray(0)
    private var mono = FloatArray(0); private var side = FloatArray(0)
    private var prevRe = 1f; private var prevIm = 0f

    // WFM
    private lateinit var monoDec: RealDecimator
    private lateinit var diffDec: RealDecimator
    private lateinit var pilotRe: Biquad
    private lateinit var pilotIm: Biquad
    private var pilotPh = 0.0
    private var lockCnt = 0
    private var blend = 0f
    private lateinit var deL: Deemphasis
    private lateinit var deR: Deemphasis
    private val diffGain: Float = (PI * 2 * PILOT / CH_RATE).let { (2.0 * it / sin(it)).toFloat() }

    // NFM / AM
    private var dec2: ComplexDecimator? = null
    private var audioHp: BiquadChain? = null
    private var audioLp: BiquadChain? = null
    private var agcGain = 1f
    private var sqGain = 1f

    init { build() }

    fun setMode(m: Modulation, bw: Int = m.defaultBw) {
        if (m == mode) { setBandwidth(bw); return }
        mode = m; bandwidth = bw
        build()
    }

    fun setBandwidth(bw: Int) {
        bandwidth = bw
        if (mode != Modulation.WFM) dec2 = channelDecimator()
    }

    private fun channelDecimator() = ComplexDecimator(CH_RATE / AUDIO_RATE,
        min(bandwidth / 2.0, 0.45 * AUDIO_RATE), CH_RATE.toDouble(), 24)

    private fun build() {
        prevRe = 1f; prevIm = 0f
        stereoLock = false; lockCnt = 0; blend = 0f
        when (mode) {
            Modulation.WFM -> {
                // L+R и L−R проходят одинаковый ФНЧ 15 кГц с прореживанием 240 → 48 кГц: задержки совпадают
                monoDec = RealDecimator(CH_RATE / AUDIO_RATE, 14_500.0, CH_RATE.toDouble(), 48)
                diffDec = RealDecimator(CH_RATE / AUDIO_RATE, 14_500.0, CH_RATE.toDouble(), 48)
                pilotRe = Biquad.lowpass(40.0, CH_RATE.toDouble())
                pilotIm = Biquad.lowpass(40.0, CH_RATE.toDouble())
                deL = Deemphasis(deemphTau, AUDIO_RATE.toDouble())
                deR = Deemphasis(deemphTau, AUDIO_RATE.toDouble())
                dec2 = null
            }
            Modulation.NFM -> {
                dec2 = channelDecimator()
                audioHp = BiquadChain.highpass2(300.0, AUDIO_RATE.toDouble())
                audioLp = BiquadChain.lowpass4(3400.0, AUDIO_RATE.toDouble())
            }
            Modulation.AM -> {
                dec2 = channelDecimator()
                audioHp = BiquadChain.highpass2(60.0, AUDIO_RATE.toDouble())
                audioLp = BiquadChain.lowpass4(min(bandwidth / 2.0, 8000.0), AUDIO_RATE.toDouble())
                agcGain = 1f
            }
        }
    }

    private fun ensure(n: Int) {
        if (r1.size < n) {
            r1 = FloatArray(n); i1 = FloatArray(n); r2 = FloatArray(n); i2 = FloatArray(n)
            mpx = FloatArray(n); dif = FloatArray(n); mono = FloatArray(n); side = FloatArray(n)
        }
    }

    /**
     * Обработать блок IQ ([re], [im] длиной [len]; массивы портятся — переносом частоты).
     * Возвращает число стереокадров, записанных в [audio] (L, R, L, R …); [audio] ≥ 2·(len·48000/fs + 2).
     */
    fun process(re: FloatArray, im: FloatArray, len: Int, audio: FloatArray): Int {
        mixer.process(re, im, len, -offsetHz)
        ensure(len / dec1.factor + 2)
        val n1 = dec1.process(re, im, len, r1, i1)
        return when (mode) {
            Modulation.WFM -> { level(r1, i1, n1); wfm(n1, audio) }
            Modulation.NFM, Modulation.AM -> {
                val n2 = dec2!!.process(r1, i1, n1, r2, i2)
                level(r2, i2, n2)
                if (mode == Modulation.NFM) nfm(n2, audio) else am(n2, audio)
            }
        }
    }

    private fun level(re: FloatArray, im: FloatArray, n: Int) {
        if (n == 0) return
        var p = 0.0
        for (i in 0 until n) p += (re[i] * re[i] + im[i] * im[i]).toDouble()
        levelDb = (10 * log10(p / n + 1e-20)).toFloat()
    }

    private fun discriminate(re: FloatArray, im: FloatArray, n: Int, out: FloatArray, scale: Float) {
        var pr = prevRe; var pi = prevIm
        for (i in 0 until n) {
            val r = re[i]; val q = im[i]
            // arg(z[n] · conj(z[n-1]))
            out[i] = atan2(q * pr - r * pi, r * pr + q * pi) * scale
            pr = r; pi = q
        }
        prevRe = pr; prevIm = pi
    }

    private fun wfm(n: Int, audio: FloatArray): Int {
        discriminate(r1, i1, n, mpx, (CH_RATE / (2 * PI * 75_000)).toFloat())  // 1.0 = девиация 75 кГц
        // пилот переносится в ноль, узкий ФНЧ даёт его фазу без шума; sin(2·фазы) — поднесущая 38 кГц
        // гетеродин пилота — вращающийся фазор (без cos/sin на отсчёт), фаза переносится между блоками
        val w = 2 * PI * PILOT / CH_RATE
        val cw = cos(w); val sw = sin(w)
        var c = cos(pilotPh); var s = sin(pilotPh)
        var magSum = 0.0; var ur = 0.0; var ui = 0.0
        for (i in 0 until n) {
            val zr = pilotRe.step(mpx[i] * c)
            val zi = pilotIm.step(-mpx[i] * s)
            val mag = sqrt(zr * zr + zi * zi) + 1e-12
            val u0 = zr / mag; val u1 = zi / mag
            magSum += mag; ur += u0; ui += u1
            // фазор пилота: exp(j(ωn + ψ)) = lo·u
            val pr = c * u0 - s * u1
            val pq = c * u1 + s * u0
            val s38 = -(2 * pr * pq)            // −Im(фазор²)
            dif[i] = (mpx[i] * s38 * diffGain).toFloat()
            val nc = c * cw - s * sw
            s = c * sw + s * cw
            c = nc
        }
        pilotPh = (pilotPh + w * n) % (2 * PI)
        if (n > 0) {
            pilotLevel = (2 * magSum / n).toFloat()
            val coh = sqrt(ur * ur + ui * ui) / n
            val good = pilotLevel > 0.025f && coh > 0.8
            // гистерезис: 2 хороших блока подряд — стерео, 3 плохих — моно
            lockCnt = if (good) min(max(lockCnt, 0) + 1, 3) else max(min(lockCnt, 0) - 1, -3)
            if (lockCnt >= 2) stereoLock = true else if (lockCnt <= -3) stereoLock = false
        }
        val m = monoDec.process(mpx, n, mono)
        val d = diffDec.process(dif, n, side)
        val k = min(m, d)
        val want = if (stereo && stereoLock) 1f else 0f
        for (i in 0 until k) {
            val b = blend + (want - blend) * (i + 1) / k   // моно ↔ стерео плавно, без щелчка
            side[i] *= b
        }
        blend = want
        for (i in 0 until k) {
            val a = mono[i]; val s = side[i]
            mono[i] = 0.5f * (a + s)
            side[i] = 0.5f * (a - s)
        }
        deL.process(mono, k); deR.process(side, k)
        squelch(k)
        for (i in 0 until k) {
            audio[2 * i] = mono[i] * 1.4f * sqGain
            audio[2 * i + 1] = side[i] * 1.4f * sqGain
        }
        return k
    }

    private fun nfm(n: Int, audio: FloatArray): Int {
        discriminate(r2, i2, n, mono, (AUDIO_RATE / (2 * PI) / 3000.0).toFloat())
        audioHp!!.process(mono, n); audioLp!!.process(mono, n)
        squelch(n)
        for (i in 0 until n) { val v = mono[i] * 0.7f * sqGain; audio[2 * i] = v; audio[2 * i + 1] = v }
        return n
    }

    private fun am(n: Int, audio: FloatArray): Int {
        for (i in 0 until n) mono[i] = sqrt(r2[i] * r2[i] + i2[i] * i2[i])
        audioHp!!.process(mono, n); audioLp!!.process(mono, n)
        // простая АРУ: огибающая пика с медленным спадом, цель 0,3 полной шкалы
        var g = agcGain
        for (i in 0 until n) {
            val v = mono[i] * g
            val a = abs(v)
            g = if (a > 0.3f) g * 0.999f else min(g * 1.00005f, 1e5f)
            mono[i] = v
        }
        agcGain = g
        squelch(n)
        for (i in 0 until n) { val v = mono[i] * sqGain; audio[2 * i] = v; audio[2 * i + 1] = v }
        return n
    }

    /** Шумодав по уровню канала: плавное открытие/закрытие за блок. */
    private fun squelch(n: Int) {
        val open = levelDb >= squelchDb
        squelchOpen = open
        val target = if (open) 1f else 0f
        if (n == 0) return
        // для простоты — один коэффициент на блок (блок ~50 мс); переход сглаживает следующий блок
        sqGain += (target - sqGain) * 0.6f
        if (abs(sqGain - target) < 0.01f) sqGain = target
    }
}
