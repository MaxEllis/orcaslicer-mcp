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


async def take_snapshot(target, client, *, now: float | None = None) -> dict:
    if target.kind == "octoprint":
        return octoprint_snapshot(await client.printer(), await client.job(), target_public=target.public())
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
    if now is None:
        # Moonraker stamps console lines with the printer's own clock, so staleness is judged on that
        # clock (from the replies' Date header), not this computer's. Read after the last request.
        now = client.server_time if client.server_time is not None else time.time()
    ps = status.get("print_stats") or {}
    active = ps.get("state") in ("printing", "paused")
    since = now - IDLE_CONSOLE_WINDOW_S
    if active:
        try:
            since = now - float(ps.get("total_duration") or 0) - 5.0
        except (TypeError, ValueError):
            pass
    meta = None
    if active and ps.get("filename"):
        # Read on every snapshot: a file re-sliced and re-uploaded under the same name has a new estimate.
        # A file the printer has no record of (None) is simply not used.
        try:
            meta = await client.file_metadata(ps["filename"])
        except PrinterError as e:
            notes.append(f"The slicer's time estimate for this file couldn't be read: {e.message}")
    snap = klipper_snapshot(status, info, console, since=since, file_meta=meta, target_public=target.public())
    if notes:
        snap["notes"] = notes
    return snap
