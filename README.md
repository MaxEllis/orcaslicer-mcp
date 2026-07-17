# orcaslicer-mcp

An MCP server that drives a running OrcaSlicer via its Remote API — status,
config read/write, slicing, live events, and (with the fork's M4a endpoints)
load-model / select-preset / get-gcode.

It is a local, transparent assistant layer: the slicer stays authoritative, every
change is visible live in the OrcaSlicer GUI, and the server holds no LLM (no API cost —
it runs under your Claude subscription via Claude Code / Claude Desktop).

## Setup

Enable the Remote API in OrcaSlicer (Preferences → **Remote API**), copy the token.

```bash
uv tool install orcaslicer-mcp   # or, from a clone: uv pip install -e .
```

## Configure in Claude Code / Claude Desktop

```json
{
  "mcpServers": {
    "orcaslicer": {
      "command": "uvx",
      "args": ["orcaslicer-mcp"],
      "env": {
        "ORCA_API_URL": "http://<orca-host>:13130",
        "ORCA_API_TOKEN": "<token>"
      }
    }
  }
}
```

`ORCA_API_URL` defaults to `http://127.0.0.1:13130`; `ORCA_API_TOKEN` is required.

## Tools

- **status/config:** `get_status`, `get_config`, `set_config`, `find_config_keys`
- **settings knowledge (offline, no live API):** `describe_setting`, `search_settings` — authoritative label/tooltip/type/range/enum/default for ~800 settings, extracted from the fork's `PrintConfig.cpp` via `scripts/extract_settings_schema.py`.
- **slicing:** `slice`, `get_slice_status`, `slice_and_wait`, `apply_and_slice`, `compare_settings`
- **events:** `watch_events`
- **M4b (needs the fork's plate/object endpoints):** `list_objects` — list plate objects (id, name, size_mm, transform); returns a `needs M4b` message until the fork ships `GET /objects`.
- **M4a (needs the fork's model/preset/gcode endpoints):** `load_model`, `select_preset`, `get_gcode` — these return `"not available on this OrcaSlicer build (needs M4a)"` until the fork ships those endpoints.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest            # unit tests (mock API) + a guarded live smoke test
```

The live smoke test (`tests/test_integration.py`) is skipped unless both
`ORCA_API_URL` and `ORCA_API_TOKEN` point at a running OrcaSlicer.
