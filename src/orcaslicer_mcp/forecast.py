"""How long a slice will really take on this printer, from how past prints compared with their estimates.

Idea from the Print Memory plugin on OrcaCloud (plugin review 2026-11-11): the slicer's estimate times the error
observed on recent finished prints, judged per OrcaSlicer version when there are enough of them (a new version can
estimate differently). The store's job time is the printer's whole job, heat-up included, so the forecast is
"from start to finish", which is what the user waits for.

Reads the outcome store only; never writes. Kept out of outcomes.py, which klipper-mcp vendors.
"""
from __future__ import annotations

import json
import statistics

from . import outcomes

VERSION_KEY = "_orcaslicer_version"  # save_gcode adds it to the settings snapshot
MIN_PRINTS = 3
LAST = 20
# A paused or babysat job can take several times its estimate; such outliers say nothing about the estimator.
RATIO_RANGE = (0.5, 2.5)


def _dur(seconds: float) -> str:
    m = max(1, int(round(seconds / 60)))
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def _rows() -> list[tuple[float, str | None]]:
    """(job time / estimate, version) of recent successful prints, newest first."""
    conn = outcomes.connect()
    try:
        rows = conn.execute(
            "SELECT duration_s, est_time_s, settings_json FROM prints WHERE result = 'success' "
            "AND duration_s > 0 AND est_time_s > 0 ORDER BY COALESCE(printed_at, sliced_at) DESC LIMIT 200").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        ratio = r["duration_s"] / r["est_time_s"]
        if not RATIO_RANGE[0] <= ratio <= RATIO_RANGE[1]:
            continue
        version = None
        if r["settings_json"]:
            try:
                version = json.loads(r["settings_json"]).get(VERSION_KEY)
            except (ValueError, AttributeError):
                version = None
        out.append((ratio, version))
    return out


def time_forecast(estimate_s, version: str | None = None) -> dict | None:
    """{expected_s, ratio, based_on, same_version, summary}, or None without enough history (or no store)."""
    try:
        est = float(estimate_s)
    except (TypeError, ValueError):
        return None
    if est <= 0 or not outcomes.is_available():
        return None
    try:
        rows = _rows()
    except Exception:  # noqa: BLE001 - a locked or odd store only means no forecast
        return None
    same = [r for r, v in rows if version and v == version][:LAST]
    pool, same_version = (same, True) if len(same) >= MIN_PRINTS else ([r for r, _ in rows][:LAST], False)
    if len(pool) < MIN_PRINTS:
        return None
    ratio = statistics.median(pool)
    expected = est * ratio
    pct = int(round((ratio - 1) * 100))
    which = f"your last {len(pool)} prints" + (f" sliced with OrcaSlicer {version}" if same_version else "")
    if abs(pct) < 2:
        how = f"{which} took about as long as estimated"
    else:
        how = f"{which} took {abs(pct)}% {'longer' if pct > 0 else 'less'} than estimated"
    return {"expected_s": int(round(expected)), "ratio": round(ratio, 3), "based_on": len(pool),
            "same_version": same_version,
            "summary": f"Expect about {_dur(expected)} from start to finish, heat-up included: {how} (median)."}
