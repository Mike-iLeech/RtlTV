package io.github.mikeileech.sdr4noob.radio

import io.github.mikeileech.sdr4noob.i18n.N_
import io.github.mikeileech.sdr4noob.i18n.tr

/** Авиакомпания из офлайн-таблицы: название и страна (ISO-код). */
data class Airline(val name: String, val cc: String)

/** Блок 24-битных адресов ICAO: [start]..[end], страна (ISO-код и название). */
data class IcaoRange(val start: Int, val end: Int, val cc: String, val name: String)

/** Офлайн-карточка борта (часть словаря AircraftInfo.card(), которую можно определить без сети). */
data class AircraftCard(val hex: String, val callsign: String?, val cc: String?, val country: String?,
                        val airline: String?, val airlineCode: String?, val airlineCc: String?,
                        /** true — частный/служебный борт (позывной — регистрация), false — рейс, null — неизвестно. */
                        val isPrivate: Boolean?)

/**
 * Карточка борта, офлайн-часть — перенос rtltv/aircraft_info.py: страна регистрации по ICAO-адресу
 * (блоки Приложения 10 ИКАО, том III), авиакомпания по позывному (таблица основных), «похоже на регистрацию».
 * Онлайн-справочники (api.adsbdb.com, hexdb.io), их кэш на диске и флаги flagcdn.com не перенесены.
 */
object AircraftInfo {
    private fun R(a: Int, b: Int, cc: String, name: String) = IcaoRange(a, b, cc, name)

    /** Блоки 24-битных адресов ICAO (Приложение 10 ИКАО, том III), по возрастанию начала. */
    val ICAO_RANGES: List<IcaoRange> = listOf(
        R(0x004000, 0x0043FF, "zw", N_("Зимбабве")), R(0x006000, 0x006FFF, "mz", N_("Мозамбик")), R(0x008000, 0x00FFFF, "za", N_("ЮАР")),
        R(0x010000, 0x017FFF, "eg", N_("Египет")), R(0x018000, 0x01FFFF, "ly", N_("Ливия")), R(0x020000, 0x027FFF, "ma", N_("Марокко")),
        R(0x028000, 0x02FFFF, "tn", N_("Тунис")), R(0x030000, 0x0303FF, "bw", N_("Ботсвана")), R(0x032000, 0x032FFF, "bi", N_("Бурунди")),
        R(0x034000, 0x034FFF, "cm", N_("Камерун")), R(0x035000, 0x0353FF, "km", N_("Коморы")), R(0x036000, 0x036FFF, "cg", N_("Конго")),
        R(0x038000, 0x038FFF, "ci", N_("Кот-д'Ивуар")), R(0x03E000, 0x03EFFF, "ga", N_("Габон")), R(0x040000, 0x040FFF, "et", N_("Эфиопия")),
        R(0x042000, 0x042FFF, "gq", N_("Экваториальная Гвинея")), R(0x044000, 0x044FFF, "gh", N_("Гана")), R(0x046000, 0x046FFF, "gn", N_("Гвинея")),
        R(0x048000, 0x0483FF, "gw", N_("Гвинея-Бисау")), R(0x04A000, 0x04A3FF, "ls", N_("Лесото")), R(0x04C000, 0x04CFFF, "ke", N_("Кения")),
        R(0x050000, 0x050FFF, "lr", N_("Либерия")), R(0x054000, 0x054FFF, "mg", N_("Мадагаскар")), R(0x058000, 0x058FFF, "mw", N_("Малави")),
        R(0x05A000, 0x05A3FF, "mv", N_("Мальдивы")), R(0x05C000, 0x05CFFF, "ml", N_("Мали")), R(0x05E000, 0x05E3FF, "mr", N_("Мавритания")),
        R(0x060000, 0x0603FF, "mu", N_("Маврикий")), R(0x062000, 0x062FFF, "ne", N_("Нигер")), R(0x064000, 0x064FFF, "ng", N_("Нигерия")),
        R(0x068000, 0x068FFF, "ug", N_("Уганда")), R(0x06A000, 0x06A3FF, "qa", N_("Катар")), R(0x06C000, 0x06CFFF, "cf", N_("ЦАР")),
        R(0x06E000, 0x06EFFF, "rw", N_("Руанда")), R(0x070000, 0x070FFF, "sn", N_("Сенегал")), R(0x074000, 0x0743FF, "sc", N_("Сейшелы")),
        R(0x076000, 0x0763FF, "sl", N_("Сьерра-Леоне")), R(0x078000, 0x078FFF, "so", N_("Сомали")), R(0x07A000, 0x07A3FF, "sz", N_("Эсватини")),
        R(0x07C000, 0x07CFFF, "sd", N_("Судан")), R(0x080000, 0x080FFF, "tz", N_("Танзания")), R(0x084000, 0x084FFF, "td", N_("Чад")),
        R(0x088000, 0x088FFF, "tg", N_("Того")), R(0x08A000, 0x08AFFF, "zm", N_("Замбия")), R(0x08C000, 0x08CFFF, "cd", N_("ДР Конго")),
        R(0x090000, 0x090FFF, "ao", N_("Ангола")), R(0x094000, 0x0943FF, "bj", N_("Бенин")), R(0x096000, 0x0963FF, "cv", N_("Кабо-Верде")),
        R(0x098000, 0x0983FF, "dj", N_("Джибути")), R(0x09A000, 0x09AFFF, "gm", N_("Гамбия")), R(0x09C000, 0x09CFFF, "bf", N_("Буркина-Фасо")),
        R(0x09E000, 0x09E3FF, "st", N_("Сан-Томе и Принсипи")), R(0x0A0000, 0x0A7FFF, "dz", N_("Алжир")), R(0x0A8000, 0x0A8FFF, "bs", N_("Багамы")),
        R(0x0AA000, 0x0AA3FF, "bb", N_("Барбадос")), R(0x0AB000, 0x0AB3FF, "bz", N_("Белиз")), R(0x0AC000, 0x0ACFFF, "co", N_("Колумбия")),
        R(0x0AE000, 0x0AEFFF, "cr", N_("Коста-Рика")), R(0x0B0000, 0x0B0FFF, "cu", N_("Куба")), R(0x0B2000, 0x0B2FFF, "sv", N_("Сальвадор")),
        R(0x0B4000, 0x0B4FFF, "gt", N_("Гватемала")), R(0x0B6000, 0x0B6FFF, "gy", N_("Гайана")), R(0x0B8000, 0x0B8FFF, "ht", N_("Гаити")),
        R(0x0BA000, 0x0BAFFF, "hn", N_("Гондурас")), R(0x0BC000, 0x0BC3FF, "vc", N_("Сент-Винсент и Гренадины")), R(0x0BE000, 0x0BEFFF, "jm", N_("Ямайка")),
        R(0x0C0000, 0x0C0FFF, "ni", N_("Никарагуа")), R(0x0C2000, 0x0C2FFF, "pa", N_("Панама")), R(0x0C4000, 0x0C4FFF, "do", N_("Доминиканская Республика")),
        R(0x0C6000, 0x0C6FFF, "tt", N_("Тринидад и Тобаго")), R(0x0C8000, 0x0C8FFF, "sr", N_("Суринам")), R(0x0CA000, 0x0CA3FF, "ag", N_("Антигуа и Барбуда")),
        R(0x0CC000, 0x0CC3FF, "gd", N_("Гренада")), R(0x0D0000, 0x0D7FFF, "mx", N_("Мексика")), R(0x0D8000, 0x0DFFFF, "ve", N_("Венесуэла")),
        R(0x100000, 0x1FFFFF, "ru", N_("Россия")), R(0x201000, 0x2013FF, "na", N_("Намибия")), R(0x202000, 0x2023FF, "er", N_("Эритрея")),
        R(0x300000, 0x33FFFF, "it", N_("Италия")), R(0x340000, 0x37FFFF, "es", N_("Испания")), R(0x380000, 0x3BFFFF, "fr", N_("Франция")),
        R(0x3C0000, 0x3FFFFF, "de", N_("Германия")), R(0x400000, 0x43FFFF, "gb", N_("Великобритания")), R(0x440000, 0x447FFF, "at", N_("Австрия")),
        R(0x448000, 0x44FFFF, "be", N_("Бельгия")), R(0x450000, 0x457FFF, "bg", N_("Болгария")), R(0x458000, 0x45FFFF, "dk", N_("Дания")),
        R(0x460000, 0x467FFF, "fi", N_("Финляндия")), R(0x468000, 0x46FFFF, "gr", N_("Греция")), R(0x470000, 0x477FFF, "hu", N_("Венгрия")),
        R(0x478000, 0x47FFFF, "no", N_("Норвегия")), R(0x480000, 0x487FFF, "nl", N_("Нидерланды")), R(0x488000, 0x48FFFF, "pl", N_("Польша")),
        R(0x490000, 0x497FFF, "pt", N_("Португалия")), R(0x498000, 0x49FFFF, "cz", N_("Чехия")), R(0x4A0000, 0x4A7FFF, "ro", N_("Румыния")),
        R(0x4A8000, 0x4AFFFF, "se", N_("Швеция")), R(0x4B0000, 0x4B7FFF, "ch", N_("Швейцария")), R(0x4B8000, 0x4BFFFF, "tr", N_("Турция")),
        R(0x4C0000, 0x4C7FFF, "rs", N_("Сербия")), R(0x4C8000, 0x4C83FF, "cy", N_("Кипр")), R(0x4CA000, 0x4CAFFF, "ie", N_("Ирландия")),
        R(0x4CC000, 0x4CCFFF, "is", N_("Исландия")), R(0x4D0000, 0x4D03FF, "lu", N_("Люксембург")), R(0x4D2000, 0x4D23FF, "mt", N_("Мальта")),
        R(0x4D4000, 0x4D43FF, "mc", N_("Монако")), R(0x500000, 0x5003FF, "sm", N_("Сан-Марино")), R(0x501000, 0x5013FF, "al", N_("Албания")),
        R(0x501C00, 0x501FFF, "hr", N_("Хорватия")), R(0x502C00, 0x502FFF, "lv", N_("Латвия")), R(0x503C00, 0x503FFF, "lt", N_("Литва")),
        R(0x504C00, 0x504FFF, "md", N_("Молдова")), R(0x505C00, 0x505FFF, "sk", N_("Словакия")), R(0x506C00, 0x506FFF, "si", N_("Словения")),
        R(0x507C00, 0x507FFF, "uz", N_("Узбекистан")), R(0x508000, 0x50FFFF, "ua", N_("Украина")), R(0x510000, 0x5103FF, "by", N_("Беларусь")),
        R(0x511000, 0x5113FF, "ee", N_("Эстония")), R(0x512000, 0x5123FF, "mk", N_("Северная Македония")), R(0x513000, 0x5133FF, "ba", N_("Босния и Герцеговина")),
        R(0x514000, 0x5143FF, "ge", N_("Грузия")), R(0x515000, 0x5153FF, "tj", N_("Таджикистан")), R(0x516000, 0x5163FF, "me", N_("Черногория")),
        R(0x600000, 0x6003FF, "am", N_("Армения")), R(0x600800, 0x600BFF, "az", N_("Азербайджан")), R(0x601000, 0x6013FF, "kg", N_("Киргизия")),
        R(0x601800, 0x601BFF, "tm", N_("Туркменистан")), R(0x680000, 0x6803FF, "bt", N_("Бутан")), R(0x681000, 0x6813FF, "fm", N_("Микронезия")),
        R(0x682000, 0x6823FF, "mn", N_("Монголия")), R(0x683000, 0x6833FF, "kz", N_("Казахстан")), R(0x684000, 0x6843FF, "pw", N_("Палау")),
        R(0x700000, 0x700FFF, "af", N_("Афганистан")), R(0x702000, 0x702FFF, "bd", N_("Бангладеш")), R(0x704000, 0x704FFF, "mm", N_("Мьянма")),
        R(0x706000, 0x706FFF, "kw", N_("Кувейт")), R(0x708000, 0x708FFF, "la", N_("Лаос")), R(0x70A000, 0x70AFFF, "np", N_("Непал")),
        R(0x70C000, 0x70C3FF, "om", N_("Оман")), R(0x70E000, 0x70EFFF, "kh", N_("Камбоджа")), R(0x710000, 0x717FFF, "sa", N_("Саудовская Аравия")),
        R(0x718000, 0x71FFFF, "kr", N_("Республика Корея")), R(0x720000, 0x727FFF, "kp", N_("КНДР")), R(0x728000, 0x72FFFF, "iq", N_("Ирак")),
        R(0x730000, 0x737FFF, "ir", N_("Иран")), R(0x738000, 0x73FFFF, "il", N_("Израиль")), R(0x740000, 0x747FFF, "jo", N_("Иордания")),
        R(0x748000, 0x74FFFF, "lb", N_("Ливан")), R(0x750000, 0x757FFF, "my", N_("Малайзия")), R(0x758000, 0x75FFFF, "ph", N_("Филиппины")),
        R(0x760000, 0x767FFF, "pk", N_("Пакистан")), R(0x768000, 0x76FFFF, "sg", N_("Сингапур")), R(0x770000, 0x777FFF, "lk", N_("Шри-Ланка")),
        R(0x778000, 0x77FFFF, "sy", N_("Сирия")), R(0x780000, 0x7BFFFF, "cn", N_("Китай")), R(0x7C0000, 0x7FFFFF, "au", N_("Австралия")),
        R(0x800000, 0x83FFFF, "in", N_("Индия")), R(0x840000, 0x87FFFF, "jp", N_("Япония")), R(0x880000, 0x887FFF, "th", N_("Таиланд")),
        R(0x888000, 0x88FFFF, "vn", N_("Вьетнам")), R(0x890000, 0x890FFF, "ye", N_("Йемен")), R(0x894000, 0x894FFF, "bh", N_("Бахрейн")),
        R(0x895000, 0x8953FF, "bn", N_("Бруней")), R(0x896000, 0x896FFF, "ae", N_("ОАЭ")), R(0x897000, 0x8973FF, "sb", N_("Соломоновы Острова")),
        R(0x898000, 0x898FFF, "pg", N_("Папуа — Новая Гвинея")), R(0x899000, 0x8993FF, "tw", N_("Тайвань")), R(0x8A0000, 0x8A7FFF, "id", N_("Индонезия")),
        R(0x900000, 0x9003FF, "mh", N_("Маршалловы Острова")), R(0x901000, 0x9013FF, "ck", N_("Острова Кука")), R(0x902000, 0x9023FF, "ws", N_("Самоа")),
        R(0xA00000, 0xAFFFFF, "us", N_("США")), R(0xC00000, 0xC3FFFF, "ca", N_("Канада")), R(0xC80000, 0xC87FFF, "nz", N_("Новая Зеландия")),
        R(0xC88000, 0xC88FFF, "fj", N_("Фиджи")), R(0xC8A000, 0xC8A3FF, "nr", N_("Науру")), R(0xC8C000, 0xC8C3FF, "lc", N_("Сент-Люсия")),
        R(0xC8D000, 0xC8D3FF, "to", N_("Тонга")), R(0xC8E000, 0xC8E3FF, "ki", N_("Кирибати")), R(0xC90000, 0xC903FF, "vu", N_("Вануату")),
        R(0xE00000, 0xE3FFFF, "ar", N_("Аргентина")), R(0xE40000, 0xE7FFFF, "br", N_("Бразилия")), R(0xE80000, 0xE80FFF, "cl", N_("Чили")),
        R(0xE84000, 0xE84FFF, "ec", N_("Эквадор")), R(0xE88000, 0xE88FFF, "py", N_("Парагвай")), R(0xE8C000, 0xE8CFFF, "pe", N_("Перу")),
        R(0xE90000, 0xE90FFF, "uy", N_("Уругвай")), R(0xE94000, 0xE94FFF, "bo", N_("Боливия")),
    ).sortedBy { it.start }
    private val starts = IntArray(ICAO_RANGES.size) { ICAO_RANGES[it].start }

    /** ICAO-коды основных авиакомпаний (офлайн): код → (название, страна ISO). */
    val AIRLINES: Map<String, Airline> = linkedMapOf(
        "AFL" to Airline(N_("Аэрофлот"), "ru"), "SBI" to Airline("S7 Airlines", "ru"), "SDM" to Airline(N_("Россия"), "ru"),
        "UTA" to Airline(N_("ЮТэйр"), "ru"), "PBD" to Airline(N_("Победа"), "ru"), "SVR" to Airline(N_("Уральские авиалинии"), "ru"),
        "NWS" to Airline(N_("Northwind (Северный ветер)"), "ru"), "AZV" to Airline(N_("Азимут"), "ru"), "RWZ" to Airline("Red Wings", "ru"),
        "AUL" to Airline("Smartavia", "ru"), "KAR" to Airline(N_("Pegas Fly (Икар)"), "ru"), "SYL" to Airline(N_("Якутия"), "ru"),
        "GZP" to Airline(N_("Газпром авиа"), "ru"), "TYA" to Airline(N_("Нордстар"), "ru"), "DRU" to Airline(N_("Алроса"), "ru"),
        "RLU" to Airline(N_("Руслайн"), "ru"), "VDA" to Airline(N_("Волга-Днепр"), "ru"), "ABW" to Airline("AirBridgeCargo", "ru"),
        "BRU" to Airline(N_("Белавиа"), "by"), "UZB" to Airline("Uzbekistan Airways", "uz"), "KZR" to Airline("Air Astana", "kz"),
        "SMR" to Airline("Somon Air", "tj"), "TZK" to Airline("Tajik Air", "tj"), "AHY" to Airline("Azerbaijan Airlines", "az"),
        "TGZ" to Airline("Georgian Airways", "ge"), "MGL" to Airline("MIAT Mongolian Airlines", "mn"), "THY" to Airline("Turkish Airlines", "tr"),
        "PGT" to Airline("Pegasus", "tr"), "SXS" to Airline("SunExpress", "tr"), "UAE" to Airline("Emirates", "ae"),
        "FDB" to Airline("flydubai", "ae"), "ETD" to Airline("Etihad", "ae"), "ABY" to Airline("Air Arabia", "ae"),
        "QTR" to Airline("Qatar Airways", "qa"), "SVA" to Airline("Saudia", "sa"), "GFA" to Airline("Gulf Air", "bh"),
        "OMA" to Airline("Oman Air", "om"), "KAC" to Airline("Kuwait Airways", "kw"), "IRA" to Airline("Iran Air", "ir"),
        "IRM" to Airline("Mahan Air", "ir"), "IAW" to Airline("Iraqi Airways", "iq"), "RJA" to Airline("Royal Jordanian", "jo"),
        "MEA" to Airline("Middle East Airlines", "lb"), "ELY" to Airline("El Al", "il"), "MSR" to Airline("EgyptAir", "eg"),
        "ETH" to Airline("Ethiopian Airlines", "et"), "CCA" to Airline("Air China", "cn"), "CES" to Airline("China Eastern", "cn"),
        "CSN" to Airline("China Southern", "cn"), "CHH" to Airline("Hainan Airlines", "cn"), "CXA" to Airline("Xiamen Airlines", "cn"),
        "CPA" to Airline("Cathay Pacific", "hk"), "EVA" to Airline("EVA Air", "tw"), "KAL" to Airline("Korean Air", "kr"),
        "AAR" to Airline("Asiana", "kr"), "JAL" to Airline("Japan Airlines", "jp"), "ANA" to Airline("All Nippon Airways", "jp"),
        "THA" to Airline("Thai Airways", "th"), "HVN" to Airline("Vietnam Airlines", "vn"), "VJC" to Airline("VietJet", "vn"),
        "SIA" to Airline("Singapore Airlines", "sg"), "AIC" to Airline("Air India", "in"), "IGO" to Airline("IndiGo", "in"),
        "PIA" to Airline("Pakistan International", "pk"), "QFA" to Airline("Qantas", "au"), "KLM" to Airline("KLM", "nl"),
        "DLH" to Airline("Lufthansa", "de"), "AFR" to Airline("Air France", "fr"), "BAW" to Airline("British Airways", "gb"),
        "SWR" to Airline("Swiss", "ch"), "AUA" to Airline("Austrian", "at"), "ITY" to Airline("ITA Airways", "it"),
        "RYR" to Airline("Ryanair", "ie"), "EZY" to Airline("easyJet", "gb"), "WZZ" to Airline("Wizz Air", "hu"),
        "SAS" to Airline("SAS", "se"), "FIN" to Airline("Finnair", "fi"), "LOT" to Airline("LOT", "pl"),
        "CSA" to Airline("Czech Airlines", "cz"), "TAP" to Airline("TAP Air Portugal", "pt"), "IBE" to Airline("Iberia", "es"),
        "VLG" to Airline("Vueling", "es"), "NAX" to Airline("Norwegian", "no"), "ICE" to Airline("Icelandair", "is"),
        "EIN" to Airline("Aer Lingus", "ie"), "BEL" to Airline("Brussels Airlines", "be"), "AEE" to Airline("Aegean", "gr"),
        "UAL" to Airline("United Airlines", "us"), "AAL" to Airline("American Airlines", "us"), "DAL" to Airline("Delta", "us"),
        "ACA" to Airline("Air Canada", "ca"),
    )

    private val AIRLINE_CS = Regex("^([A-Z]{3})([0-9][0-9A-Z]{0,4})$")
    private val IATA_CS = Regex("^[A-Z0-9]{2}[0-9]{1,4}[A-Z]?$")   // рейс с IATA-кодом (FV6555, SU1124) — не частный
    private val REG_CS = Regex("^(RA|RF|EW|UR|UK|UP|4K|4L|EK|EX|EY|EZ|[A-Z])-?[0-9A-Z]{2,6}$")

    /** 24-битный адрес → (iso, страна) или (null, null). */
    fun countryByIcao(icao: Int): Pair<String?, String?> {
        // bisect_right(starts, icao) - 1
        var lo = 0; var hi = starts.size
        while (lo < hi) { val mid = (lo + hi) ushr 1; if (icao < starts[mid]) hi = mid else lo = mid + 1 }
        val i = lo - 1
        if (i >= 0) {
            val r = ICAO_RANGES[i]
            if (icao in r.start..r.end) return r.cc to r.name
        }
        return null to null
    }

    /** Позывной → (код ИКАО, название|null, страна|null) для формата «ААА123»; иначе null. */
    fun airlineByCallsign(cs0: String?): Triple<String, String?, String?>? {
        val cs = (cs0 ?: "").trim().uppercase()
        val m = AIRLINE_CS.matchEntire(cs) ?: return null
        val code = m.groupValues[1]
        val al = AIRLINES[code]
        return Triple(code, al?.name, al?.cc)
    }

    fun looksLikeRegistration(cs0: String?): Boolean {
        val cs = (cs0 ?: "").trim().uppercase()
        return cs.isNotEmpty() && !AIRLINE_CS.matches(cs) && !IATA_CS.matches(cs) && REG_CS.matches(cs)
    }

    /**
     * Сводная карточка без сети и кэша (как AircraftInfo(online=False).card() с пустым кэшем).
     * Названия страны и авиакомпании — для показа, уже переведены на язык интерфейса (в таблицах — русские, N_).
     */
    fun card(icao: Int, callsign: String?): AircraftCard {
        val hx = "%06X".format(icao)
        val cs = callsign?.trim()?.uppercase()?.ifEmpty { null }
        val (cc, country) = countryByIcao(icao)
        var airline: String? = null
        var code: String? = null
        var acc: String? = null
        var priv: Boolean? = null
        val al = airlineByCallsign(cs)
        if (al != null) {
            code = al.first; airline = al.second; acc = al.third
            priv = false
        } else if (cs != null) {
            // позывной не в формате авиакомпании: обычно это регистрация — частный/служебный борт
            priv = if (looksLikeRegistration(cs)) true else null
        }
        return AircraftCard(hx, cs, cc, country?.let { tr(it) }, airline?.let { tr(it) }, code, acc, priv)
    }
}
