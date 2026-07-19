# orcaslicer-mcp — End-to-End Verification Results (2026-07-19)

**Target:** fork build `remote-api` @ `43b86fc7` on max-pc (Tailscale `100.84.203.81:13130`),
OrcaSlicer 2.3.2, capabilities FULL (11/11: status,config,slice,events,model,preset,gcode,objects,arrange,orient,object_config).
**Method:** each feature driven via the MCP surface (`mcp__orca__*`) and/or the `srv.<tool>()` module path, cross-checked against the raw Remote API by `curl`/`httpx`. No physical print performed.

## VERDICT: **GO** — the product works end-to-end.
All 37 tools exercised against the live fork. Both session-fixed bugs proven live (`%2C` multi-key config; `TOOLPATH_OUTSIDE` boundary warning + D1 read→fix→confirm loop). 7 findings surfaced (2 minor); none block core function.

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
| F1 | med | server `_wait_for_slice` | loops `while state=='slicing'`; returns early if first poll is `'starting'` → stale result from slice_and_wait/apply_and_slice/compare_settings | wait until TERMINAL (`done`/`error`) |
| F2 | med | fork/RemoteAPIController | object fully OUTSIDE bed → slice wedges in `slicing` forever; no cancel route; recovery = kill+relaunch. Pre-existing (new code never reached) | set `error` state on failed/aborted slice; add slice-cancel route |
| F3 | med | server `rename_preset` | leaves the OLD preset on disk (rename = copy w/o delete) | delete source preset after successful rename |
| F6 | med | server `set_layer_height` | `mode='default'` → `unknown_mode`; documented clear path broken (adaptive works) | accept 'default'/'none' to clear, or fix docs to real mode |
| F7 | low | server `_m4a/b/c_err` | maps ANY NotFound(404) to "needs M4x"; on full build a bad id/name reads as capability-missing | distinguish unimplemented-route from resource-not-found |
| F4 | minor | status vs list_presets | `presets.filaments` (plural) vs `list_presets.filament` (singular) | unify key name |
| F5 | minor | transform_object | rotate input DEGREES, readback RADIANS | document or normalize units |

## Actions taken this session (outside verification)
- Fork committed `43b86fc7` (remote-api) + relay-pushed to GitHub (both fixes).
- Recovered 3 wedged slices via kill+relaunch (F2).
- Teardown: state restored; max-pc OrcaShot task + 6 named scratch scripts removed.
