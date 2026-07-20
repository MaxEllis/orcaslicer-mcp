# orcaslicer-mcp — End-to-End Verification Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to run this plan suite-by-suite. Steps use checkbox (`- [ ]`) syntax. This is a **verification** plan: each step *observes real behavior against the live fork* and records PASS/FAIL — it does not build features. Do not mark a step PASS on assumption; only on observed output matching the expectation.

**Goal:** Prove — with no stone unturned — that every orcaslicer-mcp feature actually works end-to-end against the live OrcaSlicer fork, catching the "looks done but isn't" gaps (`%2C` config break, half-wired B1, unwired MCP) that mocked unit tests missed.

**Architecture:** Drive each feature two ways and cross-check: (A) the **MCP tool surface** the way an agent calls it (`mcp__orca__*` once Claude Code has reloaded `.mcp.json`, else the identical `srv.<tool>()` module path), and (B) the **raw Remote API** via `curl` as ground truth. Divergence between A and B is a bug (that is exactly how `%2C` hid). Every mutation snapshots state first and restores it after. Slicing computes G-code only — **no physical print is ever started** (explicitly out of scope).

**Tech Stack:** Python 3.12 + FastMCP (the MCP server), httpx client, the OrcaSlicer fork's Remote HTTP+WS API on max-pc (Tailscale `<orca-host>:13130`), pytest+respx (unit backstop), bash+curl+ssh harness from homeserver.

## Global Constraints

- **Live target:** the freshly-rebuilt fork (branch `remote-api`, this session's build adding `TOOLPATH_OUTSIDE` + `/config` URL-decode) must be running on max-pc with the Remote API bound on `<orca-host>:13130`. Confirm in Suite 0.
- **Token:** never printed to the transcript or any git-tracked file. Always sourced from the gitignored `/home/max/projects/3d-printer/.env` (`set -a; . .env; set +a`).
- **No physical printing.** Never call anything that starts a print or moves/heats the printer hardware. The orcaslicer-mcp product does not print (it slices only); the legacy `3dprint`/Moonraker tools that *can* print are covered read-only in Appendix A with all hardware-affecting actions excluded.
- **State safety:** every mutating step is bracketed by snapshot → mutate → assert → restore. Never mutate Max's tuned presets in place; operate on throwaway copies. Restore the config, selected presets, and plate to their pre-test state in Suite 12 (teardown).
- **Environment reality:** max-pc drops/UiTimeouts are expected (Modern-Standby NIC + the Remote API runs on Orca's GUI thread, so an in-progress slice blocks it). Every live call uses the retry wrapper from Suite 0; a call that fails all retries is recorded as `UNREACHABLE`, not `FAIL`, and retried after the box settles.
- **PASS/FAIL discipline:** record each step's actual observed output in the results matrix (Suite 13). "No stone unturned" = every one of the 36 tools + 21 routes + WS + knowledge/physics has an explicit row with an observed result.

---

## Suite 0: Preconditions & Test Harness

**Files:**
- Create: `/tmp/claude-1000/.../scratchpad/verify/` (harness scripts + fixtures — scratch, not committed)
- Reference: `/home/max/projects/3d-printer/.env` (token), `~/projects/orcaslicer-mcp/.venv`

**Interfaces produced (used by all later suites):**
- `RAW <METHOD> <path> [body-file]` — curl helper against the live API, token sourced, prints JSON.
- `TOOL <pyexpr>` — invoke a real `srv.<tool>()` coroutine against the live API with a 3-try ride-through-UiTimeout wrapper; prints JSON or `UNREACHABLE`.
- `SNAP_CFG <keys...>` / `RESTORE_CFG` — capture & restore specific config keys.

- [ ] **0.1 Confirm the rebuilt fork is live and is the NEW build.**
  Run:
  ```bash
  set -a; . ~/projects/3d-printer/.env; set +a; U=http://<orca-host>:13130
  curl -s -H "X-Api-Token: $ORCA_API_TOKEN" "$U/api/v1/status" | python3 -m json.tool
  ```
  Expect: HTTP 200 JSON; `app_version` `2.3.2`; `capabilities` includes `status,config,slice,events,model,preset,gcode,objects,arrange,orient,object_config`. Record the `capabilities` list — it gates M4a/M4b/M4c suites.

- [ ] **0.2 Confirm this is the post-rebuild binary (boundary + %2C fixes present).**
  Run (the `%2C` regression, one-liner): `curl -s -H "X-Api-Token: $ORCA_API_TOKEN" "$U/api/v1/config?keys=printable_area%2Cbrim_width" | python3 -c "import sys,json;print(json.load(sys.stdin)['config'])"`
  Expect: **non-empty** dict `{'brim_width': ..., 'printable_area': ...}`. (Pre-rebuild this returned `{}` — an empty result here means the wrong/old binary is running: STOP and relaunch the new build.)

- [ ] **0.3 Write the harness.** Create `scratchpad/verify/harness.sh` exposing `RAW`, and `scratchpad/verify/tool.py` exposing a retry wrapper:
  ```python
  # tool.py — usage: python tool.py 'get_status()'  (or any srv coroutine expr)
  import asyncio, json, sys, orcaslicer_mcp.server as srv
  from orcaslicer_mcp.errors import NotReachable, UiTimeout
  async def run(expr):
      for i in range(3):
          try:
              return await eval("srv."+expr)
          except (NotReachable, UiTimeout) as e:
              if i == 2: return {"_UNREACHABLE": str(e)}
              await asyncio.sleep(4)
  print(json.dumps(asyncio.run(run(sys.argv[1])), indent=2, default=str))
  ```
  Driver env prelude (every `TOOL` call): `cd ~/projects/orcaslicer-mcp; set -a; . ~/projects/3d-printer/.env; set +a; export ORCA_API_URL=http://<orca-host>:13130 ORCA_API_TIMEOUT=30`.
  Verify the harness: `TOOL 'get_status()'` prints a status dict (not `_UNREACHABLE`).

- [ ] **0.4 Full config snapshot (master restore point).**
  Run: `RAW GET /api/v1/config > scratchpad/verify/config-snapshot.json` and record the currently selected presets from `status.presets` (printer/print/filaments) into `scratchpad/verify/presets-snapshot.json`. These are the ground-truth for Suite 12 teardown.

- [ ] **0.5 Record the loaded plate.**
  Run: `TOOL 'get_objects()'` → save to `scratchpad/verify/objects-snapshot.json`. Note object ids/names/instances so mutation suites can restore the plate (or reload the known fixture).

- [ ] **0.6 Fixtures present.** Confirm `G:\orca-dev\cube20.stl` exists on max-pc (`ssh max-pc 'if exist G:\orca-dev\cube20.stl (echo OK)'`). This is the in-bounds fixture; the out-of-bounds case is produced by `transform_object` in Suite 6, not a separate STL.

**Suite 0 pass criteria:** live NEW build confirmed (0.1, 0.2 non-empty), harness returns real data (0.3), snapshots saved (0.4–0.5).

---

## Suite 1: Wiring — the product is callable as `mcp__orca__*`

**Covers:** A1 (the gap that made the product uncallable). Proves `.mcp.json` wiring, not just the module path.

- [ ] **1.1 Confirm Claude Code loaded the `orca` server.** In the executing session, confirm `mcp__orca__*` tools are present (a `<system-reminder>`/tool listing shows them, or `ToolSearch "select:mcp__orca__get_status"` resolves). If absent, the `.mcp.json` reload hasn't happened — note it, and the operator must approve/reload the `orca` server before Suite 1 can PASS. Functional suites 2–11 still run via `TOOL` (module path) meanwhile.
- [ ] **1.2 Wiring smoke.** Call `mcp__orca__get_status`. Expect a dict with `slice_result_valid` and `capabilities` — proving the wrapper sourced the token from `.env` and reached max-pc over Tailscale.
- [ ] **1.3 A↔B parity.** Compare `mcp__orca__get_status` (MCP surface) against `RAW GET /api/v1/status` (ground truth). Expect the same `app_version`, `capabilities`, and `slice_result_valid`. Any field the MCP layer drops/renames vs raw is a serialization bug — record it.
- [ ] **1.4 Secret hygiene (regression of the wiring commit).** Run `git -C ~/projects/3d-printer grep -I "$(cut -d= -f2 ~/projects/3d-printer/.env | cut -c1-6)" || echo CLEAN` and `git -C ~/projects/3d-printer check-ignore .env`. Expect `CLEAN` and `.env` printed — the token is nowhere in tracked files.

**Pass criteria:** `mcp__orca__get_status` works (1.2) and matches raw (1.3); token not committed (1.4). If 1.1 fails, mark Suite 1 BLOCKED-on-reload and proceed with `TOOL`.

---

## Suite 2: Read-only status & config surface

**Covers tools:** `get_status`, `get_config`, `find_config_keys`, `diagnose_plate`, `check_placement`, `get_slice_status`, `get_job_status`.

- [ ] **2.1 `get_status`** — `TOOL 'get_status()'`. Expect keys `app/app_version/capabilities/presets/objects/slice_result_valid/slicing/project`.
- [ ] **2.2 `get_config` single key** — `TOOL "get_config(['layer_height'])"`. Expect `{'layer_height': '<value>'}` (one key).
- [ ] **2.3 `get_config` MULTI-key (the %2C regression through the client)** — `TOOL "get_config(['printable_area','brim_width','skirt_loops'])"`. Expect all three keys present with real values. (This is the exact call that silently returned `{}` before the client fetch-all fix + fork decode. Non-empty, all-3 = PASS.)
- [ ] **2.4 `get_config(None)` full** — `TOOL 'get_config(None)'`. Expect ~600 keys. Record the count.
- [ ] **2.5 `find_config_keys`** — `TOOL "find_config_keys('brim')"`. Expect a `keys` list including `brim_width`,`brim_type`,`brim_object_gap`.
- [ ] **2.6 `diagnose_plate`** — `TOOL 'diagnose_plate()'`. Expect keys `{status,objects,adhesion_bed,slice}`; `adhesion_bed.printable_area` is a real string like `"0x0,300x0,300x300,0x300"` (NOT null — the %2C fix makes this parse); `slice.valid` mirrors `status.slice_result_valid`.
- [ ] **2.7 `check_placement`** — `TOOL 'check_placement()'`. Expect `bed.min=[0,0] bed.max=[300,300]` (NOT `bed:null`), `ring_mm` a float, and per-object `clearances` with numeric left/right/front/back. Cross-check the current object against the bed by hand (offset±size/2 within 0–300).
- [ ] **2.8 `get_slice_status`** — `TOOL 'get_slice_status()'`. Expect `state/percent/message/stats/warnings`.
- [ ] **2.9 `get_job_status`** — `TOOL 'get_job_status()'`. Expect an idle/busy indicator dict (or `needs M4b` degraded — record which).

**Pass criteria:** all read tools return well-formed live data; 2.3 and 2.6/2.7 specifically show non-empty config/bed (the regression anchors).

---

## Suite 3: Config mutation (snapshot → mutate → assert → restore)

**Covers tools:** `set_config`, `apply_and_slice`, `compare_settings`.

- [ ] **3.1 Snapshot** — `TOOL "get_config(['fan_max_speed'])"`; record original.
- [ ] **3.2 `set_config` apply** — `TOOL "set_config({'fan_max_speed':'80'})"`. Expect `applied` includes `fan_max_speed`, `errors` empty.
- [ ] **3.3 Read-back** — `TOOL "get_config(['fan_max_speed'])"` → `'80'`. (Proves PUT actually landed, not just echoed.)
- [ ] **3.4 Atomic-batch rejection** — `TOOL "set_config({'fan_max_speed':'70','not_a_real_key_xyz':'1'})"`. Expect a 422/`errors` naming the bad key AND `fan_max_speed` NOT changed (still `'80'` on read-back) — confirms the atomic all-or-nothing semantics.
- [ ] **3.5 Restore** — `TOOL "set_config({'fan_max_speed':'<original>'})"`; read-back equals original.
- [ ] **3.6 `compare_settings` (multi-key snapshot path — %2C regression #2)** — `TOOL "compare_settings('sparse_infill_density', ['10%','25%'])"`. Expect two rows, each with slice stats (filament/time) differing, and the **original value restored afterward** (`get_config(['sparse_infill_density'])` == pre-test). This tool internally does a multi-key `get_config` snapshot — the exact path `%2C` broke; verify it restores correctly.
- [ ] **3.7 `apply_and_slice`** — `TOOL "apply_and_slice({'sparse_infill_density':'15%'})"`. Expect `{applied, errors, result:{stats,warnings,...}}` with a real slice result. Restore density afterward via `set_config`.

**Pass criteria:** mutations land and read back; atomic rejection holds (3.4); `compare_settings` restores state (3.6).

---

## Suite 4: Preset lifecycle (throwaway presets only — never touch tuned ones)

**Covers tools:** `list_presets`, `get_preset_config`, `select_preset`, `save_preset`, `edit_preset`, `rename_preset`, `delete_preset`.

- [ ] **4.1 `list_presets`** — `TOOL 'list_presets()'`. Expect `filaments/print/printer` lists; the selected ones match `status.presets`.
- [ ] **4.2 `get_preset_config`** — `TOOL "get_preset_config('print', '<current print preset name>')"`. Expect a config dict for that preset.
- [ ] **4.3 `save_preset` (create a throwaway)** — first `set_config({'brim_width':'7'})`, then `TOOL "save_preset('print', 'ZZ_verify_tmp', detach=True)"`. Expect success; `list_presets` now shows `ZZ_verify_tmp`.
- [ ] **4.4 `select_preset`** — `TOOL "select_preset('print', 'ZZ_verify_tmp')"`; `get_status` shows it selected.
- [ ] **4.5 `edit_preset`** — `TOOL "edit_preset('print','ZZ_verify_tmp',{'brim_width':'9'})"`; `get_preset_config` read-back shows `brim_width=9`.
- [ ] **4.6 `rename_preset`** — `TOOL "rename_preset('print','ZZ_verify_tmp','ZZ_verify_tmp2')"`; `list_presets` shows the new name, not the old.
- [ ] **4.7 Re-select original + `delete_preset`** — select the pre-test print preset (from 0.4 snapshot), then `TOOL "delete_preset('print','ZZ_verify_tmp2')"`; `list_presets` no longer lists it. Restore `brim_width` via `set_config`.

**Pass criteria:** full create→select→edit→rename→delete cycle observable; the ORIGINAL tuned preset is re-selected and unmodified at the end.

---

## Suite 5: Model & object lifecycle (M4b/M4c)

**Covers tools:** `load_model`, `list_objects`, `set_object_config`, `duplicate_object`, `transform_object`, `delete_object`, `arrange_plate`, `auto_orient`, `get_job_status`, `set_layer_height`, `set_height_range`.

- [ ] **5.1 `load_model` known fixture** — `TOOL "load_model('G:/orca-dev/cube20.stl')"`. Expect success; `list_objects` count ≥ 1 with a `cube20` object; record its `id`.
- [ ] **5.2 `list_objects` shape** — each object has `id/index/name/instances/size_mm/transform{offset,rotation,scale}`.
- [ ] **5.3 `duplicate_object`** — `TOOL "duplicate_object(<id>)"`; count increases by 1.
- [ ] **5.4 `transform_object` translate** — `TOOL "transform_object(<id>, translate=[10,0,0])"`; read-back offset x shifted by 10.
- [ ] **5.5 `transform_object` rotate+scale** — `TOOL "transform_object(<id>, rotate=[0,0,45], scale=[1.5,1.5,1.5])"`; read-back `rotation`/`scale`/`size_mm` reflect it.
- [ ] **5.6 `set_object_config` (M4c per-object)** — `TOOL "set_object_config(<id>, {'wall_loops':'4'})"`; read-back via `list_objects`/objects config shows the per-object override.
- [ ] **5.7 `set_layer_height` (M4c)** — `TOOL "set_layer_height(<id>, 'adaptive', 0.4)"`; expect success/`custom_layer_profile` true; then `TOOL "set_layer_height(<id>, 'default')"` to clear.
- [ ] **5.8 `set_height_range` (M4c)** — `TOOL "set_height_range(<id>, min_z=0, max_z=5, layer_height=0.3)"`; read-back shows a height range; then `TOOL "set_height_range(<id>, clear=True)"`.
- [ ] **5.9 `arrange_plate`** — `TOOL 'arrange_plate()'`; then `TOOL 'get_job_status()'` until idle; objects re-centered (offsets changed, all within bed).
- [ ] **5.10 `auto_orient`** — `TOOL 'auto_orient()'`; poll `get_job_status` to idle; no error.
- [ ] **5.11 `delete_object` (cleanup dup)** — `TOOL "delete_object(<dup id>)"`; count back down.

**Pass criteria:** every object op observably changes plate state and reads back; M4c per-object tools land (or degrade with an explicit `needs M4c` recorded). Leave exactly one clean object for Suite 6.

---

## Suite 6: Slicing, warnings & the boundary-warning regression (B1/D1)

**Covers tools:** `slice`, `slice_and_wait`, `get_slice_status`, `get_slice_warnings`, `get_gcode`. **This is the D1 close-the-loop suite and the primary check of this session's fork change.**

- [ ] **6.1 In-bounds slice** — ensure one cube centered (`transform_object` offset `[150,150,...]`). `TOOL 'slice_and_wait()'`. Expect `state` `done`, real `stats` (filament/time).
- [ ] **6.2 `get_slice_warnings` — negative control** — `TOOL 'get_slice_warnings()'`. Expect `warnings` does NOT contain `TOOLPATH_OUTSIDE` (object is in-bounds). Validate warnings are objects `{code,level,message}`.
- [ ] **6.3 Force out-of-bounds** — `TOOL "transform_object(<id>, translate=[170,0,0])"` (pushes the cube's toolpaths past x=300), then `TOOL 'slice_and_wait()'`.
- [ ] **6.4 `get_slice_warnings` — POSITIVE (the fix)** — `TOOL 'get_slice_warnings()'`. Expect `warnings` now contains an entry with `code == 'TOOLPATH_OUTSIDE'` and message "A G-code path goes beyond the plate boundaries." **This is the exact thing the API could not see before the rebuild.** Cross-check raw: `RAW GET /api/v1/slice/status` shows the same entry.
- [ ] **6.5 `diagnose_plate` surfaces it too** — `TOOL 'diagnose_plate()'`; `slice.warnings` contains the `TOOLPATH_OUTSIDE` entry (proves the aggregate composes the fork warning with zero extra code).
- [ ] **6.6 `check_placement` agreement** — `TOOL 'check_placement()'`; the out-of-bounds object shows `fits=false`, `overflow_mm>0` on the right edge (advisory estimate agrees with the fork's ground-truth warning).
- [ ] **6.7 Close the D1 loop** — `TOOL "transform_object(<id>, translate=[-170,0,0])"` back to center, `TOOL 'slice_and_wait()'`, `TOOL 'get_slice_warnings()'` → `TOOLPATH_OUTSIDE` **gone**. This is the read→fix→confirm loop the product previously could not close.
- [ ] **6.8 `slice` (bare)** — `TOOL 'slice()'` on an already-valid plate returns `already_valid:true` (no redundant reslice).
- [ ] **6.9 `get_gcode` (M4a)** — `TOOL 'get_gcode()'`. Expect `{bytes:<n>, gcode:"..."}` with n>0 and the text starting with G-code (`;` header / `G` moves). (Do not print the whole payload — assert length + first line.)

**Pass criteria:** 6.4 shows `TOOLPATH_OUTSIDE` present, 6.7 shows it cleared — the B1/D1 fix is proven live. 6.9 returns real G-code.

---

## Suite 7: Settings-intelligence (the knowledge + physics gate)

**Covers tools:** `consult`, `check_profile_physics`, `remember`, and the 21-file knowledge base + `run_checks`.

- [ ] **7.1 `consult` retrieval** — `TOOL "consult('stringing')"`. Expect `chunks` non-empty, each `{file,title,content}`, at least one from `failures/stringing.md`; plus a `notes` list (possibly empty).
- [ ] **7.2 `consult` physics topic** — `TOOL "consult('volumetric flow limit large nozzle')"`. Expect a chunk from `physics/flow-limits.md` or `couplings.md` and/or `considerations/large-nozzle.md`.
- [ ] **7.3 Knowledge coverage sweep** — `TOOL "consult('elephant foot')"`, `"consult('warping')"`, `"consult('layer adhesion')"`, `"consult('overhang')"` each return ≥1 relevant chunk. Record which of the 21 KB files are reachable; any file never surfaced by an obvious query is a retrieval gap.
- [ ] **7.4 `check_profile_physics` on live config** — `TOOL 'check_profile_physics()'`. Expect a list of `CheckResult`s across flow/temperature/geometry/cooling; each has a pass/warn/fail verdict + message. Record the verdicts for the current 0.8mm profile.
- [ ] **7.5 `check_profile_physics` catches the canonical over-flow case** — `TOOL "check_profile_physics({'filament_max_volumetric_speed':'25','nozzle_temperature':'205'})"` (the real 2026-07-18 0.8mm case: demand exceeds what 205 °C sustains). Expect a FAIL/WARN on the temp–flow coupling (per `couplings.md`: PLA sustainable ≈ (T−195)/1.2 ≈ 8.3 mm³/s ≪ demand). This proves the physics gate actually computes, not just retrieves text.
- [ ] **7.6 `check_profile_physics` clean case** — `TOOL "check_profile_physics({'filament_max_volumetric_speed':'16','nozzle_temperature':'215'})"`. Expect the coupling check passes (the fix from the 0.8mm tuning). Confirms it distinguishes good from bad.
- [ ] **7.7 `remember` → `consult` roundtrip** — `TOOL "remember('E2E verify note: test marker alpha', scope='general')"`; then `TOOL "consult('test marker alpha')"` returns that note in `notes`. Then remove the scratch note file so it doesn't pollute the store (record the notes dir path; delete the added line).

**Pass criteria:** retrieval returns real KB content (7.1–7.3), the physics gate computes and correctly flags 7.5 vs passes 7.6, and remember/consult roundtrips (7.7).

---

## Suite 8: Events (WS stream)

**Covers tools:** `watch_events` and the `/api/v1/events` WS.

- [ ] **8.1 `watch_events` captures a slice event** — start `TOOL "watch_events(20)"` in the background; within its window trigger a mutation+slice from a second shell (`TOOL "set_config({'fan_max_speed':'75'})"` then `TOOL 'slice_and_wait()'`). Expect the returned `events` list to contain `config.changed` and/or `slice.*` (`slice.started`/`slice.progress`/`slice.done`) frames. Restore `fan_max_speed`.
- [ ] **8.2 WS auth/URL** — confirm the WS connected (non-empty events or a clean idle return, not an auth error). If empty, cross-check `RAW`-equivalent by re-running during an actual slice.

**Pass criteria:** at least one real event frame observed (8.1). If the box is mid-slice-flaky, retry once; only mark FAIL if events never arrive during a confirmed slice.

---

## Suite 9: Resilience & degradation (error paths are features too)

**Covers:** the error contracts every tool relies on (`_err`, `_m4b_err`, `_m4a_err`), unreachable handling, milestone degradation.

- [ ] **9.1 Not-found object** — `TOOL "transform_object(999999, translate=[1,0,0])"`. Expect a clean error dict (`error` key), NOT an exception/crash.
- [ ] **9.2 Bad preset** — `TOOL "get_preset_config('print','__no_such_preset__')"`. Expect a clean error dict.
- [ ] **9.3 Unreachable handling** — temporarily point at a dead port: `ORCA_API_URL=http://<orca-host>:1 TOOL 'get_status()'`. Expect a `NotReachable`-derived error dict / `_UNREACHABLE`, not a stack trace. (Restore the real URL.)
- [ ] **9.4 Milestone degradation honesty** — for any capability NOT in `status.capabilities` (from 0.1), confirm its tool returns the matching `needs M4a/M4b/M4c` message rather than a hard failure. If all capabilities are present, record "N/A — full build" and confirm at least one M4b tool (e.g. `list_objects`) returns real data, not the degraded string.

**Pass criteria:** every error path yields a structured dict; nothing throws to the caller.

---

## Suite 10: Unit + regression backstop (pytest)

**Covers:** the mocked suite as a fast regression net beneath the live checks.

- [ ] **10.1 Full suite green** — `cd ~/projects/orcaslicer-mcp && .venv/bin/python -m pytest -q`. Expect `128 passed, 1 skipped` (or more if this plan adds tests). Record the exact count.
- [ ] **10.2 The two regression tests exist and pass** — confirm `tests/test_client_config.py` (%2C guard) and `tests/test_server_diagnose.py`/`test_server_placement.py` (new tools) are in the run.
- [ ] **10.3 KB lint** — the knowledge-base lint test (schema/frontmatter/anti-template) passes, guarding the 21 KB files.

**Pass criteria:** full suite green; the regression tests are present.

---

## Suite 11: A↔B parity sweep (the anti-`%2C` audit)

**Covers:** the core lesson — MCP surface must equal raw API. For a representative read of each family, diff MCP vs raw.

- [ ] **11.1** `get_status` vs `RAW GET /status` — same core fields (done in 1.3; re-confirm).
- [ ] **11.2** `get_config(['printable_area','brim_width'])` vs `RAW GET /config?keys=printable_area%2Cbrim_width` — identical values (both non-empty; this is the definitive `%2C` closure across the whole stack).
- [ ] **11.3** `list_objects` vs `RAW GET /objects` — same count & ids.
- [ ] **11.4** `list_presets` vs `RAW GET /presets` — same names.
- [ ] **11.5** `get_slice_warnings` vs `RAW GET /slice/status` — same warnings array.

**Pass criteria:** no MCP-vs-raw divergence. Any mismatch = a serialization/wiring bug logged with repro.

---

## Suite 12: Teardown & state restoration

- [ ] **12.1 Restore config** — diff live `RAW GET /config` against `config-snapshot.json` (0.4); `set_config` back any key this run changed (fan_max_speed, sparse_infill_density, brim_width, wall_loops overrides). Re-diff → no residual deltas from testing.
- [ ] **12.2 Restore presets** — re-`select_preset` the printer/print/filament from `presets-snapshot.json`; confirm `status.presets` matches the snapshot. Confirm `ZZ_verify_tmp*` presets are deleted.
- [ ] **12.3 Restore plate** — reload the original model or `load_model` the pre-test fixture so the plate matches `objects-snapshot.json` (0.5); remove test duplicates.
- [ ] **12.4 Notes store clean** — the 7.7 scratch note removed.
- [ ] **12.5 max-pc scratch cleanup** — remove `G:\orca-dev\_shot.ps1,_probe.ps1,_bed.ps1,_bed2.ps1,_objects.ps1,_cfg.ps1,_syncfix.ps1` and the `OrcaShot` scheduled task (from earlier debugging); leave `cube20.stl`, `rebuild.bat`, `verify-*.ps1`.
- [ ] **12.6 Rule 7 re-confirm** — `sync_user_preset` and `sync_system_preset` both `false` in `OrcaSlicer.conf` (a slice/close may have rewritten it — re-verify, re-fix if needed).

**Pass criteria:** live state == pre-test snapshots; no test artifacts left on max-pc or in the notes store.

---

## Suite 13: Results matrix (the deliverable)

Produce a single table — one row per feature — with columns: **Feature | Suite.Step | Method (MCP/RAW/both) | Expected | Observed | PASS/FAIL/UNREACHABLE/BLOCKED**. Rows (must all be present — this is the "no stone unturned" ledger):

- 36 MCP tools: get_status, get_config, set_config, slice, get_slice_status, get_slice_warnings, slice_and_wait, apply_and_slice, compare_settings, list_objects, set_object_config, duplicate_object, delete_object, transform_object, arrange_plate, auto_orient, get_job_status, watch_events, find_config_keys, diagnose_plate, check_placement, consult, check_profile_physics, remember, load_model, select_preset, save_preset, list_presets, set_layer_height, set_height_range, get_preset_config, delete_preset, edit_preset, rename_preset, get_gcode.
- Cross-cutting: `.mcp.json` wiring, `%2C` config (client + fork), `TOOLPATH_OUTSIDE` boundary warning, D1 loop, atomic PUT, secret hygiene, WS events, physics gate compute, KB retrieval coverage, error/degradation contracts, unit suite.
- End with a **summary**: counts of PASS/FAIL/UNREACHABLE/BLOCKED, every FAIL with a one-line repro, and a go/no-go verdict on "the product works end-to-end."

---

## Appendix A: Legacy `3dprint`/Moonraker tools (read-only, hardware actions EXCLUDED)

Not part of orcaslicer-mcp, but wired in `.mcp.json`. Verify read-only reachability only; **do not** run any hardware-affecting call.
- [ ] A.1 `mcp__3dprint__get_printer_status` — returns Klipper state (read-only).
- [ ] A.2 `mcp__3dprint__list_printer_files` — lists files (read-only).
- **EXCLUDED (never run here):** `start_print`, `cancel_print`, `set_printer_temperature`, `upload_gcode`+print, `process_and_print_stl`, and any STL-mutation-then-print pipeline — all have physical side effects. Note them as "excluded by instruction (no physical printing)".

---

## Self-Review (spec coverage)

- **Every one of the 36 tools** appears in Suites 2–8 and the Suite 13 matrix. ✓
- **Every REST route** is exercised via its owning tool + a raw parity check in Suite 11. ✓
- **WS `/events`** — Suite 8. **`/gcode`** — 6.9. ✓
- **Session-found bugs** have explicit regression anchors: `%2C` (2.3, 3.6, 11.2), `TOOLPATH_OUTSIDE`/B1/D1 (6.4–6.7), wiring/A1 (Suite 1), Rule 7 (12.6). ✓
- **Knowledge base (21 files) + physics gate** — Suite 7 (retrieval sweep + compute check on the canonical case). ✓
- **State safety** — snapshot (0.4–0.5), per-suite restore, full teardown (Suite 12). ✓
- **No physical printing** — enforced globally; hardware tools excluded in Appendix A. ✓
- **Environment flakiness** — retry wrapper (0.3), UNREACHABLE≠FAIL rule (Global Constraints). ✓

Open item for the operator: Suite 1 requires Claude Code to have reloaded `.mcp.json` (approve the `orca` server). If not yet reloaded at /goal time, Suites 2–11 still run via the `TOOL` module path; Suite 1 is marked BLOCKED-on-reload until the operator reloads.
