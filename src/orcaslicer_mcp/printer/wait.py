"""wait_for_printer: poll the printer until a condition holds, something goes wrong, or time
runs out. Read-only like the rest of this package."""
from __future__ import annotations
import asyncio
import time

from . import snapshot as _snapshot
from .errors import PrinterError
from .status import ACTIVE, DONE, NEAR_TARGET_C

UNTIL = ("heated", "printing", "first_layer_done", "finished")
POLL_S = 5.0
DEFAULT_TIMEOUT_S = 300
MAX_TIMEOUT_S = 1800


def heated(snap: dict) -> bool:
    heaters = [h for h in (snap.get("temps") or {}).values() if h and h.get("target")]
    return bool(heaters) and all(
        h.get("actual") is not None and abs(h["actual"] - h["target"]) <= NEAR_TARGET_C for h in heaters)


def printing(snap: dict, kind: str | None) -> bool:
    if snap.get("state") != "printing":
        return False
    job = snap.get("job") or {}
    if kind == "octoprint":
        return (job.get("progress_percent") or 0) > 0
    return (job.get("filament_used_mm") or 0) > 0


def first_layer_done(snap: dict) -> bool:
    if snap.get("state") not in ("printing", "paused"):
        return False
    job = snap.get("job") or {}
    layer = job.get("layer") or {}
    if layer.get("current") is not None and layer.get("total"):
        return layer["current"] >= 2
    flh = job.get("first_layer_height")
    z = (snap.get("klipper") or {}).get("z_mm")
    return flh is not None and z is not None and z > flh + 0.1


def finished(snap: dict, start_state: str | None, kind: str | None) -> bool:
    state = snap.get("state")
    if kind == "octoprint":
        # OctoPrint has no "complete" state of its own: only a job that was running when the call
        # began and no longer is has ended. A job already sitting at 100 % is not an event.
        return start_state in ACTIVE and state not in ACTIVE
    return state in DONE


def stop_reason(snap: dict) -> str | None:
    for p in snap.get("problems") or []:
        if p.get("severity") == "fatal":
            return p.get("message")
    if snap.get("state") in ("error", "shutdown"):
        return "the printer reports an error"
    return None


def condition_met(until: str, snap: dict, start_state: str | None, kind: str | None) -> bool:
    if until == "heated":
        return heated(snap)
    if until == "printing":
        return printing(snap, kind)
    if until == "first_layer_done":
        return first_layer_done(snap)
    return finished(snap, start_state, kind)


async def run_wait(target, client, until: str, timeout_s: int, report=None, *,
                   poll_s: float | None = None, sleep=asyncio.sleep, clock=time.monotonic) -> dict:
    if until not in UNTIL:
        raise ValueError(f"until must be one of {UNTIL}")
    if until == "first_layer_done" and target.kind == "octoprint":
        raise PrinterError("unsupported_for_connection",
                           "OctoPrint doesn't report layers, so first_layer_done only works with Klipper printers.",
                           hint="Wait for 'printing' instead.")
    poll_s = POLL_S if poll_s is None else poll_s
    timeout_s = max(1, min(int(timeout_s), MAX_TIMEOUT_S))
    start = clock()
    snap = await _snapshot.take_snapshot(target, client)
    start_state = snap.get("state")

    def result(met: bool, stopped_early: str | None = None, note: str | None = None) -> dict:
        out = {"met": met, "until": until, "waited_s": int(round(clock() - start)),
               "stopped_early": stopped_early, "status": snap}
        if note:
            out["note"] = note
        return out

    # Nothing to wait for unless a job is running now, or (Klipper only) one already ended.
    if until == "finished" and start_state not in ACTIVE and not (
            start_state in DONE and target.kind != "octoprint"):
        reason = stop_reason(snap)
        return result(False, stopped_early=reason, note=None if reason else "nothing is printing")
    while True:
        if condition_met(until, snap, start_state, target.kind):
            return result(True)
        reason = stop_reason(snap)
        if reason:
            return result(False, stopped_early=reason)
        elapsed = clock() - start
        if elapsed >= timeout_s:
            return result(False, note=f"timed out after {timeout_s} s; call again to keep waiting")
        if report is not None:
            try:
                await report(elapsed, timeout_s, snap.get("headline") or "")
            except Exception:
                pass  # a progress notification must never break the wait
        await sleep(min(poll_s, timeout_s - elapsed))
        snap = await _snapshot.take_snapshot(target, client)
