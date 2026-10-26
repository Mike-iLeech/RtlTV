package io.github.mikeileech.sdr4noob.ui

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.os.Bundle
import android.text.InputType
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import io.github.mikeileech.sdr4noob.radio.Storage
import java.io.File
import java.util.Locale

/**
 * Настройки (как «Настройки» на ПК): папка данных — стандартные «Загрузки»/«Изображения» или своя папка
 * (например, на карте памяти) через системный выбор папки; что где лежит; место приёмника для карты, колец
 * дальности и пролётов спутников; проверка обновлений; очистка кэша карт и орбит.
 */
class SettingsActivity : Activity() {
    companion object { private const val REQ_TREE = 21 }

    private lateinit var prefs: android.content.SharedPreferences
    private lateinit var body: LinearLayout

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        title = "Настройки"
        prefs = getSharedPreferences("sdr4noob", MODE_PRIVATE)
        build()
    }

    private fun build() {
        val root = Ui.vbox(this, 16)
        root.setBackgroundColor(Ui.BG)
        body = root

        // ---- папка данных
        val data = Ui.card(this)
        data.addView(Ui.text(this, "Папка данных", 17f, bold = true))
        data.addView(Ui.text(this, "Сюда SDR4NOOB складывает всё, что вы получаете: записи звука и IQ, картинки SSTV, " +
            "снимки «Метеора». По умолчанию — стандартные папки телефона (их видно в «Файлах» и в галерее). " +
            "Можно выбрать свою папку, например на карте памяти: тогда всё ляжет в неё, в подпапку SDR4NOOB.", 13f, Ui.DIM))
        val tree = Storage.tree(this)
        data.addView(Ui.text(this, "Сейчас: " + (tree?.let { "своя папка «${Storage.treeName(it)}»" } ?: "стандартные папки телефона"),
            15f, Ui.ACCENT), Ui.lp(top = Ui.dp(this, 6)))
        val b = Ui.hbox(this)
        b.addView(Ui.button(this, "Выбрать папку…") {
            startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION or
                    Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION), REQ_TREE)
        }, Ui.lp(0, weight = 1f))
        b.addView(Ui.button(this, "По умолчанию") {
            Storage.tree(this)?.let { t -> runCatching { contentResolver.releasePersistableUriPermission(t,
                Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION) } }
            Storage.setTree(this, null); recreate()
        }, Ui.lp(0, weight = 1f))
        data.addView(b)
        root.addView(data, Ui.lp(top = Ui.dp(this, 4)))

        // ---- что где лежит
        val where = Ui.card(this)
        where.addView(Ui.text(this, "Что где будет лежать", 17f, bold = true))
        for ((sub, pic, what) in listOf(
            Triple(Storage.AUDIO, false, "записи радио (WAV)"),
            Triple(Storage.IQ, false, "записи IQ (базовая полоса, WAV)"),
            Triple(Storage.SSTV, true, "картинки SSTV"),
            Triple(Storage.METEOR, true, "снимки «Метеор-М»"),
        )) where.addView(Ui.text(this, "• $what — ${Storage.whereLabel(this, sub, pic)}", 13f))
        where.addView(Ui.text(this, "Настройки, кэш карт и орбит спутников хранятся в самом приложении и удаляются вместе с ним.",
            12f, Ui.DIM), Ui.lp(top = Ui.dp(this, 4)))
        root.addView(where, Ui.lp(top = Ui.dp(this, 12)))

        // ---- место приёмника
        val home = Ui.card(this)
        home.addView(Ui.text(this, "Место приёмника", 17f, bold = true))
        home.addView(Ui.text(this, "Для колец дальности на карте, расстояний до самолётов и станций APRS и для расчёта " +
            "пролётов спутников. Можно задать и на карте кнопкой «Дом = центр карты».", 13f, Ui.DIM))
        val (la, lo) = Home.get(prefs)
        val lat = EditText(this).apply { inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL or InputType.TYPE_NUMBER_FLAG_SIGNED
            setText(String.format(Locale.ROOT, "%.4f", la)); setTextColor(0xFFFFFFFF.toInt()); hint = "широта" }
        val lon = EditText(this).apply { inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL or InputType.TYPE_NUMBER_FLAG_SIGNED
            setText(String.format(Locale.ROOT, "%.4f", lo)); setTextColor(0xFFFFFFFF.toInt()); hint = "долгота" }
        val row = Ui.hbox(this)
        row.addView(Ui.text(this, "Широта", 13f), Ui.lp(Ui.dp(this, 70)))
        row.addView(lat, Ui.lp(0, weight = 1f))
        row.addView(Ui.text(this, "Долгота", 13f), Ui.lp(Ui.dp(this, 76)))
        row.addView(lon, Ui.lp(0, weight = 1f))
        home.addView(row)
        home.addView(Ui.button(this, "Сохранить место") {
            val a = lat.text.toString().replace(',', '.').toDoubleOrNull()
            val o = lon.text.toString().replace(',', '.').toDoubleOrNull()
            if (a == null || o == null || a !in -90.0..90.0 || o !in -180.0..180.0) toast("Широта от −90 до 90, долгота от −180 до 180")
            else { Home.set(prefs, a, o); toast("Место приёмника сохранено") }
        })
        root.addView(home, Ui.lp(top = Ui.dp(this, 12)))

        // ---- обновления и кэш
        val misc = Ui.card(this)
        misc.addView(Ui.text(this, "Обновления и кэш", 17f, bold = true))
        misc.addView(CheckBox(this).apply {
            text = "Проверять новую версию на GitHub (раз в день)"; setTextColor(0xFFFFFFFF.toInt())
            isChecked = prefs.getBoolean("update_check", true)
            setOnCheckedChangeListener { _, on -> prefs.edit().putBoolean("update_check", on).apply() }
        })
        misc.addView(Ui.button(this, "Очистить кэш карт и орбит") {
            AlertDialog.Builder(this).setMessage("Удалить скачанные тайлы карты (" + size(File(cacheDir, "tiles")) +
                ") и орбиты спутников? Они скачаются заново при необходимости.")
                .setPositiveButton("Удалить") { _, _ ->
                    File(cacheDir, "tiles").deleteRecursively(); File(filesDir, "tle").deleteRecursively()
                    toast("Кэш очищен")
                }.setNegativeButton("Отмена", null).show()
        })
        root.addView(misc, Ui.lp(top = Ui.dp(this, 12)))

        setContentView(ScrollView(this).apply { addView(root); setBackgroundColor(Ui.BG) })
    }

    private fun size(f: File): String {
        val b = f.walkBottomUp().filter { it.isFile }.sumOf { it.length() }
        return when { b >= 1e9 -> "%.1f ГБ".format(Locale.ROOT, b / 1e9); b >= 1e6 -> "%.0f МБ".format(Locale.ROOT, b / 1e6)
            else -> "%.0f КБ".format(Locale.ROOT, b / 1e3) }.replace('.', ',')
    }

    private fun toast(s: String) = android.widget.Toast.makeText(this, s, android.widget.Toast.LENGTH_LONG).show()

    @Deprecated("Activity.onActivityResult")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_TREE || resultCode != RESULT_OK) return
        val uri = data?.data ?: return
        try {
            contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            Storage.setTree(this, uri)
            toast("Папка данных: ${Storage.treeName(uri)}")
        } catch (e: SecurityException) {
            toast("Android не дал доступ к этой папке: ${e.message}")
        }
        recreate()
    }
}
