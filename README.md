# OrcaSlicer MCP

**Drive OrcaSlicer with AI.** OrcaSlicer MCP is an [MCP](https://modelcontextprotocol.io) server that lets Claude (Desktop or Code — or any MCP client) operate a real, running OrcaSlicer: load models, arrange the plate, tune settings, slice, and analyze the result — live, with every change visible in the GUI.

The slicer stays authoritative and on your machine. The server contains no LLM and makes no cloud calls; it talks only to OrcaSlicer on localhost (or a host you explicitly allow).

## How it works

Two pieces:

1. **The OrcaSlicer MCP build of OrcaSlicer** — OrcaSlicer 2.3.2 with an embedded local control API (token-authenticated, localhost-only by default). Get it from the [releases page](https://github.com/maxellis/OrcaSlicer/releases); if no binary is up for your platform yet, build the `remote-api` branch from source.
2. **This package (`orcaslicer-mcp`)** — the MCP server that connects your AI client to that build.

Stock OrcaSlicer does not have the control API — the MCP server requires the build above.

## Quickstart

1. Install and launch the OrcaSlicer MCP build.
2. In OrcaSlicer: **Preferences → Remote API → Enable Remote API**, then copy the API token shown on that page. (Access is localhost-only unless you also switch on "Allow LAN access".)
3. Add the server to your MCP client config (Claude Desktop `claude_desktop_config.json`, or a project `.mcp.json` for Claude Code):

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

4. Restart your client and ask: *“Load benchy.stl, slice it with the current profile, and tell me the print time.”*

## What Claude can do with it

- **Plate & models:** `load_model`, `list_objects`, `transform_object`, `duplicate_object`, `delete_object`, `arrange_plate`, `auto_orient`, `check_placement`, `diagnose_plate`, `get_job_status`
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

## Status

Early public release (soft launch). The server is exercised by ~170 unit tests and real print workflows; prebuilt binaries of the OrcaSlicer MCP build are rolling out per-platform. Issues and reports welcome.

## License

AGPL-3.0 — the same license as OrcaSlicer, from whose source the bundled settings schema is derived. See [LICENSE](LICENSE).
