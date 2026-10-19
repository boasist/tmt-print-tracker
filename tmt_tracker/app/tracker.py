"""Print tracking: which plate each printer is printing, counting finished plates' parts, manual corrections, and how
far along each build is. State is one JSON file in the add-on's /data.

Parts are counted into the shared pool ("counts", which fills the planned TMTs in order) or, when the printer is set to
print for a project (a request, a chair not yet for a child, a wheels order), into that project's own counts."""
import datetime as dt
import json
import logging
import os
import re
from pathlib import Path

from png import alpha_mask, iou

log = logging.getLogger("tmt.tracker")

ACTIVE = {"prepare", "running", "pause", "slicing", "init"}
NOT_TMT = "-"         # printer_project value: what this printer prints now isn't for a TMT
IMAGE_MIN = 0.80      # outline overlap needed to accept an image match
IMAGE_MARGIN = 0.03   # and how far it must beat the runner-up


def job_name(task):
    """A job name without the slicer's '_<filament>_<print time>' ending, as in Orca-style output file names:
    '3DTMT F SEAT BACK CENTER TOP REV A_PETG_9h44m' -> '3dtmt f seat back center top rev a'."""
    n = re.sub(r"(_[^_]*)?_(\d+d)?(\d+h)?(\d+m)?(\d+s)?$", lambda m: "" if any(m.groups()[1:]) else m.group(0),
               (task or "").strip())
    return n.strip().lower()


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Tracker:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "state.json"
        self.thumbs = Path(data_dir) / "thumbs"
        self.catalog = {"parts": [], "alternatives": [], "sources": []}
        self._masks = {}
        if self.path.exists():
            self.st = json.loads(self.path.read_text())
        else:
            self.st = {"target": 1, "assembled": 0, "counts": {}, "printers": {}, "aliases": {}, "log": [], "next_log": 1}
        self.st.setdefault("projects", [])
        self.st.setdefault("printer_project", {})     # printer -> project id it prints for
        self.st.setdefault("next_project", 1)

    # ------------------------------------------------------------ plumbing
    def set_catalog(self, catalog):
        self.catalog = catalog
        self._masks = {}

    def save(self):
        self.st["updated"] = now()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.st, indent=1))
        os.replace(tmp, self.path)

    @property
    def sources(self):
        return {s["id"]: s for s in self.catalog["sources"]}

    def _log(self, **entry):
        entry = {"id": self.st["next_log"], "time": now(), **entry}
        self.st["next_log"] += 1
        self.st["log"] = (self.st["log"] + [entry])[-500:]
        return entry

    def _project(self, pid):
        return next((p for p in self.st["projects"] if p["id"] == pid), None) if pid else None

    def _bucket(self, project=None):
        """The counts a print goes into: a project's, or the shared pool's."""
        p = self._project(project)
        return p["counts"] if p else self.st["counts"]

    def _add(self, src, sign=1, project=None):
        counts = self._bucket(project)
        for pid, n in src["yields"].items():
            counts[pid] = counts.get(pid, 0) + sign * n

    def _mask(self, thumb):
        if thumb not in self._masks:
            self._masks[thumb] = alpha_mask(self.thumbs / thumb)
        return self._masks[thumb]

    # ------------------------------------------------------------ progress
    def summary(self):
        """Complete builds in the printed pool, and per planned build the pieces covered."""
        counts, cat = self.st["counts"], self.catalog
        req = [p for p in cat["parts"] if not p["optional"] and not p.get("alt")]

        def covered(k):
            got = need = 0
            for p in req:
                need += p["qty"]
                got += max(0, min(p["qty"], counts.get(p["id"], 0) - (k - 1) * p["qty"]))
            for alt in cat.get("alternatives", []):
                need += alt["qty"]
                got += alt["qty"] * max(max(0, min(1, min((counts.get(pid, 0) - (k - 1) * q) / q for pid, q in o["parts"].items())))
                                        for o in alt["options"])
            return got, need

        complete = 0
        while req or cat.get("alternatives"):
            got, need = covered(complete + 1)
            if got < need:
                break
            complete += 1
        slots = []
        for k in range(1, max(self.st["target"], complete + 1) + 1):
            got, need = covered(k)
            slots.append({"build": self.st["assembled"] + k, "pieces": round(got, 2), "of": need, "complete": need > 0 and got >= need})
        return {"complete_in_pool": complete, "assembled": self.st["assembled"], "slots": slots}

    # ------------------------------------------------------------ matching
    def match(self, task, plate, cover=None):
        srcs = self.catalog["sources"]
        alias = self.st["aliases"].get(f"{task}|{plate}")
        if alias and alias in self.sources:
            return alias, {"by": "remembered"}
        titled = [s for s in srcs if s["title"].strip().lower() == (task or "").strip().lower()]
        hit = [s for s in titled if s["plate"] == plate]
        if len(hit) == 1:
            return hit[0]["id"], {"by": "title"}
        # The job named after the plate or the one part on it (Klipper/Moonraker printers send no plate number).
        name = job_name(task)
        named = [s for s in srcs if name and not titled and
                 (s.get("plate_name", "").strip().lower() == name or name in (n.lower() for n in s.get("names", [])))]
        if named and len({json.dumps(s["yields"], sort_keys=True) for s in named}) == 1:
            # One plate, or the same parts on several (e.g. a Bambu Studio file and its U1 copy): any one counts the same.
            return sorted(named, key=lambda s: (not s["counts"], s["file"], s["plate"]))[0]["id"], {"by": "name"}
        if not cover or not Path(cover).exists():
            return None, {"by": "none"}
        try:
            mask = alpha_mask(cover)
        except Exception as e:
            return None, {"by": "none", "error": f"cover image: {e}"}
        if len(mask) < 8:
            return None, {"by": "none", "error": "cover image is empty"}
        scored = sorted(((iou(mask, self._mask(s["thumb"])), s["id"]) for s in (titled or named or srcs)
                         if s.get("thumb") and (self.thumbs / s["thumb"]).exists()), reverse=True)
        if not scored:
            return None, {"by": "none"}
        best, second = scored[0], (scored[1] if len(scored) > 1 else (0.0, None))
        info = {"by": "image", "score": round(best[0], 3), "runner_up": round(second[0], 3)}
        if best[0] >= IMAGE_MIN and best[0] - second[0] >= IMAGE_MARGIN:
            return best[1], info
        return None, dict(info, by="none", reason="no clear image match")

    # ------------------------------------------------------------ commands
    def update(self, printer, status, task, gcode, cover=None, cover_at=None):
        """A printer's status, job or plate preview changed."""
        status = (status or "unknown").lower()
        task = task or ""
        m = re.search(r"plate_(\d+)", gcode or "")
        plate = int(m.group(1)) if m else None
        cur = self.st["printers"].get(printer) or {}
        if status in ACTIVE and (not cur.get("job") or cur.get("done") or cur.get("task") != task or cur.get("plate") != plate):
            prev = {k: cur.get(k) for k in ("task", "plate", "source", "cover_used")}
            cur = {"job": f"{printer}-{now()}", "task": task, "plate": plate, "source": None, "match": None,
                   "started": now(), "done": False, "prev": prev, "cover_used": prev.get("cover_used")}
            log.info("%s: new job %r plate %s", printer, task, plate)
        cur["status"] = status
        result = {"printer": printer, "status": status}
        not_tmt = self.st["printer_project"].get(printer) == NOT_TMT
        if not_tmt and cur.get("job") and not cur.get("done"):
            cur["source"], cur["match"] = None, {"by": "none", "reason": "not for a TMT"}
        elif cur.get("job") and status in ACTIVE and not cur.get("source"):
            # Title or remembered alias first; then the same plate as this printer's previous job; then the preview,
            # but only one that changed since the last image match (a new job can still show the old plate).
            sid, info = self.match(task, plate)
            prev = cur.get("prev") or {}
            if not sid and prev.get("source") and prev.get("task") == task and prev.get("plate") == plate:
                sid, info = prev["source"], {"by": "same as previous job"}
            if not sid and cover_at and cover_at not in ("unknown", "unavailable") and cover_at != cur.get("cover_used"):
                sid, info = self.match(task, plate, cover)
                if sid and sid == prev.get("source") and plate != prev.get("plate"):
                    # The printer updates the preview's timestamp before the new picture arrives: still the old plate.
                    sid, info = None, {"by": "none", "reason": "preview still shows the previous plate"}
                if sid:
                    cur["cover_used"] = cover_at
            elif not sid:
                info = {"by": "none", "reason": "waiting for this job's preview image"}
            cur["source"], cur["match"] = sid, info
            if sid:
                log.info("%s: printing %s (%s)", printer, sid, info)
        elif cur.get("job") and status in ACTIVE and (cur.get("match") or {}).get("by") == "image":
            # A newer preview during an image-matched job: if it clearly shows a different plate, follow it.
            if cover_at and cover_at not in ("unknown", "unavailable") and cover_at != cur.get("cover_used"):
                sid, info = self.match(task, plate, cover)
                cur["cover_used"] = cover_at
                if sid and sid != cur["source"]:
                    log.info("%s: newer preview shows %s, not %s", printer, sid, cur["source"])
                    info["rematched_from"] = cur["source"]
                    cur["source"], cur["match"] = sid, info
        if cur.get("job") and status in ("finish", "failed") and not cur.get("done"):
            cur["done"] = True
            src = self.sources.get(cur.get("source"))
            if not_tmt:
                e = self._log(printer=printer, task=task, plate=plate, source=None, result="not tmt")
            elif status == "failed":
                e = self._log(printer=printer, task=task, plate=plate, source=cur.get("source"), result="failed")
            elif src is None:
                e = self._log(printer=printer, task=task, plate=plate, source=None, result="unmatched")
            elif src["counts"]:
                project = self.st["printer_project"].get(printer) if self._project(self.st["printer_project"].get(printer)) else None
                self._add(src, project=project)
                e = self._log(printer=printer, task=task, plate=plate, source=src["id"], result="counted", yields=src["yields"],
                              project=project)
            else:
                e = self._log(printer=printer, task=task, plate=plate, source=src["id"], result="other")
            log.info("%s: %s %s", printer, e["result"], e.get("source"))
            result["logged"] = e
        self.st["printers"][printer] = cur
        result.update(source=cur.get("source"), match=cur.get("match"))
        self.save()
        return result

    def assign(self, source, log_id=None, printer=None):
        srcs = self.sources
        src = srcs[source]
        if log_id:
            e = next(x for x in self.st["log"] if x["id"] == int(log_id))
            if e["result"] == "counted" and e.get("source") in srcs:
                self._add(srcs[e["source"]], -1, e.get("project"))
            e["source"] = src["id"]
            if src["counts"]:
                if e["result"] != "counted":
                    e["project"] = self.st["printer_project"].get(e.get("printer"))
                if not self._project(e.get("project")):
                    e["project"] = None
                self._add(src, project=e.get("project"))
                e["result"], e["yields"] = "counted", src["yields"]
            else:
                e["result"], e["yields"] = "other", {}
            task, plate = e.get("task"), e.get("plate")
        else:
            cur = self.st["printers"][printer]
            cur["source"], cur["match"] = src["id"], {"by": "manual"}
            task, plate = cur.get("task"), cur.get("plate")
        if task:
            self.st["aliases"][f"{task}|{plate}"] = src["id"]
        self.save()
        return {"assigned": src["id"]}

    def adjust(self, part, delta, project=None):
        counts = self._bucket(project)
        counts[part] = max(0, counts.get(part, 0) + int(delta))
        self._log(result="adjusted", part=part, delta=int(delta), project=project if self._project(project) else None)
        self.save()
        return {"part": part, "count": counts[part]}

    # ------------------------------------------------------------ projects
    def build_needs(self):
        """What one TMT needs: {part id: qty} (one option of each alternative: the first)."""
        needs = {p["id"]: p["qty"] for p in self.catalog["parts"] if not p["optional"] and not p.get("alt")}
        for alt in self.catalog.get("alternatives", []):
            if alt["options"]:
                for pid, q in alt["options"][0]["parts"].items():
                    needs[pid] = q * alt.get("qty", 1)
        return needs

    def project_needs(self, p):
        if p["kind"] == "order":
            return {pid: int(q) for pid, q in (p.get("needs") or {}).items() if int(q) > 0}
        needs = self.build_needs()
        # Either option of an alternative counts: use whichever this project has more of.
        c = p["counts"]
        for alt in self.catalog.get("alternatives", []):
            if len(alt["options"]) > 1:
                best = max(alt["options"], key=lambda o: min(c.get(pid, 0) / q for pid, q in o["parts"].items()))
                for o in alt["options"]:
                    for pid in o["parts"]:
                        needs.pop(pid, None)
                for pid, q in best["parts"].items():
                    needs[pid] = q * alt.get("qty", 1)
        return needs

    def project_summaries(self):
        out = []
        for p in self.st["projects"]:
            needs, c = self.project_needs(p), p["counts"]
            got = sum(min(q, c.get(pid, 0)) for pid, q in needs.items())
            missing = {pid: q - c.get(pid, 0) for pid, q in needs.items() if c.get(pid, 0) < q}
            out.append({"id": p["id"], "pieces": got, "of": sum(needs.values()), "missing": missing,
                        "complete": bool(needs) and not missing})
        return out

    def project_add(self, name, kind="build", needs=None):
        name = (name or "").strip()
        if not name:
            raise ValueError("give the project a name")
        p = {"id": f"p{self.st['next_project']}", "name": name, "kind": "order" if kind == "order" else "build",
             "needs": {k: int(v) for k, v in (needs or {}).items() if int(v) > 0}, "counts": {}, "created": now(),
             "done": False}
        self.st["next_project"] += 1
        self.st["projects"].append(p)
        self.save()
        return {"project": p["id"]}

    def project_update(self, project, name=None, needs=None, done=None):
        p = self._project(project)
        if not p:
            raise ValueError("no such project")
        if name is not None and name.strip():
            p["name"] = name.strip()
        if needs is not None:
            p["needs"] = {k: int(v) for k, v in needs.items() if int(v) > 0}
        if done is not None:
            p["done"], p["done_at"] = bool(done), (now() if done else None)
            if done:
                for pr, pid in list(self.st["printer_project"].items()):
                    if pid == project:
                        self.st["printer_project"][pr] = None
            self._log(result="project done" if done else "project reopened", project=project)
        self.save()
        return {"project": project}

    def project_delete(self, project):
        """Remove a project; its printed parts go back into the pool."""
        p = self._project(project)
        if not p:
            raise ValueError("no such project")
        for pid, n in p["counts"].items():
            self.st["counts"][pid] = self.st["counts"].get(pid, 0) + n
        self.st["projects"].remove(p)
        for e in self.st["log"]:
            if e.get("project") == project:
                e["project"] = None
        for pr, pid in list(self.st["printer_project"].items()):
            if pid == project:
                self.st["printer_project"][pr] = None
        self.save()
        return {"deleted": project}

    def printer_project(self, printer, project=None):
        if project and project != NOT_TMT and not self._project(project):
            raise ValueError("no such project")
        self.st["printer_project"][printer] = project or None
        self.save()
        return {"printer": printer, "project": project or None}

    def move_print(self, log_id, project=None):
        """Count a finished print toward another project (or the pool)."""
        e = next(x for x in self.st["log"] if x["id"] == int(log_id))
        if project == NOT_TMT:
            if e["result"] == "counted" and e.get("source") in self.sources:
                self._add(self.sources[e["source"]], -1, e.get("project"))
            e["result"], e["project"] = "not tmt", None
            self.save()
            return {"moved": e["id"], "project": None}
        project = project if self._project(project) else None
        if e["result"] == "counted" and e.get("source") in self.sources and e.get("project") != project:
            src = self.sources[e["source"]]
            self._add(src, -1, e.get("project"))
            self._add(src, 1, project)
        e["project"] = project
        self.save()
        return {"moved": e["id"], "project": project}

    def fill_from_pool(self, project):
        """Move parts the project still needs from the pool into it."""
        p = self._project(project)
        if not p:
            raise ValueError("no such project")
        moved = 0
        for pid, q in self.project_needs(p).items():
            n = min(q - p["counts"].get(pid, 0), self.st["counts"].get(pid, 0))
            if n > 0:
                self.st["counts"][pid] -= n
                p["counts"][pid] = p["counts"].get(pid, 0) + n
                moved += n
        if moved:
            self._log(result="moved from spares", project=project, pieces=moved)
        self.save()
        return {"moved": moved}

    def undo(self, log_id):
        e = next(x for x in self.st["log"] if x["id"] == int(log_id))
        if e["result"] == "counted" and e.get("source") in self.sources:
            self._add(self.sources[e["source"]], -1, e.get("project"))
            e["result"] = "undone"
        self.save()
        return {"undone": e["id"]}

    def import_counts(self, counts, replace=False):
        """Bring in part counts from elsewhere (another tracker, a spreadsheet): {part id: count}."""
        known = {p["id"] for p in self.catalog["parts"]}
        unknown = sorted(set(counts) - known)
        if replace:
            self.st["counts"] = {}
        for pid, n in counts.items():
            if pid in known:
                self.st["counts"][pid] = (0 if replace else self.st["counts"].get(pid, 0)) + int(n)
        self._log(result="imported", parts=len(set(counts) & known), pieces=sum(int(n) for pid, n in counts.items() if pid in known))
        self.save()
        return {"imported": len(set(counts) & known), "unknown": unknown}

    def target(self, count):
        self.st["target"] = max(1, int(count))
        self.save()
        return {"target": self.st["target"]}

    def assembled(self):
        if self.summary()["complete_in_pool"] < 1:
            raise ValueError("no complete build in the printed parts yet")
        counts = self.st["counts"]
        for p in self.catalog["parts"]:
            if not p["optional"] and not p.get("alt"):
                counts[p["id"]] = counts.get(p["id"], 0) - p["qty"]
        for alt in self.catalog.get("alternatives", []):
            for o in alt["options"]:
                if all(counts.get(pid, 0) >= q for pid, q in o["parts"].items()):
                    for pid, q in o["parts"].items():
                        counts[pid] -= q
                    break
        self.st["assembled"] += 1
        self.st["target"] = max(1, self.st["target"] - 1)
        self._log(result="assembled", build=self.st["assembled"])
        self.save()
        return {"assembled": self.st["assembled"]}
