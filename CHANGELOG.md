# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
- Config writes that would change a key able to run code or drive the printer are refused with `blocked_by_local_policy` before they reach OrcaSlicer. They are `post_process`, every `*_gcode` template, `printer_model`, `printer_technology`, `filename_format`, and the printer connection settings (`print_host`, `print_host_webui`, `printhost_*`, `host_type`, `bbl_use_printhost`, `printer_agent`, `flashforge_serial_number`). This covers `set_config`, `apply_and_slice`, `edit_preset`, `compare_settings`, `compare_slices` and `set_object_config`. The fork's `PUT /config` accepts any preset key and `post_process` runs through the shell after export, so an assistant steered by injected text could otherwise save a command into a user preset. Thanks to @ferpa for the original change.
- Writing back an unchanged value passes, compared the way the fork serializes it (`true` is `1`). The host address and credentials are always refused, because OrcaSlicer never reports their current value.
- `edit_preset` checks against the named preset before selecting it, so a refusal no longer discards unsaved overrides. The `compare_*` snapshot restores skip the check, since they write back values just read from OrcaSlicer, so a restore never waits on an extra read.
- `ORCA_MCP_ALLOW_KEYS` opts specific keys back in. Wildcards such as `*_gcode` work, and the Claude Desktop extension exposes it as a setting. Names that match no protected key are logged at startup.
- `get_preset_config` hides `printhost_apikey`, `printhost_user` and `printhost_password`, and removes any `user:password@` from `print_host` and `print_host_webui`. The fork returns preset config unfiltered, unlike `GET /config`. Writes carrying the `<redacted>` placeholder are refused, so a round-tripped preset never overwrites a real credential.
- OrcaSlicer MCP v2.4.2-mcp.10 and later enforce the same rule inside the slicer, through Preferences → Remote API → Allow script, G-code and connection edits (off by default). When the slicer refuses a key, the tool reply carries a `hint` that says where the switch is.

### Documentation
- README: Windows fallback for `uvx` failing on `pywin32` with a sharing violation (pip venv, which retries). Findings in `docs/windows-install-findings-2026-09-22.md`, including the upstream 2.4.x "Profile syncing change" dialog and the fork's startup `remove_all` crash on a locked AppData datadir.

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
- `save_gcode`: save the last slice's G-code into the shared print-outcomes folder and record the slice (model, geometry, settings) so the companion klipper-mcp can join the real print result back to it.
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
