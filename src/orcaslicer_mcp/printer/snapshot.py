"""Ask the printer what it is doing, in as few requests as possible, and shape the answer.
A failed secondary request (warnings, console, file metadata) becomes a note, not a failure."""
from __future__ import annotations
import time

from .errors import PrinterError
from .status import klipper_snapshot, octoprint_snapshot

STATUS_OBJECTS = ["webhooks", "print_stats", "display_status", "virtual_sdcard", "extruder", "heater_bed",
                  "toolhead", "gcode_move", "fan", "configfile=warnings"]
AUTH_CODES = ("auth_required", "auth_rejected")
CONSOLE_LINES = 100
IDLE_CONSOLE_WINDOW_S = 600.0


async def take_snapshot(target, client, *, now: float | None = None) -> dict:
    notes: list[str] = []
    if target.kind == "octoprint":
        printer = await client.printer()  # the state itself: without it there is no snapshot
        job: dict = {}
        try:
            job = await client.job()
        except PrinterError as e:
            if e.code in AUTH_CODES:
                raise  # a refused key is not a detail that can be skipped: the fix is the key
            notes.append(f"The current job couldn't be read: {e.message}")
        snap = octoprint_snapshot(printer, job, target_public=target.public())
        if notes:
            snap["notes"] = notes
        return snap
    # The probe that found this printer already asked /server/info: use that reply once, ask again after.
    info: dict | None = client.take_probe_info()
    if info is None:
        info = {}
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
