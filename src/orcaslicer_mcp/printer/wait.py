"""wait_for_printer: poll the printer until a condition holds, something goes wrong, or time
runs out. Read-only like the rest of this package."""
from __future__ import annotations
import asyncio
import time

from . import snapshot as _snapshot
from .errors import PrinterError
from .status import ACTIVE, NEAR_TARGET_C

UNTIL = ("heated", "printing", "first_layer_done", "finished")
POLL_S = 5.0
DEFAULT_TIMEOUT_S = 300
MAX_TIMEOUT_S = 1800
# A poll that fails with one of these codes is a network blip: keep waiting, up to this many in a row.
TRANSIENT_CODES = frozenset({"not_reachable", "protocol_error"})
MAX_POLL_FAILURES = 3
# Klipper's own words for a job that has ended (print_stats.state, kept in snap["job"]["state"]).
JOB_DONE = frozenset({"complete", "cancelled", "error"})
# What to say when the job ends before the point being waited for and the printer gave no reason.
ENDED_BEFORE = {
    "heated": "The job ended before the heaters reached their targets.",
    "printing": "The job ended before extrusion started.",
    "first_layer_done": "The job ended before the first layer was done.",
}


def heated(snap: dict) -> bool:
    heaters = [h for h in (snap.get("temps") or {}).values() if h and h.get("target")]
    return bool(heaters) and all(
        h.get("actual") is not None and abs(h["actual"] - h["target"]) <= NEAR_TARGET_C for h in heaters)


def printing(snap: dict, kind: str | None) -> bool:
    """Klipper: extrusion has started (it says "printing" before that, so filament used decides).
    OctoPrint reports no extrusion, and its progress is a position in the file that moves while the
    heaters are still warming, so it counts only as "printing" once its status says so, which the
    status layer reserves for a running job whose heaters are at temperature."""
    if snap.get("state") != "printing":
        return False
    if kind == "octoprint":
        return True
    job = snap.get("job") or {}
    return (job.get("filament_used_mm") or 0) > 0


def _above_first_layer(snap: dict) -> bool:
    """Extruding, with the nozzle clearly above the first layer's height. Klipper says "printing"
    before any extrusion, and the toolhead Z includes z-hop, so one reading of Z alone proves nothing."""
    if snap.get("state") not in ("printing", "paused"):
        return False
    job = snap.get("job") or {}
    flh = job.get("first_layer_height")
    z = (snap.get("klipper") or {}).get("z_mm")
    return ((job.get("filament_used_mm") or 0) > 0
            and flh is not None and z is not None and z > flh + 0.1)


def first_layer_done(snap: dict, prev: dict | None = None) -> bool:
    if snap.get("state") not in ("printing", "paused"):
        return False
    job = snap.get("job") or {}
    layer = job.get("layer") or {}
    if layer.get("current") is not None and layer.get("total"):
        return layer["current"] >= 2
    # No layer info from the slicer: judge from the nozzle height, on two polls in a row.
    return prev is not None and _above_first_layer(snap) and _above_first_layer(prev)


def _job_done(snap: dict) -> bool:
    job = snap.get("job")
    return bool(job) and job.get("state") in JOB_DONE


def finished(snap: dict, start_state: str | None, kind: str | None) -> bool:
    if kind == "octoprint":
        # OctoPrint has no "complete" state of its own: only a job that was running when the call
        # began and no longer is has ended. A job already sitting at 100 % is not an event.
        return start_state in ACTIVE and snap.get("state") not in ACTIVE
    # Klipper: the top-level state "error" also means Klipper itself is in an error state, so
    # "the job ended" is read from the job's own state.
    return _job_done(snap)


def stop_reason(snap: dict) -> str | None:
    problems = snap.get("problems") or []
    for p in problems:
        if p.get("severity") == "fatal":
            return p.get("message")
    if snap.get("state") in ("error", "shutdown"):
        # No fatal problem, but the job errored: say it in the job's own words when it gave any.
        for p in problems:
            if p.get("source") == "job" and p.get("message"):
                return p["message"]
        return "the printer reports an error"
    return None


def ended_early(until: str, snap: dict, kind: str | None) -> str | None:
    """Why a wait that watched the job run should stop now that it has left the active states, or
    None when the wait should carry on. The caller has already checked that the condition is not met."""
    if until != "finished":
        return ENDED_BEFORE[until]
    if kind == "octoprint":
        return None  # OctoPrint's "finished" is exactly "was active, is not any more": already met
    return f"The job stopped without finishing (the printer is now {snap.get('state')})."


def condition_met(until: str, snap: dict, start_state: str | None, kind: str | None,
                  prev: dict | None = None) -> bool:
    if until == "heated":
        return heated(snap)
    if until == "printing":
        return printing(snap, kind)
    if until == "first_layer_done":
        return first_layer_done(snap, prev)
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
    prev: dict | None = None  # the poll before this one, for conditions judged on two polls in a row
    failures = 0              # consecutive failed polls
    was_running = start_state in ACTIVE  # a job has been seen running: if it stops, the wait is over

    def result(met: bool, stopped_early: str | None = None, note: str | None = None) -> dict:
        out = {"met": met, "until": until, "waited_s": int(round(clock() - start)),
               "stopped_early": stopped_early, "status": snap}
        if note:
            out["note"] = note
        return out

    # Nothing to wait for unless a job is running now, or (Klipper only) one already ended.
    if until == "finished" and start_state not in ACTIVE and not (
            target.kind != "octoprint" and _job_done(snap)):
        reason = stop_reason(snap)
        return result(False, stopped_early=reason, note=None if reason else "nothing is printing")
    while True:
        if condition_met(until, snap, start_state, target.kind, prev):
            return result(True, stopped_early=stop_reason(snap))  # met, but a fault is still worth saying
        reason = stop_reason(snap)
        if reason:
            return result(False, stopped_early=reason)
        if was_running and snap.get("state") not in ACTIVE:
            # The job ended (cancelled, finished or reset by a firmware restart) before the point
            # being waited for, and nothing says why: waiting out the timeout could only report this.
            stopped = ended_early(until, snap, target.kind)
            if stopped:
                return result(False, stopped_early=stopped)
        elapsed = clock() - start
        if elapsed >= timeout_s:
            return result(False, note=f"timed out after {timeout_s} s; call again to keep waiting")
        if report is not None:
            try:
                await report(elapsed, timeout_s, snap.get("headline") or "")
            except Exception:
                pass  # a progress notification must never break the wait
        await sleep(min(poll_s, timeout_s - elapsed))
        try:
            fresh = await _snapshot.take_snapshot(target, client)
        except PrinterError as e:
            # Keep the last good snapshot: the answer so far is not lost to one bad poll.
            failures += 1
            if e.code in TRANSIENT_CODES and failures < MAX_POLL_FAILURES:
                continue
            return result(False, stopped_early=f"Lost contact with the printer: {e.message}")
        failures = 0
        prev, snap = snap, fresh
        was_running = was_running or snap.get("state") in ACTIVE
