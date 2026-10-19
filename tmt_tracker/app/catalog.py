"""Build the parts catalog from a build profile and the user's Bambu Studio 3MF files.

A Bambu Studio 3MF holds the project Title (3D/3dmodel.model), the objects and plates (Metadata/model_settings.config)
and a preview per plate (Metadata/plate_N.png). The profile says which files (by Title) and plates count toward one
build, and how many of each part a build needs.
"""
import hashlib
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


def read_3mf(path):
    """Title, plates ({index: {name, objects: Counter}}) and plate previews ({index: png bytes}) of a 3MF."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        model = ET.fromstring(z.read("3D/3dmodel.model"))
        ns = {"m": model.tag.split("}")[0].strip("{")}
        title = next((m.text or "" for m in model.findall("m:metadata", ns) if m.get("name") == "Title"), "").strip()
        plates, thumbs = {}, {}
        if "Metadata/model_settings.config" in names:
            cfg = ET.fromstring(z.read("Metadata/model_settings.config"))
            objects = {o.get("id"): _meta(o).get("name") for o in cfg.findall("object")}
            for p in cfg.findall("plate"):
                m = _meta(p)
                idx = int(m.get("plater_id") or 0)
                objs = Counter(objects.get(_meta(mi).get("object_id")) for mi in p.findall("model_instance"))
                objs.pop(None, None)
                plates[idx] = {"name": m.get("plater_name") or "", "objects": objs}
                png = f"Metadata/plate_{idx}.png"
                if png in names:
                    thumbs[idx] = z.read(png)
    return {"title": title or Path(path).stem, "plates": plates, "thumbs": thumbs}


class PartNamer:
    def __init__(self, profile):
        self.rules = [(re.compile(r["match"], re.I), r) for r in profile.get("part_rules", [])]
        self.strip = re.compile(profile.get("strip_prefixes") or r"^$", re.I)

    def __call__(self, raw):
        """(id, display name, letter code) for an object name like '3DTMT A1 HANDLE LEFT REV A.STEP'."""
        n = re.sub(r"\.(step|stl|3mf|obj)(_\d+)?$", "", raw.strip(), flags=re.I)
        for rx, r in self.rules:
            if rx.search(n):
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


def build(profile, files_dir, thumbs_dir):
    """The catalog: parts per build, alternatives, every plate of every 3MF (sources), and which profile files are
    present or missing."""
    files_dir, thumbs_dir = Path(files_dir), Path(thumbs_dir)
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    for old in thumbs_dir.glob("*.png"):
        old.unlink()
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
            f = read_3mf(path)
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
        digest = hashlib.sha1(path.read_bytes()).hexdigest()[:10]
        files.append({"file": path.name, "title": f["title"], "profile_file": key, "plates": len(f["plates"])})
        for idx, pl in sorted(f["plates"].items()):
            rule = counting.get((key, idx)) if key else None
            yields = Counter()
            for raw, n in pl["objects"].items():
                pid, name, code = namer(raw)
                yields[pid] += n
                if rule:
                    p = parts.setdefault(pid, {"id": pid, "code": code, "name": name, "group": rule["group"], "qty": 0,
                                               "optional": bool(rule.get("optional")),
                                               "material": rule.get("material", default_material)})
                    p["qty"] = max(p["qty"], n)
            thumb = None
            if idx in f["thumbs"]:
                thumb = f"{digest}_plate_{idx}.png"
                (thumbs_dir / thumb).write_bytes(f["thumbs"][idx])
            sources.append({"id": f"{digest}#{idx}", "file": path.name, "title": f["title"], "plate": idx,
                            "plate_name": pl["name"], "counts": rule is not None, "group": rule["group"] if rule else None,
                            "yields": dict(yields), "thumb": thumb})
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
