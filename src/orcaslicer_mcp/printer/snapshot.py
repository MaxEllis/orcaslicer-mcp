"""Ask the printer what it is doing, in as few requests as possible, and shape the answer.
A failed secondary request (warnings, console, file metadata) becomes a note, not a failure."""
from __future__ import annotations
import time

from .errors import PrinterError
from .status import klipper_snapshot, octoprint_snapshot

STATUS_OBJECTS = ["webhooks", "print_stats", "display_status", "virtual_sdcard", "extruder", "heater_bed",
                  "toolhead", "gcode_move", "fan", "configfile=warnings"]
CONSOLE_LINES = 100
IDLE_CONSOLE_WINDOW_S = 600.0

# File metadata (slicer time estimate, first-layer height) per G-code file, process-wide.
_META_CACHE: dict[str, dict | None] = {}


async def _file_meta(client, filename: str) -> dict | None:
    if filename not in _META_CACHE:
        try:
            _META_CACHE[filename] = await client.file_metadata(filename)
        except PrinterError:
            return None  # a transient failure is not cached
    return _META_CACHE[filename]


async def take_snapshot(target, client, *, now: float | None = None) -> dict:
    if target.kind == "octoprint":
        return octoprint_snapshot(await client.printer(), await client.job(), target_public=target.public())
    now = time.time() if now is None else now
    notes: list[str] = []
    info: dict = {}
    try:
        info = await client.server_info()
    except PrinterError as e:
        notes.append(f"Moonraker's own warnings couldn't be read: {e.message}")
    status: dict = {}
    if info.get("klippy_connected") is not False:  # Moonraker refuses object queries while Klipper is down
        status = await client.objects_query(STATUS_OBJECTS)
    console: list[dict] = []
    try:
        console = await client.gcode_store(CONSOLE_LINES)
    except PrinterError as e:
        notes.append(f"The console log couldn't be read: {e.message}")
    ps = status.get("print_stats") or {}
    active = ps.get("state") in ("printing", "paused")
    since = now - IDLE_CONSOLE_WINDOW_S
    if active:
        try:
            since = now - float(ps.get("total_duration") or 0) - 5.0
        except (TypeError, ValueError):
            pass
    meta = await _file_meta(client, ps["filename"]) if active and ps.get("filename") else None
    snap = klipper_snapshot(status, info, console, since=since, file_meta=meta, target_public=target.public())
    if notes:
        snap["notes"] = notes
    return snap
