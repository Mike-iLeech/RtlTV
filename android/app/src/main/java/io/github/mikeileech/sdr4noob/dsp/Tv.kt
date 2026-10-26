package io.github.mikeileech.sdr4noob.dsp

import io.github.mikeileech.sdr4noob.i18n.N_
import io.github.mikeileech.sdr4noob.i18n.tr
import kotlin.math.abs

/**
 * Телевизионные стандарты и сетки каналов — перенос rtltv/standards.py, плюс то, как окно ТВ настольной программы
 * выбирает частоту приёмника (rtltv/engine.py: _center, SOUND_TUNE; rtltv/app.py: _nominal, _nearest).
 */
class TvStandard(
    val key: String,
    val name: String,
    val lines: Int,             // строк в кадре
    val lineFreq: Double,       // Гц
    val soundOffset: Double,    // несущая звука относительно несущей изображения, Гц
    val activeStartUs: Double,
    val activeUs: Double,
    val hsyncUs: Double,
    val vis1: Int,              // первая видимая строка поля 1 (от начала кадровых импульсов поля 1)
    val vis2: Int,              // первая видимая строка поля 2
    val visCount: Int,          // видимых строк в поле
    val nBroad: Int,            // полустрок с широкими кадровыми импульсами
    val nEq: Int,               // полустрок с уравнивающими импульсами
    val tuneOffset: Double = 0.5e6,  // центр приёмника выше несущей изображения (уводит DC с несущей)
)

/**
 * Канал: несущая изображения (аналог), центр (цифровой мультиплекс), ширина — всё в Гц.
 * [name] — по-русски («R1 (МВ)», «30 (ДМВ)»): это ключ (сохраняется в настройках, сравнивается); для показа — [title].
 */
data class TvChannel(val name: String, val video: Double, val center: Double, val bandwidth: Double) {
    /** Название для экрана на текущем языке: «30 (ДМВ)» → «30 (UHF)». */
    val title: String get() = name
        .replace("(ДМВ)", "(" + tr("ДМВ") + ")")  // noqa: i18n
        .replace("(МВ)", "(" + tr("МВ") + ")")  // noqa: i18n
}

/** Сетка каналов: подпись (помечена N_, показывать через tr), стандарт по умолчанию, каналы. */
class TvPlan(val key: String, val name: String, val std: String, val channels: List<TvChannel>)

enum class TvMode { ANALOG, DIGITAL }

object Tv {
    /** Несущая звука на −250 кГц от центра приёмника в режиме «звук» (engine.SOUND_TUNE). */
    const val SOUND_TUNE = 0.25e6

    /**
     * Частоты дискретизации режима ТВ на Android: только радиорежим (RadioDemod.RADIO_RATES, кратно 240 кГц) и
     * не ниже 2 МГц — иначе куски обхода мультиплекса (±HOP_KEEP = ±1 МГц) не перекрывают шаг 2 МГц.
     */
    val RATES = intArrayOf(2_400_000, 2_880_000)
    const val DEFAULT_RATE = 2_400_000

    val STANDARDS: Map<String, TvStandard> = linkedMapOf(
        "DK" to TvStandard("DK", N_("SECAM/PAL D/K — Россия, СНГ"), 625, 15625.0, 6.5e6, 10.5, 52.0, 4.7, 22, 335, 288, 5, 5),
        "BG" to TvStandard("BG", N_("PAL B/G — Европа"), 625, 15625.0, 5.5e6, 10.5, 52.0, 4.7, 22, 335, 288, 5, 5),
        "I" to TvStandard("I", N_("PAL I — Великобритания"), 625, 15625.0, 6.0e6, 10.5, 52.0, 4.7, 22, 335, 288, 5, 5),
        "M" to TvStandard("M", N_("NTSC M — США, Япония"), 525, 15734.264, 4.5e6, 10.9, 52.6, 4.7, 17, 279, 240, 6, 6),
    )

    private fun ch(name: String, vcMhz: Double, centerOff: Double, bw: Double): TvChannel {
        val vc = vcMhz * 1e6
        return TvChannel(name, vc, vc + centerOff, bw)
    }

    private fun planRu(): List<TvChannel> {
        val vhf = doubleArrayOf(49.75, 59.25, 77.25, 85.25, 93.25, 175.25, 183.25, 191.25, 199.25, 207.25, 215.25, 223.25)
        val out = vhf.mapIndexed { i, f -> ch("R${i + 1} (МВ)", f, 2.75e6, 8e6) }.toMutableList()  // noqa: i18n
        for (n in 21 until 70) out += ch("$n (ДМВ)", 471.25 + 8 * (n - 21), 2.75e6, 8e6)  // noqa: i18n
        return out
    }

    private fun planEu(): List<TvChannel> {
        val out = mutableListOf(ch("E2", 48.25, 2.25e6, 7e6), ch("E3", 55.25, 2.25e6, 7e6), ch("E4", 62.25, 2.25e6, 7e6))
        for (n in 5 until 13) out += ch("E$n", 175.25 + 7 * (n - 5), 2.25e6, 7e6)
        for (n in 21 until 70) out += ch("$n (UHF)", 471.25 + 8 * (n - 21), 2.75e6, 8e6)
        return out
    }

    private fun planUk(): List<TvChannel> = (21 until 69).map { n -> ch("$n (UHF)", 471.25 + 8 * (n - 21), 2.75e6, 8e6) }

    private fun planUs(): List<TvChannel> {
        val vhf = linkedMapOf(2 to 55.25, 3 to 61.25, 4 to 67.25, 5 to 77.25, 6 to 83.25)
        val out = vhf.map { (n, f) -> ch("$n (VHF)", f, 1.75e6, 6e6) }.toMutableList()
        for (n in 7 until 14) out += ch("$n (VHF)", 175.25 + 6 * (n - 7), 1.75e6, 6e6)
        for (n in 14 until 37) out += ch("$n (UHF)", 471.25 + 6 * (n - 14), 1.75e6, 6e6)
        return out
    }

    val PLANS: Map<String, TvPlan> = linkedMapOf(
        "RU" to TvPlan("RU", N_("Россия / СНГ (R1–R12, 21–69)"), "DK", planRu()),
        "EU" to TvPlan("EU", N_("Европа (E2–E12, 21–69)"), "BG", planEu()),
        "UK" to TvPlan("UK", N_("Великобритания (21–68)"), "I", planUk()),
        "US" to TvPlan("US", N_("США (2–36)"), "M", planUs()),
    )

    fun channels(planKey: String): List<TvChannel> = PLANS.getValue(planKey).channels

    /** Номинальная частота канала в режиме: центр мультиплекса или несущая изображения (app._nominal). */
    fun nominal(ch: TvChannel, mode: TvMode): Double = if (mode == TvMode.DIGITAL) ch.center else ch.video

    /** Ближайший канал не дальше 0,5 МГц от номинала, иначе null (app._nearest). */
    fun nearest(chans: List<TvChannel>, f: Double, mode: TvMode): TvChannel? {
        val best = chans.minByOrNull { abs(nominal(it, mode) - f) } ?: return null
        return if (abs(nominal(best, mode) - f) <= 0.5e6) best else null
    }

    /** Ширина мультиплекса для центра [f]: по ближайшему каналу, иначе 8 МГц (app._push_freq_to_engine). */
    fun muxBandwidth(chans: List<TvChannel>, f: Double): Double = nearest(chans, f, TvMode.DIGITAL)?.bandwidth ?: 8e6

    /**
     * Центр приёмника (engine._center): мультиплекс — по центру; аналог со звуком — несущая звука + SOUND_TUNE
     * (звук на −250 кГц от центра); аналог без звука — несущая изображения + tuneOffset.
     */
    fun receiverCenter(mode: TvMode, sound: Boolean, std: TvStandard, videoCarrier: Double, muxCenter: Double): Double = when {
        mode == TvMode.DIGITAL -> muxCenter
        sound -> videoCarrier + std.soundOffset + SOUND_TUNE
        else -> videoCarrier + std.tuneOffset
    }

    /** Щелчок по спектру → новая частота (app._spectrum_click): аналог — шаг 1 кГц (и минус звук), цифра — 10 кГц. */
    fun spectrumClick(f: Double, mode: TvMode, sound: Boolean, std: TvStandard): Double =
        if (mode == TvMode.ANALOG) Math.rint((if (sound) f - std.soundOffset else f) / 1e3) * 1e3
        else Math.rint(f / 10e3) * 10e3
}
