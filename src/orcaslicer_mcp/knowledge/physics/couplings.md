---
topics: [temperature, speed, coupling, melt]
orca_keys: [nozzle_temperature, nozzle_temperature_initial_layer, filament_max_volumetric_speed, filament_type]
---

# Temperature–flow coupling

## Why temperature and flow are coupled

`filament_max_volumetric_speed` is not a fixed material constant — it is a
function of how much thermal energy the hotend can transfer into the
filament per second, which itself depends on `nozzle_temperature`. Pushing
more plastic through the nozzle per second (higher demanded flow, see
`flow-limits.md`) means each cubic millimetre of polymer spends less time
in the melt zone. If the melt zone isn't hot enough to fully melt that
plastic in the shorter dwell time, the result is under-extrusion at speed:
gaps, weak layer bonding, and inconsistent width — even though the printer
reports no error and the nominal flow number looks fine on paper.

The coupling runs both ways. Raising flow demand (faster speeds, or wider
lines/thicker layers per the flow-limits area formula) without raising
`nozzle_temperature` risks starving the melt. Raising temperature without a
matching flow requirement doesn't help throughput and instead costs print
quality: more oozing between moves (stringing), softer overhangs and
bridges, and blurred fine detail from a larger, more mobile melt pool.

## Sustainable-flow rules by material

**Provenance note (2026-07-19 fact-check):** the linear rules below are an
in-house engineering heuristic, not literature-derived constants. External
bench data (CNC Kitchen) confirms the DIRECTION — hotter sustains more flow —
but no published source validates these slopes; CNC Kitchen's V6 skip-point
data suggests a shallower slope (~0.2 mm³/s per °C) for the SKIPPING
threshold. These rules are deliberately steeper/more conservative because
they target QUALITY-grade extrusion (strong layer bonds), which degrades
well before the extruder skips, and they match the real 2026-07-18
Sidewinder incident (205 °C weak layers at ~14 mm³/s; 215 °C sound). Two
structural limits apply regardless: sustainable flow can never exceed the
hotend's melt-capacity ceiling (heater power bound — see flow-limits.md),
and the profile's own `filament_max_volumetric_speed` gate catches demand
above the machine ceiling independently of temperature.

For the common filament families, a linear approximation of the
temperature-to-sustainable-flow relationship (`T` = `nozzle_temperature`,
result in mm³/s) is normative for validation:

| Material   | Sustainable flow                  |
|------------|------------------------------------|
| PLA        | `(T − 195) / 1.2`                  |
| PETG       | `(T − 220) / 1.4`                  |
| ABS / ASA  | `(T − 225) / 1.4`                  |

These are derived from `filament_type` and apply only to these families;
for any other material there is no normative rule — treat temperature/flow
adequacy as informational judgment only, not a pass/fail check.

Each formula has an implicit floor: below the subtracted constant (195 °C
for PLA, 220 °C for PETG, 225 °C for ABS/ASA) the material isn't reliably
above its melt threshold and sustainable flow is effectively zero,
regardless of what the linear formula evaluates to.

## Worked example (real case, 2026-07-18 Sidewinder X2)

A profile ran PLA at `nozzle_temperature = 205 °C` with a 0.8 mm nozzle at
0.4 mm layers. Sustainable flow at that temperature:

```
(205 − 195) / 1.2 ≈ 8.3 mm³/s
```

The profile's wall speed and 0.8 mm line width demanded ≈ 13.8 mm³/s —
roughly 66% above what 205 °C could sustain. The print under-extruded on
the walls. The fix was to raise `nozzle_temperature` to 215 °C:

```
(215 − 195) / 1.2 ≈ 16.7 mm³/s
```

16.7 mm³/s comfortably clears the 13.8 mm³/s demand, and the print ran
clean. This is the pattern the rule is meant to catch: a wide nozzle or
thick layer can demand more flow than a "safe-looking" PLA temperature
can actually deliver.
