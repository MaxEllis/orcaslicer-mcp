---
topics: [dimensional, accuracy, tolerance, fit, compensation]
orca_keys: [elefant_foot_compensation, xy_contour_compensation, xy_hole_compensation, wall_loops, outer_wall_speed, line_width]
---

# Dimensional accuracy

## What actually determines it

Dimensional error comes from a small number of independent sources
that each need their own fix — treating all size error as one problem
and reaching for a single "make it more accurate" knob tends to
overcorrect one axis while leaving another untouched.

The base of a print is systematically oversized on early layers because
the first layers get squashed slightly wider under bed-adhesion pressure
and residual heat, a defect commonly called elephant's foot (see
`failures/elephant-foot.md` for the failure-mode detail). This is a
first-layers-only, radially-outward error at the base, and
`elefant_foot_compensation` corrects it by insetting the first few
layers inward by a small compensation amount — it should not be reached
for to fix general over-sizing away from the base, since it only acts
on the bottom of the part.

General XY over- or under-sizing across the whole part (not just the
base) is a separate, uniform error usually caused by nozzle geometry,
flow calibration, or the specific slicer/printer combination
consistently drawing walls slightly fat or thin. If the schema exposes
`xy_contour_compensation` (outer-boundary offset) and
`xy_hole_compensation` (inner-boundary/hole offset) as separate keys,
they let outer dimensions and hole/inner dimensions be corrected
independently — important because a nozzle that draws outer walls
slightly fat often draws holes slightly small at the same time, and a
single uniform XY offset cannot correct both directions at once.

Single-perimeter accuracy is a speed trade-off with a physical cause,
not a magic setting: at high `outer_wall_speed`, the extruder plans
through corners with more momentum, which rounds sharp corners and
distorts hole diameters, and the resulting shape error is roughly
proportional to how much speed is being asked of the wall. Slowing down
`wall_loops`' outer pass or narrowing acceptable `line_width` variation
buys back dimensional fidelity at the direct cost of print time; there's
no way to have both without physically limiting how fast the toolpath
changes direction near a tight feature.

Material shrinkage varies materially by polymer: amorphous plastics
like PETG and ASA typically shrink less and more predictably during
cooling than semi-crystalline ones, and any given material's shrinkage
behavior should be treated as a per-material property to calibrate
against, not something a universal compensation setting corrects for
across materials — a compensation value tuned for one filament will be
wrong, in either direction, for a different one.

Because all of the above compound (base squash, uniform XY offset,
corner rounding, and material shrink all stack), the only reliable way
to hit a tight tolerance on a specific printer/material/geometry
combination is empirical: print a small test coupon with the target
hole/peg/slot dimensions before committing to the full part, measure
it, and adjust compensation values from the measured error rather than
from a formula — especially for press-fit or precision-mating features
where being off by even a few tenths of a millimeter means the part
doesn't assemble.

## Context -> consideration mappings

- IF the user's situation says the base of the part is oversized or doesn't sit flush/flat while the rest of the part measures fine THEN weigh `elefant_foot_compensation` up, because that error is specific to the squashed first layers, not a general size problem.
- IF the user's situation says the whole part (not just the base) measures uniformly oversized or undersized THEN weigh `xy_contour_compensation`/`xy_hole_compensation` (verify these exist in the schema before quoting exact keys) rather than `elefant_foot_compensation`, because the error spans the full height and needs a uniform, not base-only, offset.
- IF the user's situation says holes are undersized relative to the walls being oversized (or vice versa) THEN weigh `xy_contour_compensation` and `xy_hole_compensation` independently in opposite directions if the schema exposes them separately, because outer and inner boundary error commonly runs in opposite directions on the same print.
- IF the user's situation says the part has tight-tolerance features (press-fit pins, mating holes, snap joints) THEN weigh printing a small tolerance test coupon first over trusting a compensation formula, because compounded errors (base squash + XY offset + corner rounding + material shrink) are only reliably resolved empirically per printer/material/geometry.
- IF the user's situation says round holes or sharp corners are coming out visibly rounded or undersized on a fast profile THEN weigh `outer_wall_speed` down for that feature, because corner rounding at speed is a momentum effect, not a compensation-value problem.
- IF the user's situation names a specific material (PETG, ASA, PLA, nylon, etc.) and precision matters THEN weigh per-material shrinkage behavior as a starting calibration point (`physics/couplings.md` covers material-specific behavior generally) rather than reusing compensation values tuned on a different filament.
