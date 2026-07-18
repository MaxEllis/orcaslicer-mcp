---
topics: [large-nozzle, 0.8mm, flow, throughput, walls]
orca_keys: [nozzle_diameter, layer_height, line_width, nozzle_temperature, filament_max_volumetric_speed, retraction_length, wipe, wall_loops]
---

# Large nozzle (0.8mm+)

## What actually determines it

A larger `nozzle_diameter` doesn't just print faster — it changes the
flow, temperature, retraction, and wall-count math all at once, and
each of those has to be re-derived rather than scaled by feel from a
0.4 mm profile.

Flow demand scales with the cross-sectional area of the bead
(`layer_height` × `line_width`, corrected for the rounded-end geometry —
see `physics/flow-limits.md` for the exact area formula), and doubling
the nozzle diameter roughly doubles the sane line width and layer
height together, which multiplies demanded flow at any given speed by
roughly four rather than two. Run the actual `speed · area` arithmetic
per `physics/flow-limits.md` for the specific line width and layer
height in use — a 0.8 mm profile carried over at 0.4 mm-era speeds will
demand far more melt throughput than the hotend may be rated for at the
same temperature, which is exactly the failure this section exists to
catch.

Because demanded flow rises so sharply, `nozzle_temperature` must rise
with it: the sustainable-flow-vs-temperature relationship in
`physics/couplings.md` is the mechanism, and a 0.8 mm profile that keeps
a 0.4 mm-era temperature will under-extrude even though the temperature
"looks normal" for the material on paper. This was the real failure in
the 2026-07-18 Sidewinder X2 session: PLA at 205°C with a 0.8 mm nozzle
at 0.4 mm layers demanded roughly 13.8 mm³/s against a sustainable
ceiling of only ~8.3 mm³/s at that temperature — under-extruding the
walls — and raising `nozzle_temperature` to 215°C raised the ceiling to
~16.7 mm³/s, comfortably clearing demand and fixing the print. The
lesson generalizes: any nozzle-size change should trigger a fresh
flow-vs-temperature check, not an assumption that the old temperature
still applies.

A wider nozzle also holds a larger melt reservoir at a wider bore, so it
oozes more readily between moves at the same pressure and temperature
(see `physics/retraction.md`). This pushes `retraction_length` toward
the upper half of its drive-type range (upper end of ~0.2-2.0 mm on
direct-drive) rather than the low end used with a 0.4 mm nozzle, and it
makes `wipe` important rather than optional — without it, the larger
ooze bead a wide nozzle parks on its tip gets carried straight into the
next travel move's landing point.

Layer height for a large nozzle should stay within the same physical
ratio to `nozzle_diameter` as for any nozzle — roughly 0.25-0.65× the
bore — but because the bore itself is larger, the absolute layer height
range shifts up: a 0.8 mm nozzle's sane range (0.2-0.52 mm) sits well
above what a 0.4 mm nozzle ever runs, and going to the top of that range
is where most of a large nozzle's throughput advantage actually comes
from.

Wider lines also change how many walls are needed to reach a given
shell thickness: because each `wall_loops` pass is now roughly twice as
wide, the same total shell thickness needs roughly half as many wall
loops as a 0.4 mm profile — carrying over a wall-loop count tuned for
0.4 mm onto a 0.8 mm profile produces a shell twice as thick (and twice
as slow) as intended, so `wall_loops` should be recalculated from target
shell thickness ÷ new line width, not copied from the old profile.

## Context -> consideration mappings

- IF the user's situation says they're switching a profile from 0.4mm to a 0.8mm (or larger) `nozzle_diameter` THEN weigh re-deriving `nozzle_temperature` from the sustainable-flow relationship (`physics/couplings.md`) rather than reusing the old temperature, because demanded flow rises faster than nozzle diameter and a carried-over temperature routinely under-extrudes.
- IF the user's situation reports under-extrusion, gaps, or weak walls specifically after moving to a larger nozzle THEN weigh checking `speed · area` against `filament_max_volumetric_speed` at the current `nozzle_temperature` (`physics/flow-limits.md`) first, because this is the most common large-nozzle failure and mirrors the real 2026-07-18 case.
- IF the user's situation involves a large nozzle and reports stringing or ooze between moves THEN weigh `retraction_length` toward the upper half of the drive-type range and confirm `wipe` is on, because a wider bore oozes more at the same pressure and temperature.
- IF the user's situation asks how many `wall_loops` a large-nozzle profile needs for the same shell thickness as a known 0.4mm profile THEN weigh recalculating from target thickness ÷ new line width rather than copying the old wall count, because wider lines mean fewer loops reach the same thickness.
- IF the user's situation wants maximum throughput from a large nozzle THEN weigh `layer_height` toward the top of the 0.25-0.65× `nozzle_diameter` range, because that's where most of a large nozzle's time advantage over a 0.4mm setup actually comes from.
