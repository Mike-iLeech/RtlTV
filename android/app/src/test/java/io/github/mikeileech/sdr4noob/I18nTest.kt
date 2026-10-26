package io.github.mikeileech.sdr4noob

import io.github.mikeileech.sdr4noob.i18n.I18n
import io.github.mikeileech.sdr4noob.i18n.num
import io.github.mikeileech.sdr4noob.i18n.tr
import io.github.mikeileech.sdr4noob.i18n.trn
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.util.Locale

/**
 * Перевод интерфейса (i18n/I18n.kt) — аналог tests/test_i18n.py настольной версии:
 *  * кириллица в строковом литерале исходников (src/main/java) допустима только как аргумент tr()/trn()/N_(),
 *    в сообщении журнала (Log.x — журнал читает разработчик) или в строке с комментарием «noqa: i18n»
 *    (данные, которые не показываются: разбор ввода, имена файлов, ключи и т. п.);
 *  * ключ tr()/trn()/N_() — один литерал без строковых шаблонов ($x, ${…}) — иначе ключ зависел бы от значения;
 *  * у каждого ключа есть перевод в assets/locale/en/ *.json без кириллицы и с теми же {подстановками};
 *  * в словаре нет ключей, которых больше нет в коде.
 */
class I18nTest {
    private val cyr = Regex("[А-Яа-яЁё]")
    private val field = Regex("""\{(\w+)}""")

    @After
    fun reset() = I18n.set("ru", emptyMap())

    // ---------------------------------------------------------------- разбор исходников Kotlin

    data class Lit(val start: Int, val end: Int, val value: String, val text: String, val template: Boolean)

    /** Мини-лексер Kotlin: строковые литералы (с вложенными в ${…}) и код с вырезанными комментариями. */
    class Lexer(private val s: String) {
        val lits = ArrayList<Lit>()
        val code = StringBuilder(s)
        private var i = 0

        fun run(): Lexer { codeUntil(null); return this }

        private fun blank(a: Int, b: Int) { for (k in a until b) if (code[k] != '\n') code.setCharAt(k, ' ') }

        /** Код до закрывающей «}» шаблона (stopAtBrace) или до конца файла. */
        private fun codeUntil(stop: Char?) {
            var depth = 0
            while (i < s.length) {
                val c = s[i]
                when {
                    s.startsWith("//", i) -> { val e = s.indexOf('\n', i).let { if (it < 0) s.length else it }; blank(i, e); i = e }
                    s.startsWith("/*", i) -> {
                        val a = i; var d = 0
                        while (i < s.length) {
                            if (s.startsWith("/*", i)) { d++; i += 2 } else if (s.startsWith("*/", i)) { d--; i += 2; if (d == 0) break } else i++
                        }
                        blank(a, i)
                    }
                    s.startsWith("\"\"\"", i) -> string(raw = true)
                    c == '"' -> string(raw = false)
                    c == '\'' -> {   // символ: 'a', '\n', '"'
                        i++
                        while (i < s.length && s[i] != '\'') { if (s[i] == '\\') i++; i++ }
                        i++
                    }
                    c == '{' -> { depth++; i++ }
                    c == '}' -> { if (stop != null && depth == 0) { i++; return }; depth--; i++ }
                    else -> i++
                }
            }
        }

        private fun string(raw: Boolean) {
            val start = i
            i += if (raw) 3 else 1
            val value = StringBuilder(); val text = StringBuilder(); var template = false
            while (i < s.length) {
                if (raw && s.startsWith("\"\"\"", i)) { i += 3; while (i < s.length && s[i] == '"') i++; break }
                val c = s[i]
                if (!raw && c == '"') { i++; break }
                if (!raw && c == '\\') {
                    val n = s[i + 1]
                    val ch = when (n) {
                        'n' -> "\n"; 't' -> "\t"; 'r' -> "\r"; 'b' -> "\b"
                        'u' -> { val h = s.substring(i + 2, i + 6); i += 4; h.toInt(16).toChar().toString() }
                        else -> n.toString()
                    }
                    value.append(ch); text.append(ch); i += 2; continue
                }
                if (c == '$' && i + 1 < s.length && s[i + 1] == '{') {
                    template = true; i += 2; text.append('\u0000'); codeUntil('}'); continue
                }
                if (c == '$' && i + 1 < s.length && (s[i + 1].isLetter() || s[i + 1] == '_')) {
                    template = true; i++
                    while (i < s.length && (s[i].isLetterOrDigit() || s[i] == '_')) i++
                    text.append('\u0000'); continue
                }
                value.append(c); text.append(c); i++
            }
            lits.add(Lit(start, i, value.toString(), text.toString(), template))
        }
    }

    data class Key(val file: String, val line: Int, val key: String, val plural: Boolean)

    private val srcRoot = File("src/main/java")
    private val sources: Map<String, String> by lazy {
        assertTrue("нет исходников в ${srcRoot.absolutePath}", srcRoot.isDirectory)
        srcRoot.walkTopDown().filter { it.isFile && it.extension == "kt" && it.name != "I18n.kt" }   // сам механизм перевода
            .associate { it.relativeTo(srcRoot).path.replace('\\', '/') to it.readText() }
    }

    private fun lineOf(s: String, off: Int) = s.substring(0, off).count { it == '\n' } + 1

    /** (нарушения, ключи) по всем исходникам. */
    private fun scan(): Pair<List<String>, List<Key>> {
        val bad = ArrayList<String>(); val keys = ArrayList<Key>()
        for ((name, src) in sources) {
            val lx = Lexer(src).run()
            val code = lx.code.toString()
            val byStart = lx.lits.associateBy { it.start }
            val ok = HashSet<Int>()
            fun key(l: Lit, plural: Boolean) {
                ok.add(l.start)
                val ln = lineOf(src, l.start)
                if (l.template) bad.add("$name:$ln: строковый шаблон в ключе перевода: ${l.value}")
                val next = code.substring(l.end).trimStart().firstOrNull()
                if (next != ',' && next != ')') bad.add("$name:$ln: ключ перевода должен быть одним литералом: ${l.value}")
                keys.add(Key(name, ln, l.value, plural))
            }
            // tr("…") и N_("…") — первый аргумент
            for (l in lx.lits) {
                var k = l.start - 1
                while (k >= 0 && code[k].isWhitespace()) k--
                if (k < 0 || code[k] != '(') continue
                k--
                while (k >= 0 && code[k].isWhitespace()) k--
                val e = k + 1
                while (k >= 0 && (code[k].isLetterOrDigit() || code[k] == '_')) k--
                val ident = code.substring(k + 1, e)
                if (ident == "tr" || ident == "N_") key(l, false)
            }
            // trn(n, "one", "few", "many")
            val lits = lx.lits.sortedBy { it.start }
            fun inLit(off: Int) = lits.any { off > it.start && off < it.end }
            for (m in Regex("""(?<![\w.])(?:I18n\.)?trn\(""").findAll(code)) {
                if (inLit(m.range.first)) continue
                if (m.range.first >= 4 && code.substring(m.range.first - 4, m.range.first) == "fun ") continue
                var p = m.range.last + 1; var depth = 0
                while (p < code.length) {
                    val l = byStart[p]
                    if (l != null) { p = l.end; continue }
                    val c = code[p]
                    if (c in "([{") depth++ else if (c in ")]}") depth-- else if (c == ',' && depth == 0) break
                    p++
                }
                val ln = lineOf(src, m.range.first)
                var okForms = true
                for (f in 0 until 3) {
                    p++
                    while (p < code.length && code[p].isWhitespace()) p++
                    val l = byStart[p]
                    if (l == null) { okForms = false; break }
                    key(l, f == 0)
                    if (f > 0) keys.removeAt(keys.size - 1)   // ключ словаря — только форма one
                    p = l.end
                    while (p < code.length && code[p].isWhitespace()) p++
                    if (f < 2 && (p >= code.length || code[p] != ',')) { okForms = false; break }
                }
                if (!okForms) bad.add("$name:$ln: trn(n, \"one\", \"few\", \"many\") — формы должны быть литералами")
            }
            val lines = src.lines()
            for (l in lx.lits) {
                if (l.start in ok || !cyr.containsMatchIn(l.text)) continue
                val a = lineOf(src, l.start); val b = lineOf(src, l.end)
                val span = (a..b).map { lines[it - 1] }
                if (span.any { "noqa: i18n" in it }) continue
                if (Regex("""\bLog\.[dviwe]\(""").containsMatchIn(lines[a - 1])) continue
                bad.add("$name:$a: строка мимо tr(): ${l.value.take(100)}")
            }
        }
        return bad to keys
    }

    private fun catalog(): Map<String, Any> {
        val dir = File("src/main/assets/locale/en")
        assertTrue("нет словаря ${dir.absolutePath}", dir.isDirectory)
        val out = LinkedHashMap<String, Any>()
        for (f in dir.listFiles()!!.filter { it.extension == "json" }.sortedBy { it.name }) {
            val part = I18n.parse(f.readText())
            val dup = part.keys.intersect(out.keys)
            assertTrue("${f.name}: ключи уже есть в других файлах: $dup", dup.isEmpty())
            out.putAll(part)
        }
        return out
    }

    private fun fields(s: String) = field.findAll(s).map { it.groupValues[1] }.toSet()

    @Test
    fun noRussianOutsideTr() {
        val (bad, _) = scan()
        assertTrue("Строки без перевода (обернуть в tr()/trn()/N_() или пометить «// noqa: i18n»):\n" +
            bad.joinToString("\n"), bad.isEmpty())
    }

    @Test
    fun everyKeyTranslated() {
        val (_, keys) = scan()
        assertTrue("ключей перевода подозрительно мало: ${keys.size}", keys.size > 300)
        val cat = catalog()
        val problems = ArrayList<String>()
        for (k in keys) {
            val v = cat[k.key]
            val where = "${k.file}:${k.line}"
            when {
                v == null -> problems.add("$where: нет перевода: ${k.key}")
                k.plural && (v !is List<*> || v.size != 2) -> problems.add("$where: для trn нужен список [one, other]: ${k.key}")
                else -> {
                    val forms = if (v is List<*>) v.map { it.toString() } else listOf(v.toString())
                    for (f in forms) {
                        if (cyr.containsMatchIn(f)) problems.add("$where: кириллица в переводе: $f")
                        val want = fields(k.key) - (if (k.plural) setOf("n") else emptySet())
                        val got = fields(f) - (if (k.plural) setOf("n") else emptySet())
                        if (want != got) problems.add("$where: подстановки $want ≠ $got: ${k.key} → $f")
                    }
                }
            }
        }
        val used = keys.map { it.key }.toSet()
        for (k in cat.keys) if (k !in used) problems.add("в словаре лишний ключ: $k")
        assertTrue(problems.joinToString("\n"), problems.isEmpty())
    }

    // ---------------------------------------------------------------- поведение

    @Test
    fun russianByDefault() {
        assertEquals("Частота", tr("Частота"))
        assertEquals("Найдено 3 места", tr("Найдено {n} места", "n" to 3))
        assertEquals("21 борт", trn(21, "{n} борт", "{n} борта", "{n} бортов"))
        assertEquals("3 борта", trn(3, "{n} борт", "{n} борта", "{n} бортов"))
        assertEquals("12 бортов", trn(12, "{n} борт", "{n} борта", "{n} бортов"))
        assertEquals("105,3", num(105.3))
        assertEquals("144,800", num(144.8, 3))
    }

    @Test
    fun englishWithSubstitutions() {
        I18n.set("en", I18n.parse("""{"Частота {f} МГц": "Frequency {f} MHz", "{n} борт": ["{n} aircraft", "{n} aircraft"],
            "{n} файл": ["{n} file", "{n} files"]}"""))
        assertEquals("Frequency 101.5 MHz", tr("Частота {f} МГц", "f" to num(101.5)))
        assertEquals("Нет перевода", tr("Нет перевода"))
        // текст из эфира с «{…}» не подставляется повторно; неизвестное имя остаётся как есть
        assertEquals("a={b} b=\$1", I18n.format("a={a} b={b}", arrayOf("a" to "{b}", "b" to "\$1")))
        assertEquals("{x}", I18n.format("{x}", arrayOf("n" to 1)))
        assertEquals("1 file", trn(1, "{n} файл", "{n} файла", "{n} файлов"))
        assertEquals("5 files", trn(5, "{n} файл", "{n} файла", "{n} файлов"))
        assertEquals("{n} files", tr("{n} файл"))
        assertEquals("105.3", num(105.3))
        assertEquals("-0.50", num(-0.5, 2))
    }

    @Test
    fun languageChoice() {
        assertEquals("ru", I18n.resolve("auto", Locale("ru", "RU")))
        assertEquals("ru", I18n.resolve("auto", Locale("kk")))
        assertEquals("en", I18n.resolve("auto", Locale("de", "DE")))
        assertEquals("en", I18n.resolve("en", Locale("ru")))
        assertEquals("ru", I18n.resolve("ru", Locale.US))
        assertEquals("en", I18n.resolve("xx", Locale.US))
    }

    @Test
    fun lexerFindsKeys() {
        val src = "val a = tr(\"Да\") // \"Комментарий\"\n/* \"Тоже\" */ val b = trn(x.size, \"{n} борт\",\n \"{n} борта\", \"{n} бортов\")\n" +
            "val c = \"${'$'}{tr(\"Нет\")} шт\"; val d = '\"'; val e = \"Мимо\""
        val lx = Lexer(src).run()
        assertEquals(listOf("Да", "{n} борт", "{n} борта", "{n} бортов", "\u0000 шт", "Нет", "Мимо"),
            lx.lits.sortedBy { it.start }.map { it.text })
    }
}
