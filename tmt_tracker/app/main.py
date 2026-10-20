"""TMT Print Tracker add-on: watches Bambu Lab and Moonraker (e.g. Snapmaker U1) printers in Home Assistant, works out
which plate of which 3MF each one is printing, counts the parts of finished plates toward builds, keeps a print queue
per printer, and serves the dashboard and the build planner over ingress."""
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
from printers import Printer
from tracker import Tracker

VERSION = "0.3.1"
APP = Path(__file__).resolve().parent
DATA = Path("/data")                  # add-on private storage (state, thumbnails, covers)
CONFIG = Path("/config")              # this add-on's folder in addon_configs: profiles/ and files/
FILES = CONFIG / "files"
OPTIONS = DATA / "options.json"
CLEAR_ACTION = "TMT_TRACKER_CLEAR_"   # the alert's "Plate is clear" button: CLEAR_ACTION + printer key in capitals
TICK_S = 300                          # also look at every printer this often (filament loaded, nozzle, estimates)
HIDDEN_ATTRS = ("access_token", "entity_picture")

log = logging.getLogger("tmt")


class App:
    def __init__(self, options):
        self.options = options
        self.tracker = Tracker(DATA)
        self.lock = asyncio.Lock()
        self.printers = []            # [Printer]
        self.catalog = {}
        self.profile_path = None
        self.profile_error = None
        self.ha = None
        self._pending = {}
        self.cams = {}                # camera WebRTC sessions: subscription id -> {printer, last, candidates}

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

    def rebuild_catalog(self):
        FILES.mkdir(parents=True, exist_ok=True)
        try:
            self.profile_path = self.resolve_profile()
            profile = catalog_mod.load_profile(self.profile_path)
            self.catalog = catalog_mod.build(profile, FILES, DATA / "thumbs")
            self.profile_error = None
        except Exception as e:
            log.error("profile %s: %s", self.profile_path, e)
            self.profile_error = str(e)
            self.catalog = {"profile": "", "parts": [], "alternatives": [], "sources": [], "files": [], "missing": []}
        self.tracker.set_catalog(self.catalog)

    async def entity_ids(self):
        found = await self.ha.printers()
        configured = self.options.get("printers") or []
        if configured:
            models = {p["prefix"]: p["model"] for p in found}
            printers = [Printer(p["name"], p["entity_prefix"], p.get("type") or "bambu", models.get(p["entity_prefix"], ""))
                        for p in configured]
        else:
            printers = [Printer(**p) for p in found]
        for p in printers:
            p.entities = await self.ha.printer_entities(p.prefix, p.platform)
        self.printers = printers
        log.info("printers: %s", ", ".join(f"{p.name} ({p.type} {p.prefix}, {p.model or 'model?'}, {len(p.entities)} entities)"
                                           for p in self.printers) or "none found")
        self.tracker.set_printers({p.prefix: {"name": p.name, "model": p.model} for p in self.printers})
        ids = set()
        for p in self.printers:
            ids |= set(p.entities) | set(p.entity_ids())
        return sorted(ids)

    def printer(self, key):
        p = next((p for p in self.printers if p.prefix == key), None)
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
            await self.process(p, cover_changed=True)

    async def rematch(self):
        """After the files changed: look again at what each printer is printing (a job seen before its file was
        uploaded is recognised now)."""
        if self.ha and self.ha.connected:
            for p in self.printers:
                await self.process(p, cover_changed=True)

    async def on_change(self, entity_ids):
        changed = set(entity_ids)
        for p in self.printers:
            job = bool(p.job_entities() & changed)
            cover = p.cover_entity in changed or (p.type != "bambu" and p.entity("gcode_filename") in changed)
            if job or cover:
                # A finish usually changes several entities at once; handle them together.
                prev = self._pending.get(p.prefix)
                if prev and not prev.done():
                    prev.cancel()
                self._pending[p.prefix] = asyncio.create_task(self._debounced(p, cover))

    async def _debounced(self, printer, cover):
        await asyncio.sleep(1.5)
        await self.process(printer, cover)

    async def tick(self):
        while True:
            await asyncio.sleep(TICK_S)
            if self.ha and self.ha.connected:
                for p in self.printers:
                    try:
                        await self.process(p)
                    except Exception as e:
                        log.warning("%s: %s", p.prefix, e)

    def live_info(self, printer):
        info = printer.live_info(self.ha.states, self.ha.attrs)
        if info["materials"] is None:   # no filament sensors (Moonraker): assume every filament the build uses
            info["materials"] = sorted({(p.get("material") or "").upper() for p in self.catalog.get("parts", [])} - {""})
        return info

    async def process(self, printer, cover_changed=False):
        prefix = printer.prefix
        live = printer.live(self.ha.states)
        cover_at = printer.cover_at(self.ha.states)
        cover_path = DATA / "covers" / f"{prefix}.png"
        if cover_changed and cover_at not in (None, "unknown", "unavailable"):
            try:
                cover_path.parent.mkdir(parents=True, exist_ok=True)
                cover_path.write_bytes(await self.ha.fetch(printer.cover_url))
            except Exception as e:
                log.warning("%s: couldn't fetch the plate preview: %s", prefix, e)
        async with self.lock:
            res = self.tracker.update(prefix, live["print_status"], live["task_name"], live["gcode_filename"],
                                      str(cover_path), cover_at, self.live_info(printer))
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
        if not any(p.prefix == key for p in self.printers):
            return
        async with self.lock:
            res = self.tracker.command("bed_clear", printer=key)
        log.info("%s: plate cleared from the phone alert", key)
        if res.get("notify"):
            await self.alert(res["notify"], clear_button=False)

    # ------------------------------------------------------------ web
    def live(self):
        s = self.ha.states if self.ha else {}
        return [{"key": p.prefix, "name": p.name, "type": p.type, "model": p.model, "camera": p.camera,
                 "live": p.live(s)} for p in self.printers]

    async def h_data(self, request):
        return web.json_response({
            "version": VERSION, "connected": bool(self.ha and self.ha.connected),
            "profile": self.catalog.get("profile"), "profile_path": str(self.profile_path), "profile_error": self.profile_error,
            "catalog": {k: self.catalog.get(k) for k in ("parts", "alternatives", "sources", "files", "missing")},
            "state": self.tracker.published(), "alerts": bool(self.notify_targets()),
            "summary": self.tracker.summary(), "projects": self.tracker.project_summaries(), "printers": self.live()})

    async def h_printer(self, request):
        """Every entity of one printer (states and attributes), for the details pop-up."""
        p = self.printer(request.match_info["key"])
        s, at = (self.ha.states, self.ha.attrs) if self.ha else ({}, {})
        return web.json_response({"states": {e: s.get(e) for e in p.entities if e in s},
                                  "attrs": {e: {k: v for k, v in (at.get(e) or {}).items() if k not in HIDDEN_ATTRS}
                                            for e in p.entities if e in at}})

    async def h_action(self, request):
        a = await request.json()
        cmd = a.pop("cmd", "")
        fn = {"assign": self.tracker.assign, "adjust": self.tracker.adjust, "undo": self.tracker.undo,
              "target": self.tracker.target, "assembled": self.tracker.assembled,
              "import_counts": self.tracker.import_counts, "project_add": self.tracker.project_add,
              "project_update": self.tracker.project_update, "project_delete": self.tracker.project_delete,
              "printer_project": self.tracker.printer_project, "move_print": self.tracker.move_print,
              "fill_from_pool": self.tracker.fill_from_pool, "assign_custom": self.tracker.assign_custom,
              "take_from_pool": self.tracker.take_from_pool}.get(cmd)
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
        cam = self.printer(key).camera
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
    async def h_upload(self, request):
        reader = await request.multipart()
        saved = []
        while (part := await reader.next()) is not None:
            if not part.filename:
                continue
            name = re.sub(r"[^A-Za-z0-9 ._()+&-]", "_", Path(part.filename).name)
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
            self.rebuild_catalog()
        await self.rematch()
        return web.json_response({"ok": True, "saved": saved, "missing": self.catalog.get("missing")})

    async def h_upload_chunk(self, request):
        """One piece of a large upload (Home Assistant's ingress refuses bodies over 16 MB): ?name=&offset=&total=.
        Pieces arrive in order; the file is checked and the catalog rebuilt after the last one."""
        q = request.query
        name = re.sub(r"[^A-Za-z0-9 ._()+&-]", "_", Path(q.get("name", "")).name)
        if not name.lower().endswith(".3mf"):
            return web.json_response({"ok": False, "error": f"{name}: only .3mf files"})
        offset, total = int(q.get("offset", 0)), int(q.get("total", 0))
        FILES.mkdir(parents=True, exist_ok=True)
        tmp = FILES / (name + ".part")
        if offset == 0:
            tmp.unlink(missing_ok=True)
        elif not tmp.exists() or tmp.stat().st_size != offset:
            return web.json_response({"ok": False, "error": "upload out of order; start again"})
        with open(tmp, "ab") as f:
            async for chunk in request.content.iter_chunked(1 << 20):
                f.write(chunk)
        size = tmp.stat().st_size
        if size < total:
            return web.json_response({"ok": True, "received": size})
        if size != total:
            tmp.unlink()
            return web.json_response({"ok": False, "error": f"{name}: got {size} bytes, expected {total}"})
        os.replace(tmp, FILES / name)
        async with self.lock:
            self.rebuild_catalog()
        await self.rematch()
        return web.json_response({"ok": True, "saved": [name], "missing": self.catalog.get("missing")})

    async def h_delete_file(self, request):
        name = Path((await request.json()).get("file", "")).name
        path = FILES / name
        if not name.lower().endswith(".3mf") or not path.exists():
            return web.json_response({"ok": False, "error": "no such file"})
        path.unlink()
        async with self.lock:
            self.rebuild_catalog()
        await self.rematch()
        return web.json_response({"ok": True})

    async def h_rescan(self, request):
        async with self.lock:
            self.rebuild_catalog()
        await self.rematch()
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
        self.rebuild_catalog()
        token = os.environ.get("SUPERVISOR_TOKEN")
        s6_env = Path("/run/s6/container_environment/SUPERVISOR_TOKEN")   # if started without with-contenv
        if not token and s6_env.exists():
            token = s6_env.read_text().strip()
        app = web.Application(client_max_size=512 * 1024 * 1024)
        app.add_routes([web.get("/", self.page("index.html")), web.get("/plan", self.page("plan.html")),
                        web.get("/api/data", self.h_data), web.post("/api/action", self.h_action),
                        web.get("/api/printer/{key}", self.h_printer),
                        web.get("/api/camera/{key}/config", self.h_cam_config), web.post("/api/camera/{key}/offer", self.h_cam_offer),
                        web.post("/api/camera/{key}/hls", self.h_cam_hls), web.get("/api/camera/{key}/still", self.h_cam_still),
                        web.post("/api/camera/session/{sid}/ping", self.h_cam_ping),
                        web.post("/api/camera/session/{sid}/close", self.h_cam_close),
                        web.post("/api/upload", self.h_upload), web.post("/api/upload_chunk", self.h_upload_chunk), web.post("/api/delete_file", self.h_delete_file),
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
