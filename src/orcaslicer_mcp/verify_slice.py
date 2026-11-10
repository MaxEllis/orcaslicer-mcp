"""Was the G-code sliced with the settings that are set now?

OrcaSlicer writes every setting it sliced with into the end of the G-code (CONFIG_BLOCK). Comparing those with
the live settings catches what a slice silently used instead: a plate's own bed type, print sequence or spiral
vase (plate settings the plugin cannot see), or a change that did not reach the slice. Idea from PrusaSlicer-MCP
("verified slicing", plugin review 2026-11-11).

Only settings that change the print are compared (a curated list plus every setting with an unsaved edit), and a
value counts as the same when it differs only in format ("0.2" and "0.20", "15%" and "15", "true" and "1").
"""
from __future__ import annotations

import re

from .guard import is_sensitive

# Settings whose silent difference changes the print. Plate-level ones first: OrcaSlicer 2.5 lets a plate override
# them, and the plugin cannot read plate settings.
KEYS = ("curr_bed_type", "print_sequence", "spiral_mode",
        "layer_height", "initial_layer_print_height", "wall_loops", "top_shell_layers", "bottom_shell_layers",
        "sparse_infill_density", "sparse_infill_pattern", "enable_support", "support_type", "brim_type",
        "brim_width", "nozzle_diameter", "nozzle_temperature", "nozzle_temperature_initial_layer",
        "outer_wall_speed", "inner_wall_speed", "sparse_infill_speed", "travel_speed", "filament_type",
        "retraction_length", "fan_max_speed", "fan_min_speed", "line_width", "seam_position", "ironing_type",
        "fuzzy_skin", "elefant_foot_compensation", "xy_contour_compensation", "xy_hole_compensation")
_BOOL = {"true": "1", "false": "0"}


def _parts(value) -> list[str]:
    return [p.strip() for p in re.split(r"[,;]", str(value).strip())]


def _num(text: str) -> float | None:
    try:
        return float(text.rstrip("%"))
    except ValueError:
        return None


def same(a, b) -> bool:
    pa, pb = _parts(a), _parts(b)
    if len(pa) != len(pb):
        return str(a).strip().lower() == str(b).strip().lower()
    for x, y in zip(pa, pb):
        x, y = _BOOL.get(x.lower(), x), _BOOL.get(y.lower(), y)
        nx, ny = _num(x), _num(y)
        if nx is not None and ny is not None:
            if abs(nx - ny) > 1e-4 * max(1.0, abs(nx), abs(ny)):
                return False
        elif x.lower() != y.lower():
            return False
    return True


def check(sliced: dict, live: dict, extra_keys=()) -> dict:
    """{checked, differ: [{key, sliced, now}], summary}. Keys missing on either side are skipped."""
    keys = list(dict.fromkeys([*KEYS, *(k.split("#", 1)[0] for k in extra_keys)]))
    differ, checked = [], 0
    for key in keys:
        if is_sensitive(key) or key not in sliced or key not in live:
            continue
        checked += 1
        if not same(sliced[key], live[key]):
            differ.append({"key": key, "sliced": sliced[key], "now": live[key]})
    if not checked:
        summary = "The G-code carries no settings to compare."
    elif not differ:
        summary = f"Sliced with the settings that are set now ({checked} compared)."
    else:
        names = ", ".join(d["key"] for d in differ[:6]) + (" and more" if len(differ) > 6 else "")
        summary = (f"The G-code was sliced with different values for {len(differ)} of {checked} settings: {names}. "
                   "A plate's own settings (bed type, print sequence, spiral vase) or a change made after slicing "
                   "can cause this; check before printing.")
    return {"checked": checked, "differ": differ, "summary": summary}
