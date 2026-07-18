---
topics: [ringing, ghosting, vibration, resonance]
orca_keys: [outer_wall_speed, outer_wall_acceleration]
---

# Ringing (ghosting)

## Looks like

Faint, evenly-spaced ripples on outer walls that echo a feature's edge —
most visible near sharp direction changes like corners or holes, fading
out as distance from the feature increases. It reads as a wavy surface
texture rather than a dimensional or adhesion defect, and it repeats at a
consistent spatial frequency regardless of where on the part it appears.

## Causes, most likely first

1. **Wall speed or acceleration too high for the frame's resonant
   frequency.** Ringing is the printer's frame physically oscillating
   after a sudden direction change — the toolhead's motion excites a
   mechanical resonance that the frame keeps ringing at after the command
   ends. Reduce `outer_wall_speed` and `outer_wall_acceleration` in small
   steps; lower acceleration reduces the size of the jerk that excites the
   resonance, and lower speed reduces how often the toolhead reaches
   direction changes hard enough to trigger it.
2. **Klipper input shaping not configured (or mistuned).** This is the
   real fix for a Klipper-driven printer: input shaping measures the
   frame's actual resonant frequencies with an accelerometer and applies
   a compensating motion profile so the toolhead itself doesn't excite
   them, letting speed/acceleration stay high without ringing. This is a
   Klipper-side calibration (`SHAPER_CALIBRATE`), not an Orca setting —
   Orca's speed/acceleration reductions above are a workaround for a
   printer that hasn't had this done, not a substitute for it.
3. **Frame rigidity.** Loose belts, an under-braced gantry, or a printer
   sitting on a springy surface all lower the frequency and raise the
   amplitude at which ringing appears, making even modest speeds ring
   visibly. This is a hardware condition — tighten belts, check frame
   fasteners, and set the printer on a solid, damped surface — no Orca
   key compensates for a genuinely loose frame.

## Verify

Print a ringing-test tower (a shape with a tall vertical pillar and a
sharp perpendicular step, widely used specifically to expose this defect)
and inspect the wall opposite the step under raking light: ripple
amplitude should be at or near the surface's natural texture, with no
visible periodic waves radiating from the corner. If input shaping was
just calibrated, re-run `SHAPER_CALIBRATE` after any hardware change
(new belts, added mass, different nozzle) since it invalidates the prior
calibration.
