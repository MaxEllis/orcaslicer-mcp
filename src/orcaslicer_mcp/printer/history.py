"""Recent print jobs from Klipper's own history: how each one ended, why failed ones stopped (while
the console still holds it), how long they took against the slicer's estimate, and a copy written
into the outcome store so recall_prints learns from real results without any other software."""
from __future__ import annotations
import asyncio
import datetime
import sqlite3
import statistics

from .. import outcomes as _outcomes
from ..outcomes import _num
from .errors import PrinterError

FINISHED = frozenset({"completed", "cancelled", "error", "klippy_shutdown", "klippy_disconnect", "interrupted"})
CONSOLE_LINES = 1000          # Moonraker's default console store size
REASON_GRACE_S = 10.0         # a fault's console line can land just after the job's end time
MIN_JOBS_FOR_SUMMARY = 3


def failure_reason(job: dict, console: list[dict]) -> str | None:
    """The last console error ('!!' line) between the job's start and end (+ a grace period)."""
    if job.get("status") == "completed":
        return None
    start, end = _num(job.get("start_time")), _num(job.get("end_time"))
    if start is None or end is None:
        return None
    hits = []
    for line in console or []:
        msg, t = str(line.get("message") or ""), _num(line.get("time"))
        if msg.startswith("!!") and t is not None and start <= t <= end + REASON_GRACE_S:
            hits.append(msg[2:].strip())
    return hits[-1] if hits else None


def shape_job(job: dict, reason: str | None, row: dict | None) -> dict:
    meta = job.get("metadata") or {}
    row = row or {}
    est_time = _num(row.get("est_time_s")) if row.get("est_time_s") is not None else _num(meta.get("estimated_time"))
    est_fil = (_num(row.get("est_filament_g")) if row.get("est_filament_g") is not None
               else _num(meta.get("filament_weight_total")))
    source = None
    if est_time is not None or est_fil is not None:
        source = "slice" if row.get("sliced_at") is not None else "file"
    actual = _num(job.get("print_duration"))
    end = _num(job.get("end_time"))
    used = _num(job.get("filament_used"))
    result = _outcomes.result_for_status(job.get("status"))
    # Only a finished print says anything about the estimate: a cancelled 60 s run is not "98% faster".
    vs_pct = int(round((actual / est_time - 1) * 100)) if result == "success" and actual and est_time else None
    return {
        "file": job.get("filename"),
        "job_id": job.get("job_id"),
        "ended_at": datetime.datetime.fromtimestamp(end).astimezone().isoformat(timespec="minutes") if end else None,
        "result": result,
        "status": job.get("status"),
        "print_duration_s": None if actual is None else int(round(actual)),
        "filament_used_mm": None if used is None else int(round(used)),
        "failure_reason": row.get("failure_reason") or reason,  # the stored one, so the reply matches the row
        "estimate": {"time_s": None if est_time is None else int(round(est_time)), "filament_g": est_fil,
                     "source": source},
        "vs_estimate_pct": vs_pct,
        "slice": ({"model_name": row.get("model_name"), "settings": row.get("settings_summary")}
                  if row.get("sliced_at") is not None else None),
        "outcome_row_id": row.get("id"),
    }


def estimate_summary(jobs: list[dict]) -> str | None:
    ratios = []
    for j in jobs:
        est = (j.get("estimate") or {}).get("time_s")
        if j.get("result") == "success" and est and j.get("print_duration_s"):
            ratios.append(j["print_duration_s"] / est)
    if len(ratios) < MIN_JOBS_FOR_SUMMARY:
        return None
    n = len(ratios)
    pct = int(round((statistics.median(ratios) - 1) * 100))
    # The estimate can be OrcaSlicer's own (a slice saved here) or any slicer's file metadata, so the
    # sentence names no slicer.
    subject = f"Your last {n} successful prints with an estimate"
    if abs(pct) < 2:
        return f"{subject} matched the estimate (median)."
    return f"{subject} ran {abs(pct)}% {'longer' if pct > 0 else 'shorter'} than estimated (median)."


def _sync_to_store(jobs: list[dict], reasons: list[str | None], printer_id: str):
    """Write each job into the outcome store, oldest first, and read its row back. Runs on a worker
    thread (sqlite blocks). `jobs` is Moonraker's newest-first list: walking it from the end is the
    order the live recorder saw the jobs finish, so when a file was printed twice against one saved
    slice the earlier print claims it. Returns (rows aligned with `jobs`, rows synced, store error).
    The first store failure stops the walk; the jobs not reached keep no row."""
    rows: list[dict | None] = [None] * len(jobs)
    synced, store_error = 0, None
    for i in reversed(range(len(jobs))):
        try:
            rows[i] = _outcomes.get(_outcomes.record_outcome(jobs[i], printer_id, failure_reason=reasons[i]))
            synced += 1
        except (sqlite3.Error, OSError) as e:
            store_error = str(e)
            break
    return rows, synced, store_error


async def print_history(target, client, limit: int, printer_id: str) -> dict:
    if target.kind == "octoprint":
        return {"supported": False, "reason": "OctoPrint has no built-in print history.", "printer": target.public()}
    limit = max(1, min(int(limit), 50))
    jobs = await client.history_list(limit)
    try:
        console = await client.gcode_store(CONSOLE_LINES)
    except PrinterError:
        console = []
    finished = [j for j in jobs if j.get("status") in FINISHED]
    reasons = [failure_reason(j, console) for j in finished]
    rows, synced, store_error = await asyncio.to_thread(_sync_to_store, finished, reasons, printer_id)
    shaped = [shape_job(j, r, row) for j, r, row in zip(finished, reasons, rows)]
    out = {"printer": target.public(), "jobs": shaped, "summary": estimate_summary(shaped),
           "synced_to_store": synced, "store": str(_outcomes.db_path())}
    if store_error:
        out["store_error"] = store_error
    return out
