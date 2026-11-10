# /// script
# requires-python = ">=3.12"
#
# [tool.orcaslicer.plugin]
# name = "OrcaSlicer MCP"
# description = "Lets an AI assistant read your OrcaSlicer setup, change settings and read the slices you make, through the orcaslicer-mcp server. Loading models, starting a slice, plates, renders and projects need the OrcaSlicer MCP build."
# author = "Max Ellis"
# version = "0.2.1"
# ///
"""OrcaSlicer MCP: part of the OrcaSlicer MCP Remote API, served by a plugin on stock OrcaSlicer 2.5.

The orcaslicer-mcp server talks to this plugin exactly as it talks to the OrcaSlicer MCP build: same
routes, same JSON, same X-Api-Token header. The plugin can only do what OrcaSlicer's plugin API allows:

  can     read the settings, presets, the objects on the plate and the project's name and state;
          change settings (unsaved edits, visible in the settings tabs); switch presets
          report what the user changed by hand since the last request (GET /user_changes)
          read the slice the user makes with Slice plate: time, filament, layers and the G-code
          (GET /slice/status, GET /gcode), from OrcaSlicer's lifecycle events and its temp G-code file
  cannot  load, move or delete models; start a slice; see slice warnings or the per-feature breakdown;
          render the plate; manage plates; open or save projects; save, rename or delete presets

Every other route answers 404 with "backend": "plugin", so orcaslicer-mcp can say the tool needs the
OrcaSlicer MCP build.

How it runs:
  * on_load starts one HTTP server thread on 127.0.0.1 (port 13130 unless settings.json in the plugin
    folder says otherwise). The token is in token.txt in the same folder and shown in the panel.
  * OrcaSlicer's model and presets may only be read on the UI thread, and a plugin can only run Python
    there from a UI callback. The "OrcaSlicer MCP" panel is that callback: the server thread queues a
    read, posts to the panel, the panel's page posts back, and on_message (UI thread) runs the read.
  * The panel is the shared OrcaSlicer MCP timeline page (the same file the OrcaSlicer MCP build
    uses), fed by PanelHost: what the assistant changed, grouped in bursts, with single undo.
    The token, the port and the on/off switch live in the plugin's Config tab
    (Plugins > OrcaSlicer MCP > Config). At startup the first request opens the panel.
  * Settings writes go through OrcaSlicer's preset transaction API, which handles its own threading.
  * Script, G-code, printer-connection and plugin settings are never written (the fork's
    sensitive-key policy, without the fork's Preferences opt-in); preset credentials are redacted.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import socket
import socketserver
import sys
import threading
import time
import types
import urllib.parse

import orca

PLUGIN_VERSION = "0.2.1"
DEFAULT_PORT = 13130
BIND_HOST = "127.0.0.1"
MAX_BODY = 4 * 1024 * 1024
UI_TIMEOUT_S = 10.0
CAPABILITIES = ["status", "config", "objects", "preset_select", "preset_read", "project_read",
                "sensitive_key_policy", "slice_read"]
FORK_ONLY = "needs the OrcaSlicer MCP build: the stock-OrcaSlicer plugin cannot do this"

# Diagnostics: plugin-trace.log in the plugin's data folder, on when settings.json has "trace": true.
_TRACE_PATH = None
_TRACE_LOCK = threading.Lock()


def trace(msg: str) -> None:
    if not _TRACE_PATH:
        return
    line = f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} [{threading.current_thread().name}] {msg}\n"
    with _TRACE_LOCK:
        try:
            with open(_TRACE_PATH, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

# ---------------------------------------------------------------------------------------------
# Sensitive keys. Same rule as orcaslicer-mcp's guard.py and the fork's is_sensitive_config_key();
# tests/test_guard_drift.py keeps the copies in step.
# ---------------------------------------------------------------------------------------------
PHYSICAL_PRINTER_KEYS = frozenset({
    "printer_technology", "bbl_use_printhost", "host_type", "printer_agent", "print_host",
    "print_host_webui", "printhost_apikey", "flashforge_serial_number", "printhost_cafile",
    "printhost_port", "printhost_authorization_type", "printhost_user", "printhost_password",
    "printhost_ssl_ignore_revoke",
})
BLOCKED_EXACT = PHYSICAL_PRINTER_KEYS | {"post_process", "printer_model", "filename_format",
                                         "slicing_pipeline_plugin", "plugins"}
_GCODE_SUFFIX_EXEMPT = frozenset({"emit_machine_limits_to_gcode", "high_precision_gcode",
                                  "skip_modified_gcodes"})
# PresetBundle::full_config_secure() erases these; GET /config hides them the same way.
UNREADABLE_KEYS = frozenset({
    "print_host", "print_host_webui", "printhost_apikey", "printhost_cafile",
    "printhost_user", "printhost_password", "printhost_port",
})
CREDENTIAL_KEYS = frozenset({"printhost_apikey", "printhost_password", "printhost_user"})
URL_KEYS = frozenset({"print_host", "print_host_webui"})
REDACTED = "<redacted>"
_URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")


def is_sensitive(key: str) -> bool:
    if key in BLOCKED_EXACT or key.startswith("printhost_"):
        return True
    return key.endswith(("_gcode", "_gcodes")) and key not in _GCODE_SUFFIX_EXEMPT


def strip_userinfo(url: str) -> str:
    """Cut everything between the scheme and the LAST '@' (a password may hold '@', '/', ...)."""
    at = url.rfind("@")
    if at < 0:
        return url
    scheme = _URL_SCHEME.match(url)
    return (scheme.group(0) if scheme else "") + REDACTED + url[at:]


def redact_value(key: str, value):
    if key in CREDENTIAL_KEYS and value not in (None, ""):
        return REDACTED
    if key in URL_KEYS and isinstance(value, str):
        return strip_userinfo(value)
    return value


def as_config_text(v) -> str | None:
    """The text the fork's PUT /config would deserialize for a JSON value, or None if refused."""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return format(v, "g")
    if isinstance(v, str):
        return v
    return None


# ---------------------------------------------------------------------------------------------
# Errors that map to HTTP replies
# ---------------------------------------------------------------------------------------------
class ApiReply(Exception):
    def __init__(self, status: int, body: dict):
        super().__init__(body.get("error", ""))
        self.status = status
        self.body = body


def _not_found() -> ApiReply:
    return ApiReply(404, {"error": "not_found", "backend": "plugin", "detail": FORK_ONLY})


# ---------------------------------------------------------------------------------------------
# The user's own slice. A plugin cannot start a slice, but OrcaSlicer tells every plugin when one
# starts, where its G-code went and how it ended (lifecycle events), and the G-code's closing
# comments carry the time, filament and layer count. So once the user presses Slice, the plugin
# serves /slice/status and /gcode the way the build does.
# ---------------------------------------------------------------------------------------------
SLICE_TAIL_BYTES = 64 * 1024
SLICE_HEAD_BYTES = 16 * 1024
SLICE_IDLE_TEXT = ("Nothing sliced yet: ask the user to press Slice plate (top right of the OrcaSlicer window), "
                   "then read the result again.")
SLICE_DONE_TEXT = ("Read from the G-code of the slice made in OrcaSlicer. The plugin can't see OrcaSlicer's slice "
                   "warnings, so ask the user to check the window for any. It also can't see plate settings or more "
                   "paint on an already painted part: if the user changed those after slicing, ask them to slice "
                   "again.")
SLICE_STALE_TEXT = "out of date: something changed since this slice; slice again for current numbers"
SLICE_OUTSIDE_TEXT = ("OrcaSlicer saved this slice outside the folder the plugin may read, so the plugin can't "
                      "read it")
SLICE_GONE_TEXT = "the G-code of this slice is gone: ask the user to slice again"
SLICE_BUSY_TEXT = "the G-code of this slice couldn't be read just now (OrcaSlicer may be writing it): try again"
SLICE_RUNNING_TEXT = "OrcaSlicer is slicing"
SLICE_FAILED_TEXT = "Slicing failed"

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)([dhms])")
_DURATION_UNIT = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_HEADER_TIME = re.compile(r"^;.*total estimated time:\s*(.*?)\s*$", re.M)
_HEADER_LAYERS = re.compile(r"^;\s*total layer number:\s*(\d+)\s*$", re.M)
_SUMMARY_LINE = re.compile(r"^;\s*(filament used \[mm\]|filament used \[g\]|total filament used \[g\]|"
                           r"total filament cost|total layers count|estimated printing time \(normal mode\))"
                           r"\s*=\s*(.*?)\s*$", re.M)


def parse_duration(text: str) -> float | None:
    """OrcaSlicer's time text ("1d 2h 3m 4s", "6m 42s", "0.93s") in seconds; None for anything else."""
    parts = (text or "").split()
    if not parts:
        return None
    total = 0.0
    for part in parts:
        m = _DURATION_PART.fullmatch(part)
        if m is None:
            return None
        total += float(m[1]) * _DURATION_UNIT[m[2]]
    return total


def _numbers(text: str) -> list[float]:
    out = []
    for part in text.split(","):
        try:
            out.append(float(part))
        except ValueError:
            pass
    return out


def parse_gcode_summary(tail: str, head: str = "") -> dict:
    """The build's /slice/status stats, from the comments OrcaSlicer writes at the end of the G-code (and, for
    Bambu printers, which get no time or layer count there, at the start): keys estimated_time (OrcaSlicer's text),
    estimated_time_seconds, filament_used_mm and filament_used_g (summed over filaments), total_cost, plus layers.
    Only what the file says; {} when it says nothing."""
    found = {k: v for k, v in _SUMMARY_LINE.findall(tail)}
    out: dict = {}
    text = found.get("estimated printing time (normal mode)")
    if not text:
        m = _HEADER_TIME.search(head)
        text = m[1] if m else ""
    if text:
        out["estimated_time"] = text
        seconds = parse_duration(text)
        if seconds is not None:
            out["estimated_time_seconds"] = seconds
    mm = _numbers(found.get("filament used [mm]", ""))
    if mm:
        out["filament_used_mm"] = round(sum(mm), 2)
    grams = _numbers(found.get("total filament used [g]", "")) or _numbers(found.get("filament used [g]", ""))
    if grams:
        out["filament_used_g"] = round(sum(grams), 2)
    cost = _numbers(found.get("total filament cost", ""))
    if cost:
        out["total_cost"] = cost[0]
    layers = _numbers(found.get("total layers count", ""))
    if not layers:
        m = _HEADER_LAYERS.search(head)
        layers = [float(m[1])] if m else []
    if layers:
        out["layers"] = int(layers[0])
    return out


def slice_folders(env, windows: bool, uid, isdir=os.path.isdir) -> list[str]:
    """Where OrcaSlicer writes the G-code of a slice: <temp>/orcaslicer_model, <temp> being wxFileName::GetTempDir()
    (the first of TMPDIR, TMP, TEMP that names an existing folder, then the system's: GetTempPath falls back to
    USERPROFILE on Windows, "/tmp" elsewhere) with "/orcaslicer_<uid>" after it off Windows (OrcaSlicer.cpp
    per_user_temp_dir). [] when it can't be told."""
    names = ("TMPDIR", "TMP", "TEMP") if not windows else ("TMPDIR", "TMP", "TEMP", "USERPROFILE")
    base = next((env.get(n) for n in names if env.get(n) and (n == "USERPROFILE" or isdir(env.get(n)))), None)
    base = base or ("" if windows else "/tmp")
    if not base:
        return []
    seps = "\\/" if windows else "/"
    base = base.rstrip(seps) or base[:1]
    sep = "\\" if windows else "/"
    if not windows:
        base = base.rstrip("/") + "/orcaslicer_" + str(uid)
    return [base.rstrip(seps) + sep + "orcaslicer_model"]


def current_slice_folders() -> list[str]:
    windows = sys.platform == "win32"
    return slice_folders(os.environ, windows, None if windows else os.getuid())


def in_slice_folder(path: str, folders, pathmod=os.path) -> bool:
    """path lies inside one of the folders (after resolving "..", without touching the disk). pathmod: ntpath or
    posixpath, for tests."""
    if not path or not pathmod.isabs(path):
        return False
    p = pathmod.normcase(pathmod.normpath(path))
    for f in folders:
        root = pathmod.normcase(pathmod.normpath(f))
        if p.startswith(root.rstrip(pathmod.sep) + pathmod.sep):
            return True
    return False


def _code_name(code) -> str:
    return getattr(code, "name", str(code)).rsplit(".", 1)[-1]


# ObjectAdded and ObjectDeleted are left out: an object added or deleted changes the object list, which the
# fingerprint holds, and OrcaSlicer also sends ObjectAdded for every object each time it rebuilds the sidebar list
# (Plater::delete_plate > ObjectList::reload_all_plates) with nothing added (live test, 2026-11-10).
SLICE_OBJECT_EVENTS = frozenset({"ObjectTransformed", "ObjectChanged"})
SLICE_PROJECT_EVENTS = frozenset({"NewProject", "ProjectOpened", "ProjectClosed"})
SLICE_EVENTS = SLICE_OBJECT_EVENTS | SLICE_PROJECT_EVENTS | {"SliceStarted", "GCodeExportFinished",
                                                              "SlicingJobComplete", "PlateDeleted"}


class SliceWatch:
    """The user's slices, one per plate, from OrcaSlicer's lifecycle events.

    SliceStarted and GCodeExportFinished arrive on the slicing worker thread, the rest on the UI thread, so event()
    only records, under a lock. The two UI-thread reads, plate_index() (the model's current plate) and fingerprint()
    (settings and objects, see slice_fingerprint), are taken when a slice completes and when it is read. The plate
    comes from OrcaSlicer itself, never from PlateSelected: "Move plate to front" changes the current plate without
    that event, and Slice all selects each plate before slicing it. A slice is out of date once an object event
    arrives or the fingerprint (taken for the slice's own plate) differs. Deleting a plate renumbers the plates
    after it, so their slices are dropped with the deleted plate's. A failed step after the G-code was written (an export or upload) keeps the
    slice, as OrcaSlicer does; an error or a cancel with no slice running leaves the plate's slice alone."""

    def __init__(self, plate_index, fingerprint):
        self._plate_index = plate_index
        self._fingerprint = fingerprint
        self._lock = threading.Lock()
        self._records: dict = {}
        self._slicing = False  # a slice started and has not finished
        self._pending = None   # path of the G-code this job wrote
        self._changes = 0
        self._serial = 0

    def _read(self, fn, what):
        try:
            return fn()
        except Exception as e:
            trace(f"slice {what} failed: {type(e).__name__}: {e}")
            return None

    def event(self, name: str, ctx) -> None:
        if name not in SLICE_EVENTS:
            return
        code = _code_name(getattr(ctx, "code", "Ok"))
        msg = getattr(ctx, "msg", "") or ""
        plate = fp = None
        if name == "SlicingJobComplete":  # UI thread
            plate = self._read(self._plate_index, "plate")
            with self._lock:
                wanted = self._pending is not None
            if wanted and isinstance(plate, int):
                fp = self._read(lambda: self._fingerprint(plate), "fingerprint")  # None: counts, reads out of date
        with self._lock:
            if name in SLICE_OBJECT_EVENTS:
                self._changes += 1
            elif name in SLICE_PROJECT_EVENTS:
                self._records.clear()
                self._slicing, self._pending = False, None
            elif name == "PlateDeleted":
                gone = getattr(ctx, "index", -1)
                if isinstance(gone, int) and not isinstance(gone, bool) and gone >= 0:
                    for p in [p for p in self._records if p >= gone]:
                        del self._records[p]
                else:
                    self._records.clear()
            elif name == "SliceStarted":
                self._slicing, self._pending = True, None
            elif name == "GCodeExportFinished":
                if code == "Ok" and msg.strip():
                    self._pending = msg.strip()
            elif name == "SlicingJobComplete":
                if isinstance(plate, int) and not isinstance(plate, bool) and plate >= 0:
                    if self._pending is not None and code in ("Ok", "Error"):
                        self._serial += 1
                        self._records[plate] = {"state": "done", "path": self._pending, "fp": fp,
                                                "changes": self._changes, "serial": self._serial}
                    elif code == "Error" and self._slicing:
                        self._records[plate] = {"state": "error", "message": msg.strip() or SLICE_FAILED_TEXT}
                    elif code not in ("Ok", "Error") and self._slicing:  # Warn: the running slice was cancelled
                        self._records.pop(plate, None)
                self._slicing, self._pending = False, None

    def current(self) -> dict:
        """UI thread. The current plate's slice: {"state": "idle"|"slicing"} | {"state": "error", "message"} |
        {"state": "done", "path", "valid", "serial"}. Reads the fingerprint only for a finished slice."""
        plate = self._read(self._plate_index, "plate")
        with self._lock:
            slicing = self._slicing
            rec = self._records.get(plate)
            changes = self._changes
        if slicing:
            return {"state": "slicing"}
        if rec is None:
            return {"state": "idle"}
        if rec["state"] == "error":
            return {"state": "error", "message": rec["message"]}
        valid = rec["fp"] is not None and rec["changes"] == changes and self._read(
            lambda: self._fingerprint(plate), "fingerprint") == rec["fp"]
        return {"state": "done", "path": rec["path"], "valid": valid, "serial": rec["serial"]}


def current_plate_index() -> int:
    """UI thread: the plate OrcaSlicer shows (and slices) now."""
    return int(orca.host.plater().model().current_plate_index())


_PAINT_CHECKS = ("is_fdm_support_painted", "is_seam_painted", "is_mm_painted", "is_fuzzy_skin_painted")


def _volume_signature(mo) -> list:
    """UI thread: each part, modifier, negative volume and support enforcer/blocker of an object as the plugin API
    shows it (type, placement, size, own settings, whether it carries paint). Paint added to an already painted
    part does not show here: the API only says whether a part is painted."""
    try:
        volumes = mo.volumes()
    except AttributeError:  # an OrcaSlicer without volume bindings
        return []
    out = []
    for v in volumes:
        config = {}
        for k in v.config_keys():
            value = v.config_value(k)
            if value is not None:
                config[k] = value
        out.append([int(v.id()), str(v.type()), list(v.offset()), list(v.rotation()), list(v.scaling_factor()),
                    list(v.mirror()), int(v.facets_count()), config,
                    [bool(getattr(v, name)()) for name in _PAINT_CHECKS if hasattr(v, name)]])
    return out


PER_PLATE_PROJECT_KEYS = ("wipe_tower_x", "wipe_tower_y")  # one value per plate, in plate order


def _plate_entry(value, plate: int):
    """The plate's own entry of a per-plate list ("15,165" -> "165" for plate 1); the whole value otherwise."""
    items = list(value) if isinstance(value, (list, tuple)) else str(value).split(",")
    return items[plate] if 0 <= plate < len(items) else value


def slice_fingerprint(plate: int) -> str:
    """UI thread: one hash of what the slice of `plate` depends on and the plugin can read: everything the journal
    compares (presets and their edits, filament slots, project settings, objects with their own settings and
    placements), plus each object's parts and modifiers and whether it has a variable layer height. Of the
    per-plate project settings (the wipe tower position) only the plate's own entry counts: adding a plate
    lengthens those lists. Equal hashes: nothing that can be seen changed."""
    snap = capture_baseline(types.SimpleNamespace(project_gen=0, project_text=""))
    snap.pop("project_gen", None)
    snap.pop("project_text", None)
    for k in PER_PLATE_PROJECT_KEYS:
        if k in snap["project"]:
            snap["project"][k] = _plate_entry(snap["project"][k], plate)
    snap["volumes"] = []
    for mo in orca.host.plater().model().objects():
        try:
            layering = bool(mo.has_custom_layering())
        except AttributeError:
            layering = False
        snap["volumes"].append([int(mo.id()), layering, _volume_signature(mo)])
    return hashlib.sha256(json.dumps(snap, sort_keys=True, default=str).encode("utf-8")).hexdigest()


class RawBody:
    """A reply that is not JSON (GET /gcode)."""

    def __init__(self, data: bytes, content_type: str):
        self.data = data
        self.content_type = content_type


# ---------------------------------------------------------------------------------------------
# UI-thread pump
# ---------------------------------------------------------------------------------------------
class _Job:
    __slots__ = ("fn", "done", "result", "error")

    def __init__(self, fn):
        self.fn = fn
        self.done = threading.Event()
        self.result = None
        self.error = None


class UiPump:
    """Runs callables on the UI thread through the panel's message round trip.

    call() runs on the server thread and blocks; run_pending() runs on the UI thread from the panel's
    on_message. A job whose caller gave up is removed first, so it never runs late."""

    def __init__(self, panel_source, any_panel=None):
        self._panel_source = panel_source  # () -> panel with .post(), raises ApiReply if paused
        self._any_panel = any_panel or panel_source  # the same, also while assistant access is off
        self._jobs: dict[int, _Job] = {}
        self._lock = threading.Lock()
        self._next = 0
        self._closed = False

    def call(self, fn, timeout: float = UI_TIMEOUT_S, gate: bool = True):
        if self._closed:
            raise ApiReply(503, {"error": "shutting_down"})
        panel = self._panel_source() if gate else self._any_panel()
        job = _Job(fn)
        with self._lock:
            self._next += 1
            jid = self._next
            self._jobs[jid] = job
        try:
            panel.post({"t": "run", "id": jid})
        except Exception:
            pass  # the page also polls; a dropped post only costs a tick
        if not job.done.wait(timeout):
            with self._lock:
                still_queued = self._jobs.pop(jid, None) is not None
            if still_queued:
                raise ApiReply(504, {"error": "ui_timeout",
                                     "detail": "the OrcaSlicer MCP panel did not answer; is OrcaSlicer busy, "
                                               "or the panel hidden?"})
            job.done.wait()  # it started on the UI thread just now: let it finish
        if job.error is not None:
            raise job.error
        return job.result

    def run_pending(self):
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            try:
                job.result = job.fn()
            except BaseException as e:  # handed to the waiting thread
                job.error = e
            finally:
                job.done.set()

    def close(self):
        self._closed = True
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            job.error = ApiReply(503, {"error": "shutting_down"})
            job.done.set()


# ---------------------------------------------------------------------------------------------
# Reads (UI thread only: they run inside UiPump.call)
# ---------------------------------------------------------------------------------------------
GROUPS = (("print", "prints"), ("filament", "filaments"), ("printer", "printers"))


def _collection(bundle, ptype: str):
    attr = dict(GROUPS).get(ptype)
    return getattr(bundle, attr) if attr else None


def _modified_keys(collection) -> list[str]:
    """Keys whose edited value differs from the saved preset (PresetCollection::current_dirty_options)."""
    # Compared value by value: Preset.is_dirty is a cached UI flag, not kept current on every copy.
    edited = collection.edited_preset()
    saved = collection.selected_preset()
    keys = set(edited.config_keys()) | set(saved.config_keys())
    return sorted(k for k in keys if edited.config_value(k) != saved.config_value(k))


def _project_strings(plater):
    path = plater.project_path() or ""
    name = os.path.splitext(os.path.basename(path))[0] if path else "Untitled"
    return path, name


def read_status() -> dict:
    bundle = orca.host.preset_bundle()
    plater = orca.host.plater()
    model = plater.model()
    path, name = _project_strings(plater)
    objects = [{"name": o.name, "size_mm": list(o.bounding_box().size)} for o in model.objects()]
    info = orca.host.app_info()
    return {
        "app": "OrcaSlicer",
        "app_version": info.get("version", ""),
        "api_version": "1.0",
        "backend": "plugin",
        "plugin_version": PLUGIN_VERSION,
        "capabilities": CAPABILITIES,
        "not_available": FORK_ONLY,
        "sensitive_keys_allowed": False,
        "project": os.path.splitext(path)[0],
        "project_name": name,
        "project_dirty": bool(plater.is_project_dirty() or plater.is_presets_dirty()),
        "objects": objects,
        "presets": {
            "printer": bundle.printers.selected_preset_name(),
            "print": bundle.prints.selected_preset_name(),
            "filaments": list(bundle.current_filament_preset_names()),
        },
        "modified": {g: _modified_keys(getattr(bundle, attr)) for g, attr in GROUPS},
    }


def read_config(keys: list[str] | None) -> dict:
    bundle = orca.host.preset_bundle()
    wanted = keys if keys else bundle.full_config_keys()
    out = {}
    for k in wanted:
        if k in UNREADABLE_KEYS:
            continue
        v = bundle.full_config_value(k)
        if v is not None:
            out[k] = v
    return {"config": out}


def read_presets() -> dict:
    bundle = orca.host.preset_bundle()
    out = {}
    for group, attr in GROUPS:
        c = getattr(bundle, attr)
        sel = c.selected_preset_name()
        rows = []
        for i in range(c.size()):
            p = c.preset(i)
            if p.is_default:
                continue
            # "compatible" is extra to the fork's shape: only compatible presets can be selected.
            rows.append({"name": p.name, "system": bool(p.is_system), "visible": bool(p.is_visible),
                         "selected": p.name == sel, "compatible": bool(p.is_compatible)})
        out[group] = rows
    return out


def read_preset_config(ptype: str, name: str) -> dict:
    c = _collection(orca.host.preset_bundle(), ptype)
    if c is None:
        raise ApiReply(400, {"error": "unknown_type"})
    p = c.find_preset(name)
    if p is None:
        raise ApiReply(404, {"error": "unknown_preset"})
    cfg = {k: redact_value(k, p.config_value(k)) for k in p.config_keys()}
    return {"name": p.name, "system": bool(p.is_system), "config": cfg}


def read_objects() -> dict:
    model = orca.host.model()
    objects = []
    for i, mo in enumerate(model.objects()):
        o = {"id": int(mo.id()), "index": i, "name": mo.name, "size_mm": list(mo.bounding_box().size),
             "instances": int(mo.instance_count())}
        insts = mo.instances()
        if insts:
            mi = insts[0]
            o["transform"] = {"offset": list(mi.offset()), "rotation": list(mi.rotation()),
                              "scale": list(mi.scaling_factor())}
            bb = mi.bounding_box()
            if bb.defined:
                o["bbox_min"] = list(bb.min)
                o["bbox_max"] = list(bb.max)
                o["on_plate"] = abs(bb.min[2]) < 0.05
        o["config"] = {k: mo.config_value(k) for k in mo.config_keys()}
        o["custom_layer_profile"] = bool(mo.has_custom_layering())
        objects.append(o)
    return {"objects": objects, "count": len(objects)}


def read_project() -> dict:
    plater = orca.host.plater()
    bundle = orca.host.preset_bundle()
    path, name = _project_strings(plater)
    modified = [g for g, attr in GROUPS if _modified_keys(getattr(bundle, attr))]
    return {"name": name, "path": path, "saved": bool(path),
            "dirty": bool(plater.is_project_dirty() or plater.is_presets_dirty()),
            "objects": int(plater.model().object_count()), "modified": modified}


def plan_config_write(changes: dict) -> dict:
    """UI thread: route each key to its preset group and apply the policy. Returns
    {"groups": {group: [(key, text, old)]}, "errors": {key: reason}, "slot": n}."""
    bundle = orca.host.preset_bundle()
    edited = {g: getattr(bundle, attr).edited_preset() for g, attr in GROUPS}
    keysets = {g: set(p.config_keys()) for g, p in edited.items()}
    full = None
    groups: dict[str, list] = {}
    errors: dict[str, str] = {}
    for key, value in changes.items():
        group = next((g for g, _ in GROUPS if key in keysets[g]), None)
        if group is None:
            if full is None:
                full = set(bundle.full_config_keys())
            errors[key] = "not_editable_in_current_config" if key in full else "unknown_key"
            continue
        text = as_config_text(value)
        if text is None:
            errors[key] = "unsupported value type"
            continue
        old = edited[group].config_value(key)
        if is_sensitive(key):
            if REDACTED in text:
                errors[key] = "redacted_placeholder"
                continue
            if text != old:
                errors[key] = "blocked_by_remote_api_policy"
                continue
        groups.setdefault(group, []).append((key, text, old))
    # The Filament tab edits one preset; write through the first slot that uses it.
    names = list(bundle.current_filament_preset_names())
    fname = edited["filament"].name
    slot = names.index(fname) + 1 if fname in names else 1
    return {"groups": groups, "errors": errors, "slot": slot, "slots": len(names),
            "using": [i + 1 for i, n in enumerate(names) if n == fname] or [slot],
            "presets": {g: p.name for g, p in edited.items()}}


# ---------------------------------------------------------------------------------------------
# Row text for the panel (the same wording as the OrcaSlicer MCP build)
# ---------------------------------------------------------------------------------------------
# A refused change's row value: the build's mcp_reason() words (RemoteAPIController.cpp).
REASONS = {
    "blocked_by_remote_api_policy": "Not allowed", "unknown_key": "Unknown setting",
    "not_editable_in_current_config": "Not editable here", "redacted_placeholder": "Hidden value",
    "unknown_preset": "No such preset", "incompatible_preset": "Not compatible",
    "slot_out_of_range": "No such filament", "select_cancelled": "Switch cancelled",
    "select_failed": "Couldn't select",
}
# Where a refusal is explained. The build names the Preferences box that allows a blocked key; the plugin has
# none, so it says the plugin never does it. Other refusals read "<key>: <error>", as in the build.
NEVER_SENSITIVE = "The plugin never changes scripts, G-code or printer connection settings"
NEEDS_BUILD = "Needs the OrcaSlicer MCP build"


def filament_suffix(slots: list | None, count: int) -> str:
    """" (filament 2)" / " (filaments 1, 3)" when more than one filament is loaded (the build's
    mcp_edited_filament_suffix)."""
    if not slots or count <= 1:
        return ""
    if len(slots) == 1:
        return f" (filament {slots[0]})"
    return " (filaments " + ", ".join(str(s) for s in slots) + ")"


# The sidebar tells settings with the same label apart by the section they sit in (Speed, Acceleration, Jerk,
# Line width); the plugin API has no sidebar layout, so a shared label takes that word from the key instead.
_SECTION_WORDS = (("_line_width", "line width"), ("_acceleration", "acceleration"), ("_jerk", "jerk"),
                  ("_speed", "speed"))
# Bed temperatures are one row per plate type in the sidebar; name the plate (curr_bed_type's labels).
_PLATE_NAMES = {"cool_plate": "Smooth Cool Plate", "eng_plate": "Engineering Plate", "hot_plate": "Smooth High Temp Plate",
                "textured_plate": "Textured PEI Plate", "textured_cool_plate": "Textured Cool Plate",
                "supertack_plate": "Cool Plate (SuperTack)"}
_PLATE_TEMP = re.compile(r"(.+_plate)_temp(?:_initial_layer)?")
_shared_labels_cache: set | None = None


def _shared_labels() -> set:
    global _shared_labels_cache
    if _shared_labels_cache is None:
        seen, shared = set(), set()
        for label, _unit in LABELS.values():
            (shared if label in seen else seen).add(label)
        _shared_labels_cache = shared
    return _shared_labels_cache


def display_label(key: str) -> str:
    """OrcaSlicer's label (full_label when it has one), made unique where several settings share it
    ("Outer wall" is the speed, the acceleration, the jerk and the line width): usability finding m1."""
    label = LABELS.get(key, (key, ""))[0]
    m = _PLATE_TEMP.fullmatch(key)
    if m and m.group(1) in _PLATE_NAMES:
        return f"{label} ({_PLATE_NAMES[m.group(1)]})"
    if label in _shared_labels():
        for suffix, word in _SECTION_WORDS:
            if key.endswith(suffix) and not label.lower().endswith(word):
                return f"{label} {word}"
    return label


def settings_list_text(keys) -> str:
    """Refused settings for a pop-up, named as the panel names them (Max 2026-11-10: the pop-up said
    "wall_loops" where the panel said "Wall loops"). A key with no label keeps its key."""
    return ", ".join(sorted((display_label(k) for k in keys), key=str.lower))


def setting_label(key: str, slots: list | None = None, count: int = 1) -> str:
    return display_label(key) + filament_suffix(slots, count)


def setting_value(key: str, raw) -> str:
    return value_text(key, raw)


# Multi-line text values (the build's mcp_multiline_display / mcp_multiline_tip in McpHistory.cpp and
# Controller::config_text, remote-api-250 b007932e5f): a row shows the first line and the line count, or the first
# line that differs when both sides start alike; the hover tag shows up to 5 lines. Undo and the live check use raws.
# The coStrings options OrcaSlicer flags multiline (PrintConfig.cpp): one entry per line. Every other value is
# unescaped C-style, as a coString's (a number never holds a backslash, so it reads as itself).
MULTILINE_LISTS = frozenset({"filament_change_extrusion_role_gcode", "filament_end_gcode", "filament_notes",
                             "filament_start_gcode", "post_process"})
_LINE_CHARS, _TIP_LINE_CHARS, _TIP_LINES = 40, 60, 5


def _unescape_cstyle(s: str):
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 1
            if i == len(s):
                return None
            c = {"r": "\r", "n": "\n"}.get(s[i], s[i])
        out.append(c)
        i += 1
    return "".join(out)


def _unescape_list(s: str):
    """OrcaSlicer's unescape_strings_cstyle: entries split on ';', a quoted entry with \\n / \\r escapes."""
    out, i, n = [], 0, len(s)
    if not s:
        return out
    while True:
        while s[i] in " \t":
            i += 1
            if i == n:
                return out
        buf = []
        if s[i] == '"':
            i += 1
            while i < n and s[i] != '"':
                c = s[i]
                if c == "\\":
                    i += 1
                    if i == n:
                        return None
                    c = {"r": "\r", "n": "\n"}.get(s[i], s[i])
                buf.append(c)
                i += 1
            if i == n:
                return None
            i += 1
        else:
            while i < n and s[i] != ";":
                buf.append(s[i])
                i += 1
        out.append("".join(buf))
        if i == n:
            return out
        while s[i] in " \t":
            i += 1
            if i == n:
                return out
        if s[i] != ";":
            return None
        i += 1
        if i == n:
            out.append("")
            return out


def config_text(key: str, raw):
    """A setting's text with real line breaks (the build's Controller::config_text), or None."""
    if raw is None:
        return None
    raw = str(raw)
    if key in MULTILINE_LISTS:
        entries = _unescape_list(raw)
        return None if entries is None else "\n".join(entries)
    return _unescape_cstyle(raw)


def _split_lines(text: str) -> list:
    out, start = [], 0
    while start < len(text):
        nl = text.find("\n", start)
        line = text[start:] if nl < 0 else text[start:nl]
        if line.endswith("\r"):
            line = line[:-1]
        out.append(line)
        if nl < 0:
            break
        start = nl + 1
    return out


def _shown_line(s: str, max_chars: int) -> str:
    s = s.rstrip(" \t")
    if not s.strip(" \t"):
        return "(empty line)"
    return s[:max_chars] + ELLIPSIS if len(s) > max_chars else s


def _first_change(a: list, other) -> int:
    if other is None:
        return 0
    b = _split_lines(other)
    if not a or not b or a[0] != b[0]:
        return 0
    for i in range(1, max(len(a), len(b))):
        if i >= len(a) or i >= len(b) or a[i] != b[i]:
            return i
    return 0


def multiline_display(text: str, other) -> str:
    """'<line 1> … (N lines)', or 'line k: <line k> (N lines)' at the first line that differs from `other`; '' for
    fewer than 2 lines."""
    lines = _split_lines(text)
    if len(lines) < 2:
        return ""
    count = f" ({len(lines)} lines)"
    k = _first_change(lines, other)
    if 0 < k < len(lines):
        return f"line {k + 1}: {_shown_line(lines[k], _LINE_CHARS)}{count}"
    head = _shown_line(lines[0], _LINE_CHARS)
    return head + ("" if head.endswith(ELLIPSIS) else " " + ELLIPSIS) + count


def multiline_tip(text: str, other) -> str:
    lines = _split_lines(text)
    if len(lines) < 2:
        return ""
    start = _first_change(lines, other)
    if start >= len(lines):
        start = 0
    end = min(len(lines), start + _TIP_LINES)
    out = (f"Line {start + 1}" if end - start == 1 else f"Lines {start + 1} to {end}") + f" of {len(lines)}:"
    out += "".join("\n" + _shown_line(lines[i], _TIP_LINE_CHARS) for i in range(start, end))
    return out + ("\n" + ELLIPSIS if end < len(lines) else "")


def multiline_sides(key: str, before_raw, after_raw) -> dict:
    """{value, value_tip, before, before_tip} for whichever side is multi-line text; {} when neither is."""
    after_t, before_t = config_text(key, after_raw), config_text(key, before_raw)
    out = {}
    shown = multiline_display(after_t, before_t) if after_t is not None else ""
    if shown:
        out["value"], out["value_tip"] = shown, multiline_tip(after_t, before_t)
    shown = multiline_display(before_t, after_t) if before_t is not None else ""
    if shown:
        out["before"], out["before_tip"] = shown, multiline_tip(before_t, after_t)
    return out


def multiline_live(key: str, row: dict, raw) -> str:
    """The build's config_live_text for a multi-line row: its own value or before when the text is one of them,
    else the text's form against the assistant's value; "changed since" when that reads like either side."""
    t = row["target"]
    now, after, before = config_text(key, raw), config_text(key, t.get("value")), config_text(key, t.get("before"))
    if now is not None and now == after:
        return row["value"]
    if now is not None and now == before:
        return row["before"]
    shown = multiline_display(now, after) if now is not None else ""
    if not shown:
        shown = setting_value(key, raw)
    if _same(shown, row["value"]) or _same(shown, row["before"]):
        shown = "changed since"
    return shown


def preset_label(ptype: str, slot: int = 1, slots: int = 1) -> str:
    if ptype == "filament":
        return f"Filament {slot}" if slots > 1 else "Filament"
    return "Print preset" if ptype == "print" else "Printer"


def refusal_row(label: str, error: str, where: str | None = None, fallback: str = "Invalid value") -> dict:
    """A refused row: the build's reason words, and where it is explained."""
    if where is None:
        where = NEVER_SENSITIVE if error == "blocked_by_remote_api_policy" else error
    return {"kind": "refused", "label": label, "value": REASONS.get(error, fallback), "where": where}


def config_refusal_row(key: str, error: str) -> dict:
    if error == "blocked_by_remote_api_policy":
        return refusal_row(setting_label(key), error)
    if error == "not_editable_in_current_config":
        return refusal_row(setting_label(key), error, NEEDS_BUILD)
    return refusal_row(setting_label(key), error, f"{key}: {error}")


def _note_text(value, limit: int) -> str:
    """A note field as the fork stores it: line breaks and tabs become spaces, control and format characters (C0/C1
    controls, bidi overrides and isolates, zero-width characters, the BOM) are dropped, at most `limit` UTF-8 bytes."""
    if not isinstance(value, str):
        return ""
    import unicodedata
    text = value.replace("\r\n", " ")
    text = "".join(" " if c in "\r\n\t" else c for c in text)
    text = "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf")).strip()
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore").strip()


# ---------------------------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------------------------
class Api:
    """record(rows) hands the panel host one burst of changes (see PanelHost.record)."""

    def __init__(self, pump: UiPump, notify=None, record=None, on_request=None, journal: "Journal | None" = None,
                 clock=time.time, on_switch=None, slices: "SliceWatch | None" = None, slice_folders=()):
        self.pump = pump
        self.slices = slices
        self.slice_folders = list(slice_folders)
        self._summary = ("", -1, None)  # (path, serial, stats) of the last G-code read
        self.notify = notify or (lambda text, warning=False: None)
        self.record = record or (lambda rows, close=False, new_group=False: None)
        self.on_switch = on_switch or (lambda ptype, slot, before, name: [])  # UI thread: row ids it dropped
        self.on_request = on_request or (lambda: None)
        self.journal = journal
        self.clock = clock

    def handle(self, method: str, target: str, body: bytes | None, trial: bool = False) -> tuple[int, dict]:
        """trial: the request carried X-Orca-Mcp-Trial: 1 (orcaslicer-mcp's compare_slices trials and the restore
        after them). It changes settings like any request but adds no panel rows and no "AI changed" notice;
        the journal counts it as an API change, as always."""
        self.on_request()
        changes = JOURNAL_ROUTES.get((method, urllib.parse.urlsplit(target).path))
        try:
            if changes is not None:
                self._journal_begin()
            try:
                return self._route(method, target, body, trial)
            finally:
                if changes:
                    self._journal_end()
        except ApiReply as r:
            return r.status, r.body
        except Exception as e:  # never leak a traceback, but say what happened
            return 500, {"error": "internal", "detail": f"{type(e).__name__}: {e}"}

    # -- user-change journal: compare before the request, new baseline after a change ---------
    def _journal_begin(self) -> None:
        if self.journal is None:
            return
        try:
            now = self.pump.call(lambda: capture_baseline(self.journal))
        except ApiReply:
            raise  # paused, shutting down or a stuck UI: the request fails the same way
        except Exception as e:
            trace(f"journal compare failed: {type(e).__name__}: {e}")
            return
        self.journal.begin_request(now, time.strftime("%H:%M", time.localtime(self.clock())))

    def _journal_end(self) -> None:
        if self.journal is None:
            return
        try:
            self.journal.end_request(self.pump.call(lambda: capture_baseline(self.journal)))
        except Exception as e:
            trace(f"journal baseline failed: {type(e).__name__}: {e}")
            self.journal.invalidate()

    def _json(self, body: bytes | None) -> dict:
        try:
            data = json.loads(body or b"")
        except ValueError:
            raise ApiReply(400, {"error": "invalid_json"})
        if not isinstance(data, dict):
            raise ApiReply(400, {"error": "body_must_be_object"})
        return data

    def _route(self, method, target, body, trial=False):
        url = urllib.parse.urlsplit(target)
        path = url.path
        if method == "GET" and path == "/api/v1/status":
            return 200, self.pump.call(self._status)
        if method == "GET" and path == "/api/v1/slice/status" and self.slices is not None:
            return self._slice_status()
        if method == "GET" and path == "/api/v1/gcode" and self.slices is not None:
            return self._gcode()
        if method == "GET" and path == "/api/v1/config":
            q = urllib.parse.parse_qs(url.query)
            keys = [k for part in q.get("keys", []) for k in part.split(",") if k]
            return 200, self.pump.call(lambda: read_config(keys or None))
        if method == "PUT" and path == "/api/v1/config":
            return self._put_config(self._json(body), trial)
        if method == "GET" and path == "/api/v1/presets":
            return 200, self.pump.call(read_presets)
        if method == "POST" and path == "/api/v1/preset/config":
            ptype, name = self._type_name(self._json(body))
            return 200, self.pump.call(lambda: read_preset_config(ptype, name))
        if method == "PUT" and path == "/api/v1/preset":
            return self._select_preset(self._json(body))
        if method == "GET" and path == "/api/v1/objects":
            return 200, self.pump.call(read_objects)
        if method == "GET" and path == "/api/v1/project":
            return 200, self.pump.call(read_project)
        if method == "POST" and path == "/api/v1/activity/note":
            return self._note(self._json(body))
        if method == "GET" and path == "/api/v1/user_changes" and self.journal is not None:
            return 200, self.journal.since(parse_since(target))
        raise _not_found()

    # -- the user's own slice -----------------------------------------------------------------
    def _status(self) -> dict:  # UI thread
        st = read_status()
        if self.slices is not None:
            cur = self.slices.current()
            st["slice_result_valid"] = cur["state"] == "done" and cur["valid"] is True
        return st

    def _summary_of(self, path: str, serial: int) -> tuple[dict | None, str | None]:
        """(stats, problem) from the start and end of the G-code, cached per slice (OrcaSlicer reuses a plate's
        file name for every slice of it). Never opens a file outside the slice folders: OrcaSlicer would deny it
        and ask the user about that one file."""
        cached_path, cached_serial, cached = self._summary
        if cached_path == path and cached_serial == serial:
            return cached, None
        if not in_slice_folder(path, self.slice_folders):
            return None, SLICE_OUTSIDE_TEXT
        try:
            with open(path, "rb") as f:
                head = f.read(SLICE_HEAD_BYTES).decode("utf-8", "replace")
                size = f.seek(0, os.SEEK_END)
                f.seek(max(0, size - SLICE_TAIL_BYTES))
                tail = f.read().decode("utf-8", "replace")
        except FileNotFoundError:
            return None, SLICE_GONE_TEXT
        except PermissionError as e:  # the audit hook's denial, or a Windows sharing violation mid-rename
            trace(f"slice summary read refused: {e}")
            return None, SLICE_BUSY_TEXT
        except OSError as e:
            trace(f"slice summary read failed: {type(e).__name__}: {e}")
            return None, SLICE_BUSY_TEXT
        stats = parse_gcode_summary(tail, head) or None
        self._summary = (path, serial, stats)
        return stats, None

    def _slice_status(self):
        """GET /slice/status as the build answers it, for the slice the user made."""
        cur = self.pump.call(self.slices.current)
        state = cur["state"]
        if state == "idle":
            return 200, {"state": "idle", "percent": 0, "message": SLICE_IDLE_TEXT, "warnings": []}
        if state == "slicing":
            return 200, {"state": "slicing", "percent": -1, "message": SLICE_RUNNING_TEXT, "warnings": []}
        if state == "error":
            return 200, {"state": "error", "percent": 100, "message": cur["message"], "errors": [cur["message"]],
                         "warnings": []}
        valid = cur["valid"]
        stats, problem = self._summary_of(cur["path"], cur["serial"])
        body = {"state": "done", "percent": 100, "warnings": [], "valid": valid, "stale": not valid,
                "slice_id": cur["serial"], "message": problem or (SLICE_DONE_TEXT if valid else SLICE_STALE_TEXT)}
        if stats:
            body["stats"] = stats
        return 200, body

    def _gcode(self):
        """GET /gcode: the G-code of the user's slice, if it still matches the plate (else 409 not_sliced, as on
        the build). The whole file is read at once and closed: OrcaSlicer replaces it by rename when it slices
        again, which fails on Windows while the file is open."""
        cur = self.pump.call(self.slices.current)
        if cur["state"] != "done" or not cur["valid"]:
            raise ApiReply(409, {"error": "not_sliced"})
        path = cur["path"]
        if not in_slice_folder(path, self.slice_folders):
            raise ApiReply(500, {"error": "gcode_not_readable", "detail": SLICE_OUTSIDE_TEXT})
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            raise ApiReply(500, {"error": "gcode_file_missing", "detail": SLICE_GONE_TEXT})
        except OSError as e:
            trace(f"gcode read failed: {type(e).__name__}: {e}")
            raise ApiReply(500, {"error": "gcode_not_readable", "detail": SLICE_BUSY_TEXT})
        return 200, RawBody(data, "text/plain")

    def _note(self, data: dict):
        """POST /activity/note: one row the connector adds itself (the fork's route, same body and limits): a
        "slice" note that names a change-less group and ends it (compare_slices' summary), or a "refused" row for a
        refusal the connector made before reaching OrcaSlicer. Recorded even on a trial request; changes nothing."""
        self.pump.call(lambda: None)  # refused like every other request while assistant access is off
        kind = data.get("kind")
        if kind not in ("slice", "refused"):
            raise ApiReply(400, {"error": "bad_kind"})
        text = _note_text(data.get("text"), 160)
        if not text:
            raise ApiReply(400, {"error": "missing_text"})
        value = _note_text(data.get("value"), 80)
        detail = _note_text(data.get("detail"), 400)
        if kind == "slice":
            row = {"kind": "slice", "label": text, "value": value, "where": detail, "title_hint": "note"}
        else:
            row = {"kind": "refused", "label": text, "value": value or "Not allowed", "where": detail}
        self.record([row], close=kind == "slice", new_group=kind == "slice")
        return 200, {"recorded": True}

    @staticmethod
    def _type_name(data: dict) -> tuple[str, str]:
        ptype, name = data.get("type"), data.get("name")
        if not isinstance(ptype, str) or not isinstance(name, str):
            raise ApiReply(400, {"error": "missing_fields"})
        return ptype, name

    def _put_config(self, changes: dict, trial: bool = False):
        record = (lambda rows: None) if trial else self.record
        plan = self.pump.call(lambda: plan_config_write(changes))
        errors = plan["errors"]
        if errors:
            self.notify("Couldn't update settings: " + settings_list_text(errors), True)
            record([config_refusal_row(k, errors[k]) for k in changes if k in errors])
            return 422, {"applied": [], "errors": errors}
        applied, shown, rows = [], [], []
        for group, _ in GROUPS:
            items = plan["groups"].get(group)
            if not items:
                continue
            slots = plan["using"] if group == "filament" else None
            try:
                editor = getattr(orca.host.preset, "process" if group == "print" else group)
                tx = editor.begin_update(plan["slot"]) if group == "filament" else editor.begin_update()
                for key, text, _old in items:
                    tx.set(key, text)
                tx.commit()
            except (ValueError, RuntimeError) as e:
                msg = str(e)
                named = [k for k, _, _ in items if k in msg] or [k for k, _, _ in items]
                body = {"applied": applied, "errors": {k: msg for k in named}}
                if applied:
                    body["partial"] = True
                    body["detail"] = ("settings in an earlier group were already applied; the plugin cannot "
                                      "apply several groups as one batch")
                self.notify("Couldn't update settings: " + settings_list_text(named), True)
                rows += [refusal_row(setting_label(k, slots, plan["slots"]), "rejected", f"{k}: {msg}")
                         for k in named]
                record(rows)
                return 422, body
            preset = plan["presets"][group]
            for key, text, old in items:
                applied.append(key)
                if text == old or (config_text(key, text) is not None and config_text(key, text) == config_text(key, old)):
                    continue  # applied, but nothing changed: no row, as in the build (c.oldv == c.newv)
                if key == "filament_colour":  # the build's colour row: "Filament 2 colour"
                    label = f"Filament {plan['slot']} colour"
                else:
                    label = setting_label(key, slots, plan["slots"])
                multi = multiline_sides(key, old, text)
                before = multi.get("before", setting_value(key, old))
                value = multi.get("value", setting_value(key, text))
                shown.append(f"{label} {before} \u2192 {value}" if multi else f"{label} {old} \u2192 {text}")
                target = {"kind": "setting", "group": group, "key": key,
                          "slot": plan["slot"] if group == "filament" else None,
                          "preset": preset, "value": text, "before": old}
                if multi:
                    target["multiline"] = True
                rows.append({"kind": "setting", "label": label, "value": value, "before": before,
                             "where": f"{preset} (unsaved)", "value_tip": multi.get("value_tip", ""),
                             "before_tip": multi.get("before_tip", ""),
                             "undo_verb": "Revert", "undo_tip": f"Set {label} back to {before}",
                             "target": target})
        if shown and not trial:
            self.notify("AI changed: " + "; ".join(shown))
            record(rows)
        return 200, {"applied": applied, "errors": {}}

    def _select_preset(self, data: dict):
        """The trial header does not apply here: the build counts it only on settings writes and slices."""
        record = self.record
        ptype, name = self._type_name(data)
        if ptype not in dict(GROUPS):
            raise ApiReply(400, {"error": "unknown_type"})
        slot = data.get("slot", 1) if ptype == "filament" else 1
        if not isinstance(slot, int) or isinstance(slot, bool) or slot < 1:
            raise ApiReply(400, {"error": "bad_slot"})

        def current():  # UI thread: does it exist, and what is selected now
            bundle = orca.host.preset_bundle()
            names = list(bundle.current_filament_preset_names())
            found = _collection(bundle, ptype).find_preset(name) is not None
            if ptype == "filament":
                return found, (names[slot - 1] if slot <= len(names) else ""), len(names)
            return found, _collection(bundle, ptype).selected_preset_name(), len(names)

        found, before, slots = self.pump.call(current)
        label = preset_label(ptype, slot, slots)
        if not found:
            self.notify(f"No such {ptype} preset '{name}'", True)
            record([refusal_row(label, "unknown_preset", f"unknown_preset: {name}")])
            raise ApiReply(422, {"error": "unknown_preset"})
        # OrcaSlicer's own selection flow: with unsaved edits it asks the person at the screen
        # whether to keep, transfer or discard them, and this request waits for the answer.
        try:
            if ptype == "filament":
                tx = orca.host.preset.filament.begin_update(slot, name)
            else:
                tx = getattr(orca.host.preset, "process" if ptype == "print" else ptype).begin_update(name)
            tx.commit()
        except RuntimeError as e:
            record([refusal_row(label, "select_cancelled", f"select_cancelled: {name}")])
            raise ApiReply(409, {"error": "select_cancelled", "detail": str(e)})
        except ValueError as e:
            record([refusal_row(label, "select_failed", f"select_failed: {e}")])
            raise ApiReply(422, {"error": "unknown_preset", "detail": str(e)})
        self.notify(f"AI switched to {ptype} preset '{name}'")
        dropped = []
        if before != name:
            try:
                dropped = list(self.pump.call(lambda: self.on_switch(ptype, slot, before, name)) or [])
            except Exception as e:
                trace(f"dropped check after a preset switch failed: {type(e).__name__}: {e}")
            record([{"kind": "preset", "label": label, "value": name, "before": before,
                     "undo_verb": "Switch back", "undo_tip": f"Switch {label} back to {before}",
                     "target": {"kind": "preset", "ptype": ptype, "slot": slot, "value": name,
                                "before": before, "dropped": dropped}}])
        return 200, {"selected": name, "discarded_changes": [],
                     "note": "OrcaSlicer asks on screen about unsaved edits; nothing is discarded silently"}


# ---------------------------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------------------------
class _Server(http.server.HTTPServer):
    allow_reuse_address = False  # on Windows SO_REUSEADDR lets a second socket take the port
    _beat = 0.0

    def service_actions(self):
        now = time.monotonic()
        if now - self._beat > 10:
            self._beat = now
            trace("server loop alive")

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # TCPServer's bind, skipping HTTPServer's getfqdn() lookup (a name query nobody needs).
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port


class TokenBox:
    """The current token; the panel can replace it while the server runs."""

    def __init__(self, value: str):
        self.value = value


CLIENT_HEADER = "X-OrcaSlicer-MCP-Client"
SEEN_WRITE_EVERY = 60.0  # seconds between saves of the same app's last-request time  # the AI app's name, sent by orcaslicer-mcp (shown in the Config tab)


def make_handler(api: Api, token: TokenBox, on_seen=None):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "OrcaSlicerMCPPlugin/" + PLUGIN_VERSION

        def log_message(self, *args):
            pass

        def _reply(self, status: int, body):
            if isinstance(body, RawBody):
                data, ctype = body.data, body.content_type
            else:
                data, ctype = json.dumps(body).encode("utf-8"), "application/json"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve(self, method: str):
            length = self.headers.get("Content-Length")
            try:
                n = int(length) if length else 0
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                self.close_connection = True
                return self._reply(413, {"error": "body_too_large"})
            body = self.rfile.read(n) if n else b""
            trace(f"request {method} {self.path}")
            supplied = self.headers.get("X-Api-Token") or ""
            current = token.value
            if not current or not hmac.compare_digest(supplied.encode(), current.encode()):
                return self._reply(401, {"error": "unauthorized"})
            if on_seen is not None:
                app = "".join(ch for ch in (self.headers.get(CLIENT_HEADER) or "") if 32 <= ord(ch) < 127)[:60].strip()
                try:
                    on_seen(app)
                except Exception:
                    pass
            trial = (self.headers.get("X-Orca-Mcp-Trial") or "").strip() == "1"
            status, reply = api.handle(method, self.path, body, trial=trial)
            trace(f"reply {status} {method} {self.path}")
            self._reply(status, reply)

        def do_GET(self):
            self._serve("GET")

        def do_PUT(self):
            self._serve("PUT")

        def do_POST(self):
            self._serve("POST")

        def do_DELETE(self):
            self._serve("DELETE")

    return Handler


class Service:
    """The server thread and its lifetime. Kept apart from the capability so tests can drive it."""

    def __init__(self, api: Api, token: "TokenBox | str", port: int, host: str = BIND_HOST, on_seen=None):
        self.token = token if isinstance(token, TokenBox) else TokenBox(token)
        self.server = _Server((host, port), make_handler(api, self.token, on_seen))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.5},
                                       name="orcaslicer-mcp-plugin", daemon=False)

    def start(self):
        try:
            self.thread.start()
        except BaseException:
            # Never leave a socket listening with nobody to answer it: clients would hang.
            self.server.server_close()
            raise

    def stop(self, wait: float = 2.0):
        # Not server.shutdown(): it waits forever, and a request may be parked on the UI thread
        # that is calling us. The pump was closed first, so a parked read returns at once.
        self.server._BaseServer__shutdown_request = True
        self.thread.join(wait)
        try:
            self.server.server_close()
        except OSError:
            pass


# ---------------------------------------------------------------------------------------------
# Plugin folder: token and settings
# ---------------------------------------------------------------------------------------------
# The host calls the plugin cannot run without. OrcaSlicer 2.5.0 has them all; an older nightly may not, and then
# the user is told which OrcaSlicer they need instead of a bare AttributeError.
REQUIRED_HOST_CALLS = ("plugin.storage", "preset_bundle", "plater", "app_info", "preset", "ui.create_dock_panel",
                       "ui.push_notification")
NEEDS_NEWER_TEXT = "needs OrcaSlicer 2.5.0 or later (this OrcaSlicer has no {missing}). Update OrcaSlicer, then activate it again."


def missing_host_calls() -> list[str]:
    missing = []
    for dotted in REQUIRED_HOST_CALLS:
        obj = getattr(orca, "host", None)
        for part in dotted.split("."):
            obj = getattr(obj, part, None) if obj is not None else None
        if obj is None:
            missing.append("orca.host." + dotted)
    return missing


def request_permissions(wanted: dict) -> dict:
    """Ask for everything at once, as OrcaSlicer prefers. Some 2.5 builds refuse a keyword they do not know with a
    TypeError (another plugin stopped loading on exactly that); then ask again without the newest keywords, and
    OrcaSlicer asks about the rest the first time the plugin needs it. A refusal by the user, or any other error,
    still stops the plugin loading, as before. Returns what was granted."""
    tries = [wanted]
    if "fs_read" in wanted:
        tries.append({k: v for k, v in wanted.items() if k != "fs_read"})
    tries.append({k: v for k, v in wanted.items() if k == "network_socket"})
    first = None
    for kwargs in tries:
        try:
            orca.request_permissions(**kwargs)
            if kwargs is not wanted:
                trace(f"request_permissions: this build took only {sorted(kwargs)} (first try said: {first})")
            return kwargs
        except TypeError as e:
            first = first or f"{type(e).__name__}: {e}"
            continue
    trace(f"request_permissions: no declaration accepted ({first}); OrcaSlicer asks on first use")
    return {}


def standard_datadir() -> str:
    """OrcaSlicer's settings folder when it isn't started with --datadir (orcaslicer-mcp discovery.py looks there)."""
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming"), "OrcaSlicer")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "OrcaSlicer")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "OrcaSlicer")


def custom_datadir(storage: str) -> str | None:
    """The settings folder OrcaSlicer runs with, when it is not the standard one (then the connector needs
    ORCA_DATADIR to find it). The plugin's storage is <datadir>/orca_plugins/plugin_data/<plugin> when installed
    from a file, and <datadir>/orca_plugins/plugin_data/_subscribed/<user>/<plugin> from OrcaCloud."""
    if not storage:
        return None
    path = os.path.normpath(storage)
    while os.path.basename(path) != "orca_plugins":
        parent = os.path.dirname(path)
        if parent == path:
            return None  # not under an orca_plugins folder: nothing to name
        path = parent
    datadir = os.path.dirname(path)
    norm = lambda x: os.path.normcase(os.path.normpath(x))
    return None if norm(datadir) == norm(standard_datadir()) else datadir


# ---------------------------------------------------------------------------------------------
# Connecting an AI app: one description, rendered by the Config tab and by the panel's first run
# ---------------------------------------------------------------------------------------------
RELEASES_URL = "https://github.com/MaxEllis/orcaslicer-mcp/releases/latest"
_SVG = 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"'
# Plain symbols, all different, none a company's logo (their brand rules need permission first).
SETUP_ICONS = {
    "claude": f'<svg {_SVG}><path d="M5 5h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-8l-4.5 3.5V17H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z"/></svg>',
    "openai": f'<svg {_SVG}><path d="M8.5 4.5c-2 0-2.5 1-2.5 2.5v2.5c0 1.2-.8 2.5-2.5 2.5 1.7 0 2.5 1.3 2.5 2.5V17c0 1.5.5 2.5 2.5 2.5M15.5 4.5c2 0 2.5 1 2.5 2.5v2.5c0 1.2.8 2.5 2.5 2.5-1.7 0-2.5 1.3-2.5 2.5V17c0 1.5-.5 2.5-2.5 2.5"/></svg>',
    "cursor": f'<svg {_SVG}><path d="M5 3.5 18.5 10l-6 1.8-2.4 6.2z"/></svg>',
    "vscode": f'<svg {_SVG}><path d="M8.5 7 3.5 12l5 5M15.5 7l5 5-5 5M13.5 4.5l-3 15"/></svg>',
    "other": f'<svg {_SVG}><path d="M9 3v5M15 3v5M6.5 8h11v3.5a5.5 5.5 0 0 1-11 0zM12 17v4"/></svg>',
}


def setup_apps(datadir: str | None) -> list[dict]:
    """Each AI app and the routes to connect it, with the exact text a Copy copies and the link an Open opens.
    No token anywhere: the connector finds OrcaSlicer by itself (orcaslicer-mcp discovery.py), port included, so
    changing the port later breaks nothing. Only what it can't find is named: OrcaSlicer's own settings folder
    (--datadir)."""
    import base64
    env = {}
    if datadir:
        env["ORCA_DATADIR"] = datadir
    env_args = "".join(f' --env {k}="{v}"' if " " in v else f" --env {k}={v}" for k, v in env.items())

    def server(extra=None):
        o = {**(extra or {}), "command": "uvx", "args": ["orcaslicer-mcp"]}
        if env:
            o["env"] = dict(env)
        return o

    def cmd(cli):
        return f"{cli}{env_args} -- uvx orcaslicer-mcp"
    pretty = lambda o: json.dumps(o, indent=2)
    compact = lambda o: json.dumps(o, separators=(",", ":"))
    quote = lambda t: urllib.parse.quote(t, safe="")
    route = lambda label, hint, act, **kw: {"label": label, "hint": hint, "act": act, "primary": kw.get("primary", False),
                                            "copy": kw.get("copy"), "open": kw.get("open"), "off": kw.get("off", False),
                                            "also": kw.get("also", ""), "copy_label": kw.get("copy_label", "")}
    also = ("Also add " + ", ".join(f"{k} = {v}" for k, v in env.items()) + " as an environment variable.") if env else ""
    return [
        {"id": "claude", "name": "Claude", "where": "Claude", "routes": [
            route("Claude app", "For chats. Download the extension, then open the file.", "Download", primary=True,
                  open=RELEASES_URL, also=("In the extension's settings, set OrcaSlicer settings folder to "
                                           + datadir + ".") if datadir else ""),
            route("Claude Code", "In a terminal, an editor or the app's Code tab. Needs uv.", "Copy command",
                  copy=cmd("claude mcp add --scope user orcaslicer"))]},
        {"id": "openai", "name": "OpenAI", "where": "OpenAI", "routes": [
            route("ChatGPT desktop app", "Settings › Plugins › MCPs › Add, choose STDIO and paste this. Works in Codex "
                  "chats; ordinary ChatGPT chats can't use it yet.", "Copy", primary=True, copy="uvx orcaslicer-mcp",
                  also=also),
            route("Codex in a terminal or editor", "Run this once. The desktop app shares the setting.", "Copy command",
                  copy=cmd("codex mcp add orcaslicer")),
            route("ChatGPT on the web or phone", "Can't connect: it only reaches servers on the internet, not programs "
                  "on your computer.", "", off=True)]},
        {"id": "cursor", "name": "Cursor", "where": "Cursor", "routes": [
            route("Add to Cursor", "Cursor opens and asks you to confirm. Needs uv.", "Add to Cursor", primary=True,
                  open="cursor://anysphere.cursor-deeplink/mcp/install?name=orcaslicer&config="
                       + quote(base64.b64encode(compact(server()).encode()).decode()),
                  copy=pretty({"mcpServers": {"orcaslicer": server()}}), copy_label="Copy settings instead")]},
        {"id": "vscode", "name": "VS Code", "where": "VS Code", "routes": [
            route("Add to VS Code", "VS Code opens and asks you to confirm. Needs uv.", "Add to VS Code", primary=True,
                  open="https://vscode.dev/redirect/mcp/install?name=orcaslicer&config=" + quote(compact(server({"type": "stdio"}))),
                  copy=pretty({"servers": {"orcaslicer": server({"type": "stdio"})}}),
                  copy_label="Copy .vscode/mcp.json settings instead")]},
        {"id": "other", "name": "Other app", "where": "your app", "routes": [
            route("Your app's MCP settings", "LM Studio, Windsurf and most others take this form. Needs uv.",
                  "Copy settings", primary=True, copy=pretty({"mcpServers": {"orcaslicer": server()}}))]},
    ]


def setup_message(datadir: str | None, status: dict | None, paused: bool) -> dict:
    """What the panel shows before the first change: the setup, or once an app has connected, that it has."""
    return {"type": "setup", "apps": [{**a, "icon": SETUP_ICONS[a["id"]]} for a in setup_apps(datadir)],
            "status": status or None, "paused": bool(paused)}


# The OrcaSlicer MCP build has the connection and this panel built in. Inside it the plugin stays off, so there is one
# panel and one server. The build names itself in app_info (orca_mcp_version, from the build after 2.5.0-mcp.16); older
# builds are recognised by the panel page they ship in their resources folder, which OrcaSlicer lets plugins read. The
# bundled Python sits at <install>/python on Windows, Contents/MacOS/python on macOS and <prefix>/lib/python on Linux.
MCP_BUILD_PAGE = os.path.join("web", "orcaslicer_mcp", "panel.html")
IN_BUILD_TEXT = ("not needed here: this is the OrcaSlicer MCP build, which has the connection built in (Preferences > "
                 "Remote API). The plugin stays off; you can deactivate it.")


def app_info() -> dict | None:
    try:
        info = orca.host.app_info()
    except Exception:
        return None
    return info if isinstance(info, dict) else None


def running_in_mcp_build(info=None) -> bool:
    if isinstance(info, dict) and info.get("orca_mcp_version"):
        return True
    home = os.path.normpath(sys.prefix)
    for resources in (os.path.join(home, os.pardir, "resources"),               # Windows
                      os.path.join(home, os.pardir, os.pardir, "Resources"),    # macOS
                      os.path.join(home, os.pardir, os.pardir, "resources")):   # Linux
        if os.path.isfile(os.path.join(resources, MCP_BUILD_PAGE)):
            return True
    return False


# Links the Config tab and the panel may ask the plugin to open (neither page can open anything itself): exactly the
# ones the setup offers, compared whole, so a link that only starts like one is never opened.
OPENABLE = (RELEASES_URL, "https://docs.astral.sh/uv/")
OPEN_REQUEST_MAX_AGE_S = 6  # a Config-tab request older than this (the panel was closed) is dropped, not opened late


def openable(url, datadir: str | None = None) -> bool:
    if not isinstance(url, str):
        return False
    return url in OPENABLE or url in {r["open"] for a in setup_apps(datadir) for r in a["routes"] if r.get("open")}


def open_url(url: str) -> bool:
    """Through the system, as other OrcaSlicer plugins do (os.startfile on Windows, webbrowser elsewhere);
    OrcaSlicer asks the person the first time."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(url)  # noqa: S606 - only links openable() lists reach here
            return True
        import webbrowser
        return bool(webbrowser.open(url))
    except Exception as e:
        trace(f"open link failed: {type(e).__name__}: {e}")
        return False


# The connector finds OrcaSlicer by itself through this file (orcaslicer-mcp discovery.py), so the setup a user
# pastes into their AI app carries no token. Written while the server runs, removed when it stops.
CONNECTION_FILE = "connection.json"
CONNECTION_PRODUCT = "orcaslicer-mcp-plugin"


def write_connection_file(folder: str, port: int, token: str) -> None:
    data = {"product": CONNECTION_PRODUCT, "version": PLUGIN_VERSION, "url": f"http://{BIND_HOST}:{port}",
            "token": token, "pid": os.getpid(), "written": int(time.time())}
    path = os.path.join(folder, CONNECTION_FILE)
    tmp = path + ".tmp"
    # Made owner-only first (where the OS has modes), then written through its PATH. OrcaSlicer's permission check
    # lets a plugin write in its own folder without asking, but only when it can see the path: writing through the
    # file descriptor (os.fdopen) showed it a bare number, and it asked the user about an unnamed "open" (seen on a
    # real OrcaSlicer 2.5, 2026-11-11).
    try:
        os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        _remove_quietly(tmp)
        raise


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def remove_connection_file(folder: str) -> None:
    for name in (CONNECTION_FILE, CONNECTION_FILE + ".tmp"):
        _remove_quietly(os.path.join(folder, name))


def load_or_create_token(folder: str, renew: bool = False) -> str:
    path = os.path.join(folder, "token.txt")
    if not renew:
        try:
            with open(path, encoding="utf-8") as f:
                tok = f.read().strip()
            if len(tok) >= 32:
                return tok
        except OSError:
            pass
    tok = secrets.token_hex(24)
    with open(path, "w", encoding="utf-8") as f:
        f.write(tok + "\n")
    return tok


def load_settings(folder: str) -> dict:
    try:
        with open(os.path.join(folder, "settings.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_port(folder: str) -> int:
    try:
        with open(os.path.join(folder, "settings.json"), encoding="utf-8") as f:
            port = json.load(f).get("port", DEFAULT_PORT)
        if isinstance(port, int) and 1024 <= port <= 65535:
            return port
    except (OSError, ValueError, AttributeError):
        pass
    return DEFAULT_PORT


# ---------------------------------------------------------------------------------------------
# Panel history: a port of the OrcaSlicer MCP build's McpHistory (src/slic3r/GUI/RemoteAPI/McpHistory.cpp),
# with the same JSON. The plugin has no restore points, so there is no start point and no checkpoints.
# ---------------------------------------------------------------------------------------------
BURST_GAP_S = 60
HISTORY_MAX_AGE_S = 30 * 86400
ROW_FIELDS = {"id": 0, "group": 0, "ts": 0, "time": "", "kind": "", "label": "", "value": "", "before": "",
              "where": "", "undo_verb": "", "undo_tip": "", "undone_at": "", "title_hint": "", "target": None,
              "result": None, "error": False, "on_disk": False, "value_tip": "", "before_tip": ""}


class McpHistory:
    def __init__(self):
        self.rows: list[dict] = []
        self.groups: list[dict] = []
        self.closed = True
        self.last_ts = 0
        self.next_row = 1

    def begin_change(self, now_s: int) -> bool:
        opened = not self.groups or self.closed or now_s - self.last_ts >= BURST_GAP_S
        if opened:
            gid = self.groups[-1]["id"] + 1 if self.groups else 1
            self.groups.append({"id": gid, "ts": now_s, "time": "", "checkpoint": "", "checkpoint_failed": False})
        self.closed = False
        self.last_ts = now_s
        return opened

    def add_row(self, row: dict) -> int:
        if not self.groups:
            self.begin_change(row.get("ts", 0))
        r = dict(ROW_FIELDS)
        r.update({k: v for k, v in row.items() if k in ROW_FIELDS})
        r["id"], self.next_row = self.next_row, self.next_row + 1
        r["group"] = self.groups[-1]["id"]
        if not self.groups[-1]["time"]:
            self.groups[-1]["time"] = r["time"]
        self.rows.append(r)
        return r["id"]

    def row(self, row_id: int):
        return next((r for r in self.rows if r["id"] == row_id), None)

    def mark_undone(self, row_id: int, hhmm: str) -> None:
        r = self.row(row_id)
        if r is not None:
            r["undone_at"] = hhmm

    def page_history(self) -> dict:
        rows = []
        for r in self.rows:
            undo = None
            if r["undo_verb"]:
                undo = {"verb": r["undo_verb"]}
                if r["undo_tip"]:
                    undo["tip"] = r["undo_tip"]
            error = (r["where"] or r["value"] or "Failed") if r["error"] else ""
            rows.append({"id": r["id"], "group": r["group"], "ts": r["ts"], "time": r["time"], "kind": r["kind"],
                         "label": r["label"], "value": r["value"], "before": r["before"], "where": r["where"],
                         "undo": undo, "undone_at": r["undone_at"], "title_hint": r["title_hint"] or None,
                         "result": r["result"], "error": error, "on_disk": r["on_disk"]})
            if r["value_tip"]:
                rows[-1]["value_tip"] = r["value_tip"]
            if r["before_tip"]:
                rows[-1]["before_tip"] = r["before_tip"]
        groups = [{"id": g["id"], "ts": g["ts"], "time": g["time"], "restore": None} for g in self.groups]
        return {"rows": rows, "groups": groups}

    def to_json(self) -> dict:
        return {"version": 1, "rows": [dict(r) for r in self.rows],
                "groups": [dict(g) for g in self.groups], "start": None,
                "closed": self.closed, "last_ts": self.last_ts, "next_row": self.next_row}

    @classmethod
    def from_json(cls, j) -> "McpHistory":
        h = cls()
        try:
            if not isinstance(j, dict):
                return h
            for r in j.get("rows", []):
                row = dict(ROW_FIELDS)
                row.update({k: r[k] for k in ROW_FIELDS if k in r})
                h.rows.append(row)
            for g in j.get("groups", []):
                h.groups.append({"id": int(g.get("id", 0)), "ts": int(g.get("ts", 0)), "time": g.get("time", ""),
                                 "checkpoint": "", "checkpoint_failed": bool(g.get("checkpoint_failed", False))})
            h.closed = bool(j.get("closed", True))
            h.last_ts = int(j.get("last_ts", 0))
            h.next_row = max([int(j.get("next_row", 1))] + [r["id"] + 1 for r in h.rows])
        except (TypeError, ValueError, AttributeError):
            return cls()
        return h


def history_key(project_path: str) -> str:
    """'untitled', else 16 hex chars of FNV-1a 64 over the normalised UTF-8 path (as the build's)."""
    if not project_path:
        return "untitled"
    import ntpath
    import posixpath
    if "\\" in project_path or re.match(r"^[A-Za-z]:", project_path):
        norm = ntpath.normpath(project_path).replace("\\", "/")
    else:
        norm = posixpath.normpath(project_path)
    h = 14695981039346656037
    for c in norm.encode("utf-8"):
        h = ((h ^ c) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def _same(a, b) -> bool:
    """The page's comparison: equal text, or equal leading numbers within 1e-6."""
    a, b = "" if a is None else str(a), "" if b is None else str(b)
    if a == b:
        return True
    ma, mb = re.match(r"^\s*(-?\d+(?:\.\d+)?)", a), re.match(r"^\s*(-?\d+(?:\.\d+)?)", b)
    if not (ma and mb):
        return False
    x, y = float(ma.group(1)), float(mb.group(1))
    return abs(x - y) <= 1e-6 * max(1.0, abs(x), abs(y))


# ---------------------------------------------------------------------------------------------
# User-change journal behind GET /api/v1/user_changes: a port of the OrcaSlicer MCP build's
# McpJournal.cpp (same kinds, groups and texts). Every journalled API request compares the project
# with the baseline the previous one left; each difference is something the user did in between.
# The plugin has no plate count, so it never reports plate entries, and it takes a new baseline
# after the two routes that change something (a read leaves the baseline the compare just took).
# ---------------------------------------------------------------------------------------------
JOURNAL_MAX = 200
ARROW = "→"
ELLIPSIS = "…"
# Bookkeeping keys of a preset's config, not settings anyone edits (McpJournal.cpp skipped_key).
JOURNAL_SKIPPED_KEYS = frozenset({
    "inherits", "print_settings_id", "filament_settings_id", "printer_settings_id", "compatible_printers",
    "compatible_prints", "compatible_printers_condition", "compatible_prints_condition", "renamed_from",
    "different_settings_to_system", "version", "name", "from", "setting_id", "base_id", "updated_time",
    "user_id", "filament_id", "instantiation"})
# Routes that take part, and whether they change something (then a new baseline is taken after them).
JOURNAL_ROUTES = {
    ("GET", "/api/v1/status"): False, ("GET", "/api/v1/config"): False, ("PUT", "/api/v1/config"): True,
    ("GET", "/api/v1/presets"): False, ("POST", "/api/v1/preset/config"): False,
    ("PUT", "/api/v1/preset"): True, ("GET", "/api/v1/objects"): False, ("GET", "/api/v1/project"): False,
    ("GET", "/api/v1/user_changes"): False, ("GET", "/api/v1/slice/status"): False, ("GET", "/api/v1/gcode"): False,
}
_SINCE = re.compile(r"[+-]?[0-9]+")


def value_text(key: str, raw) -> str:
    """A setting's value as the timeline shows it (McpRecorder::config_value_text): On/Off, the enum's
    label, the unit, first line only, cut at 40 bytes."""
    v = "" if raw is None else str(raw)
    cut = False
    nl = min((i for i in (v.find("\r"), v.find("\n")) if i >= 0), default=-1)
    if nl >= 0:
        v, cut = v[:nl], True
    labelled = False
    kind = VALUE_KINDS.get(key)
    if not cut and kind is not None:
        if kind == "bool" and v in ("1", "0", "true", "false"):
            v, labelled = ("On" if v in ("1", "true") else "Off"), True
        elif isinstance(kind, dict) and v in kind:
            v, labelled = kind[v], True
    b = v.encode("utf-8")
    if len(b) > 40:
        v, cut = b[:40].decode("utf-8", "ignore"), True
    if cut:
        return v + ELLIPSIS
    unit = LABELS[key][1] if key in LABELS else ""
    if not labelled and v and unit and not v.endswith("%"):
        unit = unit.split(" or ", 1)[0]  # "mm or %" reads as mm for a plain number
        if unit and unit != "%":
            v += " " + unit
    return v


class JournalEntry:
    __slots__ = ("seq", "time", "kind", "group", "text", "key", "form", "ckey", "label", "old_cmp", "new_cmp",
                 "old_text", "new_text", "sensitive", "slots", "slot_suffix", "op")

    def __init__(self, **kw):
        for f in self.__slots__:
            setattr(self, f, kw.get(f, [] if f == "slots" else False if f in ("sensitive", "slot_suffix")
                                    else 0 if f == "seq" else ""))

    def copy(self) -> "JournalEntry":
        e = JournalEntry(**{f: getattr(self, f) for f in self.__slots__})
        e.slots = list(self.slots)
        return e


def _hidden(raw: str) -> str:  # never the value itself: equality is all a sensitive key's entry needs
    return "#" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _slot_suffix(slots) -> str:
    if not slots:
        return ""
    if len(slots) == 1:
        return f" (filament {slots[0]})"
    return " (filaments " + ", ".join(str(s) for s in slots) + ")"


def _slots_key(slots) -> str:
    return "".join(f"{s}," for s in slots)


def journal_entry_text(e: JournalEntry) -> None:
    """Recomposes e.text from its fields (McpJournal.cpp mcp_entry_text)."""
    if e.form == "setting":
        suffix = _slot_suffix(e.slots) if e.slot_suffix else ""
        if e.sensitive:
            e.text = f"{e.label} changed{suffix}"
            return
        shown = lambda v: v if v else "(empty)"  # noqa: E731
        e.text = f"{e.label} {shown(e.old_text)} {ARROW} {shown(e.new_text)}{suffix}"
    elif e.form == "switch":
        e.text = f"{e.label} preset switched to {e.new_text}" + (_slot_suffix(e.slots) if e.slot_suffix else "")
    elif e.form == "count":
        before, after = int(e.old_cmp or 0), int(e.new_cmp or 0)
        d = after - before
        n = abs(d)
        verb = "Added" if d >= 0 else "Removed"
        if e.ckey.startswith("copies:"):
            e.text = verb + (" a copy" if n == 1 else f" {n} copies") + f" of {e.label}"
        elif e.ckey == "filament_count":
            e.text = verb + (f" filament {after if d > 0 else before}" if n == 1 else f" {n} filaments")
        else:
            e.text = verb + (f" a {e.label}" if n == 1 else f" {n} {e.label}s")
    elif e.form == "object":
        e.text = (e.op[:1].upper() + e.op[1:]) + " " + e.label


class JournalText:
    """How entries word a setting: the timeline's label and value, nothing shown for a sensitive key."""

    @staticmethod
    def label(key: str) -> str:
        return display_label(key)

    value = staticmethod(value_text)
    sensitive = staticmethod(is_sensitive)


def _setting_entry(f, group, scope, key, old_raw, new_raw, slots=(), slot_text=False) -> JournalEntry:
    e = JournalEntry(kind="setting", group=group, key=key, form="setting", label=f.label(key),
                     sensitive=bool(f.sensitive(key)), slots=list(slots), slot_suffix=slot_text)
    e.ckey = f"setting:{scope}:{_slots_key(e.slots)}{key}"
    if e.sensitive:
        e.old_cmp, e.new_cmp = _hidden(old_raw), _hidden(new_raw)
    else:
        e.old_cmp, e.new_cmp = old_raw, new_raw
        e.old_text, e.new_text = f.value(key, old_raw), f.value(key, new_raw)
    journal_entry_text(e)
    return e


def _switch_entry(group, type_word, ckey, old_name, new_name, slots=(), slot_text=False) -> JournalEntry:
    e = JournalEntry(kind="preset", group=group, form="switch", ckey=ckey, label=type_word, old_cmp=old_name,
                     old_text=old_name, new_cmp=new_name, new_text=new_name, slots=list(slots),
                     slot_suffix=slot_text)
    journal_entry_text(e)
    return e


def _count_entry(kind, group, ckey, noun, before, after) -> JournalEntry:
    e = JournalEntry(kind=kind, group=group, form="count", ckey=ckey, label=noun, old_cmp=str(before),
                     new_cmp=str(after))
    journal_entry_text(e)
    return e


def _object_entry(oid, name, op) -> JournalEntry:
    e = JournalEntry(kind="object", group="Model", form="object", ckey=f"object:{oid}", label=name, op=op)
    journal_entry_text(e)
    return e


def _diff_config(f, group, scope, type_word, a, b, out) -> None:
    if a["preset"] != b["preset"]:
        out.append(_switch_entry(group, type_word, "preset:" + scope, a["preset"], b["preset"]))
        for k, saved in sorted(b["saved"].items()):
            if k in JOURNAL_SKIPPED_KEYS:
                continue
            v = b["values"].get(k)
            if v is not None and v != saved:
                out.append(_setting_entry(f, group, scope, k, saved, v))
        return
    for k, v in sorted(b["values"].items()):
        if k in JOURNAL_SKIPPED_KEYS:
            continue
        old = a["values"].get(k)
        if old is not None and old != v:
            out.append(_setting_entry(f, group, scope, k, old, v))


def _diff_filaments(f, a, b, out) -> None:
    sa, sb = a["filament_slots"], b["filament_slots"]
    na, nb = len(sa), len(sb)
    many = nb > 1
    for i in range(min(na, nb)):
        if sa[i] != sb[i]:
            out.append(_switch_entry("Filament", "Filament", f"preset:filament:{i + 1}", sa[i], sb[i], [i + 1], many))
    done = set()
    for name in sb:
        if name in done:
            continue
        done.add(name)
        bs = b["filaments"].get(name)
        if bs is None:
            continue
        same, switched = [], []
        for s in range(min(na, nb)):
            if sb[s] == name:
                (same if sa[s] == name else switched).append(s + 1)
        aset = a["filaments"].get(name)
        if same and aset is not None:
            for k, v in sorted(bs["values"].items()):
                if k in JOURNAL_SKIPPED_KEYS:
                    continue
                old = aset["values"].get(k)
                if old is not None and old != v:
                    out.append(_setting_entry(f, "Filament", "filament", k, old, v, same, many))
        if switched:
            for k, saved in sorted(bs["saved"].items()):
                if k in JOURNAL_SKIPPED_KEYS:
                    continue
                v = bs["values"].get(k)
                if v is not None and v != saved:
                    out.append(_setting_entry(f, "Filament", "filament", k, saved, v, switched, many))
    if na != nb:
        out.append(_count_entry("preset", "Filament", "filament_count", "filament", na, nb))


def _close_enough(x: float, y: float) -> bool:
    return abs(x - y) <= 1e-6 * max(abs(x), abs(y), 1.0)


def _vec_differs(p, q) -> bool:
    return any(not _close_enough(float(x), float(y)) for x, y in zip(p, q))


def transform_change(a, b) -> str:
    """The strongest change between two placements (offset, rotation, scale, mirror): rotated > scaled >
    moved, or "". The build compares matrices; a mirror flips a column there, so it counts as rotated."""
    if _vec_differs(a[1], b[1]) or _vec_differs(a[3], b[3]):
        return "rotated"
    if _vec_differs(a[2], b[2]):
        return "scaled"
    return "moved" if _vec_differs(a[0], b[0]) else ""


def _op_rank(op: str) -> int:
    return {"rotated": 3, "scaled": 2, "moved": 1}.get(op, 0)


# The project settings the build's PUT /config writes (Controller::writable_project_keys), compared as group
# "Project". filament_colour is compared per slot from its own list (b["filament_colours"]).
PROJECT_JOURNAL_KEYS = ("curr_bed_type", "flush_multiplier", "flush_multiplier_fast", "flush_volumes_matrix",
                        "prime_volume_mode", "wipe_tower_x", "wipe_tower_y")
_DEFAULT_CMP = "\x1f(default)"  # compared only, never shown


def _diff_object_config(f, p, o, out) -> None:
    """An object's own settings: "<label> of <name> <old> -> <new>", "default" for a key it does not set."""
    pc, oc = p.get("config") or {}, o.get("config") or {}
    for k in sorted(set(pc) | set(oc)):
        has_old, has_new = k in pc, k in oc
        if has_old and has_new and pc[k] == oc[k]:
            continue
        e = _setting_entry(f, "Model", f"object:{o['id']}", k, pc.get(k, ""), oc.get(k, ""))
        e.label = f.label(k) + " of " + o["name"]
        if not has_old:
            e.old_cmp = _hidden(_DEFAULT_CMP) if e.sensitive else _DEFAULT_CMP
            if not e.sensitive:
                e.old_text = "default"
        if not has_new:
            e.new_cmp = _hidden(_DEFAULT_CMP) if e.sensitive else _DEFAULT_CMP
            if not e.sensitive:
                e.new_text = "default"
        journal_entry_text(e)
        out.append(e)


def _diff_project(f, a, b, out) -> None:
    """The project settings (group "Project"). Keys sized by the filament or plate count are compared only while
    that count stays: a new filament or plate resizes them, which is not an edit of its own. The plugin cannot
    read the plate count, so for the wipe tower it compares only while the per-plate list keeps its length."""
    same_filaments = len(a["filament_slots"]) == len(b["filament_slots"])
    ap, bp = a.get("project") or {}, b.get("project") or {}
    for k, v in sorted(bp.items()):
        if not same_filaments and k in ("flush_volumes_matrix", "flush_volumes_vector"):
            continue
        old = ap.get(k)
        if old is None or old == v:
            continue
        if k in ("wipe_tower_x", "wipe_tower_y"):
            if a.get("plates") is not None and b.get("plates") is not None:
                if a["plates"] != b["plates"]:
                    continue
            elif old.count(",") != v.count(","):
                continue
        out.append(_setting_entry(f, "Project", "project", k, old, v))
    ac, bc = a.get("filament_colours") or [], b.get("filament_colours") or []
    if not same_filaments or len(ac) != len(bc):
        return
    many = len(bc) > 1
    for i, (x, y) in enumerate(zip(ac, bc)):
        if x != y:
            out.append(_setting_entry(f, "Project", "project", "filament_colour", x, y, [i + 1], many))


def _diff_objects(a, b, out, f=None) -> None:
    before = {o["id"]: o for o in a["objects"]}
    seen = set()
    for o in b["objects"]:
        seen.add(o["id"])
        p = before.get(o["id"])
        if p is None:
            out.append(_object_entry(o["id"], o["name"], "added"))
            continue
        op = ""
        for iid, t in zip(o["instance_ids"], o["transforms"]):
            for pid, pt in zip(p["instance_ids"], p["transforms"]):
                if pid == iid:
                    c = transform_change(pt, t)
                    if _op_rank(c) > _op_rank(op):
                        op = c
        if op:
            out.append(_object_entry(o["id"], o["name"], op))
        if f is not None:
            _diff_object_config(f, p, o, out)
        if len(o["instance_ids"]) != len(p["instance_ids"]):
            e = _count_entry("object", "Model", f"copies:{o['id']}", "copy", len(p["instance_ids"]),
                             len(o["instance_ids"]))
            e.label = o["name"]  # "Added a copy of <name>"
            journal_entry_text(e)
            out.append(e)
    for o in a["objects"]:
        if o["id"] not in seen:
            out.append(_object_entry(o["id"], o["name"], "removed"))


def journal_diff(a: dict, b: dict, f=JournalText) -> list:
    """The differences between two baselines, as entries without seq and time (McpJournal.cpp mcp_diff)."""
    if a["project_gen"] != b["project_gen"]:
        return [JournalEntry(kind="project", group="Project", form="fixed",
                             text=b["project_text"] or "Opened a project")]
    out: list = []
    _diff_config(f, "Printer", "printer", "Printer", a["printer"], b["printer"], out)
    _diff_config(f, "Print", "print", "Print", a["print"], b["print"], out)
    _diff_filaments(f, a, b, out)
    _diff_project(f, a, b, out)
    _diff_objects(a, b, out, f)
    if a.get("plates") is not None and b.get("plates") is not None and a["plates"] != b["plates"]:
        out.append(_count_entry("plate", "Plates", "plates", "plate", a["plates"], b["plates"]))
    return out


def journal_reply(entries, since: int, latest_seq: int) -> dict:
    """GET /user_changes: the entries newer than `since`, oldest first, coalesced per key and with the bulk
    rules applied (McpJournal.cpp mcp_journal_reply)."""
    reset = since > latest_seq
    win = [e.copy() for e in entries if reset or e.seq > since]
    for i in range(len(win) - 1, -1, -1):  # opening a project or starting a new one replaces everything before
        if win[i].kind == "project":
            win = win[i:]
            break
    out: list = []  # in seq order: a coalesced entry moves to its last part's place

    def find(ckey):
        return next((i for i, o in enumerate(out) if o.ckey == ckey), -1)

    for e in win:
        if e.form == "switch":
            filament = e.ckey.startswith("preset:filament:")
            i = 0
            while i < len(out):
                o = out[i]
                if o.form != "setting" or o.group != e.group:
                    i += 1
                    continue
                if filament:
                    slot = e.slots[0] if e.slots else 0
                    if slot not in o.slots:
                        i += 1
                        continue
                    o.slots.remove(slot)
                    if o.slots:
                        o.ckey = f"setting:filament:{_slots_key(o.slots)}{o.key}"
                        journal_entry_text(o)
                        i += 1
                        continue
                del out[i]
        at = find(e.ckey) if e.ckey else -1
        if at < 0:
            out.append(e)
            continue
        cur = out.pop(at)
        if e.form == "object":
            if cur.op == "added":
                cur.op = "" if e.op == "removed" else "added"
            elif e.op == "removed":
                cur.op = "removed"
            elif cur.op != "removed" and _op_rank(e.op) > _op_rank(cur.op):
                cur.op = e.op
            elif cur.op == "removed":
                cur.op = e.op  # an id never comes back; take the latest
            cur.label = e.label
            if not cur.op:
                continue  # added and removed again: nothing happened
        elif e.old_cmp == cur.new_cmp:
            cur.new_cmp, cur.new_text, cur.label = e.new_cmp, e.new_text, e.label
        else:
            cur = e  # changed by the API in between: what the assistant knows is the later old value
        cur.seq, cur.time = e.seq, e.time
        journal_entry_text(cur)
        out.append(cur)

    changes = []
    for e in out:
        if e.form not in ("fixed", "object") and e.old_cmp == e.new_cmp:
            continue  # changed back
        c = {"seq": e.seq, "time": e.time, "kind": e.kind, "group": e.group, "text": e.text}
        if e.key:
            c["key"] = e.key
        changes.append(c)
    reply = {"seq": latest_seq, "changes": changes}
    if reset:
        reply["reset"] = True
    return reply


class Journal:
    """The user-change journal. Any thread; the baseline itself is read on the UI thread by the caller."""

    def __init__(self, first_seq: int | None = None, text=JournalText):
        # Seqs start at the clock in milliseconds, so they keep growing across OrcaSlicer restarts.
        self._seq = int(time.time() * 1000) if first_seq is None else first_seq
        self._text = text
        self._lock = threading.Lock()
        self._entries: collections.deque = collections.deque()
        self._baseline = None
        self._project_gen = 0
        self._project_text = ""

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def project_gen(self) -> int:
        return self._project_gen

    @property
    def project_text(self) -> str:
        return self._project_text

    def _push(self, e: JournalEntry, hhmm: str) -> None:
        self._seq += 1
        e.seq, e.time = self._seq, hhmm
        self._entries.append(e)
        while len(self._entries) > JOURNAL_MAX:
            self._entries.popleft()

    def begin_request(self, now: dict, hhmm: str) -> None:
        """Start of a request (or a panel action): the differences from the baseline become entries (none
        before the first baseline), and `now` is the baseline from here on."""
        with self._lock:
            if self._baseline is not None:
                for e in journal_diff(self._baseline, now, self._text):
                    self._push(e, hhmm)
            self._baseline = now

    def end_request(self, now: dict) -> None:
        with self._lock:
            self._baseline = now

    def invalidate(self) -> None:
        """No baseline could be taken after a change: the next begin only takes one, rather than report the
        API's own change as the user's."""
        with self._lock:
            self._baseline = None

    def add(self, kind: str, group: str, text: str, hhmm: str) -> None:
        with self._lock:
            self._push(JournalEntry(kind=kind, group=group, text=text, form="fixed"), hhmm)

    def note_project(self, text: str) -> None:
        """OrcaSlicer opened a project or started a new one: the next baseline carries a new generation."""
        with self._lock:
            self._project_gen += 1
            self._project_text = text

    def since(self, seq: int) -> dict:
        with self._lock:
            return journal_reply(list(self._entries), seq, self._seq)


def capture_baseline(journal: Journal) -> dict:
    """UI thread: the project now, as the journal compares it (McpJournalCapture.cpp mcp_capture_baseline):
    the selected presets and their edited values, every filament slot, the project settings, the objects with
    their own settings and their copies. The plugin API reads project settings only through the full config,
    whose filament colours are the filament presets' (already compared as Filament settings), not the slots'
    own colours in the sidebar: those stay unread ("filament_colours" is empty)."""
    bundle = orca.host.preset_bundle()

    def snap_edited(coll) -> dict:
        ed, sel = coll.edited_preset(), coll.selected_preset()
        values, saved = {}, {}
        for k in ed.config_keys():
            if k in JOURNAL_SKIPPED_KEYS:
                continue
            v = ed.config_value(k)
            if v is None:
                continue
            values[k] = v
            s = sel.config_value(k)
            if s is not None and s != v:
                saved[k] = s
        return {"preset": coll.selected_preset_name(), "values": values, "saved": saved}

    b = {"project_gen": journal.project_gen, "project_text": journal.project_text,
         "print": snap_edited(bundle.prints), "printer": snap_edited(bundle.printers),
         "filament_slots": list(bundle.current_filament_preset_names()), "filaments": {}, "objects": [],
         "plates": None, "project": {}, "filament_colours": []}
    for k in PROJECT_JOURNAL_KEYS:
        v = bundle.full_config_value(k)
        if v is not None:
            b["project"][k] = v
    # Only the preset the Filament tab edits holds unsaved edits; every other slot shows its saved preset.
    edited = bundle.filaments.selected_preset_name()
    for name in b["filament_slots"]:
        if name in b["filaments"]:
            continue
        if name == edited:
            b["filaments"][name] = snap_edited(bundle.filaments)
            continue
        p = bundle.filaments.find_preset(name)
        values = {}
        if p is not None:
            for k in p.config_keys():
                if k not in JOURNAL_SKIPPED_KEYS:
                    v = p.config_value(k)
                    if v is not None:
                        values[k] = v
        b["filaments"][name] = {"preset": name, "values": values, "saved": {}}
    for mo in orca.host.plater().model().objects():
        insts = mo.instances()
        config = {}
        for k in mo.config_keys():
            v = mo.config_value(k)
            if v is not None:
                config[k] = v
        b["objects"].append({
            "id": int(mo.id()), "name": mo.name, "instance_ids": [int(i.id()) for i in insts], "config": config,
            "transforms": [(tuple(i.offset()), tuple(i.rotation()), tuple(i.scaling_factor()), tuple(i.mirror()))
                           for i in insts]})
    return b


def parse_since(target: str) -> int:
    """The `since` query value (the last one wins, as in the build); 0 when absent."""
    since = 0
    query = target.split("?", 1)[1] if "?" in target else ""
    for kv in query.split("&") if query else []:
        if kv.startswith("since="):
            v = kv[6:]
            if not _SINCE.fullmatch(v):
                raise ApiReply(400, {"error": "bad_since", "detail": "since must be an integer seq"})
            since = int(v)
    return since


SETTINGS_HINT = "Main menu (beside File) > Plugins > OrcaSlicer MCP > Config"
# Shown when the user closes the panel (Max: "show one of the small banners ... where to get it back and also have
# an undo there"). 2.5 has no View-menu entry for a plugin's pane; its script entry is in the Speed Dial (Space, or
# Main menu > Open Speed Dial) and on its row in the Plugins dialog (Run). The panel also comes back by itself at the
# assistant's next request: it is how the plugin reaches OrcaSlicer's UI thread.
PANEL_CLOSED_TEXT = ("OrcaSlicer MCP panel closed. It comes back when your assistant next uses OrcaSlicer, "
                     "or press Space and pick OrcaSlicer MCP.")
# The panel protocol this host implements (the page's McpPanelCore.PROTOCOL_VERSION; see the fork's
# docs/remote-api/README.md, "MCP panel page and protocol"). tests/test_embedded.py checks the two agree.
PANEL_PROTOCOL_VERSION = 1
CAPS = {"restore": False, "kinds": ["setting", "preset", "refused", "slice"], "open_settings": False,
        "settings_hint": SETTINGS_HINT, "settings_label": "Assistant settings",
        "off_text": "Assistant access is off.", "tracked_live": True}  # R43: live entries for tracked rows only


class PanelHost:
    """The plugin's side of the shared panel: records rows, answers live values, undoes, keeps history per
    project. record() may run on any thread; everything else runs on the UI thread except _apply_undo,
    which runs on its own worker because OrcaSlicer's preset transactions must not run on the UI thread."""

    def __init__(self, folder: str, post, notify, clock=time.time):
        self.folder, self.post, self.notify, self.clock = folder, post, notify, clock
        self.lock = threading.RLock()
        self.hist = McpHistory()
        self.key = "untitled"   # an untitled project keeps only the latest session
        self._live_last = None
        # User-change journal hooks, set by the plugin: before a panel action (UI thread), and after an
        # undo went through (worker thread) with its line, "Reverted in the panel: ...".
        self.on_action = lambda: None
        self.on_undone = lambda text: None
        self._remove_stale()

    # -- files ------------------------------------------------------------------------------
    def _path(self, key: str) -> str:
        return os.path.join(self.folder, "mcp_history", key, "history.json")

    def _save_locked(self) -> None:
        path = self._path(self.key)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.hist.to_json(), f)
            os.replace(tmp, path)
        except OSError as e:
            trace(f"history save failed: {e}")

    def _load(self, key: str) -> McpHistory:
        try:
            with open(self._path(key), encoding="utf-8") as f:
                return McpHistory.from_json(json.load(f))
        except (OSError, ValueError):
            return McpHistory()

    def _remove_stale(self) -> None:
        root = os.path.join(self.folder, "mcp_history")
        try:
            names = os.listdir(root)
        except OSError:
            return
        now = self.clock()
        for name in names:
            d = os.path.join(root, name)
            try:
                if os.path.isdir(d) and now - os.path.getmtime(d) > HISTORY_MAX_AGE_S:
                    import shutil
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass

    # -- messages ---------------------------------------------------------------------------
    def history_msg(self) -> dict:
        with self.lock:
            page = self.hist.page_history()
            tracked = self._tracked_locked()
            superseded = {r["id"] for r in self.hist.rows
                          if r["undo_verb"] and not r["undone_at"] and r["id"] not in tracked}
            dropped = self._dropped_locked()
        for r in page["rows"]:  # R43: a later row, not undone, changed the same setting or preset slot
            r["superseded"] = r["id"] in superseded
            r["dropped"] = r["id"] in dropped
        return {"type": "history", "version": PANEL_PROTOCOL_VERSION, "rows": page["rows"],
                "groups": page["groups"], "capabilities": dict(CAPS), "restored_to": None,
                "restore_failed": False, "can_undo_restore": False}

    def _hhmm(self, ts=None) -> str:
        return time.strftime("%H:%M", time.localtime(self.clock() if ts is None else ts))

    def record(self, rows: list, close: bool = False, new_group: bool = False) -> None:
        """new_group: the rows open a group of their own; close: they end it, so the next change opens a new one
        (both for a connector's slice note)."""
        if not rows:
            return
        with self.lock:
            now = int(self.clock())
            if new_group:
                self.hist.closed = True
            self.hist.begin_change(now)
            for r in rows:
                self.hist.add_row(dict(r, ts=now, time=self._hhmm(now)))
            if close:
                self.hist.closed = True
            self._save_locked()
        self.post(self.history_msg())

    # -- live values (UI thread) ------------------------------------------------------------
    @staticmethod
    def live_for(target, row=None):
        """row: the history row, for a multi-line setting row (its live value is judged on the raws)."""
        if not isinstance(target, dict):
            return None
        bundle = orca.host.preset_bundle()
        if target.get("kind") == "preset":
            ptype = target.get("ptype")
            if ptype == "filament":
                names = list(bundle.current_filament_preset_names())
                slot = int(target.get("slot") or 1)
                return {"value": names[slot - 1] if slot <= len(names) else "", "present": slot <= len(names)}
            return {"value": _collection(bundle, ptype).selected_preset_name(), "present": True}
        if target.get("kind") == "setting":
            group, key = target.get("group"), target.get("key")
            coll = _collection(bundle, group)
            preset = coll.edited_preset()
            if group == "filament":
                names = list(bundle.current_filament_preset_names())
                slot = int(target.get("slot") or 1)
                name = names[slot - 1] if slot <= len(names) else ""
                if name and name != preset.name:
                    preset = coll.find_preset(name) or preset
            raw = preset.config_value(key)
            if target.get("multiline") and row is not None:
                return {"value": multiline_live(key, row, raw), "present": True}
            return {"value": setting_value(key, raw), "present": True}
        return None

    @staticmethod
    def _track_key(target):
        """What a row changes: one setting (per filament slot) or one preset slot. None for anything else."""
        if not isinstance(target, dict):
            return None
        if target.get("kind") == "setting":
            group = target.get("group")
            return ("setting", group, target.get("key"), target.get("slot") if group == "filament" else None)
        if target.get("kind") == "preset":
            ptype = target.get("ptype")
            return ("preset", ptype, target.get("slot") if ptype == "filament" else None)
        return None

    def _tracked_locked(self) -> dict:
        """Ruling R43: row id -> target for the LATEST row of each setting that is not reverted, whichever group it
        is in. Only these get live values, so an earlier row of a setting the assistant changed again never reads
        "changed by you"; reverting the later row hands tracking back to the earlier one."""
        rows = self.hist.rows
        dropped = self._dropped_locked()
        latest = {}
        for i, r in enumerate(rows):
            if not r["undo_verb"] or r["undone_at"] or r["id"] in dropped:
                continue
            if self._switched_away(r, rows[i + 1:]):
                continue
            k = self._track_key(r["target"])
            latest[k if k is not None else ("row", r["id"])] = r
        return {r["id"]: r["target"] for r in latest.values()}

    @staticmethod
    def _switched_away(row: dict, later: list) -> bool:
        """A setting row written to a preset the assistant later switched away from: the newest of its later
        preset rows (not undone) for that type (and filament slot) decides, as in the build's mcp_tracking. A switch
        the user made has no row, so it never untracks anything."""
        t = row["target"]
        if not isinstance(t, dict) or t.get("kind") != "setting":
            return False
        group, slot = t.get("group"), t.get("slot")
        newest = None
        for r in later:
            pt = r["target"]
            if r["undo_verb"] and not r["undone_at"] and isinstance(pt, dict) and pt.get("kind") == "preset" \
                    and pt.get("ptype") == group and (group != "filament" or pt.get("slot") == slot):
                newest = pt
        return newest is not None and newest.get("before") == t.get("preset")

    def _dropped_locked(self) -> set:
        """Rows whose edit an assistant preset switch dropped: kept on that switch's row (target "dropped"), as the
        build keeps "discarded" on its switch rows, so history.json has the build's row fields. A switch back does
        not bring a dropped edit back."""
        out = set()
        for r in self.hist.rows:
            t = r["target"]
            if isinstance(t, dict) and t.get("kind") == "preset":
                out.update(i for i in t.get("dropped", []) if isinstance(i, int))
        return out

    def dropped_after_switch(self, ptype: str, slot: int, before: str, name: str) -> list:  # UI thread
        """After the assistant's own preset switch: a setting row written to the preset it left whose edit is now
        neither on the new preset (transferred) nor saved in the old one was dropped (OrcaSlicer asked, and the
        person chose to discard). Such a row is shown as dropped, never as current, and has no undo."""
        bundle = orca.host.preset_bundle()
        coll = _collection(bundle, ptype)
        old = coll.find_preset(before)
        with self.lock:
            gone = self._dropped_locked()
            open_rows = [dict(r) for r in self.hist.rows
                         if r["undo_verb"] and not r["undone_at"] and r["id"] not in gone
                         and isinstance(r["target"], dict) and r["target"].get("kind") == "setting"
                         and r["target"].get("group") == ptype and r["target"].get("preset") == before
                         and (ptype != "filament" or r["target"].get("slot") == slot)]
        dropped = []
        for r in open_rows:
            key = r["target"].get("key")
            now = setting_value(key, coll.edited_preset().config_value(key))
            kept = setting_value(key, old.config_value(key)) if old is not None else None
            if not _same(now, r["value"]) and not (kept is not None and _same(kept, r["value"])):
                dropped.append(r["id"])
        return dropped

    def live_values(self) -> dict:
        with self.lock:
            rows = [(rid, target, dict(self.hist.row(rid))) for rid, target in self._tracked_locked().items()]
        out = {}
        for rid, target, row in rows:
            try:
                v = self.live_for(target, row)
            except Exception:
                v = None
            if v is not None:
                out[str(rid)] = v
        return out

    def push_live(self, force: bool = False) -> None:
        values = self.live_values()
        if force or values != self._live_last:
            self._live_last = values
            self.post({"type": "live", "values": values})

    # -- undo -------------------------------------------------------------------------------
    def _result(self, row_id, status, reason=""):
        msg = {"type": "result", "action": "undo", "row": row_id, "status": status}
        if reason:
            msg["reason"] = reason
        self.post(msg)

    def undo(self, row_id) -> None:  # UI thread
        """Answers exactly one result, also when it refuses or fails before the worker starts."""
        try:
            self._undo(row_id)
        except Exception as e:
            trace(f"undo of row {row_id} failed: {type(e).__name__}: {e}")
            self._result(row_id, "failed", "Couldn't undo it")

    def _undo(self, row_id) -> None:  # UI thread
        try:
            self.on_action()  # the user's edits before this action stay theirs in the journal
        except Exception as e:
            trace(f"journal before undo failed: {e}")
        with self.lock:
            row = self.hist.row(row_id)
            row = dict(row) if row else None
        if not row or not row["undo_verb"] or row["undone_at"]:
            return self._result(row_id, "failed", "Nothing to revert")
        with self.lock:
            gone = row_id in self._dropped_locked()
        if gone:
            return self._result(row_id, "failed", "Dropped by a preset switch")
        with self.lock:
            superseded = row_id not in self._tracked_locked()
        if superseded:  # R43: the assistant changed it again later; that later row is the one to revert
            return self._result(row_id, "failed", "Changed again since")
        target = row["target"] or {}
        live = self.live_for(target, row)
        if live is None or not live["present"]:
            return self._result(row_id, "failed", "It is no longer there")
        expected = row["value"]
        if not _same(live["value"], expected):
            return self._result(row_id, "failed", "You changed it since")
        threading.Thread(target=self._apply_undo, args=(row_id, row["label"], dict(target), row["before"]),
                         name="orcaslicer-mcp-undo", daemon=False).start()

    def _apply_undo(self, row_id, label, t, before="") -> None:  # worker thread
        status, reason = "ok", ""
        try:
            if t.get("kind") == "setting":
                group = t["group"]
                editor = getattr(orca.host.preset, "process" if group == "print" else group)
                tx = editor.begin_update(int(t.get("slot") or 1)) if group == "filament" else editor.begin_update()
                tx.set(t["key"], "" if t.get("before") is None else str(t["before"]))
                tx.commit()
            elif t.get("kind") == "preset":
                ptype = t["ptype"]
                if ptype == "filament":
                    tx = orca.host.preset.filament.begin_update(int(t.get("slot") or 1), t["before"])
                else:
                    tx = getattr(orca.host.preset, "process" if ptype == "print" else ptype).begin_update(t["before"])
                tx.commit()
            else:
                status, reason = "failed", "This change can't be reverted"
        except RuntimeError as e:  # a selection the person at the screen declined, or a changed target
            status, reason = ("cancelled", "") if t.get("kind") == "preset" else ("failed", str(e))
        except ValueError as e:
            status, reason = "failed", str(e)
        except Exception as e:
            status, reason = "failed", f"{type(e).__name__}: {e}"
        if status == "ok":
            with self.lock:
                self.hist.mark_undone(row_id, self._hhmm())
                self._save_locked()
            self.post(self.history_msg())
            self.notify(f"reverted {label}")
            line = (f"Reverted in the panel: {label} back to {before}" if t.get("kind") == "setting"
                    else f"Switched back in the panel: {label} to {before}")
            try:
                self.on_undone(line)
            except Exception as e:
                trace(f"journal after undo failed: {e}")
        self._result(row_id, status, reason)

    # -- projects (UI thread) ---------------------------------------------------------------
    def project_event(self, event: str, path: str) -> None:
        new_key = history_key(path)
        with self.lock:
            if event == "ProjectAfterSave":
                if new_key != self.key:
                    old_key, self.key = self.key, new_key
                    self._save_locked()  # the saved path's history starts from here
                    if old_key == "untitled":
                        try:
                            os.remove(self._path(old_key))
                        except OSError:
                            pass
            elif event in ("ProjectOpened", "NewProject", "ProjectClosed"):
                self.key = new_key
                self.hist = McpHistory() if new_key == "untitled" else self._load(new_key)
                self._live_last = None
            else:
                return
        self.post(self.history_msg())

    def save(self) -> None:
        with self.lock:
            if self.hist.rows:
                self._save_locked()


# ---------------------------------------------------------------------------------------------
# Panel page: the shared page (PANEL_HTML, generated below) plus the plugin's UI-thread pump.
# ---------------------------------------------------------------------------------------------
PUMP_JS = """<script>
/* OrcaSlicer MCP: the plugin can run Python on OrcaSlicer's UI thread only from this page's
   messages. It asks with {t:"run"}; the page answers, and ticks so live values stay current. */
(function () {
  if (!window.orca || !window.orca.onMessage) return;
  window.orca.onMessage(function (m) { if (m && m.t === "run") window.orca.postMessage({ t: "run", id: m.id }); });
  setInterval(function () { window.orca.postMessage({ t: "tick" }); }, 500);
})();
</script>
"""


def panel_page() -> str:
    i = PANEL_HTML.rfind("</body>")
    return PANEL_HTML[:i] + PUMP_JS + PANEL_HTML[i:] if i >= 0 else PANEL_HTML + PUMP_JS


CONFIG_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
:root{--bg:var(--orca-bg,#2b2b2b);--fg:var(--orca-fg,#fafafa);--muted:var(--orca-muted,#b2b3b5);
--line:var(--orca-border,#36363b);--accent:var(--orca-accent,#00675b);--accent-fg:var(--orca-accent-fg,#fff);
--edge:color-mix(in srgb,var(--fg) 22%,var(--bg));--soft:color-mix(in srgb,var(--fg) 6%,var(--bg));
--ok:#3fb27f;--wait:#d9a441;--off:#8a8a90;--bad:#e5484d}
*{box-sizing:border-box}
body{margin:0;padding:12px 16px 16px;max-width:640px;background:var(--bg);color:var(--fg);font:13px/1.45 var(--orca-font,"Segoe UI",system-ui,sans-serif)}
button{font:inherit;color:inherit;cursor:pointer}
.status{display:flex;align-items:flex-start;gap:10px;padding:2px 0 12px;border-bottom:1px solid var(--line)}
.dot{width:9px;height:9px;border-radius:50%;flex:none;margin-top:6px}
.dot.wait{background:var(--wait)} .dot.ok{background:var(--ok)} .dot.off{background:var(--off)}
.status .what{flex:1;min-width:0} .status .what b{font-weight:600;font-size:14px;display:block}
.status .what span{color:var(--muted);font-size:12px}
.switch{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px;background:none;border:0;padding:0;margin-top:2px}
.track{width:32px;height:18px;border-radius:9px;background:var(--edge);position:relative;transition:background .15s}
.track::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:var(--fg);transition:left .15s}
.switch[aria-checked=true] .track{background:var(--accent)} .switch[aria-checked=true] .track::after{left:16px}
h2{font-size:13px;font-weight:600;margin:14px 0 8px}
.apps{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
.apps button{aspect-ratio:1/1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:7px;
border:1px solid var(--line);background:var(--soft);border-radius:8px;padding:6px 4px;font-size:11.5px;line-height:1.2;color:var(--muted)}
.apps button svg{width:28px;height:28px;flex:none}
.apps button:hover{border-color:var(--edge);color:var(--fg)}
.apps button[aria-pressed=true]{border:1.5px solid var(--accent);color:var(--fg);background:color-mix(in srgb,var(--accent) 16%,var(--bg))}
.step{margin:12px 0 0}
.routes{display:flex;flex-direction:column;gap:4px;margin-top:6px}
.route{display:grid;grid-template-columns:1fr auto;gap:2px 10px;align-items:center;padding:7px 10px;border:1px solid var(--line);border-radius:6px}
.route b{font-weight:600} .route small,.step small{display:block;color:var(--muted);font-size:12px}
.route.off{border-style:dashed} .route.off b{color:var(--muted);font-weight:500}
.act{border-radius:4px;padding:4px 12px;border:1px solid var(--edge);background:transparent;white-space:nowrap}
.act:hover{border-color:var(--accent)} .act.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}
.done{color:var(--ok);font-size:12px;white-space:nowrap} .warn{color:var(--bad);font-size:12px} .note{color:var(--muted);font-size:12px}
.peek{grid-column:1 / 3;margin-top:2px} .peek summary{color:var(--muted);font-size:12px;cursor:pointer;list-style:none;width:max-content}
.peek summary::-webkit-details-marker{display:none} .peek summary::before{content:"▸ ";font-size:10px} .peek[open] summary::before{content:"▾ "}
.peek pre{margin:6px 0 0;padding:7px 9px;border:1px solid var(--line);border-radius:4px;background:var(--soft);
font:12px/1.4 Consolas,"Cascadia Mono",monospace;white-space:pre-wrap;word-break:break-all;max-height:8em;overflow:auto;color:var(--muted)}
.lnk{background:none;border:0;padding:0;color:var(--muted);text-decoration:underline;text-underline-offset:2px;font-size:12px}
.lnk.warn{color:var(--bad)}
.again{margin-top:12px}
details.adv{margin-top:16px;border-top:1px solid var(--line);padding-top:10px}
details.adv>summary{cursor:pointer;color:var(--muted);list-style:none} details.adv>summary::-webkit-details-marker{display:none}
details.adv>summary::before{content:"▸ ";font-size:10px} details.adv[open]>summary::before{content:"▾ "}
.form{display:grid;grid-template-columns:70px 1fr;column-gap:10px;row-gap:4px;align-items:center;margin-top:10px}
.form label{color:var(--muted)} .form .hint{color:var(--muted);font-size:12px} .form .gap{margin-top:10px}
.inline{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.tok{font:12px Consolas,"Cascadia Mono",monospace;padding:4px 8px;border:1px solid var(--line);border-radius:4px;min-width:0;flex:1;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap;user-select:none} .tok.shown{white-space:normal;word-break:break-all;user-select:all}
input[type=number]{width:90px;font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--edge);border-radius:4px;padding:3px 6px}
.muted{color:var(--muted)} .small{font-size:12px} .note:empty{display:none}
</style></head><body>
<div class="status" id="status"></div>
<section id="connect">
  <h2>Connect your AI app</h2>
  <div class="apps" id="apps" role="group" aria-label="Your AI app"></div>
  <div class="step" id="steps"></div>
</section>
<button class="lnk again" id="again" hidden>Connect another app</button>
<details class="adv" id="adv">
  <summary>Token and port</summary>
  <p class="muted small" style="margin:8px 0 0">AI apps on this computer find OrcaSlicer by themselves. If yours says it can't find OrcaSlicer, give it this token.</p>
  <div class="form">
    <label>Token</label>
    <div class="inline"><span class="tok" id="tok"></span><button class="lnk" id="show">Show</button><button class="lnk" id="copytok">Copy</button></div>
    <span></span>
    <div class="hint"><button class="lnk warn" id="renew">Make a new token</button> <span id="tnote">Apps set up with the old one stop connecting.</span></div>
    <label class="gap">Port</label>
    <div class="inline gap"><input type="number" id="port" min="1024" max="65535"><button class="lnk" id="saveport">Save</button></div>
    <span></span>
    <div class="hint" id="pnote">Change it only if another program uses it. Only this computer can connect.</div>
  </div>
</details>
<script>
var TOKEN = __TOKEN__, LISTENING = __PORT__;
var APPS = __SETUP__, ICON = __ICONS__;
var cfg = window.orca.getConfig() || {}, S = { app: "claude", done: null, open: false, shown: false, opened: null }, armed = null;
function $(i){ return document.getElementById(i); }
function esc(t){ return String(t).replace(/[&<>"]/g, function(c){ return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
function save(patch){ cfg = Object.assign({}, cfg, patch); window.orca.saveConfig(cfg); draw(); }  // the plugin rewrites a status this carries back stale
// OrcaSlicer hands this page its settings when the tab opens and after the page's own saves, never when the plugin
// writes them: the status is as of opening the tab, so it names a time of day, not a "minutes ago" that would tick on.
function when(t){ var d = new Date(t * 1000), now = new Date();
  var hm = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return d.toDateString() === now.toDateString() ? "at " + hm : "on " + d.toLocaleDateString([], { day: "numeric", month: "short" }) + " at " + hm; }
function copyText(text, done){
  function fallback(){ var a = document.createElement("textarea"); a.value = text; document.body.appendChild(a); a.select();
    var ok = false; try { ok = document.execCommand("copy"); } catch (e) {} document.body.removeChild(a); done(ok); }
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(function(){ done(true); }, fallback); else fallback();
}
function drawStatus(){
  var off = cfg.paused === true, st = cfg.status || {}, h;
  if (off) h = '<span class="dot off"></span><div class="what"><b>Paused</b><span>OrcaSlicer refuses your AI app’s requests.</span></div>';
  else if (st.seen) h = '<span class="dot ok"></span><div class="what"><b>' + esc(st.app ? "Connected to " + st.app : "Connected") + '</b><span>Last request ' + when(st.seen) + '</span></div>';
  else h = '<span class="dot wait"></span><div class="what"><b>Waiting for your AI app</b><span>The panel beside the 3D view shows when it connects.</span></div>';
  h += '<button class="switch" role="switch" id="sw" aria-checked="' + !off + '" aria-label="Assistant access"><span>Assistant access</span><span class="track"></span></button>';
  $("status").innerHTML = h;
  $("sw").onclick = function(){ save({ paused: !(cfg.paused === true) }); };
}
function drawConnect(){
  var connected = !!(cfg.status && cfg.status.seen) && !S.open;
  $("connect").hidden = connected; $("again").hidden = !connected;
  if (connected) return;
  $("apps").innerHTML = APPS.map(function(a){ return '<button data-app="' + a.id + '" aria-pressed="' + (a.id === S.app) + '">' + ICON[a.id] + '<span>' + esc(a.name) + '</span></button>'; }).join("");
  [].forEach.call($("apps").children, function(b){ b.onclick = function(){ S.app = b.getAttribute("data-app"); S.done = null; draw(); }; });
  var app = APPS.filter(function(a){ return a.id === S.app; })[0];
  var routes = app.routes.map(function(r, j){
    var right = r.off ? "<span></span>" : (S.done === j && !r.open ? '<span class="done">Copied</span>' :
      '<button class="act' + (r.primary ? " primary" : "") + '" data-r="' + j + '">' + esc(r.act) + "</button>");
    var also = r.also ? "<small>" + esc(r.also) + "</small>" : "";
    var peek = r.copy && !r.off ? '<details class="peek"><summary>' + esc(r.open ? r.copy_label : "Show what it copies") + '</summary><pre>' + esc(r.copy) + "</pre>" +
      (r.open ? '<button class="lnk" data-c="' + j + '">Copy</button>' : "") + "</details>" : "";
    // The plugin opens it; this page never hears back (see when()), so it says what to do if nothing opened.
    var failed = S.opened === j ? '<span class="note" style="grid-column:1 / 3">Opening in your browser. Nothing opened? <button class="lnk" data-l="' + j + '">Copy link</button></span>' : "";
    return '<div class="route' + (r.off ? " off" : "") + '"><div><b>' + esc(r.label) + "</b><small>" + esc(r.hint) + "</small>" + also + "</div>" + right + peek + failed + "</div>";
  }).join("");
  // One step, no question to ask: the first request from the app is what connects it.
  $("steps").innerHTML = '<span>Set it up where you use ' + esc(app.where) + '</span><div class="routes">' + routes + "</div>";
  [].forEach.call($("steps").querySelectorAll("[data-r]"), function(b){ b.onclick = function(){
    var j = Number(b.getAttribute("data-r")), r = app.routes[j];
    if (r.open) { var n = (cfg.open && cfg.open.n || 0) + 1; S.opened = j; save({ open: { n: n, url: r.open, at: Date.now() / 1000 } }); }
    else copyText(r.copy, function(ok){ S.done = ok ? j : null; draw(); });
  }; });
  [].forEach.call($("steps").querySelectorAll("[data-l]"), function(b){ b.onclick = function(){ var r = app.routes[Number(b.getAttribute("data-l"))]; copyText(r.open, function(ok){ b.textContent = ok ? "Copied" : "Copy failed"; }); }; });
  [].forEach.call($("steps").querySelectorAll("[data-c]"), function(b){ b.onclick = function(){ var r = app.routes[Number(b.getAttribute("data-c"))]; copyText(r.copy, function(){ b.textContent = "Copied"; }); }; });
}
function drawAdv(){
  $("tok").textContent = S.shown ? TOKEN : "•".repeat(16); $("tok").className = "tok" + (S.shown ? " shown" : "");
  $("show").textContent = S.shown ? "Hide" : "Show";
  if (document.activeElement !== $("port")) $("port").value = cfg.port || LISTENING;
}
function draw(){ drawStatus(); drawConnect(); drawAdv(); }
$("again").onclick = function(){ S.open = true; draw(); };
$("show").onclick = function(){ S.shown = !S.shown; drawAdv(); };
$("copytok").onclick = function(){ copyText(TOKEN, function(ok){ $("copytok").textContent = ok ? "Copied" : "Copy failed: press Show"; }); };
$("renew").onclick = function(){
  if (armed) { clearTimeout(armed); armed = null; $("renew").textContent = "Make a new token";
    save({ renew: (cfg.renew || 0) + 1 }); $("tnote").textContent = "A new token is being made. Reopen this tab to see it."; return; }
  $("renew").textContent = "Click again to confirm"; $("tnote").textContent = "Apps set up with the old token stop connecting.";
  armed = setTimeout(function(){ armed = null; $("renew").textContent = "Make a new token"; }, 4000);
};
$("saveport").onclick = function(){
  var p = parseInt($("port").value, 10);
  if (!(p >= 1024 && p <= 65535)) { $("pnote").textContent = "Use a number from 1024 to 65535."; return; }
  save({ port: p });
  $("pnote").textContent = p === LISTENING ? "Saved." : "Saved. It takes effect after OrcaSlicer restarts (or the plugin is deactivated and activated).";
};
if (window.orca.onConfig) window.orca.onConfig(function(c){ cfg = c || {}; draw(); });
draw();
</script></body></html>"""


# The Config tab inside the OrcaSlicer MCP build, where the plugin stays off.
IN_BUILD_HTML = CONFIG_HTML[:CONFIG_HTML.index("</style>")] + """</style></head><body>
<div class="status"><span class="dot off"></span><div class="what"><b>Not needed in this OrcaSlicer</b>
<span>This is the OrcaSlicer MCP build: the connection and the panel are built in (Preferences &gt; Remote API).
The plugin stays off here, and you can deactivate it.</span></div></div>
</body></html>"""


def _inline(value) -> str:
    return json.dumps(value).replace("<", "\\u003c")


class McpCapability(orca.script.ScriptPluginCapabilityBase):
    """Script entry 'OrcaSlicer MCP': running it opens the panel. Its Config tab holds the token,
    the port and the on/off switch."""

    def __init__(self):
        super().__init__()
        self._panel = None
        self._lock = threading.Lock()
        self._service = None
        self._pump = UiPump(self._panel_for_request, self._panel_any)
        self.journal = Journal()
        self.slices = SliceWatch(current_plate_index, slice_fingerprint)
        self._slice_folders: list[str] = []
        self._folder = ""
        self._token = ""
        self._port = DEFAULT_PORT
        self._error = ""
        self._in_build = False  # inside the OrcaSlicer MCP build: stays off (running_in_mcp_build)
        self._paused = False
        self._renew_seen = 0
        self._open_seen = 0
        self._last_tick = 0.0
        self._seen = None          # (time, app) of the last accepted request, from the server thread
        self._seen_written = None  # the (time, app) last saved for the Config tab
        self._unloading = False
        self.host = None

    def get_name(self):
        return "OrcaSlicer MCP"

    # -- Config tab -------------------------------------------------------------------------
    def has_config_ui(self):
        return True

    def get_config_ui(self):
        if self._in_build:
            return IN_BUILD_HTML
        return CONFIG_HTML.replace("__TOKEN__", _inline(self._token)).replace("__PORT__", _inline(self._port)) \
            .replace("__SETUP__", _inline(setup_apps(custom_datadir(self._folder)))) \
            .replace("__ICONS__", _inline(SETUP_ICONS))

    def get_default_config(self):
        return {"port": DEFAULT_PORT, "paused": False, "renew": 0}

    def _read_config(self) -> dict:
        try:
            cfg = json.loads(self.get_config() or "{}")
        except Exception:
            return {}
        return cfg if isinstance(cfg, dict) else {}

    def _on_seen(self, app: str) -> None:  # server thread: an AI app reached OrcaSlicer
        with self._lock:
            prev = self._seen
            self._seen = (time.time(), app or (prev[1] if prev else ""))

    def _write_seen(self, cfg: dict) -> None:  # UI thread
        """The Config tab shows "Connected to <app>, last request N minutes ago" from this. Saved when the app
        changes or first appears, then at most once a minute, so OrcaSlicer is not writing its settings per request."""
        with self._lock:
            seen = self._seen
        if seen is None:
            return
        last = self._seen_written
        stored = cfg.get("status")
        # The Config tab saves its whole copy, status included, from when it opened: a status that isn't the one written
        # last is put back at once.
        intact = last is not None and stored == {"seen": int(last[0]), "app": last[1]}
        if intact and last[1] == seen[1] and seen[0] - last[0] < SEEN_WRITE_EVERY:
            return
        first = last is None or last[1] != seen[1]
        self._seen_written = seen
        status = {"seen": int(seen[0]), "app": seen[1]}
        try:
            self.save_config(json.dumps({**cfg, "status": status}))
        except Exception as e:
            trace(f"status not saved: {type(e).__name__}: {e}")
        if first and self.host is not None:
            self._post(self._setup_msg())

    def _poll_config(self) -> None:  # UI thread
        cfg = self._read_config()
        self._write_seen(cfg)
        paused = cfg.get("paused") is True
        if paused != self._paused:
            self._paused = paused
            self._post(self._state())
            self._notify("assistant access turned " + ("off" if paused else "on"))
        renew = cfg.get("renew", 0)
        if isinstance(renew, int) and not isinstance(renew, bool) and renew > self._renew_seen:
            self._renew_seen = renew
            self._renew_token()
        req = cfg.get("open")
        n = req.get("n") if isinstance(req, dict) else None
        if isinstance(n, int) and not isinstance(n, bool) and n > self._open_seen:
            self._open_seen = n
            url, at = req.get("url"), req.get("at")
            fresh = isinstance(at, (int, float)) and not isinstance(at, bool) and \
                abs(time.time() - at) <= OPEN_REQUEST_MAX_AGE_S
            if fresh and openable(url, custom_datadir(self._folder)) and not open_url(url):
                self._notify("couldn't open the link; the Config tab has it to copy", True)

    def _renew_token(self) -> None:
        try:
            tok = load_or_create_token(self._folder, renew=True)
        except OSError as e:
            self._notify("could not write a new token: " + str(e), True)
            return
        self._token = tok
        if self._service is not None:
            self._service.token.value = tok
            try:
                write_connection_file(self._folder, self._port, tok)
            except OSError as e:
                trace(f"connection file not rewritten: {type(e).__name__}: {e}")
        self._notify("new token made: paste it into your AI app's OrcaSlicer MCP connector")

    # -- lifecycle --------------------------------------------------------------------------
    def on_load(self):
        try:
            global _TRACE_PATH
            missing = missing_host_calls()
            if missing:
                raise RuntimeError(NEEDS_NEWER_TEXT.format(missing=", ".join(missing)))
            self._folder = orca.host.plugin.storage()
            remove_connection_file(self._folder)  # one left by a crash would point the connector at a dead server
            settings = load_settings(self._folder)
            if settings.get("trace") is True:
                _TRACE_PATH = os.path.join(self._folder, "plugin-trace.log")
            trace("on_load")
            if running_in_mcp_build(app_info()):
                self._in_build = True
                trace("inside the OrcaSlicer MCP build: staying off")
                self._notify(IN_BUILD_TEXT)
                return
            cfg = self._read_config()
            self._paused = cfg.get("paused") is True
            renew = cfg.get("renew", 0)
            self._renew_seen = renew if isinstance(renew, int) and not isinstance(renew, bool) else 0
            opened = (cfg.get("open") or {}).get("n", 0) if isinstance(cfg.get("open"), dict) else 0
            self._open_seen = opened if isinstance(opened, int) and not isinstance(opened, bool) else 0
            self._token = load_or_create_token(self._folder)
            port = settings.get("port") if "port" in settings else cfg.get("port", DEFAULT_PORT)
            self._port = port if isinstance(port, int) and not isinstance(port, bool) and \
                (port == 0 or 1024 <= port <= 65535) else DEFAULT_PORT
            self.host = PanelHost(self._folder, self._post, self._notify)
            self.host.on_action = self._journal_before_panel
            self.host.on_undone = self._journal_panel
            self._slice_folders = current_slice_folders()
            api = Api(self._pump, notify=self._notify, record=self.host.record, journal=self.journal,
                      on_switch=self.host.dropped_after_switch, slices=self.slices, slice_folders=self._slice_folders)
            self._service = Service(api, self._token, self._port, on_seen=self._on_seen)
            self._port = self._service.port
            self._service.start()
            try:
                write_connection_file(self._folder, self._port, self._token)
            except OSError as e:  # the connector then needs the token set by hand, as before
                trace(f"connection file not written: {type(e).__name__}: {e}")
            print(f"[orcaslicer-mcp-plugin] listening on {BIND_HOST}:{self._port}")
            trace(f"server thread started, alive={self._service.thread.is_alive()}")
            self._open_panel_at_load()
        except Exception as e:
            self._error = str(e) if isinstance(e, RuntimeError) and str(e).startswith("needs OrcaSlicer") \
                else f"{type(e).__name__}: {e}"
            self._service = None
            if self._folder:
                remove_connection_file(self._folder)
            trace("on_load failed: " + self._error)
            self._notify("could not start: " + self._error, True)
            print(f"[orcaslicer-mcp-plugin] not started: {self._error}")

    def _open_panel_at_load(self) -> None:
        """Usability finding m8 (Rita's F7): show the empty panel as soon as the plugin is activated, so there is
        something to see before the assistant first connects. If OrcaSlicer can't make it now, the first request
        opens it as before."""
        try:
            self._ensure_panel()
        except Exception as e:
            trace(f"panel at load failed: {type(e).__name__}: {e}")

    def on_unload(self):
        trace("on_unload")
        self._unloading = True  # closing our own pane below is not the user closing it: no banner
        self._pump.close()
        if self._service is not None:
            self._service.stop()
            trace(f"server stopped, thread alive={self._service.thread.is_alive()}")
            self._service = None
            remove_connection_file(self._folder)
        if self.host is not None:
            self.host.save()
        panel, self._panel = self._panel, None
        if panel is not None:
            try:
                panel.close()
            except Exception:
                pass

    def on_lifecycle_event(self, event, ctx):  # UI thread, except SliceStarted and GCodeExportFinished (slicing worker)
        name = getattr(event, "name", str(event))
        if name in SLICE_EVENTS:
            try:  # records only: nothing here may touch OrcaSlicer's UI from the slicing worker
                self.slices.event(name, ctx)
            except Exception as e:
                trace(f"slice event {name} failed: {type(e).__name__}: {e}")
            if name not in SLICE_PROJECT_EVENTS:
                return
        if self.host is None or name not in ("ProjectOpened", "NewProject", "ProjectClosed", "ProjectAfterSave"):
            return
        try:
            path = "" if name in ("NewProject", "ProjectClosed") else orca.host.plater().project_path()
        except Exception:
            path = ""
        if name == "ProjectOpened":
            self.journal.note_project("Opened project " + (os.path.splitext(os.path.basename(path))[0] if path
                                                           else "Untitled"))
        elif name == "NewProject":
            self.journal.note_project("New project")
        self.host.project_event(name, path)

    # -- user-change journal around the panel's own actions ---------------------------------
    def _hhmm(self) -> str:
        return time.strftime("%H:%M", time.localtime())

    def _journal_before_panel(self) -> None:  # UI thread
        self.journal.begin_request(capture_baseline(self.journal), self._hhmm())

    def _journal_panel(self, text: str) -> None:  # undo worker thread
        """Ruling R19: the panel's undo is its own entry, then a new baseline so the change is not
        reported again as the user's edit."""
        self.journal.add("panel", "Panel", text, self._hhmm())
        try:
            self.journal.end_request(self._pump.call(lambda: capture_baseline(self.journal), gate=False))
        except Exception as e:
            trace(f"journal baseline after undo failed: {e}")
            self.journal.invalidate()

    def execute(self):
        try:
            panel = self._ensure_panel()
            panel.show()
        except Exception as e:
            return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, str(e))
        if self._error:
            return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError,
                                                "The server did not start: " + self._error)
        return orca.ExecutionResult.success(f"OrcaSlicer MCP is listening on {BIND_HOST}:{self._port}.")

    # -- panel ------------------------------------------------------------------------------
    def _ensure_panel(self):
        with self._lock:
            panel = self._panel
        if panel is not None and panel.is_open():
            return panel
        trace("create_dock_panel: calling")
        panel = orca.host.ui.create_dock_panel(html=panel_page(), title="OrcaSlicer MCP", width=300, height=480,
                                               on_message=self._on_message, on_close=self._on_close,
                                               dock="right")
        trace("create_dock_panel: returned")
        with self._lock:
            self._panel = panel
        # The page also asks with "ready"; whichever arrives after it has loaded sticks.
        self._send_all()
        return panel

    def _send_all(self):
        if self.host is not None:
            self._post(self._state())
            self._post(self._setup_msg())
            self._post(self.host.history_msg())

    def _setup_msg(self) -> dict:
        """The panel's first run: how to connect an AI app, or goals to try once one has (setup_message)."""
        status = self._read_config().get("status")
        return setup_message(custom_datadir(self._folder), status if isinstance(status, dict) else None,
                             self._paused)

    def _state(self) -> dict:
        return {"type": "state", "api": "off" if (self._paused or self._error) else "on"}

    def _panel_any(self):
        try:
            return self._ensure_panel()
        except Exception as e:
            raise ApiReply(503, {"error": "ui_not_ready", "detail": str(e)})

    def _panel_for_request(self):
        if self._paused:
            raise ApiReply(503, {"error": "paused",
                                 "detail": "the user turned assistant access off in the OrcaSlicer MCP "
                                           "plugin; ask them to turn it back on in OrcaSlicer: " + SETTINGS_HINT})
        try:
            return self._ensure_panel()
        except Exception as e:
            raise ApiReply(503, {"error": "ui_not_ready", "detail": str(e)})

    def _on_message(self, message):  # UI thread
        message = message or {}
        if "t" in message:
            if message.get("t") == "tick":
                self._tick()
            self._pump.run_pending()
            return
        action = message.get("action")
        if action:
            trace(f"panel action {action}")
        # Every undo / restore / undo_restore gets exactly one result (the page shows "Reverting…" until then).
        if action == "restore":
            self._post({"type": "result", "action": "restore", "group": message.get("group"),
                        "status": "failed", "reason": "Restore points need the OrcaSlicer MCP build"})
        elif action == "undo_restore":
            self._post({"type": "result", "action": "undo_restore", "status": "failed",
                        "reason": "Restore points need the OrcaSlicer MCP build"})
        elif action == "undo" and self.host is None:
            self._post({"type": "result", "action": "undo", "row": message.get("row"), "status": "failed",
                        "reason": "The plugin did not start"})
        elif self.host is not None:
            if action == "ready":  # repeated every 500 ms until the first history: answer each, cheaply
                self._send_all()
            elif action == "live":
                self.host.push_live(force=True)
            elif action == "undo":
                self.host.undo(message.get("row"))
            elif action == "open_settings":
                self._notify(SETTINGS_HINT)
            elif action == "open":  # a setup link in the panel: only the fixed list (openable) is opened
                url = message.get("url")
                if openable(url, custom_datadir(self._folder)) and not open_url(url):
                    self._notify("couldn't open the link; the Config tab has it to copy", True)
        self._pump.run_pending()

    def _tick(self):  # UI thread, every 500 ms while the page is open
        now = time.monotonic()
        if now - self._last_tick < 1.0:
            return
        self._last_tick = now
        self._poll_config()
        if self.host is not None:
            self.host.push_live()

    def _on_close(self):
        with self._lock:
            self._panel = None  # the next request opens it again; the Config tab is the off switch
        if not self._unloading:
            self._banner_closed()

    def _banner_closed(self) -> None:  # UI thread
        """The user closed the pane: say where it comes back from, with Undo to reopen it now."""
        ui = orca.host.ui
        try:
            ui.push_notification(ui.NotificationLevel.RegularNotificationLevel, PANEL_CLOSED_TEXT, "Undo",
                                 self._reopen_from_banner)
        except Exception as e:
            trace(f"panel closed banner failed: {type(e).__name__}: {e}")

    def _reopen_from_banner(self):  # UI thread: the banner's Undo
        if self._unloading:
            return True
        try:
            self._ensure_panel()
        except Exception as e:
            trace(f"reopen from the banner failed: {type(e).__name__}: {e}")
        return True  # closes the banner

    def _post(self, data):
        with self._lock:
            panel = self._panel
        if panel is not None:
            try:
                panel.post(data)
            except Exception:
                pass

    def _notify(self, text, warning=False):
        ui = orca.host.ui
        level = ui.NotificationLevel.WarningNotificationLevel if warning else \
            ui.NotificationLevel.RegularNotificationLevel
        try:
            ui.push_notification(level, "OrcaSlicer MCP: " + text)
        except Exception:
            pass


@orca.plugin
class OrcaSlicerMcpPlugin(orca.base):
    def register_capabilities(self):
        # One thread for the server, a socket on this computer only, and reading the folder OrcaSlicer writes the
        # G-code of a slice to (a folder grant can only be asked for here); OrcaSlicer asks once.
        # Inside the OrcaSlicer MCP build the plugin starts nothing (running_in_mcp_build), so it asks for nothing.
        if not running_in_mcp_build(app_info()):
            wanted = {"threading": True, "network_socket": [BIND_HOST]}
            folders = current_slice_folders()
            if folders:
                wanted["fs_read"] = [f + "/*" for f in folders]
            request_permissions(wanted)
        orca.register_capability(McpCapability)


# ---------------------------------------------------------------------------------------------
# Generated data. Do not edit by hand: tools/embed.py rewrites these blocks from their sources.
# ---------------------------------------------------------------------------------------------
# >>> generated PANEL_HTML (tools/embed.py)
PANEL_HTML = (
    "<!DOCTYPE html>\n"
    "<!-- OrcaSlicer MCP panel. One page for every host: the messages it takes and sends are documented in\n"
    "     docs/remote-api/README.md, \"MCP panel page and protocol\". Hosts vendor this file unchanged. -->\n"
    "<html lang=\"en\">\n"
    "<head>\n"
    "<meta charset=\"utf-8\">\n"
    "<title>OrcaSlicer MCP</title>\n"
    "<style>\n"
    "  /* Colours: the host's theme variables where it sends them. The link, faint, warning, focus and type colours below\n"
    "     are the starting points for each side (data-theme on the root): the view script measures the background the host\n"
    "     really gives (and the row tints over it) and moves each colour's lightness until text reaches 4.5:1 and icons and\n"
    "     the focus ring 3:1 there (McpPanelCore.themeColours), setting the result on the root. These values apply until\n"
    "     then, or where the script cannot measure. */\n"
    "  :root { --bg: var(--orca-bg, #2b2b2f); --fg: var(--orca-fg, #e8e8e8); --muted: var(--orca-muted, #a3a3a8);\n"
    "          --border: var(--orca-border, #3c3c42); --accent: var(--orca-accent, #009688);\n"
    "          --link: #5ad1c4; --faint: #9a9aa0; --warn: #f28b82; --focus: #5ad1c4;\n"
    "          --hover: rgba(127,127,127,.12); --hover-soft: rgba(127,127,127,.08);\n"
    "          --tip-bg: #121214; --tip-fg: #e8e8e8; --tip-border: #44444b;\n"
    "          --t-set: #5fa8e8; --t-preset: #b28be8; --t-model: #e8a95a; --t-plate: #74c29a; --t-slice: #00a991;\n"
    "          --font: var(--orca-font, \"HarmonyOS Sans SC\", \"Segoe UI\", system-ui, -apple-system, sans-serif); }\n"
    "  :root[data-theme=\"light\"] { --link: #00695c; --faint: #5f5f64; --warn: #b3261e; --focus: #00695c;\n"
    "          --t-set: #1f6fb8; --t-preset: #7a4fc4; --t-model: #a8650f; --t-plate: #2e7d55; --t-slice: #00796b; }\n"
    "  * { box-sizing: border-box; }\n"
    "  html, body { margin: 0; height: 100%; }\n"
    "  body { background: var(--bg); color: var(--fg); font: 13px/1.4 var(--font); display: flex; flex-direction: column; overflow: hidden; }\n"
    "  button { font: inherit; color: inherit; background: none; border: 0; padding: 0; margin: 0; cursor: pointer; text-align: inherit; }\n"
    "  /* The host injects element defaults after this sheet (WebViewHostDialog: every button on the accent colour, with a\n"
    "     border, padding and a hover filter). These win over that by specificity; any class rule below still wins. */\n"
    "  html body button { background: none; border: 0; border-radius: 0; padding: 0; margin: 0; color: inherit; font: inherit; text-align: inherit; filter: none; }\n"
    "  html body button:hover { filter: none; }\n"
    "  /* Keyboard focus: a ring in the theme's focus colour on every control (rows are role=button). Rules that name\n"
    "     :focus-visible stand alone: an engine without it drops only them (and keeps its default focus outline). */\n"
    "  :focus-visible { outline: 2px solid var(--focus); outline-offset: -2px; border-radius: 3px; }\n"
    "  :focus:not(:focus-visible) { outline: none; }\n"
    "  #app { display: flex; flex-direction: column; flex: 1; min-height: 0; }\n"
    "  .title { display: flex; align-items: center; gap: 6px; height: 32px; padding: 0 10px; border-bottom: 1px solid var(--border); flex: none; }\n"
    "  .title .sp { flex: 1; }\n"
    "  .list { flex: 1; overflow-y: auto; overflow-x: hidden; }\n"
    "  .ibtn { width: 22px; height: 22px; display: grid; place-items: center; border-radius: 3px; color: var(--muted); cursor: pointer; }\n"
    "  .ibtn:hover { background: var(--hover); color: var(--fg); }\n"
    "  .ibtn:focus-visible { background: var(--hover); color: var(--fg); }\n"
    "  .ibtn svg { width: 14px; height: 14px; fill: none; stroke: currentColor; stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }\n"
    "  .title [data-tip]:hover::after { top: calc(100% + 6px); bottom: auto; }\n"
    "\n"
    "  svg.i { width: 14px; height: 14px; flex: none; fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }\n"
    "  .k-setting { color: var(--t-set); } .k-preset { color: var(--t-preset); } .k-model { color: var(--t-model); }\n"
    "  .k-plate { color: var(--t-plate); } .k-slice { color: var(--t-slice); } .k-refused { color: var(--warn); } .k-file { color: var(--faint); }\n"
    "\n"
    "  .empty { padding: 18px 14px; color: var(--muted); font-size: 12.5px; line-height: 1.5; }\n"
    "  .empty button { color: var(--link); text-decoration: underline; text-underline-offset: 2px; }\n"
    "  .offnote { border-bottom: 1px solid var(--border); }\n"
    "\n"
    "  /* First run (a host that sends \"setup\"): how to connect an AI app, then that it connected. */\n"
    "  .setup { padding: 14px 12px; }\n"
    "  .setup .sh { font-weight: 600; font-size: 13.5px; margin-bottom: 3px; }\n"
    "  .setup .slead { color: var(--muted); font-size: 12px; line-height: 1.45; margin-bottom: 10px; }\n"
    "  .ssqs { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }\n"
    "  .ssq { aspect-ratio: 1 / 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px;\n"
    "         border: 1px solid var(--border); border-radius: 8px; background: var(--hover-soft); color: var(--muted); font-size: 11.5px; line-height: 1.2; text-align: center; padding: 4px; }\n"
    "  .ssq svg { width: 26px; height: 26px; flex: none; }\n"
    "  .ssq:hover { color: var(--fg); }\n"
    "  .ssq[aria-pressed=\"true\"] { border: 1.5px solid var(--accent); color: var(--fg); background: color-mix(in srgb, var(--accent) 16%, transparent); }\n"
    "  .sstep { margin: 12px 0 0; font-size: 12.5px; }\n"
    "  .sroutes { display: flex; flex-direction: column; gap: 5px; margin-top: 6px; }\n"
    "  .sroute { border: 1px solid var(--border); border-radius: 6px; padding: 7px 9px; }\n"
    "  .sroute b { font-weight: 600; display: block; } .sroute small { display: block; color: var(--muted); font-size: 11.5px; line-height: 1.4; margin: 1px 0 6px; }\n"
    "  .sroute.off { border-style: dashed; } .sroute.off b { color: var(--muted); font-weight: 500; } .sroute.off small { margin-bottom: 0; }\n"
    "  .sact { border-radius: 4px; padding: 3px 10px; border: 1px solid var(--accent); background: var(--accent); color: #fff; }\n"
    "  .sact.second { background: transparent; color: var(--fg); border-color: var(--border); }\n"
    "  .sdone { color: var(--link); font-size: 12px; }\n"
    "  .sstat { display: flex; align-items: center; gap: 7px; margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--border); color: var(--muted); font-size: 12px; }\n"
    "  .sstat.top { margin: 0 0 12px; padding: 0; border: 0; color: var(--fg); }\n"
    "  .sdot { width: 8px; height: 8px; border-radius: 50%; background: #d9a441; flex: none; } .sdot.ok { background: #3fb27f; }\n"
    "  /* Connected, nothing changed yet: the message in the middle of the empty panel, both ways. */\n"
    "  .sconn { min-height: 100%; box-sizing: border-box; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; padding: 16px 20px; }\n"
    "  .sconn .sstat.top { margin: 0 0 8px; }\n"
    "  .sconn .slead { max-width: 240px; margin: 0; text-wrap: balance; }\n"
    "\n"
    "  /* Links that act: underlined in the link colour. Unavailable ones: faint, no underline, no pointer. */\n"
    "  .lnk { color: var(--link); text-decoration: underline; text-underline-offset: 2px; white-space: nowrap; border-radius: 2px; }\n"
    "  .lnk.off { color: var(--faint); text-decoration: none; cursor: default; }\n"
    "  .lnk.no { color: var(--muted); }\n"
    "  .lnk:focus-visible { outline-offset: 1px; }\n"
    "\n"
    "  .group { border-bottom: 1px solid var(--border); }\n"
    "  .ghead { position: relative; display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; column-gap: 7px; min-height: 34px; padding: 0 10px; cursor: pointer; }\n"
    "  .ghead:hover { background: var(--hover-soft); }\n"
    "  .ghead .gt { display: grid; grid-template-columns: 34px minmax(0, 1fr); gap: 7px; align-items: center; min-width: 0; min-height: 34px; text-align: left; }\n"
    "  .ghead .t { color: var(--faint); font-size: 12px; font-variant-numeric: tabular-nums; }\n"
    "  .ghead .nm { min-width: 0; display: block; }\n"
    "  .ghead .nm[data-tip]:hover::after { left: 0; transform: none; white-space: normal; max-width: 240px; }\n"
    "  .ghead .name { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }\n"
    "  .ghead .res { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; display: inline-flex; gap: 5px; align-items: center; }\n"
    "  .ghead .res.stale { color: var(--faint); }\n"
    "  /* Restore: always shown (dimmed) on an older group, brighter on hover and focus. */\n"
    "  /* opacity .8 keeps the dimmed icon at 3:1 or more (3.65:1 on #363636, 3.18:1 on #eeeeee with the hosts' muted). */\n"
    "  .ghead .act { width: 22px; height: 22px; display: grid; place-items: center; border-radius: 3px; color: var(--muted); opacity: .8; }\n"
    "  .ghead:hover .act { opacity: 1; }\n"
    "  .ghead .act:hover { background: var(--hover); color: var(--fg); }\n"
    "  .ghead .act:focus-visible { opacity: 1; background: var(--hover); color: var(--fg); }\n"
    "  .ghead .act.off { opacity: .3; cursor: default; }\n"
    "  .ghead .act svg { width: 14px; height: 14px; fill: none; stroke: currentColor; stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }\n"
    "  .group.open .ghead .t { color: var(--muted); }\n"
    "  /* The restore question: a line under the group's title (the title stays). */\n"
    "  .ask { display: none; grid-column: 1 / -1; flex-direction: column; align-items: flex-start; gap: 2px; font-size: 12px; padding: 0 0 8px 41px; cursor: default; }\n"
    "  .ask .line { display: flex; gap: 10px; flex-wrap: wrap; align-items: baseline; }\n"
    "  .ask .note { color: var(--muted); font-size: 11.5px; }\n"
    "  .ask .bad { color: var(--warn); font-size: 11.5px; overflow-wrap: anywhere; }\n"
    "  .group.asking .ask, .group.busy .ask, .group.showerr .ask { display: flex; }\n"
    "  .group.asking .ghead .act, .group.busy .ghead .act, .group.showerr .ghead .act { display: none; }\n"
    "  .ask .wait { color: var(--muted); }\n"
    "\n"
    "  .body { display: none; padding: 0 0 8px; }\n"
    "  .group.open .body { display: block; }\n"
    "  .row { display: grid; grid-template-columns: 14px minmax(4em, 1fr) fit-content(60%); gap: 8px; align-items: center; min-height: 26px; padding: 3px 10px 3px 51px; cursor: pointer; }\n"
    "  .row:hover { background: var(--hover-soft); }\n"
    "  .row .l { color: var(--muted); min-width: 0; display: block; }\n"
    "  /* A long label wraps to a second line (and is cut after it), never truncated on one line. */\n"
    "  .row .l .lt { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden; overflow-wrap: anywhere; }\n"
    "  .row .v { color: var(--fg); text-align: right; overflow-wrap: anywhere; min-width: 0; }\n"
    "  .row .v .sw { display: inline-block; width: 10px; height: 10px; margin-right: 5px; vertical-align: -1px; border: 1px solid var(--border); border-radius: 2px; }\n"
    "  .row .v .old { color: var(--faint); text-decoration: line-through; margin-right: 6px; }\n"
    "  .row .old { text-decoration: line-through; }\n"
    "  /* A long value goes on its own line under the label (the label keeps the row's width). */\n"
    "  .row.long { grid-template-columns: 14px minmax(0, 1fr); }\n"
    "  .row.long .v { grid-column: 2; text-align: left; }\n"
    "  .row .l .chg { display: block; color: var(--muted); font-size: 11px; line-height: 1.2; }\n"
    "  .row .l .chg .old { color: var(--muted); }\n"
    "  .row.refused .v { color: var(--warn); }\n"
    "  .row.stale .v, .row.stale .l .lt { color: var(--faint); }\n"
    "  .row.dropped .v { color: var(--faint); }\n"
    "  .more { display: none; padding: 0 10px 6px 73px; color: var(--muted); font-size: 12px; justify-content: space-between; gap: 8px; }\n"
    "  .row.open, .row.open + .more { background: var(--hover-soft); }\n"
    "  .row.open + .more { display: flex; }\n"
    "  .more .d { min-width: 0; overflow-wrap: anywhere; }\n"
    "  .more .d[data-tip]:hover::after { left: 0; transform: none; white-space: pre-line; max-width: 240px; }\n"
    "  .more .d.bad { color: var(--warn); }\n"
    "  .more .faint { color: var(--faint); white-space: nowrap; }\n"
    "  .group.newest .ghead .name { font-weight: 600; color: var(--fg); }\n"
    "  .group.older .ghead .name { color: var(--muted); }\n"
    "  .group.undone .ghead .name, .group.undone .ghead .res { color: var(--faint); font-weight: 400; text-decoration: line-through; text-decoration-color: rgba(127,127,127,.4); }\n"
    "  .group.undone .ghead { cursor: pointer; }\n"
    "  .group.restored .ghead { cursor: default; grid-template-columns: 34px minmax(0, 1fr) auto; }\n"
    "  .group.restored .ghead:hover { background: none; }\n"
    "  .group.restored .faint { color: var(--faint); }\n"
    "  .ghead .rl { display: flex; gap: 4px; align-items: baseline; min-width: 0; }\n"
    "  .ghead .rl .name { min-width: 0; }\n"
    "  .ghead .rl .sep { color: var(--muted); flex: none; }\n"
    "  .group.restored [data-tip]:hover::after { top: calc(100% + 6px); bottom: auto; }\n"
    "  [data-tip] { position: relative; }\n"
    "  [data-tip]:hover::after {\n"
    "    content: attr(data-tip); position: absolute; z-index: 5; bottom: calc(100% + 6px); left: 50%; transform: translateX(-50%);\n"
    "    white-space: nowrap; padding: 3px 7px; border-radius: 3px; background: var(--tip-bg); color: var(--tip-fg); border: 1px solid var(--tip-border); font-size: 11.5px; pointer-events: none; font-weight: 400; text-decoration: none; }\n"
    "  /* A tag for the control the keyboard focused (P5): one element the view script places above the control, or below\n"
    "     it, inside the pane and never over the control (McpPanelCore.placeTip). */\n"
    "  .ftip { position: fixed; z-index: 6; left: 0; top: 0; max-width: 240px; padding: 3px 7px; border-radius: 3px; background: var(--tip-bg); color: var(--tip-fg);\n"
    "          border: 1px solid var(--tip-border); font-size: 11.5px; line-height: 1.35; pointer-events: none; overflow-wrap: anywhere; white-space: pre-line; }\n"
    "  .ftip[hidden] { display: none; }\n"
    "  .tip-left:hover::after { left: auto; right: 0; transform: none; }\n"
    "  .row .ic { display: inline-flex; }\n"
    "  /* After the general tag rule: a row's tag and the restore icon's long tag wrap inside the pane. */\n"
    "  .row .l[data-tip]:hover::after { left: 0; transform: none; white-space: normal; width: max-content; max-width: 200px; }\n"
    "  .ghead .act[data-tip]:hover::after { white-space: normal; width: max-content; max-width: 220px; }\n"
    "  /* A multi-line value's tag (value_tip: a few lines of a G-code template) keeps its line breaks, inside the pane. */\n"
    "  .row .v[data-tip]:hover::after { left: auto; right: 0; transform: none; white-space: pre-line; width: max-content; max-width: 240px; text-align: left; }\n"
    "</style>\n"
    "</head>\n"
    "<body>\n"
    "<svg width=\"0\" height=\"0\" style=\"position:absolute\">\n"
    "  <symbol id=\"i-setting\" viewBox=\"0 0 24 24\"><path d=\"M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12\"/><circle cx=\"16\" cy=\"6\" r=\"2\"/><circle cx=\"10\" cy=\"12\" r=\"2\"/><circle cx=\"18\" cy=\"18\" r=\"2\"/></symbol>\n"
    "  <symbol id=\"i-preset\" viewBox=\"0 0 24 24\"><path d=\"M6 3h12v18l-6-4-6 4z\"/></symbol>\n"
    "  <symbol id=\"i-model\" viewBox=\"0 0 24 24\"><path d=\"M12 3l8 4.5v9L12 21l-8-4.5v-9z\"/><path d=\"M4 7.5l8 4.5 8-4.5M12 12v9\"/></symbol>\n"
    "  <symbol id=\"i-plate\" viewBox=\"0 0 24 24\"><rect x=\"3\" y=\"3\" width=\"8\" height=\"8\" rx=\"1\"/><rect x=\"13\" y=\"3\" width=\"8\" height=\"8\" rx=\"1\"/><rect x=\"3\" y=\"13\" width=\"8\" height=\"8\" rx=\"1\"/><rect x=\"13\" y=\"13\" width=\"8\" height=\"8\" rx=\"1\"/></symbol>\n"
    "  <symbol id=\"i-slice\" viewBox=\"0 0 24 24\"><path d=\"M3 8l9-4 9 4-9 4z\"/><path d=\"M3 12l9 4 9-4M3 16l9 4 9-4\"/></symbol>\n"
    "  <symbol id=\"i-refused\" viewBox=\"0 0 24 24\"><path d=\"M12 4l9 16H3z\"/><path d=\"M12 10v4M12 17v.5\"/></symbol>\n"
    "  <symbol id=\"i-file\" viewBox=\"0 0 24 24\"><path d=\"M6 3h8l4 4v14H6z\"/><path d=\"M14 3v4h4\"/></symbol>\n"
    "  <symbol id=\"i-expand\" viewBox=\"0 0 24 24\"><path d=\"M7 9l5-5 5 5M7 15l5 5 5-5\"/></symbol>\n"
    "  <symbol id=\"i-collapse\" viewBox=\"0 0 24 24\"><path d=\"M7 4l5 5 5-5M7 20l5-5 5 5\"/></symbol>\n"
    "  <symbol id=\"i-restore\" viewBox=\"0 0 24 24\"><path d=\"M3 12a9 9 0 1 0 3-6.7\"/><path d=\"M3 4v5h5\"/></symbol>\n"
    "  <symbol id=\"i-gear\" viewBox=\"0 0 24 24\"><path d=\"M9.97 4.68L10.25 1.55A10.6 10.6 0 0 1 13.75 1.55L14.03 4.68A7.6 7.6 0 0 1 15.74 5.39L18.16 3.37A10.6 10.6 0 0 1 20.63 5.84L18.61 8.26A7.6 7.6 0 0 1 19.32 9.97L22.45 10.25A10.6 10.6 0 0 1 22.45 13.75L19.32 14.03A7.6 7.6 0 0 1 18.61 15.74L20.63 18.16A10.6 10.6 0 0 1 18.16 20.63L15.74 18.61A7.6 7.6 0 0 1 14.03 19.32L13.75 22.45A10.6 10.6 0 0 1 10.25 22.45L9.97 19.32A7.6 7.6 0 0 1 8.26 18.61L5.84 20.63A10.6 10.6 0 0 1 3.37 18.16L5.39 15.74A7.6 7.6 0 0 1 4.68 14.03L1.55 13.75A10.6 10.6 0 0 1 1.55 10.25L4.68 9.97A7.6 7.6 0 0 1 5.39 8.26L3.37 5.84A10.6 10.6 0 0 1 5.84 3.37L8.26 5.39A7.6 7.6 0 0 1 9.97 4.68Z\"/><circle cx=\"12\" cy=\"12\" r=\"3.2\"/></symbol>\n"
    "</svg>\n"
    "<div id=\"app\">\n"
    "  <div class=\"ftip\" id=\"ftip\" role=\"tooltip\" hidden></div>\n"
    "  <div class=\"title\">Assistant<span class=\"sp\"></span><button type=\"button\" class=\"ibtn tip-left\" id=\"fold\"></button><button type=\"button\" class=\"ibtn tip-left\" id=\"gear\"></button></div>\n"
    "  <div class=\"list\" id=\"list\"></div>\n"
    "</div>\n"
    "<script id=\"panel-core\">\n"
    "(function (root) {\n"
    "  'use strict';\n"
    "\n"
    "  var PROTOCOL_VERSION = 1;\n"
    "  var OFF_TEXT = 'The Remote API is off.'; // when the host sends no capabilities.off_text\n"
    "  var SETTINGS_LABEL = 'Remote API settings'; // when the host sends no capabilities.settings_label\n"
    "\n"
    "  function has(s) { return s !== null && s !== undefined && s !== ''; }\n"
    "\n"
    "  // history = {rows, groups}; returns groups newest first (start point, id 0, last),\n"
    "  // each {id, ts, time, restore, rows}. The view does not show the start point: it is only the target of\n"
    "  // \"Restored to\" (and of a restore through the API).\n"
    "  function groupRows(history) {\n"
    "    var rows = (history && history.rows) || [];\n"
    "    var metas = (history && history.groups) || [];\n"
    "    var byId = {};\n"
    "    var list = [];\n"
    "    function ensure(id, ts, time) {\n"
    "      if (!byId[id]) {\n"
    "        byId[id] = { id: id, ts: ts || 0, time: time || '', restore: null, rows: [] };\n"
    "        list.push(byId[id]);\n"
    "      }\n"
    "      return byId[id];\n"
    "    }\n"
    "    metas.forEach(function (m) {\n"
    "      var g = ensure(m.id, m.ts, m.time);\n"
    "      g.ts = m.ts || 0;\n"
    "      g.time = m.time || '';\n"
    "      g.restore = m.restore || null;\n"
    "    });\n"
    "    rows.forEach(function (r) {\n"
    "      var g = ensure(r.group, r.ts, r.time);\n"
    "      g.rows.push(r);\n"
    "    });\n"
    "    list.sort(function (a, b) {\n"
    "      if (a.id === 0 && b.id !== 0) return 1;\n"
    "      if (b.id === 0 && a.id !== 0) return -1;\n"
    "      if (a.ts !== b.ts) return b.ts - a.ts;\n"
    "      return b.id - a.id;\n"
    "    });\n"
    "    return list;\n"
    "  }\n"
    "\n"
    "  function singleText(r) {\n"
    "    var label = r.label || '';\n"
    "    var value = has(r.value) ? String(r.value) : '';\n"
    "    if (!value) return label;\n"
    "    return label + (has(r.before) ? ' → ' : ' ') + value;\n"
    "  }\n"
    "\n"
    "  function groupTitle(group) {\n"
    "    var all = group.rows || [];\n"
    "    if (!all.length) return '';\n"
    "    var rows = all.filter(function (r) { return r.kind !== 'slice' && r.kind !== 'refused'; });\n"
    "    if (!rows.length) {\n"
    "      // A connector's note (\"Compared 3 options · nothing kept\") names a group that holds no change.\n"
    "      for (var n = all.length - 1; n >= 0; n--) {\n"
    "        if (all[n].title_hint === 'note') return (all[n].label || '') + (has(all[n].value) ? ' \\u00b7 ' + all[n].value : '');\n"
    "      }\n"
    "      var refused = all.filter(function (r) { return r.kind === 'refused'; });\n"
    "      var slices = all.filter(function (r) { return r.kind === 'slice'; });\n"
    "      if (slices.length) return 'Sliced ' + (slices[slices.length - 1].where || '').trim();\n"
    "      if (refused.length) return (refused[0].label || '') + ' not allowed';\n"
    "      return 'Sliced ' + (all[all.length - 1].where || '').trim();\n"
    "    }\n"
    "    var more = rows.length - 1;\n"
    "    var win = null;\n"
    "    for (var i = 0; i < rows.length; i++) {\n"
    "      if (rows[i].title_hint === 'added' || rows[i].title_hint === 'opened') { win = rows[i]; break; }\n"
    "    }\n"
    "    if (win) {\n"
    "      var t = (win.label || '') + (has(win.value) ? ' ' + win.value : '');\n"
    "      return more > 0 ? t + ' and ' + more + ' more' : t;\n"
    "    }\n"
    "    if (more === 0) return singleText(rows[0]);\n"
    "    return (rows[0].label || '') + ' and ' + more + ' more';\n"
    "  }\n"
    "\n"
    "  function formatDuration(sec) {\n"
    "    sec = Math.max(0, Math.round(sec));\n"
    "    if (sec < 60) return sec + 's';\n"
    "    var mins = Math.round(sec / 60);\n"
    "    if (mins < 60) return mins + 'm';\n"
    "    var h = Math.floor(mins / 60);\n"
    "    return h + 'h ' + (mins % 60) + 'm';\n"
    "  }\n"
    "\n"
    "  // allRows (optional): every row of the timeline, to tell whether the group's slice is out of date (sliceStale);\n"
    "  // staleUpTo (optional): rows up to this id are out of date (a change the page made itself, like a revert);\n"
    "  // sliceValid (optional): the host's slice_valid (see sliceStale).\n"
    "  function groupResult(group, allRows, staleUpTo, sliceValid) {\n"
    "    var rows = group.rows || [];\n"
    "    var text = '';\n"
    "    var warn = false;\n"
    "    var last = null;\n"
    "    rows.forEach(function (r) {\n"
    "      if (r.kind === 'refused' || has(r.error)) warn = true;\n"
    "      if (r.kind === 'slice' && r.result && typeof r.result.seconds === 'number') {\n"
    "        text = formatDuration(r.result.seconds);\n"
    "        last = r;\n"
    "      }\n"
    "    });\n"
    "    var out = { text: text, warn: warn };\n"
    "    if (allRows) {\n"
    "      out.stale = !!last && sliceStale(last, allRows, staleUpTo, sliceValid);\n"
    "      // Only the newest result says \"out of date\" in words; an older group's result is faint (it is history).\n"
    "      var newest = latestSlice(allRows);\n"
    "      out.current = !!last && !!newest && newest.id === last.id;\n"
    "    }\n"
    "    return out;\n"
    "  }\n"
    "\n"
    "  // A slice result is out of date once anything changed after it: a later setting, preset, model or plate row (a\n"
    "  // refusal, a note or another slice changes nothing), or a change the page made itself (staleUpTo: rows up to that\n"
    "  // id were there when the user reverted or restored something), or, for the newest result only, the host saying it\n"
    "  // no longer holds (sliceValid === false: the host's slice_valid, which also follows the user's own edits, which add\n"
    "  // no row). Without sliceValid (a host that does not send it) the rows alone decide.\n"
    "  var CHANGE_KINDS = { setting: 1, preset: 1, model: 1, plate: 1 };\n"
    "  function sliceStale(row, allRows, staleUpTo, sliceValid) {\n"
    "    if (!row || row.kind !== 'slice' || !row.result || has(row.error)) return false;\n"
    "    if (typeof staleUpTo === 'number' && row.id <= staleUpTo) return true;\n"
    "    var rs = allRows || [];\n"
    "    if (sliceValid === false) {\n"
    "      var newest = latestSlice(rs);\n"
    "      if (newest && newest.id === row.id) return true;\n"
    "    }\n"
    "    for (var i = 0; i < rs.length; i++) {\n"
    "      var r = rs[i];\n"
    "      if (later(r, row) && CHANGE_KINDS[r.kind] && !has(r.error)) return true;\n"
    "    }\n"
    "    return false;\n"
    "  }\n"
    "  // r came after row: a later time, or the same second and a later id.\n"
    "  function later(r, row) {\n"
    "    var a = Number(r.ts) || 0, b = Number(row.ts) || 0;\n"
    "    return a > b || (a === b && r.id > row.id);\n"
    "  }\n"
    "  // The newest slice result of the timeline (the one that would describe the plate now), or null.\n"
    "  function latestSlice(allRows) {\n"
    "    var best = null;\n"
    "    (allRows || []).forEach(function (r) {\n"
    "      if (r.kind === 'slice' && r.result && !has(r.error) && (!best || later(r, best))) best = r;\n"
    "    });\n"
    "    return best;\n"
    "  }\n"
    "\n"
    "  // The rows of a group in the order the page lists them: changes and results as they came, refusals last.\n"
    "  function orderedRows(group) {\n"
    "    var rows = (group && group.rows) || [];\n"
    "    return rows.filter(function (r) { return r.kind !== 'refused'; }).concat(rows.filter(function (r) { return r.kind === 'refused'; }));\n"
    "  }\n"
    "\n"
    "  var NUM_RE = /^\\s*([-+]?(?:\\d+\\.?\\d*|\\.\\d+)(?:[eE][-+]?\\d+)?)\\s*(.*?)\\s*$/;\n"
    "  function parseNum(v) {\n"
    "    var m = NUM_RE.exec(String(v));\n"
    "    if (!m) return null;\n"
    "    var n = Number(m[1]);\n"
    "    return isFinite(n) ? { n: n, unit: m[2] } : null;\n"
    "  }\n"
    "  // Numbers match within 1e-6 relative (same unit text required); anything else exactly.\n"
    "  function sameValue(a, b) {\n"
    "    var x = parseNum(a), y = parseNum(b);\n"
    "    if (x && y) {\n"
    "      if (x.unit !== y.unit) return false;\n"
    "      return Math.abs(x.n - y.n) <= 1e-6 * Math.max(Math.abs(x.n), Math.abs(y.n), 1e-12);\n"
    "    }\n"
    "    return String(a) === String(b);\n"
    "  }\n"
    "\n"
    "  // A colour value (#RGB, #RRGGBB or #RRGGBBAA, as filament colour rows carry it): the CSS colour, else ''.\n"
    "  var COLOUR_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/;\n"
    "  function colourOf(v) {\n"
    "    var t = has(v) ? String(v).trim() : '';\n"
    "    return COLOUR_RE.test(t) ? t : '';\n"
    "  }\n"
    "\n"
    "  function offText(caps) {\n"
    "    return caps && has(caps.off_text) ? String(caps.off_text) : OFF_TEXT;\n"
    "  }\n"
    "\n"
    "  // The settings icon's hover tag.\n"
    "  function settingsLabel(caps) {\n"
    "    return caps && has(caps.settings_label) ? String(caps.settings_label) : SETTINGS_LABEL;\n"
    "  }\n"
    "  // The off line's link: \"Open the \" + the label. An ordinary capitalised first word is lower-cased (\"Assistant\n"
    "  // settings\" -> \"Open the assistant settings\"); a name keeps its capitals: a first word with other capitals\n"
    "  // (\"OrcaSlicer MCP settings\") or one followed by an acronym (\"Remote API settings\").\n"
    "  function settingsLink(caps) {\n"
    "    var l = settingsLabel(caps);\n"
    "    var words = l.split(' ');\n"
    "    var plain = /^[A-Z][a-z]+$/.test(words[0]) && !/^[A-Z]{2,}$/.test(words[1] || '');\n"
    "    return 'Open the ' + (plain ? l.charAt(0).toLowerCase() + l.slice(1) : l);\n"
    "  }\n"
    "\n"
    "  function liveEntry(row, live) {\n"
    "    return live && Object.prototype.hasOwnProperty.call(live, row.id) ? live[row.id] : null;\n"
    "  }\n"
    "\n"
    "  // The value a row shows. tracked (the host's capabilities.tracked_live): the host sends live entries only for the\n"
    "  // rows it tracks (the latest row of each setting, ruling R43), so any row with an entry shows it; without it, only\n"
    "  // the newest group's rows do (isNewest).\n"
    "  // A row the host marks dropped (the assistant's own preset switch dropped its edit) shows the value it set with a\n"
    "  // note, never a live value and never \"changed by you\".\n"
    "  function liveDisplay(row, live, isNewest, tracked) {\n"
    "    var shown = has(row.value) ? String(row.value) : '';\n"
    "    if (row.dropped === true) return { value: shown, note: 'dropped by a preset switch' };\n"
    "    var e = (tracked || isNewest) ? liveEntry(row, live) : null;\n"
    "    if (!e) return { value: shown, note: '' };\n"
    "    if (!e.present) return { value: shown, note: 'removed since' };\n"
    "    // The user put the setting back to what it was before the assistant changed it (for example with OrcaSlicer's\n"
    "    // own reset arrow beside the setting): that undoes the assistant's change rather than making a new one.\n"
    "    if (!sameValue(e.value, shown) && has(row.before) && sameValue(e.value, String(row.before))) {\n"
    "      return { value: String(e.value), note: 'set back by you' };\n"
    "    }\n"
    "    if (!sameValue(e.value, shown)) return { value: String(e.value), note: 'changed by you' };\n"
    "    return { value: shown, note: '' };\n"
    "  }\n"
    "\n"
    "  // canRestore (optional, default true): the host offers restore points (capabilities.restore); without them the\n"
    "  // reason does not send the user to one.\n"
    "  function undoState(row, live, isNewest, tracked, canRestore) {\n"
    "    var again = canRestore === false ? 'Changed again since' : 'Changed again since; restore an earlier point instead';\n"
    "    if (!row.undo) return { available: false, why: '' };\n"
    "    if (has(row.undone_at)) return { available: false, why: 'Reverted at ' + row.undone_at };\n"
    "    // The assistant's own preset switch dropped the edit (the host's dropped flag): nothing of it is left to undo.\n"
    "    if (row.dropped === true) {\n"
    "      return { available: false, why: canRestore === false ? 'Dropped by a preset switch' : 'Dropped by a preset switch; restore an earlier point instead' };\n"
    "    }\n"
    "    // The assistant changed the same setting again later (the host's superseded flag): undoing this row would\n"
    "    // overwrite that change.\n"
    "    if (row.superseded === true) return { available: false, why: again };\n"
    "    var e = liveEntry(row, live);\n"
    "    if (e) {\n"
    "      if (!e.present) {\n"
    "        var name = row.where || row.value || row.label || 'It';\n"
    "        return { available: false, why: name + ' is no longer on the plate' };\n"
    "      }\n"
    "      if (!sameValue(e.value, has(row.value) ? String(row.value) : '') && has(row.before) && sameValue(e.value, String(row.before))) {\n"
    "        return { available: false, why: 'Already back to ' + String(row.before) };\n"
    "      }\n"
    "      if (!sameValue(e.value, has(row.value) ? String(row.value) : '')) {\n"
    "        return {\n"
    "          available: false,\n"
    "          why: (tracked || isNewest) ? 'You changed it since' : again\n"
    "        };\n"
    "      }\n"
    "    }\n"
    "    return { available: true, why: '' };\n"
    "  }\n"
    "\n"
    "  // ---- Colours (polish P2): measured against the backgrounds the host really gives -------------------------------\n"
    "  // A CSS colour (\"#rgb\", \"#rrggbb\", \"#rrggbbaa\", \"rgb(...)\", \"rgba(...)\") as [r, g, b, a], or null.\n"
    "  function parseColour(s) {\n"
    "    var t = String(s === null || s === undefined ? '' : s).trim(), m;\n"
    "    if ((m = /^#([0-9a-f])([0-9a-f])([0-9a-f])$/i.exec(t))) return [parseInt(m[1] + m[1], 16), parseInt(m[2] + m[2], 16), parseInt(m[3] + m[3], 16), 1];\n"
    "    if ((m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})?$/i.exec(t)))\n"
    "      return [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16), m[4] ? parseInt(m[4], 16) / 255 : 1];\n"
    "    if ((m = /^rgba?\\(\\s*([\\d.]+)[,\\s]+([\\d.]+)[,\\s]+([\\d.]+)(?:\\s*[,/]\\s*([\\d.]+%?))?\\s*\\)$/i.exec(t))) {\n"
    "      var a = m[4] === undefined ? 1 : (/%$/.test(m[4]) ? parseFloat(m[4]) / 100 : parseFloat(m[4]));\n"
    "      return [Number(m[1]), Number(m[2]), Number(m[3]), a];\n"
    "    }\n"
    "    return null;\n"
    "  }\n"
    "  function hexOf(c) {\n"
    "    return '#' + c.slice(0, 3).map(function (v) { var h = Math.max(0, Math.min(255, Math.round(v))).toString(16); return h.length < 2 ? '0' + h : h; }).join('');\n"
    "  }\n"
    "  // fg (with its alpha) laid over an opaque bg.\n"
    "  function over(fg, bg) {\n"
    "    var a = fg.length > 3 ? fg[3] : 1;\n"
    "    return [fg[0] * a + bg[0] * (1 - a), fg[1] * a + bg[1] * (1 - a), fg[2] * a + bg[2] * (1 - a), 1];\n"
    "  }\n"
    "  function channel(v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }\n"
    "  // WCAG 2 relative luminance and contrast ratio.\n"
    "  function luminance(c) { return 0.2126 * channel(c[0]) + 0.7152 * channel(c[1]) + 0.0722 * channel(c[2]); }\n"
    "  function contrast(a, b) {\n"
    "    var x = luminance(a), y = luminance(b);\n"
    "    return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);\n"
    "  }\n"
    "  // The backgrounds a colour of the page sits on: the host's, and the same under the hover and open-row tints\n"
    "  // (--hover-soft and --hover: mid grey at 8 % and 12 %).\n"
    "  var TINTS = [0.08, 0.12];\n"
    "  function backgroundsOf(bg) {\n"
    "    return [bg.slice(0, 3)].concat(TINTS.map(function (a) { return over([127, 127, 127, a], bg).slice(0, 3); }));\n"
    "  }\n"
    "  function minContrast(c, bgs) {\n"
    "    var m = Infinity;\n"
    "    bgs.forEach(function (b) { m = Math.min(m, contrast(c, b)); });\n"
    "    return m;\n"
    "  }\n"
    "  function toHsl(c) {\n"
    "    var r = c[0] / 255, g = c[1] / 255, b = c[2] / 255;\n"
    "    var mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2, h = 0, sat = 0, d = mx - mn;\n"
    "    if (d > 0) {\n"
    "      sat = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);\n"
    "      h = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;\n"
    "      h /= 6;\n"
    "    }\n"
    "    return [h, sat, l];\n"
    "  }\n"
    "  function fromHsl(h, sat, l) {\n"
    "    function f(p, q, t) {\n"
    "      if (t < 0) t += 1;\n"
    "      if (t > 1) t -= 1;\n"
    "      if (t < 1 / 6) return p + (q - p) * 6 * t;\n"
    "      if (t < 1 / 2) return q;\n"
    "      if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;\n"
    "      return p;\n"
    "    }\n"
    "    if (sat === 0) return [Math.round(l * 255), Math.round(l * 255), Math.round(l * 255)];\n"
    "    var q = l < 0.5 ? l * (1 + sat) : l + sat - l * sat, p = 2 * l - q;\n"
    "    return [Math.round(f(p, q, h + 1 / 3) * 255), Math.round(f(p, q, h) * 255), Math.round(f(p, q, h - 1 / 3) * 255)];\n"
    "  }\n"
    "  // base as it is when it reaches target on every background, else its lightness moved away from them (darker on a\n"
    "  // light background, lighter on a dark one) until it does.\n"
    "  function readable(base, bgs, target) {\n"
    "    var c = base.slice(0, 3).map(Math.round);\n"
    "    if (minContrast(c, bgs) >= target) return c;\n"
    "    var light = contrast([0, 0, 0], bgs[0]) >= contrast([255, 255, 255], bgs[0]);\n"
    "    var hsl = toHsl(c);\n"
    "    for (var i = 1; i <= 100; i++) {\n"
    "      var l = light ? hsl[2] - i / 100 : hsl[2] + i / 100;\n"
    "      if (l < 0 || l > 1) break;\n"
    "      var cand = fromHsl(hsl[0], hsl[1], l);\n"
    "      if (minContrast(cand, bgs) >= target) return cand;\n"
    "    }\n"
    "    return light ? [0, 0, 0] : [255, 255, 255];\n"
    "  }\n"
    "  // The starting points per side (the CSS defaults) and the ratio each must reach: text 4.5:1 (4.6 kept, for the\n"
    "  // engine's own rounding of the tints), icons and the focus ring 3:1 (WCAG 1.4.3, 1.4.11).\n"
    "  var THEME_BASE = {\n"
    "    dark:  { link: '#5ad1c4', faint: '#9a9aa0', warn: '#f28b82', focus: '#5ad1c4',\n"
    "             't-set': '#5fa8e8', 't-preset': '#b28be8', 't-model': '#e8a95a', 't-plate': '#74c29a', 't-slice': '#00a991' },\n"
    "    light: { link: '#00695c', faint: '#5f5f64', warn: '#b3261e', focus: '#00695c',\n"
    "             't-set': '#1f6fb8', 't-preset': '#7a4fc4', 't-model': '#a8650f', 't-plate': '#2e7d55', 't-slice': '#00796b' }\n"
    "  };\n"
    "  var TEXT_RATIO = 4.6, ICON_RATIO = 3.1;\n"
    "  // bg: the background the host gives (any CSS colour). Returns {theme: 'light'|'dark', vars: {name: '#rrggbb'}}, or\n"
    "  // null when bg is not an opaque colour.\n"
    "  function themeColours(bg) {\n"
    "    var b = parseColour(bg);\n"
    "    if (!b || b[3] < 0.99) return null;\n"
    "    var bgs = backgroundsOf(b);\n"
    "    var theme = contrast([0, 0, 0], b) >= contrast([255, 255, 255], b) ? 'light' : 'dark';\n"
    "    var base = THEME_BASE[theme], vars = {};\n"
    "    Object.keys(base).forEach(function (k) {\n"
    "      var ratio = k === 'link' || k === 'faint' || k === 'warn' ? TEXT_RATIO : ICON_RATIO;\n"
    "      vars[k] = hexOf(readable(parseColour(base[k]), bgs, ratio));\n"
    "    });\n"
    "    return { theme: theme, vars: vars };\n"
    "  }\n"
    "\n"
    "  // ---- Keyboard tooltips (polish P5) ------------------------------------------------------------------------------\n"
    "  // Where the tag of a focused control goes: above it when that fits inside the view, else below, else on the side\n"
    "  // with more room; never over the control. Horizontally centred on it and kept inside the view. ctrl = {left, top,\n"
    "  // right, bottom}, size = {w, h} of the tag, view = {w, h}. Returns {left, top, width, place}.\n"
    "  function placeTip(ctrl, size, view, gap, margin) {\n"
    "    gap = typeof gap === 'number' ? gap : 6;\n"
    "    margin = typeof margin === 'number' ? margin : 4;\n"
    "    var w = Math.min(size.w, Math.max(0, view.w - 2 * margin));\n"
    "    var left = (ctrl.left + ctrl.right) / 2 - w / 2;\n"
    "    left = Math.max(margin, Math.min(left, view.w - margin - w));\n"
    "    var above = ctrl.top - gap - size.h, below = ctrl.bottom + gap, top, place;\n"
    "    if (above >= margin) { top = above; place = 'above'; }\n"
    "    else if (below + size.h <= view.h - margin) { top = below; place = 'below'; }\n"
    "    else if (ctrl.top >= view.h - ctrl.bottom) { top = above; place = 'above'; }\n"
    "    else { top = below; place = 'below'; }\n"
    "    return { left: Math.round(left), top: Math.round(top), width: Math.round(w), place: place };\n"
    "  }\n"
    "\n"
    "  root.McpPanelCore = {\n"
    "    PROTOCOL_VERSION: PROTOCOL_VERSION,\n"
    "    groupRows: groupRows,\n"
    "    groupTitle: groupTitle,\n"
    "    groupResult: groupResult,\n"
    "    sliceStale: sliceStale,\n"
    "    latestSlice: latestSlice,\n"
    "    orderedRows: orderedRows,\n"
    "    liveDisplay: liveDisplay,\n"
    "    undoState: undoState,\n"
    "    sameValue: sameValue,\n"
    "    colourOf: colourOf,\n"
    "    offText: offText,\n"
    "    settingsLabel: settingsLabel,\n"
    "    settingsLink: settingsLink,\n"
    "    parseColour: parseColour,\n"
    "    contrast: contrast,\n"
    "    backgroundsOf: backgroundsOf,\n"
    "    themeColours: themeColours,\n"
    "    placeTip: placeTip\n"
    "  };\n"
    "})(window);\n"
    "</script>\n"
    "<script id=\"panel-view\">\n"
    "(function () {\n"
    "  'use strict';\n"
    "  var Core = window.McpPanelCore;\n"
    "  var TIP = { setting: 'Setting', preset: 'Preset', model: 'Model', plate: 'Plate', slice: 'Slice result', refused: 'Refused', file: 'File' };\n"
    "  var history = { rows: [], groups: [] };\n"
    "  var caps = { restore: false, open_settings: true, settings_hint: '', off_text: '', settings_label: '', reveal: false, tracked_live: false };\n"
    "  var restoredTo = null, canUndoRestore = false, restoreFailed = false;\n"
    "  var live = {};\n"
    "  var api = 'on';\n"
    "  var setup = null;            // the host's \"setup\" message: how to connect an AI app (first run), or null\n"
    "  var setupApp = null;         // the app square picked in it\n"
    "  var setupDone = {};          // which of its buttons were pressed (\"Copied\", \"Opening...\")\n"
    "  var openGroups = {};          // group id -> bool\n"
    "  var newestSeen = null;        // newest group id at the last render\n"
    "  var openRows = {};            // row id -> bool\n"
    "  var pendingUndo = {};         // row id -> 'busy'\n"
    "  var undoError = {};           // row id -> reason\n"
    "  var revertedHere = {};        // row id -> true (ok result, until history catches up)\n"
    "  var asking = null;            // group id with confirm showing\n"
    "  var restoreBusy = null;       // group id | 'undo'\n"
    "  var restoreError = {};        // group id | 'undo' -> reason\n"
    "  var staleUpTo = null;         // slice rows up to this id are out of date (the user reverted or restored since)\n"
    "  var sliceValid = null;        // the host's slice_valid: false = the newest slice result no longer holds; null = not sent\n"
    "  var handOpened = {};          // group id -> true: the user opened it (a click or Expand all); it stays open\n"
    "  var restoreUndoneAt = null;   // newest row id when the user undid a restore: \"Restore undone\" until a newer row\n"
    "  var focusNext = null;         // data-fk of the control to focus: kept until a render finds it (or 5 renders pass)\n"
    "  var focusTries = 0;\n"
    "  var kbFocus = false;          // the last focus came from the keyboard (a key was pressed since the last click)\n"
    "  var themeBg = null;           // the background the colours were last measured on\n"
    "  var groups = [];\n"
    "  var READY_RETRY_MS = 500, READY_RETRIES = 20;\n"
    "  var gotHistory = false, readyTries = 0, readyTimer = null;\n"
    "\n"
    "  function esc(s) { return String(s === null || s === undefined ? '' : s).replace(/[&<>\"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '\"': '&quot;', \"'\": '&#39;' }[c]; }); }\n"
    "  function has(s) { return s !== null && s !== undefined && s !== ''; }\n"
    "  function send(msg) { try { if (window.orca && window.orca.postMessage) window.orca.postMessage(msg); } catch (e) {} }\n"
    "  function icon(k) { return '<svg class=\"i k-' + k + '\" aria-hidden=\"true\"><use href=\"#i-' + k + '\"/></svg>'; }\n"
    "\n"
    "  // The page never prints itself (a web view's Ctrl+P would open a print sheet over the panel).\n"
    "  try { window.print = function () {}; } catch (e) {}\n"
    "\n"
    "  // Link, faint, warning, focus and type colours follow the background the host really gives (its --orca-bg can be\n"
    "  // light or dark, and any shade): the side (data-theme), then each colour measured against it and its row tints\n"
    "  // (Core.themeColours), set on the root. Measured again only when the background changes (a theme switch).\n"
    "  function applyTheme() {\n"
    "    try {\n"
    "      if (!window.getComputedStyle || !document.body || !document.documentElement) return;\n"
    "      var bg = window.getComputedStyle(document.body).backgroundColor || '';\n"
    "      if (bg === themeBg) return;\n"
    "      var t = Core.themeColours(bg);\n"
    "      if (!t) return;\n"
    "      themeBg = bg;\n"
    "      var rootEl = document.documentElement;\n"
    "      if (rootEl.getAttribute('data-theme') !== t.theme) rootEl.setAttribute('data-theme', t.theme);\n"
    "      if (rootEl.style && rootEl.style.setProperty)\n"
    "        Object.keys(t.vars).forEach(function (k) { rootEl.style.setProperty('--' + k, t.vars[k]); });\n"
    "    } catch (e) {}\n"
    "  }\n"
    "\n"
    "  function isNewest(g) { return groups.length && groups[0].id === g.id; }\n"
    "  // The groups the list shows: every real group, newest first. The start point (id 0) stays in groups (the\n"
    "  // \"Restored to\" line names its time) but has no row of its own.\n"
    "  function shownGroups() { return groups.filter(function (g) { return g.id !== 0; }); }\n"
    "  // A setting row the host can find in its settings sidebar (capabilities.reveal and the row's reveal flag).\n"
    "  function revealable(r) { return caps.reveal && r.reveal === true; }\n"
    "  function rowById(id) {\n"
    "    var rs = history.rows;\n"
    "    for (var i = 0; i < rs.length; i++) if (rs[i].id === id) return rs[i];\n"
    "    return null;\n"
    "  }\n"
    "  function maxRowId() {\n"
    "    var m = 0;\n"
    "    history.rows.forEach(function (r) { if (typeof r.id === 'number' && r.id > m) m = r.id; });\n"
    "    return m;\n"
    "  }\n"
    "  function isUndone(g) {\n"
    "    if (restoredTo === null || restoredTo === undefined) return false;\n"
    "    if (restoreFailed) return false; // the host marked nothing undone\n"
    "    if (g.id === restoredTo) return false;\n"
    "    var t = null;\n"
    "    groups.forEach(function (x) { if (x.id === restoredTo) t = x; });\n"
    "    if (restoredTo === 0) return g.id !== 0;\n"
    "    if (!t) return false;\n"
    "    return g.id !== 0 && (g.ts > t.ts || (g.ts === t.ts && g.id > t.id));\n"
    "  }\n"
    "  function groupsUndoneBy(g) {\n"
    "    return groups.filter(function (x) {\n"
    "      if (x.id === g.id) return false;\n"
    "      if (g.id === 0) return x.id !== 0;\n"
    "      return x.id !== 0 && (x.ts > g.ts || (x.ts === g.ts && x.id > g.id));\n"
    "    });\n"
    "  }\n"
    "  function savedToDisk(r) { return r.on_disk === true; }\n"
    "  // The preset a setting row was written to (\"0.20mm Standard @Printer (unsaved)\") goes to the hover tag: the line\n"
    "  // itself says only \"Was X\".\n"
    "  function whereInTip(r) { return has(r.where) && (r.reveal === true || /\\(unsaved\\)$/.test(String(r.where))); }\n"
    "  function detailText(r, disp) {\n"
    "    var parts = [];\n"
    "    var b = has(r.before) ? String(r.before) : '';\n"
    "    if (disp.note === 'changed by you' && b) {\n"
    "      parts.push('Assistant set ' + r.value + ' \\u00b7 was ' + b);\n"
    "      return parts.join(' \\u00b7 ');\n"
    "    }\n"
    "    if (disp.note === 'set back by you') return 'Assistant set ' + r.value + ' \\u00b7 you set it back';\n"
    "    if (b) parts.push('Was ' + b);\n"
    "    if (has(r.where) && !whereInTip(r)) parts.push(r.where);\n"
    "    return parts.join(' \\u00b7 ');\n"
    "  }\n"
    "  function undoTip(r) {\n"
    "    if (has(r.undo.tip)) return r.undo.tip;\n"
    "    if (has(r.before)) return r.undo.verb + ': ' + r.label + ' back to ' + r.before;\n"
    "    return r.undo.verb + ' ' + (has(r.value) ? r.value : r.label);\n"
    "  }\n"
    "  // What a click on the row does, for its hover tag.\n"
    "  function rowTip(r) {\n"
    "    if (r.undo && revealable(r)) return 'Show what it was, undo it, and find it in settings';\n"
    "    if (r.undo) return 'Show what it was and undo it';\n"
    "    if (revealable(r)) return 'Show what it was and find it in settings';\n"
    "    return 'Show details';\n"
    "  }\n"
    "\n"
    "  // Filament colour rows: a small swatch of the colour shown beside it (the value is validated as #hex first).\n"
    "  function swatch(r, value) {\n"
    "    if (r.kind !== 'setting') return '';\n"
    "    var c = Core.colourOf(value);\n"
    "    return c ? '<span class=\"sw\" style=\"background:' + c + '\"></span>' : '';\n"
    "  }\n"
    "\n"
    "  function rowHtml(r, g, newest) {\n"
    "    var reverted = has(r.undone_at) || !!revertedHere[r.id];\n"
    "    // A reverted row shows what it undid, whatever the live value says: right after Revert the live message (the\n"
    "    // earlier value) can come before the history that marks the row undone, and must not read \"changed by you\".\n"
    "    var disp = reverted ? { value: has(r.value) ? String(r.value) : '', note: '' } : Core.liveDisplay(r, live, newest, caps.tracked_live);\n"
    "    var detail = detailText(r, disp);\n"
    "    var kind = r.kind === 'refused' || (r.kind === 'slice' && has(r.error)) ? 'refused' : r.kind;\n"
    "    var stale = Core.sliceStale(r, history.rows, staleUpTo, sliceValid);\n"
    "    var latest = stale ? Core.latestSlice(history.rows) : null;\n"
    "    var staleNote = stale && latest && latest.id === r.id;\n"
    "    var rc = (kind === 'refused' ? ' refused' : '') + (stale ? ' stale' : '') + (reverted ? ' reverted' : '') + (r.dropped === true && !reverted ? ' dropped' : '');\n"
    "    var isOpen = !!openRows[r.id];\n"
    "    var open = isOpen ? ' open' : '';\n"
    "    var tail = '';\n"
    "    var errLine = undoError[r.id];\n"
    "    if (r.undo) {\n"
    "      var st = Core.undoState(r, live, newest, caps.tracked_live, caps.restore);\n"
    "      if (reverted) {\n"
    "        tail = '<span class=\"faint\">Reverted</span>';\n"
    "        if (has(r.undone_at)) tail = '<span class=\"faint tip-left\" data-tip=\"Reverted at ' + esc(r.undone_at) + '\">Reverted</span>';\n"
    "      } else if (pendingUndo[r.id]) {\n"
    "        tail = '<span class=\"faint\">Reverting\\u2026</span>';\n"
    "      } else if (st.available) {\n"
    "        tail = '<button type=\"button\" class=\"lnk tip-left\" data-undo=\"' + r.id + '\" data-fk=\"u:' + r.id + '\" data-tip=\"' + esc(undoTip(r)) + '\">' + esc(r.undo.verb) + '</button>';\n"
    "      } else {\n"
    "        tail = '<button type=\"button\" class=\"lnk off tip-left\" aria-disabled=\"true\" data-fk=\"u:' + r.id + '\" data-tip=\"' + esc(st.why) + '\" aria-label=\"' +\n"
    "          esc(r.undo.verb + ' (unavailable: ' + st.why + ')') + '\">' + esc(r.undo.verb) + '</button>';\n"
    "      }\n"
    "    }\n"
    "    // A multi-line value (a G-code template) carries a few of its lines for the hover tags (value_tip, before_tip).\n"
    "    var dTip = whereInTip(r) ? ' data-tip=\"' + esc(r.where) + '\"' : (has(r.before_tip) && has(r.before) ? ' data-tip=\"' + esc(r.before_tip) + '\"' : '');\n"
    "    var more = '<div class=\"more\"><span class=\"d\"' + dTip + '>' + esc(detail) + '</span>' + tail + '</div>';\n"
    "    if (errLine) more = '<div class=\"more\"><span class=\"d bad\">' + esc(errLine) + '</span>' + tail + '</div>';\n"
    "    else if (kind === 'refused' && has(r.error) && !detail) more = '<div class=\"more\"><span class=\"d\">' + esc(r.error) + '</span>' + tail + '</div>';\n"
    "    // A reverted row shows what is there now (the earlier value); the undone value, struck through, goes under the\n"
    "    // label so the label keeps its width.\n"
    "    var value = esc(disp.value);\n"
    "    var shownText = disp.value;\n"
    "    var noteHtml = '';\n"
    "    var vTip = '';\n"
    "    if (reverted) {\n"
    "      noteHtml = '<span class=\"chg\"><s class=\"old\">' + esc(disp.value) + '</s> reverted</span>';\n"
    "      value = has(r.before) ? swatch(r, r.before) + esc(r.before) : '';\n"
    "      shownText = has(r.before) ? r.before : '';\n"
    "      vTip = has(r.before) && has(r.before_tip) ? r.before_tip : '';\n"
    "    } else {\n"
    "      value = swatch(r, disp.value) + value;\n"
    "      if (has(r.value_tip) && disp.value === String(has(r.value) ? r.value : '')) vTip = r.value_tip;\n"
    "      else if (has(r.before_tip) && has(r.before) && disp.value === String(r.before)) vTip = r.before_tip;\n"
    "    }\n"
    "    var note = reverted ? '' : (disp.note || (staleNote ? 'out of date' : ''));\n"
    "    if (note) noteHtml = '<span class=\"chg\">' + esc(note) + '</span>';\n"
    "    if (String(shownText || '').length > 18) rc += ' long';\n"
    "    var label = r.label + (has(shownText) ? ' ' + shownText : '') + (note ? ', ' + note : '') + (reverted ? ', reverted' : '');\n"
    "    return '<div class=\"row' + rc + open + '\" data-row=\"' + r.id + '\" data-fk=\"r:' + r.id + '\" role=\"button\" tabindex=\"0\" aria-expanded=\"' + isOpen + '\" aria-label=\"' + esc(label) + '\">' +\n"
    "      '<span class=\"ic\" data-tip=\"' + esc(TIP[kind] || '') + '\">' + icon(kind) + '</span>' +\n"
    "      '<span class=\"l\"' + (!isOpen ? ' data-tip=\"' + esc(rowTip(r)) + '\"' : '') + '><span class=\"lt\">' + esc(r.label) + '</span>' +\n"
    "      noteHtml + '</span><span class=\"v\"' + (vTip ? ' data-tip=\"' + esc(vTip) + '\"' : '') + '>' + value + '</span></div>' + more;\n"
    "  }\n"
    "\n"
    "  function groupHtml(g, idx) {\n"
    "    var newest = idx === 0;\n"
    "    var undone = isUndone(g);\n"
    "    var open = !!openGroups[g.id] && !undone;\n"
    "    var res = Core.groupResult(g, history.rows, staleUpTo, sliceValid);\n"
    "    var cls = 'group' + (open ? ' open' : '') + (undone ? ' undone' : '') + (newest ? ' newest' : ' older');\n"
    "    var restorable = caps.restore && !newest && !undone && g.restore;\n"
    "    var ask = '';\n"
    "    if (restoreBusy === g.id) {\n"
    "      cls += ' busy';\n"
    "      ask = '<div class=\"ask\"><span class=\"line wait\">Restoring\\u2026</span></div>';\n"
    "    } else if (restoreError[g.id]) {\n"
    "      cls += ' showerr';\n"
    "      ask = '<div class=\"ask\"><span class=\"line\"><span class=\"bad\">' + esc(restoreError[g.id]) + '</span><button type=\"button\" class=\"lnk no\" data-dismiss=\"' + g.id + '\" data-fk=\"d:' + g.id + '\">OK</button></span></div>';\n"
    "    } else if (asking === g.id) {\n"
    "      cls += ' asking';\n"
    "      var notes = '';\n"
    "      var undoneGroups = groupsUndoneBy(g);\n"
    "      var disk = undoneGroups.some(function (x) { return x.rows.some(savedToDisk); });\n"
    "      if (disk) notes += '<span class=\"note\">Files and presets saved to disk stay.</span>';\n"
    "      notes += '<span class=\"note\">Your own changes since then are undone too.</span>';\n"
    "      ask = '<div class=\"ask\" role=\"group\" aria-label=\"' + esc('Restore to ' + g.time + '?') + '\"><span class=\"line\">Restore to ' + esc(g.time) + '? ' +\n"
    "        '<button type=\"button\" class=\"lnk\" data-yes=\"' + g.id + '\" data-fk=\"y:' + g.id + '\">Yes</button>' +\n"
    "        '<button type=\"button\" class=\"lnk no\" data-no=\"' + g.id + '\" data-fk=\"n:' + g.id + '\">No</button></span>' + notes + '</div>';\n"
    "    }\n"
    "    var act = '';\n"
    "    if (restorable && !ask) {\n"
    "      var ok = g.restore.available;\n"
    "      var tip = ok ? 'Restore the project to how it was at ' + g.time + ' (undoes later changes)' : (g.restore.why || 'No restore point here');\n"
    "      act = '<button type=\"button\" class=\"act tip-left' + (ok ? '' : ' off') + '\" ' + (ok ? 'data-ask=\"' + g.id + '\"' : 'aria-disabled=\"true\"') +\n"
    "        ' data-fk=\"a:' + g.id + '\" data-tip=\"' + esc(tip) + '\" aria-label=\"' + esc(tip) + '\"><svg aria-hidden=\"true\"><use href=\"#i-restore\"/></svg></button>';\n"
    "    }\n"
    "    var resText = res.text + (res.text && res.stale && res.current ? ' \\u00b7 out of date' : '');\n"
    "    var resHtml = '<span class=\"res' + (res.stale ? ' stale' : '') + '\"' + (res.stale ? ' data-tip=\"Out of date: something changed after this slice\"' : '') + '>' +\n"
    "      (res.warn ? icon('refused') : '') + esc(resText) + '</span>';\n"
    "    var title = Core.groupTitle(g);\n"
    "    return '<div class=\"' + cls + '\" data-g=\"' + g.id + '\"><div class=\"ghead\" data-toggle=\"' + g.id + '\">' +\n"
    "      '<button type=\"button\" class=\"gt\" data-toggle=\"' + g.id + '\" data-fk=\"g:' + g.id + '\" aria-expanded=\"' + open + '\" aria-label=\"' + esc(g.time + ' ' + title + (undone ? ', undone by the restore' : '')) + '\">' +\n"
    "      '<span class=\"t\">' + esc(g.time) + '</span><span class=\"nm\" data-tip=\"' + esc(title) + '\"><span class=\"name\">' + esc(title) + '</span></span></button>' +\n"
    "      resHtml + act + ask + '</div><div class=\"body\">' + Core.orderedRows(g).map(function (r) { return rowHtml(r, g, newest && !undone); }).join('') + '</div></div>';\n"
    "  }\n"
    "\n"
    "  function restoredLineHtml() {\n"
    "    if (restoredTo === null || restoredTo === undefined) {\n"
    "      // After the user undid a restore: a faint line until the next change.\n"
    "      if (restoreUndoneAt !== null && restoreUndoneAt === maxRowId())\n"
    "        return '<div class=\"group restored\"><div class=\"ghead\"><span class=\"t\"></span><span class=\"rl\"><span class=\"name faint\">Restore undone</span></span></div></div>';\n"
    "      return '';\n"
    "    }\n"
    "    var t = '';\n"
    "    groups.forEach(function (g) { if (g.id === restoredTo) t = g.time; });\n"
    "    // Column 3 holds only the transient states; the Undo link follows the text in column 2 (\"Restored to 10:31 ·\n"
    "    // Undo\"), outside .name so its hover tag is not clipped by the ellipsis.\n"
    "    var right = '';\n"
    "    if (restoreBusy === 'undo') right = '<span class=\"ask\" style=\"display:flex;grid-column:auto;padding:0\"><span class=\"line wait\">Restoring\\u2026</span></span>';\n"
    "    else if (restoreError.undo) right = '<span class=\"ask\" style=\"display:flex;grid-column:auto;padding:0\"><span class=\"line\"><span class=\"bad\">' + esc(restoreError.undo) + '</span></span></span>';\n"
    "    var inline = canUndoRestore && restoreBusy === null && !restoreError.undo;\n"
    "    var lead = restoreFailed ? 'Couldn\\'t restore to ' : 'Restored to ';\n"
    "    return '<div class=\"group restored\"><div class=\"ghead\"><span class=\"t\"></span><span class=\"rl\"><span class=\"name\">' + esc(lead + t) + '</span>' +\n"
    "      (inline ? '<span class=\"sep\">\\u00b7</span><button type=\"button\" class=\"lnk tip-left\" data-undo-restore=\"1\" data-fk=\"ur\" data-tip=\"Go back to before the restore\">Undo</button>' : '') +\n"
    "      '</span>' + right + '</div></div>';\n"
    "  }\n"
    "\n"
    "  function updateFold() {\n"
    "    var cand = shownGroups().filter(function (g) { return !isUndone(g); });\n"
    "    var allOpen = cand.length > 0 && cand.every(function (g) { return openGroups[g.id]; });\n"
    "    var f = document.getElementById('fold');\n"
    "    f.setAttribute('data-tip', allOpen ? 'Collapse all' : 'Expand all');\n"
    "    f.setAttribute('aria-label', allOpen ? 'Collapse all' : 'Expand all');\n"
    "    f.innerHTML = '<svg aria-hidden=\"true\"><use href=\"#i-' + (allOpen ? 'collapse' : 'expand') + '\"/></svg>';\n"
    "    f.style.visibility = shownGroups().length ? 'visible' : 'hidden';\n"
    "  }\n"
    "  function updateGear() {\n"
    "    var g = document.getElementById('gear');\n"
    "    var tip = caps.open_settings ? Core.settingsLabel(caps) : (caps.settings_hint || 'Settings');\n"
    "    g.setAttribute('data-tip', tip);\n"
    "    g.setAttribute('aria-label', tip);\n"
    "    g.innerHTML = '<svg aria-hidden=\"true\"><use href=\"#i-gear\"/></svg>';\n"
    "  }\n"
    "\n"
    "  // ---- The keyboard focus's tag (P5): placed by Core.placeTip inside the pane, never over the control.\n"
    "  var ftip = document.getElementById('ftip') || null;\n"
    "  function hideTip() {\n"
    "    if (!ftip) return;\n"
    "    ftip.hidden = true;\n"
    "    ftip.textContent = '';\n"
    "  }\n"
    "  // The control's tag: its own data-tip, or a row's (on its label while closed).\n"
    "  function tipText(el) {\n"
    "    if (!el || !el.getAttribute) return '';\n"
    "    var t = el.getAttribute('data-tip');\n"
    "    if (!t && el.querySelector) {\n"
    "      var l = el.querySelector('.l[data-tip]');\n"
    "      if (l) t = l.getAttribute('data-tip');\n"
    "    }\n"
    "    return t || '';\n"
    "  }\n"
    "  function showTip(el) {\n"
    "    if (!ftip || !el || !el.getBoundingClientRect) return;\n"
    "    var text = tipText(el);\n"
    "    if (!text) { hideTip(); return; }\n"
    "    var de = document.documentElement || {};\n"
    "    var vw = de.clientWidth || window.innerWidth || 0, vh = de.clientHeight || window.innerHeight || 0;\n"
    "    if (!vw || !vh) return;\n"
    "    ftip.textContent = text;\n"
    "    ftip.style.maxWidth = Math.max(60, Math.min(240, vw - 8)) + 'px';\n"
    "    ftip.style.left = '0px';\n"
    "    ftip.style.top = '0px';\n"
    "    ftip.hidden = false;\n"
    "    var r = el.getBoundingClientRect();\n"
    "    var pos = Core.placeTip({ left: r.left, top: r.top, right: r.right, bottom: r.bottom }, { w: ftip.offsetWidth, h: ftip.offsetHeight },\n"
    "                            { w: vw, h: vh }, 6, 4);\n"
    "    ftip.style.left = pos.left + 'px';\n"
    "    ftip.style.top = pos.top + 'px';\n"
    "  }\n"
    "\n"
    "  // Keyboard focus survives a render (the list is rebuilt): the control with the same data-fk gets it back, or the\n"
    "  // one an action asked for (focusNext).\n"
    "  function focusedKey() {\n"
    "    try {\n"
    "      var a = document.activeElement;\n"
    "      return a && a.getAttribute ? a.getAttribute('data-fk') : null;\n"
    "    } catch (e) { return null; }\n"
    "  }\n"
    "  // true when the control was found and focused.\n"
    "  function refocus(key) {\n"
    "    if (!key) return false;\n"
    "    try {\n"
    "      // Never take the keyboard from OrcaSlicer: only while the page itself has it (a live update after the user\n"
    "      // clicked into the sidebar must not pull focus back).\n"
    "      if (document.hasFocus && !document.hasFocus()) return false;\n"
    "      if (!document.querySelector) return false;\n"
    "      var el = document.querySelector('[data-fk=\"' + key + '\"]');\n"
    "      if (el && el.focus) { el.focus(); return true; }\n"
    "    } catch (e) {}\n"
    "    return false;\n"
    "  }\n"
    "  // An action's focus target (focusNext) waits for the render that has it: after Yes on a restore, the \"Restored to\n"
    "  // ... · Undo\" line comes with the next history, and the host may hold the focus meanwhile (m15).\n"
    "  function placeFocus(keep, pending) {\n"
    "    if (refocus(keep)) { if (pending) { focusNext = null; focusTries = 0; } return; }\n"
    "    if (pending && ++focusTries > 5) { focusNext = null; focusTries = 0; }\n"
    "  }\n"
    "\n"
    "  function render() {\n"
    "    applyTheme();\n"
    "    hideTip();\n"
    "    var pending = focusNext;\n"
    "    var keep = pending || focusedKey();\n"
    "    groups = Core.groupRows(history);\n"
    "    var html = '';\n"
    "    if (api === 'off') {\n"
    "      html += '<div class=\"empty offnote\">' + esc(Core.offText(caps)) + (caps.open_settings\n"
    "        ? ' <button type=\"button\" data-settings=\"1\" data-fk=\"set\">' + esc(Core.settingsLink(caps)) + '</button>'\n"
    "        : (caps.settings_hint ? '<br>' + esc(caps.settings_hint) : '')) + '</div>';\n"
    "    }\n"
    "    var shown = shownGroups();\n"
    "    if (!shown.length) {\n"
    "      if (api !== 'off') html += setup && setup.apps && setup.apps.length ? setupHtml()\n"
    "        : '<div class=\"empty\">When an assistant changes something, it shows up here.</div>';\n"
    "    } else {\n"
    "      html += restoredLineHtml();\n"
    "      html += shown.map(groupHtml).join(''); // index 0 is the newest group: the \"Restored to\" line is not one\n"
    "    }\n"
    "    var list = document.getElementById('list');\n"
    "    var top = list.scrollTop;\n"
    "    list.innerHTML = html;\n"
    "    list.scrollTop = top;\n"
    "    updateFold();\n"
    "    updateGear();\n"
    "    placeFocus(keep, pending);\n"
    "  }\n"
    "\n"
    "  function setupCurrent() {\n"
    "    var a = null;\n"
    "    setup.apps.forEach(function (x) { if (x.id === setupApp) a = x; });\n"
    "    return a || setup.apps[0];\n"
    "  }\n"
    "  function setupHtml() {\n"
    "    var st = setup.status;\n"
    "    if (st && st.seen) {\n"
    "      return '<div class=\"sconn\"><div class=\"sstat top\"><span class=\"sdot ok\"></span><b>' + esc(st.app ? 'Connected to ' + st.app : 'Connected') + '</b></div>' +\n"
    "        '<div class=\"slead\">Ask it for the print you want. Its changes will appear here, each with Revert.</div></div>';\n"
    "    }\n"
    "    var app = setupCurrent();\n"
    "    var squares = setup.apps.map(function (a) {\n"
    "      return '<button type=\"button\" class=\"ssq\" data-sapp=\"' + esc(a.id) + '\" data-fk=\"sa:' + esc(a.id) + '\" aria-pressed=\"' + (a.id === app.id) + '\">' +\n"
    "        (a.icon || '') + '<span>' + esc(a.name) + '</span></button>';\n"
    "    }).join('');\n"
    "    var routes = app.routes.map(function (r, j) {\n"
    "      var act = '';\n"
    "      if (!r.off) act = setupDone['r' + j] ? '<span class=\"sdone\">' + (r.open ? 'Opening\\u2026' : 'Copied') + '</span>'\n"
    "        : '<button type=\"button\" class=\"sact' + (r.primary ? '' : ' second') + '\" data-' + (r.open ? 'sopen' : 'scopy') + '=\"r' + j + '\" data-fk=\"sr' + j + '\">' + esc(r.act) + '</button>';\n"
    "      return '<div class=\"sroute' + (r.off ? ' off' : '') + '\"><b>' + esc(r.label) + '</b><small>' + esc(r.hint) + (r.also ? ' ' + esc(r.also) : '') + '</small>' + act + '</div>';\n"
    "    }).join('');\n"
    "    return '<div class=\"setup\"><div class=\"sh\">Connect your AI app</div>' +\n"
    "      '<div class=\"slead\">Then tell it the print you want. Every change it makes shows up here, and you can undo any of it.</div>' +\n"
    "      '<div class=\"ssqs\">' + squares + '</div><div class=\"sstep\"><span>Set it up where you use ' + esc(app.where || app.name) + '</span><div class=\"sroutes\">' + routes + '</div></div>' +\n"
    "      // No test question to ask: the first request from the app switches this to the connected view by itself.\n"
    "      '<div class=\"sstat\"><span class=\"sdot\"></span>Waiting for ' + esc(app.where || app.name) + '\\u2026</div></div>';\n"
    "  }\n"
    "  function copyToClipboard(text, done) {\n"
    "    function fallback() {\n"
    "      var a = document.createElement('textarea'); a.value = text; document.body.appendChild(a); a.select();\n"
    "      var ok = false; try { ok = document.execCommand('copy'); } catch (e) {} document.body.removeChild(a); done(ok);\n"
    "    }\n"
    "    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(function () { done(true); }, fallback);\n"
    "    else fallback();\n"
    "  }\n"
    "\n"
    "  function applyHistory(m) {\n"
    "    history = { rows: m.rows || [], groups: m.groups || [] };\n"
    "    var c = m.capabilities || {};\n"
    "    caps = { restore: !!c.restore, open_settings: c.open_settings !== false, settings_hint: c.settings_hint || '', off_text: c.off_text || '',\n"
    "             settings_label: c.settings_label || '', reveal: c.reveal === true, tracked_live: c.tracked_live === true };\n"
    "    restoredTo = has(m.restored_to) || m.restored_to === 0 ? m.restored_to : null;\n"
    "    restoreFailed = restoredTo !== null && m.restore_failed === true;\n"
    "    canUndoRestore = !!m.can_undo_restore;\n"
    "    sliceValid = typeof m.slice_valid === 'boolean' ? m.slice_valid : null;\n"
    "    gotHistory = true;\n"
    "    if (readyTimer !== null) { window.clearTimeout(readyTimer); readyTimer = null; }\n"
    "    var gs = Core.groupRows(history);\n"
    "    var newest = gs.length ? gs[0].id : null;\n"
    "    gs.forEach(function (g, i) { if (!(g.id in openGroups)) openGroups[g.id] = (i === 0); });\n"
    "    if (newest !== null && newest !== newestSeen && newestSeen !== null) {\n"
    "      openGroups[newest] = true;\n"
    "      // The latest group is open and older ones closed: the group that was newest closes now, unless the user opened\n"
    "      // it by hand (handOpened). Groups the user opened stay open.\n"
    "      if (!handOpened[newestSeen] && gs.some(function (g) { return g.id === newestSeen; })) openGroups[newestSeen] = false;\n"
    "    }\n"
    "    newestSeen = newest;\n"
    "    // rows that the host now reports as reverted no longer need the local mark\n"
    "    history.rows.forEach(function (r) { if (has(r.undone_at)) { delete revertedHere[r.id]; delete pendingUndo[r.id]; } });\n"
    "    if (asking !== null) {\n"
    "      var ag = null;\n"
    "      gs.forEach(function (g, i) { if (g.id === asking) ag = { g: g, newest: i === 0 }; });\n"
    "      if (!ag || ag.newest || !caps.restore || !ag.g.restore || !ag.g.restore.available || isUndone(ag.g)) asking = null;\n"
    "    }\n"
    "    render();\n"
    "    send({ action: 'live' });\n"
    "  }\n"
    "\n"
    "  function applyResult(m) {\n"
    "    if (m.action === 'undo') {\n"
    "      var id = m.row;\n"
    "      delete pendingUndo[id];\n"
    "      if (m.status === 'ok') { revertedHere[id] = true; delete undoError[id]; staleUpTo = Math.max(staleUpTo || 0, maxRowId()); }\n"
    "      else if (m.status === 'failed') { undoError[id] = m.reason || 'Could not revert'; openRows[id] = true; }\n"
    "    } else {\n"
    "      var key = m.action === 'undo_restore' ? 'undo' : m.group;\n"
    "      if (restoreBusy === key) restoreBusy = null;\n"
    "      if (m.status === 'failed') restoreError[key === undefined ? 'undo' : key] = m.reason || 'Could not restore';\n"
    "      // Where the keyboard goes: the restore's Undo line (it comes with the next history), or the error's OK.\n"
    "      if (m.action === 'restore' && m.status === 'ok') { focusNext = 'ur'; focusTries = 0; }\n"
    "      else if (m.action === 'restore' && m.status === 'failed' && key !== undefined) { focusNext = 'd:' + key; focusTries = 0; }\n"
    "      if (m.status === 'ok') staleUpTo = Math.max(staleUpTo || 0, maxRowId());\n"
    "      if (m.status === 'ok' && m.action === 'undo_restore') restoreUndoneAt = maxRowId();\n"
    "    }\n"
    "    render();\n"
    "  }\n"
    "\n"
    "  function liveSame(a, b) {\n"
    "    var ka = Object.keys(a), kb = Object.keys(b);\n"
    "    if (ka.length !== kb.length) return false;\n"
    "    for (var i = 0; i < ka.length; i++) {\n"
    "      var x = a[ka[i]], y = b[ka[i]];\n"
    "      if (!y) return false;\n"
    "      if (!!x.present !== !!y.present) return false;\n"
    "      if (x.present && !Core.sameValue(x.value, y.value)) return false;\n"
    "    }\n"
    "    return true;\n"
    "  }\n"
    "\n"
    "  function receive(m) {\n"
    "    if (!m || typeof m !== 'object') return;\n"
    "    if (m.type === 'history') applyHistory(m);\n"
    "    else if (m.type === 'live') {\n"
    "      var nv = m.values || {};\n"
    "      // slice_valid in a live message is optional: without it the last one stays.\n"
    "      var sv = typeof m.slice_valid === 'boolean' ? m.slice_valid : sliceValid;\n"
    "      if (!liveSame(live, nv) || sv !== sliceValid) { live = nv; sliceValid = sv; render(); }\n"
    "    }\n"
    "    else if (m.type === 'state') { api = m.api === 'off' ? 'off' : 'on'; render(); }\n"
    "    else if (m.type === 'result') applyResult(m);\n"
    "    else if (m.type === 'setup') { setup = m; render(); }\n"
    "  }\n"
    "  window.__mcpPanelReceive = receive;\n"
    "  if (window.orca && window.orca.onMessage) window.orca.onMessage(receive);\n"
    "\n"
    "  function ancestor(el, attr) {\n"
    "    while (el && el !== document) { if (el.getAttribute && el.hasAttribute(attr)) return el; el = el.parentNode; }\n"
    "    return null;\n"
    "  }\n"
    "  function toggleRow(rid) {\n"
    "    openRows[rid] = !openRows[rid];\n"
    "    // Opening a setting row also shows that setting in the host's settings sidebar (fire and forget).\n"
    "    var rr = openRows[rid] ? rowById(rid) : null;\n"
    "    if (rr && revealable(rr)) send({ action: 'reveal', row: rid });\n"
    "    focusNext = 'r:' + rid;\n"
    "    render();\n"
    "  }\n"
    "  // One click (or Enter / Space on a control): what it does.\n"
    "  function activate(target) {\n"
    "    var el;\n"
    "    // An unavailable control (a disabled restore icon or undo link) does nothing, and never toggles its group.\n"
    "    if (ancestor(target, 'aria-disabled')) return true;\n"
    "    if ((el = ancestor(target, 'data-sapp'))) { setupApp = el.getAttribute('data-sapp'); setupDone = {}; focusNext = 'sa:' + setupApp; render(); return true; }\n"
    "    if ((el = ancestor(target, 'data-sopen'))) {\n"
    "      var ok = el.getAttribute('data-sopen'), or = setupCurrent().routes[Number(ok.slice(1))];\n"
    "      if (or && or.open) { send({ action: 'open', url: or.open }); setupDone[ok] = true; render(); }\n"
    "      return true;\n"
    "    }\n"
    "    if ((el = ancestor(target, 'data-scopy'))) {\n"
    "      var ck = el.getAttribute('data-scopy'), text = (setupCurrent().routes[Number(ck.slice(1))] || {}).copy;\n"
    "      if (text) copyToClipboard(text, function (done) { if (done) { setupDone[ck] = true; render(); } });\n"
    "      return true;\n"
    "    }\n"
    "    if ((el = ancestor(target, 'data-undo'))) {\n"
    "      var id = Number(el.getAttribute('data-undo'));\n"
    "      pendingUndo[id] = 'busy'; delete undoError[id];\n"
    "      focusNext = 'r:' + id;\n"
    "      render(); send({ action: 'undo', row: id }); return true;\n"
    "    }\n"
    "    if (ancestor(target, 'data-undo-restore')) {\n"
    "      restoreBusy = 'undo'; delete restoreError.undo;\n"
    "      render(); send({ action: 'undo_restore' }); return true;\n"
    "    }\n"
    "    if ((el = ancestor(target, 'data-yes'))) {\n"
    "      var gid = Number(el.getAttribute('data-yes'));\n"
    "      asking = null; restoreBusy = gid; delete restoreError[gid];\n"
    "      focusNext = 'g:' + gid;\n"
    "      render(); send({ action: 'restore', group: gid }); return true;\n"
    "    }\n"
    "    if ((el = ancestor(target, 'data-no'))) { var nid = Number(el.getAttribute('data-no')); asking = null; focusNext = 'a:' + nid; render(); return true; }\n"
    "    if ((el = ancestor(target, 'data-dismiss'))) { var did = Number(el.getAttribute('data-dismiss')); delete restoreError[did]; focusNext = 'g:' + did; render(); return true; }\n"
    "    if ((el = ancestor(target, 'data-ask'))) { asking = Number(el.getAttribute('data-ask')); focusNext = 'n:' + asking; render(); return true; }\n"
    "    if (ancestor(target, 'data-settings')) { send({ action: 'open_settings' }); return true; }\n"
    "    if (ancestor(target, 'gear') || target.id === 'gear' || (target.closest && target.closest('#gear'))) {\n"
    "      if (caps.open_settings) send({ action: 'open_settings' });\n"
    "      return true;\n"
    "    }\n"
    "    if (target.closest && target.closest('#fold')) {\n"
    "      var cand = shownGroups().filter(function (g) { return !isUndone(g); });\n"
    "      var allOpen = cand.length > 0 && cand.every(function (g) { return openGroups[g.id]; });\n"
    "      cand.forEach(function (g) {\n"
    "        openGroups[g.id] = !allOpen;\n"
    "        if (allOpen) delete handOpened[g.id]; else handOpened[g.id] = true;\n"
    "      });\n"
    "      render(); return true;\n"
    "    }\n"
    "    if (target.closest && target.closest('.more')) return false;\n"
    "    if ((el = target.closest && target.closest('.row'))) { toggleRow(Number(el.getAttribute('data-row'))); return true; }\n"
    "    if (target.closest && target.closest('.ask')) return false;\n"
    "    if ((el = target.closest && target.closest('[data-toggle]'))) {\n"
    "      var tid = Number(el.getAttribute('data-toggle'));\n"
    "      if (asking === tid || restoreBusy === tid) return false;\n"
    "      openGroups[tid] = !openGroups[tid];\n"
    "      if (openGroups[tid]) handOpened[tid] = true; else delete handOpened[tid];\n"
    "      focusNext = 'g:' + tid;\n"
    "      render(); return true;\n"
    "    }\n"
    "    return false;\n"
    "  }\n"
    "  document.addEventListener('click', function (e) {\n"
    "    if (ancestor(e.target, 'data-undo')) e.stopPropagation();\n"
    "    activate(e.target);\n"
    "  });\n"
    "  // A click is not the keyboard: no focus tag, and an action's pending focus gives way to where the user went.\n"
    "  document.addEventListener('mousedown', function () { kbFocus = false; focusNext = null; focusTries = 0; hideTip(); });\n"
    "  document.addEventListener('focusin', function (e) { if (kbFocus) showTip(e.target); else hideTip(); });\n"
    "  document.addEventListener('focusout', function () { hideTip(); });\n"
    "  try {\n"
    "    var listEl = document.getElementById('list');\n"
    "    if (listEl && listEl.addEventListener) listEl.addEventListener('scroll', hideTip);\n"
    "    // Back from a dialog the host showed (a restore pumps one): the focus an action asked for.\n"
    "    if (window.addEventListener) window.addEventListener('focus', function () { if (focusNext && refocus(focusNext)) { focusNext = null; focusTries = 0; } });\n"
    "  } catch (e) {}\n"
    "  // Keyboard: Enter / Space on a row (role=button) opens it (buttons do that themselves); Esc answers No to the\n"
    "  // restore question, else closes the open row the focus is in; Ctrl+P / Ctrl+S never print or save the page.\n"
    "  document.addEventListener('keydown', function (e) {\n"
    "    var k = e.key;\n"
    "    if (k !== 'Shift' && k !== 'Control' && k !== 'Alt' && k !== 'Meta') kbFocus = true;\n"
    "    if ((e.ctrlKey || e.metaKey) && !e.altKey && (k === 'p' || k === 'P' || k === 's' || k === 'S')) {\n"
    "      if (e.preventDefault) e.preventDefault();\n"
    "      return;\n"
    "    }\n"
    "    var t = e.target || {};\n"
    "    if (k === 'Escape' || k === 'Esc') {\n"
    "      hideTip();\n"
    "      if (asking !== null) {\n"
    "        var gid = asking;\n"
    "        asking = null; focusNext = 'a:' + gid; render();\n"
    "        if (e.preventDefault) e.preventDefault();\n"
    "        return;\n"
    "      }\n"
    "      var rowEl = t.closest ? t.closest('.row') : null;\n"
    "      var rid = rowEl ? Number(rowEl.getAttribute('data-row')) : NaN;\n"
    "      if (isNaN(rid) && t.closest && t.closest('.more')) {\n"
    "        var u = t.getAttribute && (t.getAttribute('data-undo') || (t.getAttribute('data-fk') || '').replace(/^u:/, ''));\n"
    "        rid = Number(u);\n"
    "      }\n"
    "      if (!isNaN(rid) && openRows[rid]) {\n"
    "        openRows[rid] = false; focusNext = 'r:' + rid; render();\n"
    "        if (e.preventDefault) e.preventDefault();\n"
    "      }\n"
    "      return;\n"
    "    }\n"
    "    if ((k === 'Enter' || k === ' ' || k === 'Spacebar') && t.getAttribute && t.getAttribute('role') === 'button' && t.hasAttribute && t.hasAttribute('data-row')) {\n"
    "      if (e.preventDefault) e.preventDefault();\n"
    "      toggleRow(Number(t.getAttribute('data-row')));\n"
    "    }\n"
    "  });\n"
    "  // The host may miss the first ready (its message handler can register after the page's script ran): say it again\n"
    "  // every READY_RETRY_MS until the first history arrives, at most READY_RETRIES more times.\n"
    "  function retryReady() {\n"
    "    readyTimer = null;\n"
    "    if (gotHistory || readyTries >= READY_RETRIES) return;\n"
    "    readyTries++;\n"
    "    send({ action: 'ready' });\n"
    "    readyTimer = window.setTimeout(retryReady, READY_RETRY_MS);\n"
    "  }\n"
    "  render();\n"
    "  send({ action: 'ready' });\n"
    "  send({ action: 'live' });\n"
    "  if (!gotHistory) readyTimer = window.setTimeout(retryReady, READY_RETRY_MS);\n"
    "})();\n"
    "</script>\n"
    "</body>\n"
    "</html>\n"
)
# <<< generated PANEL_HTML
# >>> generated LABELS (tools/embed.py)
LABELS = {
    "accel_to_decel_enable": ("Enable accel_to_decel", ""),
    "accel_to_decel_factor": ("accel_to_decel", "%"),
    "activate_air_filtration": ("Activate air filtration", ""),
    "activate_chamber_temp_control": ("Activate temperature control", ""),
    "adaptive_bed_mesh_margin": ("Mesh margin", "mm"),
    "adaptive_infill_gradient": ("Adaptive gradient", ""),
    "adaptive_infill_interior_density": ("Interior density", "%"),
    "adaptive_infill_mode": ("Adaptive density (experimental)", ""),
    "adaptive_pressure_advance": ("Enable adaptive pressure advance (beta)", ""),
    "adaptive_pressure_advance_bridges": ("Static pressure advance for bridges", ""),
    "adaptive_pressure_advance_model": ("Adaptive pressure advance measurements (beta)", ""),
    "adaptive_pressure_advance_overhangs": ("Enable adaptive pressure advance within features (beta)", ""),
    "additional_cooling_fan_speed": ("Fan speed", "%"),
    "additional_fan_full_speed_layer": ("Full fan speed at layer", ""),
    "align_infill_direction_to_model": ("Align directions to model", ""),
    "align_to_y_axis": ("Align to Y axis when arranging", ""),
    "align_xy": ("Align XY", ""),
    "allow_mix_temp": ("Allow filaments with high/low temperature to be printed together", ""),
    "allow_multicolor_oneplate": ("Allow multiple colors on one plate", ""),
    "allow_newer_file": ("Allow 3MF with newer version to be sliced", ""),
    "allow_rotations": ("Allow rotation when arranging", ""),
    "alternate_extra_wall": ("Alternate extra wall", ""),
    "arrange": ("Arrange Options", ""),
    "assemble": ("Assemble", ""),
    "autosave": ("Autosave", ""),
    "auxiliary_fan": ("Auxiliary part cooling fan", ""),
    "avoid_extrusion_cali_region": ("Avoid extrusion calibrate region when arranging", ""),
    "bbl_calib_mark_logo": ("Show auto-calibration marks", ""),
    "bbl_use_printhost": ("Use 3rd-party print host", ""),
    "bed_custom_model": ("Bed custom model", ""),
    "bed_custom_texture": ("Bed custom texture", ""),
    "bed_exclude_area": ("Excluded bed area", ""),
    "bed_exclude_volume_mode": ("Collision volume behavior", ""),
    "bed_exclude_volumes": ("Toolhead collision volumes", ""),
    "bed_mesh_max": ("Bed mesh max", "mm"),
    "bed_mesh_min": ("Bed mesh min", "mm"),
    "bed_mesh_probe_distance": ("Probe point distance", "mm"),
    "bed_temperature_formula": ("Bed temperature type", ""),
    "before_layer_change_gcode": ("Before layer change G-code", ""),
    "belt_frame_tilt_angle": ("Machine-frame tilt angle", "°"),
    "belt_frame_tilt_decouple": ("Decouple machine-frame tilt", ""),
    "belt_printer": ("Enable belt printing", ""),
    "belt_printer_infinite_y": ("Infinite Y axis", ""),
    "belt_purge_tower_object": ("Belt purge tower object", ""),
    "belt_purge_tower_width": ("Belt purge tower width", "mm"),
    "belt_slice_rotation": ("Belt tilt axis", ""),
    "belt_slice_rotation_angle": ("Belt tilt angle", "°"),
    "belt_support_floor_offset": ("Support Floor Z offset", "mm"),
    "best_object_pos": ("Best object position", ""),
    "bottom_color_penetration_layers": ("Bottom paint penetration layers", "layers"),
    "bottom_layer_direction": ("Bottom layer direction", "°"),
    "bottom_shell_layers": ("Bottom shell layers", "layers"),
    "bottom_shell_thickness": ("Bottom shell thickness", "mm"),
    "bottom_solid_infill_flow_ratio": ("Bottom surface flow ratio", ""),
    "bottom_surface_density": ("Bottom surface density", "%"),
    "bottom_surface_filament_id": ("Bottom surface", ""),
    "bottom_surface_fill_order": ("Bottom surface fill order", ""),
    "bottom_surface_pattern": ("Bottom surface pattern", ""),
    "bridge_acceleration": ("Bridge", "mm/s² or %"),
    "bridge_angle": ("External bridge infill direction", "°"),
    "bridge_density": ("External bridge density", "%"),
    "bridge_flow": ("Bridge flow ratio", ""),
    "bridge_line_width": ("Bridge", "mm or %"),
    "bridge_no_support": ("Don't support bridges", ""),
    "bridge_speed": ("External", "mm/s"),
    "brim_ears": ("Brim ears", ""),
    "brim_ears_detection_length": ("Brim ear detection radius", "mm"),
    "brim_ears_max_angle": ("Brim ear max angle", "°"),
    "brim_ears_outer_only": ("Brim ears outer only", ""),
    "brim_flow_ratio": ("Brim flow ratio", ""),
    "brim_object_gap": ("Brim-object gap", "mm"),
    "brim_type": ("Brim type", ""),
    "brim_use_efc_outline": ("Brim follows compensated outline", ""),
    "brim_width": ("Brim width", "mm"),
    "build_plate_tilt_x": ("Build plate tilt X", "°"),
    "build_plate_tilt_y": ("Build plate tilt Y", "°"),
    "center": ("Center", ""),
    "center_of_surface_pattern": ("Center surface pattern on", ""),
    "chamber_minimal_temperature": ("Chamber minimal temperature", "℃"),
    "chamber_temperature": ("Chamber temperature", "℃"),
    "change_extrusion_role_gcode": ("Change extrusion role G-code", ""),
    "change_filament_gcode": ("Change filament G-code", ""),
    "clone_objects": ("Clone Objects", ""),
    "close_additional_fan_first_x_layers": ("For the first", "layers"),
    "close_fan_the_first_x_layers": ("No cooling for the first", "layers"),
    "combine_brims": ("Combine brims", ""),
    "compatible_printers": ("Select printers", ""),
    "compatible_printers_condition": ("Condition", ""),
    "compatible_prints": ("Select profiles", ""),
    "compatible_prints_condition": ("Condition", ""),
    "complete_print_exhaust_fan_speed": ("Fan speed", "%"),
    "config_compatibility": ("Forward-compatibility rule when loading configurations from config files and project files (3MF, AMF).", ""),
    "convert_unit": ("Convert Unit", ""),
    "cool_plate_temp": ("Bed temperature", "℃"),
    "cool_plate_temp_initial_layer": ("First layer bed temperature", "℃"),
    "cooling_filter_enabled": ("Use cooling filter", ""),
    "cooling_tube_length": ("Cooling tube length", "mm"),
    "cooling_tube_retraction": ("Cooling tube position", "mm"),
    "copy": ("Copy", ""),
    "counterbore_hole_bridging": ("Bridge counterbore holes", ""),
    "curr_bed_type": ("Bed type", ""),
    "current_extruder": ("Current extruder", ""),
    "current_object_idx": ("Current object index", ""),
    "cut": ("Cut", ""),
    "cut_grid": ("Cut", ""),
    "cut_x": ("Cut", ""),
    "cut_y": ("Cut", ""),
    "datadir": ("Data directory", ""),
    "day": ("Day", ""),
    "debug": ("Debug level", ""),
    "default_acceleration": ("Normal printing", "mm/s²"),
    "default_bed_type": ("Default bed type", ""),
    "default_filament_colour": ("Default color", ""),
    "default_filament_profile": ("Default filament profile", ""),
    "default_jerk": ("Default", "mm/s"),
    "default_junction_deviation": ("Junction Deviation", "mm"),
    "default_nozzle_volume_type": ("Default Nozzle Volume Type.", ""),
    "default_print_profile": ("Default process profile", ""),
    "deretract_speed_extruder_change": ("Deretraction speed (extruder change)", "mm/s"),
    "deretraction_speed": ("Deretraction speed", "mm/s"),
    "detect_narrow_internal_solid_infill": ("Detect narrow internal solid infills", ""),
    "detect_overhang_wall": ("Detect overhang walls", ""),
    "detect_thin_wall": ("Detect thin walls", ""),
    "disable_m73": ("Disable set remaining print time", ""),
    "display_pixels_x": ("XAxis", ""),
    "display_pixels_y": ("YAxis", ""),
    "dont_filter_internal_bridges": ("Filter out small internal bridges", ""),
    "dont_slow_down_outer_wall": ("Don't slow down outer walls", ""),
    "downward_check": ("Downward machines check", ""),
    "downward_settings": ("Downward machines settings", ""),
    "draft_shield": ("Draft shield", ""),
    "duplicate_grid": ("Duplicate by grid", ""),
    "during_print_exhaust_fan_speed": ("Fan speed", "%"),
    "e_position": ("Absolute E position", ""),
    "e_restart_extra": ("Extra de-retraction", ""),
    "e_retracted": ("Retraction", ""),
    "elefant_foot_compensation": ("Elephant foot compensation", "mm"),
    "elefant_foot_compensation_layers": ("Elephant foot compensation layers", "layers"),
    "elefant_foot_layers_density": ("Elephant foot layers density", "%"),
    "emit_machine_limits_to_gcode": ("Emit limits to G-code", ""),
    "enable_arc_fitting": ("Arc fitting", ""),
    "enable_belt_purge_tower": ("Enable belt purge tower", ""),
    "enable_extra_bridge_layer": ("Extra bridge layers (beta)", ""),
    "enable_filament_dynamic_map": ("Enable filament dynamic map", ""),
    "enable_filament_ramming": ("Enable filament ramming", ""),
    "enable_mixed_color_sublayer": ("Mixed color sublayer", ""),
    "enable_overhang_bridge_fan": ("Force cooling for overhangs and bridges", ""),
    "enable_overhang_speed": ("Slow down for overhangs", ""),
    "enable_power_loss_recovery": ("Power Loss Recovery", ""),
    "enable_pressure_advance": ("Enable pressure advance", ""),
    "enable_prime_tower": ("Enable", ""),
    "enable_support": ("Enable support", ""),
    "enable_timelapse": ("Enable timelapse for print", ""),
    "enable_tower_interface_cooldown_during_tower": ("Cool down from interface boost during prime tower", ""),
    "enable_tower_interface_features": ("Enable tower interface features", ""),
    "enable_wrapping_detection": ("Enable clumping detection", ""),
    "eng_plate_temp": ("Bed temperature", "℃"),
    "eng_plate_temp_initial_layer": ("First layer bed temperature", "℃"),
    "ensure_on_bed": ("Ensure on bed", ""),
    "ensure_vertical_shell_thickness": ("Ensure vertical shell thickness", ""),
    "exclude_object": ("Exclude objects", ""),
    "exclude_object_tolerance": ("Outline tolerance", "mm"),
    "export_3mf": ("Export 3MF", ""),
    "export_amf": ("Export AMF", ""),
    "export_gcode": ("Export G-code", ""),
    "export_obj": ("Export OBJ", ""),
    "export_settings": ("Export Settings", ""),
    "export_sla": ("Export SLA", ""),
    "export_slicedata": ("Export slicing data", ""),
    "export_statistics": ("Export statistics", ""),
    "export_stl": ("Export STL", ""),
    "export_stls": ("Export multiple STLs", ""),
    "export_svg": ("Export SVG", ""),
    "extra_brim_width": ("Extra brim width", "mm"),
    "extra_loading_move": ("Extra loading distance", "mm"),
    "extra_perimeters_on_overhangs": ("Extra perimeters on overhangs", ""),
    "extra_solid_infills": ("Insert solid layers", ""),
    "extruded_volume": ("Volume per extruder", ""),
    "extruded_volume_total": ("Total volume", ""),
    "extruded_weight": ("Weight per extruder", ""),
    "extruded_weight_total": ("Total weight", ""),
    "extruder": ("Extruder", ""),
    "extruder_ams_count": ("Extruder AMS count", ""),
    "extruder_bed_exclude_volumes": ("Extruder collision volumes", ""),
    "extruder_clearance_dist_to_rod": ("Distance to rod", "mm"),
    "extruder_clearance_height_to_lid": ("Height to lid", "mm"),
    "extruder_clearance_height_to_rod": ("Height to rod", "mm"),
    "extruder_clearance_radius": ("Radius", "mm"),
    "extruder_colour": ("Extruder Color", ""),
    "extruder_nozzle_count": ("extruder nozzle count", ""),
    "extruder_nozzle_stats": ("Extruder nozzle stats", ""),
    "extruder_nozzle_volume_type": ("Extruder nozzle volume type", ""),
    "extruder_offset": ("Extruder offset", "mm"),
    "extruder_printable_area": ("Extruder printable area", ""),
    "extruder_printable_height": ("Extruder printable height", "mm"),
    "extruder_type": ("Type", ""),
    "extruder_variant_list": ("Extruder variant list", ""),
    "extrusion_rate_smoothing_external_perimeter_only": ("Apply only on external features", ""),
    "extrusion_role": ("Extrusion role", ""),
    "fan_cooling_layer_time": ("Layer time", "ssecond"),
    "fan_direction": ("Fan direction", ""),
    "fan_kickstart": ("Fan kick-start time", "ssecond"),
    "fan_max_speed": ("Fan speed", "%"),
    "fan_min_speed": ("Fan speed", "%"),
    "fan_speedup_overhangs": ("Only overhangs", ""),
    "farthest_point_timelapse": ("Farthest point timelapse", ""),
    "filament_adaptive_volumetric_speed": ("Adaptive volumetric speed", ""),
    "filament_adhesiveness_category": ("Adhesiveness Category", ""),
    "filament_change_extrusion_role_gcode": ("Change extrusion role G-code (filament)", ""),
    "filament_change_length": ("Filament ramming length", "mm"),
    "filament_change_length_nc": ("Hotend change", "mm"),
    "filament_colour": ("Color", ""),
    "filament_cooling_before_tower": ("Wipe tower cooling", "℃"),
    "filament_cooling_final_speed": ("Speed of the last cooling move", "mm/s"),
    "filament_cooling_initial_speed": ("Speed of the first cooling move", "mm/s"),
    "filament_cooling_moves": ("Number of cooling moves", ""),
    "filament_cost": ("Price", "money/kg"),
    "filament_density": ("Density", "g/cm³"),
    "filament_diameter": ("Diameter", "mm"),
    "filament_end_gcode": ("End G-code", ""),
    "filament_extruder_compatibility": ("Filament-extruder compatibility", ""),
    "filament_extruder_id": ("Filament extruder id", ""),
    "filament_extruder_variant": ("Filament's extruder variant", ""),
    "filament_flow_ratio": ("Flow ratio", ""),
    "filament_flush_temp": ("Flush temperature", "℃"),
    "filament_flush_temp_fast": ("Flush temperature", "℃"),
    "filament_flush_volumetric_speed": ("Flush volumetric speed", "mm³/s"),
    "filament_ironing_flow": ("Ironing flow", "%"),
    "filament_ironing_inset": ("Ironing inset", "mm"),
    "filament_ironing_spacing": ("Ironing line spacing", "mm"),
    "filament_ironing_speed": ("Ironing speed", "mm/s"),
    "filament_is_mixed": ("Is mixed filament", ""),
    "filament_is_support": ("Support material", ""),
    "filament_loading_speed": ("Loading speed", "mm/s"),
    "filament_loading_speed_start": ("Loading speed at the start", "mm/s"),
    "filament_map": ("Filament map to extruder", ""),
    "filament_map_2": ("Filament map plus for multi nozzle", ""),
    "filament_map_mode": ("filament mapping mode", ""),
    "filament_max_volumetric_speed": ("Max volumetric speed", "mm³/s"),
    "filament_minimal_purge_on_wipe_tower": ("Minimal purge on wipe tower", "mm³"),
    "filament_mixed_components": ("Mixed filament components", ""),
    "filament_mixed_gradient": ("Mixed filament gradient", ""),
    "filament_mixed_gradient_curve": ("Mixed filament gradient curve", ""),
    "filament_mixed_gradient_per_part": ("Mixed filament per-part gradient", ""),
    "filament_mixed_gradient_range": ("Mixed filament gradient range", ""),
    "filament_mixed_sublayer_ratios": ("Mixed filament sublayer ratios", ""),
    "filament_multitool_ramming": ("Enable ramming for multi-tool setups", ""),
    "filament_multitool_ramming_flow": ("Multi-tool ramming flow", "mm³/s"),
    "filament_multitool_ramming_volume": ("Multi-tool ramming volume", "mm³"),
    "filament_notes": ("Filament notes", ""),
    "filament_pre_cooling_temperature": ("Extruder change", "℃"),
    "filament_pre_cooling_temperature_nc": ("Hotend change", "℃"),
    "filament_preheat_temperature_delta": ("Preheat temperature delta", "℃"),
    "filament_preset": ("Filament preset name", ""),
    "filament_prime_volume": ("Filament change", "mm³"),
    "filament_prime_volume_nc": ("Hotend change", "mm³"),
    "filament_printable": ("Filament printable", ""),
    "filament_ramming_parameters": ("Ramming parameters", ""),
    "filament_ramming_travel_time": ("Extruder change", "s"),
    "filament_ramming_travel_time_nc": ("Hotend change", "s"),
    "filament_ramming_volumetric_speed": ("Extruder change", "mm³/s"),
    "filament_ramming_volumetric_speed_nc": ("Hotend change", "mm³/s"),
    "filament_retract_length_nc": ("length when change hotend", "mm"),
    "filament_self_index": ("Filament self index", ""),
    "filament_shrink": ("Shrinkage (XY)", "%"),
    "filament_shrinkage_compensation_z": ("Shrinkage (Z)", "%"),
    "filament_soluble": ("Soluble material", ""),
    "filament_stamping_distance": ("Stamping distance measured from the center of the cooling tube", ""),
    "filament_stamping_loading_speed": ("Stamping loading speed", ""),
    "filament_start_gcode": ("Start G-code", ""),
    "filament_toolchange_delay": ("Delay after unloading", "ssecond"),
    "filament_tower_interface_pre_extrusion_dist": ("Interface layer pre-extrusion distance", "mm"),
    "filament_tower_interface_pre_extrusion_length": ("Interface layer pre-extrusion length", "mm"),
    "filament_tower_interface_print_temp": ("Interface layer print temperature", "℃"),
    "filament_tower_interface_purge_volume": ("Interface layer purge length", "mm"),
    "filament_tower_ironing_area": ("Tower ironing area", "mm²"),
    "filament_type": ("Type", ""),
    "filament_unloading_speed": ("Unloading speed", "mm/s"),
    "filament_unloading_speed_start": ("Unloading speed at the start", "mm/s"),
    "filament_vendor": ("Vendor", ""),
    "file_start_gcode": ("File header G-code", ""),
    "filename_format": ("Filename format", ""),
    "fill_multiline": ("Fill Multiline", ""),
    "filter_out_gap_fill": ("Filter out tiny gaps", "mm"),
    "first_layer_flow_ratio": ("First layer flow ratio", ""),
    "first_layer_print_convex_hull": ("First layer convex hull", ""),
    "first_layer_print_max": ("Top-right corner of the first layer bounding box", ""),
    "first_layer_print_min": ("Bottom-left corner of the first layer bounding box", ""),
    "first_layer_print_sequence": ("First layer print sequence", ""),
    "first_layer_print_size": ("Size of the first layer bounding box", ""),
    "first_layer_sequence_choice": ("First layer filament sequence", ""),
    "first_x_layer_fan_speed": ("Fan speed", "%"),
    "flashforge_serial_number": ("Serial Number", ""),
    "flush_into_infill": ("Flush into objects' infill", ""),
    "flush_into_objects": ("Flush into this object", ""),
    "flush_into_support": ("Flush into objects' support", ""),
    "flush_multiplier": ("Flush multiplier", ""),
    "flush_multiplier_fast": ("Flush multiplier (Fast mode)", ""),
    "flush_volumes_matrix": ("Purging volumes", ""),
    "flush_volumes_vector": ("Purging volumes - load/unload volumes", ""),
    "full_fan_speed_layer": ("Full fan speed at layer", "layer"),
    "fuzzy_skin": ("Fuzzy skin", ""),
    "fuzzy_skin_first_layer": ("Apply fuzzy skin to first layer", ""),
    "fuzzy_skin_layers_between_ripple_offset": ("Layers between ripple offset", ""),
    "fuzzy_skin_mode": ("Fuzzy skin generator mode", ""),
    "fuzzy_skin_noise_type": ("Fuzzy skin noise type", ""),
    "fuzzy_skin_octaves": ("Fuzzy Skin Noise Octaves", ""),
    "fuzzy_skin_persistence": ("Fuzzy skin noise persistence", ""),
    "fuzzy_skin_point_distance": ("Fuzzy skin point distance", "mm"),
    "fuzzy_skin_ripple_offset": ("Ripple offset", "%"),
    "fuzzy_skin_ripples_per_layer": ("Number of ripples per layer", ""),
    "fuzzy_skin_scale": ("Fuzzy skin feature size", "mm"),
    "fuzzy_skin_thickness": ("Fuzzy skin thickness", "mm"),
    "gap_fill_flow_ratio": ("Gap fill flow ratio", ""),
    "gap_fill_target": ("Apply gap fill", ""),
    "gap_infill_speed": ("Gap infill", "mm/s"),
    "gcode_add_line_number": ("Add line number", ""),
    "gcode_comments": ("Verbose G-code", ""),
    "gcode_flavor": ("G-code flavor", ""),
    "gcode_label_objects": ("Label objects", ""),
    "gcode_skip_config_block": ("Skip G-code config block", ""),
    "gcodeviewer": ("G-code viewer", ""),
    "grab_length": ("Grab length", "mm"),
    "ground_face_normal": ("Ground face by normal", ""),
    "ground_face_point": ("Ground face at point", ""),
    "ground_largest_face": ("Ground largest face", ""),
    "gyroid_optimized": ("Z-buckling bias optimization (experimental)", ""),
    "has_filament_switcher": ("Has filament switcher", ""),
    "has_single_extruder_multi_material_priming": ("Has single extruder MM priming", ""),
    "has_wipe_tower": ("Has wipe tower", ""),
    "head_wrap_detect_zone": ("Head wrap detect zone", ""),
    "help": ("Help", ""),
    "help_fff": ("Help (FFF options)", ""),
    "help_sla": ("Help (SLA options)", ""),
    "high_current_on_filament_swap": ("High extruder current on filament swap", ""),
    "high_precision_gcode": ("High precision G-code (experimental)", ""),
    "hole_to_polyhole": ("Convert holes to polyholes", ""),
    "hole_to_polyhole_max_edges": ("Maximum Polyhole edge count", ""),
    "hole_to_polyhole_threshold": ("Polyhole detection margin", "mm or %"),
    "hole_to_polyhole_twisted": ("Polyhole twist", ""),
    "host_type": ("Host Type", ""),
    "hot_plate_temp": ("Bed temperature", "℃"),
    "hot_plate_temp_initial_layer": ("First layer bed temperature", "℃"),
    "hour": ("Hour", ""),
    "idle_temperature": ("Idle temperature", "℃"),
    "ignore_nonexistent_config": ("Ignore non-existent config files", ""),
    "imex_carriage_margin": ("Safety Margin", "mm"),
    "imex_firmware_managed_zones": ("Firmware-managed zones", ""),
    "imex_gantry_count": ("Gantry Count", ""),
    "imex_head_filament_map": ("IDEX/IQEX head filament map", ""),
    "imex_mode_active_tools": ("IDEX/IQEX Mode Active Tools", ""),
    "imex_mode_gcodes": ("IDEX/IQEX Mode G-codes", ""),
    "imex_mode_names": ("IDEX/IQEX Mode Names", ""),
    "imex_nozzle_clearance_x": ("Nozzle Clearance X", "mm"),
    "imex_nozzle_clearance_y": ("Nozzle Clearance Y", "mm"),
    "imex_parallel_mode": ("IDEX/IQEX Print Mode", ""),
    "imex_tool_layout": ("Tool 0 Position", ""),
    "imex_tools_per_gantry": ("Tools per Gantry", ""),
    "imex_viz_theme": ("Visualization Theme", ""),
    "independent_support_layer_height": ("Independent support layer height", ""),
    "infill_anchor": ("Sparse infill anchor length", "mm or %"),
    "infill_anchor_max": ("Maximum length of the infill anchor", ""),
    "infill_combination": ("Infill combination", ""),
    "infill_combination_max_layer_height": ("Infill combination - Max layer height", "mm or %"),
    "infill_complete_top": ("Fill pattern tops", ""),
    "infill_direction": ("Sparse infill direction", "°"),
    "infill_jerk": ("Infill", "mm/s"),
    "infill_lock_depth": ("Infill lock depth", "mm"),
    "infill_overhang_angle": ("Infill overhang angle", "°"),
    "infill_shift_step": ("Infill shift step", "mm"),
    "infill_wall_overlap": ("Infill/wall overlap", "%"),
    "info": ("Output Model Info", ""),
    "inherits": ("Inherits profile", ""),
    "initial_extruder": ("Initial extruder", ""),
    "initial_layer_acceleration": ("First layer", "mm/s²"),
    "initial_layer_fan_speed": ("First layer fan speed", "%"),
    "initial_layer_infill_speed": ("First layer infill", "mm/s"),
    "initial_layer_jerk": ("First layer", "mm/s"),
    "initial_layer_line_width": ("First layer", "mm or %"),
    "initial_layer_min_bead_width": ("First layer minimum wall width", "%"),
    "initial_layer_print_height": ("First layer height", "mm"),
    "initial_layer_speed": ("First layer", "mm/s"),
    "initial_layer_travel_acceleration": ("First layer travel", "mm/s² or %"),
    "initial_layer_travel_jerk": ("First layer travel", "mm/s or %"),
    "initial_layer_travel_speed": ("First layer travel speed", "mm/s or %"),
    "initial_tool": ("Initial tool", ""),
    "inner_wall_acceleration": ("Inner wall", "mm/s²"),
    "inner_wall_filament_id": ("Inner walls", ""),
    "inner_wall_flow_ratio": ("Inner wall flow ratio", ""),
    "inner_wall_jerk": ("Inner wall", "mm/s"),
    "inner_wall_line_width": ("Inner wall", "mm or %"),
    "inner_wall_speed": ("Inner wall", "mm/s"),
    "input_filename_base": ("Input filename without extension", ""),
    "input_shaping_damp_x": ("XAxis", ""),
    "input_shaping_damp_y": ("YAxis", ""),
    "input_shaping_emit": ("Emit input shaping", ""),
    "input_shaping_freq_x": ("XAxis", "Hz"),
    "input_shaping_freq_y": ("YAxis", "Hz"),
    "input_shaping_type": ("Input shaper type", ""),
    "inspect_mesh": ("Inspect mesh (JSON to stdout)", ""),
    "inspect_paint": ("Inspect paint (JSON to stdout)", ""),
    "interface_shells": ("Interface shells", ""),
    "interlocking_beam": ("Use beam interlocking", ""),
    "interlocking_beam_layer_count": ("Interlocking beam layers", ""),
    "interlocking_beam_width": ("Interlocking beam width", "mm"),
    "interlocking_boundary_avoidance": ("Interlocking boundary avoidance", ""),
    "interlocking_depth": ("Interlocking depth", ""),
    "interlocking_orientation": ("Interlocking direction", "°"),
    "internal_bridge_angle": ("Internal bridge infill direction", "°"),
    "internal_bridge_density": ("Internal bridge density", "%"),
    "internal_bridge_fan_speed": ("Internal bridges fan speed", "%"),
    "internal_bridge_flow": ("Internal bridge flow ratio", ""),
    "internal_bridge_speed": ("Internal", "mm/s or %"),
    "internal_solid_filament_id": ("Internal solid infill", ""),
    "internal_solid_infill_acceleration": ("Internal solid infill", "mm/s² or %"),
    "internal_solid_infill_flow_ratio": ("Internal solid infill flow ratio", ""),
    "internal_solid_infill_line_width": ("Internal solid infill", "mm or %"),
    "internal_solid_infill_pattern": ("Internal solid infill pattern", ""),
    "internal_solid_infill_speed": ("Internal solid infill", "mm/s"),
    "ironing_angle": ("Ironing angle offset", "°"),
    "ironing_angle_fixed": ("Fixed ironing angle", ""),
    "ironing_expansion": ("Ironing expansion", "mm"),
    "ironing_fan_speed": ("Ironing fan speed", "%"),
    "ironing_flow": ("Ironing flow", "%"),
    "ironing_inset": ("Ironing inset", "mm"),
    "ironing_pattern": ("Ironing Pattern", ""),
    "ironing_spacing": ("Ironing line spacing", "mm"),
    "ironing_speed": ("Ironing speed", "mm/s"),
    "ironing_type": ("Ironing type", ""),
    "is_extruder_used": ("Is extruder used?", ""),
    "is_imex": ("IDEX/IQEX Printer", ""),
    "is_infill_first": ("Print infill first", ""),
    "last_extrusion_role": ("Last extrusion role", ""),
    "lateral_lattice_angle_1": ("Lateral lattice angle 1", "°"),
    "lateral_lattice_angle_2": ("Lateral lattice angle 2", "°"),
    "layer_change_gcode": ("Layer change G-code", ""),
    "layer_height": ("Layer height", "mm"),
    "layer_num": ("Layer number", ""),
    "layer_z": ("Layer Z", ""),
    "leading_brim_length": ("Leading brim length", "mm"),
    "lightning_overhang_angle": ("Lightning overhang angle", "°"),
    "lightning_prune_angle": ("Prune angle", "°"),
    "lightning_straightening_angle": ("Straightening angle", "°"),
    "line_width": ("Default", "mm or %"),
    "load": ("Load config file", ""),
    "load_assemble_list": ("Load assemble list", ""),
    "load_custom_gcodes": ("Load custom G-code", ""),
    "load_defaultfila": ("Load default filaments", ""),
    "load_filament_ids": ("Load filament IDs", ""),
    "load_filaments": ("Load Filament Settings", ""),
    "load_settings": ("Load General Settings", ""),
    "load_slicedata": ("Load slicing data", ""),
    "logfile": ("Log file", ""),
    "long_retractions_when_cut": ("Long retraction when cut (beta)", ""),
    "long_retractions_when_ec": ("Long retraction when extruder change", ""),
    "machine_end_gcode": ("End G-code", ""),
    "machine_hotend_change_time": ("Hotend change time", "ssecond"),
    "machine_load_filament_time": ("Filament load time", "ssecond"),
    "machine_pause_gcode": ("Pause G-code", ""),
    "machine_start_gcode": ("Start G-code", ""),
    "machine_tool_change_time": ("Tool change time", "ssecond"),
    "machine_unload_filament_time": ("Filament unload time", "ssecond"),
    "make_overhang_printable": ("Make overhangs printable", ""),
    "make_overhang_printable_angle": ("Make overhangs printable - Maximum angle", "°"),
    "make_overhang_printable_hole_size": ("Make overhangs printable - Hole area", "mm²"),
    "makerlab_name": ("MakerLab name", ""),
    "makerlab_version": ("MakerLab version", ""),
    "manual_filament_change": ("Manual Filament Change", ""),
    "master_extruder_id": ("Master extruder id", ""),
    "max_bridge_length": ("Max bridge length", "mm"),
    "max_layer_height": ("Max", "mm"),
    "max_layer_z": ("Maximal layer Z", ""),
    "max_resonance_avoidance_speed": ("Max", "mm/s"),
    "max_travel_detour_distance": ("Avoid crossing walls - Max detour length", "mm or %"),
    "max_volumetric_extrusion_rate_slope": ("Extrusion rate smoothing", "mm³/s²"),
    "max_volumetric_extrusion_rate_slope_segment_length": ("Smoothing segment length", "mm"),
    "metadata_name": ("Metadata name list", ""),
    "metadata_value": ("Metadata value list", ""),
    "min_bead_width": ("Minimum wall width", "%"),
    "min_feature_size": ("Minimum feature size", "%"),
    "min_layer_height": ("Min", "mm"),
    "min_length_factor": ("Minimum wall length", ""),
    "min_resonance_avoidance_speed": ("Min", "mm/s"),
    "min_save": ("Minimum save", ""),
    "min_skirt_length": ("Skirt minimum extrusion length", "mm"),
    "min_width_top_surface": ("One wall threshold", "mm or %"),
    "minimum_sparse_infill_area": ("Minimum sparse infill threshold", "mm²"),
    "minute": ("Minute", ""),
    "mmu_segmented_region_interlocking_depth": ("Interlocking depth of a segmented region", "mm"),
    "mmu_segmented_region_max_width": ("Maximum width of a segmented region", "mm"),
    "month": ("Month", ""),
    "mstpp": ("mstpp", ""),
    "mtcpp": ("mtcpp", ""),
    "no_check": ("No check", ""),
    "normal_print_time": ("Print time (normal mode)", ""),
    "normative_check": ("Normative check", ""),
    "notes": ("Configuration notes", ""),
    "nozzle_diameter": ("Nozzle diameter", "mm"),
    "nozzle_height": ("Nozzle height", "mm"),
    "nozzle_hrc": ("Nozzle HRC", "HRC"),
    "nozzle_temperature": ("Nozzle temperature", "℃"),
    "nozzle_temperature_initial_layer": ("First layer nozzle temperature", "℃"),
    "nozzle_temperature_range_high": ("Max", "℃"),
    "nozzle_temperature_range_low": ("Min", "℃"),
    "nozzle_type": ("Nozzle type", ""),
    "nozzle_volume": ("Nozzle volume", "mm³"),
    "nozzle_volume_type": ("Nozzle Volume Type", ""),
    "num_extruders": ("Number of extruders", ""),
    "num_instances": ("Number of instances", ""),
    "num_objects": ("Number of objects", ""),
    "only_one_wall_first_layer": ("Only one wall on first layer", ""),
    "only_one_wall_top": ("Only one wall on top surfaces", ""),
    "ooze_prevention": ("Enable", ""),
    "orient": ("Orient Options", ""),
    "other_layers_print_sequence": ("Other layers print sequence", ""),
    "other_layers_print_sequence_nums": ("The number of other layers print sequence", ""),
    "other_layers_sequence_choice": ("Other layers filament sequence", ""),
    "outer_wall_acceleration": ("Outer wall", "mm/s²"),
    "outer_wall_filament_id": ("Outer walls", ""),
    "outer_wall_flow_ratio": ("Outer wall flow ratio", ""),
    "outer_wall_jerk": ("Outer wall", "mm/s"),
    "outer_wall_line_width": ("Outer wall", "mm or %"),
    "outer_wall_speed": ("Outer wall", "mm/s"),
    "output": ("Output File", ""),
    "outputdir": ("Output directory", ""),
    "overhang_1_4_speed": ("10%", "mm/s or %"),
    "overhang_2_4_speed": ("25%", "mm/s or %"),
    "overhang_3_4_speed": ("50%", "mm/s or %"),
    "overhang_4_4_speed": ("75%", "mm/s or %"),
    "overhang_fan_speed": ("Overhangs and external bridges fan speed", "%"),
    "overhang_fan_threshold": ("Overhang cooling activation threshold", ""),
    "overhang_flow_ratio": ("Overhang flow ratio", ""),
    "overhang_reverse": ("Overhang reversal", ""),
    "overhang_reverse_internal_only": ("Reverse only internal perimeters", ""),
    "overhang_reverse_threshold": ("Overhang reversal threshold", "mm or %"),
    "parallel_printheads_bed_exclude_areas": ("Parallel printheads bed exclude areas", ""),
    "parallel_printheads_count": ("Parallel printheads count", ""),
    "parking_pos_retraction": ("Filament parking position", "mm"),
    "part_cooling_fan_min_pwm": ("Minimum non-zero part cooling fan speed", "%"),
    "pellet_flow_coefficient": ("Pellet flow coefficient", ""),
    "pellet_modded_printer": ("Pellet Modded Printer", ""),
    "physical_extruder_map": ("Map the logical extruder to physical extruder", ""),
    "physical_printer_preset": ("Physical printer name", ""),
    "pipe": ("Send progress to pipe", ""),
    "plugins": ("Plugins Used", ""),
    "position": ("Position", ""),
    "post_process": ("Post-processing Scripts", ""),
    "precise_outer_wall": ("Precise wall", ""),
    "precise_z_height": ("Precise Z height", ""),
    "preferred_orientation": ("Preferred orientation", "°"),
    "preheat_steps": ("Preheat steps", ""),
    "preheat_time": ("Preheat time", "ssecond"),
    "preset_names": ("Printer preset names", ""),
    "pressure_advance": ("Pressure advance", ""),
    "prime_tower_brim_width": ("Brim width", "mm"),
    "prime_tower_enable_framework": ("Internal ribs", ""),
    "prime_tower_infill_gap": ("Infill gap", "%"),
    "prime_tower_skip_points": ("Skip points", ""),
    "prime_tower_width": ("Width", "mm"),
    "prime_volume": ("Prime volume", "mm³"),
    "prime_volume_mode": ("Prime volume mode", ""),
    "print_bed_max": ("Top-right corner of print bed bounding box", ""),
    "print_bed_min": ("Bottom-left corner of print bed bounding box", ""),
    "print_bed_size": ("Size of the print bed bounding box", ""),
    "print_extruder_id": ("Print extruder id", ""),
    "print_extruder_variant": ("Print's extruder variant", ""),
    "print_flow_ratio": ("Flow ratio", ""),
    "print_host": ("Hostname, IP or URL", ""),
    "print_host_webui": ("Device UI", ""),
    "print_order": ("Intra-layer order", ""),
    "print_preset": ("Print preset name", ""),
    "print_sequence": ("Print sequence", ""),
    "print_time": ("Print time (normal mode)", ""),
    "print_time_day": ("Print time (days component)", ""),
    "print_time_hour": ("Print time (hours component)", ""),
    "print_time_minute": ("Print time (minutes component)", ""),
    "print_time_sec": ("Print time (seconds component)", ""),
    "print_time_total_sec": ("Print time (total seconds)", ""),
    "printable_area": ("Printable area", ""),
    "printable_height": ("Printable height", "mm"),
    "printer_agent": ("Printer Agent", ""),
    "printer_extruder_id": ("Printer extruder id", ""),
    "printer_extruder_variant": ("Printer's extruder variant", ""),
    "printer_model": ("Printer type", ""),
    "printer_notes": ("Printer notes", ""),
    "printer_preset": ("Printer preset name", ""),
    "printer_structure": ("Printer structure", ""),
    "printer_technology": ("Printer technology", ""),
    "printer_variant": ("Printer variant", ""),
    "printhost_apikey": ("API Key / Password", ""),
    "printhost_authorization_type": ("Authorization Type", ""),
    "printhost_cafile": ("HTTPS CA File", ""),
    "printhost_password": ("Password", ""),
    "printhost_port": ("Printer", ""),
    "printhost_ssl_ignore_revoke": ("Ignore HTTPS certificate revocation checks", ""),
    "printhost_user": ("User", ""),
    "printing_by_object_gcode": ("Between Object G-code", ""),
    "process_change_extrusion_role_gcode": ("Change extrusion role G-code (process)", ""),
    "purge_in_prime_tower": ("Purge in prime tower", ""),
    "raft_contact_distance": ("Raft contact Z distance", "mm"),
    "raft_expansion": ("Raft expansion", "mm"),
    "raft_first_layer_density": ("First layer density", "%"),
    "raft_first_layer_expansion": ("First layer expansion", "mm"),
    "raft_layers": ("Raft layers", "layers"),
    "reduce_crossing_wall": ("Avoid crossing walls", ""),
    "reduce_fan_stop_start_freq": ("Keep fan always on", ""),
    "reduce_infill_retraction": ("Reduce infill retraction", ""),
    "relative_bridge_angle": ("Relative bridge angle", ""),
    "repair": ("Repair", ""),
    "repetitions": ("Repetition count", ""),
    "required_nozzle_HRC": ("Required nozzle HRC", ""),
    "resolution": ("Resolution", "mm"),
    "resonance_avoidance": ("Resonance avoidance", ""),
    "retract_after_wipe": ("Retract amount after wipe", "%"),
    "retract_before_wipe": ("Retract amount before wipe", "%"),
    "retract_length_toolchange": ("Retraction Length (Toolchange)", "mm"),
    "retract_lift_above": ("Z-hop lower boundary", "mm"),
    "retract_lift_below": ("Z-hop upper boundary", "mm"),
    "retract_lift_enforce": ("On surfaces", ""),
    "retract_restart_extra": ("Extra length on restart", "mm"),
    "retract_restart_extra_toolchange": ("Extra length on restart (Toolchange)", "mm"),
    "retract_when_changing_layer": ("Retract on layer change", ""),
    "retraction_distances_when_cut": ("Retraction distance when cut", ""),
    "retraction_distances_when_ec": ("Retraction distance when extruder change", "mm"),
    "retraction_length": ("Retraction Length", "mm"),
    "retraction_minimum_travel": ("Travel distance threshold", "mm"),
    "retraction_speed": ("Retraction speed", "mm/s"),
    "role_based_wipe_speed": ("Role based wipe speed", ""),
    "rotate": ("Rotate", "°"),
    "rotate_x": ("Rotate around X", "°"),
    "rotate_y": ("Rotate around Y", "°"),
    "scale": ("Scale", ""),
    "scale_to_fit": ("Scale to Fit", ""),
    "scan_first_layer": ("Scan first layer", ""),
    "scarf_angle_threshold": ("Conditional angle threshold", "°"),
    "scarf_joint_flow_ratio": ("Scarf joint flow ratio", ""),
    "scarf_joint_speed": ("Scarf joint speed", "mm/s or %"),
    "scarf_overhang_threshold": ("Conditional overhang threshold", "%"),
    "seam_gap": ("Seam gap", "mm or %"),
    "seam_position": ("Seam position", ""),
    "seam_slope_conditional": ("Conditional scarf joint", ""),
    "seam_slope_entire_loop": ("Scarf around entire wall", ""),
    "seam_slope_inner_walls": ("Scarf joint for inner walls", ""),
    "seam_slope_min_length": ("Scarf length", "mm"),
    "seam_slope_start_height": ("Scarf start height", "mm or %"),
    "seam_slope_steps": ("Scarf steps", ""),
    "seam_slope_type": ("Scarf joint seam (beta)", ""),
    "second": ("Second", ""),
    "separated_infills": ("Separated infills", ""),
    "set_other_flow_ratios": ("Set other flow ratios", ""),
    "silent_mode": ("Supports silent mode", ""),
    "silent_print_time": ("Print time (silent mode)", ""),
    "single_extruder_multi_material": ("Single Extruder Multi Material", ""),
    "single_extruder_multi_material_priming": ("Prime all printing extruders", ""),
    "single_instance": ("Single instance mode", ""),
    "single_loop_draft_shield": ("Single loop after first layer", ""),
    "skeleton_infill_density": ("Skeleton infill density", "%"),
    "skeleton_infill_line_width": ("Skeleton line width", "mm"),
    "skin_infill_density": ("Skin infill density", "%"),
    "skin_infill_depth": ("Skin infill depth", "mm"),
    "skin_infill_line_width": ("Skin line width", "mm"),
    "skip_modified_gcodes": ("Skip modified G-code in 3MF", ""),
    "skip_objects": ("Skip Objects", ""),
    "skirt_distance": ("Skirt distance", "mm"),
    "skirt_height": ("Skirt height", "layers"),
    "skirt_loops": ("Skirt loops", ""),
    "skirt_speed": ("Skirt speed", "mm/s"),
    "skirt_start_angle": ("Skirt start point", "°"),
    "skirt_type": ("Skirt type", ""),
    "slice": ("Slice", ""),
    "slice_closing_radius": ("Slice gap closing radius", "mm"),
    "slicing_mode": ("Slicing Mode", ""),
    "slicing_pipeline_plugin": ("Slicing Pipeline Plugin", ""),
    "slow_down_for_layer_cooling": ("Slow printing down for better layer cooling", ""),
    "slow_down_layer_time": ("Layer time", "ssecond"),
    "slow_down_layers": ("Number of slow layers", "layers"),
    "slow_down_min_speed": ("Min print speed", "mm/s"),
    "slowdown_for_curled_perimeters": ("Slow down for curled perimeters", ""),
    "small_area_infill_flow_compensation": ("Small area flow compensation (beta)", ""),
    "small_area_infill_flow_compensation_model": ("Flow Compensation Model", ""),
    "small_perimeter_speed": ("Small perimeters", "mm/s or %"),
    "small_perimeter_threshold": ("Small perimeters threshold", "mm"),
    "small_support_perimeter_speed": ("Small support perimeters", "mm/s or %"),
    "small_support_perimeter_threshold": ("Small support perimeters threshold", "mm"),
    "solid_infill_direction": ("Solid infill direction", "°"),
    "solid_infill_rotate_template": ("Solid infill rotation template", "°"),
    "sparse_infill_acceleration": ("Sparse infill", "mm/s² or %"),
    "sparse_infill_density": ("Sparse infill density", "%"),
    "sparse_infill_filament_id": ("Infill", ""),
    "sparse_infill_flow_ratio": ("Sparse infill flow ratio", ""),
    "sparse_infill_line_width": ("Sparse infill", "mm or %"),
    "sparse_infill_pattern": ("Sparse infill pattern", ""),
    "sparse_infill_rotate_template": ("Sparse infill rotation template", "°"),
    "sparse_infill_smooth_factor": ("Sparse infill smooth factor", "%"),
    "sparse_infill_speed": ("Sparse infill", "mm/s"),
    "spiral_finishing_flow_ratio": ("Spiral finishing flow ratio", ""),
    "spiral_mode": ("Spiral vase", ""),
    "spiral_mode_max_xy_smoothing": ("Max XY Smoothing", "mm or %"),
    "spiral_mode_smooth": ("Smooth Spiral", ""),
    "spiral_starting_flow_ratio": ("Spiral starting flow ratio", ""),
    "split": ("Split", ""),
    "staggered_inner_seams": ("Staggered inner seams", ""),
    "standby_temperature_delta": ("Temperature variation", "∆℃"),
    "start_end_points": ("Start end points", ""),
    "strict": ("Strict mode", ""),
    "supertack_plate_temp": ("Bed temperature", "℃"),
    "supertack_plate_temp_initial_layer": ("First layer bed temperature", "℃"),
    "support_air_filtration": ("Support air filtration", ""),
    "support_angle": ("Pattern angle", "°"),
    "support_base_pattern": ("Base pattern", ""),
    "support_base_pattern_spacing": ("Base pattern spacing", "mm"),
    "support_bottom_interface_spacing": ("Bottom interface spacing", "mm"),
    "support_bottom_z_distance": ("Bottom Z distance", "mm"),
    "support_chamber_temp_control": ("Support controlling chamber temperature", ""),
    "support_critical_regions_only": ("Support critical regions only", ""),
    "support_expansion": ("Normal support expansion", "mm"),
    "support_fast_purge_mode": ("Support fast purge mode", ""),
    "support_filament": ("Support/raft base", ""),
    "support_flow_ratio": ("Support flow ratio", ""),
    "support_interface_bottom_layers": ("Bottom interface layers", "layers"),
    "support_interface_filament": ("Support/raft interface", ""),
    "support_interface_flow_ratio": ("Support interface flow ratio", ""),
    "support_interface_loop_pattern": ("Loop pattern interface", ""),
    "support_interface_not_for_body": ("Avoid interface filament for base", ""),
    "support_interface_pattern": ("Interface pattern", ""),
    "support_interface_spacing": ("Top interface spacing", "mm"),
    "support_interface_speed": ("Support interface", "mm/s"),
    "support_interface_top_layers": ("Top interface layers", "layers"),
    "support_ironing": ("Ironing Support Interface", ""),
    "support_ironing_flow": ("Support Ironing flow", "%"),
    "support_ironing_pattern": ("Support Ironing Pattern", ""),
    "support_ironing_spacing": ("Support Ironing line spacing", "mm"),
    "support_line_width": ("Support", "mm or %"),
    "support_material_interface_fan_speed": ("Support interface fan speed", "%"),
    "support_multi_bed_types": ("Support multi bed types", ""),
    "support_object_first_layer_gap": ("Support/object first layer gap", "mm"),
    "support_object_xy_distance": ("Support/object XY distance", "mm"),
    "support_on_build_plate_only": ("On build plate only", ""),
    "support_parallel_printheads": ("Support parallel printheads", ""),
    "support_remove_small_overhang": ("Ignore small overhangs", ""),
    "support_speed": ("Support", "mm/s"),
    "support_style": ("Style", ""),
    "support_threshold_angle": ("Threshold angle", "°"),
    "support_threshold_overlap": ("Threshold overlap", "mm or %"),
    "support_top_z_distance": ("Top Z distance", "mm"),
    "support_type": ("Type", ""),
    "sw_renderer": ("Render with a software renderer", ""),
    "symmetric_infill_y_axis": ("Symmetric infill Y axis", ""),
    "temperature_vitrification": ("Softening temperature", "℃"),
    "template_custom_gcode": ("Custom G-code", ""),
    "textured_cool_plate_temp": ("Bed temperature", "℃"),
    "textured_cool_plate_temp_initial_layer": ("First layer bed temperature", "℃"),
    "textured_plate_temp": ("Bed temperature", "℃"),
    "textured_plate_temp_initial_layer": ("First layer bed temperature", "℃"),
    "thick_bridges": ("Thick external bridges", ""),
    "thick_internal_bridges": ("Thick internal bridges", ""),
    "thumbnails": ("G-code thumbnails", ""),
    "thumbnails_format": ("Format of G-code thumbnails", ""),
    "time_cost": ("Time cost", "money/h"),
    "time_lapse_gcode": ("Timelapse G-code", ""),
    "timelapse_type": ("Timelapse", ""),
    "timestamp": ("Timestamp", ""),
    "tool_change_on_wipe_tower": ("Tool change on wipe tower", ""),
    "toolchange_cyclic_first_layer": ("Apply cyclic order to first layer", ""),
    "toolchange_cyclic_order": ("Cyclic order", ""),
    "toolchange_ordering": ("Toolchange ordering", ""),
    "top_bottom_infill_wall_overlap": ("Top/Bottom solid infill/wall overlap", "%"),
    "top_color_penetration_layers": ("Top paint penetration layers", "layers"),
    "top_layer_direction": ("Top layer direction", "°"),
    "top_shell_layers": ("Top solid layers", "layers"),
    "top_shell_thickness": ("Top shell thickness", "mm"),
    "top_solid_infill_flow_ratio": ("Top surface flow ratio", ""),
    "top_surface_acceleration": ("Top surface", "mm/s²"),
    "top_surface_density": ("Top surface density", "%"),
    "top_surface_expansion": ("Top surface expansion", "mm"),
    "top_surface_expansion_direction": ("Top expansion direction", ""),
    "top_surface_expansion_margin": ("Top expansion wall margin", "mm"),
    "top_surface_filament_id": ("Top surface", ""),
    "top_surface_fill_order": ("Top surface fill order", ""),
    "top_surface_jerk": ("Top surface", "mm/s"),
    "top_surface_line_width": ("Top surface", "mm or %"),
    "top_surface_pattern": ("Top surface pattern", ""),
    "top_surface_speed": ("Top surface", "mm/s"),
    "total_cost": ("Total cost", ""),
    "total_layer_count": ("Total layer count", ""),
    "total_toolchanges": ("Total tool changes", ""),
    "total_weight": ("Total weight", ""),
    "total_wipe_tower_cost": ("Total wipe tower cost", ""),
    "total_wipe_tower_filament": ("Total wipe tower filament", ""),
    "travel_acceleration": ("Travel", "mm/s²"),
    "travel_jerk": ("Travel", "mm/s"),
    "travel_slope": ("Traveling angle", "°"),
    "travel_speed": ("Travel", "mm/s"),
    "tree_support_angle_slow": ("Preferred Branch Angle", "°"),
    "tree_support_auto_brim": ("Auto brim width", ""),
    "tree_support_branch_angle": ("Tree support branch angle", "°"),
    "tree_support_branch_angle_organic": ("Tree support branch angle", "°"),
    "tree_support_branch_diameter": ("Tree support branch diameter", "mm"),
    "tree_support_branch_diameter_angle": ("Branch Diameter Angle", "°"),
    "tree_support_branch_diameter_organic": ("Tree support branch diameter", "mm"),
    "tree_support_branch_distance": ("Tree support branch distance", "mm"),
    "tree_support_branch_distance_organic": ("Tree support branch distance", "mm"),
    "tree_support_brim_width": ("Tree support brim width", ""),
    "tree_support_tip_diameter": ("Tip Diameter", "mm"),
    "tree_support_top_rate": ("Branch Density", "%"),
    "tree_support_wall_count": ("Support wall loops", ""),
    "tree_support_with_infill": ("Tree support with infill", ""),
    "unsupported_wall_last": ("Print unsupported walls last", ""),
    "uptodate": ("UpToDate", ""),
    "uptodate_filaments": ("Load uptodate filament settings when using uptodate", ""),
    "uptodate_settings": ("Load uptodate process/machine settings when using uptodate", ""),
    "upward_compatible_machine": ("upward compatible machine", ""),
    "use_3mf": ("Use 3MF instead of G-code", ""),
    "use_firmware_retraction": ("Use firmware retraction", ""),
    "use_relative_e_distances": ("Use relative E distances", ""),
    "used_filament": ("Used filament", ""),
    "used_filament_length": ("Filament length (meters)", ""),
    "volumetric_speed_coefficients": ("Max volumetric speed multinomial coefficients", ""),
    "wait_for_temp_on_wipe_tower": ("Wait for temperature on wipe tower", ""),
    "wall_direction": ("Wall loop direction", ""),
    "wall_distribution_count": ("Wall distribution count", ""),
    "wall_generator": ("Wall generator", ""),
    "wall_loops": ("Wall loops", ""),
    "wall_maximum_deviation": ("Maximum wall deviation", "mm"),
    "wall_maximum_resolution": ("Maximum wall resolution", "mm"),
    "wall_sequence": ("Walls printing order", ""),
    "wall_transition_angle": ("Wall transitioning threshold angle", "°"),
    "wall_transition_filter_deviation": ("Wall transitioning filter margin", "%"),
    "wall_transition_length": ("Wall transition length", "%"),
    "wipe": ("Wipe while retracting", ""),
    "wipe_before_external_loop": ("Wipe before external loop", ""),
    "wipe_distance": ("Wipe distance", "mm"),
    "wipe_inward": ("Wipe inward", ""),
    "wipe_inward_distance": ("Wipe inward distance", "mm or %"),
    "wipe_on_loops": ("Wipe on loops", ""),
    "wipe_speed": ("Wipe speed", "mm/s or %"),
    "wipe_tower_bridging": ("Maximal bridging distance", "mm"),
    "wipe_tower_cone_angle": ("Stabilization cone apex angle", "°"),
    "wipe_tower_extra_flow": ("Extra flow for purging", "%"),
    "wipe_tower_extra_rib_length": ("Extra rib length", "mm"),
    "wipe_tower_extra_spacing": ("Wipe tower purge lines spacing", "%"),
    "wipe_tower_filament": ("Wipe tower", ""),
    "wipe_tower_fillet_wall": ("Fillet wall", ""),
    "wipe_tower_max_purge_speed": ("Maximum wipe tower print speed", "mm/s"),
    "wipe_tower_no_sparse_layers": ("No sparse layers (beta)", ""),
    "wipe_tower_rib_width": ("Rib width", "mm"),
    "wipe_tower_rotation_angle": ("Wipe tower rotation angle", "°"),
    "wipe_tower_sparse_layers_combination": ("Combine sparse layers", ""),
    "wipe_tower_type": ("Wipe tower type", ""),
    "wipe_tower_wall_type": ("Wall type", ""),
    "wiping_volumes_extruders": ("Purging volumes - load/unload volumes", ""),
    "wrapping_detection_gcode": ("Clumping detection G-code", ""),
    "wrapping_detection_layers": ("Clumping detection layers", ""),
    "wrapping_exclude_area": ("Probing exclude area of clumping", ""),
    "xy_contour_compensation": ("X-Y contour compensation", "mm"),
    "xy_hole_compensation": ("X-Y hole compensation", "mm"),
    "year": ("Year", ""),
    "z_hop": ("Z-hop height", "mm"),
    "z_hop_types": ("Z-hop type", ""),
    "z_offset": ("Z offset", "mm"),
    "zaa_dont_alternate_fill_direction": ("Don't alternate fill direction", ""),
    "zaa_enabled": ("Z contouring enabled", ""),
    "zaa_min_z": ("Minimum Z height", "mm"),
    "zaa_minimize_perimeter_height": ("Minimize wall height angle", "°"),
    "zhop": ("Current Z-hop", ""),
}
# <<< generated LABELS
# >>> generated VALUE_KINDS (tools/embed.py)
VALUE_KINDS = {
    "accel_to_decel_enable": "bool",
    "adaptive_infill_gradient": {"linear": "Linear", "quadratic": "Quadratic", "exponential": "Exponential"},
    "adaptive_infill_mode": {"disabled": "Disabled", "distance_warp": "Distance warp", "smooth_blend": "Smooth blend", "stepped_shells": "Stepped shells", "lobes": "Lobes", "normal_z": "Normal Z", "normal_y": "Normal Y", "normal_x": "Normal X"},
    "align_infill_direction_to_model": "bool",
    "align_to_y_axis": "bool",
    "allow_mix_temp": "bool",
    "allow_multicolor_oneplate": "bool",
    "allow_newer_file": "bool",
    "allow_rotations": "bool",
    "alternate_extra_wall": "bool",
    "assemble": "bool",
    "auxiliary_fan": "bool",
    "avoid_extrusion_cali_region": "bool",
    "bbl_calib_mark_logo": "bool",
    "bbl_use_printhost": "bool",
    "bed_temperature_formula": {"by_first_filament": "By First filament", "by_highest_temp": "By Highest Temp"},
    "belt_frame_tilt_decouple": "bool",
    "belt_printer": "bool",
    "belt_printer_infinite_y": "bool",
    "belt_purge_tower_object": "bool",
    "bottom_surface_fill_order": {"default": "Default", "outward": "Outward", "inward": "Inward"},
    "bottom_surface_pattern": {"monotonic": "Monotonic", "monotonicline": "Monotonic line", "rectilinear": "Rectilinear", "alignedrectilinear": "Aligned Rectilinear", "concentric": "Concentric", "spiralinset": "Spiral Inset", "hilbertcurve": "Hilbert Curve", "archimedeanchords": "Archimedean Chords", "octagramspiral": "Octagram Spiral"},
    "bridge_no_support": "bool",
    "brim_ears": "bool",
    "brim_ears_outer_only": "bool",
    "brim_type": {"auto_brim": "Auto", "brim_ears": "Mouse ear", "painted": "Painted", "outer_only": "Outer brim only", "inner_only": "Inner brim only", "outer_and_inner": "Outer and inner brim", "no_brim": "No-brim", "leading_edge_only": "Leading edge only"},
    "brim_use_efc_outline": "bool",
    "calib_flowrate_topinfill_special_order": "bool",
    "center_of_surface_pattern": {"each_surface": "Each Surface", "each_model": "Each Model", "each_assembly": "Each Assembly"},
    "combine_brims": "bool",
    "config_compatibility": {"disable": "Bail out on unknown configuration values", "enable": "Enable reading unknown configuration values by verbosely substituting them with defaults.", "enable_silent": "Enable reading unknown configuration values by silently substituting them with defaults."},
    "convert_unit": "bool",
    "cooling_filter_enabled": "bool",
    "counterbore_hole_bridging": {"none": "None", "partiallybridge": "Partially bridged", "sacrificiallayer": "Sacrificial layer"},
    "curr_bed_type": {"Cool Plate": "Smooth Cool Plate", "Engineering Plate": "Engineering Plate", "High Temp Plate": "Smooth High Temp Plate", "Textured PEI Plate": "Textured PEI Plate", "Textured Cool Plate": "Textured Cool Plate", "Supertack Plate": "Cool Plate (SuperTack)"},
    "detect_narrow_internal_solid_infill": "bool",
    "detect_overhang_wall": "bool",
    "detect_thin_wall": "bool",
    "disable_m73": "bool",
    "display_mirror_x": "bool",
    "display_mirror_y": "bool",
    "display_orientation": {},
    "dont_filter_internal_bridges": {"disabled": "Filter", "limited": "Limited filtering", "nofilter": "No filtering"},
    "downward_check": "bool",
    "draft_shield": {"disabled": "Disabled", "enabled": "Enabled"},
    "emit_machine_limits_to_gcode": "bool",
    "enable_arc_fitting": {"disabled": "Disabled", "enabled": "Enabled", "sparse_infill": "Sparse infill only"},
    "enable_belt_purge_tower": "bool",
    "enable_extra_bridge_layer": {"disabled": "Disabled", "external_bridge_only": "External bridge only", "internal_bridge_only": "Internal bridge only", "apply_to_all": "Apply to all"},
    "enable_filament_dynamic_map": "bool",
    "enable_filament_ramming": "bool",
    "enable_mixed_color_sublayer": "bool",
    "enable_power_loss_recovery": {"printer_configuration": "Printer configuration", "enable": "Enable", "disable": "Disable"},
    "enable_pre_heating": "bool",
    "enable_prime_tower": "bool",
    "enable_support": "bool",
    "enable_timelapse": "bool",
    "enable_tower_interface_cooldown_during_tower": "bool",
    "enable_tower_interface_features": "bool",
    "enable_wrapping_detection": "bool",
    "ensure_on_bed": "bool",
    "ensure_vertical_shell_thickness": {"none": "None", "ensure_critical_only": "Critical Only", "ensure_moderate": "Moderate", "ensure_all": "All"},
    "exclude_object": "bool",
    "export_amf": "bool",
    "export_gcode": "bool",
    "export_obj": "bool",
    "export_sla": "bool",
    "export_statistics": "bool",
    "export_stl": "bool",
    "export_svg": "bool",
    "extra_perimeters_on_overhangs": "bool",
    "extrusion_rate_smoothing_external_perimeter_only": "bool",
    "fan_direction": {"undefine": "Undefined", "left": "Left", "right": "Right", "both": "Both"},
    "fan_speedup_overhangs": "bool",
    "farthest_point_timelapse": "bool",
    "filament_map_mode": {"Auto For Flush": "Auto For Flush", "Auto For Match": "Auto For Match", "Manual": "Manual", "Nozzle Manual": "Nozzle Manual", "Default": "Default"},
    "first_layer_sequence_choice": {"Auto": "Auto", "Customize": "Customize"},
    "flush_into_infill": "bool",
    "flush_into_objects": "bool",
    "flush_into_support": "bool",
    "fuzzy_skin": {"none": "Painted only", "external": "Contour", "hole": "Hole", "all": "Contour and hole", "allwalls": "All walls", "disabled_fuzzy": "Disabled"},
    "fuzzy_skin_first_layer": "bool",
    "fuzzy_skin_mode": {"displacement": "Displacement", "extrusion": "Extrusion", "combined": "Combined"},
    "fuzzy_skin_noise_type": {"classic": "Classic", "perlin": "Perlin", "billow": "Billow", "ridgedmulti": "Ridged Multifractal", "voronoi": "Voronoi", "ripple": "Ripple"},
    "gap_fill_target": {"everywhere": "Everywhere", "topbottom": "Top and bottom surfaces", "nowhere": "Nowhere"},
    "gcode_add_line_number": "bool",
    "gcode_comments": "bool",
    "gcode_flavor": {"marlin": "Marlin (legacy)", "klipper": "Klipper", "reprapfirmware": "RepRapFirmware", "repetier": "Repetier", "marlin2": "Marlin 2"},
    "gcode_label_objects": "bool",
    "gcode_skip_config_block": "bool",
    "gcodeviewer": "bool",
    "ground_largest_face": "bool",
    "group_algo_with_time": "bool",
    "gyroid_optimized": "bool",
    "handle_hotend_as_extruder": "bool",
    "has_filament_switcher": "bool",
    "has_scarf_joint_seam": "bool",
    "has_single_extruder_multi_material_priming": "bool",
    "has_wipe_tower": "bool",
    "help": "bool",
    "help_fff": "bool",
    "help_sla": "bool",
    "high_current_on_filament_swap": "bool",
    "high_precision_gcode": "bool",
    "hole_to_polyhole": "bool",
    "hole_to_polyhole_twisted": "bool",
    "hollowing_enable": "bool",
    "host_type": {"prusalink": "PrusaLink", "prusaconnect": "PrusaConnect", "octoprint": "Octo/Klipper", "duet": "Duet", "ultimaker": "UltiMaker", "flashair": "FlashAir", "astrobox": "AstroBox", "repetier": "Repetier", "mks": "MKS", "esp3d": "ESP3D", "crealityprint": "CrealityPrint", "obico": "Obico", "flashforge": "Flashforge", "simplyprint": "SimplyPrint", "elegoolink": "Elegoo Link", "3dprinteros": "3DPrinterOS", "moonraker": "Moonraker (Klipper)"},
    "ignore_nonexistent_config": "bool",
    "imex_firmware_managed_zones": "bool",
    "imex_tool_layout": {"front-left": "Front-left", "front-right": "Front-right", "rear-left": "Rear-left", "rear-right": "Rear-right"},
    "imex_viz_theme": {"standard": "Standard", "deuteranopia": "Deuteranopia / Protanopia (red-green)", "tritanopia": "Tritanopia (blue-yellow)", "high_contrast": "High Contrast"},
    "independent_support_layer_height": "bool",
    "infill_combination": "bool",
    "infill_complete_top": "bool",
    "info": "bool",
    "input_shaping_emit": "bool",
    "inspect_mesh": "bool",
    "inspect_paint": "bool",
    "interface_shells": "bool",
    "interlocking_beam": "bool",
    "internal_solid_infill_pattern": {"monotonic": "Monotonic", "monotonicline": "Monotonic line", "rectilinear": "Rectilinear", "alignedrectilinear": "Aligned Rectilinear", "concentric": "Concentric", "spiralinset": "Spiral Inset", "hilbertcurve": "Hilbert Curve", "archimedeanchords": "Archimedean Chords", "octagramspiral": "Octagram Spiral"},
    "ironing_angle_fixed": "bool",
    "ironing_pattern": {"rectilinear": "Rectilinear", "concentric": "Concentric"},
    "ironing_type": {"no ironing": "No ironing", "top": "All top surfaces", "topmost": "Topmost surface only", "solid": "All solid layers"},
    "is_imex": "bool",
    "is_infill_first": "bool",
    "load_defaultfila": "bool",
    "make_overhang_printable": "bool",
    "manual_filament_change": "bool",
    "material_print_speed": {},
    "min_save": "bool",
    "no_check": "bool",
    "normative_check": "bool",
    "only_one_wall_first_layer": "bool",
    "only_one_wall_top": "bool",
    "ooze_prevention": "bool",
    "other_layers_sequence_choice": {"Auto": "Auto", "Customize": "Customize"},
    "overhang_reverse": "bool",
    "overhang_reverse_internal_only": "bool",
    "pad_around_object": "bool",
    "pad_around_object_everywhere": "bool",
    "pad_enable": "bool",
    "pellet_modded_printer": "bool",
    "precise_outer_wall": "bool",
    "precise_z_height": "bool",
    "prime_tower_enable_framework": "bool",
    "prime_tower_flat_ironing": "bool",
    "prime_tower_skip_points": "bool",
    "prime_volume_mode": {"Default": "Default", "Saving": "Saving", "Fast": "Fast"},
    "print_order": {"default": "Default", "as_obj_list": "As object list", "best_of": "Best of all (shortest path)", "snake": "Snake"},
    "print_sequence": {"by layer": "By layer", "by object": "By object"},
    "printer_structure": {"undefine": "Undefined", "corexy": "CoreXY", "i3": "I3", "hbot": "Hbot", "delta": "Delta"},
    "printhost_authorization_type": {"key": "API key", "user": "HTTP digest"},
    "printhost_ssl_ignore_revoke": "bool",
    "purge_in_prime_tower": "bool",
    "reduce_crossing_wall": "bool",
    "reduce_infill_retraction": "bool",
    "relative_bridge_angle": "bool",
    "repair": "bool",
    "resonance_avoidance": "bool",
    "role_based_wipe_speed": "bool",
    "scan_first_layer": "bool",
    "seam_position": {"nearest": "Nearest", "aligned": "Aligned", "aligned_back": "Aligned back", "back": "Back", "random": "Random"},
    "seam_slope_conditional": "bool",
    "seam_slope_entire_loop": "bool",
    "seam_slope_inner_walls": "bool",
    "seam_slope_type": {"none": "None", "external": "Contour", "all": "Contour and hole"},
    "separated_infills": "bool",
    "set_other_flow_ratios": "bool",
    "silent_mode": "bool",
    "single_extruder_multi_material": "bool",
    "single_extruder_multi_material_priming": "bool",
    "single_instance": "bool",
    "single_loop_draft_shield": "bool",
    "skip_modified_gcodes": "bool",
    "skirt_type": {"combined": "Combined", "perobject": "Per object"},
    "slicing_mode": {"regular": "Regular", "even_odd": "Even-odd", "close_holes": "Close holes"},
    "small_area_infill_flow_compensation": "bool",
    "sparse_infill_pattern": {"rectilinear": "Rectilinear", "alignedrectilinear": "Aligned Rectilinear", "zigzag": "Zig Zag", "crosszag": "Cross Zag", "lockedzag": "Locked Zag", "line": "Line", "grid": "Grid", "triangles": "Triangles", "tri-hexagon": "Tri-hexagon", "cubic": "Cubic", "adaptivecubic": "Adaptive Cubic", "quartercubic": "Quarter Cubic", "supportcubic": "Support Cubic", "lightning": "Lightning", "honeycomb": "Honeycomb", "3dhoneycomb": "3D Honeycomb", "lateral-honeycomb": "Lateral Honeycomb", "lateral-lattice": "Lateral Lattice", "crosshatch": "Cross Hatch", "tpmsd": "TPMS-D", "tpmsfk": "TPMS-FK", "gyroid": "Gyroid", "concentric": "Concentric", "hilbertcurve": "Hilbert Curve", "archimedeanchords": "Archimedean Chords", "octagramspiral": "Octagram Spiral"},
    "spiral_mode": "bool",
    "spiral_mode_smooth": "bool",
    "split": "bool",
    "staggered_inner_seams": "bool",
    "strict": "bool",
    "support_air_filtration": "bool",
    "support_base_pattern": {"default": "Default", "rectilinear": "Rectilinear", "rectilinear-grid": "Rectilinear grid", "honeycomb": "Honeycomb", "lightning": "Lightning", "hollow": "Hollow"},
    "support_buildplate_only": "bool",
    "support_chamber_temp_control": "bool",
    "support_cooling_filter": "bool",
    "support_critical_regions_only": "bool",
    "support_fast_purge_mode": "bool",
    "support_interface_loop_pattern": "bool",
    "support_interface_not_for_body": "bool",
    "support_interface_pattern": {"auto": "Default", "rectilinear": "Rectilinear", "concentric": "Concentric", "spiralinset": "Spiral Inset", "rectilinear_interlaced": "Rectilinear Interlaced", "grid": "Grid"},
    "support_ironing": "bool",
    "support_ironing_pattern": {"rectilinear": "Rectilinear", "concentric": "Concentric"},
    "support_multi_bed_types": "bool",
    "support_object_skip_flush": "bool",
    "support_on_build_plate_only": "bool",
    "support_parallel_printheads": "bool",
    "support_pillar_connection_mode": {},
    "support_remove_small_overhang": "bool",
    "support_style": {"default": "Default (Grid/Organic)", "grid": "Grid", "snug": "Snug", "organic": "Organic", "tree_slim": "Tree Slim", "tree_strong": "Tree Strong", "tree_hybrid": "Tree Hybrid"},
    "support_type": {"normal(auto)": "Normal (auto)", "tree(auto)": "Tree (auto)", "normal(manual)": "Normal (manual)", "tree(manual)": "Tree (manual)"},
    "supports_enable": "bool",
    "sw_renderer": "bool",
    "symmetric_infill_y_axis": "bool",
    "thick_bridges": "bool",
    "thick_internal_bridges": "bool",
    "thumbnails_format": {"PNG": "PNG", "JPG": "JPG", "QOI": "QOI", "BTT_TFT": "BTT TT", "COLPIC": "ColPic"},
    "timelapse_type": {"0": "Traditional", "1": "Smooth"},
    "tool_change_on_wipe_tower": "bool",
    "toolchange_cyclic_first_layer": "bool",
    "toolchange_ordering": {"default": "Default", "cyclic": "Cyclic"},
    "top_surface_expansion_direction": {"inward_and_outward": "Inward and Outward", "inward": "Inward", "outward": "Outward"},
    "top_surface_fill_order": {"default": "Default", "outward": "Outward", "inward": "Inward"},
    "top_surface_pattern": {"monotonic": "Monotonic", "monotonicline": "Monotonic line", "rectilinear": "Rectilinear", "alignedrectilinear": "Aligned Rectilinear", "concentric": "Concentric", "spiralinset": "Spiral Inset", "hilbertcurve": "Hilbert Curve", "archimedeanchords": "Archimedean Chords", "octagramspiral": "Octagram Spiral"},
    "tree_support_auto_brim": "bool",
    "tree_support_with_infill": "bool",
    "unsupported_wall_last": "bool",
    "uptodate": "bool",
    "use_3mf": "bool",
    "use_firmware_retraction": "bool",
    "use_relative_e_distances": "bool",
    "wait_for_temp_on_wipe_tower": "bool",
    "wall_direction": {"ccw": "Counter clockwise", "cw": "Clockwise"},
    "wall_generator": {"classic": "Classic", "arachne": "Arachne"},
    "wall_sequence": {"inner wall/outer wall": "Inner/Outer", "outer wall/inner wall": "Outer/Inner", "inner-outer-inner wall": "Inner/Outer/Inner"},
    "wipe_before_external_loop": "bool",
    "wipe_inward": "bool",
    "wipe_on_loops": "bool",
    "wipe_tower_fillet_wall": "bool",
    "wipe_tower_no_sparse_layers": "bool",
    "wipe_tower_sparse_layers_combination": "bool",
    "wipe_tower_type": {"type1": "Type 1", "type2": "Type 2"},
    "wipe_tower_wall_type": {"rectangle": "Rectangle", "cone": "Cone", "rib": "Rib"},
    "zaa_dont_alternate_fill_direction": "bool",
    "zaa_enabled": "bool",
}
# <<< generated VALUE_KINDS
