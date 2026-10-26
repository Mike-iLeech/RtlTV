package io.github.mikeileech.sdr4noob.dsp

import io.github.mikeileech.sdr4noob.i18n.N_
import io.github.mikeileech.sdr4noob.i18n.tr
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Виды модуляции радиорежима — как MODES в rtltv/radio.py: полоса по умолчанию, допустимый диапазон полосы
 * и шаг настройки, Гц.
 */
enum class Modulation(val title: String, val defaultBw: Int, val minBw: Int, val maxBw: Int, val step: Int) {
    WFM(N_("ЧМ вещание (WFM)"), 200_000, 50_000, 220_000, 100_000),
    NFM(N_("Узкая ЧМ (NFM)"), 12_500, 5_000, 25_000, 12_500),
    AM(N_("АМ"), 10_000, 3_000, 20_000, 5_000),
    USB(N_("Верхняя боковая (USB)"), 2_800, 1_500, 4_000, 100),
    LSB(N_("Нижняя боковая (LSB)"), 2_800, 1_500, 4_000, 100),
    CW(N_("Телеграф (CW)"), 500, 100, 1_500, 10),
    DMR(N_("Цифровая рация DMR"), 12_500, 12_500, 12_500, 12_500),
}

/**
 * Канал на смещении [offsetHz] от центра приёмника → звук 48 кГц стерео (чередование L, R).
 * Цепочка как в rtltv/radio.py RadioDemod: перенос VFO в ноль → прореживание до 240 кГц →
 * WFM: дискриминатор → пилот 19 кГц «фазором» → L+R и L−R → 48 кГц → деэмфазис; RDS с поднесущей 57 кГц;
 * NFM/AM/SSB/CW: второе прореживание до 48 кГц → дискриминатор / огибающая / сдвиг боковой полосы → АРУ.
 * Частота дискретизации [fs] должна быть кратна 240 кГц (RADIO_RATES).
 */
class RadioDemod(val fs: Int, mode: Modulation = Modulation.WFM) {
    companion object {
        const val AUDIO_RATE = 48_000
        const val CH_RATE = 240_000
        val RADIO_RATES = intArrayOf(960_000, 1_200_000, 1_440_000, 1_920_000, 2_400_000, 2_880_000)
        private const val PILOT = 19_000.0
        const val SSB_LOW = 100.0     // нижний край звуковой полосы SSB, Гц (от несущей)
    }

    init { require(fs % CH_RATE == 0) { tr("частота дискретизации {fs} не кратна 240 кГц", "fs" to fs) } }

    var mode: Modulation = mode
        private set
    var bandwidth: Int = mode.defaultBw
        private set
    @Volatile var offsetHz = 0.0
    @Volatile var stereo = true
    @Volatile var squelchDb = -200f
    @Volatile var rdsOn = true
    var cwTone = 700.0
    var deemphTau = 50e-6

    // измерения для интерфейса
    @Volatile var levelDb = -120f; private set
    @Volatile var stereoLock = false; private set
    @Volatile var pilotLevel = 0f; private set
    @Volatile var squelchOpen = true; private set
    val rds = RdsDecoder(CH_RATE)
    val agc = Agc(AUDIO_RATE)
    /** Декодер DMR (создаётся при переходе в режим DMR); события — в [dmrEvents] для интерфейса. */
    var dmr: DmrDecoder? = null; private set
    val dmrEvents = java.util.concurrent.ConcurrentLinkedQueue<DmrEvent>()
    /** Синтезатор голоса DMR (mbelib); в юнит-тестах можно подменить. */
    var ambeFactory: () -> AmbeSynth = { MbeSynth() }

    private val mixer = Mixer(fs.toDouble())
    private val dec1 = ComplexDecimator(fs / CH_RATE, 108_000.0, fs.toDouble(), 16)
    private var r1 = FloatArray(0); private var i1 = FloatArray(0)
    private var r2 = FloatArray(0); private var i2 = FloatArray(0)
    private var mpx = FloatArray(0); private var dif = FloatArray(0)
    private var mono = FloatArray(0); private var side = FloatArray(0); private var agcOut = FloatArray(0)
    private var c57r = FloatArray(0); private var c57i = FloatArray(0)
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

    // узкополосные режимы
    private var dec2: ComplexDecimator? = null
    private var audioHp: BiquadChain? = null
    private var audioLp: BiquadChain? = null
    private var ssbLp: ComplexFir? = null
    private val ssbMix1 = Mixer(AUDIO_RATE.toDouble())
    private val ssbMix2 = Mixer(AUDIO_RATE.toDouble())
    private var ssbCenter = 0.0
    private var sqGain = 1f

    init { build() }

    fun setMode(m: Modulation, bw: Int = m.defaultBw) {
        if (m == mode) { setBandwidth(bw); return }
        mode = m; bandwidth = bw.coerceIn(m.minBw, m.maxBw)
        build()
    }

    /** Полоса канала в пределах режима (как set_bandwidth в настольной версии). */
    fun setBandwidth(bw: Int) {
        bandwidth = bw.coerceIn(mode.minBw, mode.maxBw)
        buildBw()
    }

    /** Новая станция: сбросить RDS и захват стерео. */
    fun resetStation() {
        rds.reset()
        stereoLock = false; lockCnt = 0
    }

    private val ssb get() = mode == Modulation.USB || mode == Modulation.LSB || mode == Modulation.CW

    private fun build() {
        prevRe = 1f; prevIm = 0f
        stereoLock = false; lockCnt = 0; blend = 0f
        agc.reset()
        dec2 = null; audioHp = null; audioLp = null; ssbLp = null
        when (mode) {
            Modulation.WFM -> {
                // L+R и L−R проходят одинаковый ФНЧ 15 кГц с прореживанием 240 → 48 кГц: задержки совпадают
                monoDec = RealDecimator(CH_RATE / AUDIO_RATE, 14_500.0, CH_RATE.toDouble(), 48)
                diffDec = RealDecimator(CH_RATE / AUDIO_RATE, 14_500.0, CH_RATE.toDouble(), 48)
                pilotRe = Biquad.lowpass(40.0, CH_RATE.toDouble())
                pilotIm = Biquad.lowpass(40.0, CH_RATE.toDouble())
                deL = Deemphasis(deemphTau, AUDIO_RATE.toDouble())
                deR = Deemphasis(deemphTau, AUDIO_RATE.toDouble())
                rds.reset()
            }
            Modulation.NFM -> {
                audioHp = BiquadChain.highpass2(300.0, AUDIO_RATE.toDouble())
                audioLp = BiquadChain.lowpass4(3400.0, AUDIO_RATE.toDouble())
            }
            Modulation.AM -> audioHp = BiquadChain.highpass2(60.0, AUDIO_RATE.toDouble())
            Modulation.DMR -> if (dmr == null) dmr = DmrDecoder(ambeFactory)
            else -> {}
        }
        buildBw()
    }

    private fun buildBw() {
        if (mode == Modulation.WFM) return
        // канал: прореживание 240 → 48 кГц; у SSB/CW полоса задаётся комплексным фильтром ниже
        val cut = if (ssb) 0.95 * 6000 else min(bandwidth / 2.0, 0.45 * AUDIO_RATE)
        dec2 = ComplexDecimator(CH_RATE / AUDIO_RATE, cut, CH_RATE.toDouble(), 24)
        val half = bandwidth / 2.0
        when (mode) {
            Modulation.AM -> audioLp = BiquadChain.lowpass4(min(half, 8000.0), AUDIO_RATE.toDouble())
            Modulation.USB, Modulation.LSB, Modulation.CW -> {
                // SSB: середина звуковой полосы сдвигается в ноль, симметричный ФНЧ, сдвиг обратно, вещественная
                // часть. CW: узкий ФНЧ вокруг несущей и перенос на тон cwTone.
                ssbCenter = when (mode) {
                    Modulation.CW -> 0.0
                    Modulation.USB -> SSB_LOW + half
                    else -> -(SSB_LOW + half)
                }
                val h = Firwin.lowpass(383, half, AUDIO_RATE.toDouble())
                ssbLp?.setTaps(h) ?: run { ssbLp = ComplexFir(h) }
            }
            else -> {}
        }
    }

    private fun ensure(n: Int) {
        if (r1.size < n) {
            r1 = FloatArray(n); i1 = FloatArray(n); r2 = FloatArray(n); i2 = FloatArray(n)
            mpx = FloatArray(n); dif = FloatArray(n)
            // выход АРУ длиннее входа на неполный кусок из прошлого блока
            mono = FloatArray(n + 2 * Agc.CH); side = FloatArray(n + 2 * Agc.CH)
            c57r = FloatArray(n); c57i = FloatArray(n); agcOut = FloatArray(n + Agc.CH * 2)
        }
    }

    /**
     * Обработать блок IQ ([re], [im] длиной [len]; массивы портятся — переносом частоты).
     * Возвращает число стереокадров, записанных в [audio] (L, R, L, R …); [audio] ≥ 2·(len·48000/fs + 64).
     */
    fun process(re: FloatArray, im: FloatArray, len: Int, audio: FloatArray): Int {
        mixer.process(re, im, len, -offsetHz)
        ensure(len / dec1.factor + 2)
        val n1 = dec1.process(re, im, len, r1, i1)
        if (mode == Modulation.WFM) { level(r1, i1, n1); return wfm(n1, audio) }
        val n2 = dec2!!.process(r1, i1, n1, r2, i2)
        level(r2, i2, n2)
        val k = when (mode) {
            Modulation.NFM -> nfm(n2)
            Modulation.AM -> am(n2)
            Modulation.DMR -> { dmrDemod(n2); n2 }
            else -> ssbDemod(n2)
        }
        if (mode == Modulation.DMR) {   // у DMR свой «шумодав» — звук только при декодированном голосе
            for (i in 0 until k) { val v = mono[i]; audio[2 * i] = v; audio[2 * i + 1] = v }
            return k
        }
        squelch(k)
        for (i in 0 until k) { val v = mono[i] * sqGain; audio[2 * i] = v; audio[2 * i + 1] = v }
        return k
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
        // пилот переносится в ноль, узкий ФНЧ даёт его фазу без шума; sin(2·фазы) — поднесущая 38 кГц,
        // фазор³ — несущая RDS 57 кГц. Гетеродин — вращающийся фазор, фаза переносится между блоками.
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
            val p2r = pr * pr - pq * pq
            val p2i = 2 * pr * pq
            dif[i] = (mpx[i] * -p2i * diffGain).toFloat()     // s38 = −Im(фазор²)
            c57r[i] = (p2r * pr - p2i * pq).toFloat()
            c57i[i] = (p2r * pq + p2i * pr).toFloat()
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
        if (rdsOn) {
            val locked = stereoLock || pilotLevel > 0.02f
            rds.process(mpx, n, if (locked) c57r else null, if (locked) c57i else null)
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
            val a = mono[i]; val sd = side[i]
            mono[i] = 0.5f * (a + sd)
            side[i] = 0.5f * (a - sd)
        }
        deL.process(mono, k); deR.process(side, k)
        squelch(k)
        for (i in 0 until k) {
            audio[2 * i] = mono[i] * 1.4f * sqGain
            audio[2 * i + 1] = side[i] * 1.4f * sqGain
        }
        return k
    }

    private fun nfm(n: Int): Int {
        discriminate(r2, i2, n, mono, (AUDIO_RATE / (2 * PI) / 3000.0).toFloat())
        audioHp!!.process(mono, n); audioLp!!.process(mono, n)
        for (i in 0 until n) mono[i] *= 0.7f
        return n
    }

    /** Мгновенная частота в Гц → декодер DMR → звук 48 кГц (как _nfm в radio.py для режима DMR). */
    private fun dmrDemod(n: Int) {
        discriminate(r2, i2, n, mono, (AUDIO_RATE / (2 * PI)).toFloat())
        val d = dmr
        if (d == null) { mono.fill(0f, 0, n); return }
        val (out, ev) = d.process(mono, n)
        System.arraycopy(out, 0, mono, 0, n)
        dmrEvents.addAll(ev)
        while (dmrEvents.size > 200) dmrEvents.poll()
    }

    /** Огибающая → ФВЧ (убирает несущую сразу) → ФНЧ → АРУ. */
    private fun am(n: Int): Int {
        for (i in 0 until n) mono[i] = sqrt(r2[i] * r2[i] + i2[i] * i2[i])
        audioHp!!.process(mono, n); audioLp!!.process(mono, n)
        val k = agc.process(mono, n, agcOut)
        System.arraycopy(agcOut, 0, mono, 0, k)
        return k
    }

    private fun ssbDemod(n: Int): Int {
        ssbMix1.process(r2, i2, n, -ssbCenter)
        ssbLp!!.process(r2, i2, n)
        ssbMix2.process(r2, i2, n, if (mode == Modulation.CW) cwTone else ssbCenter)
        val k = agc.process(r2, n, agcOut)
        System.arraycopy(agcOut, 0, mono, 0, k)
        return k
    }

    /** Шумодав по уровню канала: плавное открытие/закрытие за блок. */
    private fun squelch(n: Int) {
        val open = levelDb >= squelchDb
        squelchOpen = open
        val target = if (open) 1f else 0f
        if (n == 0) return
        sqGain += (target - sqGain) * 0.6f
        if (abs(sqGain - target) < 0.01f) sqGain = target
    }
}
