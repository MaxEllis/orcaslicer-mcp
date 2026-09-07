# describe_plate: machine-readable plate facts for the model

Date: 2026-09-08. Status: design approved in conversation (grill-me session), spec pending Max's review.
Supersedes the same-day "MCP App plate viewer" draft, which was withdrawn after the grill: an
interactive viewer helps the human, and the human already has the OrcaSlicer GUI. The party that
cannot see the plate is the model.

## Problem

After a slice the model is asked, and repeatedly gets wrong, four questions it can only answer today
by staring at one `render_plate` PNG or by inferring from Euler angles and role lists:

1. **Orientation.** Which way is the part standing (flat, tilted, on an edge or corner)? Misread twice
   on the tube connector before `render_plate` existed; a PNG still needs the model to read a picture.
2. **Plate contact.** How big is the first-layer footprint and how does it compare with the part's
   widest cross-section? `on_plate` is a boolean; footprint area was never available.
3. **Where support went.** Inferred from "a support role exists in the breakdown". That says support
   exists, not that it is under the barbs rather than under the arm.
4. **Seam and overhang location.** Never answered. The wavy-wall speckle and the Body6 barb scarring
   were about where the seam and the overhang extrusions landed.

All four are toolpath questions once a slice exists, and Orca's G-code carries the data: `;TYPE:` roles
for every extrusion block (`Outer wall`, `Overhang wall`, `Support`, `Support interface`, ...),
`;LAYER_CHANGE` with `;Z:` and `;HEIGHT:`, `;WIPE_START` at the point each loop closed (the seam),
`; printing object <name> id:N copy M` start/stop markers, and a trailing config block with
`printable_area`, `seam_position`, `layer_height`, `support_type`.

## Decision summary (from the grill)

| Question | Decision |
|---|---|
| Whose problem | The model's. No viewer. |
| Which facts | Exactly the four above. |
| Where computed | In orcaslicer-mcp, Python, from `get_gcode` + `/objects` + config keys. No fork change. |
| Output | Both: a server-written `summary` sentence per object with fixed thresholds, plus the numbers. |
| When | Standalone tool `describe_plate`, needs a completed slice; parsed result cached in-process. |
| Copies | One entry per object; copies aggregated, footprint reported as islands so per-copy contact is still visible. |
| Tests | Real fixtures (`.gcode.xz`) plus synthetic G-code for edge cases. |

## Non-goals

- No rendering, no UI, no MCP Apps.
- No mesh analysis (there is no mesh endpoint): "overhang" means Orca's `Overhang wall` extrusions,
  never a true face angle; the summary text says "overhang extrusions".
- No per-copy attribution. Verified on the fixture: Orca emits `copy 0` for every instance made with
  duplicate_object, so copies are indistinguishable in the G-code. Islands (below) cover the need.
- Not folded into `slice_and_wait`. If the model keeps forgetting to call it, a `describe=true`
  flag on `slice_and_wait` is a one-line follow-up.

## Architecture

Same split as `compare_slices` and `get_slice_breakdown`: a pure module plus a thin orchestrator.

- `src/orcaslicer_mcp/plate_describe.py` (pure, stdlib only):
  - `parse_gcode(text) -> ParsedPlate`: single pass over lines, tracking layer index, Z, height,
    current role, current object marker, and the last XY. Emits extrusion segments (E > 0 moves)
    tagged `(layer, z, role, object_key)`, wipe-start points, and the config block as a dict.
    Per-object grouping uses the `; printing object <name> id:N` markers; when a file has none
    (label objects off), everything is one object keyed `"plate"` and the result carries
    `per_object: false`.
  - `describe(parsed, objects_meta) -> dict`: the analysis (below).
  - `summarize(desc) -> str`: the sentence, from fixed thresholds.
- `server.py`: `describe_plate()` tool. Fetches `get_gcode` (error `not_sliced` when there is no
  valid slice), `/objects` (names, `instances`, bbox), config keys. Caches the parsed result keyed on
  `(len(gcode), sha1(first 64 KB + last 64 KB))` so a second question about the same slice is free.
  Cache is a single-entry dict (the plate has one current slice).

## Analysis, per object

Everything is computed on a 1 mm XY occupancy grid per layer, rasterising extrusion segments of the
object's own roles (walls, infill, surfaces; NOT skirt, brim, support). Grid cells, not polygons: it is
robust to Orca splitting a wall loop across travel moves and needs no geometry library.

1. **Footprint (layer 0)**: occupied cells of the first layer -> `footprint_area_mm2`, `footprint_bbox`,
   and `islands`: connected components (8-neighbour) with area and bbox each. Three copies give three
   islands; a corner-standing copy gives a small island.
2. **Contact ratio**: `footprint_area_mm2 / max_layer_area_mm2`, where both terms are FILLED areas,
   not raw rasterised-cell counts: for each 8-connected island of occupied cells, every row is filled
   between that row's min and max occupied x, so a sparsely infilled layer (only wall and infill-line
   centrelines rasterised) is not undercounted against a solid one. `footprint_area_mm2` is the filled
   area of layer 0; `max_layer_area_mm2` is the largest filled area over the object's layers. The
   ratio is clamped to at most 1.0 (a solid first layer against a hollow-looking mid-body layer can
   otherwise read as more than 100% contact). Classes: `< 0.15` "standing on an edge or corner",
   `0.15..0.60` "tilted", `> 0.60` "flat". Copy-agnostic because both terms sum over copies.
3. **Overhang bands**: per 10 mm Z band, `overhang_wall_mm / total_wall_mm` (Overhang wall over
   Overhang + Outer + Inner wall length). Bands over 0.10 are named in the summary
   ("overhang extrusions concentrate at Z 0 to 20 mm").
4. **Support**: `Support` + `Support interface` extrusions rasterised the same way -> support
   `islands` on layer 0 (where the towers stand), `z_range`, and `interface_zones`: XY bbox and Z of
   interface extrusions, i.e. where support actually touches the part. Attribution of support to an
   object is by the object marker; Orca prints an object's support inside its own marker block.
5. **Seam**: for each `;WIPE_START` inside an Outer wall block, the point's angle from the centroid of
   that layer's footprint island it falls in (nearest island). Report the share per side
   (`+X`, `-X`, `+Y`, `-Y`, using 90-degree sectors) and `alignment`: the fraction in the dominant
   side. `> 0.7` "aligned on the +Y side", otherwise "scattered" (random seam). Cross-check against
   `seam_position` from the config block and say when they disagree.

Per-object output shape:

```json
{"name": "Body4.stl", "copies": 3, "summary": "...",
 "orientation": {"class": "edge_or_corner", "contact_ratio": 0.08},
 "footprint": {"area_mm2": 142, "bbox": [..], "islands": [{"area_mm2": 47, "bbox": [..]}, ...]},
 "overhang": {"bands": [{"z0": 0, "z1": 10, "share": 0.31}, ...], "total_mm": 1534},
 "support": {"present": true, "z_range": [0.4, 56.8], "islands": [...], "interface_zones": [...]},
 "seam": {"sides": {"+Y": 0.82, "-Y": 0.05, "+X": 0.07, "-X": 0.06}, "alignment": 0.82,
          "configured": "back", "agrees": true}}
```

Top level: `per_object`, `objects: [...]`, `plate: {printable_area, layer_count, height_mm}`, and
`summary` (one line per object joined). Numbers rounded: areas to whole mm2, ratios to 2 dp, Z to 0.1.

## Summary sentence

Written server-side so the wording is stable and the model relays instead of computing:

> Body4.stl (3 copies) stands on an edge or corner: first-layer contact is 8% of its widest layer,
> in 3 islands of about 47 mm2 each. Overhang extrusions concentrate at Z 0 to 20 mm. Tree support
> is present from Z 0.4 to 56.8 mm, touching the part in 6 zones (listed). Seams align on the +Y side
> (82%), matching seam_position=back.

## Errors and degradation

- No valid slice: `{"error": "not_sliced"}` (same as `get_gcode`).
- No object markers: single entry, `per_object: false`, summary says "label objects is off, so this
  describes the whole plate".
- Objects on other plates or unsliced objects: not in the G-code, not described; `objects_meta` names
  that are absent from the G-code are listed under `not_in_gcode`.
- G-code over 60 MB: still parsed, but the tool reports `parse_seconds` so slowness is visible.
  Measured 0.18 s per MB of G-code with about ten times the file size resident in memory (4.1 MB ->
  0.7 to 1.0 s, ~42 MB); the parse runs on a worker thread so the server loop stays responsive.

## Deviations accepted in review

- Support towers come from the lowest support layer rather than literally layer 0.
- `overhang_bands` also returns `overhang_mm` per band, not only `share`.
- `plate` carries `layer_height`.
- The summariser is `summarize_object`.
- Interface zones are XY islands of all interface cells (union) with Z span from contributing layers.

## Testing

- Real fixtures under `tests/fixtures/`, stored as `.gcode.xz` (raw `.gcode` is gitignored, rule 4):
  - `cube20_flat.gcode.xz` (27 KB): one object, flat, no support, no overhang, seam `back`.
    Expect class `flat`, contact ratio about 1.0, one island about 400 mm2, no support, seam `+Y`.
  - `body4_corner_x3_support.gcode.xz` (827 KB): three copies of the Body4 connector rotated Z -45 then
    Y -54.7 (the corner stance), tree_slim support, 0.6 mm layers, sliced on the fork 2026-09-08
    (2h48m, 127 g, roles include Overhang wall, Support, Support interface). Expect class
    `edge_or_corner`, three footprint islands, overhang share highest in the 0 to 20 mm band, support
    present with interface zones, `copies: 3`, `per_object: true`, and `; printing object` markers
    all `copy 0` (the per-copy limitation, asserted so a future Orca that numbers copies is noticed).
- Synthetic G-code strings written in the tests: no object markers (`per_object: false`), a file with
  two differently named objects, a layer with no extrusion, wipe points outside any island, a config
  block with `seam_position = random` (expect `scattered`).
- Unit tests on the pure functions: rasteriser (a 10x10 square gives 100 cells), island splitting,
  band shares, seam sector assignment, threshold wording, cache key stability.
- Server test with respx: `describe_plate` on `not_sliced`, on the cube fixture, cache hit on the
  second call (the gcode route is called once).

## Rollout

- New tool `describe_plate` (read-only annotation), README section, CHANGELOG entry, tool count
  43 -> 44 in `lhm.plugin.json` at the next publish.
- Prompt `slice-a-model` and the 3d-printer `print-loop` skill gain one line: after slicing, call
  `describe_plate` before proposing orientation or support changes.
- Ships in orcaslicer-mcp 0.1.11 together with the mcp 2.x move.

## Found while building the fixture

- `load_model` on a path that does not exist returned "not available on this OrcaSlicer build (needs
  M4a)". The fork answers a missing file with a route-level-looking 404, so `_m4_err` misclassifies it.
  Backlog item F18: distinguish missing file from missing route (fork side, or check the body text).
- The Neutron `Body6.stl` is gone from `G:\orca-dev\tmp\Neutron`; `Body4.stl` is in Max's Downloads.
