# MCP App plate viewer (Three.js) — design draft, decision pending

Status: DRAFT written 2026-09-08 after the mcp 2.x migration unblocked it. Not approved, not built.
Max's call on the open questions at the end before any plan is written.

## What it is

An interactive 3D view of the plate, rendered inside the chat client, that a person can orbit,
zoom, and scrub layer by layer. Today `render_plate` returns a fixed PNG from a fixed camera; the
model relays it and the person cannot look around it. With the MCP Apps extension
(`io.modelcontextprotocol/ui`, shipped in mcp 2.x as `mcp.server.apps`) a tool can carry a
`ui://` HTML resource that the host renders in a sandboxed iframe, with a message channel back to
the server for follow-up tool calls.

## Why now

- The `mcp<2` pin is gone (2026-09-08). `mcp.server.apps.Apps` is in the SDK we ship on.
- Two of the last three real print sessions burned time inferring orientation and plate contact
  from Euler angles and role lists. `render_plate` fixed the worst of that with a still image; the
  remaining questions ("where exactly is the seam", "what does layer 40 look like", "is that
  support touching the wall") need a view the person can move.

## What data can drive it

The fork's Remote API exposes no mesh geometry. What it does give us:

| Source | Available today | Fidelity | Cost |
|---|---|---|---|
| G-code (`GET /api/v1/gcode`, tool `get_gcode`) | yes | exact toolpaths, per-role via Orca's `;TYPE:` comments, per-layer via `;LAYER_CHANGE` | server-side parse + downsample; a 6 h print is 100k+ segments, must be thinned |
| Object bounding boxes (`GET /objects`: bbox_min/max, on_plate, transform) | yes | boxes only | none |
| `plate/render` PNG | yes | photo-real toolpaths | already shipped; the fallback for hosts without Apps |
| Object meshes | NO endpoint | exact model | fork C++: new `GET /objects/{id}/mesh` (binary STL or flat float32 triangles), ~40 min rebuild + a fork release |
| Local model file | only when the MCP server runs on the same machine as OrcaSlicer (the public user's normal setup; NOT ours, the file is on max-pc) | exact | read the STL/STEP path from `load_model`, parse STL server-side |

Recommendation: v1 renders **toolpaths from G-code** plus **bounding boxes** for unsliced objects.
That answers orientation, plate contact, seam, support placement, and layer-by-layer questions with
zero fork changes. Meshes are v2, behind a fork endpoint, once v1 proves people use it.

## Shape of v1

- `Apps()` extension registered on the existing `MCPServer`; tool `view_plate` bound to
  `ui://orcaslicer/plate-viewer.html`. Tool returns a compact payload: layers as flat
  `Float32Array`-friendly lists of segments `[x0,y0,z0,x1,y1,z1,role]`, thinned to a budget
  (say 200k segments) by dropping collinear points and, past the budget, every nth infill segment
  first (walls and support are kept). Plus bed size from the printer preset and object bboxes.
- Graceful degradation (SEP-2133 requires it): `client_supports_apps(ctx)` false means return the
  same text summary `render_plate` gives today plus the PNG, so non-Apps hosts lose nothing.
- HTML: single file, Three.js from a CDN allowed via `ResourceCsp(resource_domains=[...])`,
  OrbitControls, one colour per Orca role (match `render_plate` colours so the two views agree),
  a layer slider, a "top / iso / front" button row, bed grid at Orca's plate size.
- Follow-up actions from the UI (optional, v1.5): buttons that call `get_slice_breakdown` for the
  hovered role, or re-run `view_plate` after the model changes something. Keep mutations OUT of
  the iframe; Rule 3 (confirm every mutation) is easier to hold when the UI only reads.
- Tests: parser on real G-code fixtures (cube20 from the outcome store's `gcode/` dir is 300 KB
  and has every role), thinning budget respected, role mapping complete, payload size ceiling,
  degradation path returns text+image, `list_tools` shows `_meta.ui.resourceUri`.

## Open questions for Max

1. Is a toolpath-only v1 worth it, or is the mesh view the real want (which means fork C++ and a
   release first)?
2. Host reality check: which client will you look at it in? Claude Desktop and claude.ai render
   MCP Apps; Claude Code in the terminal does not. If the daily driver is Claude Code, this is a
   Desktop-only feature and the PNG stays the workhorse.
3. Payload ceiling: 1 MB of JSON per call is a lot of context if a host echoes it to the model.
   Apps hosts keep `visibility: app` tool results out of the model context; confirm that before
   sizing the budget.
4. CDN for Three.js inside the iframe (needs the CSP allowance) versus vendoring a minified copy
   into the resource (~600 KB of HTML, no network). Vendoring is more robust; the file is big.
