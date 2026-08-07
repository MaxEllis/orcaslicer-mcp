# compare_slices — design

Date: 2026-08-07
Status: approved (design), pre-implementation

## Purpose

Slice the current plate under several named setting variants and report the cost of
each, so a question like "what happens at layer height 0.3 / 0.4 / 0.5 / 0.6 mm?"
is one tool call and one comparison instead of several manual apply-slice-read round
trips. This is the "learn your slicer" pitch made concrete: the tool shows the
cause-and-effect of a setting choice, it does not just pick for you.

Parallel to the existing static `compare_settings` (which diffs config values without
slicing); `compare_slices` actually slices each variant and compares the outcomes.

## Tool surface

```
compare_slices(
    variants: list[dict],        # each {"name": str, "changes": {key: value}}
    baseline: str | None = None, # variant name to anchor deltas to
    detail: bool = False,        # False = headline + table; True = adds per-feature breakdown
    timeout: int = 300,          # per-variant slice timeout (seconds)
) -> dict
```

- Each variant is `{"name": ..., "changes": {...}}`. `changes = {}` is allowed and means
  "the current config as-is" — the natural baseline row.
- `baseline` default: the variant whose `changes == {}` if one exists, else the first
  variant in the list.
- Cap: at most 8 variants (each is a full slice = minutes; an accidental 20-variant call
  is a multi-hour wedge). Over the cap returns an error and slices nothing.
- Annotations: readOnlyHint (does not persist config — see restore), idempotentHint,
  openWorldHint=false.

## Behaviour

1. Snapshot the union of all keys any variant's `changes` touches, via `get_config(keys=union)`
   (minimal, not the whole config — enough to restore).
2. For each variant in list order:
   a. `set_config(variant.changes)` (a `{}` variant applies nothing).
   b. `slice_and_wait(timeout)`.
   c. Capture: total print time, filament mass, warnings, slice validity; plus the
      per-role breakdown via `get_slice_breakdown` when `detail=True`.
   d. A variant that fails to slice (invalid key, slice error, timeout) is recorded as an
      error row; the sweep CONTINUES rather than aborting.
3. `finally`: restore the snapshot with a single `set_config(snapshot)`. The live config
   therefore always ends exactly where it started. Slice validity is left false (same
   state as any un-resliced edit); the docstring says so.

This mutates only the live in-memory config and triggers slices — it does not write
on-disk presets, so it is within normal authority (not a Rule-3 destructive op). The
restore-in-`finally` is the load-bearing correctness property and gets an explicit test.

## Output — research-backed shape

Two channels (MCP `content` vs `structuredContent`): a pre-rendered human summary the
model relays, plus structured data for machine use. If the pinned `mcp` 1.28.1 FastMCP
does not expose `ToolResult` cleanly, fall back to a single dict carrying `headline` +
`table_markdown` + structured rows (behaviour identical to the caller).

The tool computes ALL derived numbers (deltas, percentages, signs, rounding) server-side.
Rationale: LLMs are unreliable at arithmetic and number formatting alone shifts their
accuracy, so the tool hands over `"-31%"`, never raw floats to subtract.

Structured fields:
- `headline`: one BLUF sentence naming the recommended variant and the one-clause reason.
- `baseline`: the variant name deltas are anchored to (always explicit).
- `recommended`: variant name, or `null` when no variant dominates (see rules).
- `recommendation_reason`: required whenever `recommended` is set; states the rule.
- `tradeoff`: when nothing dominates, the non-dominated set each tagged by what it wins
  (fastest / lightest / fewest-warnings).
- `variants`: list, in the input's natural order (NOT re-sorted by rank, so a sweep's
  trend stays readable). Each: `name`, `changes`, `time` {min, formatted}, `filament`
  {g, formatted}, `warnings` (always a list), `valid`, `delta_vs_baseline`
  {time_pct, time_formatted (signed), filament_pct, filament_formatted}, and `roles`
  only when `detail=True`. A failed variant carries `error` instead of metrics.

### Recommendation rule (dominance / honesty)

- Variant A *dominates* B if A is no worse on time, filament, and warnings-presence, and
  strictly better on at least one. Treat "has any warning/error" as a binary-worse axis.
- If exactly one variant dominates all others -> `recommended` = it; reason states why.
- Else `recommended` = a satisficing default under a STATED house rule ("fastest variant
  with no warnings"; if all have warnings, "fastest"), and `tradeoff` names the frontier.
  Never force a false single winner; always disclose the rule (guards against the
  decoy/asymmetric-dominance effect).

### Precision (false-precision guard)

- Time: whole minutes, formatted `"6h 40m"` (drop seconds — a slicer estimate is not
  second-accurate; reporting seconds is false confidence).
- Filament: 0.1 g.
- Percentages: whole numbers.
- Consistent precision per column across all rows.

### Large N

- 8 is a backend cap, not a display target (human relational working memory ~4 items).
- The docstring instructs the model: with more than ~5 variants, lead with the
  recommendation and the extremes and offer the full table, rather than reciting all 8.
  The tool still returns every row in structured form.

### content (pre-rendered) example

> **Recommended: 0.4mm — fastest with no warnings.** (0.6mm is 31% faster still but
> flags a thin-wall warning and uses 19% more filament.)

| Layer height | Time | Filament | vs 0.4mm (baseline) | |
|---|---|---|---|---|
| 0.3mm | 8h 10m | 41.0 g | +1h 30m (+22%), -7.0 g (-15%) | slowest, lightest |
| **0.4mm ★** | 6h 40m | 48.0 g | baseline | fastest, no warnings |
| 0.5mm | 5h 20m | 53.0 g | -1h 20m (-20%), +5.0 g (+11%) | |
| 0.6mm | 4h 35m | 57.0 g | -2h 05m (-31%), +9.0 g (+19%) | thin-wall warning |

## Docstring-as-contract

The tool description states: results are ready to relay; deltas and percentages are
already computed and rounded against `baseline` — relay them rather than recomputing;
`detail=True` only when a per-feature split is asked for (response grows ~N x); with more
than ~5 variants lead with the recommendation and the extremes.

## Module layout

- New `compare.py`: pure, live-API-free logic — snapshot/diff, dominance, rounding/format,
  headline + markdown-table rendering. Unit-testable directly. Mirrors the
  `breakdown.py` / `build_breakdown` split so `server.py` does not grow another fat function.
- Thin `@mcp.tool() compare_slices` orchestrator in `server.py`: calls get_config /
  set_config / slice_and_wait / get_slice_breakdown, delegates all shaping to `compare.py`,
  owns the try/finally restore.

## Testing (TDD)

Pure (`compare.py`, no API):
- dominance: single dominant -> recommended set; trade-off -> recommended null + frontier tags.
- deltas: signed, rounded; percentage and absolute both present; baseline row = "baseline".
- rounding/format: seconds dropped; 0.1 g; whole-number %; consistent per column.
- headline + table render: BLUF names recommended + reason; recommended row marked; warning marked.

Orchestrator (`server.py`, respx-mocked fork endpoints):
- variants sliced in input order; per-variant metrics captured.
- snapshot restored in `finally` even when a middle variant errors (the key safety test).
- headline vs detail: `roles` present only when `detail=True`.
- variant cap (>8) returns an error and slices nothing.
- a variant with an invalid key surfaces as that row's error; sweep continues; config restored.

## Research basis (validation)

Info design: Shneiderman overview-first / NN-g progressive disclosure (two levels max);
Vessey cognitive fit + Larkin & Simon (table for multi-metric compare); Kahneman & Tversky
reference-dependence + absolute-vs-relative-risk (show both, anchored); Cowan ~4 / Halford
relational limit (N cap is backend, not display); Treisman preattentive + Pareto dominance +
Huber decoy effect (honest, rule-stated recommendation; no color-only cues); Ehrenberg /
Jerez-Fernandez (round to effective digits; false precision reads as false confidence).
MCP output: MCP spec content vs structuredContent; Anthropic "writing effective tools"
concise/detailed + high-signal-only; LLM-arithmetic-unreliability literature (precompute
server-side); docstring-as-contract (arXiv 2508.13774).
