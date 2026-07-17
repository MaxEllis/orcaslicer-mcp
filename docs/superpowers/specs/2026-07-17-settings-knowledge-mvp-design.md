# Settings Knowledge — static-extraction MVP (design)

Date: 2026-07-17
Repo: orcaslicer-mcp
Status: approved (brainstorm), pending implementation plan

## Context

OrcaSlicer exposes ~700 print/printer/material settings. For an AI driving the
fork through the Remote API + this MCP to work with them well, it needs to know
what each setting *does* and what values are *valid* — not just that a key
exists. Today the MCP's `find_config_keys` returns key **names** only (substring
match over live `GET /config`); it carries no definitions, ranges, or enums.

The authoritative source of that knowledge already ships inside the fork:
`src/libslic3r/PrintConfig.cpp` defines every setting as a `ConfigOptionDef`
carrying `label`, `tooltip` (the exact hover text the UI shows), `category`,
type, `sidetext` (unit), `min`/`max`, `mode`, and enum values/labels. Measured
in the current checkout: 711 `def->label` and 676 `def->tooltip` assignments.

## Problem framing (why this shape)

The original question was "a settings database vs. sending the AI to the
internet." Grounding in the code reframes it:

- The internet is the *wrong* source for setting **definitions/constraints** —
  it returns answers for a different slicer (Prusa/Bambu/Cura) or a different
  version, at a latency/token/hallucination cost. The fork's own metadata is
  strictly more authoritative.
- The internet is the *right* source for print **strategy** ("best PETG
  bridging settings") — experiential/community knowledge that isn't in any
  settings table.

So the target is two independent layers, built in order:

- **Layer 1 — authoritative settings knowledge** (definitions + valid
  ranges/enums). Covers explain, validate, and grounds autonomous tuning.
- **Layer 2 — strategy knowledge** (live internet at answer time, anchored to
  Layer 1). Deferred; out of scope here.

User priorities selected during brainstorm: **accuracy/authority** and
**always current**. Token cost and offline were explicitly not priorities.
Those priorities ultimately favor a *live* fork endpoint (see Future work), but
this MVP intentionally builds the cheap static version first to prove the
capability is actually used before spending a fork build cycle.

## Goals

1. Give the AI an authoritative, offline lookup of OrcaSlicer setting
   definitions and constraints via the MCP.
2. Do it with zero fork changes and zero PC/build/push coordination — the
   extraction runs on the relay box where the source lives.
3. Make it cheap enough to be a throwaway experiment: if explain/validate/tune
   don't actually get used, little was spent; if they do, promote to the live
   endpoint.

## Non-goals (explicitly deferred)

- The live `GET /config/schema` fork endpoint (Approach A) — Future work.
- PUT-time validation **enforcement** / a "block the slice" gate. The MVP
  surfaces the data needed to validate; it does not gate anything.
- Cross-setting interaction rules (e.g. "X requires Y > 0").
- The Layer 2 internet/strategy path.
- Translation/localization of labels/tooltips (English source strings only).

## Design

### Components

1. **`scripts/extract_settings_schema.py`** — dev/build step, run on the relay
   box. Input: path to `PrintConfig.cpp` (default: the relay checkout at
   `/home/max/orca-relay/src/libslic3r/PrintConfig.cpp`; overridable by arg/env).
   Parses `def = this->add("<key>", co<Type>);` blocks and the following
   `def-><field> = ...;` lines until the next `add(`, accumulating:
   `label, category, tooltip, unit (from sidetext), type, min, max, mode,
   enum_values, enum_labels, default`. Emits `data/print_settings_schema.json`.

2. **`data/print_settings_schema.json`** — bundled artifact, keyed by setting
   id, with a header block. Ships with the package.

3. **Two MCP tools in `src/orcaslicer_mcp/server.py`:**
   - `describe_setting(key)` → full record for one key, or a clean not-found.
   - `search_settings(query)` → compact hits (key, label, category, one-line
     tooltip) matching across key/label/tooltip. Supersedes name-only
     `find_config_keys` for discovery (keep `find_config_keys` or redirect it —
     decide in the plan).

### Data shape

```json
{
  "_meta": {
    "source": "PrintConfig.cpp",
    "source_git_sha": "<sha of the relay checkout at extraction>",
    "extracted": "2026-07-17",
    "setting_count": 711,          // illustrative
    "unparsed_keys": ["<any key the parser could not fully resolve>"]
  },
  "layer_height": {
    "label": "Layer height",
    "category": "Quality",
    "tooltip": "Slicing height for each layer. Smaller layer height means more accurate and more printing time.",
    "type": "coFloat",
    "unit": "mm",
    "min": 0,
    "max": null,
    "mode": "comSimple",
    "enum_values": null,
    "enum_labels": null,
    "default": "INITIAL_LAYER_HEIGHT"
  }
}
```

### Data flow

Fully offline. Extraction is a manual dev step, re-run when the fork updates.
The MCP loads the JSON at startup; the two tools read from memory. No network,
no live Remote-API call — the tools work even when OrcaSlicer is not running.
(A later enhancement could cross-check `describe_setting` against live
`GET /config` values, but not in the MVP.)

### Currency

Static snapshot. The `_meta.source_git_sha` makes staleness visible and
regeneration is a single script re-run. This drift is the known, accepted
limitation of the MVP — the live endpoint (Future work) removes it.

### Parser risks (the only real effort)

The `add(...)` / `def-><field>` structure is highly regular, but:
- **Multi-line tooltips**: some `tooltip = L("...")` strings are concatenated
  across several lines; the parser must join until the closing `");`.
- **Enum styles**: older settings use repeated `def->enum_values.push_back(...)`
  (+ optional `enum_labels.push_back`); newer ones use `set_enum<E>({...})`
  with inline label pairs. Handle both; where labels are absent, emit values
  with `enum_labels: null`.
- **Whitespace variance**: aligned assignments (`def           = this->add(`).
- **Defaults are often macros/expressions**, not literals — `set_default_value(new ConfigOptionFloat(INITIAL_LAYER_HEIGHT))`. The parser captures the raw source expression as `default`; it does not resolve macros. Live `GET /config` is the reliable source of a setting's *current* value, so `default` is a low-priority, best-effort field for the MVP.
- **Unparsed keys are reported, never silently dropped** (`_meta.unparsed_keys`).

## Testing (all offline, no live API)

- **Extraction**: parses at least the expected count; spot-check known keys —
  `layer_height` → type `coFloat`, `min` 0, unit `mm`; a known enum key (e.g.
  `printer_technology`) → expected `enum_values`; coverage assertion (≥95% of
  discovered `add()` keys resolve a label + tooltip).
- **Tools**: `describe_setting` returns the expected fields for a known key and
  a clean not-found for a bogus key; `search_settings` returns the expected key
  for a representative keyword.

## Future work (out of this MVP)

- **Approach A — live `GET /config/schema` endpoint** on the fork that walks
  `print_config_def` at runtime and returns the same record shape. Read-only
  static const data → no GUI-thread marshaling, none of the modal/TOCTOU/recreate
  hazards of M2/M3; among the cheapest, lowest-risk endpoints on the roadmap.
  Removes snapshot drift and satisfies accuracy + always-current directly.
  Promote to this once the MVP proves the tools get used.
- **Layer 2 — strategy**: live web lookup at answer time, anchored to Layer 1's
  authoritative key + range before suggesting/applying.
- **Validation enforcement**: a pre-slice check that flags out-of-range/enum
  values, consuming Layer 1.
