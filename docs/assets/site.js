(function () {
  "use strict";
  var root = document.body.getAttribute("data-root") || "";
  var btn = document.querySelector(".menu-btn");
  if (btn) {
    btn.addEventListener("click", function () {
      var open = document.body.classList.toggle("nav-open");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("click", function (e) {
      if (document.body.classList.contains("nav-open") && !e.target.closest(".sidebar") && !e.target.closest(".menu-btn")) {
        document.body.classList.remove("nav-open");
        btn.setAttribute("aria-expanded", "false");
      }
    });
  }

  function norm(s) { return s.toLowerCase().replace(/ё/g, "е"); }
  function esc(s) { return s.replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; }); }
  function words(q) { return norm(q).split(/\s+/).filter(function (w) { return w.length > 0; }); }

  function search(q) {
    var ws = words(q), out = [], idx = window.SDR4NOOB_SEARCH || [];
    if (!ws.length) return out;
    idx.forEach(function (p) {
      var low = p.n || (p.n = norm(p.x)), tl = norm(p.t), score = 0;
      for (var i = 0; i < ws.length; i++) {
        var inText = low.indexOf(ws[i]) >= 0, inTitle = tl.indexOf(ws[i]) >= 0;
        if (!inText && !inTitle) return;
        score += (inTitle ? 20 : 0) + low.split(ws[i]).length - 1;
      }
      var at = low.indexOf(ws[0]), a = Math.max(0, at - 50), b = Math.min(p.x.length, at + ws[0].length + 110);
      var snip = at < 0 ? p.x.slice(0, 150) : (a ? "…" : "") + p.x.slice(a, b) + (b < p.x.length ? "…" : "");
      out.push({p: p, s: score, snip: snip});
    });
    out.sort(function (x, y) { return y.s - x.s; });
    return out.slice(0, 15);
  }

  function highlight(text, ws) {
    var h = esc(text);
    ws.forEach(function (w) {
      if (w.length < 2) return;
      var re = new RegExp("(" + w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/е/g, "[её]") + ")", "gi");
      h = h.replace(re, "<mark>$1</mark>");
    });
    return h;
  }

  var input = document.getElementById("q"), box = document.getElementById("results"), sel = -1;
  function render() {
    var q = input.value.trim();
    if (!q) { box.hidden = true; box.innerHTML = ""; return; }
    var res = search(q), ws = words(q);
    sel = res.length ? 0 : -1;
    box.innerHTML = res.length ? res.map(function (r, i) {
      return '<a role="option" href="' + root + r.p.u + "?q=" + encodeURIComponent(q) + '"' + (i === 0 ? ' class="sel"' : "") + ">" +
        "<b>" + esc(r.p.t) + "</b><small>" + highlight(r.snip, ws) + "</small></a>";
    }).join("") : '<div class="none">Ничего не найдено</div>';
    box.hidden = false;
  }
  function move(d) {
    var items = box.querySelectorAll("a");
    if (!items.length) return;
    if (sel >= 0) items[sel].classList.remove("sel");
    sel = (sel + d + items.length) % items.length;
    items[sel].classList.add("sel");
    items[sel].scrollIntoView({block: "nearest"});
  }
  if (input) {
    var t = null;
    input.addEventListener("input", function () { clearTimeout(t); t = setTimeout(render, 120); });
    input.addEventListener("focus", function () { if (input.value.trim()) render(); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") {
        var items = box.querySelectorAll("a");
        if (items.length) { e.preventDefault(); location.href = items[Math.max(0, sel)].getAttribute("href"); }
      } else if (e.key === "Escape") { box.hidden = true; input.blur(); }
    });
    document.addEventListener("click", function (e) { if (!e.target.closest(".search")) box.hidden = true; });
    document.addEventListener("keydown", function (e) {
      var tag = (document.activeElement && document.activeElement.tagName) || "";
      if (e.key === "/" && tag !== "INPUT" && tag !== "TEXTAREA") { e.preventDefault(); input.focus(); input.select(); }
    });
  }

  // переход из поиска: подсветить найденные слова на странице и прокрутить к первому
  var m = /[?&]q=([^&#]*)/.exec(location.search);
  if (m) {
    var q = decodeURIComponent(m[1].replace(/\+/g, " ")), ws = words(q).filter(function (w) { return w.length > 1; });
    if (input) input.value = q;
    var main = document.getElementById("content"), first = null;
    if (main && ws.length) {
      var re = new RegExp("(" + ws.map(function (w) { return w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/е/g, "[её]"); }).join("|") + ")", "gi");
      var walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT, null), nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      nodes.forEach(function (n) {
        if (!n.nodeValue.trim() || n.parentNode.closest("script,style,mark,.anchor")) return;
        re.lastIndex = 0;
        if (!re.test(n.nodeValue)) return;
        var span = document.createElement("span");
        span.innerHTML = esc(n.nodeValue).replace(re, "<mark>$1</mark>");
        n.parentNode.replaceChild(span, n);
        if (!first) first = span.querySelector("mark");
      });
      if (first && !location.hash) first.scrollIntoView({block: "center"});
    }
  }
})();
