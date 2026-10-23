"""Build the parts catalog from a build profile and the user's Bambu Studio 3MF files.

A Bambu Studio 3MF holds the project Title (3D/3dmodel.model), the objects and plates (Metadata/model_settings.config)
and a preview per plate (Metadata/plate_N.png). The profile says which files (by Title) and plates count toward one
build, and how many of each part a build needs.
"""
import hashlib
import json
import logging
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import yaml

log = logging.getLogger("tmt.catalog")


def _meta(el):
    return {m.get("key"): m.get("value") for m in el.findall("metadata")}


def _mesh_volumes(z, path):
    """Volume (mm³) of each mesh object in one 3MF model file, from its triangles (sum of signed tetrahedra). Streamed:
    the big part files are 60+ MB of XML, which as a tree takes about 1 GB."""
    vols, verts, vol, oid = {}, [], 0.0, None
    for ev, el in ET.iterparse(z.open(path.lstrip("/")), events=("start", "end")):
        tag = el.tag.rsplit("}", 1)[-1]
        if ev == "start":
            if tag == "object":
                oid, verts, vol = el.get("id"), [], 0.0
        elif tag == "vertex":
            verts.append((float(el.get("x")), float(el.get("y")), float(el.get("z"))))
        elif tag == "triangle":
            a, b, c = verts[int(el.get("v1"))], verts[int(el.get("v2"))], verts[int(el.get("v3"))]
            vol += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
                    + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6
        elif tag == "object":
            vols[oid], verts = abs(vol), []
        elif tag in ("vertices", "triangles"):
            el.clear()
    return vols


def _object_volumes(z, model):
    """Build object id -> volume (cm³): its component meshes, each scaled by its transform's determinant. Used for a
    rough print-time guess before a printer has printed a plate."""
    ns = {"m": model.tag.split("}")[0].strip("{"), "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"}
    meshes, out = {}, {}
    for obj in model.findall("m:resources/m:object", ns):
        total = 0.0
        try:
            for comp in obj.findall("m:components/m:component", ns):
                path = comp.get(f"{{{ns['p']}}}path") or "/3D/3dmodel.model"
                if path not in meshes:
                    meshes[path] = _mesh_volumes(z, path)
                m = [float(x) for x in (comp.get("transform") or "1 0 0 0 1 0 0 0 1 0 0 0").split()]
                det = m[0] * (m[4] * m[8] - m[5] * m[7]) - m[1] * (m[3] * m[8] - m[5] * m[6]) + m[2] * (m[3] * m[7] - m[4] * m[6])
                total += meshes[path].get(comp.get("objectid"), 0.0) * abs(det)
        except (KeyError, ValueError, ET.ParseError) as e:
            log.debug("volume of object %s: %s", obj.get("id"), e)
        out[obj.get("id")] = total / 1000
    return out


def read_3mf(path):
    """Title, plates ({index: {name, objects: Counter, volume_cm3}}) and plate previews ({index: png bytes}) of a 3MF.
    Slow for big files (the volumes), so build() caches everything but the previews."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        model = ET.fromstring(z.read("3D/3dmodel.model"))
        ns = {"m": model.tag.split("}")[0].strip("{")}
        title = next((m.text or "" for m in model.findall("m:metadata", ns) if m.get("name") == "Title"), "").strip()
        vols = _object_volumes(z, model)
        plates, thumbs = {}, {}
        if "Metadata/model_settings.config" in names:
            cfg = ET.fromstring(z.read("Metadata/model_settings.config"))
            objects = {o.get("id"): _meta(o).get("name") for o in cfg.findall("object")}
            for p in cfg.findall("plate"):
                m = _meta(p)
                idx = int(m.get("plater_id") or 0)
                ids = [_meta(mi).get("object_id") for mi in p.findall("model_instance")]
                objs = Counter(objects.get(i) for i in ids)
                objs.pop(None, None)
                plates[idx] = {"name": m.get("plater_name") or "", "objects": objs,
                               "volume_cm3": round(sum(vols.get(i, 0) for i in ids), 1)}
                png = f"Metadata/plate_{idx}.png"
                if png in names:
                    thumbs[idx] = z.read(png)
    return {"title": title or Path(path).stem, "plates": plates, "thumbs": thumbs}


class PartNamer:
    def __init__(self, profile):
        self.rules = [(re.compile(r["match"], re.I), r) for r in profile.get("part_rules", [])]
        self.strip = re.compile(profile.get("strip_prefixes") or r"^$", re.I)

    def __call__(self, raw, file_key=None):
        """(id, display name, letter code) for an object name like '3DTMT A1 HANDLE LEFT REV A.STEP'. A rule with a
        `file` only applies to that profile file (for generic names like 'Part Studio 2 - Part 4')."""
        n = re.sub(r"\.(step|stl|3mf|obj)(_\d+)?$", "", raw.strip(), flags=re.I)
        for rx, r in self.rules:
            if rx.search(n) and r.get("file") in (None, file_key):
                return r["id"], r.get("name") or r["id"], r.get("code", "")
        n = self.strip.sub("", n)
        n = re.sub(r"\s+REV [A-Z]\b.*$", "", n, flags=re.I).strip()
        m = re.match(r"^([A-Z]{1,2}\d?)\s+(.+)$", n)
        code, label = (m.group(1), m.group(2)) if m and m.group(1) != "XX" else ("", re.sub(r"^XX\s+", "", n))
        label = re.sub(r"(\d)\s*Mm\b", r"\1 mm", label.title())
        pid = re.sub(r"[^a-z0-9]+", "-", f"{code} {label}".lower()).strip("-")
        return pid, label, code


def load_profile(path):
    profile = yaml.safe_load(Path(path).read_text())
    for key in ("files", "plates"):
        if key not in profile:
            raise ValueError(f"profile {path} has no '{key}'")
    return profile


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _read_cached(path, cache, thumbs_dir):
    """read_3mf through the cache (by size and modification time): {digest, title, plates, thumbs}. Writes the plate
    previews as <digest>_plate_<n>.png when they're missing."""
    st = path.stat()
    c = cache.get(path.name)
    if not (c and c.get("size") == st.st_size and c.get("mtime") == st.st_mtime_ns):
        f = read_3mf(path)
        c = {"size": st.st_size, "mtime": st.st_mtime_ns, "digest": _sha1(path)[:10], "title": f["title"],
             "plates": {str(i): {**pl, "objects": dict(pl["objects"])} for i, pl in f["plates"].items()},
             "thumbs": sorted(f["thumbs"])}
        for idx, png in f["thumbs"].items():
            (thumbs_dir / f"{c['digest']}_plate_{idx}.png").write_bytes(png)
        cache[path.name] = c
    elif any(not (thumbs_dir / f"{c['digest']}_plate_{i}.png").exists() for i in c["thumbs"]):
        with zipfile.ZipFile(path) as z:
            for i in c["thumbs"]:
                (thumbs_dir / f"{c['digest']}_plate_{i}.png").write_bytes(z.read(f"Metadata/plate_{i}.png"))
    return {"digest": c["digest"], "title": c["title"], "thumbs": set(c["thumbs"]),
            "plates": {int(i): {**pl, "objects": Counter(pl["objects"])} for i, pl in c["plates"].items()}}


def build(profile, files_dir, thumbs_dir, cache_path=None):
    """The catalog: parts per build, alternatives, every plate of every 3MF (sources), and which profile files are
    present or missing. cache_path keeps what was read from each file, so only new or changed files are read."""
    files_dir, thumbs_dir = Path(files_dir), Path(thumbs_dir)
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    cache = {}
    if cache_path and Path(cache_path).exists():
        try:
            cache = json.loads(Path(cache_path).read_text())
        except ValueError:
            log.warning("ignoring an unreadable %s", cache_path)
    namer = PartNamer(profile)
    wanted = {key: (f.get("title") or "").strip().lower() for key, f in profile["files"].items()}
    counting = {}   # (file key, plate) -> plate rule
    for rule in profile["plates"]:
        for idx in rule["plates"]:
            counting[(rule["file"], idx)] = rule
    default_material = profile.get("default_material", "")
    parts, sources, files, found = {}, [], [], {}
    for path in sorted(files_dir.glob("*.3mf")):
        try:
            f = _read_cached(path, cache, thumbs_dir)
        except Exception as e:
            log.warning("can't read %s: %s", path.name, e)
            files.append({"file": path.name, "error": str(e)})
            continue
        key = next((k for k, t in wanted.items() if t and t == f["title"].lower()), None)
        if key and key in found:
            log.warning("%s and %s both have the title %r; using the first", found[key], path.name, f["title"])
            key = None
        if key:
            found[key] = path.name
        digest = f["digest"]
        files.append({"file": path.name, "title": f["title"], "profile_file": key, "plates": len(f["plates"])})
        for idx, pl in sorted(f["plates"].items()):
            rule = counting.get((key, idx)) if key else None
            yields = Counter()
            together = (rule or {}).get("together")
            if together:   # plates that only make these parts together: each carries the set's parts
                names = {}
                for raw in pl["objects"]:
                    pid, name, code = namer(raw, key)
                    names.setdefault(pid, name)
                for pid, n in together.items():
                    yields[pid] = int(n)
                    p = parts.setdefault(pid, {"id": pid, "code": "", "name": names.get(pid) or pid.replace("-", " ").capitalize(),
                                               "group": rule["group"], "qty": 0, "optional": bool(rule.get("optional")),
                                               "plan": bool(rule.get("plan")), "material": rule.get("material", default_material)})
                    p["qty"] = max(p["qty"], int(n))
            for raw, n in ({} if together else pl["objects"]).items():
                pid, name, code = namer(raw, key)
                yields[pid] += n
                if rule:
                    p = parts.setdefault(pid, {"id": pid, "code": code, "name": name, "group": rule["group"], "qty": 0,
                                               "optional": bool(rule.get("optional")), "plan": bool(rule.get("plan")),
                                               "material": rule.get("material", default_material)})
                    p["qty"] = max(p["qty"], n)
            thumb = f"{digest}_plate_{idx}.png" if idx in f["thumbs"] else None
            sources.append({"id": f"{digest}#{idx}", "file": path.name, "title": f["title"], "plate": idx,
                            "plate_name": pl["name"], "counts": rule is not None, "group": rule["group"] if rule else None,
                            "yields": dict(yields), "thumb": thumb, "volume_cm3": pl.get("volume_cm3"),
                            **({"printer_models": rule["printer_models"]} if rule and rule.get("printer_models") else {}),
                            **({"set": {"id": f"{digest}#{'+'.join(map(str, rule['plates']))}",
                                        "plates": [f"{digest}#{i}" for i in rule["plates"]]}} if together else {})})
    keep = {s["thumb"] for s in sources if s["thumb"]}
    for old in thumbs_dir.glob("*.png"):
        if old.name not in keep and not old.name.startswith("custom-"):   # custom plates' pictures (tracker.assign_custom) stay
            old.unlink()
    if cache_path:
        present = {f["file"] for f in files}
        tmp = Path(cache_path).with_suffix(".tmp")
        tmp.write_text(json.dumps({k: v for k, v in cache.items() if k in present}))
        tmp.replace(cache_path)
    for pid, q in (profile.get("quantities") or {}).items():
        if pid in parts:
            parts[pid]["qty"] = int(q)
    alternatives = profile.get("alternatives") or []
    for alt in alternatives:
        alt.setdefault("qty", 1)
        for opt in alt["options"]:
            for pid, q in opt["parts"].items():
                if pid in parts:
                    parts[pid]["alt"] = alt["id"]
                    parts[pid]["qty"] = int(q)
    order = profile.get("group_order") or []
    rank = lambda g: order.index(g) if g in order else len(order)
    plist = sorted(parts.values(), key=lambda p: (rank(p["group"]), p["code"] or "~", p["name"]))
    missing = [{"key": k, "title": profile["files"][k].get("title")} for k in profile["files"] if k not in found]
    req = [p for p in plist if not p["optional"] and not p.get("alt")]
    log.info("catalog: %d parts (%d pieces per build) from %d files; %d plates; missing: %s", len(plist),
             sum(p["qty"] for p in req) + sum(a["qty"] for a in alternatives), len(files), len(sources),
             ", ".join(m["title"] for m in missing) or "none")
    return {"profile": profile.get("name", ""), "parts": plist, "alternatives": alternatives, "sources": sources,
            "files": files, "missing": missing}
