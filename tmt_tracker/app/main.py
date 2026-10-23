"""TMT Print Tracker add-on: watches Bambu Lab printers in Home Assistant, works out which plate of which 3MF each one
is printing, counts the parts of finished plates toward builds, keeps a print queue per printer, and serves the
dashboard and the build planner over ingress."""
import asyncio
import json
import logging
import os
import re
import shutil
import socket
import time
from pathlib import Path

from aiohttp import ClientSession, web

import catalog as catalog_mod
from ha import HomeAssistant
from tracker import Tracker

VERSION = "0.4.0"
APP = Path(__file__).resolve().parent
DATA = Path("/data")                  # add-on private storage (state, thumbnails, covers)
CONFIG = Path("/config")              # this add-on's folder in addon_configs: profiles/ and files/
FILES = CONFIG / "files"
BUILD = CONFIG / "build.json"          # what you set on the 3MF files page (catalog.py explains it)
OPTIONS = DATA / "options.json"
LIVE_SENSORS = ("print_status", "task_name", "gcode_filename", "print_progress", "remaining_time", "current_layer",
                "total_layer_count", "end_time", "current_stage", "start_time")
JOB_SENSORS = ("print_status", "task_name", "gcode_filename", "current_stage")
CLEAR_ACTION = "TMT_TRACKER_CLEAR_"   # the alert's "Plate is clear" button: CLEAR_ACTION + printer key in capitals
TICK_S = 300                          # also look at every printer this often (filament loaded, nozzle, estimates)
HIDDEN_ATTRS = ("access_token", "entity_picture")

log = logging.getLogger("tmt")


class App:
    def __init__(self, options):
        self.options = options
        self.tracker = Tracker(DATA)
        self.lock = asyncio.Lock()
        self.printers = []            # [{name, prefix, model, entities}]
        self.catalog = {}
        self.profile = None
        self.profile_path = None
        self.profile_error = None
        self.ha = None
        self._pending = {}
        self.cams = {}                # camera WebRTC sessions: subscription id -> {printer, last, candidates}
        self._warming = None          # the background task decoding plate outlines (tracker.warm_masks)

    # ------------------------------------------------------------ setup
    def resolve_profile(self):
        """The 'profile' option: a built-in profile name, or a .yaml path inside this add-on's config folder."""
        (CONFIG / "profiles").mkdir(parents=True, exist_ok=True)
        for p in (APP / "profiles").glob("*.yaml"):     # examples to copy from; never overwritten
            dst = CONFIG / "profiles" / f"example_{p.name}"
            if not dst.exists():
                shutil.copy(p, dst)
        name = (self.options.get("profile") or "makegood_tmt_rev_c").strip()
        if name.endswith((".yaml", ".yml")):
            return CONFIG / name.lstrip("/")
        return APP / "profiles" / f"{name}.yaml"

    def load_catalog(self):
        """(profile path, profile, catalog, error). Reading a new 3MF takes a while (minutes for a big one on a small
        machine), so this runs in a worker thread; files already read come from the cache."""
        FILES.mkdir(parents=True, exist_ok=True)
        path = None
        try:
            path = self.resolve_profile()
            profile = catalog_mod.load_profile(path)
            return path, profile, catalog_mod.build(profile, FILES, DATA / "thumbs", DATA / "files_cache.json", BUILD), None
        except Exception as e:
            log.error("profile %s: %s", path, e)
            return path, None, {"profile": "", "parts": [], "alternatives": [], "sources": [], "files": [], "missing": [],
                                "skipped": [], "removed": [], "gone": [],
                                "cfg": {"groups": [], "saved_groups": False, "parts": {}, "rules": [], "files": {}}}, str(e)

    async def rebuild_catalog(self):
        self.profile_path, self.profile, self.catalog, self.profile_error = await asyncio.to_thread(self.load_catalog)
        self.tracker.set_catalog(self.catalog)
        self._warming = asyncio.create_task(asyncio.to_thread(self.tracker.warm_masks))

    async def entity_ids(self):
        found = await self.ha.bambu_printers()
        configured = self.options.get("printers") or []
        if configured:
            models = {p["prefix"]: p["model"] for p in found}
            printers = [{"name": p["name"], "prefix": p["entity_prefix"], "model": models.get(p["entity_prefix"], "")} for p in configured]
        else:
            printers = found
        for p in printers:
            p["entities"] = await self.ha.printer_entities(p["prefix"])
        self.printers = printers
        log.info("printers: %s", ", ".join(f"{p['name']} ({p['prefix']}, {p['model'] or 'model?'}, {len(p['entities'])} entities)"
                                           for p in self.printers) or "none found")
        self.tracker.set_printers({p["prefix"]: {"name": p["name"], "model": p["model"]} for p in self.printers})
        ids = set()
        for p in self.printers:
            ids |= set(p["entities"]) | {f"sensor.{p['prefix']}_{s}" for s in LIVE_SENSORS} | {f"image.{p['prefix']}_cover_image"}
        return sorted(ids)

    def printer(self, key):
        p = next((p for p in self.printers if p["prefix"] == key), None)
        if not p:
            raise web.HTTPNotFound(text=f"no printer {key!r}")
        return p

    # ------------------------------------------------------------ printer events
    async def on_connect(self):
        if self.notify_targets():
            try:
                await self.ha.subscribe(self.on_notification_action, type="subscribe_events", event_type="mobile_app_notification_action")
            except Exception as e:
                log.warning("can't listen for the alerts' Plate is clear button: %s", e)
        for p in self.printers:
            await self.process(p["prefix"], cover_changed=True)

    async def on_change(self, entity_ids):
        for p in self.printers:
            pre = p["prefix"]
            job = any(e in entity_ids for e in [f"sensor.{pre}_{s}" for s in JOB_SENSORS])
            cover = f"image.{pre}_cover_image" in entity_ids
            if job or cover:
                # A finish usually changes several entities at once; handle them together.
                prev = self._pending.get(pre)
                if prev and not prev.done():
                    prev.cancel()
                self._pending[pre] = asyncio.create_task(self._debounced(pre, cover))

    async def _debounced(self, prefix, cover):
        await asyncio.sleep(1.5)
        await self.process(prefix, cover)

    async def tick(self):
        while True:
            await asyncio.sleep(TICK_S)
            if self.ha and self.ha.connected:
                for p in self.printers:
                    try:
                        await self.process(p["prefix"])
                    except Exception as e:
                        log.warning("%s: %s", p["prefix"], e)

    def live_info(self, prefix):
        """What the tracker needs beyond the job sensors: filament types loaded (AMS trays with a spool, plus the
        external spools' set type, whose 'empty' flag isn't reliable), nozzles per side, the slicer's remaining time,
        progress, stage, the filament in use, and the printer's job start time."""
        s, at = self.ha.states, self.ha.attrs
        p = next((p for p in self.printers if p["prefix"] == prefix), {"entities": []})
        mats = set()
        for eid in p["entities"]:
            if not eid.startswith("sensor.") or not ("_tray_" in eid or eid.endswith("_external_spool")):
                continue
            a, state = at.get(eid) or {}, s.get(eid)
            kind = str(a.get("type") or "").upper()
            loaded = state not in (None, "Empty", "unknown", "unavailable", "?", "") and (eid.endswith("_external_spool") or not a.get("empty"))
            if kind and loaded:
                mats.add(kind)
        sides = ("left_", "right_") if f"sensor.{prefix}_left_nozzle_size" in s else ("",)
        nozzles = {(side.rstrip("_") or "main"): {"size": s.get(f"sensor.{prefix}_{side}nozzle_size"),
                                                 "type": s.get(f"sensor.{prefix}_{side}nozzle_type"),
                                                 "target": s.get(f"sensor.{prefix}_{side}nozzle_target_temperature")}
                   for side in sides if s.get(f"sensor.{prefix}_{side}nozzle_size") not in (None, "unknown", "unavailable")}
        tray = f"sensor.{prefix}_active_tray"
        return {"materials": sorted(mats), "nozzles": nozzles or None,
                "remaining": s.get(f"sensor.{prefix}_remaining_time"), "progress": s.get(f"sensor.{prefix}_print_progress"),
                "stage": s.get(f"sensor.{prefix}_current_stage"), "start_time": s.get(f"sensor.{prefix}_start_time"),
                "filament": {"name": s.get(tray), "type": (at.get(tray) or {}).get("type"), "id": (at.get(tray) or {}).get("filament_id")}}

    async def process(self, prefix, cover_changed=False):
        s = self.ha.states
        cover_entity = f"image.{prefix}_cover_image"
        cover_at = s.get(cover_entity)
        cover_path = DATA / "covers" / f"{prefix}.png"
        if cover_changed and cover_at not in (None, "unknown", "unavailable"):
            try:
                cover_path.parent.mkdir(parents=True, exist_ok=True)
                cover_path.write_bytes(await self.ha.fetch(f"/api/image_proxy/{cover_entity}"))
            except Exception as e:
                log.warning("%s: couldn't fetch the plate preview: %s", prefix, e)
        async with self.lock:
            res = self.tracker.update(prefix, s.get(f"sensor.{prefix}_print_status"), s.get(f"sensor.{prefix}_task_name"),
                                      s.get(f"sensor.{prefix}_gcode_filename"), str(cover_path), cover_at, self.live_info(prefix))
        log.debug("%s: %s", prefix, res)
        if res.get("notify"):
            await self.alert(res["notify"], clear_button=True)

    # ------------------------------------------------------------ phone alerts
    def notify_targets(self):
        """The notify services from the 'notify' option, as (domain, service): 'notify.mobile_app_x' or 'mobile_app_x'."""
        out = []
        for t in self.options.get("notify") or []:
            t = str(t).strip()
            if t:
                out.append(tuple(t.split(".", 1)) if "." in t else ("notify", t))
        return out

    def alert_url(self):
        """Where tapping an alert goes: the 'alert_url' option, else this add-on's page (its slug is the hostname)."""
        return (self.options.get("alert_url") or "").strip() or "/hassio/ingress/" + socket.gethostname().replace("-", "_")

    async def alert(self, notice, clear_button):
        """One alert per printer (tag tmt_tracker_<printer>, so a newer one replaces it) to every notify target."""
        targets = self.notify_targets()
        if not targets or not (self.ha and self.ha.connected):
            return
        pr = notice["printer"]
        data = {"tag": f"tmt_tracker_{pr}", "group": "tmt_tracker", "url": self.alert_url(), "clickAction": self.alert_url(),
                "push": {"interruption-level": "active" if clear_button else "passive"}}
        if clear_button:
            data["actions"] = [{"action": CLEAR_ACTION + pr.upper(), "title": "Plate is clear"}]
        for domain, service in targets:
            try:
                await self.ha.service(domain, service, {"title": notice["title"], "message": notice["message"], "data": data})
            except Exception as e:
                log.warning("alert via %s.%s: %s", domain, service, e)

    async def on_notification_action(self, event):
        action = str((event.get("data") or {}).get("action") or "")
        if not action.startswith(CLEAR_ACTION):
            return
        key = action[len(CLEAR_ACTION):].lower()
        if not any(p["prefix"] == key for p in self.printers):
            return
        async with self.lock:
            res = self.tracker.command("bed_clear", printer=key)
        log.info("%s: plate cleared from the phone alert", key)
        if res.get("notify"):
            await self.alert(res["notify"], clear_button=False)

    # ------------------------------------------------------------ web
    def live(self):
        s = self.ha.states if self.ha else {}
        return [{"key": p["prefix"], "name": p["name"], "model": p.get("model"),
                 "camera": next((e for e in p.get("entities", []) if e.startswith("camera.")), None),
                 "live": {k: s.get(f"sensor.{p['prefix']}_{k}") for k in LIVE_SENSORS}} for p in self.printers]

    async def h_data(self, request):
        return web.json_response({
            "version": VERSION, "connected": bool(self.ha and self.ha.connected),
            "profile": self.catalog.get("profile"), "profile_path": str(self.profile_path), "profile_error": self.profile_error,
            "catalog": {**{k: self.catalog.get(k) for k in ("parts", "alternatives", "sources", "missing")},
                        "files_summary": self.files_summary()},
            "state": self.tracker.published(), "alerts": bool(self.notify_targets()),
            "summary": self.tracker.summary(), "printers": self.live()})

    def files_summary(self):
        rows = self.catalog.get("files") or []
        return {"files": len(rows), "plates": sum(len(f.get("plates") or []) for f in rows),
                "new": [f["file"] for f in rows if not f["classified"] and not f.get("error")],
                "errors": [f["file"] for f in rows if f.get("error")]}

    async def h_files(self, request):
        """Everything the 3MF files page shows."""
        return web.json_response({
            "version": VERSION, "profile": self.catalog.get("profile"), "profile_path": str(self.profile_path),
            "profile_error": self.profile_error, "catalog": self.catalog, "state": self.tracker.published(),
            "printers": [{"key": p["prefix"], "name": p["name"], "model": p.get("model")} for p in self.printers]})

    async def h_files_action(self, request):
        """The 3MF files page's changes: file_save, part_save, groups_save, profile_skip, file_remove, file_restore and
        scan. Each rebuilds the catalog and says what that changed for a TMT."""
        a = await request.json()
        cmd = a.pop("cmd", "")
        if cmd not in ("file_save", "part_save", "groups_save", "profile_skip", "file_remove", "file_restore", "scan"):
            return web.json_response({"ok": False, "error": f"unknown command {cmd!r}"}, status=400)
        async with self.lock:
            before = self.catalog
            try:
                if cmd in ("file_save", "part_save", "groups_save", "profile_skip"):
                    if self.profile is None:
                        raise ValueError(f"fix the build profile first: {self.profile_error}")
                    cfg = catalog_mod.load_cfg(BUILD)
                    if cmd == "file_save":
                        catalog_mod.file_save(self.profile, cfg, before, a, [p["prefix"] for p in self.printers])
                    elif cmd == "part_save":
                        catalog_mod.part_save(cfg, before, a)
                    elif cmd == "groups_save":
                        self.tracker.rename_groups(catalog_mod.groups_save(self.profile, cfg, before, a))
                    else:
                        catalog_mod.profile_skip(self.profile, cfg, a)
                    catalog_mod.save_cfg(BUILD, cfg)
                elif cmd == "file_remove":   # moved aside, never deleted; its settings stay in build.json
                    name = Path(str(a.get("file", ""))).name
                    if not name.lower().endswith(".3mf") or not (FILES / name).is_file():
                        raise ValueError("that file isn't in the folder")
                    (FILES / "removed").mkdir(exist_ok=True)
                    dest, n = FILES / "removed" / name, 1
                    while dest.exists():   # an older removed copy of the same name stays too
                        n += 1
                        dest = FILES / "removed" / f"{Path(name).stem} ({n}).3mf"
                    os.replace(FILES / name, dest)
                elif cmd == "file_restore":
                    name = Path(str(a.get("file", ""))).name
                    if not (FILES / "removed" / name).is_file():
                        raise ValueError("that file isn't in removed")
                    if (FILES / name).exists():
                        raise ValueError(f"there's already a {name} in the folder")
                    os.replace(FILES / "removed" / name, FILES / name)
            except (ValueError, OSError) as e:
                return web.json_response({"ok": False, "error": str(e)})
            await self.rebuild_catalog()
        return web.json_response({"ok": True, **self.change_notes(before)})

    def change_notes(self, before):
        """What a catalog rebuild changed for a TMT (parts leaving or joining, pieces per TMT), new and unreadable files."""
        counts, after = self.tracker.st.get("counts") or {}, self.catalog
        b, a = {p["id"]: p for p in before.get("parts") or []}, {p["id"]: p for p in after.get("parts") or []}
        notes = []
        for pid, p in b.items():
            if pid not in a:
                n = counts.get(pid, 0)
                notes.append(f"{p['name']} is no longer in the build" + (f" (its {n} printed stay in the counts)" if n else ""))
        added = [p["name"] for pid, p in a.items() if pid not in b]
        if added:
            notes.append("new in the build: " + ", ".join(added))
        pieces = lambda c: sum(p["qty"] for p in c.get("parts") or [] if not p["optional"] and not p.get("alt"))
        if before.get("parts") and pieces(before) != pieces(after):
            notes.append(f"a TMT is now {pieces(after)} pieces (was {pieces(before)})")
        fs = self.files_summary()
        return {"notes": notes, "new": fs["new"], "errors": [f"{f['file']}: {f['error']}" for f in after.get("files") or [] if f.get("error")]}

    async def h_printer(self, request):
        """Every entity of one printer (states and attributes), for the details pop-up."""
        p = self.printer(request.match_info["key"])
        s, at = (self.ha.states, self.ha.attrs) if self.ha else ({}, {})
        return web.json_response({"states": {e: s.get(e) for e in p["entities"] if e in s},
                                  "attrs": {e: {k: v for k, v in (at.get(e) or {}).items() if k not in HIDDEN_ATTRS}
                                            for e in p["entities"] if e in at}})

    async def h_action(self, request):
        a = await request.json()
        cmd = a.pop("cmd", "")
        fn = {"assign": self.tracker.assign, "adjust": self.tracker.adjust, "undo": self.tracker.undo,
              "target": self.tracker.target, "assembled": self.tracker.assembled,
              "import_counts": self.tracker.import_counts, "assign_custom": self.tracker.assign_custom}.get(cmd)
        if not fn and cmd in Tracker.COMMANDS:
            fn = lambda **kw: self.tracker.command(cmd, **kw)
        if not fn:
            return web.json_response({"ok": False, "error": f"unknown command {cmd!r}"}, status=400)
        try:
            async with self.lock:
                res = fn(**a)
            return web.json_response({"ok": True, **res})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    # ------------------------------------------------------------ camera (through Home Assistant's camera entity)
    def camera_entity(self, key):
        cam = next((e for e in self.printer(key)["entities"] if e.startswith("camera.")), None)
        if not cam or self.ha.states.get(cam) in (None, "unavailable", "unknown"):
            raise web.HTTPConflict(text="the printer's camera isn't available")
        return cam

    async def h_cam_config(self, request):
        cam = self.camera_entity(request.match_info["key"])
        try:
            return web.json_response({"ok": True, **(await self.ha.call(type="camera/webrtc/get_client_config", entity_id=cam) or {})})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    async def h_cam_offer(self, request):
        """WebRTC through Home Assistant (its go2rtc): send the page's offer, wait for the answer and the first
        candidates, and keep the session while the page pings it (later candidates come back with the pings)."""
        cam = self.camera_entity(request.match_info["key"])
        offer = (await request.json()).get("offer")
        got = {"answer": None, "error": None, "candidates": []}
        done = asyncio.Event()

        def on_event(ev):
            kind = ev.get("type")
            if kind == "answer":
                got["answer"] = ev.get("answer"); done.set()
            elif kind == "candidate" and ev.get("candidate"):
                got["candidates"].append(ev["candidate"])
            elif kind == "error":
                got["error"] = ev.get("message") or ev.get("code") or "camera error"; done.set()
        try:
            sid = await self.ha.subscribe(on_event, type="camera/webrtc/offer", entity_id=cam, offer=offer)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})
        try:
            await asyncio.wait_for(done.wait(), 15)
        except asyncio.TimeoutError:
            got["error"] = "no answer from the camera"
        if got["error"]:
            await self.ha.unsubscribe(sid)
            return web.json_response({"ok": False, "error": got["error"]})
        await asyncio.sleep(1)   # candidates usually follow the answer right away
        self.cams[sid] = {"last": time.monotonic(), "got": got, "sent": len(got["candidates"])}
        return web.json_response({"ok": True, "session": sid, "answer": got["answer"], "candidates": got["candidates"]})

    async def h_cam_ping(self, request):
        sid = int(request.match_info["sid"])
        c = self.cams.get(sid)
        if not c:
            return web.json_response({"ok": False, "error": "session closed"})
        c["last"] = time.monotonic()
        new = c["got"]["candidates"][c["sent"]:]
        c["sent"] += len(new)
        return web.json_response({"ok": True, "candidates": new})

    async def h_cam_close(self, request):
        sid = int(request.match_info["sid"])
        if self.cams.pop(sid, None) is not None:
            await self.ha.unsubscribe(sid)
        return web.json_response({"ok": True})

    async def cam_janitor(self):
        """Close camera sessions the page stopped pinging (pop-up closed, tab gone)."""
        while True:
            await asyncio.sleep(20)
            for sid, c in list(self.cams.items()):
                if time.monotonic() - c["last"] > 60:
                    self.cams.pop(sid, None)
                    if self.ha:
                        await self.ha.unsubscribe(sid)

    async def h_cam_hls(self, request):
        cam = self.camera_entity(request.match_info["key"])
        try:
            res = await self.ha.call(type="camera/stream", entity_id=cam, format="hls")
            return web.json_response({"ok": True, "url": res.get("url")})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    async def h_cam_still(self, request):
        cam = self.camera_entity(request.match_info["key"])
        try:
            body = await self.ha.fetch(f"/api/camera_proxy/{cam}")
        except Exception as e:
            raise web.HTTPBadGateway(text=str(e))
        return web.Response(body=body, content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ files
    @staticmethod
    def upload_name(filename):
        return re.sub(r"[^A-Za-z0-9 ._()+&-]", "_", Path(filename or "").name).lstrip(".")

    async def h_upload(self, request):
        """Whole files in one multipart request. Ingress refuses bodies over 16 MiB, so the page uses h_upload_part."""
        reader = await request.multipart()
        saved = []
        while (part := await reader.next()) is not None:
            if not part.filename:
                continue
            name = self.upload_name(part.filename)
            if not name.lower().endswith(".3mf"):
                return web.json_response({"ok": False, "error": f"{name}: only .3mf files"})
            FILES.mkdir(parents=True, exist_ok=True)
            tmp = FILES / (name + ".part")
            with open(tmp, "wb") as f:
                while chunk := await part.read_chunk(1 << 20):
                    f.write(chunk)
            os.replace(tmp, FILES / name)
            saved.append(name)
        async with self.lock:
            await self.rebuild_catalog()
        return web.json_response({"ok": True, "saved": saved, "missing": self.catalog.get("missing")})

    async def h_upload_part(self, request):
        """One piece of a file (raw body), in order: ?upload=<id>&name=<file>&offset=<bytes so far>&size=<file size>.
        Pieces collect in a hidden file next to the 3MFs; the last one moves it into place."""
        q = request.query
        upload, name = q.get("upload", ""), self.upload_name(q.get("name"))
        try:
            offset, size = int(q.get("offset", "")), int(q.get("size", ""))
        except ValueError:
            return web.json_response({"ok": False, "error": "offset and size must be numbers"})
        if not re.fullmatch(r"[a-z0-9]{8,32}", upload):
            return web.json_response({"ok": False, "error": "bad upload id"})
        if not name.lower().endswith(".3mf"):
            return web.json_response({"ok": False, "error": f"{name}: only .3mf files"})
        FILES.mkdir(parents=True, exist_ok=True)
        tmp = FILES / f".upload-{upload}.part"
        have = tmp.stat().st_size if tmp.exists() else 0
        if offset != have and offset != 0:
            return web.json_response({"ok": False, "error": f"{name}: piece at {offset}, expected {have}; upload it again"})
        with open(tmp, "wb" if offset == 0 else "ab") as f:
            async for chunk in request.content.iter_chunked(1 << 20):
                f.write(chunk)
        have = tmp.stat().st_size
        if have > size:
            tmp.unlink()
            return web.json_response({"ok": False, "error": f"{name}: got more than {size} bytes; upload it again"})
        if have < size:
            return web.json_response({"ok": True, "received": have})
        replaced = (FILES / name).exists()
        os.replace(tmp, FILES / name)
        async with self.lock:
            before = self.catalog
            await self.rebuild_catalog()
        res = self.change_notes(before)
        if replaced:
            res["notes"].insert(0, f"{name} replaced the file of the same name (its settings stay)")
        return web.json_response({"ok": True, "received": have, "saved": [name], "missing": self.catalog.get("missing"), **res})

    def drop_stale_uploads(self):
        for tmp in FILES.glob(".upload-*.part"):
            if time.time() - tmp.stat().st_mtime > 86400:
                tmp.unlink()

    async def h_rescan(self, request):
        async with self.lock:
            await self.rebuild_catalog()
        return web.json_response({"ok": True, "missing": self.catalog.get("missing")})

    def static_from(self, folder):
        async def handler(request):
            name = Path(request.match_info["name"]).name
            path = folder / name
            if not path.is_file():
                raise web.HTTPNotFound()
            return web.FileResponse(path, headers={"Cache-Control": "no-cache"})
        return handler

    def page(self, name):
        async def handler(request):
            return web.FileResponse(APP / "static" / name, headers={"Cache-Control": "no-cache"})
        return handler

    # ------------------------------------------------------------ run
    async def run(self):
        await self.rebuild_catalog()
        self.drop_stale_uploads()
        token = os.environ.get("SUPERVISOR_TOKEN")
        s6_env = Path("/run/s6/container_environment/SUPERVISOR_TOKEN")   # if started without with-contenv
        if not token and s6_env.exists():
            token = s6_env.read_text().strip()
        app = web.Application(client_max_size=512 * 1024 * 1024)
        app.add_routes([web.get("/", self.page("index.html")), web.get("/plan", self.page("plan.html")),
                        web.get("/files", self.page("files.html")),
                        web.get("/api/data", self.h_data), web.post("/api/action", self.h_action),
                        web.get("/api/printer/{key}", self.h_printer),
                        web.get("/api/camera/{key}/config", self.h_cam_config), web.post("/api/camera/{key}/offer", self.h_cam_offer),
                        web.post("/api/camera/{key}/hls", self.h_cam_hls), web.get("/api/camera/{key}/still", self.h_cam_still),
                        web.post("/api/camera/session/{sid}/ping", self.h_cam_ping),
                        web.post("/api/camera/session/{sid}/close", self.h_cam_close),
                        web.post("/api/upload", self.h_upload), web.post("/api/upload_part", self.h_upload_part),
                        web.get("/api/files", self.h_files), web.post("/api/files_action", self.h_files_action),
                        web.post("/api/rescan", self.h_rescan),
                        web.get("/thumbs/{name}", self.static_from(DATA / "thumbs")),
                        web.get("/covers/{name}", self.static_from(DATA / "covers"))])
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", 8099).start()
        log.info("TMT Print Tracker %s: dashboard on the ingress port 8099", VERSION)
        async with ClientSession() as session:
            if not token:
                log.error("no SUPERVISOR_TOKEN: is homeassistant_api enabled in config.yaml?")
                await asyncio.Event().wait()
            self.ha = HomeAssistant(session, token)
            asyncio.create_task(self.tick())
            asyncio.create_task(self.cam_janitor())
            await self.ha.run(self.entity_ids, self.on_connect, self.on_change)


def main():
    options = json.loads(OPTIONS.read_text()) if OPTIONS.exists() else {}
    level = getattr(logging, (options.get("log_level") or "info").upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    asyncio.run(App(options).run())


if __name__ == "__main__":
    main()
