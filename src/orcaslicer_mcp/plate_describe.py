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


CONTACT_EDGE_MAX = 0.15    # contact ratio below this: standing on an edge or corner
CONTACT_FLAT_MIN = 0.60    # at or above this: flat
OVERHANG_BAND_NOTE = 0.10  # a band with more than this share of overhang wall is named in the summary
ISLAND_MIN_CELLS = 3

_NEIGHBOURS = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)]


def islands(cells: set[tuple[int, int]], min_cells: int = ISLAND_MIN_CELLS) -> list[dict]:
    """8-connected components of occupied cells, largest first. Components smaller than
    min_cells are dropped (a lone cell is a sampling artefact, not a footprint)."""
    seen: set[tuple[int, int]] = set()
    out: list[dict] = []
    for start in cells:
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        comp: list[tuple[int, int]] = []
        while stack:
            c = stack.pop()
            comp.append(c)
            for dx, dy in _NEIGHBOURS:
                q = (c[0] + dx, c[1] + dy)
                if q in cells and q not in seen:
                    seen.add(q)
                    stack.append(q)
        if len(comp) >= min_cells:
            xs = [c[0] for c in comp]
            ys = [c[1] for c in comp]
            out.append({"area_mm2": len(comp),
                        "bbox": [min(xs), min(ys), max(xs) + 1, max(ys) + 1]})
    out.sort(key=lambda i: (-i["area_mm2"], i["bbox"]))
    return out


def filled_area(cells: set[tuple[int, int]]) -> int:
    """Total area of `cells` with every island's interior holes filled in: for each 8-connected
    island, fill each row between its min and max occupied x. A sparsely-infilled layer (only wall
    + infill-line centrelines rasterised, not the solid area between them) undercounts area against
    a densely-filled one; this gives a comparable denominator for contact_ratio."""
    seen: set[tuple[int, int]] = set()
    total = 0
    for start in cells:
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        comp: list[tuple[int, int]] = []
        while stack:
            c = stack.pop()
            comp.append(c)
            for dx, dy in _NEIGHBOURS:
                q = (c[0] + dx, c[1] + dy)
                if q in cells and q not in seen:
                    seen.add(q)
                    stack.append(q)
        rows: dict[int, list[int]] = {}
        for cx, cy in comp:
            rows.setdefault(cy, []).append(cx)
        for xs in rows.values():
            total += max(xs) - min(xs) + 1
    return total


def contact_class(ratio: float) -> str:
    if ratio < CONTACT_EDGE_MAX:
        return "edge_or_corner"
    if ratio < CONTACT_FLAT_MIN:
        return "tilted"
    return "flat"


def contact(obj: ObjectAcc) -> dict:
    """First-layer footprint versus the object's widest layer. Copy-agnostic: both sum over copies."""
    first = filled_area(obj.layers[0].cells) if 0 in obj.layers else 0
    widest = max((filled_area(a.cells) for a in obj.layers.values()), default=0)
    ratio = min(round(first / widest, 2), 1.0) if widest else 0.0
    return {"footprint_area_mm2": first, "max_layer_area_mm2": widest,
            "contact_ratio": ratio, "class": contact_class(ratio)}


def overhang_bands(obj: ObjectAcc, layers: list[tuple[float, float]], band_mm: float = 10.0) -> dict:
    """Overhang-wall length as a share of all wall length, per band_mm of Z. Bands run from 0 up to
    the highest layer that has any wall; empty intermediate bands are reported with share 0."""
    wall: dict[int, float] = {}
    over: dict[int, float] = {}
    top = -1
    for idx, acc in obj.layers.items():
        if idx >= len(layers) or acc.wall_mm == 0:
            continue
        b = int(layers[idx][0] // band_mm)
        wall[b] = wall.get(b, 0.0) + acc.wall_mm
        over[b] = over.get(b, 0.0) + acc.overhang_mm
        top = max(top, b)
    bands = []
    for b in range(top + 1):
        w = wall.get(b, 0.0)
        o = over.get(b, 0.0)
        bands.append({"z0": int(b * band_mm), "z1": int((b + 1) * band_mm),
                      "share": round(o / w, 2) if w else 0.0, "overhang_mm": round(o, 1)})
    return {"bands": bands, "total_mm": round(sum(over.values()), 1)}


SEAM_ALIGNED_MIN = 0.70
_SIDE_FOR_POSITION = {"back": "+Y", "rear": "+Y", "front": "-Y", "left": "-X", "right": "+X"}


def _seam_side_for_position(configured: str | None) -> str | None:
    return _SIDE_FOR_POSITION.get((configured or "").strip().lower())


def support(obj: ObjectAcc, layers: list[tuple[float, float]]) -> dict:
    """Where support stands (first-layer islands), how tall it is, and where its interface layers
    touch the part. Interface zones are XY islands of the UNION of interface cells across every
    layer (so a straggler cell or the same patch split across layers collapses into one zone);
    each zone's Z span is the min/max Z over the layers that actually have an interface cell
    inside its bbox."""
    sup_layers = sorted(i for i, a in obj.layers.items() if a.support_cells and i < len(layers))
    if not sup_layers:
        return {"present": False, "z_range": None, "islands": [], "interface_zones": []}
    z_lo = layers[sup_layers[0]][0]
    z_hi = layers[sup_layers[-1]][0]
    base = islands(obj.layers[sup_layers[0]].support_cells)

    union: set[tuple[int, int]] = set()
    for idx in sup_layers:
        union |= obj.layers[idx].interface_cells
    zones = islands(union)
    for zone in zones:
        x0, y0, x1, y1 = zone["bbox"]
        zs = [layers[idx][0] for idx in sup_layers
              if any(x0 <= c[0] < x1 and y0 <= c[1] < y1 for c in obj.layers[idx].interface_cells)]
        zone["z0"] = round(min(zs), 1)
        zone["z1"] = round(max(zs), 1)
    zones.sort(key=lambda zn: (zn["z0"], zn["bbox"]))
    return {"present": True, "z_range": [round(z_lo, 1), round(z_hi, 1)], "islands": base, "interface_zones": zones}


def _centroid(cells: set[tuple[int, int]]) -> tuple[float, float]:
    n = len(cells)
    return (sum(c[0] for c in cells) / n + 0.5, sum(c[1] for c in cells) / n + 0.5)


def _side(dx: float, dy: float) -> str:
    ang = math.degrees(math.atan2(dy, dx))
    if -45 <= ang < 45:
        return "+X"
    if 45 <= ang < 135:
        return "+Y"
    if -135 <= ang < -45:
        return "-Y"
    return "-X"


def seam(obj: ObjectAcc, configured: str | None) -> dict:
    """Which side of the part the outer-wall seams sit on, judged per seam against the centroid of
    the nearest footprint island on that layer (so copies do not pull the centroid to the middle
    of the plate). alignment = share on the dominant side."""
    counts = {"+X": 0, "-X": 0, "+Y": 0, "-Y": 0}
    for layer_idx, sx, sy in obj.seams:
        acc = obj.layers.get(layer_idx)
        if not acc or not acc.cells:
            continue
        isl = islands(acc.cells, min_cells=1)
        if not isl:
            continue
        # nearest island by bbox centre, then its cell centroid
        def _dist(i):
            bx = (i["bbox"][0] + i["bbox"][2]) / 2
            by = (i["bbox"][1] + i["bbox"][3]) / 2
            return (bx - sx) ** 2 + (by - sy) ** 2
        near = min(isl, key=_dist)
        members = {c for c in acc.cells
                   if near["bbox"][0] <= c[0] < near["bbox"][2] and near["bbox"][1] <= c[1] < near["bbox"][3]}
        cx, cy = _centroid(members or acc.cells)
        counts[_side(sx - cx, sy - cy)] += 1
    total = sum(counts.values())
    sides = {k: (round(v / total, 2) if total else 0.0) for k, v in counts.items()}
    dominant = max(counts, key=counts.get) if total else None
    alignment = sides[dominant] if dominant else 0.0
    expected = _seam_side_for_position(configured)
    agrees = (dominant == expected) if (dominant and expected) else None
    return {"count": total, "sides": sides, "dominant": dominant, "alignment": alignment,
            "configured": configured, "agrees": agrees}


_CLASS_PHRASE = {"edge_or_corner": "stands on an edge or corner", "tilted": "stands tilted", "flat": "lies flat"}


def _footprint_bbox(cells: set[tuple[int, int]]) -> list[int]:
    if not cells:
        return []
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    return [min(xs), min(ys), max(xs) + 1, max(ys) + 1]


def describe(parsed: ParsedPlate, objects_meta: list[dict] | None) -> dict:
    meta_by_name = {m.get("name"): m for m in (objects_meta or []) if m.get("name")}
    objs = []
    for name, obj in parsed.objects.items():
        if not obj.layers:
            continue
        first_cells = obj.layers[0].cells if 0 in obj.layers else set()
        c = contact(obj)
        d = {
            "name": name,
            "copies": int(meta_by_name.get(name, {}).get("instances") or 1),
            "orientation": {"class": c["class"], "contact_ratio": c["contact_ratio"]},
            "footprint": {"area_mm2": c["footprint_area_mm2"], "max_layer_area_mm2": c["max_layer_area_mm2"],
                          "bbox": _footprint_bbox(first_cells), "islands": islands(first_cells)},
            "overhang": overhang_bands(obj, parsed.layers),
            "support": support(obj, parsed.layers),
            "seam": seam(obj, parsed.config.get("seam_position")),
        }
        d["summary"] = summarize_object(d)
        if not parsed.per_object:
            d["summary"] = "label objects is off, so this describes the whole plate. " + d["summary"]
        objs.append(d)
    not_in_gcode = sorted(n for n in meta_by_name if n not in parsed.objects)
    height = max((z for z, _ in parsed.layers), default=0.0)
    return {
        "per_object": parsed.per_object,
        "objects": objs,
        "not_in_gcode": not_in_gcode,
        "plate": {"printable_area": [[x, y] for x, y in parsed.printable_area],
                  "layer_count": len(parsed.layers), "height_mm": round(height, 1),
                  "layer_height": parsed.config.get("layer_height")},
        "summary": "\n".join(o["summary"] for o in objs),
    }


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def summarize_object(d: dict) -> str:
    name = d["name"]
    copies = d.get("copies", 1)
    head = f"{name} ({copies} copies)" if copies > 1 else name
    ratio_pct = int(round(d["orientation"]["contact_ratio"] * 100))
    isl = d["footprint"]["islands"]
    if isl:
        mean = sum(i["area_mm2"] for i in isl) / len(isl)
        mean_txt = f"about {int(round(mean / 10.0) * 10) if mean >= 20 else int(round(mean))} mm2"
        island_txt = (f"in 1 island of {mean_txt}" if len(isl) == 1
                      else f"in {len(isl)} islands of {mean_txt} each")
    else:
        island_txt = "with no first-layer footprint found"
    parts = [f"{head} {_CLASS_PHRASE[d['orientation']['class']]}: first-layer contact is {ratio_pct}% of its "
             f"widest layer, {island_txt}."]

    heavy = [b for b in d["overhang"]["bands"] if b["share"] > OVERHANG_BAND_NOTE]
    if d["overhang"]["total_mm"] == 0:
        parts.append("No overhang extrusions.")
    elif heavy:
        parts.append(f"Overhang extrusions concentrate at Z {heavy[0]['z0']} to {heavy[-1]['z1']} mm.")
    else:
        parts.append("Overhang extrusions are present but spread thinly (no band above "
                     f"{int(OVERHANG_BAND_NOTE * 100)}% of wall length).")

    s = d["support"]
    if s["present"]:
        parts.append(f"Support is present from Z {s['z_range'][0]} to {s['z_range'][1]} mm, standing in "
                     f"{_plural(len(s['islands']), 'place')} and touching the part in {_plural(len(s['interface_zones']), 'zone')}.")
    else:
        parts.append("No support.")

    sm = d["seam"]
    cfg = sm.get("configured")
    cfg_side = _seam_side_for_position(cfg)
    if sm["count"] == 0:
        parts.append("No seam points found.")
    elif sm["alignment"] >= SEAM_ALIGNED_MIN:
        pct = int(round(sm["alignment"] * 100))
        if sm["agrees"] is True:
            parts.append(f"Seams align on the {sm['dominant']} side ({pct}%), matching seam_position={cfg}.")
        elif sm["agrees"] is False:
            parts.append(f"Seams align on the {sm['dominant']} side ({pct}%), which disagrees with seam_position={cfg}.")
        else:
            parts.append(f"Seams align on the {sm['dominant']} side ({pct}%).")
    else:
        pct = int(round(sm["alignment"] * 100))
        tail = f", although seam_position={cfg} asks for one side." if cfg_side else "."
        parts.append(f"Seams are scattered (largest share {pct}% on the {sm['dominant']} side){tail}")
    return " ".join(parts)
