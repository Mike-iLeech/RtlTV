package io.github.mikeileech.sdr4noob.sdr

import io.github.mikeileech.sdr4noob.i18n.N_

/** Ошибка приёмника с понятным пользователю текстом. */
class SdrException(message: String, cause: Throwable? = null) : Exception(message, cause)

/**
 * Источник IQ — любой USB радиоприёмник (SDR), сетевой rtl_tcp или тестовый сигнал.
 * Как SourceBase в настольной версии (rtltv/source.py), но с явным чтением блоков: поток движка
 * вызывает [read] в цикле. Все методы вызываются только из потока движка (RadioEngine ставит настройки
 * из интерфейса в очередь) — источникам не нужна потокобезопасность.
 */
interface SdrSource {
    /** Название для интерфейса: «RTL-SDR Blog V4 (R828D)», «rtl_tcp 192.168.1.10:1234» … */
    val label: String
    /** Ступени усиления, десятые доли dB (пусто — ручного усиления нет). */
    val gains: IntArray
    /** Частоты дискретизации, которые принимает устройство (из RadioDemod.RADIO_RATES). */
    val sampleRates: IntArray get() = io.github.mikeileech.sdr4noob.dsp.RadioDemod.RADIO_RATES
    /** Есть ли питание антенны bias-T. */
    val hasBiasTee: Boolean get() = false
    /** Запись (файл IQ): частоту дискретизации и центр задаёт источник, перестраивать их нельзя. */
    val fixedRate: Int? get() = null
    val fixedCenter: Long? get() = null
    /** Можно ли перестраивать центр (для поиска DMR). Читается из потока интерфейса — не должно открывать устройство. */
    val retunable: Boolean get() = true

    fun open(sampleRate: Int, frequencyHz: Long)
    fun start()

    /**
     * Прочитать до [maxSamples] комплексных отсчётов в [re]/[im] (нормированы к ±1).
     * Возвращает число отсчётов; 0 — данных пока нет (тайм-аут); бросает [SdrException] при потере устройства.
     */
    fun read(re: FloatArray, im: FloatArray, maxSamples: Int): Int

    fun setFrequency(hz: Long)
    fun setSampleRate(rate: Int)
    /** [tenthsDb] < 0 — автоматическое усиление (АРУ тюнера). */
    fun setGain(tenthsDb: Int)
    fun setPpm(ppm: Int) {}
    fun setBiasTee(on: Boolean) {}
    /** Сколько отсчётов потеряно (не успели забрать), для индикатора. */
    val droppedSamples: Long get() = 0

    fun close()
}

/** Перевод u8 IQ (как отдаёт RTL2832U и rtl_tcp) в float ±1. */
object U8 {
    private val lut = FloatArray(256) { (it - 127.4f) / 128f }

    fun convert(src: ByteArray, bytes: Int, re: FloatArray, im: FloatArray): Int {
        val n = bytes / 2
        val t = lut
        for (i in 0 until n) {
            re[i] = t[src[2 * i].toInt() and 0xFF]
            im[i] = t[src[2 * i + 1].toInt() and 0xFF]
        }
        return n
    }
}

/** Типы тюнеров librtlsdr (rtlsdr_get_tuner_type, заголовок rtl_tcp). */
/** Названия латиницей; «неизвестен» помечено N_ — показывать через tr(). */
val TUNERS = mapOf(0 to N_("неизвестен"), 1 to "E4000", 2 to "FC0012", 3 to "FC0013", 4 to "FC2580",
    5 to "R820T/R820T2", 6 to "R828D")
