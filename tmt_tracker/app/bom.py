"""A build's bill of materials (the PDF the 3d-mobility.org build page makes, "Bill of Materials" by MakeGood): which
color each printed part is to be, so a project's plates can be planned by color.

Only the color is used, not the brand: "Numakers — Atomic Pink" is the color "Atomic Pink". The filament type comes
from the BOM's filament summary (PETG HF, TPU, ...).
"""
import re
import subprocess

DASH = "—"   # "Brand — Color"


def pdf_text(path):
    """The PDF's text with its layout (columns stay in place), from poppler's pdftotext."""
    r = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, timeout=60, text=True)
    if r.returncode:
        raise ValueError("couldn't read that file as a PDF")
    return r.stdout


def _cells(line):
    """[(column, text)] of a line, splitting on runs of 2+ spaces."""
    return [(m.start(), m.group()) for m in re.finditer(r"\S+(?: \S+)*", line)]


def parse(text):
    """{name, request, date, items: [{name, grams, color, brand, material}], filaments: {brand — color: material}}"""
    lines = text.splitlines()
    out = {"name": None, "request": None, "date": None, "items": [], "filaments": {}}
    for ln in lines[:12]:
        cells = _cells(ln)
        if cells and cells[0][1].upper() == "MAKEGOOD" and len(cells) > 1 and not out["name"]:
            out["name"] = cells[-1][1]
        if (m := re.search(r"#\s*(\d+)", ln)) and not out["request"]:
            out["request"] = m.group(1)
        if (m := re.search(r"[A-Z][a-z]+ \d{1,2}, \d{4}", ln)) and not out["date"]:
            out["date"] = m.group()
    in_parts = False
    for i, ln in enumerate(lines):
        u = ln.strip().upper()
        if u == "PRINTED PARTS":
            in_parts = True
            continue
        if u.startswith("STANDARD HARDWARE"):
            in_parts = False
        if u.startswith("FILAMENT SUMMARY"):
            in_parts = False
        if not in_parts or DASH in ln:
            continue
        # A row of part names (with weights), then a row of "Brand — Color" under them.
        nxt = next((lines[j] for j in range(i + 1, min(i + 3, len(lines))) if lines[j].strip()), "")
        if DASH not in nxt:
            continue
        names = [(c, t) for c, t in _cells(ln) if not re.fullmatch(r"\d+(\.\d+)?\s*g", t)]
        grams = {c: t for c, t in _cells(ln) if re.fullmatch(r"\d+(\.\d+)?\s*g", t)}
        fils = [(c, t) for c, t in _cells(nxt) if DASH in t]
        for col, name in names:
            c, fil = min(fils, key=lambda f: abs(f[0] - col))
            brand, color = (s.strip() for s in fil.split(DASH, 1))
            g = next((v for gc, v in sorted(grams.items()) if gc > col and not any(col < nc < gc for nc, _ in names)), None)
            out["items"].append({"name": name.strip(), "grams": int(float(g.rstrip("g "))) if g else None,
                                 "brand": brand, "color": color})
    for ln in lines:
        m = re.match(r"\s*(\S.*?\S)\s{2,}(\S.*?)\s+·\s+[\d.]+\s*g", ln)
        if m and DASH in m.group(1):
            out["filaments"][re.sub(r"\s+", " ", m.group(1))] = m.group(2).strip()
    for it in out["items"]:
        it["material"] = out["filaments"].get(f"{it['brand']} {DASH} {it['color']}")
    return out


def _norm(s):
    s = re.sub(r"&", " and ", (s or "").lower())
    words = re.findall(r"[a-z0-9]+", s)
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words)


def _bare(name):
    """A part's display name without its code: 'C1 Wing Left' -> 'wing left'."""
    return _norm(re.sub(r"^[A-Z]{1,2}\d{0,2}\s+(?=[A-Za-z])", "", name or ""))


def match(items, catalog):
    """{part id: {color, material, item}} for the catalog's parts, and the BOM items that matched no part.
    By part name (code left out, plurals and '&' ignored), then a part group's name (CONNECTORS, BUCKLES AND
    SLIDES), then a part whose name contains the item's name; numbered items (WHEEL RIM 1 to 4) are one part."""
    parts = catalog.get("parts") or []
    colors, unmatched = {}, []
    names = {p["id"]: _bare(p["name"]) for p in parts}
    for exact in (True, False):
        for it in items:
            if it.get("_done"):
                continue
            want = _norm(re.sub(r"\s+\d+$", "", it["name"]))
            if exact:
                hit = [p["id"] for p in parts if names[p["id"]] == want] or \
                      [p["id"] for p in parts if _norm(p.get("group")) == want]
            else:
                hit = [p["id"] for p in parts if p["id"] not in colors
                       and re.search(rf"(^| ){re.escape(want)}( |$)", names[p["id"]])]
            for pid in hit:
                colors.setdefault(pid, {"color": it["color"], "material": it.get("material"), "item": it["name"]})
            if hit:
                it["_done"] = True
    for it in items:
        if not it.pop("_done", False):
            unmatched.append(it["name"])
    return colors, unmatched
