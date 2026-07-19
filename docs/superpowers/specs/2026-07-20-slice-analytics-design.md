# Slice Analytics — per-feature breakdown + stateless prediction-check

- **Date:** 2026-07-20
- **Status:** Design approved, pending spec review → implementation plan
- **Finding origin:** F14, from the first real end-to-end print job (2026-07-20). See
  `CLAUDE.local.md` backlog in the 3d-printer repo.

## Motivation

After a slice, the Remote API returns only five aggregate numbers (estimated time,
filament g/mm, cost) plus warnings. The rich per-feature information OrcaSlicer computes
and shows in its GUI right-side panel — time per line type, per-feature speed / flow /
line width / layer height / temperature, and the time-weighted distribution of each — is
**not exposed**. The only raw alternative is dumping the entire G-code (tens of MB),
which is unusable in an agent context.

During the 2026-07-20 session this forced ~15 trial slices to *infer* what that panel
would have shown directly: when a textured model jumped from 5h14m to 8h10m, there was no
way to read "outer walls +90 min, travel +40 min" — it had to be reconstructed by
turning levers off and re-slicing. This feature removes that blind spot.

## Goals

1. Expose a **compact per-feature breakdown** of the last successful slice (kilobytes, not
   the raw move stream), covering per-role time/filament, global time-weighted metric
   distributions, and per-layer aggregates.
2. Provide a **stateless predicted-vs-observed check**: compare the physics gate's
   per-feature flow predictions to what the slice actually did, and flag mismatches
   (notably silent speed-clamping at the flow ceiling).

## Non-goals (explicitly decided)

- **No persistence / no learning loop.** An earlier design included storing breakdowns
  keyed by printer/material and feeding `consult` to accumulate norms. This was **cut**
  after scoping: it only pays off for a single stable-printer power user (N=1 cold-start),
  does not transfer across users (siloed per machine), and the generalizable insight
  ("large parts are flow-bound", "textures are toolpath-expensive") belongs in the curated
  knowledge layer — improved offline using this analytics, shipped to everyone — not in a
  per-user runtime accumulator. Real cross-user learning would be central telemetry
  aggregation, a separate product decision with privacy implications, deliberately out of
  scope.
- **No raw per-move stream** over the API (the firehose we are avoiding).

## Architecture & data flow

```
slice completes
  → fork iterates GCodeProcessorResult.moves ONCE, accumulating role/metric/layer stats
  → builds a compact `breakdown` object
  → attaches it to the GET /api/v1/slice/status response (alongside existing stats)

MCP get_slice_breakdown()
  → reads status.breakdown
  → runs prediction_check() against the live merged config (physics-gate predictions)
  → returns { breakdown fields..., prediction_check[] }
```

The fork owns all heavy iteration (it already holds `moves`). The MCP only reshapes and
diffs. `get_slice_status` / `slice_and_wait` stay lean (the five numbers) so routine
polling remains cheap; the breakdown is a separate opt-in tool.

## The `breakdown` payload (fork computes)

Source data in the fork (confirmed present in `GCodeProcessorResult`,
`src/libslic3r/GCode/GCodeProcessor.hpp`): `used_filaments_per_role`
(`map<ExtrusionRole, pair<double,double>>`), `print_statistics` (per-role time), and the
`moves` vector of `MoveVertex` (each with `extrusion_role`, `feedrate`, `mm3_per_mm`,
`volumetric_rate()`, width, height, temperature, fan, layer).

```jsonc
"breakdown": {
  "mode": "normal",              // PrintEstimatedStatistics ETimeMode
  "total_time_s": 26854.9,
  "roles": [
    { "role": "outer_wall", "time_s": 5421.0, "time_pct": 20.2, "filament_g": 41.3,
      "speed_mm_s": {"min": 20, "max": 45, "mean": 43.1},
      "flow_mm3_s": {"min": 5.1, "max": 16.7, "mean": 15.9} },
    { "role": "inner_wall", "...": "..." },
    { "role": "sparse_infill", "...": "..." },
    { "role": "internal_solid_infill", "...": "..." },
    { "role": "top_surface", "...": "..." },
    { "role": "support", "...": "..." },
    { "role": "travel", "time_s": 2103.0, "time_pct": 7.8, "filament_g": 0.0,
      "speed_mm_s": {"min": 0, "max": 300, "mean": 210} }   // travel: no filament
  ],
  "metrics": {                    // global, time-weighted, ~10 fixed buckets each
    "speed":            {"unit":"mm/s",  "min":0,"max":300,"mean":..,"buckets":[{"lo":0,"hi":30,"time_s":..}, ...]},
    "volumetric_flow":  {"unit":"mm3/s", "min":..,"max":..,"mean":..,"ceiling":20.0,"buckets":[...]},
    "line_width":       {"unit":"mm",    "min":..,"max":..,"mean":..,"buckets":[...]},
    "layer_height":     {"unit":"mm",    "min":..,"max":..,"mean":..,"buckets":[...]},
    "temperature":      {"unit":"C",     "min":..,"max":..,"mean":..,"buckets":[...]},
    "fan":              {"unit":"%",     "min":..,"max":..,"mean":..,"buckets":[...]}
  },
  "layers": [                     // per layer, compact (hundreds of rows)
    { "z": 0.4, "time_s": 61.2, "filament_g": 1.1, "top_role": "internal_solid_infill" }
  ]
}
```

Design notes:
- **Per-role carries speed/flow ranges** (not just time/filament) because the
  prediction-check needs observed per-role flow.
- **Buckets are time-weighted** (each carries `time_s`) so "where time goes" is answerable,
  not just the value range. Fixed bucket count (10) per metric, edges linear over min..max.
- `volumetric_flow` includes the profile `ceiling` (`filament_max_volumetric_speed`) inline
  so the clamp check is self-contained.
- Roles map from `ExtrusionRole` to stable snake_case names matching the config vocabulary
  (`outer_wall`, `inner_wall`, `sparse_infill`, `internal_solid_infill`, `top_surface`,
  `support`, plus any others present).
- **`travel` is a pseudo-role derived from move *type* (`EMoveType::Travel`), not from
  `ExtrusionRole`** — travel moves carry `erNone`, so they must be bucketed by move type,
  not dropped. Genuine `erNone` *extrusion* moves (rare/none) are dropped. This is the one
  place where "role" spans both `ExtrusionRole` and move type; implementers must handle both.

## MCP changes (Python)

- `client.py`: `slice_status()` already returns the full status dict; `breakdown` rides
  along when present — no new endpoint. Add a typed accessor if convenient.
- `models.py`: shape a `SliceBreakdown` typed dict; `summarize_slice` is unchanged (keeps
  status lean).
- `server.py`: new tool `get_slice_breakdown()`:
  - returns `{ "available": true, "mode", "total_time_s", "roles", "metrics", "layers",
    "prediction_check" }`
  - if `status.breakdown` is absent (older fork build) → `{ "available": false, "reason":
    "fork build predates breakdown; rebuild or check capabilities" }` (mirrors the existing
    `get_slice_warnings` capability caveat).
- `physics_check.py`: expose the per-feature predicted flow it already computes (refactor so
  the predictions are callable without re-deriving) for the prediction-check to consume.

## Stateless prediction-check

`prediction_check(config, breakdown) -> list[CheckResult]`, computed in the MCP:

For each role present in both the gate's predictions and the breakdown:
- **`clamped`** — predicted flow (`speed × area` from the profile) exceeds observed
  `flow_mm3_s.max`, and observed max sits at/near the `ceiling` → Orca silently throttled;
  the profile's speed field is optimistic. This is the honest-numbers gap the knowledge base
  warns about, now *detected* instead of assumed.
- **`matches`** — predicted ≈ observed within tolerance.
- **`anomaly`** — observed materially exceeds predicted (model/derivation mismatch worth
  surfacing).

Tolerances: ±10% on flow, "near ceiling" = within 5% of `ceiling`. Output is advisory
(same `CheckResult` shape as the physics gate), never blocking. No storage: it compares
this slice to this config, in-session, on any printer.

## Capability gating & error handling

- Fork adds `"breakdown"` to the `capabilities` array already returned by `/status`.
- MCP checks the capability (or the presence of `status.breakdown`) and degrades to
  `available: false` gracefully — never errors on an old build.
- If no valid slice exists yet, the tool returns the same `available: false` with a
  "no valid slice" reason rather than throwing.

## Fork changes (C++) and delivery

- Extend the slice-status handler to build the `breakdown` object from the current
  `GCodeProcessorResult` at slice completion (single pass over `moves` for the metric
  buckets and per-role speed/flow ranges; per-role time/filament come from
  `print_statistics` / `used_filaments_per_role`).
- Guard cost: computed once per completed slice, cached with the slice result; not
  recomputed on each status poll.
- Built on max-pc via the OrcaRebuild flow and relayed (see
  `pc-build-verify-workflow` memory). Add `"breakdown"` to advertised capabilities in the
  same change.

## Testing

- **Fork:** unit-test bucket accumulation and per-role range computation on a synthetic
  `moves` vector with known values (deterministic bucket edges, time-weighting).
- **MCP:** 
  - shape/model tests against a canned `breakdown` payload — assert parsed semantic values,
    not byte-exact JSON (per `test-semantics-not-serialization`).
  - `prediction_check` against constructed clamped / matching / anomaly cases.
  - `available: false` degradation when `breakdown` absent and when no valid slice exists.

## Out of scope / future

- Central telemetry aggregation across users (real cross-user learning) — separate product,
  privacy implications.
- Raw per-move stream (paged/filtered) for deep dives — revisit only if a concrete need
  appears that distributions can't answer.
- Per-role × per-metric full histograms — v1 ships per-role ranges + global histograms;
  add per-role histograms only if the ranges prove insufficient.
