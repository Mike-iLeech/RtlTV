package io.github.mikeileech.sdr4noob.sat

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.pow
import kotlin.math.round
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * SGP4/SDP4 (Vallado, Crawford, Hujsak, Kelso 2006, ред. 2020) — перенос пакета Python sgp4 (Satrec, тот, что
 * использует rtltv/satpass.py): константы WGS-72, режим 'i' (improved), полная ветка глубокого космоса (SDP4:
 * лунно-солнечные возмущения, резонансы суточный и полусуточный) — нужна для «Электро-Л» (геостационар).
 *
 * Остаток от деления — `%` Kotlin, то есть fmod из C, как в C++-ядре пакета sgp4 (ускоренный Satrec).
 * Результат [propagate] — положение (км) и скорость (км/с) в системе TEME.
 */
class Sgp4 private constructor() {
    // ---------------------------------------------------------------- элементы TLE
    var satnum = ""; private set
    var epochYr = 0; private set              // две цифры года, как у Satrec.epochyr
    var epochDays = 0.0; private set
    /** Юлианская дата эпохи: целая часть (…,5) и доля суток (округлена до 8 знаков, как в TLE). */
    var jdSatEpoch = 0.0; private set
    var jdSatEpochF = 0.0; private set
    var bstar = 0.0; private set
    var ndot = 0.0; private set
    var nddot = 0.0; private set
    var ecco = 0.0; private set
    var argpo = 0.0; private set
    var inclo = 0.0; private set
    var mo = 0.0; private set
    /** Среднее движение из TLE (Козаи), рад/мин. */
    var noKozai = 0.0; private set
    var noUnkozai = 0.0; private set
    var nodeo = 0.0; private set
    /** 'n' — околоземная (SGP4), 'd' — глубокий космос (SDP4, период ≥ 225 мин). */
    var method = 'n'; private set
    /** Код ошибки последнего расчёта (0 — нет; 1…6 — как SGP4_ERRORS в sgp4.api). */
    var error = 0; private set
    var gsto = 0.0; private set

    // ---------------------------------------------------------------- константы WGS-72
    private val mu = 398600.8
    private val radiusEarthKm = 6378.135
    private val xke = 60.0 / sqrt(radiusEarthKm * radiusEarthKm * radiusEarthKm / mu)
    private val tumin = 1.0 / xke
    private val j2 = 0.001082616
    private val j3 = -0.00000253881
    private val j4 = -0.00000165597
    private val j3oj2 = j3 / j2

    // ---------------------------------------------------------------- рабочие величины
    private var isimp = 0
    private var aycof = 0.0; private var con41 = 0.0; private var cc1 = 0.0; private var cc4 = 0.0
    private var cc5 = 0.0; private var d2 = 0.0; private var d3 = 0.0; private var d4 = 0.0
    private var delmo = 0.0; private var eta = 0.0; private var argpdot = 0.0; private var omgcof = 0.0
    private var sinmao = 0.0; private var t = 0.0; private var t2cof = 0.0; private var t3cof = 0.0
    private var t4cof = 0.0; private var t5cof = 0.0; private var x1mth2 = 0.0; private var x7thm1 = 0.0
    private var mdot = 0.0; private var nodedot = 0.0; private var xlcof = 0.0; private var xmcof = 0.0
    private var nodecf = 0.0
    // глубокий космос
    private var irez = 0
    private var d2201 = 0.0; private var d2211 = 0.0; private var d3210 = 0.0; private var d3222 = 0.0
    private var d4410 = 0.0; private var d4422 = 0.0; private var d5220 = 0.0; private var d5232 = 0.0
    private var d5421 = 0.0; private var d5433 = 0.0; private var dedt = 0.0; private var del1 = 0.0
    private var del2 = 0.0; private var del3 = 0.0; private var didt = 0.0; private var dmdt = 0.0
    private var dnodt = 0.0; private var domdt = 0.0; private var e3 = 0.0; private var ee2 = 0.0
    private var peo = 0.0; private var pgho = 0.0; private var pho = 0.0; private var pinco = 0.0
    private var plo = 0.0; private var se2 = 0.0; private var se3 = 0.0; private var sgh2 = 0.0
    private var sgh3 = 0.0; private var sgh4 = 0.0; private var sh2 = 0.0; private var sh3 = 0.0
    private var si2 = 0.0; private var si3 = 0.0; private var sl2 = 0.0; private var sl3 = 0.0
    private var sl4 = 0.0; private var xfact = 0.0; private var xgh2 = 0.0; private var xgh3 = 0.0
    private var xgh4 = 0.0; private var xh2 = 0.0; private var xh3 = 0.0; private var xi2 = 0.0
    private var xi3 = 0.0; private var xl2 = 0.0; private var xl3 = 0.0; private var xl4 = 0.0
    private var xlamo = 0.0; private var zmol = 0.0; private var zmos = 0.0; private var atime = 0.0
    private var xli = 0.0; private var xni = 0.0

    /** Положение r (км) и скорость v (км/с) в TEME; [error] ≠ 0 — расчёт невозможен (r, v — NaN, кроме ошибки 6). */
    class State(val error: Int, val r: DoubleArray, val v: DoubleArray)

    /** Расчёт на юлианскую дату jd + fr (как Satrec.sgp4(jd, fr)). */
    fun propagate(jd: Double, fr: Double): State =
        propagateMinutes((jd - jdSatEpoch) * MIN_PER_DAY + (fr - jdSatEpochF) * MIN_PER_DAY)

    /** Расчёт на [tsince] минут от эпохи (как Satrec.sgp4_tsince). */
    fun propagateMinutes(tsince: Double): State {
        val r = DoubleArray(3)
        val v = DoubleArray(3)
        val e = sgp4(tsince, r, v)
        if (e != 0 && e != 6) {
            r.fill(Double.NaN); v.fill(Double.NaN)
        }
        return State(e, r, v)
    }

    // ================================================================ dpper
    private class Angles(var ep: Double, var inclp: Double, var nodep: Double, var argpp: Double, var mp: Double)

    private fun dpper(init: Boolean, a: Angles) {
        val zns = 1.19459e-5
        val zes = 0.01675
        val znl = 1.5835218e-4
        val zel = 0.05490
        var zm = if (init) zmos else zmos + zns * t
        var zf = zm + 2.0 * zes * sin(zm)
        var sinzf = sin(zf)
        var f2 = 0.5 * sinzf * sinzf - 0.25
        var f3 = -0.5 * sinzf * cos(zf)
        val ses = se2 * f2 + se3 * f3
        val sis = si2 * f2 + si3 * f3
        val sls = sl2 * f2 + sl3 * f3 + sl4 * sinzf
        val sghs = sgh2 * f2 + sgh3 * f3 + sgh4 * sinzf
        val shs = sh2 * f2 + sh3 * f3
        zm = if (init) zmol else zmol + znl * t
        zf = zm + 2.0 * zel * sin(zm)
        sinzf = sin(zf)
        f2 = 0.5 * sinzf * sinzf - 0.25
        f3 = -0.5 * sinzf * cos(zf)
        val sel = ee2 * f2 + e3 * f3
        val sil = xi2 * f2 + xi3 * f3
        val sll = xl2 * f2 + xl3 * f3 + xl4 * sinzf
        val sghl = xgh2 * f2 + xgh3 * f3 + xgh4 * sinzf
        val shll = xh2 * f2 + xh3 * f3
        var pe = ses + sel
        var pinc = sis + sil
        var pl = sls + sll
        var pgh = sghs + sghl
        var ph = shs + shll
        if (!init) {
            pe -= peo; pinc -= pinco; pl -= plo; pgh -= pgho; ph -= pho
            a.inclp += pinc
            a.ep += pe
            val sinip = sin(a.inclp)
            val cosip = cos(a.inclp)
            if (a.inclp >= 0.2) {
                ph /= sinip
                pgh -= cosip * ph
                a.argpp += pgh
                a.nodep += ph
                a.mp += pl
            } else {
                val sinop = sin(a.nodep)
                val cosop = cos(a.nodep)
                var alfdp = sinip * sinop
                var betdp = sinip * cosop
                val dalf = ph * cosop + pinc * cosip * sinop
                val dbet = -ph * sinop + pinc * cosip * cosop
                alfdp += dalf
                betdp += dbet
                a.nodep = a.nodep % TWOPI
                val xls = a.mp + a.argpp + pl + pgh + (cosip - pinc * sinip) * a.nodep
                val xnoh = a.nodep
                a.nodep = atan2(alfdp, betdp)
                if (abs(xnoh - a.nodep) > PI) {
                    a.nodep = if (a.nodep < xnoh) a.nodep + TWOPI else a.nodep - TWOPI
                }
                a.mp += pl
                a.argpp = xls - a.mp - cosip * a.nodep
            }
        }
    }

    // ================================================================ dscom
    private class Dscom {
        var snodm = 0.0; var cnodm = 0.0; var sinim = 0.0; var cosim = 0.0; var sinomm = 0.0; var cosomm = 0.0
        var day = 0.0; var em = 0.0; var emsq = 0.0; var gam = 0.0; var rtemsq = 0.0
        var s1 = 0.0; var s2 = 0.0; var s3 = 0.0; var s4 = 0.0; var s5 = 0.0; var s6 = 0.0; var s7 = 0.0
        var ss1 = 0.0; var ss2 = 0.0; var ss3 = 0.0; var ss4 = 0.0; var ss5 = 0.0; var ss6 = 0.0; var ss7 = 0.0
        var sz1 = 0.0; var sz2 = 0.0; var sz3 = 0.0; var sz11 = 0.0; var sz12 = 0.0; var sz13 = 0.0
        var sz21 = 0.0; var sz22 = 0.0; var sz23 = 0.0; var sz31 = 0.0; var sz32 = 0.0; var sz33 = 0.0
        var nm = 0.0; var z1 = 0.0; var z2 = 0.0; var z3 = 0.0; var z11 = 0.0; var z12 = 0.0; var z13 = 0.0
        var z21 = 0.0; var z22 = 0.0; var z23 = 0.0; var z31 = 0.0; var z32 = 0.0; var z33 = 0.0
    }

    private fun dscom(epoch: Double, ep: Double, argpp: Double, tc: Double, inclp: Double, nodep: Double,
                      np: Double, o: Dscom) {
        val zes = 0.01675
        val zel = 0.05490
        val c1ss = 2.9864797e-6
        val c1l = 4.7968065e-7
        val zsinis = 0.39785416
        val zcosis = 0.91744867
        val zcosgs = 0.1945905
        val zsings = -0.98088458

        o.nm = np
        o.em = ep
        o.snodm = sin(nodep)
        o.cnodm = cos(nodep)
        o.sinomm = sin(argpp)
        o.cosomm = cos(argpp)
        o.sinim = sin(inclp)
        o.cosim = cos(inclp)
        o.emsq = o.em * o.em
        val betasq = 1.0 - o.emsq
        o.rtemsq = sqrt(betasq)

        peo = 0.0; pinco = 0.0; plo = 0.0; pgho = 0.0; pho = 0.0
        o.day = epoch + 18261.5 + tc / 1440.0
        val xnodce = (4.5236020 - 9.2422029e-4 * o.day) % TWOPI
        val stem = sin(xnodce)
        val ctem = cos(xnodce)
        val zcosil = 0.91375164 - 0.03568096 * ctem
        val zsinil = sqrt(1.0 - zcosil * zcosil)
        val zsinhl = 0.089683511 * stem / zsinil
        val zcoshl = sqrt(1.0 - zsinhl * zsinhl)
        o.gam = 5.8351514 + 0.0019443680 * o.day
        var zx = 0.39785416 * stem / zsinil
        val zy = zcoshl * ctem + 0.91744867 * zsinhl * stem
        zx = atan2(zx, zy)
        zx = o.gam + zx - xnodce
        val zcosgl = cos(zx)
        val zsingl = sin(zx)

        var zcosg = zcosgs
        var zsing = zsings
        var zcosi = zcosis
        var zsini = zsinis
        var zcosh = o.cnodm
        var zsinh = o.snodm
        var cc = c1ss
        val xnoi = 1.0 / o.nm
        val emsq = o.emsq
        for (lsflg in 1..2) {
            val a1 = zcosg * zcosh + zsing * zcosi * zsinh
            val a3 = -zsing * zcosh + zcosg * zcosi * zsinh
            val a7 = -zcosg * zsinh + zsing * zcosi * zcosh
            val a8 = zsing * zsini
            val a9 = zsing * zsinh + zcosg * zcosi * zcosh
            val a10 = zcosg * zsini
            val a2 = o.cosim * a7 + o.sinim * a8
            val a4 = o.cosim * a9 + o.sinim * a10
            val a5 = -o.sinim * a7 + o.cosim * a8
            val a6 = -o.sinim * a9 + o.cosim * a10

            val x1 = a1 * o.cosomm + a2 * o.sinomm
            val x2 = a3 * o.cosomm + a4 * o.sinomm
            val x3 = -a1 * o.sinomm + a2 * o.cosomm
            val x4 = -a3 * o.sinomm + a4 * o.cosomm
            val x5 = a5 * o.sinomm
            val x6 = a6 * o.sinomm
            val x7 = a5 * o.cosomm
            val x8 = a6 * o.cosomm

            o.z31 = 12.0 * x1 * x1 - 3.0 * x3 * x3
            o.z32 = 24.0 * x1 * x2 - 6.0 * x3 * x4
            o.z33 = 12.0 * x2 * x2 - 3.0 * x4 * x4
            o.z1 = 3.0 * (a1 * a1 + a2 * a2) + o.z31 * emsq
            o.z2 = 6.0 * (a1 * a3 + a2 * a4) + o.z32 * emsq
            o.z3 = 3.0 * (a3 * a3 + a4 * a4) + o.z33 * emsq
            o.z11 = -6.0 * a1 * a5 + emsq * (-24.0 * x1 * x7 - 6.0 * x3 * x5)
            o.z12 = -6.0 * (a1 * a6 + a3 * a5) + emsq * (-24.0 * (x2 * x7 + x1 * x8) - 6.0 * (x3 * x6 + x4 * x5))
            o.z13 = -6.0 * a3 * a6 + emsq * (-24.0 * x2 * x8 - 6.0 * x4 * x6)
            o.z21 = 6.0 * a2 * a5 + emsq * (24.0 * x1 * x5 - 6.0 * x3 * x7)
            o.z22 = 6.0 * (a4 * a5 + a2 * a6) + emsq * (24.0 * (x2 * x5 + x1 * x6) - 6.0 * (x4 * x7 + x3 * x8))
            o.z23 = 6.0 * a4 * a6 + emsq * (24.0 * x2 * x6 - 6.0 * x4 * x8)
            o.z1 = o.z1 + o.z1 + betasq * o.z31
            o.z2 = o.z2 + o.z2 + betasq * o.z32
            o.z3 = o.z3 + o.z3 + betasq * o.z33
            o.s3 = cc * xnoi
            o.s2 = -0.5 * o.s3 / o.rtemsq
            o.s4 = o.s3 * o.rtemsq
            o.s1 = -15.0 * o.em * o.s4
            o.s5 = x1 * x3 + x2 * x4
            o.s6 = x2 * x3 + x1 * x4
            o.s7 = x2 * x4 - x1 * x3

            if (lsflg == 1) {
                o.ss1 = o.s1; o.ss2 = o.s2; o.ss3 = o.s3; o.ss4 = o.s4; o.ss5 = o.s5; o.ss6 = o.s6; o.ss7 = o.s7
                o.sz1 = o.z1; o.sz2 = o.z2; o.sz3 = o.z3
                o.sz11 = o.z11; o.sz12 = o.z12; o.sz13 = o.z13
                o.sz21 = o.z21; o.sz22 = o.z22; o.sz23 = o.z23
                o.sz31 = o.z31; o.sz32 = o.z32; o.sz33 = o.z33
                zcosg = zcosgl
                zsing = zsingl
                zcosi = zcosil
                zsini = zsinil
                zcosh = zcoshl * o.cnodm + zsinhl * o.snodm
                zsinh = o.snodm * zcoshl - o.cnodm * zsinhl
                cc = c1l
            }
        }
        zmol = (4.7199672 + 0.22997150 * o.day - o.gam) % TWOPI
        zmos = (6.2565837 + 0.017201977 * o.day) % TWOPI

        se2 = 2.0 * o.ss1 * o.ss6
        se3 = 2.0 * o.ss1 * o.ss7
        si2 = 2.0 * o.ss2 * o.sz12
        si3 = 2.0 * o.ss2 * (o.sz13 - o.sz11)
        sl2 = -2.0 * o.ss3 * o.sz2
        sl3 = -2.0 * o.ss3 * (o.sz3 - o.sz1)
        sl4 = -2.0 * o.ss3 * (-21.0 - 9.0 * emsq) * zes
        sgh2 = 2.0 * o.ss4 * o.sz32
        sgh3 = 2.0 * o.ss4 * (o.sz33 - o.sz31)
        sgh4 = -18.0 * o.ss4 * zes
        sh2 = -2.0 * o.ss2 * o.sz22
        sh3 = -2.0 * o.ss2 * (o.sz23 - o.sz21)

        ee2 = 2.0 * o.s1 * o.s6
        e3 = 2.0 * o.s1 * o.s7
        xi2 = 2.0 * o.s2 * o.z12
        xi3 = 2.0 * o.s2 * (o.z13 - o.z11)
        xl2 = -2.0 * o.s3 * o.z2
        xl3 = -2.0 * o.s3 * (o.z3 - o.z1)
        xl4 = -2.0 * o.s3 * (-21.0 - 9.0 * emsq) * zel
        xgh2 = 2.0 * o.s4 * o.z32
        xgh3 = 2.0 * o.s4 * (o.z33 - o.z31)
        xgh4 = -18.0 * o.s4 * zel
        xh2 = -2.0 * o.s2 * o.z22
        xh3 = -2.0 * o.s2 * (o.z23 - o.z21)
    }

    // ================================================================ dsinit
    private class Mean(var em: Double, var argpm: Double, var inclm: Double, var mm: Double, var nm: Double,
                       var nodem: Double)

    private fun dsinit(c: Dscom, tc: Double, xpidot: Double, eccsq: Double, m: Mean) {
        val q22 = 1.7891679e-6
        val q31 = 2.1460748e-6
        val q33 = 2.2123015e-7
        val root22 = 1.7891679e-6
        val root44 = 7.3636953e-9
        val root54 = 2.1765803e-9
        val rptim = 4.37526908801129966e-3
        val root32 = 3.7393792e-7
        val root52 = 1.1428639e-7
        val x2o3 = 2.0 / 3.0
        val znl = 1.5835218e-4
        val zns = 1.19459e-5
        val cosim = c.cosim
        val sinim = c.sinim
        var emsq = c.emsq

        irez = 0
        if (m.nm < 0.0052359877 && m.nm > 0.0034906585) irez = 1
        if (m.nm >= 8.26e-3 && m.nm <= 9.24e-3 && m.em >= 0.5) irez = 2

        // солнечные члены
        val ses = c.ss1 * zns * c.ss5
        val sis = c.ss2 * zns * (c.sz11 + c.sz13)
        val sls = -zns * c.ss3 * (c.sz1 + c.sz3 - 14.0 - 6.0 * emsq)
        val sghs = c.ss4 * zns * (c.sz31 + c.sz33 - 6.0)
        var shs = -zns * c.ss2 * (c.sz21 + c.sz23)
        if (m.inclm < 5.2359877e-2 || m.inclm > PI - 5.2359877e-2) shs = 0.0
        if (sinim != 0.0) shs /= sinim
        val sgs = sghs - cosim * shs

        // лунные члены
        dedt = ses + c.s1 * znl * c.s5
        didt = sis + c.s2 * znl * (c.z11 + c.z13)
        dmdt = sls - znl * c.s3 * (c.z1 + c.z3 - 14.0 - 6.0 * emsq)
        val sghl = c.s4 * znl * (c.z31 + c.z33 - 6.0)
        var shll = -znl * c.s2 * (c.z21 + c.z23)
        if (m.inclm < 5.2359877e-2 || m.inclm > PI - 5.2359877e-2) shll = 0.0
        domdt = sgs + sghl
        dnodt = shs
        if (sinim != 0.0) {
            domdt -= cosim / sinim * shll
            dnodt += shll / sinim
        }

        // глубокий космос: резонансы
        val dndt = 0.0
        val theta = (gsto + tc * rptim) % TWOPI
        m.em += dedt * t
        m.inclm += didt * t
        m.argpm += domdt * t
        m.nodem += dnodt * t
        m.mm += dmdt * t

        if (irez != 0) {
            val aonv = (m.nm / xke).pow(x2o3)
            if (irez == 2) {
                // полусуточный резонанс (Молния)
                val cosisq = cosim * cosim
                val emo = m.em
                m.em = ecco
                val emsqo = emsq
                emsq = eccsq
                val em = m.em
                val eoc = em * emsq
                val g201 = -0.306 - (em - 0.64) * 0.440
                val g211: Double; val g310: Double; val g322: Double; val g410: Double; val g422: Double
                val g520: Double
                if (em <= 0.65) {
                    g211 = 3.616 - 13.2470 * em + 16.2900 * emsq
                    g310 = -19.302 + 117.3900 * em - 228.4190 * emsq + 156.5910 * eoc
                    g322 = -18.9068 + 109.7927 * em - 214.6334 * emsq + 146.5816 * eoc
                    g410 = -41.122 + 242.6940 * em - 471.0940 * emsq + 313.9530 * eoc
                    g422 = -146.407 + 841.8800 * em - 1629.014 * emsq + 1083.4350 * eoc
                    g520 = -532.114 + 3017.977 * em - 5740.032 * emsq + 3708.2760 * eoc
                } else {
                    g211 = -72.099 + 331.819 * em - 508.738 * emsq + 266.724 * eoc
                    g310 = -346.844 + 1582.851 * em - 2415.925 * emsq + 1246.113 * eoc
                    g322 = -342.585 + 1554.908 * em - 2366.899 * emsq + 1215.972 * eoc
                    g410 = -1052.797 + 4758.686 * em - 7193.992 * emsq + 3651.957 * eoc
                    g422 = -3581.690 + 16178.110 * em - 24462.770 * emsq + 12422.520 * eoc
                    g520 = if (em > 0.715) -5149.66 + 29936.92 * em - 54087.36 * emsq + 31324.56 * eoc
                    else 1464.74 - 4664.75 * em + 3763.64 * emsq
                }
                val g533: Double; val g521: Double; val g532: Double
                if (em < 0.7) {
                    g533 = -919.22770 + 4988.6100 * em - 9064.7700 * emsq + 5542.21 * eoc
                    g521 = -822.71072 + 4568.6173 * em - 8491.4146 * emsq + 5337.524 * eoc
                    g532 = -853.66600 + 4690.2500 * em - 8624.7700 * emsq + 5341.4 * eoc
                } else {
                    g533 = -37995.780 + 161616.52 * em - 229838.20 * emsq + 109377.94 * eoc
                    g521 = -51752.104 + 218913.95 * em - 309468.16 * emsq + 146349.42 * eoc
                    g532 = -40023.880 + 170470.89 * em - 242699.48 * emsq + 115605.82 * eoc
                }
                val sini2 = sinim * sinim
                val f220 = 0.75 * (1.0 + 2.0 * cosim + cosisq)
                val f221 = 1.5 * sini2
                val f321 = 1.875 * sinim * (1.0 - 2.0 * cosim - 3.0 * cosisq)
                val f322 = -1.875 * sinim * (1.0 + 2.0 * cosim - 3.0 * cosisq)
                val f441 = 35.0 * sini2 * f220
                val f442 = 39.3750 * sini2 * sini2
                val f522 = 9.84375 * sinim * (sini2 * (1.0 - 2.0 * cosim - 5.0 * cosisq) +
                        0.33333333 * (-2.0 + 4.0 * cosim + 6.0 * cosisq))
                val f523 = sinim * (4.92187512 * sini2 * (-2.0 - 4.0 * cosim + 10.0 * cosisq) +
                        6.56250012 * (1.0 + 2.0 * cosim - 3.0 * cosisq))
                val f542 = 29.53125 * sinim * (2.0 - 8.0 * cosim + cosisq * (-12.0 + 8.0 * cosim + 10.0 * cosisq))
                val f543 = 29.53125 * sinim * (-2.0 - 8.0 * cosim + cosisq * (12.0 + 8.0 * cosim - 10.0 * cosisq))
                val xno2 = m.nm * m.nm
                val ainv2 = aonv * aonv
                var temp1 = 3.0 * xno2 * ainv2
                var temp = temp1 * root22
                d2201 = temp * f220 * g201
                d2211 = temp * f221 * g211
                temp1 *= aonv
                temp = temp1 * root32
                d3210 = temp * f321 * g310
                d3222 = temp * f322 * g322
                temp1 *= aonv
                temp = 2.0 * temp1 * root44
                d4410 = temp * f441 * g410
                d4422 = temp * f442 * g422
                temp1 *= aonv
                temp = temp1 * root52
                d5220 = temp * f522 * g520
                d5232 = temp * f523 * g532
                temp = 2.0 * temp1 * root54
                d5421 = temp * f542 * g521
                d5433 = temp * f543 * g533
                xlamo = (mo + nodeo + nodeo - theta - theta) % TWOPI
                xfact = mdot + dmdt + 2.0 * (nodedot + dnodt - rptim) - noUnkozai
                m.em = emo
                emsq = emsqo
            }
            if (irez == 1) {
                // суточный резонанс (геостационар)
                val g200 = 1.0 + emsq * (-2.5 + 0.8125 * emsq)
                val g310 = 1.0 + 2.0 * emsq
                val g300 = 1.0 + emsq * (-6.0 + 6.60937 * emsq)
                val f220 = 0.75 * (1.0 + cosim) * (1.0 + cosim)
                val f311 = 0.9375 * sinim * sinim * (1.0 + 3.0 * cosim) - 0.75 * (1.0 + cosim)
                var f330 = 1.0 + cosim
                f330 = 1.875 * f330 * f330 * f330
                del1 = 3.0 * m.nm * m.nm * aonv * aonv
                del2 = 2.0 * del1 * f220 * g200 * q22
                del3 = 3.0 * del1 * f330 * g300 * q33 * aonv
                del1 = del1 * f311 * g310 * q31 * aonv
                xlamo = (mo + nodeo + argpo - theta) % TWOPI
                xfact = mdot + xpidot - rptim + dmdt + domdt + dnodt - noUnkozai
            }
            xli = xlamo
            xni = noUnkozai
            atime = 0.0
            m.nm = noUnkozai + dndt
        }
    }

    // ================================================================ dspace
    /** Возвращает dndt; меняет [m] (em, argpm, inclm, mm, nm, nodem). */
    private fun dspace(tc: Double, m: Mean) {
        val fasx2 = 0.13130908
        val fasx4 = 2.8843198
        val fasx6 = 0.37448087
        val g22 = 5.7686396
        val g32 = 0.95240898
        val g44 = 1.8014998
        val g52 = 1.0508330
        val g54 = 4.4108898
        val rptim = 4.37526908801129966e-3
        val stepp = 720.0
        val stepn = -720.0
        val step2 = 259200.0

        val theta = (gsto + tc * rptim) % TWOPI
        m.em += dedt * t
        m.inclm += didt * t
        m.argpm += domdt * t
        m.nodem += dnodt * t
        m.mm += dmdt * t

        var ft = 0.0
        if (irez != 0) {
            if (atime == 0.0 || t * atime <= 0.0 || abs(t) < abs(atime)) {
                atime = 0.0
                xni = noUnkozai
                xli = xlamo
            }
            val delt = if (t > 0.0) stepp else stepn
            var xndt: Double
            var xldot: Double
            var xnddt: Double
            while (true) {
                if (irez != 2) {
                    xndt = del1 * sin(xli - fasx2) + del2 * sin(2.0 * (xli - fasx4)) + del3 * sin(3.0 * (xli - fasx6))
                    xldot = xni + xfact
                    xnddt = del1 * cos(xli - fasx2) + 2.0 * del2 * cos(2.0 * (xli - fasx4)) +
                            3.0 * del3 * cos(3.0 * (xli - fasx6))
                    xnddt *= xldot
                } else {
                    val xomi = argpo + argpdot * atime
                    val x2omi = xomi + xomi
                    val x2li = xli + xli
                    xndt = (d2201 * sin(x2omi + xli - g22) + d2211 * sin(xli - g22) +
                            d3210 * sin(xomi + xli - g32) + d3222 * sin(-xomi + xli - g32) +
                            d4410 * sin(x2omi + x2li - g44) + d4422 * sin(x2li - g44) +
                            d5220 * sin(xomi + xli - g52) + d5232 * sin(-xomi + xli - g52) +
                            d5421 * sin(xomi + x2li - g54) + d5433 * sin(-xomi + x2li - g54))
                    xldot = xni + xfact
                    xnddt = (d2201 * cos(x2omi + xli - g22) + d2211 * cos(xli - g22) +
                            d3210 * cos(xomi + xli - g32) + d3222 * cos(-xomi + xli - g32) +
                            d5220 * cos(xomi + xli - g52) + d5232 * cos(-xomi + xli - g52) +
                            2.0 * (d4410 * cos(x2omi + x2li - g44) +
                            d4422 * cos(x2li - g44) + d5421 * cos(xomi + x2li - g54) +
                            d5433 * cos(-xomi + x2li - g54)))
                    xnddt *= xldot
                }
                if (abs(t - atime) >= stepp) {
                    xli += xldot * delt + xndt * step2
                    xni += xndt * delt + xnddt * step2
                    atime += delt
                } else {
                    ft = t - atime
                    break
                }
            }
            m.nm = xni + xndt * ft + xnddt * ft * ft * 0.5
            val xl = xli + xldot * ft + xndt * ft * ft * 0.5
            m.mm = if (irez != 1) xl - 2.0 * m.nodem + 2.0 * theta else xl - m.nodem - m.argpm + theta
            val dndt = m.nm - noUnkozai
            m.nm = noUnkozai + dndt
        }
    }

    // ================================================================ sgp4init
    private fun init(epoch: Double) {
        val temp4 = 1.5e-12
        val ss = 78.0 / radiusEarthKm + 1.0
        val qzms2ttemp = (120.0 - 78.0) / radiusEarthKm
        val qzms2t = qzms2ttemp * qzms2ttemp * qzms2ttemp * qzms2ttemp
        val x2o3 = 2.0 / 3.0
        t = 0.0

        // initl
        val eccsq = ecco * ecco
        val omeosq = 1.0 - eccsq
        val rteosq = sqrt(omeosq)
        val cosio = cos(inclo)
        val cosio2 = cosio * cosio
        val ak = (xke / noKozai).pow(x2o3)
        val d1 = 0.75 * j2 * (3.0 * cosio2 - 1.0) / (rteosq * omeosq)
        var del = d1 / (ak * ak)
        val adel = ak * (1.0 - del * del - del * (1.0 / 3.0 + 134.0 * del * del / 81.0))
        del = d1 / (adel * adel)
        noUnkozai = noKozai / (1.0 + del)
        val ao = (xke / noUnkozai).pow(x2o3)
        val sinio = sin(inclo)
        val po = ao * omeosq
        val con42 = 1.0 - 5.0 * cosio2
        con41 = -con42 - cosio2 - cosio2
        val posq = po * po
        val rp = ao * (1.0 - ecco)
        method = 'n'
        gsto = gstime(epoch + 2433281.5)

        if (omeosq >= 0.0 || noUnkozai >= 0.0) {
            isimp = 0
            if (rp < 220.0 / radiusEarthKm + 1.0) isimp = 1
            var sfour = ss
            var qzms24 = qzms2t
            val perige = (rp - 1.0) * radiusEarthKm
            if (perige < 156.0) {
                sfour = perige - 78.0
                if (perige < 98.0) sfour = 20.0
                val qzms24temp = (120.0 - sfour) / radiusEarthKm
                qzms24 = qzms24temp * qzms24temp * qzms24temp * qzms24temp
                sfour = sfour / radiusEarthKm + 1.0
            }
            val pinvsq = 1.0 / posq
            val tsi = 1.0 / (ao - sfour)
            eta = ao * ecco * tsi
            val etasq = eta * eta
            val eeta = ecco * eta
            val psisq = abs(1.0 - etasq)
            val coef = qzms24 * tsi.pow(4.0)
            val coef1 = coef / psisq.pow(3.5)
            val cc2 = coef1 * noUnkozai * (ao * (1.0 + 1.5 * etasq + eeta * (4.0 + etasq)) +
                    0.375 * j2 * tsi / psisq * con41 * (8.0 + 3.0 * etasq * (8.0 + etasq)))
            cc1 = bstar * cc2
            var cc3 = 0.0
            if (ecco > 1.0e-4) cc3 = -2.0 * coef * tsi * j3oj2 * noUnkozai * sinio / ecco
            x1mth2 = 1.0 - cosio2
            cc4 = 2.0 * noUnkozai * coef1 * ao * omeosq *
                    (eta * (2.0 + 0.5 * etasq) + ecco * (0.5 + 2.0 * etasq) - j2 * tsi / (ao * psisq) *
                            (-3.0 * con41 * (1.0 - 2.0 * eeta + etasq * (1.5 - 0.5 * eeta)) + 0.75 * x1mth2 *
                                    (2.0 * etasq - eeta * (1.0 + etasq)) * cos(2.0 * argpo)))
            cc5 = 2.0 * coef1 * ao * omeosq * (1.0 + 2.75 * (etasq + eeta) + eeta * etasq)
            val cosio4 = cosio2 * cosio2
            val temp1 = 1.5 * j2 * pinvsq * noUnkozai
            val temp2 = 0.5 * temp1 * j2 * pinvsq
            val temp3 = -0.46875 * j4 * pinvsq * pinvsq * noUnkozai
            mdot = noUnkozai + 0.5 * temp1 * rteosq * con41 + 0.0625 * temp2 * rteosq *
                    (13.0 - 78.0 * cosio2 + 137.0 * cosio4)
            argpdot = (-0.5 * temp1 * con42 + 0.0625 * temp2 * (7.0 - 114.0 * cosio2 + 395.0 * cosio4) +
                    temp3 * (3.0 - 36.0 * cosio2 + 49.0 * cosio4))
            val xhdot1 = -temp1 * cosio
            nodedot = xhdot1 + (0.5 * temp2 * (4.0 - 19.0 * cosio2) + 2.0 * temp3 * (3.0 - 7.0 * cosio2)) * cosio
            val xpidot = argpdot + nodedot
            omgcof = bstar * cc3 * cos(argpo)
            xmcof = 0.0
            if (ecco > 1.0e-4) xmcof = -x2o3 * coef * bstar / eeta
            nodecf = 3.5 * omeosq * xhdot1 * cc1
            t2cof = 1.5 * cc1
            xlcof = if (abs(cosio + 1.0) > 1.5e-12) -0.25 * j3oj2 * sinio * (3.0 + 5.0 * cosio) / (1.0 + cosio)
            else -0.25 * j3oj2 * sinio * (3.0 + 5.0 * cosio) / temp4
            aycof = -0.5 * j3oj2 * sinio
            val delmotemp = 1.0 + eta * cos(mo)
            delmo = delmotemp * delmotemp * delmotemp
            sinmao = sin(mo)
            x7thm1 = 7.0 * cosio2 - 1.0

            // глубокий космос
            if (2 * PI / noUnkozai >= 225.0) {
                method = 'd'
                isimp = 1
                val tc = 0.0
                val inclm = inclo
                val c = Dscom()
                dscom(epoch, ecco, argpo, tc, inclo, nodeo, noUnkozai, c)
                val a = Angles(ecco, inclo, nodeo, argpo, mo)
                dpper(true, a)
                ecco = a.ep; inclo = a.inclp; nodeo = a.nodep; argpo = a.argpp; mo = a.mp
                val m = Mean(c.em, 0.0, inclm, 0.0, c.nm, 0.0)
                dsinit(c, tc, xpidot, eccsq, m)
            }

            if (isimp != 1) {
                val cc1sq = cc1 * cc1
                d2 = 4.0 * ao * tsi * cc1sq
                val temp = d2 * tsi * cc1 / 3.0
                d3 = (17.0 * ao + sfour) * temp
                d4 = 0.5 * temp * ao * tsi * (221.0 * ao + 31.0 * sfour) * cc1
                t3cof = d2 + 2.0 * cc1sq
                t4cof = 0.25 * (3.0 * d3 + cc1 * (12.0 * d2 + 10.0 * cc1sq))
                t5cof = 0.2 * (3.0 * d4 + 12.0 * cc1 * d3 + 6.0 * d2 * d2 + 15.0 * cc1sq * (2.0 * d2 + cc1sq))
            }
        }
        sgp4(0.0, DoubleArray(3), DoubleArray(3))
    }

    // ================================================================ sgp4
    private fun sgp4(tsince: Double, r: DoubleArray, v: DoubleArray): Int {
        val temp4 = 1.5e-12
        val x2o3 = 2.0 / 3.0
        val vkmpersec = radiusEarthKm * xke / 60.0

        t = tsince
        error = 0

        val xmdf = mo + mdot * t
        val argpdf = argpo + argpdot * t
        val nodedf = nodeo + nodedot * t
        var argpm = argpdf
        var mm = xmdf
        val t2 = t * t
        var nodem = nodedf + nodecf * t2
        var tempa = 1.0 - cc1 * t
        var tempe = bstar * cc4 * t
        var templ = t2cof * t2

        if (isimp != 1) {
            val delomg = omgcof * t
            val delmtemp = 1.0 + eta * cos(xmdf)
            val delm = xmcof * (delmtemp * delmtemp * delmtemp - delmo)
            val temp = delomg + delm
            mm = xmdf + temp
            argpm = argpdf - temp
            val t3 = t2 * t
            val t4 = t3 * t
            tempa = tempa - d2 * t2 - d3 * t3 - d4 * t4
            tempe += bstar * cc5 * (sin(mm) - sinmao)
            templ = templ + t3cof * t3 + t4 * (t4cof + t * t5cof)
        }

        var nm = noUnkozai
        var em = ecco
        var inclm = inclo
        if (method == 'd') {
            val tc = t
            val m = Mean(em, argpm, inclm, mm, nm, nodem)
            dspace(tc, m)
            em = m.em; argpm = m.argpm; inclm = m.inclm; mm = m.mm; nm = m.nm; nodem = m.nodem
        }

        if (nm <= 0.0) {
            error = 2
            return error
        }
        val am = (xke / nm).pow(x2o3) * tempa * tempa
        nm = xke / am.pow(1.5)
        em -= tempe

        if (em >= 1.0 || em < -0.001) {
            error = 1
            return error
        }
        if (em < 1.0e-6) em = 1.0e-6
        mm += noUnkozai * templ
        var xlm = mm + argpm + nodem
        nodem %= TWOPI
        argpm %= TWOPI
        xlm %= TWOPI
        mm = (xlm - argpm - nodem) % TWOPI

        // периодические члены глубокого космоса
        val sinim = sin(inclm)
        val cosim = cos(inclm)
        var ep = em
        var xincp = inclm
        var argpp = argpm
        var nodep = nodem
        var mp = mm
        var sinip = sinim
        var cosip = cosim
        if (method == 'd') {
            val a = Angles(ep, xincp, nodep, argpp, mp)
            dpper(false, a)
            ep = a.ep; xincp = a.inclp; nodep = a.nodep; argpp = a.argpp; mp = a.mp
            if (xincp < 0.0) {
                xincp = -xincp
                nodep += PI
                argpp -= PI
            }
            if (ep < 0.0 || ep > 1.0) {
                error = 3
                return error
            }
        }

        // долгопериодические члены
        if (method == 'd') {
            sinip = sin(xincp)
            cosip = cos(xincp)
            aycof = -0.5 * j3oj2 * sinip
            xlcof = if (abs(cosip + 1.0) > 1.5e-12) -0.25 * j3oj2 * sinip * (3.0 + 5.0 * cosip) / (1.0 + cosip)
            else -0.25 * j3oj2 * sinip * (3.0 + 5.0 * cosip) / temp4
        }
        val axnl = ep * cos(argpp)
        var temp = 1.0 / (am * (1.0 - ep * ep))
        val aynl = ep * sin(argpp) + temp * aycof
        val xl = mp + argpp + nodep + temp * xlcof * axnl

        // уравнение Кеплера
        val u = (xl - nodep) % TWOPI
        var eo1 = u
        var tem5 = 9999.9
        var ktr = 1
        var sineo1 = 0.0
        var coseo1 = 0.0
        while (abs(tem5) >= 1.0e-12 && ktr <= 10) {
            sineo1 = sin(eo1)
            coseo1 = cos(eo1)
            tem5 = 1.0 - coseo1 * axnl - sineo1 * aynl
            tem5 = (u - aynl * coseo1 + axnl * sineo1 - eo1) / tem5
            if (abs(tem5) >= 0.95) tem5 = if (tem5 > 0.0) 0.95 else -0.95
            eo1 += tem5
            ktr++
        }

        // короткопериодические члены
        val ecose = axnl * coseo1 + aynl * sineo1
        val esine = axnl * sineo1 - aynl * coseo1
        val el2 = axnl * axnl + aynl * aynl
        val pl = am * (1.0 - el2)
        if (pl < 0.0) {
            error = 4
            return error
        }
        val rl = am * (1.0 - ecose)
        val rdotl = sqrt(am) * esine / rl
        val rvdotl = sqrt(pl) / rl
        val betal = sqrt(1.0 - el2)
        temp = esine / (1.0 + betal)
        val sinu = am / rl * (sineo1 - aynl - axnl * temp)
        val cosu = am / rl * (coseo1 - axnl + aynl * temp)
        var su = atan2(sinu, cosu)
        val sin2u = (cosu + cosu) * sinu
        val cos2u = 1.0 - 2.0 * sinu * sinu
        temp = 1.0 / pl
        val temp1 = 0.5 * j2 * temp
        val temp2 = temp1 * temp

        if (method == 'd') {
            val cosisq = cosip * cosip
            con41 = 3.0 * cosisq - 1.0
            x1mth2 = 1.0 - cosisq
            x7thm1 = 7.0 * cosisq - 1.0
        }
        val mrt = rl * (1.0 - 1.5 * temp2 * betal * con41) + 0.5 * temp1 * x1mth2 * cos2u
        su -= 0.25 * temp2 * x7thm1 * sin2u
        val xnode = nodep + 1.5 * temp2 * cosip * sin2u
        val xinc = xincp + 1.5 * temp2 * cosip * sinip * cos2u
        val mvt = rdotl - nm * temp1 * x1mth2 * sin2u / xke
        val rvdot = rvdotl + nm * temp1 * (x1mth2 * cos2u + 1.5 * con41) / xke

        val sinsu = sin(su)
        val cossu = cos(su)
        val snod = sin(xnode)
        val cnod = cos(xnode)
        val sini = sin(xinc)
        val cosi = cos(xinc)
        val xmx = -snod * cosi
        val xmy = cnod * cosi
        val ux = xmx * sinsu + cnod * cossu
        val uy = xmy * sinsu + snod * cossu
        val uz = sini * sinsu
        val vx = xmx * cossu - cnod * sinsu
        val vy = xmy * cossu - snod * sinsu
        val vz = sini * cossu

        val mr = mrt * radiusEarthKm
        r[0] = mr * ux; r[1] = mr * uy; r[2] = mr * uz
        v[0] = (mvt * ux + rvdot * vx) * vkmpersec
        v[1] = (mvt * uy + rvdot * vy) * vkmpersec
        v[2] = (mvt * uz + rvdot * vz) * vkmpersec

        if (mrt < 1.0) error = 6
        return error
    }

    companion object {
        const val TWOPI = 2.0 * PI
        const val MIN_PER_DAY = 1440.0
        private const val DEG2RAD = PI / 180.0
        private const val XPDOTP = 1440.0 / (2.0 * PI)

        /** Звёздное время Гринвича (рад) по юлианской дате UT1 — gstime из sgp4. */
        fun gstime(jdut1: Double): Double {
            val tut1 = (jdut1 - 2451545.0) / 36525.0
            var temp = -6.2e-6 * tut1 * tut1 * tut1 + 0.093104 * tut1 * tut1 +
                    (876600.0 * 3600 + 8640184.812866) * tut1 + 67310.54841
            temp = (temp * DEG2RAD / 240.0) % TWOPI
            if (temp < 0.0) temp += TWOPI
            return temp
        }

        /** Поле TLE: срез [a, b) без пробелов по краям (короче строки — пусто). */
        private fun field(s: String, a: Int, b: Int): String =
            if (a >= s.length) "" else s.substring(a, minOf(b, s.length)).trim()

        /** Число в «неявной» записи TLE: « 12345-3» → 0.12345e-3; ведущий знак допустим. */
        private fun implied(mant: String, exp: String): Double {
            val m = mant.trim()
            if (m.isEmpty()) return 0.0
            val sign = if (m[0] == '-') -1.0 else 1.0
            val digits = m.trimStart('-', '+').trim()
            val x = if (digits.isEmpty()) 0.0 else "0.$digits".toDouble()
            val e = exp.trim().ifEmpty { "0" }.replace("+", "").toInt()
            return sign * x * 10.0.pow(e)
        }

        private fun num(s: String): Double = s.trim().ifEmpty { "0" }.let {
            // «.00007138», «-.00000102», «+.0001»
            it.replace("+", "").let { x -> if (x.startsWith("-.")) "-0" + x.substring(1) else x }.toDouble()
        }

        /**
         * Разбор TLE и инициализация (как Satrec.twoline2rv(line1, line2), WGS-72).
         * @throws IllegalArgumentException строки не похожи на TLE или номера в строках не совпадают.
         */
        fun twoline2rv(line1: String, line2: String): Sgp4 {
            val l1 = line1.trimEnd()
            val l2 = line2.trimEnd()
            require(l1.length >= 61 && l1.startsWith("1 ")) { "строка 1 TLE: «$l1»" }
            require(l2.length >= 63 && l2.startsWith("2 ")) { "строка 2 TLE: «$l2»" }
            val s = Sgp4()
            try {
                s.satnum = field(l1, 2, 7)
                val twoDigitYear = field(l1, 18, 20).toInt()
                s.epochDays = field(l1, 20, 32).toDouble()
                s.ndot = num(field(l1, 33, 43))
                s.nddot = implied(field(l1, 44, 50), field(l1, 50, 52))
                s.bstar = implied(field(l1, 53, 59), field(l1, 59, 61))
                require(field(l2, 2, 7) == s.satnum) { "номера спутника в строках TLE не совпадают" }
                s.inclo = field(l2, 8, 16).toDouble()
                s.nodeo = field(l2, 17, 25).toDouble()
                s.ecco = ("0." + l2.substring(26, minOf(33, l2.length)).replace(' ', '0')).toDouble()
                s.argpo = field(l2, 34, 42).toDouble()
                s.mo = field(l2, 43, 51).toDouble()
                s.noKozai = field(l2, 52, 63).toDouble()
                val year = if (twoDigitYear < 57) twoDigitYear + 2000 else twoDigitYear + 1900
                s.epochYr = twoDigitYear

                s.noKozai /= XPDOTP
                s.ndot /= XPDOTP * 1440.0
                s.nddot /= XPDOTP * 1440.0 * 1440.0
                s.inclo *= DEG2RAD
                s.nodeo *= DEG2RAD
                s.argpo *= DEG2RAD
                s.mo *= DEG2RAD

                // юлианская дата эпохи, разбитая на целое и долю (доля — ровно 8 знаков TLE)
                val days = floor(s.epochDays)
                val fraction = s.epochDays - days
                s.jdSatEpoch = year * 365.0 + Math.floorDiv(year - 1, 4) + days + 1721044.5
                s.jdSatEpochF = round(fraction * 1e8) / 1e8
            } catch (e: NumberFormatException) {
                throw IllegalArgumentException("TLE не разобран: ${e.message}", e)
            } catch (e: StringIndexOutOfBoundsException) {
                throw IllegalArgumentException("TLE слишком короткий", e)
            }
            s.init(s.jdSatEpoch + s.jdSatEpochF - 2433281.5)
            return s
        }
    }
}
