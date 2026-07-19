# Slice Analytics (MCP side) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `get_slice_breakdown` MCP tool that surfaces the fork's compact per-feature slice breakdown and runs a stateless predicted-vs-observed flow check — degrading gracefully on fork builds that don't emit the breakdown yet.

**Architecture:** The fork emits a `breakdown` object on `GET /api/v1/slice/status` (built in a separate fork-side plan). This plan is the Python consumer: extract the physics gate's per-feature flow predictions into a reusable function, add a pure `breakdown.py` module (shaping + prediction-check), and register one new tool. All logic is unit-testable against canned payloads with no live fork.

**Tech Stack:** Python, `mcp.server.fastmcp` (FastMCP), `httpx` + `respx` (HTTP mocking in tests), `pytest` (async), `uv`.

## Global Constraints

- Run everything via `uv run` (e.g. `uv run pytest`). The repo is uv-managed.
- Config values arrive from the fork as **strings** (e.g. `"45"`, `"20%"`); parse with the existing `physics_check._f` / `_width` helpers, never assume numeric types.
- Tests assert **parsed/semantic values**, never byte-exact JSON (per the `test-semantics-not-serialization` convention).
- Commit messages use the repo convention: `feat:` for features, `fix:` for fixes, `doc:` for docs. **No `Co-Authored-By: Claude` trailer** on any commit.
- New tool must **degrade, never raise**, when the fork lacks the `breakdown` field (mirror `get_slice_warnings`'s "absent key -> empty, not error" behavior).
- This plan is MCP-only. The fork C++ change that produces `breakdown` is a separate plan; until it lands, `get_slice_breakdown` returns `{"available": false, ...}`.

---

## File Structure

- **Modify** `src/orcaslicer_mcp/physics_check.py` — extract `predicted_flows(cfg) -> dict[str, float]` (per-feature flow), refactor the existing `flow_ceiling` block to consume it (behavior-preserving).
- **Create** `src/orcaslicer_mcp/breakdown.py` — pure functions: `prediction_check(cfg, breakdown)` and `build_breakdown(status, cfg)`. No I/O.
- **Modify** `src/orcaslicer_mcp/server.py` — register the `get_slice_breakdown()` tool.
- **Create** `tests/test_breakdown.py` — unit tests for `predicted_flows`, `prediction_check`, `build_breakdown`.
- **Create** `tests/test_server_breakdown.py` — tool-level test via `respx` (breakdown present / absent).

---

### Task 1: Extract per-feature flow predictions in `physics_check.py`

Behavior-preserving refactor: the flow math already exists inside `run_checks`'s `flow_ceiling` block but only the max is kept. Extract a function that returns the full per-feature map so the prediction-check can reuse it. `run_checks` output must not change (existing tests are the guard).

**Files:**
- Modify: `src/orcaslicer_mcp/physics_check.py`
- Test: `tests/test_physics_check.py`

**Interfaces:**
- Consumes: existing `_f`, `_width`, `cross_section`, `_FEATURES`.
- Produces: `predicted_flows(cfg: dict) -> dict[str, float]` — maps feature name (the speed key **without** the `_speed` suffix, e.g. `"outer_wall"`, `"inner_wall"`, `"sparse_infill"`, `"internal_solid_infill"`, `"top_surface"`, `"initial_layer"`, `"gap_infill"`, `"bridge"`) to demanded volumetric flow (mm³/s). Only features with parseable speed, width, and `layer_height` are included.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_physics_check.py`:

```python
from orcaslicer_mcp.physics_check import predicted_flows, run_checks

def test_predicted_flows_per_feature():
    cfg = {
        "layer_height": "0.5", "nozzle_diameter": "0.8", "line_width": "0.85",
        "outer_wall_speed": "45", "inner_wall_speed": "50",
        "sparse_infill_speed": "50", "sparse_infill_line_width": "0.9",
    }
    flows = predicted_flows(cfg)
    # cross_section(0.85, 0.5) = (0.85 - 0.5*(1-pi/4))*0.5 = 0.371 mm^2 -> 45*0.371 = 16.7
    assert flows["outer_wall"] == round(flows["outer_wall"], 6)
    assert abs(flows["outer_wall"] - 16.7) < 0.1
    assert abs(flows["inner_wall"] - 18.6) < 0.2      # 50 * 0.371
    assert abs(flows["sparse_infill"] - 19.8) < 0.2   # 50 * cross_section(0.9,0.5)=0.396
    assert "bridge" not in flows                        # no bridge_speed given

def test_predicted_flows_skips_unparseable():
    cfg = {"layer_height": "0.5", "nozzle_diameter": "0.8", "outer_wall_speed": ""}
    assert predicted_flows(cfg) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_physics_check.py::test_predicted_flows_per_feature -v`
Expected: FAIL with `ImportError: cannot import name 'predicted_flows'`.

- [ ] **Step 3: Add `predicted_flows` and refactor the flow_ceiling block**

In `src/orcaslicer_mcp/physics_check.py`, add this function above `run_checks`:

```python
def predicted_flows(cfg: dict) -> dict[str, float]:
    """Per-feature demanded volumetric flow (mm3/s): speed * cross_section(width, layer_height).
    Key is the speed key without its '_speed' suffix. Skips features missing speed/width/layer_height."""
    lh = _f(cfg, "layer_height")
    nd = _f(cfg, "nozzle_diameter")
    default_w = _width(cfg, "line_width", nd)
    if lh is None:
        return {}
    out: dict[str, float] = {}
    for speed_k, width_k in _FEATURES:
        sp = _f(cfg, speed_k)
        w = _width(cfg, width_k, nd)
        w = default_w if w is None else w
        if sp is None or w is None:
            continue
        out[speed_k.removesuffix("_speed")] = sp * cross_section(w, lh)
    return out
```

Then refactor the `flow_ceiling` block in `run_checks` to consume it. Replace the loop (current lines ~76-96) with:

```python
        flows = predicted_flows(cfg)
        if not flows:
            out.append(CheckResult("flow_ceiling", "warn", "insufficient data (no feature speeds)"))
            max_flow = None
        else:
            worst_name = max(flows, key=flows.get)
            max_flow = flows[worst_name]
            sp = _f(cfg, worst_name + "_speed")
            w = _width(cfg, dict(_FEATURES)[worst_name + "_speed"], nd)
            w = lw if w is None else w
            worst = f"{worst_name} {sp:g}mm/s x {w:g}mm = {max_flow:.1f}mm3/s" if max_flow > 0 else "zero flow demand (all found feature speeds are 0)"
            status = "fail" if max_flow > ceil else "pass"
            out.append(CheckResult("flow_ceiling", status, f"peak demand {worst}; ceiling {ceil:g}mm3/s"))
```

- [ ] **Step 4: Run tests to verify pass + no regression**

Run: `uv run pytest tests/test_physics_check.py -v`
Expected: PASS (new `predicted_flows` tests **and** all existing `run_checks` tests — the `flow_ceiling` detail string is unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/orcaslicer_mcp/physics_check.py tests/test_physics_check.py
git commit -m "feat: expose predicted_flows() per-feature flow map for the prediction-check"
```

---

### Task 2: `prediction_check` in `breakdown.py`

**Files:**
- Create: `src/orcaslicer_mcp/breakdown.py`
- Test: `tests/test_breakdown.py`

**Interfaces:**
- Consumes: `physics_check.predicted_flows`, `physics_check._f`.
- Produces: `prediction_check(cfg: dict, breakdown: dict) -> list[dict]` — one entry per role present in **both** the predictions and `breakdown["roles"]` (matched by role name). Each entry: `{"role": str, "verdict": "clamped"|"matches"|"anomaly", "predicted_mm3_s": float, "observed_max_mm3_s": float, "detail": str}`.
  - `clamped`: observed max is at/near the ceiling (>= 0.95 * ceiling) AND predicted exceeds observed by >10% → the profile's speed is optimistic; Orca throttled.
  - `anomaly`: observed max exceeds predicted by >10% (and not clamped).
  - `matches`: otherwise.

- [ ] **Step 1: Write the failing test**

Create `tests/test_breakdown.py`:

```python
from orcaslicer_mcp.breakdown import prediction_check

def _role(name, flow_max):
    return {"role": name, "time_s": 100.0, "time_pct": 10.0, "filament_g": 1.0,
            "speed_mm_s": {"min": 10, "max": 45, "mean": 40},
            "flow_mm3_s": {"min": 1.0, "max": flow_max, "mean": flow_max * 0.9}}

_CFG = {
    "layer_height": "0.5", "nozzle_diameter": "0.8", "line_width": "0.85",
    "filament_max_volumetric_speed": "20",
    "outer_wall_speed": "45",          # predicted ~16.7
    "inner_wall_speed": "55",          # predicted ~20.4 (over ceiling)
}

def test_prediction_check_flags_clamped():
    # inner_wall predicted ~20.4 but observed pinned at ceiling 20 -> clamped
    bd = {"roles": [_role("inner_wall", 19.9)]}
    out = prediction_check(_CFG, bd)
    entry = next(e for e in out if e["role"] == "inner_wall")
    assert entry["verdict"] == "clamped"

def test_prediction_check_matches():
    # outer_wall predicted ~16.7, observed ~16.5 -> matches
    bd = {"roles": [_role("outer_wall", 16.5)]}
    out = prediction_check(_CFG, bd)
    assert out[0]["verdict"] == "matches"

def test_prediction_check_anomaly():
    # outer_wall predicted ~16.7 but observed 25 (well above, not clamped) -> anomaly
    bd = {"roles": [_role("outer_wall", 25.0)]}
    out = prediction_check(_CFG, bd)
    assert out[0]["verdict"] == "anomaly"

def test_prediction_check_skips_roles_without_prediction():
    # travel/support have no predicted flow -> not in output
    bd = {"roles": [_role("travel", 0.0), _role("support", 5.0)]}
    assert prediction_check(_CFG, bd) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_breakdown.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orcaslicer_mcp.breakdown'`.

- [ ] **Step 3: Write `breakdown.py` with `prediction_check`**

Create `src/orcaslicer_mcp/breakdown.py`:

```python
"""Pure shaping + stateless prediction-check for the fork's slice breakdown (spec 2026-07-20)."""
from __future__ import annotations
from .physics_check import predicted_flows, _f

_FLOW_TOL = 0.10      # +/-10% flow tolerance
_NEAR_CEILING = 0.95  # observed within 5% of ceiling counts as clamped


def prediction_check(cfg: dict, breakdown: dict) -> list[dict]:
    preds = predicted_flows(cfg)
    ceiling = _f(cfg, "filament_max_volumetric_speed")
    out: list[dict] = []
    for role in breakdown.get("roles", []):
        name = role.get("role")
        flow = (role.get("flow_mm3_s") or {}).get("max")
        if name not in preds or flow is None:
            continue
        predicted = preds[name]
        clamped = (ceiling is not None and flow >= ceiling * _NEAR_CEILING
                   and predicted > flow * (1 + _FLOW_TOL))
        if clamped:
            verdict = "clamped"
            detail = (f"{name}: profile demands {predicted:.1f}mm3/s but observed max "
                      f"{flow:.1f} is pinned at ceiling {ceiling:g} - speed silently throttled")
        elif flow > predicted * (1 + _FLOW_TOL):
            verdict = "anomaly"
            detail = f"{name}: observed {flow:.1f}mm3/s exceeds predicted {predicted:.1f} (>10%)"
        else:
            verdict = "matches"
            detail = f"{name}: predicted {predicted:.1f}mm3/s vs observed {flow:.1f} (within tolerance)"
        out.append({"role": name, "verdict": verdict,
                    "predicted_mm3_s": round(predicted, 2),
                    "observed_max_mm3_s": round(flow, 2), "detail": detail})
    return out
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_breakdown.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/orcaslicer_mcp/breakdown.py tests/test_breakdown.py
git commit -m "feat: stateless prediction-check (predicted vs observed per-role flow)"
```

---

### Task 3: `build_breakdown` shaping + graceful degradation

**Files:**
- Modify: `src/orcaslicer_mcp/breakdown.py`
- Test: `tests/test_breakdown.py`

**Interfaces:**
- Consumes: `prediction_check` (Task 2).
- Produces: `build_breakdown(status: dict, cfg: dict) -> dict`. When `status["breakdown"]` is present → `{"available": True, "mode", "total_time_s", "roles", "metrics", "layers", "prediction_check"}`. When absent → `{"available": False, "reason": str}`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_breakdown.py`:

```python
from orcaslicer_mcp.breakdown import build_breakdown

def test_build_breakdown_available():
    status = {"state": "done", "breakdown": {
        "mode": "normal", "total_time_s": 26854.9,
        "roles": [_role("outer_wall", 16.5)],
        "metrics": {"speed": {"unit": "mm/s", "min": 0, "max": 300, "mean": 210, "buckets": []}},
        "layers": [{"z": 0.4, "time_s": 61.2, "filament_g": 1.1, "top_role": "internal_solid_infill"}],
    }}
    out = build_breakdown(status, _CFG)
    assert out["available"] is True
    assert out["total_time_s"] == 26854.9
    assert out["roles"][0]["role"] == "outer_wall"
    assert out["prediction_check"][0]["verdict"] == "matches"

def test_build_breakdown_unavailable_when_absent():
    out = build_breakdown({"state": "done"}, _CFG)
    assert out["available"] is False
    assert "reason" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_breakdown.py::test_build_breakdown_available -v`
Expected: FAIL with `ImportError: cannot import name 'build_breakdown'`.

- [ ] **Step 3: Add `build_breakdown` to `breakdown.py`**

Append to `src/orcaslicer_mcp/breakdown.py`:

```python
def build_breakdown(status: dict, cfg: dict) -> dict:
    bd = status.get("breakdown")
    if not bd:
        return {"available": False,
                "reason": "no breakdown in slice status (fork build predates this feature, "
                          "or no valid slice yet)"}
    return {"available": True,
            "mode": bd.get("mode"),
            "total_time_s": bd.get("total_time_s"),
            "roles": bd.get("roles", []),
            "metrics": bd.get("metrics", {}),
            "layers": bd.get("layers", []),
            "prediction_check": prediction_check(cfg, bd)}
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_breakdown.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/orcaslicer_mcp/breakdown.py tests/test_breakdown.py
git commit -m "feat: build_breakdown shaping with graceful degradation on old fork builds"
```

---

### Task 4: Register the `get_slice_breakdown` MCP tool

**Files:**
- Modify: `src/orcaslicer_mcp/server.py`
- Test: `tests/test_server_breakdown.py`

**Interfaces:**
- Consumes: `breakdown.build_breakdown`, existing `_client`, `_err`, `ApiError`.
- Produces: MCP tool `get_slice_breakdown() -> dict` (the `build_breakdown` result, or `_err` on API failure).

- [ ] **Step 1: Write the failing test**

Create `tests/test_server_breakdown.py`:

```python
import httpx, respx
import orcaslicer_mcp.server as srv


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


_BREAKDOWN = {
    "mode": "normal", "total_time_s": 26854.9,
    "roles": [{"role": "inner_wall", "time_s": 100.0, "time_pct": 10.0, "filament_g": 1.0,
               "speed_mm_s": {"min": 10, "max": 46, "mean": 44},
               "flow_mm3_s": {"min": 1.0, "max": 19.9, "mean": 18.0}}],
    "metrics": {}, "layers": [],
}
_CONFIG = {"config": {"layer_height": "0.5", "nozzle_diameter": "0.8", "line_width": "0.85",
                      "filament_max_volumetric_speed": "20", "inner_wall_speed": "55"}}


@respx.mock
async def test_get_slice_breakdown_available_with_prediction(monkeypatch):
    _env(monkeypatch)
    respx.get(url__regex=r"http://x:13130/api/v1/slice/status.*").mock(
        return_value=httpx.Response(200, json={"state": "done", "breakdown": _BREAKDOWN}))
    respx.get(url__regex=r"http://x:13130/api/v1/config.*").mock(
        return_value=httpx.Response(200, json=_CONFIG))
    out = await srv.get_slice_breakdown()
    assert out["available"] is True
    assert out["total_time_s"] == 26854.9
    # inner_wall predicted ~20.4 vs observed 19.9 pinned at ceiling 20 -> clamped
    assert out["prediction_check"][0]["verdict"] == "clamped"


@respx.mock
async def test_get_slice_breakdown_degrades_on_old_fork(monkeypatch):
    _env(monkeypatch)
    respx.get(url__regex=r"http://x:13130/api/v1/slice/status.*").mock(
        return_value=httpx.Response(200, json={"state": "done"}))  # no breakdown key
    respx.get(url__regex=r"http://x:13130/api/v1/config.*").mock(
        return_value=httpx.Response(200, json=_CONFIG))
    out = await srv.get_slice_breakdown()
    assert out["available"] is False
    assert "reason" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server_breakdown.py -v`
Expected: FAIL with `AttributeError: module 'orcaslicer_mcp.server' has no attribute 'get_slice_breakdown'`.

- [ ] **Step 3: Add the tool to `server.py`**

Add the import near the top (after `from .physics_check import run_checks`, line ~11):

```python
from .breakdown import build_breakdown
```

Add the tool (place it after `get_slice_warnings`, around line 129):

```python
@mcp.tool()
async def get_slice_breakdown() -> dict:
    """Per-feature breakdown of the last slice + a stateless predicted-vs-observed flow check.

    Returns per-role time/filament + speed/flow ranges, global time-weighted metric
    distributions, per-layer aggregates, and a prediction_check flagging where the profile's
    speed was silently throttled at the flow ceiling ('clamped'). Answers 'which feature is
    the time hog' directly instead of by trial slicing.

    Degrades to {"available": false, "reason": ...} on fork builds that don't emit the
    breakdown, or when there is no valid slice. [needs fork breakdown build]"""
    try:
        async with _client() as c:
            status = await c.slice_status()
            cfg = await c.get_config(None)
        return build_breakdown(status, cfg)
    except ApiError as e:
        return _err(e)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_server_breakdown.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `uv run pytest -q`
Expected: PASS (all existing tests + the new ones).

- [ ] **Step 6: Commit**

```bash
git add src/orcaslicer_mcp/server.py tests/test_server_breakdown.py
git commit -m "feat: get_slice_breakdown tool (per-feature analytics + prediction-check)"
```

---

## Self-Review

**Spec coverage:**
- Compact breakdown surfaced (per-role, metrics, layers) → Task 3 `build_breakdown` passes them through; Task 4 exposes them. ✅
- Stateless prediction-check (clamped/matches/anomaly) → Task 2. ✅
- Graceful `available:false` on old builds / no valid slice → Task 3 + Task 4 degradation test. ✅
- `get_slice_status`/`slice_and_wait` stay lean → untouched; breakdown is a separate tool. ✅
- **Fork-side breakdown computation + capability advertisement** → NOT in this plan by design (separate fork C++ plan). Noted in Global Constraints.

**Placeholder scan:** No TBD/TODO; every code and test step is complete. ✅

**Type consistency:** `predicted_flows` keys (speed key minus `_speed`) are matched against `breakdown["roles"][].role` in `prediction_check`; both use `outer_wall`/`inner_wall`/`sparse_infill`/`internal_solid_infill`/`top_surface`. `build_breakdown` returns the `prediction_check` list unchanged. Tool returns `build_breakdown` output verbatim. ✅

## Out of scope (this plan)
- Fork C++ change producing `breakdown` + `"breakdown"` capability — separate plan, needs an exploration spike on max-pc (locate the RemoteAPI slice-status handler, confirm `PrintEstimatedStatistics` per-role time fields).
- Per-role × per-metric histograms; central learning/aggregation (spec non-goals).
