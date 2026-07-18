---
topics: [overhang, sagging, curling, droop]
orca_keys: [overhang_fan_speed, nozzle_temperature, outer_wall_speed, support_type, enable_support]
---

# Overhangs (sagging, curling, drooping)

## Looks like

Unsupported or steeply angled surfaces sag, droop into stringy curls at
their leading edge, or lose sharp definition compared to the vertical
walls feeding into them. The defect is localized to the overhanging
region — walls directly below print cleanly — which is the tell that
distinguishes this from a general cooling or flow problem affecting the
whole part.

## Causes, most likely first

1. **Insufficient cooling on the overhang itself.** Overhanging plastic
   has no solid layer directly beneath it to conduct heat away, so it
   stays soft and sags under its own weight unless airflow cools it fast.
   Raise `overhang_fan_speed` — this is usually the single highest-leverage
   fix and can be pushed higher than general part cooling without hurting
   layer bonding, since it targets a small area printed briefly.
2. **Melt too hot.** A hotter-than-necessary melt takes longer to
   solidify once deposited, giving gravity more time to pull the
   unsupported bead down before it sets. Drop `nozzle_temperature` toward
   the low end of the material's window for overhang-heavy parts,
   trading a little bonding margin for stiffness on the overhang.
3. **Speed too high on the overhanging walls.** Faster deposition leaves
   less time between when a bead is laid and when the next layer (or the
   fan) can help it set, and outer-wall speed governs most overhang wall
   surfaces. Slow down `outer_wall_speed` for overhang-heavy geometry;
   check the schema for a printer/version that exposes graduated
   angle-based overhang speed keys before assuming only the flat wall
   speed is available — some Orca builds add per-angle overhang speed
   scaling, but treat that as an enhancement on top of the base wall
   speed rather than a required setting.
4. **Angle beyond what unsupported printing can achieve.** Past roughly
   55-60 degrees from vertical (material- and cooling-dependent), no
   amount of fan or speed tuning produces an acceptable surface — the
   bead simply has too little of the previous layer to key into. Enable
   `enable_support` and set `support_type` (normal or tree, depending on
   the geometry) rather than continuing to chase cooling/speed settings
   on geometry that's past the printable range.

## Verify

Print a graduated overhang test (a stepped or ramped test model covering
roughly 10-80 degrees from vertical) and inspect where the surface quality
transitions from acceptable to sagging: a fix should push that transition
angle further from vertical (steeper achievable overhang) without
introducing new artifacts like under-cooling stringing elsewhere on the
part.
