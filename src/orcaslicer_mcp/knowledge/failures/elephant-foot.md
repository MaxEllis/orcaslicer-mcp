---
topics: [elephant-foot, first-layer, squish, dimension]
orca_keys: [initial_layer_print_height, hot_plate_temp_initial_layer, elefant_foot_compensation]
---

# Elephant foot

## Looks like

The bottom one or two layers of a print bulge outward past the intended
wall, giving the base a flared, foot-like profile that's visibly wider
than the rest of the part and measurably out of dimension when checked
with calipers. It's the inverse problem from first-layer adhesion
failure: the layer is bonding fine — too well, spreading past its
intended footprint under its own weight and the nozzle's downward
pressure.

## Causes, most likely first

1. **First layer over-squished.** A first layer height set too low
   relative to nozzle diameter presses the bead out sideways further than
   the toolpath intends, especially once the weight of the rest of the
   print is bearing down on a still-warm base. Raise
   `initial_layer_print_height` toward a more typical first-layer value
   and check whether the flare shrinks.
2. **Bed too hot at the base.** A bed running hotter than necessary keeps
   the bottom layers soft and compliant well after they're deposited, so
   they keep spreading under the weight above them instead of setting up.
   Drop `hot_plate_temp_initial_layer` about 5C and re-test — this trades
   off against first-layer adhesion, so don't drop it further than needed.
3. **No elephant-foot compensation configured.** Orca can deliberately
   inset the first few layers to counteract the geometric squish-out
   directly, independent of temperature and height tuning. Set
   `elefant_foot_compensation` to 0.1-0.2mm — note the Orca-specific
   spelling ("elefant"), not "elephant" — and increase in small steps if
   flare persists after the above.

## Verify

Print a simple calibration cube or a dimensionally-known test part, then
measure the base width with calipers at the very bottom layer versus a
few layers up. A footprint within about 0.05-0.1mm of the model's nominal
width at the base confirms the fix; if the flare is gone visually but
calipers still show over-width, dial in `elefant_foot_compensation`
further rather than declaring it fixed on visual inspection alone.
