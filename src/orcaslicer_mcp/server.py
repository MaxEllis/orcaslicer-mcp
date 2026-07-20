from __future__ import annotations
import asyncio
from mcp.server.fastmcp import FastMCP
from .config import load_config
from .client import OrcaClient
from .errors import ApiError, Validation, NotFound, Conflict, ConfigError
from .models import summarize_slice
from . import settings_schema
from . import placement
from .knowledge_index import search_knowledge
from .physics_check import run_checks
from .breakdown import build_breakdown
from . import notes as _notes

mcp = FastMCP("orcaslicer")


def _client() -> OrcaClient:
    try:
        return OrcaClient(load_config())
    except (RuntimeError, ValueError) as e:
        raise ConfigError(str(e))


# The fork's terminal slice states: done, error, and idle (= cancelled).
# Anything else ("slicing", or transitional states like "starting") means
# keep polling — returning early hands the caller a stale result (F1).
_TERMINAL_SLICE_STATES = frozenset({"done", "error", "idle"})


async def _wait_for_slice(c, timeout: int) -> dict:
    """Poll slice_status until it reaches a terminal state (done/error/idle) or timeout. Returns the final status dict."""
    deadline = asyncio.get_running_loop().time() + timeout
    s = await c.slice_status()
    while s.get("state") not in _TERMINAL_SLICE_STATES and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(1.0)
        s = await c.slice_status()
    return s


async def _start_slice(c) -> dict:
    """POST /slice with one retry: right after a slice completes, the fork's
    background process can transiently decline to restart (422 slice_not_started,
    surfaced by the F2 wedge detection). One short pause clears it."""
    try:
        return await c.slice()
    except Validation as e:
        if "slice_not_started" not in str(e):
            raise
        await asyncio.sleep(1.5)
        return await c.slice()


def _err(e: ApiError) -> dict:
    out = {"error": str(e)}
    if isinstance(e, Validation):
        out["errors"] = e.errors
    return out


@mcp.tool()
async def get_status() -> dict:
    """App/project/preset status, dirty keys, slice validity, and whether a slice is running."""
    try:
        async with _client() as c:
            return await c.get_status()
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_config(keys: list[str] | None = None) -> dict:
    """Read merged config values (optionally filtered to `keys`)."""
    try:
        async with _client() as c:
            return {"config": await c.get_config(keys)}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def set_config(changes: dict) -> dict:
    """Apply config changes atomically. Returns {applied, errors}. On any invalid key, nothing is applied."""
    try:
        async with _client() as c:
            return await c.put_config(changes)
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def slice() -> dict:
    """Start slicing the current plate. Returns started / already_valid / conflict."""
    try:
        async with _client() as c:
            return await c.slice()
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_slice_status() -> dict:
    """Current/last slice state, stats, and warnings."""
    try:
        async with _client() as c:
            return summarize_slice(await c.slice_status())
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_slice_warnings() -> dict:
    """Just the warnings/errors from the last (or current) slice, plus validity - the
    fast 'did anything go wrong' check and the way to confirm a fix cleared.

    NOTE: only as complete as the API exposes. On the current fork build this may report
    valid with an empty warnings list even when the GUI shows a plate-boundary toast -
    the fork must populate the plater warning list (tracked as the fork batch). Once it
    does, this reports the real warnings with no change here."""
    try:
        async with _client() as c:
            st = await c.slice_status()
            status = await c.get_status()
        s = summarize_slice(st)
        return {"state": s["state"], "valid": status.get("slice_result_valid"),
                "warnings": s["warnings"], "errors": st.get("errors", []),
                "message": s["message"]}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_slice_breakdown() -> dict:
    """Per-feature breakdown of the last slice + a stateless predicted-vs-observed flow check.

    Returns per-role time/filament + speed/flow ranges, global time-weighted metric
    distributions, per-layer aggregates, and a prediction_check flagging where the profile's
    speed was silently throttled at the flow ceiling ('clamped'). Answers 'which feature is
    the time hog' directly instead of by trial slicing.

    Degrades to {"available": false, "reason": ...} on fork builds that don't emit the
    breakdown, or when there is no valid slice. [needs fork breakdown build]"""
    try:
        async with _client() as c:
            status = await c.slice_status()
            cfg = await c.get_config(None)
        return build_breakdown(status, cfg)
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def cancel_slice() -> dict:
    """Abort a running slice, or unwedge a stale 'slicing' state (e.g. after an
    object outside the bed). Safe when idle. [needs F2 build]"""
    try:
        async with _client() as c:
            return await c.cancel_slice()
    except ApiError as e:
        return _m4_err(e, "the F2 fork fix")


@mcp.tool()
async def slice_and_wait(timeout: int = 300) -> dict:
    """Slice (or reuse a valid result) and wait for completion; return final stats + warnings."""
    try:
        async with _client() as c:
            started = await _start_slice(c)
            if started.get("already_valid"):
                return summarize_slice(await c.slice_status())
            return summarize_slice(await _wait_for_slice(c, timeout))
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def apply_and_slice(changes: dict) -> dict:
    """Apply config changes, then slice and report the resulting stats/warnings."""
    try:
        async with _client() as c:
            applied = await c.put_config(changes)
            started = await _start_slice(c)
            if not started.get("already_valid"):
                await _wait_for_slice(c, 300)
            result = summarize_slice(await c.slice_status())
            return {"applied": applied.get("applied", []),
                    "errors": applied.get("errors", {}), "result": result}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def compare_settings(key: str, values: list, extra: dict | None = None) -> dict:
    """For each value of `key`, slice and collect stats/warnings; restore the original when done.

    Non-destructive: the original value of `key` is put back even on error.
    """
    try:
        async with _client() as c:
            snapshot_keys = [key] + list((extra or {}).keys())
            originals = await c.get_config(snapshot_keys)
            rows = []
            restore_error = None
            try:
                for v in values:
                    row = {"value": v, "stats": None, "warnings": [], "error": None}
                    try:
                        await c.put_config({key: v, **(extra or {})})
                        started = await _start_slice(c)
                        if not started.get("already_valid"):
                            await _wait_for_slice(c, 300)
                        s = summarize_slice(await c.slice_status())
                        row["stats"] = s["stats"]
                        row["warnings"] = s["warnings"]
                    except ApiError as e:
                        row["error"] = str(e)
                    rows.append(row)
            finally:
                if originals:
                    try:
                        await c.put_config(originals)
                    except ApiError as e:
                        restore_error = str(e)  # preserve collected rows even if restore fails
            result = {"key": key, "rows": rows}
            if restore_error is not None:
                result["restore_error"] = restore_error
            return result
    except ApiError as e:
        return _err(e)


def _m4_err(e: ApiError, milestone: str) -> dict:
    # Only a route-level 404 means the build lacks the capability; a resource-level
    # 404 (unknown_object / unknown_preset / missing file) must keep its message (F7).
    if isinstance(e, NotFound) and e.route_missing:
        return {"error": f"not available on this OrcaSlicer build (needs {milestone})"}
    return _err(e)


def _m4a_err(e: ApiError) -> dict:
    return _m4_err(e, "M4a")


def _m4b_err(e: ApiError) -> dict:
    return _m4_err(e, "M4b")


def _m4c_err(e: ApiError) -> dict:
    return _m4_err(e, "M4c")


@mcp.tool()
async def list_objects() -> dict:
    """List objects on the current plate: id (stable), name, size_mm, and transform (offset/rotation/scale). [needs M4b]"""
    try:
        async with _client() as c:
            return await c.get_objects()
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def set_object_config(object_id: int, changes: dict) -> dict:
    """Set per-object config overrides on an object by id, e.g. {"wall_loops": 4, "sparse_infill_density": "30%"}. Atomic (nothing applied if any key is invalid). [needs M4c]"""
    try:
        async with _client() as c:
            return await c.set_object_config(object_id, changes)
    except ApiError as e:
        return _m4c_err(e)


@mcp.tool()
async def duplicate_object(object_id: int) -> dict:
    """Duplicate an object on the plate by id (adds a copy, offset from the original). [needs M4b]"""
    try:
        async with _client() as c:
            return await c.duplicate_object(object_id)
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def delete_object(object_id: int) -> dict:
    """Delete an object from the current plate by its id. [needs M4b]"""
    try:
        async with _client() as c:
            return await c.delete_object(object_id)
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def transform_object(object_id: int, translate: list[float] | None = None,
                           rotate: list[float] | None = None,
                           scale: list[float] | None = None) -> dict:
    """Move/rotate/scale an object by id. translate=[dx,dy,dz] mm (relative), rotate=[rx,ry,rz] degrees (relative), scale=[sx,sy,sz] absolute factor. Provide at least one. [needs M4b]"""
    try:
        async with _client() as c:
            return await c.transform_object(object_id, translate, rotate, scale)
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def arrange_plate() -> dict:
    """Auto-arrange all objects on the plate (async job; poll get_job_status until idle). [needs M4b]"""
    try:
        async with _client() as c:
            return await c.arrange()
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def auto_orient() -> dict:
    """Auto-orient all objects for printing (async job; poll get_job_status until idle). [needs M4b]"""
    try:
        async with _client() as c:
            return await c.orient()
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def get_job_status() -> dict:
    """Whether the plate job worker is idle (poll after arrange_plate/auto_orient). [needs M4b]"""
    try:
        async with _client() as c:
            return await c.job_status()
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def watch_events(seconds: int = 10) -> dict:
    """Collect live events (slice.*/config.changed/project.opened) over a bounded window."""
    try:
        async with _client() as c:
            return {"events": await c.collect_events(seconds=seconds)}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def find_config_keys(substring: str) -> dict:
    """Find config keys containing `substring` (helps discover among the ~600 keys)."""
    try:
        async with _client() as c:
            cfg = await c.get_config(None)
            return {"keys": sorted(k for k in cfg if substring in k)}
    except ApiError as e:
        return _err(e)


_PLATE_CFG_KEYS = ["printable_area", "bed_exclude_area", "skirt_loops", "skirt_distance",
                   "skirt_height", "brim_type", "brim_width", "brim_object_gap",
                   "extruder_clearance_radius", "draft_shield"]


@mcp.tool()
async def diagnose_plate() -> dict:
    """One-call plate diagnosis: app/slice status, objects on the plate, bed + active
    skirt/brim/clearance settings, and the last slice's warnings - so you don't have to
    chain status->objects->config. Start here for 'why won't this slice / fit'.

    Slice warnings are only as complete as the fork exposes today (see get_slice_warnings).
    For a 'does it fit the bed' estimate, pair with check_placement."""
    try:
        async with _client() as c:
            status = await c.get_status()
            try:
                objs = await c.get_objects()
            except NotFound:
                objs = {"count": 0, "objects": [], "note": "objects API needs M4b build"}
            cfg = await c.get_config(_PLATE_CFG_KEYS)
            sl = summarize_slice(await c.slice_status())
        return {"status": status, "objects": objs, "adhesion_bed": cfg,
                "slice": {"state": sl["state"], "warnings": sl["warnings"],
                          "valid": status.get("slice_result_valid")}}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def check_placement() -> dict:
    """Estimate whether every object (plus its skirt/brim ring) fits inside the printable
    area. Returns per-object fit, expanded first-layer bbox, per-edge clearance (mm), and
    overflow.

    APPROXIMATE: uses the object footprint from size+offset, not the sliced toolpath (skirt
    arcs, half-line-width, travel/wipe excluded); single-instance objects only. At ~mm
    margins the true verdict needs get_slice_warnings - this is a fast first-pass. [needs M4b]"""
    try:
        async with _client() as c:
            objs = await c.get_objects()
            cfg = await c.get_config(placement.CFG_KEYS)
        return placement.check_placement(objs, cfg)
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def consult(query: str) -> dict:
    """Retrieve curated slicing knowledge + saved context notes for a topic,
    symptom, or intent. ALWAYS call before deriving or changing settings for
    a user goal. Composes principles per situation - never returns preset
    bundles. Falls back to find_config_keys/web search if empty.

    When recommending, present 2-3 concrete options quantified with
    predicted print time and filament mass from real slice results (slice +
    status tools) - never adjectives alone."""
    chunks = [{"file": c.relpath, "title": c.title, "content": c.body}
              for c in search_knowledge(query)]
    return {"chunks": chunks, "notes": _notes.search_notes(query)}


@mcp.tool()
async def check_profile_physics(changes: dict | None = None) -> dict:
    """Deterministic pre-save gate: fetches the live config, overlays optional
    proposed `changes`, and runs flow/temperature/geometry/cooling math.
    RUN THIS BEFORE save_preset. verdict=blocked means DO NOT SAVE."""
    try:
        async with _client() as c:
            cfg = await c.get_config(None)
    except ApiError as e:
        return _err(e)
    if changes:
        cfg = dict(cfg) | {k: str(v) for k, v in changes.items()}
    results = run_checks(cfg)
    grouped = {"pass": [], "warn": [], "fail": []}
    for r in results:
        grouped[r.status].append({"name": r.name, "detail": r.detail})
    grouped["verdict"] = "blocked" if grouped["fail"] else ("warnings" if grouped["warn"] else "ok")
    return grouped


@mcp.tool()
async def remember(note: str, scope: str) -> dict:
    """Persist a context fact for future sessions. scope: 'machine:<printer>/<filament>',
    'user', or 'project:<name>'. Local plain files; user-readable and deletable."""
    return {"saved": str(_notes.append_note(note, scope))}


@mcp.tool()
def describe_setting(key: str) -> dict:
    """Authoritative definition of one OrcaSlicer setting: label, tooltip, type, unit, range, enum values, default. Offline; works even when OrcaSlicer is not running."""
    rec = settings_schema.describe(key)
    if rec is None:
        return {"error": "unknown_setting", "key": key}
    return rec


@mcp.tool()
def search_settings(query: str, limit: int = 25) -> dict:
    """Search settings by keyword across key/label/tooltip; returns compact matches (key, label, category, short tooltip), ranked key/label first. Offline."""
    return {"results": settings_schema.search(query, limit)}


@mcp.tool()
async def load_model(path: str) -> dict:
    """Load a model file (path on the OrcaSlicer host) onto the current plate. [needs M4a]"""
    try:
        async with _client() as c:
            return await c.load_model(path)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def select_preset(type: str, name: str) -> dict:
    """Select a named preset. type = print|filament|printer. [needs M4a]"""
    try:
        async with _client() as c:
            return await c.select_preset(type, name)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def save_preset(type: str, name: str, detach: bool = False) -> dict:
    """Save the currently edited settings as a named user preset (create or update,
    visible in the GUI immediately). type = print|filament|printer. detach=True saves
    it standalone instead of inheriting the current base preset. [needs preset/save]

    Run check_profile_physics first; do not save when verdict=blocked."""
    try:
        async with _client() as c:
            return await c.save_preset(type, name, detach)
    except ApiError as e:
        return _m4a_err(e)


_PRESET_TYPES = ("print", "filament", "printer")


def _filter_presets(presets: dict, ptype: str | None, include_system: bool) -> dict:
    """F12 filter: unless include_system, keep only user presets plus whatever is
    currently selected (a selected system preset is the active config, so it stays
    visible). Reports how many system presets were hidden."""
    cats = [ptype] if ptype else list(_PRESET_TYPES)
    out: dict = {}
    hidden = 0
    for cat in cats:
        items = presets.get(cat, []) or []
        if include_system:
            kept = items
        else:
            kept = [p for p in items if not p.get("system") or p.get("selected")]
        hidden += len(items) - len(kept)
        out[cat] = kept
    out["hidden_system"] = hidden
    return out


@mcp.tool()
async def list_presets(type: str | None = None, include_system: bool = False) -> dict:
    """List print/filament/printer presets with system/selected/visible flags.

    F12: by default returns only USER presets plus whatever is currently SELECTED -
    the built-in system presets are ~400 entries of noise. Pass include_system=True
    for the full list, and/or type='print'|'filament'|'printer' to restrict to one
    category. `hidden_system` reports how many system presets were filtered out.
    [needs preset/save build]"""
    if type is not None and type not in _PRESET_TYPES:
        return {"error": "invalid_type", "type": type, "valid": list(_PRESET_TYPES)}
    try:
        async with _client() as c:
            presets = await c.get_presets()
    except ApiError as e:
        return _m4a_err(e)
    return _filter_presets(presets, type, include_system)


@mcp.tool()
async def set_layer_height(object_id: int, mode: str, quality: float = 0.5) -> dict:
    """Variable layer height for one object. mode='adaptive' (quality 0..1, higher = finer
    detail) generates an adaptive profile; mode='reset' (aliases: 'default', 'none')
    restores uniform layers. [needs M4c build]"""
    # F6: the fork only understands adaptive|reset; accept the spellings that
    # were documented for the clear path instead of bouncing them as unknown_mode.
    if mode in ("default", "none"):
        mode = "reset"
    try:
        async with _client() as c:
            return await c.set_layer_height(object_id, mode, quality)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def set_height_range(object_id: int, min_z: float | None = None, max_z: float | None = None,
                           layer_height: float | None = None, clear: bool = False) -> dict:
    """Set a per-height-band layer height on an object (e.g. 0-5mm at 0.1mm). Same exact
    range again = update; clear=True removes all ranges. [needs M4c build]"""
    try:
        async with _client() as c:
            return await c.set_height_range(object_id, min_z, max_z, layer_height, clear)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def get_preset_config(type: str, name: str) -> dict:
    """Read the full settings of a named preset without selecting it.
    type = print|filament|printer. [needs preset-CRUD build]"""
    try:
        async with _client() as c:
            return await c.get_preset_config(type, name)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def delete_preset(type: str, name: str) -> dict:
    """Delete a USER preset (system presets and the currently-selected one are refused).
    type = print|filament|printer. [needs preset-CRUD build]"""
    try:
        async with _client() as c:
            return await c.delete_preset(type, name)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def edit_preset(type: str, name: str, changes: dict) -> dict:
    """Edit a named preset's settings and persist them: selects it, applies the
    changes atomically, saves under the same name. [needs preset/save build]"""
    try:
        async with _client() as c:
            await c.select_preset(type, name)
            applied = await c.put_config(changes)
            if applied.get("errors"):
                return {"error": "invalid_keys", "errors": applied["errors"]}
            saved = await c.save_preset(type, name)
            return {"preset": name, "applied": applied.get("applied", []), "saved": saved}
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def rename_preset(type: str, old_name: str, new_name: str) -> dict:
    """Rename a USER preset: save a copy under the new name, select it, delete the old.
    [needs preset-CRUD build]"""
    try:
        async with _client() as c:
            await c.select_preset(type, old_name)
            await c.save_preset(type, new_name)
            await c.select_preset(type, new_name)
            deleted = await c.delete_preset(type, old_name)
            return {"renamed": old_name, "to": new_name, "deleted": deleted}
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def get_gcode() -> dict:
    """Retrieve the last successful slice's G-code as text. [needs M4a]"""
    try:
        async with _client() as c:
            data = await c.get_gcode()
            return {"bytes": len(data), "gcode": data.decode("utf-8", errors="replace")}
    except Conflict:
        return {"error": "not_sliced"}
    except ApiError as e:
        return _m4a_err(e)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
