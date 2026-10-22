"""Print tracking: which plate each printer is printing, counting finished plates' parts, manual corrections, and how
far along each build is; plus each printer's print queue, print-time samples and the planner's data (planning.py).
State is one JSON file in the add-on's /data."""
import datetime as dt
import hashlib
import json
import logging
import os
import re
from pathlib import Path

import planning as pl
from png import alpha_mask, iou

log = logging.getLogger("tmt.tracker")

ACTIVE = {"prepare", "running", "pause", "slicing", "init"}
IMAGE_MIN = 0.80      # outline overlap needed to accept an image match
IMAGE_MARGIN = 0.03   # and how far it must beat the runner-up


def file_hash(path):
    """A short hash of a preview picture, to tell a new picture from the previous job's (a stale preview)."""
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()[:16] if path else None
    except OSError:
        return None


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

    # ------------------------------------------------------------ plumbing
    def set_catalog(self, catalog):
        """The catalog from the profile and files, plus your custom plates (plates you combined or changed)."""
        known = {p["id"] for p in catalog["parts"]}   # a custom plate whose parts left the catalog is ignored
        self.catalog = dict(catalog, sources=catalog["sources"] + [s for s in self.st.get("custom_sources", [])
                                                                   if set(s["yields"]) <= known])
        self._masks = {}
        pl.reconcile(self.st, self.catalog)

    def set_printers(self, info):
        """{printer key: {name, model}} from Home Assistant, for alerts, plate limits and default planner setups."""
        self.st["printer_info"] = info
        for pr in info:
            self.st["printers"].setdefault(pr, {})
        pl.migrate_aliases(self.st)

    def save(self):
        pl.queue_state(self.st); pl.times_state(self.st); pl.plan_cfg(self.st)
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

    def _add(self, src, sign=1):
        return pl.add_yields(self.st, src, sign)

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
    def match(self, task, plate, cover=None, printer=None):
        srcs = self.catalog["sources"]
        alias = self.st["aliases"].get(f"{printer}|{task}|{plate}") or self.st["aliases"].get(f"{task}|{plate}")
        if alias and alias in self.sources:
            return alias, {"by": "remembered"}
        titled = [s for s in srcs if s["title"].strip().lower() == (task or "").strip().lower()]
        hit = [s for s in titled if s["plate"] == plate]
        if len(hit) == 1:
            return hit[0]["id"], {"by": "title"}
        if not cover or not Path(cover).exists():
            return None, {"by": "none"}
        try:
            mask = alpha_mask(cover)
        except Exception as e:
            return None, {"by": "none", "error": f"cover image: {e}"}
        if len(mask) < 8:
            return None, {"by": "none", "error": "cover image is empty"}
        scored = sorted(((iou(mask, self._mask(s["thumb"])), s["id"]) for s in (titled or srcs)
                         if s.get("thumb") and (self.thumbs / s["thumb"]).exists()), reverse=True)
        if not scored:
            return None, {"by": "none"}
        best, second = scored[0], (scored[1] if len(scored) > 1 else (0.0, None))
        info = {"by": "image", "score": round(best[0], 3), "runner_up": round(second[0], 3)}
        if best[0] >= IMAGE_MIN and best[0] - second[0] >= IMAGE_MARGIN:
            return best[1], info
        return None, dict(info, by="none", reason="no clear image match")

    # ------------------------------------------------------------ commands
    def update(self, printer, status, task, gcode, cover=None, cover_at=None, live=None):
        """A printer's status, job or plate preview changed. live: start_time, materials (filament types loaded),
        nozzles ({side: {size, type, target}}), remaining, progress, stage, filament ({name, type, id})."""
        live = live or {}
        status = (status or "unknown").lower()
        task = task or ""
        m = re.search(r"plate_(\d+)", gcode or "")
        plate = int(m.group(1)) if m else None
        cur = self.st["printers"].get(printer) or {}
        if status in ACTIVE and (not cur.get("job") or cur.get("done") or cur.get("task") != task or cur.get("plate") != plate):
            prev = {k: cur.get(k) for k in ("task", "plate", "source", "cover_used", "cover_hash")}
            cur = {"job": f"{printer}-{now()}", "task": task, "plate": plate, "source": None, "match": None,
                   "started": now(), "done": False, "prev": prev, "cover_used": prev.get("cover_used"),
                   "materials": cur.get("materials")}
            self.st.setdefault("bed", {})[printer] = {"state": "printing", "since": now()}
            log.info("%s: new job %r plate %s", printer, task, plate)
        if live.get("materials") is not None:
            cur["materials"] = sorted({str(m).upper() for m in live["materials"] if m})
        if live.get("nozzles"):
            self.st.setdefault("live", {})[printer] = {"nozzles": live["nozzles"], "at": now()}
        if live.get("start_time"):
            cur["start_time"] = live["start_time"]
        cur["status"] = status
        result = {"printer": printer, "status": status}
        if cur.get("job") and status in ACTIVE and not cur.get("source"):
            # Title or remembered alias first; then the same plate as this printer's previous job; then the preview,
            # but only one that changed since the last image match (a new job can still show the old plate).
            sid, info = self.match(task, plate, printer=printer)
            prev = cur.get("prev") or {}
            if not sid and prev.get("source") and prev.get("task") == task and prev.get("plate") == plate:
                sid, info = prev["source"], {"by": "same as previous job"}
            if not sid and cover_at and cover_at not in ("unknown", "unavailable") and cover_at != cur.get("cover_used"):
                sid, info = self.match(task, plate, cover, printer)
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
        elif cur.get("job") and status in ACTIVE and (cur.get("match") or {}).get("by") in ("image", "remembered", "same as previous job"):
            # A newer preview during a job recognised by picture, by a remembered plate or as a reprint: if the picture
            # clearly shows a different plate, follow it (and remember that for this printer). The previous job's
            # last picture is ignored: at a job start the printer can still show the old one.
            prev, h = cur.get("prev") or {}, file_hash(cover)
            if (cover_at and cover_at not in ("unknown", "unavailable") and cover_at != cur.get("cover_used")
                    and not (h and h == prev.get("cover_hash"))):
                sid, info = self.match("", plate, cover)   # by picture only
                cur["cover_used"] = cover_at
                if sid and sid != cur["source"] and info.get("by") == "image":
                    log.info("%s: newer preview shows %s, not %s", printer, sid, cur["source"])
                    info["rematched_from"] = cur["source"]
                    if (cur.get("match") or {}).get("by") == "remembered" and task:
                        self.st["aliases"][f"{printer}|{task}|{plate}"] = sid
                    cur["source"], cur["match"] = sid, info
        if cur.get("job") and status in pl.ACTIVE:
            pl.note_job_details(cur, status, live)
            pl.take_queue_entry(self.st, printer, cur)
            if cover_at and cover_at != cur.get("cover_seen"):   # this job's latest picture
                cur["cover_seen"], cur["cover_hash"] = cover_at, file_hash(cover) or cur.get("cover_hash")
        if cur.get("job") and status in ("finish", "failed") and not cur.get("done"):
            cur["done"] = True
            src = self.sources.get(cur.get("source"))
            if status == "failed":
                e = self._log(printer=printer, task=task, plate=plate, source=cur.get("source"), result="failed")
                if src:   # retry it next on this printer, as the same kind of entry
                    base = {k: v for k, v in (cur.get("took") or {}).items() if k not in ("id", "source")}
                    base.setdefault("auto", True)
                    if not (base["auto"] and not src["counts"]):
                        pl.queue_state(self.st)
                        self.st["queue"].setdefault(printer, []).insert(
                            0, {**base, "id": f"q{self.st['next_q']}", "source": src["id"], "retry": True})
                        self.st["next_q"] += 1
            elif src is None:
                e = self._log(printer=printer, task=task, plate=plate, source=None, result="unmatched")
            elif src["counts"]:
                e = self._log(printer=printer, task=task, plate=plate, source=src["id"], result="counted", yields=self._add(src))
            else:
                e = self._log(printer=printer, task=task, plate=plate, source=src["id"], result="other")
            log.info("%s: %s %s", printer, e["result"], e.get("source"))
            result["logged"] = e
            self.st.setdefault("bed", {})[printer] = {"state": "needs_clearing", "since": now(), "log": e["id"]}
            rec = pl.job_time(printer, cur, e)
            pl.times_state(self.st)
            rec["id"] = f"t{self.st['next_t']}"; self.st["next_t"] += 1
            self.st["times"].append(rec)
            e["hours"], e["est_h"] = rec["hours"], rec["est_h"]
        self.st["printers"][printer] = cur
        result.update(source=cur.get("source"), match=cur.get("match"))
        pl.reconcile(self.st, self.catalog)
        if result.get("logged"):
            result["notify"] = pl.finish_notice(self.st, self.catalog, result, self.summary())
        self.save()
        return result

    # ------------------------------------------------------------ queue, times, planner
    COMMANDS = {"bed_clear": pl.cmd_bed_clear, "queue_move": pl.cmd_queue_move, "queue_send": pl.cmd_queue_send,
                "queue_remove": pl.cmd_queue_remove, "queue_restore": pl.cmd_queue_restore, "queue_add": pl.cmd_queue_add,
                "queue_cfg": pl.cmd_queue_cfg, "time_edit": pl.cmd_time_edit, "time_exclude": pl.cmd_time_exclude,
                "time_delete": pl.cmd_time_delete, "time_add": pl.cmd_time_add, "plan_cfg": pl.cmd_plan_cfg,
                "plan_apply": pl.cmd_plan_apply, "plan_clear": pl.cmd_plan_clear}

    def command(self, cmd, **a):
        """One of COMMANDS (they raise SystemExit with a message for the user when they refuse)."""
        try:
            res = self.COMMANDS[cmd](self.st, self.catalog, a)
        except SystemExit as e:
            raise ValueError(str(e))
        pl.reconcile(self.st, self.catalog)
        self.save()
        return res

    def published(self):
        """The state parts the pages show, with the per-printer plate times and the planner's inputs."""
        st = self.st
        pl.queue_state(st); pl.times_state(st); pl.plan_cfg(st)
        keys = ("target", "assembled", "counts", "printers", "log", "updated", "queue", "queue_skip", "queue_cfg",
                "queue_unassigned", "bed", "times", "plan", "plan_cfg", "printer_info", "custom_sources", "sets")
        return dict({k: st.get(k) for k in keys}, plan_cfg=pl.plan_cfg_view(st), plate_times=pl.plate_times(st, self.catalog),
                    plan_data=pl.plan_data(st, self.catalog))

    def assign(self, source, log_id=None, printer=None):
        srcs = self.sources
        src = srcs[source]
        if log_id:
            e = next(x for x in self.st["log"] if x["id"] == int(log_id))
            for r in self.st.get("times", []):   # the print's time sample follows the assignment
                if r.get("log") == e["id"]:
                    r["source"] = src["id"]
            if e["result"] == "counted" and e.get("source") in srcs:
                self._add(srcs[e["source"]], -1)
            e["source"] = src["id"]
            if src["counts"]:
                e["result"], e["yields"] = "counted", self._add(src)
            else:
                e["result"], e["yields"] = "other", {}
            task, plate, pr = e.get("task"), e.get("plate"), e.get("printer")
        else:
            cur = self.st["printers"][printer]
            cur["source"], cur["match"] = src["id"], {"by": "manual"}
            pl.take_queue_entry(self.st, printer, cur)
            task, plate, pr = cur.get("task"), cur.get("plate"), printer
        if task:   # remembered for this printer: print profiles share names but number their plates per printer
            self.st["aliases"][f"{pr}|{task}|{plate}" if pr else f"{task}|{plate}"] = src["id"]
        pl.reconcile(self.st, self.catalog)
        self.save()
        return {"assigned": src["id"]}

    def assign_custom(self, yields, log_id=None, printer=None):
        """A plate you changed (say two wheel rims and a bolt cap on one plate): count these parts for the print and keep
        the combination as a custom plate, remembered for reprints (job name + plate number on this printer, and the
        printer's preview picture of it). The queue and planner use it like a catalog plate, on the printer it was
        printed on."""
        parts = {p["id"]: p for p in self.catalog["parts"]}
        yields = {pid: int(n) for pid, n in (yields or {}).items() if pid in parts and int(n) > 0}
        if not yields:
            raise ValueError("pick at least one part and how many")
        sid = "custom:" + ",".join(f"{pid}={n}" for pid, n in sorted(yields.items()))
        self.st.setdefault("custom_sources", [])
        src = next((s for s in self.st["custom_sources"] if s["id"] == sid), None)
        if not src:
            per_piece = {}   # each part's volume, from a plate that holds only that part (for time guesses)
            for s in self.catalog["sources"]:
                if len(s["yields"]) == 1 and s.get("volume_cm3"):
                    (pid, n), = s["yields"].items()
                    vol = (sum(x.get("volume_cm3") or 0 for x in self.catalog["sources"] if x.get("set") == s["set"])
                           if s.get("set") else s["volume_cm3"])
                    per_piece.setdefault(pid, vol / n)
            first = parts[next(iter(yields))]
            src = {"id": sid, "file": "", "title": "Custom plate", "plate": 0, "custom": True, "created": now(),
                   "plate_name": " + ".join(f"{n} × {parts[pid]['name']}" for pid, n in yields.items()),
                   "counts": True, "group": first["group"], "yields": yields, "thumb": None,
                   "printers": [printer] if printer else None,
                   "volume_cm3": round(sum(per_piece.get(pid, 0) * n for pid, n in yields.items()), 1) or None}
            cover = self.path.parent / "covers" / f"{printer}.png" if printer else None
            if cover and cover.exists():   # kept beside the plate thumbnails; catalog rebuilds leave custom-* alone
                src["thumb"] = "custom-" + re.sub(r"[^a-z0-9]+", "-", sid.lower()).strip("-") + ".png"
                self.thumbs.mkdir(parents=True, exist_ok=True)
                (self.thumbs / src["thumb"]).write_bytes(cover.read_bytes())
            self.st["custom_sources"].append(src)
            self.catalog["sources"].append(src)
        res = self.assign(sid, log_id=log_id, printer=printer)
        return dict(res, custom=sid, label=src["plate_name"])

    def adjust(self, part, delta):
        self.st["counts"][part] = max(0, self.st["counts"].get(part, 0) + int(delta))
        self._log(result="adjusted", part=part, delta=int(delta))
        pl.reconcile(self.st, self.catalog)
        self.save()
        return {"part": part, "count": self.st["counts"][part]}

    def undo(self, log_id):
        e = next(x for x in self.st["log"] if x["id"] == int(log_id))
        if e["result"] == "counted" and e.get("source") in self.sources:
            self._add(self.sources[e["source"]], -1)
            e["result"] = "undone"
        pl.reconcile(self.st, self.catalog)
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
        pl.reconcile(self.st, self.catalog)
        self.save()
        return {"imported": len(set(counts) & known), "unknown": unknown}

    def target(self, count):
        self.st["target"] = max(1, int(count))
        pl.reconcile(self.st, self.catalog)
        self.save()
        return {"target": self.st["target"]}

    def assembled(self):
        if self.summary()["complete_in_pool"] < 1:
            raise ValueError("no complete build in the printed parts yet")
        counts = self.st["counts"]
        for p in self.catalog["parts"]:
            if not p["optional"] and not p.get("alt"):
                counts[p["id"]] = counts.get(p["id"], 0) - p["qty"]
            elif p.get("plan"):   # planned optional parts (accessories) go with it as far as there are any
                counts[p["id"]] = counts.get(p["id"], 0) - min(p["qty"], max(0, counts.get(p["id"], 0)))
        for alt in self.catalog.get("alternatives", []):
            for o in alt["options"]:
                if all(counts.get(pid, 0) >= q for pid, q in o["parts"].items()):
                    for pid, q in o["parts"].items():
                        counts[pid] -= q
                    break
        self.st["assembled"] += 1
        self.st["target"] = max(1, self.st["target"] - 1)
        self._log(result="assembled", build=self.st["assembled"])
        pl.reconcile(self.st, self.catalog)
        self.save()
        return {"assembled": self.st["assembled"]}
