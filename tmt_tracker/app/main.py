"""TMT Print Tracker add-on: watches Bambu Lab and Moonraker (e.g. Snapmaker U1) printers in Home Assistant, works out which plate of which 3MF each one
is printing, counts the parts of finished plates toward builds, and serves the dashboard over ingress."""
import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path

from aiohttp import ClientSession, web

import catalog as catalog_mod
from ha import HomeAssistant
from printers import Printer
from tracker import Tracker

VERSION = "0.2.0"
APP = Path(__file__).resolve().parent
DATA = Path("/data")                  # add-on private storage (state, thumbnails, covers)
CONFIG = Path("/config")              # this add-on's folder in addon_configs: profiles/ and files/
FILES = CONFIG / "files"
OPTIONS = DATA / "options.json"

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
        configured = [{"name": p["name"], "prefix": p["entity_prefix"], "type": p.get("type") or "bambu"}
                      for p in self.options.get("printers") or []]
        self.printers = [Printer(**p) for p in configured or await self.ha.printers()]
        log.info("printers: %s", ", ".join(f"{p.name} ({p.type} {p.prefix})" for p in self.printers) or "none found")
        return [e for p in self.printers for e in p.entity_ids()]

    # ------------------------------------------------------------ printer events
    async def on_connect(self):
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
                                      str(cover_path), cover_at)
        log.debug("%s: %s", prefix, res)

    # ------------------------------------------------------------ web
    def live(self):
        s = self.ha.states if self.ha else {}
        return [{"key": p.prefix, "name": p.name, "type": p.type, "live": p.live(s)} for p in self.printers]

    async def h_data(self, request):
        st = self.tracker.st
        return web.json_response({
            "version": VERSION, "connected": bool(self.ha and self.ha.connected),
            "profile": self.catalog.get("profile"), "profile_path": str(self.profile_path), "profile_error": self.profile_error,
            "catalog": {k: self.catalog.get(k) for k in ("parts", "alternatives", "sources", "files", "missing")},
            "state": {k: st.get(k) for k in ("target", "assembled", "counts", "printers", "log", "updated")},
            "summary": self.tracker.summary(), "printers": self.live()})

    async def h_action(self, request):
        a = await request.json()
        cmd = a.pop("cmd", "")
        fn = {"assign": self.tracker.assign, "adjust": self.tracker.adjust, "undo": self.tracker.undo,
              "target": self.tracker.target, "assembled": self.tracker.assembled,
              "import_counts": self.tracker.import_counts}.get(cmd)
        if not fn:
            return web.json_response({"ok": False, "error": f"unknown command {cmd!r}"}, status=400)
        try:
            async with self.lock:
                res = fn(**a)
            return web.json_response({"ok": True, **res})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

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
        return web.json_response({"ok": True, "saved": saved, "missing": self.catalog.get("missing")})

    async def h_delete_file(self, request):
        name = Path((await request.json()).get("file", "")).name
        path = FILES / name
        if not name.lower().endswith(".3mf") or not path.exists():
            return web.json_response({"ok": False, "error": "no such file"})
        path.unlink()
        async with self.lock:
            self.rebuild_catalog()
        return web.json_response({"ok": True})

    async def h_rescan(self, request):
        async with self.lock:
            self.rebuild_catalog()
        return web.json_response({"ok": True, "missing": self.catalog.get("missing")})

    def static_from(self, folder):
        async def handler(request):
            name = Path(request.match_info["name"]).name
            path = folder / name
            if not path.is_file():
                raise web.HTTPNotFound()
            return web.FileResponse(path, headers={"Cache-Control": "no-cache"})
        return handler

    async def h_index(self, request):
        return web.FileResponse(APP / "static" / "index.html", headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------ run
    async def run(self):
        self.rebuild_catalog()
        token = os.environ.get("SUPERVISOR_TOKEN")
        s6_env = Path("/run/s6/container_environment/SUPERVISOR_TOKEN")   # if started without with-contenv
        if not token and s6_env.exists():
            token = s6_env.read_text().strip()
        app = web.Application(client_max_size=512 * 1024 * 1024)
        app.add_routes([web.get("/", self.h_index), web.get("/api/data", self.h_data), web.post("/api/action", self.h_action),
                        web.post("/api/upload", self.h_upload), web.post("/api/delete_file", self.h_delete_file),
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
            await self.ha.run(self.entity_ids, self.on_connect, self.on_change)


def main():
    options = json.loads(OPTIONS.read_text()) if OPTIONS.exists() else {}
    level = getattr(logging, (options.get("log_level") or "info").upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    asyncio.run(App(options).run())


if __name__ == "__main__":
    main()
