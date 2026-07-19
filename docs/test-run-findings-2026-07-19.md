# Test-run findings — 2026-07-19 (live "fix a slicing error" task)

Context: Max asked me (Claude, on homeserver) to "fix the slicing error I see on
OrcaSlicer" on max-pc. Real error: **"A G-code path goes beyond plate boundaries."**
Real cause: a 249.9 × 269.9 × 94 mm "Case Bottom" part centered on the 300×300 bed
(~15 mm Y clearance/side) — the skirt/brim drawn around it crossed the plate edge.
This is a **retrospective of every friction point using the product**, for a later
working session. Not fixes — just the issue list, prioritized.

Legend: **P1** blocks core use · **P2** major friction · **P3** annoyance.

---

## A. Product reachability — the product wasn't actually usable as a product

- **[P1] orcaslicer-mcp is not wired as an MCP server in the live session.**
  `~/projects/3d-printer/.mcp.json` only wires the *legacy* `3dprint`
  (`mcp-3d-printer-server`, Moonraker control-only) — and even that points at a
  macOS `SLICER_PATH` (`/Applications/OrcaSlicer.app/...`) that doesn't exist on
  this Linux server. The settings-intelligence product was never callable as
  `mcp__*` tools. I drove the Remote API by hand-writing `curl` scripts against
  `/api/v1/*`. **The product-under-test was bypassed entirely.**
- **[P1] No discovery path for the API without repo source.** To talk to the API
  I had to open `orcaslicer-mcp/src/.../client.py` to learn the routes
  (`/api/v1/objects`, `/api/v1/config`, ...) and the auth header (`X-Api-Token`).
  An agent/user without the repo checked out could not have done this.
- **[P2] The differentiator tools were never exercised** — `consult`,
  `check_profile_physics`, `remember`. This was a real slicing failure and I
  never got to point the product at it (see also C1: it wouldn't have fired).

## B. Diagnosis blind spots — the API can't see the error it's meant to fix

- **[P1] Slice warnings/errors are invisible over the API.** `/api/v1/status`
  returned `"slice_result_valid": true` while the GUI showed a red
  "path goes beyond plate boundaries" toast. There is **no endpoint that surfaces
  the plater warning/error list**. I could only learn the actual error by
  screenshotting the GUI. An API-driven agent literally cannot read the problem
  the user is asking about.
- **[P1] No placement / bounding-box validation.** `/objects` gives `size_mm`
  and `offset`, `/status` gives neither a gcode bbox nor an "out of bounds" flag.
  I could not confirm *what* crossed the boundary (skirt vs brim vs travel) from
  the API — arithmetic on size+offset+brim+skirt kept landing under 300, so I
  fell back to visual inspection. Need a "does this plate fit / what pokes out"
  capability.
- **[P2] No aggregate "diagnose this plate" entry point.** I manually chained
  `status → objects → config(brim/skirt) → config(bed)` and still couldn't close
  it. Four+ round-trips for one question.
- **[P2] Selected-preset ambiguity cost a wrong turn.** I first read the bed size
  from a *file* (`Artillery Sidewinder X2 0.4 nozzle.json`) before learning from
  `/status` that the *selected* printer was `Sidewinder X2 0.8mm Klipper`. Live
  config should be the only source I need; querying the file was a detour.

## C. Scope of "settings intelligence" — this error class isn't covered

- **[P2] The physics gate doesn't cover geometric/placement failures.** This was
  an out-of-bounds toolpath error, not a flow/thermal/coupling issue, so
  `check_profile_physics` would not have caught or explained it. Worth deciding
  explicitly whether plate-fit / skirt-brim-clearance belongs in the product's
  remit or is out of scope. Right now it's a silent gap.

## D. Verify-the-fix loop is also blind

- **[P1] Can't confirm a fix worked via the API.** The natural loop —
  read error → mutate (transform/arrange) → re-slice → confirm warning gone —
  breaks at both ends: can't read the warning (B1) and can't read that it cleared.
  The product can't close its own loop for this error class.

## E. Remote-control / observability friction (control-machines layer, not the product core)

- **[P2] Direct-SSH screenshot fails ("handle is invalid") — session 0.** Capture
  has to run in the interactive console session. I had to register an interactive
  scheduled task (`OrcaShot`, mirroring `OrcaRun`) to grab session 1. The
  control-machines skill's screenshot recipe doesn't warn about this.
- **[P2] No image tooling on the server** (no PIL, no ImageMagick). Couldn't
  crop/zoom the screenshot to read the small error toast or inspect the plate —
  had to rely on full-res reads. A crop step would've made the toast legible fast.

## F. PowerShell-over-SSH overhead

- **[P2] Inline PowerShell with nested quotes/regex silently mangles** (hit the
  `'...' is not recognized as an internal or external command` failure again).
  Memory already documents this; the reliable pattern is stage-a-`.ps1` + run
  `-File`, but that's **write + scp + run = 3 steps per query**. Every trivial
  read of a config value became a file-staging exercise.
- **[P3] `curl.exe` + JSON-body quirks** (documented) didn't bite this run — only
  GETs — but they lurk for the mutation/PUT step of any real fix.

## G. Environmental / maintenance

- **[P2] `sync_system_preset` had flipped back to `true`** in `OrcaSlicer.conf`
  (Rule 7 violation; `sync_user_preset` correctly `false`). Recurring drift of
  the Orca-profile-as-truth model, and it can't be fixed while Orca is open (Orca
  rewrites conf on close). Pending: flip back next time Orca is shut.
- **[P3] `reachable max-pc` false-negative** (tailscale node `karaoke-gpu`) —
  known, worked around with a direct `ssh` probe.
- **[P3] `3dprint` MCP server disconnected/reconnected mid-session** — flaky
  transport noise.
- **[P3] Left artifacts on max-pc**: throwaway scripts in `G:\orca-dev\`
  (`_shot/_probe/_bed/_objects/_cfg/_bed2.ps1`) + the `OrcaShot` scheduled task.
  Cleanup pending.

---

## Top candidates to work through next test (my ranking)

1. **Wire orcaslicer-mcp into `.mcp.json`** so the product is callable at all (A1).
2. **Expose slice warnings/errors over the API** — the single biggest blind spot (B1, D1).
3. **Add placement/bbox validation** ("what pokes past the bed") (B2).
4. **A `diagnose`/`inspect_plate` aggregate** that returns objects + bed + active
   brim/skirt + warnings in one call (B3, F1).
5. Decide whether geometric/plate-fit belongs in the physics/settings remit (C1).

---

## Execution progress — 2026-07-19 (same session, after the catalogue)

**Shipped (no-rebuild batch, TDD, orcaslicer-mcp @ working tree; 128 passed / 1 skipped):**
- **A1 DONE** — `orca` server wired into `3d-printer/.mcp.json` via a bash wrapper that
  sources `ORCA_API_TOKEN` from the **gitignored `.env`** (token staged from the max-pc
  conf; verified `git check-ignore` + untracked). `ORCA_API_URL=http://100.84.203.81:13130`
  (Tailscale). Takes effect on next Claude Code MCP reload.
- **B3 DONE** — `diagnose_plate()` tool: status + objects + bed/skirt/brim + slice warnings
  in one call. (`server.py`, `test_server_diagnose.py`.)
- **B2 DONE** — `placement.py` (pure) + `check_placement()` tool: advisory per-object
  fit incl. skirt/brim ring, per-edge clearance, overflow. (`test_placement.py`,
  `test_server_placement.py`.)
- **B1-consumer / D1 DONE** — `get_slice_warnings()` tool; inert-but-correct until the fork
  populates the boundary notification (see below).

**TWO live discoveries the smoke test caught (mocks could not):**
- **NEW P1 — `/config` multi-key query is broken end-to-end, fork-side.** The fork splits
  the RAW `keys` query on `,` **without URL-decoding**, so httpx's `%2C`-encoded comma
  returns `{}`. This silently broke `client.get_config([...])` for ALL multi-key reads
  (incl. `compare_settings`) — invisible because every test mocks HTTP. **FIXED client-side
  (no rebuild):** `get_config` now fetches full config (~20KB) and filters locally
  (`test_client_config.py`). The *proper* fork-side fix (URL-decode before split) is a
  separate fork-batch item.
- **B1 is only PARTLY missing.** The API **already** exposes `Print::validate`-class
  warnings as structured objects `{code, level, message}` (saw
  `bed_temperature_too_high_than_filament` live). What it does NOT surface is specifically
  the **plater/gcode-boundary notification** ("A G-code path goes beyond plate
  boundaries"). So the fork fix is NARROWER than the plan assumed: route the plater
  NotificationManager boundary warnings into the existing `warnings[]` channel — the
  Python consumer (`summarize_slice` → `get_slice_warnings`/`diagnose_plate`) then lights
  up with zero further change. Also note warnings are OBJECTS, not strings.

**Environmental:** max-pc intermittently unreachable this session (`ReadError`/`UiTimeout`)
— the documented Modern-Standby/NIC flakiness, compounded by the Remote API running on
Orca's GUI thread (an active slice blocks it). Tools degrade to clean error dicts. Not a bug.

**Remaining (fork batch, needs the ~40-min rebuild + user confirmation):**
- B1 proper: populate the plate-boundary notification into `/slice/status` `warnings[]`
  (grep-locate the source first — U2 investigator stubbed).
- `/config` `%2C` decode fix (bundle into the same rebuild).
- Then D1 loop closes automatically via the already-shipped consumer tools.
