---
topics: [material, environment, filament, temperature, uv, moisture]
orca_keys: [filament_type, nozzle_temperature, retraction_length, fan_max_speed]
---

# Material and environment

## What actually determines it

Material choice is where a part's end-use environment gets decided,
and it should be settled before profile tuning starts — no amount of
setting adjustment turns a material into one it isn't.

PLA is dimensionally rigid and easy to print, but it's a low
glass-transition polymer: it creeps (slowly deforms) under sustained
mechanical load even at room temperature, and it noticeably softens
around 55-60°C — well within the temperature a closed car interior
reaches in direct sun. A PLA part left under load in a hot car, on a
sunny windowsill, or anywhere else that gets meaningfully warmer than a
climate-controlled room will sag, warp, or lose its fit over time even
if it printed perfectly. PETG and ASA both have meaningfully higher
softening points and don't creep the same way under sustained load,
making them the correct choice whenever the end environment involves
heat, sun, or a car — that's an environment-driven material decision,
not a profile-tuning one.

Filament families differ in secondary print behavior that follows from
the material choice, not from the profile: PETG strings more readily
than PLA at comparable settings because it's more viscous and prone to
ooze at temperature (see `physics/retraction.md` for
`retraction_length` tuning to compensate), and it also needs slower,
gentler cooling than PLA — aggressive fan speed on PETG produces
matte, weak-looking layers rather than the glossy, well-bonded ones it's
capable of. This means switching material families should trigger a
review of `retraction_length` and `fan_max_speed` together, not just a
`nozzle_temperature` change.

Moisture is a cross-material concern, not one specific to a "sensitive"
filament: all common filaments (PLA, PETG, ABS/ASA, and especially
nylon) absorb ambient humidity over time, and wet filament produces
steam pressure inside the melt zone that shows up as popping, stringing,
or rough surface texture that doesn't respond to any temperature or
retraction adjustment. There's no Orca setting that fixes wet filament
— drying the spool (dedicated dryer or a low oven) before printing is
the only real fix, and it should be the first thing checked when
symptoms don't respond to the settings that normally address them.

UV/outdoor exposure is another environment-driven material axis,
independent of heat tolerance: ASA is formulated for good UV/weather
resistance and is the right default for anything left outdoors long
term. PETG holds up moderately to UV but will discolor and gradually
embrittle with prolonged sun exposure. PLA has the weakest UV/outdoor
resistance of the three and will visibly degrade (yellowing,
brittleness) faster than either when left outside.

## Context -> consideration mappings

- IF the user's situation says the part will sit in a hot car, greenhouse, or direct sun, or otherwise experience sustained heat THEN weigh `filament_type` toward PETG or ASA over PLA, because PLA creeps and softens well below temperatures those environments reach.
- IF the user's situation says the part carries sustained mechanical load (a clip under tension, a bracket under weight) for long periods THEN weigh `filament_type` away from PLA regardless of ambient temperature, because PLA's creep behavior is a sustained-load property, not only a heat one.
- IF the user's situation names PETG as the material THEN weigh `retraction_length` up from PLA-tuned values and `fan_max_speed` down, because PETG strings more readily and needs gentler cooling to bond and finish well.
- IF the user's situation reports popping, stringing, or rough texture that didn't respond to temperature/retraction changes THEN weigh filament moisture (drying) as the cause before further profile tuning, because wet filament symptoms mimic other defects but aren't fixed by any Orca setting.
- IF the user's situation says the part will be outdoors or in direct sun for extended periods THEN weigh `filament_type` toward ASA first, PETG second, and away from PLA, because UV/weathering resistance ranks ASA > PETG > PLA.
- IF the user's situation doesn't mention environment or load duration at all and the part is a quick indoor prototype THEN weigh PLA as an acceptable default, because its ease-of-printing advantage isn't offset by any of the above concerns when the part won't experience heat, sustained load, moisture, or sun.
