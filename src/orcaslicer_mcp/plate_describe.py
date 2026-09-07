"""Turn the last slice's G-code into per-object plate facts for the model (spec:
docs/superpowers/specs/2026-09-08-describe-plate-design.md). Stdlib only: this module is pure so it
can be tested on fixtures without OrcaSlicer, and vendored if ever needed.

Everything spatial is computed on a 1 mm XY occupancy grid per (object, layer), filled while the
file is read once. Grid cells, not polygons: robust to Orca splitting a wall loop across travel
moves, and needs no geometry library.
"""
from __future__ import annotations
import math
import re
from dataclasses import dataclass, field

GRID_MM = 1.0
SAMPLE_MM = 0.5          # spacing of points sampled along a move when marking cells
ARC_STEP_RAD = math.radians(10)

SUPPORT_ROLES = frozenset({"Support", "Support interface"})
NON_OBJECT_ROLES = frozenset({"Skirt", "Brim", "Custom", "Wipe tower"}) | SUPPORT_ROLES
WALL_ROLES = frozenset({"Outer wall", "Inner wall", "Overhang wall"})

_MOVE_RE = re.compile(r"([XYEIJ])(-?\d*\.?\d+)")


@dataclass
class LayerAcc:
    """Per (object, layer) accumulators."""
    cells: set[tuple[int, int]] = field(default_factory=set)            # object roles
    support_cells: set[tuple[int, int]] = field(default_factory=set)    # Support + Support interface
    interface_cells: set[tuple[int, int]] = field(default_factory=set)  # Support interface only
    wall_mm: float = 0.0
    overhang_mm: float = 0.0


@dataclass
class ObjectAcc:
    name: str
    layers: dict[int, LayerAcc] = field(default_factory=dict)
    seams: list[tuple[int, float, float]] = field(default_factory=list)   # (layer, x, y)

    def layer(self, idx: int) -> LayerAcc:
        acc = self.layers.get(idx)
        if acc is None:
            acc = self.layers[idx] = LayerAcc()
        return acc


@dataclass
class ParsedPlate:
    layers: list[tuple[float, float]] = field(default_factory=list)      # (z, height) per layer index
    objects: dict[str, ObjectAcc] = field(default_factory=dict)
    copy_labels: dict[str, set[int]] = field(default_factory=dict)
    per_object: bool = False
    config: dict[str, str] = field(default_factory=dict)
    printable_area: list[tuple[float, float]] = field(default_factory=list)


def _cell(x: float, y: float) -> tuple[int, int]:
    return (math.floor(x / GRID_MM), math.floor(y / GRID_MM))


def _mark(x0: float, y0: float, x1: float, y1: float, out: set[tuple[int, int]]) -> float:
    """Mark every grid cell a straight move passes over (sampled every SAMPLE_MM); return its length."""
    d = math.hypot(x1 - x0, y1 - y0)
    n = max(1, int(d / SAMPLE_MM))
    for i in range(n + 1):
        t = i / n
        out.add(_cell(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    return d


def _arc_points(x0, y0, x1, y1, i, j, clockwise: bool) -> list[tuple[float, float]]:
    """Approximate a G2/G3 arc (center offset I,J from the start point) as chord endpoints."""
    cx, cy = x0 + i, y0 + j
    r = math.hypot(i, j)
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    sweep = a1 - a0
    if clockwise and sweep > 0:
        sweep -= 2 * math.pi
    if not clockwise and sweep < 0:
        sweep += 2 * math.pi
    n = max(1, int(abs(sweep) / ARC_STEP_RAD))
    return [(cx + r * math.cos(a0 + sweep * k / n), cy + r * math.sin(a0 + sweep * k / n)) for k in range(1, n + 1)]


def _parse_printable_area(value: str) -> list[tuple[float, float]]:
    pts = []
    for part in value.split(","):
        if "x" in part:
            a, b = part.strip().split("x", 1)
            try:
                pts.append((float(a), float(b)))
            except ValueError:
                pass
    return pts


def parse_gcode(text: str) -> ParsedPlate:
    plate = ParsedPlate()
    layer = -1
    role: str | None = None
    obj_name = "plate"
    x = y = None
    e_abs = 0.0
    relative_e = False
    in_config = False
    saw_marker = False
    plate.objects["plate"] = ObjectAcc("plate")

    for ln in text.splitlines():
        if in_config:
            if ln.startswith("; CONFIG_BLOCK_END"):
                in_config = False
            elif ln.startswith("; ") and " = " in ln:
                k, v = ln[2:].split(" = ", 1)
                plate.config[k.strip()] = v.strip()
            continue
        if not ln:
            continue
        c0 = ln[0]
        if c0 == ";":
            if ln.startswith(";LAYER_CHANGE"):
                layer += 1
                plate.layers.append((0.0, 0.0))
            elif ln.startswith(";Z:") and layer >= 0:
                plate.layers[layer] = (float(ln[3:]), plate.layers[layer][1])
            elif ln.startswith(";HEIGHT:") and layer >= 0:
                plate.layers[layer] = (plate.layers[layer][0], float(ln[8:]))
            elif ln.startswith(";TYPE:"):
                role = ln[6:].strip()
            elif ln.startswith(";WIPE_START"):
                if role == "Outer wall" and x is not None and layer >= 0 and not (saw_marker and obj_name == "plate"):
                    plate.objects[obj_name].seams.append((layer, x, y))
            elif ln.startswith("; printing object "):
                saw_marker = True
                body = ln[len("; printing object "):]
                name, _, rest = body.partition(" id:")
                obj_name = name.strip()
                m = re.search(r"copy (\d+)", rest)
                plate.copy_labels.setdefault(obj_name, set()).add(int(m.group(1)) if m else 0)
                if obj_name not in plate.objects:
                    plate.objects[obj_name] = ObjectAcc(obj_name)
            elif ln.startswith("; stop printing object "):
                obj_name = "plate"
            elif ln.startswith("; CONFIG_BLOCK_START"):
                in_config = True
            continue
        if c0 == "M":
            if ln.startswith("M83"):
                relative_e = True
            elif ln.startswith("M82"):
                relative_e = False
            continue
        if c0 != "G":
            continue
        code = ln[:3]
        if code == "G92":
            m = re.search(r"E(-?\d*\.?\d+)", ln)
            if m:
                e_abs = float(m.group(1))
            continue
        if code not in ("G0 ", "G1 ", "G2 ", "G3 "):
            continue
        words = dict(_MOVE_RE.findall(ln))
        nx = float(words["X"]) if "X" in words else x
        ny = float(words["Y"]) if "Y" in words else y
        extruding = False
        if "E" in words:
            e = float(words["E"])
            if relative_e:
                extruding = e > 0
            else:
                extruding = e > e_abs
                e_abs = e
        if extruding and x is not None and nx is not None and ny is not None and role and layer >= 0:
            if code in ("G2 ", "G3 "):
                pts = _arc_points(x, y, nx, ny, float(words.get("I", 0)), float(words.get("J", 0)), code == "G2 ")
            else:
                pts = [(nx, ny)]
            px, py = x, y
            for qx, qy in pts:
                if role in SUPPORT_ROLES:
                    acc = plate.objects[obj_name].layer(layer)
                    _mark(px, py, qx, qy, acc.support_cells)
                    if role == "Support interface":
                        _mark(px, py, qx, qy, acc.interface_cells)
                elif role not in NON_OBJECT_ROLES:
                    acc = plate.objects[obj_name].layer(layer)
                    length = _mark(px, py, qx, qy, acc.cells)
                    if role in WALL_ROLES:
                        acc.wall_mm += length
                        if role == "Overhang wall":
                            acc.overhang_mm += length
                px, py = qx, qy
        x, y = nx, ny

    plate.per_object = saw_marker
    if saw_marker and not plate.objects["plate"].layers and not plate.objects["plate"].seams:
        del plate.objects["plate"]
    plate.printable_area = _parse_printable_area(plate.config.get("printable_area", ""))
    return plate
