"""Build the parts catalog from the 3MF files in this app's folder, the build profile and your build settings.

A Bambu Studio 3MF holds the project Title and details (3D/3dmodel.model), the objects and plates
(Metadata/model_settings.config), the print settings (Metadata/project_settings.config) and a preview per plate
(Metadata/plate_N.png).

The build profile (YAML) describes a known build: which design files (matched by the Title inside the 3MF) and plates
count, how object names become parts, and quantities. A file whose Title matches a profile file is set up from the
profile, and follows profile updates, until you edit it on the 3MF files page.

build.json, next to the files, holds only what you set on that page:
  groups         [{name, optional, plan}] in parts-table order (until you save them: the profile's). optional: not
                 needed for a complete build; plan: an optional group the queue and planner still plan.
  group_renames  {profile group: your name}, for the files that still follow the profile
  files          {file name: {label, plates: {"N": {group, material, printers?, printer_models?, name?}}, sets:
                 [{plates, yields}], objects: {object name: part id}, profile?}}: the files you set up or edited. Only
                 plates with a group count. sets: plates that only make their parts together (each printed once).
  rules          [{match, id, name?}] object names (regex, any case) that are always a given part
  parts          {part id: {name?, qty?}} your part names, and how many a build needs
  profile_skip   [profile file key] the profile's design files you don't use (no "still needed" notice)
"""
import datetime as dt
import hashlib
import html
import json
import logging
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import yaml

log = logging.getLogger("tmt.catalog")
CACHE_VERSION = 2   # 2: the files page's details (0.4.0); 0.3.5's entries had the title, plates and volumes only


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


def _plain(text, limit=400):
    """The 3MF's description is HTML, escaped (often twice): as plain text, shortened."""
    for _ in range(3):
        text = html.unescape(text)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def read_3mf(path, volumes=True):
    """Everything the catalog and the files page need from one 3MF: title and details, print settings, filaments, and
    plates {"N": {name, objects {object name: count}, volume_cm3, filaments, thumb}}. Reading the volumes (every mesh)
    is the slow part; volumes=False leaves them out."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        model = ET.fromstring(z.read("3D/3dmodel.model"))
        ns = {"m": model.tag.split("}")[0].strip("{")}
        md = {m.get("name"): (m.text or "").strip() for m in model.findall("m:metadata", ns)}
        vols = _object_volumes(z, model) if volumes else {}
        try:
            ps = json.loads(z.read("Metadata/project_settings.config"))
        except (KeyError, ValueError):
            ps = {}
        cfg = ET.fromstring(z.read("Metadata/model_settings.config")) if "Metadata/model_settings.config" in names else None
    ftypes = ps.get("filament_type") or []
    plates = {}
    if cfg is not None:
        objects = {}
        for o in cfg.findall("object"):
            m = _meta(o)
            exts = {int(e) for e in [m.get("extruder")] + [_meta(p).get("extruder") for p in o.findall("part")] if e and e.isdigit()}
            objects[o.get("id")] = (m.get("name"), exts or {1})
        for p in cfg.findall("plate"):
            m = _meta(p)
            idx = int(m.get("plater_id") or 0)
            ids = [_meta(mi).get("object_id") for mi in p.findall("model_instance")]
            plates[str(idx)] = {
                "name": m.get("plater_name") or "",
                "objects": dict(Counter(objects[i][0] for i in ids if i in objects and objects[i][0])),
                "volume_cm3": round(sum(vols.get(i, 0) for i in ids), 1) if volumes else None,
                "filaments": sorted({ftypes[e - 1] for i in ids if i in objects for e in objects[i][1] if 0 < e <= len(ftypes)}),
                "thumb": f"Metadata/plate_{idx}.png" in names}
    first = lambda v: v[0] if isinstance(v, list) and v else v
    return {"title": md.get("Title") or Path(path).stem, "designer": md.get("Designer", ""), "app": md.get("Application", ""),
            "description": _plain(md.get("Description", "")),
            "printer": ps.get("printer_settings_id") or ps.get("printer_model") or "",
            "nozzle": first(ps.get("nozzle_diameter")), "layer_mm": ps.get("layer_height"),
            "infill": ps.get("sparse_infill_density"), "walls": ps.get("wall_loops"), "bed": ps.get("curr_bed_type"),
            "filaments": [{"type": t, "colour": (ps.get("filament_colour") or [None] * len(ftypes))[i],
                           "profile": (ps.get("filament_settings_id") or [None] * len(ftypes))[i]} for i, t in enumerate(ftypes)],
            "sliced": any(n.endswith(".gcode") for n in names), "plates": plates}


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _write_json(path, data, indent=None):
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=indent) + ("\n" if indent else ""))
    os.replace(tmp, path)


def scan(files_dir, thumbs_dir, cache_path=None):
    """Every 3MF in the folder: {file name: what read_3mf says, plus its digest (the first 10 hex digits of its sha1,
    which plate ids are made of)}, through the cache: a file is read again only when it changed (size and time, or
    else its contents; a copied file gets a new time). Writes each plate's preview as <digest>_plate_<n>.png."""
    cache = {}
    if cache_path and Path(cache_path).exists():
        try:
            cache = json.loads(Path(cache_path).read_text())
        except ValueError:
            log.warning("ignoring an unreadable %s", cache_path)
    infos, out = {}, {}
    for path in sorted(files_dir.glob("*.3mf")):
        if path.name.startswith("."):   # a Mac's "._" metadata twin, or an upload still in pieces
            continue
        st, c = path.stat(), cache.get(path.name) or {}
        same = c.get("size") == st.st_size and c.get("mtime") == st.st_mtime_ns
        digest = c["digest"] if same and c.get("digest") else _sha1(path)[:10]
        try:
            if c.get("digest") == digest and c.get("v") == CACHE_VERSION:
                info = c["info"]
            elif c.get("digest") == digest and isinstance(c.get("plates"), dict):   # read by 0.3.5: add the details
                info = read_3mf(path, volumes=False)
                for i, pl in info["plates"].items():
                    pl["volume_cm3"] = (c["plates"].get(i) or {}).get("volume_cm3")
            else:
                info = read_3mf(path)
        except Exception as e:
            log.warning("can't read %s: %s", path.name, e)
            info = {"error": f"can't read it: {e}"}
        out[path.name] = {"v": CACHE_VERSION, "size": st.st_size, "mtime": st.st_mtime_ns, "digest": digest, "info": info}
        infos[path.name] = {**info, "digest": digest}
        want = [i for i, pl in (info.get("plates") or {}).items()
                if pl.get("thumb") and not (thumbs_dir / f"{digest}_plate_{i}.png").exists()]
        if want:
            with zipfile.ZipFile(path) as z:
                for i in want:
                    (thumbs_dir / f"{digest}_plate_{i}.png").write_bytes(z.read(f"Metadata/plate_{i}.png"))
    if cache_path and out != cache:
        _write_json(cache_path, out)
    return infos


class PartNamer:
    def __init__(self, profile):
        self.rules = [(re.compile(r["match"], re.I), r) for r in profile.get("part_rules", [])]
        self.strip = re.compile(profile.get("strip_prefixes") or r"^$", re.I)

    def __call__(self, raw, file_key=None):
        """(id, display name, letter code, how) for an object name like '3DTMT A1 HANDLE LEFT REV A.STEP': the
        profile's part rules ("profile"), else a cleaned-up name ("auto"). A rule with a `file` only applies to that
        profile file (for generic names like 'Part Studio 2 - Part 4')."""
        n = re.sub(r"\.(step|stl|3mf|obj)(_\d+)?$", "", raw.strip(), flags=re.I)
        for rx, r in self.rules:
            if rx.search(n) and r.get("file") in (None, file_key):
                return r["id"], r.get("name") or r["id"], r.get("code", ""), "profile"
        return (*self.auto(raw), "auto")

    def auto(self, raw):
        n = re.sub(r"\.(step|stl|3mf|obj)(_\d+)?$", "", raw.strip(), flags=re.I)
        n = self.strip.sub("", n)
        n = re.sub(r"\s+REV [A-Z]\b.*$", "", n, flags=re.I).strip()
        m = re.match(r"^([A-Z]{1,2}\d?)\s+(.+)$", n)
        code, label = (m.group(1), m.group(2)) if m and m.group(1) != "XX" else ("", re.sub(r"^XX\s+", "", n))
        label = re.sub(r"(\d)\s*Mm\b", r"\1 mm", label.title())
        pid = re.sub(r"[^a-z0-9]+", "-", f"{code} {label}".lower()).strip("-") or "part"
        return pid, label, code


def load_profile(path):
    profile = yaml.safe_load(Path(path).read_text())
    for key in ("files", "plates"):
        if key not in profile:
            raise ValueError(f"profile {path} has no '{key}'")
    return profile


# ---------------------------------------------------------------- your settings (build.json)
def load_cfg(path):
    cfg = json.loads(Path(path).read_text()) if path and Path(path).exists() else {}
    for k, v in (("files", {}), ("rules", []), ("parts", {}), ("profile_skip", []), ("group_renames", {})):
        cfg.setdefault(k, v)
    return cfg


def save_cfg(path, cfg):
    cfg["updated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    _write_json(path, cfg, indent=1)


def profile_groups(profile, cfg):
    """The profile's groups in its order (renamed as you renamed them), optional/plan as its plate rules say."""
    ren, order = cfg["group_renames"], list(profile.get("group_order") or [])
    for r in profile["plates"]:
        if r["group"] not in order:
            order.append(r["group"])
    return [{"name": ren.get(g, g), "optional": any(r.get("optional") for r in profile["plates"] if r["group"] == g),
             "plan": any(r.get("optional") and r.get("plan") for r in profile["plates"] if r["group"] == g)} for g in order]


def profile_file(profile, cfg, key):
    """A profile design file as the files page's settings: its counting plates and the plates printed together."""
    ren, dm = cfg["group_renames"], profile.get("default_material", "")
    fc = {"label": "", "plates": {}, "sets": [], "objects": {}, "profile": key}
    for r in profile["plates"]:
        if r["file"] != key:
            continue
        for i in r["plates"]:
            fc["plates"][str(i)] = {"group": ren.get(r["group"], r["group"]), "material": r.get("material", dm),
                                    **({"printer_models": list(r["printer_models"])} if r.get("printer_models") else {})}
        if r.get("together"):
            fc["sets"].append({"plates": list(r["plates"]), "yields": {k: int(n) for k, n in r["together"].items()}})
    return fc


def setups(profile, cfg, infos):
    """Each file's settings: {file: (settings or None, 'you' | 'profile' | None, profile key)} — yours (build.json),
    the profile's for a file whose Title it knows, or none yet (a new file). Also {profile key: file} found."""
    wanted = {k: (f.get("title") or "").strip().lower() for k, f in profile["files"].items()}
    found, out = {}, {}
    for name, info in infos.items():
        key = None
        if "error" not in info:
            key = next((k for k, t in wanted.items() if t and t == info["title"].lower()), None)
            if key and key in found:
                log.warning("%s and %s both have the title %r; using the first", found[key], name, info["title"])
                key = None
            if key:
                found[key] = name
        if name in cfg["files"]:
            out[name] = (cfg["files"][name], "you", key or cfg["files"][name].get("profile"))
        elif key:
            out[name] = (profile_file(profile, cfg, key), "profile", key)
        else:
            out[name] = (None, None, None)
    return out, found


def part_for(cfg, fc, key, namer, raw):
    """(part id, default name, code, how) for an object in a file: the file's own setting ("file"), else a rule of
    yours ("rule"), else the profile's rules ("profile") and automatic naming ("auto")."""
    pid = ((fc or {}).get("objects") or {}).get(raw)
    if pid:
        _, label, code = namer.auto(raw)
        return pid, label, code, "file"
    for r in cfg["rules"]:
        if re.search(r["match"], raw, re.I):
            return r["id"], r.get("name") or r["id"], r.get("code", ""), "rule"
    return namer(raw, key)


# ---------------------------------------------------------------- the catalog
def build(profile, files_dir, thumbs_dir, cache_path=None, cfg_path=None):
    """The catalog: parts per build, alternatives, every plate of every 3MF (sources), the files page's rows, and the
    profile's design files still missing. Only new or changed files are read (cache_path)."""
    files_dir, thumbs_dir = Path(files_dir), Path(thumbs_dir)
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    cfg = load_cfg(cfg_path)
    infos = scan(files_dir, thumbs_dir, cache_path)
    namer = PartNamer(profile)
    setup, found = setups(profile, cfg, infos)
    groups = [dict(g) for g in cfg["groups"]] if "groups" in cfg else profile_groups(profile, cfg)
    pg = {g["name"]: g for g in profile_groups(profile, cfg)}
    for fc, _, _ in setup.values():   # a group a plate uses that the list doesn't have yet (a profile update)
        for pc in ((fc or {}).get("plates") or {}).values():
            if pc.get("group") and pc["group"] not in {g["name"] for g in groups}:
                groups.append(dict(pg.get(pc["group"]) or {"name": pc["group"], "optional": False, "plan": False}))
    gby = {g["name"]: g for g in groups}
    dm = profile.get("default_material", "")
    parts, sources, qty_seen = {}, [], {}
    for name, info in infos.items():
        if "error" in info:
            continue
        fc, _, key = setup[name]
        fc, digest = fc or {}, info["digest"]
        set_of = {int(i): s for s in fc.get("sets") or [] for i in s["plates"]}
        for idx in sorted(info["plates"], key=int):
            pl, i = info["plates"][idx], int(idx)
            pc = (fc.get("plates") or {}).get(idx) or {}
            group = pc.get("group") if pc.get("group") in gby else None
            g = gby.get(group) or {}
            material = pc.get("material") if "material" in pc else (pl["filaments"][0] if pl["filaments"] else dm)
            in_set = set_of.get(i)

            def add_part(pid, pname, code, n):
                parts.setdefault(pid, {"id": pid, "code": code, "name": pname, "group": group, "qty": 0,
                                       "optional": bool(g.get("optional")), "plan": bool(g.get("optional") and g.get("plan")),
                                       "material": material})
                qty_seen[pid] = max(qty_seen.get(pid, 0), n)

            yields = Counter()
            if in_set:   # each plate of a set carries the set's parts; they count once the whole set has printed
                names = {}
                for raw in pl["objects"]:
                    pid, pname, _, _ = part_for(cfg, fc, key, namer, raw)
                    names.setdefault(pid, pname)
                for pid, n in in_set["yields"].items():
                    yields[pid] = int(n)
                    if group:
                        add_part(pid, names.get(pid) or pid.replace("-", " ").capitalize(), "", int(n))
            else:
                for raw, n in pl["objects"].items():
                    pid, pname, code, _ = part_for(cfg, fc, key, namer, raw)
                    yields[pid] += n
                    if group:
                        add_part(pid, pname, code, n)
            sources.append({"id": f"{digest}#{i}", "file": name, "title": info["title"], "plate": i,
                            "plate_name": pc.get("name") or pl["name"], "counts": group is not None, "group": group,
                            "yields": dict(yields), "thumb": f"{digest}_plate_{i}.png" if pl.get("thumb") else None,
                            "volume_cm3": pl.get("volume_cm3"),
                            **({"label": fc["label"]} if fc.get("label") else {}),
                            **({"printers": list(pc["printers"])} if pc.get("printers") and group else {}),
                            **({"printer_models": list(pc["printer_models"])} if pc.get("printer_models") and group else {}),
                            **({"set": {"id": f"{digest}#{'+'.join(map(str, in_set['plates']))}",
                                        "plates": [f"{digest}#{j}" for j in in_set["plates"]]}} if in_set else {})})
    keep = {s["thumb"] for s in sources if s["thumb"]}
    for old in thumbs_dir.glob("*.png"):
        if old.name not in keep and not old.name.startswith("custom-"):   # custom plates' pictures (tracker.assign_custom) stay
            old.unlink()
    quantities = profile.get("quantities") or {}
    for pid, p in parts.items():
        over = cfg["parts"].get(pid) or {}
        p["qty"] = int(over["qty"]) if over.get("qty") else int(quantities[pid]) if pid in quantities else qty_seen[pid]
        if over.get("name"):
            p["name"] = over["name"]
    alternatives = profile.get("alternatives") or []
    for alt in alternatives:
        alt.setdefault("qty", 1)
        for opt in alt["options"]:
            for pid, q in opt["parts"].items():
                if pid in parts:
                    parts[pid]["alt"] = alt["id"]
                    parts[pid]["qty"] = int(q)
    order = [g["name"] for g in groups]
    rank = lambda gr: order.index(gr) if gr in order else len(order)
    plist = sorted(parts.values(), key=lambda p: (rank(p["group"]), p["code"] or "~", p["name"]))
    skip = set(cfg["profile_skip"])
    missing = [{"key": k, "title": f.get("title")} for k, f in profile["files"].items() if k not in found and k not in skip]
    skipped = [{"key": k, "title": f.get("title")} for k, f in profile["files"].items() if k not in found and k in skip]
    rows = file_rows(cfg, setup, infos, namer, files_dir, {p["id"]: p["name"] for p in plist})
    removed = sorted(p.name for p in (files_dir / "removed").glob("*.3mf")) if (files_dir / "removed").is_dir() else []
    view = {"groups": groups, "saved_groups": "groups" in cfg, "parts": cfg["parts"], "rules": cfg["rules"],
            "files": {n: {**fc, "from": how} for n, (fc, how, _) in setup.items() if fc is not None}}
    req = [p for p in plist if not p["optional"] and not p.get("alt")]
    log.info("catalog: %d parts (%d pieces per build) from %d files; %d plates; missing: %s", len(plist),
             sum(p["qty"] for p in req) + sum(a["qty"] for a in alternatives), len(infos), len(sources),
             ", ".join(m["title"] for m in missing) or "none")
    return {"profile": profile.get("name", ""), "parts": plist, "alternatives": alternatives, "sources": sources,
            "files": rows, "missing": missing, "skipped": skipped, "removed": removed,
            "gone": [n for n in cfg["files"] if n not in infos and n not in removed], "cfg": view}


def file_rows(cfg, setup, infos, namer, files_dir, names):
    """The files page's rows: each file's details and plates, with what each object counts as."""
    rows = []
    for name, info in infos.items():
        fc, how, key = setup[name]
        st = (files_dir / name).stat()
        row = {"file": name, "classified": how is not None, "from": how, "profile_file": key,
               "label": (fc or {}).get("label", ""), "size": st.st_size,
               "modified": dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).isoformat(timespec="seconds")}
        if "error" in info:
            rows.append({**row, "error": info["error"]})
            continue
        plates = []
        for idx in sorted(info["plates"], key=int):
            pl = info["plates"][idx]
            objs = []
            for raw, n in pl["objects"].items():
                pid, pname, code, by = part_for(cfg, fc, key, namer, raw)
                objs.append({"raw": raw, "n": n, "part": pid, "code": code, "how": by,
                             "name": names.get(pid) or (cfg["parts"].get(pid) or {}).get("name") or pname})
            plates.append({"plate": int(idx), "id": f"{info['digest']}#{idx}", "name3mf": pl["name"], "objects": objs,
                           "volume_cm3": pl.get("volume_cm3"), "filaments": pl["filaments"],
                           "thumb": f"{info['digest']}_plate_{idx}.png" if pl.get("thumb") else None})
        rows.append({**row, **{k: v for k, v in info.items() if k != "plates"}, "plates": plates})
    return rows


# ---------------------------------------------------------------- the files page's changes
def clean_id(pid):
    return re.sub(r"[^a-z0-9]+", "-", str(pid).lower()).strip("-")


def file_save(profile, cfg, cat, a, printer_keys):
    """Set up a file: which plates count toward a build (their group), each plate's material, printers and name, which
    part each object is, plates that only make an item together, and names for new parts. A new file stops being new;
    a profile file stops following the profile."""
    row = next((f for f in cat["files"] if f["file"] == a.get("file") and not f.get("error")), None)
    if not row:
        raise ValueError("that file isn't in the folder (refresh the page)")
    groups = {g["name"] for g in cat["cfg"]["groups"]}
    plates = {str(p["plate"]) for p in row["plates"]}
    fc = {"label": str(a.get("label") or "").strip()[:80], "plates": {}, "sets": [], "objects": {},
          **({"profile": row["profile_file"]} if row.get("profile_file") else {})}
    for idx, pc in (a.get("plates") or {}).items():
        if idx not in plates:
            raise ValueError(f"plate {idx} isn't in the file")
        out, g, name = {}, pc.get("group") or None, str(pc.get("name") or "").strip()[:80]
        if g:
            if g not in groups:
                raise ValueError(f"there's no group {g!r}")
            out = {"group": g, "material": str(pc.get("material") or "PETG").strip().upper()[:12] or "PETG"}
            if pc.get("printers"):
                out["printers"] = sorted({str(p) for p in pc["printers"]} & set(printer_keys))
            if pc.get("printer_models"):
                out["printer_models"] = [str(m).strip()[:20] for m in pc["printer_models"] if str(m).strip()][:8]
        if name:
            out["name"] = name
        if out:
            fc["plates"][idx] = out
    raws = {o["raw"] for p in row["plates"] for o in p["objects"]}
    for raw, pid in (a.get("objects") or {}).items():
        if raw in raws and clean_id(pid):
            fc["objects"][raw] = clean_id(pid)
    for s in a.get("sets") or []:
        sp = sorted({int(i) for i in s.get("plates") or []})
        yields = {clean_id(k): int(n) for k, n in (s.get("yields") or {}).items() if clean_id(k) and int(n) > 0}
        if len(sp) < 2 or not yields or not all(str(i) in plates for i in sp):
            raise ValueError("plates printed together need 2 or more plates of the file and the item they make")
        fc["sets"].append({"plates": sp, "yields": yields})
    cfg["files"][row["file"]] = fc
    namer, key = PartNamer(profile), row.get("profile_file")
    used = {pid for s in fc["sets"] for pid in s["yields"]}   # names only for parts this file's counting plates make
    set_plates = {i for s in fc["sets"] for i in s["plates"]}
    for p in row["plates"]:
        if (fc["plates"].get(str(p["plate"])) or {}).get("group") and p["plate"] not in set_plates:
            used |= {part_for(cfg, fc, key, namer, o["raw"])[0] for o in p["objects"]}
    for pid, p in (a.get("parts") or {}).items():
        if clean_id(pid) in used and str(p.get("name") or "").strip():
            cfg["parts"].setdefault(clean_id(pid), {})["name"] = str(p["name"]).strip()[:80]


def part_save(cfg, cat, a):
    """Rename a part, or set how many a build needs (qty null: the profile's number, else the most one plate makes)."""
    pid = a.get("id")
    if pid not in {p["id"] for p in cat["parts"]}:
        raise ValueError("that part isn't in the build")
    p = cfg["parts"].setdefault(pid, {})
    if "name" in a:
        if not str(a["name"] or "").strip():
            raise ValueError("a part needs a name")
        p["name"] = str(a["name"]).strip()[:80]
    if "qty" in a:
        if a["qty"] in (None, ""):
            p.pop("qty", None)
        else:
            try:
                q = float(a["qty"])
            except (TypeError, ValueError):
                q = None
            if q is None or q != int(q) or not 1 <= q <= 999:
                raise ValueError("quantity: a whole number from 1 to 999")
            p["qty"] = int(q)
    if not p:
        del cfg["parts"][pid]


def groups_save(profile, cfg, cat, a):
    """The groups in parts-table order, each required, optional, or optional but still planned. A renamed group
    (was -> name) follows on its plates (and on the profile's); a group can only go when no plate uses it. Returns the
    renames, for the planner's setups."""
    new, seen, rename = [], set(), {}
    for g in a.get("groups") or []:
        name = str(g.get("name") or "").strip()[:60]
        if not name or name.lower() in seen:
            raise ValueError("each group needs a name of its own")
        seen.add(name.lower())
        new.append({"name": name, "optional": bool(g.get("optional")), "plan": bool(g.get("optional") and g.get("plan"))})
        if g.get("was") and g["was"] != name:
            rename[g["was"]] = name
    for fc in cfg["files"].values():
        for pc in (fc.get("plates") or {}).values():
            if pc.get("group") in rename:
                pc["group"] = rename[pc["group"]]
    ren = cfg["group_renames"]
    for g in {r["group"] for r in profile["plates"]}:
        if ren.get(g, g) in rename:
            ren[g] = rename[ren.get(g, g)]
    names = {g["name"] for g in new}
    used = {pc["group"] for fc in cat["cfg"]["files"].values() if fc.get("from") == "profile"
            for pc in (fc.get("plates") or {}).values() if pc.get("group")}
    used = {rename.get(g, g) for g in used} | {pc["group"] for fc in cfg["files"].values()
                                               for pc in (fc.get("plates") or {}).values() if pc.get("group")}
    if used - names:
        raise ValueError("still used by plates: " + ", ".join(sorted(used - names)))
    cfg["groups"] = new
    return rename


def profile_skip(profile, cfg, a):
    """Say you don't use one of the profile's design files (or that you do again)."""
    key = a.get("key")
    if key not in profile["files"]:
        raise ValueError("the profile has no such file")
    skip = [k for k in cfg["profile_skip"] if k != key]
    cfg["profile_skip"] = skip + [key] if a.get("skip") else skip
