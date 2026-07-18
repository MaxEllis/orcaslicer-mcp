---
topics: [under-extrusion, gaps, thin, flow]
orca_keys: [nozzle_temperature, filament_flow_ratio, filament_max_volumetric_speed]
---

# Under-extrusion

## Looks like

Visible gaps between adjacent lines, top surfaces with pinholes or a
striped appearance where infill shows through, or walls that feel thin
and sound hollow when tapped. The toolpath is depositing less material
than the slice math assumes it will, everywhere or in specific
fast/thick-feature regions.

## Causes, most likely first

1. **Demanded flow exceeds the melt-rate ceiling.** If a feature's
   `speed · area` is running at or above `filament_max_volumetric_speed`
   for the actual melt conditions, the hotend can't melt filament fast
   enough to keep the nozzle full — see `physics/flow-limits.md` for the
   area/flow model. Because that ceiling itself depends on temperature,
   check `physics/couplings.md` before assuming this is a speed-only
   problem: lowering the offending feature's speed and/or raising
   `nozzle_temperature` (within the material's window) are the two levers,
   and either can resolve it depending on which is more room to move.
2. **Temperature too low outright.** Independent of flow-rate coupling, a
   melt that's simply too cold for the material doesn't fully liquefy
   before extrusion, leaving under-melted polymer that doesn't bond or
   fill the toolpath cleanly. Confirm `nozzle_temperature` sits inside the
   filament's documented range, not just near its low end.
3. **Partial nozzle clog.** Carbonized residue, a bit of dust, or a
   partially-degraded filament fragment lodged in the nozzle restricts
   flow independent of any Orca setting — this is a hardware condition;
   a cold pull or nozzle swap fixes it, not a profile change.
4. **Flow ratio miscalibrated low.** `filament_flow_ratio` scales every
   commanded extrusion move; if it was calibrated (or guessed) below the
   filament's actual behavior, every feature under-extrudes uniformly
   regardless of speed. Run a proper flow ratio calibration print — don't
   nudge this value by guesswork, since an uncalibrated change just trades
   one wrong number for another.
5. **`filament_max_volumetric_speed` set optimistically.** A ceiling
   copied from a manufacturer spec sheet or another hotend's profile may
   be higher than this hotend can actually sustain (see the "Honest
   ceilings" guidance in `physics/flow-limits.md`); Orca won't clamp speed
   below a ceiling that's set too high, so profile speeds sail past the
   hotend's real limit. Derate the value and re-test.

## Verify

Print a single-wall vase-mode cylinder or a wall-thickness test block and
measure with calipers: walls should measure at or very close to the
intended line width × wall count, with no visible pinholes on solid top
layers. A fix that only helps at low speed but still shows gaps on fast
features confirms it was a flow-ceiling issue rather than a flat
temperature or clog problem.
