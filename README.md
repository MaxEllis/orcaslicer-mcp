<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MaxEllis/orcaslicer-mcp/main/docs/images/logo-dark.svg">
    <img alt="OrcaSlicer MCP" src="https://raw.githubusercontent.com/MaxEllis/orcaslicer-mcp/main/docs/images/logo-light.svg" width="140">
  </picture>
</p>

<h3 align="center">Let an assistant work the slicer with you: load, tune, slice, and explain the result.</h3>

<p align="center">
  <a href="https://pypi.org/project/orcaslicer-mcp/"><img alt="PyPI" src="https://img.shields.io/pypi/v/orcaslicer-mcp?color=009789"></a>
  <a href="https://pypi.org/project/orcaslicer-mcp/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/orcaslicer-mcp?color=555"></a>
  <a href="https://github.com/MaxEllis/orcaslicer-mcp/blob/main/LICENSE"><img alt="License: AGPL-3.0" src="https://img.shields.io/badge/license-AGPL--3.0-555"></a>
  <a href="https://lobehub.com/mcp/maxellis-orcaslicer-mcp"><img alt="MCP Badge" src="https://lobehub.com/badge/mcp/maxellis-orcaslicer-mcp"></a>
</p>

<p align="center">
  <a href="#quickstart"><b>Quickstart</b></a> ·
  <a href="#what-it-can-do"><b>What it can do</b></a> ·
  <a href="https://github.com/MaxEllis/OrcaSlicer/releases/latest"><b>Get the OrcaSlicer build</b></a> ·
  <a href="https://github.com/MaxEllis/orcaslicer-mcp/issues"><b>Report a problem</b></a>
</p>

---

OrcaSlicer MCP connects an AI assistant to a real, running OrcaSlicer. It loads your models, picks presets, changes settings, slices, and reads the result back as numbers you can question: which feature takes the time, what a setting actually does, whether a profile asks more of your printer than it can give. Every change shows up in the OrcaSlicer window while you watch, and a panel beside the 3D view lists each one with a way to take it back, so the slicer stays yours and you learn it as you go.

<table>
  <tr>
    <td width="50%"><img src="https://raw.githubusercontent.com/MaxEllis/orcaslicer-mcp/main/docs/images/plate-editor.png" alt="The Stanford bunny on the build plate, as render_plate returns the editor view"></td>
    <td width="50%"><img src="https://raw.githubusercontent.com/MaxEllis/orcaslicer-mcp/main/docs/images/plate-preview.png" alt="The same bunny sliced with tree supports, toolpaths coloured by feature"></td>
  </tr>
  <tr>
    <td colspan="2" align="center"><sub>What the assistant sees through <code>render_plate</code>, before and after slicing.</sub></td>
  </tr>
</table>

It comes in two parts:

1. **OrcaSlicer, with a way in.** Stock OrcaSlicer has no way for another program to drive it. The **OrcaSlicer MCP build** of OrcaSlicer 2.5.0, from the [fork's releases page](https://github.com/MaxEllis/OrcaSlicer/releases/latest), adds a local API, off until you enable it and bound to your own computer. To try it without replacing your OrcaSlicer, the **Lite plugin** runs on stock OrcaSlicer 2.5 and covers settings and presets: see [Plugin or OrcaSlicer MCP build?](#plugin-or-orcaslicer-mcp-build)
2. **This package**, `orcaslicer-mcp`: an [MCP](https://modelcontextprotocol.io) server that turns that API into tools your assistant can call.

The package bundles no model. The model comes from your MCP client: if the client uses a hosted model, your conversation goes there as any chat does, while your models, profiles and G-code stay on the computer running OrcaSlicer. With a local model, nothing leaves your network.

## Quickstart

You need [uv](https://docs.astral.sh/uv/getting-started/installation/), which provides the `uvx` command that runs the server: `curl -LsSf https://astral.sh/uv/install.sh | sh` on macOS and Linux, or `irm https://astral.sh/uv/install.ps1 | iex` in PowerShell on Windows.

**1. Install the OrcaSlicer MCP build** from the [releases page](https://github.com/MaxEllis/OrcaSlicer/releases/latest) and launch it. It uses the same profile folder as OrcaSlicer, so your printers and presets are already there. On a fresh install, finish the setup wizard first; if a "Bambu Network Plug-in Required" dialog appears, click **Skip for Now**.

**2. Turn on the API.** Open **Preferences → Remote API** (Ctrl+P on Windows and Linux; on a Mac, the OrcaSlicer menu), tick **Enable Remote API** and copy the token.

**3. Connect your MCP client.**

- **Most MCP clients:** add the server to the client's MCP config file:

  ```json
  {
    "mcpServers": {
      "orcaslicer": {
        "command": "uvx",
        "args": ["orcaslicer-mcp"],
        "env": { "ORCA_API_TOKEN": "<token from Preferences>" }
      }
    }
  }
  ```

  `ORCA_API_URL` defaults to `http://127.0.0.1:13130`. Set it only if you changed the port, or if OrcaSlicer runs on another computer with LAN access turned on there.
- **Clients that install `.mcpb` extensions** (Claude Desktop): download `orcaslicer-mcp-<version>.mcpb` from this repository's [latest release](https://github.com/MaxEllis/orcaslicer-mcp/releases/latest) and open it, then paste the token into the extension's settings. Those clients rewrite their own config file and drop hand-added servers, so use the extension there.

**4. Ask for something.** Restart the client and try *"Load benchy.stl, slice it with the current profile and tell me the print time."*

<details>
<summary><b>If it doesn't start</b></summary>

- **Windows, "The process cannot access the file because it is being used by another process" while installing `pywin32`:** Windows Search or Defender opened a freshly written file mid-install, and uv does not retry. Install with pip instead, which does:

  ```powershell
  python -m venv "$env:USERPROFILE\.venvs\orcaslicer-mcp"
  & "$env:USERPROFILE\.venvs\orcaslicer-mcp\Scripts\python" -m pip install orcaslicer-mcp
  ```

  Then set `"command"` to `C:\Users\<you>\.venvs\orcaslicer-mcp\Scripts\orcaslicer-mcp.exe` with no `args`. Upgrade later with the same pip command plus `-U`.
- **macOS, a GUI client set up with the config file:** apps started from the Dock don't get your terminal's PATH, so `"command": "uvx"` can fail without a message. Run `which uvx` in Terminal and put the full path it prints (usually `~/.local/bin/uvx`) in `"command"`.
- **A client that probes the newer MCP protocol first:** the server answers every stdio connection with the classic `initialize` handshake, because some clients send `server/discover` and then `initialize` anyway, which the MCP SDK would refuse. Set `ORCA_MCP_PROTOCOL=auto` to let the client's first request choose instead.
- **Updates:** take new OrcaSlicer MCP builds from the [releases page](https://github.com/MaxEllis/OrcaSlicer/releases). The build checks there for you; stock OrcaSlicer's own updates would remove the API.

</details>

## Plugin or OrcaSlicer MCP build?

The Lite plugin is one file that runs inside stock OrcaSlicer 2.5, so you can try orcaslicer-mcp without installing another OrcaSlicer. It can only do what OrcaSlicer's plugin system allows, and that leaves out loading models, slicing and projects.

| | Lite plugin on stock OrcaSlicer 2.5 | OrcaSlicer MCP build |
|---|---|---|
| **Settings** for the whole plate | Read and change | Read and change |
| **Settings** for one object or a band of layers | No | Yes |
| **Knowing what they mean** | Yes | Yes |
| **Presets** | Read and switch | Read, switch, edit, save, rename, delete |
| **Models** | See what is on the plate and check placement | Load, move, rotate, duplicate, arrange, orient |
| **Slicing** | No: you press Slice yourself | Slice, read time and filament, compare settings |
| **Seeing the plate** | No | Render and describe it |
| **Plates** | No | Yes |
| **Projects** | See the name and whether there is something to save | Save, open, start and browse projects |
| **The panel** | Settings and preset changes, each with an undo | Every change, each with an undo, plus restore points |
| **Your printer** | Yes | Yes |
| **Learning from prints** | Recall past prints | Record each slice and recall it |

22 of the 66 tools work with the plugin, including the 4 printer tools, which talk to the printer rather than to OrcaSlicer. The other 44 answer that they need the OrcaSlicer MCP build, and `get_status` says which one you are connected to, so the assistant can tell you instead of guessing.

**To use the plugin:** subscribe to [OrcaSlicer MCP Lite on OrcaCloud](https://cloud.orcaslicer.com/p/EXAMPLE) and install it from **File → Plugins** in OrcaSlicer, or download `orcaslicer_mcp_lite.py` from this repository's [latest release](https://github.com/MaxEllis/orcaslicer-mcp/releases/latest) and in **File → Plugins** choose **Install local plugin** from the arrow beside **Install plugin**. Tick **Activate**: OrcaSlicer asks once for network access to this computer only and for a background thread, and the plugin needs both. Its **Config** tab in the same dialog shows the token: use it as `ORCA_API_TOKEN` in step 3 of the Quickstart. Use one install or the other, not both: each keeps its own token.

## You stay in charge

The OrcaSlicer MCP build has a panel beside the 3D view that lists what the assistant changed, grouped by round of work, with the print time of each slice. Every change has an undo named for what it does (**Revert**, **Rotate back**, **Put back**, **Switch back**), and the restore icon on an older group puts the whole project back to that point, itself undoable. The [fork's README](https://github.com/MaxEllis/OrcaSlicer#the-mcp-panel) shows it.

The assistant also hears about what you do. When you change a setting by hand, move a model, switch a preset, or undo something in the panel, the next tool reply carries a short note, so the assistant works from the project as it is now instead of what it last saw:

```
Since your last action the user changed: Wall loops 3 → 4 (0.20mm Standard); Moved bracket.stl; Reverted Sparse infill density.
```

Only the net change is listed (a value you set and set back does not appear), a preset switch or opening a project is one line, and a reply carries at most 8 entries plus a count of the rest. Nothing is added when nothing changed. Needs v2.5.0-mcp.16 or the Lite plugin, which reports settings and presets.

## What it can do

66 tools, plus three ready-made prompts (`slice-a-model`, `optimize-print-time`, `edit-preset-safely`) and the settings reference as MCP resources.

| | | |
|---|---|---|
| **Settings** | Read and change any of OrcaSlicer's settings while it runs, for the plate, one object, or a band of layers. | `get_config` `set_config` `find_config_keys` `set_layer_height` `set_height_range` `set_object_config` |
| **Knowing what they mean** | Look settings up in OrcaSlicer's own labels and tooltips, and check a profile against your printer's physical limits before printing. | `describe_setting` `search_settings` `compare_settings` `consult` `check_profile_physics` |
| **Presets** | Switch, edit, save, rename and delete presets, and choose the filament preset of one slot on a multi-filament printer. | `list_presets` `select_preset` `get_preset_config` `edit_preset` `save_preset` `rename_preset` `delete_preset` |
| **Models** | Load STL, OBJ, 3MF and STEP files, then move, rotate, scale, duplicate, arrange and auto-orient them. | `load_model` `list_objects` `transform_object` `duplicate_object` `delete_object` `arrange_plate` `auto_orient` `check_placement` `diagnose_plate` |
| **Slicing** | Slice, read time, filament and OrcaSlicer's warnings, split the result by feature, and compare several settings in one go. | `slice_and_wait` `apply_and_slice` `get_slice_breakdown` `compare_slices` `get_slice_warnings` `get_gcode` `save_gcode` |
| **Seeing the plate** | Get the plate as a picture, or as a short description of how each part stands, where support goes and where the seams sit. | `render_plate` `describe_plate` |
| **Plates** | Build a project with several plates, one part per plate, each sliced on its own. | `list_plates` `add_plate` `select_plate` `delete_plate` `move_object_to_plate` |
| **Projects** | Save, open and start projects as the File menu does, browse and tidy project files, and see whether there is something to save. | `save_project` `open_project` `new_project` `get_project` `list_recent_projects` `browse_projects` `move_project_file` |
| **Your printer** | Read what a Klipper or OctoPrint printer is doing, wait for a print to reach a stage, list past jobs, and check the profile matches the printer. Read-only. | `get_printer_status` `wait_for_printer` `list_print_history` `check_printer_match` |
| **Learning from prints** | Record each slice with its settings, match it to how the print turned out, and recall that next time. | `recall_prints` `remember` |

The rest are status, job and event tools (`get_status`, `watch_events`, `get_job_status` and the like) and the CANVAS tray mapping for the Elegoo Centauri Carbon 2.

### Where the time goes

`get_slice_breakdown` returns what OrcaSlicer's preview legend shows, as numbers the assistant can compare and act on:

```
role                    time      share   filament   mean flow
inner_wall              5m 41s    30.8%     6.43 g    16.0 mm3/s
outer_wall              3m 19s    18.0%     3.20 g    13.6 mm3/s
sparse_infill           3m 07s    17.0%     3.57 g    17.0 mm3/s
internal_solid_infill   2m 01s    11.0%     1.72 g    11.8 mm3/s
bridge                     52s     4.7%     0.26 g     4.4 mm3/s
...
                        18m 24s            16.89 g
```

A `prediction_check` comes with it and flags any feature whose requested speed was cut back to stay under the filament's maximum flow rate.

### What a change costs

`compare_slices` slices the plate under several variants, puts your settings back afterwards, and returns one table with the differences worked out:

```
Recommended: 0.4mm - fastest with no warnings.

variant     time      filament   vs 0.4mm (baseline)
0.3mm       8h 10m    41.0 g      +1h 30m (+22%), -7.0 g (-15%)
0.4mm  *    6h 40m    48.0 g      baseline
0.5mm       5h 20m    53.4 g      -1h 20m (-20%), +5.4 g (+11%)
0.6mm       4h 35m    57.1 g      -2h 05m (-31%), +9.1 g (+19%)  thin-wall warning
```

It recommends a variant only when one beats the rest on time, filament and warnings. When they trade off, it names the fastest and the lightest and leaves the choice to you.

### How the part sits

`describe_plate` reads the sliced G-code and says, one sentence per object, what an assistant would otherwise have to guess from coordinates:

> bracket.stl (3 copies) stands on an edge or corner: first-layer contact is 5% of its widest layer, in 3 islands of about 50 mm2 each. Overhang extrusions concentrate at Z 0 to 10 mm. Support is present from Z 0.4 to 56.8 mm, standing in 3 places and touching the part in 7 zones. Seams align on the +Y side (91%), matching seam_position=back.

### Things to know

- **Order of edits.** Selecting a preset discards unsaved changes in every group, and per-filament settings are written one preset at a time. Make filament edits first and print edits second; [docs/order-of-operations.md](https://github.com/MaxEllis/orcaslicer-mcp/blob/main/docs/order-of-operations.md) explains why.
- **Plates.** Slicing, G-code and renders act on the current plate, so select a plate before slicing it. `arrange_plate` packs every unlocked plate together again, so leave it out of a layout you built by hand.
- **Projects.** OrcaSlicer's "save changes?" prompts would hang a remote caller, so each one is answered by an argument instead. `if_unsaved` defaults to `fail`: nothing unsaved is thrown away unless you ask.
- **Printer and print history.** The server finds the printer through OrcaSlicer's active printer profile, so there is usually nothing to set up. [docs/printer-feedback.md](https://github.com/MaxEllis/orcaslicer-mcp/blob/main/docs/printer-feedback.md) covers the settings, what each printer type supports, and where print records are kept.
- **Older builds.** Each tool checks what the connected OrcaSlicer build supports and says so when a feature needs a newer one:

  | Feature | OrcaSlicer MCP build |
  |---|---|
  | STEP files | v2.3.2-mcp.3 or later |
  | `render_plate` | v2.3.2-mcp.4 or later |
  | Script and G-code lock inside the slicer | v2.4.2-mcp.10 or later |
  | Plates | v2.4.2-mcp.13 or later |
  | CANVAS tray mapping | v2.4.2-mcp.14 or later |
  | Project files | v2.4.2-mcp.15 or later |
  | OrcaSlicer 2.5.0, the MCP panel, notes about your edits, filament presets per slot | v2.5.0-mcp.16 or later |

## Security

- **Local by default.** OrcaSlicer's API listens on this computer only; LAN access is a separate opt-in in Preferences. Every request needs the token, which you can regenerate at any time.
- **No scripts, G-code or connection changes.** The server refuses to change settings that run code or send commands to the printer: post-processing scripts, every custom G-code template, OrcaSlicer 2.5's Python slicing plugins, and the printer's address and credentials. An assistant can be steered by text it reads, such as a file name or a web page, and these settings would turn that into a command on your computer or printer, so they stay a decision you make in the OrcaSlicer window. OrcaSlicer MCP builds from v2.4.2-mcp.10 apply the same rule inside the slicer until you tick **Allow script, G-code and connection edits** in Preferences.
- **No deleting or replacing files** unless you tick **Allow deleting and replacing project files** in Preferences (v2.4.2-mcp.15 and later).
- **Read-only printer access.** The printer tools only make GET requests. They never send G-code, change a temperature, or start or stop a job.
- **Credentials stay out of replies.** API keys, passwords and the `user:password@` part of a printer address are removed from what the tools return.
- **Every change can be taken back** in the OrcaSlicer MCP panel, one at a time or by going back to an earlier point.

[docs/security.md](https://github.com/MaxEllis/orcaslicer-mcp/blob/main/docs/security.md) has the full list of protected settings, how to allow specific ones with `ORCA_MCP_ALLOW_KEYS`, and the exact redaction rules.

## Privacy policy

The server talks to OrcaSlicer's local API at the address you configure, localhost by default, and, when you use the printer tools, to your 3D printer at the address in your OrcaSlicer printer profile (or `ORCA_PRINTER_URL`). It talks to nothing else. It has no backend, so there is no service of ours for anything to reach. Apart from those printer requests, what leaves your machine is whatever your MCP client sends its model: the conversation, plus any settings or file contents you or the assistant put into it. Tool replies can include a short list of what you changed by hand in OrcaSlicer since the assistant's last call (setting names and values, object and preset names), so that joins the conversation too. Their terms govern that traffic, and it is the same traffic any other use of that client produces. A local model removes it entirely.

- **Data collection:** none. The server collects nothing about you or your usage.
- **Usage and storage:** models, settings, and G-code stay on the computer running OrcaSlicer. They pass through the server in memory while a request runs, and the server writes to disk only what the data-retention entry below lists. The API token authenticates the server to OrcaSlicer, and your MCP client stores it. The printer key, if you set `ORCA_PRINTER_API_KEY`, authenticates the server to the printer and is stored the same way. Printer status, history, and settings come back to your MCP client as tool results, so they join the conversation like any other tool output. The `.mcpb` extension keeps the settings marked sensitive (the OrcaSlicer API token and the printer key) in the operating system's credential store. A password in `ORCA_PRINTER_URL` is stored as an ordinary setting, so prefer `ORCA_PRINTER_API_KEY` where the printer accepts a key.
- **Third-party sharing:** none by this server, which has no analytics and no backend. Traffic between your client and its model provider sits outside this project and falls under their policies.
- **Data retention:** the only data written to disk is notes you save yourself with `remember`, stored as plain files under `~/.orcaslicer-mcp/notes/`; the G-code that `save_gcode` writes, under `~/projects/_shared/print-outcomes/` when that shared folder already exists, otherwise under `~/.orcaslicer-mcp/`; the outcome store (`outcomes.db`), which holds slice records (the model name, a fingerprint of the objects' names and sizes, and the settings each slice used) and the finished-print results `list_print_history` copies from your printer, in that shared folder when it exists, otherwise in `~/.orcaslicer-mcp/outcomes/`; and `~/.orcaslicer-mcp/printer.json`, which remembers the last printer address that answered (never a password). `PRINT_OUTCOMES_DIR` relocates the G-code and the store. Read or delete any of it whenever you like. Delete it and nothing remains.
- **Contact:** questions and concerns go in [an issue](https://github.com/MaxEllis/orcaslicer-mcp/issues).

## Development

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest   # more than 1,200 tests against a mock API, plus a guarded live smoke test
```

The live smoke test skips itself unless `ORCA_API_URL` and `ORCA_API_TOKEN` point at a running OrcaSlicer MCP build. Protocol notes live in [`docs/`](https://github.com/MaxEllis/orcaslicer-mcp/tree/main/docs).

## Support and license

The project is free and stays that way. If it saves you time and you feel like saying thanks, you can buy me a coffee.

<a href="https://buymeacoffee.com/maxellis"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me a Coffee" height="44"></a>

Licensed under AGPL-3.0, like OrcaSlicer, whose source the bundled settings reference comes from. See [LICENSE](https://github.com/MaxEllis/orcaslicer-mcp/blob/main/LICENSE).

<!-- mcp-name: io.github.MaxEllis/orcaslicer-mcp -->
