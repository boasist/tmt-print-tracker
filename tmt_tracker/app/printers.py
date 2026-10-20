"""Printer types: which Home Assistant entities a printer has, and its live values in the Bambu Lab integration's terms
(print_status running/pause/finish/failed/idle, progress in %, remaining_time in hours), which the tracker and the
dashboard use for every printer.

- bambu: the Bambu Lab integration (greghesp/ha-bambulab).
- moonraker: the Moonraker integration (marcolivierarsenault/moonraker), for Klipper printers such as the Snapmaker U1.
"""
import re

LIVE = ("print_status", "task_name", "gcode_filename", "print_progress", "remaining_time", "current_layer",
        "total_layer_count", "end_time", "current_stage", "start_time")
JOB = ("print_status", "task_name", "gcode_filename", "current_stage")

MOONRAKER_STATUS = {"standby": "idle", "printing": "running", "paused": "pause", "complete": "finish",
                    "cancelled": "failed", "error": "failed"}


def _strip_gcode(name):
    """'sub/3DTMT F SEAT BACK REV A_PETG_9h44m.gcode' -> '3DTMT F SEAT BACK REV A_PETG_9h44m'"""
    return re.sub(r"\.(bgcode|gcode|gco|g)$", "", (name or "").rsplit("/", 1)[-1], flags=re.I)


def _round(v):
    try:
        return str(round(float(v)))
    except (TypeError, ValueError):
        return v


TYPES = {
    "bambu": {
        "sensors": {k: k for k in LIVE},
        "cover": ("image.{}_cover_image", "/api/image_proxy/"),
        "convert": {},
        "cover_stamp": None,     # the image entity's state changes with each new preview
        "platform": "bambu_lab",
    },
    "moonraker": {
        "sensors": {"print_status": "current_print_state", "task_name": "filename", "gcode_filename": "filename",
                    "print_progress": "progress", "remaining_time": "print_time_left", "current_layer": "current_layer",
                    "total_layer_count": "total_layer", "end_time": "print_eta"},
        "cover": ("camera.{}_thumbnail", "/api/camera_proxy/"),
        "convert": {"print_status": lambda v: MOONRAKER_STATUS.get(v, v), "task_name": _strip_gcode,
                    "print_progress": _round},
        "cover_stamp": "gcode_filename",   # the thumbnail camera's state stays "idle"; a new file means a new picture
        "platform": "moonraker",
    },
}


class Printer:
    def __init__(self, name, prefix, type="bambu", model=""):
        self.name, self.prefix, self.model = name, prefix, model or ""
        self.type = type if type in TYPES else "bambu"
        self.t = TYPES[self.type]
        self.entities = []       # all of the printer's entities in its integration (details pop-up, filament, nozzles)

    # p["prefix"], p.get("entities"): the planner and printer-details code reads printers as dicts
    def __getitem__(self, key):
        return getattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)

    @property
    def platform(self):
        return self.t["platform"]

    @property
    def camera(self):
        """The printer's camera; a Moonraker thumbnail 'camera' is the plate preview, not a camera."""
        return next((e for e in self.entities if e.startswith("camera.") and e != self.cover_entity), None)

    def entity(self, key):
        s = self.t["sensors"].get(key)
        return f"sensor.{self.prefix}_{s}" if s else None

    @property
    def cover_entity(self):
        return self.t["cover"][0].format(self.prefix)

    @property
    def cover_url(self):
        return self.t["cover"][1] + self.cover_entity

    def entity_ids(self):
        return sorted({e for e in map(self.entity, LIVE) if e}) + [self.cover_entity]

    def job_entities(self):
        return {e for e in map(self.entity, JOB) if e}

    def live(self, states):
        out = {}
        for k in LIVE:
            e = self.entity(k)
            v = states.get(e) if e else None
            out[k] = self.t["convert"][k](v) if v is not None and k in self.t["convert"] else v
        return out

    def live_info(self, states, attrs):
        """What the tracker needs beyond the job sensors: filament types loaded (AMS trays with a spool, plus the
        external spools' set type, whose 'empty' flag isn't reliable), nozzles per side, the slicer's remaining time,
        progress, stage, the filament in use, and the printer's job start time. A Moonraker printer reports no
        filament or nozzles (materials None: unknown)."""
        live = self.live(states)
        if self.type != "bambu":
            return {"materials": None, "nozzles": None, "remaining": live["remaining_time"],
                    "progress": live["print_progress"], "stage": None, "start_time": None, "filament": {}}
        s, at, prefix = states, attrs, self.prefix
        mats = set()
        for eid in self.entities:
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

    def cover_at(self, states):
        """Changes whenever the plate preview may have changed (None while there is no preview)."""
        v = states.get(self.cover_entity)
        stamp = self.t["cover_stamp"]
        if stamp and v not in (None, "unavailable"):
            return states.get(self.entity(stamp))
        return v
