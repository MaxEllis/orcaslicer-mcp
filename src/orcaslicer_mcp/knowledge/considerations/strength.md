---
topics: [strength, load, walls, infill, mechanical]
orca_keys: [wall_loops, sparse_infill_density, sparse_infill_pattern, top_shell_layers, bottom_shell_layers, layer_height, nozzle_diameter, nozzle_temperature]
---

# Strength

## What actually determines it

A printed part's strength is anisotropic: it is strong along the
layer plane and weak across layer boundaries, because bonding between
layers is a partial re-weld of already-solid plastic rather than a
continuous pour. Two separate levers push against that weakness —
geometry (how much solid perimeter the part has) and process (how well
each layer actually fused to the one below) — and they don't trade off
against each other one-for-one.

Walls (`wall_loops`) add continuous, well-oriented solid shell that
carries load along the strongest axis of the print. Infill
(`sparse_infill_density`) fills the interior with a much sparser,
weaker lattice whose job is mostly to support the top surface and resist
local crushing, not to carry structural load. Because of this, one
additional wall loop typically contributes more real-world strength
than a large jump in infill density — going from 2 to 3 `wall_loops`
adds more continuous load-bearing material than raising
`sparse_infill_density` by 15 percentage points, because the extra wall
is solid, oriented material while the infill increase is still a sparse
lattice with air gaps. Infill density matters most for crush/compression
loads through the part's interior, not for bending or tension carried by
the shell.

Layer adhesion is the second lever, and it's a process constraint, not
a geometry one: strength across layers depends on the melt actually
being hot enough and flowing fast enough to fuse into the layer below
(see `physics/couplings.md` for the temperature-flow relationship) —
a wall printed under-temperature or over-flow-ceiling looks fine
dimensionally but delaminates under load because the layers never
fully welded. Layer height also bounds how well a bead can key into the
layer beneath it: as a rule of thumb, `layer_height` should stay at or
below roughly 65% of `nozzle_diameter` so each new bead has enough
nozzle-driven pressure and contact area to press into and fuse with the
prior layer, rather than being squeezed thin and cold on top of it.

Load direction relative to the layer stack changes which lever matters.
A part loaded parallel to the layers (bending a flat plate on its face)
is limited by wall/infill geometry, which responds well to more walls.
A part loaded across the layer stack (pulling along Z, e.g. a boss that
gets pulled straight up) is limited by interlayer adhesion, which
responds to hotter, thinner layers or, better, reorienting the part so
the load runs in-plane instead of across layers — this is a geometry
and print-orientation decision, not something any single setting fixes.

Functional parts that take bolts or repeated mechanical stress need a
floor of continuous shell regardless of infill: as a starting point,
treat roughly 3 `wall_loops`, 4 `top_shell_layers`, and 4
`bottom_shell_layers` as the functional minimum for a bolted or
load-bearing part, then adjust up for higher loads rather than down for
faster prints.

Infill pattern choice follows the same geometry-vs-cost logic. Patterns
that connect in more than one plane (gyroid, cubic, grid) resist
multi-axis loading better because a load from any direction meets some
oriented material, which matters for parts that get stressed
unpredictably. Rectilinear infill is the cheapest to print (fewest
direction changes, least travel) but only resists load well along its
own two print directions, so it is the right choice when the load path
is known and aligned, and the wrong choice when it isn't.

## Context -> consideration mappings

- IF the user's situation says the part is bolted, load-bearing, or must survive repeated stress THEN weigh `wall_loops` upward toward the ~3-wall / 4-top / 4-bottom functional floor before touching `sparse_infill_density`, because walls carry oriented load and infill mostly resists crushing.
- IF the user's situation says the load runs primarily along Z (pulled straight up, e.g. a boss or hook) THEN weigh interlayer adhesion (adequate `nozzle_temperature` per `physics/couplings.md`, `layer_height` ≤65% of `nozzle_diameter`) and part reorientation over adding more walls, because Z-loading is limited by layer bonding, not shell thickness.
- IF the user's situation says the load direction is unknown or multi-axis (dropped, twisted, handled roughly) THEN weigh `sparse_infill_pattern` toward gyroid/cubic/grid over rectilinear, because those patterns resist load from more than one direction.
- IF the user's situation says the load direction is known and in-plane (a bracket flexing on one axis) THEN weigh rectilinear `sparse_infill_pattern` as acceptable, because its cheaper print cost isn't paid for with a strength deficit along the known load axis.
- IF the user's situation says the part is decorative or low-stress THEN weigh `wall_loops` and shell layers down toward the low end of what still avoids visible infill show-through, because functional-part minimums don't apply and print time matters more.
- IF the user's situation flags under-extrusion, unusual weakness, or delamination at low apparent stress THEN weigh temperature/flow adequacy (`physics/couplings.md`) before adding walls or infill, because a process problem in layer bonding won't be fixed by adding more of the same badly-bonded geometry.
