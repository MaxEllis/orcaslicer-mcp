---
topics: [surface, quality, finish, cosmetic, appearance]
orca_keys: [outer_wall_speed, inner_wall_speed, outer_wall_acceleration, layer_height, seam_position, top_surface_line_width, top_surface_speed, line_width]
---

# Surface quality

## What actually determines it

Visible surface quality is dominated by whichever pass actually forms
the surface the eye sees, not by the print as a whole — the outer wall
for vertical faces, the top surface for upward-facing ones. Everything
that happens on interior passes (inner walls, sparse infill) is
invisible once the part is done, so surface-quality tuning should spend
effort on outer wall and top surface settings first and treat interior
passes as free to run fast.

Outer wall speed and acceleration dominate because they control how
much the nozzle can wobble, ring, or under/over-extrude at direction
changes on the one pass that's actually visible. `outer_wall_speed` is
conventionally set to roughly 60-70% of `inner_wall_speed` — slower
than the interior passes — because the outer wall has no adjacent
already-printed wall to brace against and any speed-induced deviation
shows immediately as visible ridging or wobble. `outer_wall_acceleration`
should be kept low relative to inner-wall/infill acceleration for the
same reason: high acceleration at a corner is exactly where ringing
(resonant vibration echoing backward from a sudden direction change)
imprints itself onto the one surface a viewer will actually inspect.

Layer height sets the vertical resolution of every non-vertical
surface angle: each layer's stair-step is visible at low angles as
banding, and the taller the layer, the coarser and more visible the
steps. This is a direct, unavoidable trade against print time — there's
no setting that removes banding without either shrinking layer height
or changing the angle, at which point the model geometry (not the
profile) is what's being changed.

Seam placement decides where the necessary wall start/stop discontinuity
shows up rather than whether it exists at all — every closed-loop wall
has to start and stop somewhere, and that point always leaves a small
visible mark or blob. `seam_position` chooses whether that mark lands
somewhere hidden (an edge, a corner, the back of the part) or randomly
wherever geometry happens to align, so it's a cosmetic-placement
decision, not a defect-elimination one.

Top (upward-facing) surfaces get their own tighter settings because
they're usually the most visually scrutinized flat face on a part.
`top_surface_line_width` slightly narrower than the general `line_width`
lets lines overlap a bit more per pass, closing gaps between adjacent
top lines more completely. `top_surface_speed` low (well under general
infill speed) gives the melt more time to lay down flat and level before
the nozzle moves on, which matters because top surface lines have no
solid layer immediately underneath supporting their shape the way inner
infill does.

Cooling adequacy underlies all of the above: none of the outer-wall or
top-surface speed/acceleration tuning helps if the plastic isn't
solidifying fast enough to hold its shape once deposited — see
`physics/cooling.md` for the layer-time and fan-speed relationship that
determines whether a slow, careful outer wall pass actually gets to set
before the next layer lands on it.

## Context -> consideration mappings

- IF the user's situation says the part has visible vertical faces that must look clean (display piece, gift, visible enclosure) THEN weigh `outer_wall_speed` down toward 60-70% of `inner_wall_speed` and `outer_wall_acceleration` down, because the outer wall is the one pass a viewer inspects directly.
- IF the user's situation says ringing, wobble, or ghosting artifacts appear near corners THEN weigh `outer_wall_acceleration` down first, because acceleration at direction changes is what excites the resonance that ringing shows.
- IF the user's situation says visible banding/stair-stepping on sloped or curved surfaces is a concern THEN weigh `layer_height` down, because banding visibility is a direct function of layer height at any given surface angle, not of speed or cooling.
- IF the user's situation says the seam line must be hidden or the part is symmetric with no obvious hide spot THEN weigh `seam_position` toward an edge/corner alignment rather than leaving it random, because the seam mark exists regardless of setting — the choice is only where it lands.
- IF the user's situation says the top surface has visible gaps, pinholing, or a rough finish THEN weigh `top_surface_line_width` narrower and `top_surface_speed` lower, because top lines lack a solid backing layer and need more overlap and dwell time to knit closed.
- IF the user's situation says small or thin features show sagging or rough tops despite good wall settings THEN weigh part-cooling adequacy (`physics/cooling.md`) before touching speed/accel further, because insufficient cooling undermines shape retention regardless of how carefully the toolpath itself is tuned.
