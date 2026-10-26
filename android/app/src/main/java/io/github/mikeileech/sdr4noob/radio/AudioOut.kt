package io.github.mikeileech.sdr4noob.radio

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack

/** Звук 48 кГц стерео float. Запись не блокирует поток движка: лишнее при переполнении отбрасывается. */
class AudioOut(private val rate: Int = 48_000) {
    private var track: AudioTrack? = null
    @Volatile var volume = 0.8f
        set(v) { field = v; track?.setVolume(v) }
    var muted = System.getenv("RTLTV_MUTE") == "1"

    fun write(stereo: FloatArray, frames: Int) {
        if (muted || frames == 0) return
        val t = track ?: create().also { track = it }
        t.write(stereo, 0, frames * 2, AudioTrack.WRITE_NON_BLOCKING)
    }

    private fun create(): AudioTrack {
        val fmt = AudioFormat.Builder()
            .setEncoding(AudioFormat.ENCODING_PCM_FLOAT)
            .setSampleRate(rate)
            .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO)
            .build()
        val min = AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_STEREO, AudioFormat.ENCODING_PCM_FLOAT)
        val t = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
            .setAudioFormat(fmt)
            .setBufferSizeInBytes(maxOf(min, rate * 2 * 4 * 3 / 10))   // ~0,3 с запаса на неравномерность USB
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        t.setVolume(volume)
        t.play()
        return t
    }

    fun close() {
        track?.let { it.pause(); it.flush(); it.release() }
        track = null
    }
}
