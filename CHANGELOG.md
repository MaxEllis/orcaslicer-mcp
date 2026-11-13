# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Bambu Lab printers: `get_printer_slots` reads the AMS and external spool of the printer selected in OrcaSlicer's Device tab (every tray's label, type as the AMS words it, name, brand, colour, remaining filament, humidity per unit), through OrcaSlicer's own connection, so nothing is sent to the printer and the answer is instant. `get_filament_slot_mapping` shows the tray OrcaSlicer's Send dialog will pre-fill for each filament of the plate and whether it holds what the slot's preset says: `match` exact, soft (no tray holds the type, but one holds the same base material, say Polymaker LW-ASA in an `ASA-Aero` tray for a Generic ASA slot; both names are shown), none or unknown, with a headline and warnings. `check_printer_match` runs that check on a Bambu printer profile instead of refusing. Needs the OrcaSlicer MCP build with the `bambu_ams` capability; status, waiting and print history on Bambu printers are still to come.
- `get_filament_slots`: every filament slot of the project in one read, numbered as the sidebar numbers them: its preset, filament type, vendor, colour, whether that preset is open in the Filament tab with unsaved edits, and which other slots share the preset. Needs the OrcaSlicer MCP build with the `filament_slots` capability.
- `set_filament_slot_preset(slot, name, keep_colour)`: point one slot at a preset, leaving the other slots alone and never dropping an unsaved edit (on a one-filament project, where the slot is the Filament tab's preset, it refuses while there are unsaved edits); the slot keeps its colour unless `keep_colour` is false, which sets the preset's own default colour.
- `set_config` takes `filament_slot`: the per-filament keys go to that slot's preset instead of whichever preset is open in the Filament tab. `save_preset` takes `filament_slot` too: it saves from that slot's preset and points only that slot at the saved name, where OrcaSlicer's own Save As moves every slot that used the old preset (`slots_kept` says which were put back). Both are refused, with nothing changed, when the Filament tab holds unsaved edits to a different preset (`filament_edits_unsaved`, with the keys and what to do).
- `get_config` takes `by_slot`: a per-filament setting comes back as one value per slot, `{"1": "70", "2": "100"}`, instead of the merged `"70,100"` text. Works on any build.

### Fixed
- Saving a filament preset with `save_preset` or `edit_preset` no longer waits behind OrcaSlicer 2.5's "Temperature Safety Check". OrcaSlicer asks before it saves a filament preset whose first-layer temperature is more than 30 °C from the other layers' for the nozzle, or 15 °C for a plate type, and the assistant could not answer that dialog: the save timed out and the dialog stayed open. With the OrcaSlicer MCP build that has the `preset_temperature_check` capability, the save is refused instead, with each pair that is too far apart, and nothing is saved; `allow_temperature_pairs=true` saves it as it is, the dialog's Continue, once the user agrees. On earlier 2.5 builds a filament save that times out now says to look for that dialog.

## [0.1.24] - 2026-11-13
The OrcaSlicer MCP plugin 0.2.3 for stock OrcaSlicer 2.5 is attached below (`orcaslicer_mcp_plugin.py`) and on [OrcaCloud](https://cloud.orcaslicer.com/p/16d116a6bc41), which keeps it up to date.

### Added
- The OrcaSlicer MCP plugin's panel follows OrcaSlicer's language, in each of OrcaSlicer's 22 languages, like the OrcaSlicer MCP build's panel since v2.5.0-mcp.17. Setting names and their choices come from OrcaSlicer's own translation file, so a row uses the same words as the sidebar. The plugin's own words, its setup view included, were machine-translated. Catalan, Basque, Hungarian and Traditional Chinese are recognised too, although OrcaSlicer 2.5 reports them to plugins as English or Simplified Chinese.
- The plugin takes `POST /api/v1/activity/note` with `"kind": "note"`, as the build does: the assistant's own words, a plain row in the current group with no undo.

### Changed
- The README and the OrcaSlicer MCP plugin's setup (0.2.2) say that every AI app needs uv installed once, the Claude Desktop extension included: it runs the connector with uv too. The extension's description now also lists the plugin on stock OrcaSlicer 2.5, not only the OrcaSlicer MCP build.
- orcaslicer-mcp now tells you when your plugin is older than 0.2.3.

### Fixed
- An OrcaSlicer that starts on its Home tab has no OrcaSlicer MCP plugin panel until you click Prepare, so every request timed out and the assistant was told a dialog might be open. The plugin now answers `panel_not_open`, and the assistant asks you to click Prepare.
- The plugin's setup for the ChatGPT desktop app said to paste `uvx orcaslicer-mcp`, but ChatGPT's form has separate boxes for the command and its arguments, and the menu path had changed. It now names the path (Settings › Plugins › Add › Add MCP server) and shows each box on its own line with its own Copy: `uvx` in Command to launch, `orcaslicer-mcp` in Arguments. In a translated OrcaSlicer, the path and the box names use the ChatGPT app's own words in that language.
- The plugin's setup has a link to install uv, which every AI app needs.

## [0.1.23] - 2026-11-11
The OrcaSlicer MCP plugin 0.2.1 for stock OrcaSlicer 2.5 is attached below (`orcaslicer_mcp_plugin.py`) and on [OrcaCloud](https://cloud.orcaslicer.com/p/16d116a6bc41), which keeps it up to date.

### Added
- No token needed on the same computer: with `ORCA_API_TOKEN` unset, the connector finds OrcaSlicer by itself, through the file the OrcaSlicer MCP plugin (0.2.1 and later) writes while it runs, or the OrcaSlicer MCP build's own settings. For OrcaSlicer's own settings folder (`--datadir`) set `ORCA_DATADIR`; for OrcaSlicer on another computer set `ORCA_API_URL` and `ORCA_API_TOKEN` as before. The Claude Desktop extension's token field is now optional.
- `get_config` with `keys` also says what each setting means, with its unit, range and choices, from OrcaSlicer's own tooltips.
- `set_config` and `apply_and_slice` take `null` for a key to put it back to the value saved in its preset, one setting at a time. Script, G-code and printer-connection settings are never put back this way.
- The server now sends instructions with its handshake: where to start, which tools explain a setting, how to check a result.
- `check_printer_match` returns `fix` when the profile's speeds or accelerations go past the printer's Klipper limits: the `set_config` changes that lower just those settings to the limits, for the assistant to offer and apply if you agree. Nozzle size, bed size, temperatures and retraction stay advice, since which side is wrong is your call.
- `check_profile_physics` reads the printer's start and end G-code (`gcode_scripts`): a fixed temperature where a placeholder would follow the filament, relative positioning left on, no homing, heaters never turned off, moves outside the printable area. Warnings only; scripts that call a printer macro are trusted to do that work.
- `describe_plate` says whether the G-code was sliced with the settings that are set now (`settings_check`), naming any setting the slice used instead, such as a plate's own bed type, print sequence or spiral vase.
- With enough recorded prints, `get_slice_status` and `slice_and_wait` give a `forecast`: how long the slice will really take on your printer from start to finish, from how past prints compared with their estimates, per OrcaSlicer version when there are enough of them. `save_gcode` now records the OrcaSlicer version with each slice for this.
- An agent skill (`skills/orcaslicer-mcp`) that teaches Claude Code, Codex and other agents how to work with these tools. In Claude Code: `/plugin install orcaslicer-mcp --marketplace MaxEllis/orcaslicer-mcp`. Instructions only; it runs no code.
- The OrcaSlicer MCP plugin helps you connect your AI app. Its panel does it on first run, and the Config tab after that: pick Claude, OpenAI, Cursor, VS Code or another app, then one button per step (the Claude app's extension to download, a command to copy for Claude Code or Codex, a one-click install for Cursor and VS Code). None of it holds the token. When the app first reaches OrcaSlicer, the panel says it is connected.
- In the OrcaSlicer MCP build, which has the connection and the panel built in, the plugin stays off: one panel, one server, and no permission prompt. Its Config tab says so.
- The plugin's Config tab says whether an AI app has connected, which one and when, with the assistant access switch beside it. orcaslicer-mcp tells the plugin its AI app's name for this.

### Changed
- Arguments sent under a name close to a tool's own are read as that argument instead of being dropped or failing the call: `key` for `keys`, `settings` for `changes`, `id` for `object_id`, camelCase names, a single value where a list is expected, and a comma-separated list of keys.
- `search_settings` ranks everyday settings above advanced ones when they match equally well, so "infill density" finds `sparse_infill_density` first.
- The plugin keeps loading on an OrcaSlicer build that refuses one of its permission requests; OrcaSlicer then asks the first time it is needed. On a build too old for the plugin, it says which OrcaSlicer it needs.
- The Claude Desktop extension's token and address fields are optional and empty by default: the connector finds OrcaSlicer's address and port by itself. A new **OrcaSlicer settings folder** field is for an OrcaSlicer started with its own settings folder.
- The README's setup for VS Code: its `mcp.json` takes the entry under `servers`, not `mcpServers`.
- orcaslicer-mcp now tells you when your plugin is older than 0.2.1.

## [0.1.22] - 2026-11-11

### Added
- With the OrcaSlicer MCP plugin 0.2.0 on stock OrcaSlicer, the assistant reads the slice you make with Slice plate. `get_slice_status` gives its time, filament, cost and layer count, and says when a later change has made it out of date. `get_gcode`, `describe_plate` and `save_gcode` work on it too, so the print is recorded for `recall_prints`. The plugin still can't start a slice, see OrcaSlicer's slice warnings or give the per-feature breakdown.

### Changed
- On the plugin, a tool that needs a slice asks you to press Slice plate in OrcaSlicer instead of naming `slice_and_wait`, which the plugin can't run. `get_slice_warnings` says the plugin can't see warnings, so an empty list means nothing there, and `get_slice_breakdown` says the breakdown needs the OrcaSlicer MCP build.
- orcaslicer-mcp now tells you when your plugin is older than 0.2.0.

## [0.1.21] - 2026-11-10

### Added
- Tool replies now tell the assistant what you changed by hand in OrcaSlicer since its last action: settings, presets, objects and plates, and the panel's own undos. The reply carries a `user_changes` note (the first 8 changes, then a count by group) and the full list, so the assistant works from your values instead of overwriting them. It needs an OrcaSlicer MCP build with the change journal (v2.5.0-mcp.16 and later); older builds are left alone.
- Works with the OrcaSlicer MCP plugin for stock OrcaSlicer 2.5. `get_status` reports `backend: "plugin"` with a note on what the plugin can do; a tool the plugin can't serve says how to do it by hand and that the OrcaSlicer MCP build can do it, and `apply_and_slice`, `edit_preset` and the compare tools refuse up front so nothing is left half done.
- An OrcaSlicer MCP plugin installed from a downloaded file never updates itself, so orcaslicer-mcp tells you: when the plugin is older than the one released with this version, `get_status` carries `plugin_update`, and the first tool reply of the session carries it once, with where to download the new one (or install it from OrcaCloud to get updates automatically). The plugin itself asks for no network access. The OrcaSlicer MCP build is never flagged; it has its own update notice.
- `select_preset` takes an optional 1-based `slot` for a filament preset, to set the filament of one extruder or tool on a multi-filament printer without touching the others or discarding unsaved edits. It needs an OrcaSlicer MCP build that lists `preset_slot` (v2.5.0-mcp.16 and later) and refuses on older ones.

### Changed
- The outcome store and the `save_gcode` G-code folder no longer pick up a shared folder under `~/projects` when one exists. They use `PRINT_OUTCOMES_DIR` when it is set, otherwise `~/.orcaslicer-mcp/outcomes/` (store) and `~/.orcaslicer-mcp/` (G-code). To keep an existing store in another folder, set `PRINT_OUTCOMES_DIR` to it, for the klipper-mcp recorder too.
- `describe_setting` and `search_settings` now know OrcaSlicer 2.5.0's settings (992, up from 807, built from the 2.4.2 and 2.5.0 sources). A setting added in 2.5 carries `since: "2.5.0"`; `silent_mode`, which 2.5 removed, carries `removed_in: "2.5.0"`.
- On the OrcaSlicer MCP plugin, a refused action now says what failed and how to do it by hand in OrcaSlicer (press Slice plate and read out the time, Arrange all objects in the toolbar, the save icon beside the preset name), and mentions the OrcaSlicer MCP build after that. `compare_slices` and `compare_settings` refuse up front there, so nothing changes.
- Refusals made by the connector itself (locked settings, out-of-range values, and on the plugin the actions it can't do) now show in OrcaSlicer's MCP panel too, on builds that have the panel's note route.
- `get_status` says in words whether it reached the OrcaSlicer MCP build or the plugin, and leaves OrcaSlicer's internal nozzle bookkeeping out of the list of modified settings. `get_printer_status` says first when it falls back to the printer you last used, which may not be the one selected in OrcaSlicer now.
- Every tool's description now opens with one whole sentence that says when to pick it over a similar tool, and the text below it no longer repeats that sentence.
- `add_plate` notes that on OrcaSlicer 2.5, adding a 2nd, 5th or 10th plate rearranges the plate grid and clears every plate's slice result.

### Fixed
- A setting can no longer be changed to a value outside its allowed range. A layer height of -1 used to be applied, and the next slice ran out of memory and closed OrcaSlicer with the project unsaved; 250% infill went through without a word. `set_config`, `apply_and_slice`, `set_object_config`, `edit_preset` and the compare tools now check every value against the setting's own limits first (a percentage by its number, each entry of a list on its own) and refuse with the allowed range. Writing back a value a setting already has is never refused, even when a shipped profile holds it outside the range. When OrcaSlicer refuses a value itself, its own sentence is passed on.
- When OrcaSlicer can't be reached, the reply asks whether it is open with Preferences > Remote API turned on, or, for the OrcaSlicer MCP plugin, whether the plugin is turned on. When OrcaSlicer runs on another computer, it also asks whether that computer is on and awake. A timeout no longer ends in an empty colon: it says how long it waited and that a dialog may be open in the OrcaSlicer window.
- One tick is now enough to let the assistant change scripts, custom G-code and printer connection settings: "Allow script, G-code and connection edits" in the OrcaSlicer MCP build's Preferences > Remote API. The connector reads that box from OrcaSlicer, at most a few seconds old, and refuses when it is off or can't be read. A refusal gives one plain step: tick the box, then ask again, or make the change yourself in OrcaSlicer. On the OrcaSlicer MCP plugin, which never allows these settings, it only asks you to make the change in the OrcaSlicer window. `ORCA_MCP_ALLOW_KEYS` still works as an advanced option.
- A slice result that a later change made out of date is marked as such ("out of date, slice again") by `get_slice_status`, `get_slice_warnings`, `get_slice_breakdown` and `diagnose_plate`.
- A slice OrcaSlicer won't start now says why, in OrcaSlicer's own words. Without a reason from OrcaSlicer, the reply lists what the profile check finds (a layer height too large for the nozzle, say) instead of "object outside bed". An empty plate says to load a model first, also on `render_plate`, `describe_plate` and the G-code tools.
- `compare_slices` no longer fills OrcaSlicer's MCP panel with its trial runs: one line such as "Compared 3 options · nothing kept" sums the comparison up. It recommends and stars a variant only when it is at least as good as every other on time, filament and warnings; when they trade off it says "No clear winner: X is fastest, Y is lightest." The differences now add up with the rounded minutes and grams the table shows. If a comparison can't put your settings back, the reply and the panel say which ones.
- Slice warnings carry OrcaSlicer's sentence next to the code, and the level as a word.
- Every error has a readable explanation, and none sends you to an HTTP route; a missing file given as a bare name asks for the full path.

### Security
- Ready for OrcaSlicer 2.5: the config guard also refuses `slicing_pipeline_plugin` (Python plugins that run at each slicing step), `plugins` (the plugins a preset brings with it) and `*_gcodes` lists of G-code such as `imex_mode_gcodes`. `high_precision_gcode`, a 2.5 on/off setting, stays writable.

## [0.1.20] - 2026-11-09

### Added
- Project files, for fork v2.4.2-mcp.15 and later. `save_project` writes the project `.3mf` as File > Save / Save As does, with no dialog, and refuses to replace an existing file unless `overwrite` is passed. `open_project` opens a saved project with its plates, objects, per-object and per-plate settings, slot presets and colours (`load_model` only imports geometry); `open_recent_project` opens an entry of File > Recent by index; `new_project` starts over, with an optional `name` for the window title. All three take `if_unsaved` (`fail` by default, `save`, `discard`) to answer OrcaSlicer's unsaved-changes prompt from the call, and the open tools take `presets` (`project` or `keep_current`). `get_project` reports the project's name, path, dirty flag and modified preset groups; `get_status` reports `project` as `{name, path, dirty}` on those builds.
- `list_recent_projects` shows File > Recent with whether each file still exists, and `forget_recent_project` takes an entry (or the whole list) off it. `browse_projects` lists the `.3mf` files in a folder on the OrcaSlicer host. `move_project_file` renames or moves a project file, and the window follows when it is the open project; `delete_project_file` removes one from disk (permanently) and from the recent list, never the open project's own file.
- OrcaSlicer guards the project routes with two Preferences > Remote API boxes, both off by default. Without "Allow deleting and replacing project files" it refuses `delete_project_file` and any save or move that would replace an existing file. Without "Allow script, G-code and connection edits" it refuses to open a project that brings its own scripts, custom G-code or printer connection, and lists those keys. The tools pass the reason, the file and the keys back with a hint, and `save_project` and `move_project_file` are marked destructive because `overwrite` can replace a file.

### Fixed
- `load_model` on a path that does not exist now says `file_not_found` with the path, instead of claiming the build lacks model loading. Works with older fork builds too.

## [0.1.19] - 2026-11-03

### Added
- CANVAS slot tools for the Elegoo Centauri Carbon 2: `get_printer_slots` (trays, status, material, colour), `get_filament_slot_mapping` and `set_filament_slot_mapping` (which tray each filament of the current plate prints from). Setting a mapping starts nothing; the Send dialog opens pre-filled. Needs fork v2.4.2-mcp.14 or later.

## [0.1.18] - 2026-11-02

### Fixed
- Claude Desktop could not connect: it showed the extension as failed with "connection is serving the 2026-07-28 protocol; the initialize handshake is not accepted" and no tools. The MCP SDK (2.2 and later) lets the first request on a stdio connection lock its protocol era, and a client that opens with a `server/discover` probe and then sends `initialize` on the same connection was refused. On stdio the server now answers a `server/discover` probe that arrives before any other request with METHOD_NOT_FOUND (-32601), the reply the specification tells clients to treat as "fall back to the handshake", so `initialize` opens the connection. Clients that only speak the handshake are unaffected, and clients in auto mode fall back as designed. Set `ORCA_MCP_PROTOCOL=auto` to hand the connection to the SDK untouched. Verified against Claude Desktop 4.4212 on macOS: its era probe, chat connection and local agent mode connection all complete the handshake and list the tools.
- `anyio` is now a declared dependency (the stdio transport imports it directly; before it arrived only through the MCP SDK).

## [0.1.17] - 2026-10-12

### Added
- Plate tools for fork v2.4.2-mcp.13 and later. `list_plates` shows each plate's index, name, lock state, build volume and objects, and which one is current. `add_plate` adds one. `select_plate` picks the plate that `slice_and_wait`, `get_gcode`, `render_plate` and `get_job_status` act on. `delete_plate` removes a plate; its objects are parked beside the bed and the reply counts them. `move_object_to_plate` keeps an object's position relative to its old plate, and an object parked beside the bed lands at the plate's centre. `list_objects` shows each object's plate. On an older build these tools say they need fork v2.4.2-mcp.13 or later.
- `arrange_plate` warns that with several plates it packs every unlocked plate together again.
- `transform_object` says that on fork v2.4.2-mcp.13 an object moved by coordinates joins the plate it lands on.

## [0.1.16] - 2026-10-09

### Fixed
- The printer tools no longer fail with a raw error when a printer sends a reply that can't be decoded, redirects, or isn't the JSON object expected at the top level. They return `protocol_error` (or `not_reachable` when the connection drops) with a message.
- An `ORCA_PRINTER_API_KEY` pasted with a trailing space or newline is trimmed before it is sent.
- A user name and password in the printer address are dropped from the address a message can echo, as a second guard after the existing redaction.
- Looking for the printer no longer leaves an HTTP client open when the call is cancelled or fails in an unexpected way.
- A damaged or hand-edited `printer.json` can no longer break a status call: the remembered address, protocol and time are checked when read, and the file is written in one atomic step.
- Errors from finding the printer (a bad `ORCA_PRINTER_URL`, a profile with no address, an unsupported connection type, OrcaSlicer not reachable) now carry a `printer` entry saying where the address came from.
- A printer address with port 0 is refused as not valid.
- While Klipper is starting up (right after a firmware restart, say), `get_printer_status` no longer shows 0 °C for the heaters and a warning count. It leaves the temperatures empty, because the sensors haven't been read yet, and the headline is "Klipper is starting up; check again in a minute."
- Console errors in the status lose Klipper's repeated restart instructions, as other Klipper messages already did, and an error that repeats shows once (its newest occurrence) instead of filling the list of five recent errors. A job's error message is trimmed the same way.
- Layer numbers in the job are always whole numbers or null. Text or a non-finite number from a printer no longer reaches the result.
- With OctoPrint, a job request that fails (or answers with something that isn't JSON) no longer fails the whole status. The status comes back with a note that the current job couldn't be read.
- `wait_for_printer` treats a job that completes successfully while it waits for `printing` or `first_layer_done` as met, instead of stopping with "The job ended before ...". A one-layer or very short print passes both points before the next poll. A cancelled or errored job still stops the wait early, and so does a job that was already complete when the wait began.
- `wait_for_printer` no longer sends a progress notification carrying an old status after a poll fails. The next notification comes with the next good poll.
- A hand-edited `printer.json` holding a remembered time no calendar can hold (`-1e300`, say) no longer puts a 310-character age in the "remembered printer" note. It reads "remembered earlier".
- `list_print_history` shows the failure reason already stored for a job, so the reply matches the row it just synced. Before, a reason read from the printer's console this time could differ from the one in the store.
- `list_print_history` reads a job's failure reason from the console the way the status does: Klipper's repeated restart instructions are cut, and a line with nothing else in it ("Printer is shutdown") is skipped. Before, that line could be stored as the reason, and a stored reason is not replaced later.
- `list_print_history` says what to do when the printer answers 404 for its history list: Moonraker versions before 0.9 need a `[history]` section in `moonraker.conf`, so add one and restart Moonraker. Before, it reported only the HTTP status.
- `list_print_history` copies jobs into the outcome store oldest first, as the always-on recorder does, so when one file was printed twice against a single saved slice, the earlier print claims it. The store writes also run off the event loop, so a busy store no longer stalls the server.
- When OrcaSlicer can't be read because of a setting other than the token (an invalid `ORCA_API_TIMEOUT`, say), the printer tools no longer tell you to check `ORCA_API_TOKEN`. The hint points at the server's OrcaSlicer settings (`ORCA_API_URL`, `ORCA_API_TIMEOUT`) and `detail` names the problem.
- `check_printer_match` no longer reports Klipper's `[firmware_retraction]` section as `absent` when retraction is off and Klipper's settings were never read. The printer side is empty then.
- `check_printer_match` says when OrcaSlicer didn't report the bed type, and its bed check is `unknown`, not `ok`, when the printable height or Klipper's Z travel wasn't available. The message says the height wasn't checked.
- `save_gcode` keeps the saved G-code file and returns `outcome_error` when recording the slice fails for any reason, not only a database or file error.
- An `ORCA_PRINTER_API_KEY` holding a character that can't be sent in an HTTP header (a curly quote pasted from a word processor, say) is refused with a `not_configured` message instead of a raw encoding error. The message never repeats the key.
- An OctoPrint reply that isn't a JSON object, and a Moonraker file-info reply that is the JSON value `null`, now return `protocol_error`. The first used to fail with a raw error (or, for `null`, read as offline or no job) and the second read as "no such file".
- With OctoPrint, a refused API key on the job request is reported as `auth_required` or `auth_rejected` like any other request, instead of becoming a note.
- The outcome store closes its database connection when it can't set up or upgrade the store (a locked or damaged file), instead of leaving it open for the caller.
- A printer error's extra detail can no longer replace its `error`, `message` or `hint`.
- A console error that is only Klipper's "Printer is shutdown" or "Printer is halted" line is no longer listed as a problem. The shutdown reason is reported on its own.

### Changed
- `get_printer_status` and `wait_for_printer` make one request fewer to a Klipper printer: the answer from the request that found the printer is reused once. Later polls of `wait_for_printer` still ask again.
- A printer found through OrcaSlicer's profile is tried first with the protocol that answered last time at the same address, so a new session makes fewer attempts.
- When a print that is already running is waiting for its heaters (OctoPrint reheating mid-print, or OctoPrint's first heat-up, which also shows file progress), the headline reads "Printing benchy: 28%, waiting for the heaters.", with the time left when it is known. A Klipper job's first heat-up still reads "Heating up to print benchy."
- The headline says "almost done" when no time is left or progress is 100 %, instead of "about 1 min left".
- `connected` in the status is false when the state is `offline`, OctoPrint keeps the job for a cancelled or errored print as Klipper does, and `headline` comes first in the result.
- The `list_print_history` summary reads "Your last 3 successful prints with an estimate ran 9% longer than estimated (median)." (or `shorter`, or `matched the estimate`). It no longer names OrcaSlicer, because the estimate can come from another slicer's file.
- `ended_at` in `list_print_history` carries the time zone offset, for example `2030-01-15T14:23+12:00`.
- `~/.orcaslicer-mcp/printer.json` is now created readable only by its owner.

### Documentation
- The README and the `wait_for_printer` text say that with OctoPrint, waiting for `printing` means waiting until the job is running with the heaters at temperature, because OctoPrint reports no extrusion.
- The README says the outcome store keeps a fingerprint of the objects' names and sizes, not their geometry, and `save_gcode` describes it the same way.
- The README says a finished print's row deleted from the store by hand comes back, without its saved slice settings, while its job is still in Moonraker's history, and that deleting the job there too drops it for good.
- The printer address setting in the Claude Desktop extension and the registry listing says a password in the address is stored as an ordinary setting, so the API key is the better choice.
- `list_print_history` and `recall_prints` say "the slicer's estimate", because an estimate can come from another slicer's file.
- The marketplace listing (`lhm.plugin.json`) now takes its tool descriptions from the live server with `scripts/gen_lhm_tools.py`, and a test keeps it from going stale. Before, many descriptions stopped mid-sentence or carried old `[needs M4b]` tags.

## [0.1.15] - 2026-10-09

### Added
- Printer feedback: `get_printer_status`, `wait_for_printer`, `list_print_history` and `check_printer_match`. The server finds the printer from OrcaSlicer's active printer profile, or `ORCA_PRINTER_URL`. Klipper (Moonraker) printers get all four tools; OctoPrint gets status and waiting, with `ORCA_PRINTER_API_KEY` when it needs a key. Read-only: no command, G-code or file is ever sent to the printer. The server only reads, with HTTP GET requests (carrying the API key, or the user name and password from `ORCA_PRINTER_URL`, when you set them). `wait_for_printer` also returns early when the job ends before the point it waits for, as when a print is cancelled while it heats. Requested in orcaslicer-mcp#12.
- The Claude Desktop extension has settings for the printer address, its API key and the printer name used in the outcome store.

### Changed
- `httpx` is now capped below 1.0. Its 1.0 pre-releases have no `AsyncClient`, so an install that resolved one (for example with pre-releases allowed) failed on the first printer or slicer call; the cap keeps a future 1.0 release from doing the same to fresh installs.
- The outcome store is now owned by this project (klipper-mcp carries a copy) and works without klipper-mcp. `save_gcode` always records the slice, with OrcaSlicer's time and filament estimates, and `list_print_history` adds finished prints with the printer's failure reason. New default location `~/.orcaslicer-mcp/outcomes/`. The store upgrades itself in place by adding columns, so older versions can still read it.
- `recall_prints` rows include the failure reason and OrcaSlicer's estimates. `duration_s` is Moonraker's whole job time, including heating up and any pauses, so beside the estimates it overstates an overrun; `list_print_history` compares the print time, which leaves both out, with the estimate. `filament_g` is the G-code file's own estimate, because printers report filament in millimetres, not grams.
- A new outcome store's `printer_id` column defaults to `unknown`, the same value the store module uses when a caller passes no printer. Existing stores keep the default they were created with; every write names its printer anyway.

### Security
- `get_preset_config` could show part of a printer password to the model. It removed `user:password@` from `print_host` and `print_host_webui` only when the password had no `/` or space in it, so `http://user:pa/ss@host` came back unchanged. It now hides everything between the scheme and the last `@`, whatever the password contains. A URL with an `@` in its path also loses the part before that `@`, because it looks the same as a password with a `/` in it.

### Documentation
- README: the introduction, security notes and privacy policy now name the printer as the one other place the server connects to, read-only, and the data-retention entry lists the outcome store and `printer.json`.
- `docs/order-of-operations.md` explains why filament edits come before print edits on a multi-filament setup (per-filament keys are read merged but written to the one preset open in the Filament tab, and `select_preset` discards unsaved overrides in every group), with a recipe for setting up a multi-material plate. Linked from the README and from the `set_config` and `select_preset` docstrings.
- `set_config` warns against echoing a `get_config` value back for a per-filament setting (`hot_plate_temp`, `fan_min_speed`, `nozzle_temperature_initial_layer`, ...). `get_config` reports those merged across every loaded filament, while the write lands in the one filament preset open in the Filament tab, so the merged string corrupted that preset's list. Reported by @RoyPorter (orcaslicer-mcp#9); OrcaSlicer MCP v2.4.2-mcp.12 rejects the merged shape instead of storing it.
- `select_preset` says that it discards unsaved overrides in every preset group, not only the one being switched, and that newer slicer builds list the affected groups as `discarded_changes`. Reported by @RoyPorter (orcaslicer-mcp#10).

### Known issues
- `compare_settings` and `compare_slices` snapshot with `get_config` and restore with `set_config`, so for a per-filament key on a multi-filament setup the restore writes back the merged list. That never actually restored the original: it corrupted the filament preset the same way a manual write did. On OrcaSlicer MCP v2.4.2-mcp.12 and later the restore now fails loudly with `per_filament_length_mismatch` instead of corrupting silently; re-select the filament preset to recover. Making the snapshot/restore path per-filament aware is still to do.

## [0.1.14] - 2026-10-02

### Security
- Config writes that would change a key able to run code or drive the printer are refused with `blocked_by_local_policy` before they reach OrcaSlicer. They are `post_process`, every `*_gcode` template, `printer_model`, `printer_technology`, `filename_format`, and the printer connection settings (`print_host`, `print_host_webui`, `printhost_*`, `host_type`, `bbl_use_printhost`, `printer_agent`, `flashforge_serial_number`). This covers `set_config`, `apply_and_slice`, `edit_preset`, `compare_settings`, `compare_slices` and `set_object_config`. The fork's `PUT /config` accepts any preset key and `post_process` runs through the shell after export, so an assistant steered by injected text could otherwise save a command into a user preset. Thanks to @ferpa for the original change.
- Writing back an unchanged value passes, compared the way the fork serializes it (`true` is `1`). The host address and credentials are always refused, because OrcaSlicer never reports their current value.
- `edit_preset` checks against the named preset before selecting it, so a refusal no longer discards unsaved overrides. The `compare_*` snapshot restores skip the check, since they write back values just read from OrcaSlicer, so a restore never waits on an extra read.
- `ORCA_MCP_ALLOW_KEYS` opts specific keys back in. Wildcards such as `*_gcode` work, and the Claude Desktop extension exposes it as a setting. Names that match no protected key are logged at startup.
- `get_preset_config` hides `printhost_apikey`, `printhost_user` and `printhost_password`, and removes any `user:password@` from `print_host` and `print_host_webui`. The fork returns preset config unfiltered, unlike `GET /config`. Writes carrying the `<redacted>` placeholder are refused, so a round-tripped preset never overwrites a real credential.
- OrcaSlicer MCP v2.4.2-mcp.10 and later enforce the same rule inside the slicer, through Preferences → Remote API → Allow script, G-code and connection edits (off by default). When the slicer refuses a key, the tool reply carries a `hint` that says where the switch is.

### Documentation
- README: Windows fallback for `uvx` failing on `pywin32` with a sharing violation (pip venv, which retries). The same testing found the upstream 2.4.x "Profile syncing change" dialog and the fork's startup `remove_all` crash on a locked AppData datadir.

## [0.1.13] - 2026-09-17

### Fixed
- `edit_preset` on a filament preset is no longer blocked by the print preset that happens to be selected. The physics gate now knows which preset layers feed each check: for filament edits, checks that mix filament inputs with print speeds (`flow_ceiling`, `temp_vs_flow`) are returned as `cross_layer_warnings` instead of `physics_blocked`, while filament-only checks (fan range, first-layer temperature) still block. Print-preset edits are unchanged and still block on the flow ceiling of the selected filament. Reported via a fork by shadow-fight.
- `save_preset` docstring now says when `detach=True` is needed (creating a filament preset for a different material than the selected one).

## [0.1.12] - 2026-09-10

### Fixed
- Claude Desktop extension launcher (`.mcpb`): the Node launcher now pipes stdio to the `uvx` child instead of inheriting it, so no console is created for the server on Windows. Addresses issue #3, where the Python server started with `sys.stdin` unset under Claude Desktop on Windows 11 and disconnected right after the handshake. The report could not be reproduced on Windows 10 (including with Windows Terminal as the default terminal), so this is a hardening of the launch path rather than a confirmed root-cause fix; the piped launcher was verified end to end on Windows 10 and Linux. Python package unchanged.

## [0.1.11] - 2026-09-09

### Added
- `describe_plate`: per-object plate facts from the last slice's G-code (orientation class, first-layer footprint islands, overhang bands, support placement and contact zones, seam side versus `seam_position`) with a server-written summary sentence. Answers "how is it standing, where did support go, where is the seam" without a picture.

### Changed
- Moved to the mcp SDK 2.x (`mcp>=2.2,<3`); the 0.1.8 `mcp<2` pin is gone. `FastMCP` became `MCPServer` and the server now reports its own package version in the initialize handshake. No tool, prompt, or resource changed. Cold `uvx --isolated` install verified: 43 tools at the time, 44 with describe_plate, 3 prompts, 1 resource, 2 templates, all tools annotated.

## [0.1.10] - 2026-09-07

### Added
- `save_gcode`: save the last slice's G-code into a local gcode folder and record the slice (model, geometry, settings) so the companion klipper-mcp can join the real print result back to it.
- `recall_prints`: before slicing, see how past prints of the current model went (result, your verdict, the settings used). Inert without the klipper-mcp outcome store.
- Slicing prompts now consult `recall_prints` first and end with `save_gcode`.

## [0.1.9] - 2026-08-07
### Added
- `compare_slices`: slice the current plate across up to eight named variant sweeps and get one comparison back. Every delta, percentage, and rounding is computed server-side, with an explicit baseline and an honest recommendation that does not force a winner when no variant clearly dominates.

### Changed
- `search_settings` now matches on tokens, so multi-word queries like "layer height" resolve reliably.
- Public docs and metadata reframed around understanding your slicer, not just driving it.

### Fixed
- `edit_preset` runs the physics gate and blocks only newly introduced violations, so a pre-existing quirk in a preset no longer stops an otherwise valid edit.

## [0.1.8] - 2026-07-31
### Fixed
- Pin `mcp<2`. The mcp 2.0.0 release removed `mcp.server.fastmcp`, which broke every cold `uvx orcaslicer-mcp` install.

## [0.1.7] - 2026-07-31
### Added
- MCP prompts: `slice-a-model`, `optimize-print-time`, and `edit-preset-safely`.
- MCP resources: `orca://knowledge` (indexed slicing knowledge) and `orca://setting/{key}` (per-setting reference).

## [0.1.6] - 2026-07-28
### Added
- `render_plate`: return the plate as a PNG in either the editor or the preview view, with plate or object framing. A client can now see orientation, plate contact, and toolpaths directly.

## [0.1.5] - 2026-07-22
### Added
- Tool annotations (title plus read-only and destructive hints) on every tool.
- Privacy Policy section, required for the Claude Desktop extension directory.
- Icon for the Claude Desktop extension.

## [0.1.4] - 2026-07-22
### Fixed
- Match the MCP registry namespace casing (`io.github.MaxEllis`).

## [0.1.3] - 2026-07-22
### Added
- MCP registry ownership marker and a privacy section in the README.
- Documented the STEP and STP model formats.

## [0.1.2] - 2026-07-22
### Added
- Load STEP and STP models, with an extended timeout for tessellation.
- Claude Desktop extension (`.mcpb`) for one-click install with no config-file editing.

## [0.1.1] - 2026-07-21
### Fixed
- Hide the console window that GUI MCP clients pop on Windows. It belongs to the `uv` launcher, not OrcaSlicer, and the stdio transport is unaffected.

## [0.1.0] - 2026-07-20
### Added
- First public release. Tools for slicing (`slice`, `slice_and_wait`, `cancel_slice`), printer and preset control, per-feature slice breakdown analytics (`get_slice_breakdown`), and a settings knowledge base with physics checks. Licensed AGPL-3.0-only.

[Unreleased]: https://github.com/maxellis/orcaslicer-mcp/compare/v0.1.9...HEAD
[0.1.9]: https://github.com/maxellis/orcaslicer-mcp/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/maxellis/orcaslicer-mcp/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/maxellis/orcaslicer-mcp/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/maxellis/orcaslicer-mcp/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/maxellis/orcaslicer-mcp/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/maxellis/orcaslicer-mcp/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/maxellis/orcaslicer-mcp/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/maxellis/orcaslicer-mcp/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/maxellis/orcaslicer-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/maxellis/orcaslicer-mcp/releases/tag/v0.1.0
