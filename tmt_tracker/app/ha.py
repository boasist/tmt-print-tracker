"""Home Assistant over the Supervisor: one websocket (reconnecting) for registry calls, live entity states (with their
attributes), event subscriptions and services, and REST for images. The add-on's SUPERVISOR_TOKEN authenticates both;
no user token is needed."""
import asyncio
import json
import logging

import aiohttp

log = logging.getLogger("tmt.ha")


class HomeAssistant:
    def __init__(self, session, token, base="http://supervisor/core"):
        self.session, self.token, self.base = session, token, base
        self.ws = None
        self._id = 0
        self._waiting = {}
        self._subs = {}           # subscription id -> callback(event) for subscribe()
        self._entities_sub = None
        self.states = {}          # entity_id -> state string, for the subscribed entities
        self.attrs = {}           # entity_id -> attributes
        self.connected = False

    async def run(self, entity_ids_for, on_connect, on_change):
        """Connect, keep the entity subscription live, and reconnect after any drop.
        entity_ids_for() is awaited after each connect to get the list of entities to watch."""
        while True:
            try:
                async with self.session.ws_connect(self.base.replace("http", "ws", 1) + "/websocket", heartbeat=30,
                                                   max_msg_size=64 * 1024 * 1024) as ws:
                    msg = await ws.receive_json()
                    await ws.send_json({"type": "auth", "access_token": self.token})
                    msg = await ws.receive_json()
                    if msg.get("type") != "auth_ok":
                        raise RuntimeError(f"Home Assistant refused the login: {msg}")
                    self.ws, self.connected = ws, True
                    log.info("connected to Home Assistant")
                    reader = asyncio.create_task(self._read(ws, on_change))
                    ids = await entity_ids_for()
                    if ids:
                        self._entities_sub = self._id + 1
                        await self.call(type="subscribe_entities", entity_ids=ids)
                    await on_connect()
                    await reader
            except Exception as e:
                log.warning("Home Assistant connection: %s", e)
            self.ws, self.connected, self._entities_sub = None, False, None
            for fut in self._waiting.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("disconnected"))
            self._waiting.clear()
            self._subs.clear()
            await asyncio.sleep(5)

    async def _read(self, ws, on_change):
        async for raw in ws:
            if raw.type != aiohttp.WSMsgType.TEXT:
                continue
            msg = json.loads(raw.data)
            if msg.get("type") == "result" and msg.get("id") in self._waiting:
                fut = self._waiting.pop(msg["id"])
                if not fut.done():
                    fut.set_result(msg)
            elif msg.get("type") == "event" and msg.get("id") in self._subs:
                try:
                    res = self._subs[msg["id"]](msg.get("event") or {})
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as e:
                    log.warning("event handler: %s", e)
            elif msg.get("type") == "event":
                ev = msg.get("event") or {}
                changed = []
                for eid, v in (ev.get("a") or {}).items():
                    self.states[eid] = v.get("s"); self.attrs[eid] = v.get("a") or {}; changed.append(eid)
                for eid, v in (ev.get("c") or {}).items():
                    plus, minus = v.get("+") or {}, v.get("-") or {}
                    if "s" in plus:
                        self.states[eid] = plus["s"]
                    if plus.get("a") or minus.get("a"):
                        a = self.attrs.setdefault(eid, {})
                        a.update(plus.get("a") or {})
                        for k in minus.get("a") or []:
                            a.pop(k, None)
                    changed.append(eid)
                if changed:
                    await on_change(changed)

    async def call(self, **msg):
        if not self.ws:
            raise ConnectionError("not connected to Home Assistant")
        self._id += 1
        fut = asyncio.get_running_loop().create_future()
        self._waiting[self._id] = fut
        await self.ws.send_json({"id": self._id, **msg})
        res = await asyncio.wait_for(fut, 30)
        if not res.get("success"):
            raise RuntimeError(f"{msg.get('type')}: {res.get('error')}")
        return res.get("result")

    async def subscribe(self, on_event, **msg):
        """A subscription (subscribe_events, camera/webrtc/offer, ...) whose events go to on_event. Returns its id."""
        if not self.ws:
            raise ConnectionError("not connected to Home Assistant")
        self._id += 1
        sid = self._id
        self._subs[sid] = on_event
        fut = asyncio.get_running_loop().create_future()
        self._waiting[sid] = fut
        await self.ws.send_json({"id": sid, **msg})
        res = await asyncio.wait_for(fut, 30)
        if not res.get("success"):
            self._subs.pop(sid, None)
            raise RuntimeError(f"{msg.get('type')}: {res.get('error')}")
        return sid

    async def unsubscribe(self, sid):
        if self._subs.pop(sid, None) is not None and self.ws:
            try:
                await self.call(type="unsubscribe_events", subscription=sid)
            except Exception as e:
                log.debug("unsubscribe %s: %s", sid, e)

    async def service(self, domain, service, data=None):
        return await self.call(type="call_service", domain=domain, service=service, service_data=data or {})

    async def fetch(self, path):
        async with self.session.get(self.base + path, headers={"Authorization": f"Bearer {self.token}"},
                                    timeout=aiohttp.ClientTimeout(total=30)) as r:
            r.raise_for_status()
            return await r.read()

    async def printers(self):
        """Every Bambu Lab printer (its print_status sensor) and every Moonraker printer, e.g. Klipper or a Snapmaker U1
        (its current_print_state sensor), as [{name, prefix, type, model}]."""
        ents = await self.call(type="config/entity_registry/list")
        devs = {d["id"]: d for d in await self.call(type="config/device_registry/list")}
        kinds = {"bambu_lab": ("bambu", "_print_status"), "moonraker": ("moonraker", "_current_print_state")}
        found = []
        for e in ents:
            eid, kind = e["entity_id"], kinds.get(e.get("platform"))
            if kind and eid.startswith("sensor.") and eid.endswith(kind[1]) and not e.get("disabled_by"):
                d = devs.get(e.get("device_id")) or {}
                prefix = eid[len("sensor."):-len(kind[1])]
                found.append({"name": d.get("name_by_user") or d.get("name") or prefix, "prefix": prefix, "type": kind[0],
                              "model": d.get("model") or ""})
        return sorted(found, key=lambda p: p["name"])

    async def printer_entities(self, prefix, platform="bambu_lab"):
        """A printer's entities: its integration's sensors, binary sensors, lights, images and cameras named
        <domain>.<prefix>_..."""
        ents = await self.call(type="config/entity_registry/list")
        doms = ("sensor.", "binary_sensor.", "light.", "image.", "camera.")
        return sorted(e["entity_id"] for e in ents if e.get("platform") == platform and not e.get("disabled_by")
                      and e["entity_id"].startswith(doms) and e["entity_id"].split(".", 1)[1].startswith(prefix + "_"))
