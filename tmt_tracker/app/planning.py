"""Print queue, print times and the build planner's data, on the tracker's state dict (st) and the catalog (cat).

Print queue: every printer has a list of plates to print next. Auto entries are planned from what the planned builds
still need (after what's printed and what's printing now), in catalog order, each on the printer with the shortest
queue that has the plate's filament loaded; reconcile() runs after every change, so the queues follow the counts. A
plan from the planner page replaces the queues (planned entries) until it's cleared.

Print times are kept per printer only (the same plate can take twice as long on another printer): one sample per
finished job with the slicer's estimate, the real printing time (paused time left out), filament and nozzle.
"""
import datetime as dt
import json
import re

ACTIVE = {"prepare", "running", "pause", "slicing", "init"}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def when(v):
    try:
        t = dt.datetime.fromisoformat(str(v))
        return t if t.tzinfo else None
    except ValueError:
        return None


def sources_by_id(cat):
    return {s["id"]: s for s in cat["sources"]}


# ---------------------------------------------------------------- print times (kept per printer)
def note_job_details(cur, status, a):
    """For the running job: the slicer's estimate at the start (remaining time while progress is still 0-2%), and,
    once it's actually printing, the filament in use and the nozzle(s) heated for it."""
    rem, prog = num(a.get("remaining")), num(a.get("progress"))
    if cur.get("est_h") is None and status == "running" and rem and rem > 0 and (prog or 0) <= 2:
        cur["est_h"] = round(rem, 2)
    # Paused time (a filament runout at 1 AM that waits until morning) is left out of the print's time.
    t = dt.datetime.now(dt.timezone.utc)
    if status == "pause":
        cur.setdefault("paused_since", t.isoformat(timespec="seconds"))
    elif cur.get("paused_since"):
        since = when(cur.pop("paused_since"))
        if since:
            cur["paused_h"] = round((cur.get("paused_h") or 0) + (t - since).total_seconds() / 3600, 2)
    if a.get("stage") != "printing":
        return
    fil = a.get("filament") or {}
    if not cur.get("filament") and fil.get("type") and fil.get("name") not in (None, "", "unknown", "unavailable"):
        cur["filament"] = {"name": fil["name"], "type": str(fil["type"]).upper(), "id": fil.get("id")}
    if not cur.get("nozzle"):
        hot = [{"side": side, "size": num(n.get("size")), "type": n.get("type")}
               for side, n in (a.get("nozzles") or {}).items() if (num(n.get("target")) or 0) >= 170]
        cur["nozzle"] = hot or None


def job_time(pr, cur, e):
    """A finished (or failed) job's duration: from the printer's own start time when it belongs to this job, else
    from when the tracker first saw it."""
    end, seen = dt.datetime.now(dt.timezone.utc), when(cur.get("started"))
    began = when(cur.get("start_time"))
    if not began or not seen or not (seen - dt.timedelta(hours=3) <= began <= seen + dt.timedelta(minutes=10)):
        began = seen or end
    paused = cur.get("paused_h") or 0
    if cur.get("paused_since") and when(cur["paused_since"]):   # ended (failed/cancelled) while paused
        paused += (end - when(cur["paused_since"])).total_seconds() / 3600
    total = (end - began).total_seconds() / 3600
    return {"printer": pr, "source": e.get("source"), "result": e["result"], "log": e["id"],
            "start": began.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"),
            "hours": round(max(0.0, total - paused), 2), "total_h": round(total, 2), "paused_h": round(paused, 2),
            "est_h": cur.get("est_h"), "filament": cur.get("filament"), "nozzle": cur.get("nozzle")}


def median(xs):
    xs = sorted(xs); n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def twin_of(cat, s):
    """A plate's mirror twin in the same file (WING LEFT <-> WING RIGHT): same shape mirrored, same weight."""
    name = s.get("plate_name") or ""
    for a_, b_ in (("LEFT", "RIGHT"), ("RIGHT", "LEFT")):
        if re.search(rf"\b{a_}\b", name, re.I):
            other = re.sub(rf"\b{a_}\b", b_, name, flags=re.I).lower()
            return next((x for x in cat["sources"] if x["file"] == s["file"] and (x["plate_name"] or "").lower() == other), None)
    return None


USABLE = ("counted", "other", "manual")


def time_records(st, pr, nozzle=None):
    """This printer's usable print-time samples (not excluded), optionally only those printed with a nozzle size."""
    recs = [r for r in st.get("times", []) if r["printer"] == pr and r["result"] in USABLE and r.get("hours")
            and not r.get("excluded")]
    if nozzle:
        recs = [r for r in recs if not r.get("nozzle") or any(n.get("size") == nozzle for n in r["nozzle"])]
    return recs


def plate_time(st, cat, pr, s, nozzle=None):
    """How long this printer takes for a plate, from this printer's own samples only (never another printer's):
    its prints of the plate (median), else the slicer estimate of that plate printing on it now (scaled by how this
    printer's prints of that filament ran against their estimates), else the same for the mirror twin. With a nozzle
    size, only samples printed with that nozzle size."""
    recs = time_records(st, pr, nozzle)
    cur = st["printers"].get(pr) or {}
    def ratio(ftype):
        rs = [r["hours"] / r["est_h"] for r in recs if r.get("est_h") and (r.get("filament") or {}).get("type") == ftype]
        return median(rs) if rs else 1.0
    def from_(sid):
        own = [r for r in recs if r["source"] == sid]
        if own:
            last = own[-1]
            return {"h": round(median([r["hours"] for r in own]), 2), "n": len(own), "nozzle": last.get("nozzle"),
                    "filament": (last.get("filament") or {}).get("name")}
        if (cur.get("source") == sid and not cur.get("done") and cur.get("est_h")
                and (not nozzle or not cur.get("nozzle") or any(n.get("size") == nozzle for n in cur["nozzle"]))):
            f = cur.get("filament") or {}
            return {"h": round(cur["est_h"] * ratio(f.get("type")), 2), "est_h": cur["est_h"], "n": 0, "printing": True,
                    "nozzle": cur.get("nozzle"), "filament": f.get("name")}
        return None
    t = from_(s["id"])
    if t:
        return dict(t, basis="printed" if t["n"] else "estimate")
    tw = twin_of(cat, s)
    t = from_(tw["id"]) if tw else None
    return dict(t, basis="twin", twin=tw["plate_name"]) if t else None


GUESS_OVERHEAD_H = 0.25   # heating, calibration and cooling, roughly, before size matters


def plate_guess(st, cat, pr, s, nozzle=None):
    """A rough time from the plate's model volume and this printer's own pace (hours per cm³ above the overhead) for
    the same filament type, else any filament; None when this printer has no samples yet."""
    srcs = sources_by_id(cat)
    mat = (plate_materials(cat, s) or [""])[0]
    def pace(recs):
        rs = [(r["hours"] - GUESS_OVERHEAD_H) / srcs[r["source"]]["volume_cm3"] for r in recs
              if srcs.get(r["source"], {}).get("volume_cm3") and r["hours"] > GUESS_OVERHEAD_H]
        return median(rs) if rs else None
    recs = time_records(st, pr, nozzle) or time_records(st, pr)
    rate = pace([r for r in recs if (r.get("filament") or {}).get("type") == mat]) or pace(recs)
    if rate is None or not s.get("volume_cm3"):
        return None
    return {"h": round(GUESS_OVERHEAD_H + s["volume_cm3"] * rate, 2), "basis": "guess"}


def plate_times(st, cat):
    """printer -> plate id -> its time on that printer (only plates it has a time for)."""
    out = {}
    for pr in sorted(set(st["printers"]) | set(st.get("queue") or {})):
        out[pr] = {s["id"]: t for s in cat["sources"] if s["counts"] and (t := plate_time(st, cat, pr, s))}
    return out


def times_state(st):
    st.setdefault("times", [])
    st.setdefault("next_t", 1)
    for r in st["times"]:
        if "id" not in r:
            r["id"] = f"t{st['next_t']}"; st["next_t"] += 1


def find_time(st, tid):
    times_state(st)
    r = next((r for r in st["times"] if r["id"] == tid), None)
    if not r:
        raise SystemExit("that time sample is gone")
    return r


def cmd_time_edit(st, cat, a):
    """Correct a sample's print time (e.g. a print that sat paused overnight before pauses were tracked)."""
    r = find_time(st, a["id"])
    h = num(a.get("hours"))
    if h is None or h <= 0:
        raise SystemExit("give the print time in hours")
    r.setdefault("measured_h", r.get("hours")); r["hours"] = round(h, 2); r["edited"] = now()
    return {"id": r["id"], "hours": r["hours"]}


def cmd_time_exclude(st, cat, a):
    r = find_time(st, a["id"])
    r["excluded"] = bool(a.get("exclude", True))
    return {"id": r["id"], "excluded": r["excluded"]}


def cmd_time_delete(st, cat, a):
    r = find_time(st, a["id"])
    st["times"].remove(r)
    return {"deleted": r["id"]}


def cmd_time_add(st, cat, a):
    """A time you know (from Bambu Studio, MakerWorld, or a print the tracker didn't see), as a sample for a printer."""
    times_state(st)
    src = sources_by_id(cat).get(a.get("source"))
    h = num(a.get("hours"))
    if not src or not h or h <= 0 or a.get("printer") not in st["printers"]:
        raise SystemExit("pick a printer and a plate, and give the time in hours")
    size = num(a.get("nozzle"))
    r = {"id": f"t{st['next_t']}", "printer": a["printer"], "source": src["id"], "result": "manual", "hours": round(h, 2),
         "start": now(), "end": now(), "est_h": None, "filament": None,
         "nozzle": [{"side": None, "size": size, "type": None}] if size else None, "note": (a.get("note") or "")[:120]}
    st["next_t"] += 1
    st["times"].append(r)
    return {"added": r["id"]}


# ---------------------------------------------------------------- print queue
def queue_state(st):
    for k, v in (("queue", {}), ("next_q", 1), ("queue_skip", []), ("queue_cfg", {}), ("bed", {}), ("queue_unassigned", [])):
        st.setdefault(k, v)
    for pr in st["printers"]:
        st["queue"].setdefault(pr, [])


def plate_label(s):
    if s.get("custom"):
        return f"{s['plate_name']} (custom plate)"
    return f"{s['plate_name'] or s['title']} · plate {s['plate']}"


def migrate_aliases(st):
    """Remembered plates are kept per printer ("printer|task|plate"): a print profile's name (the task) can be shared by
    every printer's version of the profile, and each version numbers its plates differently. An old shared
    "task|plate" alias moves to the printer(s) whose logged prints it came from."""
    prs = set(st.get("printers") or {})
    for key in [k for k in st.get("aliases", {}) if k.split("|", 1)[0] not in prs]:
        sid = st["aliases"][key]
        task, _, plate = key.rpartition("|")
        made_on = {e["printer"] for e in st["log"] if e.get("printer") and e.get("task") == task
                   and str(e.get("plate")) == plate and e.get("source") == sid}
        if made_on:
            for pr in made_on:
                st["aliases"].setdefault(f"{pr}|{key}", sid)
            del st["aliases"][key]


def plate_materials(cat, s):
    mats = {p["id"]: (p.get("material") or "").upper() for p in cat["parts"]}
    return sorted({mats.get(pid, "") for pid in s["yields"]} - {""})


def printer_materials(st, pr):
    """Filament types a printer can print now: what's loaded (auto), or what you set on the dashboard."""
    cfg = st["queue_cfg"].get(pr) or {}
    if cfg.get("materials") is not None:
        return [m.upper() for m in cfg["materials"]]
    return (st["printers"].get(pr) or {}).get("materials") or []


def can_print(st, cat, pr, s):
    if s.get("printers") and pr not in s["printers"]:
        return False
    models = s.get("printer_models")   # e.g. [H2] for the one-piece push handle, which needs an H2 printer's bed
    if models:
        model = (((st.get("printer_info") or {}).get(pr) or {}).get("model") or "").upper()
        if not any(model.startswith(str(m).upper()) for m in models):
            return False
    have = printer_materials(st, pr)   # tray types like PETG, TPU; 'PETG' also matches e.g. 'PETG-CF'
    return all(any(h.startswith(m) for h in have) for m in plate_materials(cat, s))


def queue_on(st, pr):
    return (st["queue_cfg"].get(pr) or {}).get("on", True)


def printing_now(st):
    """printer -> the counting plate it's printing (recognised, not finished)."""
    return {pr: cur["source"] for pr, cur in st["printers"].items()
            if cur.get("source") and cur.get("job") and not cur.get("done") and cur.get("status") in ACTIVE}


def still_needed(st, cat):
    """Pieces per part the planned TMTs still need, after what's printed and what's printing now (optional parts out)."""
    counts = st["counts"]
    need = {p["id"]: p["qty"] * st["target"] - counts.get(p["id"], 0)
            for p in cat["parts"] if not p["optional"] and not p.get("alt")}
    for alt in cat.get("alternatives", []):   # an either/or part: plan the first option unless one is already covered
        if alt["options"] and not any(all(counts.get(pid, 0) >= q * st["target"] for pid, q in o["parts"].items())
                                      for o in alt["options"]):
            for pid, q in alt["options"][0]["parts"].items():
                need[pid] = q * st["target"] - counts.get(pid, 0)
    srcs = sources_by_id(cat)
    for sid in printing_now(st).values():
        if srcs.get(sid) and srcs[sid]["counts"]:
            for pid, n in srcs[sid]["yields"].items():
                need[pid] = need.get(pid, 0) - n
    return need


def planned_runs(st, cat):
    """The plates still to print, in catalog order, a plate repeated when it's needed more than once (4 wheel rims).
    Each part comes from the plate that makes the most of it (the 8-tire plate, not the 1-tire test plate)."""
    need, skip = still_needed(st, cat), set(st["queue_skip"])
    srcs = [s for s in cat["sources"] if s["counts"] and s["id"] not in skip]
    producer = {}
    for s in srcs:
        for pid, n in s["yields"].items():
            if pid not in producer or n > producer[pid]["yields"][pid]:
                producer[pid] = s
    runs = []
    for s in srcs:
        mine = [pid for pid in s["yields"] if producer.get(pid) is s]
        while any(need.get(pid, 0) > 0 for pid in mine):
            runs.append(s["id"])
            for pid, n in s["yields"].items():
                need[pid] = need.get(pid, 0) - n
    return runs


def reconcile(st, cat):
    """Bring the queues in line with the plan: drop what's no longer needed or can't print where it is, add what's
    missing to the printer with the shortest queue that can print it. Your order, moves and manual entries stay."""
    queue_state(st)
    srcs, runs, printing = sources_by_id(cat), planned_runs(st, cat), printing_now(st)
    want = {}
    for sid in runs:
        want[sid] = want.get(sid, 0) + 1
    for pr, q in st["queue"].items():
        q[:] = [e for e in q if e["source"] in srcs and (not e["auto"] or e.get("pinned")
                or (queue_on(st, pr) and can_print(st, cat, pr, srcs[e["source"]])))]
    queued = {}
    for q in st["queue"].values():
        for e in q:
            queued[e["source"]] = queued.get(e["source"], 0) + 1
    for sid, n in queued.items():
        extra = n - want.get(sid, 0)
        # Take auto entries out: first from a printer printing that plate now (its front), then from the other queues' ends.
        here = [pr for pr, s in printing.items() if s == sid]
        order = [(pr, e) for pr in here for e in list(st["queue"].get(pr, []))]
        order += [(pr, e) for pr, q in st["queue"].items() if pr not in here for e in reversed(q)]
        for pr, e in order:
            if extra <= 0:
                break
            if e["source"] == sid and e["auto"] and e in st["queue"][pr]:
                st["queue"][pr].remove(e); extra -= 1
                queued[sid] -= 1
    seen, unassigned = {}, []
    for sid in runs:
        seen[sid] = seen.get(sid, 0) + 1
        if seen[sid] <= queued.get(sid, 0):
            continue
        fits = [pr for pr in sorted(st["queue"]) if queue_on(st, pr) and can_print(st, cat, pr, srcs[sid])]
        if not fits:
            unassigned.append(sid)
            continue
        pr = min(fits, key=lambda p: (len(st["queue"][p]) + (p in printing), p))
        st["queue"][pr].append({"id": f"q{st['next_q']}", "source": sid, "auto": True})
        st["next_q"] += 1
        queued[sid] = queued.get(sid, 0) + 1
    st["queue_unassigned"] = unassigned
    rebalance(st, cat, srcs, runs, printing)


def rebalance(st, cat, srcs, runs, printing):
    """Even the queues out (a printer that just got its filament or was switched back on starts empty): move auto
    entries from the end of a queue 2+ longer to a queue that can print them, slotted in by plan order. The front of
    each queue (what's next) stays put, and entries you placed by hand or retries never move."""
    rank = {}
    for i, sid in enumerate(runs):
        rank.setdefault(sid, i)
    load = lambda p: len(st["queue"][p]) + (p in printing)
    prs = [p for p in sorted(st["queue"]) if queue_on(st, p)]
    fresh = {p for p in prs if not st["queue"][p]}   # nothing announced as next yet: plan order from the first slot
    for _ in range(200):
        move = None
        for a in sorted(prs, key=load, reverse=True):
            for b in sorted(prs, key=load):
                if load(a) - load(b) < 2:
                    break
                e = next((e for e in reversed(st["queue"][a][1:]) if e["auto"] and not e.get("pinned") and not e.get("retry")
                          and can_print(st, cat, b, srcs[e["source"]])), None)
                if e:
                    move = (a, b, e)
                    break
            if move:
                break
        if not move:
            return
        a, b, e = move
        st["queue"][a].remove(e)
        q = st["queue"][b]
        at = next((i for i, x in enumerate(q) if x["auto"] and rank.get(x["source"], 1e9) > rank.get(e["source"], 1e9)), len(q))
        q.insert(at if b in fresh else max(at, 1 if q else 0), e)


def live_nozzle(st, pr):
    """The printing nozzle's size now: the left one on a dual-nozzle printer, else its only nozzle."""
    n = ((st.get("live") or {}).get(pr) or {}).get("nozzles") or {}
    return num((n.get("left") or n.get("main") or {}).get("size"))


def next_up(st, cat, pr):
    q = (st.get("queue") or {}).get(pr) or []
    s = sources_by_id(cat).get(q[0]["source"]) if q else None
    if not s:
        return None
    txt = f"{plate_label(s)} ({', '.join(plate_materials(cat, s))})"
    want, have = q[0].get("nozzle"), live_nozzle(st, pr)
    if want and have and abs(want - have) > 0.01:
        txt += f", swap to the {want:g} nozzle first"
    return txt


def take_queue_entry(st, pr, cur):
    """Once a job is recognised, take its plate's entry off this printer's queue (the first one for that plate). If the
    job turns out to be another plate, the entry goes back to the front. A plate with no entry here is left to
    reconcile, which takes one off another printer's queue if that's now more than needed."""
    sid, took = cur.get("source"), cur.get("took")
    if took is not None and took.get("source") == sid:
        return
    queue_state(st)
    q = st["queue"].setdefault(pr, [])
    if took and took.get("id"):
        q.insert(0, took)
    cur["took"] = {}
    if sid:
        e = next((e for e in q if e["source"] == sid), None)
        if e:
            q.remove(e)
        cur["took"] = e or {"source": sid}


# ---------------------------------------------------------------- build planner
# The planner page (tmt/plan.html) does the scheduling in the browser (it works in local time); this side gives it the
# plates to print, each printer's setups and times, and stores the plan it applies to the queues.
PLAN_DEFAULT = {
    "day_start": "06:00", "day_end": "21:00", "turnaround_min": 10, "swap_min": 20, "default_h": 6.0,
    "keep_moved": True,
    "setups": {},   # printer -> [{name, nozzle, materials, groups}] in order; empty materials/groups = any
}


def plan_cfg(st):
    """The planner's saved settings (setups only for printers you've set up on the planner page)."""
    cfg = st.setdefault("plan_cfg", json.loads(json.dumps(PLAN_DEFAULT)))
    for k, v in PLAN_DEFAULT.items():
        cfg.setdefault(k, json.loads(json.dumps(v)))
    return cfg


def setups_for(st, pr):
    """A printer's setups: yours, else one that prints everything on the nozzle fitted now (not saved, so it follows
    the printer until you save setups for it)."""
    mine = plan_cfg(st)["setups"].get(pr)
    if mine:
        return mine
    size = live_nozzle(st, pr) or 0.4
    return [{"name": f"Everything on the {size:g}", "nozzle": size, "materials": [], "groups": []}]


def plan_cfg_view(st):
    """The settings as the planner page shows them: saved ones, with the default setup for printers without any."""
    cfg = json.loads(json.dumps(plan_cfg(st)))
    cfg["setups"] = {pr: setups_for(st, pr) for pr in st["printers"]}
    return cfg


def plan_data(st, cat):
    """What the planner needs: the plates still to print (the planned TMTs' needs, plus plates you added by hand),
    where you moved plates, and each printer's time for each plate at each nozzle size its setups use."""
    cfg, srcs = plan_cfg(st), sources_by_id(cat)
    runs = planned_runs(st, cat)
    jobs = [{"source": sid} for sid in runs]
    for pr, q in (st.get("queue") or {}).items():
        for e in q:
            if e["source"] not in srcs:
                continue
            hit = next((j for j in jobs if j["source"] == e["source"] and "printer" not in j and "manual" not in j), None)
            if not e.get("auto"):
                if hit:
                    hit["manual"] = True
                else:
                    hit = {"source": e["source"], "manual": True}; jobs.append(hit)
            if hit and (e.get("pinned") and not e.get("planned") or not e.get("auto")):
                hit["printer"] = pr   # you put it on this printer
    times = {}
    for pr in st["printers"]:
        setups = setups_for(st, pr)
        times[pr] = {}
        for size in sorted({s["nozzle"] for s in setups}):
            row = {}
            for sid in {j["source"] for j in jobs}:
                s = srcs[sid]
                row[sid] = plate_time(st, cat, pr, s, size) or plate_guess(st, cat, pr, s, size)
            times[pr][f"{size:g}"] = row
    return {"jobs": jobs, "times": times, "nozzle_now": {pr: live_nozzle(st, pr) for pr in st["printers"]}}


def cmd_plan_cfg(st, cat, a):
    cfg = plan_cfg(st)
    for k in ("day_start", "day_end"):
        if k in a:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(a[k])):
                raise SystemExit(f"{k}: use HH:MM")
            cfg[k] = a[k]
    for k in ("turnaround_min", "swap_min", "default_h"):
        if k in a:
            v = num(a[k])
            if v is None or v < 0:
                raise SystemExit(f"{k}: a number, please")
            cfg[k] = v
    if "keep_moved" in a:
        cfg["keep_moved"] = bool(a["keep_moved"])
    if "setups" in a:
        groups = {p["group"] for p in cat["parts"]}
        out = {}
        for pr, setups in a["setups"].items():
            if pr not in st["printers"]:
                raise SystemExit(f"no printer {pr!r}")
            out[pr] = [{"name": str(s.get("name") or f"{num(s.get('nozzle')):g} nozzle")[:40], "nozzle": num(s.get("nozzle")) or 0.6,
                        "materials": sorted({str(m).upper() for m in s.get("materials") or []}),
                        "groups": [g for g in s.get("groups") or [] if g in groups]} for s in setups]
        cfg["setups"] = out
    return {"plan_cfg": plan_cfg_view(st)}


def cmd_plan_apply(st, cat, a):
    """Use a plan: every printer's queue becomes the plan's plates in order (with their setup and nozzle)."""
    queue_state(st)
    srcs, newq = sources_by_id(cat), {}
    for pr, items in (a.get("queues") or {}).items():
        if pr not in st["queue"]:
            raise SystemExit(f"no printer {pr!r}")
        newq[pr] = []
        for it in items:
            if it.get("source") not in srcs:
                raise SystemExit("the plan has a plate that's no longer in the catalog; make the plan again")
            newq[pr].append({"id": f"q{st['next_q']}", "source": it["source"], "auto": not it.get("manual"), "pinned": True,
                             "planned": True, "setup": str(it.get("setup") or "")[:40], "nozzle": num(it.get("nozzle")),
                             "plan_start": it.get("start"), "plan_end": it.get("end"), "guess": bool(it.get("guess"))})
            st["next_q"] += 1
    for pr in st["queue"]:
        st["queue"][pr] = newq.get(pr, [])
    st["plan"] = {"applied": now(), "summary": a.get("summary") or {}}
    return {"applied": sum(len(q) for q in newq.values())}


def cmd_plan_clear(st, cat, a):
    """Stop following the plan: the queues go back to filling and balancing themselves."""
    queue_state(st)
    for q in st["queue"].values():
        for e in q:
            if e.get("planned"):
                for k in ("pinned", "planned", "setup", "nozzle", "plan_start", "plan_end", "guess"):
                    e.pop(k, None)
    st["plan"] = None
    return {"cleared": True}


def printer_name(st, pr):
    return ((st.get("printer_info") or {}).get(pr) or {}).get("name") or pr.upper()


def finish_notice(st, cat, res, summary):
    """Phone alert text for a print that just ended: what happened, the TMT's progress, and the printer's next plate."""
    e, pr = res["logged"], res["printer"]
    s = sources_by_id(cat).get(e.get("source"))
    what = (s["plate_name"] or f"{s['title']} plate {s['plate']}") if s else (e.get("task") or "The print")
    slot = (summary.get("slots") or [{}])[0]
    name = printer_name(st, pr)
    if e["result"] == "failed":
        title, first = f"{name}: print failed", f"{what} failed and wasn't counted."
    elif e["result"] == "counted":
        title, first = f"{name} finished", f"{what} counted. TMT #{slot.get('build', 1)}: {int(slot.get('pieces', 0))} of {slot.get('of', 0)} pieces."
    elif e["result"] == "unmatched":
        title, first = f"{name} finished", "Not recognised as a TMT plate: say which plate it was on the TMT page."
    else:
        title, first = f"{name} finished", f"{what} (not a TMT plate)."
    n = next_up(st, cat, pr)
    then = f"Next: {n}. Clear the plate, then tap Plate is clear." if n else "Nothing else is queued for this printer."
    return {"printer": pr, "title": title, "message": f"{first}\n{then}"}


def find_entry(st, entry):
    for pr, q in st["queue"].items():
        for e in q:
            if e["id"] == entry:
                return pr, q, e
    raise SystemExit("that queue entry is gone (the queue changed); try again")


def cmd_bed_clear(st, cat, a):
    pr = a["printer"]
    st.setdefault("bed", {})[pr] = {"state": "clear", "since": now()}
    reconcile(st, cat)
    n = next_up(st, cat, pr)
    return {"printer": pr, "next": n, "notify": {"printer": pr, "title": f"{printer_name(st, pr)} ready",
            "message": f"Plate is clear. Start next: {n}." if n else "Plate is clear. Nothing is queued for this printer."}}


def cmd_queue_move(st, cat, a):
    queue_state(st)
    pr, q, e = find_entry(st, a["entry"])
    i = q.index(e); j = max(0, min(len(q) - 1, i + int(a["dir"])))
    q.insert(j, q.pop(i))
    return {"printer": pr, "position": j + 1}


def cmd_queue_send(st, cat, a):
    queue_state(st)
    to = a["to"]
    if to not in st["queue"]:
        raise SystemExit(f"no printer {to!r}")
    pr, q, e = find_entry(st, a["entry"])
    q.remove(e); e["pinned"] = True   # placed by hand: stays even if that printer lacks the filament
    st["queue"][to].append(e)
    return {"moved": e["id"], "to": to}


def cmd_queue_remove(st, cat, a):
    """A manual entry is removed; an auto entry's plate is skipped (not planned again until restored)."""
    queue_state(st)
    pr, q, e = find_entry(st, a["entry"])
    q.remove(e)
    if e["auto"] and e["source"] not in st["queue_skip"]:
        st["queue_skip"].append(e["source"])
    return {"removed": e["id"], "skipped": e["source"] if e["auto"] else None}


def cmd_queue_restore(st, cat, a):
    queue_state(st)
    st["queue_skip"] = [s for s in st["queue_skip"] if s != a["source"]]
    return {"restored": a["source"]}


def cmd_queue_add(st, cat, a):
    queue_state(st)
    if a["source"] not in sources_by_id(cat):
        raise SystemExit("unknown plate")
    st["queue"].setdefault(a["printer"], []).append({"id": f"q{st['next_q']}", "source": a["source"], "auto": False})
    st["next_q"] += 1
    return {"added": a["source"], "printer": a["printer"]}


def cmd_queue_cfg(st, cat, a):
    queue_state(st)
    cfg = st["queue_cfg"].setdefault(a["printer"], {})
    if "on" in a:
        cfg["on"] = bool(a["on"])
    if "materials" in a:   # a list, or null to follow what's loaded
        cfg["materials"] = None if a["materials"] is None else sorted({str(m).upper() for m in a["materials"]})
    return {"printer": a["printer"], "cfg": cfg}


