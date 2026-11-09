"""M4 (usability 2026-11-09): a comparison's trial writes, slices and final restore carry
X-Orca-Mcp-Trial: 1 so the MCP panel records none of them, and one note sums it up."""
import json

import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp import client as cl

BASE = "http://x:13130"
NOTE = f"{BASE}/api/v1/activity/note"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", BASE)
    monkeypatch.setattr(cl, "_NOTE_UNSUPPORTED", {})


def _mock_fork(note_status=200):
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(
        200, json={"config": {"layer_height": "0.2", "wall_loops": "2"}}))
    put = respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(
        200, json={"applied": ["layer_height"], "errors": {}}))
    respx.get(f"{BASE}/api/v1/slice/status").mock(return_value=httpx.Response(200, json={
        "state": "done", "percent": 100, "message": "", "warnings": [],
        "stats": {"estimated_time_seconds": 600, "filament_used_g": 3.0}}))
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json={"slice_result_valid": True}))
    respx.post(f"{BASE}/api/v1/slice").mock(return_value=httpx.Response(200, json={"already_valid": False}))
    note = respx.post(NOTE).mock(return_value=httpx.Response(note_status, json={}))
    return put, note


VARIANTS = [{"name": "now", "changes": {}},
            {"name": "0.28", "changes": {"layer_height": 0.28}},
            {"name": "3 walls", "changes": {"wall_loops": 3}}]


@respx.mock
async def test_every_trial_request_carries_the_header_and_one_note_follows():
    put, note = _mock_fork()
    out = await srv.compare_slices(VARIANTS)
    assert out["restored"] is True
    paths = [c.request.url.path for c in respx.calls]
    # Calls of the comparison itself: up to the note (the server's own after-call checks follow).
    trial_calls = list(respx.calls)[:paths.index("/api/v1/activity/note")]
    # The up-front plugin check runs before the trial starts; everything after it is marked.
    marked = [c for c in trial_calls if c.request.headers.get("X-Orca-Mcp-Trial") == "1"]
    unmarked = [c for c in trial_calls if "X-Orca-Mcp-Trial" not in c.request.headers]
    assert len(unmarked) == 1 and unmarked[0].request.url.path == "/api/v1/status"
    assert put.calls.last.request.headers["X-Orca-Mcp-Trial"] == "1"   # the final restore
    assert json.loads(put.calls.last.request.content) == {"layer_height": "0.2", "wall_loops": "2"}
    assert len(marked) > 5
    assert note.call_count == 1
    assert "X-Orca-Mcp-Trial" not in note.calls.last.request.headers
    assert json.loads(note.calls.last.request.content) == {
        "kind": "slice", "text": "Compared 3 options · nothing kept"}


@respx.mock
async def test_no_note_route_on_older_builds_is_silent():
    _, note = _mock_fork(note_status=404)
    out = await srv.compare_slices(VARIANTS)
    assert "headline" in out and out["restored"] is True
    assert note.call_count == 1


@respx.mock
async def test_header_is_gone_after_the_comparison():
    _mock_fork()
    await srv.compare_slices(VARIANTS)
    assert cl.TRIAL.get() is False
    await srv.get_status()
    assert "X-Orca-Mcp-Trial" not in respx.calls.last.request.headers


@respx.mock
async def test_compare_settings_marks_its_trials_too():
    put, note = _mock_fork()
    out = await srv.compare_settings("layer_height", [0.24, 0.28])
    assert len(out["rows"]) == 2
    assert all(c.request.headers.get("X-Orca-Mcp-Trial") == "1" for c in put.calls)
    assert json.loads(note.calls.last.request.content)["text"] == "Compared 2 options · nothing kept"


@respx.mock
async def test_restore_failure_changes_the_note():
    _mock_fork()
    respx.put(f"{BASE}/api/v1/config").mock(side_effect=[
        httpx.Response(200, json={"applied": [], "errors": {}})] * 3 + [
        httpx.Response(504, json={"error": "ui_timeout"})])
    note = respx.post(NOTE).mock(return_value=httpx.Response(200, json={}))
    out = await srv.compare_slices(VARIANTS[:2])
    assert out["restored"] is False
    bodies = [json.loads(c.request.content) for c in note.calls]
    assert bodies[0]["kind"] == "slice" and "may not be back" in bodies[0]["text"]
    # R45: the reply and a refused row name what wasn't put back.
    assert out["restore_failed"]["keys"] == ["layer_height"]
    assert out["headline"].startswith("The comparison couldn't put back Layer height (layer_height)")
    assert bodies[1]["kind"] == "refused"
    assert bodies[1]["text"] == "Comparison couldn't put back: Layer height"


@respx.mock
async def test_plugin_is_refused_before_anything_changes():
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json={"backend": "plugin"}))
    put = respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={}))
    out = await srv.compare_slices(VARIANTS)
    assert out["error"].startswith("not_available_in_plugin: Comparing slices")
    assert put.call_count == 0
