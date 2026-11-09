# OrcaSlicer MCP plugin (for stock OrcaSlicer 2.5)

Install it from [OrcaCloud](https://cloud.orcaslicer.com/p/16d116a6bc41), or download
`orcaslicer_mcp_plugin.py` from the [orcaslicer-mcp releases](https://github.com/MaxEllis/orcaslicer-mcp/releases/latest).
To connect your AI app to it, follow the [orcaslicer-mcp setup steps](https://github.com/MaxEllis/orcaslicer-mcp#get-started).

A single-file OrcaSlicer 2.5 plugin that serves part of the OrcaSlicer MCP Remote API, so the
existing `orcaslicer-mcp` server can talk to stock OrcaSlicer without the fork. It uses the same
routes, JSON and `X-Api-Token` header, so orcaslicer-mcp needs no new client code.

## What it can and cannot do

| | Plugin on stock OrcaSlicer 2.5 | OrcaSlicer MCP build |
| --- | --- | --- |
| Read printer, filament and process settings | Yes | Yes |
| Change settings (unsaved; you see them in the tabs) | Yes | Yes |
| Switch presets | Yes (OrcaSlicer asks on screen about unsaved edits) | Yes |
| Save, rename, delete presets | No | Yes |
| See the objects on the plate (sizes, positions) | Yes, read-only | Yes |
| Load, move, rotate, arrange, orient models | No | Yes |
| Slice, read time and filament, get the G-code | No, you press Slice | Yes |
| Render the plate as an image | No | Yes |
| Plates; open and save projects | No | Yes |

Why: OrcaSlicer 2.5's plugin API lets a plugin read the model and edit presets, but has no call to
load a model, start a slice, read slice results, render, manage plates or open/save a project.

Against the plugin, 18 orcaslicer-mcp tools work (status, config, presets, objects, project,
settings advice and checks), 44 answer that they need the OrcaSlicer MCP build, and the 4 printer
tools work as before because they talk to the printer, not OrcaSlicer (`tests/test_tool_matrix.py`).

## Install (stock OrcaSlicer 2.5)

1. In OrcaSlicer open the main menu (the icon beside File) and choose **Plugins**.
2. **From OrcaCloud:** subscribe on [OrcaCloud](https://cloud.orcaslicer.com/p/16d116a6bc41), then
   install OrcaSlicer MCP from the list in this window. OrcaSlicer keeps it up to date.

   **From a file:** click the small arrow beside **Install plugin**, choose **Install local plugin**,
   then click the button (it now reads Install local plugin) and pick `orcaslicer_mcp_plugin.py`. A
   plugin installed from a file never updates itself; orcaslicer-mcp tells you when a newer one is out.
3. Tick **Activate** next to OrcaSlicer MCP. OrcaSlicer asks once for network access to
   127.0.0.1 and permission to create a thread; click **Yes** (No stops the plugin loading).
4. Open the plugin's **Config** tab in that dialog: it shows the token (hidden until Show, with Copy
   and New token), the port, and the switch that turns assistant access off and on.
5. orcaslicer-mcp: set `ORCA_API_TOKEN` to that token. The URL default
   (`http://127.0.0.1:13130`) already matches. A port changed in the Config tab takes effect after a
   restart. For tests, `settings.json` in the plugin's data folder
   (`<OrcaSlicer data folder>/orca_plugins/plugin_data/orcaslicer_mcp_plugin/`) overrides it, and
   `"trace": true` there writes a diagnostic `plugin-trace.log`.

The panel beside the 3D view is the same timeline the OrcaSlicer MCP build shows (the page is
vendored from the fork's `resources/web/orcaslicer_mcp/panel.html` and kept identical by
`tests/test_embedded.py`): each burst of changes with a title, the setting and preset changes and
refusals in it, a live "changed by you" when you edit something afterwards, and Revert / Switch back
on each change. Restore points need the build. The history is kept per project, in the same
`history.json` format as the build. The panel is also how the plugin reaches OrcaSlicer's UI thread;
it opens as soon as the plugin is activated, keeps working with OrcaSlicer minimised, and the first request opens it again if it was closed.
Unticking Activate stops the server.

## Safety

- Listens on 127.0.0.1 only; every request needs the token (compared in constant time).
- Never writes script, G-code, printer-connection, printer-model, file-name or plugin settings
  (the fork's sensitive-key policy; there is no opt-in on stock). Writing a key back unchanged
  passes, so snapshot/restore works.
- Hides `print_host`/`printhost_*` from `GET /config` and redacts credentials in
  `POST /preset/config`.
- Every change shows as an OrcaSlicer notification and in the panel's timeline, with undo.

## Tests

```bash
uv run pytest -q plugin/tests   # from the repository root
```

`tests/fake_orca.py` stands in for OrcaSlicer's `orca` module and refuses model/preset reads off
its UI thread, so a read that skips the panel pump fails. `test_mcp_client.py` and
`test_tool_matrix.py` run this repository's orcaslicer-mcp against the plugin (`ORCASLICER_MCP_SRC`
points them at another checkout).
`tests/live/run_live.py` drives a real OrcaSlicer on a test PC through an SSH tunnel (see
`tests/live/README.md`).

## Releasing

1. Raise the version in the file header and in `PLUGIN_VERSION`, and `LATEST_PLUGIN_VERSION` in
   `src/orcaslicer_mcp/plugin_update.py` with them (a test checks all three).
2. Publish a GitHub release tagged `plugin-vX.Y.Z` with `--latest=false`, so the latest release stays
   the orcaslicer-mcp one. `.github/workflows/publish-orcacloud.yml` then uploads that version to the
   OrcaCloud listing; the release notes become its changelog.
3. Attach `orcaslicer_mcp_plugin.py` and its `.sha256` to the next orcaslicer-mcp release too, for
   people who install from a file.

## License

AGPL-3.0, like the rest of this repository. See [LICENSE](../LICENSE).
