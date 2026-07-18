---
topics: [layer-adhesion, splitting, delamination, strength]
orca_keys: [nozzle_temperature, layer_height, line_width, fan_max_speed]
---

# Layer splitting (delamination)

## Looks like

The part cracks or fully separates cleanly along a layer boundary under
load — bending, dropping, or even normal handling snaps it flat along one
horizontal plane rather than tearing across the print direction. This is
a bonding failure between layers specifically, distinct from a weak
infill pattern or a thin-wall failure that would crack across layers
instead of along one.

## Causes, most likely first

1. **Melt too cold for the demanded flow at the moment of bonding.**
   Interlayer bonding happens when a new layer's hot polymer partially
   re-melts the top of the layer below it; if the effective melt
   temperature at deposition is too low for the flow rate in play (see
   `physics/couplings.md` for how flow and temperature trade off), that
   re-melt doesn't happen deeply enough and the boundary stays a weak
   seam even though the print looks visually fine. Raise
   `nozzle_temperature` within the material's window before changing
   anything geometric.
2. **Layer height too thick for the nozzle.** A `layer_height` above
   roughly 80% of nozzle diameter leaves the new layer's bead too tall and
   too far from the previous layer's peak contact zone to bond as deeply,
   independent of temperature. Check the `layer_height`:nozzle-diameter
   ratio and bring it back under 80% for parts that need strength across
   this axis; this also interacts with `line_width` — a wider line at the
   same height increases contact area and helps bonding.
3. **Cooling too aggressive.** Fan airflow that cools a layer before the
   next one lands reduces how much residual heat is available to re-melt
   into the layer above — good for overhang crispness, bad for interlayer
   strength on parts that need to hold load along the layer axis. Turn
   `fan_max_speed` down for structural parts, accepting some loss of
   overhang/bridge quality as the tradeoff.
4. **Wet filament.** Moisture boiling out of the filament at nozzle
   temperature creates micro-voids right at the bonding interface, weakening
   it independent of any Orca setting — dry the filament and re-test if
   the above changes don't fully resolve it.

## Verify

Print a layer-adhesion test bar (a simple rectangular bar printed flat, then
snapped or pulled to failure) and compare break behavior before and after:
a part that now cracks diagonally across layers instead of cleanly along
one layer boundary has had its interlayer bond strengthened past the
part's own bulk strength, which is the target state.
