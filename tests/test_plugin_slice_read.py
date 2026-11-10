"""On the OrcaSlicer MCP plugin (0.2.0 and later) the user slices by hand and the tools read that slice: the
replies must send the assistant to the user's Slice plate button, never to a tool the plugin can't run."""
import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv

BASE = "http://x:13130"
PLUGIN_STATUS = {"backend": "plugin", "plugin_version": "0.2.0", "capabilities": ["status", "slice_read"],
                 "presets": {"printer": "P"}, "slice_result_valid": True}
PLUGIN_404 = httpx.Response(404, json={"error": "not_found", "backend": "plugin"})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", BASE)


@respx.mock
async def test_slice_refusal_points_at_get_slice_status():
    respx.post(f"{BASE}/api/v1/slice").mock(return_value=PLUGIN_404)
    out = await srv.slice_and_wait()
    assert "Slice plate" in out["by_hand"] and "get_slice_status" in out["by_hand"]


@respx.mock
async def test_not_sliced_on_the_plugin_asks_for_slice_plate():
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json=PLUGIN_STATUS))
    respx.get(f"{BASE}/api/v1/gcode").mock(return_value=httpx.Response(409, json={"error": "not_sliced"}))
    respx.get(f"{BASE}/api/v1/objects").mock(
        return_value=httpx.Response(200, json={"objects": [{"id": 1, "name": "cube"}]}))
    out = await srv.describe_plate()
    assert out["error"] == "not_sliced"
    assert "Slice plate" in out["detail"] and "slice_and_wait" not in out["detail"]


@respx.mock
async def test_not_sliced_on_the_build_still_names_slice_and_wait():
    respx.get(f"{BASE}/api/v1/status").mock(
        return_value=httpx.Response(200, json={"app": "OrcaSlicer", "capabilities": ["slice"]}))
    respx.get(f"{BASE}/api/v1/gcode").mock(return_value=httpx.Response(409, json={"error": "not_sliced"}))
    respx.get(f"{BASE}/api/v1/objects").mock(
        return_value=httpx.Response(200, json={"objects": [{"id": 1, "name": "cube"}]}))
    out = await srv.describe_plate()
    assert "slice_and_wait" in out["detail"]


@respx.mock
async def test_slice_warnings_on_the_plugin_say_they_are_not_visible():
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json=PLUGIN_STATUS))
    respx.get(f"{BASE}/api/v1/slice/status").mock(return_value=httpx.Response(200, json={
        "state": "done", "percent": 100, "message": "", "warnings": [], "valid": True, "stale": False,
        "stats": {"estimated_time_seconds": 402.0}}))
    out = await srv.get_slice_warnings()
    assert out["warnings_visible"] is False and "OrcaSlicer window" in out["note"]


@respx.mock
async def test_breakdown_on_the_plugin_names_the_build():
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json=PLUGIN_STATUS))
    respx.get(f"{BASE}/api/v1/slice/status").mock(return_value=httpx.Response(200, json={
        "state": "done", "percent": 100, "warnings": [], "valid": True, "stale": False,
        "stats": {"estimated_time_seconds": 402.0}}))
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"layer_height": "0.2"}))
    out = await srv.get_slice_breakdown()
    assert out["available"] is False
    assert "plugin" in out["reason"] and "OrcaSlicer MCP build" in out["reason"]
    assert "get_slice_status" in out["reason"]


def test_plugin_limits_say_what_the_plugin_reads():
    from orcaslicer_mcp.errors import PLUGIN_LIMITS
    assert "slice the user makes" in PLUGIN_LIMITS and "starting a slice" in PLUGIN_LIMITS
