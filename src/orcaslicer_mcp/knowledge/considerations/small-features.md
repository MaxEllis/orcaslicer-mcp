---
topics: [small-features, miniatures, detail, tiny, thin-walls]
orca_keys: [layer_height, nozzle_diameter, slow_down_layer_time, detect_thin_wall, support_type]
---

# Small features and miniatures

## What actually determines it

Small features — miniature figures, tiny mechanisms, fine surface
detail — are limited by two things a normal-scale print rarely runs
into: vertical resolution and layer heat accumulation, and both need to
be pushed harder than default settings assume.

Vertical resolution is the layer-height ceiling on how fine a facial or
surface detail can be reproduced at all: on a standard 0.4 mm
`nozzle_diameter`, facial features and other fine miniature detail need
`layer_height` at or below roughly 0.12 mm — well under the
0.2 mm-ish default many profiles ship with — because each visible
detail (an eye, a fold, a small raised line) is often only a layer or
two of relief, and a layer height comparable to the feature's own height
simply can't resolve it. This is a hard resolution limit, not a
speed/quality trade-off in the usual sense: there's no way to recover
detail lost to too-tall layers by tuning speed or cooling afterward.

Small layers create a heat-accumulation problem that's the opposite of
what happens on large prints: because each layer covers so little area,
it prints in very little time, and the next layer lands on plastic that
hasn't had time to solidify. `slow_down_layer_time` needs to be set high
(well above default) specifically for small/miniature prints so that
the slicer artificially stretches out layer print time whenever a layer
would otherwise finish too fast — see `physics/cooling.md` for why
layer time below roughly 8 seconds causes stacking and softening, which
is nearly guaranteed on tiny cross-sections without this setting raised.

Thin-wall features (a miniature's sword, a thin fin, a wall narrower
than a full nozzle-width bead) need a narrower line width than the
model's nominal geometry would otherwise get, because Orca's default
wall generation can skip or badly approximate features thinner than one
full bead. If the schema exposes `detect_thin_wall`, enabling it lets
the slicer specifically identify and handle single-bead-width walls that
would otherwise be dropped or merged incorrectly with neighboring
geometry — this is a detection/handling setting, distinct from just
lowering line width globally, which would also thin every normal wall.

Supports on small, organic, or highly detailed miniatures have a
different shape requirement than supports on a mechanical part: dense
tree-style supports (via `support_type`, tree-family) conform to organic
surfaces at oblique angles far better than grid supports do, follow the
model's contours instead of imposing a rectilinear support footprint
underneath it, and leave much smaller/cleaner witness marks on the fine
detail directly beneath overhangs — the exact spots where a miniature's
final appearance is most scrutinized.

## Context -> consideration mappings

- IF the user's situation says the model has fine facial or surface detail on a 0.4mm nozzle THEN weigh `layer_height` down to roughly 0.12mm or below, because that detail scale needs vertical resolution finer than default layer heights provide.
- IF the user's situation says the print is small/miniature scale generally (not just facial detail) THEN weigh `slow_down_layer_time` up well above default, because tiny cross-sections finish layers fast enough to stack heat and soften without it.
- IF the user's situation says the model has thin fins, blades, or single-bead-width walls THEN weigh enabling `detect_thin_wall` (verify it exists in the schema) and a narrower line width for those features specifically, rather than lowering line width globally, because a global change would also unnecessarily thin normal-thickness walls.
- IF the user's situation says the model is organic/detailed (a miniature figure, sculpted surface) and needs supports THEN weigh `support_type` toward the tree family over grid, because tree supports conform to oblique organic surfaces and leave cleaner marks on fine detail.
- IF the user's situation says the model is mechanical or has flat overhangs needing supports THEN weigh grid-style `support_type` as acceptable, because the organic-conforming advantage of tree supports matters less on flat, non-organic geometry.
- IF the user's situation reports softened, sagging, or blobby small features despite fine layer height already set THEN weigh `slow_down_layer_time` as the next lever, because layer-height resolution and layer-time cooling are independent causes and fixing one doesn't fix the other.
