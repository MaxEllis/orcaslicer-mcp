from __future__ import annotations
import asyncio
import re
import datetime
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Annotated
from mcp.server.fastmcp import FastMCP, Image
from pydantic import Field
from mcp.types import ToolAnnotations
from .config import load_config
from .client import OrcaClient
from .errors import ApiError, Validation, NotFound, Conflict, ConfigError
from .models import summarize_slice
from . import settings_schema
from . import placement
from .knowledge_index import load_knowledge, search_knowledge
from .physics_check import run_checks
from .breakdown import build_breakdown
from .compare import compute_comparison
from . import notes as _notes
from . import outcomes as _outcomes

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
    """Snapshot of the current OrcaSlicer session: app and project info, the active
    print/filament/printer presets with which of their keys are modified (dirty), whether the
    last slice is still valid, and whether a slice is running. Read-only.

    Call it first to orient before slicing or editing, to see which settings drift from their
    preset, or to check slice_result_valid before trusting earlier stats."""
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
async def set_config(
    changes: Annotated[dict, Field(description=(
        "Map of OrcaSlicer config key to new value, e.g. "
        "{'layer_height': 0.2, 'sparse_infill_density': '15%'}. Values must match each "
        "setting's type; percent settings take strings like '15%'. Discover valid keys "
        "with search_settings, find_config_keys, or describe_setting."))],
) -> dict:
    """Apply config changes to the active project as unsaved overrides, atomically: if any
    key is invalid the whole batch is rejected and nothing changes. Returns {applied, errors}.

    Overrides show as modified in get_status, are not written to any preset file, and revert
    if the preset is reselected; call save_preset to persist them. Each apply invalidates the
    last slice, so re-slice afterwards. It does not run the physics gate, so for temperature,
    speed, acceleration, or flow keys run check_profile_physics before trusting the result. To
    edit a stored preset rather than the live project, use edit_preset."""
    try:
        async with _client() as c:
            return await c.put_config(changes)
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def slice() -> dict:
    """Start slicing the current plate in the background and return immediately, without waiting
    for the result. The reply is 'started' (a slice began), 'already_valid' (the plate is
    unchanged and the last result still holds), or a conflict if a slice is already running.

    Fire-and-forget: poll get_slice_status for progress and stats, or cancel_slice to stop it.
    Prefer slice_and_wait when you want the finished stats back in one call."""
    try:
        async with _client() as c:
            return await c.slice()
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_slice_status() -> dict:
    """State of the current or most recent slice: state (slicing, done, error, or idle), stats
    (print time and filament use when done), and any warnings or errors. Read-only.

    Poll this after slice to follow progress and read the result; 'idle' means no slice has run
    or it was cancelled. For only the pass/fail warnings use get_slice_warnings; for a
    per-feature time and filament breakdown use get_slice_breakdown."""
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
    breakdown, or when there is no valid slice."""
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
    object outside the bed). Safe when idle."""
    try:
        async with _client() as c:
            return await c.cancel_slice()
    except ApiError as e:
        return _m4_err(e, "the F2 fork fix")


@mcp.tool()
async def slice_and_wait(
    timeout: Annotated[int, Field(description=(
        "Maximum seconds to wait for the slice to finish before returning the last known "
        "state. Default 300; raise it for large or textured plates that slice slowly."))] = 300,
) -> dict:
    """Slice the current plate and block until it finishes, then return the final stats and
    warnings in one call. If the plate is already sliced and unchanged, it returns the existing
    result without re-slicing.

    This is the usual way to slice when you want the outcome immediately. For a non-blocking
    start, use slice then poll get_slice_status; to sweep one setting across values, use
    compare_settings."""
    try:
        async with _client() as c:
            started = await _start_slice(c)
            if started.get("already_valid"):
                return summarize_slice(await c.slice_status())
            return summarize_slice(await _wait_for_slice(c, timeout))
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def apply_and_slice(
    changes: Annotated[dict, Field(description=(
        "Map of OrcaSlicer config key to new value to apply before slicing, e.g. "
        "{'layer_height': 0.2}. Same format and validation as set_config; discover keys with "
        "search_settings or find_config_keys."))],
) -> dict:
    """Apply config overrides and then slice in one step, returning {applied, errors, result}
    with the resulting stats and warnings. The changes are atomic (any invalid key rejects the
    whole batch) and unsaved, exactly like set_config, so they revert if the preset is reselected.

    Use this to test the effect of a tweak in a single call. Use set_config then slice_and_wait
    to keep the steps separate, or compare_settings to try several values of one key."""
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


@mcp.tool()
async def compare_slices(variants: list[dict], baseline: str | None = None,
                         detail: bool = False, timeout: int = 300) -> dict:
    """Slice the current plate under several named variants and compare the cost of each.

    Each variant is {"name": str, "changes": {setting: value}}; changes={} means the
    current config as-is (a natural baseline row). Applies each variant over the ORIGINAL
    config (resetting between variants, so they don't stack), slices it, then restores your
    config exactly as it was - nothing is left changed (slice validity is left false, as
    after any un-resliced edit).

    Returns a ready-to-relay `headline` and `table_markdown`, plus structured `variants`.
    All deltas and percentages are ALREADY computed and rounded against `baseline`
    (defaults to the changes={} variant, else the first) - relay them as given rather than
    recomputing. `recommended` names one pick; `recommended_is_dominant` says whether it
    beats every variant on every axis (time, filament, warnings) or is only the fastest
    warning-free option amid a genuine trade-off (`tradeoff` then names the frontier).

    Each variant is a full slice (minutes); capped at 8. Set detail=True only when a
    per-feature (wall/infill/support) split is wanted - it grows the response ~N x. With
    more than ~5 variants, lead with the recommendation and the extremes, not all rows.
    """
    if not isinstance(variants, list) or len(variants) < 2:
        return {"error": "need_at_least_two_variants"}
    if len(variants) > 8:
        return {"error": "too_many_variants", "max": 8, "given": len(variants)}
    names = [v.get("name") for v in variants]
    if any(not n for n in names):
        return {"error": "each_variant_needs_a_name"}
    if len(set(names)) != len(names):
        return {"error": "variant_names_must_be_unique"}
    if baseline is not None and baseline not in names:
        return {"error": "unknown_baseline", "baseline": baseline, "names": names}

    try:
        async with _client() as c:
            union = sorted({k for v in variants for k in (v.get("changes") or {})})
            snapshot = await c.get_config(union) if union else {}
            results: list[dict] = []
            restore_error = None
            try:
                for v in variants:
                    changes = v.get("changes") or {}
                    r = {"name": v["name"], "changes": changes, "time_s": None,
                         "filament_g": None, "warnings": [], "valid": False,
                         "error": None, "roles": None}
                    try:
                        if snapshot:
                            await c.put_config(snapshot)  # reset to baseline so variants don't stack
                        if changes:
                            applied = await c.put_config(changes)
                            if applied.get("errors"):
                                r["error"] = "invalid_keys"
                                r["errors"] = applied["errors"]
                                results.append(r)
                                continue
                        started = await _start_slice(c)
                        if not started.get("already_valid"):
                            await _wait_for_slice(c, timeout)
                        st = await c.slice_status()
                        s = summarize_slice(st)
                        stats = s.get("stats") or {}
                        status = await c.get_status()
                        r["time_s"] = stats.get("estimated_time_seconds")
                        r["filament_g"] = stats.get("filament_used_g")
                        r["warnings"] = s.get("warnings") or []
                        r["valid"] = bool(status.get("slice_result_valid"))
                        if s.get("state") == "error" or r["time_s"] is None:
                            r["error"] = r["error"] or (s.get("message") or "slice_failed")
                        if detail:
                            r["roles"] = build_breakdown(st, await c.get_config(None)).get("roles")
                    except ApiError as e:
                        r["error"] = str(e)
                    results.append(r)
            finally:
                if snapshot:
                    try:
                        await c.put_config(snapshot)
                    except ApiError as e:
                        restore_error = str(e)
    except ApiError as e:
        return _err(e)

    out = compute_comparison(results, baseline, detail)
    out["restored"] = restore_error is None
    if restore_error is not None:
        out["restore_error"] = restore_error
    return out


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
    """List objects on the current plate: id (stable), name, size_mm, and transform (offset/rotation/scale)."""
    try:
        async with _client() as c:
            return await c.get_objects()
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def set_object_config(object_id: int, changes: dict) -> dict:
    """Set per-object config overrides on an object by id, e.g. {"wall_loops": 4, "sparse_infill_density": "30%"}. Atomic (nothing applied if any key is invalid)."""
    try:
        async with _client() as c:
            return await c.set_object_config(object_id, changes)
    except ApiError as e:
        return _m4c_err(e)


@mcp.tool()
async def duplicate_object(object_id: int) -> dict:
    """Duplicate an object on the plate by id (adds a copy, offset from the original)."""
    try:
        async with _client() as c:
            return await c.duplicate_object(object_id)
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def delete_object(
    object_id: Annotated[int, Field(description=(
        "Integer id of the object to remove, taken from the 'id' field of list_objects "
        "(not the array index or the file name)."))],
) -> dict:
    """Remove one object from the current plate by id. This is permanent within the session
    and cannot be undone through the API; the other objects keep their ids.

    Call list_objects first to get the id. Deleting leaves the last slice invalid, so re-slice
    afterwards. To drop just one copy made with duplicate_object, pass that copy's id."""
    try:
        async with _client() as c:
            return await c.delete_object(object_id)
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def transform_object(object_id: int, translate: list[float] | None = None,
                           rotate: list[float] | None = None,
                           scale: list[float] | None = None) -> dict:
    """Move/rotate/scale an object by id. translate=[dx,dy,dz] mm (relative), rotate=[rx,ry,rz] degrees (relative), scale=[sx,sy,sz] absolute factor. Provide at least one."""
    try:
        async with _client() as c:
            return await c.transform_object(object_id, translate, rotate, scale)
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def arrange_plate() -> dict:
    """Auto-arrange all objects on the plate (async job; poll get_job_status until idle)."""
    try:
        async with _client() as c:
            return await c.arrange()
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def auto_orient() -> dict:
    """Auto-orient all objects for printing (async job; poll get_job_status until idle)."""
    try:
        async with _client() as c:
            return await c.orient()
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def get_job_status() -> dict:
    """Whether the plate's background job worker is idle or still running. Read-only.

    arrange_plate and auto_orient start async jobs; poll this until it reports idle before you
    read object positions or slice, so you act on the settled layout rather than a mid-move
    state."""
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
    margins the true verdict needs get_slice_warnings - this is a fast first-pass."""
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
def describe_setting(
    key: Annotated[str, Field(description=(
        "Exact OrcaSlicer config key, e.g. 'layer_height' or 'sparse_infill_density'. Find keys "
        "with search_settings or find_config_keys. Unknown keys return an error."))],
) -> dict:
    """Authoritative definition of one OrcaSlicer setting: label, tooltip, type, unit, valid
    range, enum values, and default. Read-only and offline, so it works even when OrcaSlicer is
    not running.

    Use it to learn a setting's exact type and allowed values before writing it with set_config
    or edit_preset. To find candidate keys by keyword first, use search_settings."""
    rec = settings_schema.describe(key)
    if rec is None:
        return {"error": "unknown_setting", "key": key}
    return rec


@mcp.tool()
def search_settings(query: str, limit: int = 25) -> dict:
    """Search settings by keyword across key/label/tooltip; returns compact matches (key, label, category, short tooltip), ranked key/label first. Offline."""
    return {"results": settings_schema.search(query, limit)}


# --- MCP resources: the offline reference data, addressable as context ---
# All of these read the packaged settings schema / knowledge base, so they
# work even when OrcaSlicer is not running.


@mcp.resource("orca://knowledge", title="Slicing knowledge index",
              description="Index of the curated slicing knowledge base shipped with this server; each entry lists its orca://knowledge/{slug} URI.")
def knowledge_index_resource() -> str:
    lines = ["# Curated slicing knowledge", ""]
    for c in load_knowledge():
        slug = c.relpath[:-3].replace("/", "__") if c.relpath.endswith(".md") else c.relpath.replace("/", "__")
        topics = f" (topics: {', '.join(c.topics)})" if c.topics else ""
        lines.append(f"- orca://knowledge/{slug} — {c.title}{topics}")
    return "\n".join(lines)


@mcp.resource("orca://knowledge/{slug}", title="Slicing knowledge chunk",
              description="One knowledge chunk by slug (relpath with '/' as '__', no .md); see orca://knowledge for the index.")
def knowledge_chunk_resource(slug: str) -> str:
    for c in load_knowledge():
        rel = c.relpath[:-3] if c.relpath.endswith(".md") else c.relpath
        if rel.replace("/", "__") == slug:
            return c.body
    raise ValueError(f"unknown knowledge slug: {slug}")


@mcp.resource("orca://setting/{key}", title="Setting definition",
              description="Authoritative definition of one OrcaSlicer setting (label, tooltip, type, unit, range, enum, default), from OrcaSlicer's own source.")
def setting_resource(key: str) -> str:
    import json as _json
    rec = settings_schema.describe(key)
    if rec is None:
        raise ValueError(f"unknown setting: {key}")
    return _json.dumps(rec, indent=2)


# --- MCP prompts: guided workflows that encode this server's intended use ---


@mcp.prompt(name="slice-a-model", title="Slice a model, safely",
            description="Guided load -> check -> slice -> read-back workflow for one model file.")
def prompt_slice_a_model(model_path: str) -> str:
    return (
        f"Slice {model_path} in OrcaSlicer via the orcaslicer tools, step by step:\n"
        "1. get_status — confirm OrcaSlicer is reachable and note the selected presets.\n"
        "2. list_objects — see what is already on the plate before adding anything.\n"
        f"3. load_model with path {model_path}, then check_placement; fix placement with "
        "auto_orient / arrange_plate / transform_object if it reports problems.\n"
        "4. consult with the model's material and purpose before touching settings.\n"
        "5. slice_and_wait, then get_slice_warnings and get_slice_breakdown; report "
        "print time, filament mass, and any warnings. Use render_plate to show the result.\n"
        "Do not start a print or change temperatures without asking me first."
    )


@mcp.prompt(name="optimize-print-time", title="Optimize print time",
            description="Data-driven print-time reduction from a real slice breakdown, never adjectives alone.")
def prompt_optimize_print_time(constraints: str = "") -> str:
    extra = f"\nMy constraints: {constraints}" if constraints else ""
    return (
        "Reduce the print time of the current plate, driven by data:\n"
        "1. slice_and_wait (if no valid slice), then get_slice_breakdown — identify which "
        "feature roles actually dominate time.\n"
        "2. consult with 'print time optimization' plus the dominant roles; only propose "
        "levers the knowledge base or breakdown supports.\n"
        "3. Apply candidate changes with set_config, then check_profile_physics — drop "
        "anything blocked.\n"
        "4. Re-slice and quantify: present 2-3 options as minutes and grams versus the "
        "baseline, with the quality trade-off of each. Do not save presets unless I say so."
        + extra
    )


@mcp.prompt(name="edit-preset-safely", title="Edit a preset, safely",
            description="Preset change with the physics gate: consult -> set_config -> check_profile_physics -> save_preset.")
def prompt_edit_preset_safely(change: str) -> str:
    return (
        f"I want this preset change: {change}\n"
        "Follow the safe path — never edit_preset directly for physics-relevant keys:\n"
        "1. consult and describe_setting for every key involved; confirm units and ranges.\n"
        "2. Apply with set_config on the live config first.\n"
        "3. check_profile_physics — if verdict is blocked, stop and show me why; if "
        "warnings, explain them before proceeding.\n"
        "4. Only then save_preset, and confirm with get_status that nothing is left dirty."
    )


@mcp.tool()
async def load_model(path: str) -> dict:
    """Load a model file (path on the OrcaSlicer host) onto the current plate.
    Accepts .stl/.obj/.3mf, plus .step/.stp on fork v2.3.2-mcp.3+. Large STEP files
    can take a minute to tessellate; the call waits."""
    try:
        async with _client() as c:
            return await c.load_model(path)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def select_preset(
    type: Annotated[str, Field(description=(
        "Which preset group to switch: 'print' (process/quality), 'filament' (material), "
        "or 'printer' (machine)."))],
    name: Annotated[str, Field(description=(
        "Exact name of an existing preset in that group, as returned by list_presets "
        "(e.g. '0.20mm Standard'). Unknown names are rejected."))],
) -> dict:
    """Make the named preset the active one for its group (print, filament, or printer).

    Selecting a preset discards unsaved set_config overrides and reverts settings to the
    preset's stored values, so it is also the canonical way to reset dirty config; it leaves
    the last slice invalid, so re-slice afterwards. Use list_presets for valid names, and
    save_preset first if unsaved edits should survive the switch."""
    try:
        async with _client() as c:
            return await c.select_preset(type, name)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def save_preset(type: str, name: str, detach: bool = False) -> dict:
    """Save the currently edited settings as a named user preset (create or update,
    visible in the GUI immediately). type = print|filament|printer. detach=True saves
    it standalone instead of inheriting the current base preset.

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
    category. `hidden_system` reports how many system presets were filtered out."""
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
    restores uniform layers."""
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
async def set_height_range(
    object_id: Annotated[int, Field(description="Integer id of the target object, from list_objects.")],
    min_z: Annotated[float | None, Field(description="Lower Z bound of the band in mm (object-relative). Required unless clear=True.")] = None,
    max_z: Annotated[float | None, Field(description="Upper Z bound of the band in mm. Required unless clear=True.")] = None,
    layer_height: Annotated[float | None, Field(description="Layer height in mm to use within the band, e.g. 0.1. Required unless clear=True.")] = None,
    clear: Annotated[bool, Field(description="If True, remove all height-range overrides on the object and ignore the z and layer_height args.")] = False,
) -> dict:
    """Override the layer height over a Z band of one object (e.g. 0 to 5 mm printed at 0.1 mm
    for finer detail near the base). Passing the same min_z and max_z again updates that band's
    height; clear=True removes every band on the object.

    Bands are per-object and invalidate the last slice, so re-slice afterwards. For a single
    height across the whole object use set_layer_height instead. Get the id from list_objects."""
    try:
        async with _client() as c:
            return await c.set_height_range(object_id, min_z, max_z, layer_height, clear)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def get_preset_config(type: str, name: str) -> dict:
    """Read the full settings of a named preset without selecting it.
    type = print|filament|printer."""
    try:
        async with _client() as c:
            return await c.get_preset_config(type, name)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def delete_preset(type: str, name: str) -> dict:
    """Delete a USER preset (system presets and the currently-selected one are refused).
    type = print|filament|printer."""
    try:
        async with _client() as c:
            return await c.delete_preset(type, name)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def edit_preset(type: str, name: str, changes: dict) -> dict:
    """Edit a named preset's settings and persist them: selects it, applies the
    changes atomically, saves under the same name. Runs the check_profile_physics
    gate first (F15) and refuses with error=physics_blocked if the changes would
    INTRODUCE a failing physics check (pre-existing failures do not block
    unrelated edits)."""
    try:
        async with _client() as c:
            await c.select_preset(type, name)
            cfg = await c.get_config(None)
            overlay = dict(cfg) | {k: str(v) for k, v in changes.items()}
            fails_before = {r.name for r in run_checks(cfg) if r.status == "fail"}
            fails_after = [r for r in run_checks(overlay) if r.status == "fail"]
            new_fails = [r for r in fails_after if r.name not in fails_before]
            if new_fails:
                return {"error": "physics_blocked", "preset": name,
                        "fails": [{"name": r.name, "detail": r.detail} for r in new_fails],
                        "hint": "these changes introduce a physics failure; adjust them or "
                                "inspect with check_profile_physics(changes)"}
            applied = await c.put_config(changes)
            if applied.get("errors"):
                return {"error": "invalid_keys", "errors": applied["errors"]}
            saved = await c.save_preset(type, name)
            return {"preset": name, "applied": applied.get("applied", []), "saved": saved}
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def rename_preset(
    type: Annotated[str, Field(description="Preset group: 'print', 'filament', or 'printer'.")],
    old_name: Annotated[str, Field(description="Current name of the user preset to rename, as shown by list_presets.")],
    new_name: Annotated[str, Field(description="New name for the preset. Should not collide with an existing preset of the same type.")],
) -> dict:
    """Rename a user preset by copying it to new_name, selecting the copy, and deleting the
    original. Only user presets can be renamed; system presets are read-only.

    Because it selects the renamed preset, this leaves it active and discards unsaved config
    overrides, the same as select_preset, and leaves the last slice invalid, so re-slice
    afterwards."""
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
    """Retrieve the last successful slice's G-code as text."""
    try:
        async with _client() as c:
            data = await c.get_gcode()
            return {"bytes": len(data), "gcode": data.decode("utf-8", errors="replace")}
    except Conflict:
        return {"error": "not_sliced"}
    except ApiError as e:
        return _m4a_err(e)


def _safe_gcode_name(name: str) -> str:
    base = Path(name).name
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    base = re.sub(r"\.\.+", "_", base).strip("._")
    if not base or base == "gcode":
        base = f"print_{datetime.datetime.now():%Y%m%d-%H%M%S}"
    return base if base.lower().endswith(".gcode") else base + ".gcode"


def _unique_gcode_path(out_dir: Path, fname: str) -> tuple[Path, str]:
    """Never overwrite an existing file: append -2, -3, ... (then a uuid4 suffix as a last resort)."""
    path = out_dir / fname
    if not path.exists():
        return path, fname
    stem, suffix = fname[: -len(".gcode")], ".gcode"
    for n in range(2, 1000):
        candidate = f"{stem}-{n}{suffix}"
        path = out_dir / candidate
        if not path.exists():
            return path, candidate
    candidate = f"{stem}-{uuid.uuid4().hex[:6]}{suffix}"
    return out_dir / candidate, candidate


@mcp.tool()
async def save_gcode(filename: str | None = None) -> dict:
    """Save the last successful slice's G-code to the shared print-outcomes folder and record the
    slice (model, geometry, full settings snapshot) under that filename, so that when klipper-mcp
    later prints this exact file the real outcome joins back to these settings. Returns the saved
    path; hand it to klipper-mcp's start_print. Default filename: <object>_<timestamp>.gcode.
    Never overwrites an existing file — a name collision gets a -2, -3, ... suffix. Creates the
    gcode folder under the outcome directory (default ~/projects/_shared/print-outcomes, override
    PRINT_OUTCOMES_DIR) if it does not exist. If the shared outcome store is not present, or the
    store write fails, the file is still saved and outcome_recorded is False."""
    try:
        async with _client() as c:
            data = await c.get_gcode()
            objs = (await c.get_objects()).get("objects") or []
            cfg = await c.get_config(None)
    except Conflict:
        return {"error": "not_sliced"}
    except ApiError as e:
        return _m4a_err(e)
    model_name = objs[0]["name"] if objs else "plate"
    fname = _safe_gcode_name(filename or f"{model_name}_{datetime.datetime.now():%Y%m%d-%H%M%S}")
    out_dir = _outcomes.store_dir() / "gcode"
    out_dir.mkdir(parents=True, exist_ok=True)
    path, fname = _unique_gcode_path(out_dir, fname)
    path.write_bytes(data)
    row_id = None
    outcome_error = None
    if _outcomes.is_available():
        try:
            row_id = _outcomes.record_slice(fname, model_name, _outcomes.geometry_hash_for(objs), cfg)
        except sqlite3.Error as e:
            outcome_error = str(e)
    result = {"path": str(path), "filename": fname, "bytes": len(data), "model_name": model_name,
              "outcome_recorded": row_id is not None, "outcome_row_id": row_id}
    if outcome_error is not None:
        result["outcome_error"] = outcome_error
    return result


def _recall_summary(rows: list[dict], subject: str) -> str:
    n = len(rows)
    if n == 0:
        return f"no past prints of {subject}" if subject != "recent" else "no recorded prints"
    counts: dict[str, int] = {}
    for r in rows:
        k = r.get("result") or "unprinted"
        counts[k] = counts.get(k, 0) + 1
    parts = ", ".join(f"{v} {k}" for k, v in counts.items())
    verdicts = [r["human_verdict"] for r in rows if r.get("human_verdict")]
    tail = f" ({len(verdicts)} marked {', '.join(sorted(set(verdicts)))})" if verdicts else ""
    head = f"{n} recent print{'s' if n != 1 else ''}" if subject == "recent" else f"{n} past print{'s' if n != 1 else ''} of {subject}"
    return f"{head}: {parts}{tail}"


@mcp.tool()
async def recall_prints(model_name: str | None = None, limit: int = 5) -> dict:
    """How did past prints of THIS model actually turn out? Matches the current plate by geometry
    (or by model_name if given / the slicer is offline), returning each past print's result
    (success/cancelled/error), your recorded verdict (e.g. 'warped'), and the settings it was sliced
    with. Call this BEFORE slicing and tell the user anything relevant (a past warp, a failed layer
    height). Read-only. Returns available=false and nothing else when no outcome store exists."""
    if not _outcomes.is_available():
        return {"available": False, "matched_by": None, "prints": [], "summary": "no outcome store"}
    objs: list[dict] = []
    try:
        async with _client() as c:
            objs = (await c.get_objects()).get("objects") or []
    except ApiError:
        pass
    limit = max(1, min(limit, 50))
    if objs:
        h = _outcomes.geometry_hash_for(objs)
        rows = _outcomes.recall(geometry_hash=h, limit=limit)
        if rows:
            return {"available": True, "matched_by": "geometry", "prints": rows,
                    "summary": _recall_summary(rows, objs[0]["name"])}
        model_name = model_name or objs[0]["name"]
    if model_name:
        rows = _outcomes.recall(model_name=model_name, limit=limit)
        if rows:
            return {"available": True, "matched_by": "name", "prints": rows,
                    "summary": _recall_summary(rows, model_name)}
    rows = _outcomes.recall(limit=limit)
    return {"available": True, "matched_by": "recent", "prints": rows, "summary": _recall_summary(rows, "recent")}


@mcp.tool()
async def render_plate(view: str = "editor", angle: str = "iso",
                       width: int = 800, height: int = 600,
                       frame: str | None = None):
    """Render a PNG picture of the current plate so you can SEE it.

    view="editor": the models on the bed BEFORE slicing - use to check
    orientation, plate contact, and first-layer footprint (an Euler triple is
    near-unreadable; this is the ground truth). view="preview": the sliced
    toolpaths colored by feature role AFTER a successful slice - support is
    visibly distinct, so use it to check where support actually went.
    angle: iso|top|front|left|right|rear|bottom.
    frame: "plate" zooms out to the whole bed (where the part sits, footprint),
    "object" zooms in on the model/toolpaths (detail). Defaults to "plate" for
    the editor view and "object" for the preview view; pass it explicitly when
    a side view of a small part would otherwise be a speck on a big bed.
    """
    try:
        async with _client() as c:
            png = await c.get_plate_render(view=view, angle=angle,
                                           width=width, height=height, frame=frame)
            return Image(data=png, format="png")
    except Conflict:
        return {"error": "no_slice_result",
                "hint": "slice first, then render view='preview'"}
    except ApiError as e:
        return _m4_err(e, "plate_render (fork v2.3.2-mcp.4+)")



# --- Tool annotations (title + read-only/destructive hints) -----------------
# The Claude connectors directory requires every tool to carry a title and the
# applicable readOnlyHint / destructiveHint. Kept as one table so completeness
# is reviewable at a glance; _apply_tool_annotations() raises on any tool
# missing from it, and tests/test_tool_annotations.py enforces the inverse.

_TOOL_ANNOTATIONS: dict[str, tuple[str, bool, bool]] = {
    # name: (title, read_only, destructive)
    "get_status": ("Get OrcaSlicer status", True, False),
    "get_config": ("Get config values", True, False),
    "get_slice_status": ("Get slice status", True, False),
    "get_slice_warnings": ("Get slice warnings", True, False),
    "get_slice_breakdown": ("Get slice time/flow breakdown", True, False),
    "compare_settings": ("Compare settings", True, False),
    "compare_slices": ("Compare slice variants", True, False),
    "list_objects": ("List plate objects", True, False),
    "get_job_status": ("Get background job status", True, False),
    "watch_events": ("Watch OrcaSlicer events", True, False),
    "find_config_keys": ("Find config keys", True, False),
    "diagnose_plate": ("Diagnose plate issues", True, False),
    "check_placement": ("Check object placement", True, False),
    "consult": ("Consult slicing knowledge", True, False),
    "check_profile_physics": ("Sanity-check profile physics", True, False),
    "describe_setting": ("Describe a setting", True, False),
    "search_settings": ("Search settings", True, False),
    "list_presets": ("List presets", True, False),
    "get_preset_config": ("Get preset config", True, False),
    "get_gcode": ("Download sliced gcode", True, False),
    "save_gcode": ("Save G-code and record the slice", False, False),
    "recall_prints": ("Recall past prints of this model", True, False),
    "render_plate": ("Render plate image", True, False),
    "set_config": ("Set config values", False, False),
    "slice": ("Start slicing", False, False),
    "slice_and_wait": ("Slice and wait for result", False, False),
    "apply_and_slice": ("Apply config and slice", False, False),
    "cancel_slice": ("Cancel running slice", False, False),
    "set_object_config": ("Set per-object config", False, False),
    "duplicate_object": ("Duplicate object", False, False),
    "transform_object": ("Transform object", False, False),
    "arrange_plate": ("Arrange plate", False, False),
    "auto_orient": ("Auto-orient objects", False, False),
    "remember": ("Save a note", False, False),
    "load_model": ("Load a model file", False, False),
    "select_preset": ("Select preset", False, False),
    "set_layer_height": ("Set layer height", False, False),
    "set_height_range": ("Set height-range config", False, False),
    "rename_preset": ("Rename preset", False, False),
    "save_preset": ("Save preset (overwrites stored settings)", False, True),
    "edit_preset": ("Edit preset (overwrites stored settings)", False, True),
    "delete_object": ("Delete object from plate", False, True),
    "delete_preset": ("Delete preset", False, True),
}


def _apply_tool_annotations() -> None:
    for name, tool in mcp._tool_manager._tools.items():
        title, read_only, destructive = _TOOL_ANNOTATIONS[name]
        tool.title = title
        # destructiveHint is only meaningful on tools that modify state, so
        # read-only tools carry no hint rather than a misleading False.
        tool.annotations = ToolAnnotations(
            title=title,
            readOnlyHint=read_only,
            destructiveHint=None if read_only else destructive,
        )


_apply_tool_annotations()


def _hide_windows_console() -> None:
    """Hide the console window an MCP client pops when launching this stdio server
    on Windows (GUI clients have no console, so uv.exe/the launcher spawns one).

    The MCP transport uses the redirected stdin/stdout *pipes* the client hands us,
    not the console, so hiding the window leaves the protocol untouched. No-op off
    Windows and when there is no console (e.g. launched from a terminal a user owns
    — GetConsoleWindow returns NULL and ShowWindow(NULL, ...) does nothing).
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass  # never let a cosmetic tweak stop the server from starting


def main() -> None:
    _hide_windows_console()
    mcp.run()


if __name__ == "__main__":
    main()
