---
topics: [seam, zits, blobs, scar]
orca_keys: [seam_position, wipe, retraction_length, retraction_speed]
---

# Seam defects (zits, blobs, scarring)

## Looks like

A visible vertical line or scattering of small bumps running up one side
of otherwise-round or curved walls, marking where each layer's perimeter
starts and stops. On a good print the seam is faint and consistently
placed; on a defective one it shows as a raised bead ("zit"), a pockmark,
or a scar that wanders across layers instead of stacking into one clean
line.

## Causes, most likely first

1. **Seam placement fighting the geometry.** A seam position that lands
   on a visually prominent or highly curved part of the wall (or one that
   Orca can't place consistently layer to layer) makes any residual
   start/stop imperfection stand out and drift. Set `seam_position` to
   `back` to hide it against a wall the part will usually face away from,
   or `aligned` to let Orca stack seams consistently on top of each other
   even if not hidden — a wandering scar across layers is a placement
   problem before it's a material one.
2. **No wipe at the seam.** Without `wipe` enabled, the small amount of
   ooze that accumulates at a perimeter's end travels with the nozzle into
   the start of the next feature instead of being scraped off on already
   printed material, showing up as a bump right at the seam. Turn `wipe`
   on.
3. **Retraction or pressure mismatched at the perimeter start/stop.** A
   seam-start blob or seam-end pit usually means the retract/prime timing
   doesn't match how much melt pressure is actually built up at that
   point in the toolpath — see `physics/retraction.md` for tuning
   `retraction_length` and `retraction_speed` to the extruder type; too
   little retraction leaves a blob, too much (or too slow to re-prime)
   leaves a pit at the next feature's start.
4. **Pressure advance untuned (Klipper-side).** On a Klipper printer,
   pressure advance governs how the extruder ramps flow up and down
   around a stop/start point independent of Orca's retraction settings;
   an untuned value causes a small over- or under-extrusion right at the
   seam that Orca-side retraction tuning can't fully compensate for. This
   is calibrated on the Klipper side (`PRESSURE_ADVANCE`), not an Orca
   profile setting.

## Verify

Print a plain cylinder or vase and examine the seam line under raking
light: it should read as a single thin, consistent vertical line (or be
essentially invisible with `aligned`/`back` placement against a hidden
face), with no individual raised dots or pits breaking the line. Seam
consistency across layers — not just at one layer — confirms retraction
and pressure advance are both in tune, since a seam that's clean on some
layers and blobbed on others points at a timing mismatch that only shows
up intermittently.
