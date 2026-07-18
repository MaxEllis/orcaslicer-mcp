---
topics: [flow, volumetric, speed, extrusion, clog]
orca_keys: [filament_max_volumetric_speed, outer_wall_speed, inner_wall_speed, sparse_infill_speed, internal_solid_infill_speed, top_surface_speed, initial_layer_speed, gap_infill_speed, bridge_speed, line_width, layer_height]
---

# Volumetric flow limits

## Cross-section of a deposited line

Orca models an extruded line as a rounded rectangle: a flat-sided bead whose
two ends are rounded by the nozzle's circular profile. The cross-sectional
area of that bead is:

```
area = (line_width − layer_height · (1 − π/4)) · layer_height      [mm²]
```

The `(1 − π/4)` term (≈ 0.215) subtracts the corner area that the rounded
ends remove from the naive `line_width × layer_height` rectangle. As
`layer_height` grows relative to `line_width`, this correction becomes a
larger fraction of the total area, and the bead's effective cross-section
shrinks relative to the naive rectangle.

## Demanded flow

Volumetric flow is the rate at which the nozzle must push melted filament
to keep up with the toolpath:

```
flow = speed · area      [mm³/s]
```

`speed` is the feature's print speed (`outer_wall_speed`, `inner_wall_speed`,
`sparse_infill_speed`, `internal_solid_infill_speed`, `top_surface_speed`,
`initial_layer_speed`, `gap_infill_speed`, `bridge_speed`, etc.) and `area`
uses that same feature's line width — `outer_wall_line_width` for outer
walls, and so on — falling back to the generic `line_width` when a
feature-specific width isn't set.

## The governing rule

Every feature's demanded flow must stay at or below the filament's rated
ceiling:

```
flow ≤ filament_max_volumetric_speed
```

`filament_max_volumetric_speed` is the melt zone's throughput limit: the
maximum rate at which the hotend can raise incoming filament to melt
temperature and push it out the nozzle before the polymer is under-heated
mid-stream. Exceeding it doesn't jam the printer outright — Orca clamps
speed at print time to keep flow at the ceiling — but a profile that relies
on that clamp is dishonest about its own settings: the walls it draws will
run slower than the speed field claims, and if clamping interacts badly
with acceleration/jerk planning the surface can show ridging or
under-extrusion artifacts. Profiles should be authored so that
`speed · area` already sits under the ceiling for every feature, with
clamping treated as a safety net rather than a normal operating mode.

## Honest ceilings

Manufacturer spec-sheet maximums are marketing numbers measured under
best-case conditions (short retraction-free test paths, ideal cooling,
sometimes non-standard hotends). Real print-quality ceilings run lower:

- Standard brass-nozzle hotends (V6-style, ~20 mm melt zone) with PLA:
  roughly **11–15 mm³/s** sustained.
- Volcano-style hotends (longer melt zone, more heater contact) with PLA:
  roughly **16–24 mm³/s** sustained.

Treat these as starting ceilings to derate from, not targets to chase —
pushing right up to a spec-sheet number on a real profile routinely trades
away wall consistency and top-surface finish for a small time savings.
