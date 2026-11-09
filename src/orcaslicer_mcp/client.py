from __future__ import annotations
import asyncio
import contextvars
import json
import math
import httpx
import websockets
from .config import Config
from .errors import error_from_status, NotReachable, ApiError, UiTimeout, OutOfRange
from .ranges import out_of_range
from .guard import (BlockedKey, sensitive_keys, changed_keys, check_placeholders,
                    redact_secrets)


# Per tool call: the server sets a one-item list here before a tool runs, and every request
# the call sends to OrcaSlicer flips it, so the server can tell which calls reached it.
REACHED: contextvars.ContextVar = contextvars.ContextVar("orca_reached", default=None)


# Set while compare_slices (or compare_settings) tries variants: every request then carries
# X-Orca-Mcp-Trial: 1, so OrcaSlicer's MCP panel records none of the trial writes and slices.
TRIAL: contextvars.ContextVar = contextvars.ContextVar("orca_trial", default=False)
TRIAL_HEADER = "X-Orca-Mcp-Trial"

# Which OrcaSlicer answered at each address, once /status has been read: "build" (the
# OrcaSlicer MCP build) or "plugin" (the OrcaSlicer MCP plugin on stock OrcaSlicer).
_BACKENDS: dict[str, str] = {}


def known_backend(base_url: str) -> str | None:
    return _BACKENDS.get(base_url)


def unreachable_message(base_url: str, e: Exception | None = None) -> str:
    why = (str(e) if e is not None else "") or (type(e).__name__ if e is not None else "")
    backend = _BACKENDS.get(base_url)
    if backend == "build":
        ask = "Is OrcaSlicer open, with Preferences > Remote API turned on?"
    elif backend == "plugin":
        ask = "Is OrcaSlicer open, with the OrcaSlicer MCP plugin turned on?"
    else:
        ask = ("Is OrcaSlicer open, with Preferences > Remote API turned on? With the OrcaSlicer "
               "MCP plugin on stock OrcaSlicer instead: is the plugin turned on?")
    return (f"not_reachable: OrcaSlicer isn't answering at {base_url}"
            + (f" ({why})" if why else "") + f". {ask} Ask the user to check.")


def timeout_message(seconds: float | None = None) -> str:
    within = f" within {seconds:g} s" if seconds else " in time"
    return (f"ui_timeout: OrcaSlicer didn't answer{within}; a dialog may be open in its window, "
            "ask the user to look.")


def mark_reached() -> None:
    flag = REACHED.get()
    if flag is not None:
        flag[0] = True


def check_ranges(changes: dict) -> None:
    """Refuse values outside their setting's min/max before anything is sent (ranges.py)."""
    found = out_of_range(changes or {})
    if found:
        raise OutOfRange(found)


class OrcaClient:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._http = httpx.AsyncClient(
            base_url=cfg.base_url,
            headers={"X-Api-Token": cfg.token},
            timeout=cfg.timeout,
        )

    async def __aenter__(self) -> "OrcaClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()

    def _effective_timeout(self, timeout: float | None) -> float:
        return float(timeout) if timeout is not None else float(self._cfg.timeout)

    async def _send(self, method: str, path: str, *, json=None, params=None,
                    timeout: float | None = None, headers: dict | None = None) -> httpx.Response:
        """One request with the transport failures turned into errors an assistant can act on.
        A trial in progress (TRIAL, compare_slices) marks every request it sends."""
        hdrs = dict(headers or {})
        if TRIAL.get():
            hdrs[TRIAL_HEADER] = "1"
        try:
            resp = await self._http.request(
                method, path, json=json, params=params, headers=hdrs or None,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT)
        except httpx.TimeoutException as e:
            raise UiTimeout(timeout_message(self._effective_timeout(timeout))) from e
        except httpx.TransportError as e:
            raise NotReachable(unreachable_message(self._cfg.base_url, e)) from e
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = {}
            raise error_from_status(resp.status_code, body)
        return resp

    async def _request(self, method: str, path: str, *, json=None, params=None,
                       timeout: float | None = None, _count: bool = True,
                       headers: dict | None = None) -> dict:
        if _count:
            mark_reached()
        resp = await self._send(method, path, json=json, params=params, timeout=timeout,
                                headers=headers)
        data = resp.json()
        if path == "/api/v1/status" and isinstance(data, dict):
            _BACKENDS[self._cfg.base_url] = "plugin" if data.get("backend") == "plugin" else "build"
        return data

    async def get_status(self) -> dict:
        data = await self._request("GET", "/api/v1/status")
        # F4: the fork emits presets.filaments (plural), but the rest of the API
        # surface is singular (type=filament, /presets keys, modified.filament).
        # Normalize here so callers see one convention (it holds a list either way).
        presets = data.get("presets")
        if isinstance(presets, dict) and "filaments" in presets:
            presets["filament"] = presets.pop("filaments")
        # Fork v2.4.2-mcp.15 adds project_name and project_dirty beside the path in
        # `project`. Fold the three into one object so a caller sees name, path and
        # dirty together; an older build keeps `project` as the bare path string.
        if "project_dirty" in data:
            data["project"] = {"name": data.pop("project_name", ""),
                               "path": data.get("project") or "",
                               "dirty": bool(data.pop("project_dirty"))}
        return data

    async def get_objects(self) -> dict:
        data = await self._request("GET", "/api/v1/objects")
        # F5: transform_object takes rotate in DEGREES, but the fork reports
        # transform.rotation in RADIANS. Convert the readback to degrees so a
        # set-then-read round-trip is consistent. (placement uses size_mm, not
        # rotation, so this is safe for internal consumers.)
        for obj in data.get("objects") or []:
            rot = obj.get("transform", {}).get("rotation")
            if isinstance(rot, list):
                obj["transform"]["rotation"] = [math.degrees(v) for v in rot]
        return data

    async def delete_object(self, obj_id: int) -> dict:
        return await self._request("DELETE", f"/api/v1/objects/{obj_id}")

    async def duplicate_object(self, obj_id: int) -> dict:
        return await self._request("POST", f"/api/v1/objects/{obj_id}/duplicate")

    async def _guard_config(self, changes: dict) -> None:
        """Refuse writes that would change a sensitive key (see guard.py). Writing a key
        back to its current value is allowed."""
        check_placeholders(changes)
        check_ranges(changes)
        hits = sensitive_keys(changes or {})
        if not hits:
            return
        current = await self.get_config(hits)
        changed = changed_keys(changes, hits, current)
        if changed:
            raise BlockedKey(changed)

    async def guard_preset_edit(self, ptype: str, name: str, changes: dict) -> None:
        """The same refusal for edit_preset, judged against the named preset's stored values
        and BEFORE anything is selected: selecting a preset discards the user's unsaved
        overrides, so a refusal must not come after it."""
        check_placeholders(changes)
        check_ranges(changes)
        hits = sensitive_keys(changes or {})
        if not hits:
            return
        stored = (await self._request("POST", "/api/v1/preset/config",
                                      json={"type": ptype, "name": name})).get("config", {})
        changed = changed_keys(changes, hits, stored)
        if changed:
            raise BlockedKey(changed)

    async def set_object_config(self, obj_id: int, changes: dict) -> dict:
        check_placeholders(changes)
        check_ranges(changes)
        hits = sensitive_keys(changes or {})
        if hits:
            raise BlockedKey(hits)
        return await self._request("PUT", f"/api/v1/objects/{obj_id}/config", json=changes)

    async def transform_object(self, obj_id: int, translate=None, rotate=None, scale=None) -> dict:
        body = {}
        if translate is not None:
            body["translate"] = translate
        if rotate is not None:
            body["rotate"] = rotate
        if scale is not None:
            body["scale"] = scale
        return await self._request("POST", f"/api/v1/objects/{obj_id}/transform", json=body)

    async def arrange(self) -> dict:
        return await self._request("POST", "/api/v1/arrange")

    async def orient(self) -> dict:
        return await self._request("POST", "/api/v1/orient")

    async def job_status(self) -> dict:
        return await self._request("GET", "/api/v1/jobs/status")

    # Plates (fork v2.4.2-mcp.13 and later).
    async def get_plates(self) -> dict:
        return await self._request("GET", "/api/v1/plates")

    async def create_plate(self, select: bool = True) -> dict:
        return await self._request("POST", "/api/v1/plates", json={"select": select})

    async def select_plate(self, index: int) -> dict:
        return await self._request("POST", f"/api/v1/plates/{index}/select")

    async def delete_plate(self, index: int) -> dict:
        return await self._request("DELETE", f"/api/v1/plates/{index}")

    async def set_object_plate(self, obj_id: int, plate: int, instance: int = 0) -> dict:
        return await self._request("POST", f"/api/v1/objects/{obj_id}/plate",
                                   json={"plate": plate, "instance": instance})

    # Printer slots (CANVAS), fork v2.4.2-mcp.14 and later.
    def _slot_timeout(self) -> float:
        # A slot read holds an MQTT exchange of up to ~18 s; never undercut the user's setting.
        return max(self._cfg.timeout, 25.0)

    async def get_printer_slots(self) -> dict:
        return await self._request("GET", "/api/v1/printer/slots", timeout=self._slot_timeout())

    async def get_slot_mapping(self) -> dict:
        return await self._request("GET", "/api/v1/printer/slot_mapping", timeout=self._slot_timeout())

    # Project files (fork v2.4.2-mcp.15 and later). Paths are on the OrcaSlicer host.
    async def get_project(self) -> dict:
        return await self._request("GET", "/api/v1/project")

    async def save_project(self, path: str | None = None, overwrite: bool = False) -> dict:
        # export_3mf renders a thumbnail per plate; the fork holds the request up to 120 s.
        return await self._request("POST", "/api/v1/project/save",
                                   json={"path": path, "overwrite": overwrite}, timeout=130.0)

    async def open_project(self, path: str | None = None, if_unsaved: str = "fail",
                           presets: str = "project", recent: int | None = None) -> dict:
        body: dict = {"if_unsaved": if_unsaved, "presets": presets}
        if recent is not None:
            body["recent"] = recent
        else:
            body["path"] = path
        return await self._request("POST", "/api/v1/project/open", json=body, timeout=130.0)

    async def new_project(self, if_unsaved: str = "fail", name: str | None = None) -> dict:
        body: dict = {"if_unsaved": if_unsaved}
        if name:
            body["name"] = name
        return await self._request("POST", "/api/v1/project/new", json=body, timeout=70.0)

    async def get_recent_projects(self) -> dict:
        return await self._request("GET", "/api/v1/project/recent")

    async def forget_recent_project(self, path: str | None = None, index: int | None = None,
                                    all: bool = False) -> dict:
        body: dict = {}
        if all:
            body["all"] = True
        elif path is not None:
            body["path"] = path
        elif index is not None:
            body["index"] = index
        return await self._request("DELETE", "/api/v1/project/recent", json=body)

    async def browse_projects(self, dir: str | None = None) -> dict:
        return await self._request("GET", "/api/v1/project/browse",
                                   params={"dir": dir} if dir else None, timeout=40.0)

    async def move_project_file(self, src: str, dst: str, overwrite: bool = False) -> dict:
        return await self._request("POST", "/api/v1/project/move",
                                   json={"from": src, "to": dst, "overwrite": overwrite}, timeout=70.0)

    async def delete_project_file(self, path: str) -> dict:
        return await self._request("DELETE", "/api/v1/project/file", json={"path": path}, timeout=40.0)

    async def put_slot_mapping(self, mapping: list[dict]) -> dict:
        return await self._request("PUT", "/api/v1/printer/slot_mapping", json={"mapping": mapping},
                                   timeout=self._slot_timeout())

    async def get_config(self, keys: list[str] | None) -> dict:
        # The fork's /config `keys` filter splits the RAW query string on ',' without
        # URL-decoding first, so httpx's percent-encoded comma (%2C) matches nothing and
        # returns {}. Rather than depend on that fragile path, fetch the full config
        # (~20KB on LAN) and filter locally — parser-independent. See test_client_config.
        data = await self._request("GET", "/api/v1/config")
        cfg = data.get("config", {})
        if keys:
            return {k: cfg[k] for k in keys if k in cfg}
        return cfg

    async def put_config(self, changes: dict) -> dict:
        await self._guard_config(changes)
        return await self._request("PUT", "/api/v1/config", json=changes)

    async def restore_config(self, snapshot: dict) -> dict:
        """Write back values read with get_config earlier in the same call (compare_*
        resets and restores). They are the slicer's own current values, so the guard has
        nothing to judge, and skipping it keeps a restore from depending on one more GET."""
        return await self._request("PUT", "/api/v1/config", json=snapshot)

    async def slice(self) -> dict:
        return await self._request("POST", "/api/v1/slice")

    async def slice_status(self) -> dict:
        return await self._request("GET", "/api/v1/slice/status")

    async def cancel_slice(self) -> dict:
        return await self._request("POST", "/api/v1/slice/cancel")

    async def load_model(self, path: str) -> dict:
        # STEP tessellation can far exceed the default timeout (the fork holds
        # the request up to 120 s); give the read a matching allowance.
        return await self._request("POST", "/api/v1/model", json={"path": path},
                                   timeout=130.0)

    async def select_preset(self, ptype: str, name: str, slot: int | None = None) -> dict:
        body = {"type": ptype, "name": name}
        if slot is not None:
            body["slot"] = slot  # 0-based filament slot (fork build with per-slot select)
        return await self._request("PUT", "/api/v1/preset", json=body)

    async def save_preset(self, ptype: str, name: str, detach: bool = False) -> dict:
        return await self._request("POST", "/api/v1/preset/save",
                                   json={"type": ptype, "name": name, "detach": detach})

    async def get_presets(self) -> dict:
        return await self._request("GET", "/api/v1/presets")

    async def get_preset_config(self, ptype: str, name: str) -> dict:
        # Fork builds before v2.4.2-mcp.10 return this unfiltered (printhost_apikey/user/password
        # included); later ones redact it too. This pass covers both.
        return redact_secrets(await self._request(
            "POST", "/api/v1/preset/config", json={"type": ptype, "name": name}))

    async def delete_preset(self, ptype: str, name: str) -> dict:
        return await self._request("DELETE", "/api/v1/preset", json={"type": ptype, "name": name})

    async def set_layer_height(self, obj_id: int, mode: str, quality: float = 0.5) -> dict:
        body = {"mode": mode}
        if mode == "adaptive":
            body["quality"] = quality
        return await self._request("PUT", f"/api/v1/objects/{obj_id}/layer_height", json=body)

    async def set_height_range(self, obj_id: int, min_z: float | None = None,
                               max_z: float | None = None, layer_height: float | None = None,
                               clear: bool = False) -> dict:
        body = {"clear": True} if clear else {"min_z": min_z, "max_z": max_z, "layer_height": layer_height}
        return await self._request("PUT", f"/api/v1/objects/{obj_id}/height_range", json=body)

    async def get_gcode(self) -> bytes:
        mark_reached()
        return (await self._send("GET", "/api/v1/gcode")).content

    async def get_plate_render(self, view: str = "editor", angle: str = "iso",
                               width: int = 800, height: int = 600,
                               frame: str | None = None) -> bytes:
        params: dict[str, str | int] = {
            "view": view, "angle": angle, "width": width, "height": height}
        if frame is not None:
            params["frame"] = frame
        mark_reached()
        return (await self._send("GET", "/api/v1/plate/render", params=params)).content

    def _ws_url(self) -> str:
        base = self._cfg.base_url
        ws = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return f"{ws}/api/v1/events?token={self._cfg.token}"

    async def collect_events(self, seconds: float, stop_on: set[str] | None = None) -> list[dict]:
        events: list[dict] = []
        deadline = asyncio.get_event_loop().time() + seconds
        try:
            mark_reached()
            async with websockets.connect(self._ws_url()) as ws:
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    try:
                        evt = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(evt, dict):
                        continue
                    events.append(evt)
                    if stop_on and evt.get("event") in stop_on:
                        break
        except Exception:
            # A closed/failed WS (e.g. bad token) just yields what we have.
            pass
        return events
