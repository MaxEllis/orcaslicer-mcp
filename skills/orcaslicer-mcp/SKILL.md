---
name: orcaslicer-mcp
description: "Use with the orcaslicer-mcp server (tools such as get_status, get_config, set_config, slice_and_wait, describe_plate, check_profile_physics, check_printer_match, recall_prints): help the user reach the print they want in a running OrcaSlicer (the OrcaSlicer MCP build, or stock OrcaSlicer 2.5 with the OrcaSlicer MCP plugin), explain why each setting matters, check slices and profiles, and learn from past prints. Use whenever those tools are available or the user asks for something in OrcaSlicer through them."
---

# Working in OrcaSlicer through orcaslicer-mcp

The user works with you toward a print: stronger, faster, cleaner, a better fit. Change settings with them,
say why each change helps, and check the result with data. Every change you make shows in OrcaSlicer's
OrcaSlicer MCP panel, where the user can revert it.

## Start

1. If the client loads tool schemas on demand, load what the task needs in one go, for example
   `get_status, get_config, set_config, search_settings, describe_setting, slice_and_wait, describe_plate`;
   add `list_objects, transform_object, arrange_plate` for the plate, `list_presets, select_preset,
   save_preset` for presets, `check_profile_physics` before temperature, speed or flow changes, and the
   printer tools only when a printer is involved.
2. Call `get_status`. `connected_to` says build or plugin:
   - **Build** (OrcaSlicer with the Remote API built in): every tool works.
   - **Plugin** on stock OrcaSlicer 2.5: settings, presets (switch, not save), objects (read only) and the
     slice the user makes. It cannot load models, start a slice, render, manage plates, or open and save
     projects. Say so before trying; ask the user to press **Slice plate**, then read it with
     `get_slice_status`.
3. No orcaslicer tools at all: the connector is not set up. The plugin's Config tab (main menu beside File >
   Plugins > OrcaSlicer MCP > Config) has copy-ready setup for each AI app.

## Settings

- **Find the key**: `search_settings` with a few words ("first layer speed"); `describe_setting` for one key's
  meaning, unit, range and choices. `get_config` with `keys` returns the values and, under `about`, what each
  one means. Name the keys: the whole config is several hundred settings.
- **Change**: `set_config` writes unsaved edits, all or nothing. Percentages are strings (`"15%"`). A key set
  to `null` goes back to its saved preset value. The reply's `applied` and `errors` say what happened.
- **Before temperature, speed, acceleration or flow changes**: `check_profile_physics` with the proposed
  `changes`. `verdict: blocked` means do not save; explain the failing check. To keep a change, `save_preset`
  (or `edit_preset` to change a stored preset in one step, which runs the same gate).
- **Per-filament settings** (temperatures, fans, `hot_plate_temp`, ...): `get_config` shows one value per
  loaded slot, `"70,100,70"`; `set_config` writes ONE value into the filament preset open in the Filament tab.
  On a multi-filament plate do filament edits first: `select_preset` for another slot discards unsaved print
  edits (see references/traps.md). On a build with the `filament_slots` capability use the slot tools instead:
  `get_filament_slots` (every slot's preset, type, colour, unsaved edits, shared presets), `get_config` with
  `by_slot`, `set_config` and `save_preset` with `filament_slot`, `set_filament_slot_preset`.
- **Refused keys**: scripts, custom G-code and printer-connection settings are refused. On the build, the user
  can allow them with **Allow script, G-code and connection edits** in Preferences > Remote API; with the
  plugin, never. Tell the user; do not look for a way around it.
- **The user's own edits**: a reply carrying `user_changes` means they changed something by hand since your
  last call. Build on it; never undo it.

## Slicing and checking

- `slice_and_wait` (build) returns time, filament and warnings. A reply with `forecast` says how long it will
  really take on their printer from start to finish; relay its `summary`.
- After any change the last slice is out of date: slice again before quoting numbers.
- `get_slice_warnings`, `get_slice_breakdown` (time and flow per feature), and `describe_plate` (how each
  part stands on the bed, overhangs, supports, seams, from the G-code). Relay `describe_plate`'s `summary`
  before proposing orientation or support changes. If its `settings_check.differ` lists settings, the slice
  used other values than those set now (often a plate's own bed type, print sequence or spiral vase): tell
  the user before printing.
- `compare_slices` slices up to 8 variants of the current plate and restores the settings afterwards; relay
  its `headline` and table. It names a winner only when one option beats the others on every measure.
- `render_plate` (build) shows the plate or the toolpaths as a picture when the user wants to see it.

## Printers and past prints

- Before slicing a model, `recall_prints`: how earlier prints of it went (result, the user's verdict, the
  settings used). Mention a past warp or failure before proposing the same settings.
- `check_printer_match` before a print. On Klipper it compares the profile with the printer's own limits; when
  it returns `fix`, offer it, and if the user agrees pass `fix` to `set_config`. On a Bambu Lab printer it checks
  each filament's slot preset against the AMS tray OrcaSlicer's Send dialog will pick (`match` exact, soft, none):
  relay `headline` and `warnings`, and on a soft match name both filaments and let the user decide.
- `save_gcode` before printing records the slice so the finished print joins back to its settings.
- `get_printer_status`, `wait_for_printer` and `list_print_history` read the printer. Starting, stopping or
  heating a printer is never done without the user's explicit yes.

## More

references/traps.md: per-filament writes, plate and object ids, arrange, the plugin's slice reading, units.
