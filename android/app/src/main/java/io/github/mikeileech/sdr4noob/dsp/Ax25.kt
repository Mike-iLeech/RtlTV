package io.github.mikeileech.sdr4noob.dsp

import java.nio.ByteBuffer
import java.nio.charset.CharacterCodingException
import java.nio.charset.Charset
import java.nio.charset.CodingErrorAction

/**
 * AX.25 и FCS — перенос rtltv/aprs.py (разделы «CRC / FCS» и «AX.25»): CRC-16-CCITT (X.25),
 * исправление 1–2 соседних битов по синдрому, адреса, UI-кадры, строки TNC2.
 * Кадры — байты без FCS (как в Python), поле info — байты.
 */
data class Ax25Frame(
    val src: String,
    val dest: String,
    /** digipeater'ы: (позывной, использован — бит H) */
    val path: List<Pair<String, Boolean>>,
    val control: Int,
    val pid: Int?,
    val info: ByteArray,
) {
    override fun equals(other: Any?) = other is Ax25Frame && src == other.src && dest == other.dest &&
        path == other.path && control == other.control && pid == other.pid && info.contentEquals(other.info)
    override fun hashCode() = ((src.hashCode() * 31 + dest.hashCode()) * 31 + path.hashCode()) * 31 + info.contentHashCode()
}

object Ax25 {
    const val GOOD_RESIDUE = 0xF0B8   // остаток CRC по кадру вместе с правильной FCS
    const val MAX_FRAME = 340         // байт: 10 адресов + упр. + PID + 256 инфо + FCS — с запасом
    const val MIN_FRAME = 18          // 2 адреса + упр. + PID + FCS

    private val CRC_TAB = IntArray(256) { i ->
        var c = i
        repeat(8) { c = if (c and 1 != 0) (c ushr 1) xor 0x8408 else c ushr 1 }
        c
    }

    fun crcRun(data: ByteArray, crc0: Int = 0xFFFF, len: Int = data.size): Int {
        var crc = crc0
        for (i in 0 until len) crc = (crc ushr 8) xor CRC_TAB[(crc xor data[i].toInt()) and 0xFF]
        return crc
    }

    /** FCS кадра AX.25 (CRC-16-CCITT, X.25): передаётся младшим байтом вперёд. */
    fun fcs(data: ByteArray) = crcRun(data) xor 0xFFFF

    fun checkFcs(frame: ByteArray) = frame.size >= 3 && crcRun(frame) == GOOD_RESIDUE

    // синдромы одиночной и двойной (соседние биты) ошибки → позиция с конца (первое вхождение, как setdefault)
    private val SYN: Pair<Map<Int, Int>, Map<Int, Int>> by lazy {
        val s = IntArray(MAX_FRAME * 8 + 2)
        var v = 1
        for (k in 1 until s.size) {
            v = if (v and 1 != 0) (v ushr 1) xor 0x8408 else v ushr 1
            s[k] = v
        }
        val one = HashMap<Int, Int>(); val two = HashMap<Int, Int>()
        for (k in 1 until s.size) {
            one.putIfAbsent(s[k], k)
            if (k >= 2) two.putIfAbsent(s[k] xor s[k - 1], k)
        }
        one to two
    }

    /**
     * Кадр с FCS, где CRC не сошёлся: (исправленный кадр, число исправленных битов) или (null, 0).
     * CRC линеен: инверсия бита i меняет остаток на S[n-i], поэтому позицию ищем по таблице.
     */
    fun tryFix(frame: ByteArray): Pair<ByteArray?, Int> {
        val n = frame.size * 8
        val syn = crcRun(frame) xor GOOD_RESIDUE
        val (one, two) = SYN
        var k = one[syn]
        val flips: IntArray
        if (k != null && k <= n) {
            flips = intArrayOf(n - k)
        } else {
            k = two[syn]
            if (k == null || k > n) return null to 0
            flips = intArrayOf(n - k, n - k + 1)
        }
        val fr = frame.copyOf()
        for (i in flips) fr[i / 8] = (fr[i / 8].toInt() xor (1 shl (i % 8))).toByte()
        if (!checkFcs(fr)) return null to 0
        return fr to flips.size
    }

    /** 7 байт адреса → (позывной-SSID, бит H/C, последний ли) или null, если это не позывной. */
    private fun addrDecode(d: ByteArray, off: Int): Triple<String, Boolean, Boolean>? {
        val sb = StringBuilder()
        for (j in 0 until 6) {
            val c = d[off + j].toInt() and 0xFF
            if (c and 1 != 0) return null
            val ch = c ushr 1
            sb.append(if (ch < 128) ch.toChar() else '�')
        }
        val s = sb.toString().trimEnd(' ')
        if (s.isEmpty() || !s.all { it in 'A'..'Z' || it in '0'..'9' }) return null
        val b6 = d[off + 6].toInt() and 0xFF
        val ssid = (b6 ushr 1) and 0x0F
        val call = s + (if (ssid != 0) "-$ssid" else "")
        return Triple(call, b6 and 0x80 != 0, b6 and 1 != 0)
    }

    /** «R2ABC-7*» → («R2ABC», 7); SSID ограничивается 0…15, нечисловой — 0. */
    fun splitCall(call: String): Pair<String, Int> {
        val c = call.trimEnd('*').uppercase()
        val k = c.indexOf('-')
        val base = if (k >= 0) c.substring(0, k) else c
        val ss = if (k >= 0) c.substring(k + 1) else ""
        val ssid = if (ss.isEmpty()) 0 else (PyNum.int(ss)?.toInt() ?: 0)
        return base to ssid.coerceIn(0, 15)
    }

    private fun addrEncode(call: String, last: Boolean = false, hbit: Boolean = false): ByteArray {
        val (b, ssid) = splitCall(call)
        val base = (b + "      ").substring(0, 6)
        val out = ByteArray(7)
        for (j in 0 until 6) out[j] = ((base[j].code shl 1) and 0xFE).toByte()
        out[6] = (0x60 or (ssid shl 1) or (if (hbit) 0x80 else 0) or (if (last) 1 else 0)).toByte()
        return out
    }

    /** Кадр без FCS → Ax25Frame или null. */
    fun decode(data: ByteArray): Ax25Frame? {
        val addrs = ArrayList<Triple<String, Boolean, Boolean>>()
        var i = 0
        val n = data.size
        while (true) {
            if (i + 7 > n) return null
            val a = addrDecode(data, i) ?: return null
            addrs.add(a)
            i += 7
            if (a.third) break
            if (addrs.size >= 10) return null
        }
        if (addrs.size < 2 || i >= n) return null
        val control = data[i].toInt() and 0xFF
        var pid: Int? = null
        var info = ByteArray(0)
        if (control and 0xEF == 0x03) {           // UI (с битом P/F или без)
            if (i + 1 >= n) return null
            pid = data[i + 1].toInt() and 0xFF
            info = data.copyOfRange(i + 2, n)
        }
        return Ax25Frame(addrs[1].first, addrs[0].first, addrs.drop(2).map { it.first to it.second }, control, pid, info)
    }

    fun isAprs(fr: Ax25Frame?) = fr != null && fr.control and 0xEF == 0x03 && fr.pid == 0xF0

    /** UI-кадр AX.25 без FCS. path — позывные, «*» в конце = уже повторён (бит H). */
    fun makeFrame(src: String, dest: String, path: List<String> = emptyList(), info: ByteArray = ByteArray(0)): ByteArray {
        val p = path.filter { it.isNotEmpty() }
        val out = java.io.ByteArrayOutputStream()
        out.write(addrEncode(dest, last = false, hbit = true))
        out.write(addrEncode(src, last = p.isEmpty()))
        for ((k, c) in p.withIndex()) out.write(addrEncode(c, last = k == p.size - 1, hbit = c.endsWith("*")))
        out.write(0x03); out.write(0xF0)
        out.write(info)
        return out.toByteArray()
    }

    fun makeFrame(src: String, dest: String, path: List<String>, info: String) =
        makeFrame(src, dest, path, info.toByteArray(Charsets.UTF_8))

    fun addFcs(frame: ByteArray): ByteArray {
        val f = fcs(frame)
        return frame + byteArrayOf((f and 0xFF).toByte(), (f ushr 8).toByte())
    }

    /** «SRC>DEST,PATH:info» → Ax25Frame (info — байты) или null. */
    fun parseTnc2(text: String): Ax25Frame? {
        val c = text.indexOf(':')
        if (c < 0) return null
        val head = text.substring(0, c)
        val info = text.substring(c + 1)
        val g = head.indexOf('>')
        if (g < 0) return null
        val src = head.substring(0, g)
        val parts = head.substring(g + 1).split(",")
        val raw = parts.drop(1).filter { it.isNotEmpty() }.map { it.trimEnd('*') to it.endsWith("*") }
        // в TNC2 «*» стоит только у последнего повторившего — все до него тоже использованы
        val lastUsed = raw.indices.lastOrNull { raw[it].second } ?: -1
        val path = raw.mapIndexed { k, (cl, _) -> cl to (k <= lastUsed) }
        val infoB = if (info.all { it.code <= 0xFF }) info.toByteArray(Charsets.ISO_8859_1) else info.toByteArray(Charsets.UTF_8)
        return Ax25Frame(PyStr.strip(src).uppercase(), PyStr.strip(parts[0]).uppercase(), path, 0x03, 0xF0, infoB)
    }

    fun parseTnc2(bytes: ByteArray) = parseTnc2(String(bytes, Charsets.ISO_8859_1))

    /** TNC2-строка → байты кадра без FCS (для кодера). */
    fun tnc2Frame(text: String): ByteArray {
        val fr = parseTnc2(text)!!
        return makeFrame(fr.src, fr.dest, fr.path.map { (c, h) -> c + if (h) "*" else "" }, fr.info)
    }

    private val CP1251: Charset = Charset.forName("windows-1251")

    private fun strictDecode(b: ByteArray, cs: Charset): String? = try {
        cs.newDecoder().onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT)
            .decode(ByteBuffer.wrap(b)).toString()
    } catch (e: CharacterCodingException) { null }

    /** Байты комментария: UTF-8, иначе cp1251 (так шлют многие станции в России), иначе latin-1. */
    fun smartText(b: ByteArray): String =
        strictDecode(b, Charsets.UTF_8) ?: strictDecode(b, CP1251) ?: String(b, Charsets.ISO_8859_1)

    /** Строка (символы ≤ 0xFF, как после latin-1) → smartText; символы выше 0xFF заменяются «?». */
    fun smartText(s: String): String = smartText(s.toByteArray(Charsets.ISO_8859_1))

    /** Кадр → строка TNC2 (звёздочка у последнего повторившего digipeater). */
    fun tnc2(fr: Ax25Frame): String {
        val last = fr.path.indices.lastOrNull { fr.path[it].second } ?: -1
        val sb = StringBuilder()
        sb.append(fr.src).append('>').append(fr.dest)
        for ((k, p) in fr.path.withIndex()) sb.append(',').append(p.first).append(if (k == last) "*" else "")
        sb.append(':')
        for (ch in smartText(fr.info)) {
            if (ch >= ' ' || ch == '\t') sb.append(ch) else sb.append("<0x%02x>".format(ch.code))
        }
        return sb.toString()
    }
}

/** Строковые операции с семантикой Python (str.strip/isspace). */
object PyStr {
    fun isSpace(c: Char) = when (c) {
        ' ', '\t', '\n', '\u000B', '\u000C', '\r', '\u001C', '\u001D', '\u001E', '\u001F', '\u0085', ' ',
        ' ', ' ', ' ', ' ', ' ', '　' -> true
        else -> c in ' '..' '
    }

    fun strip(s: String) = s.trim { isSpace(it) }

    /** Пробелы вокруг числа для int()/float(): как isSpace, но без \x1c–\x1f (их Python там не снимает). */
    fun stripNum(s: String) = s.trim { isSpace(it) && it !in '\u001C'..'\u001F' }
    fun rstrip(s: String) = s.trimEnd { isSpace(it) }
    /** str.isdigit(): кроме 0–9 истинно и для надстрочных ¹²³ (в latin-1 других «цифр» нет). */
    fun isDigits(s: String) = s.isNotEmpty() && s.all { it in '0'..'9' || it == '²' || it == '³' || it == '¹' ||
        (it.code > 0xFF && Character.isDigit(it)) }
}

/** Разбор и форматирование чисел как в Python: int(), float(), round(), f"{:.Nf}", f"{:g}", %. */
object PyNum {
    private val INT_RE = Regex("[+-]?[0-9](?:_?[0-9])*")
    private val DIG = "[0-9](?:_?[0-9])*"
    private val FLOAT_RE = Regex("[+-]?(?:(?:$DIG(?:\\.(?:$DIG)?)?|\\.$DIG)(?:[eE][+-]?$DIG)?|inf|infinity|nan)",
        RegexOption.IGNORE_CASE)

    /** int(s) для строк из ASCII-цифр; null вместо ValueError; огромные числа насыщаются до ±Long.MAX_VALUE. */
    fun int(s: String): Long? {
        val t = PyStr.stripNum(s)
        if (!INT_RE.matches(t)) return null
        val v = t.replace("_", "").toBigInteger()
        return if (v.bitLength() < 63) v.toLong() else if (v.signum() < 0) -Long.MAX_VALUE else Long.MAX_VALUE
    }

    fun float(s: String): Double? {
        val t = PyStr.stripNum(s)
        if (!FLOAT_RE.matches(t)) return null
        val u = t.replace("_", "").lowercase()
        val neg = u.startsWith("-")
        val body = u.trimStart('+', '-')
        return when (body) {
            "inf", "infinity" -> if (neg) Double.NEGATIVE_INFINITY else Double.POSITIVE_INFINITY
            "nan" -> Double.NaN
            else -> u.toDouble()
        }
    }

    /** a % b для чисел с плавающей точкой (знак делителя). */
    fun mod(a: Double, b: Double): Double {
        var m = a % b
        if (m != 0.0) {
            if ((b < 0) != (m < 0)) m += b
        } else {
            m = if (b < 0) -0.0 else 0.0
        }
        return m
    }

    /** round(x, nd) — точное значение double, банковское округление половин. */
    fun round(x: Double, nd: Int): Double {
        if (x.isNaN() || x.isInfinite()) return x
        return java.math.BigDecimal(x).setScale(nd, java.math.RoundingMode.HALF_EVEN).toDouble()
    }

    /** f"{v:.{nd}f}" */
    fun fixed(v: Double, nd: Int): String {
        if (v.isNaN()) return "nan"
        if (v.isInfinite()) return if (v > 0) "inf" else "-inf"
        val bd = java.math.BigDecimal(v).setScale(nd, java.math.RoundingMode.HALF_EVEN)
        val s = bd.abs().toPlainString()
        val neg = v < 0 || (v == 0.0 && 1.0 / v < 0)
        return if (neg) "-$s" else s
    }

    /** f"{v:g}" (6 значащих цифр). */
    fun g(v: Double): String {
        if (v.isNaN()) return "nan"
        if (v.isInfinite()) return if (v > 0) "inf" else "-inf"
        val neg = v < 0 || (v == 0.0 && 1.0 / v < 0)
        val sign = if (neg) "-" else ""
        if (v == 0.0) return sign + "0"
        val bd = java.math.BigDecimal(v).abs().round(java.math.MathContext(6, java.math.RoundingMode.HALF_EVEN))
        val exp = bd.precision() - bd.scale() - 1
        return if (exp >= -4 && exp < 6) {
            var s = bd.setScale(maxOf(0, 5 - exp), java.math.RoundingMode.HALF_EVEN).toPlainString()
            if (s.contains('.')) s = s.trimEnd('0').trimEnd('.')
            sign + s
        } else {
            val digits = bd.unscaledValue().toString().trimEnd('0').ifEmpty { "0" }
            val mant = if (digits.length > 1) digits[0] + "." + digits.substring(1) else digits
            val e = if (exp < 0) "-" + (-exp).toString().padStart(2, '0') else "+" + exp.toString().padStart(2, '0')
            sign + mant + "e" + e
        }
    }
}
