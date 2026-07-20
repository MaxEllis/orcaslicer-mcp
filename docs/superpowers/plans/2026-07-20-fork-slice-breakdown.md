# Fork slice-breakdown emit (C++ side) — Implementation Plan

> **STATUS: SHIPPED + VERIFIED 2026-07-20.** Fork commit `be2aca3f` (remote-api, relayed). Built clean (~50min), `slice_breakdown` capability advertised, breakdown emits top-level on completed slice with per-role time/flow/filament. Live verify (system Artillery SWX2 0.4 printer, cube20): `get_slice_breakdown` returned available=true + **5 prediction_check verdicts** (4 matches, 1 anomaly on outer_wall) — token contract holds end-to-end. **Unit check exact**: sum(role.filament_mm) == stats.filament_used_mm (ratio 1.000), confirming the meters→mm ×1000 conversion. MCP guard test `0e5bf1c`. Implementation matched the plan except: breakdown stored under `stats["breakdown"]` and LIFTED to top-level in handle_slice_status (the shipped MCP reads top-level `status.breakdown`), and per-move flow uses `MoveVertex::volumetric_rate()` (feedrate*mm3_per_mm).

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:executing-plans (or subagent-driven-development) to implement task-by-task. Steps use checkbox (`- [ ]`) syntax. This is a **fork C++** plan (OrcaSlicer Remote API), the companion to the already-shipped MCP-side plan `2026-07-20-slice-analytics-mcp.md`. That plan's consumer (`get_slice_breakdown`, `breakdown.py`) already exists and degrades to `{"available": false}` until this fork change lands.

**Goal:** Make the fork compute and emit a compact per-feature `breakdown` object on `GET /api/v1/slice/status`, and advertise a `slice_breakdown` capability, so the MCP `get_slice_breakdown` tool returns real per-role time/filament + a working predicted-vs-observed flow check instead of degrading.

**Fork tree (verified on max-pc 2026-07-20):** `G:\orca-dev\OrcaSlicer`, branch `remote-api`, HEAD `1776a884`. Remote API code under `src/slic3r/GUI/RemoteAPI/`.

**Tech Stack:** C++17, wxWidgets (GUI-thread event handlers), `nlohmann::json`, libslic3r (`GCodeProcessorResult`, `PrintEstimatedStatistics`, `ExtrusionRole`). Build via `OrcaRebuild` on max-pc (full recompile ~48 min). Relaunch discipline: kill `orca-slicer` → wait port 13130 drains to 0 sockets (~90s) → `OrcaRun`.

---

## Spike findings (ground truth — do not re-discover)

All file:line refs are on branch `remote-api` @ `1776a884`.

1. **Route dispatch:** `RemoteAPIController.cpp:1261` — `if (is("GET","/api/v1/slice/status")) return handle_slice_status();`
2. **Status handler serializes a CACHED snapshot** — `handle_slice_status()` at `RemoteAPIController.cpp:596-603` just returns `m_slice.state/percent/message/stats/warnings`. It does NOT build stats. **Do not add breakdown here.**
3. **Stats are BUILT in the slice-completed event handler** — `bind_plater_events()`, the `EVT_PROCESS_COMPLETED` success branch, `RemoteAPIController.cpp:512-549`. This is where to build `breakdown`, because the slice result is in hand there:
   - `GCodeProcessorResult *res = plates.get_curr_plate()->get_slice_result();`
   - `const PrintStatistics &ps = plates.get_current_fff_print().print_statistics();`
   - the block **already iterates `res->moves`** (for the toolpath-boundary warning) — fold the per-role time accumulation into that existing loop.
   - result is stashed via `set_slice_state([&](SliceState &s){ s.stats = stats; ... })`. Add `s.stats["breakdown"] = breakdown;` (or nest under stats) so the cached snapshot carries it.
4. **Per-role FILAMENT — available directly.** `PrintEstimatedStatistics` (`GCode/GCodeProcessor.hpp:46-99`) has:
   `std::map<ExtrusionRole, std::pair<double,double>> used_filaments_per_role;` — `.first` = length, `.second` = grams (`GCodeProcessor.cpp:1527-1544`). **CAVEAT (unit mismatch):** `.first` is scaled `*0.001` and the GUI treats it as **meters**, whereas top-level `ps.total_used_filament` is **mm**. Normalize the emitted length to **mm** (×1000) and label the field unambiguously (`filament_mm`, `filament_g`).
5. **Per-role TIME — NOT in `PrintEstimatedStatistics`.** `Mode` (`GCodeProcessor.hpp`) has only total `time`/`prepare_time`. Recompute per-role time by summing `mv.time[0]` (Normal mode, index 0 — matches the existing `estimated_time_seconds = modes.front().time`) over `res->moves` where `mv.type == EMoveType::Extrude`, grouped by `mv.extrusion_role`. `MoveVertex` (`GCodeProcessor.hpp:170-200`) carries `extrusion_role` and `std::array<float,ETimeMode::Count> time`. This reproduces exactly what libvgcode does (`ViewerImpl.cpp:1002-1018`) — no need to touch the GUI viewer.
6. **`ExtrusionRole` enum** (`ExtrusionEntity.hpp:20-43`, `enum ExtrusionRole : uint8_t`): `erNone, erPerimeter, erExternalPerimeter, erOverhangPerimeter, erInternalInfill, erSolidInfill, erTopSolidInfill, erBottomSurface, erIroning, erBridgeInfill, erInternalBridgeInfill, erGapFill, erSkirt, erBrim, erSupportMaterial, erSupportMaterialInterface, erSupportTransition, erWipeTower, erCustom, erMixed, erCount`.
   - **Do NOT use `ExtrusionEntity::role_to_string()`** (`ExtrusionEntity.cpp:562`) for keys — it returns **localized** GUI labels wrapped in `L()` (unstable across locales). Emit fixed ASCII tokens via the mapping in Task 2.
7. **Capability flag:** `handle_status()` (`RemoteAPIController.cpp:250`) advertises the capabilities array at line ~267. Add `"slice_breakdown"`.
8. **Reachability: EASY.** Both data sources are already local to the success branch; no cross-subsystem plumbing. Change is localized to `RemoteAPIController.cpp`. Runs on the GUI thread where the data is valid.

---

## THE integration contract (load-bearing — get this wrong and the flow check silently empties)

The MCP `prediction_check` (`breakdown.py:9-36`) matches `breakdown.roles[].role` **string-equal** against `predicted_flows(cfg)` keys — no mapping layer. Those keys are Orca **config-feature** tokens derived from the speed settings (`physics_check.py:_FEATURES`):

`outer_wall, inner_wall, sparse_infill, internal_solid_infill, top_surface, initial_layer, gap_infill, bridge`

The fork emits its native `ExtrusionRole` enum. **The fork must therefore map ExtrusionRole → the Orca config-feature token** so the roles that HAVE a predicted counterpart match by string. This is an Orca→Orca mapping (both vocabularies are OrcaSlicer's own), so it belongs in the fork. Mapping table:

| ExtrusionRole | emitted `role` token | matches a predicted_flows key? |
|---|---|---|
| erExternalPerimeter | `outer_wall` | ✅ |
| erPerimeter | `inner_wall` | ✅ |
| erInternalInfill | `sparse_infill` | ✅ |
| erSolidInfill | `internal_solid_infill` | ✅ |
| erTopSolidInfill | `top_surface` | ✅ |
| erGapFill | `gap_infill` | ✅ |
| erBridgeInfill | `bridge` | ✅ |
| erOverhangPerimeter | `overhang_perimeter` | ❌ (informational) |
| erBottomSurface | `bottom_surface` | ❌ |
| erIroning | `ironing` | ❌ |
| erInternalBridgeInfill | `internal_bridge` | ❌ |
| erSkirt | `skirt` | ❌ |
| erBrim | `brim` | ❌ |
| erSupportMaterial | `support` | ❌ |
| erSupportMaterialInterface | `support_interface` | ❌ |
| erSupportTransition | `support_transition` | ❌ |
| erWipeTower | `wipe_tower` | ❌ |
| erCustom / erMixed | `custom` / `mixed` | ❌ |
| erNone / erCount | (skip — not emitted) | — |

- `initial_layer` is a predicted key with **no** ExtrusionRole counterpart (first layer is cross-cutting, not a role). It simply won't appear in `breakdown.roles`; `prediction_check` omits it. Acceptable.
- Non-matching roles (❌) still SHIP in the breakdown — they answer "which feature is the time hog" (support/skirt/brim are often the surprise). They just don't participate in the flow check.

**MCP-side guard (add in this plan, not the shipped one):** a test in `tests/test_breakdown.py` asserting the fork's documented matchable token set exactly equals `{k for k in predicted_flows(FULL_CFG) if k != "initial_layer"}` — so a future rename on either side fails a test instead of silently emptying the check. Pin the fork token list as a literal in that test with a comment pointing here.

---

## Emitted `breakdown` shape (must satisfy `build_breakdown` / `prediction_check`)

```json
"breakdown": {
  "mode": "normal",
  "total_time_s": 26580.0,
  "roles": [
    {"role": "outer_wall", "time_s": 5400.0, "time_pct": 20.3,
     "filament_mm": 12030.0, "filament_g": 35.9,
     "flow_mm3_s": {"max": 11.8, "mean": 9.2}},
    {"role": "support", "time_s": 3100.0, "time_pct": 11.7, "filament_mm": 8020.0, "filament_g": 23.9,
     "flow_mm3_s": {"max": 14.0, "mean": 12.1}}
  ],
  "metrics": {},
  "layers": []
}
```

- `prediction_check` reads only `role.role` and `role.flow_mm3_s.max` — those two are REQUIRED per role. `flow_mm3_s.max` = the peak observed volumetric flow for that role: for each Extrude move of the role, `flow = mv.mm3_per_mm * feedrate` (or `extruded_volume / time`); track the max (and mean for display). Confirm the per-move volume/feedrate fields on `MoveVertex` during implementation (spike did not pin the exact flow field names — verify `mm3_per_mm` / `feedrate` / `volumetric_rate` on the struct).
- `time_s` per role from Task 1's accumulation. `time_pct` = role time / `total_time_s`.
- `metrics` and `layers` may ship empty for v1 (`build_breakdown` passes them through, `{}`/`[]` are valid); populate later if wanted.

---

## Tasks

### Task 1: Per-role time + flow accumulation over `res->moves`
- [ ] In the `EVT_PROCESS_COMPLETED` success branch, extend the existing `res->moves` loop: for `mv.type == EMoveType::Extrude`, accumulate per-`extrusion_role` `time += mv.time[0]`, and track max/sum of per-move volumetric flow (verify the move's volume/rate fields first).
- [ ] Skip `erNone`. Guard the `res == nullptr` case (leave breakdown absent, not crashing).
- **Files:** `RemoteAPIController.cpp` (success branch ~512-549).

### Task 2: ExtrusionRole → token map + assemble `breakdown` JSON
- [ ] Add a `static const std::map<ExtrusionRole,const char*>` (or switch) per the contract table above. Do NOT use `role_to_string()`.
- [ ] Build the `roles` array (one object per role with time>0 or filament>0), pulling filament from `res->print_statistics.used_filaments_per_role` (length ×1000 → mm; grams as-is), time/flow from Task 1, `time_pct` from `total_time_s`.
- [ ] Set `total_time_s = res->print_statistics.modes.front().time`, `mode = "normal"`.
- [ ] Attach as `stats["breakdown"]` in the `set_slice_state` lambda so the cached snapshot carries it (served verbatim by `handle_slice_status`).
- **Files:** `RemoteAPIController.cpp`.

### Task 3: Advertise capability
- [ ] Add `"slice_breakdown"` to the capabilities array in `handle_status()` (~line 267).
- **Files:** `RemoteAPIController.cpp`.

### Task 4: Build, relaunch, live-verify
- [ ] `OrcaRebuild` on max-pc (~48 min). Zero errors required.
- [ ] Relaunch discipline: kill `orca-slicer` → port 13130 drains to 0 → `OrcaRun`.
- [ ] Load a model, slice, then `curl -H "X-Api-Token: <tok>" http://<host>:13130/api/v1/slice/status` → confirm `breakdown.roles[]` present with sane time/filament; confirm `capabilities` includes `slice_breakdown`.
- [ ] From the server: `get_slice_breakdown` now returns `available:true`; `prediction_check` produces `matches`/`clamped`/`anomaly` verdicts (not empty) — proving the token contract holds.

### Task 5: MCP-side integration guard + docs
- [ ] Add the token-contract test described above to `tests/test_breakdown.py`.
- [ ] Update `docs/superpowers/plans/2026-07-19-e2e-verification.md` results (F14 closed) and memory `test-run-findings-2026-07-19`.
- **Files (orcaslicer-mcp repo):** `tests/test_breakdown.py`, docs, memory.

---

## Global Constraints
- Fork commits are relayed via the orca-relay push flow; the **PC repo is the dev tree** (edit on max-pc / scp over, commit on PC, relay-push). Never commit in `3d-printer/orca-relay` (phantom staged changes are benign).
- Commit convention on the fork follows its own history; **no `Co-Authored-By: Claude` trailer**.
- A rebuild REPLACES the running fork (currently the known-good `1776a884`). Rebuild only when the plate/print state can tolerate the fork restarting. (A Pi print is unaffected — it runs on Klipper, not the fork.)
- Verify the per-move flow field names on `MoveVertex` before relying on them (spike pinned time + filament, not the exact flow field).
