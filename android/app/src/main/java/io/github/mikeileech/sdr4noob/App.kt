package io.github.mikeileech.sdr4noob

import android.app.Application
import io.github.mikeileech.sdr4noob.i18n.I18n

/** Приложение: до первой активити и сервиса включает язык интерфейса из настроек (I18n). */
class App : Application() {
    override fun onCreate() {
        super.onCreate()
        I18n.init(this)
    }
}
