# OrcaSlicer MCP

**Drive OrcaSlicer with AI.** OrcaSlicer MCP is an [MCP](https://modelcontextprotocol.io) server that lets Claude (Desktop or Code — or any MCP client) operate a real, running OrcaSlicer: load models, arrange the plate, tune settings, slice, and analyze the result — live, with every change visible in the GUI.

The slicer stays authoritative and on your machine. The server contains no LLM and makes no cloud calls; it talks only to OrcaSlicer on localhost (or a host you explicitly allow).

## How it works

Two pieces:

1. **The OrcaSlicer MCP build of OrcaSlicer** — OrcaSlicer 2.3.2 with an embedded local control API (token-authenticated, localhost-only by default). Get it from the [releases page](https://github.com/maxellis/OrcaSlicer/releases); if no binary is up for your platform yet, build the `remote-api` branch from source.
2. **This package (`orcaslicer-mcp`)** — the MCP server that connects your AI client to that build.

Stock OrcaSlicer does not have the control API — the MCP server requires the build above.

> **Updating:** always get new builds from the [releases page](https://github.com/maxellis/OrcaSlicer/releases), never from inside the app. Builds **mcp.2 and later** disable OrcaSlicer's built-in updater for you (left on, it offers *stock* OrcaSlicer — which removes the control API). If you're on an older build and a "new version available" prompt appears, click **Skip this Version**.

## Quickstart

You'll need [uv](https://docs.astral.sh/uv/getting-started/installation/) installed (it provides the `uvx` command that runs the server) — one line: `curl -LsSf https://astral.sh/uv/install.sh | sh` on macOS/Linux, or `irm https://astral.sh/uv/install.ps1 | iex` in PowerShell on Windows.

1. Install and launch the OrcaSlicer MCP build, and complete the one-time first-run setup (pick your printer). On a fresh install OrcaSlicer may show a **"Bambu Network Plug-in Required"** dialog — click **Skip for Now**; that plug-in is only for Bambu cloud printing and isn't needed here. (The control API starts once first-run setup is finished.)
2. In OrcaSlicer: **Preferences** (Ctrl+P) **→ Remote API → Enable Remote API**, then copy the API token shown on that page. (Access is localhost-only unless you also switch on "Allow LAN access".)
3. Connect your MCP client.

    **Claude Desktop:** download `orcaslicer-mcp-<version>.mcpb` from the [releases page](https://github.com/maxellis/orcaslicer-mcp/releases/latest) and open the file. Claude Desktop shows an install prompt. Once it's in, open the extension's settings, paste the token from step 2, and enable it.

    > Skip any guide that tells you to hand-edit `claude_desktop_config.json`. Current Claude Desktop builds rewrite that file on their own and drop added `mcpServers` entries, so edits don't stick. The extension never touches the file, and it finds `uvx` by itself.

    **Claude Code and other MCP clients:** add the server to the client's MCP config (for Claude Code, a project `.mcp.json`):

    ```json
    {
      "mcpServers": {
        "orcaslicer": {
          "command": "uvx",
          "args": ["orcaslicer-mcp"],
          "env": {
            "ORCA_API_TOKEN": "<token from Preferences>"
          }
        }
      }
    }
    ```

    `ORCA_API_URL` defaults to `http://127.0.0.1:13130` — set it only if you changed the port, or run OrcaSlicer on another machine (with LAN access enabled there).

    > **macOS note for GUI clients other than Claude Desktop:** apps launched from the Dock don't inherit your terminal's PATH, so `"command": "uvx"` can fail silently. Run `which uvx` in Terminal and put the full path it prints (usually `~/.local/bin/uvx`) in `"command"`.

4. Restart your client and ask: *“Load benchy.stl, slice it with the current profile, and tell me the print time.”*

## What Claude can do with it

- **Plate & models:** `load_model` (`.stl`/`.obj`/`.3mf`, plus `.step`/`.stp` with fork v2.3.2-mcp.3+), `list_objects`, `transform_object`, `duplicate_object`, `delete_object`, `arrange_plate`, `auto_orient`, `check_placement`, `diagnose_plate`, `get_job_status`
- **Settings:** `get_config`, `set_config`, `find_config_keys`, `describe_setting`, `search_settings`, `compare_settings`, `set_layer_height`, `set_height_range`, `set_object_config` (per-object overrides)
- **Presets:** `list_presets`, `select_preset`, `get_preset_config`, `edit_preset`, `save_preset`, `rename_preset`, `delete_preset`
- **Slicing:** `slice`, `slice_and_wait`, `apply_and_slice`, `cancel_slice`, `get_slice_status`, `get_slice_warnings`, `get_slice_breakdown` (per-feature time/flow analysis), `get_gcode`
- **Live state & events:** `get_status`, `watch_events`

### Settings intelligence

- **`consult(query)`** — curated slicing knowledge plus your saved notes, composed per topic, symptom, or goal.
- **`check_profile_physics(changes?)`** — deterministic sanity gate: overlays proposed changes on the live config and runs flow/temperature/geometry/cooling math. Verdict: `ok`, `warnings`, or `blocked`.
- **`remember(note, scope)`** — persist machine/user/project facts for future sessions as plain local files in `~/.orcaslicer-mcp/notes/` (relocatable via `ORCA_MCP_NOTES_DIR`).

Plus an offline settings reference: authoritative label/tooltip/type/range/enum/default for ~800 OrcaSlicer settings, bundled with the package.

## Security

- The control API binds **127.0.0.1 only** by default; LAN access is an explicit opt-in in Preferences.
- Every request must carry the API token; OrcaSlicer generates it on first run and can regenerate it at any time.
- The MCP server is a local stdio process. No telemetry, no cloud.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest   # unit tests (mock API) + a guarded live smoke test
```

The live smoke test is skipped unless `ORCA_API_URL` / `ORCA_API_TOKEN` point at a running OrcaSlicer MCP build.

Developer docs — protocol notes, design specs, verification results — live in [`docs/`](docs/).

## Privacy

Everything runs on your own machines. The server talks only to OrcaSlicer's local API at the address you configure (localhost by default) and to nothing else: no telemetry, no analytics, no accounts, no cloud calls. Models, settings, and gcode never leave your computer. The API token authenticates the server to OrcaSlicer and is stored by your MCP client (Claude Desktop keeps extension settings in the operating system's credential store). Notes you save with `remember` are plain files under `~/.orcaslicer-mcp/notes/`, yours to read or delete at any time.

## Status

Early public release (soft launch). The server is exercised by ~170 unit tests and real print workflows; prebuilt OrcaSlicer MCP builds are available for Windows, macOS, and Linux on the [releases page](https://github.com/maxellis/OrcaSlicer/releases). Issues and reports welcome.

## License

AGPL-3.0 — the same license as OrcaSlicer, from whose source the bundled settings schema is derived. See [LICENSE](LICENSE).

<!-- mcp-name: io.github.maxellis/orcaslicer-mcp -->
