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
  function fmtDbm(d, k) { return num(d, k === undefined ? 1 : k) + " dBm"; }
  function fmtDb(d, k) { return (d > 0 ? "+" : "") + num(d, k === undefined ? 1 : k) + " dB"; }
  function fmtBw(hz) { return hz >= 1e6 ? num(hz / 1e6, 1).replace(/,0$/, "") + " МГц" : num(hz / 1e3, hz < 1e5 ? 1 : 0).replace(/,0$/, "") + " кГц"; }
  function fmtLoss(x, k) { return x < 0.005 ? "0 dB" : "\u2212" + num(x, k) + " dB"; }
  function fmtKm(d) { return d < 1 ? num(d * 1000, 0) + " м" : num(d, d < 10 ? 2 : d < 100 ? 1 : 0) + " км"; }
  function fmtMHz(f) { var s = num(f, f < 10 ? 2 : 1); return s.replace(/,0$/, "") + " МГц"; }
  function fmtLen(m) { return m < 1 ? num(m * 100, 0) + " см" : num(m, m < 10 ? 2 : 1).replace(/,0+$/, "") + " м"; }
  function fmtField(e) { return unitFmt(e, [[1, "В/м"], [1e-3, "мВ/м"], [1e-6, "мкВ/м"], [1e-9, "нВ/м"]]); }
  function fmtTime(s) {
    return s >= 1e-3 ? num(s * 1e3, sig(s * 1e3)) + " мс" : num(s * 1e6, sig(s * 1e6)) + " мкс";
  }

  /* ------------------------------------------------------------------ кабели */
  // потери, dB на 100 м ≈ a·√f + b·f (медь + диэлектрик), f в МГц; подогнано под паспорта производителей
  // RG-174: 29 / 98 dB на 100 м при 100 / 1000 МГц; RG316: 26 / 85; RG-58: 16 / 56; RG-6: 6,6 / 21;
  // RG-213: 7,1 / 26; LMR-400: 4,9 на 150 МГц / 12,8 на 900 МГц (паспорта Belden, Times Microwave)
  var CAB = { "RG-174": [2.808, 0.0092], "RG316": [2.559, 0.0041], "RG-58": [1.521, 0.0079],
    "RG-6": [0.6537, 0.00063], "RG-213": [0.655, 0.00548], "LMR-400": [0.3816, 0.0015] };
  function cableLoss(type, f, len) { var c = CAB[type]; return (c[0] * Math.sqrt(f) + c[1] * f) * len / 100; }

  /* ------------------------------------------------------------------ состояние и примеры */
  var S = {};
  var PRESETS = {
    pmr: { txAnt: "dipole", pTx: 27, txType: "RG-58", txLen: 0, gTx: 0, dist: 2, freq: 446.1, extra: 0, gRx: 0, lnaOn: false,
      gLna: 20, rxType: "RG-58", rxLen: 2, bw: 12500, sigBw: 11000, rq: "nfm", tGain: 30, nfRx: 4.5, nfLna: 0.8, conn: 1,
      note: "Рация PMR446: 0,5 Вт, штатная антенна ≈ 0 dBi, 2 км прямой видимости, приём на штырь и 2 м RG-58. " +
        "В городе за домами добавьте 20–30 dB потерь ползунком «Доп. потери»." },
    fm: { txAnt: "colinear", pTx: 70, txType: "LMR-400", txLen: 50, gTx: 6, dist: 50, freq: 100, extra: 0, gRx: 0, lnaOn: false,
      gLna: 20, rxType: "RG-6", rxLen: 10, bw: 200000, sigBw: 180000, rq: "wfm", tGain: 0, nfRx: 4.5, nfLna: 0.8, conn: 2,
      note: "Вещательный передатчик 10 кВт, 50 м кабеля на мачту, антенная решётка 6 dBi (ЭИИМ около 23 кВт). " +
        "Приём за 50 км на диполь и 10 м RG-6: сигнал сильный, МШУ не нужен." },
    meteor: { txAnt: "dipole", txPol: "c", rxPol: "v", pTx: 37, txType: "LMR-400", txLen: 1, gTx: 0, dist: 800, freq: 137.9, extra: 0, gRx: 2, lnaOn: true,
      gLna: 20, rxType: "RG-58", rxLen: 10, bw: 120000, sigBw: 120000, rq: "lrpt", tGain: 40, nfRx: 4.5, nfLna: 0.8, conn: 2,
      note: "«Метеор-М»: около 5 Вт на 137,9 МГц, 800 км — пролёт прямо над головой; 3 dB — потери линейной антенны " +
        "на круговой поляризации. Получается около −100 dBm у антенны; на угле места 10° (≈ 2400 км) — ещё на " +
        "9,5 dB меньше, у самого горизонта (≈ 3300 км) — на 12 dB. На спектре SDR при RBW около 1–2 кГц сигнал шириной 120 кГц выглядит ещё на ~20 dB ниже — " +
        "около −120 dBm в бине." },
    adsb: { txAnt: "dipole", pTx: 54, txType: "RG-58", txLen: 3, gTx: 0, dist: 100, freq: 1090, extra: 0, gRx: 3, lnaOn: false,
      gLna: 20, rxType: "RG-58", rxLen: 3, bw: 2000000, sigBw: 2000000, rq: "adsb", tGain: 49, nfRx: 4.5, nfLna: 0.8, conn: 2,
      note: "Ответчик самолёта: импульсы 250 Вт на 1090 МГц. За 100 км на коллинеарную антенну 3 dBi; на 1 ГГц " +
        "тонкий RG-58 уже заметно теряет — попробуйте 20 м кабеля без МШУ и с ним." }
  };

  var IDS = ["pTx", "txType", "txLen", "gTx", "dist", "freq", "extra", "gRx", "lnaOn", "gLna", "rxType", "rxLen",
    "bw", "nfRx", "nfLna", "conn", "thTx", "thRx", "txAnt", "exc", "txPol", "rxPol"];

  function setControls(p) {
    var ta = document.getElementById("txAnt");
    if (ta && p.txAnt) { ta.value = p.txAnt; applyAntLimits(p.gTx); }  // пресет задаёт тип явно, затем значение
    IDS.forEach(function (k) {
      var el = document.getElementById(k), v = p[k];
      if (!el || v === undefined) { return; }
      if (k === "dist" || k === "freq") { el.value = LOG10(v); }
      else if (el.type === "checkbox") { el.checked = !!v; }
      else { el.value = v; }
    });
    S.note = p.note || "";
    S.sigBw = p.sigBw || 12500;
    ["thTx", "thRx", "exc"].forEach(function (k) { var el = document.getElementById(k); if (el) { el.value = p[k] || 0; } });
    ["txPol", "rxPol"].forEach(function (k) { var el = document.getElementById(k); if (el) { el.value = p[k] || "v"; } });
    var rq = document.getElementById("rqMode"), tg = document.getElementById("tGain");
    if (rq && p.rq) { rq.value = p.rq; }
    if (tg && p.tGain !== undefined) { tg.value = p.tGain; }
  }

  /* RSSI, S-метр, dBFS и запас по связи */
  var REQ = {  // требуемое С/Ш (dB) для вида модуляции
    nfm: [12, "NFM речь: ≈ 12 dB С/Ш в канале (с запасом выше порога FM; 12 dB SINAD после демодулятора — уже при 5–8 dB)"], wfm: [20, "WFM моно без шипения: ≈ 20 dB"],
    dmr: [6, "DMR: ≈ 5–7 dB"], lrpt: [6.5, "LRPT QPSK: ≈ 5–8 dB"], adsb: [10, "ADS-B: ≈ 10 dB"]
  };
  var FS_DBM = -10;  // условно: полная шкала АЦП на входе при усилении тюнера 0 dB
  function sUnits(dbm) {
    var s9 = S.freq < 30 ? -73 : -93;
    if (dbm >= s9) { return "S9" + (dbm - s9 >= 0.5 ? "+" + num(dbm - s9, 0) + " dB" : ""); }
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
      (cut > 0.05 ? " · канал уже сигнала: −" + num(cut, 1) + " dB" : " · весь сигнал в канале"));
    setLive("rssi_noise", fmtDbm(nrssi));
    setLive("rssi_noise_sub", "шум в " + fmtBw(S.bw) + "; разница = С/Ш " + num(snr, 1) + " dB");
    setLive("smeter", sUnits(meas));
    setLive("smeter_sub", "шум: " + sUnits(nrssi) + "; S9 = " + num(s9, 0) + " dBm (" + (S.freq < 30 ? "50" : "5") + " мкВ), " +
      (S.freq < 30 ? "ниже" : "выше") + " 30 МГц");
    setLive("margin", fmtDb(margin).replace("+", "+"));
    setLive("margin_sub", req[1] + "; нужно ≥ " + num(req[0], 1) + " dB, есть " + num(snr, 1));
    var mk = document.getElementById("k-margin");
    if (mk) { mk.className = "kpi " + (margin >= 10 ? "good" : margin >= 3 ? "warn" : "bad"); }
    document.getElementById("o-tGain").textContent = num(tg, 0) + " dB";
    var fsIn = FS_DBM - tg, dfs = rssi - fsIn, nfs = nrssi - fsIn;
    // шум квантования 8-битного АЦП (как у RTL-SDR, f_д = 2,4 МГц) в полосе канала
    var qn = -(6.02 * 8 + 1.76) - 10 * LOG10(2.4e6 / S.bw);   // комплексный IQ: шум по всей полосе f_д
    setLive("dbfs_q", num(qn, 1) + " dBFS");
    setLive("dbfs", num(dfs, 1) + " dBFS");
    setLive("dbfs_noise", num(nfs, 1) + " dBFS");
    setLive("dbfs_full", "≈ " + fmtDbm(fsIn, 0) + " на входе");
    var st = dfs > -1 ? ["bad", "клиппинг: сигнал упёрся в полную шкалу АЦП — уменьшите усиление"] :
      dfs > -10 ? ["warn", "на грани: запас до полной шкалы меньше 10 dB"] :
        nfs < qn ? ["warn", "шум антенны ниже шума квантования АЦП в полосе канала: слабые сигналы теряются, " +
          "прибавьте усиление (сильным станциям это не мешает)"] :
          ["good", "нормально: есть запас до перегрузки, шум антенны выше шума квантования"];
    var stEl = document.getElementById("dbfs_state");
    if (stEl) { stEl.textContent = st[1]; stEl.style.color = C[st[0]]; }
    // шкала S-метра: баллы IARU сверху, вторая ось — dBm для текущего диапазона частот
    var svg = document.getElementById("smeterSvg");
    while (svg.firstChild) { svg.removeChild(svg.firstChild); }
    var W = 760, L = 70, Rr = 740, lo = s9 - 54, hi = s9 + 60, X = function (d) { return L + (Rr - L) * (Math.max(lo, Math.min(hi, d)) - lo) / (hi - lo); };
    var T = function (x, y, txt, at) { svg.appendChild(svgEl("text", Object.assign({ x: x, y: y, "font-size": 11, fill: C.dim }, at || {}), txt)); };
    T(4, 22, "баллы S", { "font-weight": 600, fill: C.fg });
    T(4, 66, "dBm", { "font-weight": 600, fill: C.fg });
    svg.appendChild(svgEl("rect", { x: L, y: 30, width: X(s9) - L, height: 10, rx: 3, fill: C.good, opacity: 0.55 }));
    svg.appendChild(svgEl("rect", { x: X(s9), y: 30, width: Rr - X(s9), height: 10, rx: 3, fill: C.bad, opacity: 0.55 }));
    for (var k = 1; k <= 9; k++) {
      var xk = X(s9 - (9 - k) * 6);
      svg.appendChild(svgEl("line", { x1: xk, x2: xk, y1: 26, y2: 52, stroke: C.fg, "stroke-width": 1 }));
      T(xk, 22, "S" + k, { "text-anchor": "middle", "font-size": 12, fill: C.fg });
      if (k % 2 || k === 9) { T(xk, 66, num(s9 - (9 - k) * 6, 0), { "text-anchor": "middle" }); }
    }
    [20, 40, 60].forEach(function (pp) {
      var xp = X(s9 + pp);
      svg.appendChild(svgEl("line", { x1: xp, x2: xp, y1: 26, y2: 52, stroke: C.bad, "stroke-width": 1 }));
      T(xp, 22, "+" + pp + " dB", { "text-anchor": pp === 60 ? "end" : "middle", "font-size": 12, fill: C.bad });
      T(xp, 66, num(s9 + pp, 0), { "text-anchor": pp === 60 ? "end" : "middle" });
    });
    var mark = function (d, col, lbl, up) {
      var xm = X(d);
      svg.appendChild(svgEl("polygon", { points: (xm - 6) + ",84 " + (xm + 6) + ",84 " + xm + ",72", fill: col }));
      svg.appendChild(svgEl("text", { x: Math.max(110, Math.min(W - 110, xm)), y: up ? 114 : 98, "text-anchor": "middle",
        "font-size": 12, "font-weight": 600, fill: col }, lbl));
    };
    mark(nrssi, C.noise, "шум " + sUnits(nrssi) + " (" + num(nrssi, 0) + " dBm)", false);
    mark(meas, C.accent, "показание " + sUnits(meas) + " (" + num(meas, 0) + " dBm)", true);
    var cap = document.getElementById("smeterCap");
    if (cap) {
      cap.textContent = "Шкала S-метра по IARU: баллы S1…S9 по 6 dB на балл, выше S9 — сколько dB над S9 (+20, +40, +60 dB). " +
        "Привязка: S9 = " + num(s9, 0) + " dBm (" + (S.freq < 30 ? "50 мкВ" : "5 мкВ") + " на 50 Ом), потому что частота " +
        fmtMHz(S.freq) + (S.freq < 30 ? " ниже" : " выше") + " 30 МГц (ниже 30 МГц S9 = −73 dBm, выше — −93 dBm). " +
        "Зелёная часть — баллы S1…S9, красная — сильные сигналы «S9+», как на стрелочных S-метрах раций. " +
        "Нижняя ось — те же деления в dBm.";
    }
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
    r.exc = s.exc || 0;                              // возбудитель (выход модулятора): по умолчанию 0 dBm = 1 мВт
    r.pa = s.pTx;
    r.paGain = r.pa - r.exc;
    r.txLoss = s.txLen > 0 ? cableLoss(s.txType, f, s.txLen) + conn : 0;
    r.txOut = r.pa - r.txLoss;
    r.antLoss = -10 * LOG10(antEff(s.gTx, s.txAnt));           // потери в самой антенне: у укороченного штыря < 1,76 dBi
    r.prad = r.txOut - r.antLoss;                     // полная излучаемая мощность — от направленности не зависит
    r.gTxEff = antGainAt(s.gTx, s.thTx || 0, s.txAnt, s.txPol === "h" ? "h" : "v");         // усиление в сторону приёмника по диаграмме
    r.gRxEff = antGainAt(s.gRx, s.thRx || 0, "auto", s.rxPol === "h" ? "h" : "v");
    r.polLoss = polLoss(s.txPol || "v", s.rxPol || "v");      // несовпадение поляризаций передатчика и приёмника
    r.eirp = r.txOut + r.gTxEff;                      // эквивалент на приёмник (G уже включает КПД), не энергия
    r.fspl = 20 * LOG10(s.dist) + 20 * LOG10(f) + 32.44;
    r.air = r.eirp - r.fspl - s.extra;
    r.rxAnt = r.air + r.gRxEff - r.polLoss;
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
    { k: "src", name: "Возбудитель (выход модулятора, вход усилителя мощности)", short: "Возбудитель", st: "tx" },
    { k: "pa", name: "После усилителя мощности", short: "Усилитель", st: "tx" },
    { k: "txfeed", name: "После фидера передатчика", short: "Фидер", st: "tx" },
    { k: "prad", name: "Излучено антенной (полная мощность)", short: "Излучено", st: "tx" },
    { k: "air", name: "Уровень сигнала, попадающий на приёмную антенну (изотропную)", short: "Эфир", st: "air" },
    { k: "rxant", name: "На выходе приёмной антенны", short: "Антенна", st: "rx" },
    { k: "lna", name: "После МШУ", short: "МШУ", st: "rx" },
    { k: "rxin", name: "На входе приёмника", short: "Вход SDR", st: "rx" }
  ];
  function nodeVals(r) {
    return {
      src: [r.exc, "—"], pa: [r.pa, fmtDb(r.paGain)], txfeed: [r.txOut, S.txLen > 0 ? fmtLoss(r.txLoss, 2) : "нет кабеля"],
      prad: [r.prad, r.antLoss > 0.005 ? "КПД " + num(100 / lin(r.antLoss), 0) + " % (" + fmtLoss(r.antLoss, 1) + ": потери в антенне — часть мощности её греет)" : "КПД 100 % (0 dB)"], air: [r.air, fmtLoss(r.fspl + S.extra, 1) + ", G(θ) " + fmtDb(r.gTxEff) + "i"],
      rxant: [r.rxAnt, "G(θ) " + fmtDb(r.gRxEff) + "i" + (r.polLoss ? ", поляризация " + fmtLoss(r.polLoss, 0) : "")], lna: [r.lna, S.lnaOn ? fmtDb(r.lnaG) : "нет МШУ"],
      rxin: [r.rxIn, fmtLoss(r.rxLoss, 2)]
    };
  }
  function levelText(d) { return fmtDbm(d) + " · " + fmtW(dbmW(d)) + " · " + fmtV(dbmV(d)); }

  function setLive(key, text) {
    $$('[data-live="' + key + '"]').forEach(function (el) { el.textContent = text; });
  }

  var lastAnt = null;
  function applyAntLimits(gain) {  // пределы усиления зависят от типа передающей антенны
    var sel = document.getElementById("txAnt"), gs = document.getElementById("gTx");
    if (!sel || !gs) { return; }
    var t = ANT_TYPES[sel.value] || ANT_TYPES.dipole;
    var v = gain !== undefined ? gain : (lastAnt !== null && lastAnt !== sel.value ? t.def : Number(gs.value));
    gs.step = "0.05"; gs.min = String(t.lo); gs.max = String(Math.max(t.hi, t.lo));
    gs.value = String(Math.max(t.lo, Math.min(t.hi, v)));
    gs.disabled = t.lo === t.hi;
    lastAnt = sel.value;
    var lim = document.getElementById("gTxLim");
    var bandTxt = "";
    if (t.band) {
      var fq = Math.pow(10, Number(document.getElementById("freq").value));
      bandTxt = fq < t.band[0] || fq > t.band[1] ? " · ВНЕ полосы LPDA " + t.band[0] + "–" + t.band[1] + " МГц: усиление резко падает" :
        " · в полосе LPDA " + t.band[0] + "–" + t.band[1] + " МГц";
    }
    if (lim) { lim.style.color = bandTxt.indexOf("ВНЕ") >= 0 ? C.bad : ""; }
    if (lim) { lim.textContent = (t.lo === t.hi ? "фиксировано " + num(t.lo, 0) + " dBi" : "для типа «" + t.name + "»: " + num(t.lo, 2).replace(/,00$/, "") + "…" + num(t.hi, 2).replace(/,00$/, "") + " dBi") + bandTxt; }
  }
  function update() {
    applyAntLimits();
    readControls();
    patTypeSync(S.txAnt);
    var r = R = calc(S);
    // подписи ползунков
    var o = function (id, t) { var el = document.getElementById("o-" + id); if (el) { el.textContent = t; } };
    o("exc", fmtDbm(S.exc || 0, 1) + " = " + fmtW(dbmW(S.exc || 0)) + " ≈ " + fmtV(dbmV(S.exc || 0)));
    o("pTx", fmtW(dbmW(S.pTx)) + " · " + fmtDbm(S.pTx, 1) + " · УМ " + fmtDb(S.pTx - (S.exc || 0), 1));
    o("txLen", S.txLen > 0 ? fmtLen(S.txLen) + " · " + fmtLoss(r.txLoss, 2) : "нет кабеля");
    o("gTx", num(S.gTx, 1) + " dBi = " + num(S.gTx - 2.15, 2) + " dBd");
    o("thTx", num(S.thTx, 0) + "° → G = " + num(r.gTxEff, 1) + " dBi");
    o("dist", fmtKm(S.dist));
    o("freq", fmtMHz(S.freq) + " · λ = " + fmtLen(r.lambda));
    o("extra", fmtLoss(S.extra, 0));
    o("gRx", num(S.gRx, 1) + " dBi = " + num(S.gRx - 2.15, 2) + " dBd");
    o("thRx", num(S.thRx, 0) + "° → G = " + num(r.gRxEff, 1) + " dBi");
    o("gLna", "+" + num(S.gLna, 1) + " dB" + (S.lnaOn ? "" : " (выкл.)"));
    o("rxLen", (S.rxLen > 0 ? fmtLen(S.rxLen) : "только разъёмы") + " · " + fmtLoss(r.rxLoss, 2));
    o("nfRx", num(S.nfRx, 1) + " dB");
    o("nfLna", num(S.nfLna, 1) + " dB");
    o("conn", String(S.conn));
    document.getElementById("gLna").disabled = !S.lnaOn;

    var nv = nodeVals(r);
    $$("[data-node]").forEach(function (el) {
      var k = el.getAttribute("data-node");
      el.textContent = fmtDbm(nv[k][0], 0) + (k === "prad" ? " · ЭИИМ " + num(r.eirp, 0) : "");
    });
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
    setLive("gtx_eff", num(r.gTxEff, 1) + " dBi (угол " + num(S.thTx, 0) + "°)");
    setLive("eirp_full", fmtW(r.eirpW) + " · " + fmtDbm(r.eirp));
    setLive("eirp_short", "излучено " + fmtW(dbmW(r.prad)) + " · ЭИИМ " + fmtW(r.eirpW));
    setLive("aeff_eff", num(r.gRxEff, 1) + " dBi в сторону волны → A = " +
      (r.aeff >= 0.01 ? num(r.aeff, sig(r.aeff)) + " м²" : num(r.aeff * 1e4, sig(r.aeff * 1e4)) + " см²"));
    setLive("erp", fmtW(dbmW(r.eirp - 2.15)));
    setLive("lambda", fmtLen(r.lambda));
    setLive("dist", fmtKm(S.dist));
    setLive("fspl", "−" + num(r.fspl, 1) + " dB");
    setLive("air_short", "−" + num(r.fspl + S.extra, 0) + " dB");
    var ratio = lin(r.fspl);
    setLive("air_units", "мощность меньше в 10^" + num(LOG10(ratio), 1) + " раз");
    setLive("efield", fmtField(r.efield) + " · " + num(20 * LOG10(r.efield * 1e6), 1) + " dBµV/m");
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
    setLive("snr_text", verdict[1] + "; шум " + fmtDbm(r.noiseAnt, 0) + ", КШ " + num(r.nfSys, 1) + " dB");
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
      if (n.k === "prad") {  // ЭИИМ — виртуальная пара к реально излучённой мощности: в той же строке
        var ei = '<br><i class="eirp-pair">ЭИИМ ';
        html += '<tr class="st tx"><td>' + n.name + '<br><i class="eirp-pair">и ЭИИМ в сторону приёмника — эквивалент, не энергия</i>' +
          '<span class="chg-inline">' + v[1] + "; ЭИИМ: G(θ) " + fmtDb(r.gTxEff) + "i</span>" + '</td><td>' + v[1] + ei + "G(θ) " + fmtDb(r.gTxEff) + "i</i>" + '</td><td class="n">' + num(d, 1) + " dBm" + ei +
          num(r.eirp, 1) + ' dBm</i></td><td class="n">' + fmtW(dbmW(d)) + ei + fmtW(r.eirpW) + '</i></td><td class="n">' +
          fmtV(dbmV(d)) + "</td></tr>";
        return;
      }
      html += '<tr class="st ' + n.st + '"><td>' + n.name + '<span class="chg-inline">' + v[1] + "</span></td><td>" + v[1] + '</td><td class="n">' +
        num(d, 1) + ' dBm</td><td class="n">' + fmtW(dbmW(d)) + '</td><td class="n">' + fmtV(dbmV(d)) + "</td></tr>";
    });
    html += '<tr><td>Шум в канале ' + fmtBw(S.bw) + ' (к выходу антенны)<span class="chg-inline">КШ ' + num(r.nfSys, 1) +
      ' dB</span></td><td>КШ ' + num(r.nfSys, 1) + ' dB</td><td class="n">' + num(r.noiseAnt, 1) + ' dBm</td><td class="n">' + fmtW(dbmW(r.noiseAnt)) +
      '</td><td class="n">' + fmtV(dbmV(r.noiseAnt)) + "</td></tr>";
    var rs = r.rxIn - Math.max(0, 10 * LOG10((S.sigBw || S.bw) / S.bw));
    html += '<tr class="st rx"><td><a href="#rssi">RSSI в канале</a> (что измерит приёмник)<span class="chg-inline">' + sUnits(rs) +
      '</span></td><td>' + sUnits(rs) +
      '</td><td class="n">' + num(rs, 1) + ' dBm</td><td class="n">' + fmtW(dbmW(rs)) + '</td><td class="n">' + fmtV(dbmV(rs)) +
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
    var W = 860, H = 370, L = 56, Rm = 18, T = 30, B = 64;
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
    svg.appendChild(svgEl("text", Object.assign({ x: 8, y: 14 }, font), "dBm — абсолютная электрическая мощность"));
    // зоны этапов
    var stages = [["tx", 0, 3, "Передатчик"], ["air", 4, 4, "Эфир"], ["rx", 5, 7, "Приём"]];
    stages.forEach(function (s) {
      var x0 = x(s[1]) - (W - L - Rm) / NODES.length / 2 + 3, x1 = x(s[2]) + (W - L - Rm) / NODES.length / 2 - 3;
      svg.appendChild(svgEl("rect", { x: x0, y: H - B + 34, width: x1 - x0, height: 4, rx: 2, fill: C[s[0]] }));
      svg.appendChild(svgEl("text", { x: (x0 + x1) / 2, y: H - B + 54, "text-anchor": "middle", "font-size": 12,
        fill: C[s[0]], "font-weight": 600 }, s[3]));
    });
    // ---- линии (без подписей)
    var over = -30 < mx && -30 > mn;
    if (over) {
      svg.appendChild(svgEl("line", { x1: x(5) - 30, x2: W - Rm, y1: y(-30), y2: y(-30), stroke: C.warn,
        "stroke-dasharray": "3 4", "stroke-width": 1.2 }));
    }
    var np = [5, 6, 7].map(function (i, j) { return x(i) + "," + y(noiseRx[j]); }).join(" ");
    svg.appendChild(svgEl("polyline", { points: np, fill: "none", stroke: C.noise, "stroke-width": 2,
      "stroke-dasharray": "6 5" }));
    var xs = x(5) + 14;
    svg.appendChild(svgEl("line", { x1: xs, x2: xs, y1: y(vals[5]) + 6, y2: y(noiseRx[0]) - 4, stroke: C.dim,
      "stroke-width": 1 }));
    for (var i = 1; i < NODES.length; i++) {
      svg.appendChild(svgEl("line", { x1: x(i - 1), y1: y(vals[i - 1]), x2: x(i), y2: y(vals[i]),
        stroke: C[NODES[i].st], "stroke-width": 3, "stroke-linecap": "round",
        opacity: (NODES[i].k === "lna" && !S.lnaOn) ? 0.5 : 1 }));
    }
    // ЭИИМ — не ступенька мощности, а эквивалент в направлении лепестка: отдельный пунктирный маркер
    var xe = x(3) + 26, ye = y(r.eirp);
    svg.appendChild(svgEl("line", { x1: x(3), y1: y(vals[3]), x2: xe, y2: ye, stroke: C.tx, "stroke-width": 1.5,
      "stroke-dasharray": "4 4", opacity: 0.8 }));
    svg.appendChild(svgEl("circle", { cx: xe, cy: ye, r: 5, fill: "none", stroke: C.tx, "stroke-width": 2,
      "stroke-dasharray": "3 2" }));
    NODES.forEach(function (n, j) {
      var a = svgEl("a", { href: "#" + ({ src: "b-src", pa: "b-pa", txfeed: "b-txfeed", prad: "b-txant", air: "b-air",
        rxant: "b-rxant", lna: "b-lna", rxin: "b-rxfeed" })[n.k] });
      a.appendChild(svgEl("circle", { cx: x(j), cy: y(vals[j]), r: 6, fill: C.panel, stroke: C[n.st], "stroke-width": 3 }));
      svg.appendChild(a);
      svg.appendChild(svgEl("text", { x: x(j), y: H - B + 20, "text-anchor": "middle", "font-size": 12, fill: C.fg }, n.short));
    });

    // ---- подписи-пилюли без наложений: не пересекают друг друга, точки и отрезки линий графика
    var boxes = [{ x0: 0, y0: 0, x1: 330, y1: 20 }, { x0: 0, y0: H - B + 6, x1: W, y1: H }];
    NODES.forEach(function (n, j) { boxes.push({ x0: x(j) - 8, y0: y(vals[j]) - 8, x1: x(j) + 8, y1: y(vals[j]) + 8 }); });
    boxes.push({ x0: xe - 7, y0: ye - 7, x1: xe + 7, y1: ye + 7 });
    var segs = [];
    for (var sg = 1; sg < NODES.length; sg++) { segs.push([x(sg - 1), y(vals[sg - 1]), x(sg), y(vals[sg])]); }
    segs.push([x(5), y(noiseRx[0]), x(6), y(noiseRx[1])], [x(6), y(noiseRx[1]), x(7), y(noiseRx[2])]);
    segs.push([xs, y(vals[5]) + 6, xs, y(noiseRx[0]) - 4], [x(3), y(vals[3]), xe, ye]);
    if (over) { segs.push([x(5) - 30, y(-30), W - Rm, y(-30)]); }
    var hitSeg = function (b, s) {  // пересекает ли отрезок прямоугольник (отсечение Лианга — Барски)
      var x1 = s[0], y1 = s[1], dx = s[2] - s[0], dy = s[3] - s[1], t0 = 0, t1 = 1;
      var P = [-dx, dx, -dy, dy], Q = [x1 - b.x0, b.x1 - x1, y1 - b.y0, b.y1 - y1];
      for (var i = 0; i < 4; i++) {
        if (P[i] === 0) { if (Q[i] < 0) { return false; } continue; }
        var tt = Q[i] / P[i];
        if (P[i] < 0) { if (tt > t1) { return false; } if (tt > t0) { t0 = tt; } } else { if (tt < t0) { return false; } if (tt < t1) { t1 = tt; } }
      }
      return true;
    };
    var free = function (b) {
      if (b.x0 < L - 2 || b.x1 > W - 2 || b.y0 < 2 || b.y1 > H - B + 6) { return false; }
      if (!boxes.every(function (o) { return b.x1 <= o.x0 || b.x0 >= o.x1 || b.y1 <= o.y0 || b.y0 >= o.y1; })) { return false; }
      return !segs.some(function (s) { return hitSeg(b, s); });
    };
    var place = function (txt, px, py, cands, attrs, pillCol) {  // txt может быть в несколько строк через \n
      var lines = txt.split("\n"), fs = attrs["font-size"] || 11, pick = null;
      var w = Math.max.apply(null, lines.map(function (l) { return l.length; })) * fs * 0.55 + 12, hgt = lines.length * (fs + 2) + 5;
      for (var c = 0; c < cands.length && !pick; c++) {
        var dx = cands[c][0], dy = cands[c][1], anc = cands[c][2] || "middle";
        var cxp = anc === "middle" ? px + dx : anc === "start" ? px + dx + w / 2 : px + dx - w / 2;
        var b = { x0: cxp - w / 2, y0: py + dy - hgt / 2, x1: cxp + w / 2, y1: py + dy + hgt / 2 };
        if (free(b)) { pick = [cxp, py + dy, b]; }
      }
      if (!pick) { return false; }  // места нет — значение есть в таблице под графиком
      boxes.push(pick[2]);
      if (Math.hypot(pick[0] - px, pick[1] - py) > 34) {  // далёкая подпись — тонкая выноска к своей точке
        var bx = Math.max(pick[2].x0, Math.min(pick[2].x1, px)), by = Math.max(pick[2].y0, Math.min(pick[2].y1, py));
        svg.appendChild(svgEl("line", { x1: px, y1: py, x2: bx, y2: by, stroke: pillCol || C.dim, "stroke-width": 0.8,
          "stroke-dasharray": "2 3", opacity: 0.8 }));
      }
      svg.appendChild(svgEl("rect", { x: pick[2].x0, y: pick[2].y0, width: w, height: hgt, rx: hgt / 2, fill: C.panel,
        stroke: pillCol || C.grid, "stroke-width": 1, opacity: 0.96 }));
      var te = svgEl("text", Object.assign({ x: pick[0], y: pick[1] - (lines.length - 1) * (fs + 2) / 2 + fs * 0.36, "text-anchor": "middle" }, attrs));
      lines.forEach(function (l, i) { te.appendChild(svgEl("tspan", { x: pick[0], dy: i ? fs + 2 : 0 }, l)); });
      svg.appendChild(te);
      return true;
    };
    var ring = function (r1) {  // кандидаты по кругу вокруг точки, ближние — первыми
      var out = [];
      [r1, r1 + 14, r1 + 28, r1 + 44, r1 + 62, r1 + 84, r1 + 110].forEach(function (rr) {
        [[0, -1], [0, 1], [1, -0.6], [-1, -0.6], [1, 0.6], [-1, 0.6], [1, 0], [-1, 0]].forEach(function (d) {
          out.push([d[0] * rr * 1.6, d[1] * rr]);
        });
      });
      return out;
    };
    NODES.forEach(function (n, j) {
      var d = vals[j], lbl = fmtDbm(d, 0);
      if (j === 0) { lbl += " (" + fmtW(dbmW(d)) + ")"; }
      if (n.k === "air") { lbl += "\n" + fmtW(dbmW(d)) + " · " + fmtV(dbmV(d)); }
      if (n.k === "rxin") { lbl += "\n" + fmtW(dbmW(d)) + " · " + fmtV(dbmV(d)) + "\n= " + num(d + 107, 1) + " dBµV"; }
      place(lbl, x(j), y(d), ring(16), { "font-size": 11, "font-weight": 600, fill: C.fg }, C[n.st]);
    });
    place("ЭИИМ " + fmtDbm(r.eirp, 0) + " (экв.)", xe, ye, ring(14), { "font-size": 10.5, fill: C.tx }, C.tx);
    var WHY = { pa: "усилитель", txfeed: "кабель", prad: "КПД антенны", air: "эфир", rxant: "антенна", lna: "МШУ", rxin: "кабель" };
    for (var q = 1; q < NODES.length; q++) {
      var dlt = vals[q] - vals[q - 1];
      if (Math.abs(dlt) < 0.05) { continue; }
      var why = WHY[NODES[q].k];
      if (NODES[q].k === "air") { why += ", G(θ) " + fmtDb(r.gTxEff, 1) + "i"; }
      if (NODES[q].k === "rxant") { why += ", G(θ) " + fmtDb(r.gRxEff, 1) + "i"; }
      var mxp = (x(q - 1) + x(q)) / 2, myp = (y(vals[q - 1]) + y(vals[q])) / 2;
      place((dlt > 0 ? "+" : "−") + num(Math.abs(dlt), Math.abs(dlt) < 10 ? 1 : 0) + " dB (" + why + ")",
        mxp, myp, ring(14), { "font-size": 10, "font-weight": 600, fill: dlt > 0 ? C.good : C.bad }, dlt > 0 ? C.good : C.bad);
    }
    var ysn = (y(vals[5]) + y(noiseRx[0])) / 2;
    place("С/Ш " + num(r.snr, 0) + " dB", xs, ysn, [[40, 0], [-40, 0], [40, -16], [40, 16], [-40, 16]].concat(ring(20)), { "font-size": 10.5, fill: C.dim });
    place("шум " + fmtDbm(r.noiseAnt, 0), x(5), y(noiseRx[0]), ring(14), { "font-size": 10.5, fill: C.noise }, C.noise);
    if (over) {
      place("перегрузка приёмника", W - Rm - 70, y(-30), [[0, -10], [0, 12], [-120, -10], [-120, 12]], { "font-size": 10.5, fill: C.warn }, C.warn);
    }
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
        '<br><span class="small dim">КШ ' + num(s.nf, 1) + ' dB</span></span><div class="track"><div class="fill" style="width:' +
        w.toFixed(1) + "%;background:" + (s.snr > 10 ? C.good : s.snr > 3 ? C.warn : C.bad) + '"></div></div><span class="val">' +
        num(s.snr, 1) + " dB</span></div>";
    });
    html += '<p class="small dim">Сигнал у антенны ' + fmtDbm(r.rxAnt) + ", кабель −" + num(r.rxLoss, 1) +
      " dB, полоса " + fmtBw(S.bw) + ", МШУ +" + num(S.gLna, 0) + " dB с КШ " +
      num(S.nfLna, 1) + " dB.</p>";
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
      html += '<tr><td colspan="' + (CAB_F.length + 1) + '" class="small dim">dB на 10 м; на частоте ' + fmtMHz(S.freq) +
        ": " + Object.keys(CAB).map(function (k) { return k + " — " + num(cableLoss(k, S.freq, 10), 1); }).join(", ") +
        " dB</td></tr></tbody>";
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
    document.getElementById("o-adcBits").textContent = s.bits + " бит · ДД ≈ " + num(6.02 * s.bits + 1.76, 0) + " dB";
    var nyq = s.fs / 2;
    document.getElementById("adcNyq").textContent = num(nyq / 1000, 1) + " кГц" + (nyq <= 3000 ? " — тон 3 кГц искажён!" : "");
    document.getElementById("adcNyq").style.color = nyq <= 3000 ? C.bad : "";
    document.getElementById("adcLev").textContent = num(s.L, 0);
    document.getElementById("adcSnr").textContent = "≈ " + num(6.02 * s.bits + 1.76, 1) + " dB";
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
    // сетка уровней квантования: если шаг меньше 4 px, рисуем каждый k-й уровень (k — степень двойки)
    var stepPx = (Y(-1) - Y(1)) / Math.max(1, s.L - 1), kEvery = 1;
    while (stepPx * kEvery < 4) { kEvery *= 2; }
    for (var lv = 0; lv < s.L; lv += kEvery) { hline(ctx, L0, R0, Y(lv / (s.L - 1 || 1) * 2 - 1), alpha(C.grid, 0.9)); }
    hline(ctx, L0, R0, Y(0), C.grid);
    var gridTxt = kEvery > 1 ? "сетка: каждый " + num(kEvery, 0) + "-й из 2^" + s.bits + " = " + num(s.L, 0) + " уровней" :
      "сетка: все " + s.L + " уровней";
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
    text(ctx, gridTxt, L0 + 2, T0 - 2, C.dim, "left", "11px 'Segoe UI', sans-serif");
    // лупа: один отсчёт крупно — видно уровни квантования и ошибку округления (при 6 бит и больше)
    if (s.bits >= 6) {
      var n0 = 1, tv = adcSig(n0 / s.fs), q = quant(tv, s.L), lsb = 2 / (s.L - 1);
      var bw = Math.min(190, w * 0.36), bh = Math.min(120, h * 0.48), bx = R0 - bw, by = T0 + 6;
      ctx.fillStyle = C.panel; ctx.fillRect(bx, by, bw, bh);
      ctx.strokeStyle = C.accent; ctx.lineWidth = 1; ctx.strokeRect(bx, by, bw, bh);
      var ZY = function (v) { return by + bh / 2 - (v - q.v) / lsb * (bh / 7); };      // ±3,5 уровня по высоте
      for (var j = -3; j <= 3; j++) {
        hline(ctx, bx + 2, bx + bw - 2, ZY(q.v + j * lsb), j === 0 ? alpha(C.accent, 0.9) : alpha(C.grid, 0.95), j === 0 ? [] : [3, 3]);
      }
      var zx = bx + bw * 0.3;
      line(ctx, [[zx, ZY(q.v)], [bx + bw - 4, ZY(q.v)]], C.accent, 2.5);
      var ty = Math.max(by + 4, Math.min(by + bh - 4, ZY(tv)));
      line(ctx, [[zx, ty], [zx, ZY(q.v)]], C.bad, 1.5);
      ctx.beginPath(); ctx.arc(zx, ty, 3.5, 0, TAU); ctx.fillStyle = C.dim; ctx.fill();
      text(ctx, "лупа: отсчёт №1, ×" + num(Math.max(1, (bh / 7) / Math.max(1e-9, stepPx)), 0), bx + 6, by + 13, C.fg, "left",
        "600 11px 'Segoe UI', sans-serif");
      text(ctx, "ошибка ≤ ½ уровня", bx + 6, by + bh - 6, C.bad, "left", "11px 'Segoe UI', sans-serif");
      line(ctx, [[X(n0 / s.fs), Y(tv)], [zx, ty]], alpha(C.accent, 0.5), 1, [2, 3]);
    }
  });

  /* ------------------------------------------------------------------ 3. модуляция */
  var modMode = segBind("modMode", function (v) { modMode = v; modUpdate(); });
  var fskPre = [0];
  function fskSum(n) {  // сумма ±1 по битам до n — для непрерывной фазы FSK
    while (fskPre.length <= n) { var i = fskPre.length - 1; fskPre.push(fskPre[i] + (hash(i + 777) > 0.5 ? 1 : -1)); }
    return fskPre[n];
  }
  function modVol() { var el = document.getElementById("modVol"); return el ? Number(el.value) / 100 : 0.7; }
  function modFa() { var el = document.getElementById("modFa"); return el ? Number(el.value) : 2; }
  function modLoud(u) { return 0.6 + 0.4 * Math.sin(TAU * 0.23 * u); }  // громкость фразы медленно меняется
  // звук: громкость × медленная огибающая фразы × тон; FM берёт чистый тон, чтобы фаза считалась точно
  function modMsg(u) { return modVol() * modLoud(u) * Math.sin(TAU * modFa() * u); }
  function modMsgFm(u) { return modVol() * Math.sin(TAU * modFa() * u); }
  function modMsgFmInt(u) { return -modVol() * Math.cos(TAU * modFa() * u) / (TAU * modFa()); }
  function modUpdate() {
    var v = Number(document.getElementById("modDepth").value), analog = modMode === "am" || modMode === "fm";
    var lbl = { am: "", fm: "", fsk: "Разнос частот", qpsk: "Символов на экране" }[modMode];
    document.getElementById("modDepthLbl").textContent = lbl;
    document.getElementById("o-modDepth").textContent = modMode === "qpsk" ? String(Math.round(4 + v / 10)) : v + " %";
    $$(".mod-analog").forEach(function (el) { el.hidden = !analog; });
    $$(".mod-digital").forEach(function (el) { el.hidden = analog; });
    var vol = modVol(), fa = modFa();
    var ov = document.getElementById("o-modVol"), of = document.getElementById("o-modFa");
    if (ov) { ov.textContent = num(vol * 100, 0) + " %"; }
    if (of) { of.textContent = "≈ " + num(150 * fa, 0) + " Гц (условно)"; }
    document.getElementById("modCap").textContent = {
      am: "Сверху звук, снизу несущая. Огибающая несущей (пунктир) повторяет форму звука: громче звук — больше размах " +
        "огибающей. При 100 % несущая в самых тихих местах почти исчезает, больше — искажения.",
      fm: "Размах несущей постоянный. Мгновенное значение звука (напряжение в этот момент) сдвигает частоту: плюс — волны " +
        "гуще, минус — реже. Громкость задаёт, насколько далеко уходит частота (девиацию), частота звука — как быстро " +
        "частота несущей качается туда-сюда.",
      fsk: "Ноль — одна частота, единица — другая. Переход без скачка фазы (непрерывная фаза), чтобы сигнал не был широким.",
      qpsk: "Каждые 2 бита — одна из четырёх фаз. Справа созвездие: точка текущего символа (по линии посередине)."
    }[modMode];
    if (DEMOS.modsch) { DEMOS.modsch.dirty = true; }
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
    text(ctx, modMode === "am" || modMode === "fm" ? "звук (мгновенное напряжение микрофона)" : "биты", L0, 13, C.dim);
    text(ctx, "несущая в эфире", L0, rfY - rfA - 8, C.dim);
    var N = Math.round(R0 - L0) * 2;
    if (modMode === "am") {
      plot(ctx, L0, R0, N / 2, function (x) { return topY - topA * modMsg(U(x)); }, C.tx, 1.8);
      var env = function (u) { return (1 + modMsg(u)) / 2; };  // размах несущей ∝ 1 + m(t), m ≤ 1
      plot(ctx, L0, R0, N, function (x) { var u = U(x); return rfY - rfA * env(u) * Math.cos(TAU * fc * u); }, C.accent, 1.4);
      plot(ctx, L0, R0, N / 4, function (x) { return rfY - rfA * env(U(x)); }, C.tx, 1, [4, 4]);
      plot(ctx, L0, R0, N / 4, function (x) { return rfY + rfA * env(U(x)); }, C.tx, 1, [4, 4]);
    } else if (modMode === "fm") {
      plot(ctx, L0, R0, N / 2, function (x) { return topY - topA * modMsgFm(U(x)); }, C.tx, 1.8);
      var dev = 0.45 * fc;  // девиация при громкости 100 %
      plot(ctx, L0, R0, N, function (x) { var u = U(x); return rfY - rfA * Math.cos(TAU * fc * u + TAU * dev * modMsgFmInt(u)); }, C.accent, 1.4);
      // где частота выше, а где ниже
      var u0f = U(L0);
      for (var kk = Math.ceil(u0f * modFa() * 2); (kk / 2) / modFa() < U(R0); kk++) {
        var uu = (kk / 2 + 0.25) / modFa(), xq = L0 + (uu - u0f) * (R0 - L0);
        if (xq < L0 + 20 || xq > R0 - 20 || modVol() < 0.15) { continue; }
        text(ctx, kk % 2 === 0 ? "чаще" : "реже", xq, rfY + rfA + 14, kk % 2 === 0 ? C.good : C.warn, "center", "11px 'Segoe UI', sans-serif");
      }
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

  // схема модулятора: условные обозначения, «осциллограммы» на проводах и бегущие точки тока
  demo("modsch", true, function (d) {
    var ctx = d.ctx, w = d.w, h = d.h, t = d.t, fm = modMode === "fm" || modMode === "fsk", dig = modMode === "fsk" || modMode === "qpsk";
    var S1 = Math.min(1, w / 640), yA = h * 0.3, yC = h * 0.74, F11 = "11px 'Segoe UI', sans-serif";
    var xs = function (f) { return 14 + (w - 28) * f; };
    var wire = function (pts, phase) {  // провод и бегущие по нему точки
      line(ctx, pts, C.dim, 1.6);
      var len = 0, segs = [];
      for (var i = 1; i < pts.length; i++) { var l = Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]); segs.push(l); len += l; }
      for (var k = 0; k < Math.max(2, Math.floor(len / 28)); k++) {
        var pos = ((t * 40 + phase) + k * 28) % len, j = 0;
        while (j < segs.length - 1 && pos > segs[j]) { pos -= segs[j]; j++; }
        var f = segs[j] ? pos / segs[j] : 0, px = pts[j][0] + (pts[j + 1][0] - pts[j][0]) * f, py = pts[j][1] + (pts[j + 1][1] - pts[j][1]) * f;
        ctx.beginPath(); ctx.arc(px, py, 2.2, 0, TAU); ctx.fillStyle = C.accent; ctx.fill();
      }
    };
    var scope = function (cx, cy, fn, col, lbl, below) {  // маленькая осциллограмма над (или под) проводом
      var bw = 70 * S1 + 20, bh = 26, y0 = below ? cy + 6 : cy - bh - 6;
      ctx.fillStyle = C.panel; ctx.fillRect(cx - bw / 2, y0, bw, bh);
      ctx.strokeStyle = C.grid; ctx.lineWidth = 1; ctx.strokeRect(cx - bw / 2, y0, bw, bh);
      plot(ctx, cx - bw / 2 + 2, cx + bw / 2 - 2, 160, function (x, f) { return y0 + bh / 2 - fn(f + t * 0.15) * (bh / 2 - 3); }, col, 1.2);
      text(ctx, lbl, cx, below ? y0 + bh + 12 : y0 - 3, C.dim, "center", "10px 'Segoe UI', sans-serif");
    };
    var audio = dig ? function (u) { return hash(Math.floor(u * 4) + 777) > 0.5 ? 0.8 : -0.8; } : function (u) { return 0.85 * modLoud(u * 3) * Math.sin(TAU * 1.5 * u); };
    var carrier = function (u) { return Math.sin(TAU * 10 * u); };
    var am = function (u) { return 0.5 * (1 + audio(u)) * carrier(u); };
    var fmw = function (u) {  // фаза FM для отображения: интеграл звука, численно по 40 шагам
      var ph = 0, n = 40;
      for (var i = 0; i < n; i++) { ph += audio(u * (i + 0.5) / n) * u / n; }
      return Math.sin(TAU * 10 * u + TAU * 4 * ph);
    };
    var out = fm ? fmw : am;
    // источник звука: микрофон → микрофонный усилитель (или биты)
    var mx = xs(0.04), ax = xs(0.2), modx = xs(0.5), pax = xs(0.76), antx = xs(0.94);
    if (dig) {
      ctx.strokeStyle = C.fg; ctx.lineWidth = 1.6; ctx.strokeRect(mx - 14, yA - 12, 28, 24);
      text(ctx, "101", mx, yA + 4, C.fg, "center", "600 11px Consolas, monospace");
      text(ctx, "данные", mx, yA + 28, C.dim, "center", F11);
    } else {
      ctx.beginPath(); ctx.arc(mx, yA, 11, 0, TAU); ctx.strokeStyle = C.fg; ctx.lineWidth = 1.6; ctx.stroke();
      line(ctx, [[mx - 11, yA - 13], [mx - 11, yA + 13]], C.fg, 2.5);
      text(ctx, "микрофон", mx, yA + 28, C.dim, "center", F11);
    }
    var tri = function (x0, y0, lbl, above) {  // усилитель — треугольник
      ctx.beginPath(); ctx.moveTo(x0 - 14, y0 - 14); ctx.lineTo(x0 - 14, y0 + 14); ctx.lineTo(x0 + 14, y0); ctx.closePath();
      ctx.strokeStyle = C.fg; ctx.lineWidth = 1.6; ctx.stroke();
      text(ctx, lbl, x0, above ? y0 - 20 : y0 + 30, C.dim, "center", F11);
    };
    tri(ax, yA, dig ? "формирователь" : "микр. усилитель");
    wire([[mx + 13, yA], [ax - 14, yA]], 0);
    if (!fm) {
      // генератор несущей (кварц) → смеситель ⊗ ← звук
      var gx = xs(0.2);
      ctx.beginPath(); ctx.arc(gx, yC, 14, 0, TAU); ctx.strokeStyle = C.fg; ctx.lineWidth = 1.6; ctx.stroke();
      plot(ctx, gx - 8, gx + 8, 20, function (x, f) { return yC - 5 * Math.sin(TAU * f); }, C.fg, 1.4);
      ctx.strokeRect(gx - 34, yC - 6, 10, 12);  // кварц
      text(ctx, "генератор", gx - 6, yC + 28, C.dim, "center", F11);
      text(ctx, "несущей (кварц)", gx - 6, yC + 41, C.dim, "center", F11);
      ctx.beginPath(); ctx.arc(modx, (yA + yC) / 2, 15, 0, TAU); ctx.stroke();
      line(ctx, [[modx - 10, (yA + yC) / 2 - 10], [modx + 10, (yA + yC) / 2 + 10]], C.fg, 1.6);
      line(ctx, [[modx - 10, (yA + yC) / 2 + 10], [modx + 10, (yA + yC) / 2 - 10]], C.fg, 1.6);
      text(ctx, modMode === "qpsk" ? "I/Q-модулятор" : "смеситель (умножитель)", modx - 20, (yA + yC) / 2 + 26, C.dim, "right", F11);
      wire([[ax + 14, yA], [modx, yA], [modx, (yA + yC) / 2 - 15]], 10);
      wire([[gx + 14, yC], [modx, yC], [modx, (yA + yC) / 2 + 15]], 20);
      scope((ax + modx) / 2, yA, audio, C.tx, dig ? "биты" : "звук");
      scope((gx + modx) / 2 + 22, yC, carrier, C.fg, "несущая", true);
      wire([[modx + 15, (yA + yC) / 2], [pax - 14, (yA + yC) / 2]], 30);
      scope((modx + pax) / 2, (yA + yC) / 2, out, C.accent, modMode === "qpsk" ? "QPSK" : "AM");
    } else {
      // VCO: звук подаётся на управляющий вход (варикап меняет ёмкость контура → частоту)
      var vy = (yA + yC) / 2 + 10;
      ctx.strokeStyle = C.fg; ctx.lineWidth = 1.6; ctx.strokeRect(modx - 30, vy - 22, 60, 44);
      plot(ctx, modx - 18, modx + 18, 40, function (x, f) { return vy - 6 * Math.sin(TAU * f * (2 + 1.5 * f)); }, C.fg, 1.4);
      text(ctx, "VCO", modx, vy + 18, C.fg, "center", "600 11px 'Segoe UI', sans-serif");
      // варикап на управляющем входе
      var vx = modx, vty = vy - 40;
      ctx.beginPath(); ctx.moveTo(vx - 7, vty - 6); ctx.lineTo(vx + 7, vty - 6); ctx.lineTo(vx, vty + 4); ctx.closePath(); ctx.stroke();
      line(ctx, [[vx - 7, vty + 4], [vx + 7, vty + 4]], C.fg, 1.6); line(ctx, [[vx - 7, vty + 7], [vx + 7, vty + 7]], C.fg, 1.6);
      line(ctx, [[vx + 10, vty + 2], [vx + 18, vty - 6]], C.fg, 1.2);
      text(ctx, "варикап", vx - 12, vty + 4, C.dim, "right", F11);
      wire([[ax + 14, yA], [vx - 40, yA], [vx - 40, vty - 14], [vx, vty - 14], [vx, vty - 6]], 10);
      line(ctx, [[vx, vty + 7], [vx, vy - 22]], C.dim, 1.6);
      scope((ax + vx - 40) / 2, yA, audio, C.tx, dig ? "биты" : "звук → управляющее напряжение");
      text(ctx, "генератор, управляемый", modx, vy + 38, C.dim, "center", F11);
      text(ctx, "напряжением", modx, vy + 51, C.dim, "center", F11);
      wire([[modx + 30, vy], [pax - 14, vy]], 30);
      scope((modx + pax) / 2 + 6, vy, out, C.accent, modMode === "fsk" ? "FSK" : "FM");
    }
    var py0 = fm ? (yA + yC) / 2 + 10 : (yA + yC) / 2;
    tri(pax, py0, "усилитель мощности", false);
    wire([[pax + 14, py0], [antx, py0], [antx, py0 - 30]], 40);
    line(ctx, [[antx, py0 - 30], [antx - 10, py0 - 46]], C.fg, 2); line(ctx, [[antx, py0 - 30], [antx + 10, py0 - 46]], C.fg, 2);
    line(ctx, [[antx, py0 - 30], [antx, py0 - 48]], C.fg, 2);
    text(ctx, "антенна", antx, py0 + 18, C.dim, "center", F11);
    text(ctx, fm ? "Частотная модуляция: звук управляет частотой генератора (VCO)" :
      (modMode === "qpsk" ? "Фазовая модуляция: биты задают фазу несущей в I/Q-смесителе" : "Амплитудная модуляция: звук умножается на несущую в смесителе"),
      10, 14, C.fg, "left", "600 12px 'Segoe UI', sans-serif");
  });

  // звук модуляции: модулирующий сигнал, несущая со сдвигом в звуковой диапазон (условно 2 кГц) и результат демодуляции
  function modPlay(kind) {
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC || !(modMode === "am" || modMode === "fm")) { return; }
    if (!actx) { actx = new AC(); }
    if (actx.state === "suspended") { actx.resume(); }
    var sr = actx.sampleRate, n = Math.round(sr * 2), buf = actx.createBuffer(1, n, sr), x = buf.getChannelData(0);
    var vol = modVol(), fa = 150 * modFa(), fc = 2000, dev = 700, ph = 0, phm = 0, msg = new Float32Array(n), sig = new Float32Array(n);
    for (var i = 0; i < n; i++) {
      var t = i / sr;
      phm += TAU * fa / sr;
      var loud = modMode === "am" ? 0.6 + 0.4 * Math.sin(TAU * 0.7 * t) : 1, m = vol * loud * Math.sin(phm);
      msg[i] = m;
      if (modMode === "am") { ph += TAU * fc / sr; sig[i] = 0.5 * (1 + m) * Math.cos(ph); }
      else { ph += TAU * (fc + dev * m) / sr; sig[i] = Math.cos(ph); }
    }
    var outA = msg;
    if (kind === "c") { outA = sig; }
    if (kind === "d") {  // честная демодуляция: перенос в ноль (I/Q), скользящее среднее на период несущей, огибающая или фаза
      var L = Math.round(sr / fc), I = new Float32Array(n), Q = new Float32Array(n), si = 0, sq = 0, dem = new Float32Array(n), pr = 0, pi = 0;
      for (var k = 0; k < n; k++) {
        var a = TAU * fc * k / sr;
        I[k] = sig[k] * Math.cos(a); Q[k] = -sig[k] * Math.sin(a);
        si += I[k]; sq += Q[k];
        if (k >= L) { si -= I[k - L]; sq -= Q[k - L]; }
        var zi = si / L, zq = sq / L;
        if (modMode === "am") { dem[k] = 2 * Math.hypot(zi, zq); }
        else { dem[k] = Math.atan2(zq * pr - zi * pi, zi * pr + zq * pi) * sr / (TAU * dev); }
        pr = zi; pi = zq;
      }
      var mean = 0;
      for (k = 0; k < n; k++) { mean += dem[k]; }
      mean /= n;
      for (k = 0; k < n; k++) { dem[k] -= modMode === "am" ? mean : 0; }
      outA = dem;
    }
    var peak = 1e-6;
    for (i = 0; i < n; i++) { peak = Math.max(peak, Math.abs(outA[i])); }
    for (i = 0; i < n; i++) {
      var env = Math.min(1, i / sr * 20, (2 - i / sr) * 20);
      x[i] = outA[i] / peak * 0.18 * env;
    }
    var src = actx.createBufferSource();
    src.buffer = buf;
    src.connect(actx.destination);
    src.start();
  }

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
  // тип антенны — единственный источник истины: ползунок меняет только G в пределах типа
  var ANT_TYPES = {
    iso: { name: "изотропная", lo: 0, hi: 0, def: 0 },
    dipole: { name: "штырь / диполь", lo: -5, hi: 2.15, def: 2.15 },
    colinear: { name: "коллинеар", lo: 3, hi: 9, def: 6 },
    yagi: { name: "Яги (волновой канал)", lo: 5, hi: 20, def: 10 },
    lpda: { name: "логопериодическая (LPDA)", lo: 5, hi: 8, def: 6.5, band: [100, 1300] }
  };
  function antKind(g, type) {  // семейство формы: iso / omni (бублик) / col (сплющенный бублик) / yagi (луч)
    if (type === "iso") { return "iso"; }
    if (type === "yagi" || type === "lpda") { return "yagi"; }
    if (type === "dipole") { return "omni"; }
    if (type === "colinear") { return "col"; }
    return g <= 6 ? "omni" : "yagi";  // приёмная антенна: тип по усилению
  }
  // Направление u = (x, y, z): x — горизонт в сторону приёмника, y — горизонт вбок, z — зенит.
  // Поляризация задаёт, куда смотрят вибраторы: вертикальная — вдоль z, горизонтальная — вдоль y.
  // Поворот антенны не меняет интеграл по сфере, поэтому нормировка остаётся прежней.
  function polAxis(pol) { return pol === "h" ? [0, 1, 0] : [0, 0, 1]; }
  function antShape(g, type, pol) {
    var G = lin(g), kind = antKind(g, type), P = polAxis(pol), f3;
    if (kind === "iso") {
      f3 = function () { return G; };
    } else if (kind === "omni" || kind === "col") {  // бублик вокруг оси вибратора
      var fe;
      if (kind === "omni") {
        var key = g.toFixed(2);
        if (OMNI_M[key] === undefined) { OMNI_M[key] = omniM(G); }
        var m = OMNI_M[key], peak = Math.max(G, 1.5) * (G < 1.5 ? G / 1.5 : 1);
        fe = function (e) { return Math.max(peak * Math.pow(Math.abs(Math.cos(e)), m), peak * 1e-3); };  // провал вдоль оси
      } else {
        var c = COL[g.toFixed(2)] || (COL[g.toFixed(2)] = colFit(G));
        fe = function (e) { return G * colRel(e, c.m); };
      }
      f3 = function (x, y, z) { return fe(Math.asin(Math.max(-1, Math.min(1, x * P[0] + y * P[1] + z * P[2])))); };
    } else {  // Яги и LPDA: луч вдоль x; множитель вибратора 1 − (u·p)² сужает лепесток в E-плоскости
      var yk = (type === "lpda" ? "L" : "Y") + g.toFixed(2);
      var yf = YAGI[yk] || (YAGI[yk] = yagiFit(Math.max(G, 1.01)));
      f3 = function (x, y, z) {
        var up = x * P[0] + y * P[1] + z * P[2];
        return G * yagiRel(Math.acos(Math.max(-1, Math.min(1, x))), yf.n, yf.k) * Math.max(1e-4, 1 - up * up);
      };
    }
    return {
      dir: kind === "iso" ? "iso" : kind === "yagi" ? "yagi" : "omni", pol: pol === "h" ? "h" : "v", f3: f3,
      f: function (a) { return f3(Math.cos(a), 0, Math.sin(a)); },    // вертикальное сечение (вид сбоку)
      fh: function (a) { return f3(Math.cos(a), Math.sin(a), 0); }    // горизонтальное сечение (вид сверху)
    };
  }
  // Яги: форма относительно пика = max(главный лепесток ((1+cos a)/2)^n, k·задние/боковые −15/−20 dB) × (1 − (u·p)²).
  // Среднее множителя вибратора по повороту вокруг оси = 1 − sin²a/2, поэтому нормировка — одномерный интеграл:
  // ½∫ rel(a)·(1 − sin²a/2)·sin a da = 1/G, тогда ∮G dΩ = 4π (КПД 100 %). Если задние/боковые «съедают» больше
  // 20 % излучения, их уровень k снижается, а n подбирается так, чтобы главный лепесток давал ровно G.
  // Самопроверка selfTestAnt (двумерный интеграл по сфере, обе поляризации): среднее G / КПД = 1 ± 1 %.
  // Всенаправленная модель: среднее = КПД = min(1, G/1,5); изотропная: среднее = КПД = G.
  var YAGI = {}, COL = {};
  // коллинеар: форма относительно пика по углу места e = max(|cos e|^m, 0,01·|sin 2e|) — главный лепесток к
  // горизонту и боковые около −20 dB под 45°; m подбирается так, чтобы среднее по сфере было 1/G
  function colRel(e, m) { return Math.max(Math.pow(Math.abs(Math.cos(e)), m), 0.01 * Math.abs(Math.sin(2 * e))); }
  function colFit(G) {
    var avg = function (m) {
      var acc = 0, N = 720;
      for (var i = 0; i < N; i++) { var e = -Math.PI / 2 + Math.PI * (i + 0.5) / N; acc += colRel(e, m) * Math.cos(e) * Math.PI / N; }
      return acc / 2;
    };
    var lo = 0, hi = 2000;
    for (var it = 0; it < 60; it++) { var m = (lo + hi) / 2; if (1 / avg(m) < G) { lo = m; } else { hi = m; } }
    return { m: (lo + hi) / 2 };
  }
  function yagiRel(a, n, k) {
    return Math.max(Math.pow((1 + Math.cos(a)) / 2, n), k * Math.pow(10, -(15 + 5 * Math.abs(Math.sin(a))) / 10));
  }
  function sphereAvg(f) {  // ½∫₀^π f(a)·(1 − sin²a/2)·sin a da — с множителем вибратора
    var acc = 0, N = 720;
    for (var i = 0; i < N; i++) {
      var a = Math.PI * (i + 0.5) / N, s = Math.sin(a);
      acc += f(a) * (1 - s * s / 2) * s * Math.PI / N;
    }
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
  function antEff(g, type) {  // КПД, заложенный в модель
    var kind = antKind(g, type);
    return kind === "iso" ? Math.min(1, lin(g)) : kind === "omni" ? Math.min(1, lin(g) / 1.5) : 1;  // col, yagi: 100 %
  }
  function antGainAt(g, deg, type, pol) { return 10 * LOG10(Math.max(1e-6, antShape(g, type, pol).f(deg * Math.PI / 180))); }
  function beamwidth(sh) {  // ширина главного лепестка по −3 dB: [вертикальное сечение, горизонтальное], null — нет спада
    if (sh.dir === "iso") { return [null, null]; }
    var one = function (f) {
      var p = f(0), a = 0;
      while (a < Math.PI && f(a) > p / 2) { a += Math.PI / 1800; }
      return a >= Math.PI ? null : 2 * a * 180 / Math.PI;
    };
    return [one(sh.f), one(sh.fh)];
  }
  // потери на несовпадении поляризаций: одинаковые — 0, круговая против линейной — 3 dB, скрещенные линейные — 20 dB
  function polLoss(tx, rx) {
    if (tx === rx) { return 0; }
    if (tx === "c" || rx === "c") { return 3; }
    return 20;
  }
  var POL_NAME = { v: "вертикальная", h: "горизонтальная", c: "круговая" };

  var PAT_NOTE = {
    iso: "Изотропная антенна — воображаемая сфера: во все стороны одинаково, 0 dBi. Эталон для отсчёта dBi.",
    dipole: "Штырь или диполь — вертикальный «бублик»: по горизонту во все стороны одинаково, вдоль оси антенны " +
      "(вверх и вниз) провал. Вертикальное сечение — восьмёрка, горизонтальное — круг.",
    colinear: "Коллинеар (несколько полуволновых секций друг над другом, синфазно) — тот же бублик, что у диполя, но " +
      "сплющенный: в вертикальном сечении два узких лепестка к горизонту слева и справа, ширина по −3 dB падает с " +
      "числом секций (≈ 79° у диполя → ≈ 27° при 6 dBi), плюс небольшие боковые лепестки вверх и вниз под углом. " +
      "Горизонтальное сечение — круг: по азимуту антенна всенаправленная.",
    lpda: "Логопериодическая (LPDA) — направленная, как Яги, но шире луч и меньше усиление (5–8 dBi), задний " +
      "лепесток −15…−20 dB. Зато широкополосная: усиление почти постоянно в широком диапазоне (например, 100–1300 МГц).",
    yagi: "Яги (волновой канал) — луч вдоль штанги: с ростом усиления луч сужается в обеих плоскостях, " +
      "задний лепесток около −15 dB, боковые около −20 dB."
  };
  function drawAnt(ctx, type, pol, P, cx, cy) {  // P(x, y, z) → [sx, sy]; луч — +x, вверх — +z
    var ev = polAxis(pol);
    var at = function (x, t) { return [x + 0 * t, ev[1] * t, ev[2] * t]; };  // точка вибратора: по штанге x, вдоль ev на t
    var seg = function (a, b, lw) {
      var p0 = P(a[0], a[1], a[2]), p1 = P(b[0], b[1], b[2]);
      if (Math.abs(p0[0] - p1[0]) + Math.abs(p0[1] - p1[1]) < 1.5) {  // вибратор смотрит на нас торцом — точка
        ctx.beginPath(); ctx.arc(p0[0], p0[1], 2.4, 0, TAU); ctx.fillStyle = C.fg; ctx.fill();
      } else { line(ctx, [[p0[0], p0[1]], [p1[0], p1[1]]], C.fg, lw); }
    };
    if (type === "yagi") {  // штанга вдоль луча, вибраторы поперёк по направлению поляризации
      seg([-16, 0, 0], [18, 0, 0], 3);
      [[-12, 9], [-4, 8], [4, 7], [12, 6.5]].forEach(function (e) { seg(at(e[0], -e[1]), at(e[0], e[1]), 2); });
    } else if (type === "lpda") {  // вибраторы убывающей длины к приёмнику, две собирающие линии, питание крест-накрест
      seg([-16, 0, 0], [18, 0, 0], 3);
      [[-14, 12], [-7, 10], [0, 8], [6, 6.5], [11, 5], [15, 4]].forEach(function (e) { seg(at(e[0], -e[1]), at(e[0], e[1]), 2); });
    } else if (type === "iso") {
      ctx.beginPath(); ctx.arc(cx, cy, 4, 0, TAU); ctx.fillStyle = C.fg; ctx.fill();
    } else {
      seg(at(0, -16), at(0, 16), 3);
      if (type === "colinear") { [-6, 6].forEach(function (t) { var q = at(0, t); seg([q[0] - 3, q[1], q[2]], [q[0] + 3, q[1], q[2]], 2); }); }
    }
  }
  // вид диаграммы: 3D-каркас (поворот мышью/пальцем или автоповорот) и сечения
  var PAT = { view: "3d", yaw: -0.6, pitch: 0.38, drag: null, gShow: null };
  function patTypeSync(t) {
    $$("#patType button").forEach(function (b) { b.classList.toggle("on", b.getAttribute("data-v") === t); });
    var pv = S.txPol === "h" ? "h" : "v";
    $$("#patPol button").forEach(function (b) { b.classList.toggle("on", b.getAttribute("data-v") === pv); });
  }
  demo("pattern", true, function (d, dt) {
    var ctx = d.ctx, w = d.w, h = d.h, type = S.txAnt || "dipole";
    // плавный переход к новому усилению — видно, как сжимается «бублик» или сужается луч
    if (PAT.gShow === null || PAT.type !== type) { PAT.gShow = S.gTx; PAT.type = type; }
    PAT.gShow += (S.gTx - PAT.gShow) * Math.min(1, (dt || 1) * 6);
    if (Math.abs(S.gTx - PAT.gShow) < 0.03) { PAT.gShow = S.gTx; }
    var g = Math.round(PAT.gShow * 20) / 20;
    var pol = S.txPol === "h" ? "h" : "v", sh = antShape(g, type, pol), shape = sh.f, dir = sh.dir;
    var floor = -30, top = 20, Rr = Math.min(w * 0.4, h * 0.38), cx = w / 2, cy = h / 2 + 8;
    var rad = function (db) { return Math.max(0, (db - floor) / (top - floor)) * Rr; };
    var rOf = function (a) { return rad(10 * LOG10(Math.max(1e-6, shape(a)))); };
    var FS = "11px 'Segoe UI', sans-serif";
    var bw = beamwidth(sh);
    if (PAT.view === "3d") {
      if (!PAT.drag && dt > 0) { PAT.yaw += dt * 0.35; }
      var cyw = Math.cos(PAT.yaw), syw = Math.sin(PAT.yaw), cp = Math.cos(PAT.pitch), sp = Math.sin(PAT.pitch);
      var proj = function (x, y, z) {  // z — вверх; поворот вокруг z (yaw), наклон камеры (pitch)
        var x1 = x * cyw - y * syw, y1 = x * syw + y * cyw;
        var y2 = y1 * cp - z * sp, z2 = y1 * sp + z * cp;
        return [cx + x1, cy - z2, y2];
      };
      // земля и горизонт
      ctx.beginPath();
      for (var i = 0; i <= 72; i++) {
        var ph = TAU * i / 72, P = proj(Rr * 1.08 * Math.cos(ph), Rr * 1.08 * Math.sin(ph), 0);
        if (i) { ctx.lineTo(P[0], P[1]); } else { ctx.moveTo(P[0], P[1]); }
      }
      ctx.fillStyle = alpha(C.air, 0.08); ctx.fill();
      ctx.strokeStyle = alpha(C.air, 0.6); ctx.lineWidth = 1; ctx.setLineDash([4, 4]); ctx.stroke(); ctx.setLineDash([]);
      // сетка строится вокруг оси симметрии фигуры: у бублика — ось вибратора, у Яги/LPDA — ось луча,
      // поэтому «бублик» при горизонтальной поляризации виден как бублик с горизонтальной осью
      var pole = dir === "yagi" ? [1, 0, 0] : dir === "omni" ? polAxis(pol) : [0, 0, 1];
      var b1 = pole[0] ? [0, 1, 0] : pole[1] ? [1, 0, 0] : [1, 0, 0], b2 = pole[0] ? [0, 0, 1] : pole[1] ? [0, 0, 1] : [0, 1, 0];
      var pt = function (e, az) {  // точка поверхности: e — «широта» от экватора фигуры, az — поворот вокруг её оси
        var ce = Math.cos(e), se = Math.sin(e), ca = Math.cos(az), sa = Math.sin(az);
        var ux = ce * (ca * b1[0] + sa * b2[0]) + se * pole[0];
        var uy = ce * (ca * b1[1] + sa * b2[1]) + se * pole[1];
        var uz = ce * (ca * b1[2] + sa * b2[2]) + se * pole[2];
        var r = rad(10 * LOG10(Math.max(1e-6, sh.f3(ux, uy, uz))));
        return proj(r * ux, r * uy, r * uz);
      };
      var A1 = [-Math.PI / 2, Math.PI / 2];
      var seg = function (pts) {
        for (var k = 1; k < pts.length; k++) {
          var dz = (pts[k][2] + pts[k - 1][2]) / 2, front = dz < 0;
          ctx.strokeStyle = alpha(C.tx, front ? 0.85 : 0.25); ctx.lineWidth = front ? 1.3 : 1;
          ctx.beginPath(); ctx.moveTo(pts[k - 1][0], pts[k - 1][1]); ctx.lineTo(pts[k][0], pts[k][1]); ctx.stroke();
        }
      };
      var NU = 36, NV = 24, u, v, line1;
      for (v = 0; v < NV; v++) {        // «меридианы»
        line1 = [];
        for (u = 0; u <= NU; u++) { line1.push(pt(A1[0] + (A1[1] - A1[0]) * u / NU, TAU * v / NV)); }
        seg(line1);
      }
      for (u = 1; u < 12; u++) {        // «параллели»
        line1 = [];
        for (v = 0; v <= 48; v++) { line1.push(pt(A1[0] + (A1[1] - A1[0]) * u / 12, TAU * v / 48)); }
        seg(line1);
      }
      // антенна и направления
      drawAnt(ctx, type, pol, proj, cx, cy);
      var th = (S.thTx || 0) * Math.PI / 180, ge = rOf(th);
      var tp = dir === "yagi" ? proj(ge * Math.cos(th), 0, ge * Math.sin(th)) : proj(ge * Math.cos(th), 0, ge * Math.sin(th));
      var far = proj(Rr * 1.15 * Math.cos(th), 0, Rr * 1.15 * Math.sin(th));
      line(ctx, [[cx, cy], [far[0], far[1]]], C.rx, 2, [6, 4]);
      ctx.beginPath(); ctx.arc(tp[0], tp[1], 4.5, 0, TAU); ctx.fillStyle = C.rx; ctx.fill();
      text(ctx, "к приёмнику", far[0], far[1] - 6, C.rx, far[0] > cx ? "right" : "left", "600 11px 'Segoe UI', sans-serif");
      var zt = proj(0, 0, Rr * 1.12);
      text(ctx, "↑ зенит", zt[0], zt[1] - 2, C.dim, "center", FS);
      text(ctx, "3D: потяните мышью или пальцем, двойной щелчок — сброс", 10, h - 8, C.dim, "left", FS);
    } else {
      var horiz = PAT.view === "h", pb = [];
      var cPut = function (txt, x0, y0, cands, col, font) {  // подпись в первом свободном месте из кандидатов
        ctx.font = font || FS;
        var wd = ctx.measureText(txt).width;
        for (var c = 0; c < cands.length; c++) {
          var xx = x0 + cands[c][0], yy = y0 + cands[c][1], al = cands[c][2] || "center";
          var bx0 = al === "center" ? xx - wd / 2 : al === "left" ? xx : xx - wd;
          var bb = { x0: bx0 - 2, y0: yy - 11, x1: bx0 + wd + 2, y1: yy + 3 };
          if (bb.x0 < 2 || bb.x1 > w - 2 || bb.y0 < 22 || bb.y1 > h - 2) { continue; }
          if (pb.every(function (o) { return bb.x1 <= o.x0 || bb.x0 >= o.x1 || bb.y1 <= o.y0 || bb.y0 >= o.y1; })) {
            pb.push(bb); text(ctx, txt, xx, yy, col, al, font || FS); return true;
          }
        }
        return false;
      };
      // занятые места: антенна и точка на луче
      var P2 = function (x, y, z) { return horiz ? [cx + x, cy - y] : [cx + x, cy - z]; };
      (function () {  // место под изображением антенны — по её проекции в этом виде
        var ev = polAxis(pol), pts = [[-16, 0, 0], [18, 0, 0], [0, ev[1] * 16, ev[2] * 16], [0, -ev[1] * 16, -ev[2] * 16]];
        if (dir === "omni") { pts = [[-3, 0, 0], [3, 0, 0], [0, ev[1] * 16, ev[2] * 16], [0, -ev[1] * 16, -ev[2] * 16]]; }
        if (dir === "yagi") { pts.push([-14, ev[1] * 12, ev[2] * 12], [-14, -ev[1] * 12, -ev[2] * 12]); }
        var xs = pts.map(function (q) { return P2(q[0], q[1], q[2])[0]; }), ys = pts.map(function (q) { return P2(q[0], q[1], q[2])[1]; });
        pb.push({ x0: Math.min.apply(null, xs) - 6, y0: Math.min.apply(null, ys) - 6, x1: Math.max.apply(null, xs) + 6, y1: Math.max.apply(null, ys) + 6 });
      })();
      if (!horiz) {
        ctx.fillStyle = alpha(C.air, 0.07); ctx.fillRect(6, cy, w - 12, h - cy - 2);
        hline(ctx, 6, w - 6, cy, alpha(C.air, 0.9), [6, 4]);
      } else {
        hline(ctx, cx - Rr - 10, cx + Rr + 10, cy, C.grid);
        line(ctx, [[cx, cy - Rr - 10], [cx, cy + Rr + 10]], C.grid, 1);
      }
      [-20, -10, 0, 10, 20].forEach(function (db) {
        ctx.beginPath(); ctx.arc(cx, cy, rad(db), 0, TAU);
        ctx.strokeStyle = db === 0 ? alpha(C.dim, 0.9) : C.grid; ctx.lineWidth = 1;
        ctx.setLineDash(db === 0 ? [4, 4] : []); ctx.stroke(); ctx.setLineDash([]);
      });
      var fsec = horiz ? sh.fh : sh.f;
      ctx.beginPath();
      for (var j = 0; j <= 360; j++) {
        var aa = TAU * j / 360, rr = rad(10 * LOG10(Math.max(1e-6, fsec(aa))));
        var px = cx + rr * Math.cos(aa), py = cy - rr * Math.sin(aa);
        if (j) { ctx.lineTo(px, py); } else { ctx.moveTo(px, py); }
      }
      ctx.closePath();
      ctx.fillStyle = alpha(C.tx, 0.22); ctx.fill();
      ctx.strokeStyle = C.tx; ctx.lineWidth = 2; ctx.stroke();
      // сама антенна: в вертикальном сечении штырь стоит, в горизонтальном виден сверху точкой
      drawAnt(ctx, type, pol, P2, cx, cy);
      // луч на приёмник
      var th2 = horiz ? 0 : (S.thTx || 0) * Math.PI / 180, ge2 = 10 * LOG10(Math.max(1e-6, fsec(th2)));
      var rx = cx + Rr * 1.02 * Math.cos(th2), ry = cy - Rr * 1.02 * Math.sin(th2);
      line(ctx, [[cx, cy], [rx, ry]], C.rx, 2, [6, 4]);
      var rg = rad(ge2), dx2 = cx + rg * Math.cos(th2), dy2 = cy - rg * Math.sin(th2);
      ctx.beginPath(); ctx.arc(dx2, dy2, 5, 0, TAU); ctx.fillStyle = C.rx; ctx.fill();
      pb.push({ x0: dx2 - 7, y0: dy2 - 7, x1: dx2 + 7, y1: dy2 + 7 });
      // подписи по важности; каждая — в первое свободное место
      var B11 = "600 11px 'Segoe UI', sans-serif", up = ry < cy - 4, dn = ry > cy + 4;
      cPut("на приёмник: " + num(ge2, 1) + " dBi", rx, ry,
        [[0, up ? -8 : 16, rx > cx ? "right" : "left"], [0, up ? 16 : -8, rx > cx ? "right" : "left"],
          [0, up ? -22 : 30, rx > cx ? "right" : "left"], [-Rr * 0.4, dn ? 18 : -10, "center"], [-Rr * 0.4, dn ? -10 : 18, "center"], [0, 44, "right"], [0, -36, "right"], [w - 8 - rx, h - 22 - ry, "right"]], C.rx, B11);
      if (!horiz) {
        if (dir !== "iso") { cPut("вид сбоку: " + (pol === "h" ? "H" : "E") + "-плоскость", 10, 44, [[0, 0, "left"]], C.tx); }
        cPut("↑ вверх (зенит)", cx, 26 + 12, [[0, 0], [Rr * 0.5, 0]], C.dim);
        cPut("↓ вниз (земля)", cx, h - 6, [[0, 0], [Rr * 0.5, 0]], C.dim);
        cPut("горизонт", 8, cy - 5, [[0, 0, "left"], [0, 15, "left"]], C.air);
        cPut("горизонт →", w - 8, cy - 5, [[0, 0, "right"], [0, 15, "right"], [0, -14, "right"]], C.air);
      } else {
        cPut("вид сверху: плоскость горизонта" + (dir === "iso" ? "" : ", " + (pol === "h" ? "E" : "H") + "-плоскость"), cx, 38, [[0, 0]], C.dim);
      }
      if (dir === "yagi") {
        cPut("вдоль оси антенны →", cx + 24, cy, [[0, 16, "left"], [0, -8, "left"], [0, 30, "left"], [0, -22, "left"], [0, 44, "left"], [0, -36, "left"], [w - 8 - cx - 24, 60 - cy, "right"]], C.fg);
      } else if (dir === "omni" && (horiz === (pol === "h"))) {  // ось вибратора видна в этом сечении
        cPut("ось антенны", cx + 10, cy - 18, [[0, 0, "left"], [-20, 0, "right"], [0, 34, "left"], [12, 22, "left"]], C.fg);
      }
      [0, 10, 20].forEach(function (db) {
        cPut((db > 0 ? "+" : "") + db + " dBi", cx - rad(db) * 0.707 - 3, cy - rad(db) * 0.707 - 2, [[0, 0, "right"], [0, 12, "right"]], C.dim,
          "10px 'Segoe UI', sans-serif");
      });
    }
    var hdr1 = ANT_TYPES[type].name + " · " + num(g, 2).replace(/0$/, "") + " dBi",
      hdr2 = "КПД " + num(100 * antEff(g, type), 0) + " % · " + (S.txPol === "c" ? "круговая (рисуется как вертикальная)" : POL_NAME[pol]);
    var bwf = function (v) { return v === null ? "360°" : num(v, 0) + "°"; };
    var bwt = dir === "iso" ? "ширина луча: во все стороны одинаково" :
      "по −3 dB: сбоку (" + (pol === "h" ? "H" : "E") + ") " + bwf(bw[0]) + ", сверху (" + (pol === "h" ? "E" : "H") + ") " + bwf(bw[1]);
    ctx.font = "600 12px 'Segoe UI', sans-serif";
    var wH = ctx.measureText(hdr1 + " · " + hdr2).width;
    ctx.font = "600 11px 'Segoe UI', sans-serif";
    var wB = ctx.measureText(bwt).width;
    var F12 = "600 12px 'Segoe UI', sans-serif";
    if (wH + wB + 30 <= w) {               // широко: всё в одну строку
      text(ctx, hdr1 + " · " + hdr2, 10, 14, C.fg, "left", F12);
      text(ctx, bwt, w - 10, 14, C.tx, "right", "600 11px 'Segoe UI', sans-serif");
    } else if (wH + 20 <= w) {             // средне: заголовок, под ним ширина луча
      text(ctx, hdr1 + " · " + hdr2, 10, 14, C.fg, "left", F12);
      text(ctx, bwt, 10, 29, C.tx, "left", "600 11px 'Segoe UI', sans-serif");
    } else {                               // узко: три строки
      text(ctx, hdr1, 10, 14, C.fg, "left", F12);
      text(ctx, hdr2, 10, 28, C.dim, "left", "11px 'Segoe UI', sans-serif");
      text(ctx, bwt, 10, 42, C.tx, "left", "600 10px 'Segoe UI', sans-serif");
    }
    var bwEl = document.getElementById("patBw");
    if (bwEl) { bwEl.textContent = bwt.replace("по −3 dB: ", ""); }
    var note = document.getElementById("patNote"), nt = PAT_NOTE[type] || "";
    if (note && note.textContent !== nt) { note.textContent = nt; }
  });
  // самопроверка (вызывается из консоли и при проверке страницы): для всех переходов «тип A → тип B» форма
  // совпадает с прямой загрузкой типа B, а среднее усиление по сфере равно КПД для всех типов и пределов
  function selfTestAnt() {
    var sel = document.getElementById("txAnt"), gs = document.getElementById("gTx");
    var keep = [sel.value, gs.value], errs = [], types = Object.keys(ANT_TYPES);
    var sample = function (g, t, pl) {
      var sh = antShape(g, t, pl);
      return [0, 15, 30, 45, 60, 90, 120, 180].map(function (deg) { return 10 * LOG10(Math.max(1e-9, sh.f(deg * Math.PI / 180))); });
    };
    var pl0 = S.txPol === "h" ? "h" : "v";
    types.forEach(function (b) {
      var ref = sample(ANT_TYPES[b].def, b, pl0);
      types.forEach(function (a) {
        sel.value = a; update(); sel.value = b; update();
        var got = sample(S.gTx, S.txAnt, pl0);
        if (S.txAnt !== b || Math.abs(S.gTx - ANT_TYPES[b].def) > 1e-9 ||
          got.some(function (v, i) { return Math.abs(v - ref[i]) > 1e-6; })) { errs.push(a + "→" + b); }
      });
      [ANT_TYPES[b].lo, ANT_TYPES[b].def, ANT_TYPES[b].hi].forEach(function (g) {
        ["v", "h"].forEach(function (pl) {
          var sh = antShape(g, b, pl), acc = 0, NE = 180, NA = 180;
          for (var i = 0; i < NE; i++) {
            var e = -Math.PI / 2 + Math.PI * (i + 0.5) / NE, ce = Math.cos(e), se = Math.sin(e);
            for (var j = 0; j < NA; j++) {
              var az = TAU * (j + 0.5) / NA;
              acc += sh.f3(ce * Math.cos(az), ce * Math.sin(az), se) * ce;
            }
          }
          var avg = acc * (Math.PI / NE) * (TAU / NA) / (4 * Math.PI), eff = antEff(g, b);
          if (Math.abs(avg / eff - 1) > 0.01) { errs.push(b + " " + g + " dBi " + pl + ": среднее " + avg.toFixed(3) + " ≠ КПД " + eff.toFixed(3)); }
        });
      });
    });
    sel.value = keep[0]; lastAnt = keep[0]; applyAntLimits(Number(keep[1])); update();
    return { ok: !errs.length, errors: errs };
  }
  function bindPattern() {
    var cv = document.getElementById("cv-pattern");
    if (!cv) { return; }
    cv.style.touchAction = "none";
    cv.addEventListener("pointerdown", function (e) {
      if (PAT.view !== "3d") { return; }
      PAT.drag = { x: e.clientX, y: e.clientY, yaw: PAT.yaw, pitch: PAT.pitch };
      cv.setPointerCapture(e.pointerId);
    });
    cv.addEventListener("pointermove", function (e) {
      if (!PAT.drag) { return; }
      PAT.yaw = PAT.drag.yaw + (e.clientX - PAT.drag.x) * 0.01;
      PAT.pitch = Math.max(-1.2, Math.min(1.2, PAT.drag.pitch + (e.clientY - PAT.drag.y) * 0.01));
      if (DEMOS.pattern) { DEMOS.pattern.dirty = true; }
    });
    var end = function () { PAT.drag = null; };
    cv.addEventListener("pointerup", end);
    cv.addEventListener("pointercancel", end);
    cv.addEventListener("dblclick", function () { PAT.yaw = -0.6; PAT.pitch = 0.38; });
    if (document.getElementById("patView")) { segBind("patView", function (v) { PAT.view = v; if (DEMOS.pattern) { DEMOS.pattern.dirty = true; } }); }
    var pp = document.getElementById("patPol");
    if (pp) {
      pp.addEventListener("click", function (e) {
        var b = e.target.closest("button");
        if (!b) { return; }
        document.getElementById("txPol").value = b.getAttribute("data-v");
        update();
      });
    }
    var pt = document.getElementById("patType");
    if (pt) {
      pt.addEventListener("click", function (e) {
        var b = e.target.closest("button");
        if (!b) { return; }
        var sel = document.getElementById("txAnt");
        sel.value = b.getAttribute("data-v");
        update();
      });
    }
  }

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
    text(ctx, "−" + num(R.fspl, 1) + " dB", xr - 8, cy - 22, C.air, "right", "600 13px 'Segoe UI', sans-serif");
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
    text(ctx, "dBm", 4, 12, C.dim, "left", "11px 'Segoe UI', sans-serif");
    text(ctx, "−6 dB на каждое удвоение расстояния, −20 dB на каждые ×10", L0 + 6, B0 - 6, C.dim, "left", "11px 'Segoe UI', sans-serif");
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
    set("rbwFloor", fmtDbm(floor, 0) + " (КШ 4,5 dB)");
    set("rbwShift", FS.N === 512 ? "0 dB (база N = 512)" : fmtDb(floor - ref) + " к N = 512");
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
    text(ctx, "спектр (строка БПФ, dB)", sx0 + 4, sy0 + 13, C.fg, "left", "11px 'Segoe UI', sans-serif");
    text(ctx, "IQ → окно Ханна → БПФ → |X|² в dB", sx1 - 4, sy0 + 13, C.sdr, "right", "600 11px 'Segoe UI', sans-serif");
    text(ctx, sel.name, (selX0 + selX1) / 2, sy0 + 28, C.accent, "center", "600 11px 'Segoe UI', sans-serif");
    // «пол» шума: медиана сглаженного спектра — смещается на 3 dB при каждом удвоении N
    var srt = Array.prototype.slice.call(FS.avg).sort(function (a, b) { return a - b; }), med = srt[srt.length >> 1];
    hline(ctx, sx0, sx1, SY(med), C.warn, [5, 4]);
    text(ctx, "пол шума " + num(med, 0) + " dB (RBW " + num(2400 / N * 1.5, 1) + " кГц)", sx0 + 4, SY(med) - 4, C.warn,
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
    [10, 0, -10, -20, -30].forEach(function (g) { hline(ctx, L0, R0, Y(g), alpha(C.grid, 0.9)); text(ctx, g + " dB", L0 - 4, Y(g) + 4, C.dim, "right", "11px 'Segoe UI', sans-serif"); });
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
    document.getElementById("o-avdSnr").textContent = num(s, 1) + " dB";
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
    for (var s = 0; s <= 35; s += 5) { text(ctx, s + " dB", X(s), h - 8, C.dim, "center", "11px 'Segoe UI', sans-serif"); }
    plot(ctx, L0, R0, 200, function (x) { return Y(qAnalog(s0 + (x - L0) / (R0 - L0) * (s1 - s0))); }, C.tx, 2.5);
    plot(ctx, L0, R0, 300, function (x) { return Y(qDigital(s0 + (x - L0) / (R0 - L0) * (s1 - s0))); }, C.accent, 2.5);
    var thr = 0;
    for (var q = -5; q < 35; q += 0.05) { if (qDigital(q) >= 0.5) { thr = q; break; } }
    line(ctx, [[X(thr), T0], [X(thr), B0]], C.accent, 1, [3, 3]);
    text(ctx, "порог цифры ≈ " + num(thr, 1) + " dB", X(thr) + 4, T0 + 26, C.accent, "left", "11px 'Segoe UI', sans-serif");
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

  /* ------------------------------------------------------------------ сокращения: ссылки и подсказки */
  // Первое упоминание термина в каждом блоке становится ссылкой с подсказкой. Термин не размечается в блоке,
  // где он сам раскрывается (href ведёт на этот блок). Работает и для перерисованных частей (MutationObserver).
  var TERMS = window.PIPELINE_TERMS || [], TERM_BY = {}, TERM_RE = null, termObs = null, tipEl = null, tipFor = null;
  var SKIP_SEL = "a, button, select, option, label, input, textarea, output, script, style, svg, canvas, summary, code, " +
    ".formula, .mono, td.n, .fact .v, .kpi .v, .lv, .term, #termTip, #cryptRows, [data-noterm]";
  function escRe(s) { return s.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&"); }
  function initTerms() {
    var words = [];
    TERMS.forEach(function (t) { t.w.forEach(function (w) { TERM_BY[w] = t; words.push(w); }); });
    if (!words.length) { return; }
    words.sort(function (a, b) { return b.length - a.length; });  // сначала длинные: AMBE+2 раньше AMBE
    try {
      TERM_RE = new RegExp("(?<![\\p{L}\\p{N}\\-])(" + words.map(escRe).join("|") + ")(?![\\p{L}\\p{N}])", "u");
    } catch (e) { TERM_RE = null; return; }  // старый браузер без lookbehind — без подсказок
    tipEl = document.createElement("div");
    tipEl.id = "termTip";
    tipEl.setAttribute("role", "tooltip");
    tipEl.hidden = true;
    document.body.appendChild(tipEl);
    markTerms(document.querySelector("main"));
    termObs = new MutationObserver(function (recs) {
      var roots = [];
      recs.forEach(function (r) {
        var n = r.target.nodeType === 1 ? r.target : r.target.parentNode;
        if (n && !n.closest("#termTip") && roots.indexOf(n) < 0) { roots.push(n); }
      });
      roots.forEach(function (n) { markTerms(n); });
      termObs.takeRecords();
    });
    termObs.observe(document.querySelector("main"), { childList: true, subtree: true, characterData: true });
    var main = document.querySelector("main");
    main.addEventListener("mouseover", function (e) { var t = e.target.closest && e.target.closest(".term"); if (t) { showTip(t); } });
    main.addEventListener("mouseout", function (e) {
      var t = e.target.closest && e.target.closest(".term");
      if (t && !(e.relatedTarget && (t.contains(e.relatedTarget) || tipEl.contains(e.relatedTarget)))) { hideTip(); }
    });
    main.addEventListener("focusin", function (e) { var t = e.target.closest && e.target.closest(".term"); if (t) { showTip(t); } });
    main.addEventListener("focusout", function (e) { if (e.target.closest && e.target.closest(".term")) { hideTip(); } });
    tipEl.addEventListener("mouseleave", hideTip);
    var lastPointer = "mouse";
    document.addEventListener("pointerdown", function (e) { lastPointer = e.pointerType || "mouse"; }, true);
    main.addEventListener("click", function (e) {  // на телефоне первое касание показывает подсказку, второе — переход
      var t = e.target.closest && e.target.closest(".term");
      if (!t) { if (!(e.target.closest && e.target.closest("#termTip"))) { hideTip(); } return; }
      if (lastPointer !== "mouse" && tipFor !== t) { e.preventDefault(); showTip(t); }
    });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") { hideTip(); } });
  }
  function scopeOf(node) {
    return node.closest("details.blk, .card, .stage, #ctl, .chain, header, footer, .demo") || document.querySelector("main");
  }
  function markTerms(root) {
    if (!TERM_RE || !root || root.nodeType !== 1) { return; }
    if (root.closest && root.closest(SKIP_SEL)) { return; }
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null), nodes = [], n;
    while ((n = walker.nextNode())) {
      if (n.nodeValue.length > 1 && TERM_RE.test(n.nodeValue) && !n.parentNode.closest(SKIP_SEL)) { nodes.push(n); }
    }
    nodes.forEach(function (tn) {
      var scope = scopeOf(tn.parentNode);
      var home0 = tn.parentNode.closest("details.blk, #rssiBox") || scope, blockId = home0.id || "";
      var seen = {};
      $$(".term", home0).forEach(function (el) { seen[el.getAttribute("data-term")] = true; });
      var node = tn;
      for (var guard = 0; guard < 20 && node; guard++) {
        var m = TERM_RE.exec(node.nodeValue);
        if (!m) { break; }
        var t = TERM_BY[m[1]], after = null;
        // не размечать там, где термин раскрывается: блок с этим id или блок, внутри которого цель ссылки
        var home = t && t.href && (t.href === "#" + blockId || !!home0.querySelector(t.href) ||
          (t.href === "#rssi" && blockId === "rssiBox"));
        var skip = !t || seen[t.id] || home;
        if (skip) {
          // пропустить это вхождение, искать дальше в остатке
          var rest = node.splitText(m.index + m[1].length);
          node = rest;
          continue;
        }
        var mid = node.splitText(m.index);
        after = mid.splitText(m[1].length);
        var a = document.createElement("a");
        a.className = "term";
        a.setAttribute("data-term", t.id);
        a.href = t.href || ("../glossary.html#" + t.gl);
        a.textContent = mid.nodeValue;
        mid.parentNode.replaceChild(a, mid);
        seen[t.id] = true;
        node = after;
      }
    });
  }
  function showTip(el) {
    var t = TERMS.filter(function (x) { return x.id === el.getAttribute("data-term"); })[0];
    if (!t || !tipEl) { return; }
    tipFor = el;
    var esc = function (s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;"); };
    tipEl.innerHTML = "<b>" + esc(el.textContent) + "</b> — " + esc(t.full) + "<div class=\"tt\">" + esc(t.tip) + "</div>" +
      "<div class=\"tl\">" + (t.href ? "<a href=\"" + t.href + "\">подробнее на этой странице</a> · " : "") +
      "<a href=\"../glossary.html#" + t.gl + "\">в словаре</a></div>";
    tipEl.hidden = false;
    var r = el.getBoundingClientRect(), tw = Math.min(320, window.innerWidth - 24);
    tipEl.style.maxWidth = tw + "px";
    var left = Math.max(12, Math.min(window.innerWidth - tw - 12, r.left + window.scrollX - 10));
    tipEl.style.left = left + "px";
    var top = r.bottom + window.scrollY + 6;
    tipEl.style.top = top + "px";
    var tr = tipEl.getBoundingClientRect();
    if (tr.bottom > window.innerHeight - 8) { tipEl.style.top = (r.top + window.scrollY - tr.height - 6) + "px"; }
  }
  function hideTip() { if (tipEl) { tipEl.hidden = true; } tipFor = null; }

  // звук оцифровки: оригинал или после дискретизации и квантования (только по щелчку, громкость ограничена)
  function adcPlay(kind) {
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) { return; }
    if (!actx) { actx = new AC(); }
    if (actx.state === "suspended") { actx.resume(); }
    var s = adcState(), sr = actx.sampleRate, n = Math.round(sr * 1.5), buf = actx.createBuffer(1, n, sr), x = buf.getChannelData(0);
    var lin2 = (document.getElementById("adcRecon") || {}).value === "lin";
    for (var i = 0; i < n; i++) {
      var t = i / sr, v;
      if (kind === "o") { v = adcSig(t); } else {
        var u = t * s.fs, k = Math.floor(u), q0 = quant(adcSig(k / s.fs), s.L).v;
        v = lin2 ? q0 + (quant(adcSig((k + 1) / s.fs), s.L).v - q0) * (u - k) : q0;
      }
      var env = Math.min(1, t * 20, (1.5 - t) * 20);
      x[i] = Math.max(-1, Math.min(1, v)) * 0.16 * env;
    }
    var src = actx.createBufferSource();
    src.buffer = buf;
    src.connect(actx.destination);
    src.start();
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
    document.getElementById("adcPlayO").addEventListener("click", function () { adcPlay("o"); });
    document.getElementById("adcPlayD").addEventListener("click", function () { adcPlay("d"); });
    document.getElementById("modDepth").addEventListener("input", modUpdate);
    ["modVol", "modFa"].forEach(function (id) { document.getElementById(id).addEventListener("input", modUpdate); });
    document.getElementById("modPlayM").addEventListener("click", function () { modPlay("m"); });
    document.getElementById("modPlayC").addEventListener("click", function () { modPlay("c"); });
    document.getElementById("modPlayD").addEventListener("click", function () { modPlay("d"); });
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
    bindPattern();
    initTerms();
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
    window.SDR4NOOB_PIPELINE = { calc: calc, state: S, demos: DEMOS, cableLoss: cableLoss, antShape: antShape, antEff: antEff, antTypes: ANT_TYPES, selfTestAnt: selfTestAnt };  // для проверки
  }
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", init); } else { init(); }
})();
