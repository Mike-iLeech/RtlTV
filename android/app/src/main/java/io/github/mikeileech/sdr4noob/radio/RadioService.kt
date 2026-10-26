package io.github.mikeileech.sdr4noob.radio

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import io.github.mikeileech.sdr4noob.ui.RadioActivity

/**
 * Приём в фоне: пока открыто окно радио, этот сервис переднего плана (тип «воспроизведение») держит процесс,
 * процессор и — для rtl_tcp — Wi-Fi, чтобы звук не прерывался при выключенном экране. В уведомлении — частота
 * и кнопка «Стоп» (закрывает окно радио, а с ним и приёмник). Сам движок живёт в окне радио.
 */
class RadioService : Service() {
    companion object {
        private const val CHANNEL = "radio"
        private const val ID = 1
        private const val ACTION_STOP = "io.github.mikeileech.sdr4noob.STOP"
        private const val EXTRA_TEXT = "text"
        private const val EXTRA_WIFI = "wifi"

        /** Что сделать по кнопке «Стоп» (ставит окно радио). */
        @Volatile var onStop: (() -> Unit)? = null

        fun start(ctx: Context, text: String, wifi: Boolean) {
            val i = Intent(ctx, RadioService::class.java).putExtra(EXTRA_TEXT, text).putExtra(EXTRA_WIFI, wifi)
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i) else ctx.startService(i)
        }

        fun stop(ctx: Context) { ctx.stopService(Intent(ctx, RadioService::class.java)) }
    }

    private var wake: PowerManager.WakeLock? = null
    private var wifi: WifiManager.WifiLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            Handler(Looper.getMainLooper()).post { onStop?.invoke() }
            stopSelf()
            return START_NOT_STICKY
        }
        val n = notification(intent?.getStringExtra(EXTRA_TEXT) ?: "Радио")
        if (Build.VERSION.SDK_INT >= 29) startForeground(ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK)
        else startForeground(ID, n)
        if (wake == null) {
            wake = (getSystemService(POWER_SERVICE) as PowerManager)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "sdr4noob:radio").apply { setReferenceCounted(false); acquire() }
        }
        if (intent?.getBooleanExtra(EXTRA_WIFI, false) == true && wifi == null) {
            @Suppress("DEPRECATION")
            val mode = if (Build.VERSION.SDK_INT >= 29) WifiManager.WIFI_MODE_FULL_HIGH_PERF else WifiManager.WIFI_MODE_FULL
            wifi = (applicationContext.getSystemService(WIFI_SERVICE) as WifiManager)
                .createWifiLock(mode, "sdr4noob:rtltcp").apply { setReferenceCounted(false); acquire() }
        }
        return START_NOT_STICKY
    }

    private fun notification(text: String): Notification {
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Приём в фоне", NotificationManager.IMPORTANCE_LOW))
        }
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        val open = PendingIntent.getActivity(this, 0,
            Intent(this, RadioActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP), flags)
        val stop = PendingIntent.getService(this, 1, Intent(this, RadioService::class.java).setAction(ACTION_STOP), flags)
        @Suppress("DEPRECATION")
        val b = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, CHANNEL) else Notification.Builder(this)
        return b.setSmallIcon(android.R.drawable.stat_sys_headset)
            .setContentTitle("SDR4NOOB — приём")
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null as android.graphics.drawable.Icon?, "Стоп", stop).build())
            .build()
    }

    override fun onDestroy() {
        runCatching { wake?.release() }
        runCatching { wifi?.release() }
        wake = null; wifi = null
        super.onDestroy()
    }
}
