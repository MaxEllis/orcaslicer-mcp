# Order of operations on a multi-filament setup

**Filament edits first, print edits second, slice last.**

Two behaviours of the API combine to force that order. Both are by design; this page
explains them so the sequence makes sense rather than being a rule to memorise. Verified on
a Bambu Lab P1S with five AMS slots, orcaslicer-mcp 0.1.14 and OrcaSlicer MCP v2.4.2-mcp.12.

## 1. Per-filament settings are read merged, but written one preset at a time

`get_config` returns per-filament keys (`hot_plate_temp`, `fan_min_speed`, `fan_max_speed`,
`close_fan_the_first_x_layers`, `overhang_fan_speed`, the `nozzle_temperature` family, ...)
as one entry per loaded slot:

```
get_config(keys=["hot_plate_temp"])
→ "70,100,70,70,35"
```

`set_config` writes a single value, and it lands in whichever filament preset is open in the
Filament tab. Echoing the merged form back is refused:

```
set_config({"hot_plate_temp": "70,100,70,70,35"})
→ 422 per_filament_length_mismatch: the filament preset open in the Filament tab
  holds 1 value(s), got 5 ...

set_config({"hot_plate_temp": "100"})
→ applied. Every slot loaded with that preset now reads 100.
```

Find the open preset with `list_presets(type="filament")`: it is the one marked
`selected`. Two slots loaded with the same preset always share a value; a preset cannot hold
different values for different slots.

Before v2.4.2-mcp.12 the merged write was accepted and spliced the whole list into that one
preset, so a five-entry list grew to thirteen on the next read.

## 2. select_preset discards unsaved overrides in every group

To change a filament value for a slot that is not open in the Filament tab, you select that
slot's preset first. Selecting any preset discards unsaved overrides in the print and printer
groups as well, including edits made by hand in the OrcaSlicer window, and even when
re-selecting the preset that is already active. The slicer does this so the switch cannot
open a modal dialog that nothing can dismiss remotely. v2.4.2-mcp.12 reports it:

```
select_preset(type="filament", name="Generic ASA")
→ {"selected": "Generic ASA", "discarded_changes": ["print", "filament"]}
```

So any print-group edit made before a filament switch is lost. Call `save_preset` first if
it must survive, or, more simply, do the filament work first.

## Recipe for a multi-material plate

1. `get_status`, and check `project` is the file you expect. The API acts on whatever
   OrcaSlicer currently has open.
2. For each slot whose filament values you want to change:
   `select_preset(type="filament", name=<that slot's preset>)`, then `set_config` with single
   values.
3. Print-group changes with `set_config` (`wall_loops`, `brim_width`,
   `sparse_infill_density`, `initial_layer_speed`, ...). These are plain scalars.
4. `load_model`, then `set_object_config(id, {"extruder": N})` for each object.
5. `slice_and_wait`, then `get_slice_warnings`.

## Related

- `get_status` lists modified keys per group; a per-filament key shows as `key#slot`
  (`hot_plate_temp#0`).
- A corrupted filament list from an older build is repaired by re-selecting the preset,
  at the cost of the print overrides (see 2).
- To confirm which filament profile actually sliced, divide the reported filament mass by
  its length (2.405 mm² cross-section for 1.75 mm filament). ASA comes out near 1.05 g/cm³,
  PETG near 1.27.
