---
topics: [speed, quality, tradeoff, throughput, drafting]
orca_keys: [filament_max_volumetric_speed, outer_wall_speed, top_surface_speed, sparse_infill_speed, travel_speed, layer_height, outer_wall_acceleration, nozzle_diameter, nozzle_temperature]
---

# Speed vs quality

## What actually determines it

Speed and quality trade against each other through two independent
ceilings, and conflating them leads to the wrong fix being applied.

The first ceiling is physics: `filament_max_volumetric_speed` caps how
fast the hotend can melt and push plastic, full stop (see
`physics/flow-limits.md` for the area × speed derivation). Above that
ceiling, Orca clamps actual speed at print time regardless of what the
speed field says — pushing the requested speed higher past this point
doesn't buy real throughput, it just makes the profile's numbers
dishonest about what's actually happening on the printer. This ceiling
is a hard physical limit that scales with `nozzle_temperature`
(hotter melt, higher sustainable flow) and line cross-section
(`layer_height` × line width), not something a speed setting alone can
push past.

The second ceiling is quality, not physics: acceleration caps how
faithfully a corner or fine feature gets reproduced. Even well under the
volumetric flow ceiling, high acceleration at direction changes
introduces momentum-driven corner rounding and ringing (see
`surface-quality.md`) that no amount of spare flow headroom fixes —
this is a motion-planning limit, not a melt-throughput one, and it has
to be addressed by slowing/accelerating less at the specific features
that show the artifact, not by a blanket speed cut everywhere.

Because these ceilings apply unevenly across a print, the honest
strategy is to spend earned speed budget where it's invisible and hold
it back where it's visible. `sparse_infill_speed` and `travel_speed` can
run close to the volumetric ceiling because interior infill and travel
moves are never seen once the part is done — errors there cost nothing
cosmetically. `outer_wall_speed` and `top_surface_speed` should be spent
last and most conservatively, because those are the passes that
actually form the surfaces a viewer inspects (per `surface-quality.md`);
shaving time off those the same way as infill trades away exactly the
quality the print is meant to have.

A legitimate way to draft faster without recklessly chasing speed is to
increase `layer_height` within the sane 25-65% of `nozzle_diameter`
ratio (see `physics/flow-limits.md` and `large-nozzle.md`): fewer,
thicker layers cut total print time by printing less total toolpath
length for the same volume, without needing to push any single pass's
speed or acceleration past the point where quality degrades. This is
categorically different from just cranking speed everywhere — it trades
vertical resolution (coarser banding) for time, rather than trading
motion fidelity (ringing, rounding) or flow adequacy (under-extrusion)
for time.

## Context -> consideration mappings

- IF the user's situation says the priority is fast throughput and cosmetic finish doesn't matter much THEN weigh `sparse_infill_speed` and `travel_speed` up toward the volumetric ceiling first, because those passes are invisible once the part is done.
- IF the user's situation says the print needs to look clean or the outer surfaces are inspected/handled THEN weigh `outer_wall_speed` and `top_surface_speed` down even if that costs total print time, because those are the passes speed-vs-quality tradeoffs actually show up on.
- IF the user's situation says the goal is a quick draft/prototype rather than a final part THEN weigh `layer_height` up toward the top of the 25-65% nozzle-diameter ratio over blanket speed increases, because thicker layers cut time without degrading motion fidelity the way high speed/acceleration does.
- IF the user's situation reports under-extrusion, gaps, or inconsistent width at high speed THEN weigh `filament_max_volumetric_speed` and `nozzle_temperature` (`physics/couplings.md`) as the binding constraint, because that's a flow-ceiling problem no acceleration change fixes.
- IF the user's situation reports ringing, corner rounding, or wobble even though flow seems adequate THEN weigh `outer_wall_acceleration` down at the affected features, because that's a motion-planning limit independent of the volumetric ceiling.
- IF the user's situation says time is the binding constraint but the part still needs functional accuracy (bolts, mating features) THEN weigh sparing outer-wall/top-surface speed even while raising infill/travel speed, because dimensional and cosmetic quality on functional surfaces shouldn't be paid for out of the same budget as bulk interior time savings.
