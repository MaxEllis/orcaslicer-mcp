---
topics: [adhesion, first-layer, bed]
orca_keys: [initial_layer_speed, nozzle_temperature_initial_layer, hot_plate_temp_initial_layer, initial_layer_line_width]
---

# First-layer adhesion

## Looks like

The first layer doesn't stick to the bed at all, or sticks patchily —
edges lift almost immediately, lines drag or get knocked loose by the
nozzle, or the whole layer detaches as a sheet when the print moves past
it. Unlike warping, this is a single-layer failure that shows up in the
first minute of the print, before any thermal shrinkage has had time to
accumulate.

## Causes, most likely first

1. **Z-offset or bed leveling wrong.** If the nozzle sits too far from the
   bed, the first layer never gets pressed into the surface; too close,
   and it gets scraped or starved of material. This is a hardware/leveling
   issue, not an Orca setting — re-run bed leveling and check Z-offset
   before touching profile values.
2. **First layer printed too fast.** Speed that's fine for later layers
   doesn't give the first layer enough dwell time against the bed to bond.
   Check `initial_layer_speed` and bring it down into the 15-25mm/s range.
3. **Temperatures too cold for good first-layer bonding.** A first layer
   printed too cold doesn't flow into the bed's texture or a textured
   plate's grip pattern. Raise `nozzle_temperature_initial_layer` 5-10C
   above the bulk `nozzle_temperature` and confirm `hot_plate_temp_initial_layer`
   is at or above the bulk bed temperature.
4. **Line width too narrow.** A first layer laid down with too little
   width per line reduces the bonded contact area with the bed. Check
   `initial_layer_line_width` sits around 105-120% of nozzle diameter
   rather than matching the bulk line width.
5. **Dirty or contaminated bed.** Oils from handling, dust, or old glue
   stick residue block adhesion no matter what the profile says — no Orca
   key controls this; clean the bed surface (isopropyl alcohol for
   smooth/textured PEI, dedicated cleaner per plate type).

## Verify

Print a single-layer skirt or a small first-layer-only test square and
inspect it right after it finishes: lines should look pressed flat and
slightly squished, not round or beaded, and should require real effort to
peel with a scraper. A first layer that lifts with light fingernail
pressure hasn't been fixed yet.
