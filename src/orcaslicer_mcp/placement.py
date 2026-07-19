"""Pure, I/O-free plate-fit estimation.

Advisory ONLY. Given the object footprints (world-space AABB from size_mm + offset,
already baked with rotation/scale by the fork) plus the active skirt/brim settings, it
estimates whether each object *and its skirt/brim ring* stays inside the printable area.

Accuracy floor: this works from the object footprint, NOT the sliced toolpath. It excludes
skirt arc rounding, half-line-width, travel/wipe excursions, and exclusion zones. At small
margins (a few mm) it can report "fits" when OrcaSlicer's own boundary check errors - so it
is a fast first-pass, and the fork's real slice warnings (get_slice_warnings) are the arbiter.
"""
from __future__ import annotations
import re

# Config keys the check needs; server fetches these and passes cfg through.
CFG_KEYS = ["printable_area", "brim_type", "brim_width", "brim_object_gap",
            "skirt_loops", "skirt_distance", "skirt_line_width",
            "initial_layer_line_width", "line_width", "nozzle_diameter"]

# Brim types that add material OUTSIDE the object outline.
_OUTWARD_BRIM = {"outer_only", "outer_and_inner", "auto_brim", "brim_ears", "painted"}


def _f(cfg: dict, key: str, default: float = 0.0) -> float:
    """Coerce a config value (often a string) to float; default on missing/blank/bad."""
    v = cfg.get(key, default)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_bed(s):
    """Parse a printable_area polygon string ('0x0,300x0,...' or ';'-separated) to
    (min_xy, max_xy). Returns None if absent/unparseable."""
    if not s or not isinstance(s, str):
        return None
    pts = []
    for tok in re.split(r"[;,]", s):
        tok = tok.strip()
        if not tok:
            continue
        parts = tok.split("x")
        if len(parts) != 2:
            return None
        try:
            pts.append((float(parts[0]), float(parts[1])))
        except ValueError:
            return None
    if len(pts) < 3:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return ([min(xs), min(ys)], [max(xs), max(ys)])


def _ring(cfg: dict) -> float:
    """Isotropic outward margin = brim reach + skirt reach (skirt stacks outside brim)."""
    brim_out = 0.0
    if cfg.get("brim_type") in _OUTWARD_BRIM and _f(cfg, "brim_width") > 0:
        brim_out = _f(cfg, "brim_object_gap") + _f(cfg, "brim_width")
    skirt_out = 0.0
    loops = _f(cfg, "skirt_loops")
    if loops > 0:
        skw = next((w for w in (_f(cfg, "skirt_line_width"), _f(cfg, "initial_layer_line_width"),
                                _f(cfg, "line_width"), _f(cfg, "nozzle_diameter")) if w > 0), 0.0)
        skirt_out = _f(cfg, "skirt_distance") + loops * skw
    return brim_out + skirt_out


def _clearances(cx, cy, hx, hy, m, bmin, bmax):
    return {
        "left": cx - hx - m - bmin[0],
        "right": bmax[0] - (cx + hx + m),
        "front": cy - hy - m - bmin[1],
        "back": bmax[1] - (cy + hy + m),
    }


def check_placement(objects: dict, cfg: dict) -> dict:
    """Estimate per-object plate fit including the skirt/brim ring. See module docstring
    for the accuracy floor. Returns bed rect, ring width, per-object clearances, all_fit."""
    bed = _parse_bed(cfg.get("printable_area"))
    m = _ring(cfg)
    if bed is None:
        return {"bed": None, "ring_mm": m, "objects": [],
                "all_fit": None, "note": "printable_area missing/unparseable"}
    bmin, bmax = bed
    results = []
    all_fit = True
    for o in objects.get("objects", []):
        entry = {"id": o.get("id"), "name": o.get("name")}
        if o.get("instances", 1) != 1:
            entry.update({"supported": False,
                          "note": "multi-instance not supported by the estimate"})
            results.append(entry)
            all_fit = None if all_fit is not False else False
            continue
        size = o.get("size_mm", [0, 0, 0])
        off = o.get("transform", {}).get("offset", [0, 0, 0])
        cx, cy = float(off[0]), float(off[1])
        hx, hy = float(size[0]) / 2.0, float(size[1]) / 2.0
        cl = _clearances(cx, cy, hx, hy, m, bmin, bmax)
        obj_only = _clearances(cx, cy, hx, hy, 0.0, bmin, bmax)
        fits = all(v >= 0 for v in cl.values())
        overflow = max(0.0, -min(cl.values()))
        entry.update({
            "fits": fits,
            "expanded_bbox": {"min": [cx - hx - m, cy - hy - m],
                              "max": [cx + hx + m, cy + hy + m]},
            "clearances": cl,
            "object_only_clearances": obj_only,
            "overflow_mm": overflow,
        })
        results.append(entry)
        if not fits:
            all_fit = False
    return {"bed": {"min": bmin, "max": bmax}, "ring_mm": m,
            "objects": results, "all_fit": all_fit}
