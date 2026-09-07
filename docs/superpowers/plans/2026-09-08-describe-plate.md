# describe_plate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only `describe_plate` MCP tool that turns the last slice's G-code into per-object plate facts (orientation class, footprint islands, overhang bands, support placement, seam side) plus a server-written summary sentence.

**Architecture:** One pure stdlib module `plate_describe.py` does everything computable: a single-pass G-code parser that rasterises extrusion moves onto a 1 mm occupancy grid per (object, layer) as it reads, an analysis step over those grids, and a summariser with fixed thresholds. `server.py` gains a thin async orchestrator that fetches the G-code and object list, caches the parsed result for the current slice, and returns the description. This mirrors `compare.py` + `compare_slices` and `breakdown.py` + `get_slice_breakdown`.

**Tech Stack:** Python 3.11+, stdlib only for the module (`re`, `math`, `dataclasses`, `hashlib`, `lzma` in tests), mcp 2.x `MCPServer`, httpx client already in the repo, pytest + respx for tests.

**Spec:** `docs/superpowers/specs/2026-09-08-describe-plate-design.md`

## Global Constraints

- Repo: `~/projects/orcaslicer-mcp`. Run tests with `uv run pytest -q` from the repo root. The suite currently passes 235 tests, 1 skipped; it must stay green after every task.
- No `git config user.*` in any form; the global identity is correct. No `Co-Authored-By` or any Claude attribution in commits, docs, or manifests.
- Never commit a raw `.gcode` file (pre-commit hook rejects files over 1 MB and `tests/fixtures/*.gcode` is gitignored). Fixtures are `.gcode.xz`.
- `plate_describe.py` imports only the standard library.
- Public copy (README, docstrings) uses no em-dashes.
- Tool count goes 43 -> 44; the annotation table in `server.py` (`_TOOL_ANNOTATIONS`) must list the new tool or `tests/test_tool_annotations.py` fails.
- Role names are Orca's exact `;TYPE:` strings: `Outer wall`, `Inner wall`, `Overhang wall`, `Sparse infill`, `Internal solid infill`, `Top surface`, `Bottom surface`, `Bridge`, `Internal Bridge`, `Gap infill`, `Skirt`, `Brim`, `Support`, `Support interface`, `Custom`, `Wipe tower`.

## G-code facts the parser relies on (verified on the fixtures 2026-09-08)

- `;LAYER_CHANGE` then `;Z:<z>` then `;HEIGHT:<h>` start each layer. Layer index starts at 0.
- `;TYPE:<role>` starts an extrusion block; the role stays current until the next `;TYPE:`.
- `; printing object <name> id:<n> copy <m>` and `; stop printing object ...` bracket per-object toolpaths. Every instance made with duplicate_object is labelled `copy 0`, so copies cannot be separated. When a file has no such markers, everything belongs to one object keyed `"plate"`.
- Moves: `G1`/`G0` with optional `X`, `Y`, `E`, `F`; arcs `G2` (clockwise) / `G3` (counter-clockwise) with `I`, `J` center offsets (arc fitting is on: 29,908 arcs in the Body4 fixture). Extrusion is relative (`M83`); an extruding move has `E` > 0. Support absolute E too: after `M82`/`G90` without `M83`, extrusion is `E - previous E > 0`. `G92 E0` resets the absolute E reference.
- `;WIPE_START` immediately follows the retraction after a loop closes; the current XY at that comment, while the role is `Outer wall`, is a seam point.
- The config block sits between `; CONFIG_BLOCK_START` and `; CONFIG_BLOCK_END` as `; key = value` lines. Needed keys: `printable_area` (`0x0,300x0,300x300,0x300`), `seam_position`, `layer_height`, `support_type`, `support_style`, `gcode_label_objects`.
- Measured expectations (grid prototype): cube20 fixture -> first layer 400 cells, max layer 400 cells, one island, 1 seam on `+Y`. Body4 fixture -> first layer 151 cells, max 1553 (ratio 0.097), islands of 62/44/43 cells plus two 1-cell stragglers, overhang wall 3.4% of wall length overall, support on layers 0..188, 90 seams with 84% on `+Y`. Parse time 0.9 s in pure Python.

## File structure

- Create `src/orcaslicer_mcp/plate_describe.py`: `parse_gcode`, `islands`, `describe`, `summarize`, dataclasses, thresholds. One responsibility: G-code text in, description dict out.
- Modify `src/orcaslicer_mcp/server.py`: import, `describe_plate` tool, cache, annotation row, one line in the `slice-a-model` prompt.
- Create `tests/conftest.py` (does not exist yet): fixture loader for `.gcode.xz`.
- Create `tests/test_plate_describe.py` (pure module) and `tests/test_server_describe_plate.py` (tool with respx).
- Modify `README.md`, `CHANGELOG.md`, `lhm.plugin.json`.
- Modify `~/projects/3d-printer/.claude/skills/print-loop/SKILL.md` (other repo, one line).

---

### Task 1: Fixture loader and parser skeleton (layers, roles, objects, config block)

**Files:**
- Create: `src/orcaslicer_mcp/plate_describe.py`
- Create: `tests/conftest.py`
- Create: `tests/test_plate_describe.py`

**Interfaces:**
- Produces: `parse_gcode(text: str) -> ParsedPlate`; dataclasses `LayerAcc`, `ObjectAcc`, `ParsedPlate` as below. Later tasks add fields to `LayerAcc`/`ObjectAcc` but never rename these.

- [ ] **Step 1: Write the fixture loader**

`tests/conftest.py`:
```python
import lzma
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def gcode_fixture():
    """Return the decompressed text of tests/fixtures/<name>.gcode.xz (cached per session)."""
    cache: dict[str, str] = {}

    def load(name: str) -> str:
        if name not in cache:
            with lzma.open(FIXTURES / f"{name}.gcode.xz", "rt", encoding="utf-8", errors="replace") as fh:
                cache[name] = fh.read()
        return cache[name]
    return load
```

- [ ] **Step 2: Write the failing tests**

`tests/test_plate_describe.py`:
```python
from orcaslicer_mcp import plate_describe as pd

MINI = """; header
; EXECUTABLE_BLOCK_START
M83
;LAYER_CHANGE
;Z:0.4
;HEIGHT:0.4
;TYPE:Outer wall
G1 X10 Y10 F3000
G1 X20 Y10 E1
G1 X20 Y20 E1
G1 X10 Y20 E1
G1 X10 Y10 E1
;LAYER_CHANGE
;Z:1
;HEIGHT:0.6
;TYPE:Inner wall
G1 X12 Y12 E0.5
; EXECUTABLE_BLOCK_END
; CONFIG_BLOCK_START
; layer_height = 0.6
; printable_area = 0x0,300x0,300x300,0x300
; seam_position = back
; CONFIG_BLOCK_END
"""


def test_parse_layers_and_config_from_minimal_gcode():
    p = pd.parse_gcode(MINI)
    assert [round(z, 1) for z, _ in p.layers] == [0.4, 1.0]
    assert [round(h, 1) for _, h in p.layers] == [0.4, 0.6]
    assert p.config["layer_height"] == "0.6"
    assert p.config["seam_position"] == "back"
    assert p.printable_area == [(0.0, 0.0), (300.0, 0.0), (300.0, 300.0), (0.0, 300.0)]
    assert p.per_object is False
    assert list(p.objects) == ["plate"]


def test_object_markers_split_objects_and_set_per_object():
    text = MINI.replace(";TYPE:Outer wall\n", "; printing object A.stl id:0 copy 0\n;TYPE:Outer wall\n") \
               .replace(";TYPE:Inner wall\n", "; stop printing object A.stl id:0 copy 0\n; printing object B.stl id:1 copy 0\n;TYPE:Inner wall\n")
    p = pd.parse_gcode(text)
    assert p.per_object is True
    assert sorted(p.objects) == ["A.stl", "B.stl"]
    assert p.copy_labels == {"A.stl": {0}, "B.stl": {0}}


def test_layer_with_no_extrusion_and_move_before_any_layer_are_harmless():
    text = MINI.replace(";TYPE:Inner wall\nG1 X12 Y12 E0.5\n", ";TYPE:Inner wall\nG1 X12 Y12 F3000\n")
    text = "G1 X5 Y5 E1\n" + text          # extrusion before the first ;LAYER_CHANGE is ignored
    p = pd.parse_gcode(text)
    assert len(p.layers) == 2
    assert 1 not in p.objects["plate"].layers          # no accumulator is created for an empty layer
    assert len(p.objects["plate"].layers[0].cells) > 0


def test_real_fixtures_parse(gcode_fixture):
    cube = pd.parse_gcode(gcode_fixture("cube20_flat"))
    assert len(cube.layers) == 34 and cube.per_object is True and list(cube.objects) == ["cube20.stl"]
    body = pd.parse_gcode(gcode_fixture("body4_corner_x3_support"))
    assert len(body.layers) == 194 and list(body.objects) == ["Body4.stl"]
    assert body.copy_labels == {"Body4.stl": {0}}  # Orca labels every duplicate copy 0 (spec: per-copy limitation)
    assert body.config["support_type"] == "tree(auto)" and body.config["seam_position"] == "back"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_plate_describe.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'orcaslicer_mcp.plate_describe'`

- [ ] **Step 4: Write the parser skeleton**

`src/orcaslicer_mcp/plate_describe.py`:
```python
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
                if role == "Outer wall" and x is not None and layer >= 0:
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
            acc = plate.objects[obj_name].layer(layer)
            px, py = x, y
            for qx, qy in pts:
                if role in SUPPORT_ROLES:
                    _mark(px, py, qx, qy, acc.support_cells)
                    if role == "Support interface":
                        _mark(px, py, qx, qy, acc.interface_cells)
                elif role not in NON_OBJECT_ROLES:
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_plate_describe.py -q`
Expected: 4 passed. If `test_real_fixtures_parse` reports `["Body4.stl", "plate"]`, some extrusion happened outside object markers (skirt/brim before the first marker are NON_OBJECT_ROLES and do not create layers, but check); the deletion rule above only drops `"plate"` when it is empty, which is the intended behaviour: keep it if anything real landed there.

- [ ] **Step 6: Run the whole suite and commit**

Run: `uv run pytest -q` (expected 238 passed, 1 skipped)
```bash
git add src/orcaslicer_mcp/plate_describe.py tests/conftest.py tests/test_plate_describe.py
git commit -m "feat: G-code parser for describe_plate (layers, roles, object markers, config block, occupancy grid)"
```

---

### Task 2: Grid facts: occupancy counts, islands, contact ratio, overhang bands

**Files:**
- Modify: `src/orcaslicer_mcp/plate_describe.py`
- Modify: `tests/test_plate_describe.py`

**Interfaces:**
- Consumes: `ParsedPlate`, `ObjectAcc`, `LayerAcc` from Task 1.
- Produces: `islands(cells: set[tuple[int,int]], min_cells: int = 3) -> list[dict]` each `{"area_mm2": int, "bbox": [xmin, ymin, xmax, ymax]}` sorted by area desc; `contact(obj: ObjectAcc) -> dict` = `{"footprint_area_mm2", "max_layer_area_mm2", "contact_ratio", "class"}`; `overhang_bands(obj, layers, band_mm=10.0) -> dict` = `{"bands": [{"z0","z1","share","overhang_mm"}], "total_mm"}`; constants `CONTACT_EDGE_MAX = 0.15`, `CONTACT_FLAT_MIN = 0.60`, `OVERHANG_BAND_NOTE = 0.10`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_plate_describe.py`:
```python
def _square(x0, y0, n):
    return {(x0 + i, y0 + j) for i in range(n) for j in range(n)}


def test_islands_splits_components_and_drops_stragglers():
    cells = _square(0, 0, 10) | _square(20, 20, 5) | {(40, 40)}
    out = pd.islands(cells)
    assert [i["area_mm2"] for i in out] == [100, 25]           # the 1-cell straggler is dropped (min_cells=3)
    assert out[0]["bbox"] == [0, 0, 10, 10]                      # bbox is in mm: cell max + 1
    assert pd.islands(cells, min_cells=1)[-1] == {"area_mm2": 1, "bbox": [40, 40, 41, 41]}


def test_islands_are_8_connected():
    cells = {(0, 0), (1, 1), (2, 2)}                            # diagonal chain
    assert len(pd.islands(cells, min_cells=1)) == 1


def test_contact_classes():
    o = pd.ObjectAcc("t")
    o.layer(0).cells = _square(0, 0, 4)          # 16 cells on the plate
    o.layer(5).cells = _square(0, 0, 20)         # widest layer 400 cells
    c = pd.contact(o)
    assert c["footprint_area_mm2"] == 16 and c["max_layer_area_mm2"] == 400
    assert c["contact_ratio"] == 0.04 and c["class"] == "edge_or_corner"
    o.layer(0).cells = _square(0, 0, 12)         # 144/400 = 0.36
    assert pd.contact(o)["class"] == "tilted"
    o.layer(0).cells = _square(0, 0, 20)
    assert pd.contact(o)["class"] == "flat" and pd.contact(o)["contact_ratio"] == 1.0


def test_contact_with_no_first_layer_is_zero_not_error():
    o = pd.ObjectAcc("t")
    o.layer(3).cells = _square(0, 0, 5)
    c = pd.contact(o)
    assert c["footprint_area_mm2"] == 0 and c["contact_ratio"] == 0.0 and c["class"] == "edge_or_corner"


def test_overhang_bands_by_z():
    o = pd.ObjectAcc("t")
    layers = [(0.4, 0.4), (5.0, 0.6), (12.0, 0.6), (25.0, 0.6)]
    o.layer(0).wall_mm, o.layer(0).overhang_mm = 100.0, 30.0
    o.layer(1).wall_mm, o.layer(1).overhang_mm = 100.0, 0.0
    o.layer(2).wall_mm, o.layer(2).overhang_mm = 50.0, 10.0
    o.layer(3).wall_mm, o.layer(3).overhang_mm = 50.0, 0.0
    out = pd.overhang_bands(o, layers)
    assert out["total_mm"] == 40.0
    assert out["bands"] == [{"z0": 0, "z1": 10, "share": 0.15, "overhang_mm": 30.0},
                            {"z0": 10, "z1": 20, "share": 0.2, "overhang_mm": 10.0},
                            {"z0": 20, "z1": 30, "share": 0.0, "overhang_mm": 0.0}]


def test_real_fixture_grid_facts(gcode_fixture):
    cube = pd.parse_gcode(gcode_fixture("cube20_flat")).objects["cube20.stl"]
    c = pd.contact(cube)
    assert c["class"] == "flat" and c["contact_ratio"] == 1.0
    assert 380 <= c["footprint_area_mm2"] <= 420
    assert len(pd.islands(cube.layer(0).cells)) == 1
    body = pd.parse_gcode(gcode_fixture("body4_corner_x3_support")).objects["Body4.stl"]
    c = pd.contact(body)
    assert c["class"] == "edge_or_corner" and 0.05 <= c["contact_ratio"] <= 0.15
    assert len(pd.islands(body.layer(0).cells)) == 3       # three copies, stragglers filtered
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_plate_describe.py -q`
Expected: the new tests FAIL with `AttributeError: module ... has no attribute 'islands'` (and `contact`, `overhang_bands`).

- [ ] **Step 3: Implement**

Append to `src/orcaslicer_mcp/plate_describe.py`:
```python
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


def contact_class(ratio: float) -> str:
    if ratio < CONTACT_EDGE_MAX:
        return "edge_or_corner"
    if ratio < CONTACT_FLAT_MIN:
        return "tilted"
    return "flat"


def contact(obj: ObjectAcc) -> dict:
    """First-layer footprint versus the object's widest layer. Copy-agnostic: both sum over copies."""
    first = len(obj.layers[0].cells) if 0 in obj.layers else 0
    widest = max((len(a.cells) for a in obj.layers.values()), default=0)
    ratio = round(first / widest, 2) if widest else 0.0
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_plate_describe.py -q`
Expected: all pass. If `test_real_fixture_grid_facts` finds 4 or 5 islands on Body4, raise `ISLAND_MIN_CELLS` to 4 and re-run; the prototype saw stragglers of exactly 1 cell, so 3 should do.

- [ ] **Step 5: Commit**

```bash
git add src/orcaslicer_mcp/plate_describe.py tests/test_plate_describe.py
git commit -m "feat: describe_plate grid facts (islands, contact class, overhang bands)"
```

---

### Task 3: Support placement and seam sides

**Files:**
- Modify: `src/orcaslicer_mcp/plate_describe.py`
- Modify: `tests/test_plate_describe.py`

**Interfaces:**
- Consumes: `islands`, `ObjectAcc`, `ParsedPlate` (Tasks 1, 2).
- Produces: `support(obj: ObjectAcc, layers) -> dict` = `{"present": bool, "z_range": [z_lo, z_hi] | None, "islands": [...], "interface_zones": [{"bbox": [...], "z0": float, "z1": float, "area_mm2": int}]}`; `seam(obj: ObjectAcc, configured: str | None) -> dict` = `{"count": int, "sides": {"+X","-X","+Y","-Y"}, "dominant": str | None, "alignment": float, "configured": str | None, "agrees": bool | None}`; constant `SEAM_ALIGNED_MIN = 0.70`; helper `_seam_side_for_position(configured) -> str | None` mapping `back->+Y`, `front->-Y`, `left->-X`, `right->+X`, else None.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_plate_describe.py`:
```python
def test_support_absent():
    o = pd.ObjectAcc("t")
    o.layer(0).cells = _square(0, 0, 5)
    out = pd.support(o, [(0.4, 0.4)])
    assert out == {"present": False, "z_range": None, "islands": [], "interface_zones": []}


def test_support_towers_and_interface_zones():
    o = pd.ObjectAcc("t")
    layers = [(0.4, 0.4), (1.0, 0.6), (1.6, 0.6), (2.2, 0.6)]
    o.layer(0).support_cells = _square(0, 0, 4) | _square(30, 30, 4)   # two towers stand here
    o.layer(1).support_cells = _square(0, 0, 4) | _square(30, 30, 4)
    o.layer(2).support_cells = _square(0, 0, 4)
    o.layer(2).interface_cells = _square(0, 0, 4)                        # interface touches the part at Z 1.6
    o.layer(3).support_cells = _square(30, 30, 4)
    o.layer(3).interface_cells = _square(30, 30, 4)
    out = pd.support(o, layers)
    assert out["present"] is True and out["z_range"] == [0.4, 2.2]
    assert [i["area_mm2"] for i in out["islands"]] == [16, 16]
    zones = out["interface_zones"]
    assert len(zones) == 2
    assert {"bbox": [0, 0, 4, 4], "z0": 1.6, "z1": 1.6, "area_mm2": 16} in zones
    assert {"bbox": [30, 30, 34, 34], "z0": 2.2, "z1": 2.2, "area_mm2": 16} in zones


def test_interface_zones_merge_adjacent_layers_over_the_same_spot():
    o = pd.ObjectAcc("t")
    layers = [(0.4, 0.4), (1.0, 0.6), (1.6, 0.6)]
    for i in range(3):
        o.layer(i).support_cells = _square(0, 0, 3)
        o.layer(i).interface_cells = _square(0, 0, 3)
    zones = pd.support(o, layers)["interface_zones"]
    assert zones == [{"bbox": [0, 0, 3, 3], "z0": 0.4, "z1": 1.6, "area_mm2": 9}]


def test_seam_sides_relative_to_layer_centroid():
    o = pd.ObjectAcc("t")
    o.layer(0).cells = _square(0, 0, 20)          # centroid at (10, 10)
    o.seams = [(0, 10.0, 19.5), (0, 10.0, 19.0), (0, 19.5, 10.0)]   # two on +Y, one on +X
    out = pd.seam(o, "back")
    assert out["count"] == 3
    assert out["sides"] == {"+X": 0.33, "-X": 0.0, "+Y": 0.67, "-Y": 0.0}
    assert out["dominant"] == "+Y" and out["alignment"] == 0.67
    assert out["configured"] == "back" and out["agrees"] is True


def test_seam_scattered_and_unknown_config():
    o = pd.ObjectAcc("t")
    o.layer(0).cells = _square(0, 0, 20)
    o.seams = [(0, 10.0, 19.5), (0, 19.5, 10.0), (0, 0.5, 10.0), (0, 10.0, 0.5)]
    out = pd.seam(o, "random")
    assert out["alignment"] == 0.25 and out["dominant"] in ("+X", "-X", "+Y", "-Y")
    assert out["configured"] == "random" and out["agrees"] is None   # random/aligned/nearest have no side
    assert pd.seam(pd.ObjectAcc("empty"), "back") == {"count": 0, "sides": {"+X": 0.0, "-X": 0.0, "+Y": 0.0, "-Y": 0.0},
                                                       "dominant": None, "alignment": 0.0, "configured": "back", "agrees": None}


def test_real_fixture_support_and_seams(gcode_fixture):
    cube = pd.parse_gcode(gcode_fixture("cube20_flat"))
    s = pd.seam(cube.objects["cube20.stl"], cube.config.get("seam_position"))
    assert s["dominant"] == "+Y" and s["agrees"] is True
    assert pd.support(cube.objects["cube20.stl"], cube.layers)["present"] is False
    body = pd.parse_gcode(gcode_fixture("body4_corner_x3_support"))
    obj = body.objects["Body4.stl"]
    sup = pd.support(obj, body.layers)
    assert sup["present"] is True and sup["z_range"][0] < 1.0 and sup["z_range"][1] > 50.0
    assert len(sup["islands"]) >= 3 and len(sup["interface_zones"]) >= 3
    s = pd.seam(obj, body.config.get("seam_position"))
    assert s["count"] > 50 and s["dominant"] == "+Y" and s["alignment"] >= 0.7 and s["agrees"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_plate_describe.py -q -k "support or seam"`
Expected: FAIL with `AttributeError: ... 'support'` / `'seam'`.

- [ ] **Step 3: Implement**

Append to `src/orcaslicer_mcp/plate_describe.py`:
```python
SEAM_ALIGNED_MIN = 0.70
_SIDE_FOR_POSITION = {"back": "+Y", "rear": "+Y", "front": "-Y", "left": "-X", "right": "+X"}


def _seam_side_for_position(configured: str | None) -> str | None:
    return _SIDE_FOR_POSITION.get((configured or "").strip().lower())


def support(obj: ObjectAcc, layers: list[tuple[float, float]]) -> dict:
    """Where support stands (first-layer islands), how tall it is, and where its interface layers
    touch the part. Interface zones are XY islands of interface cells, merged across consecutive
    layers when their bboxes overlap, so one contact patch is one zone with a Z span."""
    sup_layers = sorted(i for i, a in obj.layers.items() if a.support_cells and i < len(layers))
    if not sup_layers:
        return {"present": False, "z_range": None, "islands": [], "interface_zones": []}
    z_lo = layers[sup_layers[0]][0]
    z_hi = layers[sup_layers[-1]][0]
    base = islands(obj.layers[sup_layers[0]].support_cells)
    zones: list[dict] = []
    for idx in sup_layers:
        acc = obj.layers[idx]
        if not acc.interface_cells:
            continue
        z = layers[idx][0]
        for isl in islands(acc.interface_cells, min_cells=1):
            merged = False
            for zone in zones:
                if _bbox_overlap(zone["bbox"], isl["bbox"]) and _prev_layer_z(layers, idx) <= zone["z1"] + 1e-6:
                    zone["bbox"] = [min(zone["bbox"][0], isl["bbox"][0]), min(zone["bbox"][1], isl["bbox"][1]),
                                    max(zone["bbox"][2], isl["bbox"][2]), max(zone["bbox"][3], isl["bbox"][3])]
                    zone["z1"] = z
                    zone["area_mm2"] = max(zone["area_mm2"], isl["area_mm2"])
                    merged = True
                    break
            if not merged:
                zones.append({"bbox": list(isl["bbox"]), "z0": z, "z1": z, "area_mm2": isl["area_mm2"]})
    zones.sort(key=lambda zn: (zn["z0"], zn["bbox"]))
    return {"present": True, "z_range": [round(z_lo, 1), round(z_hi, 1)], "islands": base, "interface_zones": zones}


def _bbox_overlap(a: list, b: list) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _prev_layer_z(layers: list[tuple[float, float]], idx: int) -> float:
    return layers[idx - 1][0] if idx > 0 else layers[idx][0]


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_plate_describe.py -q`
Expected: all pass. `test_seam_sides_relative_to_layer_centroid` expects `0.33`/`0.67`: two of three seams on +Y round to 0.67, one to 0.33.

- [ ] **Step 5: Commit**

```bash
git add src/orcaslicer_mcp/plate_describe.py tests/test_plate_describe.py
git commit -m "feat: describe_plate support placement and seam sides"
```

---

### Task 4: describe() and summarize(): the assembled description and the sentence

**Files:**
- Modify: `src/orcaslicer_mcp/plate_describe.py`
- Modify: `tests/test_plate_describe.py`

**Interfaces:**
- Consumes: everything from Tasks 1 to 3.
- Produces: `describe(parsed: ParsedPlate, objects_meta: list[dict] | None) -> dict` with the top-level shape below; `summarize_object(desc: dict) -> str`. `objects_meta` is the `objects` list from the fork's `GET /objects` (`name`, `instances`, ...).

Top-level shape (spec):
```python
{"per_object": bool, "objects": [obj_desc, ...], "not_in_gcode": [names],
 "plate": {"printable_area": [[x,y],...], "layer_count": int, "height_mm": float, "layer_height": str | None},
 "summary": "one line per object, joined by newlines"}
obj_desc = {"name", "copies", "summary", "orientation": {"class", "contact_ratio"},
            "footprint": {"area_mm2", "max_layer_area_mm2", "bbox", "islands"},
            "overhang": {"bands", "total_mm"}, "support": {...}, "seam": {...}}
```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_plate_describe.py`:
```python
def test_describe_assembles_objects_copies_and_not_in_gcode(gcode_fixture):
    parsed = pd.parse_gcode(gcode_fixture("body4_corner_x3_support"))
    meta = [{"name": "Body4.stl", "instances": 3}, {"name": "ghost.stl", "instances": 1}]
    out = pd.describe(parsed, meta)
    assert out["per_object"] is True and out["not_in_gcode"] == ["ghost.stl"]
    assert out["plate"]["layer_count"] == 194 and out["plate"]["printable_area"][2] == [300.0, 300.0]
    assert out["plate"]["layer_height"] == "0.6" and 55 < out["plate"]["height_mm"] < 62
    (o,) = out["objects"]
    assert o["name"] == "Body4.stl" and o["copies"] == 3
    assert o["orientation"]["class"] == "edge_or_corner"
    assert len(o["footprint"]["islands"]) == 3 and o["footprint"]["bbox"][0] < o["footprint"]["bbox"][2]
    assert o["support"]["present"] is True and o["seam"]["dominant"] == "+Y"
    assert o["summary"] == out["summary"]          # one object -> the plate summary is its sentence


def test_describe_without_meta_defaults_copies_to_one_and_whole_plate_when_unlabelled():
    parsed = pd.parse_gcode(MINI)
    out = pd.describe(parsed, None)
    assert out["per_object"] is False
    (o,) = out["objects"]
    assert o["name"] == "plate" and o["copies"] == 1
    assert "label objects is off" in o["summary"]


def test_summarize_wording_edge_case_with_support_and_aligned_seam():
    desc = {"name": "Body4.stl", "copies": 3,
            "orientation": {"class": "edge_or_corner", "contact_ratio": 0.1},
            "footprint": {"area_mm2": 151, "max_layer_area_mm2": 1553, "bbox": [0, 0, 1, 1],
                          "islands": [{"area_mm2": 62, "bbox": []}, {"area_mm2": 44, "bbox": []}, {"area_mm2": 43, "bbox": []}]},
            "overhang": {"bands": [{"z0": 0, "z1": 10, "share": 0.31, "overhang_mm": 1500.0},
                                   {"z0": 10, "z1": 20, "share": 0.12, "overhang_mm": 500.0},
                                   {"z0": 20, "z1": 30, "share": 0.01, "overhang_mm": 10.0}], "total_mm": 2010.0},
            "support": {"present": True, "z_range": [0.4, 56.8], "islands": [{}] * 5,
                        "interface_zones": [{}] * 6},
            "seam": {"count": 90, "sides": {"+Y": 0.84, "-Y": 0.1, "+X": 0.02, "-X": 0.03}, "dominant": "+Y",
                     "alignment": 0.84, "configured": "back", "agrees": True}}
    s = pd.summarize_object(desc)
    assert s == ("Body4.stl (3 copies) stands on an edge or corner: first-layer contact is 10% of its widest "
                 "layer, in 3 islands of about 50 mm2 each. Overhang extrusions concentrate at Z 0 to 20 mm. "
                 "Support is present from Z 0.4 to 56.8 mm, standing in 5 places and touching the part in 6 zones. "
                 "Seams align on the +Y side (84%), matching seam_position=back.")


def test_summarize_wording_flat_no_support_scattered_seam_disagrees():
    desc = {"name": "cube20.stl", "copies": 1,
            "orientation": {"class": "flat", "contact_ratio": 1.0},
            "footprint": {"area_mm2": 400, "max_layer_area_mm2": 400, "bbox": [], "islands": [{"area_mm2": 400, "bbox": []}]},
            "overhang": {"bands": [{"z0": 0, "z1": 10, "share": 0.0, "overhang_mm": 0.0}], "total_mm": 0.0},
            "support": {"present": False, "z_range": None, "islands": [], "interface_zones": []},
            "seam": {"count": 33, "sides": {"+Y": 0.3, "-Y": 0.3, "+X": 0.2, "-X": 0.2}, "dominant": "+Y",
                     "alignment": 0.3, "configured": "back", "agrees": True}}
    s = pd.summarize_object(desc)
    assert s == ("cube20.stl lies flat: first-layer contact is 100% of its widest layer, in 1 island of about "
                 "400 mm2. No overhang extrusions. No support. Seams are scattered (largest share 30% on the +Y side), "
                 "although seam_position=back asks for one side.")
    desc["seam"] = {"count": 33, "sides": {"+Y": 0.1, "-Y": 0.8, "+X": 0.05, "-X": 0.05}, "dominant": "-Y",
                    "alignment": 0.8, "configured": "back", "agrees": False}
    assert pd.summarize_object(desc).endswith("Seams align on the -Y side (80%), which disagrees with seam_position=back.")
    desc["seam"] = {"count": 0, "sides": {"+Y": 0.0, "-Y": 0.0, "+X": 0.0, "-X": 0.0}, "dominant": None,
                    "alignment": 0.0, "configured": "back", "agrees": None}
    assert pd.summarize_object(desc).endswith("No seam points found.")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_plate_describe.py -q -k "describe or summarize"`
Expected: FAIL with `AttributeError: ... 'describe'`.

- [ ] **Step 3: Implement**

Append to `src/orcaslicer_mcp/plate_describe.py`:
```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_plate_describe.py -q`
Expected: all pass. The wording tests are exact strings; if one fails, fix the implementation to match the test, not the other way round, unless the test contradicts the spec.

- [ ] **Step 5: Commit**

```bash
git add src/orcaslicer_mcp/plate_describe.py tests/test_plate_describe.py
git commit -m "feat: describe_plate assembly and summary sentence"
```

---

### Task 5: The `describe_plate` tool, cache, and annotation

**Files:**
- Modify: `src/orcaslicer_mcp/server.py` (imports near line 25; new tool after `get_gcode` at about line 940; `_TOOL_ANNOTATIONS` table at about line 1105)
- Create: `tests/test_server_describe_plate.py`

**Interfaces:**
- Consumes: `plate_describe.parse_gcode`, `plate_describe.describe`; existing `OrcaClient.get_gcode() -> bytes` (raises `Conflict` when not sliced), `OrcaClient.get_objects()` (returns the `/objects` JSON; check its shape in `src/orcaslicer_mcp/client.py` and unwrap the `objects` list the same way `list_objects` does in `server.py`).
- Produces: tool `describe_plate() -> dict`; module-level `_DESCRIBE_CACHE: dict[str, dict]` and `_gcode_cache_key(data: bytes) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/test_server_describe_plate.py`:
```python
import lzma
from pathlib import Path

import httpx, respx
import orcaslicer_mcp.server as srv

B = "http://x:13130"
CUBE = lzma.open(Path(__file__).parent / "fixtures" / "cube20_flat.gcode.xz", "rb").read()
OBJECTS = {"objects": [{"id": 61, "name": "cube20.stl", "instances": 1, "size_mm": [20, 20, 20]}]}


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", B)
    srv._DESCRIBE_CACHE.clear()


@respx.mock
async def test_describe_plate_not_sliced(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(409, json={"error": "no_slice"}))
    assert await srv.describe_plate() == {"error": "not_sliced"}


@respx.mock
async def test_describe_plate_on_cube_and_cache_hit(monkeypatch):
    _env(monkeypatch)
    gcode = respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(200, content=CUBE))
    respx.get(url__regex=rf"{B}/api/v1/objects.*").mock(return_value=httpx.Response(200, json=OBJECTS))
    out = await srv.describe_plate()
    assert out["per_object"] is True and out["not_in_gcode"] == []
    (o,) = out["objects"]
    assert o["name"] == "cube20.stl" and o["copies"] == 1 and o["orientation"]["class"] == "flat"
    assert o["seam"]["dominant"] == "+Y" and o["support"]["present"] is False
    assert out["summary"].startswith("cube20.stl lies flat")
    assert isinstance(out["parse_seconds"], float) and out["cached"] is False
    again = await srv.describe_plate()
    assert again["cached"] is True and again["objects"] == out["objects"]
    assert gcode.call_count == 2          # the G-code is still fetched (that is how we know the slice is unchanged)


@respx.mock
async def test_describe_plate_objects_endpoint_missing_still_describes(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(200, content=CUBE))
    respx.get(url__regex=rf"{B}/api/v1/objects.*").mock(return_value=httpx.Response(404, json={"error": "not found"}))
    out = await srv.describe_plate()
    assert out["objects"][0]["copies"] == 1 and out["not_in_gcode"] == []


def test_describe_plate_is_annotated_read_only():
    tool = srv.mcp._tool_manager._tools["describe_plate"]
    assert tool.annotations.read_only_hint is True and tool.annotations.destructive_hint is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server_describe_plate.py tests/test_tool_annotations.py -q`
Expected: FAIL with `AttributeError: module 'orcaslicer_mcp.server' has no attribute 'describe_plate'`.

- [ ] **Step 3: Implement the tool**

In `src/orcaslicer_mcp/server.py`, add to the imports block (after `from . import outcomes as _outcomes`):
```python
import hashlib
import time
from . import plate_describe as _plate
```

After the `get_gcode` tool (about line 940), add:
```python
_DESCRIBE_CACHE: dict[str, dict] = {}   # single entry in practice: the current slice


def _gcode_cache_key(data: bytes) -> str:
    h = hashlib.sha1()
    h.update(data[:65536])
    h.update(data[-65536:])
    return f"{len(data)}:{h.hexdigest()}"


@mcp.tool()
async def describe_plate() -> dict:
    """Machine-readable plate facts from the last slice's G-code, per object, so you can answer
    orientation and placement questions instead of guessing from Euler angles or a picture:
    how the part stands (flat / tilted / on an edge or corner, from first-layer contact versus its
    widest layer), the first-layer footprint as islands, where overhang extrusions concentrate by
    10 mm height band, where support stands and where its interface touches the part, and which
    side the outer-wall seams sit on (checked against seam_position). Each object gets a
    server-written summary sentence; relay it rather than recomputing. Read-only. Needs a valid
    slice; returns {"error": "not_sliced"} otherwise. Copies of one object are aggregated (Orca
    labels every copy 0); footprint islands still show per-copy contact. Cached per slice."""
    try:
        async with _client() as c:
            data = await c.get_gcode()
            try:
                objs = (await c.get_objects()).get("objects", [])
            except ApiError:
                objs = []
    except Conflict:
        return {"error": "not_sliced"}
    except ApiError as e:
        return _m4a_err(e)
    key = _gcode_cache_key(data)
    hit = _DESCRIBE_CACHE.get(key)
    if hit is not None:
        return {**hit, "cached": True}
    t0 = time.perf_counter()
    parsed = _plate.parse_gcode(data.decode("utf-8", errors="replace"))
    out = _plate.describe(parsed, objs)
    out["parse_seconds"] = round(time.perf_counter() - t0, 2)
    _DESCRIBE_CACHE.clear()
    _DESCRIBE_CACHE[key] = out
    return {**out, "cached": False}
```
Check `c.get_objects()` in `client.py`: if it already returns the bare list, drop the `.get("objects", [])`; the `list_objects` tool shows the right unwrapping. Then add to `_TOOL_ANNOTATIONS`, next to `"get_gcode"`:
```python
    "describe_plate": ("Describe plate placement from G-code", True, False),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q`
Expected: everything passes (about 257 tests). `test_annotation_table_matches_registered_tools_exactly` fails if the table row is missing.

- [ ] **Step 5: Commit**

```bash
git add src/orcaslicer_mcp/server.py tests/test_server_describe_plate.py
git commit -m "feat: describe_plate tool (orientation, footprint, overhang, support, seam from the last slice)"
```

---

### Task 6: Prompt line, docs, manifest, print-loop skill

**Files:**
- Modify: `src/orcaslicer_mcp/server.py` (prompt `slice-a-model`, step 6, about line 687)
- Modify: `README.md` (section `### Plate renders`, about line 71)
- Modify: `CHANGELOG.md` (`## [Unreleased]`)
- Modify: `lhm.plugin.json` (tools array)
- Modify: `~/projects/3d-printer/.claude/skills/print-loop/SKILL.md` (step 2)
- Modify: `tests/test_server_prompts_resources.py`

**Interfaces:**
- Consumes: the tool name `describe_plate` from Task 5.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_server_prompts_resources.py`:
```python
def test_slice_prompt_calls_describe_plate_after_slicing():
    msgs = anyio.run(lambda: srv.mcp.get_prompt("slice-a-model", {"model_path": "/tmp/cube.stl"}))
    text = msgs.messages[0].content.text
    assert "describe_plate" in text
    assert text.index("slice_and_wait") < text.index("describe_plate")
```
(Check the top of that file for how existing tests read the prompt text and mirror it exactly.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server_prompts_resources.py -q`
Expected: FAIL on the `describe_plate` assertion.

- [ ] **Step 3: Edit the prompt**

In `prompt_slice_a_model`, replace step 6 with:
```python
        "6. slice_and_wait, then get_slice_warnings and get_slice_breakdown; report "
        "print time, filament mass, and any warnings. Call describe_plate and relay its summary "
        "before proposing any orientation or support change; use render_plate to show the result.\n"
```

- [ ] **Step 4: README**

Under `### Plate renders` (after the `render_plate` paragraph), add:
```markdown
`describe_plate` answers the same questions as numbers and one sentence per object, computed from the sliced G-code: how the part stands (flat, tilted, or on an edge or corner, from first-layer contact against its widest layer), the first-layer footprint as islands, where overhang extrusions concentrate by height band, where support stands and where it touches the part, and which side the seams sit on, checked against `seam_position`. It exists because an assistant reads a sentence more reliably than a picture. Copies of an object are aggregated; the islands still show each copy's contact patch.
```
Confirm no em-dash was introduced: `grep -c "—" README.md` must not increase.

- [ ] **Step 5: CHANGELOG**

Under `## [Unreleased]`, add an `### Added` section above the existing `### Changed`:
```markdown
### Added
- `describe_plate`: per-object plate facts from the last slice's G-code (orientation class, first-layer footprint islands, overhang bands, support placement and contact zones, seam side versus `seam_position`) with a server-written summary sentence. Answers "how is it standing, where did support go, where is the seam" without a picture.
```

- [ ] **Step 6: lhm.plugin.json**

Insert into the `tools` array right after the `get_gcode` entry (keep the array order matching `list_tools`):
```json
    {
      "name": "describe_plate",
      "description": "Machine-readable plate facts from the last slice's G-code, per object, so you can answer"
    },
```
Verify: `python3 -c "import json;d=json.load(open('lhm.plugin.json'));print(len(d['tools']))"` prints 44.

- [ ] **Step 7: print-loop skill (other repo)**

In `~/projects/3d-printer/.claude/skills/print-loop/SKILL.md`, step `## 2. Slice`, append a sentence:
```
Then `describe_plate` and relay its summary (orientation, footprint islands, overhang bands, support zones, seam side) before touching orientation or support.
```

- [ ] **Step 8: Run everything and commit both repos**

Run: `uv run pytest -q` (all green)
```bash
git add src/orcaslicer_mcp/server.py README.md CHANGELOG.md lhm.plugin.json tests/test_server_prompts_resources.py
git commit -m "doc: describe_plate in the slice prompt, README, changelog, LobeHub manifest"
cd ~/projects/3d-printer && git add .claude/skills/print-loop/SKILL.md && git commit -m "doc: print-loop calls describe_plate after slicing"
```

---

### Task 7: Live verification against the fork (manual, no code)

**Files:** none. Requires the fork API up on max-pc (port 13130); the plate currently holds Body4 x3 with support overrides, which is the perfect live target.

- [ ] **Step 1:** From a fresh MCP session (or a stdio client script), call `describe_plate` against the live fork. Expected: `per_object: true`, one object `Body4.stl` with `copies: 3`, class `edge_or_corner`, three footprint islands, support present, seams dominant `+Y` matching `seam_position=back`, `parse_seconds` under 3.
- [ ] **Step 2:** Call `render_plate(view="preview", angle="top", frame="object")` and check by looking that the three islands' bboxes sit where the three parts are and that support islands sit under the green towers.
- [ ] **Step 3:** Record the result (numbers and whether the picture agreed) in `CLAUDE.local.md` in `~/projects/3d-printer`. Any disagreement is a bug: open the fixture in the tests and fix before the release.
- [ ] **Step 4:** Leave the PC as the session found it: cube20 alone on the plate, presets reselected so `modified` is empty (`select_preset` for the print preset resets the support overrides; `delete_object` Body4; `load_model` cube20 from wherever `OrcaRelaunchClean` loads it, see the task definition on max-pc).
