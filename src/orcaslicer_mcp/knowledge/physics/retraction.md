---
topics: [retraction, stringing, ooze, z-hop]
orca_keys: [retraction_length, retraction_speed, z_hop, wipe]
---

# Retraction and stringing

## What retraction is for

Between extrusion moves — travelling over a gap, moving to a new island,
finishing a wall before lifting off — the pressurized melt sitting in the
nozzle keeps oozing out under its own residual pressure unless that
pressure is relieved. `retraction_length` pulls filament back up through
the hotend (at `retraction_speed`) before a travel move, dropping melt
pressure at the nozzle tip so it stops drooling; the same length is pushed
back out before extrusion resumes. Under-retracting leaves residual ooze
that turns into stringing between features; over-retracting wastes time,
risks grinding the filament against the drive gear, and on some materials
introduces gaps/blobs when the extra length is re-primed.

## Sane ranges by drive type

How much retraction is needed depends on how far the retraction pull has
to travel before it actually relieves pressure at the nozzle tip:

- **Direct-drive** extruders sit right on top of the hotend, so the
  filament path from drive gear to nozzle is short. Sane
  `retraction_length` is roughly **0.2–2.0 mm**. Anything above **3 mm on
  a direct-drive setup is a smell** — it usually means retraction is
  compensating for something else (a leaking heatbreak, wrong
  `filament_max_volumetric_speed`/temperature coupling, or a profile
  copied from a Bowden setup) rather than being a genuinely tuned value.
- **Bowden** extruders push filament through a long PTFE tube before it
  reaches the hotend, so the pull has to take up slack and tube
  compliance before it affects nozzle pressure. Sane `retraction_length`
  is roughly **2–7 mm**.

## Nozzle bore and wipe

Larger nozzle bores hold a larger melt reservoir and a wider opening, so
residual pressure drools more readily between moves — a 0.8 mm nozzle
oozes more than a 0.4 mm nozzle at the same pressure and temperature. On
direct drive, a 0.8 mm nozzle profile should sit in the **upper half of
the 0.2–2.0 mm range** rather than the low end, and should pair retraction
with `wipe` enabled: wiping the nozzle across already-printed material at
the end of the retraction move scrapes off the ooze bead that would
otherwise stay parked on the tip and blob onto the next travel move's
landing point.

## Z-hop

`z_hop` lifts the nozzle vertically during travel moves so it clears
printed features instead of dragging through them if any ooze or
part-warp bump is in the way. It doesn't reduce ooze itself — it only
prevents whatever ooze exists from being smeared into the part — so it is
a complement to retraction tuning, not a substitute for it.

## Pressure advance changes the picture

Klipper's pressure advance compensates for the melt pressure lag directly:
it anticipates the pressure buildup/release at accelerations and
direction changes and adjusts extruder motion ahead of time, so the
nozzle carries less residual pressure into a travel move in the first
place. With pressure advance properly tuned, the retraction length needed
to stop oozing drops — profiles migrating to a Klipper printer with
pressure advance enabled should expect to retune retraction downward
rather than reusing values tuned on a printer without it.

The ranges above are baselines for setups WITHOUT tuned pressure advance.
With PA tuned (Klipper), Ellis' starting points drop to roughly 0.5-1 mm
direct drive and 1-3 mm bowden — PA removes most of the pressure the
retraction otherwise has to relieve.
