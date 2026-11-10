# Traps that cost retries

## Per-filament values and the order of edits

- `get_config(keys=["hot_plate_temp"])` on a five-slot printer returns `"70,100,70,70,35"`: one value per
  loaded slot. Writing that list back is refused (`per_filament_length_mismatch`). `set_config` takes one
  value and writes it into the filament preset open in the Filament tab; every slot loaded with that preset
  then has it. `list_presets(type="filament")` marks the open one `selected`.
- To change another slot's value, `select_preset(type="filament", name=...)` for it first. Selecting any preset
  discards the unsaved edits of the print and printer groups too (the reply lists `discarded_changes`), so do
  filament work first, print edits second, slice last. `save_preset` first if an edit must survive.
- Setting a per-filament key to `null` with several different filament presets loaded is refused as
  ambiguous: set the value you want instead.

## Objects and plates

- Object tools take the stable `id` from `list_objects` (not the list position). Ids survive deleting other
  objects.
- Plate tools take the 0-based plate index. Deleting a plate renumbers the later ones.
- `arrange_plate` is OrcaSlicer's Arrange all: it re-packs every unlocked plate together and deletes empty
  plates at the end. To move one object use `transform_object` (`translate` is relative mm, `rotate` relative
  degrees, `scale` an absolute factor) or `move_object_to_plate`.
- On the build, with several OrcaSlicer windows open, the connection talks to the first one. Call `list_objects` right before
  slicing if the user may have edited the plate.

## The plugin's slice reading

- The plugin learns about a slice from OrcaSlicer's slice messages, one slice per plate. After a change on the
  plate or in the settings, `get_slice_status` says the slice is out of date and `get_gcode` answers
  `not_sliced` until the user slices again.
- It cannot see plate settings (a plate's own bed type, print sequence, spiral vase), OrcaSlicer's slice
  warnings, or time per feature. `describe_plate`'s `settings_check` shows when a plate setting changed what
  was sliced.

## Checks are heuristics, not measurements

- `check_profile_physics`' `gcode_scripts` reads the start and end G-code text. It trusts a printer macro
  (PRINT_START and the like) to home and heat, because the macro lives on the printer.
- A `forecast` comes from how the user's past prints compared with their estimates; with few prints there is
  none.
