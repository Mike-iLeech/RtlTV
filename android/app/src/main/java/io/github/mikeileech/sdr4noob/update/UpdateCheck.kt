package io.github.mikeileech.sdr4noob.update

import android.content.Context
import io.github.mikeileech.sdr4noob.BuildConfig
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * Проверка обновлений — как rtltv/updater.py, но APK не ставится сам: Android требует, чтобы
 * установку подтвердил пользователь, поэтому даётся ссылка на файл SDR4NOOB-<версия>-android.apk
 * последнего выпуска GitHub. Проверка — не чаще раза в сутки.
 */
object UpdateCheck {
    const val REPO = "Mike-iLeech/SDR4NOOB"

    data class Info(val version: String, val apkUrl: String, val sizeMb: String)

    /** Сравнение версий X.Y.Z: >0 — a новее b. */
    fun compare(a: String, b: String): Int {
        val pa = a.trim().removePrefix("v").split(".").map { it.toIntOrNull() ?: 0 }
        val pb = b.trim().removePrefix("v").split(".").map { it.toIntOrNull() ?: 0 }
        for (i in 0 until maxOf(pa.size, pb.size)) {
            val d = pa.getOrElse(i) { 0 } - pb.getOrElse(i) { 0 }
            if (d != 0) return d
        }
        return 0
    }

    /** Разобрать ответ releases/latest: новая версия с APK или null. */
    fun parse(json: String, current: String): Info? {
        val o = JSONObject(json)
        val ver = o.optString("tag_name").removePrefix("v").removePrefix("V")
        if (ver.isEmpty() || compare(ver, current) <= 0) return null
        val assets = o.optJSONArray("assets") ?: return null
        for (i in 0 until assets.length()) {
            val a = assets.getJSONObject(i)
            val name = a.optString("name")
            if (name.endsWith("-android.apk", ignoreCase = true)) {
                val mb = "%.1f".format(a.optLong("size") / 1048576.0)
                return Info(ver, a.optString("browser_download_url"), mb)
            }
        }
        return null
    }

    fun checkInBackground(context: Context, onNewer: (Info) -> Unit) {
        val prefs = context.getSharedPreferences("sdr4noob", Context.MODE_PRIVATE)
        val now = System.currentTimeMillis()
        if (!prefs.getBoolean("update_check", true)) return
        if (now - prefs.getLong("update_last", 0) < 24 * 3600_000L) {
            prefs.getString("update_cached", null)?.let { c -> runCatching { parse(c, BuildConfig.VERSION_NAME) }.getOrNull()?.let(onNewer) }
            return
        }
        Thread {
            try {
                val c = URL("https://api.github.com/repos/$REPO/releases/latest").openConnection() as HttpURLConnection
                c.connectTimeout = 8000; c.readTimeout = 8000
                c.setRequestProperty("Accept", "application/vnd.github+json")
                c.setRequestProperty("User-Agent", "SDR4NOOB-Android/${BuildConfig.VERSION_NAME}")
                val body = c.inputStream.bufferedReader().use { it.readText() }
                prefs.edit().putLong("update_last", now).putString("update_cached", body).apply()
                parse(body, BuildConfig.VERSION_NAME)?.let(onNewer)
            } catch (_: Exception) {
                // нет сети — проверим в следующий раз
            }
        }.apply { isDaemon = true }.start()
    }
}
