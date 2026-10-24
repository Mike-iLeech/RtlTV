/* Путь радиосигнала — интерактивная схема SDR4NOOB.
   Без библиотек и сети: расчёт уровней по цепочке + лёгкие анимации на Canvas
   (requestAnimationFrame, стоят на паузе, когда блок закрыт или не виден). */
(function () {
  "use strict";

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var TAU = Math.PI * 2;
  var LOG10 = Math.log10 || function (x) { return Math.log(x) / Math.LN10; };

  /* ------------------------------------------------------------------ цвета темы */
  var C = {};
  var WF = [];  // палитра водопада, 256 цветов [r,g,b]
  function hexRgb(h) {
    h = h.replace("#", "");
    if (h.length === 3) { h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2]; }
    var n = parseInt(h, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function readTheme() {
    var cs = getComputedStyle(document.documentElement);
    ["bg", "panel", "panel2", "fg", "dim", "grid", "accent", "tx", "air", "rx", "sdr", "good", "warn", "bad",
      "noise", "wf0", "wf1", "wf2", "wf3", "wf4"].forEach(function (k) {
      C[k] = cs.getPropertyValue("--" + k).trim() || "#888888";
    });
    var stops = [[0, C.wf0], [0.35, C.wf1], [0.62, C.wf2], [0.85, C.wf3], [1, C.wf4]].map(function (s) {
      return [s[0], hexRgb(s[1])];
    });
    WF = [];
    for (var i = 0; i < 256; i++) {
      var x = i / 255, j = 0;
      while (j < stops.length - 2 && x > stops[j + 1][0]) { j++; }
      var a = stops[j], b = stops[j + 1], k = (x - a[0]) / (b[0] - a[0]);
      WF.push([0, 1, 2].map(function (c) { return Math.round(a[1][c] + (b[1][c] - a[1][c]) * k); }));
    }
  }
  function alpha(hex, a) {
    var c = hexRgb(hex);
    return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + a + ")";
  }

  /* ------------------------------------------------------------------ числа по-русски */
  function num(x, d) {
    if (!isFinite(x)) { return "—"; }
    var s = Math.abs(x).toFixed(d);
    var neg = x < 0 && Number(s) !== 0;
    var parts = s.split(".");
    if (parts[0].length > 4) { parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, " "); }
    return (neg ? "−" : "") + parts.join(",");
  }
  function sig(v) { var a = Math.abs(v); return a >= 100 ? 0 : a >= 10 ? 1 : 2; }
  function unitFmt(v, units) {
    if (!isFinite(v) || v <= 0) { return "0 " + units[units.length - 1][1]; }
    for (var i = 0; i < units.length; i++) {
      if (v >= units[i][0] * 0.9995) { var x = v / units[i][0]; return num(x, sig(x)) + " " + units[i][1]; }
    }
    var last = units[units.length - 1], y = v / last[0];
    return num(y, y < 0.01 ? 4 : 3) + " " + last[1];
  }
  var W_UNITS = [[1e3, "кВт"], [1, "Вт"], [1e-3, "мВт"], [1e-6, "мкВт"], [1e-9, "нВт"], [1e-12, "пВт"],
    [1e-15, "фВт"], [1e-18, "аВт"]];
  var V_UNITS = [[1e3, "кВ"], [1, "В"], [1e-3, "мВ"], [1e-6, "мкВ"], [1e-9, "нВ"]];
  function fmtW(w) { return unitFmt(w, W_UNITS); }
  function fmtV(v) { return unitFmt(v, V_UNITS); }
  function dbmW(d) { return Math.pow(10, d / 10) / 1000; }
  function dbmV(d) { return Math.sqrt(dbmW(d) * 50); }
  function fmtDbm(d, k) { return num(d, k === undefined ? 1 : k) + " дБм"; }
  function fmtDb(d, k) { return (d > 0 ? "+" : "") + num(d, k === undefined ? 1 : k) + " дБ"; }
  function fmtBw(hz) { return hz >= 1e6 ? num(hz / 1e6, 1).replace(/,0$/, "") + " МГц" : num(hz / 1e3, hz < 1e5 ? 1 : 0).replace(/,0$/, "") + " кГц"; }
  function fmtLoss(x, k) { return x < 0.005 ? "0 дБ" : "\u2212" + num(x, k) + " дБ"; }
  function fmtKm(d) { return d < 1 ? num(d * 1000, 0) + " м" : num(d, d < 10 ? 2 : d < 100 ? 1 : 0) + " км"; }
  function fmtMHz(f) { var s = num(f, f < 10 ? 2 : 1); return s.replace(/,0$/, "") + " МГц"; }
  function fmtLen(m) { return m < 1 ? num(m * 100, 0) + " см" : num(m, m < 10 ? 2 : 1).replace(/,0+$/, "") + " м"; }
  function fmtField(e) { return unitFmt(e, [[1, "В/м"], [1e-3, "мВ/м"], [1e-6, "мкВ/м"], [1e-9, "нВ/м"]]); }
  function fmtTime(s) {
    return s >= 1e-3 ? num(s * 1e3, sig(s * 1e3)) + " мс" : num(s * 1e6, sig(s * 1e6)) + " мкс";
  }

  /* ------------------------------------------------------------------ кабели */
  // потери, дБ на 100 м ≈ a·√f + b·f (медь + диэлектрик), f в МГц; подогнано под паспорта производителей
  var CAB = { "RG-58": [1.521, 0.0079], "RG-6": [0.6537, 0.00063], "LMR-400": [0.3816, 0.0015] };
  function cableLoss(type, f, len) { var c = CAB[type]; return (c[0] * Math.sqrt(f) + c[1] * f) * len / 100; }

  /* ------------------------------------------------------------------ состояние и примеры */
  var S = {};
  var PRESETS = {
    pmr: { pTx: 27, txType: "RG-58", txLen: 0, gTx: 0, dist: 2, freq: 446.1, extra: 0, gRx: 0, lnaOn: false,
      gLna: 20, rxType: "RG-58", rxLen: 2, bw: 12500, sigBw: 11000, rq: "nfm", tGain: 30, nfRx: 4.5, nfLna: 0.8, conn: 1,
      note: "Рация PMR446: 0,5 Вт, штатная антенна ≈ 0 дБi, 2 км прямой видимости, приём на штырь и 2 м RG-58. " +
        "В городе за домами добавьте 20–30 дБ потерь ползунком «Доп. потери»." },
    fm: { pTx: 70, txType: "LMR-400", txLen: 50, gTx: 6, dist: 50, freq: 100, extra: 0, gRx: 0, lnaOn: false,
      gLna: 20, rxType: "RG-6", rxLen: 10, bw: 200000, sigBw: 180000, rq: "wfm", tGain: 0, nfRx: 4.5, nfLna: 0.8, conn: 2,
      note: "Вещательный передатчик 10 кВт, 50 м кабеля на мачту, антенная решётка 6 дБi (ЭИИМ около 23 кВт). " +
        "Приём за 50 км на диполь и 10 м RG-6: сигнал сильный, МШУ не нужен." },
    meteor: { pTx: 37, txType: "LMR-400", txLen: 1, gTx: 0, dist: 800, freq: 137.9, extra: 3, gRx: 2, lnaOn: true,
      gLna: 20, rxType: "RG-58", rxLen: 10, bw: 120000, sigBw: 120000, rq: "lrpt", tGain: 40, nfRx: 4.5, nfLna: 0.8, conn: 2,
      note: "«Метеор-М»: около 5 Вт на 137,9 МГц, 800 км — пролёт прямо над головой; 3 дБ — потери линейной антенны " +
        "на круговой поляризации. Получается около −100 дБм у антенны; на угле места 10° (≈ 2400 км) — ещё на " +
        "9,5 дБ меньше, у самого горизонта (≈ 3300 км) — на 12 дБ. На спектре SDR при RBW около 1–2 кГц сигнал шириной 120 кГц выглядит ещё на ~20 дБ ниже — " +
        "около −120 дБм в бине." },
    adsb: { pTx: 54, txType: "RG-58", txLen: 3, gTx: 0, dist: 100, freq: 1090, extra: 0, gRx: 3, lnaOn: false,
      gLna: 20, rxType: "RG-58", rxLen: 3, bw: 2000000, sigBw: 2000000, rq: "adsb", tGain: 49, nfRx: 4.5, nfLna: 0.8, conn: 2,
      note: "Ответчик самолёта: импульсы 250 Вт на 1090 МГц. За 100 км на коллинеарную антенну 3 дБi; на 1 ГГц " +
        "тонкий RG-58 уже заметно теряет — попробуйте 20 м кабеля без МШУ и с ним." }
  };

  var IDS = ["pTx", "txType", "txLen", "gTx", "dist", "freq", "extra", "gRx", "lnaOn", "gLna", "rxType", "rxLen",
    "bw", "nfRx", "nfLna", "conn", "thTx", "thRx"];

  function setControls(p) {
    IDS.forEach(function (k) {
      var el = document.getElementById(k), v = p[k];
      if (!el || v === undefined) { return; }
      if (k === "dist" || k === "freq") { el.value = LOG10(v); }
      else if (el.type === "checkbox") { el.checked = !!v; }
      else { el.value = v; }
    });
    S.note = p.note || "";
    S.sigBw = p.sigBw || 12500;
    ["thTx", "thRx"].forEach(function (k) { var el = document.getElementById(k); if (el) { el.value = p[k] || 0; } });
    var rq = document.getElementById("rqMode"), tg = document.getElementById("tGain");
    if (rq && p.rq) { rq.value = p.rq; }
    if (tg && p.tGain !== undefined) { tg.value = p.tGain; }
  }

  /* RSSI, S-метр, dBFS и запас по связи */
  var REQ = {  // требуемое С/Ш (дБ) для вида модуляции
    nfm: [12, "NFM речь: ≈ 12 дБ С/Ш в канале (с запасом выше порога FM; 12 дБ SINAD после демодулятора — уже при 5–8 дБ)"], wfm: [20, "WFM моно без шипения: ≈ 20 дБ"],
    dmr: [6, "DMR: ≈ 5–7 дБ"], lrpt: [6.5, "LRPT QPSK: ≈ 5–8 дБ"], adsb: [10, "ADS-B: ≈ 10 дБ"]
  };
  var FS_DBM = -10;  // условно: полная шкала АЦП на входе при усилении тюнера 0 дБ
  function sUnits(dbm) {
    var s9 = S.freq < 30 ? -73 : -93;
    if (dbm >= s9) { return "S9" + (dbm - s9 >= 0.5 ? "+" + num(dbm - s9, 0) + " дБ" : ""); }
    var s = 9 + (dbm - s9) / 6;
    return s < 1 ? "ниже S1" : "S" + Math.floor(s + 1e-9);
  }
  function renderRssi(r) {
    var box = document.getElementById("rssiBox");
    if (!box) { return; }
    var rqEl = document.getElementById("rqMode"), tgEl = document.getElementById("tGain");
    var req = REQ[rqEl.value] || REQ.nfm, tg = Number(tgEl.value);
    var cut = Math.max(0, 10 * LOG10(S.sigBw / S.bw));             // канал уже сигнала — часть мощности за фильтром
    var rssi = r.rxIn - cut;
    var nrssi = r.noiseAnt + r.lnaG - r.rxLoss;                     // шум в полосе канала на входе приёмника
    var snr = rssi - nrssi, margin = snr - req[0];
    var s9 = S.freq < 30 ? -73 : -93;
    var meas = 10 * LOG10(lin(rssi) + lin(nrssi));                    // приёмник меряет сумму сигнала и шума
    setLive("rssi", fmtDbm(meas));
    setLive("rssi_sub", "сам сигнал " + fmtDbm(rssi) + " · " + fmtV(dbmV(rssi)) +
      (cut > 0.05 ? " · канал уже сигнала: −" + num(cut, 1) + " дБ" : " · весь сигнал в канале"));
    setLive("rssi_noise", fmtDbm(nrssi));
    setLive("rssi_noise_sub", "шум в " + fmtBw(S.bw) + "; разница = С/Ш " + num(snr, 1) + " дБ");
    setLive("smeter", sUnits(meas));
    setLive("smeter_sub", "шум: " + sUnits(nrssi) + "; S9 = " + num(s9, 0) + " дБм (" + (S.freq < 30 ? "50" : "5") + " мкВ), " +
      (S.freq < 30 ? "ниже" : "выше") + " 30 МГц");
    setLive("margin", fmtDb(margin).replace("+", "+"));
    setLive("margin_sub", req[1] + "; нужно ≥ " + num(req[0], 1) + " дБ, есть " + num(snr, 1));
    var mk = document.getElementById("k-margin");
    if (mk) { mk.className = "kpi " + (margin >= 10 ? "good" : margin >= 3 ? "warn" : "bad"); }
    document.getElementById("o-tGain").textContent = num(tg, 0) + " дБ";
    var fsIn = FS_DBM - tg, dfs = rssi - fsIn, nfs = nrssi - fsIn;
    // шум квантования 8-битного АЦП (как у RTL-SDR, f_д = 2,4 МГц) в полосе канала
    var qn = -(6.02 * 8 + 1.76) - 10 * LOG10(2.4e6 / S.bw);   // комплексный IQ: шум по всей полосе f_д
    setLive("dbfs_q", num(qn, 1) + " дБFS");
    setLive("dbfs", num(dfs, 1) + " дБFS");
    setLive("dbfs_noise", num(nfs, 1) + " дБFS");
    setLive("dbfs_full", "≈ " + fmtDbm(fsIn, 0) + " на входе");
    var st = dfs > -1 ? ["bad", "клиппинг: сигнал упёрся в полную шкалу АЦП — уменьшите усиление"] :
      dfs > -10 ? ["warn", "на грани: запас до полной шкалы меньше 10 дБ"] :
        nfs < qn ? ["warn", "шум антенны ниже шума квантования АЦП в полосе канала: слабые сигналы теряются, " +
          "прибавьте усиление (сильным станциям это не мешает)"] :
          ["good", "нормально: есть запас до перегрузки, шум антенны выше шума квантования"];
    var stEl = document.getElementById("dbfs_state");
    if (stEl) { stEl.textContent = st[1]; stEl.style.color = C[st[0]]; }
    // мини-шкала S-метра
    var svg = document.getElementById("smeterSvg");
    while (svg.firstChild) { svg.removeChild(svg.firstChild); }
    var W = 760, L = 20, Rr = 740, lo = s9 - 54, hi = s9 + 60, X = function (d) { return L + (Rr - L) * (Math.max(lo, Math.min(hi, d)) - lo) / (hi - lo); };
    svg.appendChild(svgEl("rect", { x: L, y: 30, width: X(s9) - L, height: 10, rx: 3, fill: C.good, opacity: 0.55 }));
    svg.appendChild(svgEl("rect", { x: X(s9), y: 30, width: Rr - X(s9), height: 10, rx: 3, fill: C.bad, opacity: 0.55 }));
    for (var k = 1; k <= 9; k++) {
      var xk = X(s9 - (9 - k) * 6);
      svg.appendChild(svgEl("line", { x1: xk, x2: xk, y1: 26, y2: 44, stroke: C.fg, "stroke-width": 1 }));
      svg.appendChild(svgEl("text", { x: xk, y: 20, "text-anchor": "middle", "font-size": 12, fill: C.fg }, String(k)));
    }
    [20, 40, 60].forEach(function (p) {
      var xp = X(s9 + p);
      svg.appendChild(svgEl("line", { x1: xp, x2: xp, y1: 26, y2: 44, stroke: C.bad, "stroke-width": 1 }));
      svg.appendChild(svgEl("text", { x: xp, y: 20, "text-anchor": p === 60 ? "end" : "middle", "font-size": 12, fill: C.bad }, "+" + p));
    });
    var mark = function (d, col, lbl, up) {
      var xm = X(d);
      svg.appendChild(svgEl("polygon", { points: (xm - 6) + ",58 " + (xm + 6) + ",58 " + xm + ",46", fill: col }));
      svg.appendChild(svgEl("text", { x: Math.max(50, Math.min(W - 50, xm)), y: up ? 88 : 72, "text-anchor": "middle",
        "font-size": 12, "font-weight": 600, fill: col }, lbl));
    };
    mark(nrssi, C.noise, "шум " + sUnits(nrssi), false);
    mark(meas, C.accent, "показание " + sUnits(meas) + " (" + num(meas, 0) + " дБм)", true);
  }
  function readControls() {
    IDS.forEach(function (k) {
      var el = document.getElementById(k);
      if (!el) { S[k] = 0; return; }
      if (k === "dist" || k === "freq") { S[k] = Math.pow(10, Number(el.value)); }
      else if (el.type === "checkbox") { S[k] = el.checked; }
      else if (el.tagName === "SELECT" && k !== "bw") { S[k] = el.value; }
      else { S[k] = Number(el.value); }
    });
  }

  /* ------------------------------------------------------------------ расчёт тракта */
  function lin(db) { return Math.pow(10, db / 10); }
  function nfSystem(o) {
    // коэффициент шума системы, приведённый к выходу антенны (формула Фрииса)
    var Lr = lin(o.rxLoss), Frx = lin(o.nfRx), Fl = lin(o.nfLna), G = lin(o.gLna), F;
    if (o.place === "ant") { F = Fl + (Lr - 1) / G + (Frx - 1) * Lr / G; }
    else if (o.place === "rx") { F = Lr + (Fl - 1) * Lr + (Frx - 1) * Lr / G; }
    else { F = Lr + (Frx - 1) * Lr; }
    return 10 * LOG10(F);
  }
  function calc(s) {
    var r = {}, f = s.freq, conn = 0.2 * s.conn;
    r.exc = 0;                                       // возбудитель: 1 мВт
    r.pa = s.pTx;
    r.paGain = r.pa - r.exc;
    r.txLoss = s.txLen > 0 ? cableLoss(s.txType, f, s.txLen) + conn : 0;
    r.txOut = r.pa - r.txLoss;
    r.antLoss = -10 * LOG10(antEff(s.gTx));           // потери в самой антенне: у укороченного штыря < 1,76 дБi
    r.prad = r.txOut - r.antLoss;                     // полная излучаемая мощность — от направленности не зависит
    r.gTxEff = antGainAt(s.gTx, s.thTx || 0);         // усиление в сторону приёмника по диаграмме
    r.gRxEff = antGainAt(s.gRx, s.thRx || 0);
    r.eirp = r.txOut + r.gTxEff;                      // эквивалент на приёмник (G уже включает КПД), не энергия
    r.fspl = 20 * LOG10(s.dist) + 20 * LOG10(f) + 32.44;
    r.air = r.eirp - r.fspl - s.extra;
    r.rxAnt = r.air + r.gRxEff;
    r.lnaG = s.lnaOn ? s.gLna : 0;
    r.lna = r.rxAnt + r.lnaG;
    r.rxLoss = cableLoss(s.rxType, f, s.rxLen) + conn;
    r.rxIn = r.lna - r.rxLoss;
    var base = { rxLoss: r.rxLoss, nfRx: s.nfRx, nfLna: s.nfLna, gLna: s.gLna };
    r.nfSys = nfSystem(Object.assign({ place: s.lnaOn ? "ant" : "none" }, base));
    r.bwDb = 10 * LOG10(s.bw);
    r.noiseAnt = -174 + r.nfSys + r.bwDb;           // шум в канале, приведённый к выходу антенны
    r.snr = r.rxAnt - r.noiseAnt;
    r.scen = ["none", "ant", "rx"].map(function (p) {
      var nf = nfSystem(Object.assign({ place: p }, base));
      return { place: p, nf: nf, noise: -174 + nf + r.bwDb, snr: r.rxAnt - (-174 + nf + r.bwDb) };
    });
    r.eirpW = dbmW(r.eirp);
    r.efield = Math.sqrt(30 * r.eirpW) / (s.dist * 1000) * Math.pow(10, -s.extra / 20);
    r.lambda = 300 / f;
    r.leff = r.lambda / Math.PI;
    r.uoc = r.efield * r.leff;
    r.aeff = lin(r.gRxEff) * r.lambda * r.lambda / (4 * Math.PI);
    r.delay = s.dist / 299792.458;
    return r;
  }

  var R = null;
  var NODES = [
    { k: "src", name: "Возбудитель (источник)", short: "Источник", st: "tx" },
    { k: "pa", name: "После усилителя мощности", short: "Усилитель", st: "tx" },
    { k: "txfeed", name: "После фидера передатчика", short: "Фидер", st: "tx" },
    { k: "prad", name: "Излучено антенной (полная мощность)", short: "Излучено", st: "tx" },
    { k: "air", name: "У приёмной антенны (изотропной)", short: "Эфир", st: "air" },
    { k: "rxant", name: "На выходе приёмной антенны", short: "Антенна", st: "rx" },
    { k: "lna", name: "После МШУ", short: "МШУ", st: "rx" },
    { k: "rxin", name: "На входе приёмника", short: "Вход SDR", st: "rx" }
  ];
  function nodeVals(r) {
    return {
      src: [r.exc, "—"], pa: [r.pa, fmtDb(r.paGain)], txfeed: [r.txOut, S.txLen > 0 ? fmtLoss(r.txLoss, 2) : "нет кабеля"],
      prad: [r.prad, r.antLoss > 0.005 ? "КПД " + num(100 / lin(r.antLoss), 0) + " % (" + fmtLoss(r.antLoss, 1) + ": потери в антенне — часть мощности её греет)" : "КПД 100 % (0 дБ)"], air: [r.air, fmtLoss(r.fspl + S.extra, 1) + ", G(θ) " + fmtDb(r.gTxEff) + "i"],
      rxant: [r.rxAnt, "G(θ) " + fmtDb(r.gRxEff) + "i"], lna: [r.lna, S.lnaOn ? fmtDb(r.lnaG) : "нет МШУ"],
      rxin: [r.rxIn, fmtLoss(r.rxLoss, 2)]
    };
  }
  function levelText(d) { return fmtDbm(d) + " · " + fmtW(dbmW(d)) + " · " + fmtV(dbmV(d)); }

  function setLive(key, text) {
    $$('[data-live="' + key + '"]').forEach(function (el) { el.textContent = text; });
  }

  function update() {
    readControls();
    var r = R = calc(S);
    // подписи ползунков
    var o = function (id, t) { var el = document.getElementById("o-" + id); if (el) { el.textContent = t; } };
    o("pTx", fmtW(dbmW(S.pTx)) + " · " + fmtDbm(S.pTx, 1));
    o("txLen", S.txLen > 0 ? fmtLen(S.txLen) + " · " + fmtLoss(r.txLoss, 2) : "нет кабеля");
    o("gTx", num(S.gTx, 1) + " дБi");
    o("thTx", num(S.thTx, 0) + "° → G = " + num(r.gTxEff, 1) + " дБi");
    o("dist", fmtKm(S.dist));
    o("freq", fmtMHz(S.freq) + " · λ = " + fmtLen(r.lambda));
    o("extra", fmtLoss(S.extra, 0));
    o("gRx", num(S.gRx, 1) + " дБi");
    o("thRx", num(S.thRx, 0) + "° → G = " + num(r.gRxEff, 1) + " дБi");
    o("gLna", "+" + num(S.gLna, 1) + " дБ" + (S.lnaOn ? "" : " (выкл.)"));
    o("rxLen", (S.rxLen > 0 ? fmtLen(S.rxLen) : "только разъёмы") + " · " + fmtLoss(r.rxLoss, 2));
    o("nfRx", num(S.nfRx, 1) + " дБ");
    o("nfLna", num(S.nfLna, 1) + " дБ");
    o("conn", String(S.conn));
    document.getElementById("gLna").disabled = !S.lnaOn;

    var nv = nodeVals(r);
    $$("[data-node]").forEach(function (el) { el.textContent = fmtDbm(nv[el.getAttribute("data-node")][0], 0); });
    var nodeL = document.getElementById("nodeLna");
    nodeL.classList.toggle("off", !S.lnaOn);
    if (!S.lnaOn) { nodeL.querySelector(".lv").textContent = "нет"; }

    // живые цифры в подробностях
    setLive("exc_full", levelText(r.exc));
    setLive("exc_short", fmtDbm(r.exc, 0));
    setLive("pa_gain", fmtDb(r.paGain) + " (×" + num(lin(r.paGain), lin(r.paGain) < 10 ? 1 : 0) + ")");
    setLive("pa_full", levelText(r.pa));
    setLive("pa_short", fmtW(dbmW(r.pa)));
    setLive("tx_cable", S.txLen > 0 ? S.txType + ", " + fmtLen(S.txLen) : "нет (антенна на корпусе)");
    setLive("txloss", fmtLoss(r.txLoss, 2) + " (" + num(100 * (1 - 1 / lin(r.txLoss)), 0) + " % мощности)");
    setLive("txloss_short", fmtLoss(r.txLoss, 1));
    setLive("txfeed_full", levelText(r.txOut));
    setLive("prad_full", levelText(r.prad));
    setLive("prad_short", fmtW(dbmW(r.prad)));
    setLive("gtx_eff", num(r.gTxEff, 1) + " дБi (угол " + num(S.thTx, 0) + "°)");
    setLive("eirp_full", fmtW(r.eirpW) + " · " + fmtDbm(r.eirp));
    setLive("eirp_short", "излучено " + fmtW(dbmW(r.prad)));
    setLive("aeff_eff", num(r.gRxEff, 1) + " дБi в сторону волны → A = " +
      (r.aeff >= 0.01 ? num(r.aeff, sig(r.aeff)) + " м²" : num(r.aeff * 1e4, sig(r.aeff * 1e4)) + " см²"));
    setLive("erp", fmtW(dbmW(r.eirp - 2.15)));
    setLive("lambda", fmtLen(r.lambda));
    setLive("dist", fmtKm(S.dist));
    setLive("fspl", "−" + num(r.fspl, 1) + " дБ");
    setLive("air_short", "−" + num(r.fspl + S.extra, 0) + " дБ");
    var ratio = lin(r.fspl);
    setLive("air_units", "мощность меньше в 10^" + num(LOG10(ratio), 1) + " раз");
    setLive("efield", fmtField(r.efield) + " · " + num(20 * LOG10(r.efield * 1e6), 1) + " дБмкВ/м");
    setLive("delay", fmtTime(r.delay));
    setLive("leff", fmtLen(r.leff));
    setLive("uoc", fmtV(r.uoc));
    setLive("uoc_half", fmtV(r.uoc / 2) + " на 73 Ом");
    setLive("aeff", r.aeff >= 0.01 ? num(r.aeff, sig(r.aeff)) + " м²" : num(r.aeff * 1e4, sig(r.aeff * 1e4)) + " см²");
    setLive("rxant_full", levelText(r.rxAnt));
    setLive("rxant_short", fmtDbm(r.rxAnt, 0));
    setLive("lna_gain", S.lnaOn ? fmtDb(r.lnaG) + " (×" + num(lin(r.lnaG), 0) + " по мощности)" : "выключен");
    setLive("lna_volt", S.lnaOn ? fmtV(dbmV(r.rxAnt)) + " → " + fmtV(dbmV(r.lna)) + " (×" +
      num(Math.pow(10, r.lnaG / 20), 1) + ")" : "—");
    setLive("lna_full", levelText(r.lna));
    setLive("lna_short", S.lnaOn ? fmtDb(r.lnaG) : "нет");
    setLive("rx_cable", S.rxType + ", " + fmtLen(S.rxLen) + ", разъёмов " + S.conn);
    setLive("rxloss", fmtLoss(r.rxLoss, 2));
    setLive("rxloss_short", fmtLoss(r.rxLoss, 1));
    setLive("rxin_full", levelText(r.rxIn));
    setLive("rxin_dbm", fmtDbm(r.rxIn));
    setLive("rxin_units", fmtW(dbmW(r.rxIn)) + " · " + fmtV(dbmV(r.rxIn)) +
      (r.rxIn > -30 ? " — риск перегрузки (например, RTL-SDR)" : ""));
    setLive("snr", fmtDb(r.snr).replace("+", ""));
    var verdict = r.snr > 20 ? ["good", "уверенный чистый приём"] : r.snr > 10 ? ["good", "приём есть"] :
      r.snr > 3 ? ["warn", "на грани, с шумом"] : ["bad", "сигнал тонет в шуме"];
    setLive("snr_text", verdict[1] + "; шум " + fmtDbm(r.noiseAnt, 0) + ", КШ " + num(r.nfSys, 1) + " дБ");
    document.getElementById("k-snr").className = "kpi " + verdict[0];
    document.getElementById("k-in").className = "kpi " + (r.rxIn > -30 ? "warn" : "");
    document.getElementById("presetNote").textContent = S.note || "";

    renderTable(r, nv);
    renderBudget(r, nv);
    renderLnaBars(r);
    renderRssi(r);
    renderCables();
    ["pattern", "airchart", "rxant"].forEach(function (k) { if (DEMOS[k]) { DEMOS[k].dirty = true; } });
  }

  function renderTable(r, nv) {
    var tb = $("#budgetTable tbody"), html = "";
    NODES.forEach(function (n) {
      var v = nv[n.k], d = v[0];
      html += '<tr class="st ' + n.st + '"><td>' + n.name + "</td><td class=\"n\">" + v[1] + '</td><td class="n">' +
        num(d, 1) + '</td><td class="n">' + fmtW(dbmW(d)) + '</td><td class="n">' + fmtV(dbmV(d)) + "</td></tr>";
      if (n.k === "prad") {
        html += '<tr class="st tx"><td><i>ЭИИМ в сторону приёмника — эквивалент, не энергия</i><br><span class="small dim">' +
          "столько пришлось бы подать на изотропную антенну</span></td><td class=\"n\">G(θ) " + fmtDb(r.gTxEff) +
          'i</td><td class="n"><i>' + num(r.eirp, 1) + '</i></td><td class="n"><i>' + fmtW(r.eirpW) +
          '</i></td><td class="n dim">—</td></tr>';
      }
    });
    html += '<tr><td>Шум в канале ' + fmtBw(S.bw) + ' (к выходу антенны)</td><td class="n">КШ ' +
      num(r.nfSys, 1) + ' дБ</td><td class="n">' + num(r.noiseAnt, 1) + '</td><td class="n">' + fmtW(dbmW(r.noiseAnt)) +
      '</td><td class="n">' + fmtV(dbmV(r.noiseAnt)) + "</td></tr>";
    var rs = r.rxIn - Math.max(0, 10 * LOG10((S.sigBw || S.bw) / S.bw));
    html += '<tr class="st rx"><td><a href="#rssi">RSSI в канале</a> (что измерит приёмник)</td><td class="n">' + sUnits(rs) +
      '</td><td class="n">' + num(rs, 1) + '</td><td class="n">' + fmtW(dbmW(rs)) + '</td><td class="n">' + fmtV(dbmV(rs)) +
      "</td></tr>";
    tb.innerHTML = html;
  }

  var SVGNS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs, text) {
    var e = document.createElementNS(SVGNS, tag);
    for (var k in attrs) { if (Object.prototype.hasOwnProperty.call(attrs, k)) { e.setAttribute(k, attrs[k]); } }
    if (text !== undefined) { e.textContent = text; }
    return e;
  }
  function renderBudget(r, nv) {
    var svg = document.getElementById("budgetSvg");
    while (svg.firstChild) { svg.removeChild(svg.firstChild); }
    var W = 860, H = 340, L = 56, Rm = 18, T = 26, B = 64;
    var vals = NODES.map(function (n) { return nv[n.k][0]; });
    var noiseRx = [r.noiseAnt, r.noiseAnt + r.lnaG, r.noiseAnt + r.lnaG - r.rxLoss];
    var all = vals.concat(noiseRx, [r.eirp]);
    var mx = Math.ceil((Math.max.apply(null, all) + 8) / 20) * 20, mn = Math.floor((Math.min.apply(null, all) - 8) / 20) * 20;
    var step = (mx - mn) > 160 ? 40 : 20;
    var x = function (i) { return L + (W - L - Rm) * (i + 0.5) / NODES.length; };
    var y = function (d) { return T + (H - T - B) * (mx - d) / (mx - mn); };
    var font = { "font-size": 12, fill: C.dim };
    for (var g = mn; g <= mx; g += step) {
      svg.appendChild(svgEl("line", { x1: L, x2: W - Rm, y1: y(g), y2: y(g), stroke: C.grid, "stroke-width": 1 }));
      svg.appendChild(svgEl("text", Object.assign({ x: L - 6, y: y(g) + 4, "text-anchor": "end" }, font), num(g, 0)));
    }
    svg.appendChild(svgEl("text", Object.assign({ x: 8, y: 14 }, font), "дБм"));
    // зоны этапов
    var stages = [["tx", 0, 3, "Передатчик"], ["air", 4, 4, "Эфир"], ["rx", 5, 7, "Приём"]];
    stages.forEach(function (s) {
      var x0 = x(s[1]) - (W - L - Rm) / NODES.length / 2 + 3, x1 = x(s[2]) + (W - L - Rm) / NODES.length / 2 - 3;
      svg.appendChild(svgEl("rect", { x: x0, y: H - B + 34, width: x1 - x0, height: 4, rx: 2, fill: C[s[0]] }));
      svg.appendChild(svgEl("text", { x: (x0 + x1) / 2, y: H - B + 54, "text-anchor": "middle", "font-size": 12,
        fill: C[s[0]], "font-weight": 600 }, s[3]));
    });
    // перегрузка RTL-SDR
    if (-30 < mx && -30 > mn) {
      svg.appendChild(svgEl("line", { x1: x(5) - 30, x2: W - Rm, y1: y(-30), y2: y(-30), stroke: C.warn,
        "stroke-dasharray": "3 4", "stroke-width": 1.2 }));
      svg.appendChild(svgEl("text", { x: W - Rm - 2, y: y(-30) - 4, "text-anchor": "end", "font-size": 11, fill: C.warn },
        "перегрузка (≈ RTL-SDR)"));
    }
    // шум
    var np = [5, 6, 7].map(function (i, j) { return x(i) + "," + y(noiseRx[j]); }).join(" ");
    svg.appendChild(svgEl("polyline", { points: np, fill: "none", stroke: C.noise, "stroke-width": 2,
      "stroke-dasharray": "6 5" }));
    svg.appendChild(svgEl("text", { x: x(5) - 10, y: y(noiseRx[0]) + 16, "text-anchor": "middle", "font-size": 11,
      fill: C.noise }, "шум"));
    // С/Ш стрелкой
    var xs = x(5) + 14;
    svg.appendChild(svgEl("line", { x1: xs, x2: xs, y1: y(vals[5]) + 6, y2: y(noiseRx[0]) - 4, stroke: C.dim,
      "stroke-width": 1 }));
    svg.appendChild(svgEl("text", { x: xs + 4, y: (y(vals[5]) + y(noiseRx[0])) / 2 + 4, "font-size": 11, fill: C.dim },
      "С/Ш " + num(r.snr, 0) + " дБ"));
    // линия уровня
    for (var i = 1; i < NODES.length; i++) {
      svg.appendChild(svgEl("line", { x1: x(i - 1), y1: y(vals[i - 1]), x2: x(i), y2: y(vals[i]),
        stroke: C[NODES[i].st], "stroke-width": 3, "stroke-linecap": "round",
        opacity: (NODES[i].k === "lna" && !S.lnaOn) ? 0.5 : 1 }));
    }
    // ЭИИМ — не ступенька мощности, а эквивалент в направлении лепестка: отдельный пунктирный маркер
    var xe = x(3) + 22, ye = y(r.eirp);
    svg.appendChild(svgEl("line", { x1: x(3), y1: y(vals[3]), x2: xe, y2: ye, stroke: C.tx, "stroke-width": 1.5,
      "stroke-dasharray": "4 4", opacity: 0.8 }));
    svg.appendChild(svgEl("circle", { cx: xe, cy: ye, r: 5, fill: "none", stroke: C.tx, "stroke-width": 2,
      "stroke-dasharray": "3 2" }));
    svg.appendChild(svgEl("text", { x: xe + 8, y: ye + (r.eirp >= vals[3] ? -6 : 14), "font-size": 11, fill: C.tx },
      "ЭИИМ " + num(r.eirp, 0) + " (экв.)"));
    NODES.forEach(function (n, j) {
      var cx = x(j), cy = y(vals[j]);
      var a = svgEl("a", { href: "#" + ({ src: "b-src", pa: "b-pa", txfeed: "b-txfeed", prad: "b-txant", air: "b-air",
        rxant: "b-rxant", lna: "b-lna", rxin: "b-rxfeed" })[n.k] });
      a.appendChild(svgEl("circle", { cx: cx, cy: cy, r: 6, fill: C.panel, stroke: C[n.st], "stroke-width": 3 }));
      svg.appendChild(a);
      var above = j === 0 || vals[j] >= vals[j - 1] - 1;
      svg.appendChild(svgEl("text", { x: cx, y: above ? cy - 12 : cy + 20, "text-anchor": "middle", "font-size": 12,
        "font-weight": 600, fill: C.fg }, num(vals[j], 0)));
      svg.appendChild(svgEl("text", { x: cx, y: H - B + 20, "text-anchor": "middle", "font-size": 12, fill: C.fg },
        n.short));
    });
  }

  function renderLnaBars(r) {
    var box = document.getElementById("lnaBars");
    var names = { none: "Без МШУ", ant: "МШУ у антенны", rx: "МШУ у приёмника" };
    var max = Math.max(40, Math.max.apply(null, r.scen.map(function (s) { return s.snr; })) + 5);
    var html = "";
    r.scen.forEach(function (s) {
      var w = Math.max(0, Math.min(100, s.snr / max * 100));
      var cur = (S.lnaOn && s.place === "ant") || (!S.lnaOn && s.place === "none");
      html += '<div class="bar"><span>' + (cur ? "<b>" + names[s.place] + "</b>" : names[s.place]) +
        '<br><span class="small dim">КШ ' + num(s.nf, 1) + ' дБ</span></span><div class="track"><div class="fill" style="width:' +
        w.toFixed(1) + "%;background:" + (s.snr > 10 ? C.good : s.snr > 3 ? C.warn : C.bad) + '"></div></div><span class="val">' +
        num(s.snr, 1) + " дБ</span></div>";
    });
    html += '<p class="small dim">Сигнал у антенны ' + fmtDbm(r.rxAnt) + ", кабель −" + num(r.rxLoss, 1) +
      " дБ, полоса " + fmtBw(S.bw) + ", МШУ +" + num(S.gLna, 0) + " дБ с КШ " +
      num(S.nfLna, 1) + " дБ.</p>";
    box.innerHTML = html;
  }

  var CAB_F = [30, 100, 146, 446, 1090, 1700];
  function renderCables() {
    [["cableTable1", S.txType], ["cableTable2", S.rxType]].forEach(function (t) {
      var el = document.getElementById(t[0]);
      var html = "<thead><tr><th>Потери на 10 м</th>" + CAB_F.map(function (f) { return "<th>" + fmtMHz(f) + "</th>"; }).join("") +
        "</tr></thead><tbody>";
      Object.keys(CAB).forEach(function (k) {
        html += "<tr" + (k === t[1] ? ' style="background:' + alpha(C.accent, 0.12) + '"' : "") + "><td><b>" + k + "</b></td>" +
          CAB_F.map(function (f) { return '<td class="n">' + num(cableLoss(k, f, 10), 1) + "</td>"; }).join("") + "</tr>";
      });
      html += '<tr><td colspan="' + (CAB_F.length + 1) + '" class="small dim">дБ; на частоте ' + fmtMHz(S.freq) +
        ": RG-58 — " + num(cableLoss("RG-58", S.freq, 10), 1) + ", RG-6 — " + num(cableLoss("RG-6", S.freq, 10), 1) +
        ", LMR-400 — " + num(cableLoss("LMR-400", S.freq, 10), 1) + " дБ на 10 м</td></tr></tbody>";
      el.innerHTML = html;
    });
  }

  /* ------------------------------------------------------------------ движок анимаций */
  var DEMOS = {};
  var paused = false;
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced) { paused = true; }

  function demo(name, anim, draw) { DEMOS[name] = { name: name, anim: anim, draw: draw, t: 0, dirty: true, visible: false }; }

  function setupCanvas(d) {
    var cv = d.cv;
    if (!cv.dataset.h) { cv.dataset.h = cv.getAttribute("height"); }
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    var w = cv.clientWidth, h = Number(cv.dataset.h);
    if (!w) { return false; }
    cv.style.height = h + "px";
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
    }
    d.ctx = cv.getContext("2d");
    d.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    d.w = w;
    d.h = h;
    d.ready = true;
    return true;
  }

  var io = "IntersectionObserver" in window ? new IntersectionObserver(function (ents) {
    ents.forEach(function (e) {
      var d = DEMOS[e.target.getAttribute("data-demo")];
      if (d) { d.visible = e.isIntersecting; if (d.visible) { d.dirty = true; } }
    });
  }, { rootMargin: "60px" }) : null;
  var ro = "ResizeObserver" in window ? new ResizeObserver(function (ents) {
    ents.forEach(function (e) {
      var d = DEMOS[e.target.getAttribute("data-demo")];
      if (d) { d.ready = false; d.dirty = true; }
    });
  }) : null;

  function bindCanvases() {
    $$("canvas[data-demo]").forEach(function (cv) {
      var d = DEMOS[cv.getAttribute("data-demo")];
      if (!d) { return; }
      d.cv = cv;
      cv.dataset.h = cv.getAttribute("height");
      cv.style.height = cv.dataset.h + "px";
      if (io) { io.observe(cv); } else { d.visible = true; }
      if (ro) { ro.observe(cv); }
    });
  }

  var last = 0;
  function frame(ts) {
    var dt = last ? Math.min(0.1, (ts - last) / 1000) : 0;
    last = ts;
    var hidden = document.hidden;
    for (var k in DEMOS) {
      if (!Object.prototype.hasOwnProperty.call(DEMOS, k)) { continue; }
      var d = DEMOS[k];
      if (!d.cv || !d.visible || hidden) { continue; }
      var run = d.anim && !paused;
      if (!run && !d.dirty) { continue; }
      if (!d.ready && !setupCanvas(d)) { continue; }
      if (run) { d.t += dt; }
      d.ctx.clearRect(0, 0, d.w, d.h);
      try { d.draw(d, run ? dt : 0); } catch (err) { if (window.console) { console.error(k, err); } d.anim = false; }
      d.dirty = false;
    }
    requestAnimationFrame(frame);
  }

  /* ------------------------------------------------------------------ рисование: помощники */
  var FONT = "12px 'Segoe UI', system-ui, sans-serif";
  function text(ctx, s, x, y, col, align, font) {
    ctx.fillStyle = col || C.dim;
    ctx.font = font || FONT;
    ctx.textAlign = align || "left";
    ctx.textBaseline = "alphabetic";
    ctx.fillText(s, x, y);
  }
  function line(ctx, pts, col, lw, dash) {
    ctx.beginPath();
    for (var i = 0; i < pts.length; i++) { if (i) { ctx.lineTo(pts[i][0], pts[i][1]); } else { ctx.moveTo(pts[i][0], pts[i][1]); } }
    ctx.strokeStyle = col;
    ctx.lineWidth = lw || 1.5;
    ctx.setLineDash(dash || []);
    ctx.stroke();
    ctx.setLineDash([]);
  }
  function plot(ctx, x0, x1, n, fx, col, lw, dash) {
    ctx.beginPath();
    for (var i = 0; i <= n; i++) {
      var x = x0 + (x1 - x0) * i / n, y = fx(x, i / n);
      if (i) { ctx.lineTo(x, y); } else { ctx.moveTo(x, y); }
    }
    ctx.strokeStyle = col;
    ctx.lineWidth = lw || 1.5;
    ctx.setLineDash(dash || []);
    ctx.stroke();
    ctx.setLineDash([]);
  }
  function hline(ctx, x0, x1, y, col, dash) { line(ctx, [[x0, y], [x1, y]], col, 1, dash); }
  function hash(i) {
    var x = (i | 0) * 2654435761 ^ 0x5bd1e995;
    x = Math.imul(x ^ (x >>> 15), 0x2c1b3c6d);
    x = Math.imul(x ^ (x >>> 12), 0x297a2d39);
    return ((x ^ (x >>> 15)) >>> 0) / 4294967296;
  }
  var spare = null;
  function gauss() {
    if (spare !== null) { var s = spare; spare = null; return s; }
    var u = Math.random() || 1e-9, v = Math.random(), m = Math.sqrt(-2 * Math.log(u));
    spare = m * Math.sin(TAU * v);
    return m * Math.cos(TAU * v);
  }
  function erfc(x) {  // Абрамовиц–Стиган 7.1.26
    var z = Math.abs(x), t = 1 / (1 + 0.3275911 * z);
    var y = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429)))) * Math.exp(-z * z);
    return x >= 0 ? y : 2 - y;
  }
  function segBind(id, cb) {
    var box = document.getElementById(id);
    box.addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (!b) { return; }
      $$("button", box).forEach(function (x) { x.classList.toggle("on", x === b); });
      cb(b.getAttribute("data-v"));
    });
    var on = $("button.on", box);
    return on ? on.getAttribute("data-v") : null;
  }

  /* ------------------------------------------------------------------ 1. источник */
  var srcMode = segBind("srcMode", function (v) { srcMode = v; });
  function voice(u) {
    var env = 0.35 + 0.65 * Math.abs(Math.sin(u * 1.3)) * (0.6 + 0.4 * Math.sin(u * 0.37));
    return env * (0.55 * Math.sin(u * 19) + 0.3 * Math.sin(u * 41 + 1) + 0.15 * Math.sin(u * 83 + 2));
  }
  demo("src", true, function (d) {
    var ctx = d.ctx, w = d.w, h = d.h, cy = h / 2, a = h * 0.36;
    hline(ctx, 10, w - 10, cy, C.grid);
    if (srcMode === "voice") {
      plot(ctx, 10, w - 10, Math.round(w), function (x) { return cy - a * voice(x / 40 + d.t * 3); }, C.tx, 1.8);
      text(ctx, "Микрофон: несколько милливольт, звук 100 Гц – 10 кГц", 12, 18, C.dim);
    } else {
      var bw = 26, off = (d.t * 40) % bw, n0 = Math.floor(d.t * 40 / bw);
      var pts = [];
      for (var i = -1; i * bw - off < w; i++) {
        var b = hash(n0 + i) > 0.5 ? 1 : 0, x0 = 10 + i * bw - off, yb = cy - (b ? a : -a) * 0.8;
        pts.push([x0, yb], [x0 + bw, yb]);
        text(ctx, String(b), x0 + bw / 2, cy + a + 18, C.fg, "center");
      }
      ctx.save();
      ctx.beginPath();
      ctx.rect(10, 0, w - 20, h);
      ctx.clip();
      line(ctx, pts, C.tx, 2);
      ctx.restore();
      text(ctx, "Данные: нули и единицы, например 9600 бит/с", 12, 18, C.dim);
    }
  });

  /* ------------------------------------------------------------------ 2. АЦП */
  function adcSig(t) { return 0.6 * Math.sin(TAU * 1000 * t) + 0.35 * Math.sin(TAU * 3000 * t + 0.5); }
  // ползунок разрядности — номер в этом списке
  var ADC_BITS = [1, 2, 3, 4, 5, 6, 8, 12, 14, 16, 24, 32];
  var ADC_EX = {
    1: "1-битный АЦП (дельта-сигма до фильтра)", 2: "грубая учебная оцифровка", 3: "грубая учебная оцифровка",
    4: "старые игровые приставки", 5: "грубая учебная оцифровка", 6: "грубая учебная оцифровка",
    8: "RTL-SDR, HackRF", 12: "Airspy R2/Mini, LimeSDR, PlutoSDR", 14: "SDRplay RSP1A/RSPdx (14 бит до ~6 МГц, выше 12/10/8)",
    16: "профессиональные SDR, CD-звук", 24: "звуковые карты (реально ≈ 18–21 бит ENOB)",
    32: "формат хранения в программе (int32/float32), не АЦП в радио"
  };
  function adcState() {
    var fs = Number(document.getElementById("adcFs").value) * 1000;
    var i = Math.max(0, Math.min(ADC_BITS.length - 1, Number(document.getElementById("adcBits").value)));
    var bits = ADC_BITS[i];
    return { fs: fs, bits: bits, L: Math.pow(2, bits) };
  }
  function quant(x, L) {
    var k = Math.round((Math.max(-1, Math.min(1, x)) + 1) / 2 * (L - 1));
    return { k: k, v: L > 1 ? k / (L - 1) * 2 - 1 : 0 };
  }
  function adcUpdate() {
    var s = adcState();
    document.getElementById("o-adcFs").textContent = num(s.fs / 1000, 0) + " кГц";
    document.getElementById("o-adcBits").textContent = s.bits + " бит · ДД ≈ " + num(6.02 * s.bits + 1.76, 0) + " дБ";
    var nyq = s.fs / 2;
    document.getElementById("adcNyq").textContent = num(nyq / 1000, 1) + " кГц" + (nyq <= 3000 ? " — тон 3 кГц искажён!" : "");
    document.getElementById("adcNyq").style.color = nyq <= 3000 ? C.bad : "";
    document.getElementById("adcLev").textContent = num(s.L, 0);
    document.getElementById("adcSnr").textContent = "≈ " + num(6.02 * s.bits + 1.76, 1) + " дБ";
    document.getElementById("adcRate").textContent = num(s.fs * s.bits / 1000, 0) + " кбит/с";
    var ex = document.getElementById("adcEx");
    if (ex) { ex.textContent = ADC_EX[s.bits] || ""; }
    var codes = [], hex = s.bits > 8, nd = hex ? Math.ceil(s.bits / 4) : s.bits;
    for (var n = 0; n < (hex ? 4 : 8); n++) {
      var q = quant(adcSig(n / s.fs), s.L).k.toString(hex ? 16 : 2).toUpperCase();
      while (q.length < nd) { q = "0" + q; }
      codes.push(hex ? "0x" + q : q);
    }
    document.getElementById("adcCodes").textContent = "Коды" + (hex ? " (шестнадцатерично)" : "") + ": " +
      codes.join(" ") + " …";
    $$("#adcTable tr[data-bits]").forEach(function (tr) {
      tr.classList.toggle("cur", Number(tr.getAttribute("data-bits")) === s.bits);
    });
    if (DEMOS.adc) { DEMOS.adc.dirty = true; }
  }
  demo("adc", false, function (d) {
    var ctx = d.ctx, w = d.w, h = d.h, s = adcState();
    var L0 = 12, R0 = w - 10, T0 = 14, B0 = h - 24, span = 0.002;  // 2 мс
    var X = function (t) { return L0 + (R0 - L0) * t / span; };
    var Y = function (v) { return (T0 + B0) / 2 - (B0 - T0) / 2 * v / 1.05; };
    if (s.L <= 32) {
      for (var k = 0; k < s.L; k++) { hline(ctx, L0, R0, Y(k / (s.L - 1 || 1) * 2 - 1), alpha(C.grid, 0.9)); }
    }
    hline(ctx, L0, R0, Y(0), C.grid);
    // аналоговый сигнал
    plot(ctx, L0, R0, 400, function (x) { return Y(adcSig((x - L0) / (R0 - L0) * span)); }, C.dim, 1.2);
    // восстановление по отсчётам (интерполяция sinc) — показывает наложение спектров
    var ns = Math.ceil(span * s.fs), qs = {};
    for (var n = -40; n <= ns + 40; n++) { qs[n] = quant(adcSig(n / s.fs), s.L).v; }
    plot(ctx, L0, R0, 300, function (x) {
      var t = (x - L0) / (R0 - L0) * span, u = t * s.fs, acc = 0;
      for (var m = Math.floor(u) - 40; m <= Math.floor(u) + 40; m++) {
        var z = u - m;
        acc += (qs[m] !== undefined ? qs[m] : 0) * (Math.abs(z) < 1e-9 ? 1 : Math.sin(Math.PI * z) / (Math.PI * z));
      }
      return Y(acc);
    }, C.tx, 1.4, [5, 4]);
    // ступеньки и отсчёты
    var pts = [];
    for (n = 0; n <= ns; n++) {
      var t0 = n / s.fs, v = qs[n], x0 = X(t0), x1 = Math.min(R0, X((n + 1) / s.fs));
      pts.push([x0, Y(v)], [x1, Y(v)]);
    }
    line(ctx, pts, C.accent, 2);
    for (n = 0; n <= ns; n++) {
      var xx = X(n / s.fs);
      line(ctx, [[xx, Y(0)], [xx, Y(adcSig(n / s.fs))]], alpha(C.accent, 0.45), 1);
      ctx.beginPath();
      ctx.arc(xx, Y(adcSig(n / s.fs)), 3, 0, TAU);
      ctx.fillStyle = C.accent;
      ctx.fill();
    }
    text(ctx, "аналог", L0, h - 8, C.dim);
    text(ctx, "▬ квантованный", L0 + 56, h - 8, C.accent);
    text(ctx, "‒ ‒ что восстановит приёмник", L0 + 160, h - 8, C.tx);
    text(ctx, "2 мс", R0, h - 8, C.dim, "right");
  });

  /* ------------------------------------------------------------------ 3. модуляция */
  var modMode = segBind("modMode", function (v) { modMode = v; modUpdate(); });
  var fskPre = [0];
  function fskSum(n) {  // сумма ±1 по битам до n — для непрерывной фазы FSK
    while (fskPre.length <= n) { var i = fskPre.length - 1; fskPre.push(fskPre[i] + (hash(i + 777) > 0.5 ? 1 : -1)); }
    return fskPre[n];
  }
  function modMsg(u) { return 0.7 * Math.sin(TAU * u) + 0.3 * Math.sin(TAU * 2.3 * u + 1); }
  function modMsgInt(u) { return -0.7 * Math.cos(TAU * u) / TAU - 0.3 * Math.cos(TAU * 2.3 * u + 1) / (TAU * 2.3); }
  function modUpdate() {
    var v = Number(document.getElementById("modDepth").value);
    var lbl = { am: "Глубина модуляции", fm: "Девиация частоты", fsk: "Разнос частот", qpsk: "Символов на экране" }[modMode];
    document.getElementById("modDepthLbl").textContent = lbl;
    document.getElementById("o-modDepth").textContent = modMode === "qpsk" ? String(Math.round(4 + v / 10)) : v + " %";
    document.getElementById("modCap").textContent = {
      am: "Сверху звук, снизу несущая: её размах повторяет звук (пунктир — огибающая). При 100 % несущая в паузах звука " +
        "почти исчезает, больше — искажения.",
      fm: "Размах постоянный, меняется частота: где звук выше — волны гуще. Девиация — насколько сильно звук сдвигает частоту.",
      fsk: "Ноль — одна частота, единица — другая. Переход без скачка фазы (непрерывная фаза), чтобы сигнал не был широким.",
      qpsk: "Каждые 2 бита — одна из четырёх фаз. Справа созвездие: точка текущего символа (по линии посередине)."
    }[modMode];
  }
  demo("mod", true, function (d) {
    var ctx = d.ctx, w = d.w, h = d.h, v = Number(document.getElementById("modDepth").value) / 100;
    var cw = modMode === "qpsk" ? Math.min(120, w * 0.26) : 0;
    var L0 = 10, R0 = w - 10 - (cw ? cw + 10 : 0), mid = (L0 + R0) / 2;
    var topY = h * 0.2, topA = h * 0.12, rfY = h * 0.64, rfA = h * 0.27;
    var speed = 0.12, fc = 18;
    var U = function (x) { return (x - L0) / (R0 - L0) + d.t * speed; };
    hline(ctx, L0, R0, topY, C.grid);
    hline(ctx, L0, R0, rfY, C.grid);
    text(ctx, modMode === "am" || modMode === "fm" ? "звук" : "биты", L0, 13, C.dim);
    text(ctx, "несущая в эфире", L0, rfY - rfA - 8, C.dim);
    var N = Math.round(R0 - L0) * 2;
    if (modMode === "am" || modMode === "fm") {
      plot(ctx, L0, R0, N / 2, function (x) { return topY - topA * modMsg(U(x)); }, C.tx, 1.8);
    }
    if (modMode === "am") {
      var k = v;
      plot(ctx, L0, R0, N, function (x) { var u = U(x); return rfY - rfA * (1 + k * modMsg(u)) / (1 + k) * Math.cos(TAU * fc * u); }, C.accent, 1.4);
      plot(ctx, L0, R0, N / 4, function (x) { return rfY - rfA * (1 + k * modMsg(U(x))) / (1 + k); }, C.tx, 1, [4, 4]);
      plot(ctx, L0, R0, N / 4, function (x) { return rfY + rfA * (1 + k * modMsg(U(x))) / (1 + k); }, C.tx, 1, [4, 4]);
    } else if (modMode === "fm") {
      var dev = v * 0.5 * fc;
      plot(ctx, L0, R0, N, function (x) { var u = U(x); return rfY - rfA * Math.cos(TAU * fc * u + TAU * dev * modMsgInt(u)); }, C.accent, 1.4);
    } else if (modMode === "fsk") {
      var bps = 6, sh = 0.15 + v * 0.45, bits = [];
      plot(ctx, L0, R0, N, function (x) {
        var u = U(x), n = Math.floor(u * bps), s = hash(n + 777) > 0.5 ? 1 : -1;
        var integ = (fskSum(n) + s * (u * bps - n)) / bps;
        return rfY - rfA * Math.cos(TAU * fc * u + TAU * fc * sh / 2 * integ);
      }, C.accent, 1.4);
      var u0 = U(L0), n0 = Math.floor(u0 * bps);
      for (var n = n0; n / bps < U(R0); n++) {
        var xa = L0 + (n / bps - u0) * (R0 - L0), xb = xa + (R0 - L0) / bps, b = hash(n + 777) > 0.5 ? 1 : 0;
        bits.push([Math.max(L0, xa), topY - (b ? topA : -topA) * 0.8], [Math.min(R0, xb), topY - (b ? topA : -topA) * 0.8]);
        if (xa + 6 > L0 && xa + 6 < R0) { text(ctx, String(b), (Math.max(L0, xa) + Math.min(R0, xb)) / 2, topY + topA + 14, C.fg, "center"); }
      }
      line(ctx, bits, C.tx, 2);
    } else {
      var sps = 4 + Math.round(v * 10), PH = [Math.PI / 4, 3 * Math.PI / 4, -3 * Math.PI / 4, -Math.PI / 4];
      var GRAY = ["00", "01", "11", "10"];
      var symAt = function (n) { return Math.floor(hash(n + 4242) * 4); };
      plot(ctx, L0, R0, N, function (x) { var u = U(x), n = Math.floor(u * sps); return rfY - rfA * Math.cos(TAU * fc * u + PH[symAt(n)]); }, C.accent, 1.4);
      var uu0 = U(L0), m0 = Math.floor(uu0 * sps);
      for (var m = m0; m / sps < U(R0); m++) {
        var xs = L0 + (m / sps - uu0) * (R0 - L0), xe = xs + (R0 - L0) / sps;
        if (xs > L0) { line(ctx, [[xs, topY - topA], [xs, rfY + rfA]], alpha(C.grid, 0.9), 1); }
        var xc = (Math.max(L0, xs) + Math.min(R0, xe)) / 2;
        if (xe - L0 > 14 && R0 - xs > 14) { text(ctx, GRAY[symAt(m)], xc, topY + 4, C.tx, "center", "600 13px Consolas, monospace"); }
      }
      // созвездие
      var ccx = R0 + 10 + cw / 2, ccy = h / 2, cr = cw * 0.36;
      ctx.strokeStyle = C.grid;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(ccx, ccy, cr, 0, TAU); ctx.stroke();
      hline(ctx, ccx - cr - 8, ccx + cr + 8, ccy, C.grid);
      line(ctx, [[ccx, ccy - cr - 8], [ccx, ccy + cr + 8]], C.grid, 1);
      var cur = symAt(Math.floor(U(mid) * sps));
      for (var q = 0; q < 4; q++) {
        var px = ccx + cr * Math.cos(PH[q]), py = ccy - cr * Math.sin(PH[q]);
        ctx.beginPath(); ctx.arc(px, py, q === cur ? 7 : 4, 0, TAU);
        ctx.fillStyle = q === cur ? C.accent : C.dim; ctx.fill();
        text(ctx, GRAY[q], px + (px > ccx ? 4 : -4), py + (py > ccy ? 18 : -10), q === cur ? C.fg : C.dim, px > ccx ? "left" : "right");
      }
      text(ctx, "созвездие", ccx, ccy + cr + 34, C.dim, "center");
      line(ctx, [[mid, topY - topA - 4], [mid, rfY + rfA + 4]], C.tx, 1, [3, 3]);
    }
  });

  /* ------------------------------------------------------------------ 4. диаграмма направленности */
  function omniM(G) {  // показатель cos^m(угол места) для всенаправленной антенны с КНД G
    var D = function (m) {
      var acc = 0, n = 200;
      for (var i = 0; i < n; i++) { var e = -Math.PI / 2 + Math.PI * (i + 0.5) / n; acc += Math.pow(Math.cos(e), m) * Math.cos(e) * Math.PI / n; }
      return 2 / acc;
    };
    var lo = 0, hi = 400;
    if (G <= D(2)) { return 2; }
    for (var it = 0; it < 40; it++) { var mm = (lo + hi) / 2; if (D(mm) < G) { lo = mm; } else { hi = mm; } }
    return (lo + hi) / 2;
  }
  var OMNI_M = {};
  function antShape(g) {
    // форма диаграммы в линейных единицах: до 6 дБi — всенаправленная (угол = угол места), больше — Яги
    var G = lin(g);
    if (g <= 6) {
      var key = g.toFixed(2);
      if (OMNI_M[key] === undefined) { OMNI_M[key] = omniM(G); }
      var m = OMNI_M[key], peak = Math.max(G, 1.5) * (G < 1.5 ? G / 1.5 : 1);
      return { dir: "omni", f: function (a) {
        var e = Math.asin(Math.sin(a));
        return Math.max(peak * Math.pow(Math.abs(Math.cos(e)), m), peak * 1e-3);  // провал вдоль оси
      } };
    }
    var y = YAGI[g.toFixed(2)] || (YAGI[g.toFixed(2)] = yagiFit(G));
    return { dir: "yagi", f: function (a) { return G * yagiRel(a, y.n, y.k); } };
  }
  // Яги: форма относительно пика = max(главный лепесток ((1+cos a)/2)^n, k·задние/боковые −15/−20 дБ).
  // Нормировка: среднее по сфере ½∫ f(a)·sin a da = 1/G, тогда G·f даёт ∮G dΩ = 4π (КПД 100 %).
  // Если боковые −15/−20 дБ «съедают» больше 20 % излучения (G > ~10,5 дБi), их уровень k снижается,
  // а n подбирается так, чтобы главный лепесток давал ровно G (ширина луча ~ 1/√G).
  // Проверено в браузере (шаг 0,25 дБ, 4000 точек): среднее G по сфере / КПД = 1 с ошибкой ≤ 0,12 % для −5…20 дБi
  // (всенаправленная модель: среднее = КПД = min(1, G/1,5)).
  var YAGI = {};
  function yagiRel(a, n, k) {
    return Math.max(Math.pow((1 + Math.cos(a)) / 2, n), k * Math.pow(10, -(15 + 5 * Math.abs(Math.sin(a))) / 10));
  }
  function sphereAvg(f) {  // ½∫₀^π f(a)·sin a da
    var acc = 0, N = 720;
    for (var i = 0; i < N; i++) { var a = Math.PI * (i + 0.5) / N; acc += f(a) * Math.sin(a) * Math.PI / N; }
    return acc / 2;
  }
  function yagiFit(G) {
    var sideAvg = sphereAvg(function (a) { return yagiRel(a, 1e6, 1); });
    var k = Math.min(1, 0.2 / (G * sideAvg));
    var lo = 0, hi = 5000;
    for (var it = 0; it < 60; it++) {
      var n = (lo + hi) / 2;
      if (1 / sphereAvg(function (a) { return yagiRel(a, n, k); }) < G) { lo = n; } else { hi = n; }
    }
    return { n: (lo + hi) / 2, k: k };
  }
  function antEff(g) { return g <= 6 ? Math.min(1, lin(g) / 1.5) : 1; }  // КПД, заложенный в модель
  function antGainAt(g, deg) { return 10 * LOG10(Math.max(1e-6, antShape(g).f(deg * Math.PI / 180))); }
  demo("pattern", false, function (d) {
    var ctx = d.ctx, w = d.w, h = d.h, g = S.gTx;
    var cx = w / 2, cy = h / 2 + 6, Rr = Math.min(w * 0.42, h * 0.4);
    var floor = -30, top = 20;
    var rad = function (db) { return Math.max(0, (db - floor) / (top - floor)) * Rr; };
    [-20, -10, 0, 10, 20].forEach(function (db) {
      ctx.beginPath(); ctx.arc(cx, cy, rad(db), 0, TAU);
      ctx.strokeStyle = db === 0 ? alpha(C.dim, 0.9) : C.grid; ctx.lineWidth = 1;
      ctx.setLineDash(db === 0 ? [4, 4] : []); ctx.stroke(); ctx.setLineDash([]);
      if (db >= 0) {
        text(ctx, (db > 0 ? "+" : "") + db + " дБi", cx - rad(db) * 0.707 - 3, cy - rad(db) * 0.707 - 2, C.dim, "right",
          "10px 'Segoe UI', sans-serif");
      }
    });
    hline(ctx, cx - Rr, cx + Rr, cy, C.grid);
    var sh = antShape(g), dir = sh.dir, shape = sh.f;
    ctx.beginPath();
    for (var i = 0; i <= 360; i++) {
      var a = TAU * i / 360, r = rad(10 * LOG10(Math.max(1e-6, shape(a))));
      var px = cx + r * Math.cos(a), py = cy - r * Math.sin(a);
      if (i) { ctx.lineTo(px, py); } else { ctx.moveTo(px, py); }
    }
    ctx.closePath();
    ctx.fillStyle = alpha(C.tx, 0.22); ctx.fill();
    ctx.strokeStyle = C.tx; ctx.lineWidth = 2; ctx.stroke();
    // антенна в центре
    line(ctx, [[cx, cy - 12], [cx, cy + 12]], C.fg, 3);
    text(ctx, "вид сбоку · " + num(g, 1) + " дБi · " + (dir === "omni" ? "всенаправленная" : "направленная (Яги)"), 10, 16, C.fg);
    // луч на приёмник
    var th = (S.thTx || 0) * Math.PI / 180, ge = 10 * LOG10(Math.max(1e-6, shape(th)));
    var rx = cx + Rr * 1.02 * Math.cos(th), ry = cy - Rr * 1.02 * Math.sin(th);
    line(ctx, [[cx, cy], [rx, ry]], C.rx, 2, [6, 4]);
    var rg = rad(ge);
    ctx.beginPath(); ctx.arc(cx + rg * Math.cos(th), cy - rg * Math.sin(th), 5, 0, TAU); ctx.fillStyle = C.rx; ctx.fill();
    text(ctx, "на приёмник: " + num(ge, 1) + " дБi", Math.min(w - 8, Math.max(8, rx)), Math.max(30, Math.min(h - 6, ry + (ry < cy ? -6 : 14))),
      C.rx, rx > cx ? "right" : "left", "600 11px 'Segoe UI', sans-serif");
    text(ctx, "к горизонту →", w - 8, cy + 13, C.dim, "right", "11px 'Segoe UI', sans-serif");
  });

  /* ------------------------------------------------------------------ 5. эфир */
  demo("air", true, function (d) {
    var ctx = d.ctx, w = d.w, h = d.h, x0 = 26, cy = h / 2, xr = w - 34;
    for (var i = 0; i < 6; i++) {
      var ph = (d.t * 0.5 + i / 6) % 1, r = ph * (xr - x0 + 40);
      ctx.beginPath(); ctx.arc(x0, cy, r, -0.9, 0.9);
      ctx.strokeStyle = alpha(C.air, Math.max(0, 0.9 * (1 - ph) * (1 - ph)));
      ctx.lineWidth = 2.5 * (1 - ph) + 0.6; ctx.stroke();
    }
    line(ctx, [[x0, cy - 20], [x0, cy + 20]], C.tx, 3);
    line(ctx, [[xr, cy - 16], [xr, cy + 16]], C.rx, 3);
    line(ctx, [[x0, h - 14], [xr, h - 14]], C.grid, 1);
    text(ctx, fmtKm(S.dist) + " · " + fmtMHz(S.freq), (x0 + xr) / 2, h - 20, C.fg, "center");
    text(ctx, "−" + num(R.fspl, 1) + " дБ", xr - 8, cy - 22, C.air, "right", "600 13px 'Segoe UI', sans-serif");
    text(ctx, "излучено " + fmtW(dbmW(R.prad)) + " · ЭИИМ к приёмнику " + fmtW(R.eirpW) + " (экв.)", x0 + 10, 16, C.tx);
    text(ctx, "у антенны " + fmtW(dbmW(R.air)), xr, 16, C.rx, "right");
  });
  demo("airchart", false, function (d) {
    var ctx = d.ctx, w = d.w, h = d.h, L0 = 46, R0 = w - 12, T0 = 12, B0 = h - 30;
    var lx0 = -1, lx1 = 4.6;
    var lev = function (km) { return R.eirp - (20 * LOG10(km) + 20 * LOG10(S.freq) + 32.44) - S.extra + R.gRxEff; };
    var y0 = lev(Math.pow(10, lx0)), y1 = lev(Math.pow(10, lx1));
    var mx = Math.ceil(Math.max(y0, R.noiseAnt) / 20) * 20 + 10, mn = Math.floor(Math.min(y1, R.noiseAnt) / 20) * 20 - 10;
    var X = function (lk) { return L0 + (R0 - L0) * (lk - lx0) / (lx1 - lx0); };
    var Y = function (v) { return T0 + (B0 - T0) * (mx - v) / (mx - mn); };
    for (var g = Math.ceil(mn / 20) * 20; g <= mx; g += 20) {
      hline(ctx, L0, R0, Y(g), C.grid);
      text(ctx, num(g, 0), L0 - 4, Y(g) + 4, C.dim, "right", "11px 'Segoe UI', sans-serif");
    }
    [[0.1, "100 м"], [1, "1 км"], [10, "10 км"], [100, "100 км"], [1000, "1000 км"], [10000, "10 000 км"]].forEach(function (t) {
      var xx = X(LOG10(t[0]));
      line(ctx, [[xx, T0], [xx, B0]], alpha(C.grid, 0.7), 1);
      text(ctx, t[1], xx, B0 + 16, C.dim, "center", "11px 'Segoe UI', sans-serif");
    });
    // зона ниже шума
    ctx.fillStyle = alpha(C.noise, 0.12);
    ctx.fillRect(L0, Y(R.noiseAnt), R0 - L0, B0 - Y(R.noiseAnt));
    hline(ctx, L0, R0, Y(R.noiseAnt), C.noise, [5, 4]);
    text(ctx, "шум в канале", R0 - 4, Y(R.noiseAnt) + 14, C.noise, "right", "11px 'Segoe UI', sans-serif");
    plot(ctx, X(lx0), X(lx1), 2, function (x) { return Y(lev(Math.pow(10, lx0 + (x - L0) / (R0 - L0) * (lx1 - lx0)))); }, C.air, 2.5);
    var cxp = X(LOG10(S.dist)), cyp = Y(R.rxAnt);
    ctx.beginPath(); ctx.arc(cxp, cyp, 6, 0, TAU); ctx.fillStyle = C.air; ctx.fill();
    text(ctx, fmtDbm(R.rxAnt, 0), cxp + 9, cyp - 8, C.fg, "left", "600 12px 'Segoe UI', sans-serif");
    text(ctx, "дБм", 4, 12, C.dim, "left", "11px 'Segoe UI', sans-serif");
    text(ctx, "−6 дБ на каждое удвоение расстояния, −20 дБ на каждые ×10", L0 + 6, B0 - 6, C.dim, "left", "11px 'Segoe UI', sans-serif");
  });

  /* ------------------------------------------------------------------ 6. приёмная антенна */
  var rxHist = [];
  demo("rxant", true, function (d, dt) {
    var ctx = d.ctx, w = d.w, h = d.h, cy = h / 2 - 8, split = w * 0.6, xd = split * 0.62;
    var k = TAU / 120, om = TAU * 0.6;
    var E = function (x) { return Math.sin(k * x - om * d.t); };
    // стрелки поля
    for (var x = 12; x < split - 6; x += 16) {
      var e = E(x), len = e * h * 0.3;
      var near = Math.abs(x - xd) < 10;
      if (near) { continue; }
      ctx.strokeStyle = alpha(C.air, 0.35 + 0.55 * Math.abs(e)); ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(x, cy); ctx.lineTo(x, cy - len); ctx.stroke();
      if (Math.abs(len) > 6) {
        ctx.beginPath(); ctx.moveTo(x - 4, cy - len + (len > 0 ? 6 : -6)); ctx.lineTo(x, cy - len); ctx.lineTo(x + 4, cy - len + (len > 0 ? 6 : -6));
        ctx.stroke();
      }
    }
    text(ctx, "волна →", 12, 16, C.air);
    // диполь
    var e0 = E(xd), arm = h * 0.3;
    line(ctx, [[xd, cy - arm], [xd, cy - 5]], C.rx, 4);
    line(ctx, [[xd, cy + 5], [xd, cy + arm]], C.rx, 4);
    // электроны смещаются против поля
    for (var i = 0; i < 5; i++) {
      [-1, 1].forEach(function (sgn) {
        var base = sgn * (10 + i * (arm - 18) / 4), yy = cy + base + e0 * 7;
        ctx.beginPath(); ctx.arc(xd, Math.max(cy - arm, Math.min(cy + arm, yy)), 2.6, 0, TAU);
        ctx.fillStyle = C.fg; ctx.fill();
      });
    }
    var plus = e0 > 0 ? "−" : "+", minus = e0 > 0 ? "+" : "−";
    var fz = "700 " + Math.round(13 + 6 * Math.abs(e0)) + "px 'Segoe UI', sans-serif";
    text(ctx, minus, xd + 10, cy - arm + 10, C.tx, "left", fz);
    text(ctx, plus, xd + 10, cy + arm, C.tx, "left", fz);
    // провода к выходу
    line(ctx, [[xd, cy - 5], [split - 10, cy - 5]], C.dim, 1);
    line(ctx, [[xd, cy + 5], [split - 10, cy + 5]], C.dim, 1);
    // осциллограмма напряжения
    if (dt > 0 || !rxHist.length) { rxHist.push(e0); }
    var nmax = Math.round(w - split - 16);
    while (rxHist.length > nmax) { rxHist.shift(); }
    var x0 = split + 6, x1 = w - 8;
    ctx.strokeStyle = C.grid; ctx.strokeRect(x0, cy - arm - 6, x1 - x0, 2 * arm + 12);
    hline(ctx, x0, x1, cy, C.grid);
    var pts = rxHist.map(function (v, j) { return [x1 - (rxHist.length - 1 - j), cy - v * arm]; });
    if (pts.length > 1) { line(ctx, pts, C.rx, 2); }
    text(ctx, "U на выходе", x0 + 4, cy - arm - 10, C.dim);
    text(ctx, "ЭДС диполя ≈ " + fmtV(R.uoc) + " при E = " + fmtField(R.efield), 12, h - 10, C.fg);
  });

  /* ------------------------------------------------------------------ 7. входной фильтр */
  demo("filter", false, function (d) {
    var ctx = d.ctx, w = d.w, h = d.h, L0 = 10, R0 = w - 10, B0 = h - 26, T0 = 18;
    var f0 = 50, f1 = 250, X = function (f) { return L0 + (R0 - L0) * (f - f0) / (f1 - f0); };
    var pass = function (f) { var c = 140, bw = 22; var x = (f - c) / bw; return 1 / Math.sqrt(1 + Math.pow(x, 8)); };
    var sigs = [[95, 0.9, "FM-вещание"], [104, 0.8, ""], [137.9, 0.35, "Метеор"], [145.5, 0.45, "2 м"], [162, 0.5, "служебные"], [210, 0.7, "DAB/ТВ"]];
    hline(ctx, L0, R0, B0, C.grid);
    sigs.forEach(function (s, si) {
      var a = s[1] * pass(s[0]), x = X(s[0]), hh = (B0 - T0) * 0.8;
      ctx.fillStyle = alpha(C.dim, 0.25);
      ctx.fillRect(x - 3, B0 - hh * s[1], 6, hh * s[1]);
      ctx.fillStyle = C.sdr;
      ctx.fillRect(x - 3, B0 - hh * a, 6, hh * a);
      if (s[2]) { text(ctx, s[2], x, B0 - hh * Math.max(a, s[1]) - 6 - (si % 2) * 14, a > 0.3 ? C.fg : C.dim, "center", "11px 'Segoe UI', sans-serif"); }
    });
    plot(ctx, L0, R0, 300, function (x) { var f = f0 + (x - L0) / (R0 - L0) * (f1 - f0); return B0 - (B0 - T0) * 0.92 * pass(f); }, C.sdr, 1.5, [5, 4]);
    [50, 100, 150, 200, 250].forEach(function (f) { text(ctx, f + " МГц", X(f), h - 8, C.dim, f === 50 ? "left" : f === 250 ? "right" : "center", "11px 'Segoe UI', sans-serif"); });
  });

  /* ------------------------------------------------------------------ 8. вектор IQ */
  var iqHist = [];
  function iqUpdate() {
    var v = Number(document.getElementById("iqOff").value);
    document.getElementById("o-iqOff").textContent = v === 0 ? "точно на настройке" :
      (v > 0 ? "выше на " : "ниже на ") + num(Math.abs(v), 1) + " (условно)";
  }
  demo("iq", true, function (d, dt) {
    var ctx = d.ctx, w = d.w, h = d.h, f = Number(document.getElementById("iqOff").value) * 0.25;
    d.ph = (d.ph || 0) + TAU * f * dt;
    var I = Math.cos(d.ph), Q = Math.sin(d.ph);
    var r = Math.min(h * 0.38, w * 0.16), cx = r + 18, cy = h / 2;
    ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, TAU); ctx.stroke();
    hline(ctx, cx - r - 8, cx + r + 8, cy, C.grid);
    line(ctx, [[cx, cy - r - 8], [cx, cy + r + 8]], C.grid, 1);
    text(ctx, "I", cx + r + 8, cy - 4, C.dim);
    text(ctx, "Q", cx + 4, cy - r - 2, C.dim);
    line(ctx, [[cx, cy], [cx + r * I, cy - r * Q]], C.sdr, 3);
    ctx.beginPath(); ctx.arc(cx + r * I, cy - r * Q, 5, 0, TAU); ctx.fillStyle = C.sdr; ctx.fill();
    line(ctx, [[cx + r * I, cy - r * Q], [cx + r * I, cy]], alpha(C.rx, 0.6), 1, [3, 3]);
    line(ctx, [[cx + r * I, cy - r * Q], [cx, cy - r * Q]], alpha(C.sdr, 0.6), 1, [3, 3]);
    if (dt > 0 || !iqHist.length) { iqHist.push([I, Q]); }
    var x0 = cx + r + 30, x1 = w - 10, n = Math.round(x1 - x0);
    while (iqHist.length > n) { iqHist.shift(); }
    hline(ctx, x0, x1, cy, C.grid);
    line(ctx, iqHist.map(function (p, j) { return [x1 - (iqHist.length - 1 - j), cy - p[0] * r]; }), C.rx, 2);
    line(ctx, iqHist.map(function (p, j) { return [x1 - (iqHist.length - 1 - j), cy - p[1] * r]; }), C.sdr, 2, [5, 4]);
    text(ctx, f > 0 ? "вращение против часовой: частота выше настройки" : f < 0 ? "по часовой: частота ниже настройки" :
      "вектор стоит: сигнал ровно на частоте настройки", x0, h - 8, C.fg);
  });

  /* ------------------------------------------------------------------ 9. БПФ, спектр, водопад */
  function fft(re, im) {
    var n = re.length, i, j, k, l;
    for (i = 1, j = 0; i < n; i++) {
      var bit = n >> 1;
      for (; j & bit; bit >>= 1) { j ^= bit; }
      j ^= bit;
      if (i < j) { var t = re[i]; re[i] = re[j]; re[j] = t; t = im[i]; im[i] = im[j]; im[j] = t; }
    }
    for (l = 2; l <= n; l <<= 1) {
      var ang = -TAU / l, wr = Math.cos(ang), wi = Math.sin(ang);
      for (i = 0; i < n; i += l) {
        var cr = 1, ci = 0;
        for (k = 0; k < l / 2; k++) {
          var a = i + k, b = a + l / 2;
          var xr = re[b] * cr - im[b] * ci, xi = re[b] * ci + im[b] * cr;
          re[b] = re[a] - xr; im[b] = im[a] - xi; re[a] += xr; im[a] += xi;
          var ncr = cr * wr - ci * wi; ci = cr * wi + ci * wr; cr = ncr;
        }
      }
    }
  }
  var FS = {
    N: Number(segBind("fftN", function (v) { FS.N = Number(v); FS.avg = null; fftInfo(); })),
    n: 0, acc: 0, avg: null, rows: 0, wf: null, iq: null,
    ph: { fm: 0, fmA: 0, fsk: 0, am: 0, cw: 0 },
    sel: 2
  };
  var SIGS = [  // нормированная частота (доля от fs), полоса, имя
    { f: -0.33, bw: 0.006, name: "несущая CW" },
    { f: -0.08, bw: 0.04, name: "всплески FSK" },
    { f: 0.15, bw: 0.1, name: "FM-станция" },
    { f: 0.36, bw: 0.012, name: "AM-станция" }
  ];
  function fftInfo() {
    var bin = 2.4e6 / FS.N, rbw = bin * 1.5, nf = 4.5;
    var floor = -174 + 10 * LOG10(rbw) + nf, ref = -174 + 10 * LOG10(2.4e6 / 512 * 1.5) + nf;
    document.getElementById("fftInfo").textContent = "Бин = 2,4 МГц / " + FS.N + " = " + num(bin / 1000, 2) +
      " кГц; RBW ≈ 1,5 × бин = " + num(rbw / 1000, 2) + " кГц; блок " + num(FS.N / 2.4, 0) + " мкс";
    var set = function (id, t) { var el = document.getElementById(id); if (el) { el.textContent = t; } };
    set("rbwVal", num(rbw / 1000, 2) + " кГц");
    set("rbwFloor", fmtDbm(floor, 0) + " (КШ 4,5 дБ)");
    set("rbwShift", FS.N === 512 ? "0 дБ (база N = 512)" : fmtDb(floor - ref) + " к N = 512");
    set("rbwLrpt", fmtDbm(-100 - 10 * LOG10(120e3 / Math.min(rbw, 120e3)), 0) + " в бине");
  }
  function genBlock(N, tsec) {
    var I = new Float64Array(N), Q = new Float64Array(N), p = FS.ph;
    var burst = (tsec % 2.6) < 1.5;
    for (var i = 0; i < N; i++) {
      var n = FS.n + i, re = 0.02 * gauss(), imv = 0.02 * gauss();
      p.cw += TAU * -0.33; re += 0.25 * Math.cos(p.cw); imv += 0.25 * Math.sin(p.cw);
      if (burst) {
        var sym = hash(Math.floor(n / 24)) > 0.5 ? 1 : -1;
        p.fsk += TAU * (-0.08 + 0.012 * sym); re += 0.2 * Math.cos(p.fsk); imv += 0.2 * Math.sin(p.fsk);
      }
      var m = 0.6 * Math.sin(TAU * 0.00031 * n) + 0.4 * Math.sin(TAU * 0.000083 * n + 1.1);
      p.fm += TAU * (0.15 + 0.04 * m); re += 0.7 * Math.cos(p.fm); imv += 0.7 * Math.sin(p.fm);
      p.am += TAU * 0.36;
      var env = 0.12 * (1 + 0.7 * Math.sin(TAU * 0.0021 * n) * (0.6 + 0.4 * Math.sin(TAU * 0.00007 * n)));
      re += env * Math.cos(p.am); imv += env * Math.sin(p.am);
      I[i] = re; Q[i] = imv;
    }
    FS.n += N;
    p.cw %= TAU; p.fsk %= TAU; p.fm %= TAU; p.am %= TAU;
    return [I, Q];
  }
  function fftLayout(d) {
    var narrow = d.w < 620, lw = narrow ? 0 : Math.round(d.w * 0.27);
    var iqH = narrow ? 90 : d.h;
    return {
      narrow: narrow, iq: narrow ? [8, 6, d.w - 8, iqH - 6] : [8, 8, lw - 8, d.h - 8],
      sp: [narrow ? 8 : lw + 8, narrow ? iqH + 4 : 8, d.w - 8, narrow ? iqH + 4 + (d.h - iqH) * 0.38 : d.h * 0.38],
      wf: [narrow ? 8 : lw + 8, narrow ? iqH + 4 + (d.h - iqH) * 0.38 + 18 : d.h * 0.38 + 18, d.w - 8, d.h - 18]
    };
  }
  demo("fft", true, function (d, dt) {
    var ctx = d.ctx, lay = fftLayout(d), N = FS.N;
    var sp = lay.sp, wfR = lay.wf, pw = Math.round(sp[2] - sp[0]), wh = Math.round(wfR[3] - wfR[1]);
    if (!FS.wf || FS.wf.width !== pw || FS.wf.height !== wh) {
      FS.wf = document.createElement("canvas"); FS.wf.width = pw; FS.wf.height = wh;
      var c0 = FS.wf.getContext("2d"); c0.fillStyle = "rgb(" + WF[0].join(",") + ")"; c0.fillRect(0, 0, pw, wh);
      FS.avg = null;
    }
    FS.acc += dt;
    var newRow = false;
    if (FS.acc >= 1 / 16 || !FS.avg) {
      FS.acc = 0;
      newRow = true;
      var blk = genBlock(N, d.t), re = new Float64Array(N), im = new Float64Array(N);
      for (var i = 0; i < N; i++) { var wv = 0.5 - 0.5 * Math.cos(TAU * i / N); re[i] = blk[0][i] * wv; im[i] = blk[1][i] * wv; }
      FS.iq = blk;
      fft(re, im);
      var db = new Float64Array(N);
      for (i = 0; i < N; i++) {
        var j = (i + N / 2) % N;  // fftshift: отрицательные частоты слева
        db[i] = 10 * LOG10((re[j] * re[j] + im[j] * im[j]) / (N * N / 4) + 1e-12);
      }
      if (!FS.avg || FS.avg.length !== N) { FS.avg = Float64Array.from(db); }
      for (i = 0; i < N; i++) { FS.avg[i] = FS.avg[i] * 0.6 + db[i] * 0.4; }
      FS.db = db;
      // строка водопада
      var wc = FS.wf.getContext("2d");
      wc.drawImage(FS.wf, 0, 1);
      var row = wc.createImageData(pw, 1);
      for (var x = 0; x < pw; x++) {
        var b0 = Math.floor(x * N / pw), b1 = Math.max(b0 + 1, Math.floor((x + 1) * N / pw)), mx = -200;
        for (var b = b0; b < b1; b++) { if (db[b] > mx) { mx = db[b]; } }
        var c = WF[Math.max(0, Math.min(255, Math.round((mx + 75) / 70 * 255)))];
        row.data[x * 4] = c[0]; row.data[x * 4 + 1] = c[1]; row.data[x * 4 + 2] = c[2]; row.data[x * 4 + 3] = 255;
      }
      wc.putImageData(row, 0, 0);
      FS.rows++;
    }
    // IQ
    var iqb = lay.iq, blkq = FS.iq, M = Math.min(N, 160);
    var ih = (iqb[3] - iqb[1]) / 2;
    ["I", "Q"].forEach(function (nm, k) {
      var y0 = iqb[1] + ih * k + ih / 2, arr = blkq[k];
      hline(ctx, iqb[0], iqb[2], y0, C.grid);
      plot(ctx, iqb[0], iqb[2], M - 1, function (_x, t) { return y0 - arr[Math.round(t * (M - 1))] * ih * 0.45; }, k ? C.sdr : C.rx, 1.2);
      text(ctx, nm + (k ? "" : " — отсчёты блока (" + M + " из " + N + ")"), iqb[0] + 2, y0 - ih * 0.5 + 12, C.dim, "left", "11px 'Segoe UI', sans-serif");
    });
    // спектр
    var lo = -80, hi = -5, sx0 = sp[0], sx1 = sp[2], sy0 = sp[1], sy1 = sp[3];
    var SY = function (v) { return sy1 - (sy1 - sy0) * (Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo); };
    ctx.fillStyle = C.panel2; ctx.fillRect(sx0, sy0, sx1 - sx0, sy1 - sy0);
    for (var g = -80; g <= 0; g += 20) { hline(ctx, sx0, sx1, SY(g), alpha(C.grid, 0.9)); }
    var sel = SIGS[FS.sel], selX0 = sx0 + (sel.f - sel.bw / 2 + 0.5) * (sx1 - sx0), selX1 = sx0 + (sel.f + sel.bw / 2 + 0.5) * (sx1 - sx0);
    ctx.fillStyle = alpha(C.accent, 0.16); ctx.fillRect(selX0, sy0, selX1 - selX0, sy1 - sy0);
    ctx.beginPath();
    ctx.moveTo(sx0, sy1);
    for (i = 0; i < N; i++) { ctx.lineTo(sx0 + (i + 0.5) / N * (sx1 - sx0), SY(FS.avg[i])); }
    ctx.lineTo(sx1, sy1); ctx.closePath();
    ctx.fillStyle = alpha(C.sdr, 0.22); ctx.fill();
    ctx.beginPath();
    for (i = 0; i < N; i++) { var px = sx0 + (i + 0.5) / N * (sx1 - sx0), py = SY(FS.avg[i]); if (i) { ctx.lineTo(px, py); } else { ctx.moveTo(px, py); } }
    ctx.strokeStyle = C.sdr; ctx.lineWidth = 1.3; ctx.stroke();
    text(ctx, "спектр (строка БПФ, дБ)", sx0 + 4, sy0 + 13, C.fg, "left", "11px 'Segoe UI', sans-serif");
    text(ctx, "IQ → окно Ханна → БПФ → |X|² в дБ", sx1 - 4, sy0 + 13, C.sdr, "right", "600 11px 'Segoe UI', sans-serif");
    text(ctx, sel.name, (selX0 + selX1) / 2, sy0 + 28, C.accent, "center", "600 11px 'Segoe UI', sans-serif");
    // «пол» шума: медиана сглаженного спектра — смещается на 3 дБ при каждом удвоении N
    var srt = Array.prototype.slice.call(FS.avg).sort(function (a, b) { return a - b; }), med = srt[srt.length >> 1];
    hline(ctx, sx0, sx1, SY(med), C.warn, [5, 4]);
    text(ctx, "пол шума " + num(med, 0) + " дБ (RBW " + num(2400 / N * 1.5, 1) + " кГц)", sx0 + 4, SY(med) - 4, C.warn,
      "left", "600 11px 'Segoe UI', sans-serif");
    // водопад
    ctx.drawImage(FS.wf, wfR[0], wfR[1], wfR[2] - wfR[0], wfR[3] - wfR[1]);
    ctx.strokeStyle = C.accent; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
    ctx.strokeRect(selX0, wfR[1], selX1 - selX0, wfR[3] - wfR[1]); ctx.setLineDash([]);
    if (newRow || paused) {
      ctx.fillStyle = alpha(C.accent, 0.9);
      ctx.fillRect(wfR[0], wfR[1] - 2, wfR[2] - wfR[0], 2);
    }
    text(ctx, "водопад: новая строка сверху, время идёт вниз", wfR[0] + 4, wfR[1] - 5, C.dim, "left", "11px 'Segoe UI', sans-serif");
    [-1.2, -0.6, 0, 0.6, 1.2].forEach(function (f, k) {
      text(ctx, f === 0 ? "частота настройки" : (f > 0 ? "+" : "") + num(f, 1) + " МГц", wfR[0] + (f / 2.4 + 0.5) * (wfR[2] - wfR[0]),
        d.h - 4, C.dim, k === 0 ? "left" : k === 4 ? "right" : "center", "11px 'Segoe UI', sans-serif");
    });
  });
  function fftClick(e) {
    var d = DEMOS.fft; if (!d || !d.ready) { return; }
    var rect = d.cv.getBoundingClientRect(), lay = fftLayout(d), x = e.clientX - rect.left, y = e.clientY - rect.top;
    if (x < lay.sp[0] || x > lay.sp[2] || y < lay.sp[1]) { return; }
    var f = (x - lay.sp[0]) / (lay.sp[2] - lay.sp[0]) - 0.5, best = 0;
    SIGS.forEach(function (s, i) { if (Math.abs(s.f - f) < Math.abs(SIGS[best].f - f)) { best = i; } });
    FS.sel = best;
    document.getElementById("chanSel").textContent = SIGS[best].name + " (" + (SIGS[best].f > 0 ? "+" : "") +
      num(SIGS[best].f * 2400, 0) + " кГц от центра, полоса ≈ " + num(SIGS[best].bw * 2400, 0) + " кГц)";
    d.dirty = true;
  }

  /* ------------------------------------------------------------------ 10. демодуляция */
  var demodMode = segBind("demodMode", function (v) { demodMode = v; DM.buf = []; });
  var DM = { buf: [], rate: 90, tau: 0, ph: 0 };
  function dmMsg(t) { return 0.6 * Math.sin(TAU * 0.45 * t) + 0.35 * Math.sin(TAU * 1.15 * t + 0.7); }
  function dmMsgInt(t) { return -0.6 * Math.cos(TAU * 0.45 * t) / (TAU * 0.45) - 0.35 * Math.cos(TAU * 1.15 * t + 0.7) / (TAU * 1.15); }
  demo("demod", true, function (d, dt) {
    var ctx = d.ctx, w = d.w, h = d.h, sig = Number(document.getElementById("demodNoise").value) / 100 * 0.5;
    var keep = DM.rate * 4;
    if (!DM.buf.length) { DM.tau = d.t; }
    var target = d.t;
    if (!DM.buf.length) { DM.tau = target - 4; }
    while (DM.tau < target) {
      DM.tau += 1 / DM.rate;
      var t = DM.tau, m = dmMsg(t), zr, zi;
      if (demodMode === "am") {
        var a = 0.62 * (1 + 0.6 * m), ph = TAU * 0.07 * t;
        zr = a * Math.cos(ph); zi = a * Math.sin(ph);
      } else {
        var th = TAU * 0.9 * dmMsgInt(t) + TAU * 0.05 * t;
        zr = 0.8 * Math.cos(th); zi = 0.8 * Math.sin(th);
      }
      zr += sig * gauss(); zi += sig * gauss();
      var prev = DM.buf.length ? DM.buf[DM.buf.length - 1] : null, out;
      if (demodMode === "am") { out = (Math.sqrt(zr * zr + zi * zi) / 0.62 - 1) / 0.6; }
      else if (prev) {
        var dr = zr * prev.r + zi * prev.i, di = zi * prev.r - zr * prev.i;
        out = (Math.atan2(di, dr) * DM.rate / TAU - 0.05) / 0.9;
      } else { out = 0; }
      DM.buf.push({ r: zr, i: zi, m: m, o: out, t: t });
    }
    while (DM.buf.length > keep) { DM.buf.shift(); }
    var buf = DM.buf, n = buf.length;
    if (!n) { return; }
    // плоскость IQ
    var narrow = w < 520, r = narrow ? Math.min(w * 0.3, 70) : Math.min(h * 0.36, w * 0.17);
    var cx = r + 14, cy = narrow ? r + 16 : h / 2;
    ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(cx, cy, r * 0.8, 0, TAU); ctx.stroke();
    hline(ctx, cx - r, cx + r, cy, C.grid); line(ctx, [[cx, cy - r], [cx, cy + r]], C.grid, 1);
    var trail = buf.slice(Math.max(0, n - DM.rate));
    ctx.fillStyle = alpha(C.sdr, 0.35);
    trail.forEach(function (p) { ctx.fillRect(cx + p.r * r - 1, cy - p.i * r - 1, 2, 2); });
    var pz = buf[n - 1];
    line(ctx, [[cx, cy], [cx + pz.r * r, cy - pz.i * r]], C.sdr, 3);
    text(ctx, "IQ", cx - r, cy - r - 2, C.dim);
    text(ctx, demodMode === "am" ? "длина = звук" : "скорость поворота = звук", cx, cy + r + 16, C.fg, "center", "11px 'Segoe UI', sans-serif");
    // графики
    var x0 = narrow ? 10 : cx + r + 22, x1 = w - 8, top = narrow ? 2 * r + 34 : 8, ph3 = (h - top - 8) / 3;
    var X = function (j) { return x1 - (n - 1 - j) * (x1 - x0) / (keep - 1); };
    var rows = [
      { lbl: "I и Q", f: [function (p) { return p.r; }, function (p) { return p.i; }], col: [C.rx, C.sdr], sc: 0.9 },
      demodMode === "am" ? { lbl: "длина √(I²+Q²)", f: [function (p) { return Math.sqrt(p.r * p.r + p.i * p.i) - 0.62; }], col: [C.accent], sc: 1.2 } :
        { lbl: "фаза arg(IQ), −π…π", f: [function (p) { return Math.atan2(p.i, p.r) / Math.PI; }], col: [C.accent], sc: 0.9 },
      { lbl: "звук на выходе", f: [function (p) { return p.m; }, function (p) { return Math.max(-1.6, Math.min(1.6, p.o)); }], col: [C.dim, C.tx], sc: 0.55, dash: [[4, 3], null] }
    ];
    rows.forEach(function (row, k) {
      var y0 = top + ph3 * k + ph3 / 2;
      hline(ctx, x0, x1, y0, C.grid);
      row.f.forEach(function (fn, q) {
        var pts = [];
        for (var j = 0; j < n; j++) { pts.push([X(j), y0 - fn(buf[j]) * ph3 * 0.5 * row.sc]); }
        line(ctx, pts, row.col[q], q ? 1.8 : 1.4, row.dash ? row.dash[q] || [] : (q ? [5, 3] : []));
      });
      text(ctx, row.lbl, x0 + 2, y0 - ph3 / 2 + 12, C.dim, "left", "11px 'Segoe UI', sans-serif");
    });
  });

  /* ------------------------------------------------------------------ 11. декодер DMR */
  var SYNC = "755FD7DF75F7";
  var DIB = { "01": 3, "00": 1, "10": -1, "11": -3 };
  function burstDibits() {
    var bits = "", i;
    for (i = 0; i < 108; i++) { bits += hash(i + 99) > 0.5 ? "1" : "0"; }
    for (i = 0; i < SYNC.length; i++) { var b = parseInt(SYNC[i], 16).toString(2); while (b.length < 4) { b = "0" + b; } bits += b; }
    for (i = 0; i < 108; i++) { bits += hash(i + 555) > 0.5 ? "1" : "0"; }
    var out = [];
    for (i = 0; i < bits.length; i += 2) { out.push(bits.substr(i, 2)); }
    return out;  // 132 символа
  }
  var BURST = burstDibits();
  demo("dmr", true, function (d) {
    var ctx = d.ctx, w = d.w, h = d.h, rate = 2.5, pos = d.t * rate, L0 = 44, R0 = w - 10;
    var vis = Math.max(8, Math.round((R0 - L0) / 46)), sw = (R0 - L0) / vis;
    var top = 14, wh = h * 0.5, ymid = top + wh / 2;
    var Y = function (lv) { return ymid - lv / 3 * wh * 0.42; };
    [3, 1, -1, -3].forEach(function (lv) {
      hline(ctx, L0, R0, Y(lv), alpha(C.grid, 0.9), [2, 4]);
      text(ctx, (lv > 0 ? "+" : "−") + Math.abs(lv), L0 - 6, Y(lv) + 4, C.dim, "right", "11px Consolas, monospace");
    });
    var symAt = function (k) { var i = ((k % 132) + 132) % 132; return BURST[i]; };
    var lvAt = function (k) { return DIB[symAt(k)]; };
    var first = Math.floor(pos);
    var X = function (k) { return L0 + (k - pos + vis * 0.5) * sw; };
    plot(ctx, L0, R0, Math.round(R0 - L0), function (x) {
      var kk = (x - L0) / sw + pos - vis * 0.5, k0 = Math.floor(kk), fr = kk - k0;
      var s = (1 - Math.cos(Math.PI * Math.max(0, Math.min(1, (fr - 0.25) * 2)))) / 2;
      return Y(lvAt(k0) + (lvAt(k0 + 1) - lvAt(k0)) * s);
    }, C.sdr, 2);
    for (var k = first - Math.ceil(vis / 2) - 1; k <= first + Math.ceil(vis / 2) + 1; k++) {
      var xc = X(k), idx = ((k % 132) + 132) % 132, isSync = idx >= 54 && idx < 78;
      if (xc < L0 + 8 || xc > R0 - 8) { continue; }
      ctx.beginPath(); ctx.arc(xc, Y(lvAt(k)), 3.5, 0, TAU); ctx.fillStyle = isSync ? C.tx : C.sdr; ctx.fill();
      text(ctx, symAt(k), xc, top + wh + 16, isSync ? C.tx : C.fg, "center", "600 13px Consolas, monospace");
    }
    line(ctx, [[L0 + (R0 - L0) / 2, top], [L0 + (R0 - L0) / 2, top + wh + 20]], C.accent, 1, [3, 3]);
    // полоса кадра
    var fy = h - 58, fw = R0 - L0, segs = [[108, C.rx, "голос 108"], [48, C.tx, "синхро 48"], [108, C.rx, "голос 108"], [24, C.noise, "пауза / CACH"]];
    var xx = L0;
    segs.forEach(function (s) {
      var sw2 = fw * s[0] / 288;
      ctx.fillStyle = alpha(s[1], 0.85); ctx.fillRect(xx, fy, sw2 - 2, 20);
      text(ctx, s[2], xx + sw2 / 2, fy + 14, "#ffffff", "center", "11px 'Segoe UI', sans-serif");
      xx += sw2;
    });
    var cur = (((Math.floor(pos + 0.5)) % 132) + 132) % 132, cb = cur * 2;
    var cpx = L0 + fw * cb / 288;
    ctx.fillStyle = C.fg; ctx.fillRect(cpx - 1, fy - 4, 3, 28);
    // кадры вокодера
    var vb = cb < 108 ? cb : cb >= 156 ? cb - 48 : -1, af = vb >= 0 ? Math.floor(vb / 72) + 1 : 0;
    text(ctx, "бит " + cb + "–" + (cb + 1) + " из 264 · " + (af ? "кадр вокодера AMBE+2 №" + af + " из 3 (72 бита = 20 мс речи)" :
      "синхрослово: по нему декодер находит начало пакета"), L0, fy + 40, C.fg, "left", "12px 'Segoe UI', sans-serif");
    text(ctx, "4FSK", 4, top + 4, C.dim, "left", "11px 'Segoe UI', sans-serif");
  });

  /* ------------------------------------------------------------------ 12. звук */
  var audioMode = segBind("audioMode", function (v) { audioMode = v; });
  var audBars = null;
  demo("audio", true, function (d, dt) {
    var ctx = d.ctx, w = d.w, h = d.h, L0 = 10, R0 = w - 10, B0 = h - 22, T0 = 14, nb = 64, fmax = 16000;
    if (!audBars) { audBars = new Float64Array(nb); }
    var cut = audioMode === "wfm" ? [30, 15000] : [300, 3000];
    var t = d.t;
    for (var i = 0; i < nb; i++) {
      var f = (i + 0.5) * fmax / nb, target;
      if (audioMode === "wfm") {
        target = 0.95 * Math.pow(f / 200 + 1, -0.55) * (0.7 + 0.3 * Math.sin(t * 2.3 + i * 0.4)) + 0.25 * Math.exp(-Math.pow((f - 3000 - 1500 * Math.sin(t * 0.7)) / 900, 2));
      } else {
        var f1 = 500 + 120 * Math.sin(t * 1.7), f2 = 1500 + 300 * Math.sin(t * 1.1 + 1), f3 = 2500;
        var syl = 0.5 + 0.5 * Math.abs(Math.sin(t * 2.6));
        target = syl * (0.9 * Math.exp(-Math.pow((f - f1) / 260, 2)) + 0.6 * Math.exp(-Math.pow((f - f2) / 380, 2)) + 0.3 * Math.exp(-Math.pow((f - f3) / 420, 2))) + 0.04;
      }
      if (f < cut[0] || f > cut[1]) { target *= 0.03; }
      target = Math.min(1, target * (0.92 + 0.16 * Math.random()));
      audBars[i] += (target - audBars[i]) * Math.min(1, dt * 10 || 1);
    }
    var bw = (R0 - L0) / nb;
    var X = function (f) { return L0 + (R0 - L0) * f / fmax; };
    ctx.fillStyle = alpha(C.noise, 0.18);
    ctx.fillRect(L0, T0, X(cut[0]) - L0, B0 - T0);
    ctx.fillRect(X(cut[1]), T0, R0 - X(cut[1]), B0 - T0);
    for (i = 0; i < nb; i++) {
      var hh = (B0 - T0) * audBars[i];
      ctx.fillStyle = alpha(C.accent, 0.35 + 0.6 * audBars[i]);
      ctx.fillRect(L0 + i * bw + 1, B0 - hh, bw - 2, hh);
    }
    hline(ctx, L0, R0, B0, C.grid);
    [0, 2, 4, 6, 8, 10, 12, 14, 16].forEach(function (k) {
      text(ctx, k + (k === 16 ? " кГц" : ""), X(k * 1000), h - 6, C.dim, k === 0 ? "left" : k === 16 ? "right" : "center", "11px 'Segoe UI', sans-serif");
    });
    text(ctx, audioMode === "wfm" ? "музыка: до 15 кГц" : "речь: 300–3000 Гц", L0 + 4, T0 + 12, C.fg);
  });

  /* ------------------------------------------------------------------ 13. разнесённый приём */
  var divMode = segBind("divMode", function (v) { divMode = v; DV.buf = []; });
  var DV = { buf: [], k: 0, cur: 0, rate: 40 };
  var FADE = (function () {
    var a = [];
    for (var j = 0; j < 2; j++) {
      var comps = [];
      for (var i = 0; i < 8; i++) { comps.push([0.4 + 1.6 * hash(i * 7 + j * 101 + 3), TAU * hash(i * 13 + j * 57 + 9), TAU * hash(i * 3 + j * 29 + 1)]); }
      a.push(comps);
    }
    return a;
  })();
  function fadeDb(j, t) {
    var re = 0, im = 0;
    FADE[j].forEach(function (c) { var ph = c[0] * Math.cos(c[2]) * t * 2 + c[1]; re += Math.cos(ph); im += Math.sin(ph); });
    return 10 * LOG10((re * re + im * im) / 8 + 1e-6);
  }
  demo("div", true, function (d) {
    var ctx = d.ctx, w = d.w, h = d.h, L0 = 40, R0 = w - 10, T0 = 12, B0 = h - 22, keep = DV.rate * 8, thr = -8;
    if (!DV.buf.length) { DV.tau = d.t - 8; DV.cur = 0; }
    while (DV.tau < d.t) {
      DV.tau += 1 / DV.rate;
      var a = fadeDb(0, DV.tau), b = fadeDb(1, DV.tau), o;
      if (divMode === "sel") { o = Math.max(a, b); }
      else if (divMode === "sw") { if ((DV.cur ? b : a) < thr) { DV.cur = 1 - DV.cur; } o = DV.cur ? b : a; }
      else { o = 10 * LOG10(lin(a) + lin(b)); }
      DV.buf.push([a, b, o]);
    }
    while (DV.buf.length > keep) { DV.buf.shift(); }
    var n = DV.buf.length, lo = -30, hi = 10;
    var Y = function (v) { return T0 + (B0 - T0) * (hi - Math.max(lo, Math.min(hi, v))) / (hi - lo); };
    var X = function (j) { return R0 - (n - 1 - j) * (R0 - L0) / (keep - 1); };
    [10, 0, -10, -20, -30].forEach(function (g) { hline(ctx, L0, R0, Y(g), alpha(C.grid, 0.9)); text(ctx, g + " дБ", L0 - 4, Y(g) + 4, C.dim, "right", "11px 'Segoe UI', sans-serif"); });
    ctx.fillStyle = alpha(C.bad, 0.08); ctx.fillRect(L0, Y(thr), R0 - L0, B0 - Y(thr));
    hline(ctx, L0, R0, Y(thr), C.bad, [4, 4]);
    var col = [C.rx, C.air, C.tx];
    [0, 1, 2].forEach(function (q) {
      var pts = DV.buf.map(function (p, j) { return [X(j), Y(p[q])]; });
      line(ctx, pts, q === 2 ? col[q] : alpha(col[q], 0.7), q === 2 ? 2.6 : 1.3);
    });
    var below = [0, 0, 0];
    DV.buf.forEach(function (p) { for (var q = 0; q < 3; q++) { if (p[q] < thr) { below[q]++; } } });
    text(ctx, "антенна A", L0 + 4, h - 6, C.rx);
    text(ctx, "антенна B", L0 + 80, h - 6, C.air);
    text(ctx, "результат", L0 + 160, h - 6, C.tx);
    var pc = function (q) { return num(100 * below[q] / Math.max(1, n), 0) + " %"; };
    document.getElementById("divCap").textContent = "Время в провале ниже порога: антенна A — " + pc(0) + ", B — " + pc(1) +
      ", после объединения — " + pc(2) + ".";
  });

  /* ------------------------------------------------------------------ 14. аналог и цифра */
  function avdSnr() { return Number(document.getElementById("avdSnr").value); }
  function qAnalog(s) { return 1 / (1 + Math.exp(-(s - 10) / 4.5)); }
  function ber(s) { return 0.5 * erfc(Math.sqrt(lin(s) / 2)); }
  function qDigital(s) { return Math.pow(1 - ber(s), 100); }
  function avdUpdate() {
    var s = avdSnr(), qa = qAnalog(s), qd = qDigital(s), b = ber(s);
    document.getElementById("o-avdSnr").textContent = num(s, 1) + " дБ";
    var mA = document.getElementById("mA"), mD = document.getElementById("mD");
    mA.style.width = (qa * 100).toFixed(0) + "%"; mA.style.background = qa > 0.75 ? C.good : qa > 0.4 ? C.warn : C.bad;
    mD.style.width = (qd * 100).toFixed(0) + "%"; mD.style.background = qd > 0.75 ? C.good : qd > 0.4 ? C.warn : C.bad;
    document.getElementById("capA").textContent = "Разборчивость ≈ " + num(qa * 100, 0) + " %: " +
      (s > 25 ? "чисто" : s > 15 ? "лёгкое шипение" : s > 8 ? "сильный шум, речь понятна" : s > 3 ? "речь с трудом, щелчки" : "почти один шум");
    document.getElementById("capD").textContent = "BER " + (b < 1e-6 ? "< 10⁻⁶" : b.toExponential(1).replace(".", ",").replace("e-", "·10^−")) +
      ", целых кадров " + num(qd * 100, 0) + " %: " +
      (qd > 0.98 ? "звук чистый, как без шума" : qd > 0.6 ? "звук рвётся, роботизированные обрывы" : qd > 0.05 ? "почти все кадры потеряны" : "тишина — связь оборвалась");
    ["avdA", "avdD", "avdCurve"].forEach(function (k) { if (DEMOS[k]) { DEMOS[k].dirty = true; } });
  }
  demo("avdA", true, function (d) {
    var ctx = d.ctx, w = d.w, h = d.h, s = avdSnr(), nAmp = Math.pow(10, -s / 20), L0 = 8, R0 = w - 8;
    var yT = h * 0.27, yB = h * 0.72, a = h * 0.17;
    text(ctx, "в эфире: FM-несущая + шум", L0, 12, C.dim, "left", "11px 'Segoe UI', sans-serif");
    hline(ctx, L0, R0, yT, C.grid);
    plot(ctx, L0, R0, Math.round(R0 - L0), function (x) {
      var u = x / 40 + d.t * 2; return yT - a * 0.8 * (Math.cos(u * 9 + 3 * Math.sin(u * 0.6)) + nAmp * 1.6 * gauss());
    }, C.accent, 1);
    text(ctx, "на выходе: звук (пунктир — без шума)", L0, yB - a - 8, C.dim, "left", "11px 'Segoe UI', sans-serif");
    hline(ctx, L0, R0, yB, C.grid);
    var click = s < 9 ? (9 - s) / 60 : 0;
    plot(ctx, L0, R0, Math.round(R0 - L0), function (x) {
      var u = x / 110 + d.t * 0.8, v = voice(u) + nAmp * 1.4 * gauss();
      if (click && Math.random() < click) { v += (Math.random() < 0.5 ? -1 : 1) * 1.4; }
      return yB - a * Math.max(-1.6, Math.min(1.6, v));
    }, C.tx, 1.2);
    plot(ctx, L0, R0, Math.round((R0 - L0) / 2), function (x) { return yB - a * voice(x / 110 + d.t * 0.8); }, C.dim, 1, [4, 3]);
  });
  demo("avdD", true, function (d) {
    var ctx = d.ctx, w = d.w, h = d.h, s = avdSnr(), sg = Math.pow(10, -s / 20), L0 = 8, R0 = w - 8;
    var yT = h * 0.27, yB = h * 0.72, a = h * 0.17, fer = 1 - qDigital(s);
    text(ctx, "в эфире: символы ±1 + шум (линия — порог решения)", L0, 12, C.dim, "left", "11px 'Segoe UI', sans-serif");
    hline(ctx, L0, R0, yT, C.bad, [3, 3]);
    var spx = 9, off = (d.t * 120) % spx, k0 = Math.floor(d.t * 120 / spx);
    for (var i = 0; i * spx < R0 - L0 + spx; i++) {
      var k = k0 + i, sym = hash(k * 3 + 1) > 0.5 ? 1 : -1, rv = sym + sg * gauss() * 1.0;
      var x = L0 + i * spx - off;
      if (x < L0) { continue; }
      ctx.beginPath(); ctx.arc(x, yT - a * 0.55 * Math.max(-2.2, Math.min(2.2, rv)), 2.2, 0, TAU);
      ctx.fillStyle = (rv > 0) === (sym > 0) ? C.accent : C.bad; ctx.fill();
    }
    text(ctx, "на выходе: кадры по 20 мс (красные — выброшены)", L0, yB - a - 8, C.dim, "left", "11px 'Segoe UI', sans-serif");
    hline(ctx, L0, R0, yB, C.grid);
    var fw = 30, foff = (d.t * 88) % fw, f0 = Math.floor(d.t * 88 / fw);
    for (var j = 0; j * fw < R0 - L0 + fw; j++) {
      var fi = f0 + j, lost = hash(fi * 7 + 5) < fer, xa = L0 + j * fw - foff, xb = xa + fw;
      var xs = Math.max(L0, xa), xe = Math.min(R0, xb);
      if (xe <= xs) { continue; }
      if (lost) {
        ctx.fillStyle = alpha(C.bad, 0.18); ctx.fillRect(xs, yB - a, xe - xs - 1, 2 * a);
      } else {
        plot(ctx, xs, xe - 1, Math.round(xe - xs), function (xx) { return yB - a * voice(xx / 110 + d.t * 0.8); }, C.tx, 1.2);
      }
    }
  });
  demo("avdCurve", false, function (d) {
    var ctx = d.ctx, w = d.w, h = d.h, L0 = 40, R0 = w - 12, T0 = 12, B0 = h - 26, s0 = -5, s1 = 35;
    var X = function (s) { return L0 + (R0 - L0) * (s - s0) / (s1 - s0); };
    var Y = function (q) { return B0 - (B0 - T0) * q; };
    [0, 0.25, 0.5, 0.75, 1].forEach(function (q) { hline(ctx, L0, R0, Y(q), alpha(C.grid, 0.9)); text(ctx, (q * 100) + " %", L0 - 4, Y(q) + 4, C.dim, "right", "11px 'Segoe UI', sans-serif"); });
    for (var s = 0; s <= 35; s += 5) { text(ctx, s + " дБ", X(s), h - 8, C.dim, "center", "11px 'Segoe UI', sans-serif"); }
    plot(ctx, L0, R0, 200, function (x) { return Y(qAnalog(s0 + (x - L0) / (R0 - L0) * (s1 - s0))); }, C.tx, 2.5);
    plot(ctx, L0, R0, 300, function (x) { return Y(qDigital(s0 + (x - L0) / (R0 - L0) * (s1 - s0))); }, C.accent, 2.5);
    var thr = 0;
    for (var q = -5; q < 35; q += 0.05) { if (qDigital(q) >= 0.5) { thr = q; break; } }
    line(ctx, [[X(thr), T0], [X(thr), B0]], C.accent, 1, [3, 3]);
    text(ctx, "порог цифры ≈ " + num(thr, 1) + " дБ", X(thr) + 4, T0 + 26, C.accent, "left", "11px 'Segoe UI', sans-serif");
    var cur = avdSnr();
    line(ctx, [[X(cur), T0], [X(cur), B0]], C.fg, 1.5);
    text(ctx, "аналог: плавно", X(22), Y(qAnalog(22)) + 18, C.tx, "left", "600 12px 'Segoe UI', sans-serif");
    text(ctx, "цифра: обрыв", X(thr) - 6, Y(0.95), C.accent, "right", "600 12px 'Segoe UI', sans-serif");
  });

  // звук: 2,5 с синтезированной «фразы» с шумом (аналог) или с потерей кадров (цифра)
  var actx = null;
  function play(kind) {
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) { return; }
    if (!actx) { actx = new AC(); }
    if (actx.state === "suspended") { actx.resume(); }
    var sr = actx.sampleRate, n = Math.round(sr * 2.5), buf = actx.createBuffer(1, n, sr), x = buf.getChannelData(0);
    var s = avdSnr(), nAmp = Math.pow(10, -s / 20), fer = 1 - qDigital(s);
    var notes = [392, 440, 494, 523, 494, 440, 392, 330], ph = 0;
    for (var i = 0; i < n; i++) {
      var t = i / sr, k = Math.floor(t / (2.5 / notes.length)), f = notes[k % notes.length] * (1 + 0.004 * Math.sin(TAU * 5 * t));
      ph += TAU * f / sr;
      var env = Math.min(1, (t % (2.5 / notes.length)) * 30) * Math.min(1, ((2.5 / notes.length) - (t % (2.5 / notes.length))) * 30);
      var v = env * (0.6 * Math.sin(ph) + 0.25 * Math.sin(2 * ph) + 0.1 * Math.sin(3 * ph));
      if (kind === "a") {
        v += nAmp * 1.2 * gauss();
        if (s < 9 && Math.random() < (9 - s) / 4000) { v += 2; }
      } else {
        var fr = Math.floor(t / 0.02);
        if (hash(fr * 7 + 5) < fer) { v = 0; }
      }
      x[i] = Math.max(-1, Math.min(1, v * 0.22));
    }
    var src = actx.createBufferSource();
    src.buffer = buf;
    src.connect(actx.destination);
    src.start();
  }

  /* ------------------------------------------------------------------ 15. шифрование и законная расшифровка */
  // Учебная потоковая схема: ключ + IV/счётчик → гамма → XOR. НЕ криптостойкая — только для наглядности.
  // Если в браузере есть WebCrypto (нужен защищённый контекст), можно включить настоящий AES-256-CTR.
  var CR = { mode: "edu", seq: 0 };
  function fnv(str) {
    var h = 0x811c9dc5;
    for (var i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
    return h >>> 0;
  }
  function eduStream(key, iv, n) {
    var out = new Uint8Array(n), st = (fnv(key + "|" + iv) || 1) >>> 0;
    for (var i = 0; i < n; i++) {
      st ^= st << 13; st >>>= 0; st ^= st >>> 17; st ^= st << 5; st >>>= 0;
      out[i] = (st ^ (st >>> 11) ^ fnv(key) >>> ((i & 3) * 8)) & 255;
    }
    return out;
  }
  function subtleOk() { return !!(window.crypto && window.crypto.subtle && window.isSecureContext); }
  function aesStream(key, iv, n) {
    var enc = new TextEncoder(), subtle = window.crypto.subtle;
    return subtle.digest("SHA-256", enc.encode(key)).then(function (raw) {
      return subtle.importKey("raw", raw, { name: "AES-CTR" }, false, ["encrypt"]);
    }).then(function (k) {
      var ctr = new Uint8Array(16), ivn = fnv(String(iv));
      for (var i = 0; i < 4; i++) { ctr[i] = (ivn >>> (24 - 8 * i)) & 255; }
      return subtle.encrypt({ name: "AES-CTR", counter: ctr, length: 64 }, k, new Uint8Array(n));  // гамма = AES(ключ, счётчик)
    }).then(function (buf) { return new Uint8Array(buf); });
  }
  function streamOf(key, iv, n) {
    return CR.mode === "aes" && subtleOk() ? aesStream(key, iv, n) : Promise.resolve(eduStream(key, iv, n));
  }
  function xorb(a, b) { var o = new Uint8Array(a.length); for (var i = 0; i < a.length; i++) { o[i] = a[i] ^ b[i]; } return o; }
  function hexOf(u) { return Array.prototype.map.call(u, function (x) { return (x < 16 ? "0" : "") + x.toString(16).toUpperCase(); }).join(" "); }
  function bitsHtml(u) {
    var s = "", n = Math.min(u.length, 8);
    for (var i = 0; i < n; i++) { for (var b = 7; b >= 0; b--) { s += "<i class=\"b" + ((u[i] >> b) & 1) + "\"></i>"; } }
    return "<div class=\"bits\" aria-hidden=\"true\">" + s + "</div>";
  }
  function asText(u) {
    var t;
    try { t = new TextDecoder("utf-8", { fatal: false }).decode(u); } catch (e) { t = ""; }
    return t.replace(/[\u0000-\u001f\u007f-\u009f�]/g, "·");
  }
  function cryptUpdate() {
    var box = document.getElementById("cryptRows");
    if (!box || !window.TextEncoder) { return; }
    var msg = document.getElementById("cMsg").value || " ", k1 = document.getElementById("cKey").value,
      k2 = document.getElementById("cKeyRx").value, iv = document.getElementById("cIv").value;
    var te = new TextEncoder(), used = "", cutMsg = false;
    Array.from(msg).forEach(function (ch) {
      if (cutMsg || te.encode(used + ch).length > 64) { cutMsg = true; } else { used += ch; }
    });
    var plain = te.encode(used), seq = ++CR.seq;
    Promise.all([streamOf(k1, iv, plain.length), streamOf(k2, iv, plain.length), streamOf(k1 + "?", iv, plain.length)])
      .then(function (ks) {
        if (seq !== CR.seq) { return; }
        var air = xorb(plain, ks[0]), rx = xorb(air, ks[1]), eve = xorb(air, ks[2]), same = k1 === k2;
        var row = function (cls, lbl, sub, u, txt) {
          return "<div class=\"crow " + cls + "\"><div class=\"lbl\">" + lbl + "<small>" + sub + "</small></div><div>" +
            bitsHtml(u) + "<div class=\"hex\">" + hexOf(u) + "</div>" +
            (txt !== undefined ? "<div class=\"txt\">" + txt.replace(/&/g, "&amp;").replace(/</g, "&lt;") + "</div>" : "") + "</div></div>";
        };
        box.innerHTML =
          row("plain", "1. Открытые данные", "голосовой кадр или текст, " + plain.length + " байт" +
            (cutMsg ? " (обрезано до 64)" : ""), plain, used) +
          row("ks", "2. Гамма передатчика", "ключ + IV/счётчик → генератор", ks[0]) +
          row("air", "3. В эфире: данные XOR гамма", "выглядит как случайные биты", air, asText(air)) +
          row(same ? "ok" : "bad", "4. Свой приёмник", same ? "тот же ключ → та же гамма → XOR" : "ключ не совпал!", rx, asText(rx)) +
          row("bad", "5. Чужой приёмник", "неверный ключ → другая гамма → мусор", eve, asText(eve));
      }).catch(function () {
        CR.mode = "edu";
        $$("#cryptModeSeg button").forEach(function (b) { b.classList.toggle("on", b.getAttribute("data-v") === "edu"); });
        cryptUpdate();
      });
    var note = document.getElementById("cryptMode");
    if (note) {
      note.textContent = CR.mode === "aes" && subtleOk() ? "AES-256-CTR через WebCrypto браузера: ключ = SHA-256(пароль), гамма = AES(ключ, счётчик)." :
        "Учебная потоковая схема (xorshift от ключа и IV) — НЕ криптостойкая, только для наглядности." +
        (subtleOk() ? "" : " AES в этом окне недоступен: WebCrypto работает только в защищённом контексте (https или localhost).");
    }
  }

  /* ------------------------------------------------------------------ навигация по блокам */
  function openBlock(id, scroll) {
    var el = document.getElementById(id);
    if (!el || el.tagName !== "DETAILS") { return; }
    el.open = true;
    $$(".node").forEach(function (n) { n.classList.toggle("cur", n.getAttribute("data-go") === id); });
    if (scroll) { el.scrollIntoView({ block: "start" }); }
  }
  function onToggle(e) {
    var det = e.target;
    $$("canvas[data-demo]", det).forEach(function (cv) {
      var d = DEMOS[cv.getAttribute("data-demo")];
      if (d) { d.ready = false; d.dirty = true; }
    });
  }

  /* ------------------------------------------------------------------ запуск */
  function init() {
    readTheme();
    if (window.matchMedia) {
      var mq = window.matchMedia("(prefers-color-scheme: dark)");
      var re = function () {
        readTheme();
        FS.wf = null;
        for (var k in DEMOS) { if (Object.prototype.hasOwnProperty.call(DEMOS, k)) { DEMOS[k].dirty = true; } }
        update(); adcUpdate(); avdUpdate();
      };
      if (mq.addEventListener) { mq.addEventListener("change", re); } else if (mq.addListener) { mq.addListener(re); }
    }
    setControls(PRESETS.pmr);
    $("#ctl").addEventListener("input", update);
    $("#ctl").addEventListener("change", update);
    ["rqMode", "tGain"].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) { el.addEventListener("input", update); el.addEventListener("change", update); }
    });
    $("#presets").addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (!b) { return; }
      $$("#presets button").forEach(function (x) { x.classList.toggle("on", x === b); });
      setControls(PRESETS[b.getAttribute("data-preset")]);
      update();
    });
    $$(".node").forEach(function (n) {
      n.addEventListener("click", function () { openBlock(n.getAttribute("data-go"), true); });
    });
    $$("details.blk").forEach(function (det) { det.addEventListener("toggle", onToggle); });
    document.addEventListener("click", function (e) {
      var a = e.target.closest && e.target.closest('a[href^="#b-"]');
      if (a) { openBlock(a.getAttribute("href").slice(1), false); }
    });
    var pb = $("#pauseAll");
    var setPause = function (p) {
      paused = p;
      pb.textContent = p ? "Запустить анимации" : "Пауза анимаций";
      pb.setAttribute("aria-pressed", p ? "true" : "false");
      pb.classList.toggle("on", p);
    };
    setPause(paused);
    pb.addEventListener("click", function () { setPause(!paused); });
    ["adcFs", "adcBits"].forEach(function (id) { document.getElementById(id).addEventListener("input", adcUpdate); });
    document.getElementById("modDepth").addEventListener("input", modUpdate);
    document.getElementById("iqOff").addEventListener("input", iqUpdate);
    document.getElementById("demodNoise").addEventListener("input", function () {
      document.getElementById("o-demodNoise").textContent = this.value + " %";
    });
    document.getElementById("avdSnr").addEventListener("input", avdUpdate);
    document.getElementById("avdFromBudget").addEventListener("click", function () {
      var v = Math.max(-5, Math.min(35, Math.round(R.snr * 2) / 2));
      document.getElementById("avdSnr").value = v;
      avdUpdate();
    });
    document.getElementById("playAnalog").addEventListener("click", function () { play("a"); });
    document.getElementById("playDigital").addEventListener("click", function () { play("d"); });
    document.getElementById("cv-fft").addEventListener("click", fftClick);
    ["cMsg", "cKey", "cKeyRx", "cIv"].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) { el.addEventListener("input", cryptUpdate); }
    });
    if (document.getElementById("cryptModeSeg")) {
      var aesBtn = $('#cryptModeSeg [data-v="aes"]');
      if (!subtleOk()) { aesBtn.disabled = true; aesBtn.title = "WebCrypto недоступен: откройте страницу по https или с localhost"; }
      CR.mode = segBind("cryptModeSeg", function (v) { CR.mode = v; cryptUpdate(); });
    }
    cryptUpdate();
    document.getElementById("o-demodNoise").textContent = document.getElementById("demodNoise").value + " %";
    update();
    adcUpdate();
    modUpdate();
    iqUpdate();
    fftInfo();
    avdUpdate();
    bindCanvases();
    if (/[?&]open=all\b/.test(location.search)) { $$("details.blk").forEach(function (det) { det.open = true; }); }
    if (location.hash && /^#b-/.test(location.hash)) { openBlock(location.hash.slice(1), true); }
    requestAnimationFrame(frame);
    window.SDR4NOOB_PIPELINE = { calc: calc, state: S, demos: DEMOS, cableLoss: cableLoss, antShape: antShape, antEff: antEff };  // для проверки
  }
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", init); } else { init(); }
})();
