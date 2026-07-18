---
topics: [stringing, oozing, travel, hairs]
orca_keys: [nozzle_temperature, retraction_length, retraction_speed, wipe, travel_speed]
---

# Stringing

## Looks like

Thin hairs or webs of filament strung between separated features — tower
tops, islands, perimeter to perimeter — left behind after every travel
move. Left unaddressed, the hairs char on subsequent passes and coat the
part in a fuzzy residue. Distinguish stringing (fine threads that span the
gap, snap or brush away cleanly) from blobbing (isolated globs that sit on
the surface where a move starts or ends) — the two look similar at a
glance but point at different causes below.

## Causes, most likely first

1. **Melt too hot.** Above the material's sweet spot, viscosity drops and
   the melt oozes out of the nozzle under residual pressure alone, with no
   travel move required to trigger it. Drop `nozzle_temperature` 5-10C at
   a time, staying inside the filament's rated window, and re-test between
   steps — stringing that clears at the first 5C step confirms this cause.
2. **Retraction too short or too slow.** If `retraction_length` isn't
   pulling enough filament back to relieve nozzle pressure before a travel
   move, or `retraction_speed` is too slow to do it before the move
   starts, ooze escapes during every gap crossing. Raise `retraction_length`
   in 0.2-0.5mm steps and `retraction_speed` toward 30-45mm/s; see
   `physics/retraction.md` for direct-drive vs Bowden ranges before pushing
   either past the sane ceiling for the extruder type.
3. **No wipe on retraction.** Without `wipe` enabled, the ooze bead parked
   on the nozzle tip at the end of a retraction gets carried straight into
   the next travel move's landing point instead of being scraped off on
   already-printed material. Turn `wipe` on.
4. **Travel moves too slow.** A slow `travel_speed` gives residual ooze
   more time to droop and stretch into a visible string before the move
   completes. Raising `travel_speed` shortens that exposure window without
   touching the underlying ooze rate.
5. **Wet filament.** Moisture in the filament boils and off-gasses at
   nozzle temperature, and the escaping steam pushes melt out past the
   retraction that would otherwise hold it back — this shows up as
   stringing that doesn't respond to any of the settings above. Dry the
   filament (no Orca key controls this) and re-test.

## Verify

Print a two-tower travel test (two thin towers a fixed distance apart,
sliced so the toolpath travels between them dozens of times): count
threads spanning the gap before and after each change. A fix that works
shows a clean drop in thread count per pass, not just thinner threads —
threads that persist but thin out point at a partial cause (e.g.
temperature) with another one still active (e.g. retraction). If globs
rather than threads dominate, treat it as a wipe/travel-speed problem
first, not a temperature one.
