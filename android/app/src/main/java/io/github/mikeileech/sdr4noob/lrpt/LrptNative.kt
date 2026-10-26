package io.github.mikeileech.sdr4noob.lrpt

/** JNI к lrpt_dsp.c (src/main/cpp/lrpt_jni.c). Без нативной библиотеки (JVM-тесты) loaded = false. */
object LrptNative {
    val loaded: Boolean = runCatching { System.loadLibrary("sdr4noob"); demodSize() > 0 }.getOrDefault(false)

    external fun demodSize(): Int
    external fun demodCreate(sps: Double, mode: Int, costasBw: Double, timingBw: Double, fmaxRad: Double): Long
    external fun demodFree(h: Long)
    external fun demodSetCostas(h: Long, bw: Double)
    external fun demodSetFreq(h: Long, freqRad: Double)
    external fun demodState(h: Long, out: DoubleArray)
    external fun demodProcess(h: Long, iq: FloatArray, n: Int, soft: ByteArray, cons: FloatArray?, maxSym: Int): Int
    external fun convEncode(bits: ByteArray, n: Int, out: ByteArray, state: Int): Int
    external fun viterbi27(soft: ByteArray, npairs: Int, out: ByteArray): Int
    external fun rsEncode(data: ByteArray, parity: ByteArray, dual: Boolean)
    external fun rsDecode(cw: ByteArray, dual: Boolean): Int
}

/** Демодулятор на библиотеке C. Состояние — память C, освобождается close() или сборщиком мусора. */
class NativeDemod(sps: Double, mode: Int, costasBw: Double, timingBw: Double, fmaxRad: Double) : DemodCore {
    private var h: Long = LrptNative.demodCreate(sps, mode, costasBw, timingBw, fmaxRad)

    init {
        check(h != 0L) { "demodCreate: не удалось создать демодулятор" }
    }

    override fun setCostas(bw: Double) = LrptNative.demodSetCostas(h, bw)
    override fun setFreq(freqRad: Double) = LrptNative.demodSetFreq(h, freqRad)
    override fun state(out: DoubleArray) = LrptNative.demodState(h, out)
    override fun process(iq: FloatArray, n: Int, soft: ByteArray, cons: FloatArray?, maxSym: Int): Int =
        LrptNative.demodProcess(h, iq, n, soft, cons, maxSym).coerceAtLeast(0)

    @Synchronized
    override fun close() {
        if (h != 0L) LrptNative.demodFree(h)
        h = 0L
    }

    @Suppress("deprecation")
    protected fun finalize() = close()
}

/**
 * Тяжёлые блоки декодера: на устройстве — библиотека C (как ctypes-вызовы в rtltv/lrpt.py), в JVM-тестах —
 * их перенос на Kotlin (KotlinDemod, KotlinCodec), совпадающий с C бит в бит для Витерби, кодера и RS.
 */
object LrptCodec {
    /** true — библиотека C; false — Kotlin. Переключать можно в любой момент (новые демодуляторы). */
    @Volatile
    var useNative: Boolean = LrptNative.loaded

    fun demod(sps: Double, mode: Int, costasBw: Double, timingBw: Double, fmaxRad: Double): DemodCore =
        if (useNative) NativeDemod(sps, mode, costasBw, timingBw, fmaxRad)
        else KotlinDemod(sps, mode, costasBw, timingBw, fmaxRad)

    fun convEncode(bits: ByteArray, n: Int, out: ByteArray, state: Int): Int =
        if (useNative) LrptNative.convEncode(bits, n, out, state) else KotlinCodec.convEncode(bits, n, out, state)

    fun viterbi27(soft: ByteArray, npairs: Int, out: ByteArray): Int =
        if (useNative) LrptNative.viterbi27(soft, npairs, out) else KotlinCodec.viterbi27(soft, npairs, out)

    fun rsEncode(data: ByteArray, parity: ByteArray, dual: Boolean) =
        if (useNative) LrptNative.rsEncode(data, parity, dual) else KotlinCodec.rsEncode(data, parity, dual)

    fun rsDecode(cw: ByteArray, dual: Boolean): Int =
        if (useNative) LrptNative.rsDecode(cw, dual) else KotlinCodec.rsDecode(cw, dual)
}
