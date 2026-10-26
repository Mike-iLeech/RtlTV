package io.github.mikeileech.sdr4noob.i18n

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale

/**
 * Язык интерфейса: русский (исходный) и английский — как rtltv/i18n.py настольной версии.
 *
 * Как пользоваться в коде:
 *     tr("Частота")                                         // простая строка
 *     tr("Найдено {n} мест, всего {size}", "n" to 3, "size" to s)   // вместо строкового шаблона — подстановки
 *     trn(n, "{n} борт", "{n} борта", "{n} бортов")          // множественное число: 1 / 2–4 / 5+
 *     val NAMES = listOf(N_("Тёмная")); tr(name)            // пометить строку на уровне модуля, перевести при показе
 *     num(105.3, 1)                                          // «105,3» по-русски, «105.3» по-английски
 *
 * Ключ перевода — сама русская строка (без строковых шаблонов `$x` — ключ не должен зависеть от значения).
 * Английские строки — assets/locale/en/ *.json ({"русская": "english"}; для trn — {"<форма one>": ["one", "other"]}),
 * файлы при загрузке сливаются в один словарь. Строки без перевода показываются по-русски, а тест I18nTest
 * не даёт им попасть в main.
 *
 * Язык — SharedPreferences "sdr4noob", ключ "language": "auto" | "ru" | "en"; "auto" — по языку системы
 * (русский для ru/be/kk/ky/tg, иначе английский). tr() вызывается при построении экрана, поэтому смена
 * языка — пересоздание активити (recreate). Без init() (тесты JVM) язык — русский.
 */
object I18n {
    const val PREFS = "sdr4noob"
    const val KEY = "language"
    const val AUTO = "auto"
    val LANGS = listOf("ru", "en")
    private val RU_SYSTEM = setOf("ru", "be", "kk", "ky", "tg")
    private const val LOCALE_DIR = "locale"

    @Volatile
    var language: String = "ru"
        private set

    @Volatile
    private var table: Map<String, Any> = emptyMap()

    /** Язык системы: "ru" для русскоязычных, иначе "en". */
    fun systemLanguage(locale: Locale = Locale.getDefault()): String =
        if (locale.language.lowercase(Locale.ROOT) in RU_SYSTEM) "ru" else "en"

    /** Какой язык включить по выбору пользователя ("auto" | "ru" | "en"). */
    fun resolve(choice: String?, locale: Locale = Locale.getDefault()): String =
        if (choice in LANGS) choice!! else systemLanguage(locale)

    /** Сохранённый выбор пользователя: "auto" | "ru" | "en". */
    fun configured(ctx: Context): String {
        val v = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY, AUTO)
        return if (v in LANGS) v!! else AUTO
    }

    /** Запомнить выбор пользователя и включить язык (экраны после этого пересоздаются). */
    fun choose(ctx: Context, choice: String) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString(KEY, if (choice in LANGS) choice else AUTO).apply()
        init(ctx, force = true)
    }

    private var initialized = false

    /** Включить язык из настроек и загрузить словарь из assets. Повторный вызов без force ничего не делает. */
    @Synchronized
    fun init(ctx: Context, force: Boolean = false) {
        if (initialized && !force) return
        val lang = resolve(configured(ctx))
        val cat = if (lang == "ru") emptyMap() else runCatching { loadAssets(ctx, lang) }.getOrDefault(emptyMap())
        set(lang, cat)
        initialized = true
    }

    /** Переключить язык в этом процессе с готовым словарём (тесты и init). */
    fun set(lang: String, catalog: Map<String, Any>) {
        table = if (lang == "ru") emptyMap() else catalog
        language = if (lang in LANGS) lang else "ru"
    }

    private fun loadAssets(ctx: Context, lang: String): Map<String, Any> {
        val am = ctx.assets
        val names = am.list("$LOCALE_DIR/$lang").orEmpty().filter { it.endsWith(".json") }.sorted()
        val out = LinkedHashMap<String, Any>()
        for (n in names) {
            runCatching {
                out.putAll(parse(am.open("$LOCALE_DIR/$lang/$n").use { it.readBytes().toString(Charsets.UTF_8) }))
            }
        }
        return out
    }

    /** Словарь из JSON: значение — строка или список форм [one, other]. */
    fun parse(json: String): Map<String, Any> {
        val o = JSONObject(json)
        val out = LinkedHashMap<String, Any>()
        for (k in o.keys()) {
            when (val v = o.get(k)) {
                is JSONArray -> out[k] = List(v.length()) { v.getString(it) }
                else -> out[k] = v.toString()
            }
        }
        return out
    }

    /** Есть ли перевод страницы справки (assets/<dir>/en/<file>) — тогда она открывается при английском. */
    fun localizedAsset(ctx: Context, path: String): String {
        if (language == "ru") return path
        val slash = path.indexOf('/')
        if (slash < 0) return path
        val alt = path.substring(0, slash) + "/" + language + path.substring(slash)
        return if (runCatching { ctx.assets.open(alt).close() }.isSuccess) alt else path
    }

    private val FIELD = Regex("""\{(\w+)\}""")      // «\}»: ICU на Android не принимает голую «}» (JVM принимает)

    /** Подстановка {имя} за один проход: «{…}» внутри подставленных значений (текст из эфира) не трогается. */
    fun format(s: String, kw: Array<out Pair<String, Any?>>): String {
        if (kw.isEmpty()) return s
        val m = kw.toMap()
        return FIELD.replace(s) { r -> if (m.containsKey(r.groupValues[1])) m[r.groupValues[1]].toString() else r.value }
    }

    fun tr(text: String, vararg kw: Pair<String, Any?>): String {
        val s = if (language == "ru") text else when (val v = table[text]) {
            is String -> v
            is List<*> -> (v.lastOrNull() as? String) ?: text   // ключ множественной формы, вызванный как tr — «other»
            else -> text
        }
        return format(s, kw)
    }

    internal fun ruForm(n: Long): Int {
        val a = Math.abs(n)
        if (a % 10 == 1L && a % 100 != 11L) return 0
        if (a % 10 in 2..4 && a % 100 !in 12..14) return 1
        return 2
    }

    fun trn(n: Long, one: String, few: String, many: String, vararg kw: Pair<String, Any?>): String {
        val forms = if (language == "ru") null else table[one] as? List<*>
        val s = if (forms != null && forms.isNotEmpty())
            (if (Math.abs(n) == 1L) forms.first() else forms.last()) as String
        else listOf(one, few, many)[ruForm(n)]
        val all = if (kw.any { it.first == "n" }) kw else arrayOf("n" to n, *kw)
        return format(s, all)
    }

    fun num(x: Double, nd: Int = 1): String {
        val s = String.format(Locale.ROOT, "%.${nd}f", x)
        return if (language == "ru") s.replace('.', ',') else s
    }

    /** Поправить десятичный разделитель в готовой строке с числом ("105.3" → "105,3" по-русски). */
    fun dec(s: String): String = if (language == "ru") s.replace(Regex("(?<=\\d)\\.(?=\\d)"), ",") else s
}

fun tr(text: String, vararg kw: Pair<String, Any?>): String = I18n.tr(text, *kw)

fun trn(n: Number, one: String, few: String, many: String, vararg kw: Pair<String, Any?>): String =
    I18n.trn(n.toLong(), one, few, many, *kw)

/** Пометить строку для перевода, не переводя (на уровне модуля; переводится потом через tr(переменная)). */
@Suppress("FunctionName")
fun N_(text: String): String = text

fun num(x: Number, nd: Int = 1): String = I18n.num(x.toDouble(), nd)

/** Десятичный разделитель в готовой строке с числом: "105.3" → "105,3" по-русски. */
fun dec(s: String): String = I18n.dec(s)
