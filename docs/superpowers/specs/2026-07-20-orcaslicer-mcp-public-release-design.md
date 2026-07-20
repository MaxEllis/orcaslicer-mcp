# OrcaSlicer MCP — public release design (soft launch)

**Date:** 2026-07-20
**Status:** approved (brainstorm 2026-07-20)
**Scope:** first public, open-source release of the OrcaSlicer MCP system

## Background

The system today has two working components:

- **orcaslicer-mcp** (this repo) — Python MCP server, v0.1.0, ~150 tests green,
  packaged with hatchling, configured via `ORCA_API_URL` / `ORCA_API_TOKEN` env
  vars. Never published.
- **The fork** (`maxellis/OrcaSlicer`, branch `remote-api`) — OrcaSlicer 2.3.2
  with an embedded HTTP Remote API: token auth (`X-Api-Token`), localhost-only
  by default (`bind_lan=false`), token auto-generated on first run, full
  Preferences → Remote API GUI page (enable toggle, LAN toggle, notifications,
  port field with validation, read-only token display + regenerate button).

The MCP server is useless against stock OrcaSlicer — the fork is a hard
dependency. Both pieces ship together or neither matters.

## Decisions (from brainstorm)

1. **Release model: open source.** Not a hosted service; upstreaming is a
   possible later step, not this one.
2. **Scope: slicing only.** No printer control, no Klipper/Moonraker
   integration. The product is "AI drives your slicer."
3. **Fork distribution: prebuilt binaries via CI** (adapt upstream's release
   workflows). Source-only or MCP-only releases were rejected — both ship a
   car without wheels.
4. **Branding: one product name, "OrcaSlicer MCP."** This repo is the front
   door (README, quickstart, links). The fork is a component: "requires the
   OrcaSlicer MCP build — download here." Fork repo keeps the name
   `maxellis/OrcaSlicer` (clean lineage for a future upstream PR); release
   artifacts carry the product name. "Remote API" language survives only in
   developer docs.
5. **Launch: soft launch now, announce only after the cold-start gate passes**
   (see §Launch gate). Registry listings and community posts are a separate,
   later decision.

## Phase 1 — MCP package release (small, immediate)

- README rewrite: product-first framing ("OrcaSlicer MCP — Claude drives your
  slicer"), quickstart (§below), link to fork binary downloads. Fix stale
  content: M4a/M4b/M4c endpoints are described as "not shipped" but all shipped.
- Add `LICENSE`: **AGPL-3.0**. The bundled settings schema is extracted from
  OrcaSlicer's AGPL-3.0 source (`PrintConfig.cpp`), so matching licenses
  sidesteps any derivative-work question. The fork is AGPL-3.0 regardless (no
  choice — it is OrcaSlicer).
- `pyproject.toml` release metadata: license, authors, project URLs,
  classifiers.
- Repo hygiene before flipping public:
  - History scan result (done 2026-07-20): no tokens and no `.env` in any of the
    66 commits. Safe to publish without history rewrite.
  - Redact-in-place commit: three internal docs contain private LAN/VPN IPs
    (`docs/superpowers/2026-07-19-e2e-verification-results.md`,
    `docs/superpowers/plans/2026-07-19-e2e-verification.md`,
    `docs/test-run-findings-2026-07-19.md`). Replace with placeholders in a
    normal commit; no history rewrite needed (IPs without tokens are not
    exploitable, and the VPN address is unreachable outside the private
    network).
- Publish **v0.1.0 to PyPI** (name `orcaslicer-mcp` verified free 2026-07-20).
  Requires a PyPI account + publish credential (owner action). Mechanism
  (manual `uv publish` vs. GitHub Actions trusted publishing) decided at
  implementation time.
- Flip `maxellis/orcaslicer-mcp` to public.

## Phase 2 — fork binaries via CI (the meaty phase)

- Adapt upstream OrcaSlicer's existing release workflows (already present in
  the fork) to build **Windows / macOS / Linux** artifacts on tag.
- Artifact naming: `OrcaSlicer-MCP-<upstream>-mcp.<N>-<platform>`, e.g.
  `OrcaSlicer-MCP-2.3.2-mcp.1-win64.zip`. Git tag: `v2.3.2-mcp.1`.
- Version scheme: `<upstream version>-mcp.<N>` — N bumps for Remote API
  changes on the same upstream base; rebasing onto a newer upstream resets N.
- Known frictions (from fork CI notes, to budget for):
  - Fork workflows must be enabled in the GitHub UI (fork gate); the
    `disabled_fork` state and workflow_dispatch-needs-default-branch quirks.
  - Prior red runs were a read-only-token results-publish 403 — builds and
    tests actually passed. Fix permissions or drop the publish step.
  - macOS builds will be **unsigned** — README documents the
    right-click-open / `xattr -d com.apple.quarantine` dance.
  - Before the CI Windows build becomes the recommended download, verify it
    behaves identically to the locally-built production build (smoke: launch,
    enable API, slice a model via MCP).

## Quickstart path (what the README teaches)

1. Download the OrcaSlicer MCP build for your platform (fork releases page).
2. Launch it → Preferences → Remote API → **Enable**, copy the token.
   (Localhost-only by default; LAN access is an explicit opt-in toggle.)
3. Add to Claude Desktop / Claude Code config:
   `uvx orcaslicer-mcp` with `ORCA_API_TOKEN` (and `ORCA_API_URL` only if
   non-default).
4. Ask Claude: "load this STL and slice it."

All GUI pieces for step 2 already exist in the fork — no C++ work required
for the quickstart.

## Launch gate (definition of "soft launch done")

On a machine that has never seen this project (fresh Windows VM or new PC user
profile; macOS as second platform), following **only the public README**, a
person gets from download → Claude-sliced gcode in one sitting with zero
insider knowledge. Everything that breaks in that run is the remaining
backlog. Announcing (MCP registries, r/klippers, OrcaSlicer Discussions) only
after this gate passes.

## Out of scope

- Printer control (Klipper / Moonraker / any printer-side MCP).
- Community announcements, registry listings (post-gate decision).
- Upstreaming the Remote API into SoftFever/OrcaSlicer (future option; repo
  naming and clean fork lineage deliberately preserve it).
- Hosted/cloud anything.
- Rebasing the fork onto a newer upstream OrcaSlicer (only if the gate forces
  it, which is not expected).

## Sequencing

Phase 1 → Phase 2 → gate test (Phase 3). Each phase gets its own
implementation plan. Phase 1 can start immediately.
