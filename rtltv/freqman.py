"""Менеджер частот режима «Радио»: записи {name, freq, mode, bw, group}, импорт и экспорт.

CSV (разделитель «;» — открывается в Excel; при импорте «,», «;» и табуляция определяются сами,
столбцы ищутся по заголовку по-русски или по-английски) и XML менеджера частот SDR# (Frequencies.xml).
"""
import csv
import io
import os
import xml.etree.ElementTree as ET

from .radio import MODES

HEADER = ["Название", "Частота, Гц", "Режим", "Полоса, Гц", "Группа"]
DEFAULT_GROUP = "Мои"
_COLS = {
    "name": ("название", "name", "имя", "станция"),
    "freq": ("частота, гц", "частота", "frequency", "freq", "freq_hz", "frequency_hz", "hz"),
    "mode": ("режим", "mode", "modulation", "detectortype", "демодулятор"),
    "bw": ("полоса, гц", "полоса", "bandwidth", "bw", "filterbandwidth", "bw_hz"),
    "group": ("группа", "group", "groupname", "категория", "category"),
}
SDRSHARP_MODES = {"WFM": "WFM", "NFM": "NFM", "AM": "AM", "DSB": "AM", "USB": "USB", "LSB": "LSB",
                  "CW": "CW", "RAW": "NFM"}


def normalize(e):
    """Запись с проверенными полями (частота — Гц, режим из MODES, полоса в пределах режима)."""
    mode = str(e.get("mode") or "NFM").upper()
    if mode not in MODES:
        mode = SDRSHARP_MODES.get(mode, "NFM")
    try:
        freq = float(e.get("freq") or 0)
    except (TypeError, ValueError):
        freq = 0.0
    lo, hi = MODES[mode][1]
    try:
        bw = float(e.get("bw") or 0)
    except (TypeError, ValueError):
        bw = 0.0
    bw = min(max(bw, lo), hi) if bw > 0 else float(MODES[mode][0])
    name = str(e.get("name") or "").strip() or f"{freq / 1e6:.4f} МГц"
    group = str(e.get("group") or "").strip() or DEFAULT_GROUP
    return {"name": name, "freq": freq, "mode": mode, "bw": bw, "group": group}


def _parse_freq(text, guess_mhz=True):
    t = str(text).strip().replace(" ", "").replace(" ", "")
    low = t.lower()
    mul = 1.0
    for suf, m in (("ghz", 1e9), ("mhz", 1e6), ("мгц", 1e6), ("khz", 1e3), ("кгц", 1e3), ("hz", 1.0), ("гц", 1.0)):
        if low.endswith(suf):
            low, mul = low[: -len(suf)], m
            break
    v = float(low.replace(",", "."))
    if guess_mhz and mul == 1.0 and v < 10000:     # похоже на МГц без единиц: 101.2
        mul = 1e6
    return v * mul


def export_csv(path, entries):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(HEADER)
        for e in entries:
            e = normalize(e)
            w.writerow([e["name"], int(round(e["freq"])), e["mode"], int(round(e["bw"])), e["group"]])


def import_csv(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";" if text.count(";") > text.count(",") else ","
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        return []
    head = [c.strip().lower() for c in rows[0]]
    idx = {}
    for key, names in _COLS.items():
        for i, h in enumerate(head):
            if h in names:
                idx[key] = i
                break
    if "freq" in idx:
        body = rows[1:]
    else:
        idx = {"name": 0, "freq": 1, "mode": 2, "bw": 3, "group": 4}
        body = rows
    out = []
    for r in body:
        def col(k):
            i = idx.get(k)
            return r[i].strip() if i is not None and i < len(r) else ""
        try:
            freq = _parse_freq(col("freq"))
        except ValueError:
            continue
        try:
            bw = _parse_freq(col("bw"), guess_mhz=False) if col("bw") else 0
        except ValueError:
            bw = 0
        out.append(normalize({"name": col("name"), "freq": freq, "mode": col("mode"), "bw": bw,
                              "group": col("group")}))
    return out


def import_sdrsharp_xml(path):
    root = ET.parse(path).getroot()
    out = []
    for m in root.iter("MemoryEntry"):
        def tx(tag):
            el = m.find(tag)
            return el.text if el is not None and el.text else ""
        try:
            freq = float(tx("Frequency"))
        except ValueError:
            continue
        try:
            bw = float(tx("FilterBandwidth") or 0)
        except ValueError:
            bw = 0
        out.append(normalize({"name": tx("Name"), "freq": freq, "mode": tx("DetectorType"), "bw": bw,
                              "group": tx("GroupName")}))
    return out


def export_sdrsharp_xml(path, entries):
    root = ET.Element("ArrayOfMemoryEntry")
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")
    root.set("xmlns:xsd", "http://www.w3.org/2001/XMLSchema")
    for e in entries:
        e = normalize(e)
        m = ET.SubElement(root, "MemoryEntry")
        for tag, val in (("IsFavourite", "true"), ("Name", e["name"]), ("GroupName", e["group"]),
                         ("Frequency", str(int(round(e["freq"])))), ("DetectorType", e["mode"]),
                         ("Shift", "0"), ("FilterBandwidth", str(int(round(e["bw"]))))):
            ET.SubElement(m, tag).text = val
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def import_any(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xml":
        return import_sdrsharp_xml(path)
    return import_csv(path)


def export_any(path, entries):
    if os.path.splitext(path)[1].lower() == ".xml":
        export_sdrsharp_xml(path, entries)
    else:
        export_csv(path, entries)


def merge(existing, new):
    """Добавить записи без повторов (та же частота с точностью 1 Гц, тот же режим и группа)."""
    seen = {(round(e["freq"]), e["mode"], e["group"]) for e in existing}
    added = 0
    for e in new:
        k = (round(e["freq"]), e["mode"], e["group"])
        if k not in seen:
            existing.append(e)
            seen.add(k)
            added += 1
    return added
