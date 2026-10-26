package io.github.mikeileech.sdr4noob.dsp

/** JNI к mbelib (src/main/cpp/mbe_jni.c). */
object MbeNative {
    val loaded: Boolean = runCatching { System.loadLibrary("sdr4noob"); true }.getOrDefault(false)
    external fun create(): Long
    external fun reset(h: Long)
    external fun free(h: Long)
    external fun decode(h: Long, fr: ByteArray, out: FloatArray): Int
}

/** Синтезатор AMBE+2 одного слота на mbelib. Без нативной библиотеки (юнит-тесты) — тишина, ok = false. */
class MbeSynth : AmbeSynth {
    private val h: Long = if (MbeNative.loaded) MbeNative.create() else 0L
    override val ok get() = h != 0L
    override fun reset() { if (h != 0L) MbeNative.reset(h) }
    override fun decode(fr: ByteArray, out: FloatArray): Int {
        if (h == 0L) { out.fill(0f); return 0 }
        return MbeNative.decode(h, fr, out)
    }

    /** Состояние синтезатора — кусок памяти C: освобождается вместе с декодером (он живёт, пока режим DMR). */
    @Suppress("deprecation")
    protected fun finalize() { if (h != 0L) MbeNative.free(h) }
}
