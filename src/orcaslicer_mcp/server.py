from __future__ import annotations
import asyncio
import inspect
import contextvars
import functools
import json
import os
import re
import datetime
import sys
import uuid
from pathlib import Path
from typing import Annotated, Literal
from mcp.server.mcpserver import MCPServer, Image, Context
from pydantic import Field
from mcp.types import ToolAnnotations
from .config import load_config
from . import client as _client_mod
from .client import OrcaClient
from .plugin_update import plugin_update as _plugin_update
from .plugin_text import plugin_refusal as _plugin_text, plugin_note as _plugin_note
from .errors import ApiError, Validation, NotFound, Conflict, ConfigError, OutOfRange, PLUGIN_LIMITS, UiTimeout, NotReachable
from .models import summarize_slice, is_out_of_date, OUT_OF_DATE_NOTE
from . import settings_schema
from . import placement
from .knowledge_index import load_knowledge, search_knowledge
from .physics_check import run_checks, CHECK_LAYERS
from .breakdown import build_breakdown
from .compare import compute_comparison
from .guard import allow_override_warnings, is_sensitive
from .stdio_compat import run_stdio
from . import notes as _notes
from . import outcomes as _outcomes
import hashlib
import time
from . import plate_describe as _plate
from . import verify_slice as _verify
from . import forecast as _forecast
from .printer import target as _ptarget
from .printer import wait as _pwait
from .printer import history as _phistory
from .printer import match as _pmatch
from .printer import ams_match as _ams
from .printer.errors import PrinterError
from .printer.snapshot import take_snapshot
from .printer.status import _dur as _duration_text
from .normalize import normalize_arguments

# Sent with initialize (clients keep up to 4096 characters). Clients that load tool schemas on demand see only
# this and the tool names at first, so it says where to start and which tools answer "what does this setting do".
SERVER_INSTRUCTIONS = (
    "OrcaSlicer MCP: work with the user's live OrcaSlicer toward the print they want, and explain why.\n"
    "1. Start with get_status: build or plugin, the active presets, their unsaved edits, and whether the last "
    "slice still holds. If it says plugin, tell the user before trying what the plugin cannot do (loading models, "
    "starting slices, renders, plates, projects, saving presets); they press Slice themselves and you read it.\n"
    "2. Before changing a setting you are not sure of, look it up: search_settings finds keys by words, "
    "describe_setting gives the meaning, unit, range and choices, consult has slicing knowledge and the user's own "
    "notes. get_config with keys also returns what each key means.\n"
    "3. Change with set_config (unsaved edits; a key set to null goes back to its saved preset value). For "
    "temperature, speed, acceleration or flow, run check_profile_physics first. Script, G-code and printer-connection "
    "keys are refused unless the user allows them in OrcaSlicer.\n"
    "4. Every change shows in the OrcaSlicer MCP panel, where the user can revert it. When a reply carries "
    "user_changes, the user edited something by hand: build on it, never undo it.\n"
    "5. Check results through data, not guesses: get_slice_status, get_slice_breakdown, describe_plate (what the "
    "G-code shows about contact, overhangs, supports, seams), get_slice_warnings, compare_slices for a sweep.\n"
    "6. A slice in progress: wait with slice_and_wait or get_slice_status; do not start another.\n"
    "7. Printer tools (get_printer_status, wait_for_printer, list_print_history, check_printer_match) read the "
    "printer; recall_prints and save_gcode keep slice and print outcomes for next time.\n"
    "Object tools take the stable id from list_objects; plate tools take the 0-based plate index."
)

try:
    import importlib.metadata as _md
    _VERSION = _md.version("orcaslicer-mcp")
except Exception:  # source checkout without an installed dist
    _VERSION = "0.0.0"

mcp = MCPServer("orcaslicer", instructions=SERVER_INSTRUCTIONS, version=_VERSION)

_orig_call_tool = mcp.call_tool


# MCP client names as apps send them -> the name a person knows the app by.
_CLIENT_LABELS = {"claude-ai": "Claude", "claude-desktop": "Claude", "claude-code": "Claude Code",
                  "codex-mcp-client": "Codex", "codex": "Codex", "cursor-vscode": "Cursor", "cursor": "Cursor",
                  "visual studio code": "VS Code", "visual studio code - insiders": "VS Code", "lm studio": "LM Studio",
                  "windsurf-client": "Windsurf", "zed": "Zed"}


def _client_label(context) -> str | None:
    """The AI app's name from the MCP handshake, made header-safe; None when the client sent none."""
    try:
        info = context.session.client_params.client_info
    except AttributeError:
        return None
    raw = (getattr(info, "name", "") or "").strip()
    label = _CLIENT_LABELS.get(raw.lower()) or (getattr(info, "title", None) or raw)
    label = "".join(ch for ch in str(label) if 32 <= ord(ch) < 127).strip()[:60]
    return label or None


async def _call_tool_normalized(name, arguments, context=None):
    """Every tools/call passes here: arguments under a name the tool does not take, or a single value where it
    takes a list, are read in the tool's own form first (normalize.py). It also notes the AI app's name, which
    the client sends to OrcaSlicer so its Config tab can say which app is connected."""
    label = _client_label(context)
    if label:
        _client_mod.CLIENT_NAME[0] = label
    tool = mcp._tool_manager.get_tool(name)
    if tool is not None:
        arguments, _notes = normalize_arguments(tool.parameters, arguments or {})
    return await _orig_call_tool(name, arguments, context)


mcp.call_tool = _call_tool_normalized


# --- Changes the user made by hand, passed on in tool replies -------------------------
# The fork keeps a journal of GUI edits at GET /api/v1/user_changes?since=. After every
# tool call that reached OrcaSlicer we fetch what is new and attach it to a dict reply.
_USER_CHANGES_CAP = 8
_FETCH_TIMEOUT_S = 4.0
_last_user_seq: int | None = None  # None until the first call of the session seeds it
_seq_url: str | None = None  # base URL _last_user_seq belongs to
_unsupported_url: str | None = None  # base URL that answered 404: an older build
_unsupported_at: float = 0.0  # monotonic time of that 404
_UNSUPPORTED_TTL_S = 300.0  # then probe once more (OrcaSlicer may have been upgraded)
_changes_lock: asyncio.Lock | None = None
_changes_lock_loop = None


def _get_changes_lock() -> asyncio.Lock:
    """Created on first use inside the running loop, and again if the loop changes."""
    global _changes_lock, _changes_lock_loop
    loop = asyncio.get_running_loop()
    if _changes_lock is None or _changes_lock_loop is not loop:
        _changes_lock, _changes_lock_loop = asyncio.Lock(), loop
    return _changes_lock


def _user_changes_note(changes: list) -> str | None:
    entries = [c for c in changes if isinstance(c, dict) and str(c.get("text", ""))]
    if not entries:
        return None
    parts = [str(c["text"]) for c in entries[:_USER_CHANGES_CAP]]
    rest = entries[_USER_CHANGES_CAP:]
    if rest:
        counts: dict[str, int] = {}
        for c in rest:
            g = str(c.get("group", "")) or "Other"
            counts[g] = counts.get(g, 0) + 1
        parts.append("\u2026and %d more (%s)" % (
            len(rest), ", ".join(f"{g}: {n}" for g, n in counts.items())))
    return ("Since your last action the user changed: " + "; ".join(parts) +
            ". Work from these values; don't undo them without asking.")


async def _fetch_user_changes(result: dict) -> None:
    global _last_user_seq, _unsupported_url, _unsupported_at, _seq_url
    cfg_url = None
    async with _client() as c:
        cfg_url = c._cfg.base_url
        if _unsupported_url == cfg_url:
            if time.monotonic() - _unsupported_at < _UNSUPPORTED_TTL_S:
                return
            _unsupported_url = None
        if _seq_url != cfg_url:  # another OrcaSlicer: its seqs are unrelated, so seed again
            _last_user_seq, _seq_url = None, cfg_url
        try:
            data = await c._request("GET", "/api/v1/user_changes", _count=False,
                                    params={"since": _last_user_seq or 0})
        except NotFound:
            _unsupported_url, _unsupported_at = cfg_url, time.monotonic()
            return
    if not isinstance(data, dict) or not isinstance(data.get("seq"), int):
        return
    new_seq = data["seq"]
    if _last_user_seq is not None and not data.get("reset"):
        new_seq = max(_last_user_seq, new_seq)
    seeding = _last_user_seq is None
    changes = data.get("changes")
    note = None
    if not seeding and isinstance(changes, list) and changes:
        note = _user_changes_note(changes)  # build first; only then advance the seq
    _last_user_seq = new_seq
    if note:
        result["user_changes"] = {"note": note, "changes": changes}


async def _attach_user_changes(result):
    """Fetch the journal and add `user_changes` to a dict reply. Never raises."""
    if not isinstance(result, dict):
        return result
    try:
        async def locked():
            async with _get_changes_lock():
                await _fetch_user_changes(result)
        await asyncio.wait_for(locked(), _FETCH_TIMEOUT_S)
    except Exception:  # unreachable, 401, bad JSON, timeout: the tool reply stands
        pass
    return result


# --- An older OrcaSlicer MCP plugin, said once ------------------------------------------------
# A plugin installed from a .py file never updates itself, so the first tool reply of the session that reaches
# OrcaSlicer carries a one-time `plugin_update` note when the plugin is older than this release's
# (plugin_update.LATEST_PLUGIN_VERSION). get_status says so every time. The build is never flagged.
_plugin_checked_url: str | None = None  # base URL already checked this session


async def _check_plugin_update(result: dict) -> None:
    global _plugin_checked_url
    async with _client() as c:
        url = c._cfg.base_url
        if _plugin_checked_url == url:
            return
        status = await c._request("GET", "/api/v1/status", _count=False)
    _plugin_checked_url = url  # checked, whatever it said: the note is one-time
    upd = _plugin_update(status) if isinstance(status, dict) else None
    if upd:
        result["plugin_update"] = upd


async def _attach_plugin_update(result):
    """Add the one-time `plugin_update` note to a dict reply. Never raises; a failed check is tried again on
    the next call."""
    if not isinstance(result, dict) or "plugin_update" in result:
        return result
    try:
        async def locked():
            async with _get_changes_lock():
                await _check_plugin_update(result)
        await asyncio.wait_for(locked(), _FETCH_TIMEOUT_S)
    except Exception:
        pass
    return result


# The tool being run, so a refusal can name the action that failed.
_CURRENT_TOOL: contextvars.ContextVar = contextvars.ContextVar("orca_tool", default=None)


def _with_user_changes(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        flag = [False]
        token = _client_mod.REACHED.set(flag)
        tool_token = _CURRENT_TOOL.set(fn.__name__)
        try:
            result = await fn(*args, **kwargs)
        finally:
            _client_mod.REACHED.reset(token)
            _CURRENT_TOOL.reset(tool_token)
        _polish_error(result)
        if isinstance(result, dict) and str(result.get("error", "")).startswith("not_available_in_plugin:"):
            await _note_plugin_refusal(fn.__name__)
        if flag[0]:
            result = await _attach_user_changes(result)
            result = await _attach_plugin_update(result)
        return result
    return wrapper


async def _note_plugin_refusal(tool: str) -> None:
    """A refusal the plugin can't serve shows in its panel too, as a 'refused' row. Never raises."""
    try:
        async with _client() as c:
            await c.post_note("refused", *_plugin_note(tool))
    except Exception:  # noqa: BLE001 - a note never decides the reply
        pass


# --- Every error reads as a sentence ------------------------------------------------------
# A reply such as {"error": "file_not_found"} or "open takes a .3mf project; POST /model imports
# geometry" left the assistant to guess (usability test 2026-11-09, m12): every error gets a
# `detail` sentence, and HTTP routes become the tool names that do the same thing.
_ROUTE_TOOLS = {
    ("POST", "/model"): "load_model", ("POST", "/project/open"): "open_project",
    ("POST", "/project/save"): "save_project", ("POST", "/project/new"): "new_project",
    ("GET", "/project"): "get_project", ("GET", "/project/recent"): "list_recent_projects",
    ("DELETE", "/project/recent"): "forget_recent_project", ("GET", "/project/browse"): "browse_projects",
    ("POST", "/project/move"): "move_project_file", ("DELETE", "/project/file"): "delete_project_file",
    ("POST", "/slice"): "slice", ("GET", "/slice/status"): "get_slice_status",
    ("POST", "/slice/cancel"): "cancel_slice",
    ("GET", "/config"): "get_config", ("PUT", "/config"): "set_config",
    ("GET", "/status"): "get_status", ("GET", "/objects"): "list_objects",
    ("PUT", "/preset"): "select_preset", ("DELETE", "/preset"): "delete_preset",
    ("POST", "/preset/save"): "save_preset", ("POST", "/preset/config"): "get_preset_config",
    ("GET", "/presets"): "list_presets",
    ("GET", "/plates"): "list_plates", ("POST", "/plates"): "add_plate",
    ("POST", "/arrange"): "arrange_plate", ("POST", "/orient"): "auto_orient",
    ("GET", "/gcode"): "get_gcode", ("GET", "/plate/render"): "render_plate",
    ("GET", "/jobs/status"): "get_job_status",
}
_ROUTE = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH)\s+(?:/api/v1)?(/[\w/{}.-]*)")


_ERROR_DETAILS = {
    "already_slicing": "A slice is already running; wait for it (get_slice_status) or stop it (cancel_slice).",
    "slicing": "A slice is running; wait for it to finish (get_slice_status), then try again.",
    "job_running": "OrcaSlicer is still arranging or orienting; wait until get_job_status says idle.",
    "unknown_object": "There is no object with that id; list_objects shows the ids.",
    "unknown_preset": "There is no preset with that name; list_presets shows the names.",
    "unknown_plate": "There is no plate with that index; list_plates shows the 0-based indexes.",
    "file_not_found": "There is no such file on the OrcaSlicer machine; give the full path as it is on that computer.",
    "need_at_least_two_variants": "compare_slices needs at least two variants to compare.",
    "too_many_variants": "compare_slices takes at most 8 variants; each one is a full slice.",
    "each_variant_needs_a_name": "Every variant needs a name, shown in the comparison table.",
    "variant_names_must_be_unique": "Two variants share a name; give each a different one.",
    "unknown_baseline": "The baseline must be the name of one of the variants.",
    "unknown_setting": "There is no setting with that key; search_settings finds keys by words.",
    "invalid_type": "The preset type must be print, filament or printer.",
    "invalid_keys": "OrcaSlicer refused some of the values; `errors` names each one, and nothing was saved.",
    "no_project_path": "The project has never been saved, so it has no file yet: pass a path to save_project.",
    "unsaved_changes": "The open project has unsaved changes; pass if_unsaved='save' or 'discard' deliberately, after asking the user.",
    "exists": "A file already exists at that path; pick another name, or pass overwrite=True if the user agrees.",
    "plate_locked": "That plate is locked in OrcaSlicer; the user can unlock it there.",
    "last_plate": "The only plate cannot be deleted.",
    "parse_failed": "The G-code of this slice could not be read.",
    "blocked_by_remote_api_policy": "OrcaSlicer's own Preferences refused this; `hint` says which box.",
}


def _without_routes(text: str) -> str:
    def repl(m):
        return _ROUTE_TOOLS.get((m.group(1), m.group(2).rstrip(".")), "another tool")
    return _ROUTE.sub(repl, text)


def _looks_relative(path) -> bool:
    return isinstance(path, str) and bool(path) and not (
        path.startswith(("/", "\\", "~")) or re.match(r"[A-Za-z]:[\\/]", path))


def _polish_error(result) -> None:
    if not isinstance(result, dict) or not isinstance(result.get("error"), str):
        return
    for k in ("error", "detail", "hint"):
        if isinstance(result.get(k), str):
            result[k] = _without_routes(result[k])
    if isinstance(result.get("errors"), dict):
        result["errors"] = {k: _without_routes(v) if isinstance(v, str) else v
                            for k, v in result["errors"].items()}
    token = result["error"]
    if "detail" not in result and "message" not in result and re.fullmatch(r"[a-z_]+", token):
        detail = _ERROR_DETAILS.get(token) or (token.replace("_", " ").capitalize() + ".")
        if token == "file_not_found" and isinstance(result.get("path"), str):
            detail = (f"There is no file at {result['path']} on the OrcaSlicer machine; give the full "
                      "path as it is on that computer.")
        result["detail"] = detail
    if token == "file_not_found" and _looks_relative(result.get("path")) and "full path" not in result.get("detail", ""):
        result["detail"] += " Give the full path, for example C:\\Users\\name\\Downloads\\part.stl or /home/name/part.stl."


_orig_tool = mcp.tool


def _tool(*targs, **tkw):
    register = _orig_tool(*targs, **tkw)

    def deco(fn):
        # Sync tools never talk to OrcaSlicer; only coroutines can reach it.
        wrapped = _with_user_changes(fn) if inspect.iscoroutinefunction(fn) else fn
        register(wrapped)
        return wrapped
    return deco


mcp.tool = _tool


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


def _backend_now() -> str | None:
    """Which OrcaSlicer answered at the configured address ("build" or "plugin"), once known."""
    try:
        return _client_mod.known_backend(load_config().base_url)
    except Exception:  # noqa: BLE001 - no token configured, say
        return None


def _err(e: ApiError) -> dict:
    if isinstance(e, NotFound) and e.plugin:
        return _plugin_text(_CURRENT_TOOL.get())
    out = {"error": str(e)}
    if isinstance(e, Validation):
        out["errors"] = e.errors
        body = getattr(e, "body", None) or {}
        if not isinstance(e, OutOfRange):
            # OrcaSlicer's own explanation of a refusal travels with it.
            for k in ("detail", "key"):
                if isinstance(body.get(k), str) and body[k].strip():
                    out[k] = body[k]
        if isinstance(e, OutOfRange):
            out["allowed"] = {k: {"min": r["min"], "max": r["max"], "unit": r["unit"],
                                  **({"min_exclusive": True} if r.get("min_exclusive") else {}),
                                  **({"index": r["index"]} if r.get("index") is not None else {})}
                              for k, r in e.ranges.items()}
        if "blocked_by_remote_api_policy" in (e.errors or {}).values() and _backend_now() == "plugin":
            out["hint"] = ("The OrcaSlicer MCP plugin never lets the assistant change scripts, custom "
                           "G-code or printer connection settings. Ask the user to make this change "
                           "in the OrcaSlicer window.")
        elif "blocked_by_remote_api_policy" in (e.errors or {}).values():
            out["hint"] = ("OrcaSlicer itself refused these keys: Preferences > Remote API > "
                           "'Allow script, G-code and connection edits' is off. Ask the user to "
                           "tick it, then ask again; or to make the change themselves in the "
                           "OrcaSlicer window.")
    return out


# --- Slices that will not start, and empty plates ---------------------------------------
# A slice OrcaSlicer declines used to come back as {"error": "slice_not_started", "errors": {}}
# while the panel said "object outside bed or invalid state", although the real cause was the
# preset (0.8 mm layers on a 0.4 mm nozzle): the assistant started moving the object.
_GENERIC_SLICE_MESSAGES = ("object outside bed or invalid state",)
_EMPTY_PLATE = "The plate has no objects; load a model first (load_model)."


def _reason_from(d: dict) -> str | None:
    for k in ("validation", "detail", "reason", "message"):
        v = d.get(k)
        if isinstance(v, str) and v.strip() and not any(g in v for g in _GENERIC_SLICE_MESSAGES):
            return v.strip()
    errs = d.get("errors")
    if isinstance(errs, list):
        texts = [e.get("message") if isinstance(e, dict) else e for e in errs]
        texts = [t for t in texts if isinstance(t, str) and t.strip()]
        if texts:
            return "; ".join(texts)
    return None


async def _slice_refusal(c, e: ApiError) -> dict | None:
    """The reply for a slice OrcaSlicer would not start, or None when `e` is something else.
    It relays OrcaSlicer's own validation message (on the reply, else on /slice/status); with
    none, it runs the physics check and lists what fails. Never raises."""
    body = getattr(e, "body", None) or {}
    token = body.get("error") or str(e)
    if token == "nothing_to_slice":
        return {"error": "nothing_to_slice", "detail": _EMPTY_PLATE}
    if token != "slice_not_started":
        return None
    out: dict = {"error": "slice_not_started"}
    reason = _reason_from(body)
    if reason is None:
        try:
            reason = _reason_from(await c.slice_status())
        except Exception:  # noqa: BLE001 - only refines the reply
            reason = None
    if isinstance(body.get("key"), str) and body["key"]:
        out["key"] = body["key"]  # the setting OrcaSlicer's check blames
        rec = settings_schema.describe(body["key"]) or {}
        if rec.get("full_label") or rec.get("label"):
            out["setting"] = rec.get("full_label") or rec.get("label")
    if reason:
        out["detail"] = f"OrcaSlicer won't slice this plate: {reason}"
        return out
    fails: list[dict] = []
    try:
        fails = [{"name": r.name, "detail": r.detail}
                 for r in run_checks(await c.get_config(None)) if r.status == "fail"]
    except Exception:  # noqa: BLE001
        fails = []
    if fails:
        out["detail"] = ("OrcaSlicer won't start this slice and gave no reason. The profile check "
                         "found: " + "; ".join(f["detail"] for f in fails)
                         + ". Fix those first; check_profile_physics shows every check.")
        out["physics_fails"] = fails
    else:
        out["detail"] = ("OrcaSlicer won't start this slice and gave no reason. Ask the user to look "
                         "at the OrcaSlicer window for a message; check_placement checks whether the "
                         "objects fit the bed.")
    return out


async def _slice_err(c, e: ApiError) -> dict:
    return (await _slice_refusal(c, e)) or _err(e)


async def _plate_is_empty() -> bool:
    try:
        async with _client() as c:
            return not ((await c.get_objects()).get("objects") or [])
    except Exception:  # noqa: BLE001
        return False


_NOT_SLICED_PLUGIN = ("There is no finished slice of this plate yet, or something changed since: ask the "
                      "user to press Slice plate (top right of the OrcaSlicer window), then call this again.")


async def _is_plugin() -> bool:
    """The OrcaSlicer MCP plugin answers at the configured address (asked now: only error paths call this)."""
    try:
        async with _client() as c:
            return (await c.get_status()).get("backend") == "plugin"
    except Exception:  # noqa: BLE001 - only picks the wording
        return False


async def _not_sliced(token: str = "not_sliced") -> dict:
    if await _plate_is_empty():
        return {"error": token, "detail": _EMPTY_PLATE}
    if await _is_plugin():
        return {"error": token, "detail": _NOT_SLICED_PLUGIN}
    return {"error": token, "detail": "There is no finished slice of this plate yet: slice it first "
                                      "(slice_and_wait)."}



# Settings OrcaSlicer tracks as "modified" that no user edits: extruder_nozzle_stats (2.5) is
# the printer's nozzle bookkeeping and shows as a dirty printer key straight after loading.
_INTERNAL_MODIFIED = frozenset({"extruder_nozzle_stats"})


def _hide_internal_modified(status: dict) -> None:
    modified = status.get("modified")
    if not isinstance(modified, dict):
        return
    for group, keys in modified.items():
        if isinstance(keys, list):
            modified[group] = [k for k in keys
                               if not (isinstance(k, str) and k.split("#", 1)[0] in _INTERNAL_MODIFIED)]


@mcp.tool()
async def get_status() -> dict:
    """Start here: a snapshot of the OrcaSlicer session (build or plugin, project, active presets and their unsaved edits, whether the last slice still holds); for slice numbers use get_slice_status.

    Also lists which keys of each active preset are modified (dirty) and whether a slice is
    running. Read-only.

    Call it first to orient before slicing or editing, to see which settings drift from their
    preset, or to check slice_result_valid before trusting earlier stats. On fork
    v2.4.2-mcp.15 and later `project` is {name, path, dirty}: path is empty until the project
    has been saved, and dirty says File > Save has something to write, so an agent knows when
    to save_project and when the user has been editing in the window. Older builds report
    `project` as the bare path string.

    `connected_to` says in words which one answered: the OrcaSlicer MCP build, or
    `backend: "plugin"`, the OrcaSlicer MCP plugin on stock
    OrcaSlicer: `note` says what it can do. Tell the user before attempting anything it
    cannot (loading models, slicing, renders, plates, projects, saving presets). When that
    plugin is older than the newest release, `plugin_update` says where to get the new one;
    pass its note on to the user."""
    global _plugin_checked_url
    try:
        async with _client() as c:
            status = await c.get_status()
            url = c._cfg.base_url
    except ApiError as e:
        return _err(e)
    if status.get("backend") == "plugin":
        status["connected_to"] = "the OrcaSlicer MCP plugin on stock OrcaSlicer"
        status["note"] = ("Connected to the OrcaSlicer MCP plugin on stock OrcaSlicer, not the "
                          "OrcaSlicer MCP build: " + PLUGIN_LIMITS.split(". ", 1)[1])
    else:
        status["connected_to"] = "the OrcaSlicer MCP build (OrcaSlicer with the Remote API built in)"
    _hide_internal_modified(status)
    upd = _plugin_update(status)
    if upd:
        status["plugin_update"] = upd
    _plugin_checked_url = url  # this reply already said it: no one-time note on top
    return status


_ABOUT_MAX_KEYS = 12  # beyond this the reply would mostly be explanations; describe_setting has them one by one


def _about(keys: list[str]) -> dict:
    """What each named key means, from OrcaSlicer's own tooltips (as the sidebar shows them). Agents read settings
    far more often than they ask for an explanation, so the explanation comes with the value (OrcaSlicer's own MCP
    work found the same)."""
    out = {}
    for k in keys[:_ABOUT_MAX_KEYS]:
        rec = settings_schema.describe(k)
        if rec is None:
            continue
        tip = rec.get("tooltip") or ""
        item = {"label": rec.get("label"), "meaning": tip[:240] + ("…" if len(tip) > 240 else "")}
        if rec.get("unit"):
            item["unit"] = rec["unit"]
        if rec.get("min") is not None or rec.get("max") is not None:
            item["range"] = [rec.get("min"), rec.get("max")]
        if rec.get("enum_values"):
            item["choices"] = rec["enum_values"]
        out[k] = item
    return out


@mcp.tool()
async def get_config(
    keys: list[str] | None = None,
    by_slot: Annotated[bool, Field(description=(
        "Return each per-filament setting as one value per filament slot, {'1': '70', '2': '100'}, numbered "
        "as the sidebar numbers them, instead of the merged '70,100' text. Other settings are unchanged."))] = False,
) -> dict:
    """Read the current value of settings (all, or only `keys`); with `keys`, `about` also says what each one means, its unit, range and choices.

    Values come back as text, merged across the active print, filament and printer presets: a per-filament
    setting such as hot_plate_temp holds one value per loaded filament slot, '70,100,70'. With by_slot those
    come back as {slot: value} instead, so slot 2's bed temperature is config['hot_plate_temp']['2'], and
    `slots` says how many there are. Name the keys you need: the whole config is several hundred settings.
    describe_setting gives the full record of one key.
    """
    try:
        async with _client() as c:
            cfg = await c.get_config(keys)
            reply = {"config": cfg}
            if by_slot:
                status = await c.get_status()
                names = [n for n in (status.get("presets") or {}).get("filament") or [] if n]
                filament_keys: set = set()
                if names:
                    try:
                        filament_keys = set((await c.get_preset_config("filament", names[0])).get("config") or {})
                    except ApiError:
                        pass  # the values are read already: without the key list they stay as merged text
                filament_keys -= _NOT_PER_SLOT
                cfg, left = _config_by_slot(cfg, filament_keys, len(names))
                reply = {"config": cfg, "slots": len(names)}
                if left:
                    reply["not_per_slot"] = left
                    reply["note"] = "not_per_slot: filament settings whose value does not hold one entry per slot, left as text"
    except ApiError as e:
        return _err(e)
    if keys:
        about = _about(list(keys))
        if about:
            reply["about"] = about
    return reply


_GROUP_TYPE = {"print": "print", "filament": "filament", "printer": "printer"}

# Filament preset keys whose list is not one value per slot (as the fork's is_merged_per_filament_key).
_NOT_PER_SLOT = frozenset({"compatible_printers", "compatible_prints", "compatible_printers_condition",
                           "compatible_prints_condition", "inherits"})

_FILAMENT_SLOTS_NEED = "the filament slot routes"


async def _capability_missing(c, cap: str, what: str, plugin_action: str | None = None) -> dict | None:
    """The error reply when this OrcaSlicer lacks `cap` (a GET /status capability), else None. An older build
    ignores the argument the capability covers and does something else with the request (a slot write would
    land in the Filament tab's preset), so this runs before the request. The plugin gets its own wording."""
    status = await c.get_status()
    if cap in (status.get("capabilities") or []):
        return None
    if status.get("backend") == "plugin":
        return _plugin_text(plugin_action or _CURRENT_TOOL.get())
    return {"error": f"not available on this OrcaSlicer build (needs {what}, capability '{cap}')"}


def _visible_keys(keys) -> list:
    return [k for k in (keys or []) if not (isinstance(k, str) and k.split("#", 1)[0] in _INTERNAL_MODIFIED)]


def _filament_slots_view(raw: dict) -> dict:
    """GET /filaments as the tools show it: slots numbered from 1 as the sidebar does, and for each slot which
    other slots share its preset (an edit or a save of that preset reaches them all)."""
    rows = raw.get("filaments") or []
    using: dict = {}
    for r in rows:
        using.setdefault(r.get("preset"), []).append(int(r.get("index", 0)) + 1)
    slots = []
    for r in rows:
        slot = int(r.get("index", 0)) + 1
        row = {"slot": slot, "preset": r.get("preset"), "filament_type": r.get("filament_type"),
               "vendor": r.get("vendor"), "colour": r.get("colour"), "system_preset": bool(r.get("system")),
               "open_in_tab": bool(r.get("open_in_tab")), "modified": bool(r.get("modified")),
               "modified_keys": _visible_keys(r.get("modified_keys"))}
        shared = [n for n in using.get(r.get("preset"), []) if n != slot]
        if shared:
            row["shares_preset_with"] = shared
        slots.append(row)
    tab = raw.get("tab") or {}
    tab_slot = tab.get("slot")
    return {"count": raw.get("count", len(rows)), "multi_filament": bool(raw.get("multi_filament")), "slots": slots,
            "filament_tab": {"preset": tab.get("preset"),
                             "slot": tab_slot + 1 if isinstance(tab_slot, int) else None,
                             "modified": bool(tab.get("modified"))}}


def _slot_reply(result: dict) -> dict:
    """The API counts slots from 0; the tools, like the sidebar, from 1."""
    if isinstance(result.get("slot"), int):
        result["slot"] += 1
    for key in ("slots_kept", "slots_reassigned"):
        if isinstance(result.get(key), list):
            result[key] = [n + 1 for n in result[key] if isinstance(n, int)]
    if isinstance(result.get("filaments"), list):
        result["filaments"] = {str(i + 1): name for i, name in enumerate(result["filaments"])}
    return result


def _slot_err(e: ApiError) -> dict:
    """A slot write or save refused: the reply keeps what the API said about why, and says what to do."""
    out = _m4_err(e, _FILAMENT_SLOTS_NEED)
    body = getattr(e, "body", None) or {}
    if body.get("error") == "filament_edits_unsaved":
        out["error"] = ("filament_edits_unsaved: the Filament tab holds unsaved edits to a different preset, "
                        f"{body.get('preset')!r}, and switching it to this slot's preset would drop them; nothing was changed")
        out["preset"] = body.get("preset")
        out["modified"] = list(dict.fromkeys(k.split("#", 1)[0] for k in _visible_keys(body.get("modified"))))
        out["hint"] = ("first save them, with save_preset and the filament_slot of a slot that uses that preset (under "
                       "a new name when it is a built-in preset), or put them back, with set_config, each key set to "
                       "null and the filament_slot of a slot that uses that preset")
    elif body.get("error") == "slot_out_of_range":
        out["error"] = f"slot_out_of_range: the project has {body.get('filament_count')} filament slot(s)"
    return out


def _pair_text(p: dict) -> str:
    return (f"{p.get('label')}: first layer {p.get('first_layer')} °C, other layers {p.get('other_layers')} °C "
            f"({p.get('delta')} °C apart, at most {p.get('max_delta')} °C)")


def _save_refusal(e: ApiError, ptype: str, edited: bool = False) -> dict | None:
    """OrcaSlicer 2.5's temperature safety check on a filament save, said plainly; None for any other error.

    The OrcaSlicer MCP build refuses with the pairs (capability preset_temperature_check). Earlier 2.5 builds open
    OrcaSlicer's own "Temperature Safety Check" dialog instead, and the save times out behind it."""
    body = getattr(e, "body", None) or {}
    if body.get("error") == "temperature_pairs_unsafe":
        pairs = [p for p in body.get("pairs") or [] if isinstance(p, dict)]
        hint = ("OrcaSlicer checks that a filament preset's first-layer temperature stays within 30 °C of the other "
                "layers' for the nozzle and 15 °C for each plate type. Tell the user which pair is past that. Bring "
                "the values closer with set_config and save again, or, if the user wants the preset as it is, save "
                "again with allow_temperature_pairs=true.")
        if edited:
            hint += " The changes stay in the project as unsaved edits; set_config with null for a key puts it back."
        return {"error": "temperature_pairs_unsafe: OrcaSlicer's temperature safety check stopped this filament "
                         "preset save; nothing was saved",
                "pairs": [{**p, "text": _pair_text(p)} for p in pairs], "hint": hint}
    if isinstance(e, UiTimeout) and ptype == "filament":
        out = _err(e)
        out["hint"] = ("On OrcaSlicer 2.5 builds without the preset_temperature_check capability, a filament save "
                       "whose first-layer and other-layer temperatures are far apart waits behind OrcaSlicer's "
                       "'Temperature Safety Check' dialog. Ask the user to look at the OrcaSlicer window and choose "
                       "Continue or Back.")
        return out
    return None


# OrcaSlicer serializes a vector setting as one string: numbers, enums and booleans joined with ',', strings with
# ';' (ConfigOptionStrings::serialize, an item that needs it in C-style quotes). One entry per loaded filament for
# a filament preset key.
_STRING_VECTOR = "coStrings"
_VECTOR_TYPES = frozenset({"coFloats", "coInts", "coStrings", "coBools", "coPercents", "coFloatsOrPercents",
                           "coPoints", "coEnums"})


def _split_config_strings(value: str) -> list[str]:
    items, buf, i, n = [], [], 0, len(value)
    while i < n:
        ch = value[i]
        if ch == '"':
            i += 1
            while i < n and value[i] != '"':
                if value[i] == "\\" and i + 1 < n:
                    i += 1
                    buf.append({"n": "\n", "t": "\t", "r": "\r"}.get(value[i], value[i]))
                else:
                    buf.append(value[i])
                i += 1
            i += 1  # the closing quote
        elif ch == ";":
            items.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    items.append("".join(buf))
    return items


def _config_by_slot(cfg: dict, filament_keys: set, slots: int) -> tuple[dict, list]:
    """Per-filament keys as {"1": value, "2": value, ...}; a key whose value does not hold one entry per slot
    stays as it came and is named in the second result."""
    out, left = {}, []
    for key, value in cfg.items():
        if not isinstance(value, str) or (key not in filament_keys and key != "filament_colour"):
            out[key] = value
            continue
        ctype = (settings_schema.describe(key) or {}).get("type") or (_STRING_VECTOR if ";" in value else "coFloats")
        if ctype not in _VECTOR_TYPES:
            out[key] = value
            continue
        parts = _split_config_strings(value) if ctype == _STRING_VECTOR else value.split(",")
        if len(parts) != slots:
            out[key] = value
            left.append(key)
            continue
        out[key] = {str(i + 1): p for i, p in enumerate(parts)}
    return out, left


async def _resolve_reverts(c, changes: dict, slot: int | None = None) -> tuple[dict, list, list, dict]:
    """Keys set to null go back to the value saved in their active preset (OrcaSlicer's own MCP work takes null the
    same way); with `slot` (0-based) a filament key goes back to that slot's preset. Returns (changes with the
    saved values filled in, reverted keys, keys already at their saved value, errors). Only reads; nothing
    changes here."""
    wanted = [k for k, v in changes.items() if v is None]
    if not wanted:
        return changes, [], [], {}
    status = await c.get_status()
    presets = status.get("presets") or {}
    modified = status.get("modified") or {}
    group_of = {}
    for group, keys in modified.items():
        for k in keys if isinstance(keys, list) else []:
            if isinstance(k, str):
                group_of.setdefault(k.split("#", 1)[0], group)
    out = {k: v for k, v in changes.items() if v is not None}
    reverted, unchanged, errors = [], [], {}
    saved_cache: dict = {}
    live_keys: set | None = None
    for key in wanted:
        if is_sensitive(key):
            errors[key] = "not_revertible: script, G-code and printer-connection keys are not put back this way"
            continue
        group = group_of.get(key)
        if group is None:
            if settings_schema.describe(key) is None:  # not in the schema: maybe a newer key, maybe a typo
                if live_keys is None:
                    live_keys = set(await c.get_config(None))
                if key not in live_keys:
                    errors[key] = "unknown_setting: OrcaSlicer has no setting by this name"
                    continue
            unchanged.append(key)  # no unsaved edit: it already has its saved value
            continue
        if group == "filament" and slot is not None:
            rows = (await c.get_filaments()).get("filaments") or []
            name = rows[slot].get("preset") if slot < len(rows) else None
            if not name:
                errors[key] = f"slot_out_of_range: the project has {len(rows)} filament slot(s)"
                continue
        elif group == "filament":
            names = [n for n in presets.get("filament") or presets.get("filaments") or [] if n]  # the client says filament
            if len(set(names)) != 1:
                errors[key] = ("several filament presets are loaded, so which saved value to use is ambiguous; "
                               "set the value you want instead")
                continue
            name = names[0]
        else:
            name = presets.get(group)
        if not name or group not in _GROUP_TYPE:
            errors[key] = "the active preset for this setting is unknown"
            continue
        if (group, name) not in saved_cache:
            saved_cache[(group, name)] = (await c.get_preset_config(_GROUP_TYPE[group], name)).get("config", {})
        saved = saved_cache[(group, name)].get(key)
        if saved is None:
            errors[key] = f"not in the saved preset {name!r}"
            continue
        out[key] = saved
        reverted.append(key)
    return out, reverted, unchanged, errors


async def _put_config_with_reverts(c, changes: dict, slot: int | None = None) -> dict:
    resolved, reverted, unchanged, errors = await _resolve_reverts(c, changes, slot)
    if errors:
        return {"applied": [], "errors": errors, "detail": "nothing was changed (all or nothing)"}
    if resolved:
        result = _slot_reply(await c.put_config(resolved, slot))
    else:
        result = {"applied": [], "errors": {}}
        if slot is not None:
            result["slot"] = slot + 1
    if reverted:
        result["reverted"] = reverted
    if unchanged:
        result["already_saved"] = unchanged
    return result


@mcp.tool()
async def set_config(
    changes: Annotated[dict, Field(description=(
        "Map of OrcaSlicer config key to new value, e.g. "
        "{'layer_height': 0.2, 'sparse_infill_density': '15%'}. Values must match each "
        "setting's type; percent settings take strings like '15%'. Discover valid keys "
        "with search_settings, find_config_keys, or describe_setting. Do NOT echo back a "
        "get_config value for a per-filament setting (hot_plate_temp, fan_min_speed, "
        "nozzle_temperature_initial_layer, ...): get_config reports those merged across "
        "every loaded filament ('70,100,70,70,35'), while this writes one filament preset "
        "(the slot's with filament_slot, else the one open in the Filament tab), so pass that "
        "preset's single value instead. A key set to "
        "null goes back to the value saved in its preset (an unsaved edit undone)."))],
    filament_slot: Annotated[int | None, Field(description=(
        "Write the per-filament keys to this slot's filament preset (1-based, as the sidebar numbers "
        "the slots; get_filament_slots lists them) instead of the preset open in the Filament tab. "
        "Needs the OrcaSlicer MCP build with the filament_slots capability."))] = None,
) -> dict:
    """Change settings on the open project as unsaved edits, all or nothing; a key set to null goes back to its saved preset value; use apply_and_slice to also slice, or edit_preset to change a saved preset.

    Returns {applied, errors}; if any key is invalid the whole batch is rejected and nothing
    changes. Keys set to null are listed under `reverted` (or `already_saved` when they had no
    unsaved edit).

    With filament_slot the per-filament keys land in that slot's preset: OrcaSlicer binds the
    Filament tab to the slot first, as the slot's edit button does, and the reply names the
    `preset` written. A preset shared by several slots is still one preset, so the edit reaches
    every slot using it (get_filament_slots shows `shares_preset_with`); to change one slot alone,
    save_preset with filament_slot under a new name first. Only one filament preset can hold
    unsaved edits at a time: when the Filament tab holds unsaved edits to a different preset the
    call is refused with filament_edits_unsaved and nothing changes (save them, or put the keys
    back, first). Unsaved print and printer edits are never touched.

    Overrides show as modified in get_status, are not written to any preset file, and revert
    if the preset is reselected; call save_preset to persist them. Each apply invalidates the
    last slice, so re-slice afterwards. It does not run the physics gate, so for temperature,
    speed, acceleration, or flow keys run check_profile_physics before trusting the result. To
    edit a stored preset rather than the live project, use edit_preset.

    Without filament_slot on a multi-filament setup, do filament edits before print edits:
    changing another slot's value means select_preset for that slot, which discards unsaved
    print overrides (docs/order-of-operations.md)."""
    if filament_slot is not None and filament_slot < 1:
        return {"error": "filament_slot is 1-based; use filament_slot=1 for the first filament"}
    try:
        async with _client() as c:
            if filament_slot is None:
                return await _put_config_with_reverts(c, changes)
            if (missing := await _capability_missing(c, "filament_slots", _FILAMENT_SLOTS_NEED,
                                                     "set_config:filament_slot")) is not None:
                return missing
            return await _put_config_with_reverts(c, changes, filament_slot - 1)
    except ApiError as e:
        return _slot_err(e) if filament_slot is not None else _err(e)


@mcp.tool()
async def slice() -> dict:
    """Start a slice and return at once without waiting; use slice_and_wait when you want the finished time and filament in the same call.

    The reply is 'started' (a slice began), 'already_valid' (the plate is unchanged and the last
    result still holds), or a conflict if a slice is already running.

    Fire-and-forget: poll get_slice_status for progress and stats, or cancel_slice to stop it.
    Prefer slice_and_wait when you want the finished stats back in one call."""
    try:
        async with _client() as c:
            try:
                return await c.slice()
            except Validation as e:
                return await _slice_err(c, e)
    except ApiError as e:
        return _err(e)


async def _with_forecast(c, result: dict) -> dict:
    """A finished slice also says how long it will really take on this printer, when the outcome store has
    enough past prints to judge (forecast.py). Optional: any problem only leaves it out."""
    est = (result.get("stats") or {}).get("estimated_time_seconds")
    if result.get("state") != "done" or not est or not _outcomes.is_available():
        return result
    try:
        status = await c.get_status()
        printer = _ptarget.printer_id((status.get("presets") or {}).get("printer"))
        fc = _forecast.time_forecast(est, status.get("app_version"), printer)
    except Exception:  # noqa: BLE001
        fc = None
    if fc:
        result["forecast"] = fc
    return result


@mcp.tool()
async def get_slice_status() -> dict:
    """Read the state, time and filament of the current or last slice, and whether it is out of date; for the warnings alone use get_slice_warnings.

    state is slicing, done, error or idle; stats (print time and filament use) appear when it is
    done, with any warnings or errors. Read-only.

    Poll this after slice to follow progress and read the result; 'idle' means no slice has run
    or it was cancelled. For only the pass/fail warnings use get_slice_warnings; for a
    per-feature time and filament breakdown use get_slice_breakdown.

    When the outcome store holds enough finished prints, `forecast` says how long this slice will
    really take from start to finish on this printer (OrcaSlicer's estimate corrected by how past
    prints compared with theirs, per OrcaSlicer version when possible); relay its summary."""
    try:
        async with _client() as c:
            st = await c.slice_status()
            valid = None
            if st.get("state") == "done" and "valid" not in st and "stale" not in st:
                try:  # builds without valid/stale on /slice/status: ask /status
                    valid = (await c.get_status()).get("slice_result_valid")
                except Exception:  # noqa: BLE001 - only decides the out-of-date note
                    pass
            return await _with_forecast(c, summarize_slice(st, valid))
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_slice_warnings() -> dict:
    """Read only the warnings and errors of the last slice and whether it still holds; the quick check that a fix cleared a warning.

    NOTE: only as complete as the API exposes. On the current fork build this may report
    valid with an empty warnings list even when the GUI shows a plate-boundary toast -
    the fork must populate the plater warning list (tracked as the fork batch). Once it
    does, this reports the real warnings with no change here."""
    try:
        async with _client() as c:
            st = await c.slice_status()
            status = await c.get_status()
        s = summarize_slice(st, status.get("slice_result_valid"))
        out = {"state": s["state"], "valid": status.get("slice_result_valid"),
               "warnings": s["warnings"], "errors": st.get("errors", []),
               "message": s["message"]}
        if s.get("out_of_date"):
            out["out_of_date"], out["note"] = True, s["note"]
        if status.get("backend") == "plugin":
            out["warnings_visible"] = False
            out["note"] = (out.get("note", "") + " The OrcaSlicer MCP plugin can't see OrcaSlicer's slice "
                           "warnings; an empty list means nothing. Ask the user to check the OrcaSlicer window "
                           "for any.").strip()
        return out
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_slice_breakdown() -> dict:
    """Read where the time and filament of the last slice go, per feature (walls, infill, support), with a check for speeds capped by the flow limit.

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
            valid = None
            if status.get("state") == "done" and "valid" not in status and "stale" not in status:
                try:
                    valid = (await c.get_status()).get("slice_result_valid")
                except Exception:  # noqa: BLE001 - only decides the out-of-date note
                    pass
        out = build_breakdown(status, cfg)
        if not out.get("available") and await _is_plugin():
            out["reason"] = ("the OrcaSlicer MCP plugin can't read the per-feature breakdown; that needs the "
                             "OrcaSlicer MCP build. get_slice_status has the totals of the user's slice.")
        if is_out_of_date(status, valid):
            out["out_of_date"], out["note"] = True, OUT_OF_DATE_NOTE
        return out
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def cancel_slice() -> dict:
    """Stop a running slice, or clear a slice stuck in 'slicing'; safe to call when nothing is running."""
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
    """Slice the current plate and wait for the finished time, filament and warnings; the usual way to slice (slice returns at once instead).

    If the plate is already sliced and unchanged, it returns the existing result without
    re-slicing. With enough recorded prints, `forecast` says how long it will really take on this
    printer (see get_slice_status).

    This is the usual way to slice when you want the outcome immediately. For a non-blocking
    start, use slice then poll get_slice_status; to sweep one setting across values, use
    compare_settings."""
    try:
        async with _client() as c:
            try:
                started = await _start_slice(c)
            except Validation as e:
                return await _slice_err(c, e)
            if started.get("already_valid"):
                return await _with_forecast(c, summarize_slice(await c.slice_status()))
            return await _with_forecast(c, summarize_slice(await _wait_for_slice(c, timeout)))
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def apply_and_slice(
    changes: Annotated[dict, Field(description=(
        "Map of OrcaSlicer config key to new value to apply before slicing, e.g. "
        "{'layer_height': 0.2}. Same format and validation as set_config (null puts a key back to its "
        "saved preset value); discover keys with search_settings or find_config_keys."))],
) -> dict:
    """Change settings and slice in one call, returning both; use set_config and then slice_and_wait to keep the steps apart.

    Returns {applied, errors, result}. The changes are atomic (any invalid key rejects the whole
    batch) and unsaved, exactly like set_config, so they revert if the preset is reselected.

    Use this to test the effect of a tweak in a single call. Use set_config then slice_and_wait
    to keep the steps separate, or compare_settings to try several values of one key."""
    try:
        async with _client() as c:
            if (refusal := await _plugin_refusal(c)) is not None:  # it cannot slice
                return refusal
            applied = await _put_config_with_reverts(c, changes)
            if applied.get("errors") and not applied.get("applied"):
                return {"applied": [], "errors": applied["errors"], "result": None}
            try:
                started = await _start_slice(c)
            except Validation as e:
                return {"applied": applied.get("applied", []),
                        "errors": applied.get("errors", {}), "result": await _slice_err(c, e)}
            if not started.get("already_valid"):
                await _wait_for_slice(c, 300)
            result = summarize_slice(await c.slice_status())
            return {"applied": applied.get("applied", []),
                    "errors": applied.get("errors", {}), "result": result}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def compare_settings(key: str, values: list, extra: dict | None = None) -> dict:
    """Slice once per value of a single setting and return the raw numbers, putting the value back after; compare_slices adds deltas and a verdict.

    The original value is put back even on error.

    Use it for a quick sweep of a single key. To compare named combinations of settings with
    ready-to-relay deltas and a verdict, use compare_slices instead. Like compare_slices it adds
    one line to OrcaSlicer's MCP panel, not one per trial."""
    if not values:
        return {"error": "no_values", "detail": "Give at least one value of the setting to compare."}
    try:
        async with _client() as c:
            if (refusal := await _plugin_refusal(c)) is not None:  # it cannot slice
                return refusal
            snapshot_keys = [key] + list((extra or {}).keys())
            rows = []
            restore_error = None
            trial = _client_mod.TRIAL.set(True)
            try:
                originals = await c.get_config(snapshot_keys)
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
                            row["error"] = await _variant_error(c, e)
                        rows.append(row)
                finally:
                    if originals:
                        try:
                            await c.restore_config(originals)
                        except ApiError as e:
                            restore_error = str(e)  # preserve collected rows even if restore fails
            finally:
                _client_mod.TRIAL.reset(trial)
            await c.post_note("slice", _compare_note(len(values), restore_error is None))
            result = {"key": key, "rows": rows}
            if restore_error is not None:
                result["restore_error"] = restore_error
                result["restore_failed"] = await _restore_failed(c, list(originals), restore_error)
            return result
    except ApiError as e:
        return _err(e)


async def _restore_failed(c, keys: list[str], error: str) -> dict:
    """A comparison that couldn't put the settings back says so, with the keys, in its reply and
    as a 'refused' row in OrcaSlicer's MCP panel (R45)."""
    keys = sorted(keys)
    labels = ", ".join(_setting_label(k) for k in keys)
    detail = (f"The comparison couldn't put back {labels} ({', '.join(keys)}); they may still hold "
              "the last option's values. Tell the user to check them in OrcaSlicer.")
    await c.post_note("refused", f"Comparison couldn't put back: {labels}", "Not put back",
                      f"They may still hold the last option's values ({error[:200]}).")
    return {"keys": keys, "detail": detail}


def _setting_label(key: str) -> str:
    rec = settings_schema.describe(key) or {}
    return rec.get("full_label") or rec.get("label") or key


def _compare_note(n: int, restored: bool) -> str:
    """The one panel row a comparison leaves: its trial writes and slices record nothing."""
    tail = "nothing kept" if restored else "settings may not be back as they were"
    return f"Compared {n} option{'s' if n != 1 else ''} \u00b7 {tail}"


async def _variant_error(c, e: ApiError) -> str:
    refusal = await _slice_refusal(c, e)
    if refusal is not None:
        return f"{refusal['error']}: {refusal['detail']}"
    return str(e)


@mcp.tool()
async def compare_slices(variants: list[dict], baseline: str | None = None,
                         detail: bool = False, timeout: int = 300) -> dict:
    """Slice under 2-8 named combinations of settings and compare time, filament and warnings with ready-made deltas and a verdict, putting the settings back after; compare_settings only sweeps one setting.

    Each variant is {"name": str, "changes": {setting: value}}; changes={} means the
    current config as-is (a natural baseline row). Applies each variant over the ORIGINAL
    config (resetting between variants, so they don't stack), slices it, then restores your
    config exactly as it was - nothing is left changed (slice validity is left false, as
    after any un-resliced edit).

    Returns a ready-to-relay `headline` and `table_markdown`, plus structured `variants`.
    All deltas and percentages are ALREADY computed against `baseline` (defaults to the
    changes={} variant, else the first) from the rounded minutes and grams the table shows -
    relay them as given rather than recomputing. `recommended` (starred in the table) is set
    only when one variant is at least as good as every other on time, filament and warnings.
    In a trade-off it is null and the headline reads "No clear winner: X is fastest, Y is
    lightest."; `tradeoff` names them, so leave the choice to the user.

    The trial writes and slices are not recorded in OrcaSlicer's MCP panel; one line such as
    "Compared 3 options · nothing kept" is added instead.

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
            if (refusal := await _plugin_refusal(c)) is not None:  # it cannot slice
                return refusal
            union = sorted({k for v in variants for k in (v.get("changes") or {})})
            results: list[dict] = []
            restore_error = None
            snapshot: dict = {}
            # Every request of the trial, the final restore included, carries the trial header,
            # so OrcaSlicer's MCP panel records none of it; one note sums it up afterwards.
            trial = _client_mod.TRIAL.set(True)
            try:
                snapshot = await c.get_config(union) if union else {}
                try:
                    for v in variants:
                        changes = v.get("changes") or {}
                        r = {"name": v["name"], "changes": changes, "time_s": None,
                             "filament_g": None, "warnings": [], "valid": False,
                             "error": None, "roles": None}
                        try:
                            if snapshot:
                                await c.restore_config(snapshot)  # reset to baseline so variants don't stack
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
                            r["error"] = await _variant_error(c, e)
                        results.append(r)
                finally:
                    if snapshot:
                        try:
                            await c.restore_config(snapshot)
                        except ApiError as e:
                            restore_error = str(e)
            finally:
                _client_mod.TRIAL.reset(trial)
            await c.post_note("slice", _compare_note(len(variants), restore_error is None))
            failed = (await _restore_failed(c, list(snapshot), restore_error)
                      if restore_error is not None else None)
    except ApiError as e:
        return _err(e)

    out = compute_comparison(results, baseline, detail)
    out["restored"] = restore_error is None
    if restore_error is not None:
        out["restore_error"] = restore_error
        out["restore_failed"] = failed
        out["headline"] = failed["detail"] + " " + out["headline"]
    return out


def _m4_err(e: ApiError, milestone: str) -> dict:
    # Only a route-level 404 means the build lacks the capability; a resource-level
    # 404 (unknown_object / unknown_preset / missing file) must keep its message (F7).
    if isinstance(e, NotFound) and e.route_missing:
        if e.plugin:
            return _plugin_text(_CURRENT_TOOL.get())
        return {"error": f"not available on this OrcaSlicer build (needs {milestone})"}
    return _err(e)


def _m4a_err(e: ApiError) -> dict:
    return _m4_err(e, "M4a")


def _m4b_err(e: ApiError) -> dict:
    return _m4_err(e, "M4b")


def _m4c_err(e: ApiError) -> dict:
    return _m4_err(e, "M4c")


def _plates_err(e: ApiError) -> dict:
    return _m4_err(e, "fork v2.4.2-mcp.13 or later")


# What a project route's error reply carries besides the token, kept so the agent can
# act on it: which file, why, the keys a policy refused, and the state of the project
# an unsaved-changes refusal is about.
_PROJECT_ERR_FIELDS = ("detail", "path", "keys", "index", "count", "dir", "project", "modified")


def _project_err(e: ApiError) -> dict:
    out = _m4_err(e, "the project routes, fork v2.4.2-mcp.15 or later")
    if isinstance(e, NotFound) and e.route_missing:
        return out
    if not out.get("errors"):
        out.pop("errors", None)
    body = getattr(e, "body", None) or {}
    for k in _PROJECT_ERR_FIELDS:
        if k in body:
            out[k] = body[k]
    if body.get("error") == "blocked_by_remote_api_policy":
        if body.get("keys"):
            out["hint"] = ("OrcaSlicer refused to open this project: it brings its own script, "
                           "G-code or connection settings (listed in keys) and Preferences > "
                           "Remote API > 'Allow script, G-code and connection edits' is off. Tell "
                           "the user; they can open it in the OrcaSlicer window, where OrcaSlicer "
                           "asks about modified G-code itself.")
        else:
            out["hint"] = ("OrcaSlicer refused: deleting a project file, or saving or moving onto "
                           "an existing one, needs Preferences > Remote API > 'Allow deleting and "
                           "replacing project files', which is off. Pick a free name, or ask the "
                           "user to do it in OrcaSlicer.")
    return out


def _slots_err(e: ApiError) -> dict:
    out = _m4_err(e, "fork v2.4.2-mcp.14 or later")
    body = getattr(e, "body", None) or {}
    extra = ""
    entry = body.get("entry")
    if entry:
        extra += " for entry " + json.dumps(entry)
    if body.get("reason"):
        extra += " (" + str(body["reason"]) + ")"
    if body.get("message"):
        extra += ": " + str(body["message"])
    if extra and "error" in out:
        out["error"] += extra
    return out



@mcp.tool()
async def list_objects() -> dict:
    """List the objects in the project with their ids, names, sizes, placement and plate; the object tools take these ids.

    Each has a stable id, name, size_mm, transform (offset/rotation/scale) and, on fork
    v2.4.2-mcp.13 and later, the 0-based plate it sits on (-1 when it is off every plate).
    """
    try:
        async with _client() as c:
            return await c.get_objects()
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def set_object_config(object_id: int, changes: dict) -> dict:
    """Change settings for one object only, all or nothing; use set_config to change them for the whole plate.

    Example: {"wall_loops": 4, "sparse_infill_density": "30%"}.
    """
    try:
        async with _client() as c:
            return await c.set_object_config(object_id, changes)
    except ApiError as e:
        return _m4c_err(e)


@mcp.tool()
async def duplicate_object(object_id: int) -> dict:
    """Add one copy of an object next to it on the plate; call it again for more copies."""
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
    """Remove one object from the plate by id; this cannot be undone through these tools.

    The other objects keep their ids.

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
    """Move, rotate or scale one object by id; use move_object_to_plate to change its plate, or arrange_plate to tidy the whole plate.

    translate=[dx,dy,dz] mm (relative), rotate=[rx,ry,rz] degrees (relative), scale=[sx,sy,sz]
    absolute factor. Provide at least one.

    On fork v2.4.2-mcp.13 and later the object then belongs to whichever plate it sits on,
    as after a drag in the window; list_objects shows the plate. To send an object to
    another plate, move_object_to_plate is simpler than working out coordinates."""
    try:
        async with _client() as c:
            return await c.transform_object(object_id, translate, rotate, scale)
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def arrange_plate() -> dict:
    """Let OrcaSlicer pack every object on the unlocked plates (Arrange all); use transform_object to move just one.

    It runs as a background job: poll get_job_status until idle.

    With several plates this is OrcaSlicer's "Arrange all": it re-packs the objects of every
    unlocked plate together and deletes plates left empty at the end, which undoes a
    one-part-per-plate layout. To shift one object, use transform_object instead."""
    try:
        async with _client() as c:
            return await c.arrange()
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def auto_orient() -> dict:
    """Let OrcaSlicer turn every object to its best printing orientation; use transform_object to rotate one by a set angle.

    It runs as a background job: poll get_job_status until idle.
    """
    try:
        async with _client() as c:
            return await c.orient()
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def list_plates() -> dict:
    """List the project's plates with their objects and which one is current; the plate tools take the 0-based index shown here.

    For each plate: its 0-based index, name, whether it is current (the one slice, get_gcode,
    render_plate and get_job_status act on), lock state, build volume and the objects it holds.
    Also says how many plates the build allows (max) and whether one can be added or deleted
    right now. Read-only.

    Plate numbers shown in the OrcaSlicer window are 1-based; every tool here takes the
    0-based index from this list. Needs fork v2.4.2-mcp.13 or later."""
    try:
        async with _client() as c:
            return await c.get_plates()
    except ApiError as e:
        return _plates_err(e)


_BAMBU_AMS_HINT = ("If the printer is a Bambu Lab one, reading its AMS needs the OrcaSlicer MCP build with the "
                   "bambu_ams capability.")


def _slots_reply(out: dict) -> dict:
    """An older build answers not_elegoo_cc2 for every printer that is no CC2, a Bambu one included."""
    if isinstance(out, dict) and out.get("supported") is False and out.get("reason") == "not_elegoo_cc2":
        out = {**out, "hint": _BAMBU_AMS_HINT}
    return out


@mcp.tool()
async def get_printer_slots() -> dict:
    """Read the printer's filament trays: a Bambu Lab printer's AMS and external spool (instant), or an Elegoo Centauri Carbon 2's CANVAS unit (slow, up to about 18 s). Read-only.

    The reply says which `kind` of printer answered. On a Bambu Lab printer (kind "bambu": the
    printer selected in OrcaSlicer's Device tab, read from the state OrcaSlicer's own connection
    keeps, nothing sent to the printer) each tray has its label as the AMS panel shows it (A1,
    HT-A, Ext), status (loaded, unknown, empty), filament_type as the AMS words it (ASA-Aero,
    PLA-CF, ...), filament_name, brand, colour, remain_percent, whether it is the tray feeding the
    nozzle (`active`), and `ams` summarises the units with their humidity. unknown_filament is
    true when a spool is in but the printer reports no type or colour for it: the user sets it
    in the AMS panel. Needs the OrcaSlicer MCP build with the bambu_ams capability.

    On an Elegoo CC2 (kind "canvas") each tray has its label, status (loaded, preloaded:
    filament waiting at the feeder; disconnected; error), material, brand and colour, and
    canvas_id and tray_id for set_filament_slot_mapping. OrcaSlicer's API serves one request at
    a time while that printer is asked, so other tools wait. Needs fork v2.4.2-mcp.14 or later.
    ready is true when the tray can be printed from now.

    When the printer is not supported or not reachable the reply is supported false with a
    reason (not_supported, no_printer_selected, printer_offline; on a CC2 not_elegoo_cc2,
    no_canvas, unreachable, too_many_clients, registration_refused, timeout, printer_error,
    firmware_too_old) and a message. Relay the message to the user."""
    try:
        async with _client() as c:
            return _slots_reply(await c.get_printer_slots())
    except ApiError as e:
        return _slots_err(e)


async def _bambu_mapping(c, mapping: dict) -> dict:
    """A Bambu mapping with each filament's match against the trays (ams_match.classify); the tray
    list is read for the soft matches, and skipped when it cannot be."""
    try:
        slots = await c.get_printer_slots()
    except ApiError:
        slots = None
    return _ams.classify(mapping, slots if isinstance(slots, dict) and slots.get("supported") else None)


@mcp.tool()
async def get_filament_slot_mapping() -> dict:
    """Show which printer tray each filament of the plate would print from on the next Send, and whether that tray holds what the slot's preset says (Bambu Lab AMS, or an Elegoo Centauri Carbon 2's CANVAS); read-only. The check to run before telling the user a plate is ready to send.

    The filaments listed are the ones on the plate the next Send would use, each with its
    0-based index (on a Bambu Lab printer also `slot`, the 1-based number the sidebar shows), type, preset name, colour,
    the tray it maps to (or null), the source of that choice and any warnings. valid is true when
    that Send would go through without a choice from the user; if not, problems says why. Nothing
    is sent to the printer. A supported false reply carries a reason to relay, as
    get_printer_slots does.

    On a Bambu Lab printer (kind "bambu", the OrcaSlicer MCP build with bambu_ams) the mapping is
    the one OrcaSlicer's Send dialog pre-fills: it takes a tray only when the AMS's filament type
    equals the slot preset's type, then the nearest colour. Each filament gets `match`: exact
    (its tray holds its type; `note` names the tray's filament when it is a different one of the
    same type), soft (no tray holds the type, but `candidates` hold the same base material, say
    Polymaker LW-ASA / ASA-Aero in A2 for a Generic ASA slot: the Send dialog takes it if the user
    picks it, so show both names and ask), none (nothing loaded fits: load a spool or change the
    slot's preset) or unknown. Relay `headline` and `warnings`; they already say what to do.

    On an Elegoo CC2 (kind "canvas", fork v2.4.2-mcp.14 or later) the source is saved, matched,
    user, api or none, and set_filament_slot_mapping can change it. It reads the printer when no
    slot list is cached, so it can take up to about 18 seconds, and OrcaSlicer's API serves one
    request at a time meanwhile, so other tools wait."""
    try:
        async with _client() as c:
            mapping = await c.get_slot_mapping()
            if isinstance(mapping, dict) and mapping.get("kind") == "bambu" and mapping.get("supported"):
                return await _bambu_mapping(c, mapping)
            return _slots_reply(mapping)
    except ApiError as e:
        return _slots_err(e)


@mcp.tool()
async def set_filament_slot_mapping(
    mapping: Annotated[list[dict], Field(description=(
        "One entry per filament of the plate the next Send uses (the indexes "
        "get_filament_slot_mapping lists): {filament: 0-based filament index, canvas_id, "
        "tray_id} from get_printer_slots."))],
) -> dict:
    """Choose which CANVAS tray each filament prints from on the next Send (Elegoo Centauri Carbon 2 only); it starts nothing.

    Returns the same reply as get_filament_slot_mapping, so check valid and problems. Needs fork
    v2.4.2-mcp.14 or later. Not for Bambu Lab printers: there the tray is chosen in OrcaSlicer's
    Send dialog, which keeps nothing this tool could set; get_filament_slot_mapping shows what it
    will pre-fill. It reads the printer when no slot list is cached, so it can take up
    to about 18 seconds, and OrcaSlicer's API serves one request at a time meanwhile, so other
    tools wait.

    The list REPLACES the whole mapping set through the API: a filament left out gets no tray
    from the API, and an empty list clears the API choices so Send falls back to the saved
    choices and automatic matching. Include every filament you want pinned.

    This starts nothing. OrcaSlicer's own Send dialog opens pre-filled with this mapping and
    the user still clicks Send. Each tray can serve only one filament (422 duplicate_tray), the
    tray must be ready (422 tray_not_ready), and the indexes must exist (422 unknown_filament_index, which
    also covers a project filament that is not on the plate; unknown_tray, slots_unavailable when the printer could not be read; the reply then
    says why). Malformed lists get 400: each filament at most once, and integers for
    filament, canvas_id and tray_id. Use get_printer_slots first to pick trays whose material and colour fit."""
    try:
        async with _client() as c:
            return await c.put_slot_mapping(mapping)
    except ApiError as e:
        return _slots_err(e)


@mcp.tool()
async def add_plate(
    select: Annotated[bool, Field(description=(
        "Make the new plate the current one, as the toolbar button does. Pass false to "
        "keep working on the plate that is current now."))] = True,
) -> dict:
    """Add an empty plate, like the toolbar's Add plate button, so parts can print separately.

    Returns the new plate's 0-based index for move_object_to_plate and select_plate.

    Use it when parts should print separately: load them, add a plate, move the second
    part across with move_object_to_plate, then select_plate and slice each one. A plate
    cannot be added while a slice is running (409) or at the build's plate limit (422).
    On OrcaSlicer 2.5, the 2nd, 5th and 10th plates widen the plate grid, which clears
    every plate's slice result, as the toolbar button does: slice again afterwards."""
    try:
        async with _client() as c:
            return await c.create_plate(select)
    except ApiError as e:
        return _plates_err(e)


@mcp.tool()
async def select_plate(
    index: Annotated[int, Field(description=(
        "0-based plate index from list_plates (the window shows plates 1-based)."))],
) -> dict:
    """Make a plate the current one, so slicing, G-code, renders and slice status act on it.

    get_slice_status then reports that plate's own result: its stats if it is still sliced,
    otherwise idle with "plate N is not sliced". Switching plates leaves unsaved config
    overrides alone; it changes only which plate is sliced. Selecting the plate that is
    already current is a no-op."""
    try:
        async with _client() as c:
            return await c.select_plate(index)
    except ApiError as e:
        return _plates_err(e)


@mcp.tool()
async def delete_plate(
    index: Annotated[int, Field(description=(
        "0-based plate index from list_plates. Later plates shift down one index."))],
) -> dict:
    """Delete a plate; the objects on it are parked beside the bed, not deleted.

    OrcaSlicer parks them as the toolbar's delete does, and the reply says how many were moved
    off-plate (objects_moved_off_plate). Use move_object_to_plate to put them somewhere
    printable. OrcaSlicer parks them where the next plate appears, so an add_plate straight
    after a delete picks them back up.

    The only plate cannot be deleted (422 last_plate). Call list_plates first for the index;
    this cannot be undone through the API."""
    try:
        async with _client() as c:
            return await c.delete_plate(index)
    except ApiError as e:
        return _plates_err(e)


@mcp.tool()
async def move_object_to_plate(
    object_id: Annotated[int, Field(description=(
        "Integer id of the object, from the 'id' field of list_objects."))],
    plate: Annotated[int, Field(description=(
        "0-based index of the destination plate, from list_plates or add_plate."))],
    instance: Annotated[int, Field(description=(
        "Which copy to move when the object has several (duplicate_object); 0 is the "
        "original."))] = 0,
) -> dict:
    """Move an object onto another plate by its index; simpler than transform_object for changing plates.

    Coming from a plate it keeps its position relative to that plate (front-left of plate 1
    lands front-left of plate 2); coming from off-plate (parked beside the bed) it lands at the
    destination's centre. If it then overlaps a part, shift it with transform_object;
    arrange_plate would re-pack every plate together.

    list_objects reports each object's plate (-1 when off every plate). Moving leaves both
    plates' slices invalid, so re-slice. An object already on that plate is left where it
    is (moved: false); a locked plate at either end refuses the move (plate_locked)."""
    try:
        async with _client() as c:
            return await c.set_object_plate(object_id, plate, instance)
    except ApiError as e:
        return _plates_err(e)


@mcp.tool()
async def get_project() -> dict:
    """Read the open project as a file (name, path, unsaved changes); check it before save_project, open_project or new_project.

    Also reports whether it has been saved, plate and object counts, the preset groups with
    unsaved edits (modified) and how many entries File > Recent holds; path is empty until the
    first save. Read-only. Needs fork v2.4.2-mcp.15 or later.
    """
    try:
        async with _client() as c:
            return await c.get_project()
    except ApiError as e:
        return _project_err(e)


@mcp.tool()
async def save_project(
    path: Annotated[str | None, Field(description=(
        "Where to save, a .3mf path on the OrcaSlicer host (Save As). None saves to the "
        "project's own path (Save); that fails with no_project_path when the project has "
        "never been saved."))] = None,
    overwrite: Annotated[bool, Field(description=(
        "Replace an existing file at `path`. Off by default: a collision is an error, "
        "never a silent clobber. Saving the project back onto its own path is a plain "
        "Save and needs no overwrite."))] = False,
) -> dict:
    """Save the open project as a .3mf with its plates and settings (File > Save or Save As); save_gcode saves the sliced G-code instead.

    It writes exactly what File > Save / Save Project As writes: plates, object placement,
    per-object and per-plate settings, height-range modifiers, slot presets and colours, and
    unsaved config overrides. No dialog opens; OrcaSlicer's own prompts are answered from the
    arguments. To create a new project file, call new_project and then save_project with a path.

    Afterwards get_status reports the new path and dirty false, and the file joins File >
    Recent. Save early: everything set up through these tools lives only in the open session
    until it is saved, and is lost if OrcaSlicer closes. A save is refused while a slice runs
    (slicing). Replacing an existing file also needs Preferences > Remote API > 'Allow
    deleting and replacing project files' in OrcaSlicer (blocked_by_remote_api_policy
    otherwise). Needs fork v2.4.2-mcp.15 or later."""
    try:
        async with _client() as c:
            return await c.save_project(path, overwrite)
    except ApiError as e:
        return _project_err(e)


@mcp.tool()
async def open_project(
    path: Annotated[str, Field(description="A .3mf project on the OrcaSlicer host.")],
    if_unsaved: Annotated[Literal["save", "discard", "fail"], Field(description=(
        "What to do when the current project has unsaved changes: 'fail' (default) refuses "
        "and changes nothing, 'save' writes it to its own path first (fails with "
        "no_project_path if it has none), 'discard' throws the changes away."))] = "fail",
    presets: Annotated[Literal["project", "keep_current"], Field(description=(
        "'project' (default) selects the printer, print and filament presets stored in the "
        "project, as OrcaSlicer does. 'keep_current' opens the project and then selects the "
        "presets that were active before the call again (for names that still exist); the "
        "project's own config overrides are not kept in that mode."))] = "project",
) -> dict:
    """Open a saved .3mf project with its plates and settings, replacing the open one; load_model only adds a model's geometry.

    As with File > Open Project, plates, objects, per-object and per-plate settings, slot
    presets and colours all come back. Use open_recent_project for an entry of File > Recent and
    browse_projects to find project files in a folder.

    The two prompts OrcaSlicer would show are answered from the arguments, so nothing blocks.
    Unsaved preset overrides are dropped before the open because the project's config
    replaces them; the reply names those groups in discarded_changes. Call get_project first
    if you are unsure whether the user has unsaved work in the window, and pass
    if_unsaved='save' or 'discard' deliberately. A project that brings its own scripts, custom
    G-code or printer connection is refused (blocked_by_remote_api_policy, with the keys)
    unless the user allows those edits in OrcaSlicer's Preferences; nothing changes then.
    Needs fork v2.4.2-mcp.15 or later."""
    try:
        async with _client() as c:
            return await c.open_project(path, if_unsaved, presets)
    except ApiError as e:
        return _project_err(e)


@mcp.tool()
async def open_recent_project(
    index: Annotated[int, Field(description=(
        "0-based position in list_recent_projects (0 is the most recent)."), ge=0)],
    if_unsaved: Annotated[Literal["save", "discard", "fail"], Field(description=(
        "What to do when the current project has unsaved changes: 'fail' (default) refuses "
        "and changes nothing, 'save' writes it to its own path first, 'discard' throws the "
        "changes away."))] = "fail",
    presets: Annotated[Literal["project", "keep_current"], Field(description=(
        "'project' (default) selects the presets stored in the project; 'keep_current' "
        "selects the presets active before the call again."))] = "project",
) -> dict:
    """Open an entry of File > Recent by its position in list_recent_projects; open_project takes a path instead.

    Same behaviour and arguments as open_project. The list can change between calls (a save or
    an open moves its file to the top), so list first and open straight after; a file no longer
    on disk answers file_not_found and forget_recent_project removes the entry. Needs fork
    v2.4.2-mcp.15 or later.
    """
    try:
        async with _client() as c:
            return await c.open_project(None, if_unsaved, presets, recent=index)
    except ApiError as e:
        return _project_err(e)


@mcp.tool()
async def new_project(
    if_unsaved: Annotated[Literal["save", "discard", "fail"], Field(description=(
        "What to do when the current project has unsaved changes: 'fail' (default) refuses "
        "and changes nothing, 'save' writes it to its own path first (fails with "
        "no_project_path if it has none), 'discard' throws the changes away."))] = "fail",
    name: Annotated[str | None, Field(description=(
        "A title for the new project, shown in the window; 'Untitled' when omitted. The file "
        "name is chosen when save_project is called with a path."))] = None,
) -> dict:
    """Start a new, empty project (File > New Project) without a dialog.

    The unsaved-changes prompt is answered by if_unsaved. OrcaSlicer's second prompt, keep or
    discard modified presets, is answered with Keep: unsaved preset overrides carry over into
    the new project and the reply names them in kept_changes (select_preset drops them if that
    is not wanted). Follow with load_model and save_project to create a project file. Needs fork
    v2.4.2-mcp.15 or later.
    """
    try:
        async with _client() as c:
            return await c.new_project(if_unsaved, name)
    except ApiError as e:
        return _project_err(e)


@mcp.tool()
async def list_recent_projects() -> dict:
    """List File > Recent, most recent first, with the index that open_recent_project takes.

    For each entry: its path, project name, whether the file still exists, whether it is the
    open project, and its size and last-modified time. Read-only. Needs fork v2.4.2-mcp.15 or
    later.
    """
    try:
        async with _client() as c:
            return await c.get_recent_projects()
    except ApiError as e:
        return _project_err(e)


@mcp.tool()
async def forget_recent_project(
    path: Annotated[str | None, Field(description=(
        "The entry's path as list_recent_projects shows it (any spelling of the same file "
        "works while the file exists)."))] = None,
    index: Annotated[int | None, Field(description=(
        "Alternatively, the entry's 0-based index from list_recent_projects."), ge=0)] = None,
    all: Annotated[bool, Field(description="Clear the whole list instead.")] = False,
) -> dict:
    """Remove an entry from File > Recent, or clear the list; the file on disk stays.

    By path or index. Useful after a file was deleted or moved outside OrcaSlicer. The reply
    names what was forgotten and returns the list as it is now. Needs fork v2.4.2-mcp.15 or
    later.
    """
    if not all and path is None and index is None:
        return {"error": "nothing_to_forget", "detail": "pass path, index or all=True"}
    try:
        async with _client() as c:
            return await c.forget_recent_project(path=path, index=index, all=all)
    except ApiError as e:
        return _project_err(e)


@mcp.tool()
async def browse_projects(
    dir: Annotated[str | None, Field(description=(
        "A folder on the OrcaSlicer host. None uses the open project's folder, or the folder "
        "OrcaSlicer's last file dialog used."))] = None,
) -> dict:
    """List the .3mf project files in one folder on the OrcaSlicer machine; list_recent_projects shows File > Recent instead.

    Not recursive, newest first: path, project name, size and last-modified time, and whether
    each is the open project. Read-only. Use it to find a project to open_project, or to pick a
    free name before save_project. Needs fork v2.4.2-mcp.15 or later.
    """
    try:
        async with _client() as c:
            return await c.browse_projects(dir)
    except ApiError as e:
        return _project_err(e)


@mcp.tool()
async def move_project_file(
    path: Annotated[str, Field(description="The .3mf file to move or rename, on the OrcaSlicer host.")],
    to: Annotated[str, Field(description="Its new .3mf path (a new name in the same folder renames it).")],
    overwrite: Annotated[bool, Field(description=(
        "Replace an existing file at `to`. Off by default. The open project's own file is "
        "never replaced."))] = False,
) -> dict:
    """Move or rename a .3mf project file on disk; the window follows when it is the open project.

    When it is the open project's file, the title, project name and File > Recent show the new
    path, and the next save_project writes there. Any other recent-list entry naming the old
    path is pointed at the new one. The file's content and the project's dirty state do not
    change. Fails with exists when something is already at `to` and overwrite is off; replacing
    it also needs Preferences > Remote API > 'Allow deleting and replacing project files' in
    OrcaSlicer. Needs fork v2.4.2-mcp.15 or later.
    """
    try:
        async with _client() as c:
            return await c.move_project_file(path, to, overwrite)
    except ApiError as e:
        return _project_err(e)


@mcp.tool()
async def delete_project_file(
    path: Annotated[str, Field(description="The .3mf file to delete, on the OrcaSlicer host.")],
) -> dict:
    """Delete a .3mf project file from disk for good; forget_recent_project only takes it off File > Recent.

    There is no trash, and the file leaves File > Recent too. Only .3mf files can be deleted
    this way, and never the open project's own file: new_project or open_project first. Confirm
    with the user before calling this on a file they did not just ask to remove. OrcaSlicer
    refuses it (blocked_by_remote_api_policy) unless the user ticked Preferences > Remote API >
    'Allow deleting and replacing project files'. Needs fork v2.4.2-mcp.15 or later.
    """
    try:
        async with _client() as c:
            return await c.delete_project_file(path)
    except ApiError as e:
        return _project_err(e)


@mcp.tool()
async def get_job_status() -> dict:
    """Check whether arranging or orienting is still running; wait for idle before reading positions or slicing.

    Read-only.

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
    """Listen to OrcaSlicer's live events (slices, setting changes, projects opened) for a few seconds.

    Events: slice.*, config.changed and project.opened, over a bounded window.
    """
    try:
        async with _client() as c:
            return {"events": await c.collect_events(seconds=seconds)}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def find_config_keys(substring: str) -> dict:
    """Find setting keys that contain a piece of text, from the live settings; search_settings also matches labels and descriptions, offline.

    Helps discover among the ~600 keys.
    """
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
    """Start here when a plate won't slice or fit: status, objects, bed and skirt or brim settings and slice warnings in one call.

    One call instead of chaining status, objects and config.

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
            sl = summarize_slice(await c.slice_status(), status.get("slice_result_valid"))
        return {"status": status, "objects": objs, "adhesion_bed": cfg,
                "slice": {"state": sl["state"], "warnings": sl["warnings"],
                          "valid": status.get("slice_result_valid"),
                          **({"out_of_date": True, "note": sl["note"]} if sl.get("out_of_date") else {})}}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def check_placement() -> dict:
    """Estimate before slicing whether every object, with its skirt or brim, fits the bed; get_slice_warnings has the exact verdict after.

    Returns per-object fit, expanded first-layer bbox, per-edge clearance (mm), and overflow.

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
    """Look up curated slicing advice and saved notes for a goal or symptom; call it before choosing settings.

    ALWAYS call before deriving or changing settings for a user goal. Composes principles per
    situation, never returns preset bundles. Falls back to find_config_keys/web search if empty.

    When recommending, present 2-3 concrete options quantified with
    predicted print time and filament mass from real slice results (slice +
    status tools) - never adjectives alone."""
    chunks = [{"file": c.relpath, "title": c.title, "content": c.body}
              for c in search_knowledge(query)]
    return {"chunks": chunks, "notes": _notes.search_notes(query)}


@mcp.tool()
async def check_profile_physics(changes: dict | None = None) -> dict:
    """Check the live settings, plus optional proposed changes, against flow, temperature, geometry and cooling limits; run it before save_preset.

    Deterministic: it fetches the live config, overlays the optional `changes` and runs the
    math. RUN THIS BEFORE save_preset. verdict=blocked means DO NOT SAVE.
    """
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
    """Save a short note for future sessions (about a printer, the user or a project); consult finds it again.

    scope: 'machine:<printer>/<filament>', 'user', or 'project:<name>'. Local plain files;
    user-readable and deletable.
    """
    return {"saved": str(_notes.append_note(note, scope))}


@mcp.tool()
def describe_setting(
    key: Annotated[str, Field(description=(
        "Exact OrcaSlicer config key, e.g. 'layer_height' or 'sparse_infill_density'. Find keys "
        "with search_settings or find_config_keys. Unknown keys return an error."))],
) -> dict:
    """Explain one setting from its key: label, meaning, type, unit, allowed range and choices; works offline.

    Authoritative, from OrcaSlicer's own source: label, tooltip, type, unit, valid range, enum
    values and default. Read-only, so it works even when OrcaSlicer is not running.

    Use it to learn a setting's exact type and allowed values before writing it with set_config
    or edit_preset. To find candidate keys by keyword first, use search_settings."""
    rec = settings_schema.describe(key)
    if rec is None:
        return {"error": "unknown_setting", "key": key, "detail": _ERROR_DETAILS["unknown_setting"]}
    return rec


@mcp.tool()
def search_settings(query: str, limit: int = 25) -> dict:
    """Find settings by words in their key, label or description, offline; describe_setting then explains one.

    Returns compact matches (key, label, category, short tooltip), ranked key and label matches
    first.
    """
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
        "4. recall_prints — if this model (or one like it) was printed before, say how it went "
        "(result, any verdict such as warped or stringing, and the settings used) BEFORE proposing changes.\n"
        "5. consult with the model's material and purpose before touching settings.\n"
        "6. slice_and_wait, then get_slice_warnings and get_slice_breakdown; report "
        "print time, filament mass, and any warnings. Call describe_plate and relay its summary "
        "before proposing any orientation or support change; use render_plate to show the result.\n"
        "7. If the user wants to print it, save_gcode (this records the slice so the real outcome "
        "can be learned from later) and hand the returned path to the printer tools.\n"
        "Do not start a print or change temperatures without asking me first."
    )


@mcp.prompt(name="optimize-print-time", title="Optimize print time",
            description="Data-driven print-time reduction from a real slice breakdown, never adjectives alone.")
def prompt_optimize_print_time(constraints: str = "") -> str:
    extra = f"\nMy constraints: {constraints}" if constraints else ""
    return (
        "Reduce the print time of the current plate, driven by data:\n"
        "1. recall_prints — check whether faster settings for this model already failed or "
        "warped before; do not re-propose a known failure.\n"
        "2. slice_and_wait (if no valid slice), then get_slice_breakdown — identify which "
        "feature roles actually dominate time.\n"
        "3. consult with 'print time optimization' plus the dominant roles; only propose "
        "levers the knowledge base or breakdown supports.\n"
        "4. Apply candidate changes with set_config, then check_profile_physics — drop "
        "anything blocked.\n"
        "5. Re-slice and quantify: present 2-3 options as minutes and grams versus the "
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
    """Load a model file (.stl, .obj, .3mf geometry, .step) from the OrcaSlicer machine onto the plate; open_project opens a saved project instead.

    The path is on the OrcaSlicer host. .step/.stp need fork v2.3.2-mcp.3 or later; large STEP
    files can take a minute to tessellate, and the call waits. A .3mf is imported as geometry
    only.
    """
    try:
        async with _client() as c:
            try:
                return await c.load_model(path)
            except NotFound as e:
                if e.body.get("error") == "file_not_found" or (
                        e.route_missing and await _has_capability(c, "model")):
                    # Builds before mcp.14 answer a missing file with the same
                    # {"error":"not_found"} as a missing route (F18); the model
                    # capability tells them apart.
                    return {"error": "file_not_found", "path": path,
                            "detail": f"no file at {path} on the OrcaSlicer machine"}
                raise
    except ApiError as e:
        return _m4a_err(e)


async def _plugin_refusal(c) -> dict | None:
    """For a tool that would change something before reaching a route the plugin lacks:
    the refusal to return up front, so nothing is left half done. None on the fork."""
    try:
        status = await c.get_status()
    except ApiError:
        return None  # let the tool's own call report the problem
    if status.get("backend") == "plugin":
        return _plugin_text(_CURRENT_TOOL.get())
    return None


async def _has_capability(c, name: str) -> bool:
    try:
        status = await c.get_status()
    except ApiError:
        return False
    return name in (status.get("capabilities") or [])


@mcp.tool()
async def select_preset(
    type: Annotated[str, Field(description=(
        "Which preset group to switch: 'print' (process/quality), 'filament' (material), "
        "or 'printer' (machine)."))],
    name: Annotated[str, Field(description=(
        "Exact name of an existing preset in that group, as returned by list_presets "
        "(e.g. '0.20mm Standard'). Unknown names are rejected."))],
    slot: Annotated[int | None, Field(description=(
        "Filament only: the 1-based filament slot (extruder/tool, e.g. 1-4 on a 4-head "
        "toolchanger) to set. Omit to switch the preset open in the Filament tab."))] = None,
) -> dict:
    """Switch the active print, filament or printer preset; without a filament slot this drops every unsaved edit.

    With `slot` (filament only), only that slot's filament preset changes, like picking it
    in the sidebar's combo for that slot, and no unsaved edits are discarded
    (set_filament_slot_preset does the same and can also set the slot's colour). On a
    single-filament printer slot 1 is the Filament tab's preset, so it behaves like the call
    without `slot` below. Needs a slicer build that lists 'preset_slot' in get_status
    capabilities; older builds would ignore the slot and switch the Filament tab instead, so
    this tool refuses there.

    Without `slot`: discards unsaved overrides in EVERY preset group, not just the one being switched: a
    filament switch also drops unsaved print and printer edits, including ones made by hand
    in the OrcaSlicer window. That is deliberate (OrcaSlicer consults the other groups when
    switching, and any unsaved one would open a modal dialog nothing can dismiss remotely),
    so call save_preset first if any unsaved edit should survive. Newer slicer builds report
    which groups were affected as 'discarded_changes' in the reply.

    This also makes it the canonical way to reset dirty config. It leaves the last slice
    invalid, so re-slice afterwards. Use list_presets for valid names. Because of this, make
    filament selections and edits before print-group edits (docs/order-of-operations.md)."""
    if slot is not None:
        if type != "filament":
            return {"error": "slot applies to type 'filament' only"}
        if slot < 1:
            return {"error": "slot is 1-based; use slot=1 for the first filament"}
    try:
        async with _client() as c:
            if slot is None:
                return await c.select_preset(type, name)
            caps = (await c.get_status()).get("capabilities") or []
            if "preset_slot" not in caps:
                return {"error": "not available on this OrcaSlicer build (needs per-slot "
                                 "filament select, capability 'preset_slot')"}
            return await c.select_preset(type, name, slot - 1)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def save_preset(
    type: str,
    name: str,
    detach: bool = False,
    filament_slot: Annotated[int | None, Field(description=(
        "Filament only: save from this slot's preset (1-based, as the sidebar numbers the slots) and point "
        "only this slot at the saved name; other slots that used the same preset keep it. Needs the "
        "OrcaSlicer MCP build with the filament_slots capability."))] = None,
    allow_temperature_pairs: Annotated[bool, Field(description=(
        "Filament only: save even though OrcaSlicer's temperature safety check finds the first-layer temperature "
        "far from the other layers' (its own 'Continue anyway?'). Only after the user has agreed to those "
        "temperatures."))] = False,
) -> dict:
    """Save the current settings as a named user preset; edit_preset changes a stored preset in one step instead.

    It creates or updates the preset, visible in the GUI immediately. type =
    print|filament|printer. detach=True saves it standalone instead of inheriting the current
    base preset; use it when creating a filament preset for a DIFFERENT material than the one
    selected, otherwise the new preset inherits the base's filament_type and temperatures.

    Saving a filament preset under a new name normally moves EVERY slot that used the old
    preset to the new one (OrcaSlicer's own Save As rule). With filament_slot only that slot
    moves: the reply's `slots_reassigned` is that slot, `slots_kept` the slots OrcaSlicer had
    moved and the API put back, and `filaments` the preset of each slot afterwards. The same
    binding as set_config with filament_slot applies, with the same filament_edits_unsaved
    refusal when the Filament tab holds unsaved edits to a different preset.

    OrcaSlicer 2.5 checks a filament preset's unsaved edits before saving: the first-layer
    temperature within 30 °C of the other layers' for the nozzle, 15 °C for each plate type.
    Past that the save is refused with error temperature_pairs_unsafe and the `pairs`; nothing
    is saved. Tell the user, then change the values or, with their agreement, save again with
    allow_temperature_pairs=true (the reply then lists them as temperature_pairs_allowed).

    Run check_profile_physics first; do not save when verdict=blocked."""
    if filament_slot is not None:
        if type != "filament":
            return {"error": "filament_slot applies to type 'filament' only"}
        if filament_slot < 1:
            return {"error": "filament_slot is 1-based; use filament_slot=1 for the first filament"}
    try:
        async with _client() as c:
            if filament_slot is None:
                return await c.save_preset(type, name, detach, allow_temperature_pairs=allow_temperature_pairs)
            if (missing := await _capability_missing(c, "filament_slots", _FILAMENT_SLOTS_NEED,
                                                     "save_preset:filament_slot")) is not None:
                return missing
            return _slot_reply(await c.save_preset(type, name, detach, filament_slot - 1,
                                                   allow_temperature_pairs=allow_temperature_pairs))
    except ApiError as e:
        if (refusal := _save_refusal(e, type)) is not None:
            return refusal
        return _slot_err(e) if filament_slot is not None else _m4a_err(e)


@mcp.tool()
async def get_filament_slots() -> dict:
    """List the project's filament slots as the sidebar shows them: each slot's number, preset, filament type, vendor, colour, whether its preset is open in the Filament tab with unsaved edits, and which slots share a preset. Read-only.

    One row per slot, numbered from 1. `preset` is the slot's filament preset; `filament_type`
    and `vendor` come from that preset, so a slot whose preset is 'Generic ASA' shows vendor
    Generic, type ASA; `colour` is the project's colour for the slot. `shares_preset_with`
    names the other slots loaded with the same preset: a set_config or save_preset on any of
    them reaches all of them. Only the preset open in the Filament tab can hold unsaved edits,
    so `modified` and `modified_keys` are set on the slots using it while it is dirty;
    `filament_tab` says which preset the tab edits and the slot it is bound to.

    Use it before set_config or save_preset with filament_slot, and to see at a glance when a
    slot's preset is not the material loaded (the printer's own trays are read with
    get_printer_slots). Needs the OrcaSlicer MCP build with the filament_slots capability;
    get_status lists the preset names alone on older builds."""
    try:
        async with _client() as c:
            return _filament_slots_view(await c.get_filaments())
    except ApiError as e:
        return _m4_err(e, _FILAMENT_SLOTS_NEED)


@mcp.tool()
async def set_filament_slot_preset(
    slot: Annotated[int, Field(description="The filament slot, 1-based as the sidebar numbers them.")],
    name: Annotated[str, Field(description="Exact name of a filament preset, as list_presets(type='filament') returns it.")],
    keep_colour: Annotated[bool, Field(description=(
        "Keep the slot's current colour (the default). False sets the colour the preset carries as its "
        "default (default_filament_colour), when it has one."))] = True,
) -> dict:
    """Point one filament slot at a named preset, leaving the other slots alone and never dropping an unsaved edit; the slot keeps its colour unless keep_colour is false.

    On a multi-filament project it is the same as picking the preset in that slot's sidebar
    combo: no unsaved print, filament or printer edit is touched. On a one-filament project the
    slot is the Filament tab's preset, and switching it would drop every unsaved edit, so while
    there are any the call is refused (`unsaved_edits`, naming them) and nothing changes: save
    them or put them back first. The reply ends with `filaments`, the slots afterwards as
    get_filament_slots lists them. Needs a build with the preset_slot capability; the colour
    change needs a writable filament_colour (any build). Unknown names and presets not
    compatible with the selected printer are refused."""
    if slot < 1:
        return {"error": "slot is 1-based; use slot=1 for the first filament"}
    try:
        async with _client() as c:
            if (missing := await _capability_missing(c, "preset_slot", "per-slot filament select")) is not None:
                return missing
            status = await c.get_status()
            loaded = [n for n in (status.get("presets") or {}).get("filament") or [] if n]
            if len(loaded) <= 1:
                # One filament: OrcaSlicer switches the Filament tab itself, which drops every unsaved edit.
                dirty = {g: list(dict.fromkeys(k.split("#", 1)[0] for k in _visible_keys(keys)))
                         for g, keys in (status.get("modified") or {}).items() if _visible_keys(keys)}
                if dirty:
                    groups = " and ".join(f"{g} ({', '.join(keys)})" for g, keys in dirty.items())
                    return {"error": (f"unsaved_edits: this project has one filament, so its slot is the Filament "
                                      f"tab's preset, and switching it would drop the unsaved edits to {groups}; "
                                      "nothing was changed"),
                            "modified": dirty,
                            "hint": ("save them first (save_preset), or put them back (set_config with each key set "
                                     "to null), then switch; or ask the user whether to drop them")}
            r = await c.select_preset("filament", name, slot - 1)
            out = {"slot": slot, "selected": r.get("selected", name),
                   "discarded_changes": r.get("discarded_changes") or [], "colour": "kept"}
            if not keep_colour:
                try:
                    saved = (await c.get_preset_config("filament", name)).get("config") or {}
                    default = _split_config_strings(saved.get("default_filament_colour") or "")[0].strip()
                    if not default:
                        out["colour"] = "kept: the preset carries no default colour"
                    else:
                        colours = _split_config_strings((await c.get_config(["filament_colour"])).get("filament_colour") or "")
                        if slot - 1 >= len(colours):
                            out["colour"] = f"kept: the project has {len(colours)} colour(s)"
                        else:
                            colours[slot - 1] = default
                            w = await c.put_config({"filament_colour": ";".join(colours)})
                            out["colour"] = {"set": default, "applied": w.get("applied", []), "errors": w.get("errors", {})}
                except ApiError as ce:
                    # The slot has switched already: only the colour step failed.
                    out["colour"] = {"error": "kept: the colour could not be changed, " + str(_err(ce).get("error", ce))}
            try:
                out["filaments"] = _filament_slots_view(await c.get_filaments())["slots"]
            except NotFound:
                pass  # a build with preset_slot but not yet the filament slot routes
            return out
    except ApiError as e:
        out = _m4a_err(e)
        body = getattr(e, "body", None) or {}
        if body.get("error") == "slot_out_of_range":
            out["error"] = f"slot_out_of_range: the project has {body.get('filament_count')} filament slot(s)"
        return out


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
    """List the print, filament and printer presets: the user's own and the selected ones, or all with include_system.

    Each with system/selected/visible flags.

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
    """Turn adaptive layer height on or off for one object; set_height_range sets a fixed height over a Z band instead.

    mode='adaptive' (quality 0..1, higher = finer detail) generates an adaptive profile;
    mode='reset' (aliases: 'default', 'none') restores uniform layers.
    """
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
    """Set a layer height over a Z band of one object; set_layer_height makes the whole object adaptive instead.

    For example 0 to 5 mm printed at 0.1 mm for finer detail near the base. Passing the same
    min_z and max_z again updates that band's height; clear=True removes every band on the
    object.

    Bands are per-object and invalidate the last slice, so re-slice afterwards. For a single
    height across the whole object use set_layer_height instead. Get the id from list_objects."""
    try:
        async with _client() as c:
            return await c.set_height_range(object_id, min_z, max_z, layer_height, clear)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def get_preset_config(type: str, name: str) -> dict:
    """Read every setting of a named preset without selecting it; get_config reads the live settings.

    type = print|filament|printer.
    """
    try:
        async with _client() as c:
            return await c.get_preset_config(type, name)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def delete_preset(type: str, name: str) -> dict:
    """Delete one of the user's presets (never a system preset or the selected one).

    type = print|filament|printer.
    """
    try:
        async with _client() as c:
            return await c.delete_preset(type, name)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def edit_preset(
    type: str,
    name: str,
    changes: dict,
    allow_temperature_pairs: Annotated[bool, Field(description=(
        "Filament only: save even though OrcaSlicer's temperature safety check finds the first-layer temperature "
        "far from the other layers' (its own 'Continue anyway?'). Only after the user has agreed to those "
        "temperatures."))] = False,
) -> dict:
    """Change a stored preset and save it under the same name, after the physics check; set_config changes only the open project.

    It selects the preset, applies the changes atomically and saves under the same name. Runs
    the check_profile_physics gate first (F15) and refuses with error=physics_blocked if the
    changes would INTRODUCE a failing physics check (pre-existing failures do not block
    unrelated edits). For type='filament', checks that also depend on the currently selected
    PRINT preset (flow_ceiling, temp_vs_flow) do not block; they come back as
    cross_layer_warnings, because a filament preset pairs with many print presets.

    OrcaSlicer 2.5's temperature safety check applies to the save as in save_preset: past it
    the reply is temperature_pairs_unsafe with the pairs, and the changes stay unsaved.
    """
    try:
        async with _client() as c:
            await c.guard_preset_edit(type, name, changes)  # refuse before selecting anything
            if (refusal := await _plugin_refusal(c)) is not None:  # it cannot save presets
                return refusal
            await c.select_preset(type, name)
            cfg = await c.get_config(None)
            overlay = dict(cfg) | {k: str(v) for k, v in changes.items()}
            fails_before = {r.name for r in run_checks(cfg) if r.status == "fail"}
            fails_after = [r for r in run_checks(overlay) if r.status == "fail"]
            new_fails = [r for r in fails_after if r.name not in fails_before]
            # A filament preset pairs with many print presets, so a check that mixes
            # its inputs with the currently selected print preset's speeds cannot
            # fairly block the filament edit; it is reported instead. (Print-preset
            # edits are still blocked by those checks: they are tuned for the
            # filament selected at the time.)
            blocking, cross_layer = [], []
            for r in new_fails:
                layers = CHECK_LAYERS.get(r.name, frozenset({"print", "filament", "printer"}))
                if type == "filament" and not layers <= {"filament", "printer"}:
                    cross_layer.append(r)
                else:
                    blocking.append(r)
            if blocking:
                return {"error": "physics_blocked", "preset": name,
                        "fails": [{"name": r.name, "detail": r.detail} for r in blocking],
                        "hint": "these changes introduce a physics failure; adjust them or "
                                "inspect with check_profile_physics(changes)"}
            applied = await c.put_config(changes)
            if applied.get("errors"):
                return {"error": "invalid_keys", "errors": applied["errors"]}
            saved = await c.save_preset(type, name, allow_temperature_pairs=allow_temperature_pairs)
            out = {"preset": name, "applied": applied.get("applied", []), "saved": saved}
            if cross_layer:
                out["cross_layer_warnings"] = [
                    {"name": r.name, "detail": r.detail,
                     "hint": "judged against the currently selected print preset's speeds; this "
                             "filament preset was saved anyway. Pair it with a slower print preset, "
                             "or re-run check_profile_physics with the print preset you intend to use."}
                    for r in cross_layer]
            return out
    except ApiError as e:
        if (refusal := _save_refusal(e, type, edited=True)) is not None:
            return refusal
        return _m4a_err(e)


@mcp.tool()
async def rename_preset(
    type: Annotated[str, Field(description="Preset group: 'print', 'filament', or 'printer'.")],
    old_name: Annotated[str, Field(description="Current name of the user preset to rename, as shown by list_presets.")],
    new_name: Annotated[str, Field(description="New name for the preset. Should not collide with an existing preset of the same type.")],
) -> dict:
    """Rename one of the user's presets; the renamed preset becomes the selected one.

    It copies the preset to new_name, selects the copy and deletes the original. System presets
    are read-only.

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
    """Return the last slice's G-code as text; save_gcode writes it to a file and records the slice."""
    try:
        async with _client() as c:
            data = await c.get_gcode()
            return {"bytes": len(data), "gcode": data.decode("utf-8", errors="replace")}
    except Conflict:
        return await _not_sliced()
    except ApiError as e:
        return _m4a_err(e)


_DESCRIBE_CACHE: dict[str, dict] = {}   # single entry in practice: the current slice


def _gcode_cache_key(data: bytes) -> str:
    h = hashlib.sha1()
    h.update(data[:65536])
    h.update(data[-65536:])
    return f"{len(data)}:{h.hexdigest()}"


@mcp.tool()
async def describe_plate() -> dict:
    """Describe from the G-code how each sliced part stands, touches the bed, overhangs, is supported and where its seams are; needs a finished slice.

    Machine-readable facts per object, so you can answer orientation and placement questions
    instead of guessing from Euler angles or a picture: how the part stands (flat / tilted / on
    an edge or corner, from first-layer contact versus its widest layer), the first-layer
    footprint as islands, where overhang extrusions concentrate by 10 mm height band, where
    support stands and where its interface touches the part, and which side the outer-wall seams
    sit on (checked against seam_position). Each object gets a server-written summary sentence;
    relay it rather than recomputing. Read-only. Needs a valid slice; returns {"error":
    "not_sliced"} otherwise. Copies of one object are aggregated (Orca labels every copy 0);
    footprint islands still show per-copy contact. The parsed result is cached per slice (the
    G-code itself is still downloaded each call to detect a new slice). All bboxes are [x0, y0,
    x1, y1] in plate millimetres.

    `settings_check` says whether the G-code was sliced with the settings that are set now
    (layer height, walls, infill, temperatures, speeds, supports, bed type, print sequence, spiral
    vase and every setting with an unsaved edit); any `differ` entry names what the slice used
    instead. Tell the user before printing when it lists differences.
    """
    live = modified = None
    try:
        async with _client() as c:
            data = await c.get_gcode()
            try:
                objs = (await c.get_objects()).get("objects", [])
            except ApiError:
                objs = []
            try:  # for the settings check; optional, so any failure only leaves it out
                live = await c.get_config(None)
                modified = [k for keys in ((await c.get_status()).get("modified") or {}).values()
                            if isinstance(keys, list) for k in keys if isinstance(k, str)]
            except Exception:  # noqa: BLE001
                live = None
    except Conflict:
        return await _not_sliced()
    except ApiError as e:
        return _m4a_err(e)
    key = _gcode_cache_key(data)
    hit = _DESCRIBE_CACHE.get(key)
    if hit is not None:
        return _with_settings_check({**hit, "cached": True}, _SLICED_CONFIG.get(key), live, modified)
    text = data.decode("utf-8", errors="replace")
    try:
        t0 = time.perf_counter()
        parsed = await asyncio.to_thread(_plate.parse_gcode, text)
        out = _plate.describe(parsed, objs)
        out["parse_seconds"] = round(time.perf_counter() - t0, 2)
    except Exception as e:
        return {"error": "parse_failed", "detail": str(e)[:200]}
    _DESCRIBE_CACHE.clear()
    _DESCRIBE_CACHE[key] = out
    _SLICED_CONFIG.clear()
    _SLICED_CONFIG[key] = dict(parsed.config)
    return _with_settings_check({**out, "cached": False}, parsed.config, live, modified)


_SLICED_CONFIG: dict[str, dict] = {}  # the settings each cached slice's G-code says it was sliced with


def _with_settings_check(out: dict, sliced: dict | None, live: dict | None, modified) -> dict:
    """Verified slicing: compare what the G-code was sliced with against what is set now (verify_slice.py).
    Left out when either side is unavailable; checked again on every call, as the live settings move on."""
    if sliced and live:
        out["settings_check"] = _verify.check(sliced, live, modified or ())
    return out


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


def _gcode_base_dir() -> Path:
    """Where save_gcode writes the G-code file: PRINT_OUTCOMES_DIR if set (whitespace counts as unset),
    else ~/.orcaslicer-mcp (the folder `remember` already owns). The outcome store itself lives
    at outcomes.store_dir(), which defaults to ~/.orcaslicer-mcp/outcomes."""
    if os.environ.get("PRINT_OUTCOMES_DIR", "").strip():
        return _outcomes.store_dir()
    return Path.home() / ".orcaslicer-mcp"


@mcp.tool()
async def save_gcode(filename: str | None = None) -> dict:
    """Save the last slice's G-code to a file and record the slice so the print can be learned from later; hand the path to the printer tools.

    The record (model, a fingerprint of the objects' names and sizes, full settings snapshot,
    and OrcaSlicer's time and filament estimates) goes in the local outcome store under that
    filename. When that file is printed, list_print_history (or klipper-mcp's recorder) matches
    the finished job by filename, so the real result joins back to these settings. Returns the
    saved path; hand it to klipper-mcp's start_print. Default filename:
    <object>_<timestamp>.gcode. Never overwrites an existing file: a name collision gets a -2,
    -3, ... suffix. Writes into a gcode folder under PRINT_OUTCOMES_DIR if set, else under
    ~/.orcaslicer-mcp; the folder is created if missing. If the store write fails, the
    file is still saved and outcome_recorded is False.
    """
    try:
        async with _client() as c:
            data = await c.get_gcode()
            objs = (await c.get_objects()).get("objects") or []
            cfg = await c.get_config(None)
            stats: dict = {}
            printer_name = None
            try:
                stats = (await c.slice_status()).get("stats") or {}
            except ApiError:
                pass  # the estimate is nice to have; the file still saves without it
            app_version = None
            try:
                status = await c.get_status()
                printer_name = (status.get("presets") or {}).get("printer")
                app_version = status.get("app_version")
            except ApiError:
                pass
    except Conflict:
        return await _not_sliced()
    except ApiError as e:
        return _m4a_err(e)
    model_name = objs[0]["name"] if objs else "plate"
    fname = _safe_gcode_name(filename or f"{model_name}_{datetime.datetime.now():%Y%m%d-%H%M%S}")
    out_dir = _gcode_base_dir() / "gcode"
    out_dir.mkdir(parents=True, exist_ok=True)
    path, fname = _unique_gcode_path(out_dir, fname)
    try:
        path.write_bytes(data)
    except OSError as e:
        return {"error": "write_failed", "detail": str(e), "path": str(path)}
    row_id = None
    outcome_error = None
    try:
        snapshot = {**cfg, _forecast.VERSION_KEY: app_version} if app_version else cfg  # per-version forecasts
        row_id = _outcomes.record_slice(
            fname, model_name, _outcomes.geometry_hash_for(objs), snapshot,
            printer_id=_ptarget.printer_id(printer_name),
            est_time_s=stats.get("estimated_time_seconds"), est_filament_g=stats.get("filament_used_g"))
    except Exception as e:  # noqa: BLE001 - whatever goes wrong while recording, the saved file stays
        outcome_error = str(e) or type(e).__name__
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
    verdict_counts: dict[str, int] = {}
    for r in rows:
        v = r.get("human_verdict")
        if v:
            verdict_counts[v] = verdict_counts.get(v, 0) + 1
    tail = f" ({', '.join(f'{c} marked {v}' for v, c in sorted(verdict_counts.items()))})" if verdict_counts else ""
    head = f"{n} recent print{'s' if n != 1 else ''}" if subject == "recent" else f"{n} past print{'s' if n != 1 else ''} of {subject}"
    return f"{head}: {parts}{tail}"


@mcp.tool()
async def recall_prints(model_name: str | None = None, limit: int = 5) -> dict:
    """Recall how past prints of this model turned out and with which settings; call it before slicing.

    Matches the current plate by geometry (or by model_name if given / the slicer is offline),
    returning each past print's result (success/cancelled/error), your recorded verdict (e.g.
    'warped'), and the settings it was sliced with. Call this BEFORE slicing and tell the user
    anything relevant (a past warp, a failed layer height). Each row also carries failure_reason
    (the printer's own words, when known) and the slicer's estimates (est_time_s,
    est_filament_g). duration_s is Moonraker's whole job time, including heating up and any
    pauses, so beside the estimates it overstates an overrun; list_print_history compares the
    print time (without heating or pauses) with the estimate. filament_g comes from the G-code
    file's own estimate, because printers don't report grams. Call list_print_history first to
    pull in recent results. Read-only. Returns available=false and nothing else when no outcome
    store exists yet. If neither geometry nor name matches, it returns the most recent prints of
    ANY model with matched_by='recent'; never attribute those to the current model.
    """
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
    """Render a picture of the plate: the models before slicing (editor) or the toolpaths after it (preview).

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
        if await _plate_is_empty():
            return {"error": "no_slice_result", "detail": _EMPTY_PLATE,
                    "hint": "load a model, slice, then render view='preview'"}
        return {"error": "no_slice_result",
                "detail": "There is no finished slice to show yet.",
                "hint": "slice first (slice_and_wait), then render view='preview'"}
    except ApiError as e:
        return _m4_err(e, "plate_render (fork v2.3.2-mcp.4+)")



# --- Printer feedback (read-only) ---------------------------------------------
# The printer is found through OrcaSlicer's active printer profile (or ORCA_PRINTER_URL) and asked
# over HTTP GET only. Nothing here sends commands to a printer.


def _printer_api_key() -> str | None:
    return os.environ.get("ORCA_PRINTER_API_KEY", "").strip() or None


def _remembered_age(at) -> str | None:
    """How long ago a remembered time was, in words, or None when it is no usable time: not a number,
    NaN or Infinity (json.loads accepts both), an integer too big for a float, or a number no calendar
    can hold (all of which a hand-edited printer.json can contain). Asking the calendar to read the
    time is the test: it refuses each of those with OverflowError, OSError or ValueError."""
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    try:
        datetime.datetime.fromtimestamp(at, datetime.timezone.utc)
        age = time.time() - at
    except (OverflowError, OSError, ValueError):
        return None
    return f"{_duration_text(max(0.0, age))} ago"


def _remembered_note(target) -> str:
    age = _remembered_age(target.remembered_at) or "earlier"
    return f"OrcaSlicer isn't running or can't be reached, so this uses the printer remembered {age}."


def _remembered_lead(target) -> str:
    """Said first when the printer comes from memory: OrcaSlicer could not be read, so this may
    not be the printer selected there now (usability test 2026-11-09)."""
    name = f" ({target.profile})" if target.profile else ""
    return (f"The printer you last used{name}, not necessarily the one selected in OrcaSlicer now "
            "(OrcaSlicer can't be read): ")


async def _with_printer(fn, bambu=None) -> dict:
    """Find the printer (override > OrcaSlicer profile > remembered), open the protocol that
    answers, and run fn(target, client). Printer problems come back as {error, message, hint, ...};
    once the printer is known, every reply (error or not) says which one it is in `printer`.
    `bambu`, when given, answers instead for a Bambu Lab printer profile (which has no address
    to open) through OrcaSlicer's own connection; its None means it could not, and the usual
    unsupported_connection reply stands."""
    target = None
    try:
        target = await _ptarget.resolve_target(_client)
        target, client = await _ptarget.open_printer(target, _printer_api_key())
        async with client:
            out = await fn(target, client)
        if target.source == "remembered" and isinstance(out, dict):
            notes = out.get("notes")
            out["notes"] = (notes if isinstance(notes, list) else []) + [_remembered_note(target)]
            if isinstance(out.get("headline"), str):
                out["headline"] = _remembered_lead(target) + out["headline"]
        return out
    except PrinterError as e:
        if bambu is not None and e.details.get("host_type") == "bambu":
            out = await bambu()
            if out is not None:
                return out
        out = e.as_dict()
        if target is not None:
            out.setdefault("printer", target.public())
        return out


@mcp.tool()
async def get_printer_status() -> dict:
    """Read what the 3D printer is doing now (state, temperatures, job progress, problems); wait_for_printer waits for a point in a print.

    It reports state (idle, heating, printing, paused, finished, cancelled, error, shutdown,
    offline), nozzle and bed temperatures with their targets, the current job (file, progress,
    layer, time elapsed and left, and what the time left is based on), and any problems the
    printer reports, each in the printer's own words with a plain-English hint for common ones.
    Relay `headline` as-is. Never invent a cause the printer did not report.

    Finds the printer from OrcaSlicer's active printer profile (or ORCA_PRINTER_URL), so it needs no
    extra setup once the printer is connected in OrcaSlicer. Klipper (Moonraker) printers report the
    most; OctoPrint reports basic status. Read-only: it never sends commands to the printer."""
    return await _with_printer(lambda t, c: take_snapshot(t, c))


@mcp.tool()
async def wait_for_printer(
    until: Annotated[Literal["heated", "printing", "first_layer_done", "finished"], Field(description=(
        "What to wait for. 'heated': every heater with a target is within 3 °C of it. 'printing': "
        "extrusion has started. OctoPrint reports no extrusion, so there it means the job is "
        "running with the heaters at temperature. 'first_layer_done': the second layer has begun "
        "(Klipper only); without layer info from the slicer it is judged from the nozzle height, so "
        "it is approximate. 'finished': the job completed, was cancelled or errored."))],
    timeout_s: Annotated[int, Field(description=(
        "Seconds to wait before returning the latest status. Default 300, at most 1800. "
        "Call again to keep waiting."))] = _pwait.DEFAULT_TIMEOUT_S,
    ctx: Context | None = None,
) -> dict:
    """Wait until the printer is heated, printing, past the first layer or finished, checking every 5 seconds.

    Returns as soon as it happens (met: true), or at the timeout with the latest status (met:
    false). It also returns early, with stopped_early saying why, when the printer reports a
    fault, when the job ends before the point it waits for (a cancelled print, say), or after
    losing contact ("Lost contact with the printer"). A job that completes successfully while
    the wait is for 'printing' or 'first_layer_done' counts as met (a one-layer print passes
    both at once). A met result can carry stopped_early too, for example a job that finished
    with an error. Waiting for 'finished' when nothing is printing returns at once with the note
    'nothing is printing'. Read-only: it never sends commands.
    """
    async def report(done: float, total: float, message: str) -> None:
        if ctx is not None:
            await ctx.report_progress(done, total, message)

    return await _with_printer(lambda t, c: _pwait.run_wait(t, c, until, timeout_s, report))


@mcp.tool()
async def list_print_history(limit: int = 10) -> dict:
    """List how recent prints on the printer ended against the slicer's estimates (Klipper only); recall_prints looks up one model's past prints.

    For each finished job: result (success, cancelled or error), how long it took against the
    slicer's estimate, filament used, and for failed or stopped jobs the printer's own reason
    while its console still holds it. `summary` says how the slicer's time estimates compare
    with reality once there are a few finished prints. Also copies those jobs into the local
    outcome store (safe to repeat), so recall_prints can learn from them. Klipper printers only.
    `limit` is capped at 50.
    """
    return await _with_printer(
        lambda t, c: _phistory.print_history(t, c, limit, _ptarget.printer_id_for(t)))


async def _bambu_ams_check() -> dict | None:
    """check_printer_match on a Bambu Lab printer profile: the AMS match of the plate's filaments.
    None when the build cannot read the printer (then the usual 'not supported' reply stands)."""
    try:
        async with _client() as c:
            mapping = await c.get_slot_mapping()
            if not isinstance(mapping, dict) or mapping.get("kind") != "bambu":
                return None
            if not mapping.get("supported"):
                if mapping.get("reason") == "not_supported":
                    return None
                return {"supported": False, "kind": "bambu", "reason": mapping.get("reason"),
                        "message": mapping.get("message"),
                        "hint": "Select the printer in OrcaSlicer's Device tab and wait for it to connect."}
            return await _bambu_mapping(c, mapping)
    except (UiTimeout, NotReachable) as e:
        return {"supported": False, "kind": "bambu", "reason": "orcaslicer_not_answering", "message": _err(e).get("error")}
    except ApiError:
        return None


@mcp.tool()
async def check_printer_match() -> dict:
    """Check the active OrcaSlicer profile against the printer before printing: on a Bambu Lab printer, that each filament's slot preset matches what its AMS tray holds; on Klipper, the printer's own settings (nozzle, bed, speeds, temperatures). Warnings only.

    On a Bambu Lab printer (the OrcaSlicer MCP build with bambu_ams, the printer selected in the
    Device tab) it returns what get_filament_slot_mapping returns: per filament the tray
    OrcaSlicer's Send dialog will pre-fill and `match` (exact, soft, none, unknown), with
    `headline` and `warnings` to relay. A soft match names both filaments, say a Generic ASA
    slot with Polymaker LW-ASA loaded: same base material, the user's call.

    On Klipper it compares nozzle size, bed size and height, the highest speeds and
    accelerations, nozzle and bed temperatures, and firmware retraction against the printer's own
    settings. Each check comes back ok, warn or unknown with both values and why it matters;
    relay `headline` and the warnings. It never changes or blocks anything itself. When speeds or
    accelerations go past the printer's limits, `fix` holds the set_config changes that lower
    just those settings to the limits: offer them to the user and pass `fix` to set_config if
    they agree. Run it before printing with a new or edited profile, or when prints take longer
    than estimated. Needs OrcaSlicer running.
    """
    async def run(t, c):
        if t.kind != "klipper":
            return {"supported": False, "reason": "Only Klipper printers report their settings.",
                    "printer": t.public()}
        settings = ((await c.objects_query(["configfile=settings"])).get("configfile") or {}).get("settings") or {}
        try:
            async with _client() as fork:
                cfg = await fork.get_config(list(_pmatch.PROFILE_KEYS))
        except ApiError as e:
            raise PrinterError("orca_unreachable",
                               "check_printer_match couldn't read the active profile from OrcaSlicer.",
                               hint=_ptarget.orca_read_hint(
                                   e, "Start OrcaSlicer (MCP build) with the Remote API enabled."),
                               detail=str(e)) from e
        out = _pmatch.compare(cfg, settings)
        out["printer"] = t.public()
        return out

    return await _with_printer(run, bambu=_bambu_ams_check)


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
    "list_plates": ("List plates", True, False),
    "get_project": ("Get project file state", True, False),
    "list_recent_projects": ("List recent projects", True, False),
    "browse_projects": ("List project files in a folder", True, False),
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
    "describe_plate": ("Describe plate placement from G-code", True, False),
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
    "add_plate": ("Add plate", False, False),
    "select_plate": ("Select plate", False, False),
    "get_printer_slots": ("Get printer trays", True, False),
    "get_filament_slot_mapping": ("Get filament slot mapping", True, False),
    "set_filament_slot_mapping": ("Set filament slot mapping", False, False),
    "move_object_to_plate": ("Move object to plate", False, False),
    "forget_recent_project": ("Remove an entry from recent projects", False, False),
    "auto_orient": ("Auto-orient objects", False, False),
    "remember": ("Save a note", False, False),
    "load_model": ("Load a model file", False, False),
    "select_preset": ("Select preset", False, False),
    "get_filament_slots": ("Get filament slots", True, False),
    "set_filament_slot_preset": ("Set a slot's filament preset", False, False),
    "set_layer_height": ("Set layer height", False, False),
    "set_height_range": ("Set height-range config", False, False),
    "rename_preset": ("Rename preset", False, False),
    "save_preset": ("Save preset (overwrites stored settings)", False, True),
    "edit_preset": ("Edit preset (overwrites stored settings)", False, True),
    "delete_object": ("Delete object from plate", False, True),
    "delete_plate": ("Delete plate (its objects are parked off-plate)", False, True),
    "delete_preset": ("Delete preset", False, True),
    "open_project": ("Open project (replaces the open session)", False, True),
    "open_recent_project": ("Open a recent project (replaces the open session)", False, True),
    "new_project": ("New project (replaces the open session)", False, True),
    "delete_project_file": ("Delete a project file from disk", False, True),
    # overwrite=True replaces an existing file, so both can destroy data.
    "save_project": ("Save project (.3mf, can replace a file)", False, True),
    "move_project_file": ("Move or rename a project file (can replace a file)", False, True),
    "get_printer_status": ("Get printer status", True, False),
    "wait_for_printer": ("Wait for the printer", True, False),
    "list_print_history": ("List print history", False, False),
    "check_printer_match": ("Check the profile against the printer", True, False),
}


# Tools that reach outside OrcaSlicer (the printer on the network), and tools that are safe to repeat
# although they write local state.
_OPEN_WORLD_TOOLS = frozenset({"get_printer_status", "wait_for_printer", "list_print_history", "check_printer_match"})
_IDEMPOTENT_TOOLS = frozenset({"list_print_history"})


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
            idempotentHint=True if name in _IDEMPOTENT_TOOLS else None,
            openWorldHint=True if name in _OPEN_WORLD_TOOLS else None,
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
    for w in allow_override_warnings():
        print(w, file=sys.stderr)  # stdout carries the MCP protocol; stderr reaches the client log
    import anyio
    anyio.run(run_stdio, mcp._lowlevel_server)


if __name__ == "__main__":
    main()
