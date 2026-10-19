"""Printer types: which Home Assistant entities a printer has, and its live values in the Bambu Lab integration's terms
(print_status running/pause/finish/failed/idle, progress in %, remaining_time in hours), which the tracker and the
dashboard use for every printer.

- bambu: the Bambu Lab integration (greghesp/ha-bambulab).
- moonraker: the Moonraker integration (marcolivierarsenault/moonraker), for Klipper printers such as the Snapmaker U1.
"""
import re

LIVE = ("print_status", "task_name", "gcode_filename", "print_progress", "remaining_time", "current_layer",
        "total_layer_count", "end_time", "current_stage")
JOB = ("print_status", "task_name", "gcode_filename")

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
    },
    "moonraker": {
        "sensors": {"print_status": "current_print_state", "task_name": "filename", "gcode_filename": "filename",
                    "print_progress": "progress", "remaining_time": "print_time_left", "current_layer": "current_layer",
                    "total_layer_count": "total_layer", "end_time": "print_eta"},
        "cover": ("camera.{}_thumbnail", "/api/camera_proxy/"),
        "convert": {"print_status": lambda v: MOONRAKER_STATUS.get(v, v), "task_name": _strip_gcode,
                    "print_progress": _round},
        "cover_stamp": "gcode_filename",   # the thumbnail camera's state stays "idle"; a new file means a new picture
    },
}


class Printer:
    def __init__(self, name, prefix, type="bambu"):
        self.name, self.prefix = name, prefix
        self.type = type if type in TYPES else "bambu"
        self.t = TYPES[self.type]

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

    def cover_at(self, states):
        """Changes whenever the plate preview may have changed (None while there is no preview)."""
        v = states.get(self.cover_entity)
        stamp = self.t["cover_stamp"]
        if stamp and v not in (None, "unavailable"):
            return states.get(self.entity(stamp))
        return v
