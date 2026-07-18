---
topics: [warping, corners, lifting, adhesion]
orca_keys: [hot_plate_temp, fan_min_speed, close_fan_the_first_x_layers, brim_type, brim_width, initial_layer_print_height]
---

# Warping

## Looks like

Corners or edges of a print curl upward off the bed as the print
progresses, sometimes popping free entirely; on large flat parts the whole
first layer can lift in the middle instead of just at corners. Warping is
a bed-adhesion failure driven by differential shrinkage, not a single-layer
squish problem — it distinguishes itself from ordinary first-layer
adhesion issues by showing up progressively, layers after the first ones
went down fine.

## Causes, most likely first

1. **Bed temperature too low for the material.** Warp-prone materials
   (ABS, ASA, PETG under some conditions) need the bed to stay near the
   material's glass transition temperature for the whole print so lower
   layers don't cool and shrink out of plane with the layers still being
   deposited above them. Check `hot_plate_temp` against the material's
   documented range and raise it toward the top of that range before
   trying anything else.
2. **Cooling too aggressive on the first layers.** Part-cooling fan
   airflow across freshly deposited plastic accelerates the same
   differential-shrinkage effect that causes warp, especially on the first
   few layers where the bed is doing most of the thermal work. Keep
   `fan_min_speed` low and use `close_fan_the_first_x_layers` to disable
   the fan for the first 2-3 layers on warp-prone materials.
3. **Draft or open enclosure.** Ambient air currents across the print bed
   pull heat out of the part unevenly, same failure mode as fan overshoot
   but from the environment instead of Orca settings — no Orca key
   controls this; enclose the printer or shield it from drafts.
4. **Insufficient bed anchoring.** A brim increases the bonded footprint
   at the corners most prone to lifting. Enable `brim_type` (outer or
   outer-and-inner) and widen `brim_width` — start around 5-10mm and
   increase if corners still lift.
5. **First-layer squish too light.** A first layer that isn't pressed
   firmly enough into the bed has less contact area to resist the
   shrink-driven peel force. Check `initial_layer_print_height` isn't set
   so high that the layer barely touches the bed.

## Verify

Print a part with several 90-degree corners (a simple flat-bottomed box or
the classic corner-warp test) at least 30 minutes long, and watch the
corners through the middle third of the print, not just at completion —
warping that starts small and grows over time confirms the diagnosis and
lets you see whether a change stops it from starting rather than just
slows it down.
