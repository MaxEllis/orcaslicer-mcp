# orcaslicer-mcp — End-to-End Verification Results (2026-07-19)

**Target:** fork build `remote-api` @ `43b86fc7` on max-pc (Tailscale `100.84.203.81:13130`),
OrcaSlicer 2.3.2, capabilities FULL (11/11: status,config,slice,events,model,preset,gcode,objects,arrange,orient,object_config).
**Method:** each feature driven via the MCP surface (`mcp__orca__*`) and/or the `srv.<tool>()` module path, cross-checked against the raw Remote API by `curl`/`httpx`. No physical print performed.

## VERDICT: **GO** — the product works end-to-end.
All 37 tools exercised against the live fork. Both session-fixed bugs proven live (`%2C` multi-key config; `TOOLPATH_OUTSIDE` boundary warning + D1 read→fix→confirm loop). 7 findings surfaced (2 minor); none block core function. **Later addendum:** a settings write-path sweep added **F8** (fork crash under rapid writes — high sev) and **F9** (GET/PUT key asymmetry) — see Addendum.

## Suite summary
| Suite | Result | Notes |
|---|---|---|
| 0 Preconditions/harness | PASS | NEW build confirmed; harness validated offline; fixture ok |
| 1 Wiring (mcp__orca__*) | PASS | .mcp.json reloaded; MCP==raw |
| 2 Read surface (9 tools) | PASS | get_config wraps `{"config":{…}}`; 606 keys; bed 0–300 |
| 3 Config mutation | PASS | atomic 422 rejection holds; compare/apply restore |
| 4 Preset lifecycle | PASS* | **4.6 rename FAIL (F3)**; rest pass; tuned preset intact |
| 5 Model/object (M4b/M4c) | PASS* | **5.7 layer-height 'default' clear FAIL (F6)**; rest pass |
| 6 Slicing + TOOLPATH_OUTSIDE | PASS | **fix proven: warning appears (brim crossing) & clears (D1)** |
| 7 Knowledge + physics gate | PASS | physics COMPUTES (flags 25@205, passes 16@215); KB reachable |
| 8 WS events | PASS | config.changed + full slice lifecycle (started/progress/done) |
| 9 Resilience/degradation | PASS | structured errors, no crashes (see F7 on messaging) |
| 10 Unit backstop (pytest) | PASS | 128 passed, 1 skipped |
| 11 A↔B parity sweep | PASS | zero MCP-vs-raw divergence (incl. %2C definitive) |
| 12 Teardown | PASS | config/plate/presets restored; Rule 7 false/false; scratch cleaned |
| A Legacy 3dprint (read-only) | PASS | Klipper ready; file list; hardware actions excluded |

## 37 tools — per-tool ledger
PASS unless noted. get_status, get_config, set_config, slice, get_slice_status, get_slice_warnings,
slice_and_wait(⚠F1), apply_and_slice(⚠F1), compare_settings, list_objects, set_object_config,
duplicate_object(instance semantics), delete_object, transform_object(⚠F5 radians readback),
arrange_plate, auto_orient, get_job_status, watch_events, find_config_keys, diagnose_plate,
check_placement, consult, check_profile_physics, remember, load_model, select_preset, save_preset,
list_presets(⚠F4 key name), set_layer_height(**F6 'default' clear broken**), set_height_range,
get_preset_config, delete_preset, edit_preset, **rename_preset (F3 FAIL)**, get_gcode,
describe_setting, search_settings.

## FINDINGS (for the product backlog — none block GO)
| # | Sev | Where | Defect | Fix direction |
|---|---|---|---|---|
| F8 | **high** | fork: config-apply vs 10s auto-backup exporter | Sustained rapid `set_config` writes CRASH the fork (~300 writes in) — concurrency race with the 10s auto-backup 3MF exporter on the GUI thread (`_add_model_config_file_to_archive: invalid object id -1`). WER dump `%LOCALAPPDATA%\CrashDumps\orca-slicer.exe.<pid>.dmp`. On accumulating-dirty config also ~25% `ui_timeout`. An API client hammering set_config can take the fork down. | serialize/lock config-apply against the backup exporter; make config-apply thread-safe; pause auto-backup during API mutation. Related to F2 (fork robustness). |
| F1 | med | server `_wait_for_slice` | loops `while state=='slicing'`; returns early if first poll is `'starting'` → stale result from slice_and_wait/apply_and_slice/compare_settings | wait until TERMINAL (`done`/`error`) |
| F2 | med | fork/RemoteAPIController | object fully OUTSIDE bed → slice wedges in `slicing` forever; no cancel route; recovery = kill+relaunch. Pre-existing (new code never reached) | set `error` state on failed/aborted slice; add slice-cancel route |
| F3 | med | server `rename_preset` | leaves the OLD preset on disk (rename = copy w/o delete) | delete source preset after successful rename |
| F6 | med | server `set_layer_height` | `mode='default'` → `unknown_mode`; documented clear path broken (adaptive works) | accept 'default'/'none' to clear, or fix docs to real mode |
| F9 | med | fork: `PUT /config` key whitelist | 18 keys returned by `GET /config` are rejected by `PUT /config` as `unknown_key` (read/write asymmetry): wipe_tower_x/y, flush_multiplier, flush_volumes_vector/matrix, has_scarf_joint_seam, first_layer_print_sequence, other_layers_print_sequence(_nums), start_end_points, extruder_ams_count, bbl_calib_mark_logo, filament_colour(_type), filament_multi_colour, filament_map(_mode), filament_self_index | add the missing settable keys to the PUT whitelist (at least print-geometry: wipe_tower_x/y, flush_multiplier, has_scarf_joint_seam), or document the rest as intentionally read-only |
| F7 | low | server `_m4a/b/c_err` | maps ANY NotFound(404) to "needs M4x"; on full build a bad id/name reads as capability-missing | distinguish unimplemented-route from resource-not-found |
| F4 | minor | status vs list_presets | `presets.filaments` (plural) vs `list_presets.filament` (singular) | unify key name |
| F5 | minor | transform_object | rotate input DEGREES, readback RADIANS | document or normalize units |

## Actions taken this session (outside verification)
- Fork committed `43b86fc7` (remote-api) + relay-pushed to GitHub (both fixes).
- Recovered 3 wedged slices via kill+relaunch (F2).
- Teardown: state restored; max-pc OrcaShot task + 6 named scratch scripts removed.


## Addendum (2026-07-19, later) — settings write-path sweep

Follow-on to the e2e run: exhaustively verified **"does every setting accept a change?"** by driving `PUT /api/v1/config` over all **606** keys with schema-derived *valid* values (set -> readback -> verify), then a Phase-2 re-test of the recoverable non-accepts with corrected candidate values. Fork left clean (`modified:{}`, tuned preset restored).

**Result (585 testable; 21 identity/lineage keys excluded):**

| Outcome | Count | Meaning |
|---|---:|---|
| Accept changes | **560** | write applied + value verifiably changed |
| Accept write, value clamped/gated | 5 | `wall_filament`/`solid_infill_filament`/`sparse_infill_filament` clamp to 1 (single filament); `silent_mode`, `wipe_tower_rotation_angle` gated by disabled features |
| Recognized, no valid test value found | 2 | `curr_bed_type`, `nozzle_volume_type` (key accepted by writer; no tested token stuck) |
| `unknown_key` (not writable) | 18 | see **F9** |

So ~96% of settings accept changes; the only genuine write gaps are F9's 18 keys (+ the 2 indeterminate enums).

**Method notes / gotchas (for whoever re-runs this):**
- The naive per-key sweep (PUT set / GET / PUT restore, ~3-4s/key) **crashed the fork** (-> **F8**) and hit ~25% `ui_timeout`. `ui_timeout` (HTTP 4xx `{"error":"ui_timeout"}`) is **RETRYABLE, not a rejection** — mis-classifying it poisons results.
- **Reset config to clean via preset re-select** (PUT `/api/v1/preset` for print+filament+printer) — reliable; discards in-memory edits without a relaunch. **Do NOT restore by re-setting old values** — the restore PUT silently `ui_timeout`s and leaves +0.1/+1 drift; a baseline captured mid-drift then poisons the whole run.
- Hardened sweep (reset-to-clean every 25 keys + pacing + schema-valid values) ran **585/585 with 0 crashes / 0 timeouts**.
- Schema caveats in `data/print_settings_schema.json`: several **bool** keys are typed `coInt` (naive "+1" -> out-of-domain value `2` -> false invalid_value); some enum `enum_values` are **incomplete** (e.g. `brim_type`, `curr_bed_type`); `overhang_fan_threshold` is a %-token enum.

**Correction (2026-07-19, live-confirmed):** the 2 "recognized, no valid test value found" keys
(`curr_bed_type`, `nozzle_volume_type`) actually return `unknown_key` on PUT — **F9 is 20 keys, not 18.**
Root cause confirmed in `orca-relay/src/slic3r/GUI/RemoteAPI/RemoteAPIController.cpp:298-306`: PUT `/config`
only accepts keys present in one of the 3 *edited-preset* configs; GET serializes the fuller merged config,
so project/plate/computed-layer keys are structurally unreachable. Nearly all 20 are multi-material/AMS/Bambu
options that don't exist on a single-extruder SWX2 — recommended F9 resolution is a clearer error
(`not_editable_in_current_config`) + document read-only, NOT whitelisting. Meanwhile `overhang_fan_threshold`
was proven **writable** (needs exact `%` enum token, per-filament vector) — fixed data-side by repairing the
schema extractor (emplace_back + alias-copy enum parsing), which now populates 56/56 coEnum keys.
