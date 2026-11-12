# /// script
# requires-python = ">=3.12"
#
# [tool.orcaslicer.plugin]
# name = "OrcaSlicer MCP"
# description = "Lets an AI assistant read your OrcaSlicer setup, change settings and read the slices you make, through the orcaslicer-mcp server. Loading models, starting a slice, plates, renders and projects need the OrcaSlicer MCP build."
# author = "Max Ellis"
# version = "0.2.3"
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

PLUGIN_VERSION = "0.2.3"
DEFAULT_PORT = 13130
BIND_HOST = "127.0.0.1"
MAX_BODY = 4 * 1024 * 1024
UI_TIMEOUT_S = 10.0
# Stock 2.5 creates a plugin's dock panel only once its Prepare view is on screen, so an OrcaSlicer that opens on its
# Home tab has no page to carry requests until the user clicks Prepare (seen on a Mac, 2026-11-12). Sent as a 503 with
# its own error: the connector shows a 503's reason as it is, while a 504 becomes "a dialog may be open".
PANEL_NOT_OPEN_TEXT = ("the OrcaSlicer MCP panel has not opened yet: OrcaSlicer opens it once its Prepare tab shows. "
                       "Ask the user to click Prepare at the top of OrcaSlicer's window, and to close any dialog "
                       "that is open there.")
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
        self.page_answered = False  # the current panel's page has run a job or posted anything at all

    def panel_created(self):
        """A new panel: its page has not loaded yet (and won't before OrcaSlicer shows its Prepare tab)."""
        self.page_answered = False

    def call(self, fn, timeout: float | None = None, gate: bool = True):
        timeout = UI_TIMEOUT_S if timeout is None else timeout
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
            if still_queued and not self.page_answered:
                raise ApiReply(503, {"error": "panel_not_open", "detail": PANEL_NOT_OPEN_TEXT})
            if still_queued:
                raise ApiReply(504, {"error": "ui_timeout",
                                     "detail": "the OrcaSlicer MCP panel did not answer; is OrcaSlicer busy, "
                                               "or the panel hidden?"})
            job.done.wait()  # it started on the UI thread just now: let it finish
        if job.error is not None:
            raise job.error
        return job.result

    def run_pending(self):
        self.page_answered = True
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
        refusal the connector made before reaching OrcaSlicer, or a "note": the assistant's own words, a plain row in the
        group still open (mcp.17). Recorded even on a trial request; changes nothing."""
        self.pump.call(lambda: None)  # refused like every other request while assistant access is off
        kind = data.get("kind")
        if kind not in ("slice", "refused", "note"):
            raise ApiReply(400, {"error": "bad_kind"})
        text = _note_text(data.get("text"), 160)
        if not text:
            raise ApiReply(400, {"error": "missing_text"})
        value = _note_text(data.get("value"), 80)
        detail = _note_text(data.get("detail"), 400)
        if kind == "note":  # no undo, and it does not end the group
            self.record([{"kind": "note", "label": text, "value": value, "where": detail}])
            return 200, {"recorded": True}
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
UV_URL = "https://docs.astral.sh/uv/getting-started/installation/"  # every route runs the connector with uvx
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
                                            "also": kw.get("also", ""), "copy_label": kw.get("copy_label", ""),
                                            **({"fields": kw["fields"]} if kw.get("fields") else {})}
    box = lambda label, value: {"label": label, "value": value}
    also = ("Also add " + ", ".join(f"{k} = {v}" for k, v in env.items()) + " as an environment variable.") if env else ""
    return [
        {"id": "claude", "name": "Claude", "where": "Claude", "routes": [
            route("Claude app", "For chats. Download the extension, then open the file. Needs uv.", "Download", primary=True,
                  open=RELEASES_URL, also=("In the extension's settings, set OrcaSlicer settings folder to "
                                           + datadir + ".") if datadir else ""),
            route("Claude Code", "In a terminal, an editor or the app's Code tab. Needs uv.", "Copy command",
                  copy=cmd("claude mcp add --scope user orcaslicer"))]},
        {"id": "openai", "name": "OpenAI", "where": "OpenAI", "routes": [
            # Its form has a box for the command and another for the arguments: "uvx orcaslicer-mcp" pasted whole
            # into the first does not start (a user on OrcaCloud, 2026-11). So one line per box, in its words.
            route("ChatGPT desktop app", "Settings › Plugins › Add › Add MCP server, then choose STDIO and fill in each box "
                  "below. Works in Codex chats; ordinary ChatGPT chats can't use it yet. Needs uv.", "", primary=True,
                  fields=[box("Name", "orcaslicer"), box("Command to launch", "uvx"), box("Arguments", "orcaslicer-mcp")],
                  also=also),
            route("Codex in a terminal or editor", "Run this once. The desktop app shares the setting. Needs uv.", "Copy command",
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
            "status": status or None, "paused": bool(paused), "uv": UV_URL}


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
    for resources in resources_dirs():
        if os.path.isfile(os.path.join(resources, MCP_BUILD_PAGE)):
            return True
    return False


# ---- The panel's language (orcaslicer-mcp #21) ---------------------------------------------------------------------
# The panel page translates its own words and the host's phrases from the tables inside it (the fork's
# scripts/mcp_panel_i18n.py; the plugin's own phrases are in its i18n/external.json). The host tells it OrcaSlicer's
# language and, for setting rows, OrcaSlicer's own words for their English names and choices (the build's McpPanel.cpp
# local_words). The rows themselves stay English: the assistant reads them.
#
# Plugins learn the language from app_info, which reports Catalan, Basque and Hungarian as en_US and Traditional
# Chinese as zh_CN (current_language_code_safe). OrcaSlicer sets the process's C locale to its language when it
# starts (wxLocale), before any plugin loads, and Python changes only LC_CTYPE of it, so the other categories tell
# those four apart. OrcaSlicer's own words come from its translation file, resources/i18n/<lang>/OrcaSlicer.mo, in the
# resources folder that plugins may read.
_REFINED = (("ca", "catalan", "ca_ES"), ("eu", "basque", "eu_ES"), ("hu", "hungarian", "hu_HU"))
_TRADITIONAL = ("zh_tw", "zh-tw", "zh_hk", "zh-hk", "zh_mo", "zh-mo", "zh-hant", "zh_hant", "chinese (traditional)",
                "chinese_taiwan", "chinese_hong kong", "chinese (traditional)_")


def _c_locale_name() -> str:
    """The C locale OrcaSlicer set for its language ("ca_ES.UTF-8", "Catalan_Spain.1252"), "" when unknown. Only
    queried, never set."""
    import locale
    for name in ("LC_NUMERIC", "LC_TIME", "LC_COLLATE", "LC_MONETARY"):
        cat = getattr(locale, name, None)
        if cat is None:
            continue
        try:
            current = locale.setlocale(cat)
        except Exception:
            continue
        if current and current not in ("C", "POSIX"):
            return current
    return ""


def ui_language(info: dict | None, c_locale: str | None = None) -> str:
    """OrcaSlicer's language as the build sends it ("de_DE", "ca_ES", "zh_TW"), "" when it is English or unknown."""
    code = info.get("language") if isinstance(info, dict) else None
    code = code if isinstance(code, str) else ""
    c = (_c_locale_name() if c_locale is None else c_locale).strip().lower()
    head = re.split(r"[_.\-@ ]", c, maxsplit=1)[0] if c else ""
    if code in ("", "en_US"):
        for short, english, full in _REFINED:
            if head in (short, english):
                return full
    if code == "zh_CN" and c.startswith(_TRADITIONAL):
        return "zh_TW"
    return "" if code.startswith("en") else code


def resources_dirs() -> list:
    """Where OrcaSlicer's resources folder may be, from the bundled Python's home (see running_in_mcp_build)."""
    home = os.path.normpath(sys.prefix)
    return [os.path.join(home, os.pardir, "resources"),               # Windows
            os.path.join(home, os.pardir, os.pardir, "Resources"),    # macOS
            os.path.join(home, os.pardir, os.pardir, "resources")]    # Linux


class OrcaWords:
    """OrcaSlicer's own translation of its English words (a setting's name, a section, a choice), as the build's
    orca_word: its translation file for the language, read once. Every word is itself when there is no file."""

    def __init__(self, language: str, dirs: list | None = None):
        self.language, self._tr, self._names = language, None, {}
        if not language:
            return
        import gettext
        names = [language, language.split("_")[0]]
        for res in (resources_dirs() if dirs is None else dirs):
            for name in names:
                path = os.path.join(res, "i18n", name, "OrcaSlicer.mo")
                try:
                    with open(path, "rb") as f:
                        self._tr = gettext.GNUTranslations(f)
                    trace(f"OrcaSlicer's words for {language}: {path}")
                    return
                except (OSError, ValueError, UnicodeError) as e:
                    if not isinstance(e, FileNotFoundError):
                        trace(f"translation file {path} unreadable: {type(e).__name__}: {e}")

    @property
    def available(self) -> bool:
        return self._tr is not None

    def word(self, en: str) -> str:
        if not en or self._tr is None:
            return en
        try:
            return self._tr.gettext(en)
        except Exception:
            return en

    def setting_name(self, en: str) -> str:
        """A setting's English name ("Wall loops", "Acceleration: Normal printing", "Nozzle temperature – Other
        layers") in OrcaSlicer's words part by part, as the build's local_setting_name; "" when nothing changed."""
        if en in self._names:
            return self._names[en]
        colon, dash = ": ", " – "

        def dashed(t):
            d = t.find(dash)
            return self.word(t) if d < 0 else self.word(t[:d]) + dash + self.word(t[d + len(dash):])
        out = self.word(en)
        if out == en:
            c = en.find(colon)
            out = dashed(en) if c < 0 else self.word(en[:c]) + colon + dashed(en[c + len(colon):])
        if out == en and en.endswith(")") and " (" in en:
            # The plugin's own composed names: "Bed temperature (Smooth Cool Plate)" (display_label), each part
            head, inner = en[:-1].rsplit(" (", 1)
            out = self.word(head) + " (" + self.word(inner) + ")"
        self._names[en] = "" if out == en else out
        return self._names[en]


_SLOTS = re.compile(r" \(filaments? \d+(, \d+)*\)$")


def local_words(row: dict, words: OrcaWords) -> dict | None:
    """{"<English>": "<OrcaSlicer's word>"} for a row's setting name and its setting's choices (all of them, so a value
    the user set since reads in the language too), or None: the build's local_words for the plugin's rows."""
    if not words.available or row.get("kind") not in ("setting", "refused"):
        return None
    out = {}
    label = row.get("label") or ""
    target = row.get("target") if isinstance(row.get("target"), dict) else {}
    if row.get("kind") == "refused" and ", " in label and not words.setting_name(label):
        # A refusal the connector made itself (POST /activity/note): the setting names it refused, joined with ", ".
        for name in label.split(", "):
            local = words.setting_name(name) if name else ""
            if local:
                out[name] = local
        return out or None
    name = _SLOTS.sub("", label) if label.endswith(")") else label
    local = words.setting_name(name) if name else ""
    if local:
        out[name] = local
    kind = VALUE_KINDS.get(target.get("key")) if target.get("kind") == "setting" else None
    if isinstance(kind, dict):
        for choice in kind.values():
            w = words.word(choice)
            if choice and w != choice:
                out[choice] = w
    return out or None


_TABLES: dict = {}


def _panel_tables() -> dict:
    """The page's translation tables (its panel-i18n block), for the text the plugin shows outside the page."""
    if _TABLES:
        return _TABLES
    begin, end = "// BEGIN PANEL I18N\n", "// END PANEL I18N"
    try:
        block = PANEL_HTML.split(begin, 1)[1].split(end, 1)[0].strip()
        data = json.loads(block[block.index("{"):block.rindex("}") + 1])
    except (IndexError, ValueError):
        data = None
    _TABLES.update(data if isinstance(data, dict) else {"keys": [], "langs": {}})
    return _TABLES


def panel_phrase(en: str, language: str) -> str:
    """One of the panel's phrases in the language, from the page's own tables (the build's mcp_phrase): the code, else
    its language ("ca"), else a table of that language ("pt" -> "pt_BR"); English when there is none."""
    if not language:
        return en
    data = _panel_tables()
    langs = data.get("langs") or {}
    code = language.replace("-", "_").split(".")[0]
    base = code.split("_")[0].lower()
    pick = code if code in langs else base if base in langs else next(
        (k for k in sorted(langs) if k.split("_")[0].lower() == base), "")
    keys = data.get("keys") or []
    if not pick or en not in keys:
        return en
    table = langs[pick]
    i = keys.index(en)
    return table[i] if isinstance(table, list) and i < len(table) and table[i] else en


# Links the Config tab and the panel may ask the plugin to open (neither page can open anything itself): exactly the
# ones the setup offers, compared whole, so a link that only starts like one is never opened.
OPENABLE = (RELEASES_URL, UV_URL)
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
CAPS = {"restore": False, "kinds": ["setting", "preset", "refused", "slice", "note"], "open_settings": False,
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
        # OrcaSlicer's language for the page ("" = English) and its own words in it (set_language, UI thread).
        self.language, self.words = "", OrcaWords("")
        self._remove_stale()

    def set_language(self, language: str) -> None:
        if language != self.language:
            self.language, self.words = language, OrcaWords(language)

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
            words = self.words
            sources = {r["id"]: {"kind": r["kind"], "label": r["label"], "target": r["target"]}
                       for r in self.hist.rows} if words.available else {}
        for r in page["rows"]:  # R43: a later row, not undone, changed the same setting or preset slot
            r["superseded"] = r["id"] in superseded
            r["dropped"] = r["id"] in dropped
            local = local_words(sources[r["id"]], words) if r["id"] in sources else None
            if local:  # OrcaSlicer's own words, for the page only (GET /activity and the rows stay English)
                r["local"] = local
        caps = dict(CAPS, language=self.language)
        return {"type": "history", "version": PANEL_PROTOCOL_VERSION, "rows": page["rows"],
                "groups": page["groups"], "capabilities": caps, "restored_to": None,
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
.fields{grid-column:1 / 3;display:grid;grid-template-columns:max-content max-content auto;justify-content:start;gap:4px 12px;align-items:center;margin-top:4px}
.fields span{color:var(--muted);font-size:12px} .fields code{white-space:nowrap;padding:1px 6px;
border:1px solid var(--line);border-radius:4px;background:var(--soft);font:12px/1.4 Consolas,"Cascadia Mono",monospace}
.fields .lnk,.fields .done{justify-self:end}
.uvline{margin:8px 0 0}
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
var APPS = __SETUP__, ICON = __ICONS__, UV = __UV__;
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
    var right = r.off || !r.act ? "<span></span>" : (S.done === j && !r.open ? '<span class="done">Copied</span>' :
      '<button class="act' + (r.primary ? " primary" : "") + '" data-r="' + j + '">' + esc(r.act) + "</button>");
    // An app whose form has separate boxes: one line per box, each with its own Copy.
    var fields = r.fields && !r.off ? '<div class="fields">' + r.fields.map(function(f, k){
      var key = j + "." + k;
      return "<span>" + esc(f.label) + "</span><code>" + esc(f.value) + "</code>" + (S.done === key ? '<span class="done">Copied</span>'
        : '<button class="lnk" data-f="' + key + '" aria-label="Copy ' + esc(f.label) + '">Copy</button>');
    }).join("") + "</div>" : "";
    var also = r.also ? "<small>" + esc(r.also) + "</small>" : "";
    var peek = r.copy && !r.off ? '<details class="peek"><summary>' + esc(r.open ? r.copy_label : "Show what it copies") + '</summary><pre>' + esc(r.copy) + "</pre>" +
      (r.open ? '<button class="lnk" data-c="' + j + '">Copy</button>' : "") + "</details>" : "";
    // The plugin opens it; this page never hears back (see when()), so it says what to do if nothing opened.
    var failed = S.opened === j ? '<span class="note" style="grid-column:1 / 3">Opening in your browser. Nothing opened? <button class="lnk" data-l="' + j + '">Copy link</button></span>' : "";
    return '<div class="route' + (r.off ? " off" : "") + '"><div><b>' + esc(r.label) + "</b><small>" + esc(r.hint) + "</small>" + also + "</div>" + right + fields + peek + failed + "</div>";
  }).join("");
  var uv = '<p class="note uvline">Every app needs uv, a small free tool that runs the connector. Install it once. ' +
    (S.opened === "uv" ? "Opening in your browser." : '<button class="lnk" id="getuv">Get uv</button>') + "</p>";
  // One step, no question to ask: the first request from the app is what connects it.
  $("steps").innerHTML = '<span>Set it up where you use ' + esc(app.where) + '</span><div class="routes">' + routes + "</div>" + uv;
  if ($("getuv")) $("getuv").onclick = function(){ var n = (cfg.open && cfg.open.n || 0) + 1; S.opened = "uv"; save({ open: { n: n, url: UV, at: Date.now() / 1000 } }); };
  [].forEach.call($("steps").querySelectorAll("[data-f]"), function(b){ b.onclick = function(){
    var at = b.getAttribute("data-f").split("."), f = app.routes[Number(at[0])].fields[Number(at[1])];
    copyText(f.value, function(ok){ S.done = ok ? b.getAttribute("data-f") : null; draw(); });
  }; });
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
        self._ui_language = None  # OrcaSlicer's language, once read on the UI thread (_language)
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
            .replace("__ICONS__", _inline(SETUP_ICONS)).replace("__UV__", _inline(UV_URL))

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
        self._pump.panel_created()
        panel = orca.host.ui.create_dock_panel(html=panel_page(), title="OrcaSlicer MCP", width=300, height=480,
                                               on_message=self._on_message, on_close=self._on_close,
                                               dock="right")
        trace("create_dock_panel: returned")
        with self._lock:
            self._panel = panel
        # The page also asks with "ready"; whichever arrives after it has loaded sticks.
        self._send_all()
        return panel

    def _language(self) -> str:
        """OrcaSlicer's language, read once on the UI thread (app_info needs it); "" = English."""
        if self._ui_language is None:
            info = app_info()
            if info is not None:
                self._ui_language = ui_language(info)
                trace(f"panel language: {self._ui_language or 'English'} (app_info {info.get('language')!r}, "
                      f"C locale {_c_locale_name()!r})")
        return self._ui_language or ""

    def _send_all(self):
        if self.host is not None:
            self.host.set_language(self._language())
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
            lang = self._language()
            ui.push_notification(ui.NotificationLevel.RegularNotificationLevel,
                                 panel_phrase(PANEL_CLOSED_TEXT, lang), panel_phrase("Undo", lang),
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
    "  .k-plate { color: var(--t-plate); } .k-slice { color: var(--t-slice); } .k-refused { color: var(--warn); } .k-file { color: var(--faint); } .k-note { color: var(--faint); }\n"
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
    "  /* A route whose app has a form of separate boxes: one line per box, each with its own Copy. */\n"
    "  /* The values stay whole (a broken \"orcaslicer-mcp\" reads as two words); a long box name wraps instead. */\n"
    "  .sfields { display: grid; grid-template-columns: minmax(0, 1fr) max-content auto; gap: 4px 8px; align-items: center; }\n"
    "  .sfields > span { color: var(--muted); font-size: 11.5px; line-height: 1.3; }\n"
    "  .sfields code { white-space: nowrap; padding: 1px 5px; border: 1px solid var(--border); border-radius: 3px;\n"
    "    font: 11.5px/1.35 Consolas, \"Cascadia Mono\", Menlo, monospace; }\n"
    "  .sfields .sact, .sfields .sdone { justify-self: end; }\n"
    "  .suv { margin-top: 7px; color: var(--muted); font-size: 11.5px; line-height: 1.4; }\n"
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
    "  <symbol id=\"i-note\" viewBox=\"0 0 24 24\"><path d=\"M5 4h14v12H10l-5 4z\"/><path d=\"M9 9h6M9 12h4\"/></symbol>\n"
    "  <symbol id=\"i-expand\" viewBox=\"0 0 24 24\"><path d=\"M7 9l5-5 5 5M7 15l5 5 5-5\"/></symbol>\n"
    "  <symbol id=\"i-collapse\" viewBox=\"0 0 24 24\"><path d=\"M7 4l5 5 5-5M7 20l5-5 5 5\"/></symbol>\n"
    "  <symbol id=\"i-restore\" viewBox=\"0 0 24 24\"><path d=\"M3 12a9 9 0 1 0 3-6.7\"/><path d=\"M3 4v5h5\"/></symbol>\n"
    "  <symbol id=\"i-gear\" viewBox=\"0 0 24 24\"><path d=\"M9.97 4.68L10.25 1.55A10.6 10.6 0 0 1 13.75 1.55L14.03 4.68A7.6 7.6 0 0 1 15.74 5.39L18.16 3.37A10.6 10.6 0 0 1 20.63 5.84L18.61 8.26A7.6 7.6 0 0 1 19.32 9.97L22.45 10.25A10.6 10.6 0 0 1 22.45 13.75L19.32 14.03A7.6 7.6 0 0 1 18.61 15.74L20.63 18.16A10.6 10.6 0 0 1 18.16 20.63L15.74 18.61A7.6 7.6 0 0 1 14.03 19.32L13.75 22.45A10.6 10.6 0 0 1 10.25 22.45L9.97 19.32A7.6 7.6 0 0 1 8.26 18.61L5.84 20.63A10.6 10.6 0 0 1 3.37 18.16L5.39 15.74A7.6 7.6 0 0 1 4.68 14.03L1.55 13.75A10.6 10.6 0 0 1 1.55 10.25L4.68 9.97A7.6 7.6 0 0 1 5.39 8.26L3.37 5.84A10.6 10.6 0 0 1 5.84 3.37L8.26 5.39A7.6 7.6 0 0 1 9.97 4.68Z\"/><circle cx=\"12\" cy=\"12\" r=\"3.2\"/></symbol>\n"
    "</svg>\n"
    "<div id=\"app\">\n"
    "  <div class=\"ftip\" id=\"ftip\" role=\"tooltip\" hidden></div>\n"
    "  <div class=\"title\"><span id=\"ttl\">Assistant</span><span class=\"sp\"></span><button type=\"button\" class=\"ibtn tip-left\" id=\"fold\"></button><button type=\"button\" class=\"ibtn tip-left\" id=\"gear\"></button></div>\n"
    "  <div class=\"list\" id=\"list\"></div>\n"
    "</div>\n"
    "<script id=\"panel-i18n\">\n"
    "// Made by scripts/mcp_panel_i18n.py from resources/web/orcaslicer_mcp/i18n/*.json: do not edit by hand.\n"
    "// BEGIN PANEL I18N\n"
    "window.McpPanelI18n = {\"keys\": [\"(empty line)\",\"(hidden)\",\"A restore is running\",\"Adaptive layer height\",\"Add model\",\"Add plate\",\"Add to Cursor\",\"Add to VS Code\",\"Add {name}\",\"Added\",\"Added plate\",\"Already back to {before}\",\"Also add {setting} as an environment variable.\",\"An object\",\"Arguments\",\"Arrange\",\"Arranged\",\"Ask it for the print you want. Its changes will appear here, each with Revert.\",\"Assistant\",\"Assistant access is off.\",\"Assistant set {value} · was {before}\",\"Assistant set {value} · you set it back\",\"Assistant settings\",\"Auto-orient\",\"Auto-oriented\",\"Built-in name\",\"Built-in preset\",\"Busy\",\"Busy slicing\",\"Busy with the assistant's request; try again in a moment\",\"Can't change it back\",\"Can't check it now: {reason}\",\"Can't connect: it only reaches servers on the internet, not programs on your computer.\",\"Can't delete it\",\"Can't put it back after a restart; restore an earlier point instead\",\"Can't put the ranges back\",\"Can't set it back\",\"Can't set it back: {reason}\",\"Can't switch back\",\"Can't track this object after the project was reloaded\",\"Cancelled\",\"Change back\",\"Change {label} back to {before}\",\"Changed again since\",\"Changed again since; restore an earlier point instead\",\"ChatGPT desktop app\",\"ChatGPT on the web or phone\",\"Claude app\",\"Clear height ranges\",\"Codex in a terminal or editor\",\"Collapse all\",\"Command to launch\",\"Compared {n} option\",\"Compared {n} option · nothing kept\",\"Compared {n} option · settings may not be back as they were\",\"Compared {n} options\",\"Compared {n} options · nothing kept\",\"Compared {n} options · settings may not be back as they were\",\"Comparison couldn't put back: {labels}\",\"Connect your AI app\",\"Connected\",\"Connected to {app}\",\"Copied\",\"Copy\",\"Copy .vscode/mcp.json settings instead\",\"Copy command\",\"Copy settings\",\"Copy settings instead\",\"Could not restore\",\"Could not revert\",\"Couldn't compute\",\"Couldn't delete\",\"Couldn't delete it\",\"Couldn't move\",\"Couldn't move it back\",\"Couldn't open it\",\"Couldn't open the restore point\",\"Couldn't restore to {time}\",\"Couldn't save\",\"Couldn't save the current state first\",\"Couldn't select\",\"Couldn't undo it\",\"Cursor opens and asks you to confirm. Needs uv.\",\"Delete Plate {n} (only while it is empty)\",\"Delete plate\",\"Delete plate {n}\",\"Delete preset\",\"Delete the {type} preset {name}\",\"Delete {name}\",\"Delete {type} preset\",\"Deleted file\",\"Deleted plate\",\"Deleted preset\",\"Download\",\"Dropped by a preset switch\",\"Dropped by a preset switch; restore an earlier point instead\",\"Duplicate object {n}\",\"Duplicated\",\"Every app needs uv, a small free tool that runs the connector. Install it once.\",\"Expand all\",\"Failed\",\"Filament\",\"Filament preset\",\"Filament trays\",\"Filament {n}\",\"Filament {n} colour\",\"Filament {n} tray\",\"File\",\"File exists\",\"File not found\",\"Files and presets saved to disk stay.\",\"Folder not found\",\"For chats. Download the extension, then open the file. Needs uv.\",\"Get uv\",\"Go back to before the restore\",\"Height range {min}–{max} mm\",\"Height ranges cleared\",\"Hidden value\",\"In a terminal, an editor or the app's Code tab. Needs uv.\",\"In the extension's settings, set OrcaSlicer settings folder to {folder}.\",\"Invalid value\",\"It has objects on it now\",\"It is no longer on the plate\",\"It is no longer there\",\"It is selected; switch to another preset first\",\"It is the last plate\",\"Its objects moved beside the bed\",\"LM Studio, Windsurf and most others take this form. Needs uv.\",\"Last plate\",\"Layer height out of range\",\"Layer height reset\",\"Line {line} of {count}:\",\"Lines {first} to {last} of {count}:\",\"Main menu (beside File) > Plugins > OrcaSlicer MCP > Config\",\"Model\",\"Move back\",\"Move object {n}\",\"Move object {n} to Plate {index}\",\"Move {name}\",\"Move {name} back to {path}\",\"Move {name} back to {plate}\",\"Move {name} back where it was\",\"Moved\",\"Moved again since\",\"Moved to plate\",\"Name\",\"Needs Preferences › Remote API › Allow deleting and replacing project files\",\"Needs Preferences › Remote API › Allow script, G-code and connection edits\",\"Needs Preferences › Remote API › Allow script, G-code and connection edits · {object}\",\"Needs the OrcaSlicer MCP build\",\"Never saved\",\"New project\",\"New {type} preset\",\"No\",\"No CANVAS trays\",\"No changes\",\"No restore point here\",\"No room for another plate\",\"No such change\",\"No such copy\",\"No such filament\",\"No such plate\",\"No such preset\",\"No such restore point\",\"No such tray\",\"Not allowed\",\"Not compatible\",\"Not editable here\",\"Not in the recent list\",\"Not in this project's timeline\",\"Not put back\",\"Not ready\",\"Not stored\",\"Not while OrcaSlicer is busy with a job\",\"Not while OrcaSlicer is closing\",\"Not while a project is loading\",\"Not while slicing\",\"Not while the window is being rebuilt\",\"Note\",\"Nothing moved\",\"Nothing on the plate\",\"Nothing to revert\",\"Nothing to show in the settings\",\"Nothing to undo\",\"OK\",\"Object has no copy\",\"Object not found\",\"Object settings {n}\",\"Off\",\"On\",\"Open project {name}\",\"Open the {settings}\",\"Opened project\",\"Opening…\",\"OrcaSlicer MCP panel closed. Get it back from View > OrcaSlicer MCP panel.\",\"OrcaSlicer MCP panel closed. It comes back when your assistant next uses OrcaSlicer, or press Space and pick OrcaSlicer MCP.\",\"OrcaSlicer reported an error\",\"Other app\",\"Other presets inherit it\",\"Out of date: something changed after this slice\",\"Out of range\",\"Overlaps a range\",\"Plate\",\"Plate locked\",\"Plate {n}\",\"Plate {n} holds other objects now\",\"Preset\",\"Preset in use\",\"Print preset\",\"Printer\",\"Printer preset\",\"Project is open\",\"Put back\",\"Put the height ranges of {object} back\",\"Put the previous layer height profile of {object} back\",\"Put the {n} objects back where they were\",\"Put {name} back on {plate}\",\"Quality {value}\",\"Read-only\",\"Refused\",\"Rejected\",\"Remote API settings\",\"Remove\",\"Remove copy\",\"Remove object {n}\",\"Remove range\",\"Remove the new copy of {name}\",\"Remove the {label} override from {object}\",\"Remove the {min}–{max} mm range from {object}\",\"Remove {name} from the plate\",\"Removed\",\"Renamed\",\"Replaced the saved {type} preset\",\"Reset layer height\",\"Restore point folder\",\"Restore points need the OrcaSlicer MCP build\",\"Restore the project to how it was at {time} (undoes later changes)\",\"Restore to {time}?\",\"Restore undone\",\"Restored to {time}\",\"Restoring…\",\"Revert\",\"Reverted\",\"Reverted at {time}\",\"Reverting…\",\"Rotate back\",\"Rotate object {n}\",\"Rotate {name} back\",\"Rotated\",\"Run this once. The desktop app shares the setting. Needs uv.\",\"Same path\",\"Save project\",\"Save project as {name}\",\"Save {type} preset\",\"Saved preset\",\"Saved project\",\"Scale back\",\"Scale object {n}\",\"Scale {name} back\",\"Scaled\",\"Scripts, custom G-code and printer connection settings are locked. Tick 'Allow script, G-code and connection edits' in Preferences > Remote API to let the assistant change them, or change it yourself here.\",\"Set it up where you use your app\",\"Set it up where you use {app}\",\"Set the {min}–{max} mm range of {object} back to {before}\",\"Set {label} back to {before}\",\"Set {label} on {object} back to {before}\",\"Setting\",\"Settings\",\"Settings › Plugins › Add › Add MCP server, then choose STDIO and fill in each box below. Works in Codex chats; ordinary ChatGPT chats can't use it yet. Needs uv.\",\"Show details\",\"Show what it was and find it in settings\",\"Show what it was and undo it\",\"Show what it was, undo it, and find it in settings\",\"Slice result\",\"Sliced\",\"Sliced {where}\",\"Slicing cancelled\",\"Slicing failed\",\"Something went wrong\",\"Something went wrong: {reason}\",\"Switch back\",\"Switch cancelled\",\"Switch {label} back to {before}\",\"Switch {label} back to {before} (drops unsaved edits)\",\"That filament is gone\",\"The Remote API couldn't start. Check its port.\",\"The Remote API is off.\",\"The copy is gone already\",\"The old folder is gone\",\"The old name is taken now\",\"The plate has changed since; restore an earlier point instead\",\"The plugin did not start\",\"The plugin never changes scripts, G-code or printer connection settings\",\"The project changed since\",\"The restore point can't be read\",\"The restore point is gone\",\"The restore point was changed on disk, so it is not used\",\"The setting is not in this preset any more\",\"The settings tab is not ready\",\"The settings tab is not there\",\"The snapshot before the restore is gone\",\"The snapshot before the restore was changed on disk, so it is not used\",\"Then tell it the print you want. Every change it makes shows up here, and you can undo any of it.\",\"They may still hold the last option's values ({reason}).\",\"Tray not ready\",\"Tray used twice\",\"Undo\",\"Unknown setting\",\"Unsaved changes\",\"Unsaved edits dropped: {list}\",\"Unsupported file\",\"Untitled\",\"VS Code opens and asks you to confirm. Needs uv.\",\"Waiting for your app…\",\"Waiting for {app}…\",\"Was {before}\",\"When an assistant changes something, it shows up here.\",\"Wipe tower X\",\"Wipe tower Y\",\"Wrong number of values\",\"Yes\",\"You changed it since\",\"Your app's MCP settings\",\"Your own changes since then are undone too.\",\"beside the bed\",\"changed by you\",\"changed since\",\"copy removed since\",\"deleted since\",\"dropped by a preset switch\",\"has objects now\",\"layer {height} mm\",\"line {line}: {text} ({count} lines)\",\"moved since\",\"none\",\"not a number\",\"not rotated\",\"not set\",\"nothing kept\",\"old name taken\",\"out of date\",\"plate {n}\",\"plate {n} · {error}\",\"ranges set since\",\"removed since\",\"reverted\",\"set back by you\",\"settings may not be back as they were\",\"the plate\",\"the settings tab is not ready\",\"to {position}\",\"undone by the restore\",\"your app\",\"{angle}° about {axis}\",\"{key}: not a print, filament or printer setting\",\"{key}: unsupported value\",\"{label} (filament {n})\",\"{label} (filaments {list})\",\"{label} must be above {min} (got {got})\",\"{label} must be above {min} (got {got}); the lowest the printer allows is set in Printer Settings > Layer height limits\",\"{label} must be above {min} and at most {max} (got {got})\",\"{label} must be above {min} and at most {max} (got {got}); the lowest the printer allows is set in Printer Settings > Layer height limits\",\"{label} must be at least {min} (got {got})\",\"{label} must be at least {min} (got {got}); the lowest the printer allows is set in Printer Settings > Layer height limits\",\"{label} must be at most {max} (got {got})\",\"{label} must be at most {max} (got {got}); the lowest the printer allows is set in Printer Settings > Layer height limits\",\"{label} must be between {min} and {max} (got {got})\",\"{label} must be between {min} and {max} (got {got}); the lowest the printer allows is set in Printer Settings > Layer height limits\",\"{label} not allowed\",\"{name} doesn't suit this printer now\",\"{name} is gone\",\"{name} is gone already\",\"{name} is no longer on the plate\",\"{name} is no longer there\",\"{name} → {plate}\",\"{name}, now {n} copies\",\"{n} object\",\"{n} objects\",\"{n} range\",\"{n} ranges\",\"{object} is no longer on the plate\",\"{object} only\",\"{object} only · from the print preset\",\"{preset} (unsaved)\",\"{text} ({count} lines)\",\"{title} and {n} more\",\"{value} in {preset}\",\"{verb} (unavailable: {why})\",\"{verb} {what}\",\"{verb}: {label} back to {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"host\": [\"Add {name}\",\"Also add {setting} as an environment variable.\",\"Can't check it now: {reason}\",\"Can't set it back: {reason}\",\"Change {label} back to {before}\",\"Compared {n} option\",\"Compared {n} option · nothing kept\",\"Compared {n} option · settings may not be back as they were\",\"Compared {n} options\",\"Compared {n} options · nothing kept\",\"Compared {n} options · settings may not be back as they were\",\"Comparison couldn't put back: {labels}\",\"Delete Plate {n} (only while it is empty)\",\"Delete plate {n}\",\"Delete the {type} preset {name}\",\"Delete {name}\",\"Delete {type} preset\",\"Duplicate object {n}\",\"Filament {n}\",\"Filament {n} colour\",\"Filament {n} tray\",\"Height range {min}–{max} mm\",\"In the extension's settings, set OrcaSlicer settings folder to {folder}.\",\"Line {line} of {count}:\",\"Lines {first} to {last} of {count}:\",\"Move object {n}\",\"Move object {n} to Plate {index}\",\"Move {name}\",\"Move {name} back to {path}\",\"Move {name} back to {plate}\",\"Move {name} back where it was\",\"Needs Preferences › Remote API › Allow script, G-code and connection edits · {object}\",\"New {type} preset\",\"Object settings {n}\",\"Open project {name}\",\"Plate {n}\",\"Plate {n} holds other objects now\",\"Put the height ranges of {object} back\",\"Put the previous layer height profile of {object} back\",\"Put the {n} objects back where they were\",\"Put {name} back on {plate}\",\"Quality {value}\",\"Remove object {n}\",\"Remove the new copy of {name}\",\"Remove the {label} override from {object}\",\"Remove the {min}–{max} mm range from {object}\",\"Remove {name} from the plate\",\"Replaced the saved {type} preset\",\"Reverted at {time}\",\"Rotate object {n}\",\"Rotate {name} back\",\"Save project as {name}\",\"Save {type} preset\",\"Scale object {n}\",\"Scale {name} back\",\"Set the {min}–{max} mm range of {object} back to {before}\",\"Set {label} back to {before}\",\"Set {label} on {object} back to {before}\",\"Something went wrong: {reason}\",\"Switch {label} back to {before}\",\"Switch {label} back to {before} (drops unsaved edits)\",\"They may still hold the last option's values ({reason}).\",\"Unsaved edits dropped: {list}\",\"layer {height} mm\",\"line {line}: {text} ({count} lines)\",\"plate {n}\",\"plate {n} · {error}\",\"to {position}\",\"{angle}° about {axis}\",\"{key}: not a print, filament or printer setting\",\"{key}: unsupported value\",\"{label} (filament {n})\",\"{label} (filaments {list})\",\"{label} must be above {min} (got {got})\",\"{label} must be above {min} (got {got}); the lowest the printer allows is set in Printer Settings > Layer height limits\",\"{label} must be above {min} and at most {max} (got {got})\",\"{label} must be above {min} and at most {max} (got {got}); the lowest the printer allows is set in Printer Settings > Layer height limits\",\"{label} must be at least {min} (got {got})\",\"{label} must be at least {min} (got {got}); the lowest the printer allows is set in Printer Settings > Layer height limits\",\"{label} must be at most {max} (got {got})\",\"{label} must be at most {max} (got {got}); the lowest the printer allows is set in Printer Settings > Layer height limits\",\"{label} must be between {min} and {max} (got {got})\",\"{label} must be between {min} and {max} (got {got}); the lowest the printer allows is set in Printer Settings > Layer height limits\",\"{name} doesn't suit this printer now\",\"{name} is gone\",\"{name} is gone already\",\"{name} is no longer there\",\"{name} → {plate}\",\"{name}, now {n} copies\",\"{n} object\",\"{n} objects\",\"{n} range\",\"{n} ranges\",\"{object} is no longer on the plate\",\"{object} only\",\"{object} only · from the print preset\",\"{preset} (unsaved)\",\"{text} ({count} lines)\",\"{value} in {preset}\",\"{x} × {y} × {z} mm\"],\n"
    "\"langs\": {\n"
    "\"ca\": [\"(línia buida)\",\"(ocult)\",\"S'està executant una restauració\",\"Alçada de capa adaptativa\",\"Afegir model\",\"Afegir placa\",\"Afegeix a Cursor\",\"Afegeix a VS Code\",\"Afegir {name}\",\"Afegit\",\"Placa afegida\",\"Ja torna a ser {before}\",\"Afegeix també {setting} com a variable d'entorn.\",\"Un objecte\",\"Paràmetres\",\"Ordenar\",\"Ordenat\",\"Demana-li la impressió que vols. Els seus canvis apareixeran aquí, cadascun amb Revertir.\",\"Assistent\",\"L'accés de l'assistent està desactivat.\",\"L'assistent ha establert {value} · era {before}\",\"L'assistent ha establert {value} · ho has revertit\",\"Configuració de l'assistent\",\"Orientar automàticament\",\"Orientat automàticament\",\"Nom integrat\",\"Perfil integrat\",\"Ocupat\",\"Ocupat laminant\",\"Ocupat amb la sol·licitud de l'assistent; torna-ho a provar d'aquí a un moment\",\"No es pot tornar a canviar\",\"Ara no es pot comprovar: {reason}\",\"No es pot connectar: només arriba a servidors d'internet, no a programes del teu ordinador.\",\"No es pot esborrar\",\"No es pot tornar a posar després d'un reinici; restaura un punt anterior\",\"No es poden tornar a posar els rangs\",\"No es pot tornar al valor anterior\",\"No es pot tornar al valor anterior: {reason}\",\"No es pot tornar a l'anterior\",\"No es pot fer el seguiment d'aquest objecte després de tornar a carregar el projecte\",\"Cancel·lat\",\"Tornar a canviar\",\"Tornar {label} a {before}\",\"Canviat de nou des d'aleshores\",\"Canviat de nou des d'aleshores; restaura un punt anterior\",\"Aplicació d'escriptori de ChatGPT\",\"ChatGPT al web o al mòbil\",\"Aplicació Claude\",\"Esborrar els rangs d'alçada\",\"Codex en un terminal o un editor\",\"Replega-ho tot\",\"Ordre per publicar\",\"S'ha comparat {n} opció\",\"S'ha comparat {n} opció · no s'ha conservat res\",\"S'ha comparat {n} opció · és possible que la configuració no hagi tornat a com era\",\"S'han comparat {n} opcions\",\"S'han comparat {n} opcions · no s'ha conservat res\",\"S'han comparat {n} opcions · és possible que la configuració no hagi tornat a com era\",\"La comparació no ha pogut restablir: {labels}\",\"Connecta la teva aplicació d'IA\",\"Connectat\",\"Connectat a {app}\",\"Copiat\",\"Copiar\",\"Copiar la configuració de .vscode/mcp.json\",\"Copiar ordre\",\"Copiar configuració\",\"Copiar la configuració\",\"No s'ha pogut restaurar\",\"No s'ha pogut revertir\",\"No s'ha pogut calcular\",\"No s'ha pogut esborrar\",\"No s'ha pogut esborrar\",\"No s'ha pogut moure\",\"No s'ha pogut tornar a moure\",\"No s'ha pogut obrir\",\"No s'ha pogut obrir el punt de restauració\",\"No s'ha pogut restaurar a les {time}\",\"No s'ha pogut desar\",\"No s'ha pogut desar primer l'estat actual\",\"No s'ha pogut seleccionar\",\"No s'ha pogut desfer\",\"S'obre Cursor i et demana confirmació. Requereix uv.\",\"Esborrar la Placa {n} (només mentre és buida)\",\"Esborrar placa\",\"Esborrar placa {n}\",\"Esborrar perfil\",\"Esborrar el perfil {type} {name}\",\"Esborrar {name}\",\"Esborrar perfil {type}\",\"Fitxer esborrat\",\"Placa esborrada\",\"Perfil esborrat\",\"Descarregar\",\"Descartat per un canvi de perfil\",\"Descartat per un canvi de perfil; restaura un punt anterior\",\"Duplicar l'objecte {n}\",\"Duplicat\",\"Cada aplicació necessita uv, una eina petita i gratuïta que executa el connector. Instal·la-la un cop.\",\"Desplega-ho tot\",\"Ha fallat\",\"Filament\",\"Perfil de filament\",\"Safates de filament\",\"Filament {n}\",\"Color del filament {n}\",\"Safata del filament {n}\",\"Fitxer\",\"El fitxer ja existeix\",\"No s'ha trobat el fitxer\",\"Els fitxers i perfils desats al disc es mantenen.\",\"No s'ha trobat la carpeta\",\"Per a xats. Descarrega l'extensió i obre el fitxer. Requereix uv.\",\"Obtenir uv\",\"Tornar a abans de la restauració\",\"Rang d'alçada {min}–{max} mm\",\"Rangs d'alçada esborrats\",\"Valor ocult\",\"En un terminal, un editor o la pestanya Code de l'aplicació. Requereix uv.\",\"A la configuració de l'extensió, defineix \\\"OrcaSlicer settings folder\\\" com a {folder}.\",\"Valor no vàlid\",\"Ara té objectes\",\"Ja no és a la placa\",\"Ja no hi és\",\"Està seleccionat; canvia primer a un altre perfil\",\"És l'última placa\",\"Els seus objectes s'han mogut al costat del llit\",\"LM Studio, Windsurf i la majoria d'altres admeten aquest format. Requereix uv.\",\"Última placa\",\"Alçada de capa fora de rang\",\"Alçada de capa restablerta\",\"Línia {line} de {count}:\",\"Línies {first} a {last} de {count}:\",\"Menú principal (al costat de Fitxer) > Connectors > OrcaSlicer MCP > Configuració\",\"Model\",\"Tornar a moure\",\"Moure l'objecte {n}\",\"Moure l'objecte {n} a la Placa {index}\",\"Moure {name}\",\"Tornar a moure {name} a {path}\",\"Tornar a moure {name} a {plate}\",\"Tornar {name} on era\",\"Mogut\",\"Mogut de nou des d'aleshores\",\"Mogut a una placa\",\"Nom\",\"Requereix Preferències › Remote API › Allow deleting and replacing project files\",\"Requereix Preferències › Remote API › Allow script, G-code and connection edits\",\"Requereix Preferències › Remote API › Allow script, G-code and connection edits · {object}\",\"Requereix la versió d'OrcaSlicer MCP\",\"Mai desat\",\"Projecte nou\",\"Perfil {type} nou\",\"No\",\"Cap safata CANVAS\",\"Cap canvi\",\"Aquí no hi ha cap punt de restauració\",\"No hi ha lloc per a una altra placa\",\"No existeix aquest canvi\",\"No existeix aquesta còpia\",\"No existeix aquest filament\",\"No existeix aquesta placa\",\"No existeix aquest perfil\",\"No existeix aquest punt de restauració\",\"No existeix aquesta safata\",\"No permès\",\"No compatible\",\"No es pot editar aquí\",\"No és a la llista recent\",\"No és a la cronologia d'aquest projecte\",\"No s'ha tornat a posar\",\"No està a punt\",\"No emmagatzemat\",\"No mentre OrcaSlicer està ocupat amb una tasca\",\"No mentre OrcaSlicer es tanca\",\"No mentre es carrega un projecte\",\"No mentre es lamina\",\"No mentre es reconstrueix la finestra\",\"Nota\",\"No s'ha mogut res\",\"No hi ha res a la placa\",\"No hi ha res per revertir\",\"Res a mostrar a la configuració\",\"Res a desfer\",\"D'acord\",\"L'objecte no té còpia\",\"No s'ha trobat l'objecte\",\"Configuració de l'objecte {n}\",\"Apagat\",\"Encès\",\"Obrir el projecte {name}\",\"Obrir {settings}\",\"Projecte obert\",\"S'està obrint…\",\"S'ha tancat el panell d'OrcaSlicer MCP. Recupera'l des de Vista > OrcaSlicer MCP panel.\",\"S'ha tancat el panell d'OrcaSlicer MCP. Torna quan l'assistent faci servir OrcaSlicer, o prem Espai i tria OrcaSlicer MCP.\",\"OrcaSlicer ha informat d'un error\",\"Una altra aplicació\",\"Altres perfils hereten d'aquest\",\"Obsolet: alguna cosa ha canviat després d'aquest laminat\",\"Fora de rang\",\"Se superposa amb un rang\",\"Placa\",\"Placa bloquejada\",\"Placa {n}\",\"La placa {n} ara conté altres objectes\",\"Perfil\",\"Perfil en ús\",\"Perfil d'impressió\",\"Impressora\",\"Perfil d'impressora\",\"El projecte és obert\",\"Tornar a posar\",\"Tornar a posar els rangs d'alçada de {object}\",\"Tornar a posar el perfil d'alçada de capa anterior de {object}\",\"Tornar a posar els {n} objectes on eren\",\"Tornar a posar {name} a {plate}\",\"Qualitat {value}\",\"Només lectura\",\"Rebutjat\",\"Denegat\",\"Configuració de Remote API\",\"Eliminar\",\"Eliminar còpia\",\"Eliminar l'objecte {n}\",\"Eliminar rang\",\"Eliminar la còpia nova de {name}\",\"Eliminar la substitució de {label} a {object}\",\"Eliminar el rang de {min}–{max} mm de {object}\",\"Eliminar {name} de la placa\",\"Eliminat\",\"Reanomenat\",\"S'ha substituït el perfil {type} desat\",\"Restablir l'alçada de capa\",\"Carpeta del punt de restauració\",\"Els punts de restauració requereixen la versió d'OrcaSlicer MCP\",\"Restaurar el projecte a com era a les {time} (desfà els canvis posteriors)\",\"Restaurar a les {time}?\",\"Restauració desfeta\",\"Restaurat a les {time}\",\"Restaurant…\",\"Revertir\",\"Revertit\",\"Revertit a les {time}\",\"Revertint…\",\"Tornar a rotar\",\"Rotar l'objecte {n}\",\"Tornar a rotar {name}\",\"Rotat\",\"Executa-ho una vegada. L'aplicació d'escriptori comparteix la configuració. Requereix uv.\",\"El mateix camí\",\"Desar projecte\",\"Desar el projecte com a {name}\",\"Desar perfil {type}\",\"Perfil desat\",\"Projecte desat\",\"Tornar a escalar\",\"Escalar l'objecte {n}\",\"Tornar a escalar {name}\",\"Escalat\",\"Els scripts, el G-code personalitzat i la configuració de connexió de la impressora estan bloquejats. Marca 'Allow script, G-code and connection edits' a Preferències > Remote API perquè l'assistent els pugui canviar, o canvia'ls tu mateix aquí.\",\"Configura-ho allà on fas servir la teva aplicació\",\"Configura-ho allà on fas servir {app}\",\"Tornar el rang de {min}–{max} mm de {object} a {before}\",\"Tornar {label} a {before}\",\"Tornar {label} de {object} a {before}\",\"Configuració\",\"Configuració\",\"Ajustaments › Connectors › Afegeix › Afegeix un servidor MCP, tria STDIO i omple cada camp de sota. Funciona als xats de Codex; els xats normals de ChatGPT encara no el poden fer servir. Requereix uv.\",\"Mostra detalls\",\"Mostra què era i troba-ho a la configuració\",\"Mostra què era i desfés-ho\",\"Mostra què era, desfés-ho i troba-ho a la configuració\",\"Resultat del laminat\",\"Laminat\",\"Laminat {where}\",\"Laminat cancel·lat\",\"El laminat ha fallat\",\"Alguna cosa ha anat malament\",\"Alguna cosa ha anat malament: {reason}\",\"Tornar a l'anterior\",\"Canvi cancel·lat\",\"Tornar {label} a {before}\",\"Tornar {label} a {before} (descarta les edicions sense desar)\",\"Aquest filament ja no existeix\",\"Remote API no s'ha pogut iniciar. Comprova'n el port.\",\"Remote API està desactivada.\",\"La còpia ja no hi és\",\"La carpeta antiga ja no hi és\",\"El nom antic ja està ocupat\",\"La placa ha canviat des d'aleshores; restaura un punt anterior\",\"El connector no s'ha iniciat\",\"El connector mai canvia scripts, G-code ni la configuració de connexió de la impressora\",\"El projecte ha canviat des d'aleshores\",\"No es pot llegir el punt de restauració\",\"El punt de restauració ja no hi és\",\"El punt de restauració s'ha modificat al disc, així que no s'utilitza\",\"Aquesta configuració ja no és en aquest perfil\",\"La pestanya de configuració no està a punt\",\"La pestanya de configuració no hi és\",\"La instantània d'abans de la restauració ja no hi és\",\"La instantània d'abans de la restauració s'ha modificat al disc, així que no s'utilitza\",\"Després digues-li la impressió que vols. Cada canvi que faci apareixerà aquí i el podràs desfer.\",\"Potser encara conserven els valors de l'última opció ({reason}).\",\"Safata no preparada\",\"Safata utilitzada dues vegades\",\"Desfer\",\"Configuració desconeguda\",\"Canvis sense desar\",\"Edicions sense desar descartades: {list}\",\"Fitxer no admès\",\"Sense títol\",\"S'obre VS Code i et demana confirmació. Requereix uv.\",\"Esperant la teva aplicació…\",\"Esperant {app}…\",\"Era {before}\",\"Quan un assistent canvia alguna cosa, apareix aquí.\",\"Torre de Purga X\",\"Torre de Purga Y\",\"Nombre de valors incorrecte\",\"Sí\",\"L'has canviat des d'aleshores\",\"Configuració MCP de la teva aplicació\",\"Els teus propis canvis des d'aleshores també es desfan.\",\"al costat del llit\",\"canviat per tu\",\"canviat des d'aleshores\",\"còpia eliminada des d'aleshores\",\"esborrat des d'aleshores\",\"descartat per un canvi de perfil\",\"ara té objectes\",\"capa {height} mm\",\"línia {line}: {text} ({count} línies)\",\"mogut des d'aleshores\",\"cap\",\"no és un número\",\"no rotat\",\"no definit\",\"no s'ha conservat res\",\"nom antic ocupat\",\"obsolet\",\"placa {n}\",\"placa {n} · {error}\",\"rangs establerts des d'aleshores\",\"eliminat des d'aleshores\",\"revertit\",\"tornat enrere per tu\",\"és possible que la configuració no hagi tornat a com era\",\"la placa\",\"la pestanya de configuració no està a punt\",\"a {position}\",\"desfet per la restauració\",\"la teva aplicació\",\"{angle}° al voltant de {axis}\",\"{key}: no és una configuració d'impressió, de filament ni d'impressora\",\"{key}: valor no admès\",\"{label} (filament {n})\",\"{label} (filaments {list})\",\"{label} ha de ser superior a {min} (s'ha rebut {got})\",\"{label} ha de ser superior a {min} (s'ha rebut {got}); el mínim que permet la impressora s'estableix a Configuració de la impressora > Límits d'alçada de capa\",\"{label} ha de ser superior a {min} i com a màxim {max} (s'ha rebut {got})\",\"{label} ha de ser superior a {min} i com a màxim {max} (s'ha rebut {got}); el mínim que permet la impressora s'estableix a Configuració de la impressora > Límits d'alçada de capa\",\"{label} ha de ser com a mínim {min} (s'ha rebut {got})\",\"{label} ha de ser com a mínim {min} (s'ha rebut {got}); el mínim que permet la impressora s'estableix a Configuració de la impressora > Límits d'alçada de capa\",\"{label} ha de ser com a màxim {max} (s'ha rebut {got})\",\"{label} ha de ser com a màxim {max} (s'ha rebut {got}); el mínim que permet la impressora s'estableix a Configuració de la impressora > Límits d'alçada de capa\",\"{label} ha d'estar entre {min} i {max} (s'ha rebut {got})\",\"{label} ha d'estar entre {min} i {max} (s'ha rebut {got}); el mínim que permet la impressora s'estableix a Configuració de la impressora > Límits d'alçada de capa\",\"{label} no permès\",\"{name} ja no és adequat per a aquesta impressora\",\"{name} ja no hi és\",\"{name} ja ha desaparegut\",\"{name} ja no és a la placa\",\"{name} ja no és allà\",\"{name} → {plate}\",\"{name}, ara {n} còpies\",\"{n} objecte\",\"{n} objectes\",\"{n} rang\",\"{n} rangs\",\"{object} ja no és a la placa\",\"Només {object}\",\"Només {object} · del perfil d'impressió\",\"{preset} (sense desar)\",\"{text} ({count} línies)\",\"{title} i {n} més\",\"{value} a {preset}\",\"{verb} (no disponible: {why})\",\"{verb} {what}\",\"{verb}: {label} torna a {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"cs\": [\"(prázdný řádek)\",\"(skryto)\",\"Probíhá obnovení\",\"Adaptivní výška vrstvy\",\"Přidat model\",\"Přidat desku\",\"Přidat do Cursoru\",\"Přidat do VS Code\",\"Přidat {name}\",\"Přidáno\",\"Deska přidána\",\"Už je zpět na {before}\",\"Přidejte také {setting} jako proměnnou prostředí.\",\"Objekt\",\"Argumenty\",\"Rozložit\",\"Rozloženo\",\"Popište asistentovi tisk, který chcete. Jeho změny se zobrazí zde, každá s tlačítkem Vrátit změnu.\",\"Asistent\",\"Přístup asistenta je vypnutý.\",\"Asistent nastavil {value} · bylo {before}\",\"Asistent nastavil {value} · vrátili jste to zpět\",\"Nastavení asistenta\",\"Automaticky orientovat\",\"Automaticky zorientováno\",\"Vestavěný název\",\"Vestavěná předvolba\",\"Zaneprázdněno\",\"Probíhá slicování\",\"Zpracovává se požadavek asistenta; zkuste to za chvíli znovu\",\"Nelze vrátit zpět\",\"Nyní nelze zkontrolovat: {reason}\",\"Nelze se připojit: dosáhne jen na servery na internetu, ne na programy ve vašem počítači.\",\"Nelze smazat\",\"Po restartu nelze vrátit zpět; obnovte raději starší bod\",\"Nelze vrátit rozsahy zpět\",\"Nelze vrátit zpět\",\"Nelze vrátit zpět: {reason}\",\"Nelze přepnout zpět\",\"Po opětovném načtení projektu nelze tento objekt sledovat\",\"Zrušeno\",\"Vrátit hodnotu\",\"Vrátit {label} na {before}\",\"Od té doby znovu změněno\",\"Od té doby znovu změněno; obnovte raději starší bod\",\"Desktopová aplikace ChatGPT\",\"ChatGPT na webu nebo v telefonu\",\"Aplikace Claude\",\"Vymazat rozsahy výšky\",\"Codex v terminálu nebo editoru\",\"Sbalit vše\",\"Příkaz ke spuštění\",\"Porovnána {n} možnost\",\"Porovnána {n} možnost · nic nezachováno\",\"Porovnána {n} možnost · nastavení se nemusela vrátit do původního stavu\",\"Porovnáno možností: {n}\",\"Porovnáno možností: {n} · nic nezachováno\",\"Porovnáno možností: {n} · nastavení se nemusela vrátit do původního stavu\",\"Porovnání nemohlo vrátit: {labels}\",\"Připojte svou AI aplikaci\",\"Připojeno\",\"Připojeno k {app}\",\"Zkopírováno\",\"Kopírovat\",\"Místo toho kopírovat nastavení .vscode/mcp.json\",\"Kopírovat příkaz\",\"Kopírovat nastavení\",\"Místo toho kopírovat nastavení\",\"Nelze obnovit\",\"Nelze vrátit změnu\",\"Nelze vypočítat\",\"Nelze smazat\",\"Nelze to smazat\",\"Nelze přesunout\",\"Nelze přesunout zpět\",\"Nelze otevřít\",\"Nelze otevřít bod obnovení\",\"Nelze obnovit stav z {time}\",\"Nelze uložit\",\"Nelze nejdřív uložit aktuální stav\",\"Nelze vybrat\",\"Nelze vzít zpět\",\"Cursor se otevře a požádá vás o potvrzení. Vyžaduje uv.\",\"Smazat desku {n} (jen když je prázdná)\",\"Smazat desku\",\"Smazat desku {n}\",\"Smazat předvolbu\",\"Smazat předvolbu {type} {name}\",\"Smazat {name}\",\"Smazat předvolbu {type}\",\"Soubor smazán\",\"Deska smazána\",\"Předvolba smazána\",\"Stáhnout\",\"Zahozeno přepnutím předvolby\",\"Zahozeno přepnutím předvolby; obnovte raději starší bod\",\"Duplikovat objekt {n}\",\"Duplikováno\",\"Každá aplikace potřebuje uv, malý bezplatný nástroj, který spouští konektor. Stačí ho nainstalovat jednou.\",\"Rozbalit vše\",\"Selhalo\",\"Filament\",\"Předvolba filamentu\",\"Zásobníky filamentu\",\"Filament {n}\",\"Barva filamentu {n}\",\"Zásobník filamentu {n}\",\"Soubor\",\"Soubor už existuje\",\"Soubor nenalezen\",\"Soubory a předvolby uložené na disk zůstanou.\",\"Složka nenalezena\",\"Pro chaty. Stáhněte rozšíření a poté otevřete soubor. Vyžaduje uv.\",\"Získat uv\",\"Vrátit se do stavu před obnovením\",\"Rozsah výšky {min}–{max} mm\",\"Rozsahy výšky vymazány\",\"Skrytá hodnota\",\"V terminálu, editoru nebo na kartě Code v aplikaci. Vyžaduje uv.\",\"V nastavení rozšíření nastavte „OrcaSlicer settings folder“ na {folder}.\",\"Neplatná hodnota\",\"Nyní jsou na ní objekty\",\"Už není na desce\",\"Už tam není\",\"Je vybrána; nejdřív přepněte na jinou předvolbu\",\"Je to poslední deska\",\"Její objekty byly přesunuty vedle podložky\",\"LM Studio, Windsurf a většina dalších používají tuto formu. Vyžaduje uv.\",\"Poslední deska\",\"Výška vrstvy mimo rozsah\",\"Výška vrstvy obnovena\",\"Řádek {line} z {count}:\",\"Řádky {first} až {last} z {count}:\",\"Hlavní nabídka (vedle Soubor) > Pluginy > OrcaSlicer MCP > Konfigurace\",\"Model\",\"Přesunout zpět\",\"Přesunout objekt {n}\",\"Přesunout objekt {n} na desku {index}\",\"Přesunout {name}\",\"Přesunout {name} zpět do {path}\",\"Přesunout {name} zpět na {plate}\",\"Přesunout {name} zpět na původní místo\",\"Přesunuto\",\"Od té doby znovu přesunuto\",\"Přesunuto na desku\",\"Název\",\"Vyžaduje Předvolby › Remote API › Allow deleting and replacing project files\",\"Vyžaduje Předvolby › Remote API › Allow script, G-code and connection edits\",\"Vyžaduje Předvolby › Remote API › Allow script, G-code and connection edits · {object}\",\"Vyžaduje sestavení OrcaSlicer MCP\",\"Nikdy neuloženo\",\"Nový projekt\",\"Nová předvolba {type}\",\"Ne\",\"Žádné zásobníky CANVAS\",\"Žádné změny\",\"Zde není žádný bod obnovení\",\"Není místo pro další desku\",\"Taková změna neexistuje\",\"Taková kopie neexistuje\",\"Takový filament neexistuje\",\"Taková deska neexistuje\",\"Taková předvolba neexistuje\",\"Takový bod obnovení neexistuje\",\"Takový zásobník neexistuje\",\"Nepovoleno\",\"Nekompatibilní\",\"Zde nelze upravit\",\"Není v seznamu nedávných\",\"Není v historii tohoto projektu\",\"Nevráceno zpět\",\"Nepřipraveno\",\"Neuloženo\",\"Ne, dokud OrcaSlicer zpracovává úlohu\",\"Ne, když se OrcaSlicer zavírá\",\"Ne, dokud se načítá projekt\",\"Ne během slicování\",\"Ne, dokud se okno znovu vytváří\",\"Poznámka\",\"Nic se nepřesunulo\",\"Na desce nic není\",\"Není co vracet\",\"V nastavení není co zobrazit\",\"Není co vrátit zpět\",\"OK\",\"Objekt nemá kopii\",\"Objekt nenalezen\",\"Nastavení objektu {n}\",\"Vypnuto\",\"Zapnuto\",\"Otevřít projekt {name}\",\"Otevřít {settings}\",\"Projekt otevřen\",\"Otevírání…\",\"Panel OrcaSlicer MCP je zavřen. Znovu ho zobrazíte přes Zobrazit > OrcaSlicer MCP panel.\",\"Panel OrcaSlicer MCP je zavřen. Znovu se objeví, až váš asistent příště použije OrcaSlicer, nebo stiskněte mezerník a vyberte OrcaSlicer MCP.\",\"OrcaSlicer ohlásil chybu\",\"Jiná aplikace\",\"Dědí ho jiné předvolby\",\"Zastaralé: po tomto slicování se něco změnilo\",\"Mimo rozsah\",\"Překrývá se s rozsahem\",\"Deska\",\"Deska zamčena\",\"Deska {n}\",\"Deska {n} nyní obsahuje jiné objekty\",\"Předvolba\",\"Předvolba se používá\",\"Předvolba tisku\",\"Tiskárna\",\"Předvolba tiskárny\",\"Projekt je otevřen\",\"Vrátit zpět\",\"Vrátit rozsahy výšky objektu {object}\",\"Vrátit předchozí profil výšky vrstvy objektu {object}\",\"Vrátit objekty na původní místo (počet: {n})\",\"Vrátit {name} na {plate}\",\"Kvalita {value}\",\"Jen pro čtení\",\"Odmítnuto\",\"Zamítnuto\",\"Nastavení Remote API\",\"Odstranit\",\"Odstranit kopii\",\"Odstranit objekt {n}\",\"Odstranit rozsah\",\"Odstranit novou kopii objektu {name}\",\"Odstranit přepsání {label} z objektu {object}\",\"Odstranit rozsah {min}–{max} mm z objektu {object}\",\"Odstranit {name} z desky\",\"Odstraněno\",\"Přejmenováno\",\"Uložená předvolba {type} nahrazena\",\"Obnovit výšku vrstvy\",\"Složka bodů obnovení\",\"Body obnovení vyžadují sestavení OrcaSlicer MCP\",\"Obnovit projekt do stavu z {time} (vrátí pozdější změny)\",\"Obnovit stav z {time}?\",\"Obnovení vráceno\",\"Obnoveno do stavu z {time}\",\"Obnovování…\",\"Vrátit změnu\",\"Změna vrácena\",\"Změna vrácena v {time}\",\"Vracení změny…\",\"Otočit zpět\",\"Otočit objekt {n}\",\"Otočit {name} zpět\",\"Otočeno\",\"Spusťte to jednou. Desktopová aplikace nastavení sdílí. Vyžaduje uv.\",\"Stejná cesta\",\"Uložit projekt\",\"Uložit projekt jako {name}\",\"Uložit předvolbu {type}\",\"Předvolba uložena\",\"Projekt uložen\",\"Vrátit měřítko\",\"Změnit měřítko objektu {n}\",\"Vrátit měřítko objektu {name}\",\"Měřítko změněno\",\"Skripty, vlastní G-code a nastavení připojení tiskárny jsou uzamčeny. Zaškrtněte 'Allow script, G-code and connection edits' v Předvolby > Remote API, aby je asistent mohl měnit, nebo je změňte sami zde.\",\"Nastavte to ve vaší aplikaci\",\"Nastavte to v aplikaci {app}\",\"Vrátit rozsah {min}–{max} mm objektu {object} na {before}\",\"Vrátit {label} na {before}\",\"Vrátit {label} u objektu {object} na {before}\",\"Nastavení\",\"Nastavení\",\"Nastavení › Pluginy › Přidat › Přidat server MCP, vyberte STDIO a vyplňte každé pole níže. Funguje v chatech Codexu; běžné chaty ChatGPT to zatím používat nemohou. Vyžaduje uv.\",\"Zobrazit podrobnosti\",\"Zobrazit původní hodnotu a najít ji v nastavení\",\"Zobrazit původní hodnotu a vrátit ji zpět\",\"Zobrazit původní hodnotu, vrátit ji zpět a najít ji v nastavení\",\"Výsledek slicování\",\"Naslicováno\",\"Naslicováno {where}\",\"Slicování zrušeno\",\"Slicování selhalo\",\"Něco se pokazilo\",\"Něco se pokazilo: {reason}\",\"Přepnout zpět\",\"Přepnutí zrušeno\",\"Přepnout {label} zpět na {before}\",\"Přepnout {label} zpět na {before} (zahodí neuložené úpravy)\",\"Ten filament už neexistuje\",\"Remote API se nepodařilo spustit. Zkontrolujte její port.\",\"Remote API je vypnuté.\",\"Kopie už neexistuje\",\"Stará složka už neexistuje\",\"Starý název je nyní obsazený\",\"Deska se od té doby změnila; obnovte raději starší bod\",\"Plugin se nespustil\",\"Plugin nikdy nemění skripty, G-code ani nastavení připojení tiskárny\",\"Projekt se od té doby změnil\",\"Bod obnovení nelze přečíst\",\"Bod obnovení už neexistuje\",\"Bod obnovení byl změněn na disku, proto se nepoužije\",\"Nastavení už v této předvolbě není\",\"Karta nastavení není připravena\",\"Karta nastavení tu není\",\"Snímek před obnovením už neexistuje\",\"Snímek před obnovením byl změněn na disku, proto se nepoužije\",\"Potom mu řekněte, jaký tisk chcete. Každá jeho změna se zobrazí zde a kteroukoli můžete vrátit zpět.\",\"Mohou stále obsahovat hodnoty poslední možnosti ({reason}).\",\"Zásobník není připraven\",\"Zásobník použit dvakrát\",\"Zpět\",\"Neznámé nastavení\",\"Neuložené změny\",\"Neuložené úpravy zahozeny: {list}\",\"Nepodporovaný soubor\",\"Bez názvu\",\"VS Code se otevře a požádá vás o potvrzení. Vyžaduje uv.\",\"Čekání na vaši aplikaci…\",\"Čekání na {app}…\",\"Bylo {before}\",\"Když asistent něco změní, zobrazí se to zde.\",\"Věž na očištění trysky X\",\"Věž na očištění trysky Y\",\"Nesprávný počet hodnot\",\"Ano\",\"Od té doby jste to změnili\",\"Nastavení MCP ve vaší aplikaci\",\"Vaše vlastní pozdější změny se také vrátí zpět.\",\"vedle podložky\",\"změněno vámi\",\"od té doby změněno\",\"kopie od té doby odstraněna\",\"od té doby smazáno\",\"zahozeno přepnutím předvolby\",\"nyní má objekty\",\"vrstva {height} mm\",\"řádek {line}: {text} (řádků: {count})\",\"od té doby přesunuto\",\"žádné\",\"není číslo\",\"neotočeno\",\"nenastaveno\",\"nic nezachováno\",\"starý název obsazen\",\"zastaralé\",\"deska {n}\",\"deska {n} · {error}\",\"rozsahy od té doby nastaveny\",\"od té doby odstraněno\",\"vráceno\",\"vráceno vámi\",\"nastavení se nemusela vrátit do původního stavu\",\"deska\",\"karta nastavení není připravena\",\"na {position}\",\"vráceno obnovením\",\"vaše aplikace\",\"{angle}° kolem {axis}\",\"{key}: není nastavení tisku, filamentu ani tiskárny\",\"{key}: nepodporovaná hodnota\",\"{label} (filament {n})\",\"{label} (filamenty {list})\",\"{label} musí být větší než {min} (zadáno {got})\",\"{label} musí být větší než {min} (zadáno {got}); nejnižší hodnota, kterou tiskárna povolí, se nastavuje v Nastavení tiskárny > Omezení výšky vrstvy\",\"{label} musí být větší než {min} a nejvýše {max} (zadáno {got})\",\"{label} musí být větší než {min} a nejvýše {max} (zadáno {got}); nejnižší hodnota, kterou tiskárna povolí, se nastavuje v Nastavení tiskárny > Omezení výšky vrstvy\",\"{label} musí být alespoň {min} (zadáno {got})\",\"{label} musí být alespoň {min} (zadáno {got}); nejnižší hodnota, kterou tiskárna povolí, se nastavuje v Nastavení tiskárny > Omezení výšky vrstvy\",\"{label} musí být nejvýše {max} (zadáno {got})\",\"{label} musí být nejvýše {max} (zadáno {got}); nejnižší hodnota, kterou tiskárna povolí, se nastavuje v Nastavení tiskárny > Omezení výšky vrstvy\",\"{label} musí být mezi {min} a {max} (zadáno {got})\",\"{label} musí být mezi {min} a {max} (zadáno {got}); nejnižší hodnota, kterou tiskárna povolí, se nastavuje v Nastavení tiskárny > Omezení výšky vrstvy\",\"{label} není povoleno\",\"{name} nyní nevyhovuje této tiskárně\",\"{name} už neexistuje\",\"{name} už neexistuje\",\"{name} už není na desce\",\"{name} tam už není\",\"{name} → {plate}\",\"{name}, nyní kopií: {n}\",\"{n} objekt\",\"počet objektů: {n}\",\"{n} rozsah\",\"počet rozsahů: {n}\",\"{object} už není na desce\",\"jen {object}\",\"jen {object} · z předvolby tisku\",\"{preset} (neuloženo)\",\"{text} (řádků: {count})\",\"{title} a ještě {n}\",\"{value} v {preset}\",\"{verb} (nedostupné: {why})\",\"{verb} {what}\",\"{verb}: {label} zpět na {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"de\": [\"(leere Zeile)\",\"(verborgen)\",\"Eine Wiederherstellung läuft\",\"Adaptive Schichthöhe\",\"Modell hinzufügen\",\"Platte hinzufügen\",\"Zu Cursor hinzufügen\",\"Zu VS Code hinzufügen\",\"{name} hinzufügen\",\"Hinzugefügt\",\"Platte hinzugefügt\",\"Bereits wieder {before}\",\"Fügen Sie außerdem {setting} als Umgebungsvariable hinzu.\",\"Ein Objekt\",\"Argumente\",\"Anordnen\",\"Angeordnet\",\"Bitten Sie den Assistenten um den gewünschten Druck. Seine Änderungen erscheinen hier, jeweils mit Zurücksetzen.\",\"Assistent\",\"Der Zugriff des Assistenten ist aus.\",\"Assistent setzte {value} · war {before}\",\"Assistent setzte {value} · von Ihnen zurückgesetzt\",\"Assistenten-Einstellungen\",\"Automatisch ausrichten\",\"Automatisch ausgerichtet\",\"Integrierter Name\",\"Integriertes Profil\",\"Beschäftigt\",\"Slicen läuft\",\"Beschäftigt mit der Anfrage des Assistenten; gleich noch einmal versuchen\",\"Kann nicht zurückgeändert werden\",\"Kann jetzt nicht geprüft werden: {reason}\",\"Verbindung nicht möglich: Es erreicht nur Server im Internet, keine Programme auf Ihrem Computer.\",\"Kann nicht gelöscht werden\",\"Nach einem Neustart nicht mehr zurücklegbar; stattdessen einen früheren Stand wiederherstellen\",\"Bereiche können nicht zurückgesetzt werden\",\"Kann nicht zurückgesetzt werden\",\"Kann nicht zurückgesetzt werden: {reason}\",\"Kann nicht zurückgewechselt werden\",\"Dieses Objekt lässt sich nach dem erneuten Laden des Projekts nicht mehr verfolgen\",\"Abgebrochen\",\"Zurückändern\",\"{label} zurück auf {before} ändern\",\"Seitdem erneut geändert\",\"Seitdem erneut geändert; stattdessen einen früheren Stand wiederherstellen\",\"ChatGPT-Desktop-App\",\"ChatGPT im Web oder auf dem Smartphone\",\"Claude-App\",\"Höhenbereiche löschen\",\"Codex in einem Terminal oder Editor\",\"Alle einklappen\",\"Befehl zum Starten\",\"{n} Option verglichen\",\"{n} Option verglichen · nichts behalten\",\"{n} Option verglichen · Einstellungen sind evtl. nicht wie zuvor\",\"{n} Optionen verglichen\",\"{n} Optionen verglichen · nichts behalten\",\"{n} Optionen verglichen · Einstellungen sind evtl. nicht wie zuvor\",\"Der Vergleich konnte nicht zurücksetzen: {labels}\",\"KI-App verbinden\",\"Verbunden\",\"Verbunden mit {app}\",\"Kopiert\",\"Kopieren\",\"Stattdessen .vscode/mcp.json-Einstellungen kopieren\",\"Befehl kopieren\",\"Einstellungen kopieren\",\"Stattdessen Einstellungen kopieren\",\"Wiederherstellen fehlgeschlagen\",\"Zurücksetzen fehlgeschlagen\",\"Berechnung fehlgeschlagen\",\"Löschen fehlgeschlagen\",\"Konnte nicht gelöscht werden\",\"Verschieben fehlgeschlagen\",\"Konnte nicht zurückbewegt werden\",\"Konnte nicht geöffnet werden\",\"Der Wiederherstellungspunkt konnte nicht geöffnet werden\",\"Wiederherstellung auf {time} fehlgeschlagen\",\"Speichern fehlgeschlagen\",\"Der aktuelle Stand konnte vorher nicht gespeichert werden\",\"Auswahl fehlgeschlagen\",\"Konnte nicht rückgängig gemacht werden\",\"Cursor öffnet sich und bittet um Bestätigung. Benötigt uv.\",\"Platte {n} löschen (nur solange sie leer ist)\",\"Platte löschen\",\"Platte {n} löschen\",\"Profil löschen\",\"Das {type}-Profil {name} löschen\",\"{name} löschen\",\"{type}-Profil löschen\",\"Datei gelöscht\",\"Platte gelöscht\",\"Profil gelöscht\",\"Herunterladen\",\"Durch einen Profilwechsel verworfen\",\"Durch einen Profilwechsel verworfen; stattdessen einen früheren Stand wiederherstellen\",\"Objekt {n} duplizieren\",\"Dupliziert\",\"Jede App braucht uv, ein kleines kostenloses Werkzeug, das den Connector ausführt. Einmal installieren genügt.\",\"Alle ausklappen\",\"Fehlgeschlagen\",\"Filament\",\"Filament-Profil\",\"Filament-Fächer\",\"Filament {n}\",\"Farbe von Filament {n}\",\"Fach für Filament {n}\",\"Datei\",\"Datei existiert bereits\",\"Datei nicht gefunden\",\"Auf der Festplatte gespeicherte Dateien und Profile bleiben erhalten.\",\"Ordner nicht gefunden\",\"Für Chats. Laden Sie die Erweiterung herunter und öffnen Sie dann die Datei. Benötigt uv.\",\"uv holen\",\"Zurück zum Stand vor der Wiederherstellung\",\"Höhenbereich {min}–{max} mm\",\"Höhenbereiche gelöscht\",\"Verborgener Wert\",\"In einem Terminal, einem Editor oder im Code-Tab der App. Benötigt uv.\",\"Setzen Sie in den Einstellungen der Erweiterung „OrcaSlicer settings folder“ auf {folder}.\",\"Ungültiger Wert\",\"Es befinden sich jetzt Objekte darauf\",\"Es liegt nicht mehr auf der Platte\",\"Es ist nicht mehr vorhanden\",\"Es ist ausgewählt; zuerst zu einem anderen Profil wechseln\",\"Es ist die letzte Platte\",\"Seine Objekte wurden neben das Druckbett verschoben\",\"LM Studio, Windsurf und die meisten anderen verwenden diese Form. Benötigt uv.\",\"Letzte Platte\",\"Schichthöhe außerhalb des Bereichs\",\"Schichthöhe zurückgesetzt\",\"Zeile {line} von {count}:\",\"Zeilen {first} bis {last} von {count}:\",\"Hauptmenü (neben Datei) > Plugins > OrcaSlicer MCP > Konfiguration\",\"Modell\",\"Zurückbewegen\",\"Objekt {n} bewegen\",\"Objekt {n} auf Platte {index} bewegen\",\"{name} bewegen\",\"{name} zurück nach {path} bewegen\",\"{name} zurück auf {plate} bewegen\",\"{name} dorthin zurückbewegen, wo es war\",\"Bewegt\",\"Seitdem erneut bewegt\",\"Auf Platte bewegt\",\"Name\",\"Erfordert Einstellungen › Remote API › Allow deleting and replacing project files\",\"Erfordert Einstellungen › Remote API › Allow script, G-code and connection edits\",\"Erfordert Einstellungen › Remote API › Allow script, G-code and connection edits · {object}\",\"Benötigt den OrcaSlicer-MCP-Build\",\"Nie gespeichert\",\"Neues Projekt\",\"Neues {type}-Profil\",\"Nein\",\"Keine CANVAS-Fächer\",\"Keine Änderungen\",\"Hier gibt es keinen Wiederherstellungspunkt\",\"Kein Platz für eine weitere Platte\",\"Keine solche Änderung\",\"Keine solche Kopie\",\"Kein solches Filament\",\"Keine solche Platte\",\"Kein solches Profil\",\"Kein solcher Wiederherstellungspunkt\",\"Kein solches Fach\",\"Nicht erlaubt\",\"Nicht kompatibel\",\"Hier nicht bearbeitbar\",\"Nicht in der Liste zuletzt verwendeter Projekte\",\"Nicht im Verlauf dieses Projekts\",\"Nicht zurückgesetzt\",\"Nicht bereit\",\"Nicht gespeichert\",\"Nicht, solange OrcaSlicer mit einer Aufgabe beschäftigt ist\",\"Nicht, solange OrcaSlicer geschlossen wird\",\"Nicht, solange ein Projekt geladen wird\",\"Nicht während des Slicens\",\"Nicht, solange das Fenster neu aufgebaut wird\",\"Notiz\",\"Nichts bewegt\",\"Nichts auf der Platte\",\"Nichts zum Zurücksetzen\",\"Nichts in den Einstellungen anzuzeigen\",\"Nichts rückgängig zu machen\",\"OK\",\"Das Objekt hat keine Kopie\",\"Objekt nicht gefunden\",\"Objekteinstellungen {n}\",\"Aus\",\"An\",\"Projekt {name} öffnen\",\"{settings} öffnen\",\"Projekt geöffnet\",\"Wird geöffnet…\",\"Das OrcaSlicer-MCP-Panel wurde geschlossen. Es lässt sich über Ansicht > OrcaSlicer MCP panel wieder öffnen.\",\"Das OrcaSlicer-MCP-Panel wurde geschlossen. Es erscheint wieder, wenn Ihr Assistent das nächste Mal OrcaSlicer nutzt, oder drücken Sie die Leertaste und wählen Sie OrcaSlicer MCP.\",\"OrcaSlicer hat einen Fehler gemeldet\",\"Andere App\",\"Andere Profile erben es\",\"Veraltet: Nach diesem Slice hat sich etwas geändert\",\"Außerhalb des Bereichs\",\"Überschneidet sich mit einem Bereich\",\"Platte\",\"Platte gesperrt\",\"Platte {n}\",\"Platte {n} enthält jetzt andere Objekte\",\"Profil\",\"Profil in Verwendung\",\"Prozessprofil\",\"Drucker\",\"Druckerprofil\",\"Projekt ist geöffnet\",\"Zurücklegen\",\"Die Höhenbereiche von {object} zurücksetzen\",\"Das vorherige Schichthöhenprofil von {object} zurücksetzen\",\"Die {n} Objekte dorthin zurückbewegen, wo sie waren\",\"{name} zurück auf {plate} legen\",\"Qualität {value}\",\"Schreibgeschützt\",\"Abgelehnt\",\"Zurückgewiesen\",\"Remote-API-Einstellungen\",\"Entfernen\",\"Kopie entfernen\",\"Objekt {n} entfernen\",\"Bereich entfernen\",\"Die neue Kopie von {name} entfernen\",\"Die Überschreibung von {label} bei {object} entfernen\",\"Den Bereich {min}–{max} mm von {object} entfernen\",\"{name} von der Platte entfernen\",\"Entfernt\",\"Umbenannt\",\"Gespeichertes {type}-Profil ersetzt\",\"Schichthöhe zurücksetzen\",\"Ordner der Wiederherstellungspunkte\",\"Wiederherstellungspunkte benötigen den OrcaSlicer-MCP-Build\",\"Das Projekt auf den Stand von {time} wiederherstellen (macht spätere Änderungen rückgängig)\",\"Auf {time} wiederherstellen?\",\"Wiederherstellung rückgängig gemacht\",\"Auf {time} wiederhergestellt\",\"Wird wiederhergestellt…\",\"Zurücksetzen\",\"Zurückgesetzt\",\"Zurückgesetzt um {time}\",\"Wird zurückgesetzt…\",\"Zurückdrehen\",\"Objekt {n} drehen\",\"{name} zurückdrehen\",\"Gedreht\",\"Führen Sie dies einmal aus. Die Desktop-App übernimmt die Einstellung. Benötigt uv.\",\"Gleicher Pfad\",\"Projekt speichern\",\"Projekt als {name} speichern\",\"{type}-Profil speichern\",\"Profil gespeichert\",\"Projekt gespeichert\",\"Zurückskalieren\",\"Objekt {n} skalieren\",\"{name} zurückskalieren\",\"Skaliert\",\"Skripte, benutzerdefinierter G-code und Druckerverbindungs-Einstellungen sind gesperrt. Aktivieren Sie 'Allow script, G-code and connection edits' unter Einstellungen > Remote API, damit der Assistent sie ändern darf, oder ändern Sie sie hier selbst.\",\"Einrichten dort, wo Sie Ihre App nutzen\",\"Einrichten dort, wo Sie {app} nutzen\",\"Den Bereich {min}–{max} mm von {object} auf {before} zurücksetzen\",\"{label} auf {before} zurücksetzen\",\"{label} bei {object} auf {before} zurücksetzen\",\"Einstellung\",\"Einstellungen\",\"Einstellungen › Plugins › Hinzufügen › MCP-Server hinzufügen, STDIO wählen und jedes Feld unten ausfüllen. Funktioniert in Codex-Chats; normale ChatGPT-Chats können es noch nicht nutzen. Benötigt uv.\",\"Details zeigen\",\"Zeigen, was es war, und in den Einstellungen finden\",\"Zeigen, was es war, und rückgängig machen\",\"Zeigen, was es war, rückgängig machen und in den Einstellungen finden\",\"Slice-Ergebnis\",\"Gesliced\",\"Gesliced {where}\",\"Slicen abgebrochen\",\"Slicen fehlgeschlagen\",\"Etwas ist schiefgelaufen\",\"Etwas ist schiefgelaufen: {reason}\",\"Zurückwechseln\",\"Wechsel abgebrochen\",\"{label} zurück auf {before} wechseln\",\"{label} zurück auf {before} wechseln (verwirft nicht gespeicherte Änderungen)\",\"Dieses Filament ist nicht mehr da\",\"Die Remote API konnte nicht starten. Port prüfen.\",\"Die Remote API ist aus.\",\"Die Kopie ist bereits weg\",\"Der alte Ordner ist nicht mehr da\",\"Der alte Name ist jetzt vergeben\",\"Die Platte hat sich seitdem geändert; stattdessen einen früheren Stand wiederherstellen\",\"Das Plugin wurde nicht gestartet\",\"Das Plugin ändert nie Skripte, G-Code oder Druckerverbindungseinstellungen\",\"Das Projekt hat sich seitdem geändert\",\"Der Wiederherstellungspunkt ist nicht lesbar\",\"Der Wiederherstellungspunkt ist nicht mehr da\",\"Der Wiederherstellungspunkt wurde auf der Festplatte geändert und wird daher nicht verwendet\",\"Die Einstellung ist nicht mehr in diesem Profil\",\"Der Einstellungs-Tab ist nicht bereit\",\"Der Einstellungs-Tab ist nicht vorhanden\",\"Der Stand vor der Wiederherstellung ist nicht mehr da\",\"Der Stand vor der Wiederherstellung wurde auf der Festplatte geändert und wird daher nicht verwendet\",\"Sagen Sie ihm dann, welchen Druck Sie wollen. Jede seiner Änderungen erscheint hier, und Sie können jede rückgängig machen.\",\"Sie enthalten evtl. noch die Werte der letzten Option ({reason}).\",\"Fach nicht bereit\",\"Fach doppelt verwendet\",\"Rückgängig machen\",\"Unbekannte Einstellung\",\"Nicht gespeicherte Änderungen\",\"Nicht gespeicherte Änderungen verworfen: {list}\",\"Nicht unterstützte Datei\",\"Unbenannt\",\"VS Code öffnet sich und bittet um Bestätigung. Benötigt uv.\",\"Warte auf Ihre App…\",\"Warte auf {app}…\",\"War {before}\",\"Wenn ein Assistent etwas ändert, erscheint es hier.\",\"Reinigungsturm X\",\"Reinigungsturm Y\",\"Falsche Anzahl an Werten\",\"Ja\",\"Von Ihnen seitdem geändert\",\"MCP-Einstellungen Ihrer App\",\"Ihre eigenen Änderungen seitdem werden ebenfalls rückgängig gemacht.\",\"neben dem Druckbett\",\"von Ihnen geändert\",\"seitdem geändert\",\"Kopie seitdem entfernt\",\"seitdem gelöscht\",\"durch Profilwechsel verworfen\",\"enthält jetzt Objekte\",\"Schicht {height} mm\",\"Zeile {line}: {text} ({count} Zeilen)\",\"seitdem bewegt\",\"keine\",\"keine Zahl\",\"nicht gedreht\",\"nicht gesetzt\",\"nichts behalten\",\"alter Name vergeben\",\"veraltet\",\"Platte {n}\",\"Platte {n} · {error}\",\"Bereiche seitdem gesetzt\",\"seitdem entfernt\",\"zurückgesetzt\",\"von Ihnen zurückgesetzt\",\"Einstellungen sind evtl. nicht wie zuvor\",\"die Platte\",\"der Einstellungs-Tab ist nicht bereit\",\"auf {position}\",\"durch die Wiederherstellung rückgängig gemacht\",\"Ihre App\",\"{angle}° um {axis}\",\"{key}: keine Prozess-, Filament- oder Druckereinstellung\",\"{key}: nicht unterstützter Wert\",\"{label} (Filament {n})\",\"{label} (Filamente {list})\",\"{label} muss größer als {min} sein (ist {got})\",\"{label} muss größer als {min} sein (ist {got}); der niedrigste vom Drucker erlaubte Wert wird unter Drucker-Einstellungen > Höhenbegrenzungen für Schichten festgelegt\",\"{label} muss größer als {min} und höchstens {max} sein (ist {got})\",\"{label} muss größer als {min} und höchstens {max} sein (ist {got}); der niedrigste vom Drucker erlaubte Wert wird unter Drucker-Einstellungen > Höhenbegrenzungen für Schichten festgelegt\",\"{label} muss mindestens {min} sein (ist {got})\",\"{label} muss mindestens {min} sein (ist {got}); der niedrigste vom Drucker erlaubte Wert wird unter Drucker-Einstellungen > Höhenbegrenzungen für Schichten festgelegt\",\"{label} darf höchstens {max} sein (ist {got})\",\"{label} darf höchstens {max} sein (ist {got}); der niedrigste vom Drucker erlaubte Wert wird unter Drucker-Einstellungen > Höhenbegrenzungen für Schichten festgelegt\",\"{label} muss zwischen {min} und {max} liegen (ist {got})\",\"{label} muss zwischen {min} und {max} liegen (ist {got}); der niedrigste vom Drucker erlaubte Wert wird unter Drucker-Einstellungen > Höhenbegrenzungen für Schichten festgelegt\",\"{label} nicht erlaubt\",\"{name} passt jetzt nicht mehr zu diesem Drucker\",\"{name} ist nicht mehr da\",\"{name} ist bereits weg\",\"{name} liegt nicht mehr auf der Platte\",\"{name} ist nicht mehr vorhanden\",\"{name} → {plate}\",\"{name}, jetzt {n} Kopien\",\"{n} Objekt\",\"{n} Objekte\",\"{n} Bereich\",\"{n} Bereiche\",\"{object} liegt nicht mehr auf der Platte\",\"Nur {object}\",\"Nur {object} · aus dem Prozessprofil\",\"{preset} (nicht gespeichert)\",\"{text} ({count} Zeilen)\",\"{title} und {n} weitere\",\"{value} in {preset}\",\"{verb} (nicht verfügbar: {why})\",\"{verb}: {what}\",\"{verb}: {label} zurück auf {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"es\": [\"(línea vacía)\",\"(oculto)\",\"Se está ejecutando una restauración\",\"Altura de la capa adaptable\",\"Añadir modelo\",\"Añadir cama\",\"Añadir a Cursor\",\"Añadir a VS Code\",\"Añadir {name}\",\"Añadido\",\"Cama añadida\",\"Ya está de nuevo en {before}\",\"Añade también {setting} como variable de entorno.\",\"Un objeto\",\"Argumentos\",\"Organizar\",\"Organizado\",\"Pídele la impresión que quieres. Sus cambios aparecerán aquí, cada uno con Revertir.\",\"Asistente\",\"El acceso del asistente está desactivado.\",\"El asistente ha establecido {value} · era {before}\",\"El asistente ha establecido {value} · lo has revertido\",\"Ajustes del asistente\",\"Orientar automáticamente\",\"Orientado automáticamente\",\"Nombre integrado\",\"Perfil integrado\",\"Ocupado\",\"Ocupado laminando\",\"Ocupado con la solicitud del asistente; inténtalo de nuevo en un momento\",\"No se puede volver a cambiar\",\"Ahora no se puede comprobar: {reason}\",\"No se puede conectar: solo llega a servidores de internet, no a programas de tu ordenador.\",\"No se puede borrar\",\"No se puede devolver tras un reinicio; restaura un punto anterior\",\"No se pueden devolver los rangos\",\"No se puede volver al valor anterior\",\"No se puede volver al valor anterior: {reason}\",\"No se puede volver al anterior\",\"No se puede seguir este objeto tras recargar el proyecto\",\"Cancelado\",\"Volver a cambiar\",\"Devolver {label} a {before}\",\"Cambiado de nuevo desde entonces\",\"Cambiado de nuevo desde entonces; restaura un punto anterior\",\"Aplicación de escritorio de ChatGPT\",\"ChatGPT en la web o en el móvil\",\"Aplicación de Claude\",\"Borrar los rangos de altura\",\"Codex en un terminal o un editor\",\"Contraer todo\",\"Comando para iniciar\",\"Se comparó {n} opción\",\"Se comparó {n} opción · no se conservó nada\",\"Se comparó {n} opción · puede que los ajustes no hayan vuelto a como estaban\",\"Se compararon {n} opciones\",\"Se compararon {n} opciones · no se conservó nada\",\"Se compararon {n} opciones · puede que los ajustes no hayan vuelto a como estaban\",\"La comparación no pudo restablecer: {labels}\",\"Conecta tu aplicación de IA\",\"Conectado\",\"Conectado a {app}\",\"Copiado\",\"Copiar\",\"Copiar ajustes de .vscode/mcp.json en su lugar\",\"Copiar comando\",\"Copiar ajustes\",\"Copiar ajustes en su lugar\",\"No se pudo restaurar\",\"No se pudo revertir\",\"No se pudo calcular\",\"No se pudo borrar\",\"No se pudo borrar\",\"No se pudo mover\",\"No se pudo volver a mover\",\"No se pudo abrir\",\"No se pudo abrir el punto de restauración\",\"No se pudo restaurar a las {time}\",\"No se pudo guardar\",\"No se pudo guardar antes el estado actual\",\"No se pudo seleccionar\",\"No se pudo deshacer\",\"Se abre Cursor y te pide confirmación. Requiere uv.\",\"Borrar la Cama {n} (solo mientras esté vacía)\",\"Borrar cama\",\"Borrar cama {n}\",\"Borrar perfil\",\"Borrar el perfil {type} {name}\",\"Borrar {name}\",\"Borrar perfil {type}\",\"Archivo borrado\",\"Cama borrada\",\"Perfil borrado\",\"Descargar\",\"Descartado por un cambio de perfil\",\"Descartado por un cambio de perfil; restaura un punto anterior\",\"Duplicar el objeto {n}\",\"Duplicado\",\"Todas las apps necesitan uv, una pequeña herramienta gratuita que ejecuta el conector. Basta con instalarla una vez.\",\"Expandir todo\",\"Error\",\"Filamento\",\"Perfil de filamento\",\"Bandejas de filamento\",\"Filamento {n}\",\"Color del filamento {n}\",\"Bandeja del filamento {n}\",\"Archivo\",\"El archivo ya existe\",\"No se encontró el archivo\",\"Los archivos y perfiles guardados en el disco se conservan.\",\"No se encontró la carpeta\",\"Para chats. Descarga la extensión y abre el archivo. Requiere uv.\",\"Obtener uv\",\"Volver a antes de la restauración\",\"Rango de altura {min}–{max} mm\",\"Rangos de altura borrados\",\"Valor oculto\",\"En un terminal, un editor o la pestaña Code de la aplicación. Requiere uv.\",\"En los ajustes de la extensión, establece \\\"OrcaSlicer settings folder\\\" como {folder}.\",\"Valor no válido\",\"Ahora tiene objetos\",\"Ya no está en la cama\",\"Ya no está\",\"Está seleccionado; cambia antes a otro perfil\",\"Es la última cama\",\"Sus objetos se movieron junto a la cama\",\"LM Studio, Windsurf y la mayoría de las demás aceptan este formato. Requiere uv.\",\"Última cama\",\"Altura de la capa fuera de rango\",\"Altura de la capa reiniciada\",\"Línea {line} de {count}:\",\"Líneas {first} a {last} de {count}:\",\"Menú principal (junto a Archivo) > Plugins > OrcaSlicer MCP > Configuración\",\"Modelo\",\"Volver a mover\",\"Mover el objeto {n}\",\"Mover el objeto {n} a la Cama {index}\",\"Mover {name}\",\"Volver a mover {name} a {path}\",\"Volver a mover {name} a {plate}\",\"Volver a poner {name} donde estaba\",\"Movido\",\"Movido de nuevo desde entonces\",\"Movido a una cama\",\"Nombre\",\"Requiere Preferencias › Remote API › Allow deleting and replacing project files\",\"Requiere Preferencias › Remote API › Allow script, G-code and connection edits\",\"Requiere Preferencias › Remote API › Allow script, G-code and connection edits · {object}\",\"Requiere la versión de OrcaSlicer MCP\",\"Nunca guardado\",\"Proyecto nuevo\",\"Perfil {type} nuevo\",\"No\",\"Ninguna bandeja CANVAS\",\"Sin cambios\",\"Aquí no hay ningún punto de restauración\",\"No hay sitio para otra cama\",\"No existe ese cambio\",\"No existe esa copia\",\"No existe ese filamento\",\"No existe esa cama\",\"No existe ese perfil\",\"No existe ese punto de restauración\",\"No existe esa bandeja\",\"No permitido\",\"No compatible\",\"No editable aquí\",\"No está en la lista reciente\",\"No está en la cronología de este proyecto\",\"No se ha devuelto\",\"No está listo\",\"No almacenado\",\"No mientras OrcaSlicer está ocupado con una tarea\",\"No mientras OrcaSlicer se está cerrando\",\"No mientras se carga un proyecto\",\"No mientras se lamina\",\"No mientras se reconstruye la ventana\",\"Nota\",\"No se movió nada\",\"No hay nada en la cama\",\"No hay nada que revertir\",\"Nada que mostrar en los ajustes\",\"Nada que deshacer\",\"OK\",\"El objeto no tiene copia\",\"No se encontró el objeto\",\"Ajustes del objeto {n}\",\"Apagado\",\"Activado\",\"Abrir el proyecto {name}\",\"Abrir {settings}\",\"Proyecto abierto\",\"Abriendo…\",\"Panel de OrcaSlicer MCP cerrado. Recupéralo desde Vista > OrcaSlicer MCP panel.\",\"Panel de OrcaSlicer MCP cerrado. Vuelve cuando tu asistente use OrcaSlicer, o pulsa la barra espaciadora y elige OrcaSlicer MCP.\",\"OrcaSlicer informó de un error\",\"Otra aplicación\",\"Otros perfiles heredan de este\",\"Desactualizado: algo cambió después de este laminado\",\"Fuera de rango\",\"Se solapa con un rango\",\"Cama\",\"Cama bloqueada\",\"Cama {n}\",\"La cama {n} ahora contiene otros objetos\",\"Perfil\",\"Perfil en uso\",\"Perfil de impresión\",\"Impresora\",\"Perfil de impresora\",\"El proyecto está abierto\",\"Volver a poner\",\"Volver a poner los rangos de altura de {object}\",\"Volver a poner el perfil de altura de la capa anterior de {object}\",\"Volver a poner los {n} objetos donde estaban\",\"Volver a poner {name} en {plate}\",\"Calidad {value}\",\"Solo lectura\",\"Rechazado\",\"Denegado\",\"Ajustes de Remote API\",\"Eliminar\",\"Eliminar copia\",\"Eliminar el objeto {n}\",\"Eliminar rango\",\"Eliminar la copia nueva de {name}\",\"Eliminar la anulación de {label} en {object}\",\"Eliminar el rango de {min}–{max} mm de {object}\",\"Eliminar {name} de la cama\",\"Eliminado\",\"Renombrado\",\"Se reemplazó el perfil {type} guardado\",\"Reiniciar la altura de la capa\",\"Carpeta del punto de restauración\",\"Los puntos de restauración requieren la versión de OrcaSlicer MCP\",\"Restaurar el proyecto a como estaba a las {time} (deshace los cambios posteriores)\",\"¿Restaurar a las {time}?\",\"Restauración deshecha\",\"Restaurado a las {time}\",\"Restaurando…\",\"Revertir\",\"Revertido\",\"Revertido a las {time}\",\"Revirtiendo…\",\"Volver a rotar\",\"Rotar el objeto {n}\",\"Volver a rotar {name}\",\"Rotado\",\"Ejecútalo una vez. La aplicación de escritorio comparte el ajuste. Requiere uv.\",\"La misma ruta\",\"Guardar proyecto\",\"Guardar el proyecto como {name}\",\"Guardar perfil {type}\",\"Perfil guardado\",\"Proyecto guardado\",\"Volver a escalar\",\"Escalar el objeto {n}\",\"Volver a escalar {name}\",\"Escalado\",\"Los scripts, el G-code personalizado y los ajustes de conexión de la impresora están bloqueados. Marca 'Allow script, G-code and connection edits' en Preferencias > Remote API para que el asistente pueda cambiarlos, o cámbialos tú mismo aquí.\",\"Configúralo donde uses tu aplicación\",\"Configúralo donde uses {app}\",\"Volver a poner el rango de {min}–{max} mm de {object} en {before}\",\"Devolver {label} a {before}\",\"Devolver {label} de {object} a {before}\",\"Ajuste\",\"Ajustes\",\"Ajustes › Complementos › Añadir › Añadir servidor MCP, elige STDIO y rellena cada campo de abajo. Funciona en los chats de Codex; los chats normales de ChatGPT aún no pueden usarlo. Requiere uv.\",\"Mostrar detalles\",\"Mostrar cómo era y buscarlo en los ajustes\",\"Mostrar cómo era y deshacerlo\",\"Mostrar cómo era, deshacerlo y buscarlo en los ajustes\",\"Resultado del laminado\",\"Laminado\",\"Laminado {where}\",\"Laminado cancelado\",\"Falló el laminado\",\"Algo salió mal\",\"Algo salió mal: {reason}\",\"Volver al anterior\",\"Cambio cancelado\",\"Volver {label} a {before}\",\"Volver {label} a {before} (descarta las ediciones sin guardar)\",\"Ese filamento ya no existe\",\"Remote API no pudo iniciarse. Comprueba su puerto.\",\"Remote API está desactivada.\",\"La copia ya no existe\",\"La carpeta anterior ya no existe\",\"El nombre anterior ya está en uso\",\"La cama ha cambiado desde entonces; restaura un punto anterior\",\"El plugin no se inició\",\"El plugin nunca cambia scripts, G-code ni los ajustes de conexión de la impresora\",\"El proyecto ha cambiado desde entonces\",\"No se puede leer el punto de restauración\",\"El punto de restauración ya no existe\",\"El punto de restauración se modificó en el disco, así que no se usa\",\"El ajuste ya no está en este perfil\",\"La pestaña de ajustes no está lista\",\"La pestaña de ajustes no está\",\"La instantánea anterior a la restauración ya no existe\",\"La instantánea anterior a la restauración se modificó en el disco, así que no se usa\",\"Después dile la impresión que quieres. Cada cambio que haga aparecerá aquí y podrás deshacer cualquiera.\",\"Puede que aún conserven los valores de la última opción ({reason}).\",\"Bandeja no lista\",\"Bandeja usada dos veces\",\"Deshacer\",\"Ajuste desconocido\",\"Cambios sin guardar\",\"Ediciones sin guardar descartadas: {list}\",\"Archivo no admitido\",\"Sin título\",\"Se abre VS Code y te pide confirmación. Requiere uv.\",\"Esperando a tu aplicación…\",\"Esperando a {app}…\",\"Era {before}\",\"Cuando un asistente cambia algo, aparece aquí.\",\"Torre de purga X\",\"Torre de purga Y\",\"Número de valores incorrecto\",\"Sí\",\"Lo has cambiado desde entonces\",\"Ajustes de MCP de tu aplicación\",\"Tus propios cambios desde entonces también se deshacen.\",\"junto a la cama\",\"cambiado por ti\",\"cambiado desde entonces\",\"copia eliminada desde entonces\",\"borrado desde entonces\",\"descartado por un cambio de perfil\",\"ahora tiene objetos\",\"capa {height} mm\",\"línea {line}: {text} ({count} líneas)\",\"movido desde entonces\",\"ninguno\",\"no es un número\",\"no rotado\",\"sin definir\",\"no se conservó nada\",\"nombre anterior en uso\",\"desactualizado\",\"cama {n}\",\"cama {n} · {error}\",\"rangos establecidos desde entonces\",\"eliminado desde entonces\",\"revertido\",\"devuelto por ti\",\"puede que los ajustes no hayan vuelto a como estaban\",\"la cama\",\"la pestaña de ajustes no está lista\",\"a {position}\",\"deshecho por la restauración\",\"tu aplicación\",\"{angle}° alrededor de {axis}\",\"{key}: no es un ajuste de impresión, filamento ni impresora\",\"{key}: valor no admitido\",\"{label} (filamento {n})\",\"{label} (filamentos {list})\",\"{label} debe ser superior a {min} (se recibió {got})\",\"{label} debe ser superior a {min} (se recibió {got}); el mínimo que permite la impresora se define en Ajustes de la impresora > Límites de altura de la capa\",\"{label} debe ser superior a {min} y como máximo {max} (se recibió {got})\",\"{label} debe ser superior a {min} y como máximo {max} (se recibió {got}); el mínimo que permite la impresora se define en Ajustes de la impresora > Límites de altura de la capa\",\"{label} debe ser como mínimo {min} (se recibió {got})\",\"{label} debe ser como mínimo {min} (se recibió {got}); el mínimo que permite la impresora se define en Ajustes de la impresora > Límites de altura de la capa\",\"{label} debe ser como máximo {max} (se recibió {got})\",\"{label} debe ser como máximo {max} (se recibió {got}); el mínimo que permite la impresora se define en Ajustes de la impresora > Límites de altura de la capa\",\"{label} debe estar entre {min} y {max} (se recibió {got})\",\"{label} debe estar entre {min} y {max} (se recibió {got}); el mínimo que permite la impresora se define en Ajustes de la impresora > Límites de altura de la capa\",\"{label} no permitido\",\"{name} ya no es adecuado para esta impresora\",\"{name} ya no existe\",\"{name} ya ha desaparecido\",\"{name} ya no está en la cama\",\"{name} ya no está ahí\",\"{name} → {plate}\",\"{name}, ahora {n} copias\",\"{n} objeto\",\"{n} objetos\",\"{n} rango\",\"{n} rangos\",\"{object} ya no está en la cama\",\"Solo {object}\",\"Solo {object} · del perfil de impresión\",\"{preset} (sin guardar)\",\"{text} ({count} líneas)\",\"{title} y {n} más\",\"{value} en {preset}\",\"{verb} (no disponible: {why})\",\"{verb} {what}\",\"{verb}: {label} vuelve a {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"eu\": [\"(lerro hutsa)\",\"(ezkutua)\",\"Berrezarpen bat martxan dago\",\"Geruza-altuera egokitua\",\"Modeloa gehitu\",\"Plaka gehitu\",\"Gehitu Cursor-era\",\"Gehitu VS Code-ra\",\"{name} gehitu\",\"Gehituta\",\"Plaka gehituta\",\"Dagoeneko {before} da berriro\",\"Gehitu {setting} ere ingurune-aldagai gisa.\",\"Objektu bat\",\"Arguments\",\"Antolatu\",\"Antolatuta\",\"Eskatu nahi duzun inprimaketa. Bere aldaketak hemen agertuko dira, bakoitza Leheneratu botoiarekin.\",\"Laguntzailea\",\"Laguntzailearen sarbidea desaktibatuta dago.\",\"Laguntzaileak {value} ezarri du · {before} zen\",\"Laguntzaileak {value} ezarri du · zuk atzera ezarri duzu\",\"Laguntzailearen ezarpenak\",\"Orientatu automatikoki\",\"Automatikoki orientatuta\",\"Barneratutako izena\",\"Barneratutako aurrezarpena\",\"Lanpetuta\",\"Xerratzen lanpetuta\",\"Laguntzailearen eskaerarekin lanpetuta; saiatu berriro une batean\",\"Ezin da atzera aldatu\",\"Orain ezin da egiaztatu: {reason}\",\"Ezin da konektatu: internet bidezko zerbitzarietara bakarrik iristen da, ez zure ordenagailuko programetara.\",\"Ezin da ezabatu\",\"Ezin da atzera jarri berrabiarazi ondoren; berrezarri lehenagoko puntu bat\",\"Ezin dira tarteak atzera jarri\",\"Ezin da aurreko balioa berrezarri\",\"Ezin da aurreko balioa berrezarri: {reason}\",\"Ezin da aurrekora itzuli\",\"Ezin da objektu hau jarraitu proiektua birkargatu ondoren\",\"Bertan behera utzita\",\"Atzera aldatu\",\"Aldatu {label} atzera: {before}\",\"Ordutik berriro aldatua\",\"Ordutik berriro aldatua; berrezarri lehenagoko puntu bat\",\"ChatGPT mahaigaineko aplikazioa\",\"ChatGPT webean edo telefonoan\",\"Claude aplikazioa\",\"Garbitu altuera-tarteak\",\"Codex terminal edo editore batean\",\"Tolestu guztiak\",\"Command to launch\",\"{n} aukera konparatu da\",\"{n} aukera konparatu da · ez da ezer mantendu\",\"{n} aukera konparatu da · baliteke ezarpenak lehen bezala ez egotea\",\"{n} aukera konparatu dira\",\"{n} aukera konparatu dira · ez da ezer mantendu\",\"{n} aukera konparatu dira · baliteke ezarpenak lehen bezala ez egotea\",\"Konparazioak ezin izan du berrezarri: {labels}\",\"Konektatu zure IA aplikazioa\",\"Konektatuta\",\"Konektatuta: {app}\",\"Kopiatuta\",\"Kopiatu\",\"Kopiatu .vscode/mcp.json ezarpenak horren ordez\",\"Kopiatu komandoa\",\"Kopiatu ezarpenak\",\"Kopiatu ezarpenak horren ordez\",\"Ezin izan da berrezarri\",\"Ezin izan da leheneratu\",\"Ezin izan da kalkulatu\",\"Ezin izan da ezabatu\",\"Ezin izan da ezabatu\",\"Ezin izan da mugitu\",\"Ezin izan da atzera mugitu\",\"Ezin izan da ireki\",\"Ezin izan da berrezarpen-puntua ireki\",\"Ezin izan da {time} orduko egoerara berrezarri\",\"Ezin izan da gorde\",\"Ezin izan da aurretik uneko egoera gorde\",\"Ezin izan da hautatu\",\"Ezin izan da desegin\",\"Cursor irekitzen da eta berrespena eskatzen du. uv behar da.\",\"Ezabatu Plaka {n} (hutsik dagoen bitartean soilik)\",\"Ezabatu plaka\",\"Ezabatu plaka {n}\",\"Ezabatu aurrezarpena\",\"Ezabatu {name} {type} aurrezarpena\",\"Ezabatu {name}\",\"Ezabatu {type} aurrezarpena\",\"Fitxategia ezabatuta\",\"Plaka ezabatuta\",\"Aurrezarpena ezabatuta\",\"Deskargatu\",\"Aurrezarpen-aldaketak baztertu du\",\"Aurrezarpen-aldaketak baztertu du; berrezarri lehenagoko puntu bat\",\"Bikoiztu {n}. objektua\",\"Bikoiztuta\",\"Aplikazio guztiek uv behar dute, konektorea exekutatzen duen tresna txiki eta doakoa. Behin instalatzea nahikoa da.\",\"Zabaldu guztiak\",\"Huts egin du\",\"Filamentua\",\"Filamentu-aurrezarpena\",\"Filamentu-erretiluak\",\"{n}. filamentua\",\"{n}. filamentuaren kolorea\",\"{n}. filamentuaren erretilua\",\"Fitxategia\",\"Fitxategia badago\",\"Ez da fitxategia aurkitu\",\"Diskoan gordetako fitxategiak eta aurrezarpenak mantentzen dira.\",\"Ez da karpeta aurkitu\",\"Txatentzat. Deskargatu luzapena eta ireki fitxategia. uv behar da.\",\"Lortu uv\",\"Itzuli berrezarpenaren aurreko egoerara\",\"Altuera-tartea {min}–{max} mm\",\"Altuera-tarteak garbituta\",\"Balio ezkutua\",\"Terminal batean, editore batean edo aplikazioaren Code fitxan. uv behar da.\",\"Luzapenaren ezarpenetan, ezarri \\\"OrcaSlicer settings folder\\\" honela: {folder}.\",\"Balio baliogabea\",\"Orain objektuak ditu\",\"Ez dago plakan jada\",\"Ez dago jada\",\"Hautatuta dago; aldatu lehenik beste aurrezarpen batera\",\"Azken plaka da\",\"Bere objektuak ohearen ondora mugitu dira\",\"LM Studio, Windsurf eta beste gehienek forma hau onartzen dute. uv behar da.\",\"Azken plaka\",\"Geruza-altuera tartetik kanpo\",\"Geruza-altuera berrezarrita\",\"{line}. lerroa ({count} guztira):\",\"{first}–{last} lerroak ({count} guztira):\",\"Menu nagusia (Fitxategia ondoan) > Pluginak > OrcaSlicer MCP > Konfigurazioa\",\"Modeloa\",\"Atzera mugitu\",\"Mugitu {n}. objektua\",\"Mugitu {n}. objektua (helburua: Plaka {index})\",\"Mugitu {name}\",\"Mugitu {name} atzera hona: {path}\",\"Mugitu {name} atzera hona: {plate}\",\"Itzuli {name} zegoen lekura\",\"Mugituta\",\"Ordutik berriro mugitua\",\"Plaka batera mugituta\",\"Name\",\"Beharrezkoa: Hobespenak › Remote API › Allow deleting and replacing project files\",\"Beharrezkoa: Hobespenak › Remote API › Allow script, G-code and connection edits\",\"Beharrezkoa: Hobespenak › Remote API › Allow script, G-code and connection edits · {object}\",\"OrcaSlicer MCP bertsioa behar da\",\"Inoiz gorde gabe\",\"Proiektu berria\",\"{type} aurrezarpen berria\",\"Ez\",\"CANVAS erretilurik ez\",\"Aldaketarik ez\",\"Hemen ez dago berrezarpen-punturik\",\"Ez dago lekurik beste plaka baterako\",\"Ez dago aldaketa hori\",\"Ez dago kopia hori\",\"Ez dago filamentu hori\",\"Ez dago plaka hori\",\"Ez dago aurrezarpen hori\",\"Ez dago berrezarpen-puntu hori\",\"Ez dago erretilu hori\",\"Ez dago baimenduta\",\"Ez da bateragarria\",\"Hemen ezin da editatu\",\"Ez dago azken zerrendan\",\"Ez dago proiektu honen kronologian\",\"Ez da atzera jarri\",\"Ez dago prest\",\"Gorde gabe\",\"Ez OrcaSlicer lan batean lanpetuta dagoen bitartean\",\"Ez OrcaSlicer ixten ari den bitartean\",\"Ez proiektu bat kargatzen ari den bitartean\",\"Ez xerratzen ari den bitartean\",\"Ez leihoa berreraikitzen ari den bitartean\",\"Oharra\",\"Ez da ezer mugitu\",\"Ez dago ezer plakan\",\"Ez dago leheneratzeko ezer\",\"Ez dago ezer erakusteko ezarpenetan\",\"Ez dago desegiteko ezer\",\"Ados\",\"Objektuak ez du kopiarik\",\"Ez da objektua aurkitu\",\"{n}. objektuaren ezarpenak\",\"Desaktibatuta\",\"Aktibatuta\",\"Ireki {name} proiektua\",\"Ireki {settings}\",\"Proiektua irekita\",\"Irekitzen…\",\"OrcaSlicer MCP panela itxi da. Berreskuratu Bista > OrcaSlicer MCP panel menutik.\",\"OrcaSlicer MCP panela itxi da. Itzuliko da zure laguntzaileak OrcaSlicer erabiltzen duenean, edo sakatu zuriune-barra eta aukeratu OrcaSlicer MCP.\",\"OrcaSlicerrek errore bat jakinarazi du\",\"Beste aplikazio bat\",\"Beste aurrezarpen batzuek hortik heredatzen dute\",\"Zaharkituta: zerbait aldatu da xerratze honen ondoren\",\"Tartetik kanpo\",\"Tarte batekin gainjartzen da\",\"Plaka\",\"Plaka blokeatuta\",\"Plaka {n}\",\"Plaka {n} horretan beste objektu batzuk daude orain\",\"Aurrezarpena\",\"Aurrezarpena erabiltzen ari da\",\"Inprimatze-aurrezarpena\",\"Inprimagailua\",\"Inprimagailu-aurrezarpena\",\"Proiektua irekita dago\",\"Atzera jarri\",\"Jarri {object} objektuaren altuera-tarteak atzera\",\"Jarri {object} objektuaren aurreko geruza-altueraren profila atzera\",\"Jarri {n} objektuak zeuden lekuan berriro\",\"Jarri {name} berriro hemen: {plate}\",\"Kalitatea {value}\",\"Irakurtzeko soilik\",\"Baztertua\",\"Ukatua\",\"Remote API ezarpenak\",\"Kendu\",\"Kendu kopia\",\"Kendu {n}. objektua\",\"Kendu tartea\",\"Kendu kopia berria: {name}\",\"Kendu {label} ezarpenaren ordezkapena hemen: {object}\",\"Kendu {min}–{max} mm-ko tartea hemendik: {object}\",\"Kendu {name} plakatik\",\"Kenduta\",\"Izena aldatuta\",\"Gordetako {type} aurrezarpena ordezkatu da\",\"Berrezarri geruza-altuera\",\"Berrezarpen-puntuaren karpeta\",\"Berrezarpen-puntuek OrcaSlicer MCP bertsioa behar dute\",\"Berrezarri proiektua {time} orduko egoerara (geroko aldaketak desegiten ditu)\",\"{time} orduko egoerara berrezarri?\",\"Berrezarpena desegin da\",\"{time} orduko egoerara berrezarrita\",\"Berrezartzen…\",\"Leheneratu\",\"Leheneratuta\",\"Leheneratuta: {time}\",\"Leheneratzen…\",\"Atzera biratu\",\"Biratu {n}. objektua\",\"Biratu {name} atzera\",\"Biratuta\",\"Exekutatu behin. Mahaigaineko aplikazioak ezarpena partekatzen du. uv behar da.\",\"Bide bera\",\"Gorde proiektua\",\"Gorde proiektua {name} izenarekin\",\"Gorde {type} aurrezarpena\",\"Aurrezarpena gordeta\",\"Proiektua gordeta\",\"Atzera eskalatu\",\"Eskalatu {n}. objektua\",\"Eskalatu {name} atzera\",\"Eskalatuta\",\"Scriptak, G-code pertsonalizatua eta inprimagailuaren konexio-ezarpenak blokeatuta daude. Markatu 'Allow script, G-code and connection edits' Hobespenak > Remote API atalean laguntzaileak alda ditzan, edo aldatu hemen zuk zeuk.\",\"Konfiguratu zure aplikazioa erabiltzen duzun lekuan\",\"Konfiguratu {app} erabiltzen duzun lekuan\",\"Berrezarri {object} objektuaren {min}–{max} mm-ko tartea: {before}\",\"Berrezarri {label}: {before}\",\"Berrezarri {object} objektuaren {label}: {before}\",\"Ezarpena\",\"Ezarpenak\",\"Settings › Plugins › Add › Add MCP server, aukeratu STDIO eta bete beheko eremu bakoitza. Codex txatetan funtzionatzen du; ChatGPT txat arruntek ezin dute oraindik erabili. uv behar da.\",\"Erakutsi xehetasunak\",\"Erakutsi zer zen eta aurkitu ezarpenetan\",\"Erakutsi zer zen eta desegin\",\"Erakutsi zer zen, desegin eta aurkitu ezarpenetan\",\"Xerratzearen emaitza\",\"Xerratuta\",\"Xerratuta: {where}\",\"Xerratzea bertan behera utzi da\",\"Xerratzeak huts egin du\",\"Zerbait gaizki atera da\",\"Zerbait gaizki atera da: {reason}\",\"Aurrekora itzuli\",\"Aldaketa bertan behera utzi da\",\"Itzuli {label}: {before}\",\"Itzuli {label}: {before} (gorde gabeko edizioak baztertzen ditu)\",\"Filamentu hori ez dago jada\",\"Remote API ezin izan da abiarazi. Egiaztatu bere ataka.\",\"Remote API desaktibatuta dago.\",\"Kopia desagertu da jada\",\"Karpeta zaharra desagertu da\",\"Izen zaharra erabilita dago orain\",\"Plaka aldatu egin da ordutik; berrezarri lehenagoko puntu bat\",\"Pluginak ez du abiarazi\",\"Pluginak ez ditu inoiz aldatzen scriptak, G-code edo inprimagailuaren konexio-ezarpenak\",\"Proiektua aldatu egin da ordutik\",\"Berrezarpen-puntua ezin da irakurri\",\"Berrezarpen-puntua desagertu da\",\"Berrezarpen-puntua diskoan aldatu da, beraz ez da erabiltzen\",\"Ezarpena ez dago aurrezarpen honetan jada\",\"Ezarpenen fitxa ez dago prest\",\"Ezarpenen fitxa ez dago\",\"Berrezarpenaren aurreko argazkia desagertu da\",\"Berrezarpenaren aurreko argazkia diskoan aldatu da, beraz ez da erabiltzen\",\"Gero esan nahi duzun inprimaketa. Egiten duen aldaketa guztiak hemen agertuko dira, eta edozein desegin dezakezu.\",\"Baliteke azken aukeraren balioak gordetzea oraindik ({reason}).\",\"Erretilua ez dago prest\",\"Erretilua bitan erabilita\",\"Desegin\",\"Ezarpen ezezaguna\",\"Gorde gabeko aldaketak\",\"Gorde gabeko edizioak baztertu dira: {list}\",\"Fitxategi onartezina\",\"Izenbururik gabea\",\"VS Code irekitzen da eta berrespena eskatzen du. uv behar da.\",\"Zure aplikazioaren zain…\",\"{app} zain…\",\"{before} zen\",\"Laguntzaile batek zerbait aldatzen duenean, hemen agertzen da.\",\"Purgatze-dorrea X\",\"Purgatze-dorrea Y\",\"Balio kopuru okerra\",\"Bai\",\"Ordutik aldatu duzu\",\"Zure aplikazioaren MCP ezarpenak\",\"Zure aldaketa propioak ere desegiten dira ordutik.\",\"ohearen ondoan\",\"zuk aldatuta\",\"ordutik aldatuta\",\"kopia ordutik kenduta\",\"ordutik ezabatuta\",\"aurrezarpen-aldaketak baztertuta\",\"orain objektuak ditu\",\"geruza {height} mm\",\"{line}. lerroa: {text} ({count} lerro)\",\"ordutik mugituta\",\"bat ere ez\",\"ez da zenbaki bat\",\"biratu gabe\",\"ezarri gabe\",\"ez da ezer mantendu\",\"izen zaharra hartuta\",\"zaharkituta\",\"plaka {n}\",\"plaka {n} · {error}\",\"ordutik ezarritako tarteak\",\"ordutik kenduta\",\"leheneratuta\",\"zuk atzera ezarrita\",\"baliteke ezarpenak lehen bezala ez egotea\",\"plaka\",\"ezarpenen fitxa ez dago prest\",\"hona: {position}\",\"berrezarpenak desegina\",\"zure aplikazioa\",\"{angle}° {axis} ardatzaren inguruan\",\"{key}: ez da inprimaketa, filamentu edo inprimagailu ezarpena\",\"{key}: balio onartezina\",\"{label} ({n}. filamentua)\",\"{label} ({list} filamentuak)\",\"{label} {min} baino handiagoa izan behar da (jasotakoa: {got})\",\"{label} {min} baino handiagoa izan behar da (jasotakoa: {got}); inprimagailuak onartzen duen minimoa Inprimagailuaren ezarpenak > Geruza-altueraren mugak atalean ezartzen da\",\"{label} {min} baino handiagoa eta gehienez {max} izan behar da (jasotakoa: {got})\",\"{label} {min} baino handiagoa eta gehienez {max} izan behar da (jasotakoa: {got}); inprimagailuak onartzen duen minimoa Inprimagailuaren ezarpenak > Geruza-altueraren mugak atalean ezartzen da\",\"{label} gutxienez {min} izan behar da (jasotakoa: {got})\",\"{label} gutxienez {min} izan behar da (jasotakoa: {got}); inprimagailuak onartzen duen minimoa Inprimagailuaren ezarpenak > Geruza-altueraren mugak atalean ezartzen da\",\"{label} gehienez {max} izan behar da (jasotakoa: {got})\",\"{label} gehienez {max} izan behar da (jasotakoa: {got}); inprimagailuak onartzen duen minimoa Inprimagailuaren ezarpenak > Geruza-altueraren mugak atalean ezartzen da\",\"{label} {min} eta {max} artean egon behar da (jasotakoa: {got})\",\"{label} {min} eta {max} artean egon behar da (jasotakoa: {got}); inprimagailuak onartzen duen minimoa Inprimagailuaren ezarpenak > Geruza-altueraren mugak atalean ezartzen da\",\"{label} ez dago baimenduta\",\"{name} ez dator bat inprimagailu honekin orain\",\"{name} desagertu da\",\"{name} dagoeneko desagertu da\",\"{name} ez dago plakan jada\",\"{name} ez dago han jada\",\"{name} → {plate}\",\"{name}, orain {n} kopia\",\"{n} objektu\",\"{n} objektu\",\"{n} tarte\",\"{n} tarte\",\"{object} ez dago plakan jada\",\"{object} soilik\",\"{object} soilik · inprimatze-aurrezarpenetik\",\"{preset} (gorde gabe)\",\"{text} ({count} lerro)\",\"{title} eta beste {n}\",\"{value} hemen: {preset}\",\"{verb} (ez dago erabilgarri: {why})\",\"{verb} {what}\",\"{verb}: {label} berriro {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"fr\": [\"(ligne vide)\",\"(masqué)\",\"Une restauration est en cours\",\"Hauteur de couche adaptative\",\"Ajouter un modèle\",\"Ajouter une plaque\",\"Ajouter à Cursor\",\"Ajouter à VS Code\",\"Ajouter {name}\",\"Ajouté\",\"Plaque ajoutée\",\"Déjà revenu à {before}\",\"Ajoutez aussi {setting} comme variable d'environnement.\",\"Un objet\",\"Arguments\",\"Organiser\",\"Organisé\",\"Demandez à l'assistant l'impression que vous voulez. Ses modifications apparaîtront ici, chacune avec Revenir.\",\"Assistant\",\"L'accès de l'assistant est désactivé.\",\"L'assistant a défini {value} · c'était {before}\",\"L'assistant a défini {value} · vous l'avez remis comme avant\",\"Réglages de l'assistant\",\"Orienter automatiquement\",\"Orienté automatiquement\",\"Nom intégré\",\"Préréglage intégré\",\"Occupé\",\"Découpe en cours\",\"Occupé avec la demande de l'assistant ; réessayez dans un instant\",\"Impossible de revenir en arrière\",\"Impossible de vérifier pour l'instant : {reason}\",\"Connexion impossible : il n'atteint que des serveurs sur internet, pas les programmes de votre ordinateur.\",\"Impossible de le supprimer\",\"Impossible de le remettre après un redémarrage ; restaurez plutôt un point antérieur\",\"Impossible de remettre les plages\",\"Impossible de le remettre comme avant\",\"Impossible de le remettre comme avant : {reason}\",\"Impossible de rebasculer\",\"Impossible de suivre cet objet après le rechargement du projet\",\"Annulé\",\"Annuler la modification\",\"Remettre {label} à {before}\",\"Modifié à nouveau depuis\",\"Modifié à nouveau depuis ; restaurez plutôt un point antérieur\",\"Application de bureau ChatGPT\",\"ChatGPT sur le web ou sur mobile\",\"Application Claude\",\"Effacer les plages de hauteur\",\"Codex dans un terminal ou un éditeur\",\"Tout réduire\",\"Commande à lancer\",\"{n} option comparée\",\"{n} option comparée · rien conservé\",\"{n} option comparée · les réglages ne sont peut-être pas revenus comme avant\",\"{n} options comparées\",\"{n} options comparées · rien conservé\",\"{n} options comparées · les réglages ne sont peut-être pas revenus comme avant\",\"La comparaison n'a pas pu remettre : {labels}\",\"Connectez votre application d'IA\",\"Connecté\",\"Connecté à {app}\",\"Copié\",\"Copier\",\"Copier plutôt les réglages .vscode/mcp.json\",\"Copier la commande\",\"Copier les réglages\",\"Copier plutôt les réglages\",\"Restauration impossible\",\"Retour impossible\",\"Calcul impossible\",\"Suppression impossible\",\"Impossible de le supprimer\",\"Déplacement impossible\",\"Impossible de le replacer\",\"Impossible de l'ouvrir\",\"Impossible d'ouvrir le point de restauration\",\"Impossible de restaurer à {time}\",\"Enregistrement impossible\",\"Impossible d'enregistrer d'abord l'état actuel\",\"Sélection impossible\",\"Impossible de l'annuler\",\"Cursor s'ouvre et vous demande de confirmer. Nécessite uv.\",\"Supprimer la plaque {n} (seulement si elle est vide)\",\"Supprimer la plaque\",\"Supprimer la plaque {n}\",\"Supprimer le préréglage\",\"Supprimer le préréglage {type} {name}\",\"Supprimer {name}\",\"Supprimer le préréglage {type}\",\"Fichier supprimé\",\"Plaque supprimée\",\"Préréglage supprimé\",\"Télécharger\",\"Perdu lors d'un changement de préréglage\",\"Perdu lors d'un changement de préréglage ; restaurez plutôt un point antérieur\",\"Dupliquer l'objet {n}\",\"Dupliqué\",\"Chaque application a besoin d'uv, un petit outil gratuit qui lance le connecteur. Une seule installation suffit.\",\"Tout développer\",\"Échoué\",\"Filament\",\"Préréglage de filament\",\"Bacs de filament\",\"Filament {n}\",\"Couleur du filament {n}\",\"Bac du filament {n}\",\"Fichier\",\"Le fichier existe déjà\",\"Fichier introuvable\",\"Les fichiers et préréglages enregistrés sur le disque sont conservés.\",\"Dossier introuvable\",\"Pour les conversations. Téléchargez l'extension, puis ouvrez le fichier. Nécessite uv.\",\"Obtenir uv\",\"Revenir à l'état d'avant la restauration\",\"Plage de hauteur {min}–{max} mm\",\"Plages de hauteur effacées\",\"Valeur masquée\",\"Dans un terminal, un éditeur ou l'onglet Code de l'application. Nécessite uv.\",\"Dans les réglages de l'extension, définissez \\\"OrcaSlicer settings folder\\\" sur {folder}.\",\"Valeur non valide\",\"Il contient maintenant des objets\",\"Il n'est plus sur la plaque\",\"Il n'existe plus\",\"Il est sélectionné ; passez d'abord à un autre préréglage\",\"C'est la dernière plaque\",\"Ses objets ont été déplacés à côté du plateau\",\"LM Studio, Windsurf et la plupart des autres acceptent cette forme. Nécessite uv.\",\"Dernière plaque\",\"Hauteur de couche hors limites\",\"Hauteur de couche réinitialisée\",\"Ligne {line} sur {count} :\",\"Lignes {first} à {last} sur {count} :\",\"Menu principal (à côté de Fichier) > Plugins > OrcaSlicer MCP > Configuration\",\"Modèle\",\"Replacer\",\"Déplacer l'objet {n}\",\"Déplacer l'objet {n} vers la plaque {index}\",\"Déplacer {name}\",\"Replacer {name} dans {path}\",\"Replacer {name} sur {plate}\",\"Replacer {name} là où il était\",\"Déplacé\",\"Déplacé à nouveau depuis\",\"Déplacé vers la plaque\",\"Nom\",\"Nécessite Préférences › Remote API › Allow deleting and replacing project files\",\"Nécessite Préférences › Remote API › Allow script, G-code and connection edits\",\"Nécessite Préférences › Remote API › Allow script, G-code and connection edits · {object}\",\"Nécessite la version OrcaSlicer MCP\",\"Jamais enregistré\",\"Nouveau projet\",\"Nouveau préréglage {type}\",\"Non\",\"Aucun bac CANVAS\",\"Aucune modification\",\"Aucun point de restauration ici\",\"Pas de place pour une autre plaque\",\"Aucune modification de ce type\",\"Aucune copie de ce type\",\"Aucun filament de ce type\",\"Aucune plaque de ce type\",\"Aucun préréglage de ce type\",\"Aucun point de restauration de ce type\",\"Aucun bac de ce type\",\"Non autorisé\",\"Non compatible\",\"Non modifiable ici\",\"Absent de la liste récente\",\"Absent de l'historique de ce projet\",\"Non remis\",\"Pas prêt\",\"Non stocké\",\"Impossible tant qu'OrcaSlicer est occupé par une tâche\",\"Impossible pendant la fermeture d'OrcaSlicer\",\"Impossible pendant le chargement d'un projet\",\"Impossible pendant la découpe\",\"Impossible pendant la reconstruction de la fenêtre\",\"Note\",\"Rien n'a été déplacé\",\"Rien sur la plaque\",\"Rien à annuler\",\"Rien à afficher dans les réglages\",\"Rien à annuler\",\"OK\",\"L'objet n'a pas de copie\",\"Objet introuvable\",\"Réglages de l'objet {n}\",\"Désactivé\",\"Activé\",\"Ouvrir le projet {name}\",\"Ouvrir {settings}\",\"Projet ouvert\",\"Ouverture…\",\"Le panneau OrcaSlicer MCP a été fermé. Rouvrez-le depuis Affichage > OrcaSlicer MCP panel.\",\"Panneau OrcaSlicer MCP fermé. Il revient quand votre assistant utilise OrcaSlicer, ou appuyez sur Espace et choisissez OrcaSlicer MCP.\",\"OrcaSlicer a signalé une erreur\",\"Autre application\",\"D'autres préréglages en héritent\",\"Obsolète : quelque chose a changé après cette découpe\",\"Hors limites\",\"Chevauche une plage\",\"Plaque\",\"Plaque verrouillée\",\"Plaque {n}\",\"La plaque {n} contient maintenant d'autres objets\",\"Préréglage\",\"Préréglage utilisé\",\"Préréglage de traitement\",\"Imprimante\",\"Préréglage d'imprimante\",\"Le projet est ouvert\",\"Remettre\",\"Remettre les plages de hauteur de {object}\",\"Remettre le profil de hauteur de couche précédent de {object}\",\"Remettre les {n} objets là où ils étaient\",\"Remettre {name} sur {plate}\",\"Qualité {value}\",\"Lecture seule\",\"Refusé\",\"Rejeté\",\"Réglages Remote API\",\"Retirer\",\"Retirer la copie\",\"Retirer l'objet {n}\",\"Retirer la plage\",\"Retirer la nouvelle copie de {name}\",\"Retirer le remplacement de {label} de {object}\",\"Retirer la plage {min}–{max} mm de {object}\",\"Retirer {name} de la plaque\",\"Retiré\",\"Renommé\",\"Préréglage {type} enregistré remplacé\",\"Réinitialiser la hauteur de couche\",\"Dossier des points de restauration\",\"Les points de restauration nécessitent la version OrcaSlicer MCP\",\"Restaurer le projet tel qu'il était à {time} (annule les modifications ultérieures)\",\"Restaurer à {time} ?\",\"Restauration annulée\",\"Restauré à {time}\",\"Restauration…\",\"Revenir\",\"Revenu en arrière\",\"Retour effectué à {time}\",\"Retour en cours…\",\"Annuler la rotation\",\"Pivoter l'objet {n}\",\"Annuler la rotation de {name}\",\"Pivoté\",\"Exécutez ceci une seule fois. L'application de bureau partage le réglage. Nécessite uv.\",\"Même chemin\",\"Enregistrer le projet\",\"Enregistrer le projet sous {name}\",\"Enregistrer le préréglage {type}\",\"Préréglage enregistré\",\"Projet enregistré\",\"Annuler la mise à l'échelle\",\"Redimensionner l'objet {n}\",\"Annuler la mise à l'échelle de {name}\",\"Redimensionné\",\"Les scripts, le G-code personnalisé et les réglages de connexion à l'imprimante sont verrouillés. Cochez 'Allow script, G-code and connection edits' dans Préférences > Remote API pour que l'assistant puisse les modifier, ou modifiez-les vous-même ici.\",\"Configurez-le là où vous utilisez votre application\",\"Configurez-le là où vous utilisez {app}\",\"Remettre la plage {min}–{max} mm de {object} à {before}\",\"Remettre {label} à {before}\",\"Remettre {label} de {object} à {before}\",\"Réglage\",\"Réglages\",\"Paramètres › Plugins › Ajouter › Ajouter un serveur MCP, choisissez STDIO et remplissez chaque champ ci-dessous. Fonctionne dans les conversations Codex ; les conversations ChatGPT ordinaires ne peuvent pas encore l'utiliser. Nécessite uv.\",\"Afficher les détails\",\"Afficher ce que c'était et le retrouver dans les réglages\",\"Afficher ce que c'était et l'annuler\",\"Afficher ce que c'était, l'annuler et le retrouver dans les réglages\",\"Résultat de découpe\",\"Découpé\",\"Découpé {where}\",\"Découpe annulée\",\"Échec de la découpe\",\"Une erreur s'est produite\",\"Une erreur s'est produite : {reason}\",\"Rebasculer\",\"Changement annulé\",\"Rebasculer {label} à {before}\",\"Rebasculer {label} à {before} (abandonne les modifications non enregistrées)\",\"Ce filament n'existe plus\",\"Remote API n'a pas pu démarrer. Vérifiez son port.\",\"Remote API est désactivé.\",\"La copie a déjà disparu\",\"L'ancien dossier n'existe plus\",\"L'ancien nom est maintenant pris\",\"La plaque a changé depuis ; restaurez plutôt un point antérieur\",\"Le plugin n'a pas démarré\",\"Le plugin ne modifie jamais les scripts, le G-code ni les réglages de connexion de l'imprimante\",\"Le projet a changé depuis\",\"Le point de restauration est illisible\",\"Le point de restauration n'existe plus\",\"Le point de restauration a été modifié sur le disque, il n'est donc pas utilisé\",\"Le réglage n'est plus dans ce préréglage\",\"L'onglet des réglages n'est pas prêt\",\"L'onglet des réglages est introuvable\",\"L'instantané d'avant la restauration n'existe plus\",\"L'instantané d'avant la restauration a été modifié sur le disque, il n'est donc pas utilisé\",\"Dites-lui ensuite quelle impression vous voulez. Chaque modification qu'il fait apparaît ici, et vous pouvez toutes les annuler.\",\"Ils contiennent peut-être encore les valeurs de la dernière option ({reason}).\",\"Bac pas prêt\",\"Bac utilisé deux fois\",\"Annuler\",\"Réglage inconnu\",\"Modifications non enregistrées\",\"Modifications non enregistrées abandonnées : {list}\",\"Fichier non pris en charge\",\"Sans titre\",\"VS Code s'ouvre et vous demande de confirmer. Nécessite uv.\",\"En attente de votre application…\",\"En attente de {app}…\",\"C'était {before}\",\"Quand un assistant modifie quelque chose, cela apparaît ici.\",\"Tour d’amorçage X\",\"Tour d’amorçage Y\",\"Nombre de valeurs incorrect\",\"Oui\",\"Modifié par vous depuis\",\"Réglages MCP de votre application\",\"Vos propres modifications depuis sont également annulées.\",\"à côté du plateau\",\"modifié par vous\",\"modifié depuis\",\"copie retirée depuis\",\"supprimé depuis\",\"perdu lors d'un changement de préréglage\",\"contient maintenant des objets\",\"couche {height} mm\",\"ligne {line} : {text} ({count} lignes)\",\"déplacé depuis\",\"aucun\",\"pas un nombre\",\"non pivoté\",\"non défini\",\"rien conservé\",\"ancien nom pris\",\"obsolète\",\"plaque {n}\",\"plaque {n} · {error}\",\"plages définies depuis\",\"retiré depuis\",\"revenu en arrière\",\"remis comme avant par vous\",\"les réglages ne sont peut-être pas revenus comme avant\",\"la plaque\",\"l'onglet des réglages n'est pas prêt\",\"vers {position}\",\"annulé par la restauration\",\"votre application\",\"{angle}° autour de {axis}\",\"{key} : n'est pas un réglage de traitement, de filament ou d'imprimante\",\"{key} : valeur non prise en charge\",\"{label} (filament {n})\",\"{label} (filaments {list})\",\"{label} doit être supérieur à {min} (reçu {got})\",\"{label} doit être supérieur à {min} (reçu {got}) ; la valeur minimale autorisée par l'imprimante se règle dans Paramètres de l'imprimante > Limites de hauteur de couche\",\"{label} doit être supérieur à {min} et au plus {max} (reçu {got})\",\"{label} doit être supérieur à {min} et au plus {max} (reçu {got}) ; la valeur minimale autorisée par l'imprimante se règle dans Paramètres de l'imprimante > Limites de hauteur de couche\",\"{label} doit être au moins {min} (reçu {got})\",\"{label} doit être au moins {min} (reçu {got}) ; la valeur minimale autorisée par l'imprimante se règle dans Paramètres de l'imprimante > Limites de hauteur de couche\",\"{label} doit être au plus {max} (reçu {got})\",\"{label} doit être au plus {max} (reçu {got}) ; la valeur minimale autorisée par l'imprimante se règle dans Paramètres de l'imprimante > Limites de hauteur de couche\",\"{label} doit être compris entre {min} et {max} (reçu {got})\",\"{label} doit être compris entre {min} et {max} (reçu {got}) ; la valeur minimale autorisée par l'imprimante se règle dans Paramètres de l'imprimante > Limites de hauteur de couche\",\"{label} non autorisé\",\"{name} ne convient plus à cette imprimante\",\"{name} n'existe plus\",\"{name} a déjà disparu\",\"{name} n'est plus sur la plaque\",\"{name} n'est plus là\",\"{name} → {plate}\",\"{name}, maintenant {n} copies\",\"{n} objet\",\"{n} objets\",\"{n} plage\",\"{n} plages\",\"{object} n'est plus sur la plaque\",\"{object} uniquement\",\"{object} uniquement · du préréglage de traitement\",\"{preset} (non enregistré)\",\"{text} ({count} lignes)\",\"{title} et {n} de plus\",\"{value} dans {preset}\",\"{verb} (indisponible : {why})\",\"{verb} {what}\",\"{verb} : {label} remis à {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"hu\": [\"(üres sor)\",\"(rejtett)\",\"Visszaállítás folyamatban\",\"Adaptív rétegmagasság\",\"Modell hozzáadása\",\"Tálca hozzáadása\",\"Hozzáadás a Cursorhoz\",\"Hozzáadás a VS Code-hoz\",\"{name} hozzáadása\",\"Hozzáadva\",\"Tálca hozzáadva\",\"Már vissza van állítva erre: {before}\",\"Add hozzá a(z) {setting} értéket környezeti változóként is.\",\"Egy objektum\",\"Argumentumok\",\"Elrendezés\",\"Elrendezve\",\"Kérd el az asszisztenstől a kívánt nyomtatást. A módosításai itt jelennek meg, mindegyik Módosítás visszavonása gombbal.\",\"Asszisztens\",\"Az asszisztens hozzáférése ki van kapcsolva.\",\"Az asszisztens beállította: {value} · volt: {before}\",\"Az asszisztens beállította: {value} · te visszaállítottad\",\"Asszisztens beállításai\",\"Automatikus tájolás\",\"Automatikusan tájolva\",\"Beépített név\",\"Beépített beállítás\",\"Elfoglalt\",\"Szeletelés folyamatban\",\"Az asszisztens kérésével elfoglalt; próbáld újra egy pillanat múlva\",\"Nem állítható vissza\",\"Most nem ellenőrizhető: {reason}\",\"Nem tud csatlakozni: csak az interneten lévő szerverekhez fér hozzá, a gépeden futó programokhoz nem.\",\"Nem törölhető\",\"Újraindítás után nem tehető vissza; állíts vissza inkább egy korábbi pontot\",\"A tartományok nem állíthatók vissza\",\"Nem állítható vissza\",\"Nem állítható vissza: {reason}\",\"Nem váltható vissza\",\"Ez az objektum a projekt újratöltése után nem követhető\",\"Megszakítva\",\"Érték visszaállítása\",\"{label} visszaállítása erre: {before}\",\"Azóta ismét módosítva\",\"Azóta ismét módosítva; állíts vissza inkább egy korábbi pontot\",\"ChatGPT asztali alkalmazás\",\"ChatGPT weben vagy telefonon\",\"Claude alkalmazás\",\"Magasságtartományok törlése\",\"Codex terminálban vagy szerkesztőben\",\"Összes összecsukása\",\"Indítandó parancs\",\"{n} opció összehasonlítva\",\"{n} opció összehasonlítva · semmi sem maradt meg\",\"{n} opció összehasonlítva · a beállítások nem biztos, hogy visszaálltak az eredetire\",\"{n} opció összehasonlítva\",\"{n} opció összehasonlítva · semmi sem maradt meg\",\"{n} opció összehasonlítva · a beállítások nem biztos, hogy visszaálltak az eredetire\",\"Az összehasonlítás nem tudta visszaállítani: {labels}\",\"Kapcsold be az MI-alkalmazásodat\",\"Csatlakoztatva\",\"Csatlakoztatva: {app}\",\"Másolva\",\"Másolás\",\"A .vscode/mcp.json beállítások másolása helyette\",\"Parancs másolása\",\"Beállítások másolása\",\"Beállítások másolása helyette\",\"Nem sikerült visszaállítani\",\"Nem sikerült visszavonni\",\"Nem sikerült kiszámítani\",\"Nem sikerült törölni\",\"Nem sikerült törölni\",\"Nem sikerült áthelyezni\",\"Nem sikerült visszahelyezni\",\"Nem sikerült megnyitni\",\"Nem sikerült megnyitni a visszaállítási pontot\",\"Nem sikerült visszaállítani a(z) {time} állapotra\",\"Nem sikerült menteni\",\"Nem sikerült előbb elmenteni a jelenlegi állapotot\",\"Nem sikerült kijelölni\",\"Nem sikerült visszavonni\",\"A Cursor megnyílik, és megerősítést kér. uv szükséges hozzá.\",\"{n}. tálca törlése (csak amíg üres)\",\"Tálca törlése\",\"{n}. tálca törlése\",\"Beállítás törlése\",\"A(z) {name} {type} beállítás törlése\",\"{name} törlése\",\"{type} beállítás törlése\",\"Fájl törölve\",\"Tálca törölve\",\"Beállítás törölve\",\"Letöltés\",\"Elvetve beállításváltás miatt\",\"Elvetve beállításváltás miatt; állíts vissza inkább egy korábbi pontot\",\"{n}. objektum duplikálása\",\"Duplikálva\",\"Minden alkalmazásnak szüksége van az uv-re, egy kis ingyenes eszközre, amely a csatlakozót futtatja. Elég egyszer telepíteni.\",\"Összes kibontása\",\"Sikertelen\",\"Filament\",\"Filament beállítás\",\"Filamenttálcák\",\"{n}. filament\",\"{n}. filament színe\",\"{n}. filamenttálca\",\"Fájl\",\"A fájl már létezik\",\"A fájl nem található\",\"A lemezre mentett fájlok és beállítások megmaradnak.\",\"A mappa nem található\",\"Csevegésekhez. Töltsd le a bővítményt, majd nyisd meg a fájlt. uv szükséges hozzá.\",\"uv letöltése\",\"Vissza a visszaállítás előtti állapotra\",\"Magasságtartomány {min}–{max} mm\",\"Magasságtartományok törölve\",\"Rejtett érték\",\"Terminálban, szerkesztőben vagy az alkalmazás Code lapján. uv szükséges hozzá.\",\"A bővítmény beállításaiban add meg az „OrcaSlicer settings folder” mezőnek ezt: {folder}.\",\"Érvénytelen érték\",\"Most vannak rajta objektumok\",\"Már nincs a tálcán\",\"Már nincs ott\",\"Ki van választva; előbb válts másik beállításra\",\"Ez az utolsó tálca\",\"Az objektumai az asztal mellé kerültek\",\"Az LM Studio, a Windsurf és a legtöbb más program ezt a formát használja. uv szükséges hozzá.\",\"Utolsó tálca\",\"A rétegmagasság tartományon kívül van\",\"Rétegmagasság visszaállítva\",\"{line}. sor / {count}:\",\"{first}.–{last}. sor / {count}:\",\"Főmenü (a Fájl mellett) > Bővítmények > OrcaSlicer MCP > Konfiguráció\",\"Modell\",\"Visszahelyezés\",\"{n}. objektum mozgatása\",\"{n}. objektum áthelyezése a(z) {index}. tálcára\",\"{name} mozgatása\",\"{name} visszahelyezése ide: {path}\",\"{name} visszahelyezése ide: {plate}\",\"{name} visszahelyezése az eredeti helyére\",\"Áthelyezve\",\"Azóta ismét áthelyezve\",\"Tálcára helyezve\",\"Név\",\"Szükséges: Beállítások › Remote API › Allow deleting and replacing project files\",\"Szükséges: Beállítások › Remote API › Allow script, G-code and connection edits\",\"Szükséges: Beállítások › Remote API › Allow script, G-code and connection edits · {object}\",\"Az OrcaSlicer MCP build szükséges hozzá\",\"Soha nem mentve\",\"Új projekt\",\"Új {type} beállítás\",\"Nem\",\"Nincs CANVAS tálca\",\"Nincs módosítás\",\"Itt nincs visszaállítási pont\",\"Nincs hely újabb tálcának\",\"Nincs ilyen módosítás\",\"Nincs ilyen másolat\",\"Nincs ilyen filament\",\"Nincs ilyen tálca\",\"Nincs ilyen beállítás\",\"Nincs ilyen visszaállítási pont\",\"Nincs ilyen tálca\",\"Nem engedélyezett\",\"Nem kompatibilis\",\"Itt nem szerkeszthető\",\"Nincs a legutóbbiak között\",\"Nincs a projekt előzményei között\",\"Nem helyezve vissza\",\"Nem kész\",\"Nincs tárolva\",\"Nem, amíg az OrcaSlicer feladattal van elfoglalva\",\"Nem, amíg az OrcaSlicer bezáródik\",\"Nem, amíg egy projekt töltődik\",\"Nem szeletelés közben\",\"Nem, amíg az ablak újraépül\",\"Megjegyzés\",\"Semmi sem mozdult\",\"Nincs semmi a tálcán\",\"Nincs mit visszavonni\",\"Nincs mit megjeleníteni a beállításokban\",\"Nincs mit visszavonni\",\"OK\",\"Az objektumnak nincs másolata\",\"Az objektum nem található\",\"{n}. objektum beállításai\",\"Ki\",\"Be\",\"{name} projekt megnyitása\",\"{settings} megnyitása\",\"Projekt megnyitva\",\"Megnyitás…\",\"Az OrcaSlicer MCP panel bezárva. Előhozhatod a Nézet > OrcaSlicer MCP panel menüpontból.\",\"Az OrcaSlicer MCP panel bezárva. Visszatér, amikor az asszisztensed legközelebb használja az OrcaSlicert, vagy nyomd meg a Szóköz billentyűt, és válaszd az OrcaSlicer MCP elemet.\",\"Az OrcaSlicer hibát jelzett\",\"Másik alkalmazás\",\"Más beállítások öröklik\",\"Elavult: valami megváltozott e szeletelés után\",\"Tartományon kívül\",\"Átfed egy tartományt\",\"Tálca\",\"Tálca zárolva\",\"{n}. tálca\",\"A(z) {n}. tálcán most más objektumok vannak\",\"Beállítás\",\"Használatban lévő beállítás\",\"Nyomtatási beállítás\",\"Nyomtató\",\"Nyomtató beállítás\",\"A projekt meg van nyitva\",\"Visszatevés\",\"{object} magasságtartományainak visszaállítása\",\"{object} előző rétegmagasság-profiljának visszaállítása\",\"A(z) {n} objektum visszahelyezése az eredeti helyére\",\"{name} visszahelyezése ide: {plate}\",\"Minőség {value}\",\"Csak olvasható\",\"Elutasítva\",\"Visszautasítva\",\"Remote API beállításai\",\"Eltávolítás\",\"Másolat eltávolítása\",\"{n}. objektum eltávolítása\",\"Tartomány eltávolítása\",\"{name} új másolatának eltávolítása\",\"{label} felülbírálásának eltávolítása innen: {object}\",\"A {min}–{max} mm tartomány eltávolítása innen: {object}\",\"{name} eltávolítása a tálcáról\",\"Eltávolítva\",\"Átnevezve\",\"A mentett {type} beállítás lecserélve\",\"Rétegmagasság visszaállítása\",\"Visszaállítási pontok mappája\",\"A visszaállítási pontokhoz az OrcaSlicer MCP build szükséges\",\"A projekt visszaállítása a(z) {time} állapotra (visszavonja a későbbi módosításokat)\",\"Visszaállítás a(z) {time} állapotra?\",\"Visszaállítás visszavonva\",\"Visszaállítva a(z) {time} állapotra\",\"Visszaállítás…\",\"Módosítás visszavonása\",\"Módosítás visszavonva\",\"Módosítás visszavonva: {time}\",\"Visszavonás…\",\"Visszaforgatás\",\"{n}. objektum forgatása\",\"{name} visszaforgatása\",\"Elforgatva\",\"Futtasd ezt egyszer. Az asztali alkalmazás ugyanazt a beállítást használja. uv szükséges hozzá.\",\"Ugyanaz az útvonal\",\"Projekt mentése\",\"Projekt mentése így: {name}\",\"{type} beállítás mentése\",\"Beállítás mentve\",\"Projekt mentve\",\"Méret visszaállítása\",\"{n}. objektum átméretezése\",\"{name} méretének visszaállítása\",\"Átméretezve\",\"A szkriptek, az egyéni G-code és a nyomtatókapcsolat beállításai zároltak. Jelöld be az 'Allow script, G-code and connection edits' opciót a Beállítások > Remote API oldalon, hogy az asszisztens módosíthassa őket, vagy módosítsd őket te itt.\",\"Állítsd be ott, ahol az alkalmazásodat használod\",\"Állítsd be ott, ahol a(z) {app} alkalmazást használod\",\"{object} {min}–{max} mm tartományának visszaállítása erre: {before}\",\"{label} visszaállítása erre: {before}\",\"{label} visszaállítása erre: {before} ({object})\",\"Paraméter\",\"Beállítások\",\"Beállítások › Bővítmények › Hozzáadás › MCP-kiszolgáló hozzáadása, válaszd a STDIO-t, és töltsd ki az alábbi mezőket. Codex-csevegésekben működik; a szokásos ChatGPT-csevegések még nem tudják használni. uv szükséges hozzá.\",\"Részletek megjelenítése\",\"Megmutatja, mi volt, és megkeresi a beállításokban\",\"Megmutatja, mi volt, és visszavonja\",\"Megmutatja, mi volt, visszavonja, és megkeresi a beállításokban\",\"Szeletelési eredmény\",\"Szeletelve\",\"Szeletelve: {where}\",\"Szeletelés megszakítva\",\"A szeletelés sikertelen\",\"Valami hiba történt\",\"Valami hiba történt: {reason}\",\"Visszaváltás\",\"Váltás megszakítva\",\"{label} visszaváltása erre: {before}\",\"{label} visszaváltása erre: {before} (elveti a nem mentett módosításokat)\",\"Ez a filament már nincs meg\",\"A Remote API nem tudott elindulni. Ellenőrizd a portját.\",\"A Remote API ki van kapcsolva.\",\"A másolat már nincs meg\",\"A régi mappa már nincs meg\",\"A régi név már foglalt\",\"A tálca azóta megváltozott; állíts vissza inkább egy korábbi pontot\",\"A bővítmény nem indult el\",\"A bővítmény soha nem módosítja a szkripteket, a G-code-ot és a nyomtató csatlakozási beállításait\",\"A projekt azóta megváltozott\",\"A visszaállítási pont nem olvasható\",\"A visszaállítási pont már nincs meg\",\"A visszaállítási pont megváltozott a lemezen, ezért nem használjuk\",\"A beállítás már nincs ebben a beállításban\",\"A beállítások lap még nem kész\",\"A beállítások lap nincs ott\",\"A visszaállítás előtti pillanatkép már nincs meg\",\"A visszaállítás előtti pillanatkép megváltozott a lemezen, ezért nem használjuk\",\"Aztán mondd el neki, milyen nyomtatást szeretnél. Minden módosítása itt jelenik meg, és bármelyiket visszavonhatod.\",\"Még mindig az utolsó opció értékei lehetnek bennük ({reason}).\",\"A tálca nem kész\",\"A tálca kétszer van használva\",\"Visszavonás\",\"Ismeretlen beállítás\",\"Nem mentett módosítások\",\"Nem mentett módosítások elvetve: {list}\",\"Nem támogatott fájl\",\"Névtelen\",\"A VS Code megnyílik, és megerősítést kér. uv szükséges hozzá.\",\"Várakozás az alkalmazásodra…\",\"Várakozás erre: {app}…\",\"Volt: {before}\",\"Amikor egy asszisztens módosít valamit, az itt jelenik meg.\",\"Törlőtorony X\",\"Törlőtorony Y\",\"Hibás számú érték\",\"Igen\",\"Azóta te módosítottad\",\"Az alkalmazásod MCP-beállításai\",\"A saját azóta végzett módosításaid is visszavonásra kerülnek.\",\"az asztal mellett\",\"általad módosítva\",\"azóta módosítva\",\"másolat azóta eltávolítva\",\"azóta törölve\",\"elvetve beállításváltás miatt\",\"most vannak objektumai\",\"{height} mm-es réteg\",\"{line}. sor: {text} ({count} sor)\",\"azóta áthelyezve\",\"nincs\",\"nem szám\",\"nincs elforgatva\",\"nincs beállítva\",\"semmi sem maradt meg\",\"a régi név foglalt\",\"elavult\",\"{n}. tálca\",\"{n}. tálca · {error}\",\"tartományok azóta beállítva\",\"azóta eltávolítva\",\"visszavonva\",\"általad visszaállítva\",\"a beállítások nem biztos, hogy visszaálltak az eredetire\",\"a tálca\",\"a beállítások lap még nem kész\",\"ide: {position}\",\"visszavonta a visszaállítás\",\"az alkalmazásod\",\"{angle}° a(z) {axis} körül\",\"{key}: nem nyomtatási, filament- vagy nyomtatóbeállítás\",\"{key}: nem támogatott érték\",\"{label} ({n}. filament)\",\"{label} (filamentek: {list})\",\"{label} értékének nagyobbnak kell lennie, mint {min} (kapott: {got})\",\"{label} értékének nagyobbnak kell lennie, mint {min} (kapott: {got}); a nyomtató által engedett legalacsonyabb érték a Nyomtatóbeállítások > Rétegmagasság limitek helyen állítható\",\"{label} értékének nagyobbnak kell lennie, mint {min}, és legfeljebb {max} lehet (kapott: {got})\",\"{label} értékének nagyobbnak kell lennie, mint {min}, és legfeljebb {max} lehet (kapott: {got}); a nyomtató által engedett legalacsonyabb érték a Nyomtatóbeállítások > Rétegmagasság limitek helyen állítható\",\"{label} értéke legalább {min} legyen (kapott: {got})\",\"{label} értéke legalább {min} legyen (kapott: {got}); a nyomtató által engedett legalacsonyabb érték a Nyomtatóbeállítások > Rétegmagasság limitek helyen állítható\",\"{label} értéke legfeljebb {max} lehet (kapott: {got})\",\"{label} értéke legfeljebb {max} lehet (kapott: {got}); a nyomtató által engedett legalacsonyabb érték a Nyomtatóbeállítások > Rétegmagasság limitek helyen állítható\",\"{label} értékének {min} és {max} között kell lennie (kapott: {got})\",\"{label} értékének {min} és {max} között kell lennie (kapott: {got}); a nyomtató által engedett legalacsonyabb érték a Nyomtatóbeállítások > Rétegmagasság limitek helyen állítható\",\"{label} nem engedélyezett\",\"{name} most nem illik ehhez a nyomtatóhoz\",\"{name} már nincs meg\",\"{name} már nincs meg\",\"{name} már nincs a tálcán\",\"{name} már nincs ott\",\"{name} → {plate}\",\"{name}, most {n} másolat\",\"{n} objektum\",\"{n} objektum\",\"{n} tartomány\",\"{n} tartomány\",\"{object} már nincs a tálcán\",\"csak {object}\",\"csak {object} · a nyomtatási beállításból\",\"{preset} (nem mentett)\",\"{text} ({count} sor)\",\"{title} és még {n}\",\"{value} itt: {preset}\",\"{verb} (nem elérhető: {why})\",\"{verb} {what}\",\"{verb}: {label} vissza erre: {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"it\": [\"(riga vuota)\",\"(nascosto)\",\"Ripristino in corso\",\"Altezza strato adattiva\",\"Aggiungi modello\",\"Aggiungi piatto\",\"Aggiungi a Cursor\",\"Aggiungi a VS Code\",\"Aggiungi {name}\",\"Aggiunto\",\"Piatto aggiunto\",\"Già tornato a {before}\",\"Aggiungi anche {setting} come variabile d'ambiente.\",\"Un oggetto\",\"Argomenti\",\"Disponi\",\"Disposto\",\"Chiedi all'assistente la stampa che desideri. Le sue modifiche compariranno qui, ognuna con Annulla modifica.\",\"Assistente\",\"L'accesso dell'assistente è disattivato.\",\"L'assistente ha impostato {value} · era {before}\",\"L'assistente ha impostato {value} · l'hai ripristinato tu\",\"Impostazioni dell'assistente\",\"Orienta automaticamente\",\"Orientato automaticamente\",\"Nome integrato\",\"Profilo integrato\",\"Occupato\",\"Elaborazione in corso\",\"Occupato con la richiesta dell'assistente; riprova tra un attimo\",\"Impossibile ripristinare il valore\",\"Impossibile verificare ora: {reason}\",\"Impossibile connettersi: raggiunge solo server su internet, non programmi sul tuo computer.\",\"Impossibile eliminarlo\",\"Impossibile rimetterlo a posto dopo un riavvio; ripristina invece un punto precedente\",\"Impossibile ripristinare gli intervalli\",\"Impossibile ripristinarlo\",\"Impossibile ripristinarlo: {reason}\",\"Impossibile tornare indietro\",\"Impossibile tracciare questo oggetto dopo il ricaricamento del progetto\",\"Annullato\",\"Ripristina valore\",\"Ripristina {label} a {before}\",\"Modificato di nuovo da allora\",\"Modificato di nuovo da allora; ripristina invece un punto precedente\",\"App desktop di ChatGPT\",\"ChatGPT sul web o sul telefono\",\"App Claude\",\"Cancella intervalli di altezza\",\"Codex in un terminale o in un editor\",\"Comprimi tutto\",\"Comando da lanciare\",\"Confrontata {n} opzione\",\"Confrontata {n} opzione · nulla conservato\",\"Confrontata {n} opzione · le impostazioni potrebbero non essere tornate come prima\",\"Confrontate {n} opzioni\",\"Confrontate {n} opzioni · nulla conservato\",\"Confrontate {n} opzioni · le impostazioni potrebbero non essere tornate come prima\",\"Il confronto non ha potuto ripristinare: {labels}\",\"Collega la tua app AI\",\"Connesso\",\"Connesso a {app}\",\"Copiato\",\"Copia\",\"Copia invece le impostazioni di .vscode/mcp.json\",\"Copia comando\",\"Copia impostazioni\",\"Copia invece le impostazioni\",\"Impossibile ripristinare\",\"Impossibile annullare la modifica\",\"Impossibile calcolare\",\"Impossibile eliminare\",\"Impossibile eliminarlo\",\"Impossibile spostare\",\"Impossibile spostarlo indietro\",\"Impossibile aprirlo\",\"Impossibile aprire il punto di ripristino\",\"Impossibile ripristinare allo stato delle {time}\",\"Impossibile salvare\",\"Impossibile salvare prima lo stato attuale\",\"Impossibile selezionare\",\"Impossibile annullarlo\",\"Cursor si apre e ti chiede di confermare. Richiede uv.\",\"Elimina Piatto {n} (solo se è vuoto)\",\"Elimina piatto\",\"Elimina piatto {n}\",\"Elimina profilo\",\"Elimina il profilo {type} {name}\",\"Elimina {name}\",\"Elimina profilo {type}\",\"File eliminato\",\"Piatto eliminato\",\"Profilo eliminato\",\"Scarica\",\"Scartato da un cambio di profilo\",\"Scartato da un cambio di profilo; ripristina invece un punto precedente\",\"Duplica oggetto {n}\",\"Duplicato\",\"Ogni app ha bisogno di uv, un piccolo strumento gratuito che avvia il connettore. Basta installarlo una volta.\",\"Espandi tutto\",\"Fallito\",\"Filamento\",\"Profilo filamento\",\"Vassoi filamento\",\"Filamento {n}\",\"Colore filamento {n}\",\"Vassoio filamento {n}\",\"File\",\"Il file esiste già\",\"File non trovato\",\"I file e i profili salvati su disco restano.\",\"Cartella non trovata\",\"Per le chat. Scarica l'estensione, poi apri il file. Richiede uv.\",\"Scarica uv\",\"Torna a prima del ripristino\",\"Intervallo altezza {min}–{max} mm\",\"Intervalli di altezza cancellati\",\"Valore nascosto\",\"In un terminale, in un editor o nella scheda Code dell'app. Richiede uv.\",\"Nelle impostazioni dell'estensione, imposta \\\"OrcaSlicer settings folder\\\" su {folder}.\",\"Valore non valido\",\"Ora contiene degli oggetti\",\"Non è più sul piatto\",\"Non esiste più\",\"È selezionato; passa prima a un altro profilo\",\"È l'ultimo piatto\",\"I suoi oggetti sono stati spostati accanto al piatto\",\"LM Studio, Windsurf e la maggior parte degli altri usano questa forma. Richiede uv.\",\"Ultimo piatto\",\"Altezza strato fuori intervallo\",\"Altezza strato reimpostata\",\"Riga {line} di {count}:\",\"Righe da {first} a {last} di {count}:\",\"Menu principale (accanto a File) > Plugin > OrcaSlicer MCP > Configurazione\",\"Modello\",\"Sposta indietro\",\"Sposta oggetto {n}\",\"Sposta oggetto {n} su Piatto {index}\",\"Sposta {name}\",\"Sposta {name} di nuovo in {path}\",\"Sposta {name} di nuovo su {plate}\",\"Rimetti {name} dove si trovava\",\"Spostato\",\"Spostato di nuovo da allora\",\"Spostato sul piatto\",\"Nome\",\"Richiede Preferenze › Remote API › Allow deleting and replacing project files\",\"Richiede Preferenze › Remote API › Allow script, G-code and connection edits\",\"Richiede Preferenze › Remote API › Allow script, G-code and connection edits · {object}\",\"Richiede la build OrcaSlicer MCP\",\"Mai salvato\",\"Nuovo progetto\",\"Nuovo profilo {type}\",\"No\",\"Nessun vassoio CANVAS\",\"Nessuna modifica\",\"Nessun punto di ripristino qui\",\"Non c'è spazio per un altro piatto\",\"Modifica inesistente\",\"Copia inesistente\",\"Filamento inesistente\",\"Piatto inesistente\",\"Profilo inesistente\",\"Punto di ripristino inesistente\",\"Vassoio inesistente\",\"Non consentito\",\"Non compatibile\",\"Non modificabile qui\",\"Non nell'elenco recenti\",\"Non nella cronologia di questo progetto\",\"Non rimesso a posto\",\"Non pronto\",\"Non memorizzato\",\"Non mentre OrcaSlicer è occupato con un lavoro\",\"Non mentre OrcaSlicer si sta chiudendo\",\"Non mentre un progetto è in caricamento\",\"Non durante l'elaborazione\",\"Non mentre la finestra viene ricostruita\",\"Nota\",\"Nulla è stato spostato\",\"Niente sul piatto\",\"Nessuna modifica da annullare\",\"Niente da mostrare nelle impostazioni\",\"Niente da annullare\",\"OK\",\"L'oggetto non ha copie\",\"Oggetto non trovato\",\"Impostazioni oggetto {n}\",\"Spento\",\"Acceso\",\"Apri progetto {name}\",\"Apri {settings}\",\"Progetto aperto\",\"Apertura…\",\"Pannello OrcaSlicer MCP chiuso. Riaprilo da Vista > OrcaSlicer MCP panel.\",\"Pannello OrcaSlicer MCP chiuso. Tornerà quando il tuo assistente userà OrcaSlicer, oppure premi Spazio e scegli OrcaSlicer MCP.\",\"OrcaSlicer ha segnalato un errore\",\"Altra app\",\"Altri profili lo ereditano\",\"Non aggiornato: qualcosa è cambiato dopo questa elaborazione\",\"Fuori intervallo\",\"Si sovrappone a un intervallo\",\"Piatto\",\"Piatto bloccato\",\"Piatto {n}\",\"Il Piatto {n} ora contiene altri oggetti\",\"Profilo\",\"Profilo in uso\",\"Profilo di stampa\",\"Stampante\",\"Profilo stampante\",\"Il progetto è aperto\",\"Rimetti a posto\",\"Ripristina gli intervalli di altezza di {object}\",\"Ripristina il profilo altezza strato precedente di {object}\",\"Rimetti i {n} oggetti dove si trovavano\",\"Rimetti {name} su {plate}\",\"Qualità {value}\",\"Sola lettura\",\"Rifiutato\",\"Respinto\",\"Impostazioni Remote API\",\"Rimuovi\",\"Rimuovi copia\",\"Rimuovi oggetto {n}\",\"Rimuovi intervallo\",\"Rimuovi la nuova copia di {name}\",\"Rimuovi l'override {label} da {object}\",\"Rimuovi l'intervallo {min}–{max} mm da {object}\",\"Rimuovi {name} dal piatto\",\"Rimosso\",\"Rinominato\",\"Profilo {type} salvato sostituito\",\"Reimposta altezza strato\",\"Cartella dei punti di ripristino\",\"I punti di ripristino richiedono la build OrcaSlicer MCP\",\"Ripristina il progetto allo stato delle {time} (annulla le modifiche successive)\",\"Ripristinare allo stato delle {time}?\",\"Ripristino annullato\",\"Ripristinato allo stato delle {time}\",\"Ripristino…\",\"Annulla modifica\",\"Modifica annullata\",\"Modifica annullata alle {time}\",\"Annullamento…\",\"Ruota indietro\",\"Ruota oggetto {n}\",\"Ruota {name} indietro\",\"Ruotato\",\"Eseguilo una sola volta. L'app desktop condivide l'impostazione. Richiede uv.\",\"Stesso percorso\",\"Salva progetto\",\"Salva progetto come {name}\",\"Salva profilo {type}\",\"Profilo salvato\",\"Progetto salvato\",\"Ripristina scala\",\"Ridimensiona oggetto {n}\",\"Ripristina la scala di {name}\",\"Ridimensionato\",\"Script, G-code personalizzato e impostazioni di connessione della stampante sono bloccati. Spunta 'Allow script, G-code and connection edits' in Preferenze > Remote API per consentire all'assistente di modificarli, oppure modificali tu qui.\",\"Configuralo dove usi la tua app\",\"Configuralo dove usi {app}\",\"Ripristina l'intervallo {min}–{max} mm di {object} a {before}\",\"Ripristina {label} a {before}\",\"Ripristina {label} su {object} a {before}\",\"Impostazione\",\"Impostazioni\",\"Impostazioni › Plugin › Aggiungi › Aggiungi server MCP, scegli STDIO e compila ogni campo qui sotto. Funziona nelle chat di Codex; le normali chat di ChatGPT non possono ancora usarlo. Richiede uv.\",\"Mostra dettagli\",\"Mostra com'era e trovalo nelle impostazioni\",\"Mostra com'era e annulla\",\"Mostra com'era, annulla e trovalo nelle impostazioni\",\"Risultato elaborazione\",\"Elaborato\",\"Elaborato {where}\",\"Elaborazione annullata\",\"Elaborazione fallita\",\"Qualcosa è andato storto\",\"Qualcosa è andato storto: {reason}\",\"Torna indietro\",\"Cambio annullato\",\"Torna da {label} a {before}\",\"Torna da {label} a {before} (scarta le modifiche non salvate)\",\"Quel filamento non esiste più\",\"Remote API non è riuscita ad avviarsi. Controlla la sua porta.\",\"Remote API è disattivata.\",\"La copia non esiste già più\",\"La vecchia cartella non esiste più\",\"Il vecchio nome è ora occupato\",\"Il piatto è cambiato da allora; ripristina invece un punto precedente\",\"Il plugin non si è avviato\",\"Il plugin non modifica mai script, G-code o impostazioni di connessione della stampante\",\"Il progetto è cambiato da allora\",\"Impossibile leggere il punto di ripristino\",\"Il punto di ripristino non esiste più\",\"Il punto di ripristino è stato modificato su disco, quindi non viene usato\",\"L'impostazione non è più in questo profilo\",\"La scheda delle impostazioni non è pronta\",\"La scheda delle impostazioni non è presente\",\"L'istantanea precedente al ripristino non esiste più\",\"L'istantanea precedente al ripristino è stata modificata su disco, quindi non viene usata\",\"Poi descrivi la stampa che vuoi. Ogni modifica apportata compare qui e puoi annullarla.\",\"Potrebbero contenere ancora i valori dell'ultima opzione ({reason}).\",\"Vassoio non pronto\",\"Vassoio usato due volte\",\"Annulla\",\"Impostazione sconosciuta\",\"Modifiche non salvate\",\"Modifiche non salvate scartate: {list}\",\"File non supportato\",\"Senza titolo\",\"VS Code si apre e ti chiede di confermare. Richiede uv.\",\"In attesa della tua app…\",\"In attesa di {app}…\",\"Era {before}\",\"Quando un assistente cambia qualcosa, compare qui.\",\"Torre di spurgo X\",\"Torre di spurgo Y\",\"Numero di valori errato\",\"Sì\",\"L'hai modificato tu da allora\",\"Impostazioni MCP della tua app\",\"Anche le tue modifiche successive vengono annullate.\",\"accanto al piatto\",\"modificato da te\",\"modificato da allora\",\"copia rimossa da allora\",\"eliminato da allora\",\"scartato da un cambio di profilo\",\"ora ha degli oggetti\",\"strato {height} mm\",\"riga {line}: {text} ({count} righe)\",\"spostato da allora\",\"nessuno\",\"non è un numero\",\"non ruotato\",\"non impostato\",\"nulla conservato\",\"vecchio nome occupato\",\"non aggiornato\",\"piatto {n}\",\"piatto {n} · {error}\",\"intervalli impostati da allora\",\"rimosso da allora\",\"modifica annullata\",\"ripristinato da te\",\"le impostazioni potrebbero non essere tornate come prima\",\"il piatto\",\"la scheda delle impostazioni non è pronta\",\"a {position}\",\"annullato dal ripristino\",\"la tua app\",\"{angle}° attorno a {axis}\",\"{key}: non è un'impostazione di stampa, filamento o stampante\",\"{key}: valore non supportato\",\"{label} (filamento {n})\",\"{label} (filamenti {list})\",\"{label} deve essere superiore a {min} (ricevuto {got})\",\"{label} deve essere superiore a {min} (ricevuto {got}); il minimo consentito dalla stampante si imposta in Impostazioni stampante > Limiti altezza strati\",\"{label} deve essere superiore a {min} e al massimo {max} (ricevuto {got})\",\"{label} deve essere superiore a {min} e al massimo {max} (ricevuto {got}); il minimo consentito dalla stampante si imposta in Impostazioni stampante > Limiti altezza strati\",\"{label} deve essere almeno {min} (ricevuto {got})\",\"{label} deve essere almeno {min} (ricevuto {got}); il minimo consentito dalla stampante si imposta in Impostazioni stampante > Limiti altezza strati\",\"{label} deve essere al massimo {max} (ricevuto {got})\",\"{label} deve essere al massimo {max} (ricevuto {got}); il minimo consentito dalla stampante si imposta in Impostazioni stampante > Limiti altezza strati\",\"{label} deve essere compreso tra {min} e {max} (ricevuto {got})\",\"{label} deve essere compreso tra {min} e {max} (ricevuto {got}); il minimo consentito dalla stampante si imposta in Impostazioni stampante > Limiti altezza strati\",\"{label} non consentito\",\"{name} ora non è adatto a questa stampante\",\"{name} non esiste più\",\"{name} non esiste già più\",\"{name} non è più sul piatto\",\"{name} non è più lì\",\"{name} → {plate}\",\"{name}, ora {n} copie\",\"{n} oggetto\",\"{n} oggetti\",\"{n} intervallo\",\"{n} intervalli\",\"{object} non è più sul piatto\",\"solo {object}\",\"solo {object} · dal profilo di stampa\",\"{preset} (non salvato)\",\"{text} ({count} righe)\",\"{title} e altri {n}\",\"{value} in {preset}\",\"{verb} (non disponibile: {why})\",\"{verb} {what}\",\"{verb}: {label} a {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"ja\": [\"(空行)\",\"(非表示)\",\"復元を実行中です\",\"適応積層ピッチ\",\"モデルを追加\",\"プレートを追加\",\"Cursor に追加\",\"VS Code に追加\",\"{name} を追加\",\"追加しました\",\"プレートを追加しました\",\"すでに {before} に戻っています\",\"環境変数として {setting} も追加してください。\",\"オブジェクト\",\"引数\",\"レイアウト\",\"レイアウトしました\",\"造形したいものを伝えてください。変更はここに表示され、それぞれ元に戻せます。\",\"アシスタント\",\"アシスタントのアクセスはオフです。\",\"アシスタントが {value} に設定 · 元は {before}\",\"アシスタントが {value} に設定 · あなたが元に戻しました\",\"アシスタント設定\",\"向き調整\",\"向きを調整しました\",\"組み込みの名前\",\"組み込みのプリセット\",\"ビジー状態\",\"スライス中\",\"アシスタントのリクエストを処理中です。しばらくしてからもう一度お試しください\",\"元に戻せません\",\"今は確認できません: {reason}\",\"接続できません。インターネット上のサーバーにしか届かず、お使いのコンピューター上のプログラムには届きません。\",\"削除できません\",\"再起動後は元に戻せません。代わりに以前の時点に復元してください\",\"範囲を元に戻せません\",\"元の値に戻せません\",\"元の値に戻せません: {reason}\",\"切り戻せません\",\"プロジェクトが再読み込みされたため、このオブジェクトを追跡できません\",\"キャンセルしました\",\"変更を戻す\",\"{label} を {before} に戻す\",\"その後に再度変更されています\",\"その後に再度変更されています。代わりに以前の時点に復元してください\",\"ChatGPT デスクトップアプリ\",\"Web またはスマートフォンの ChatGPT\",\"Claude アプリ\",\"高さ範囲をクリア\",\"ターミナルまたはエディターの Codex\",\"すべて折りたたむ\",\"起動用コマンド\",\"{n} 件のオプションを比較\",\"{n} 件のオプションを比較 · 何も残していません\",\"{n} 件のオプションを比較 · 設定が元に戻っていない可能性があります\",\"{n} 件のオプションを比較\",\"{n} 件のオプションを比較 · 何も残していません\",\"{n} 件のオプションを比較 · 設定が元に戻っていない可能性があります\",\"比較で元に戻せなかった項目: {labels}\",\"AI アプリを接続\",\"接続済み\",\"{app} に接続済み\",\"コピーしました\",\"コピー\",\"代わりに .vscode/mcp.json の設定をコピー\",\"コマンドをコピー\",\"設定をコピー\",\"代わりに設定をコピー\",\"復元できませんでした\",\"元に戻せませんでした\",\"計算できませんでした\",\"削除できませんでした\",\"削除できませんでした\",\"移動できませんでした\",\"移動を元に戻せませんでした\",\"開けませんでした\",\"復元ポイントを開けませんでした\",\"{time} に復元できませんでした\",\"保存できませんでした\",\"先に現在の状態を保存できませんでした\",\"選択できませんでした\",\"元に戻せませんでした\",\"Cursor が開き、確認を求められます。uv が必要です。\",\"プレート {n} を削除 (空の場合のみ)\",\"プレートを削除\",\"プレート {n} を削除\",\"プリセットを削除\",\"{type} プリセット {name} を削除\",\"{name} を削除\",\"{type} プリセットを削除\",\"ファイルを削除しました\",\"プレートを削除しました\",\"プリセットを削除しました\",\"ダウンロード\",\"プリセットの切り替えで破棄されました\",\"プリセットの切り替えで破棄されました。代わりに以前の時点に復元してください\",\"オブジェクト {n} を複製\",\"複製しました\",\"どのアプリにも uv が必要です。コネクタを実行する無料の小さなツールで、インストールは一度だけです。\",\"すべて展開\",\"失敗\",\"フィラメント\",\"フィラメントプリセット\",\"フィラメントトレイ\",\"フィラメント {n}\",\"フィラメント {n} の色\",\"フィラメント {n} のトレイ\",\"ファイル\",\"ファイルが既にあります\",\"ファイルが見つかりません\",\"ディスクに保存したファイルとプリセットはそのまま残ります。\",\"フォルダーが見つかりません\",\"チャット用です。拡張機能をダウンロードしてから、そのファイルを開いてください。uv が必要です。\",\"uv を入手\",\"復元前の状態に戻す\",\"高さ範囲 {min}–{max} mm\",\"高さ範囲をクリアしました\",\"非表示の値\",\"ターミナル、エディター、またはアプリの Code タブで使えます。uv が必要です。\",\"拡張機能の設定で、「OrcaSlicer settings folder」を {folder} に設定してください。\",\"無効な値\",\"現在はオブジェクトが載っています\",\"プレートの上にはもうありません\",\"すでに存在しません\",\"選択中です。先に別のプリセットに切り替えてください\",\"最後のプレートです\",\"オブジェクトはベッドの横に移動しました\",\"LM Studio、Windsurf、ほかのほとんどのアプリはこの形式を使います。uv が必要です。\",\"最後のプレート\",\"積層ピッチが範囲外です\",\"積層ピッチをリセットしました\",\"{count} 行中 {line} 行目:\",\"{count} 行中 {first}～{last} 行目:\",\"メインメニュー（ファイルの横） > プラグイン > OrcaSlicer MCP > 設定\",\"モデル\",\"移動を戻す\",\"オブジェクト {n} を移動\",\"オブジェクト {n} をプレート {index} に移動\",\"{name} を移動\",\"{name} を {path} に戻す\",\"{name} を {plate} に戻す\",\"{name} を元の位置に戻す\",\"移動しました\",\"その後に再度移動されています\",\"プレートに移動しました\",\"名前\",\"設定 › Remote API › Allow deleting and replacing project files が必要です\",\"設定 › Remote API › Allow script, G-code and connection edits が必要です\",\"設定 › Remote API › Allow script, G-code and connection edits が必要です · {object}\",\"OrcaSlicer MCP ビルドが必要です\",\"一度も保存されていません\",\"新規プロジェクト\",\"新しい {type} プリセット\",\"いいえ\",\"CANVAS トレイがありません\",\"変更なし\",\"ここには復元ポイントがありません\",\"これ以上プレートを追加する空きがありません\",\"該当する変更がありません\",\"該当するコピーがありません\",\"該当するフィラメントがありません\",\"該当するプレートがありません\",\"該当するプリセットがありません\",\"該当する復元ポイントがありません\",\"該当するトレイがありません\",\"許可されていません\",\"互換性がありません\",\"ここでは編集できません\",\"最近のリストにありません\",\"このプロジェクトのタイムラインにありません\",\"元に戻せていません\",\"準備できていません\",\"保存されていません\",\"OrcaSlicer がジョブを処理中の間は実行できません\",\"OrcaSlicer の終了中は実行できません\",\"プロジェクトの読み込み中は実行できません\",\"スライス中は実行できません\",\"ウィンドウの再構築中は実行できません\",\"メモ\",\"移動したものはありません\",\"プレート上には何もありません\",\"元に戻す変更はありません\",\"設定に表示するものはありません\",\"元に戻すものはありません\",\"OK\",\"オブジェクトにコピーがありません\",\"オブジェクトが見つかりません\",\"オブジェクト設定 {n}\",\"オフ\",\"オン\",\"プロジェクト {name} を開く\",\"{settings} を開く\",\"プロジェクトを開きました\",\"開いています…\",\"OrcaSlicer MCP パネルを閉じました。表示 > OrcaSlicer MCP panel から再表示できます。\",\"OrcaSlicer MCP パネルを閉じました。アシスタントが次に OrcaSlicer を使うと再び表示されます。スペースキーを押して OrcaSlicer MCP を選んでも表示できます。\",\"OrcaSlicer がエラーを報告しました\",\"その他のアプリ\",\"ほかのプリセットがこれを継承しています\",\"古い情報: このスライスの後に何かが変更されました\",\"範囲外\",\"別の範囲と重なっています\",\"プレート\",\"プレートはロックされています\",\"プレート {n}\",\"プレート {n} には現在ほかのオブジェクトがあります\",\"プリセット\",\"プリセットは使用中です\",\"プロセスプリセット\",\"プリンター\",\"プリンタープリセット\",\"プロジェクトは開いています\",\"戻す\",\"{object} の高さ範囲を元に戻す\",\"{object} の以前の積層ピッチプロファイルを元に戻す\",\"{n} 個のオブジェクトを元の位置に戻す\",\"{name} を {plate} に戻す\",\"品質 {value}\",\"読み取り専用\",\"拒否\",\"却下されました\",\"Remote API 設定\",\"削除\",\"コピーを削除\",\"オブジェクト {n} を削除\",\"範囲を削除\",\"{name} の新しいコピーを削除\",\"{object} から {label} の上書きを削除\",\"{object} から {min}–{max} mm の範囲を削除\",\"{name} をプレートから削除\",\"削除しました\",\"名前を変更しました\",\"保存済みの {type} プリセットを置き換えました\",\"積層ピッチをリセット\",\"復元ポイントのフォルダー\",\"復元ポイントには OrcaSlicer MCP ビルドが必要です\",\"プロジェクトを {time} の状態に復元 (それ以降の変更は取り消されます)\",\"{time} に復元しますか?\",\"復元を取り消しました\",\"{time} に復元しました\",\"復元中…\",\"元に戻す\",\"元に戻しました\",\"{time} に元に戻しました\",\"元に戻しています…\",\"回転を戻す\",\"オブジェクト {n} を回転\",\"{name} の回転を戻す\",\"回転しました\",\"一度だけ実行してください。デスクトップアプリも同じ設定を共有します。uv が必要です。\",\"同じパス\",\"プロジェクトを保存\",\"プロジェクトを {name} として保存\",\"{type} プリセットを保存\",\"プリセットを保存しました\",\"プロジェクトを保存しました\",\"スケールを戻す\",\"オブジェクト {n} をスケール\",\"{name} のスケールを戻す\",\"スケールしました\",\"スクリプト、カスタム G-code、プリンター接続の設定はロックされています。アシスタントに変更を許可するには、設定 > Remote API の 'Allow script, G-code and connection edits' にチェックを入れてください。ここで自分で変更することもできます。\",\"お使いのアプリを使う場所で設定してください\",\"{app} を使う場所で設定してください\",\"{object} の {min}–{max} mm の範囲を {before} に戻す\",\"{label} を {before} に戻す\",\"{object} の {label} を {before} に戻す\",\"設定\",\"設定\",\"設定 › プラグイン › 追加 › MCP サーバーを追加 を開き、STDIO を選んで下の各欄に入力してください。Codex のチャットで使えます。通常の ChatGPT のチャットではまだ使えません。uv が必要です。\",\"詳細を表示\",\"元の値を表示して設定で探す\",\"元の値を表示して元に戻す\",\"元の値を表示し、元に戻し、設定で探す\",\"スライス結果\",\"スライスしました\",\"{where} をスライスしました\",\"スライスをキャンセルしました\",\"スライスに失敗しました\",\"問題が発生しました\",\"問題が発生しました: {reason}\",\"切り戻す\",\"切り替えをキャンセルしました\",\"{label} を {before} に切り戻す\",\"{label} を {before} に切り戻す (未保存の編集は破棄されます)\",\"そのフィラメントはもうありません\",\"Remote API を開始できませんでした。ポートを確認してください。\",\"Remote API はオフです。\",\"コピーはすでにありません\",\"元のフォルダーはもうありません\",\"元の名前はすでに使われています\",\"プレートがその後に変更されています。代わりに以前の時点に復元してください\",\"プラグインが起動しませんでした\",\"プラグインがスクリプト、G-code、プリンター接続の設定を変更することはありません\",\"プロジェクトがその後に変更されています\",\"復元ポイントを読み取れません\",\"復元ポイントはもうありません\",\"復元ポイントがディスク上で変更されているため、使用しません\",\"この設定はもうこのプリセットにありません\",\"設定タブの準備ができていません\",\"設定タブが見つかりません\",\"復元前のスナップショットはもうありません\",\"復元前のスナップショットがディスク上で変更されているため、使用しません\",\"次に、造形したいものを伝えてください。アシスタントが行った変更はすべてここに表示され、どれでも元に戻せます。\",\"最後のオプションの値が残っている可能性があります ({reason})。\",\"トレイの準備ができていません\",\"トレイが 2 回使われています\",\"元に戻す\",\"不明な設定\",\"未保存の変更\",\"未保存の編集を破棄しました: {list}\",\"非対応のファイル\",\"無題\",\"VS Code が開き、確認を求められます。uv が必要です。\",\"お使いのアプリを待っています…\",\"{app} を待っています…\",\"元は {before}\",\"アシスタントが何かを変更すると、ここに表示されます。\",\"ワイプタワー X\",\"ワイプタワー Y\",\"値の数が正しくありません\",\"はい\",\"その後にあなたが変更しました\",\"お使いのアプリの MCP 設定\",\"それ以降のあなた自身の変更も取り消されます。\",\"ベッドの横\",\"あなたが変更\",\"その後に変更\",\"その後にコピーを削除\",\"その後に削除\",\"プリセット切り替えで破棄\",\"現在オブジェクトあり\",\"積層 {height} mm\",\"{line} 行目: {text} ({count} 行)\",\"その後に移動\",\"なし\",\"数値ではありません\",\"回転なし\",\"未設定\",\"何も残していません\",\"元の名前は使用済み\",\"古い\",\"プレート {n}\",\"プレート {n} · {error}\",\"その後に範囲を設定\",\"その後に削除\",\"元に戻しました\",\"あなたが元に戻しました\",\"設定が元に戻っていない可能性があります\",\"プレート\",\"設定タブの準備ができていません\",\"{position} へ\",\"復元で取り消されました\",\"お使いのアプリ\",\"{axis} 軸まわりに {angle}°\",\"{key}: プロセス、フィラメント、プリンターのいずれの設定でもありません\",\"{key}: 非対応の値\",\"{label} (フィラメント {n})\",\"{label} (フィラメント {list})\",\"{label} は {min} より大きくする必要があります (現在 {got})\",\"{label} は {min} より大きくする必要があります (現在 {got})。プリンターが許す最小値は プリンター設定 > 積層ピッチの制限 で設定します\",\"{label} は {min} より大きく {max} 以下にする必要があります (現在 {got})\",\"{label} は {min} より大きく {max} 以下にする必要があります (現在 {got})。プリンターが許す最小値は プリンター設定 > 積層ピッチの制限 で設定します\",\"{label} は {min} 以上にする必要があります (現在 {got})\",\"{label} は {min} 以上にする必要があります (現在 {got})。プリンターが許す最小値は プリンター設定 > 積層ピッチの制限 で設定します\",\"{label} は {max} 以下にする必要があります (現在 {got})\",\"{label} は {max} 以下にする必要があります (現在 {got})。プリンターが許す最小値は プリンター設定 > 積層ピッチの制限 で設定します\",\"{label} は {min} から {max} の間にする必要があります (現在 {got})\",\"{label} は {min} から {max} の間にする必要があります (現在 {got})。プリンターが許す最小値は プリンター設定 > 積層ピッチの制限 で設定します\",\"{label} は許可されていません\",\"{name} は今のプリンターには合いません\",\"{name} はもうありません\",\"{name} はすでにありません\",\"{name} はプレートの上にはもうありません\",\"{name} はもうそこにありません\",\"{name} → {plate}\",\"{name}、現在 {n} 個\",\"{n} 個のオブジェクト\",\"{n} 個のオブジェクト\",\"{n} 件の範囲\",\"{n} 件の範囲\",\"{object} はプレートの上にはもうありません\",\"{object} のみ\",\"{object} のみ · プロセスプリセットから\",\"{preset} (未保存)\",\"{text} ({count} 行)\",\"{title} ほか {n} 件\",\"{preset} の {value}\",\"{verb} (利用不可: {why})\",\"{what} を{verb}\",\"{verb}: {label} を {before} に戻す\",\"{x} × {y} × {z} mm\"],\n"
    "\"ko\": [\"(빈 줄)\",\"(숨김)\",\"복원이 진행 중입니다\",\"적응형 레이어 높이\",\"모델 추가\",\"플레이트 추가\",\"Cursor에 추가\",\"VS Code에 추가\",\"{name} 추가\",\"추가됨\",\"플레이트 추가됨\",\"이미 {before}(으)로 되돌아왔습니다\",\"{setting}도 환경 변수로 추가하세요.\",\"객체\",\"인자\",\"정렬\",\"정렬됨\",\"원하는 출력물을 요청하세요. 변경 내용이 여기에 표시되며 각각 되돌릴 수 있습니다.\",\"어시스턴트\",\"어시스턴트 접근이 꺼져 있습니다.\",\"어시스턴트가 {value}(으)로 설정 · 이전 값 {before}\",\"어시스턴트가 {value}(으)로 설정 · 사용자가 되돌림\",\"어시스턴트 설정\",\"방향 자동 조정\",\"방향 자동 조정됨\",\"기본 제공 이름\",\"기본 제공 사전 설정\",\"사용중\",\"슬라이싱 중\",\"어시스턴트의 요청을 처리 중입니다. 잠시 후 다시 시도하세요\",\"되돌릴 수 없습니다\",\"지금은 확인할 수 없습니다: {reason}\",\"연결할 수 없습니다. 인터넷에 있는 서버에만 접속할 수 있고, 사용자 컴퓨터의 프로그램에는 접속할 수 없습니다.\",\"삭제할 수 없습니다\",\"다시 시작한 뒤에는 되돌릴 수 없습니다. 이전 시점으로 복원하세요\",\"범위를 되돌릴 수 없습니다\",\"이전 값으로 되돌릴 수 없습니다\",\"이전 값으로 되돌릴 수 없습니다: {reason}\",\"다시 전환할 수 없습니다\",\"프로젝트를 다시 불러온 뒤에는 이 객체를 추적할 수 없습니다\",\"취소됨\",\"변경 되돌리기\",\"{label}을(를) {before}(으)로 되돌리기\",\"이후 다시 변경됨\",\"이후 다시 변경되었습니다. 이전 시점으로 복원하세요\",\"ChatGPT 데스크톱 앱\",\"웹 또는 휴대폰의 ChatGPT\",\"Claude 앱\",\"높이 범위 지우기\",\"터미널 또는 편집기의 Codex\",\"모두 접기\",\"실행 명령\",\"옵션 {n}개 비교\",\"옵션 {n}개 비교 · 남긴 항목 없음\",\"옵션 {n}개 비교 · 설정이 원래대로 돌아오지 않았을 수 있음\",\"옵션 {n}개 비교\",\"옵션 {n}개 비교 · 남긴 항목 없음\",\"옵션 {n}개 비교 · 설정이 원래대로 돌아오지 않았을 수 있음\",\"비교 후 되돌리지 못한 항목: {labels}\",\"AI 앱 연결\",\"연결됨\",\"{app}에 연결됨\",\"복사됨\",\"복사\",\"대신 .vscode/mcp.json 설정 복사\",\"명령 복사\",\"설정 복사\",\"대신 설정 복사\",\"복원하지 못했습니다\",\"되돌리지 못했습니다\",\"계산하지 못했습니다\",\"삭제하지 못했습니다\",\"삭제하지 못했습니다\",\"이동하지 못했습니다\",\"이동을 되돌리지 못했습니다\",\"열지 못했습니다\",\"복원 지점을 열지 못했습니다\",\"{time}(으)로 복원하지 못했습니다\",\"저장하지 못했습니다\",\"먼저 현재 상태를 저장하지 못했습니다\",\"선택하지 못했습니다\",\"실행 취소하지 못했습니다\",\"Cursor가 열리고 확인을 요청합니다. uv가 필요합니다.\",\"플레이트 {n} 삭제 (비어 있을 때만)\",\"플레이트 삭제\",\"플레이트 {n} 삭제\",\"사전 설정 삭제\",\"{type} 사전 설정 {name} 삭제\",\"{name} 삭제\",\"{type} 사전 설정 삭제\",\"파일 삭제됨\",\"플레이트 삭제됨\",\"사전 설정 삭제됨\",\"다운로드\",\"사전 설정 전환으로 삭제됨\",\"사전 설정 전환으로 삭제되었습니다. 이전 시점으로 복원하세요\",\"객체 {n} 복제\",\"복제됨\",\"모든 앱에 uv가 필요합니다. 커넥터를 실행하는 작은 무료 도구이며, 한 번만 설치하면 됩니다.\",\"모두 펼치기\",\"실패\",\"필라멘트\",\"필라멘트 사전 설정\",\"필라멘트 트레이\",\"필라멘트 {n}\",\"필라멘트 {n} 색상\",\"필라멘트 {n} 트레이\",\"파일\",\"파일이 이미 있음\",\"파일을 찾을 수 없음\",\"디스크에 저장된 파일과 사전 설정은 그대로 유지됩니다.\",\"폴더를 찾을 수 없음\",\"채팅용입니다. 확장 프로그램을 다운로드한 다음 파일을 여세요. uv가 필요합니다.\",\"uv 받기\",\"복원 이전 상태로 되돌아가기\",\"높이 범위 {min}–{max} mm\",\"높이 범위 지워짐\",\"숨겨진 값\",\"터미널, 편집기 또는 앱의 Code 탭에서 사용합니다. uv가 필요합니다.\",\"확장 프로그램 설정에서 \\\"OrcaSlicer settings folder\\\"를 {folder}(으)로 설정하세요.\",\"잘못된 값\",\"지금은 객체가 놓여 있습니다\",\"더 이상 플레이트 위에 없습니다\",\"더 이상 없습니다\",\"선택되어 있습니다. 먼저 다른 사전 설정으로 전환하세요\",\"마지막 플레이트입니다\",\"객체가 베드 옆으로 이동했습니다\",\"LM Studio, Windsurf 및 대부분의 다른 앱은 이 형식을 사용합니다. uv가 필요합니다.\",\"마지막 플레이트\",\"레이어 높이가 범위를 벗어남\",\"레이어 높이 초기화됨\",\"{count}줄 중 {line}번째 줄:\",\"{count}줄 중 {first}~{last}번째 줄:\",\"메인 메뉴(파일 옆) > 플러그인 > OrcaSlicer MCP > 구성\",\"모델\",\"이동 되돌리기\",\"객체 {n} 이동\",\"객체 {n}을(를) 플레이트 {index}(으)로 이동\",\"{name} 이동\",\"{name}을(를) {path}(으)로 되돌리기\",\"{name}을(를) {plate}(으)로 되돌리기\",\"{name}을(를) 원래 위치로 되돌리기\",\"이동됨\",\"이후 다시 이동됨\",\"플레이트로 이동됨\",\"이름\",\"기본 설정 › Remote API › Allow deleting and replacing project files 필요\",\"기본 설정 › Remote API › Allow script, G-code and connection edits 필요\",\"기본 설정 › Remote API › Allow script, G-code and connection edits 필요 · {object}\",\"OrcaSlicer MCP 빌드가 필요합니다\",\"저장된 적 없음\",\"새 프로젝트\",\"새 {type} 사전 설정\",\"아니오\",\"CANVAS 트레이 없음\",\"변경 사항 없음\",\"여기에는 복원 지점이 없습니다\",\"플레이트를 더 추가할 공간이 없습니다\",\"해당 변경 사항이 없습니다\",\"해당 복사본이 없습니다\",\"해당 필라멘트가 없습니다\",\"해당 플레이트가 없습니다\",\"해당 사전 설정이 없습니다\",\"해당 복원 지점이 없습니다\",\"해당 트레이가 없습니다\",\"허용되지 않음\",\"호환되지 않음\",\"여기서는 편집할 수 없음\",\"최근 목록에 없음\",\"이 프로젝트의 타임라인에 없음\",\"되돌리지 못함\",\"준비되지 않음\",\"저장되지 않음\",\"OrcaSlicer가 작업 중일 때는 할 수 없습니다\",\"OrcaSlicer가 종료 중일 때는 할 수 없습니다\",\"프로젝트를 불러오는 중에는 할 수 없습니다\",\"슬라이싱 중에는 할 수 없습니다\",\"창을 다시 구성하는 중에는 할 수 없습니다\",\"메모\",\"이동된 항목 없음\",\"플레이트에 아무것도 없음\",\"되돌릴 항목이 없습니다\",\"설정에 표시할 항목이 없습니다\",\"실행 취소할 항목이 없습니다\",\"확인\",\"객체에 복사본이 없습니다\",\"객체를 찾을 수 없음\",\"객체 설정 {n}\",\"끄기\",\"켜기\",\"프로젝트 {name} 열기\",\"{settings} 열기\",\"프로젝트 열림\",\"여는 중…\",\"OrcaSlicer MCP 패널이 닫혔습니다. 시점 > OrcaSlicer MCP panel에서 다시 열 수 있습니다.\",\"OrcaSlicer MCP 패널이 닫혔습니다. 어시스턴트가 다음에 OrcaSlicer를 사용하면 다시 나타나며, 스페이스바를 눌러 OrcaSlicer MCP를 선택해도 됩니다.\",\"OrcaSlicer에서 오류가 보고되었습니다\",\"다른 앱\",\"다른 사전 설정이 이것을 상속합니다\",\"최신 상태 아님: 이 슬라이스 이후 변경된 내용이 있습니다\",\"범위를 벗어남\",\"다른 범위와 겹침\",\"플레이트\",\"플레이트가 잠겨 있음\",\"플레이트 {n}\",\"플레이트 {n}에 지금 다른 객체가 있습니다\",\"사전 설정\",\"사전 설정 사용 중\",\"프로세스 사전 설정\",\"프린터\",\"프린터 사전 설정\",\"프로젝트가 열려 있음\",\"되돌리기\",\"{object}의 높이 범위 되돌리기\",\"{object}의 이전 레이어 높이 프로파일 되돌리기\",\"객체 {n}개를 원래 위치로 되돌리기\",\"{name}을(를) {plate}에 되돌리기\",\"품질 {value}\",\"읽기 전용\",\"거부됨\",\"거절됨\",\"Remote API 설정\",\"제거\",\"복사본 제거\",\"객체 {n} 제거\",\"범위 제거\",\"{name}의 새 복사본 제거\",\"{object}에서 {label} 덮어쓰기 제거\",\"{object}에서 {min}–{max} mm 범위 제거\",\"{name}을(를) 플레이트에서 제거\",\"제거됨\",\"이름 변경됨\",\"저장된 {type} 사전 설정을 대체했습니다\",\"레이어 높이 초기화\",\"복원 지점 폴더\",\"복원 지점에는 OrcaSlicer MCP 빌드가 필요합니다\",\"프로젝트를 {time} 상태로 복원 (이후 변경 사항이 취소됩니다)\",\"{time}(으)로 복원할까요?\",\"복원 취소됨\",\"{time}(으)로 복원됨\",\"복원하는 중…\",\"되돌리기\",\"되돌림\",\"{time}에 되돌림\",\"되돌리는 중…\",\"회전 되돌리기\",\"객체 {n} 회전\",\"{name} 회전 되돌리기\",\"회전됨\",\"한 번만 실행하세요. 데스크톱 앱이 이 설정을 공유합니다. uv가 필요합니다.\",\"같은 경로\",\"프로젝트 저장\",\"프로젝트를 {name}(으)로 저장\",\"{type} 사전 설정 저장\",\"사전 설정 저장됨\",\"프로젝트 저장됨\",\"배율 되돌리기\",\"객체 {n} 배율 조정\",\"{name} 배율 되돌리기\",\"배율 조정됨\",\"스크립트, 사용자 지정 G-code, 프린터 연결 설정은 잠겨 있습니다. 어시스턴트가 변경하도록 허용하려면 기본 설정 > Remote API에서 'Allow script, G-code and connection edits'를 선택하거나, 여기서 직접 변경하세요.\",\"사용 중인 앱을 쓰는 곳에서 설정하세요\",\"{app}을(를) 사용하는 곳에서 설정하세요\",\"{object}의 {min}–{max} mm 범위를 {before}(으)로 되돌리기\",\"{label}을(를) {before}(으)로 되돌리기\",\"{object}의 {label}을(를) {before}(으)로 되돌리기\",\"설정\",\"설정\",\"설정 › 플러그인 › 추가 › MCP 서버 추가에서 STDIO를 선택하고 아래 각 칸을 채우세요. Codex 채팅에서 작동하며, 일반 ChatGPT 채팅에서는 아직 사용할 수 없습니다. uv가 필요합니다.\",\"세부 정보 표시\",\"이전 값을 보고 설정에서 찾기\",\"이전 값을 보고 되돌리기\",\"이전 값을 보고, 되돌리고, 설정에서 찾기\",\"슬라이스 결과\",\"슬라이스됨\",\"{where} 슬라이스됨\",\"슬라이싱이 취소되었습니다\",\"슬라이싱 실패\",\"문제가 발생했습니다\",\"문제가 발생했습니다: {reason}\",\"다시 전환\",\"전환이 취소되었습니다\",\"{label}을(를) {before}(으)로 다시 전환\",\"{label}을(를) {before}(으)로 다시 전환 (저장하지 않은 편집 내용이 삭제됩니다)\",\"해당 필라멘트가 더 이상 없습니다\",\"Remote API를 시작하지 못했습니다. 포트를 확인하세요.\",\"Remote API가 꺼져 있습니다.\",\"복사본이 이미 없습니다\",\"이전 폴더가 더 이상 없습니다\",\"이전 이름을 이미 사용 중입니다\",\"이후 플레이트가 변경되었습니다. 이전 시점으로 복원하세요\",\"플러그인이 시작되지 않았습니다\",\"플러그인은 스크립트, G-code, 프린터 연결 설정을 변경하지 않습니다\",\"이후 프로젝트가 변경되었습니다\",\"복원 지점을 읽을 수 없습니다\",\"복원 지점이 더 이상 없습니다\",\"복원 지점이 디스크에서 변경되어 사용하지 않습니다\",\"이 설정은 더 이상 이 사전 설정에 없습니다\",\"설정 탭이 준비되지 않았습니다\",\"설정 탭을 찾을 수 없습니다\",\"복원 전 스냅샷이 더 이상 없습니다\",\"복원 전 스냅샷이 디스크에서 변경되어 사용하지 않습니다\",\"그런 다음 원하는 출력물을 알려 주세요. 어시스턴트가 하는 모든 변경 사항이 여기에 표시되며, 어느 것이든 되돌릴 수 있습니다.\",\"마지막 옵션의 값이 남아 있을 수 있습니다 ({reason}).\",\"트레이가 준비되지 않았습니다\",\"트레이가 두 번 사용되었습니다\",\"실행 취소\",\"알 수 없는 설정\",\"저장하지 않은 변경 사항\",\"저장하지 않은 편집 내용이 삭제됨: {list}\",\"지원되지 않는 파일\",\"제목 없음\",\"VS Code가 열리고 확인을 요청합니다. uv가 필요합니다.\",\"사용 중인 앱을 기다리는 중…\",\"{app}을(를) 기다리는 중…\",\"이전 값 {before}\",\"어시스턴트가 무언가를 변경하면 여기에 표시됩니다.\",\"프라임 타워 X\",\"프라임 타워 Y\",\"값의 개수가 올바르지 않습니다\",\"예\",\"이후 사용자가 변경함\",\"앱의 MCP 설정\",\"그 이후 사용자가 직접 한 변경 사항도 취소됩니다.\",\"베드 옆\",\"사용자가 변경함\",\"이후 변경됨\",\"이후 복사본 제거됨\",\"이후 삭제됨\",\"사전 설정 전환으로 삭제됨\",\"지금 객체 있음\",\"레이어 {height} mm\",\"{line}번째 줄: {text} ({count}줄)\",\"이후 이동됨\",\"없음\",\"숫자가 아님\",\"회전 안 됨\",\"설정 안 됨\",\"남긴 항목 없음\",\"이전 이름 사용 중\",\"최신 아님\",\"플레이트 {n}\",\"플레이트 {n} · {error}\",\"이후 범위 설정됨\",\"이후 제거됨\",\"되돌림\",\"사용자가 되돌림\",\"설정이 원래대로 돌아오지 않았을 수 있음\",\"플레이트\",\"설정 탭이 준비되지 않음\",\"{position}(으)로\",\"복원으로 취소됨\",\"사용 중인 앱\",\"{axis}축 기준 {angle}°\",\"{key}: 프로세스, 필라멘트, 프린터 설정이 아닙니다\",\"{key}: 지원되지 않는 값\",\"{label} (필라멘트 {n})\",\"{label} (필라멘트 {list})\",\"{label}은(는) {min}보다 커야 합니다 (현재 {got})\",\"{label}은(는) {min}보다 커야 합니다 (현재 {got}). 프린터가 허용하는 최솟값은 프린터 설정 > 레이어 높이 한도에서 설정합니다\",\"{label}은(는) {min}보다 크고 {max} 이하여야 합니다 (현재 {got})\",\"{label}은(는) {min}보다 크고 {max} 이하여야 합니다 (현재 {got}). 프린터가 허용하는 최솟값은 프린터 설정 > 레이어 높이 한도에서 설정합니다\",\"{label}은(는) {min} 이상이어야 합니다 (현재 {got})\",\"{label}은(는) {min} 이상이어야 합니다 (현재 {got}). 프린터가 허용하는 최솟값은 프린터 설정 > 레이어 높이 한도에서 설정합니다\",\"{label}은(는) {max} 이하여야 합니다 (현재 {got})\",\"{label}은(는) {max} 이하여야 합니다 (현재 {got}). 프린터가 허용하는 최솟값은 프린터 설정 > 레이어 높이 한도에서 설정합니다\",\"{label}은(는) {min}에서 {max} 사이여야 합니다 (현재 {got})\",\"{label}은(는) {min}에서 {max} 사이여야 합니다 (현재 {got}). 프린터가 허용하는 최솟값은 프린터 설정 > 레이어 높이 한도에서 설정합니다\",\"{label}은(는) 허용되지 않음\",\"{name}은(는) 이제 이 프린터와 맞지 않습니다\",\"{name}이(가) 더 이상 없습니다\",\"{name}이(가) 이미 없습니다\",\"{name}이(가) 더 이상 플레이트 위에 없습니다\",\"{name}이(가) 더 이상 그곳에 없습니다\",\"{name} → {plate}\",\"{name}, 현재 {n}개\",\"객체 {n}개\",\"객체 {n}개\",\"범위 {n}개\",\"범위 {n}개\",\"{object}이(가) 더 이상 플레이트 위에 없습니다\",\"{object}만\",\"{object}만 · 프로세스 사전 설정에서\",\"{preset} (저장 안 됨)\",\"{text} ({count}줄)\",\"{title} 외 {n}개\",\"{preset}의 {value}\",\"{verb} (사용할 수 없음: {why})\",\"{what} {verb}\",\"{verb}: {label}을(를) {before}(으)로 되돌리기\",\"{x} × {y} × {z} mm\"],\n"
    "\"lt\": [\"(tuščia eilutė)\",\"(paslėpta)\",\"Vyksta atkūrimas\",\"Prisitaikantis sluoksnio aukštis\",\"Pridėti modelį\",\"Įtraukti plokštę\",\"Pridėti prie Cursor\",\"Pridėti prie VS Code\",\"Pridėti {name}\",\"Pridėta\",\"Pridėta plokštė\",\"Jau grąžinta į {before}\",\"Taip pat pridėkite {setting} kaip aplinkos kintamąjį.\",\"Objektas\",\"Argumentai\",\"Išdėstyti\",\"Išdėstyta\",\"Papasakokite, kokį spaudinį norite. Jo pakeitimai atsiras čia, kiekvienas su mygtuku „Grąžinti“.\",\"Asistentas\",\"Asistento prieiga išjungta.\",\"Asistentas nustatė {value} · buvo {before}\",\"Asistentas nustatė {value} · jūs grąžinote\",\"Asistento parametrai\",\"Automatiškai orientuoti\",\"Orientuota automatiškai\",\"Integruotas pavadinimas\",\"Integruotas profilis\",\"Užimtas\",\"Sluoksniuojama\",\"Užimta asistento užklausa; pabandykite po akimirkos\",\"Nepavyko pakeisti atgal\",\"Dabar patikrinti nepavyksta: {reason}\",\"Prisijungti nepavyksta: pasiekiami tik serveriai internete, o ne programos jūsų kompiuteryje.\",\"Nepavyko ištrinti\",\"Po perkrovimo grąžinti nepavyks; vietoje to atkurkite ankstesnį tašką\",\"Nepavyko grąžinti diapazonų\",\"Nepavyko grąžinti\",\"Nepavyko grąžinti: {reason}\",\"Nepavyko perjungti atgal\",\"Perkrovus projektą šio objekto sekti nebeįmanoma\",\"Atšaukta\",\"Pakeisti atgal\",\"Pakeisti {label} atgal į {before}\",\"Vėl pakeista nuo to laiko\",\"Vėl pakeista nuo to laiko; vietoje to atkurkite ankstesnį tašką\",\"ChatGPT darbalaukio programa\",\"ChatGPT internete ar telefone\",\"Claude programa\",\"Išvalyti aukščio diapazonus\",\"Codex terminale ar redaktoriuje\",\"Suskleisti viską\",\"Komanda paleisti\",\"Palyginta parinkčių: {n}\",\"Palyginta parinkčių: {n} · nieko neišsaugota\",\"Palyginta parinkčių: {n} · parametrai gali būti nebe tokie, kokie buvo\",\"Palyginta parinkčių: {n}\",\"Palyginta parinkčių: {n} · nieko neišsaugota\",\"Palyginta parinkčių: {n} · parametrai gali būti nebe tokie, kokie buvo\",\"Palyginimas nesugebėjo grąžinti: {labels}\",\"Prijunkite savo DI programą\",\"Prijungta\",\"Prijungta prie {app}\",\"Nukopijuota\",\"Kopijuoti\",\"Vietoje to kopijuoti .vscode/mcp.json parametrus\",\"Kopijuoti komandą\",\"Kopijuoti parametrus\",\"Vietoje to kopijuoti parametrus\",\"Nepavyko atkurti\",\"Nepavyko grąžinti\",\"Nepavyko apskaičiuoti\",\"Nepavyko ištrinti\",\"Nepavyko ištrinti\",\"Nepavyko perkelti\",\"Nepavyko perkelti atgal\",\"Nepavyko atverti\",\"Nepavyko atverti atkūrimo taško\",\"Nepavyko atkurti {time} būsenos\",\"Nepavyko išsaugoti\",\"Nepavyko pirmiausia išsaugoti dabartinės būsenos\",\"Nepavyko pasirinkti\",\"Nepavyko atšaukti\",\"Cursor atsidaro ir paprašo patvirtinti. Reikia uv.\",\"Ištrinti plokštę {n} (tik kol ji tuščia)\",\"Ištrinti plokštę\",\"Ištrinti plokštę {n}\",\"Ištrinti profilį\",\"Ištrinti {type} profilį {name}\",\"Ištrinti {name}\",\"Ištrinti {type} profilį\",\"Ištrintas failas\",\"Ištrinta plokštė\",\"Ištrintas profilis\",\"Atsisiųsti\",\"Pašalinta perjungus profilį\",\"Pašalinta perjungus profilį; vietoje to atkurkite ankstesnį tašką\",\"Dubliuoti objektą {n}\",\"Dubliuota\",\"Kiekvienai programai reikia uv – nedidelio nemokamo įrankio, kuris paleidžia jungtį. Užtenka įdiegti vieną kartą.\",\"Išskleisti viską\",\"Nepavyko\",\"Gija\",\"Gijos profilis\",\"Gijų dėklai\",\"Gija {n}\",\"Gijos {n} spalva\",\"Gijos {n} dėklas\",\"Failas\",\"Failas jau yra\",\"Failas nerastas\",\"Į diską išsaugoti failai ir profiliai lieka.\",\"Aplankas nerastas\",\"Pokalbiams. Atsisiųskite plėtinį, tada atidarykite failą. Reikia uv.\",\"Gauti uv\",\"Grįžti į būseną prieš atkūrimą\",\"Aukščio diapazonas {min}–{max} mm\",\"Aukščio diapazonai išvalyti\",\"Paslėpta reikšmė\",\"Terminale, redaktoriuje arba programos skirtuke Code. Reikia uv.\",\"Plėtinio parametruose nustatykite „OrcaSlicer settings folder“ reikšmę {folder}.\",\"Netinkama reikšmė\",\"Dabar joje yra objektų\",\"Jo nebėra plokštėje\",\"Jo nebėra\",\"Jis pasirinktas; pirmiausia perjunkite į kitą profilį\",\"Tai paskutinė plokštė\",\"Jos objektai perkelti šalia pagrindo\",\"LM Studio, Windsurf ir dauguma kitų naudoja šią formą. Reikia uv.\",\"Paskutinė plokštė\",\"Sluoksnio aukštis už ribų\",\"Sluoksnio aukštis atstatytas\",\"{line} eilutė iš {count}:\",\"Eilutės nuo {first} iki {last} iš {count}:\",\"Pagrindinis meniu (šalia Failas) > Papildiniai > OrcaSlicer MCP > Konfigūracija\",\"Modelis\",\"Perkelti atgal\",\"Perkelti objektą {n}\",\"Perkelti objektą {n} į Plokštę {index}\",\"Perkelti {name}\",\"Perkelti {name} atgal į {path}\",\"Perkelti {name} atgal į {plate}\",\"Perkelti {name} atgal į ankstesnę vietą\",\"Perkelta\",\"Vėl perkelta nuo to laiko\",\"Perkelta į plokštę\",\"Pavadinimas\",\"Reikia: Parinktys › Remote API › Allow deleting and replacing project files\",\"Reikia: Parinktys › Remote API › Allow script, G-code and connection edits\",\"Reikia: Parinktys › Remote API › Allow script, G-code and connection edits · {object}\",\"Reikia OrcaSlicer MCP versijos\",\"Niekada neišsaugota\",\"Naujas projektas\",\"Naujas {type} profilis\",\"Ne\",\"Nėra CANVAS dėklų\",\"Pakeitimų nėra\",\"Čia nėra atkūrimo taško\",\"Nėra vietos dar vienai plokštei\",\"Tokio pakeitimo nėra\",\"Tokios kopijos nėra\",\"Tokios gijos nėra\",\"Tokios plokštės nėra\",\"Tokio profilio nėra\",\"Tokio atkūrimo taško nėra\",\"Tokio dėklo nėra\",\"Neleidžiama\",\"Nesuderinama\",\"Čia neredaguojama\",\"Nėra neseniai naudotų sąraše\",\"Nėra šio projekto laiko juostoje\",\"Negrąžinta\",\"Nepasiruošęs\",\"Neišsaugota\",\"Ne tada, kai „OrcaSlicer“ vykdo užduotį\",\"Ne tada, kai „OrcaSlicer“ užsidaro\",\"Ne tada, kai įkeliamas projektas\",\"Ne tada, kai sluoksniuojama\",\"Ne tada, kai langas perkuriamas\",\"Pastaba\",\"Niekas neperkelta\",\"Plokštėje nieko nėra\",\"Nėra ko grąžinti\",\"Parametruose nėra ką rodyti\",\"Nėra ką atšaukti\",\"Gerai\",\"Objektas neturi kopijos\",\"Objektas nerastas\",\"Objekto {n} parametrai\",\"Išjungta\",\"Įjungta\",\"Atverti projektą {name}\",\"Atverti {settings}\",\"Atvertas projektas\",\"Atveriama…\",\"OrcaSlicer MCP skydelis uždarytas. Grąžinkite jį per Vaizdas > OrcaSlicer MCP panel.\",\"OrcaSlicer MCP skydelis uždarytas. Jis sugrįš, kai asistentas kitą kartą naudos OrcaSlicer, arba paspauskite tarpo klavišą ir pasirinkite OrcaSlicer MCP.\",\"OrcaSlicer pranešė apie klaidą\",\"Kita programa\",\"Kiti profiliai jį paveldi\",\"Pasenę: po šio sluoksniavimo kažkas pasikeitė\",\"Už ribų\",\"Persidengia su diapazonu\",\"Plokštė\",\"Plokštė užrakinta\",\"Plokštė {n}\",\"Plokštėje {n} dabar yra kitų objektų\",\"Profilis\",\"Profilis naudojamas\",\"Spausdinimo profilis\",\"Spausdintuvas\",\"Spausdintuvo profilis\",\"Projektas atvertas\",\"Grąžinti\",\"Grąžinti {object} aukščio diapazonus\",\"Grąžinti ankstesnį {object} sluoksnio aukščio profilį\",\"Grąžinti {n} objektų į ankstesnes vietas\",\"Grąžinti {name} į {plate}\",\"Kokybė {value}\",\"Tik skaitymui\",\"Atmesta\",\"Atmesta\",\"Remote API parametrai\",\"Pašalinti\",\"Pašalinti kopiją\",\"Pašalinti objektą {n}\",\"Pašalinti diapazoną\",\"Pašalinti naują {name} kopiją\",\"Pašalinti {label} nuokrypį iš {object}\",\"Pašalinti {min}–{max} mm diapazoną iš {object}\",\"Pašalinti {name} iš plokštės\",\"Pašalinta\",\"Pervadinta\",\"Pakeistas išsaugotas {type} profilis\",\"Atstatyti sluoksnio aukštį\",\"Atkūrimo taško aplankas\",\"Atkūrimo taškams reikia OrcaSlicer MCP versijos\",\"Atkurti projektą į {time} būseną (atšaukia vėlesnius pakeitimus)\",\"Atkurti {time} būseną?\",\"Atkūrimas atšauktas\",\"Atkurta {time} būsena\",\"Atkuriama…\",\"Grąžinti\",\"Grąžinta\",\"Grąžinta {time}\",\"Grąžinama…\",\"Pasukti atgal\",\"Pasukti objektą {n}\",\"Pasukti {name} atgal\",\"Pasukta\",\"Paleiskite tai vieną kartą. Darbalaukio programa dalijasi šiuo parametru. Reikia uv.\",\"Tas pats kelias\",\"Išsaugoti projektą\",\"Išsaugoti projektą kaip {name}\",\"Išsaugoti {type} profilį\",\"Išsaugotas profilis\",\"Išsaugotas projektas\",\"Grąžinti mastelį\",\"Keisti objekto {n} mastelį\",\"Grąžinti {name} mastelį\",\"Pakeistas mastelis\",\"Scenarijai, pasirinktinis G-code ir spausdintuvo ryšio parametrai užrakinti. Pažymėkite „Allow script, G-code and connection edits“ skiltyje Parinktys > Remote API, kad asistentas galėtų juos keisti, arba pakeiskite patys čia.\",\"Nustatykite ten, kur naudojate savo programą\",\"Nustatykite ten, kur naudojate {app}\",\"Grąžinti {object} {min}–{max} mm diapazoną į {before}\",\"Grąžinti {label} į {before}\",\"Grąžinti {label} objekte {object} į {before}\",\"Parametras\",\"Parametrai\",\"Nuostatos › Įskiepiai › Pridėti › Pridėti MCP serverį, pasirinkite STDIO ir užpildykite kiekvieną toliau esantį laukelį. Veikia Codex pokalbiuose; paprasti ChatGPT pokalbiai to dar negali naudoti. Reikia uv.\",\"Rodyti išsamiau\",\"Parodyti, kas tai buvo, ir rasti parametruose\",\"Parodyti, kas tai buvo, ir atšaukti\",\"Parodyti, kas tai buvo, atšaukti ir rasti parametruose\",\"Sluoksniavimo rezultatas\",\"Sluoksniuota\",\"Sluoksniuota {where}\",\"Sluoksniavimas atšauktas\",\"Sluoksniavimas nepavyko\",\"Kažkas nepavyko\",\"Kažkas nepavyko: {reason}\",\"Perjungti atgal\",\"Perjungimas atšauktas\",\"Perjungti {label} atgal į {before}\",\"Perjungti {label} atgal į {before} (atmeta neišsaugotus pakeitimus)\",\"Tos gijos nebėra\",\"Remote API nepavyko paleisti. Patikrinkite jo prievadą.\",\"Remote API išjungtas.\",\"Kopijos jau nebėra\",\"Seno aplanko nebėra\",\"Senas pavadinimas dabar užimtas\",\"Plokštė nuo to laiko pasikeitė; vietoje to atkurkite ankstesnį tašką\",\"Papildinys nepasileido\",\"Papildinys niekada nekeičia scenarijų, G-code ar spausdintuvo ryšio parametrų\",\"Projektas nuo to laiko pasikeitė\",\"Atkūrimo taško nepavyksta nuskaityti\",\"Atkūrimo taško nebėra\",\"Atkūrimo taškas buvo pakeistas diske, todėl nenaudojamas\",\"Parametro šiame profilyje nebėra\",\"Parametrų kortelė neparengta\",\"Parametrų kortelės nėra\",\"Momentinės kopijos prieš atkūrimą nebėra\",\"Momentinė kopija prieš atkūrimą buvo pakeista diske, todėl nenaudojama\",\"Tada papasakokite, kokį spaudinį norite. Kiekvienas jo pakeitimas rodomas čia, ir bet kurį galite atšaukti.\",\"Jie vis dar gali turėti paskutinės parinkties reikšmes ({reason}).\",\"Dėklas neparengtas\",\"Dėklas panaudotas du kartus\",\"Atšaukti\",\"Nežinomas parametras\",\"Neišsaugoti pakeitimai\",\"Neišsaugoti pakeitimai atmesti: {list}\",\"Nepalaikomas failas\",\"Be pavadinimo\",\"VS Code atsidaro ir paprašo patvirtinti. Reikia uv.\",\"Laukiama jūsų programos…\",\"Laukiama {app}…\",\"Buvo {before}\",\"Kai asistentas ką nors pakeičia, tai rodoma čia.\",\"Valymo bokštas X\",\"Valymo bokštas Y\",\"Neteisingas reikšmių skaičius\",\"Taip\",\"Nuo to laiko jūs tai pakeitėte\",\"Jūsų programos MCP parametrai\",\"Jūsų pačių vėlesni pakeitimai taip pat atšaukiami.\",\"šalia pagrindo\",\"pakeista jūsų\",\"pakeista nuo to laiko\",\"kopija pašalinta nuo to laiko\",\"ištrinta nuo to laiko\",\"pašalinta perjungus profilį\",\"dabar yra objektų\",\"sluoksnis {height} mm\",\"eilutė {line}: {text} (eilučių: {count})\",\"perkelta nuo to laiko\",\"nėra\",\"ne skaičius\",\"nepasukta\",\"nenustatyta\",\"nieko neišsaugota\",\"senas pavadinimas užimtas\",\"pasenę\",\"plokštė {n}\",\"plokštė {n} · {error}\",\"diapazonai nustatyti nuo to laiko\",\"pašalinta nuo to laiko\",\"grąžinta\",\"grąžinta jūsų\",\"parametrai gali būti nebe tokie, kokie buvo\",\"plokštė\",\"parametrų kortelė neparengta\",\"į {position}\",\"atšaukta atkūrimo metu\",\"jūsų programa\",\"{angle}° aplink {axis}\",\"{key}: tai nėra spausdinimo, gijos ar spausdintuvo parametras\",\"{key}: nepalaikoma reikšmė\",\"{label} (gija {n})\",\"{label} (gijos {list})\",\"{label} turi būti didesnė nei {min} (gauta {got})\",\"{label} turi būti didesnė nei {min} (gauta {got}); žemiausia spausdintuvo leidžiama reikšmė nustatoma skiltyje Spausdintuvo nustatymai > Sluoksnio aukščio ribos\",\"{label} turi būti didesnė nei {min} ir ne didesnė nei {max} (gauta {got})\",\"{label} turi būti didesnė nei {min} ir ne didesnė nei {max} (gauta {got}); žemiausia spausdintuvo leidžiama reikšmė nustatoma skiltyje Spausdintuvo nustatymai > Sluoksnio aukščio ribos\",\"{label} turi būti ne mažesnė kaip {min} (gauta {got})\",\"{label} turi būti ne mažesnė kaip {min} (gauta {got}); žemiausia spausdintuvo leidžiama reikšmė nustatoma skiltyje Spausdintuvo nustatymai > Sluoksnio aukščio ribos\",\"{label} turi būti ne didesnė kaip {max} (gauta {got})\",\"{label} turi būti ne didesnė kaip {max} (gauta {got}); žemiausia spausdintuvo leidžiama reikšmė nustatoma skiltyje Spausdintuvo nustatymai > Sluoksnio aukščio ribos\",\"{label} turi būti tarp {min} ir {max} (gauta {got})\",\"{label} turi būti tarp {min} ir {max} (gauta {got}); žemiausia spausdintuvo leidžiama reikšmė nustatoma skiltyje Spausdintuvo nustatymai > Sluoksnio aukščio ribos\",\"{label} neleidžiama\",\"{name} dabar netinka šiam spausdintuvui\",\"{name} dingo\",\"{name} jau dingo\",\"{name} nebėra plokštėje\",\"{name} nebėra\",\"{name} → {plate}\",\"{name}, kopijų dabar: {n}\",\"{n} objektas\",\"objektų: {n}\",\"{n} diapazonas\",\"diapazonų: {n}\",\"{object} nebėra plokštėje\",\"Tik {object}\",\"Tik {object} · iš spausdinimo profilio\",\"{preset} (neišsaugota)\",\"{text} (eilučių: {count})\",\"{title} ir dar {n}\",\"{value} profilyje {preset}\",\"{verb} (nepasiekiama: {why})\",\"{verb} {what}\",\"{verb}: {label} atgal į {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"nl\": [\"(lege regel)\",\"(verborgen)\",\"Er loopt een herstelactie\",\"Adaptieve laaghoogte\",\"Model toevoegen\",\"Printbed toevoegen\",\"Toevoegen aan Cursor\",\"Toevoegen aan VS Code\",\"{name} toevoegen\",\"Toegevoegd\",\"Printbed toegevoegd\",\"Al terug op {before}\",\"Voeg {setting} ook toe als omgevingsvariabele.\",\"Een object\",\"Argumenten\",\"Rangschikken\",\"Gerangschikt\",\"Vraag de assistent om de print die u wilt. Zijn wijzigingen verschijnen hier, elk met Terugzetten.\",\"Assistent\",\"De toegang van de assistent staat uit.\",\"Assistent stelde {value} in · was {before}\",\"Assistent stelde {value} in · door u teruggezet\",\"Assistent-instellingen\",\"Automatisch oriënteren\",\"Automatisch georiënteerd\",\"Ingebouwde naam\",\"Ingebouwde voorinstelling\",\"Bezet\",\"Bezig met slicen\",\"Bezig met het verzoek van de assistent; probeer het zo opnieuw\",\"Kan niet worden teruggewijzigd\",\"Kan nu niet worden gecontroleerd: {reason}\",\"Verbinden lukt niet: alleen servers op internet zijn bereikbaar, geen programma's op uw computer.\",\"Kan niet worden verwijderd\",\"Kan na een herstart niet worden teruggeplaatst; herstel in plaats daarvan een eerder punt\",\"Kan de bereiken niet terugplaatsen\",\"Kan niet worden teruggezet\",\"Kan niet worden teruggezet: {reason}\",\"Kan niet worden teruggeschakeld\",\"Kan dit object niet volgen nadat het project opnieuw is geladen\",\"Geannuleerd\",\"Terugwijzigen\",\"{label} terugwijzigen naar {before}\",\"Sindsdien opnieuw gewijzigd\",\"Sindsdien opnieuw gewijzigd; herstel in plaats daarvan een eerder punt\",\"ChatGPT-desktopapp\",\"ChatGPT op het web of de telefoon\",\"Claude-app\",\"Hoogtebereiken wissen\",\"Codex in een terminal of editor\",\"Alles samenvouwen\",\"Opdracht om op te starten\",\"{n} optie vergeleken\",\"{n} optie vergeleken · niets bewaard\",\"{n} optie vergeleken · instellingen staan mogelijk niet zoals eerst\",\"{n} opties vergeleken\",\"{n} opties vergeleken · niets bewaard\",\"{n} opties vergeleken · instellingen staan mogelijk niet zoals eerst\",\"Vergelijking kon niet terugzetten: {labels}\",\"Verbind uw AI-app\",\"Verbonden\",\"Verbonden met {app}\",\"Gekopieerd\",\"Kopiëren\",\"Kopieer in plaats daarvan de .vscode/mcp.json-instellingen\",\"Opdracht kopiëren\",\"Instellingen kopiëren\",\"Kopieer in plaats daarvan de instellingen\",\"Herstellen mislukt\",\"Terugzetten mislukt\",\"Berekenen mislukt\",\"Verwijderen mislukt\",\"Kon niet worden verwijderd\",\"Verplaatsen mislukt\",\"Kon niet worden teruggeplaatst\",\"Kon niet worden geopend\",\"Kon het herstelpunt niet openen\",\"Herstellen naar {time} mislukt\",\"Opslaan mislukt\",\"Kon de huidige staat niet eerst opslaan\",\"Selecteren mislukt\",\"Kon niet ongedaan worden gemaakt\",\"Cursor opent en vraagt om bevestiging. Vereist uv.\",\"Printbed {n} verwijderen (alleen als het leeg is)\",\"Printbed verwijderen\",\"Printbed {n} verwijderen\",\"Voorinstelling verwijderen\",\"De {type}-voorinstelling {name} verwijderen\",\"{name} verwijderen\",\"{type}-voorinstelling verwijderen\",\"Bestand verwijderd\",\"Printbed verwijderd\",\"Voorinstelling verwijderd\",\"Downloaden\",\"Vervallen door het wisselen van voorinstelling\",\"Vervallen door het wisselen van voorinstelling; herstel in plaats daarvan een eerder punt\",\"Object {n} dupliceren\",\"Gedupliceerd\",\"Elke app heeft uv nodig, een klein gratis hulpmiddel dat de connector start. Eén keer installeren is genoeg.\",\"Alles uitvouwen\",\"Mislukt\",\"Filament\",\"Filament-voorinstelling\",\"Filamentlades\",\"Filament {n}\",\"Kleur van filament {n}\",\"Lade van filament {n}\",\"Bestand\",\"Bestand bestaat al\",\"Bestand niet gevonden\",\"Op schijf opgeslagen bestanden en voorinstellingen blijven behouden.\",\"Map niet gevonden\",\"Voor chats. Download de extensie en open daarna het bestand. Vereist uv.\",\"uv downloaden\",\"Terug naar de staat van vóór het herstellen\",\"Hoogtebereik {min}–{max} mm\",\"Hoogtebereiken gewist\",\"Verborgen waarde\",\"In een terminal, een editor of het Code-tabblad van de app. Vereist uv.\",\"Stel in de instellingen van de extensie \\\"OrcaSlicer settings folder\\\" in op {folder}.\",\"Ongeldige waarde\",\"Er staan nu objecten op\",\"Het staat niet meer op het printbed\",\"Het is er niet meer\",\"Het is geselecteerd; wissel eerst naar een andere voorinstelling\",\"Het is het laatste printbed\",\"De objecten zijn naast het bed gezet\",\"LM Studio, Windsurf en de meeste andere gebruiken deze vorm. Vereist uv.\",\"Laatste printbed\",\"Laaghoogte buiten bereik\",\"Laaghoogte teruggezet\",\"Regel {line} van {count}:\",\"Regels {first} tot {last} van {count}:\",\"Hoofdmenu (naast Bestand) > Plug-ins > OrcaSlicer MCP > Configuratie\",\"Model\",\"Terugverplaatsen\",\"Object {n} verplaatsen\",\"Object {n} verplaatsen naar printbed {index}\",\"{name} verplaatsen\",\"{name} terugverplaatsen naar {path}\",\"{name} terugverplaatsen naar {plate}\",\"{name} terugverplaatsen naar waar het was\",\"Verplaatst\",\"Sindsdien opnieuw verplaatst\",\"Naar printbed verplaatst\",\"Naam\",\"Vereist Voorkeuren › Remote API › Allow deleting and replacing project files\",\"Vereist Voorkeuren › Remote API › Allow script, G-code and connection edits\",\"Vereist Voorkeuren › Remote API › Allow script, G-code and connection edits · {object}\",\"Vereist de OrcaSlicer MCP-build\",\"Nooit opgeslagen\",\"Nieuw project\",\"Nieuwe {type}-voorinstelling\",\"Nee\",\"Geen CANVAS-laden\",\"Geen wijzigingen\",\"Hier is geen herstelpunt\",\"Geen ruimte voor nog een printbed\",\"Geen zodanige wijziging\",\"Geen zodanige kopie\",\"Geen zodanig filament\",\"Geen zodanig printbed\",\"Geen zodanige voorinstelling\",\"Geen zodanig herstelpunt\",\"Geen zodanige lade\",\"Niet toegestaan\",\"Niet compatibel\",\"Hier niet bewerkbaar\",\"Niet in de lijst met recente projecten\",\"Niet in de tijdlijn van dit project\",\"Niet teruggezet\",\"Niet gereed\",\"Niet opgeslagen\",\"Niet zolang OrcaSlicer met een taak bezig is\",\"Niet zolang OrcaSlicer wordt afgesloten\",\"Niet zolang een project wordt geladen\",\"Niet tijdens het slicen\",\"Niet zolang het venster wordt opgebouwd\",\"Notitie\",\"Niets verplaatst\",\"Niets op het printbed\",\"Niets om terug te zetten\",\"Niets om te tonen in de instellingen\",\"Niets om ongedaan te maken\",\"OK\",\"Object heeft geen kopie\",\"Object niet gevonden\",\"Objectinstellingen {n}\",\"Uit\",\"Aan\",\"Project {name} openen\",\"{settings} openen\",\"Project geopend\",\"Openen…\",\"Het OrcaSlicer MCP-paneel is gesloten. Open het opnieuw via Weergave > OrcaSlicer MCP panel.\",\"Het OrcaSlicer MCP-paneel is gesloten. Het komt terug wanneer uw assistent OrcaSlicer weer gebruikt, of druk op de spatiebalk en kies OrcaSlicer MCP.\",\"OrcaSlicer meldde een fout\",\"Andere app\",\"Andere voorinstellingen erven het\",\"Verouderd: er is iets gewijzigd na deze slice\",\"Buiten bereik\",\"Overlapt met een bereik\",\"Printbed\",\"Printbed vergrendeld\",\"Printbed {n}\",\"Printbed {n} bevat nu andere objecten\",\"Voorinstelling\",\"Voorinstelling in gebruik\",\"Printvoorinstelling\",\"Printer\",\"Printervoorinstelling\",\"Project is geopend\",\"Terugplaatsen\",\"De hoogtebereiken van {object} terugplaatsen\",\"Het vorige laaghoogteprofiel van {object} terugplaatsen\",\"De {n} objecten terugplaatsen waar ze waren\",\"{name} terugplaatsen op {plate}\",\"Kwaliteit {value}\",\"Alleen-lezen\",\"Geweigerd\",\"Afgewezen\",\"Remote API-instellingen\",\"Verwijderen\",\"Kopie verwijderen\",\"Object {n} verwijderen\",\"Bereik verwijderen\",\"De nieuwe kopie van {name} verwijderen\",\"De overschrijving van {label} bij {object} verwijderen\",\"Het bereik {min}–{max} mm van {object} verwijderen\",\"{name} van het printbed verwijderen\",\"Verwijderd\",\"Hernoemd\",\"Opgeslagen {type}-voorinstelling vervangen\",\"Laaghoogte terugzetten\",\"Map met herstelpunten\",\"Herstelpunten vereisen de OrcaSlicer MCP-build\",\"Het project herstellen naar de staat van {time} (maakt latere wijzigingen ongedaan)\",\"Herstellen naar {time}?\",\"Herstelactie ongedaan gemaakt\",\"Hersteld naar {time}\",\"Herstellen…\",\"Terugzetten\",\"Teruggezet\",\"Teruggezet om {time}\",\"Terugzetten…\",\"Terugdraaien\",\"Object {n} draaien\",\"{name} terugdraaien\",\"Gedraaid\",\"Voer dit één keer uit. De desktopapp deelt de instelling. Vereist uv.\",\"Hetzelfde pad\",\"Project opslaan\",\"Project opslaan als {name}\",\"{type}-voorinstelling opslaan\",\"Voorinstelling opgeslagen\",\"Project opgeslagen\",\"Terugschalen\",\"Object {n} schalen\",\"{name} terugschalen\",\"Geschaald\",\"Scripts, aangepaste G-code en printerverbindingsinstellingen zijn vergrendeld. Vink 'Allow script, G-code and connection edits' aan onder Voorkeuren > Remote API zodat de assistent ze mag wijzigen, of wijzig ze hier zelf.\",\"Stel het in waar u uw app gebruikt\",\"Stel het in waar u {app} gebruikt\",\"Het bereik {min}–{max} mm van {object} terugzetten naar {before}\",\"{label} terugzetten naar {before}\",\"{label} bij {object} terugzetten naar {before}\",\"Instelling\",\"Instellingen\",\"Instellingen › Plug-ins › Toevoegen › MCP-server toevoegen, kies STDIO en vul elk veld hieronder in. Werkt in Codex-chats; gewone ChatGPT-chats kunnen het nog niet gebruiken. Vereist uv.\",\"Details tonen\",\"Toon wat het was en zoek het op in de instellingen\",\"Toon wat het was en maak het ongedaan\",\"Toon wat het was, maak het ongedaan en zoek het op in de instellingen\",\"Slice-resultaat\",\"Geslicet\",\"Geslicet {where}\",\"Slicen geannuleerd\",\"Slicen mislukt\",\"Er ging iets mis\",\"Er ging iets mis: {reason}\",\"Terugschakelen\",\"Wisselen geannuleerd\",\"{label} terugschakelen naar {before}\",\"{label} terugschakelen naar {before} (verwerpt niet-opgeslagen wijzigingen)\",\"Dat filament bestaat niet meer\",\"De Remote API kon niet starten. Controleer de poort.\",\"De Remote API staat uit.\",\"De kopie is al weg\",\"De oude map bestaat niet meer\",\"De oude naam is nu bezet\",\"Het printbed is sindsdien gewijzigd; herstel in plaats daarvan een eerder punt\",\"De plug-in is niet gestart\",\"De plug-in wijzigt nooit scripts, G-code of printerverbindingsinstellingen\",\"Het project is sindsdien gewijzigd\",\"Het herstelpunt kan niet worden gelezen\",\"Het herstelpunt bestaat niet meer\",\"Het herstelpunt is op schijf gewijzigd en wordt daarom niet gebruikt\",\"De instelling staat niet meer in deze voorinstelling\",\"Het tabblad Instellingen is niet gereed\",\"Het tabblad Instellingen is er niet\",\"De momentopname van vóór het herstellen bestaat niet meer\",\"De momentopname van vóór het herstellen is op schijf gewijzigd en wordt daarom niet gebruikt\",\"Zeg de assistent dan welke print u wilt. Elke wijziging die hij maakt verschijnt hier, en u kunt elke wijziging ongedaan maken.\",\"Ze bevatten mogelijk nog de waarden van de laatste optie ({reason}).\",\"Lade niet gereed\",\"Lade twee keer gebruikt\",\"Ongedaan maken\",\"Onbekende instelling\",\"Niet-opgeslagen wijzigingen\",\"Niet-opgeslagen wijzigingen verworpen: {list}\",\"Niet-ondersteund bestand\",\"Naamloos\",\"VS Code opent en vraagt om bevestiging. Vereist uv.\",\"Wachten op uw app…\",\"Wachten op {app}…\",\"Was {before}\",\"Wanneer een assistent iets wijzigt, verschijnt het hier.\",\"Afveegblok X\",\"Afveegblok Y\",\"Verkeerd aantal waarden\",\"Ja\",\"Door u sindsdien gewijzigd\",\"MCP-instellingen van uw app\",\"Uw eigen wijzigingen sindsdien worden ook ongedaan gemaakt.\",\"naast het bed\",\"door u gewijzigd\",\"sindsdien gewijzigd\",\"kopie sindsdien verwijderd\",\"sindsdien verwijderd\",\"vervallen door wisselen van voorinstelling\",\"bevat nu objecten\",\"laag {height} mm\",\"regel {line}: {text} ({count} regels)\",\"sindsdien verplaatst\",\"geen\",\"geen getal\",\"niet gedraaid\",\"niet ingesteld\",\"niets bewaard\",\"oude naam bezet\",\"verouderd\",\"printbed {n}\",\"printbed {n} · {error}\",\"bereiken sindsdien ingesteld\",\"sindsdien verwijderd\",\"teruggezet\",\"door u teruggezet\",\"instellingen staan mogelijk niet zoals eerst\",\"het printbed\",\"het tabblad Instellingen is niet gereed\",\"naar {position}\",\"ongedaan gemaakt door het herstel\",\"uw app\",\"{angle}° om {axis}\",\"{key}: geen print-, filament- of printerinstelling\",\"{key}: niet-ondersteunde waarde\",\"{label} (filament {n})\",\"{label} (filamenten {list})\",\"{label} moet groter zijn dan {min} (is {got})\",\"{label} moet groter zijn dan {min} (is {got}); de laagste waarde die de printer toestaat staat onder Printer instellingen > Limieten voor laaghoogte\",\"{label} moet groter zijn dan {min} en hoogstens {max} zijn (is {got})\",\"{label} moet groter zijn dan {min} en hoogstens {max} zijn (is {got}); de laagste waarde die de printer toestaat staat onder Printer instellingen > Limieten voor laaghoogte\",\"{label} moet minstens {min} zijn (is {got})\",\"{label} moet minstens {min} zijn (is {got}); de laagste waarde die de printer toestaat staat onder Printer instellingen > Limieten voor laaghoogte\",\"{label} mag hoogstens {max} zijn (is {got})\",\"{label} mag hoogstens {max} zijn (is {got}); de laagste waarde die de printer toestaat staat onder Printer instellingen > Limieten voor laaghoogte\",\"{label} moet tussen {min} en {max} liggen (is {got})\",\"{label} moet tussen {min} en {max} liggen (is {got}); de laagste waarde die de printer toestaat staat onder Printer instellingen > Limieten voor laaghoogte\",\"{label} niet toegestaan\",\"{name} past nu niet meer bij deze printer\",\"{name} bestaat niet meer\",\"{name} is al weg\",\"{name} staat niet meer op het printbed\",\"{name} is er niet meer\",\"{name} → {plate}\",\"{name}, nu {n} kopieën\",\"{n} object\",\"{n} objecten\",\"{n} bereik\",\"{n} bereiken\",\"{object} staat niet meer op het printbed\",\"Alleen {object}\",\"Alleen {object} · uit de printvoorinstelling\",\"{preset} (niet opgeslagen)\",\"{text} ({count} regels)\",\"{title} en {n} meer\",\"{value} in {preset}\",\"{verb} (niet beschikbaar: {why})\",\"{verb} {what}\",\"{verb}: {label} terug naar {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"pl\": [\"(pusty wiersz)\",\"(ukryte)\",\"Trwa przywracanie\",\"Adaptacyjna wysokość warstwy\",\"Dodaj model\",\"Dodaj płytę\",\"Dodaj do Cursora\",\"Dodaj do VS Code\",\"Dodaj {name}\",\"Dodano\",\"Dodano płytę\",\"Już przywrócono do {before}\",\"Dodaj też {setting} jako zmienną środowiskową.\",\"Obiekt\",\"Argumenty\",\"Ustaw\",\"Ustawiono\",\"Poproś asystenta o wydruk, jaki chcesz. Jego zmiany pojawią się tutaj, każda z przyciskiem Cofnij zmianę.\",\"Asystent\",\"Dostęp asystenta jest wyłączony.\",\"Asystent ustawił {value} · było {before}\",\"Asystent ustawił {value} · cofnięto to\",\"Ustawienia asystenta\",\"Orientuj automatycznie\",\"Zorientowano automatycznie\",\"Nazwa wbudowana\",\"Profil wbudowany\",\"Zajęty\",\"Trwa cięcie\",\"Zajęty żądaniem asystenta; spróbuj za chwilę\",\"Nie można przywrócić wartości\",\"Nie można teraz sprawdzić: {reason}\",\"Nie można się połączyć: sięga tylko do serwerów w internecie, nie do programów na twoim komputerze.\",\"Nie można usunąć\",\"Po restarcie nie można tego przywrócić; przywróć zamiast tego wcześniejszy punkt\",\"Nie można przywrócić zakresów\",\"Nie można przywrócić\",\"Nie można przywrócić: {reason}\",\"Nie można przełączyć z powrotem\",\"Nie można śledzić tego obiektu po ponownym wczytaniu projektu\",\"Anulowano\",\"Przywróć wartość\",\"Przywróć {label} na {before}\",\"Zmieniono ponownie od tamtej pory\",\"Zmieniono ponownie od tamtej pory; przywróć zamiast tego wcześniejszy punkt\",\"Aplikacja ChatGPT na komputer\",\"ChatGPT w przeglądarce lub na telefonie\",\"Aplikacja Claude\",\"Wyczyść zakresy wysokości\",\"Codex w terminalu lub edytorze\",\"Zwiń wszystko\",\"Polecenie do uruchomienia\",\"Porównano {n} opcję\",\"Porównano {n} opcję · nic nie zachowano\",\"Porównano {n} opcję · ustawienia mogły nie wrócić do poprzedniego stanu\",\"Porównano opcji: {n}\",\"Porównano opcji: {n} · nic nie zachowano\",\"Porównano opcji: {n} · ustawienia mogły nie wrócić do poprzedniego stanu\",\"Porównanie nie mogło przywrócić: {labels}\",\"Połącz swoją aplikację AI\",\"Połączono\",\"Połączono z {app}\",\"Skopiowano\",\"Kopiuj\",\"Zamiast tego kopiuj ustawienia .vscode/mcp.json\",\"Kopiuj polecenie\",\"Kopiuj ustawienia\",\"Zamiast tego kopiuj ustawienia\",\"Nie udało się przywrócić\",\"Nie udało się cofnąć zmiany\",\"Nie udało się obliczyć\",\"Nie udało się usunąć\",\"Nie udało się tego usunąć\",\"Nie udało się przesunąć\",\"Nie udało się przesunąć z powrotem\",\"Nie udało się otworzyć\",\"Nie udało się otworzyć punktu przywracania\",\"Nie udało się przywrócić stanu z {time}\",\"Nie udało się zapisać\",\"Nie udało się najpierw zapisać bieżącego stanu\",\"Nie udało się wybrać\",\"Nie udało się cofnąć\",\"Cursor się otworzy i poprosi o potwierdzenie. Wymaga uv.\",\"Usuń płytę {n} (tylko gdy jest pusta)\",\"Usuń płytę\",\"Usuń płytę {n}\",\"Usuń profil\",\"Usuń profil {type} {name}\",\"Usuń {name}\",\"Usuń profil {type}\",\"Usunięto plik\",\"Usunięto płytę\",\"Usunięto profil\",\"Pobierz\",\"Odrzucone przez zmianę profilu\",\"Odrzucone przez zmianę profilu; przywróć zamiast tego wcześniejszy punkt\",\"Duplikuj obiekt {n}\",\"Zduplikowano\",\"Każda aplikacja potrzebuje uv – małego, darmowego narzędzia, które uruchamia łącznik. Wystarczy zainstalować je raz.\",\"Rozwiń wszystko\",\"Nieudane\",\"Filament\",\"Profil filamentu\",\"Tace filamentu\",\"Filament {n}\",\"Kolor filamentu {n}\",\"Taca filamentu {n}\",\"Plik\",\"Plik już istnieje\",\"Nie znaleziono pliku\",\"Pliki i profile zapisane na dysku pozostają.\",\"Nie znaleziono folderu\",\"Do czatów. Pobierz rozszerzenie, a potem otwórz plik. Wymaga uv.\",\"Pobierz uv\",\"Wróć do stanu sprzed przywrócenia\",\"Zakres wysokości {min}–{max} mm\",\"Wyczyszczono zakresy wysokości\",\"Ukryta wartość\",\"W terminalu, edytorze lub na karcie Code aplikacji. Wymaga uv.\",\"W ustawieniach rozszerzenia ustaw „OrcaSlicer settings folder” na {folder}.\",\"Nieprawidłowa wartość\",\"Są na niej teraz obiekty\",\"Nie ma go już na płycie\",\"Już go tam nie ma\",\"Jest wybrany; najpierw przełącz na inny profil\",\"To ostatnia płyta\",\"Jej obiekty przesunięto obok stołu\",\"LM Studio, Windsurf i większość innych używa tej postaci. Wymaga uv.\",\"Ostatnia płyta\",\"Wysokość warstwy poza zakresem\",\"Zresetowano wysokość warstwy\",\"Wiersz {line} z {count}:\",\"Wiersze od {first} do {last} z {count}:\",\"Menu główne (obok Plik) > Wtyczki > OrcaSlicer MCP > Konfiguracja\",\"Model\",\"Przesuń z powrotem\",\"Przesuń obiekt {n}\",\"Przesuń obiekt {n} na płytę {index}\",\"Przesuń {name}\",\"Przenieś {name} z powrotem do {path}\",\"Przesuń {name} z powrotem na {plate}\",\"Przesuń {name} z powrotem na dawne miejsce\",\"Przesunięto\",\"Przesunięto ponownie od tamtej pory\",\"Przeniesiono na płytę\",\"Nazwa\",\"Wymaga: Preferencje › Remote API › Allow deleting and replacing project files\",\"Wymaga: Preferencje › Remote API › Allow script, G-code and connection edits\",\"Wymaga: Preferencje › Remote API › Allow script, G-code and connection edits · {object}\",\"Wymaga kompilacji OrcaSlicer MCP\",\"Nigdy nie zapisano\",\"Nowy projekt\",\"Nowy profil {type}\",\"Nie\",\"Brak tac CANVAS\",\"Brak zmian\",\"Brak tu punktu przywracania\",\"Brak miejsca na kolejną płytę\",\"Nie ma takiej zmiany\",\"Nie ma takiej kopii\",\"Nie ma takiego filamentu\",\"Nie ma takiej płyty\",\"Nie ma takiego profilu\",\"Nie ma takiego punktu przywracania\",\"Nie ma takiej tacy\",\"Niedozwolone\",\"Niezgodne\",\"Nie można tu edytować\",\"Brak na liście ostatnich\",\"Brak w historii tego projektu\",\"Nie przywrócono\",\"Niegotowe\",\"Nie zapisano\",\"Nie, gdy OrcaSlicer jest zajęty zadaniem\",\"Nie, gdy OrcaSlicer się zamyka\",\"Nie, gdy projekt się wczytuje\",\"Nie podczas cięcia\",\"Nie, gdy okno jest odbudowywane\",\"Notatka\",\"Nic nie przesunięto\",\"Nic nie ma na płycie\",\"Nie ma czego cofać\",\"Nie ma nic do pokazania w ustawieniach\",\"Nie ma nic do cofnięcia\",\"OK\",\"Obiekt nie ma kopii\",\"Nie znaleziono obiektu\",\"Ustawienia obiektu {n}\",\"Wył.\",\"Wł.\",\"Otwórz projekt {name}\",\"Otwórz {settings}\",\"Otwarto projekt\",\"Otwieranie…\",\"Panel OrcaSlicer MCP został zamknięty. Przywrócisz go przez Widok > OrcaSlicer MCP panel.\",\"Panel OrcaSlicer MCP został zamknięty. Wróci, gdy asystent następnym razem użyje OrcaSlicer, albo naciśnij spację i wybierz OrcaSlicer MCP.\",\"OrcaSlicer zgłosił błąd\",\"Inna aplikacja\",\"Dziedziczą go inne profile\",\"Nieaktualne: coś zmieniło się po tym cięciu\",\"Poza zakresem\",\"Nakłada się na zakres\",\"Płyta\",\"Płyta zablokowana\",\"Płyta {n}\",\"Na płycie {n} są teraz inne obiekty\",\"Profil\",\"Profil w użyciu\",\"Profil druku\",\"Drukarka\",\"Profil drukarki\",\"Projekt jest otwarty\",\"Przywróć\",\"Przywróć zakresy wysokości obiektu {object}\",\"Przywróć poprzedni profil wysokości warstwy obiektu {object}\",\"Przywróć obiekty na dawne miejsca (liczba: {n})\",\"Przywróć {name} na {plate}\",\"Jakość {value}\",\"Tylko do odczytu\",\"Odmówiono\",\"Odrzucono\",\"Ustawienia Remote API\",\"Usuń\",\"Usuń kopię\",\"Usuń obiekt {n}\",\"Usuń zakres\",\"Usuń nową kopię obiektu {name}\",\"Usuń nadpisanie {label} z obiektu {object}\",\"Usuń zakres {min}–{max} mm z obiektu {object}\",\"Usuń {name} z płyty\",\"Usunięto\",\"Zmieniono nazwę\",\"Zastąpiono zapisany profil {type}\",\"Zresetuj wysokość warstwy\",\"Folder punktów przywracania\",\"Punkty przywracania wymagają kompilacji OrcaSlicer MCP\",\"Przywróć projekt do stanu z {time} (cofa późniejsze zmiany)\",\"Przywrócić stan z {time}?\",\"Cofnięto przywracanie\",\"Przywrócono stan z {time}\",\"Przywracanie…\",\"Cofnij zmianę\",\"Cofnięto zmianę\",\"Cofnięto zmianę o {time}\",\"Cofanie zmiany…\",\"Obróć z powrotem\",\"Obróć obiekt {n}\",\"Obróć {name} z powrotem\",\"Obrócono\",\"Uruchom to raz. Aplikacja na komputer współdzieli to ustawienie. Wymaga uv.\",\"Ta sama ścieżka\",\"Zapisz projekt\",\"Zapisz projekt jako {name}\",\"Zapisz profil {type}\",\"Zapisano profil\",\"Zapisano projekt\",\"Przywróć skalę\",\"Skaluj obiekt {n}\",\"Przywróć skalę obiektu {name}\",\"Przeskalowano\",\"Skrypty, własny G-code i ustawienia połączenia z drukarką są zablokowane. Zaznacz 'Allow script, G-code and connection edits' w Preferencje > Remote API, aby asystent mógł je zmieniać, albo zmień je sam tutaj.\",\"Skonfiguruj to tam, gdzie używasz swojej aplikacji\",\"Skonfiguruj to tam, gdzie używasz {app}\",\"Przywróć zakres {min}–{max} mm obiektu {object} na {before}\",\"Przywróć {label} na {before}\",\"Przywróć {label} obiektu {object} na {before}\",\"Ustawienie\",\"Ustawienia\",\"Ustawienia › Wtyczki › Dodaj › Dodaj serwer MCP, wybierz STDIO i wypełnij każde pole poniżej. Działa w czatach Codex; zwykłe czaty ChatGPT jeszcze tego nie obsługują. Wymaga uv.\",\"Pokaż szczegóły\",\"Pokaż, jak było, i znajdź w ustawieniach\",\"Pokaż, jak było, i cofnij\",\"Pokaż, jak było, cofnij i znajdź w ustawieniach\",\"Wynik cięcia\",\"Pocięto\",\"Pocięto {where}\",\"Anulowano cięcie\",\"Cięcie nie powiodło się\",\"Coś poszło nie tak\",\"Coś poszło nie tak: {reason}\",\"Przełącz z powrotem\",\"Anulowano przełączenie\",\"Przełącz {label} z powrotem na {before}\",\"Przełącz {label} z powrotem na {before} (odrzuca niezapisane zmiany)\",\"Tego filamentu już nie ma\",\"Remote API nie mogło się uruchomić. Sprawdź jego port.\",\"Remote API jest wyłączone.\",\"Kopii już nie ma\",\"Starego folderu już nie ma\",\"Stara nazwa jest już zajęta\",\"Płyta zmieniła się od tamtej pory; przywróć zamiast tego wcześniejszy punkt\",\"Wtyczka nie uruchomiła się\",\"Wtyczka nigdy nie zmienia skryptów, G-code ani ustawień połączenia z drukarką\",\"Projekt zmienił się od tamtej pory\",\"Nie można odczytać punktu przywracania\",\"Punktu przywracania już nie ma\",\"Punkt przywracania zmieniono na dysku, więc nie jest używany\",\"Tego ustawienia nie ma już w tym profilu\",\"Karta ustawień nie jest gotowa\",\"Karty ustawień nie ma\",\"Migawki sprzed przywrócenia już nie ma\",\"Migawkę sprzed przywrócenia zmieniono na dysku, więc nie jest używana\",\"Następnie opisz, jaki wydruk chcesz. Każda jego zmiana pojawi się tutaj i możesz ją cofnąć.\",\"Mogą nadal mieć wartości ostatniej opcji ({reason}).\",\"Taca niegotowa\",\"Taca użyta dwa razy\",\"Cofnij\",\"Nieznane ustawienie\",\"Niezapisane zmiany\",\"Odrzucono niezapisane zmiany: {list}\",\"Nieobsługiwany plik\",\"Bez tytułu\",\"VS Code się otworzy i poprosi o potwierdzenie. Wymaga uv.\",\"Oczekiwanie na twoją aplikację…\",\"Oczekiwanie na {app}…\",\"Było {before}\",\"Gdy asystent coś zmieni, pojawi się to tutaj.\",\"Wieża czyszcząca X\",\"Wieża czyszcząca Y\",\"Nieprawidłowa liczba wartości\",\"Tak\",\"Zmieniono to od tamtej pory\",\"Ustawienia MCP twojej aplikacji\",\"Twoje późniejsze zmiany też zostaną cofnięte.\",\"obok stołu\",\"zmienione przez ciebie\",\"zmienione od tamtej pory\",\"kopia usunięta od tamtej pory\",\"usunięte od tamtej pory\",\"odrzucone przez zmianę profilu\",\"ma teraz obiekty\",\"warstwa {height} mm\",\"wiersz {line}: {text} (wierszy: {count})\",\"przesunięte od tamtej pory\",\"brak\",\"to nie liczba\",\"nie obrócono\",\"nie ustawiono\",\"nic nie zachowano\",\"stara nazwa zajęta\",\"nieaktualne\",\"płyta {n}\",\"płyta {n} · {error}\",\"zakresy ustawione od tamtej pory\",\"usunięte od tamtej pory\",\"cofnięte\",\"cofnięte przez ciebie\",\"ustawienia mogły nie wrócić do poprzedniego stanu\",\"płyta\",\"karta ustawień nie jest gotowa\",\"do {position}\",\"cofnięte przez przywrócenie\",\"twoja aplikacja\",\"{angle}° wokół {axis}\",\"{key}: to nie jest ustawienie druku, filamentu ani drukarki\",\"{key}: nieobsługiwana wartość\",\"{label} (filament {n})\",\"{label} (filamenty {list})\",\"{label} musi być większe niż {min} (podano {got})\",\"{label} musi być większe niż {min} (podano {got}); najniższą wartość dozwoloną przez drukarkę ustawia się w Ustawienia drukarki > Ograniczenia wysokości warstwy\",\"{label} musi być większe niż {min} i co najwyżej {max} (podano {got})\",\"{label} musi być większe niż {min} i co najwyżej {max} (podano {got}); najniższą wartość dozwoloną przez drukarkę ustawia się w Ustawienia drukarki > Ograniczenia wysokości warstwy\",\"{label} musi wynosić co najmniej {min} (podano {got})\",\"{label} musi wynosić co najmniej {min} (podano {got}); najniższą wartość dozwoloną przez drukarkę ustawia się w Ustawienia drukarki > Ograniczenia wysokości warstwy\",\"{label} może wynosić co najwyżej {max} (podano {got})\",\"{label} może wynosić co najwyżej {max} (podano {got}); najniższą wartość dozwoloną przez drukarkę ustawia się w Ustawienia drukarki > Ograniczenia wysokości warstwy\",\"{label} musi mieścić się między {min} a {max} (podano {got})\",\"{label} musi mieścić się między {min} a {max} (podano {got}); najniższą wartość dozwoloną przez drukarkę ustawia się w Ustawienia drukarki > Ograniczenia wysokości warstwy\",\"{label} niedozwolone\",\"{name} nie pasuje teraz do tej drukarki\",\"{name} już nie istnieje\",\"{name} już nie istnieje\",\"{name} nie ma już na płycie\",\"{name} nie ma już w tym miejscu\",\"{name} → {plate}\",\"{name}, teraz kopii: {n}\",\"{n} obiekt\",\"liczba obiektów: {n}\",\"{n} zakres\",\"liczba zakresów: {n}\",\"{object} nie ma już na płycie\",\"tylko {object}\",\"tylko {object} · z profilu druku\",\"{preset} (niezapisany)\",\"{text} (wierszy: {count})\",\"{title} i jeszcze {n}\",\"{value} w {preset}\",\"{verb} (niedostępne: {why})\",\"{verb} {what}\",\"{verb}: {label} na {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"pt_BR\": [\"(linha vazia)\",\"(oculto)\",\"Uma restauração está em andamento\",\"Altura da camada adaptativa\",\"Adicionar modelo\",\"Adicionar placa\",\"Adicionar ao Cursor\",\"Adicionar ao VS Code\",\"Adicionar {name}\",\"Adicionado\",\"Placa adicionada\",\"Já voltou para {before}\",\"Adicione também {setting} como variável de ambiente.\",\"Um objeto\",\"Argumentos\",\"Organizar\",\"Organizado\",\"Peça a impressão que você quer. As alterações aparecerão aqui, cada uma com Reverter.\",\"Assistente\",\"O acesso do assistente está desligado.\",\"O assistente definiu {value} · era {before}\",\"O assistente definiu {value} · você reverteu\",\"Configurações do assistente\",\"Orientar automaticamente\",\"Orientado automaticamente\",\"Nome integrado\",\"Predefinição integrada\",\"Ocupado\",\"Ocupado fatiando\",\"Ocupado com a solicitação do assistente; tente novamente em instantes\",\"Não é possível alterar de volta\",\"Não é possível verificar agora: {reason}\",\"Não é possível conectar: ele só alcança servidores na internet, não programas no seu computador.\",\"Não é possível excluir\",\"Não é possível colocar de volta após uma reinicialização; restaure um ponto anterior\",\"Não é possível colocar os intervalos de volta\",\"Não é possível voltar ao valor anterior\",\"Não é possível voltar ao valor anterior: {reason}\",\"Não é possível voltar ao anterior\",\"Não é possível rastrear este objeto após o projeto ser recarregado\",\"Cancelado\",\"Alterar de volta\",\"Devolver {label} para {before}\",\"Alterado novamente desde então\",\"Alterado novamente desde então; restaure um ponto anterior\",\"App de desktop do ChatGPT\",\"ChatGPT na web ou no celular\",\"App Claude\",\"Limpar intervalos de altura\",\"Codex em um terminal ou editor\",\"Recolher tudo\",\"Comando para iniciar\",\"{n} opção comparada\",\"{n} opção comparada · nada mantido\",\"{n} opção comparada · as configurações podem não ter voltado como estavam\",\"{n} opções comparadas\",\"{n} opções comparadas · nada mantido\",\"{n} opções comparadas · as configurações podem não ter voltado como estavam\",\"A comparação não conseguiu restaurar: {labels}\",\"Conecte seu app de IA\",\"Conectado\",\"Conectado a {app}\",\"Copiado\",\"Copiar\",\"Copiar configurações de .vscode/mcp.json em vez disso\",\"Copiar comando\",\"Copiar configurações\",\"Copiar configurações em vez disso\",\"Não foi possível restaurar\",\"Não foi possível reverter\",\"Não foi possível calcular\",\"Não foi possível excluir\",\"Não foi possível excluir\",\"Não foi possível mover\",\"Não foi possível mover de volta\",\"Não foi possível abrir\",\"Não foi possível abrir o ponto de restauração\",\"Não foi possível restaurar para {time}\",\"Não foi possível salvar\",\"Não foi possível salvar o estado atual antes\",\"Não foi possível selecionar\",\"Não foi possível desfazer\",\"O Cursor abre e pede confirmação. Requer uv.\",\"Excluir a Placa {n} (somente enquanto estiver vazia)\",\"Excluir placa\",\"Excluir placa {n}\",\"Excluir predefinição\",\"Excluir a predefinição {type} {name}\",\"Excluir {name}\",\"Excluir predefinição {type}\",\"Arquivo excluído\",\"Placa excluída\",\"Predefinição excluída\",\"Baixar\",\"Descartado por uma troca de predefinição\",\"Descartado por uma troca de predefinição; restaure um ponto anterior\",\"Duplicar o objeto {n}\",\"Duplicado\",\"Todo app precisa do uv, uma pequena ferramenta gratuita que executa o conector. Basta instalar uma vez.\",\"Expandir tudo\",\"Falhou\",\"Filamento\",\"Predefinição de filamento\",\"Bandejas de filamento\",\"Filamento {n}\",\"Cor do filamento {n}\",\"Bandeja do filamento {n}\",\"Arquivo\",\"O arquivo já existe\",\"Arquivo não encontrado\",\"Arquivos e predefinições salvos no disco permanecem.\",\"Pasta não encontrada\",\"Para chats. Baixe a extensão e abra o arquivo. Requer uv.\",\"Obter o uv\",\"Voltar para antes da restauração\",\"Intervalo de altura {min}–{max} mm\",\"Intervalos de altura limpos\",\"Valor oculto\",\"Em um terminal, um editor ou na aba Code do app. Requer uv.\",\"Nas configurações da extensão, defina \\\"OrcaSlicer settings folder\\\" como {folder}.\",\"Valor inválido\",\"Agora tem objetos\",\"Não está mais na placa\",\"Não existe mais\",\"Está selecionada; mude antes para outra predefinição\",\"É a última placa\",\"Os objetos dela foram movidos para o lado da mesa\",\"LM Studio, Windsurf e a maioria dos outros usam este formato. Requer uv.\",\"Última placa\",\"Altura da camada fora do intervalo\",\"Altura da camada reiniciada\",\"Linha {line} de {count}:\",\"Linhas {first} a {last} de {count}:\",\"Menu principal (ao lado de Arquivo) > Plugins > OrcaSlicer MCP > Configuração\",\"Modelo\",\"Mover de volta\",\"Mover o objeto {n}\",\"Mover o objeto {n} para a Placa {index}\",\"Mover {name}\",\"Mover {name} de volta para {path}\",\"Mover {name} de volta para {plate}\",\"Mover {name} de volta para onde estava\",\"Movido\",\"Movido novamente desde então\",\"Movido para a placa\",\"Nome\",\"Requer Preferências › Remote API › Allow deleting and replacing project files\",\"Requer Preferências › Remote API › Allow script, G-code and connection edits\",\"Requer Preferências › Remote API › Allow script, G-code and connection edits · {object}\",\"Requer a versão OrcaSlicer MCP\",\"Nunca salvo\",\"Novo projeto\",\"Nova predefinição {type}\",\"Não\",\"Nenhuma bandeja CANVAS\",\"Nenhuma alteração\",\"Nenhum ponto de restauração aqui\",\"Não há espaço para outra placa\",\"Essa alteração não existe\",\"Essa cópia não existe\",\"Esse filamento não existe\",\"Essa placa não existe\",\"Essa predefinição não existe\",\"Esse ponto de restauração não existe\",\"Essa bandeja não existe\",\"Não permitido\",\"Incompatível\",\"Não editável aqui\",\"Não está na lista recente\",\"Não está na linha do tempo deste projeto\",\"Não foi colocado de volta\",\"Não está pronto\",\"Não armazenado\",\"Não enquanto o OrcaSlicer está ocupado com uma tarefa\",\"Não enquanto o OrcaSlicer está fechando\",\"Não enquanto um projeto está sendo carregado\",\"Não enquanto está fatiando\",\"Não enquanto a janela está sendo reconstruída\",\"Nota\",\"Nada foi movido\",\"Nada na placa\",\"Nada para reverter\",\"Nada para mostrar nas configurações\",\"Nada para desfazer\",\"OK\",\"O objeto não tem cópia\",\"Objeto não encontrado\",\"Configurações do objeto {n}\",\"Desligado\",\"Ligado\",\"Abrir o projeto {name}\",\"Abrir {settings}\",\"Projeto aberto\",\"Abrindo…\",\"Painel do OrcaSlicer MCP fechado. Recupere em Visualizar > OrcaSlicer MCP panel.\",\"Painel do OrcaSlicer MCP fechado. Ele volta quando seu assistente usar o OrcaSlicer, ou pressione Espaço e escolha OrcaSlicer MCP.\",\"O OrcaSlicer informou um erro\",\"Outro app\",\"Outras predefinições herdam dela\",\"Desatualizado: algo mudou depois deste fatiamento\",\"Fora do intervalo\",\"Sobrepõe um intervalo\",\"Placa\",\"Placa bloqueada\",\"Placa {n}\",\"A placa {n} agora contém outros objetos\",\"Predefinição\",\"Predefinição em uso\",\"Predefinição de impressão\",\"Impressora\",\"Predefinição de impressora\",\"O projeto está aberto\",\"Colocar de volta\",\"Colocar de volta os intervalos de altura de {object}\",\"Colocar de volta o perfil de altura da camada anterior de {object}\",\"Colocar os {n} objetos de volta onde estavam\",\"Colocar {name} de volta em {plate}\",\"Qualidade {value}\",\"Somente leitura\",\"Recusado\",\"Rejeitado\",\"Configurações da Remote API\",\"Remover\",\"Remover cópia\",\"Remover o objeto {n}\",\"Remover intervalo\",\"Remover a nova cópia de {name}\",\"Remover a substituição de {label} em {object}\",\"Remover o intervalo de {min}–{max} mm de {object}\",\"Remover {name} da placa\",\"Removido\",\"Renomeado\",\"Predefinição {type} salva substituída\",\"Reiniciar altura da camada\",\"Pasta do ponto de restauração\",\"Os pontos de restauração requerem a versão OrcaSlicer MCP\",\"Restaurar o projeto como estava às {time} (desfaz as alterações posteriores)\",\"Restaurar para {time}?\",\"Restauração desfeita\",\"Restaurado para {time}\",\"Restaurando…\",\"Reverter\",\"Revertido\",\"Revertido às {time}\",\"Revertendo…\",\"Rotacionar de volta\",\"Rotacionar o objeto {n}\",\"Rotacionar {name} de volta\",\"Rotacionado\",\"Execute isto uma vez. O app de desktop compartilha a configuração. Requer uv.\",\"Mesmo caminho\",\"Salvar projeto\",\"Salvar o projeto como {name}\",\"Salvar predefinição {type}\",\"Predefinição salva\",\"Projeto salvo\",\"Escalar de volta\",\"Escalar o objeto {n}\",\"Escalar {name} de volta\",\"Escalado\",\"Scripts, G-code personalizado e configurações de conexão da impressora estão bloqueados. Marque 'Allow script, G-code and connection edits' em Preferências > Remote API para o assistente poder alterá-los, ou altere você mesmo aqui.\",\"Configure onde você usa o seu app\",\"Configure onde você usa o {app}\",\"Voltar o intervalo de {min}–{max} mm de {object} para {before}\",\"Devolver {label} para {before}\",\"Devolver {label} de {object} para {before}\",\"Configuração\",\"Configurações\",\"Configurações › Plugins › Adicionar › Adicionar servidor MCP, escolha STDIO e preencha cada campo abaixo. Funciona nos chats do Codex; chats comuns do ChatGPT ainda não podem usá-lo. Requer uv.\",\"Mostrar detalhes\",\"Mostrar como era e encontrar nas configurações\",\"Mostrar como era e desfazer\",\"Mostrar como era, desfazer e encontrar nas configurações\",\"Resultado do fatiamento\",\"Fatiado\",\"Fatiado {where}\",\"Fatiamento cancelado\",\"O fatiamento falhou\",\"Algo deu errado\",\"Algo deu errado: {reason}\",\"Voltar ao anterior\",\"Troca cancelada\",\"Voltar {label} para {before}\",\"Voltar {label} para {before} (descarta as edições não salvas)\",\"Esse filamento não existe mais\",\"A Remote API não pôde iniciar. Verifique a porta dela.\",\"A Remote API está desligada.\",\"A cópia já não existe\",\"A pasta antiga não existe mais\",\"O nome antigo já está em uso\",\"A placa mudou desde então; restaure um ponto anterior\",\"O plugin não iniciou\",\"O plugin nunca altera scripts, G-code nem as configurações de conexão da impressora\",\"O projeto mudou desde então\",\"Não é possível ler o ponto de restauração\",\"O ponto de restauração não existe mais\",\"O ponto de restauração foi alterado no disco, então não é usado\",\"A configuração não está mais nesta predefinição\",\"A aba de configurações não está pronta\",\"A aba de configurações não existe\",\"O instantâneo anterior à restauração não existe mais\",\"O instantâneo anterior à restauração foi alterado no disco, então não é usado\",\"Depois diga a impressão que você quer. Cada alteração feita aparecerá aqui, e você pode desfazer qualquer uma.\",\"Elas ainda podem manter os valores da última opção ({reason}).\",\"Bandeja não pronta\",\"Bandeja usada duas vezes\",\"Desfazer\",\"Configuração desconhecida\",\"Alterações não salvas\",\"Edições não salvas descartadas: {list}\",\"Arquivo não suportado\",\"Sem título\",\"O VS Code abre e pede confirmação. Requer uv.\",\"Aguardando o seu app…\",\"Aguardando {app}…\",\"Era {before}\",\"Quando um assistente altera algo, aparece aqui.\",\"Torre de purga X\",\"Torre de purga Y\",\"Número de valores incorreto\",\"Sim\",\"Você alterou desde então\",\"Configurações de MCP do seu app\",\"Suas próprias alterações desde então também são desfeitas.\",\"ao lado da mesa\",\"alterado por você\",\"alterado desde então\",\"cópia removida desde então\",\"excluído desde então\",\"descartado por uma troca de predefinição\",\"agora tem objetos\",\"camada {height} mm\",\"linha {line}: {text} ({count} linhas)\",\"movido desde então\",\"nenhum\",\"não é um número\",\"não rotacionado\",\"não definido\",\"nada mantido\",\"nome antigo em uso\",\"desatualizado\",\"placa {n}\",\"placa {n} · {error}\",\"intervalos definidos desde então\",\"removido desde então\",\"revertido\",\"revertido por você\",\"as configurações podem não ter voltado como estavam\",\"a placa\",\"a aba de configurações não está pronta\",\"para {position}\",\"desfeito pela restauração\",\"seu app\",\"{angle}° em torno de {axis}\",\"{key}: não é uma configuração de impressão, filamento nem impressora\",\"{key}: valor não suportado\",\"{label} (filamento {n})\",\"{label} (filamentos {list})\",\"{label} deve ser maior que {min} (recebido: {got})\",\"{label} deve ser maior que {min} (recebido: {got}); o mínimo que a impressora permite é definido em Configurações da impressora > Limites de altura da camada\",\"{label} deve ser maior que {min} e no máximo {max} (recebido: {got})\",\"{label} deve ser maior que {min} e no máximo {max} (recebido: {got}); o mínimo que a impressora permite é definido em Configurações da impressora > Limites de altura da camada\",\"{label} deve ser no mínimo {min} (recebido: {got})\",\"{label} deve ser no mínimo {min} (recebido: {got}); o mínimo que a impressora permite é definido em Configurações da impressora > Limites de altura da camada\",\"{label} deve ser no máximo {max} (recebido: {got})\",\"{label} deve ser no máximo {max} (recebido: {got}); o mínimo que a impressora permite é definido em Configurações da impressora > Limites de altura da camada\",\"{label} deve estar entre {min} e {max} (recebido: {got})\",\"{label} deve estar entre {min} e {max} (recebido: {got}); o mínimo que a impressora permite é definido em Configurações da impressora > Limites de altura da camada\",\"{label} não permitido\",\"{name} não serve mais para esta impressora\",\"{name} não existe mais\",\"{name} já não existe\",\"{name} não está mais na placa\",\"{name} não está mais lá\",\"{name} → {plate}\",\"{name}, agora {n} cópias\",\"{n} objeto\",\"{n} objetos\",\"{n} intervalo\",\"{n} intervalos\",\"{object} não está mais na placa\",\"Somente {object}\",\"Somente {object} · da predefinição de impressão\",\"{preset} (não salvo)\",\"{text} ({count} linhas)\",\"{title} e mais {n}\",\"{value} em {preset}\",\"{verb} (indisponível: {why})\",\"{verb} {what}\",\"{verb}: {label} de volta para {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"ru\": [\"(пустая строка)\",\"(скрыто)\",\"Идёт восстановление\",\"Адаптивная высота слоя\",\"Добавить модель\",\"Добавить стол\",\"Добавить в Cursor\",\"Добавить в VS Code\",\"Добавить {name}\",\"Добавлено\",\"Стол добавлен\",\"Уже возвращено к {before}\",\"Также добавьте {setting} как переменную окружения.\",\"Модель\",\"Аргументы\",\"Расставить\",\"Расставлено\",\"Расскажите, какую печать хотите. Его изменения появятся здесь, каждое с кнопкой «Вернуть».\",\"Ассистент\",\"Доступ ассистента выключен.\",\"Ассистент задал {value} · было {before}\",\"Ассистент задал {value} · вы вернули\",\"Настройки ассистента\",\"Автоориентация\",\"Ориентация подобрана\",\"Встроенное имя\",\"Встроенный профиль\",\"Занят\",\"Идёт нарезка\",\"Занят запросом ассистента; повторите чуть позже\",\"Не удалось изменить обратно\",\"Сейчас не удаётся проверить: {reason}\",\"Подключиться нельзя: оно доступно только серверам в интернете, но не программам на вашем компьютере.\",\"Не удалось удалить\",\"После перезапуска вернуть не получится; восстановите более раннюю точку\",\"Не удалось вернуть диапазоны\",\"Не удалось вернуть\",\"Не удалось вернуть: {reason}\",\"Не удалось переключить обратно\",\"После перезагрузки проекта эту модель отследить нельзя\",\"Отменено\",\"Изменить обратно\",\"Изменить {label} обратно на {before}\",\"С тех пор изменено снова\",\"С тех пор изменено снова; восстановите более раннюю точку\",\"Десктопное приложение ChatGPT\",\"ChatGPT в браузере или на телефоне\",\"Приложение Claude\",\"Очистить диапазоны высот\",\"Codex в терминале или редакторе\",\"Свернуть все\",\"Команда на запуск\",\"Сравнено вариантов: {n}\",\"Сравнено вариантов: {n} · ничего не сохранено\",\"Сравнено вариантов: {n} · настройки могли не вернуться к прежним\",\"Сравнено вариантов: {n}\",\"Сравнено вариантов: {n} · ничего не сохранено\",\"Сравнено вариантов: {n} · настройки могли не вернуться к прежним\",\"Сравнение не смогло вернуть: {labels}\",\"Подключите приложение с ИИ\",\"Подключено\",\"Подключено к {app}\",\"Скопировано\",\"Копировать\",\"Копировать настройки .vscode/mcp.json\",\"Копировать команду\",\"Копировать настройки\",\"Копировать настройки\",\"Не удалось восстановить\",\"Не удалось вернуть\",\"Не удалось вычислить\",\"Не удалось удалить\",\"Не удалось удалить\",\"Не удалось переместить\",\"Не удалось переместить обратно\",\"Не удалось открыть\",\"Не удалось открыть точку восстановления\",\"Не удалось восстановить состояние на {time}\",\"Не удалось сохранить\",\"Не удалось сначала сохранить текущее состояние\",\"Не удалось выбрать\",\"Не удалось отменить\",\"Cursor откроется и попросит подтвердить. Требуется uv.\",\"Удалить стол {n} (только пока он пуст)\",\"Удалить стол\",\"Удалить стол {n}\",\"Удалить профиль\",\"Удалить профиль {type} {name}\",\"Удалить {name}\",\"Удалить профиль {type}\",\"Файл удалён\",\"Стол удалён\",\"Профиль удалён\",\"Скачать\",\"Сброшено при смене профиля\",\"Сброшено при смене профиля; восстановите более раннюю точку\",\"Дублировать модель {n}\",\"Продублировано\",\"Каждому приложению нужен uv — небольшой бесплатный инструмент, который запускает коннектор. Достаточно установить его один раз.\",\"Развернуть все\",\"Ошибка\",\"Материал\",\"Профиль материала\",\"Лотки материалов\",\"Материал {n}\",\"Цвет материала {n}\",\"Лоток материала {n}\",\"Файл\",\"Файл уже существует\",\"Файл не найден\",\"Сохранённые на диск файлы и профили остаются.\",\"Папка не найдена\",\"Для чатов. Скачайте расширение, затем откройте файл. Требуется uv.\",\"Скачать uv\",\"Вернуться к состоянию до восстановления\",\"Диапазон высот {min}–{max} mm\",\"Диапазоны высот очищены\",\"Скрытое значение\",\"В терминале, редакторе или на вкладке Code приложения. Требуется uv.\",\"В настройках расширения укажите для «OrcaSlicer settings folder» значение {folder}.\",\"Недопустимое значение\",\"Теперь на нём есть модели\",\"Его больше нет на столе\",\"Этого больше нет\",\"Он выбран; сначала переключитесь на другой профиль\",\"Это последний стол\",\"Его модели перемещены за край стола\",\"LM Studio, Windsurf и большинство других используют эту форму. Требуется uv.\",\"Последний стол\",\"Высота слоя вне допустимых пределов\",\"Высота слоя сброшена\",\"Строка {line} из {count}:\",\"Строки с {first} по {last} из {count}:\",\"Главное меню (рядом с Файл) > Плагины > OrcaSlicer MCP > Настройки\",\"Модель\",\"Переместить обратно\",\"Переместить модель {n}\",\"Переместить модель {n} на Стол {index}\",\"Переместить {name}\",\"Переместить {name} обратно в {path}\",\"Переместить {name} обратно на {plate}\",\"Вернуть {name} на прежнее место\",\"Перемещено\",\"С тех пор перемещено снова\",\"Перемещено на стол\",\"Имя\",\"Требуется: Настройки › Remote API › Allow deleting and replacing project files\",\"Требуется: Настройки › Remote API › Allow script, G-code and connection edits\",\"Требуется: Настройки › Remote API › Allow script, G-code and connection edits · {object}\",\"Требуется сборка OrcaSlicer MCP\",\"Не сохранялось\",\"Новый проект\",\"Новый профиль {type}\",\"Нет\",\"Нет лотков CANVAS\",\"Нет изменений\",\"Здесь нет точки восстановления\",\"Нет места для ещё одного стола\",\"Такого изменения нет\",\"Такой копии нет\",\"Такого материала нет\",\"Такого стола нет\",\"Такого профиля нет\",\"Такой точки восстановления нет\",\"Такого лотка нет\",\"Не разрешено\",\"Несовместимо\",\"Здесь не редактируется\",\"Нет в списке недавних\",\"Нет в истории этого проекта\",\"Не возвращено\",\"Не готово\",\"Не сохранено\",\"Недоступно, пока OrcaSlicer занят заданием\",\"Недоступно, пока OrcaSlicer закрывается\",\"Недоступно, пока загружается проект\",\"Недоступно во время нарезки\",\"Недоступно, пока окно перестраивается\",\"Заметка\",\"Ничего не перемещено\",\"На столе ничего нет\",\"Нечего возвращать\",\"В настройках нечего показать\",\"Нечего отменять\",\"Ок\",\"У модели нет копии\",\"Модель не найдена\",\"Настройки модели {n}\",\"Выкл\",\"Вкл\",\"Открыть проект {name}\",\"Открыть: {settings}\",\"Проект открыт\",\"Открывается…\",\"Панель OrcaSlicer MCP закрыта. Вернуть её можно через Вид > OrcaSlicer MCP panel.\",\"Панель OrcaSlicer MCP закрыта. Она вернётся, когда ваш ассистент в следующий раз использует OrcaSlicer, или нажмите Пробел и выберите OrcaSlicer MCP.\",\"OrcaSlicer сообщил об ошибке\",\"Другое приложение\",\"Другие профили наследуют его\",\"Устарело: после этой нарезки что-то изменилось\",\"Вне диапазона\",\"Пересекается с диапазоном\",\"Стол\",\"Стол заблокирован\",\"Стол {n}\",\"На столе {n} теперь другие модели\",\"Профиль\",\"Профиль используется\",\"Профиль печати\",\"Принтер\",\"Профиль принтера\",\"Проект открыт\",\"Вернуть\",\"Вернуть диапазоны высот для {object}\",\"Вернуть прежний профиль высоты слоя для {object}\",\"Вернуть {n} моделей на прежние места\",\"Вернуть {name} на {plate}\",\"Качество {value}\",\"Только чтение\",\"Отклонено\",\"Отклонено\",\"Настройки Remote API\",\"Удалить\",\"Удалить копию\",\"Удалить модель {n}\",\"Удалить диапазон\",\"Удалить новую копию {name}\",\"Удалить переопределение {label} у {object}\",\"Удалить диапазон {min}–{max} mm у {object}\",\"Убрать {name} со стола\",\"Удалено\",\"Переименовано\",\"Заменён сохранённый профиль {type}\",\"Сбросить высоту слоя\",\"Папка точки восстановления\",\"Для точек восстановления нужна сборка OrcaSlicer MCP\",\"Восстановить проект в состояние на {time} (отменяет последующие изменения)\",\"Восстановить состояние на {time}?\",\"Восстановление отменено\",\"Восстановлено состояние на {time}\",\"Восстановление…\",\"Вернуть\",\"Возвращено\",\"Возвращено в {time}\",\"Возврат…\",\"Повернуть обратно\",\"Повернуть модель {n}\",\"Повернуть {name} обратно\",\"Повёрнуто\",\"Выполните это один раз. Десктопное приложение использует те же настройки. Требуется uv.\",\"Тот же путь\",\"Сохранить проект\",\"Сохранить проект как {name}\",\"Сохранить профиль {type}\",\"Профиль сохранён\",\"Проект сохранён\",\"Вернуть масштаб\",\"Изменить масштаб модели {n}\",\"Вернуть масштаб {name}\",\"Масштаб изменён\",\"Скрипты, пользовательский G-code и настройки подключения принтера заблокированы. Отметьте «Allow script, G-code and connection edits» в Настройки > Remote API, чтобы ассистент мог их менять, или измените их сами здесь.\",\"Настройте там, где вы используете ваше приложение\",\"Настройте там, где вы используете {app}\",\"Вернуть диапазон {min}–{max} mm у {object} к значению {before}\",\"Вернуть {label} к значению {before}\",\"Вернуть {label} у {object} к значению {before}\",\"Настройка\",\"Настройки\",\"Настройки › Плагины › Добавить › Добавить сервер MCP, выберите STDIO и заполните каждое поле ниже. Работает в чатах Codex; обычные чаты ChatGPT пока не могут это использовать. Требуется uv.\",\"Подробнее\",\"Показать прежнее значение и найти в настройках\",\"Показать прежнее значение и отменить\",\"Показать прежнее значение, отменить и найти в настройках\",\"Результат нарезки\",\"Нарезано\",\"Нарезано: {where}\",\"Нарезка отменена\",\"Нарезка не удалась\",\"Что-то пошло не так\",\"Что-то пошло не так: {reason}\",\"Переключить обратно\",\"Переключение отменено\",\"Переключить {label} обратно на {before}\",\"Переключить {label} обратно на {before} (несохранённые правки будут потеряны)\",\"Этого материала больше нет\",\"Remote API не удалось запустить. Проверьте его порт.\",\"Remote API выключен.\",\"Копии уже нет\",\"Старой папки больше нет\",\"Старое имя теперь занято\",\"С тех пор стол изменился; восстановите более раннюю точку\",\"Плагин не запустился\",\"Плагин никогда не меняет скрипты, G-code и настройки подключения к принтеру\",\"С тех пор проект изменился\",\"Точку восстановления не удаётся прочитать\",\"Точки восстановления больше нет\",\"Точка восстановления была изменена на диске, поэтому не используется\",\"Этой настройки больше нет в этом профиле\",\"Вкладка настроек не готова\",\"Вкладки настроек нет\",\"Снимка до восстановления больше нет\",\"Снимок до восстановления был изменён на диске, поэтому не используется\",\"Затем расскажите, какую печать хотите. Каждое его изменение появляется здесь, и любое можно отменить.\",\"В них могут остаться значения последнего варианта ({reason}).\",\"Лоток не готов\",\"Лоток использован дважды\",\"Отменить\",\"Неизвестная настройка\",\"Несохранённые изменения\",\"Несохранённые правки отброшены: {list}\",\"Неподдерживаемый файл\",\"Без названия\",\"VS Code откроется и попросит подтвердить. Требуется uv.\",\"Ожидание вашего приложения…\",\"Ожидание {app}…\",\"Было {before}\",\"Когда ассистент что-то меняет, это появляется здесь.\",\"Черновая башня X\",\"Черновая башня Y\",\"Неверное число значений\",\"Да\",\"С тех пор вы изменили это\",\"Настройки MCP вашего приложения\",\"Ваши собственные последующие изменения тоже будут отменены.\",\"за краем стола\",\"изменено вами\",\"изменено с тех пор\",\"копия удалена с тех пор\",\"удалено с тех пор\",\"сброшено при смене профиля\",\"теперь есть модели\",\"слой {height} mm\",\"строка {line}: {text} (строк: {count})\",\"перемещено с тех пор\",\"нет\",\"не число\",\"не повёрнуто\",\"не задано\",\"ничего не сохранено\",\"старое имя занято\",\"устарело\",\"стол {n}\",\"стол {n} · {error}\",\"диапазоны заданы с тех пор\",\"удалено с тех пор\",\"возвращено\",\"возвращено вами\",\"настройки могли не вернуться к прежним\",\"стол\",\"вкладка настроек не готова\",\"в {position}\",\"отменено восстановлением\",\"ваше приложение\",\"{angle}° вокруг {axis}\",\"{key}: это не настройка печати, материала или принтера\",\"{key}: неподдерживаемое значение\",\"{label} (материал {n})\",\"{label} (материалы {list})\",\"{label} должно быть больше {min} (получено {got})\",\"{label} должно быть больше {min} (получено {got}); минимум, который допускает принтер, задаётся в Настройки принтера > Ограничение высоты слоя\",\"{label} должно быть больше {min} и не больше {max} (получено {got})\",\"{label} должно быть больше {min} и не больше {max} (получено {got}); минимум, который допускает принтер, задаётся в Настройки принтера > Ограничение высоты слоя\",\"{label} должно быть не меньше {min} (получено {got})\",\"{label} должно быть не меньше {min} (получено {got}); минимум, который допускает принтер, задаётся в Настройки принтера > Ограничение высоты слоя\",\"{label} должно быть не больше {max} (получено {got})\",\"{label} должно быть не больше {max} (получено {got}); минимум, который допускает принтер, задаётся в Настройки принтера > Ограничение высоты слоя\",\"{label} должно быть от {min} до {max} (получено {got})\",\"{label} должно быть от {min} до {max} (получено {got}); минимум, который допускает принтер, задаётся в Настройки принтера > Ограничение высоты слоя\",\"{label}: не разрешено\",\"{name} сейчас не подходит этому принтеру\",\"{name} пропал\",\"{name} уже пропал\",\"{name} больше не на столе\",\"{name} больше нет\",\"{name} → {plate}\",\"{name}, теперь копий: {n}\",\"{n} модель\",\"моделей: {n}\",\"{n} диапазон\",\"диапазонов: {n}\",\"{object} больше не на столе\",\"Только {object}\",\"Только {object} · из профиля печати\",\"{preset} (не сохранён)\",\"{text} (строк: {count})\",\"{title} и ещё {n}\",\"{value} в {preset}\",\"{verb} (недоступно: {why})\",\"{verb} {what}\",\"{verb}: {label} обратно на {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"sv\": [\"(tom rad)\",\"(dolt)\",\"En återställning pågår\",\"Adaptiv lagerhöjd\",\"Lägg till modell\",\"Lägg till platta\",\"Lägg till i Cursor\",\"Lägg till i VS Code\",\"Lägg till {name}\",\"Tillagd\",\"Platta tillagd\",\"Redan tillbaka till {before}\",\"Lägg också till {setting} som en miljövariabel.\",\"Ett objekt\",\"Argument\",\"Arrangera\",\"Arrangerade\",\"Be assistenten om den utskrift du vill ha. Dess ändringar visas här, var och en med Återgå.\",\"Assistent\",\"Assistentens åtkomst är av.\",\"Assistenten satte {value} · var {before}\",\"Assistenten satte {value} · du ställde tillbaka det\",\"Assistentinställningar\",\"Orientera automatiskt\",\"Orienterades automatiskt\",\"Inbyggt namn\",\"Inbyggd förinställning\",\"Upptagen\",\"Upptagen med att bereda\",\"Upptagen med assistentens begäran; försök igen om en stund\",\"Kan inte ändras tillbaka\",\"Kan inte kontrolleras nu: {reason}\",\"Kan inte ansluta: den når bara servrar på internet, inte program på din dator.\",\"Kan inte raderas\",\"Kan inte läggas tillbaka efter en omstart; återställ en tidigare punkt i stället\",\"Kan inte lägga tillbaka intervallen\",\"Kan inte ställas tillbaka\",\"Kan inte ställas tillbaka: {reason}\",\"Kan inte växla tillbaka\",\"Kan inte följa det här objektet efter att projektet laddats om\",\"Avbröts\",\"Ändra tillbaka\",\"Ändra {label} tillbaka till {before}\",\"Ändrad igen sedan dess\",\"Ändrad igen sedan dess; återställ en tidigare punkt i stället\",\"ChatGPT-skrivbordsappen\",\"ChatGPT på webben eller i mobilen\",\"Claude-appen\",\"Rensa höjdintervall\",\"Codex i en terminal eller redigerare\",\"Fäll ihop alla\",\"Kommando för att starta\",\"{n} alternativ jämfört\",\"{n} alternativ jämfört · inget behölls\",\"{n} alternativ jämfört · inställningarna är kanske inte som förut\",\"{n} alternativ jämförda\",\"{n} alternativ jämförda · inget behölls\",\"{n} alternativ jämförda · inställningarna är kanske inte som förut\",\"Jämförelsen kunde inte lägga tillbaka: {labels}\",\"Anslut din AI-app\",\"Ansluten\",\"Ansluten till {app}\",\"Kopierad\",\"Kopiera\",\"Kopiera .vscode/mcp.json-inställningarna i stället\",\"Kopiera kommando\",\"Kopiera inställningar\",\"Kopiera inställningarna i stället\",\"Kunde inte återställa\",\"Kunde inte återgå\",\"Kunde inte beräkna\",\"Kunde inte radera\",\"Kunde inte raderas\",\"Kunde inte flytta\",\"Kunde inte flyttas tillbaka\",\"Kunde inte öppnas\",\"Kunde inte öppna återställningspunkten\",\"Kunde inte återställa till {time}\",\"Kunde inte spara\",\"Kunde inte spara det aktuella tillståndet först\",\"Kunde inte välja\",\"Kunde inte ångras\",\"Cursor öppnas och ber dig bekräfta. Kräver uv.\",\"Radera platta {n} (bara när den är tom)\",\"Radera platta\",\"Radera platta {n}\",\"Radera förinställning\",\"Radera {type}-förinställningen {name}\",\"Radera {name}\",\"Radera {type}-förinställning\",\"Fil raderad\",\"Platta raderad\",\"Förinställning raderad\",\"Ladda ner\",\"Förlorad vid byte av förinställning\",\"Förlorad vid byte av förinställning; återställ en tidigare punkt i stället\",\"Duplicera objekt {n}\",\"Duplicerad\",\"Alla appar behöver uv, ett litet gratis verktyg som kör kopplingen. Det räcker att installera det en gång.\",\"Fäll ut alla\",\"Misslyckades\",\"Filament\",\"Filamentförinställning\",\"Filamentfack\",\"Filament {n}\",\"Färg för filament {n}\",\"Fack för filament {n}\",\"Fil\",\"Filen finns redan\",\"Filen hittades inte\",\"Filer och förinställningar som sparats på disk finns kvar.\",\"Mappen hittades inte\",\"För chattar. Ladda ner tillägget och öppna sedan filen. Kräver uv.\",\"Hämta uv\",\"Gå tillbaka till läget före återställningen\",\"Höjdintervall {min}–{max} mm\",\"Höjdintervall rensade\",\"Dolt värde\",\"I en terminal, en redigerare eller appens Code-flik. Kräver uv.\",\"Ställ in \\\"OrcaSlicer settings folder\\\" på {folder} i tilläggets inställningar.\",\"Ogiltigt värde\",\"Det finns objekt på den nu\",\"Den ligger inte längre på plattan\",\"Den finns inte kvar\",\"Den är vald; växla först till en annan förinställning\",\"Det är den sista plattan\",\"Dess objekt flyttades bredvid byggplattan\",\"LM Studio, Windsurf och de flesta andra använder den här formen. Kräver uv.\",\"Sista plattan\",\"Lagerhöjd utanför intervallet\",\"Lagerhöjd återställd\",\"Rad {line} av {count}:\",\"Rad {first} till {last} av {count}:\",\"Huvudmenyn (bredvid Fil) > Insticksmoduler > OrcaSlicer MCP > Konfiguration\",\"Modell\",\"Flytta tillbaka\",\"Flytta objekt {n}\",\"Flytta objekt {n} till platta {index}\",\"Flytta {name}\",\"Flytta {name} tillbaka till {path}\",\"Flytta {name} tillbaka till {plate}\",\"Flytta {name} tillbaka dit den var\",\"Flyttad\",\"Flyttad igen sedan dess\",\"Flyttad till platta\",\"Namn\",\"Kräver Inställningar › Remote API › Allow deleting and replacing project files\",\"Kräver Inställningar › Remote API › Allow script, G-code and connection edits\",\"Kräver Inställningar › Remote API › Allow script, G-code and connection edits · {object}\",\"Kräver OrcaSlicer MCP-bygget\",\"Aldrig sparad\",\"Nytt projekt\",\"Ny {type}-förinställning\",\"Nej\",\"Inga CANVAS-fack\",\"Inga ändringar\",\"Ingen återställningspunkt här\",\"Inget utrymme för ytterligare en platta\",\"Ingen sådan ändring\",\"Ingen sådan kopia\",\"Inget sådant filament\",\"Ingen sådan platta\",\"Ingen sådan förinställning\",\"Ingen sådan återställningspunkt\",\"Inget sådant fack\",\"Inte tillåtet\",\"Inte kompatibel\",\"Kan inte redigeras här\",\"Finns inte i listan över senaste\",\"Finns inte i det här projektets tidslinje\",\"Inte tillbakalagd\",\"Inte redo\",\"Inte lagrad\",\"Inte medan OrcaSlicer är upptaget med ett jobb\",\"Inte medan OrcaSlicer stängs\",\"Inte medan ett projekt laddas\",\"Inte under beredning\",\"Inte medan fönstret byggs om\",\"Anteckning\",\"Inget flyttades\",\"Inget på plattan\",\"Inget att återgå till\",\"Inget att visa i inställningarna\",\"Inget att ångra\",\"OK\",\"Objektet har ingen kopia\",\"Objektet hittades inte\",\"Objektinställningar {n}\",\"Av\",\"På\",\"Öppna projektet {name}\",\"Öppna {settings}\",\"Öppnade projekt\",\"Öppnar…\",\"OrcaSlicer MCP-panelen stängdes. Öppna den igen via Vy > OrcaSlicer MCP panel.\",\"OrcaSlicer MCP-panelen stängdes. Den kommer tillbaka när din assistent nästa gång använder OrcaSlicer, eller tryck på mellanslag och välj OrcaSlicer MCP.\",\"OrcaSlicer rapporterade ett fel\",\"Annan app\",\"Andra förinställningar ärver den\",\"Inaktuell: något ändrades efter den här beredningen\",\"Utanför intervallet\",\"Överlappar ett intervall\",\"Platta\",\"Plattan är låst\",\"Platta {n}\",\"Platta {n} innehåller andra objekt nu\",\"Förinställning\",\"Förinställningen används\",\"Processförinställning\",\"Skrivare\",\"Skrivarförinställning\",\"Projektet är öppet\",\"Lägg tillbaka\",\"Lägg tillbaka höjdintervallen för {object}\",\"Lägg tillbaka den tidigare lagerhöjdsprofilen för {object}\",\"Lägg tillbaka de {n} objekten dit de var\",\"Lägg tillbaka {name} på {plate}\",\"Kvalitet {value}\",\"Skrivskyddad\",\"Nekad\",\"Avvisad\",\"Remote API-inställningar\",\"Ta bort\",\"Ta bort kopia\",\"Ta bort objekt {n}\",\"Ta bort intervall\",\"Ta bort den nya kopian av {name}\",\"Ta bort åsidosättningen av {label} från {object}\",\"Ta bort intervallet {min}–{max} mm från {object}\",\"Ta bort {name} från plattan\",\"Borttagen\",\"Bytte namn\",\"Ersatte den sparade {type}-förinställningen\",\"Återställ lagerhöjd\",\"Mapp för återställningspunkter\",\"Återställningspunkter kräver OrcaSlicer MCP-bygget\",\"Återställ projektet till hur det var kl. {time} (ångrar senare ändringar)\",\"Återställ till {time}?\",\"Återställning ångrad\",\"Återställd till {time}\",\"Återställer…\",\"Återgå\",\"Återgått\",\"Återgått kl. {time}\",\"Återgår…\",\"Rotera tillbaka\",\"Rotera objekt {n}\",\"Rotera {name} tillbaka\",\"Roterad\",\"Kör detta en gång. Skrivbordsappen delar inställningen. Kräver uv.\",\"Samma sökväg\",\"Spara projekt\",\"Spara projekt som {name}\",\"Spara {type}-förinställning\",\"Förinställning sparad\",\"Projekt sparat\",\"Skala tillbaka\",\"Skala objekt {n}\",\"Skala {name} tillbaka\",\"Skalad\",\"Skript, anpassad G-code och skrivaranslutningsinställningar är låsta. Bocka i 'Allow script, G-code and connection edits' under Inställningar > Remote API så att assistenten får ändra dem, eller ändra dem själv här.\",\"Ställ in det där du använder din app\",\"Ställ in det där du använder {app}\",\"Ställ tillbaka intervallet {min}–{max} mm för {object} till {before}\",\"Ställ tillbaka {label} till {before}\",\"Ställ tillbaka {label} på {object} till {before}\",\"Inställning\",\"Inställningar\",\"Inställningar › Pluginer › Lägg till › Lägg till MCP-server, välj STDIO och fyll i varje fält nedan. Fungerar i Codex-chattar; vanliga ChatGPT-chattar kan inte använda det ännu. Kräver uv.\",\"Visa detaljer\",\"Visa hur det var och hitta det i inställningarna\",\"Visa hur det var och ångra det\",\"Visa hur det var, ångra det och hitta det i inställningarna\",\"Beredningsresultat\",\"Beredd\",\"Beredd {where}\",\"Beredningen avbröts\",\"Beredningen misslyckades\",\"Något gick fel\",\"Något gick fel: {reason}\",\"Växla tillbaka\",\"Växlingen avbröts\",\"Växla {label} tillbaka till {before}\",\"Växla {label} tillbaka till {before} (förkastar osparade ändringar)\",\"Det filamentet finns inte längre\",\"Remote API kunde inte starta. Kontrollera porten.\",\"Remote API är av.\",\"Kopian är redan borta\",\"Den gamla mappen finns inte längre\",\"Det gamla namnet är upptaget nu\",\"Plattan har ändrats sedan dess; återställ en tidigare punkt i stället\",\"Insticksmodulen startade inte\",\"Insticksmodulen ändrar aldrig skript, G-kod eller skrivarens anslutningsinställningar\",\"Projektet har ändrats sedan dess\",\"Återställningspunkten kan inte läsas\",\"Återställningspunkten finns inte längre\",\"Återställningspunkten ändrades på disk och används därför inte\",\"Inställningen finns inte kvar i den här förinställningen\",\"Inställningsfliken är inte redo\",\"Inställningsfliken finns inte\",\"Ögonblicksbilden före återställningen finns inte längre\",\"Ögonblicksbilden före återställningen ändrades på disk och används därför inte\",\"Berätta sedan vilken utskrift du vill ha. Varje ändring den gör visas här, och du kan ångra vilken som helst.\",\"De kan fortfarande ha värdena från det sista alternativet ({reason}).\",\"Facket är inte redo\",\"Facket används två gånger\",\"Ångra\",\"Okänd inställning\",\"Osparade ändringar\",\"Osparade ändringar förkastade: {list}\",\"Filtypen stöds inte\",\"Namnlös\",\"VS Code öppnas och ber dig bekräfta. Kräver uv.\",\"Väntar på din app…\",\"Väntar på {app}…\",\"Var {before}\",\"När en assistent ändrar något visas det här.\",\"Prime torn X\",\"Prime torn Y\",\"Fel antal värden\",\"Ja\",\"Ändrad av dig sedan dess\",\"Din apps MCP-inställningar\",\"Dina egna ändringar sedan dess ångras också.\",\"bredvid byggplattan\",\"ändrad av dig\",\"ändrad sedan dess\",\"kopia borttagen sedan dess\",\"raderad sedan dess\",\"förlorad vid byte av förinställning\",\"innehåller objekt nu\",\"lager {height} mm\",\"rad {line}: {text} ({count} rader)\",\"flyttad sedan dess\",\"inga\",\"inte ett tal\",\"inte roterad\",\"inte inställd\",\"inget behölls\",\"gammalt namn upptaget\",\"inaktuell\",\"platta {n}\",\"platta {n} · {error}\",\"intervall satta sedan dess\",\"borttagen sedan dess\",\"återgått\",\"tillbakaställd av dig\",\"inställningarna är kanske inte som förut\",\"plattan\",\"inställningsfliken är inte redo\",\"till {position}\",\"ångrad av återställningen\",\"din app\",\"{angle}° kring {axis}\",\"{key}: inte en process-, filament- eller skrivarinställning\",\"{key}: värdet stöds inte\",\"{label} (filament {n})\",\"{label} (filament {list})\",\"{label} måste vara större än {min} (fick {got})\",\"{label} måste vara större än {min} (fick {got}); den lägsta nivån som skrivaren tillåter anges i Skrivarens inställningar > Lagerhöjds begränsning\",\"{label} måste vara större än {min} och högst {max} (fick {got})\",\"{label} måste vara större än {min} och högst {max} (fick {got}); den lägsta nivån som skrivaren tillåter anges i Skrivarens inställningar > Lagerhöjds begränsning\",\"{label} måste vara minst {min} (fick {got})\",\"{label} måste vara minst {min} (fick {got}); den lägsta nivån som skrivaren tillåter anges i Skrivarens inställningar > Lagerhöjds begränsning\",\"{label} får vara högst {max} (fick {got})\",\"{label} får vara högst {max} (fick {got}); den lägsta nivån som skrivaren tillåter anges i Skrivarens inställningar > Lagerhöjds begränsning\",\"{label} måste vara mellan {min} och {max} (fick {got})\",\"{label} måste vara mellan {min} och {max} (fick {got}); den lägsta nivån som skrivaren tillåter anges i Skrivarens inställningar > Lagerhöjds begränsning\",\"{label} är inte tillåtet\",\"{name} passar inte den här skrivaren längre\",\"{name} finns inte längre\",\"{name} är redan borta\",\"{name} ligger inte längre på plattan\",\"{name} finns inte kvar\",\"{name} → {plate}\",\"{name}, nu {n} kopior\",\"{n} objekt\",\"{n} objekt\",\"{n} intervall\",\"{n} intervall\",\"{object} ligger inte längre på plattan\",\"Bara {object}\",\"Bara {object} · från processförinställningen\",\"{preset} (osparad)\",\"{text} ({count} rader)\",\"{title} och {n} till\",\"{value} i {preset}\",\"{verb} (inte tillgängligt: {why})\",\"{verb} {what}\",\"{verb}: {label} tillbaka till {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"th\": [\"(บรรทัดว่าง)\",\"(ซ่อน)\",\"กำลังคืนค่าอยู่\",\"ความสูงเลเยอร์แบบปรับได้\",\"เพิ่มโมเดล\",\"เพิ่มเพลต\",\"เพิ่มใน Cursor\",\"เพิ่มใน VS Code\",\"เพิ่ม {name}\",\"เพิ่มแล้ว\",\"เพิ่มเพลตแล้ว\",\"กลับเป็น {before} แล้ว\",\"เพิ่ม {setting} เป็นตัวแปรสภาพแวดล้อมด้วย\",\"วัตถุ\",\"อาร์กิวเมนต์\",\"จัดเรียง\",\"จัดเรียงแล้ว\",\"บอกชิ้นงานที่ต้องการพิมพ์ การเปลี่ยนแปลงของผู้ช่วยจะแสดงที่นี่ พร้อมปุ่มย้อนกลับ\",\"ผู้ช่วย\",\"การเข้าถึงของผู้ช่วยปิดอยู่\",\"ผู้ช่วยตั้งเป็น {value} · เดิม {before}\",\"ผู้ช่วยตั้งเป็น {value} · คุณตั้งกลับแล้ว\",\"การตั้งค่าผู้ช่วย\",\"จัดวางทิศทางอัตโนมัติ\",\"จัดวางทิศทางอัตโนมัติแล้ว\",\"ชื่อที่มีมาในตัว\",\"พรีเซ็ตที่มีมาในตัว\",\"ไม่ว่าง\",\"กำลังสไลซ์อยู่\",\"กำลังทำตามคำขอของผู้ช่วย ลองอีกครั้งในอีกสักครู่\",\"เปลี่ยนกลับไม่ได้\",\"ตรวจสอบตอนนี้ไม่ได้: {reason}\",\"เชื่อมต่อไม่ได้ เพราะเข้าถึงได้เฉพาะเซิร์ฟเวอร์บนอินเทอร์เน็ต ไม่ใช่โปรแกรมบนคอมพิวเตอร์ของคุณ\",\"ลบไม่ได้\",\"ใส่คืนไม่ได้หลังเริ่มใหม่ ให้คืนค่าไปยังจุดก่อนหน้าแทน\",\"ใส่ช่วงคืนไม่ได้\",\"ตั้งกลับไม่ได้\",\"ตั้งกลับไม่ได้: {reason}\",\"สลับกลับไม่ได้\",\"ติดตามวัตถุนี้ไม่ได้หลังจากโหลดโปรเจกต์ใหม่\",\"ยกเลิกแล้ว\",\"เปลี่ยนกลับ\",\"เปลี่ยน {label} กลับเป็น {before}\",\"มีการเปลี่ยนอีกครั้งหลังจากนั้น\",\"มีการเปลี่ยนอีกครั้งหลังจากนั้น ให้คืนค่าไปยังจุดก่อนหน้าแทน\",\"แอป ChatGPT บนเดสก์ท็อป\",\"ChatGPT บนเว็บหรือโทรศัพท์\",\"แอป Claude\",\"ล้างช่วงความสูง\",\"Codex ในเทอร์มินัลหรือตัวแก้ไข\",\"ยุบทั้งหมด\",\"คำสั่งเปิด\",\"เปรียบเทียบ {n} ตัวเลือก\",\"เปรียบเทียบ {n} ตัวเลือก · ไม่ได้เก็บอะไรไว้\",\"เปรียบเทียบ {n} ตัวเลือก · การตั้งค่าอาจไม่กลับเป็นเหมือนเดิม\",\"เปรียบเทียบ {n} ตัวเลือก\",\"เปรียบเทียบ {n} ตัวเลือก · ไม่ได้เก็บอะไรไว้\",\"เปรียบเทียบ {n} ตัวเลือก · การตั้งค่าอาจไม่กลับเป็นเหมือนเดิม\",\"การเปรียบเทียบใส่คืนไม่ได้: {labels}\",\"เชื่อมต่อแอป AI ของคุณ\",\"เชื่อมต่อแล้ว\",\"เชื่อมต่อกับ {app} แล้ว\",\"คัดลอกแล้ว\",\"คัดลอก\",\"คัดลอกการตั้งค่า .vscode/mcp.json แทน\",\"คัดลอกคำสั่ง\",\"คัดลอกการตั้งค่า\",\"คัดลอกการตั้งค่าแทน\",\"คืนค่าไม่ได้\",\"ย้อนกลับไม่ได้\",\"คำนวณไม่ได้\",\"ลบไม่ได้\",\"ลบไม่ได้\",\"ย้ายไม่ได้\",\"ย้ายกลับไม่ได้\",\"เปิดไม่ได้\",\"เปิดจุดคืนค่าไม่ได้\",\"คืนค่าไปยัง {time} ไม่ได้\",\"บันทึกไม่ได้\",\"บันทึกสถานะปัจจุบันก่อนไม่ได้\",\"เลือกไม่ได้\",\"เลิกทำไม่ได้\",\"Cursor จะเปิดขึ้นและขอให้คุณยืนยัน ต้องมี uv\",\"ลบเพลต {n} (เฉพาะเมื่อว่างเปล่า)\",\"ลบเพลต\",\"ลบเพลต {n}\",\"ลบพรีเซ็ต\",\"ลบพรีเซ็ต {type} {name}\",\"ลบ {name}\",\"ลบพรีเซ็ต {type}\",\"ลบไฟล์แล้ว\",\"ลบเพลตแล้ว\",\"ลบพรีเซ็ตแล้ว\",\"ดาวน์โหลด\",\"ถูกทิ้งเพราะสลับพรีเซ็ต\",\"ถูกทิ้งเพราะสลับพรีเซ็ต ให้คืนค่าไปยังจุดก่อนหน้าแทน\",\"ทำสำเนาวัตถุ {n}\",\"ทำสำเนาแล้ว\",\"ทุกแอปต้องมี uv ซึ่งเป็นเครื่องมือฟรีขนาดเล็กที่ใช้รันตัวเชื่อมต่อ ติดตั้งเพียงครั้งเดียวก็พอ\",\"ขยายทั้งหมด\",\"ล้มเหลว\",\"เส้นพลาสติก\",\"พรีเซ็ตเส้นพลาสติก\",\"ถาดเส้นพลาสติก\",\"เส้นพลาสติก {n}\",\"สีเส้นพลาสติก {n}\",\"ถาดเส้นพลาสติก {n}\",\"ไฟล์\",\"มีไฟล์นี้อยู่แล้ว\",\"ไม่พบไฟล์\",\"ไฟล์และพรีเซ็ตที่บันทึกลงดิสก์ยังคงอยู่\",\"ไม่พบโฟลเดอร์\",\"สำหรับแชต ดาวน์โหลดส่วนขยาย แล้วเปิดไฟล์ ต้องมี uv\",\"ดาวน์โหลด uv\",\"กลับไปก่อนการคืนค่า\",\"ช่วงความสูง {min}–{max} mm\",\"ล้างช่วงความสูงแล้ว\",\"ค่าที่ซ่อนอยู่\",\"ในเทอร์มินัล ตัวแก้ไข หรือแท็บ Code ของแอป ต้องมี uv\",\"ในการตั้งค่าของส่วนขยาย ตั้งค่า \\\"OrcaSlicer settings folder\\\" เป็น {folder}\",\"ค่าไม่ถูกต้อง\",\"ตอนนี้มีวัตถุอยู่บนเพลตนี้\",\"ไม่อยู่บนเพลตแล้ว\",\"ไม่มีอยู่แล้ว\",\"พรีเซ็ตนี้ถูกเลือกอยู่ ให้สลับไปพรีเซ็ตอื่นก่อน\",\"เป็นเพลตสุดท้าย\",\"วัตถุของเพลตนี้ถูกย้ายไปข้างฐานพิมพ์\",\"LM Studio, Windsurf และแอปอื่นส่วนใหญ่ใช้รูปแบบนี้ ต้องมี uv\",\"เพลตสุดท้าย\",\"ความสูงเลเยอร์อยู่นอกช่วง\",\"รีเซ็ตความสูงเลเยอร์แล้ว\",\"บรรทัดที่ {line} จาก {count}:\",\"บรรทัดที่ {first} ถึง {last} จาก {count}:\",\"เมนูหลัก (ข้าง ไฟล์) > ปลั๊กอิน > OrcaSlicer MCP > การกำหนดค่า\",\"โมเดล\",\"ย้ายกลับ\",\"ย้ายวัตถุ {n}\",\"ย้ายวัตถุ {n} ไปเพลต {index}\",\"ย้าย {name}\",\"ย้าย {name} กลับไปที่ {path}\",\"ย้าย {name} กลับไปที่{plate}\",\"ย้าย {name} กลับที่เดิม\",\"ย้ายแล้ว\",\"มีการย้ายอีกครั้งหลังจากนั้น\",\"ย้ายไปเพลตแล้ว\",\"ชื่อ\",\"ต้องเปิดที่ การตั้งค่า › Remote API › Allow deleting and replacing project files\",\"ต้องเปิดที่ การตั้งค่า › Remote API › Allow script, G-code and connection edits\",\"ต้องเปิดที่ การตั้งค่า › Remote API › Allow script, G-code and connection edits · {object}\",\"ต้องใช้บิลด์ OrcaSlicer MCP\",\"ไม่เคยบันทึก\",\"โปรเจกต์ใหม่\",\"พรีเซ็ต {type} ใหม่\",\"ไม่\",\"ไม่มีถาด CANVAS\",\"ไม่มีการเปลี่ยนแปลง\",\"ไม่มีจุดคืนค่าที่นี่\",\"ไม่มีที่ว่างสำหรับเพลตเพิ่ม\",\"ไม่มีการเปลี่ยนแปลงนี้\",\"ไม่มีสำเนานี้\",\"ไม่มีเส้นพลาสติกนี้\",\"ไม่มีเพลตนี้\",\"ไม่มีพรีเซ็ตนี้\",\"ไม่มีจุดคืนค่านี้\",\"ไม่มีถาดนี้\",\"ไม่อนุญาต\",\"ไม่เข้ากัน\",\"แก้ไขที่นี่ไม่ได้\",\"ไม่อยู่ในรายการล่าสุด\",\"ไม่อยู่ในไทม์ไลน์ของโปรเจกต์นี้\",\"ไม่ได้ใส่คืน\",\"ยังไม่พร้อม\",\"ไม่ได้จัดเก็บ\",\"ทำไม่ได้ขณะที่ OrcaSlicer กำลังทำงานอยู่\",\"ทำไม่ได้ขณะที่ OrcaSlicer กำลังปิด\",\"ทำไม่ได้ขณะที่กำลังโหลดโปรเจกต์\",\"ทำไม่ได้ขณะสไลซ์\",\"ทำไม่ได้ขณะที่กำลังสร้างหน้าต่างใหม่\",\"หมายเหตุ\",\"ไม่มีอะไรถูกย้าย\",\"ไม่มีอะไรบนเพลต\",\"ไม่มีอะไรให้ย้อนกลับ\",\"ไม่มีอะไรจะแสดงในการตั้งค่า\",\"ไม่มีอะไรให้เลิกทำ\",\"ตกลง\",\"วัตถุไม่มีสำเนา\",\"ไม่พบวัตถุ\",\"การตั้งค่าวัตถุ {n}\",\"ปิด\",\"เปิด\",\"เปิดโปรเจกต์ {name}\",\"เปิด{settings}\",\"เปิดโปรเจกต์แล้ว\",\"กำลังเปิด…\",\"ปิดแผง OrcaSlicer MCP panel แล้ว เปิดกลับได้ที่ มุมมอง > OrcaSlicer MCP panel\",\"ปิดแผง OrcaSlicer MCP แล้ว แผงจะกลับมาเมื่อผู้ช่วยของคุณใช้ OrcaSlicer ครั้งถัดไป หรือกด Spacebar แล้วเลือก OrcaSlicer MCP\",\"OrcaSlicer รายงานข้อผิดพลาด\",\"แอปอื่น\",\"พรีเซ็ตอื่นสืบทอดจากพรีเซ็ตนี้\",\"ล้าสมัย: มีบางอย่างเปลี่ยนไปหลังการสไลซ์นี้\",\"อยู่นอกช่วง\",\"ซ้อนทับกับช่วงอื่น\",\"เพลต\",\"เพลตถูกล็อก\",\"เพลต {n}\",\"ตอนนี้เพลต {n} มีวัตถุอื่นอยู่\",\"พรีเซ็ต\",\"พรีเซ็ตกำลังถูกใช้งาน\",\"พรีเซ็ตการพิมพ์\",\"เครื่องพิมพ์\",\"พรีเซ็ตเครื่องพิมพ์\",\"โปรเจกต์เปิดอยู่\",\"ใส่คืน\",\"ใส่ช่วงความสูงของ {object} คืน\",\"ใส่โปรไฟล์ความสูงเลเยอร์เดิมของ {object} คืน\",\"ใส่วัตถุ {n} ชิ้นกลับที่เดิม\",\"ใส่ {name} กลับบน{plate}\",\"คุณภาพ {value}\",\"อ่านอย่างเดียว\",\"ปฏิเสธ\",\"ถูกปฏิเสธ\",\"การตั้งค่า Remote API\",\"ลบ\",\"ลบสำเนา\",\"ลบวัตถุ {n}\",\"ลบช่วง\",\"ลบสำเนาใหม่ของ {name}\",\"ลบการแทนที่ {label} ออกจาก {object}\",\"ลบช่วง {min}–{max} mm ออกจาก {object}\",\"ลบ {name} ออกจากเพลต\",\"ลบออก\",\"เปลี่ยนชื่อแล้ว\",\"แทนที่พรีเซ็ต {type} ที่บันทึกไว้แล้ว\",\"รีเซ็ตความสูงเลเยอร์\",\"โฟลเดอร์จุดคืนค่า\",\"จุดคืนค่าต้องใช้บิลด์ OrcaSlicer MCP\",\"คืนค่าโปรเจกต์ให้เป็นเหมือนตอน {time} (จะเลิกทำการเปลี่ยนแปลงหลังจากนั้น)\",\"คืนค่าไปยัง {time} หรือไม่\",\"เลิกทำการคืนค่าแล้ว\",\"คืนค่าไปยัง {time} แล้ว\",\"กำลังคืนค่า…\",\"ย้อนกลับ\",\"ย้อนกลับแล้ว\",\"ย้อนกลับเมื่อ {time}\",\"กำลังย้อนกลับ…\",\"หมุนกลับ\",\"หมุนวัตถุ {n}\",\"หมุน {name} กลับ\",\"หมุนแล้ว\",\"เรียกใช้คำสั่งนี้ครั้งเดียว แอปบนเดสก์ท็อปใช้การตั้งค่าร่วมกัน ต้องมี uv\",\"พาธเดียวกัน\",\"บันทึกโปรเจกต์\",\"บันทึกโปรเจกต์เป็น {name}\",\"บันทึกพรีเซ็ต {type}\",\"บันทึกพรีเซ็ตแล้ว\",\"บันทึกโปรเจกต์แล้ว\",\"ปรับขนาดกลับ\",\"ปรับขนาดวัตถุ {n}\",\"ปรับขนาด {name} กลับ\",\"ปรับขนาดแล้ว\",\"สคริปต์ G-code ที่กำหนดเอง และการตั้งค่าการเชื่อมต่อเครื่องพิมพ์ถูกล็อกอยู่ ติ๊ก 'Allow script, G-code and connection edits' ที่ การตั้งค่า > Remote API เพื่อให้ผู้ช่วยเปลี่ยนได้ หรือเปลี่ยนเองที่นี่\",\"ตั้งค่าในที่ที่คุณใช้แอปของคุณ\",\"ตั้งค่าในที่ที่คุณใช้ {app}\",\"ตั้งช่วง {min}–{max} mm ของ {object} กลับเป็น {before}\",\"ตั้ง {label} กลับเป็น {before}\",\"ตั้ง {label} ของ {object} กลับเป็น {before}\",\"การตั้งค่า\",\"การตั้งค่า\",\"การตั้งค่า › ปลั๊กอิน › เพิ่ม › เพิ่มเซิร์ฟเวอร์ MCP เลือก STDIO แล้วกรอกแต่ละช่องด้านล่าง ใช้ได้ในแชต Codex แชต ChatGPT ทั่วไปยังใช้ไม่ได้ ต้องมี uv\",\"แสดงรายละเอียด\",\"แสดงค่าเดิมและหาในการตั้งค่า\",\"แสดงค่าเดิมและเลิกทำ\",\"แสดงค่าเดิม เลิกทำ และหาในการตั้งค่า\",\"ผลการสไลซ์\",\"สไลซ์แล้ว\",\"สไลซ์{where}แล้ว\",\"ยกเลิกการสไลซ์แล้ว\",\"สไลซ์ล้มเหลว\",\"เกิดข้อผิดพลาดบางอย่าง\",\"เกิดข้อผิดพลาดบางอย่าง: {reason}\",\"สลับกลับ\",\"ยกเลิกการสลับแล้ว\",\"สลับ {label} กลับเป็น {before}\",\"สลับ {label} กลับเป็น {before} (จะทิ้งการแก้ไขที่ยังไม่ได้บันทึก)\",\"เส้นพลาสติกนั้นหายไปแล้ว\",\"Remote API เริ่มทำงานไม่ได้ ตรวจสอบพอร์ตของมัน\",\"Remote API ปิดอยู่\",\"สำเนานั้นหายไปแล้ว\",\"โฟลเดอร์เดิมหายไปแล้ว\",\"ตอนนี้ชื่อเดิมถูกใช้ไปแล้ว\",\"เพลตมีการเปลี่ยนแปลงหลังจากนั้น ให้คืนค่าไปยังจุดก่อนหน้าแทน\",\"ปลั๊กอินไม่เริ่มทำงาน\",\"ปลั๊กอินไม่เคยเปลี่ยนสคริปต์ G-code หรือการตั้งค่าการเชื่อมต่อเครื่องพิมพ์\",\"โปรเจกต์มีการเปลี่ยนแปลงหลังจากนั้น\",\"อ่านจุดคืนค่าไม่ได้\",\"จุดคืนค่าหายไปแล้ว\",\"จุดคืนค่าถูกเปลี่ยนบนดิสก์ จึงไม่ถูกนำมาใช้\",\"การตั้งค่านี้ไม่อยู่ในพรีเซ็ตนี้แล้ว\",\"แท็บการตั้งค่ายังไม่พร้อม\",\"ไม่มีแท็บการตั้งค่า\",\"สแนปช็อตก่อนการคืนค่าหายไปแล้ว\",\"สแนปช็อตก่อนการคืนค่าถูกเปลี่ยนบนดิสก์ จึงไม่ถูกนำมาใช้\",\"จากนั้นบอกชิ้นงานที่ต้องการพิมพ์ ทุกการเปลี่ยนแปลงที่ผู้ช่วยทำจะแสดงที่นี่ และคุณเลิกทำได้ทั้งหมด\",\"อาจยังคงเป็นค่าของตัวเลือกสุดท้าย ({reason})\",\"ถาดยังไม่พร้อม\",\"ใช้ถาดซ้ำสองครั้ง\",\"เลิกทำ\",\"ไม่รู้จักการตั้งค่านี้\",\"การเปลี่ยนแปลงที่ยังไม่ได้บันทึก\",\"ทิ้งการแก้ไขที่ยังไม่ได้บันทึกแล้ว: {list}\",\"ไม่รองรับไฟล์นี้\",\"ไม่มีชื่อ\",\"VS Code จะเปิดขึ้นและขอให้คุณยืนยัน ต้องมี uv\",\"กำลังรอแอปของคุณ…\",\"กำลังรอ {app}…\",\"เดิมเป็น {before}\",\"เมื่อผู้ช่วยเปลี่ยนอะไร จะแสดงที่นี่\",\"Wipe tower X\",\"Wipe tower Y\",\"จำนวนค่าไม่ถูกต้อง\",\"ใช่\",\"คุณเปลี่ยนไปหลังจากนั้น\",\"การตั้งค่า MCP ของแอป\",\"การเปลี่ยนแปลงของคุณเองหลังจากนั้นก็จะถูกเลิกทำด้วย\",\"ข้างฐานพิมพ์\",\"คุณเปลี่ยนเอง\",\"เปลี่ยนไปแล้วหลังจากนั้น\",\"ลบสำเนาไปแล้วหลังจากนั้น\",\"ลบไปแล้วหลังจากนั้น\",\"ถูกทิ้งเพราะสลับพรีเซ็ต\",\"ตอนนี้มีวัตถุ\",\"เลเยอร์ {height} mm\",\"บรรทัดที่ {line}: {text} ({count} บรรทัด)\",\"ย้ายไปแล้วหลังจากนั้น\",\"ไม่มี\",\"ไม่ใช่ตัวเลข\",\"ไม่ได้หมุน\",\"ไม่ได้ตั้งค่า\",\"ไม่ได้เก็บอะไรไว้\",\"ชื่อเดิมถูกใช้แล้ว\",\"ล้าสมัย\",\"เพลต {n}\",\"เพลต {n} · {error}\",\"ตั้งช่วงใหม่แล้วหลังจากนั้น\",\"ลบออกไปแล้วหลังจากนั้น\",\"ย้อนกลับแล้ว\",\"คุณตั้งกลับเอง\",\"การตั้งค่าอาจไม่กลับเป็นเหมือนเดิม\",\"เพลต\",\"แท็บการตั้งค่ายังไม่พร้อม\",\"ไปที่ {position}\",\"ถูกเลิกทำโดยการคืนค่า\",\"แอปของคุณ\",\"{angle}° รอบแกน {axis}\",\"{key}: ไม่ใช่การตั้งค่าการพิมพ์ เส้นพลาสติก หรือเครื่องพิมพ์\",\"{key}: ค่าที่ไม่รองรับ\",\"{label} (เส้นพลาสติก {n})\",\"{label} (เส้นพลาสติก {list})\",\"{label} ต้องมากกว่า {min} (ได้ {got})\",\"{label} ต้องมากกว่า {min} (ได้ {got}) ค่าต่ำสุดที่เครื่องพิมพ์อนุญาตตั้งไว้ที่ การตั้งค่าเครื่องพิมพ์ > การจำกัดความสูงของเลเยอร์\",\"{label} ต้องมากกว่า {min} และไม่เกิน {max} (ได้ {got})\",\"{label} ต้องมากกว่า {min} และไม่เกิน {max} (ได้ {got}) ค่าต่ำสุดที่เครื่องพิมพ์อนุญาตตั้งไว้ที่ การตั้งค่าเครื่องพิมพ์ > การจำกัดความสูงของเลเยอร์\",\"{label} ต้องไม่น้อยกว่า {min} (ได้ {got})\",\"{label} ต้องไม่น้อยกว่า {min} (ได้ {got}) ค่าต่ำสุดที่เครื่องพิมพ์อนุญาตตั้งไว้ที่ การตั้งค่าเครื่องพิมพ์ > การจำกัดความสูงของเลเยอร์\",\"{label} ต้องไม่เกิน {max} (ได้ {got})\",\"{label} ต้องไม่เกิน {max} (ได้ {got}) ค่าต่ำสุดที่เครื่องพิมพ์อนุญาตตั้งไว้ที่ การตั้งค่าเครื่องพิมพ์ > การจำกัดความสูงของเลเยอร์\",\"{label} ต้องอยู่ระหว่าง {min} ถึง {max} (ได้ {got})\",\"{label} ต้องอยู่ระหว่าง {min} ถึง {max} (ได้ {got}) ค่าต่ำสุดที่เครื่องพิมพ์อนุญาตตั้งไว้ที่ การตั้งค่าเครื่องพิมพ์ > การจำกัดความสูงของเลเยอร์\",\"ไม่อนุญาต {label}\",\"ตอนนี้ {name} ไม่เหมาะกับเครื่องพิมพ์นี้\",\"{name} หายไปแล้ว\",\"{name} หายไปแล้ว\",\"{name} ไม่อยู่บนเพลตแล้ว\",\"{name} ไม่อยู่ที่นั่นแล้ว\",\"{name} → {plate}\",\"{name} ตอนนี้มี {n} สำเนา\",\"{n} วัตถุ\",\"{n} วัตถุ\",\"{n} ช่วง\",\"{n} ช่วง\",\"{object} ไม่อยู่บนเพลตแล้ว\",\"เฉพาะ {object}\",\"เฉพาะ {object} · จากพรีเซ็ตการพิมพ์\",\"{preset} (ยังไม่ได้บันทึก)\",\"{text} ({count} บรรทัด)\",\"{title} และอีก {n} รายการ\",\"{value} ใน {preset}\",\"{verb} (ใช้ไม่ได้: {why})\",\"{verb} {what}\",\"{verb}: {label} กลับเป็น {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"tr\": [\"(boş satır)\",\"(gizli)\",\"Bir geri yükleme sürüyor\",\"Uyarlanabilir katman yüksekliği\",\"Model ekle\",\"Plaka ekle\",\"Cursor'a ekle\",\"VS Code'a ekle\",\"{name} ekle\",\"Eklendi\",\"Plaka eklendi\",\"Zaten {before} değerine döndü\",\"{setting} değerini ortam değişkeni olarak da ekleyin.\",\"Bir nesne\",\"Argümanlar\",\"Hizala\",\"Hizalandı\",\"İstediğiniz baskıyı ondan isteyin. Yaptığı değişiklikler burada görünür, her birinin yanında Geri döndür düğmesi olur.\",\"Asistan\",\"Asistan erişimi kapalı.\",\"Asistan {value} yaptı · önceki {before}\",\"Asistan {value} yaptı · siz eski haline getirdiniz\",\"Asistan ayarları\",\"Otomatik yönlendir\",\"Otomatik yönlendirildi\",\"Yerleşik ad\",\"Yerleşik ön ayar\",\"Meşgul\",\"Dilimleme sürüyor\",\"Asistanın isteğiyle meşgul; birazdan tekrar deneyin\",\"Geri değiştirilemiyor\",\"Şu an denetlenemiyor: {reason}\",\"Bağlanamaz: yalnızca internetteki sunuculara ulaşır, bilgisayarınızdaki programlara ulaşamaz.\",\"Silinemiyor\",\"Yeniden başlatmadan sonra geri konamaz; bunun yerine daha önceki bir noktayı geri yükleyin\",\"Aralıklar geri konamıyor\",\"Geri alınamıyor\",\"Geri alınamıyor: {reason}\",\"Eskisine geçilemiyor\",\"Proje yeniden yüklendikten sonra bu nesne izlenemiyor\",\"İptal edildi\",\"Geri değiştir\",\"{label} değerini {before} olarak geri değiştir\",\"O zamandan beri yeniden değiştirildi\",\"O zamandan beri yeniden değiştirildi; bunun yerine daha önceki bir noktayı geri yükleyin\",\"ChatGPT masaüstü uygulaması\",\"Web'de veya telefonda ChatGPT\",\"Claude uygulaması\",\"Yükseklik aralıklarını temizle\",\"Terminalde veya düzenleyicide Codex\",\"Tümünü daralt\",\"Başlatma komutu\",\"{n} seçenek karşılaştırıldı\",\"{n} seçenek karşılaştırıldı · hiçbiri saklanmadı\",\"{n} seçenek karşılaştırıldı · ayarlar eskisi gibi geri gelmemiş olabilir\",\"{n} seçenek karşılaştırıldı\",\"{n} seçenek karşılaştırıldı · hiçbiri saklanmadı\",\"{n} seçenek karşılaştırıldı · ayarlar eskisi gibi geri gelmemiş olabilir\",\"Karşılaştırma şunları geri koyamadı: {labels}\",\"Yapay zeka uygulamanızı bağlayın\",\"Bağlandı\",\"{app} uygulamasına bağlandı\",\"Kopyalandı\",\"Kopyala\",\"Bunun yerine .vscode/mcp.json ayarlarını kopyala\",\"Komutu kopyala\",\"Ayarları kopyala\",\"Bunun yerine ayarları kopyala\",\"Geri yüklenemedi\",\"Geri alınamadı\",\"Hesaplanamadı\",\"Silinemedi\",\"Silinemedi\",\"Taşınamadı\",\"Geri taşınamadı\",\"Açılamadı\",\"Geri yükleme noktası açılamadı\",\"{time} durumuna geri yüklenemedi\",\"Kaydedilemedi\",\"Önce mevcut durum kaydedilemedi\",\"Seçilemedi\",\"Geri alınamadı\",\"Cursor açılır ve onay ister. uv gerekir.\",\"{n}. plakayı sil (yalnızca boşken)\",\"Plakayı sil\",\"{n}. plakayı sil\",\"Ön ayarı sil\",\"{type} ön ayarı {name} öğesini sil\",\"{name} öğesini sil\",\"{type} ön ayarını sil\",\"Dosya silindi\",\"Plaka silindi\",\"Ön ayar silindi\",\"İndir\",\"Ön ayar değişikliğiyle bırakıldı\",\"Ön ayar değişikliğiyle bırakıldı; bunun yerine daha önceki bir noktayı geri yükleyin\",\"{n}. nesneyi çoğalt\",\"Çoğaltıldı\",\"Her uygulamanın uv'ye ihtiyacı vardır: bağlayıcıyı çalıştıran küçük, ücretsiz bir araç. Bir kez kurmanız yeterli.\",\"Tümünü genişlet\",\"Başarısız\",\"Filament\",\"Filament ön ayarı\",\"Filament tepsileri\",\"Filament {n}\",\"Filament {n} rengi\",\"Filament {n} tepsisi\",\"Dosya\",\"Dosya zaten var\",\"Dosya bulunamadı\",\"Diske kaydedilen dosyalar ve ön ayarlar kalır.\",\"Klasör bulunamadı\",\"Sohbetler için. Uzantıyı indirin, ardından dosyayı açın. uv gerekir.\",\"uv'yi indirin\",\"Geri yüklemeden önceki duruma dön\",\"Yükseklik aralığı {min}–{max} mm\",\"Yükseklik aralıkları temizlendi\",\"Gizli değer\",\"Terminalde, düzenleyicide veya uygulamanın Code sekmesinde. uv gerekir.\",\"Uzantının ayarlarında \\\"OrcaSlicer settings folder\\\" alanını {folder} olarak ayarlayın.\",\"Geçersiz değer\",\"Üzerinde artık nesneler var\",\"Artık plakada değil\",\"Artık orada değil\",\"Seçili; önce başka bir ön ayara geçin\",\"Bu son plaka\",\"Nesneleri yatağın yanına taşındı\",\"LM Studio, Windsurf ve diğerlerinin çoğu bu biçimi kullanır. uv gerekir.\",\"Son plaka\",\"Katman yüksekliği aralık dışında\",\"Katman yüksekliği sıfırlandı\",\"{count} satırın {line}. satırı:\",\"{count} satırın {first}–{last} arası satırları:\",\"Ana menü (Dosya yanında) > Eklentiler > OrcaSlicer MCP > Yapılandırma\",\"Model\",\"Geri taşı\",\"{n}. nesneyi taşı\",\"{n}. nesneyi {index}. plakaya taşı\",\"{name} öğesini taşı\",\"{name} öğesini {path} konumuna geri taşı\",\"{name} öğesini {plate} üzerine geri taşı\",\"{name} öğesini eski yerine geri taşı\",\"Taşındı\",\"O zamandan beri yeniden taşındı\",\"Plakaya taşındı\",\"Ad\",\"Gerekli: Tercihler › Remote API › Allow deleting and replacing project files\",\"Gerekli: Tercihler › Remote API › Allow script, G-code and connection edits\",\"Gerekli: Tercihler › Remote API › Allow script, G-code and connection edits · {object}\",\"OrcaSlicer MCP sürümü gerekir\",\"Hiç kaydedilmedi\",\"Yeni proje\",\"Yeni {type} ön ayarı\",\"Hayır\",\"CANVAS tepsisi yok\",\"Değişiklik yok\",\"Burada geri yükleme noktası yok\",\"Bir plaka daha için yer yok\",\"Böyle bir değişiklik yok\",\"Böyle bir kopya yok\",\"Böyle bir filament yok\",\"Böyle bir plaka yok\",\"Böyle bir ön ayar yok\",\"Böyle bir geri yükleme noktası yok\",\"Böyle bir tepsi yok\",\"İzin verilmiyor\",\"Uyumlu değil\",\"Burada düzenlenemez\",\"Son kullanılanlar listesinde yok\",\"Bu projenin zaman çizelgesinde yok\",\"Geri konmadı\",\"Hazır değil\",\"Saklanmadı\",\"OrcaSlicer bir işle meşgulken olmaz\",\"OrcaSlicer kapanırken olmaz\",\"Bir proje yüklenirken olmaz\",\"Dilimleme sırasında olmaz\",\"Pencere yeniden oluşturulurken olmaz\",\"Not\",\"Hiçbir şey taşınmadı\",\"Plakada hiçbir şey yok\",\"Geri döndürülecek bir şey yok\",\"Ayarlarda gösterilecek bir şey yok\",\"Geri alınacak bir şey yok\",\"TAMAM\",\"Nesnenin kopyası yok\",\"Nesne bulunamadı\",\"Nesne ayarları {n}\",\"Kapalı\",\"Açık\",\"{name} projesini aç\",\"{settings} bölümünü aç\",\"Proje açıldı\",\"Açılıyor…\",\"OrcaSlicer MCP paneli kapatıldı. Görünüm > OrcaSlicer MCP panel menüsünden geri getirebilirsiniz.\",\"OrcaSlicer MCP paneli kapatıldı. Asistanınız OrcaSlicer'ı bir sonraki kullanışında geri gelir ya da Boşluk tuşuna basıp OrcaSlicer MCP'yi seçin.\",\"OrcaSlicer bir hata bildirdi\",\"Diğer uygulama\",\"Diğer ön ayarlar bunu devralır\",\"Güncel değil: bu dilimlemeden sonra bir şey değişti\",\"Aralık dışında\",\"Bir aralıkla çakışıyor\",\"Plaka\",\"Plaka kilitli\",\"Plaka {n}\",\"Plaka {n} üzerinde artık başka nesneler var\",\"Ön ayar\",\"Ön ayar kullanımda\",\"Baskı ön ayarı\",\"Yazıcı\",\"Yazıcı ön ayarı\",\"Proje açık\",\"Geri koy\",\"{object} yükseklik aralıklarını geri koy\",\"{object} önceki katman yüksekliği profilini geri koy\",\"{n} nesneyi eski yerlerine geri koy\",\"{name} öğesini {plate} üzerine geri koy\",\"Kalite {value}\",\"Salt okunur\",\"Reddedildi\",\"Reddedildi\",\"Remote API ayarları\",\"Kaldır\",\"Kopyayı kaldır\",\"{n}. nesneyi kaldır\",\"Aralığı kaldır\",\"{name} öğesinin yeni kopyasını kaldır\",\"{object} üzerinden {label} geçersiz kılmasını kaldır\",\"{object} üzerinden {min}–{max} mm aralığını kaldır\",\"{name} öğesini plakadan kaldır\",\"Kaldırıldı\",\"Yeniden adlandırıldı\",\"Kayıtlı {type} ön ayarı değiştirildi\",\"Katman yüksekliğini sıfırla\",\"Geri yükleme noktası klasörü\",\"Geri yükleme noktaları için OrcaSlicer MCP sürümü gerekir\",\"Projeyi {time} anındaki haline geri yükle (sonraki değişiklikleri geri alır)\",\"{time} durumuna geri yüklensin mi?\",\"Geri yükleme geri alındı\",\"{time} durumuna geri yüklendi\",\"Geri yükleniyor…\",\"Geri döndür\",\"Geri döndürüldü\",\"{time} saatinde geri döndürüldü\",\"Geri döndürülüyor…\",\"Geri çevir\",\"{n}. nesneyi döndür\",\"{name} öğesini geri çevir\",\"Döndürüldü\",\"Bunu bir kez çalıştırın. Masaüstü uygulaması ayarı paylaşır. uv gerekir.\",\"Aynı yol\",\"Projeyi kaydet\",\"Projeyi {name} olarak kaydet\",\"{type} ön ayarını kaydet\",\"Ön ayar kaydedildi\",\"Proje kaydedildi\",\"Ölçeği geri döndür\",\"{n}. nesneyi ölçeklendir\",\"{name} ölçeğini geri döndür\",\"Ölçeklendi\",\"Betikler, özel G-code ve yazıcı bağlantı ayarları kilitli. Asistanın bunları değiştirebilmesi için Tercihler > Remote API altında 'Allow script, G-code and connection edits' seçeneğini işaretleyin ya da burada kendiniz değiştirin.\",\"Uygulamanızı kullandığınız yerde kurun\",\"{app} uygulamasını kullandığınız yerde kurun\",\"{object} üzerindeki {min}–{max} mm aralığını {before} değerine geri al\",\"{label} değerini {before} olarak geri al\",\"{object} üzerindeki {label} değerini {before} olarak geri al\",\"Ayar\",\"Ayarlar\",\"Ayarlar › Eklentiler › Ekle › MCP sunucusu ekle yolunu izleyin, STDIO'yu seçin ve aşağıdaki her alanı doldurun. Codex sohbetlerinde çalışır; normal ChatGPT sohbetleri henüz kullanamaz. uv gerekir.\",\"Ayrıntıları göster\",\"Eski halini göster ve ayarlarda bul\",\"Eski halini göster ve geri al\",\"Eski halini göster, geri al ve ayarlarda bul\",\"Dilimleme sonucu\",\"Dilimlendi\",\"Dilimlendi: {where}\",\"Dilimleme iptal edildi\",\"Dilimleme başarısız oldu\",\"Bir şeyler ters gitti\",\"Bir şeyler ters gitti: {reason}\",\"Eskisine geç\",\"Geçiş iptal edildi\",\"{label} ayarını {before} değerine geri geçir\",\"{label} ayarını {before} değerine geri geçir (kaydedilmemiş düzenlemeler atılır)\",\"O filament artık yok\",\"Remote API başlatılamadı. Portunu kontrol edin.\",\"Remote API kapalı.\",\"Kopya zaten yok\",\"Eski klasör artık yok\",\"Eski ad artık kullanımda\",\"Plaka o zamandan beri değişti; bunun yerine daha önceki bir noktayı geri yükleyin\",\"Eklenti başlamadı\",\"Eklenti betikleri, G-code'u veya yazıcı bağlantı ayarlarını asla değiştirmez\",\"Proje o zamandan beri değişti\",\"Geri yükleme noktası okunamıyor\",\"Geri yükleme noktası artık yok\",\"Geri yükleme noktası diskte değiştirilmiş, bu yüzden kullanılmıyor\",\"Ayar artık bu ön ayarda yok\",\"Ayarlar sekmesi hazır değil\",\"Ayarlar sekmesi yok\",\"Geri yüklemeden önceki anlık görüntü artık yok\",\"Geri yüklemeden önceki anlık görüntü diskte değiştirilmiş, bu yüzden kullanılmıyor\",\"Sonra ondan istediğiniz baskıyı isteyin. Yaptığı her değişiklik burada görünür ve herhangi birini geri alabilirsiniz.\",\"Hâlâ son seçeneğin değerlerini taşıyor olabilirler ({reason}).\",\"Tepsi hazır değil\",\"Tepsi iki kez kullanıldı\",\"Geri al\",\"Bilinmeyen ayar\",\"Kaydedilmemiş değişiklikler\",\"Kaydedilmemiş düzenlemeler atıldı: {list}\",\"Desteklenmeyen dosya\",\"Adsız\",\"VS Code açılır ve onay ister. uv gerekir.\",\"Uygulamanız bekleniyor…\",\"{app} bekleniyor…\",\"Önceki: {before}\",\"Bir asistan bir şeyi değiştirdiğinde burada görünür.\",\"Silme kulesi X\",\"Silme kulesi Y\",\"Yanlış sayıda değer\",\"Evet\",\"O zamandan beri siz değiştirdiniz\",\"Uygulamanızın MCP ayarları\",\"Sizin sonradan yaptığınız değişiklikler de geri alınır.\",\"yatağın yanında\",\"sizin tarafınızdan değiştirildi\",\"o zamandan beri değiştirildi\",\"kopya o zamandan beri kaldırıldı\",\"o zamandan beri silindi\",\"ön ayar değişikliğiyle bırakıldı\",\"artık nesneler var\",\"katman {height} mm\",\"satır {line}: {text} ({count} satır)\",\"o zamandan beri taşındı\",\"yok\",\"sayı değil\",\"döndürülmedi\",\"ayarlanmadı\",\"hiçbiri saklanmadı\",\"eski ad kullanımda\",\"güncel değil\",\"plaka {n}\",\"plaka {n} · {error}\",\"aralıklar o zamandan beri ayarlandı\",\"o zamandan beri kaldırıldı\",\"geri döndürüldü\",\"sizin tarafınızdan geri alındı\",\"ayarlar eskisi gibi geri gelmemiş olabilir\",\"plaka\",\"ayarlar sekmesi hazır değil\",\"{position} konumuna\",\"geri yükleme ile geri alındı\",\"uygulamanız\",\"{axis} ekseni etrafında {angle}°\",\"{key}: baskı, filament veya yazıcı ayarı değil\",\"{key}: desteklenmeyen değer\",\"{label} (filament {n})\",\"{label} (filamentler {list})\",\"{label} {min} değerinden büyük olmalı (alınan: {got})\",\"{label} {min} değerinden büyük olmalı (alınan: {got}); yazıcının izin verdiği en düşük değer Yazıcı Ayarları > Katman Yüksekliği Sınırları bölümünde belirlenir\",\"{label} {min} değerinden büyük ve en fazla {max} olmalı (alınan: {got})\",\"{label} {min} değerinden büyük ve en fazla {max} olmalı (alınan: {got}); yazıcının izin verdiği en düşük değer Yazıcı Ayarları > Katman Yüksekliği Sınırları bölümünde belirlenir\",\"{label} en az {min} olmalı (alınan: {got})\",\"{label} en az {min} olmalı (alınan: {got}); yazıcının izin verdiği en düşük değer Yazıcı Ayarları > Katman Yüksekliği Sınırları bölümünde belirlenir\",\"{label} en fazla {max} olmalı (alınan: {got})\",\"{label} en fazla {max} olmalı (alınan: {got}); yazıcının izin verdiği en düşük değer Yazıcı Ayarları > Katman Yüksekliği Sınırları bölümünde belirlenir\",\"{label} {min} ile {max} arasında olmalı (alınan: {got})\",\"{label} {min} ile {max} arasında olmalı (alınan: {got}); yazıcının izin verdiği en düşük değer Yazıcı Ayarları > Katman Yüksekliği Sınırları bölümünde belirlenir\",\"{label} izin verilmiyor\",\"{name} artık bu yazıcıya uymuyor\",\"{name} yok oldu\",\"{name} zaten yok oldu\",\"{name} artık plakada değil\",\"{name} artık orada değil\",\"{name} → {plate}\",\"{name}, şimdi {n} kopya\",\"{n} nesne\",\"{n} nesne\",\"{n} aralık\",\"{n} aralık\",\"{object} artık plakada değil\",\"Yalnızca {object}\",\"Yalnızca {object} · baskı ön ayarından\",\"{preset} (kaydedilmemiş)\",\"{text} ({count} satır)\",\"{title} ve {n} tane daha\",\"{preset} içinde {value}\",\"{verb} (kullanılamıyor: {why})\",\"{verb} {what}\",\"{verb}: {label} değerini {before} olarak geri al\",\"{x} × {y} × {z} mm\"],\n"
    "\"uk\": [\"(порожній рядок)\",\"(приховано)\",\"Триває відновлення\",\"Адаптивна висота шару\",\"Додати модель\",\"Додати пластину\",\"Додати до Cursor\",\"Додати до VS Code\",\"Додати {name}\",\"Додано\",\"Пластину додано\",\"Уже повернуто до {before}\",\"Також додайте {setting} як змінну середовища.\",\"Обʼєкт\",\"Аргументи\",\"Впорядкувати\",\"Впорядковано\",\"Розкажіть, який друк вам потрібен. Його зміни зʼявлятимуться тут, кожна з кнопкою «Повернути».\",\"Асистент\",\"Доступ асистента вимкнено.\",\"Асистент встановив {value} · було {before}\",\"Асистент встановив {value} · ви повернули\",\"Налаштування асистента\",\"Автоорієнтація\",\"Орієнтацію підібрано автоматично\",\"Вбудована назва\",\"Вбудований пресет\",\"Зайнятий\",\"Триває нарізка\",\"Зайнятий запитом асистента; спробуйте за мить\",\"Не вдалося змінити назад\",\"Зараз не вдається перевірити: {reason}\",\"Підключитися не можна: він доступний лише серверам в інтернеті, але не програмам на вашому комп’ютері.\",\"Не вдалося видалити\",\"Після перезапуску повернути не вдасться; натомість відновіть раніший пункт\",\"Не вдалося повернути діапазони\",\"Не вдалося повернути\",\"Не вдалося повернути: {reason}\",\"Не вдалося перемкнути назад\",\"Після перезавантаження проєкту цей обʼєкт відстежити неможливо\",\"Скасовано\",\"Змінити назад\",\"Змінити {label} назад на {before}\",\"З того часу змінено знову\",\"З того часу змінено знову; натомість відновіть раніший пункт\",\"Десктопний застосунок ChatGPT\",\"ChatGPT у браузері або на телефоні\",\"Застосунок Claude\",\"Очистити діапазони висоти\",\"Codex у терміналі або редакторі\",\"Згорнути все\",\"Команда для виконання\",\"Порівняно варіантів: {n}\",\"Порівняно варіантів: {n} · нічого не збережено\",\"Порівняно варіантів: {n} · налаштування могли не повернутися до попередніх\",\"Порівняно варіантів: {n}\",\"Порівняно варіантів: {n} · нічого не збережено\",\"Порівняно варіантів: {n} · налаштування могли не повернутися до попередніх\",\"Порівняння не змогло повернути: {labels}\",\"Підключіть свій застосунок зі ШІ\",\"Підключено\",\"Підключено до {app}\",\"Скопійовано\",\"Копіювати\",\"Копіювати налаштування .vscode/mcp.json\",\"Копіювати команду\",\"Копіювати налаштування\",\"Копіювати налаштування\",\"Не вдалося відновити\",\"Не вдалося повернути\",\"Не вдалося обчислити\",\"Не вдалося видалити\",\"Не вдалося видалити\",\"Не вдалося перемістити\",\"Не вдалося перемістити назад\",\"Не вдалося відкрити\",\"Не вдалося відкрити точку відновлення\",\"Не вдалося відновити стан на {time}\",\"Не вдалося зберегти\",\"Не вдалося спершу зберегти поточний стан\",\"Не вдалося вибрати\",\"Не вдалося скасувати\",\"Cursor відкриється і попросить підтвердити. Потрібен uv.\",\"Видалити пластину {n} (лише поки вона порожня)\",\"Видалити пластину\",\"Видалити пластину {n}\",\"Видалити пресет\",\"Видалити пресет {type} {name}\",\"Видалити {name}\",\"Видалити пресет {type}\",\"Файл видалено\",\"Пластину видалено\",\"Пресет видалено\",\"Завантажити\",\"Скинуто через перемикання пресета\",\"Скинуто через перемикання пресета; натомість відновіть раніший пункт\",\"Дублювати обʼєкт {n}\",\"Продубльовано\",\"Кожному застосунку потрібен uv — невеликий безкоштовний інструмент, який запускає конектор. Достатньо встановити його один раз.\",\"Розгорнути все\",\"Помилка\",\"Філамент\",\"Пресет філамента\",\"Лотки філаментів\",\"Філамент {n}\",\"Колір філамента {n}\",\"Лоток філамента {n}\",\"Файл\",\"Файл уже існує\",\"Файл не знайдено\",\"Файли й пресети, збережені на диск, залишаються.\",\"Теку не знайдено\",\"Для чатів. Завантажте розширення, потім відкрийте файл. Потрібен uv.\",\"Завантажити uv\",\"Повернутися до стану до відновлення\",\"Діапазон висоти {min}–{max} mm\",\"Діапазони висоти очищено\",\"Приховане значення\",\"У терміналі, редакторі або на вкладці Code застосунку. Потрібен uv.\",\"У налаштуваннях розширення вкажіть для «OrcaSlicer settings folder» значення {folder}.\",\"Недійсне значення\",\"Тепер на ній є обʼєкти\",\"Його більше немає на пластині\",\"Цього вже немає\",\"Його вибрано; спершу перемкніться на інший пресет\",\"Це остання пластина\",\"Її обʼєкти переміщено поруч зі столом\",\"LM Studio, Windsurf і більшість інших використовують цю форму. Потрібен uv.\",\"Остання пластина\",\"Висота шару поза межами\",\"Висоту шару скинуто\",\"Рядок {line} із {count}:\",\"Рядки з {first} по {last} із {count}:\",\"Головне меню (поруч із Файл) > Плагіни > OrcaSlicer MCP > Конфігурація\",\"Модель\",\"Перемістити назад\",\"Перемістити обʼєкт {n}\",\"Перемістити обʼєкт {n} на Пластину {index}\",\"Перемістити {name}\",\"Перемістити {name} назад до {path}\",\"Перемістити {name} назад на {plate}\",\"Повернути {name} на попереднє місце\",\"Переміщено\",\"З того часу переміщено знову\",\"Переміщено на пластину\",\"Ім’я\",\"Потрібно: Налаштування › Remote API › Allow deleting and replacing project files\",\"Потрібно: Налаштування › Remote API › Allow script, G-code and connection edits\",\"Потрібно: Налаштування › Remote API › Allow script, G-code and connection edits · {object}\",\"Потрібна збірка OrcaSlicer MCP\",\"Ніколи не зберігалося\",\"Новий проєкт\",\"Новий пресет {type}\",\"Ні\",\"Немає лотків CANVAS\",\"Немає змін\",\"Тут немає точки відновлення\",\"Немає місця для ще однієї пластини\",\"Такої зміни немає\",\"Такої копії немає\",\"Такого філамента немає\",\"Такої пластини немає\",\"Такого пресета немає\",\"Такої точки відновлення немає\",\"Такого лотка немає\",\"Не дозволено\",\"Несумісно\",\"Тут не редагується\",\"Немає в списку нещодавніх\",\"Немає в історії цього проєкту\",\"Не повернуто\",\"Не готово\",\"Не збережено\",\"Недоступно, поки OrcaSlicer зайнятий завданням\",\"Недоступно, поки OrcaSlicer закривається\",\"Недоступно, поки завантажується проєкт\",\"Недоступно під час нарізки\",\"Недоступно, поки вікно перебудовується\",\"Нотатка\",\"Нічого не переміщено\",\"На пластині нічого немає\",\"Нічого повертати\",\"У налаштуваннях нічого показувати\",\"Нічого скасовувати\",\"ОК\",\"Обʼєкт не має копії\",\"Обʼєкт не знайдено\",\"Налаштування обʼєкта {n}\",\"Вимк.\",\"Увімк.\",\"Відкрити проєкт {name}\",\"Відкрити: {settings}\",\"Проєкт відкрито\",\"Відкривається…\",\"Панель OrcaSlicer MCP закрито. Повернути її можна через Вигляд > OrcaSlicer MCP panel.\",\"Панель OrcaSlicer MCP закрито. Вона повернеться, коли ваш асистент наступного разу використає OrcaSlicer, або натисніть Пробіл і виберіть OrcaSlicer MCP.\",\"OrcaSlicer повідомив про помилку\",\"Інший застосунок\",\"Інші пресети успадковують його\",\"Застаріло: після цієї нарізки щось змінилося\",\"Поза діапазоном\",\"Перетинається з діапазоном\",\"Пластина\",\"Пластину заблоковано\",\"Пластина {n}\",\"На пластині {n} тепер інші обʼєкти\",\"Пресет\",\"Пресет використовується\",\"Пресет друку\",\"Принтер\",\"Пресет принтера\",\"Проєкт відкрито\",\"Повернути\",\"Повернути діапазони висоти для {object}\",\"Повернути попередній профіль висоти шару для {object}\",\"Повернути {n} обʼєктів на попередні місця\",\"Повернути {name} на {plate}\",\"Якість {value}\",\"Лише читання\",\"Відхилено\",\"Відхилено\",\"Налаштування Remote API\",\"Вилучити\",\"Вилучити копію\",\"Вилучити обʼєкт {n}\",\"Вилучити діапазон\",\"Вилучити нову копію {name}\",\"Вилучити перевизначення {label} для {object}\",\"Вилучити діапазон {min}–{max} mm для {object}\",\"Прибрати {name} з пластини\",\"Вилучено\",\"Перейменовано\",\"Замінено збережений пресет {type}\",\"Скинути висоту шару\",\"Тека точки відновлення\",\"Для точок відновлення потрібна збірка OrcaSlicer MCP\",\"Відновити проєкт до стану на {time} (скасовує пізніші зміни)\",\"Відновити стан на {time}?\",\"Відновлення скасовано\",\"Відновлено стан на {time}\",\"Відновлення…\",\"Повернути\",\"Повернуто\",\"Повернуто о {time}\",\"Повернення…\",\"Повернути назад\",\"Повернути обʼєкт {n}\",\"Повернути {name} назад\",\"Повернуто\",\"Виконайте це один раз. Десктопний застосунок використовує ті самі налаштування. Потрібен uv.\",\"Той самий шлях\",\"Зберегти проєкт\",\"Зберегти проєкт як {name}\",\"Зберегти пресет {type}\",\"Пресет збережено\",\"Проєкт збережено\",\"Повернути масштаб\",\"Змінити масштаб обʼєкта {n}\",\"Повернути масштаб {name}\",\"Масштаб змінено\",\"Скрипти, власний G-code і налаштування підключення принтера заблоковано. Позначте «Allow script, G-code and connection edits» у Налаштування > Remote API, щоб асистент міг їх змінювати, або змініть їх самі тут.\",\"Налаштуйте там, де ви користуєтеся вашим застосунком\",\"Налаштуйте там, де ви користуєтеся {app}\",\"Повернути діапазон {min}–{max} mm для {object} до значення {before}\",\"Повернути {label} до значення {before}\",\"Повернути {label} для {object} до значення {before}\",\"Параметр\",\"Налаштування\",\"Налаштування › Плагіни › Додати › Додати сервер MCP, виберіть STDIO і заповніть кожне поле нижче. Працює в чатах Codex; звичайні чати ChatGPT поки не можуть цього використовувати. Потрібен uv.\",\"Показати подробиці\",\"Показати, що було, і знайти в налаштуваннях\",\"Показати, що було, і скасувати\",\"Показати, що було, скасувати і знайти в налаштуваннях\",\"Результат нарізки\",\"Нарізано\",\"Нарізано: {where}\",\"Нарізку скасовано\",\"Нарізка не вдалася\",\"Щось пішло не так\",\"Щось пішло не так: {reason}\",\"Перемкнути назад\",\"Перемикання скасовано\",\"Перемкнути {label} назад на {before}\",\"Перемкнути {label} назад на {before} (незбережені зміни буде втрачено)\",\"Цього філамента більше немає\",\"Remote API не вдалося запустити. Перевірте його порт.\",\"Remote API вимкнено.\",\"Копії вже немає\",\"Старої теки більше немає\",\"Стара назва тепер зайнята\",\"З того часу пластина змінилася; натомість відновіть раніший пункт\",\"Плагін не запустився\",\"Плагін ніколи не змінює скрипти, G-code і налаштування підключення до принтера\",\"З того часу проєкт змінився\",\"Точку відновлення не вдається прочитати\",\"Точки відновлення більше немає\",\"Точку відновлення змінено на диску, тому її не використано\",\"Цього параметра більше немає в цьому пресеті\",\"Вкладка налаштувань не готова\",\"Вкладки налаштувань немає\",\"Знімка до відновлення більше немає\",\"Знімок до відновлення змінено на диску, тому його не використано\",\"Потім розкажіть, який друк вам потрібен. Кожна його зміна зʼявляється тут, і будь-яку можна скасувати.\",\"У них можуть залишитися значення останнього варіанта ({reason}).\",\"Лоток не готовий\",\"Лоток використано двічі\",\"Скасувати\",\"Невідомий параметр\",\"Незбережені зміни\",\"Незбережені правки відкинуто: {list}\",\"Непідтримуваний файл\",\"Без назви\",\"VS Code відкриється і попросить підтвердити. Потрібен uv.\",\"Очікування вашого застосунку…\",\"Очікування {app}…\",\"Було {before}\",\"Коли асистент щось змінює, це зʼявляється тут.\",\"Вежа протирання X\",\"Вежа протирання Y\",\"Неправильна кількість значень\",\"Так\",\"З того часу ви це змінили\",\"Налаштування MCP вашого застосунку\",\"Ваші власні пізніші зміни теж буде скасовано.\",\"поруч зі столом\",\"змінено вами\",\"змінено з того часу\",\"копію вилучено з того часу\",\"видалено з того часу\",\"скинуто через перемикання пресета\",\"тепер є обʼєкти\",\"шар {height} mm\",\"рядок {line}: {text} (рядків: {count})\",\"переміщено з того часу\",\"немає\",\"не число\",\"не повернуто\",\"не задано\",\"нічого не збережено\",\"стара назва зайнята\",\"застаріло\",\"пластина {n}\",\"пластина {n} · {error}\",\"діапазони задано з того часу\",\"вилучено з того часу\",\"повернуто\",\"повернуто вами\",\"налаштування могли не повернутися до попередніх\",\"пластина\",\"вкладка налаштувань не готова\",\"у {position}\",\"скасовано відновленням\",\"ваш застосунок\",\"{angle}° навколо {axis}\",\"{key}: це не параметр друку, філамента чи принтера\",\"{key}: непідтримуване значення\",\"{label} (філамент {n})\",\"{label} (філаменти {list})\",\"{label} має бути більшим за {min} (отримано {got})\",\"{label} має бути більшим за {min} (отримано {got}); найнижче значення, яке дозволяє принтер, задається в Налаштування принтера > Обмеження висоти шару\",\"{label} має бути більшим за {min} і не більшим за {max} (отримано {got})\",\"{label} має бути більшим за {min} і не більшим за {max} (отримано {got}); найнижче значення, яке дозволяє принтер, задається в Налаштування принтера > Обмеження висоти шару\",\"{label} має бути не меншим за {min} (отримано {got})\",\"{label} має бути не меншим за {min} (отримано {got}); найнижче значення, яке дозволяє принтер, задається в Налаштування принтера > Обмеження висоти шару\",\"{label} має бути не більшим за {max} (отримано {got})\",\"{label} має бути не більшим за {max} (отримано {got}); найнижче значення, яке дозволяє принтер, задається в Налаштування принтера > Обмеження висоти шару\",\"{label} має бути від {min} до {max} (отримано {got})\",\"{label} має бути від {min} до {max} (отримано {got}); найнижче значення, яке дозволяє принтер, задається в Налаштування принтера > Обмеження висоти шару\",\"{label}: не дозволено\",\"{name} зараз не підходить цьому принтеру\",\"{name} зник\",\"{name} уже зник\",\"{name} більше не на пластині\",\"{name} більше немає\",\"{name} → {plate}\",\"{name}, тепер копій: {n}\",\"{n} обʼєкт\",\"обʼєктів: {n}\",\"{n} діапазон\",\"діапазонів: {n}\",\"{object} більше не на пластині\",\"Лише {object}\",\"Лише {object} · із пресета друку\",\"{preset} (не збережено)\",\"{text} (рядків: {count})\",\"{title} і ще {n}\",\"{value} у {preset}\",\"{verb} (недоступно: {why})\",\"{verb} {what}\",\"{verb}: {label} назад на {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"vi\": [\"(dòng trống)\",\"(ẩn)\",\"Đang khôi phục\",\"Chiều cao lớp thích ứng\",\"Thêm mô hình\",\"Thêm Plate\",\"Thêm vào Cursor\",\"Thêm vào VS Code\",\"Thêm {name}\",\"Đã thêm\",\"Đã thêm Plate\",\"Đã trở lại {before}\",\"Hãy thêm cả {setting} làm biến môi trường.\",\"Một vật thể\",\"Đối số\",\"Sắp xếp\",\"Đã sắp xếp\",\"Hãy nói bản in bạn muốn. Các thay đổi của trợ lý sẽ hiện ở đây, mỗi thay đổi kèm Hoàn nguyên.\",\"Trợ lý\",\"Quyền truy cập của trợ lý đang tắt.\",\"Trợ lý đặt {value} · trước đó {before}\",\"Trợ lý đặt {value} · bạn đã đặt lại\",\"Cài đặt trợ lý\",\"Tự động xoay hướng\",\"Đã tự động xoay hướng\",\"Tên có sẵn\",\"Cài đặt sẵn có sẵn\",\"Bận\",\"Đang slice\",\"Đang xử lý yêu cầu của trợ lý; hãy thử lại sau giây lát\",\"Không thể đổi lại\",\"Không thể kiểm tra lúc này: {reason}\",\"Không thể kết nối: chỉ truy cập được máy chủ trên internet, không truy cập được chương trình trên máy tính của bạn.\",\"Không thể xóa\",\"Không thể đặt lại sau khi khởi động lại; hãy khôi phục về một điểm trước đó\",\"Không thể đặt lại các phạm vi\",\"Không thể đặt lại\",\"Không thể đặt lại: {reason}\",\"Không thể chuyển lại\",\"Không thể theo dõi vật thể này sau khi dự án được tải lại\",\"Đã hủy\",\"Đổi lại\",\"Đổi {label} lại thành {before}\",\"Đã đổi lại sau đó\",\"Đã đổi lại sau đó; hãy khôi phục về một điểm trước đó\",\"Ứng dụng ChatGPT cho máy tính\",\"ChatGPT trên web hoặc điện thoại\",\"Ứng dụng Claude\",\"Xóa các phạm vi chiều cao\",\"Codex trong terminal hoặc trình soạn thảo\",\"Thu gọn tất cả\",\"Lệnh cần khởi chạy\",\"Đã so sánh {n} tùy chọn\",\"Đã so sánh {n} tùy chọn · không giữ gì\",\"Đã so sánh {n} tùy chọn · cài đặt có thể không trở lại như cũ\",\"Đã so sánh {n} tùy chọn\",\"Đã so sánh {n} tùy chọn · không giữ gì\",\"Đã so sánh {n} tùy chọn · cài đặt có thể không trở lại như cũ\",\"Phép so sánh không thể đặt lại: {labels}\",\"Kết nối ứng dụng AI của bạn\",\"Đã kết nối\",\"Đã kết nối với {app}\",\"Đã sao chép\",\"Sao chép\",\"Sao chép cài đặt .vscode/mcp.json thay thế\",\"Sao chép lệnh\",\"Sao chép cài đặt\",\"Sao chép cài đặt thay thế\",\"Không thể khôi phục\",\"Không thể hoàn nguyên\",\"Không thể tính toán\",\"Không thể xóa\",\"Không thể xóa\",\"Không thể di chuyển\",\"Không thể di chuyển lại\",\"Không thể mở\",\"Không thể mở điểm khôi phục\",\"Không thể khôi phục về {time}\",\"Không thể lưu\",\"Không thể lưu trạng thái hiện tại trước\",\"Không thể chọn\",\"Không thể hoàn tác\",\"Cursor sẽ mở và yêu cầu bạn xác nhận. Cần uv.\",\"Xóa Plate {n} (chỉ khi đang trống)\",\"Xóa Plate\",\"Xóa Plate {n}\",\"Xóa cài đặt sẵn\",\"Xóa cài đặt sẵn {type} {name}\",\"Xóa {name}\",\"Xóa cài đặt sẵn {type}\",\"Đã xóa file\",\"Đã xóa Plate\",\"Đã xóa cài đặt sẵn\",\"Tải xuống\",\"Bị loại bỏ do chuyển cài đặt sẵn\",\"Bị loại bỏ do chuyển cài đặt sẵn; hãy khôi phục về một điểm trước đó\",\"Nhân bản vật thể {n}\",\"Đã nhân bản\",\"Mọi ứng dụng đều cần uv, một công cụ nhỏ miễn phí để chạy trình kết nối. Chỉ cần cài một lần.\",\"Mở rộng tất cả\",\"Thất bại\",\"Filament\",\"Cài đặt sẵn filament\",\"Khay filament\",\"Filament {n}\",\"Màu filament {n}\",\"Khay filament {n}\",\"File\",\"File đã tồn tại\",\"Không tìm thấy file\",\"File và cài đặt sẵn đã lưu vào ổ đĩa vẫn được giữ nguyên.\",\"Không tìm thấy thư mục\",\"Dành cho chat. Tải tiện ích mở rộng xuống, rồi mở tệp. Cần uv.\",\"Tải uv\",\"Quay lại trước khi khôi phục\",\"Phạm vi chiều cao {min}–{max} mm\",\"Đã xóa các phạm vi chiều cao\",\"Giá trị bị ẩn\",\"Trong terminal, trình soạn thảo hoặc tab Code của ứng dụng. Cần uv.\",\"Trong cài đặt của tiện ích mở rộng, đặt \\\"OrcaSlicer settings folder\\\" thành {folder}.\",\"Giá trị không hợp lệ\",\"Hiện đã có vật thể trên đó\",\"Không còn trên Plate nữa\",\"Không còn nữa\",\"Cài đặt sẵn này đang được chọn; hãy chuyển sang cài đặt sẵn khác trước\",\"Đây là Plate cuối cùng\",\"Các vật thể của nó đã được chuyển sang bên cạnh bàn in\",\"LM Studio, Windsurf và hầu hết ứng dụng khác dùng dạng này. Cần uv.\",\"Plate cuối cùng\",\"Chiều cao lớp nằm ngoài phạm vi\",\"Đã đặt lại chiều cao lớp\",\"Dòng {line} trong {count}:\",\"Dòng {first} đến {last} trong {count}:\",\"Menu chính (cạnh File) > Plugin > OrcaSlicer MCP > Cấu hình\",\"Mô hình\",\"Di chuyển lại\",\"Di chuyển vật thể {n}\",\"Di chuyển vật thể {n} sang Plate {index}\",\"Di chuyển {name}\",\"Di chuyển {name} lại về {path}\",\"Di chuyển {name} lại về {plate}\",\"Di chuyển {name} lại vị trí cũ\",\"Đã di chuyển\",\"Đã di chuyển lại sau đó\",\"Đã chuyển sang Plate\",\"Tên\",\"Cần bật ở Tùy chọn › Remote API › Allow deleting and replacing project files\",\"Cần bật ở Tùy chọn › Remote API › Allow script, G-code and connection edits\",\"Cần bật ở Tùy chọn › Remote API › Allow script, G-code and connection edits · {object}\",\"Cần bản dựng OrcaSlicer MCP\",\"Chưa từng lưu\",\"Dự án mới\",\"Cài đặt sẵn {type} mới\",\"Không\",\"Không có khay CANVAS\",\"Không có thay đổi\",\"Không có điểm khôi phục ở đây\",\"Không còn chỗ cho Plate khác\",\"Không có thay đổi này\",\"Không có bản sao này\",\"Không có filament này\",\"Không có Plate này\",\"Không có cài đặt sẵn này\",\"Không có điểm khôi phục này\",\"Không có khay này\",\"Không được phép\",\"Không tương thích\",\"Không thể chỉnh sửa ở đây\",\"Không có trong danh sách gần đây\",\"Không có trong dòng thời gian của dự án này\",\"Chưa được đặt lại\",\"Chưa sẵn sàng\",\"Không được lưu trữ\",\"Không thể làm khi OrcaSlicer đang bận một tác vụ\",\"Không thể làm khi OrcaSlicer đang đóng\",\"Không thể làm khi đang tải dự án\",\"Không thể làm khi đang slice\",\"Không thể làm khi cửa sổ đang được dựng lại\",\"Ghi chú\",\"Không có gì được di chuyển\",\"Không có gì trên Plate\",\"Không có gì để hoàn nguyên\",\"Không có gì để hiện trong cài đặt\",\"Không có gì để hoàn tác\",\"OK\",\"Vật thể không có bản sao\",\"Không tìm thấy vật thể\",\"Cài đặt vật thể {n}\",\"Tắt\",\"Bật\",\"Mở dự án {name}\",\"Mở {settings}\",\"Đã mở dự án\",\"Đang mở…\",\"Đã đóng bảng OrcaSlicer MCP panel. Mở lại từ Xem > OrcaSlicer MCP panel.\",\"Bảng OrcaSlicer MCP đã đóng. Bảng sẽ quay lại khi trợ lý của bạn dùng OrcaSlicer lần tới, hoặc nhấn phím Cách và chọn OrcaSlicer MCP.\",\"OrcaSlicer báo lỗi\",\"Ứng dụng khác\",\"Các cài đặt sẵn khác kế thừa từ nó\",\"Đã cũ: có thay đổi sau lần slice này\",\"Ngoài phạm vi\",\"Chồng lên một phạm vi khác\",\"Plate\",\"Plate bị khóa\",\"Plate {n}\",\"Plate {n} hiện chứa các vật thể khác\",\"Cài đặt sẵn\",\"Cài đặt sẵn đang được dùng\",\"Cài đặt sẵn in\",\"Máy in\",\"Cài đặt sẵn máy in\",\"Dự án đang mở\",\"Đặt lại\",\"Đặt lại các phạm vi chiều cao của {object}\",\"Đặt lại hồ sơ chiều cao lớp trước đó của {object}\",\"Đặt {n} vật thể về vị trí cũ\",\"Đặt {name} lại lên {plate}\",\"Chất lượng {value}\",\"Chỉ đọc\",\"Bị từ chối\",\"Bị từ chối\",\"Cài đặt Remote API\",\"Xóa\",\"Xóa bản sao\",\"Xóa vật thể {n}\",\"Xóa phạm vi\",\"Xóa bản sao mới của {name}\",\"Xóa phần ghi đè {label} khỏi {object}\",\"Xóa phạm vi {min}–{max} mm khỏi {object}\",\"Xóa {name} khỏi Plate\",\"Đã xóa\",\"Đã đổi tên\",\"Đã thay thế cài đặt sẵn {type} đã lưu\",\"Đặt lại chiều cao lớp\",\"Thư mục điểm khôi phục\",\"Điểm khôi phục cần bản dựng OrcaSlicer MCP\",\"Khôi phục dự án về trạng thái lúc {time} (hoàn tác các thay đổi sau đó)\",\"Khôi phục về {time}?\",\"Đã hoàn tác việc khôi phục\",\"Đã khôi phục về {time}\",\"Đang khôi phục…\",\"Hoàn nguyên\",\"Đã hoàn nguyên\",\"Đã hoàn nguyên lúc {time}\",\"Đang hoàn nguyên…\",\"Xoay lại\",\"Xoay vật thể {n}\",\"Xoay {name} lại\",\"Đã xoay\",\"Chạy lệnh này một lần. Ứng dụng máy tính dùng chung cài đặt này. Cần uv.\",\"Cùng đường dẫn\",\"Lưu dự án\",\"Lưu dự án thành {name}\",\"Lưu cài đặt sẵn {type}\",\"Đã lưu cài đặt sẵn\",\"Đã lưu dự án\",\"Chỉnh tỷ lệ lại\",\"Chỉnh tỷ lệ vật thể {n}\",\"Chỉnh tỷ lệ {name} lại\",\"Đã chỉnh tỷ lệ\",\"Script, G-code tùy chỉnh và cài đặt kết nối máy in đang bị khóa. Hãy đánh dấu 'Allow script, G-code and connection edits' trong Tùy chọn > Remote API để trợ lý được thay đổi, hoặc tự thay đổi tại đây.\",\"Hãy thiết lập ở nơi bạn dùng ứng dụng của bạn\",\"Hãy thiết lập ở nơi bạn dùng {app}\",\"Đặt lại phạm vi {min}–{max} mm của {object} thành {before}\",\"Đặt {label} lại thành {before}\",\"Đặt {label} của {object} lại thành {before}\",\"Cài đặt\",\"Cài đặt\",\"Cài đặt › Plugin › Thêm › Thêm server MCP, chọn STDIO rồi điền từng ô bên dưới. Dùng được trong chat Codex; chat ChatGPT thông thường chưa dùng được. Cần uv.\",\"Hiện chi tiết\",\"Hiện giá trị trước đó và tìm trong cài đặt\",\"Hiện giá trị trước đó và hoàn tác\",\"Hiện giá trị trước đó, hoàn tác và tìm trong cài đặt\",\"Kết quả slice\",\"Đã slice\",\"Đã slice {where}\",\"Đã hủy slice\",\"Slice thất bại\",\"Đã xảy ra sự cố\",\"Đã xảy ra sự cố: {reason}\",\"Chuyển lại\",\"Đã hủy việc chuyển\",\"Chuyển {label} lại thành {before}\",\"Chuyển {label} lại thành {before} (bỏ các chỉnh sửa chưa lưu)\",\"Filament đó không còn nữa\",\"Remote API không thể khởi động. Hãy kiểm tra cổng của nó.\",\"Remote API đang tắt.\",\"Bản sao đó đã không còn\",\"Thư mục cũ không còn nữa\",\"Tên cũ hiện đã được dùng\",\"Plate đã thay đổi sau đó; hãy khôi phục về một điểm trước đó\",\"Plugin không khởi động được\",\"Plugin không bao giờ thay đổi script, G-code hay cài đặt kết nối máy in\",\"Dự án đã thay đổi sau đó\",\"Không thể đọc điểm khôi phục\",\"Điểm khôi phục không còn nữa\",\"Điểm khôi phục đã bị thay đổi trên ổ đĩa nên không được dùng\",\"Cài đặt này không còn trong cài đặt sẵn này nữa\",\"Tab cài đặt chưa sẵn sàng\",\"Không có tab cài đặt\",\"Bản chụp trước khi khôi phục không còn nữa\",\"Bản chụp trước khi khôi phục đã bị thay đổi trên ổ đĩa nên không được dùng\",\"Sau đó hãy nói bản in bạn muốn. Mọi thay đổi trợ lý thực hiện đều hiện ở đây và bạn có thể hoàn tác bất kỳ thay đổi nào.\",\"Chúng có thể vẫn giữ giá trị của tùy chọn cuối cùng ({reason}).\",\"Khay chưa sẵn sàng\",\"Khay bị dùng hai lần\",\"Hoàn tác\",\"Không rõ cài đặt này\",\"Thay đổi chưa lưu\",\"Đã bỏ các chỉnh sửa chưa lưu: {list}\",\"File không được hỗ trợ\",\"Không tiêu đề\",\"VS Code sẽ mở và yêu cầu bạn xác nhận. Cần uv.\",\"Đang chờ ứng dụng của bạn…\",\"Đang chờ {app}…\",\"Trước đó là {before}\",\"Khi trợ lý thay đổi điều gì, nó sẽ hiện ở đây.\",\"Wipe tower X\",\"Wipe tower Y\",\"Số lượng giá trị không đúng\",\"Có\",\"Bạn đã đổi nó sau đó\",\"Cài đặt MCP của ứng dụng\",\"Các thay đổi của chính bạn từ lúc đó cũng sẽ bị hoàn tác.\",\"bên cạnh bàn in\",\"bạn đã đổi\",\"đã đổi sau đó\",\"bản sao đã bị xóa sau đó\",\"đã bị xóa sau đó\",\"bị loại bỏ do chuyển cài đặt sẵn\",\"hiện có vật thể\",\"lớp {height} mm\",\"dòng {line}: {text} ({count} dòng)\",\"đã di chuyển sau đó\",\"không có\",\"không phải số\",\"không xoay\",\"chưa đặt\",\"không giữ gì\",\"tên cũ đã được dùng\",\"đã cũ\",\"Plate {n}\",\"Plate {n} · {error}\",\"đã đặt phạm vi sau đó\",\"đã bị xóa sau đó\",\"đã hoàn nguyên\",\"bạn đã đặt lại\",\"cài đặt có thể không trở lại như cũ\",\"Plate\",\"tab cài đặt chưa sẵn sàng\",\"đến {position}\",\"bị hoàn tác do khôi phục\",\"ứng dụng của bạn\",\"{angle}° quanh {axis}\",\"{key}: không phải cài đặt in, filament hay máy in\",\"{key}: giá trị không được hỗ trợ\",\"{label} (filament {n})\",\"{label} (filament {list})\",\"{label} phải lớn hơn {min} (nhận được {got})\",\"{label} phải lớn hơn {min} (nhận được {got}); mức thấp nhất máy in cho phép được đặt trong Cài đặt máy in > Giới hạn chiều cao lớp\",\"{label} phải lớn hơn {min} và tối đa {max} (nhận được {got})\",\"{label} phải lớn hơn {min} và tối đa {max} (nhận được {got}); mức thấp nhất máy in cho phép được đặt trong Cài đặt máy in > Giới hạn chiều cao lớp\",\"{label} phải tối thiểu {min} (nhận được {got})\",\"{label} phải tối thiểu {min} (nhận được {got}); mức thấp nhất máy in cho phép được đặt trong Cài đặt máy in > Giới hạn chiều cao lớp\",\"{label} phải tối đa {max} (nhận được {got})\",\"{label} phải tối đa {max} (nhận được {got}); mức thấp nhất máy in cho phép được đặt trong Cài đặt máy in > Giới hạn chiều cao lớp\",\"{label} phải nằm trong khoảng {min} đến {max} (nhận được {got})\",\"{label} phải nằm trong khoảng {min} đến {max} (nhận được {got}); mức thấp nhất máy in cho phép được đặt trong Cài đặt máy in > Giới hạn chiều cao lớp\",\"Không cho phép {label}\",\"Hiện {name} không phù hợp với máy in này\",\"{name} không còn nữa\",\"{name} đã không còn\",\"{name} không còn trên Plate nữa\",\"{name} không còn ở đó nữa\",\"{name} → {plate}\",\"{name}, hiện có {n} bản sao\",\"{n} vật thể\",\"{n} vật thể\",\"{n} phạm vi\",\"{n} phạm vi\",\"{object} không còn trên Plate nữa\",\"Chỉ {object}\",\"Chỉ {object} · từ cài đặt sẵn in\",\"{preset} (chưa lưu)\",\"{text} ({count} dòng)\",\"{title} và {n} mục khác\",\"{value} trong {preset}\",\"{verb} (không khả dụng: {why})\",\"{verb} {what}\",\"{verb}: {label} lại thành {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"zh_CN\": [\"（空行）\",\"（已隐藏）\",\"正在恢复\",\"自适应层高\",\"添加模型\",\"添加新盘\",\"添加到 Cursor\",\"添加到 VS Code\",\"添加 {name}\",\"已添加\",\"已添加盘\",\"已经改回 {before}\",\"还需将 {setting} 添加为环境变量。\",\"一个对象\",\"参数\",\"自动摆放\",\"已自动摆放\",\"告诉它你想打印什么。它的修改会显示在这里，每一项都可以撤销。\",\"助手\",\"助手的访问已关闭。\",\"助手设为 {value} · 原为 {before}\",\"助手设为 {value} · 你已改回\",\"助手设置\",\"自动朝向\",\"已自动调整朝向\",\"内置名称\",\"内置预设\",\"忙碌\",\"正在切片\",\"正在处理助手的请求，请稍后再试\",\"无法改回\",\"现在无法检查： {reason}\",\"无法连接：它只能访问互联网上的服务器，无法访问你电脑上的程序。\",\"无法删除\",\"重启后无法恢复原样，请改为恢复到更早的时间点\",\"无法恢复这些范围\",\"无法改回原值\",\"无法改回原值： {reason}\",\"无法切换回去\",\"项目重新载入后无法跟踪此对象\",\"已取消\",\"改回\",\"将 {label} 改回 {before}\",\"之后又被更改\",\"之后又被更改，请改为恢复到更早的时间点\",\"ChatGPT 桌面应用\",\"网页版或手机版 ChatGPT\",\"Claude 应用\",\"清除高度范围\",\"终端或编辑器中的 Codex\",\"全部收起\",\"启动命令\",\"已比较 {n} 个选项\",\"已比较 {n} 个选项 · 未保留任何更改\",\"已比较 {n} 个选项 · 设置可能未恢复原样\",\"已比较 {n} 个选项\",\"已比较 {n} 个选项 · 未保留任何更改\",\"已比较 {n} 个选项 · 设置可能未恢复原样\",\"比较后未能恢复： {labels}\",\"连接你的 AI 应用\",\"已连接\",\"已连接到 {app}\",\"已复制\",\"复制\",\"改为复制 .vscode/mcp.json 设置\",\"复制命令\",\"复制设置\",\"改为复制设置\",\"无法恢复\",\"无法撤销\",\"无法计算\",\"无法删除\",\"无法删除\",\"无法移动\",\"无法移回原处\",\"无法打开\",\"无法打开恢复点\",\"无法恢复到 {time}\",\"无法保存\",\"无法先保存当前状态\",\"无法选择\",\"无法撤销\",\"Cursor 会打开并请你确认。需要 uv。\",\"删除盘 {n}（仅限为空时）\",\"删除盘\",\"删除盘 {n}\",\"删除预设\",\"删除 {type} 预设 {name}\",\"删除 {name}\",\"删除 {type} 预设\",\"已删除文件\",\"已删除盘\",\"已删除预设\",\"下载\",\"切换预设时被丢弃\",\"切换预设时被丢弃，请改为恢复到更早的时间点\",\"复制对象 {n}\",\"已复制\",\"每个应用都需要 uv，这是一个运行连接器的免费小工具，只需安装一次。\",\"全部展开\",\"失败\",\"耗材丝\",\"耗材丝预设\",\"耗材丝料盘\",\"耗材丝 {n}\",\"耗材丝 {n} 颜色\",\"耗材丝 {n} 料盘\",\"文件\",\"文件已存在\",\"未找到文件\",\"已保存到磁盘的文件和预设会保留。\",\"未找到文件夹\",\"用于聊天。下载扩展，然后打开该文件。需要 uv。\",\"获取 uv\",\"回到恢复之前的状态\",\"高度范围 {min}–{max} mm\",\"已清除高度范围\",\"已隐藏的值\",\"在终端、编辑器或应用的 Code 标签页中使用。需要 uv。\",\"在扩展的设置中，将“OrcaSlicer settings folder”设为 {folder}。\",\"无效的值\",\"现在上面有对象\",\"已不在盘上\",\"已不存在\",\"它正被选中，请先切换到其他预设\",\"这是最后一个盘\",\"其上的对象已移到热床旁边\",\"LM Studio、Windsurf 和大多数其他应用都使用这种形式。需要 uv。\",\"最后一个盘\",\"层高超出范围\",\"层高已重置\",\"第 {line} 行，共 {count} 行：\",\"第 {first} 至 {last} 行，共 {count} 行：\",\"主菜单（文件旁） > 插件 > OrcaSlicer MCP > 配置\",\"模型\",\"移回\",\"移动对象 {n}\",\"将对象 {n} 移到盘 {index}\",\"移动 {name}\",\"将 {name} 移回 {path}\",\"将 {name} 移回 {plate}\",\"将 {name} 移回原位置\",\"已移动\",\"之后又被移动\",\"已移到盘\",\"名称\",\"需要 偏好设置 › Remote API › Allow deleting and replacing project files\",\"需要 偏好设置 › Remote API › Allow script, G-code and connection edits\",\"需要 偏好设置 › Remote API › Allow script, G-code and connection edits · {object}\",\"需要 OrcaSlicer MCP 版本\",\"从未保存\",\"新建项目\",\"新建 {type} 预设\",\"否\",\"没有 CANVAS 料盘\",\"没有更改\",\"此处没有恢复点\",\"没有空间再添加盘\",\"没有这项更改\",\"没有这个副本\",\"没有这个耗材丝\",\"没有这个盘\",\"没有这个预设\",\"没有这个恢复点\",\"没有这个料盘\",\"不允许\",\"不兼容\",\"此处无法编辑\",\"不在最近列表中\",\"不在此项目的时间线中\",\"未恢复\",\"未就绪\",\"未存储\",\"OrcaSlicer 正忙于任务时无法进行\",\"OrcaSlicer 正在关闭时无法进行\",\"项目加载期间无法进行\",\"切片期间无法进行\",\"窗口重建期间无法进行\",\"备注\",\"没有移动任何内容\",\"盘上没有任何内容\",\"没有可撤销的内容\",\"设置中没有可显示的内容\",\"没有可撤销的内容\",\"确认\",\"对象没有副本\",\"未找到对象\",\"对象设置 {n}\",\"关\",\"开\",\"打开项目 {name}\",\"打开{settings}\",\"已打开项目\",\"正在打开…\",\"OrcaSlicer MCP 面板已关闭。可从 视图 > OrcaSlicer MCP panel 重新打开。\",\"OrcaSlicer MCP 面板已关闭。助手下次使用 OrcaSlicer 时会重新打开，也可以按空格键并选择 OrcaSlicer MCP。\",\"OrcaSlicer 报告了错误\",\"其他应用\",\"其他预设继承了它\",\"已过期： 此次切片之后有内容发生了变化\",\"超出范围\",\"与某个范围重叠\",\"盘\",\"盘已锁定\",\"盘 {n}\",\"盘 {n} 上现在有其他对象\",\"预设\",\"预设正在使用\",\"工艺预设\",\"打印机\",\"打印机预设\",\"项目已打开\",\"恢复\",\"恢复 {object} 的高度范围\",\"恢复 {object} 之前的层高曲线\",\"将这 {n} 个对象移回原位置\",\"将 {name} 放回 {plate}\",\"质量 {value}\",\"只读\",\"已拒绝\",\"被驳回\",\"Remote API 设置\",\"移除\",\"移除副本\",\"移除对象 {n}\",\"移除范围\",\"移除 {name} 的新副本\",\"从 {object} 移除 {label} 覆盖值\",\"从 {object} 移除 {min}–{max} mm 范围\",\"将 {name} 从盘上移除\",\"已移除\",\"已重命名\",\"已替换已保存的 {type} 预设\",\"重置层高\",\"恢复点文件夹\",\"恢复点需要 OrcaSlicer MCP 版本\",\"将项目恢复到 {time} 时的样子（撤销之后的更改）\",\"恢复到 {time}?\",\"已撤销恢复\",\"已恢复到 {time}\",\"正在恢复…\",\"撤销\",\"已撤销\",\"已于 {time} 撤销\",\"正在撤销…\",\"转回\",\"旋转对象 {n}\",\"将 {name} 转回原样\",\"已旋转\",\"只需运行一次。桌面应用共用此设置。需要 uv。\",\"路径相同\",\"保存项目\",\"将项目另存为 {name}\",\"保存 {type} 预设\",\"已保存预设\",\"已保存项目\",\"缩放还原\",\"缩放对象 {n}\",\"将 {name} 的缩放还原\",\"已缩放\",\"脚本、自定义 G-code 和打印机连接设置已锁定。要允许助手修改，请在 偏好设置 > Remote API 中勾选 'Allow script, G-code and connection edits'，也可以在此自行修改。\",\"请在你使用应用的地方进行设置\",\"请在你使用 {app} 的地方进行设置\",\"将 {object} 的 {min}–{max} mm 范围改回 {before}\",\"将 {label} 改回 {before}\",\"将 {object} 的 {label} 改回 {before}\",\"设置\",\"设置\",\"打开 设置 › 插件 › 添加 › 添加 MCP 服务器，选择 STDIO 并填写下方每一栏。可在 Codex 聊天中使用；普通 ChatGPT 聊天暂时无法使用。需要 uv。\",\"显示详情\",\"查看原值并在设置中找到它\",\"查看原值并撤销\",\"查看原值、撤销并在设置中找到它\",\"切片结果\",\"已切片\",\"已切片 {where}\",\"切片已取消\",\"切片失败\",\"出了点问题\",\"出了点问题： {reason}\",\"切换回去\",\"切换已取消\",\"将 {label} 切换回 {before}\",\"将 {label} 切换回 {before}（会丢弃未保存的编辑）\",\"那个耗材丝已不存在\",\"Remote API 无法启动。请检查它的端口。\",\"Remote API 已关闭。\",\"副本已经不存在\",\"原文件夹已不存在\",\"原名称现在已被占用\",\"盘之后发生了变化，请改为恢复到更早的时间点\",\"插件未能启动\",\"插件不会更改脚本、G-code 或打印机连接设置\",\"项目之后发生了变化\",\"无法读取恢复点\",\"恢复点已不存在\",\"恢复点在磁盘上被改动过，因此不会使用\",\"此设置已不在这个预设中\",\"设置标签页尚未就绪\",\"找不到设置标签页\",\"恢复前的快照已不存在\",\"恢复前的快照在磁盘上被改动过，因此不会使用\",\"然后告诉它你想打印什么。它所做的每项更改都会显示在这里，你可以撤销其中任何一项。\",\"它们可能仍保留着最后一个选项的值（{reason}）。\",\"料盘未就绪\",\"料盘被重复使用\",\"撤销\",\"未知设置\",\"未保存的更改\",\"已丢弃未保存的编辑： {list}\",\"不支持的文件\",\"未命名\",\"VS Code 会打开并请你确认。需要 uv。\",\"正在等待你的应用…\",\"正在等待 {app}…\",\"原为 {before}\",\"助手做出更改后，会显示在这里。\",\"擦拭塔 X\",\"擦拭塔 Y\",\"值的数量不对\",\"是\",\"之后你又改过它\",\"你的应用的 MCP 设置\",\"你在那之后自己做的更改也会被撤销。\",\"热床旁边\",\"已被你更改\",\"之后已更改\",\"之后副本已移除\",\"之后已删除\",\"切换预设时被丢弃\",\"现在有对象\",\"层 {height} mm\",\"第 {line} 行： {text}（共 {count} 行）\",\"之后已移动\",\"无\",\"不是数字\",\"未旋转\",\"未设置\",\"未保留任何更改\",\"原名称已被占用\",\"已过期\",\"盘 {n}\",\"盘 {n} · {error}\",\"之后已设置范围\",\"之后已移除\",\"已撤销\",\"已被你改回\",\"设置可能未恢复原样\",\"盘\",\"设置标签页尚未就绪\",\"到 {position}\",\"已被恢复操作撤销\",\"你的应用\",\"绕 {axis} 轴 {angle}°\",\"{key}： 不是工艺、耗材丝或打印机设置\",\"{key}： 不支持的值\",\"{label}（耗材丝 {n}）\",\"{label}（耗材丝 {list}）\",\"{label} 必须大于 {min}（当前为 {got}）\",\"{label} 必须大于 {min}（当前为 {got}）;打印机允许的最小值在 打印机设置 > 层高限制 中设置\",\"{label} 必须大于 {min} 且不超过 {max}（当前为 {got}）\",\"{label} 必须大于 {min} 且不超过 {max}（当前为 {got}）;打印机允许的最小值在 打印机设置 > 层高限制 中设置\",\"{label} 必须不小于 {min}（当前为 {got}）\",\"{label} 必须不小于 {min}（当前为 {got}）;打印机允许的最小值在 打印机设置 > 层高限制 中设置\",\"{label} 必须不超过 {max}（当前为 {got}）\",\"{label} 必须不超过 {max}（当前为 {got}）;打印机允许的最小值在 打印机设置 > 层高限制 中设置\",\"{label} 必须介于 {min} 和 {max} 之间（当前为 {got}）\",\"{label} 必须介于 {min} 和 {max} 之间（当前为 {got}）;打印机允许的最小值在 打印机设置 > 层高限制 中设置\",\"不允许 {label}\",\"{name} 现在不适合这台打印机\",\"{name} 已不存在\",\"{name} 已经不存在\",\"{name} 已不在盘上\",\"{name} 已不在那里\",\"{name} → {plate}\",\"{name}，现有 {n} 份\",\"{n} 个对象\",\"{n} 个对象\",\"{n} 个范围\",\"{n} 个范围\",\"{object} 已不在盘上\",\"仅 {object}\",\"仅 {object} · 来自工艺预设\",\"{preset}（未保存）\",\"{text}（共 {count} 行）\",\"{title} 等另外 {n} 项\",\"{preset} 中的 {value}\",\"{verb}（不可用： {why}）\",\"{verb} {what}\",\"{verb}： 将 {label} 改回 {before}\",\"{x} × {y} × {z} mm\"],\n"
    "\"zh_TW\": [\"（空行）\",\"（已隱藏）\",\"正在還原\",\"自適應層高\",\"新增模型\",\"新增列印板\",\"加入 Cursor\",\"加入 VS Code\",\"新增 {name}\",\"已新增\",\"已新增列印板\",\"已經改回 {before}\",\"還需將 {setting} 加入為環境變數。\",\"一個物件\",\"引數\",\"自動擺放\",\"已自動擺放\",\"告訴它你想列印什麼。它的修改會顯示在這裡，每一項都可以復原。\",\"助理\",\"助理的存取已關閉。\",\"助理設為 {value} · 原為 {before}\",\"助理設為 {value} · 你已改回\",\"助理設定\",\"自動定向\",\"已自動定向\",\"內建名稱\",\"內建預設\",\"忙碌\",\"正在切片\",\"正在處理助理的請求，請稍後再試\",\"無法改回\",\"現在無法檢查： {reason}\",\"無法連接：它只能存取網際網路上的伺服器，無法存取你電腦上的程式。\",\"無法刪除\",\"重新啟動後無法復原，請改為還原到更早的時間點\",\"無法復原這些範圍\",\"無法改回原值\",\"無法改回原值： {reason}\",\"無法切換回去\",\"專案重新載入後無法追蹤此物件\",\"已取消\",\"改回\",\"將 {label} 改回 {before}\",\"之後又被更改\",\"之後又被更改，請改為還原到更早的時間點\",\"ChatGPT 桌面應用程式\",\"網頁版或手機版 ChatGPT\",\"Claude 應用程式\",\"清除高度範圍\",\"終端機或編輯器中的 Codex\",\"全部摺疊\",\"啟動指令\",\"已比較 {n} 個選項\",\"已比較 {n} 個選項 · 未保留任何變更\",\"已比較 {n} 個選項 · 設定可能未還原\",\"已比較 {n} 個選項\",\"已比較 {n} 個選項 · 未保留任何變更\",\"已比較 {n} 個選項 · 設定可能未還原\",\"比較後未能還原： {labels}\",\"連接你的 AI 應用程式\",\"已連接\",\"已連接到 {app}\",\"已複製\",\"複製\",\"改為複製 .vscode/mcp.json 設定\",\"複製指令\",\"複製設定\",\"改為複製設定\",\"無法還原\",\"無法復原\",\"無法計算\",\"無法刪除\",\"無法刪除\",\"無法移動\",\"無法移回原處\",\"無法開啟\",\"無法開啟還原點\",\"無法還原到 {time}\",\"無法儲存\",\"無法先儲存目前狀態\",\"無法選取\",\"無法復原\",\"Cursor 會開啟並請你確認。需要 uv。\",\"刪除列印板 {n}（僅限為空時）\",\"刪除列印板\",\"刪除列印板 {n}\",\"刪除預設\",\"刪除 {type} 預設 {name}\",\"刪除 {name}\",\"刪除 {type} 預設\",\"已刪除檔案\",\"已刪除列印板\",\"已刪除預設\",\"下載\",\"切換預設時被捨棄\",\"切換預設時被捨棄，請改為還原到更早的時間點\",\"複製物件 {n}\",\"已複製\",\"每個應用程式都需要 uv，這是執行連接器的免費小工具，只需安裝一次。\",\"全部展開\",\"失敗\",\"線材\",\"線材預設\",\"線材料盤\",\"線材 {n}\",\"線材 {n} 顏色\",\"線材 {n} 料盤\",\"檔案\",\"檔案已存在\",\"找不到檔案\",\"已儲存到磁碟的檔案和預設會保留。\",\"找不到資料夾\",\"用於聊天。下載擴充功能，然後開啟該檔案。需要 uv。\",\"取得 uv\",\"回到還原之前的狀態\",\"高度範圍 {min}–{max} mm\",\"已清除高度範圍\",\"已隱藏的值\",\"在終端機、編輯器或應用程式的 Code 分頁中使用。需要 uv。\",\"在擴充功能的設定中，將「OrcaSlicer settings folder」設為 {folder}。\",\"無效的值\",\"現在上面有物件\",\"已不在列印板上\",\"已不存在\",\"它正被選取，請先切換到其他預設\",\"這是最後一個列印板\",\"其上的物件已移到熱床旁邊\",\"LM Studio、Windsurf 和大多數其他應用程式都使用這種形式。需要 uv。\",\"最後一個列印板\",\"層高超出範圍\",\"層高已重設\",\"第 {line} 行，共 {count} 行：\",\"第 {first} 至 {last} 行，共 {count} 行：\",\"主選單（檔案旁） > 外掛 > OrcaSlicer MCP > 設定\",\"模型\",\"移回\",\"移動物件 {n}\",\"將物件 {n} 移到列印板 {index}\",\"移動 {name}\",\"將 {name} 移回 {path}\",\"將 {name} 移回 {plate}\",\"將 {name} 移回原位置\",\"已移動\",\"之後又被移動\",\"已移到列印板\",\"名稱\",\"需要 偏好設定 › Remote API › Allow deleting and replacing project files\",\"需要 偏好設定 › Remote API › Allow script, G-code and connection edits\",\"需要 偏好設定 › Remote API › Allow script, G-code and connection edits · {object}\",\"需要 OrcaSlicer MCP 版本\",\"從未儲存\",\"新增專案\",\"新增 {type} 預設\",\"否\",\"沒有 CANVAS 料盤\",\"沒有變更\",\"此處沒有還原點\",\"沒有空間再新增列印板\",\"沒有這項變更\",\"沒有這個副本\",\"沒有這個線材\",\"沒有這個列印板\",\"沒有這個預設\",\"沒有這個還原點\",\"沒有這個料盤\",\"不允許\",\"不相容\",\"此處無法編輯\",\"不在最近清單中\",\"不在此專案的時間軸中\",\"未還原\",\"尚未就緒\",\"未儲存\",\"OrcaSlicer 正忙於工作時無法進行\",\"OrcaSlicer 正在關閉時無法進行\",\"專案載入期間無法進行\",\"切片期間無法進行\",\"視窗重建期間無法進行\",\"備註\",\"沒有移動任何內容\",\"列印板上沒有任何內容\",\"沒有可復原的內容\",\"設定中沒有可顯示的內容\",\"沒有可復原的內容\",\"確認\",\"物件沒有副本\",\"找不到物件\",\"物件設定 {n}\",\"關閉\",\"開啟\",\"開啟專案 {name}\",\"開啟{settings}\",\"已開啟專案\",\"正在開啟…\",\"OrcaSlicer MCP 面板已關閉。可從 檢視 > OrcaSlicer MCP panel 重新開啟。\",\"OrcaSlicer MCP 面板已關閉。助理下次使用 OrcaSlicer 時會重新開啟，也可以按空白鍵並選擇 OrcaSlicer MCP。\",\"OrcaSlicer 回報了錯誤\",\"其他應用程式\",\"其他預設繼承了它\",\"已過期： 此次切片之後有內容發生了變化\",\"超出範圍\",\"與某個範圍重疊\",\"列印板\",\"列印板已鎖定\",\"列印板 {n}\",\"列印板 {n} 上現在有其他物件\",\"預設\",\"預設正在使用\",\"列印參數預設\",\"列印裝置\",\"列印裝置預設\",\"專案已開啟\",\"還原\",\"還原 {object} 的高度範圍\",\"還原 {object} 之前的層高曲線\",\"將這 {n} 個物件移回原位置\",\"將 {name} 放回 {plate}\",\"品質 {value}\",\"唯讀\",\"已拒絕\",\"被駁回\",\"Remote API 設定\",\"移除\",\"移除副本\",\"移除物件 {n}\",\"移除範圍\",\"移除 {name} 的新副本\",\"從 {object} 移除 {label} 覆寫值\",\"從 {object} 移除 {min}–{max} mm 範圍\",\"將 {name} 從列印板上移除\",\"已移除\",\"已重新命名\",\"已取代已儲存的 {type} 預設\",\"重設層高\",\"還原點資料夾\",\"還原點需要 OrcaSlicer MCP 版本\",\"將專案還原到 {time} 時的樣子（復原之後的變更）\",\"還原到 {time}?\",\"已復原還原動作\",\"已還原到 {time}\",\"正在還原…\",\"復原\",\"已復原\",\"已於 {time} 復原\",\"正在復原…\",\"轉回\",\"旋轉物件 {n}\",\"將 {name} 轉回原樣\",\"已旋轉\",\"只需執行一次。桌面應用程式共用此設定。需要 uv。\",\"路徑相同\",\"儲存專案\",\"將專案另存為 {name}\",\"儲存 {type} 預設\",\"已儲存預設\",\"已儲存專案\",\"縮放還原\",\"縮放物件 {n}\",\"將 {name} 的縮放還原\",\"已縮放\",\"指令碼、自訂 G-code 和列印裝置連線設定已鎖定。要允許助理修改，請在 偏好設定 > Remote API 中勾選 'Allow script, G-code and connection edits'，也可以在此自行修改。\",\"請在你使用應用程式的地方進行設定\",\"請在你使用 {app} 的地方進行設定\",\"將 {object} 的 {min}–{max} mm 範圍改回 {before}\",\"將 {label} 改回 {before}\",\"將 {object} 的 {label} 改回 {before}\",\"設定\",\"設定\",\"開啟 設定 › 外掛程式 › 新增 › 新增 MCP 伺服器，選擇「標準輸入/輸出」並填寫下方每一欄。可在 Codex 聊天中使用；一般 ChatGPT 聊天暫時無法使用。需要 uv。\",\"顯示詳細資訊\",\"查看原值並在設定中找到它\",\"查看原值並復原\",\"查看原值、復原並在設定中找到它\",\"切片結果\",\"已切片\",\"已切片 {where}\",\"切片已取消\",\"切片失敗\",\"發生了問題\",\"發生了問題： {reason}\",\"切換回去\",\"切換已取消\",\"將 {label} 切換回 {before}\",\"將 {label} 切換回 {before}（會捨棄未儲存的編輯）\",\"那個線材已不存在\",\"Remote API 無法啟動。請檢查它的連接埠。\",\"Remote API 已關閉。\",\"副本已經不存在\",\"原資料夾已不存在\",\"原名稱現在已被占用\",\"列印板之後發生了變化，請改為還原到更早的時間點\",\"外掛未能啟動\",\"外掛不會變更指令碼、G-code 或列印裝置連線設定\",\"專案之後發生了變化\",\"無法讀取還原點\",\"還原點已不存在\",\"還原點在磁碟上被改動過，因此不會使用\",\"此設定已不在這個預設中\",\"設定分頁尚未就緒\",\"找不到設定分頁\",\"還原前的快照已不存在\",\"還原前的快照在磁碟上被改動過，因此不會使用\",\"然後告訴它你想列印什麼。它所做的每項變更都會顯示在這裡，你可以復原其中任何一項。\",\"它們可能仍保留著最後一個選項的值（{reason}）。\",\"料盤尚未就緒\",\"料盤被重複使用\",\"復原\",\"未知設定\",\"未儲存的變更\",\"已捨棄未儲存的編輯： {list}\",\"不支援的檔案\",\"未命名\",\"VS Code 會開啟並請你確認。需要 uv。\",\"正在等待你的應用程式…\",\"正在等待 {app}…\",\"原為 {before}\",\"助理做出變更後，會顯示在這裡。\",\"換料塔 X\",\"換料塔 Y\",\"值的數量不對\",\"是\",\"之後你又改過它\",\"你的應用程式的 MCP 設定\",\"你在那之後自己做的變更也會被復原。\",\"熱床旁邊\",\"已被你變更\",\"之後已變更\",\"之後副本已移除\",\"之後已刪除\",\"切換預設時被捨棄\",\"現在有物件\",\"層 {height} mm\",\"第 {line} 行： {text}（共 {count} 行）\",\"之後已移動\",\"無\",\"不是數字\",\"未旋轉\",\"未設定\",\"未保留任何變更\",\"原名稱已被占用\",\"已過期\",\"列印板 {n}\",\"列印板 {n} · {error}\",\"之後已設定範圍\",\"之後已移除\",\"已復原\",\"已被你改回\",\"設定可能未還原\",\"列印板\",\"設定分頁尚未就緒\",\"到 {position}\",\"已被還原動作復原\",\"你的應用程式\",\"繞 {axis} 軸 {angle}°\",\"{key}： 不是列印參數、線材或列印裝置設定\",\"{key}： 不支援的值\",\"{label}（線材 {n}）\",\"{label}（線材 {list}）\",\"{label} 必須大於 {min}（目前為 {got}）\",\"{label} 必須大於 {min}（目前為 {got}）;列印裝置允許的最小值在 列印裝置設定 > 層高限制 中設定\",\"{label} 必須大於 {min} 且不超過 {max}（目前為 {got}）\",\"{label} 必須大於 {min} 且不超過 {max}（目前為 {got}）;列印裝置允許的最小值在 列印裝置設定 > 層高限制 中設定\",\"{label} 必須不小於 {min}（目前為 {got}）\",\"{label} 必須不小於 {min}（目前為 {got}）;列印裝置允許的最小值在 列印裝置設定 > 層高限制 中設定\",\"{label} 必須不超過 {max}（目前為 {got}）\",\"{label} 必須不超過 {max}（目前為 {got}）;列印裝置允許的最小值在 列印裝置設定 > 層高限制 中設定\",\"{label} 必須介於 {min} 和 {max} 之間（目前為 {got}）\",\"{label} 必須介於 {min} 和 {max} 之間（目前為 {got}）;列印裝置允許的最小值在 列印裝置設定 > 層高限制 中設定\",\"不允許 {label}\",\"{name} 現在不適合這台列印裝置\",\"{name} 已不存在\",\"{name} 已經不存在\",\"{name} 已不在列印板上\",\"{name} 已不在那裡\",\"{name} → {plate}\",\"{name}，現有 {n} 份\",\"{n} 個物件\",\"{n} 個物件\",\"{n} 個範圍\",\"{n} 個範圍\",\"{object} 已不在列印板上\",\"僅 {object}\",\"僅 {object} · 來自列印參數預設\",\"{preset}（未儲存）\",\"{text}（共 {count} 行）\",\"{title} 等另外 {n} 項\",\"{preset} 中的 {value}\",\"{verb}（無法使用： {why}）\",\"{verb} {what}\",\"{verb}： 將 {label} 改回 {before}\",\"{x} × {y} × {z} mm\"]\n"
    "}};\n"
    "// END PANEL I18N\n"
    "</script>\n"
    "<script id=\"panel-core\">\n"
    "(function (root) {\n"
    "  'use strict';\n"
    "\n"
    "  var PROTOCOL_VERSION = 1;\n"
    "  var OFF_TEXT = 'The Remote API is off.'; // when the host sends no capabilities.off_text (drawn through t)\n"
    "  var SETTINGS_LABEL = 'Remote API settings'; // when the host sends no capabilities.settings_label (drawn through t)\n"
    "  N_('The Remote API is off.'); N_('Remote API settings');\n"
    "\n"
    "  function has(s) { return s !== null && s !== undefined && s !== ''; }\n"
    "\n"
    "  // ---- Translation (orcaslicer-mcp #21) ---------------------------------------------------------------------------\n"
    "  // The page's words are English in this code: t('...') draws one, N_('...') marks one drawn later through t().\n"
    "  // root.McpPanelI18n (the panel-i18n block, made by scripts/mcp_panel_i18n.py) holds a table per language keyed by\n"
    "  // that English, and `host`: the templates of the text the host sends (\"Set {label} back to {before}\"). The host's\n"
    "  // words stay English in the rows (the assistant and the host's own code read them); ht() translates them only when\n"
    "  // drawn: an exact entry, else the first template that matches, its parts translated in turn.\n"
    "  var I18N = root.McpPanelI18n || { langs: {}, host: [] };\n"
    "  var tr = null, langCode = '', hostRes = null;\n"
    "  function N_(en) { return en; }\n"
    "  // code: the host's capabilities.language (\"ca_ES\", \"zh_TW\", \"pt_BR\"; \"en_US\" or nothing = English). Returns the\n"
    "  // table picked (\"\" = English): the code, else its language (\"ca\"), else a table of that language (\"pt\" -> \"pt_BR\").\n"
    "  function setLanguage(code) {\n"
    "    var langs = I18N.langs || {};\n"
    "    var c = String(code || '').replace(/-/g, '_').replace(/\\..*$/, '');\n"
    "    var base = c.split('_')[0].toLowerCase();\n"
    "    var pick = '';\n"
    "    if (Object.prototype.hasOwnProperty.call(langs, c)) pick = c;\n"
    "    else if (Object.prototype.hasOwnProperty.call(langs, base)) pick = base;\n"
    "    else Object.keys(langs).sort().forEach(function (k) { if (!pick && k.split('_')[0].toLowerCase() === base) pick = k; });\n"
    "    langCode = pick;\n"
    "    tr = pick ? table(langs[pick]) : null;\n"
    "    return pick;\n"
    "  }\n"
    "  function language() { return langCode; }\n"
    "  // A language's table: an object, or (as the panel-i18n block ships them) its translations in the order of I18N.keys.\n"
    "  var tables = {};\n"
    "  function table(t) {\n"
    "    if (!t || Object.prototype.toString.call(t) !== '[object Array]') return t || null;\n"
    "    if (tables[langCode]) return tables[langCode];\n"
    "    var keys = I18N.keys || [], out = {};\n"
    "    for (var i = 0; i < keys.length && i < t.length; i++) if (has(t[i])) out[keys[i]] = t[i];\n"
    "    tables[langCode] = out;\n"
    "    return out;\n"
    "  }\n"
    "  function fill(s, args) {\n"
    "    if (!args) return s;\n"
    "    return String(s).replace(/\\{([a-z_]+)\\}/g, function (m, k) {\n"
    "      return Object.prototype.hasOwnProperty.call(args, k) && args[k] !== null && args[k] !== undefined ? String(args[k]) : m;\n"
    "    });\n"
    "  }\n"
    "  function t(en, args) { return fill(tr && has(tr[en]) ? tr[en] : en, args); }\n"
    "  // A placeholder's text in a host template: numbers for counts and indexes, a clock time, else anything.\n"
    "  var NUMBER_NAMES = { n: 1, count: 1, index: 1, line: 1, first: 1, last: 1 };\n"
    "  function hostTemplates() {\n"
    "    if (hostRes) return hostRes;\n"
    "    hostRes = (I18N.host || []).map(function (en) {\n"
    "      var names = [], lit = 0, letters = 0, bound = false;\n"
    "      var src = String(en).replace(/\\{([a-z_]+)\\}|([^{]+|\\{)/g, function (m, name, text) {\n"
    "        if (name) {\n"
    "          names.push(name);\n"
    "          if (NUMBER_NAMES[name] || name === 'time') bound = true;\n"
    "          return NUMBER_NAMES[name] ? '(\\\\d+)' : name === 'time' ? '(\\\\d{1,2}:\\\\d{2})' : '([\\\\s\\\\S]+?)';\n"
    "        }\n"
    "        lit += text.length;\n"
    "        letters += (text.match(/[A-Za-z\\u00c0-\\uffff]/g) || []).length;\n"
    "        return text.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');\n"
    "      });\n"
    "      // A template that is nearly all placeholders (\"{value} in {preset}\", \"{name} → {plate}\") would also match a\n"
    "      // user's own name (\"Box in a box\"): used only when a number or a time holds it, or its own words are 4 letters+.\n"
    "      return { en: en, names: names, lit: lit, re: new RegExp('^' + src + '$'), usable: bound || letters >= 4 };\n"
    "    }).filter(function (h) { return h.names.length && h.usable; });\n"
    "    // The most specific first: the longest literal text.\n"
    "    hostRes.sort(function (a, b) { return b.lit - a.lit; });\n"
    "    return hostRes;\n"
    "  }\n"
    "  // The host's own translation of an English word of a row: row.local maps a setting's name and a choice's label\n"
    "  // (\"Wall loops\", \"Classic\") to the words OrcaSlicer itself uses for them in its language.\n"
    "  function local(row, en) {\n"
    "    return row && row.local && typeof row.local === 'object' && Object.prototype.hasOwnProperty.call(row.local, en) &&\n"
    "      has(row.local[en]) ? String(row.local[en]) : null;\n"
    "  }\n"
    "  // text the host sent (any of its fields), drawn in the page's language. row (optional): the row it belongs to, whose\n"
    "  // local words stand in for the English ones, also inside a template (\"Set {label} back to {before}\").\n"
    "  function ht(text, row, depth) {\n"
    "    if (!has(text) || !tr) return text;\n"
    "    var s = String(text);\n"
    "    var own = local(row, s);\n"
    "    if (own !== null) return own;\n"
    "    if (has(tr[s])) return tr[s];\n"
    "    if ((depth || 0) > 3) return s;\n"
    "    var hs = hostTemplates();\n"
    "    for (var i = 0; i < hs.length; i++) {\n"
    "      var m = hs[i].re.exec(s);\n"
    "      if (!m) continue;\n"
    "      var args = {};\n"
    "      for (var j = 0; j < hs[i].names.length; j++) {\n"
    "        var v = m[j + 1];\n"
    "        args[hs[i].names[j]] = ht(v, row, (depth || 0) + 1);\n"
    "      }\n"
    "      return fill(has(tr[hs[i].en]) ? tr[hs[i].en] : hs[i].en, args);\n"
    "    }\n"
    "    return s;\n"
    "  }\n"
    "  // A multi-line value's hover tag (\"Lines 3 to 7 of 14:\" and then G-code lines): its first line through ht, the\n"
    "  // quoted lines as they are.\n"
    "  function tipText(text, row) {\n"
    "    if (!has(text) || !tr) return text;\n"
    "    var s = String(text), nl = s.indexOf('\\n');\n"
    "    return nl < 0 ? ht(s, row) : ht(s.slice(0, nl), row) + s.slice(nl);\n"
    "  }\n"
    "  // A row's label, value or earlier value as drawn.\n"
    "  function rowLabel(r) {\n"
    "    if (!r) return '';\n"
    "    var label = String(r.label || '');\n"
    "    // A list of setting names the host gave local words for (\"Start G-code, End G-code\", a connector's refusal):\n"
    "    // each name on its own. Only with local words, so a user's own name with a comma is never split.\n"
    "    if (tr && r.local && label.indexOf(', ') >= 0 && local(r, label) === null)\n"
    "      return label.split(', ').map(function (x) { return ht(x, r); }).join(', ');\n"
    "    return ht(label, r);\n"
    "  }\n"
    "  function valueText(r, v) { return has(v) ? ht(v, r) : v; }\n"
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
    "    var label = rowLabel(r);\n"
    "    var value = has(r.value) ? String(valueText(r, r.value)) : '';\n"
    "    if (!value) return label;\n"
    "    return label + (has(r.before) ? ' → ' : ' ') + value;\n"
    "  }\n"
    "\n"
    "  function groupTitle(group) {\n"
    "    var all = group.rows || [];\n"
    "    if (!all.length) return '';\n"
    "    var rows = all.filter(function (r) { return r.kind !== 'slice' && r.kind !== 'refused' && r.kind !== 'note'; });\n"
    "    if (!rows.length) {\n"
    "      // A connector's note (\"Compared 3 options · nothing kept\"), or the assistant's own note, names a group that\n"
    "      // holds no change.\n"
    "      for (var n = all.length - 1; n >= 0; n--) {\n"
    "        if (all[n].title_hint === 'note' || all[n].kind === 'note')\n"
    "          return rowLabel(all[n]) + (has(all[n].value) ? ' \\u00b7 ' + valueText(all[n], all[n].value) : '');\n"
    "      }\n"
    "      var refused = all.filter(function (r) { return r.kind === 'refused'; });\n"
    "      var slices = all.filter(function (r) { return r.kind === 'slice'; });\n"
    "      if (slices.length) return t('Sliced {where}', { where: ht((slices[slices.length - 1].where || '').trim()) });\n"
    "      if (refused.length) return t('{label} not allowed', { label: rowLabel(refused[0]) });\n"
    "      return t('Sliced {where}', { where: ht((all[all.length - 1].where || '').trim()) });\n"
    "    }\n"
    "    var more = rows.length - 1;\n"
    "    var win = null;\n"
    "    for (var i = 0; i < rows.length; i++) {\n"
    "      if (rows[i].title_hint === 'added' || rows[i].title_hint === 'opened') { win = rows[i]; break; }\n"
    "    }\n"
    "    if (win) {\n"
    "      var title = rowLabel(win) + (has(win.value) ? ' ' + valueText(win, win.value) : '');\n"
    "      return more > 0 ? t('{title} and {n} more', { title: title, n: more }) : title;\n"
    "    }\n"
    "    if (more === 0) return singleText(rows[0]);\n"
    "    return t('{title} and {n} more', { title: rowLabel(rows[0]), n: more });\n"
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
    "    return caps && has(caps.off_text) ? String(ht(caps.off_text)) : t(OFF_TEXT);\n"
    "  }\n"
    "\n"
    "  // The settings icon's hover tag.\n"
    "  function settingsLabel(caps) {\n"
    "    return caps && has(caps.settings_label) ? String(ht(caps.settings_label)) : t(SETTINGS_LABEL);\n"
    "  }\n"
    "  // The off line's link: \"Open the \" + the label. An ordinary capitalised first word is lower-cased (\"Assistant\n"
    "  // settings\" -> \"Open the assistant settings\"); a name keeps its capitals: a first word with other capitals\n"
    "  // (\"OrcaSlicer MCP settings\") or one followed by an acronym (\"Remote API settings\").\n"
    "  function settingsLink(caps) {\n"
    "    var l = settingsLabel(caps);\n"
    "    // Another language says it its own way, with the label as the host or the table gives it.\n"
    "    if (tr) return t('Open the {settings}', { settings: l });\n"
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
    "    if (row.dropped === true) return { value: shown, note: N_('dropped by a preset switch') };\n"
    "    var e = (tracked || isNewest) ? liveEntry(row, live) : null;\n"
    "    if (!e) return { value: shown, note: '' };\n"
    "    if (!e.present) return { value: shown, note: N_('removed since') };\n"
    "    // The user put the setting back to what it was before the assistant changed it (for example with OrcaSlicer's\n"
    "    // own reset arrow beside the setting): that undoes the assistant's change rather than making a new one.\n"
    "    if (!sameValue(e.value, shown) && has(row.before) && sameValue(e.value, String(row.before))) {\n"
    "      return { value: String(e.value), note: N_('set back by you') };\n"
    "    }\n"
    "    if (!sameValue(e.value, shown)) return { value: String(e.value), note: N_('changed by you') };\n"
    "    return { value: shown, note: '' };\n"
    "  }\n"
    "\n"
    "  // canRestore (optional, default true): the host offers restore points (capabilities.restore); without them the\n"
    "  // reason does not send the user to one.\n"
    "  function undoState(row, live, isNewest, tracked, canRestore) {\n"
    "    var again = canRestore === false ? t('Changed again since') : t('Changed again since; restore an earlier point instead');\n"
    "    if (!row.undo) return { available: false, why: '' };\n"
    "    if (has(row.undone_at)) return { available: false, why: t('Reverted at {time}', { time: row.undone_at }) };\n"
    "    // The assistant's own preset switch dropped the edit (the host's dropped flag): nothing of it is left to undo.\n"
    "    if (row.dropped === true) {\n"
    "      return { available: false, why: canRestore === false ? t('Dropped by a preset switch') : t('Dropped by a preset switch; restore an earlier point instead') };\n"
    "    }\n"
    "    // The assistant changed the same setting again later (the host's superseded flag): undoing this row would\n"
    "    // overwrite that change.\n"
    "    if (row.superseded === true) return { available: false, why: again };\n"
    "    var e = liveEntry(row, live);\n"
    "    if (e) {\n"
    "      if (!e.present) {\n"
    "        var name = row.where || row.value || row.label;\n"
    "        return { available: false, why: has(name) ? t('{name} is no longer on the plate', { name: name === row.label ? rowLabel(row) : ht(name, row) })\n"
    "                                                  : t('It is no longer on the plate') };\n"
    "      }\n"
    "      if (!sameValue(e.value, has(row.value) ? String(row.value) : '') && has(row.before) && sameValue(e.value, String(row.before))) {\n"
    "        return { available: false, why: t('Already back to {before}', { before: valueText(row, String(row.before)) }) };\n"
    "      }\n"
    "      if (!sameValue(e.value, has(row.value) ? String(row.value) : '')) {\n"
    "        return {\n"
    "          available: false,\n"
    "          why: (tracked || isNewest) ? t('You changed it since') : again\n"
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
    "    setLanguage: setLanguage,\n"
    "    language: language,\n"
    "    t: t,\n"
    "    N_: N_,\n"
    "    ht: ht,\n"
    "    tipText: tipText,\n"
    "    rowLabel: rowLabel,\n"
    "    valueText: valueText,\n"
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
    "  var t = Core.t, N_ = Core.N_, ht = Core.ht;\n"
    "  // The type icon's hover tag, drawn through t.\n"
    "  var TIP = { setting: N_('Setting'), preset: N_('Preset'), model: N_('Model'), plate: N_('Plate'), slice: N_('Slice result'),\n"
    "              refused: N_('Refused'), file: N_('File'), note: N_('Note') };\n"
    "  var history = { rows: [], groups: [] };\n"
    "  var caps = { restore: false, open_settings: true, settings_hint: '', off_text: '', settings_label: '', reveal: false, tracked_live: false, language: '' };\n"
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
    "  // disp.note is one of liveDisplay's English notes (compared here as they are; drawn through t).\n"
    "  function detailText(r, disp) {\n"
    "    var parts = [];\n"
    "    var b = has(r.before) ? String(Core.valueText(r, String(r.before))) : '';\n"
    "    var v = Core.valueText(r, r.value);\n"
    "    if (disp.note === 'changed by you' && b) {\n"
    "      parts.push(t('Assistant set {value} \\u00b7 was {before}', { value: v, before: b }));\n"
    "      return parts.join(' \\u00b7 ');\n"
    "    }\n"
    "    if (disp.note === 'set back by you') return t('Assistant set {value} \\u00b7 you set it back', { value: v });\n"
    "    if (b) parts.push(t('Was {before}', { before: b }));\n"
    "    if (has(r.where) && !whereInTip(r)) parts.push(ht(r.where, r));\n"
    "    return parts.join(' \\u00b7 ');\n"
    "  }\n"
    "  function undoTip(r) {\n"
    "    var verb = ht(r.undo.verb, r);\n"
    "    if (has(r.undo.tip)) return ht(r.undo.tip, r);\n"
    "    if (has(r.before)) return t('{verb}: {label} back to {before}', { verb: verb, label: Core.rowLabel(r), before: Core.valueText(r, String(r.before)) });\n"
    "    return t('{verb} {what}', { verb: verb, what: has(r.value) ? Core.valueText(r, r.value) : Core.rowLabel(r) });\n"
    "  }\n"
    "  // What a click on the row does, for its hover tag.\n"
    "  function rowTip(r) {\n"
    "    if (r.undo && revealable(r)) return t('Show what it was, undo it, and find it in settings');\n"
    "    if (r.undo) return t('Show what it was and undo it');\n"
    "    if (revealable(r)) return t('Show what it was and find it in settings');\n"
    "    return t('Show details');\n"
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
    "    var verb = r.undo ? ht(r.undo.verb, r) : '';\n"
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
    "        tail = '<span class=\"faint\">' + esc(t('Reverted')) + '</span>';\n"
    "        if (has(r.undone_at)) tail = '<span class=\"faint tip-left\" data-tip=\"' + esc(t('Reverted at {time}', { time: r.undone_at })) + '\">' + esc(t('Reverted')) + '</span>';\n"
    "      } else if (pendingUndo[r.id]) {\n"
    "        tail = '<span class=\"faint\">' + esc(t('Reverting\\u2026')) + '</span>';\n"
    "      } else if (st.available) {\n"
    "        tail = '<button type=\"button\" class=\"lnk tip-left\" data-undo=\"' + r.id + '\" data-fk=\"u:' + r.id + '\" data-tip=\"' + esc(undoTip(r)) + '\">' + esc(verb) + '</button>';\n"
    "      } else {\n"
    "        tail = '<button type=\"button\" class=\"lnk off tip-left\" aria-disabled=\"true\" data-fk=\"u:' + r.id + '\" data-tip=\"' + esc(st.why) + '\" aria-label=\"' +\n"
    "          esc(t('{verb} (unavailable: {why})', { verb: verb, why: st.why })) + '\">' + esc(verb) + '</button>';\n"
    "      }\n"
    "    }\n"
    "    // A multi-line value (a G-code template) carries a few of its lines for the hover tags (value_tip, before_tip).\n"
    "    var dTip = whereInTip(r) ? ' data-tip=\"' + esc(ht(r.where, r)) + '\"' : (has(r.before_tip) && has(r.before) ? ' data-tip=\"' + esc(Core.tipText(r.before_tip, r)) + '\"' : '');\n"
    "    var more = '<div class=\"more\"><span class=\"d\"' + dTip + '>' + esc(detail) + '</span>' + tail + '</div>';\n"
    "    if (errLine) more = '<div class=\"more\"><span class=\"d bad\">' + esc(ht(errLine, r)) + '</span>' + tail + '</div>';\n"
    "    else if (kind === 'refused' && has(r.error) && !detail) more = '<div class=\"more\"><span class=\"d\">' + esc(ht(r.error, r)) + '</span>' + tail + '</div>';\n"
    "    // A reverted row shows what is there now (the earlier value); the undone value, struck through, goes under the\n"
    "    // label so the label keeps its width.\n"
    "    var shownValue = Core.valueText(r, disp.value);\n"
    "    var value = esc(shownValue);\n"
    "    var shownText = shownValue;\n"
    "    var noteHtml = '';\n"
    "    var vTip = '';\n"
    "    if (reverted) {\n"
    "      noteHtml = '<span class=\"chg\"><s class=\"old\">' + esc(shownValue) + '</s> ' + esc(t('reverted')) + '</span>';\n"
    "      var shownBefore = has(r.before) ? Core.valueText(r, String(r.before)) : '';\n"
    "      value = has(r.before) ? swatch(r, r.before) + esc(shownBefore) : '';\n"
    "      shownText = shownBefore;\n"
    "      vTip = has(r.before) && has(r.before_tip) ? r.before_tip : '';\n"
    "    } else {\n"
    "      value = swatch(r, disp.value) + value;\n"
    "      if (has(r.value_tip) && disp.value === String(has(r.value) ? r.value : '')) vTip = r.value_tip;\n"
    "      else if (has(r.before_tip) && has(r.before) && disp.value === String(r.before)) vTip = r.before_tip;\n"
    "    }\n"
    "    // liveDisplay's notes and 'out of date' are English keys: drawn through t.\n"
    "    var note = reverted ? '' : (disp.note || (staleNote ? N_('out of date') : ''));\n"
    "    var noteText = note ? t(note) : '';\n"
    "    if (note) noteHtml = '<span class=\"chg\">' + esc(noteText) + '</span>';\n"
    "    if (String(shownText || '').length > 18) rc += ' long';\n"
    "    var labelText = Core.rowLabel(r);\n"
    "    var label = labelText + (has(shownText) ? ' ' + shownText : '') + (note ? ', ' + noteText : '') + (reverted ? ', ' + t('reverted') : '');\n"
    "    return '<div class=\"row' + rc + open + '\" data-row=\"' + r.id + '\" data-fk=\"r:' + r.id + '\" role=\"button\" tabindex=\"0\" aria-expanded=\"' + isOpen + '\" aria-label=\"' + esc(label) + '\">' +\n"
    "      '<span class=\"ic\" data-tip=\"' + esc(TIP[kind] ? t(TIP[kind]) : '') + '\">' + icon(kind) + '</span>' +\n"
    "      '<span class=\"l\"' + (!isOpen ? ' data-tip=\"' + esc(rowTip(r)) + '\"' : '') + '><span class=\"lt\">' + esc(labelText) + '</span>' +\n"
    "      noteHtml + '</span><span class=\"v\"' + (vTip ? ' data-tip=\"' + esc(Core.tipText(vTip, r)) + '\"' : '') + '>' + value + '</span></div>' + more;\n"
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
    "      ask = '<div class=\"ask\"><span class=\"line wait\">' + esc(t('Restoring\\u2026')) + '</span></div>';\n"
    "    } else if (restoreError[g.id]) {\n"
    "      cls += ' showerr';\n"
    "      ask = '<div class=\"ask\"><span class=\"line\"><span class=\"bad\">' + esc(ht(restoreError[g.id])) + '</span><button type=\"button\" class=\"lnk no\" data-dismiss=\"' + g.id + '\" data-fk=\"d:' + g.id + '\">' + esc(t('OK')) + '</button></span></div>';\n"
    "    } else if (asking === g.id) {\n"
    "      cls += ' asking';\n"
    "      var notes = '';\n"
    "      var undoneGroups = groupsUndoneBy(g);\n"
    "      var disk = undoneGroups.some(function (x) { return x.rows.some(savedToDisk); });\n"
    "      if (disk) notes += '<span class=\"note\">' + esc(t('Files and presets saved to disk stay.')) + '</span>';\n"
    "      notes += '<span class=\"note\">' + esc(t('Your own changes since then are undone too.')) + '</span>';\n"
    "      var askText = t('Restore to {time}?', { time: g.time });\n"
    "      ask = '<div class=\"ask\" role=\"group\" aria-label=\"' + esc(askText) + '\"><span class=\"line\">' + esc(askText) + ' ' +\n"
    "        '<button type=\"button\" class=\"lnk\" data-yes=\"' + g.id + '\" data-fk=\"y:' + g.id + '\">' + esc(t('Yes')) + '</button>' +\n"
    "        '<button type=\"button\" class=\"lnk no\" data-no=\"' + g.id + '\" data-fk=\"n:' + g.id + '\">' + esc(t('No')) + '</button></span>' + notes + '</div>';\n"
    "    }\n"
    "    var act = '';\n"
    "    if (restorable && !ask) {\n"
    "      var ok = g.restore.available;\n"
    "      var tip = ok ? t('Restore the project to how it was at {time} (undoes later changes)', { time: g.time }) : ht(g.restore.why || N_('No restore point here'));\n"
    "      act = '<button type=\"button\" class=\"act tip-left' + (ok ? '' : ' off') + '\" ' + (ok ? 'data-ask=\"' + g.id + '\"' : 'aria-disabled=\"true\"') +\n"
    "        ' data-fk=\"a:' + g.id + '\" data-tip=\"' + esc(tip) + '\" aria-label=\"' + esc(tip) + '\"><svg aria-hidden=\"true\"><use href=\"#i-restore\"/></svg></button>';\n"
    "    }\n"
    "    var resText = res.text + (res.text && res.stale && res.current ? ' \\u00b7 ' + t('out of date') : '');\n"
    "    var resHtml = '<span class=\"res' + (res.stale ? ' stale' : '') + '\"' + (res.stale ? ' data-tip=\"' + esc(t('Out of date: something changed after this slice')) + '\"' : '') + '>' +\n"
    "      (res.warn ? icon('refused') : '') + esc(resText) + '</span>';\n"
    "    var title = Core.groupTitle(g);\n"
    "    return '<div class=\"' + cls + '\" data-g=\"' + g.id + '\"><div class=\"ghead\" data-toggle=\"' + g.id + '\">' +\n"
    "      '<button type=\"button\" class=\"gt\" data-toggle=\"' + g.id + '\" data-fk=\"g:' + g.id + '\" aria-expanded=\"' + open + '\" aria-label=\"' + esc(g.time + ' ' + title + (undone ? ', ' + t('undone by the restore') : '')) + '\">' +\n"
    "      '<span class=\"t\">' + esc(g.time) + '</span><span class=\"nm\" data-tip=\"' + esc(title) + '\"><span class=\"name\">' + esc(title) + '</span></span></button>' +\n"
    "      resHtml + act + ask + '</div><div class=\"body\">' + Core.orderedRows(g).map(function (r) { return rowHtml(r, g, newest && !undone); }).join('') + '</div></div>';\n"
    "  }\n"
    "\n"
    "  function restoredLineHtml() {\n"
    "    if (restoredTo === null || restoredTo === undefined) {\n"
    "      // After the user undid a restore: a faint line until the next change.\n"
    "      if (restoreUndoneAt !== null && restoreUndoneAt === maxRowId())\n"
    "        return '<div class=\"group restored\"><div class=\"ghead\"><span class=\"t\"></span><span class=\"rl\"><span class=\"name faint\">' + esc(t('Restore undone')) + '</span></span></div></div>';\n"
    "      return '';\n"
    "    }\n"
    "    var when = '';\n"
    "    groups.forEach(function (g) { if (g.id === restoredTo) when = g.time; });\n"
    "    // Column 3 holds only the transient states; the Undo link follows the text in column 2 (\"Restored to 10:31 ·\n"
    "    // Undo\"), outside .name so its hover tag is not clipped by the ellipsis.\n"
    "    var right = '';\n"
    "    if (restoreBusy === 'undo') right = '<span class=\"ask\" style=\"display:flex;grid-column:auto;padding:0\"><span class=\"line wait\">' + esc(t('Restoring\\u2026')) + '</span></span>';\n"
    "    else if (restoreError.undo) right = '<span class=\"ask\" style=\"display:flex;grid-column:auto;padding:0\"><span class=\"line\"><span class=\"bad\">' + esc(ht(restoreError.undo)) + '</span></span></span>';\n"
    "    var inline = canUndoRestore && restoreBusy === null && !restoreError.undo;\n"
    "    var line = restoreFailed ? t('Couldn\\'t restore to {time}', { time: when }) : t('Restored to {time}', { time: when });\n"
    "    return '<div class=\"group restored\"><div class=\"ghead\"><span class=\"t\"></span><span class=\"rl\"><span class=\"name\">' + esc(line) + '</span>' +\n"
    "      (inline ? '<span class=\"sep\">\\u00b7</span><button type=\"button\" class=\"lnk tip-left\" data-undo-restore=\"1\" data-fk=\"ur\" data-tip=\"' + esc(t('Go back to before the restore')) + '\">' + esc(t('Undo')) + '</button>' : '') +\n"
    "      '</span>' + right + '</div></div>';\n"
    "  }\n"
    "\n"
    "  function updateFold() {\n"
    "    var cand = shownGroups().filter(function (g) { return !isUndone(g); });\n"
    "    var allOpen = cand.length > 0 && cand.every(function (g) { return openGroups[g.id]; });\n"
    "    var f = document.getElementById('fold');\n"
    "    var foldText = allOpen ? t('Collapse all') : t('Expand all');\n"
    "    f.setAttribute('data-tip', foldText);\n"
    "    f.setAttribute('aria-label', foldText);\n"
    "    f.innerHTML = '<svg aria-hidden=\"true\"><use href=\"#i-' + (allOpen ? 'collapse' : 'expand') + '\"/></svg>';\n"
    "    f.style.visibility = shownGroups().length ? 'visible' : 'hidden';\n"
    "  }\n"
    "  function updateGear() {\n"
    "    var g = document.getElementById('gear');\n"
    "    var tip = caps.open_settings ? Core.settingsLabel(caps) : (caps.settings_hint ? ht(caps.settings_hint) : t('Settings'));\n"
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
    "  // The words outside the list: the title, and the page's language for screen readers.\n"
    "  function applyLanguage() {\n"
    "    try {\n"
    "      var ttl = document.getElementById('ttl');\n"
    "      if (ttl) ttl.textContent = t('Assistant');\n"
    "      if (document.documentElement) document.documentElement.lang = Core.language() ? Core.language().replace('_', '-') : 'en';\n"
    "    } catch (e) {}\n"
    "  }\n"
    "\n"
    "  function render() {\n"
    "    applyTheme();\n"
    "    applyLanguage();\n"
    "    hideTip();\n"
    "    var pending = focusNext;\n"
    "    var keep = pending || focusedKey();\n"
    "    groups = Core.groupRows(history);\n"
    "    var html = '';\n"
    "    if (api === 'off') {\n"
    "      html += '<div class=\"empty offnote\">' + esc(Core.offText(caps)) + (caps.open_settings\n"
    "        ? ' <button type=\"button\" data-settings=\"1\" data-fk=\"set\">' + esc(Core.settingsLink(caps)) + '</button>'\n"
    "        : (caps.settings_hint ? '<br>' + esc(ht(caps.settings_hint)) : '')) + '</div>';\n"
    "    }\n"
    "    var shown = shownGroups();\n"
    "    if (!shown.length) {\n"
    "      if (api !== 'off') html += setup && setup.apps && setup.apps.length ? setupHtml()\n"
    "        : '<div class=\"empty\">' + esc(t('When an assistant changes something, it shows up here.')) + '</div>';\n"
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
    "      return '<div class=\"sconn\"><div class=\"sstat top\"><span class=\"sdot ok\"></span><b>' + esc(st.app ? t('Connected to {app}', { app: st.app }) : t('Connected')) + '</b></div>' +\n"
    "        '<div class=\"slead\">' + esc(t('Ask it for the print you want. Its changes will appear here, each with Revert.')) + '</div></div>';\n"
    "    }\n"
    "    var app = setupCurrent();\n"
    "    var squares = setup.apps.map(function (a) {\n"
    "      return '<button type=\"button\" class=\"ssq\" data-sapp=\"' + esc(a.id) + '\" data-fk=\"sa:' + esc(a.id) + '\" aria-pressed=\"' + (a.id === app.id) + '\">' +\n"
    "        (a.icon || '') + '<span>' + esc(ht(a.name)) + '</span></button>';\n"
    "    }).join('');\n"
    "    var routes = app.routes.map(function (r, j) {\n"
    "      var act = '';\n"
    "      if (!r.off && r.act) act = setupDone['r' + j] ? '<span class=\"sdone\">' + esc(r.open ? t('Opening\\u2026') : t('Copied')) + '</span>'\n"
    "        : '<button type=\"button\" class=\"sact' + (r.primary ? '' : ' second') + '\" data-' + (r.open ? 'sopen' : 'scopy') + '=\"r' + j + '\" data-fk=\"sr' + j + '\">' + esc(ht(r.act)) + '</button>';\n"
    "      // An app whose form has separate boxes (ChatGPT: Command to launch, Arguments) gets one line per box, so nothing\n"
    "      // pasted whole lands in the wrong one. The labels are that app's own words; the values are never translated.\n"
    "      var fields = r.off || !r.fields || !r.fields.length ? '' : '<div class=\"sfields\">' + r.fields.map(function (f, k) {\n"
    "        var key = 'f' + j + '.' + k;\n"
    "        return '<span>' + esc(ht(f.label)) + '</span><code>' + esc(f.value) + '</code>' + (setupDone[key]\n"
    "          ? '<span class=\"sdone\">' + esc(t('Copied')) + '</span>'\n"
    "          : '<button type=\"button\" class=\"sact second\" data-scopy=\"' + key + '\" data-fk=\"s' + key + '\" aria-label=\"' + esc(t('Copy') + ' ' + ht(f.label)) + '\">' + esc(t('Copy')) + '</button>');\n"
    "      }).join('') + '</div>';\n"
    "      return '<div class=\"sroute' + (r.off ? ' off' : '') + '\"><b>' + esc(ht(r.label)) + '</b><small>' + esc(ht(r.hint)) + (r.also ? ' ' + esc(ht(r.also)) : '') + '</small>' + act + fields + '</div>';\n"
    "    }).join('');\n"
    "    // Every route runs the connector with uv: a host that names where to get it gets one line for that under the routes.\n"
    "    var uv = !setup.uv ? '' : '<div class=\"suv\">' + esc(t('Every app needs uv, a small free tool that runs the connector. Install it once.')) + ' ' +\n"
    "      (setupDone.uv ? '<span class=\"sdone\">' + esc(t('Opening\\u2026')) + '</span>'\n"
    "        : '<button type=\"button\" class=\"lnk\" data-suv=\"1\" data-fk=\"suv\">' + esc(t('Get uv')) + '</button>') + '</div>';\n"
    "    var appName = ht(app.where || app.name);\n"
    "    // \"your app\" (the host's word for any other app) gets whole sentences: a language with cases can't put one form of\n"
    "    // it into both sentences (\"where you use ...\" and \"waiting for ...\").\n"
    "    var anyApp = (app.where || app.name) === 'your app';\n"
    "    return '<div class=\"setup\"><div class=\"sh\">' + esc(t('Connect your AI app')) + '</div>' +\n"
    "      '<div class=\"slead\">' + esc(t('Then tell it the print you want. Every change it makes shows up here, and you can undo any of it.')) + '</div>' +\n"
    "      '<div class=\"ssqs\">' + squares + '</div><div class=\"sstep\"><span>' + esc(anyApp ? t('Set it up where you use your app') : t('Set it up where you use {app}', { app: appName })) + '</span><div class=\"sroutes\">' + routes + '</div>' + uv + '</div>' +\n"
    "      // No test question to ask: the first request from the app switches this to the connected view by itself.\n"
    "      '<div class=\"sstat\"><span class=\"sdot\"></span>' + esc(anyApp ? t('Waiting for your app\\u2026') : t('Waiting for {app}\\u2026', { app: appName })) + '</div></div>';\n"
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
    "             settings_label: c.settings_label || '', reveal: c.reveal === true, tracked_live: c.tracked_live === true,\n"
    "             language: typeof c.language === 'string' ? c.language : '' };\n"
    "    Core.setLanguage(caps.language);\n"
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
    "      else if (m.status === 'failed') { undoError[id] = m.reason || N_('Could not revert'); openRows[id] = true; }\n"
    "    } else {\n"
    "      var key = m.action === 'undo_restore' ? 'undo' : m.group;\n"
    "      if (restoreBusy === key) restoreBusy = null;\n"
    "      if (m.status === 'failed') restoreError[key === undefined ? 'undo' : key] = m.reason || N_('Could not restore');\n"
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
    "    if ((el = ancestor(target, 'data-suv'))) {\n"
    "      if (setup && setup.uv) { send({ action: 'open', url: setup.uv }); setupDone.uv = true; render(); }\n"
    "      return true;\n"
    "    }\n"
    "    if ((el = ancestor(target, 'data-scopy'))) {\n"
    "      // \"r<j>\": route j's copy; \"f<j>.<k>\": box k of route j.\n"
    "      var ck = el.getAttribute('data-scopy'), at = ck.slice(1).split('.'), cr = setupCurrent().routes[Number(at[0])] || {};\n"
    "      var text = ck.charAt(0) === 'f' ? ((cr.fields || [])[Number(at[1])] || {}).value : cr.copy;\n"
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
