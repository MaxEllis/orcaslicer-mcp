"""Orchestration tests for compare_slices (respx-mocked fork). Pure logic is in test_compare.py."""
import json
import httpx, respx
import orcaslicer_mcp.server as srv

BASE = "http://x:13130"


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", BASE)


def _mock_slice_ok(*, breakdown=None, warnings=None):
    """Wire the endpoints a single variant slice touches, all succeeding."""
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(
        200, json={"config": {"layer_height": "0.2", "nozzle_diameter": "0.4"}}))
    status = {"state": "done", "percent": 100, "message": "",
              "stats": {"estimated_time_seconds": 410, "filament_used_g": 48.0},
              "warnings": warnings or []}
    if breakdown is not None:
        status["breakdown"] = breakdown
    respx.get(f"{BASE}/api/v1/slice/status").mock(return_value=httpx.Response(200, json=status))
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(
        200, json={"slice_result_valid": True}))
    respx.post(f"{BASE}/api/v1/slice").mock(return_value=httpx.Response(
        200, json={"already_valid": False}))


# --- validation (no HTTP) ---------------------------------------------------

async def test_needs_at_least_two(monkeypatch):
    _env(monkeypatch)
    out = await srv.compare_slices([{"name": "only", "changes": {}}])
    assert out["error"] == "need_at_least_two_variants"


async def test_variant_cap(monkeypatch):
    _env(monkeypatch)
    many = [{"name": f"v{i}", "changes": {"layer_height": f"0.{i}"}} for i in range(9)]
    out = await srv.compare_slices(many)
    assert out["error"] == "too_many_variants" and out["max"] == 8


async def test_duplicate_names(monkeypatch):
    _env(monkeypatch)
    out = await srv.compare_slices([{"name": "a", "changes": {}}, {"name": "a", "changes": {"x": "1"}}])
    assert out["error"] == "variant_names_must_be_unique"


async def test_unknown_baseline(monkeypatch):
    _env(monkeypatch)
    out = await srv.compare_slices(
        [{"name": "a", "changes": {}}, {"name": "b", "changes": {"layer_height": "0.3"}}],
        baseline="nope")
    assert out["error"] == "unknown_baseline"


# --- orchestration ----------------------------------------------------------

@respx.mock
async def test_happy_path_restores_snapshot(monkeypatch):
    _env(monkeypatch)
    put = respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(
        200, json={"applied": ["layer_height"], "errors": {}}))
    _mock_slice_ok()
    out = await srv.compare_slices([
        {"name": "current", "changes": {}},
        {"name": "0.4mm", "changes": {"layer_height": "0.4"}},
    ])
    assert len(out["variants"]) == 2
    assert out["recommended"] in ("current", "0.4mm")
    assert out["restored"] is True
    # the final PUT /config must restore the snapshot (original union values)
    last_body = json.loads(put.calls.last.request.content)
    assert last_body == {"layer_height": "0.2"}


@respx.mock
async def test_error_variant_continues_and_restores(monkeypatch):
    _env(monkeypatch)

    def put_handler(request):
        body = json.loads(request.content)
        if "bad_key" in body:
            return httpx.Response(200, json={"applied": [], "errors": {"bad_key": "unknown"}})
        return httpx.Response(200, json={"applied": list(body), "errors": {}})

    put = respx.put(f"{BASE}/api/v1/config").mock(side_effect=put_handler)
    _mock_slice_ok()
    out = await srv.compare_slices([
        {"name": "good", "changes": {"layer_height": "0.4"}},
        {"name": "broken", "changes": {"bad_key": "x"}},
    ])
    rows = {v["name"]: v for v in out["variants"]}
    assert "error" in rows["broken"]              # captured, not raised
    assert "error" not in rows["good"]            # sweep continued
    assert out["restored"] is True
    assert json.loads(put.calls.last.request.content) == {"layer_height": "0.2"}


@respx.mock
async def test_detail_includes_roles(monkeypatch):
    _env(monkeypatch)
    respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(
        200, json={"applied": ["layer_height"], "errors": {}}))
    _mock_slice_ok(breakdown={"roles": [{"role": "inner_wall", "time_s": 100}], "metrics": {}, "layers": []})
    out = await srv.compare_slices([
        {"name": "a", "changes": {"layer_height": "0.4"}},
        {"name": "b", "changes": {"layer_height": "0.6"}},
    ], detail=True)
    assert all("roles" in v for v in out["variants"])
    assert out["variants"][0]["roles"] == [{"role": "inner_wall", "time_s": 100}]
