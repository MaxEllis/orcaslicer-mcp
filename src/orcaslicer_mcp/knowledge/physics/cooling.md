---
topics: [cooling, fan, layer-time, quality]
orca_keys: [fan_max_speed, fan_min_speed, fan_cooling_layer_time, slow_down_layer_time, slow_down_min_speed, overhang_fan_speed]
---

# Part cooling

## Heat load scales with deposited volume

Each new layer arrives as hot polymer sitting on top of the previous one.
The heat that layer carries is proportional to the volume of plastic just
deposited: thicker layers, wider lines, and larger solid areas all deposit
more mass per unit time and therefore carry more thermal energy that has
to be removed before the next layer lands. A thin layer over a small
perimeter cools almost immediately; a thick layer over a large solid
infill area holds heat far longer and stays soft underneath the next pass.
This is why layer height and feature size — not just print speed — drive
how aggressive cooling needs to be.

## Layer time and the small-parts problem

If a layer finishes printing faster than the plastic underneath it can
solidify, the nozzle deposits new material onto a surface that is still
molten. This causes stacking/sagging on small or thin features (the
classic "leaning tower" or blobby small-print symptom) because each new
layer partially remelts and deforms the one below instead of bonding onto
a solid surface.

`fan_cooling_layer_time` and `slow_down_layer_time` exist to prevent this:
when a layer's estimated print time drops below the configured threshold,
the slicer slows the print (down to `slow_down_min_speed`) so that the
layer takes longer to print, giving the previous layer more real time to
cool. As a practical floor, small or thin-walled parts need
`slow_down_layer_time` of roughly **8 seconds or more** per layer (a
conservative choice within the 4–10 s range of common slicer defaults —
Cura ships 5–10 s per printer; PLA guides cite 3–5 s, ABS ~15 s) to avoid
molten stacking; layer times well under that reliably show softening and
dimensional creep on the smallest features of a print, regardless of fan
speed.

## Fan speed ordering is a correctness constraint, not a preference

`fan_min_speed` and `fan_max_speed` define the range Orca ramps the part
cooling fan across as layer time drops (slower layers → more cooling, up
to the max). If `fan_min_speed > fan_max_speed`, the range is inverted and
nonsensical — this is a configuration bug, not a stylistic choice, and
should be flagged rather than treated as an intentional low-cooling
profile.

`overhang_fan_speed` is a separate, feature-triggered override: bridges and
steep overhangs get pushed toward this fan speed regardless of the
layer-time ramp, because unsupported spans need immediate solidification
to hold their shape against gravity, independent of how long the rest of
the layer takes.

## Material dependence

Cooling needs are material-specific because they trade off against
interlayer bonding strength, which depends on how long adjacent layers
stay hot enough to fuse:

- **PLA** has a low glass-transition temperature and cools/solidifies
  quickly without losing much interlayer strength, so it tolerates —
  and generally wants — high fan speed for crisp overhangs, bridges,
  and fine detail.
- **ABS/ASA** are prone to warping and layer delamination when cooled too
  aggressively: rapid cooling shrinks the outer layers faster than the
  core, building internal stress, and it also shortens the window in
  which adjacent layers can properly fuse. These materials want low fan
  speed (often near zero) except on isolated overhangs/bridges where
  `overhang_fan_speed` still needs to kick in locally.
