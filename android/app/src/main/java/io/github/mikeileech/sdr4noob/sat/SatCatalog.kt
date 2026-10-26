package io.github.mikeileech.sdr4noob.sat

import io.github.mikeileech.sdr4noob.i18n.N_
import io.github.mikeileech.sdr4noob.i18n.tr

/**
 * Каталог спутников режима «Спутники» — перенос rtltv/satcat.py (данные как есть): метео, МКС, любительские FM,
 * SSB/CW-транспондеры, телеметрия и «Электро-Л» (геостационар).
 *
 * Частоты — по SatNOGS DB (db.satnogs.org, выгрузка 23.10.2026) и AMSAT. Статус — на октябрь 2026 г.
 * по данным SatNOGS (на орбите / сошёл) и сообщениям AMSAT: работа любительских аппаратов часто
 * зависит от освещённости и расписания операторов — «активен» не гарантирует сигнал на каждом витке.
 */

/** Передатчик спутника. [freq] — частота вниз (для транспондера — нижний край полосы), Гц. */
data class Tx(
    val label: String,
    val freq: Double,
    /** Режим демодуляции RadioDemod: NFM, USB, LSB, CW, AM; LRPT/LRIT — приём снимков. */
    val mode: String,
    /** Верхний край полосы транспондера, Гц. */
    val hi: Double = 0.0,
    /** Полоса канала, Гц (0 — по умолчанию режима). */
    val bw: Double = 0.0,
    val note: String = "",
    /** voice | lrpt | lrit | iq; пусто — по режиму. */
    val decoder: String = "",
) {
    val kind: String
        get() = when {
            decoder.isNotEmpty() -> decoder
            mode == "LRPT" -> "lrpt"
            mode == "LRIT" -> "lrit"
            mode in SatCatalog.LISTEN_MODES -> "voice"
            else -> "iq"
        }

    val listenable: Boolean get() = mode in SatCatalog.LISTEN_MODES
    val isBand: Boolean get() = hi > freq
    val center: Double get() = if (isBand) (freq + hi) / 2 else freq

    fun toMap(): Map<String, Any?> =
        linkedMapOf("label" to label, "freq" to freq, "mode" to mode, "hi" to hi, "bw" to bw, "note" to note,
            "decoder" to decoder)
}

data class Sat(
    val norad: Int,
    val name: String,
    /** Категория — ключ [SatCatalog.CATEGORIES]. */
    val cat: String,
    val color: String,
    val txs: List<Tx> = emptyList(),
    val status: String = N_("активен"),
    val note: String = "",
    /** Добавлен пользователем. */
    val user: Boolean = false,
    /** (строка 1, строка 2) — TLE, введённый вручную; пусто — по каталогу celestrak. */
    val tle: List<String> = emptyList(),
) {
    fun toMap(): Map<String, Any?> =
        linkedMapOf("norad" to norad, "name" to name, "cat" to cat, "color" to color, "status" to status,
            "note" to note, "tle" to tle.toList(), "txs" to txs.map { it.toMap() })

    companion object {
        private fun num(x: Any?): Double = when (x) {
            null -> 0.0
            is Number -> x.toDouble()
            is String -> x.trim().ifEmpty { "0" }.toDouble()
            else -> error(tr("не число: {x}", "x" to x))
        }

        private fun str(x: Any?, def: String): String = (x as? String)?.takeIf { it.isNotEmpty() } ?: def

        /** Из настроек (как Sat.from_dict): спутник пользователя. */
        fun fromMap(d: Map<String, Any?>): Sat {
            val norad = num(d["norad"] ?: error(tr("нет norad"))).toInt()
            @Suppress("UNCHECKED_CAST")
            val txs = (d["txs"] as? List<Map<String, Any?>> ?: emptyList()).map { t ->
                Tx((t["label"] as? String) ?: "", num(t["freq"] ?: error(tr("нет freq"))), (t["mode"] as? String) ?: "NFM",
                    num(t["hi"]), num(t["bw"]), (t["note"] as? String) ?: "", (t["decoder"] as? String) ?: "")
            }
            return Sat(norad, str(d["name"], "NORAD $norad"), (d["cat"] as? String) ?: "other",
                (d["color"] as? String) ?: "#e0e0e0", txs, (d["status"] as? String) ?: N_("добавлен вручную"),
                (d["note"] as? String) ?: "", true, (d["tle"] as? List<*>)?.map { it.toString() } ?: emptyList())
        }
    }
}

object SatCatalog {
    val CATEGORIES: Map<String, String> = linkedMapOf(
        "meteo" to N_("Метео"),
        "iss" to N_("МКС"),
        "fm" to N_("FM-ретрансляторы"),
        "ssb" to N_("SSB/CW-транспондеры"),
        "tlm" to N_("Телеметрия"),
        "other" to N_("Свои / другие"),
    )

    /** Режимы демодуляции для прослушивания (RadioDemod). */
    val LISTEN_MODES = listOf("NFM", "WFM", "AM", "USB", "LSB", "CW")

    /** Цифровые декодеры приёма. */
    val DECODERS: Map<String, String> = linkedMapOf(
        "voice" to N_("голос / звук"),
        "lrpt" to N_("LRPT («Метеор-М», снимки)"),
        "lrit" to N_("LRIT («Электро-Л», снимки)"),
        "iq" to N_("только приём и запись IQ"),
    )

    private fun mhz(x: Double) = x * 1e6

    val BUILTIN: List<Sat> = listOf(
        // --- метео
        Sat(57166, N_("Метеор-М №2-3"), "meteo", "#ffb84d",
            listOf(Tx(N_("LRPT (снимки)"), mhz(137.900), "LRPT", note = "72k OQPSK"),
                Tx(N_("LRPT, резервная частота"), mhz(137.100), "LRPT", note = "72k OQPSK")),
            N_("активен"), N_("антенна раскрылась не полностью — сигнал слабее №2-4")),
        Sat(59051, N_("Метеор-М №2-4"), "meteo", "#ff6a4d",
            listOf(Tx(N_("LRPT (снимки)"), mhz(137.900), "LRPT", note = "72k OQPSK"),
                Tx(N_("LRPT, резервная частота"), mhz(137.100), "LRPT", note = "72k OQPSK")), N_("активен")),
        // --- МКС
        Sat(25544, N_("МКС"), "iss", "#7fd1ff",
            listOf(Tx(N_("Голос / SSTV"), mhz(145.800), "NFM", bw = 15000.0, note = N_("SSTV — режим SSTV")),
                Tx("APRS", mhz(145.825), "NFM", bw = 15000.0, note = N_("пакеты AFSK 1200")),
                Tx(N_("Кросс-бэнд репитер"), mhz(437.800), "NFM", bw = 15000.0, note = N_("вверх 145,990 (67 Гц), при включении"))),
            N_("активна"), N_("SSTV/голос 145,800 МГц — режим SSTV")),
        // --- FM
        Sat(27607, "SO-50 (SaudiSat-1C)", "fm", "#9be37a",
            listOf(Tx(N_("FM-ретранслятор"), mhz(436.795), "NFM", bw = 15000.0, note = N_("вверх 145,850 (67 Гц)"))), N_("активен")),
        Sat(43678, "PO-101 (Diwata-2)", "fm", "#c9f27a",
            listOf(Tx(N_("FM-ретранслятор"), mhz(145.900), "NFM", bw = 15000.0, note = N_("вверх 437,500"))),
            N_("по расписанию"), N_("включают по объявлениям операторов")),
        Sat(43017, "AO-91 (Fox-1B)", "fm", "#7ae3c3",
            listOf(Tx(N_("FM-ретранслятор"), mhz(145.960), "NFM", bw = 15000.0, note = N_("вверх 435,250 (без тона)"))),
            N_("только на солнце"), N_("аккумулятор деградировал — работает на освещённой части витка")),
        Sat(61781, "AO-123 (ASRTU-1)", "fm", "#e3d77a",
            listOf(Tx(N_("FM-ретранслятор"), mhz(435.400), "NFM", bw = 15000.0, note = N_("вверх 145,850"))), N_("активен")),
        Sat(22825, "AO-27 (EYESAT-1)", "fm", "#a7a7a7",
            listOf(Tx(N_("FM-ретранслятор"), mhz(436.795), "NFM", bw = 15000.0, note = N_("вверх 145,850"))),
            N_("нерегулярно"), N_("аппарату 30+ лет, включается эпизодически")),
        // --- SSB/CW
        Sat(44909, "RS-44 (DOSAAF-85)", "ssb", "#f27ab8",
            listOf(Tx(N_("Транспондер (инверт.)"), mhz(435.610), "USB", hi = mhz(435.670), note = N_("вверх 145,935–145,995")),
                Tx(N_("CW-маяк"), mhz(435.605), "CW")), N_("активен")),
        Sat(24278, "FO-29 (JAS-2)", "ssb", "#d07af2",
            listOf(Tx(N_("Транспондер (инверт.)"), mhz(435.800), "USB", hi = mhz(435.900), note = N_("вверх 145,900–146,000")),
                Tx(N_("CW-маяк"), mhz(435.795), "CW")), N_("по расписанию"), N_("включает JARL, обычно по выходным")),
        Sat(39444, "AO-73 (FUNcube-1)", "ssb", "#7a9bf2",
            listOf(Tx(N_("Транспондер (инверт.)"), mhz(145.950), "USB", hi = mhz(145.970), note = N_("вверх 435,130–435,150")),
                Tx(N_("Телеметрия BPSK 1200"), mhz(145.935), "USB")),
            N_("в тени Земли"), N_("транспондер работает в тени, телеметрия — на солнце")),
        Sat(43803, "JO-97 (JY1Sat)", "ssb", "#b0b0f2",
            listOf(Tx(N_("Транспондер (инверт.)"), mhz(145.855), "USB", hi = mhz(145.875), note = N_("вверх 435,100–435,120")),
                Tx(N_("Телеметрия BPSK 1200"), mhz(145.840), "USB")), N_("нерегулярно")),
        // --- телеметрия
        Sat(40908, "LilacSat-2 (CAS-3H)", "tlm", "#f2c27a",
            listOf(Tx(N_("Телеметрия BPSK 9600 / CW"), mhz(437.200), "USB"), Tx(N_("Телеметрия GFSK 4800"), mhz(437.225), "NFM")),
            N_("активен")),
        Sat(43880, "UWE-4", "tlm", "#7af2e0",
            listOf(Tx(N_("Телеметрия FSK 9600"), mhz(435.600), "NFM", bw = 20000.0)), N_("активен")),
        Sat(59112, "SONATE-2", "tlm", "#f27a7a",
            listOf(Tx("SSTV (Martin M1)", mhz(145.880), "NFM", bw = 15000.0), Tx(N_("CW-маяк"), mhz(145.840), "CW"),
                Tx(N_("Телеметрия GMSK 9600"), mhz(437.025), "NFM", bw = 20000.0)), N_("активен")),
    )

    /** «Электро-Л» (геостационар, LRIT 1691 МГц): в каталоге для «Приёма со спутника», пролётов у них нет. */
    val GEO: List<Sat> = listOf(
        Sat(67756, N_("Электро-Л №5"), "meteo", "#ffd84d", listOf(Tx(N_("LRIT (снимки)"), mhz(1691.0), "LRIT", note = "BPSK 294k")),
            N_("активен"), N_("76° в. д.")),
        Sat(44903, N_("Электро-Л №3"), "meteo", "#ffd84d", listOf(Tx(N_("LRIT (снимки)"), mhz(1691.0), "LRIT", note = "BPSK 294k")),
            N_("перегоняется"), N_("с 76° в. д. на 14,5° з. д.")),
        Sat(55506, N_("Электро-Л №4"), "meteo", "#ffd84d", listOf(Tx(N_("LRIT (снимки)"), mhz(1691.0), "LRIT", note = "BPSK 294k")),
            N_("активен"), N_("165,8° в. д. — из России не виден")),
        Sat(41105, N_("Электро-Л №2"), "meteo", "#ffd84d", listOf(Tx(N_("LRIT (снимки)"), mhz(1691.0), "LRIT", note = "BPSK 294k")),
            N_("заменяется №3"), N_("14,5° з. д.")),
    )
    val GEO_NORADS: Set<Int> = GEO.map { it.norad }.toSet()

    /** Номинальные долготы «Электро-Л» (elektro.SATS): NORAD → (название, долгота °). */
    val ELEKTRO: Map<Int, Pair<String, Double>> = linkedMapOf(
        67756 to (N_("Электро-Л №5") to 76.0),
        44903 to (N_("Электро-Л №3") to -14.5),
        55506 to (N_("Электро-Л №4") to 165.8),
        41105 to (N_("Электро-Л №2") to -14.5),
    )

    /** «Метеоры» для автоприёма по расписанию (sat_window.METEORS). */
    val METEORS: Set<Int> = linkedSetOf(57166, 59051)

    @Volatile private var user: List<Sat> = emptyList()
    @Volatile private var catalog: List<Sat> = BUILTIN
    @Volatile private var byNorad: Map<Int, Sat> = emptyMap()

    init {
        rebuild()
    }

    private fun rebuild() {
        val builtin = BUILTIN.map { it.norad }.toSet()
        catalog = BUILTIN + user.filter { it.norad !in builtin }
        val m = LinkedHashMap<Int, Sat>()
        for (s in catalog + GEO) m[s.norad] = s
        byNorad = m
    }

    /** Свои спутники (Sat или словари из настроек) — добавляются в каталог; номера встроенных не заменяются. */
    @Synchronized
    fun setUser(sats: List<Any>) {
        @Suppress("UNCHECKED_CAST")
        user = sats.map { if (it is Sat) it else Sat.fromMap(it as Map<String, Any?>) }
        rebuild()
    }

    /** Каталог: встроенные + свои (без «Электро-Л»). */
    val CATALOG: List<Sat> get() = catalog
    val USER: List<Sat> get() = user
    val BY_NORAD: Map<Int, Sat> get() = byNorad

    /** Все спутники для выбора приёма: каталог + «Электро-Л». */
    fun allSats(): List<Sat> = catalog + GEO

    fun norads(cats: Collection<String>? = null): List<Int> =
        catalog.filter { cats == null || it.cat in cats }.map { it.norad }
}
