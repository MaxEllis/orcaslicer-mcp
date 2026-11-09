"""Polish pass Q1, Q2, Q5 (2026-11-10)."""
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
    monkeypatch.setattr(cl, "_NOTE_STUCK", {})
    monkeypatch.setattr(cl, "_BACKENDS", {})


@respx.mock
async def test_current_value_read_on_a_range_refusal_is_short():
    cfg = respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {}}))
    respx.post(NOTE).mock(return_value=httpx.Response(200, json={}))
    await srv.set_config({"layer_height": -1})
    assert cfg.calls.last.request.extensions["timeout"]["read"] == pytest.approx(1.5)


@respx.mock
async def test_edit_preset_stored_read_on_a_range_refusal_is_short():
    stored = respx.post(f"{BASE}/api/v1/preset/config").mock(return_value=httpx.Response(200, json={"config": {}}))
    respx.post(NOTE).mock(return_value=httpx.Response(200, json={}))
    out = await srv.edit_preset("print", "P", {"layer_height": -1})
    assert out["error"].startswith("out_of_range")
    assert stored.calls.last.request.extensions["timeout"]["read"] == pytest.approx(1.5)


@respx.mock
async def test_any_answer_ends_the_note_skip():
    note = respx.post(NOTE).mock(side_effect=[httpx.ReadTimeout(""), httpx.Response(200, json={})])
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {}}))
    await srv.set_config({"layer_height": -1})      # the note times out: skip begins
    assert BASE in cl._NOTE_STUCK
    await srv.get_config(None)                       # OrcaSlicer answers again
    assert BASE not in cl._NOTE_STUCK
    await srv.set_config({"layer_height": -2})
    assert note.call_count == 2


@respx.mock
async def test_plugin_refusals_show_in_its_panel():
    respx.post(f"{BASE}/api/v1/slice").mock(
        return_value=httpx.Response(404, json={"error": "not_found", "backend": "plugin"}))
    note = respx.post(NOTE).mock(return_value=httpx.Response(200, json={}))
    out = await srv.slice_and_wait(timeout=5)
    assert out["error"].startswith("not_available_in_plugin")
    body = json.loads(note.calls.last.request.content)
    assert body["kind"] == "refused" and body["text"] == "Slicing"
    assert body["value"] == "Not in the plugin" and "Slice plate" in body["detail"]


@respx.mock
async def test_up_front_plugin_refusal_is_noted_too():
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json={"backend": "plugin"}))
    note = respx.post(NOTE).mock(return_value=httpx.Response(200, json={}))
    await srv.compare_slices([{"name": "a", "changes": {}}, {"name": "b", "changes": {"layer_height": 0.3}}])
    body = json.loads(note.calls.last.request.content)
    assert body["text"] == "Comparing slices"


@respx.mock
async def test_compare_settings_says_what_it_couldnt_put_back():
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {"wall_loops": "2"}}))
    respx.put(f"{BASE}/api/v1/config").mock(side_effect=[
        httpx.Response(200, json={"applied": [], "errors": {}}),
        httpx.Response(504, json={"error": "ui_timeout"})])
    respx.post(f"{BASE}/api/v1/slice").mock(return_value=httpx.Response(200, json={"already_valid": True}))
    respx.get(f"{BASE}/api/v1/slice/status").mock(return_value=httpx.Response(200, json={"state": "done"}))
    note = respx.post(NOTE).mock(return_value=httpx.Response(200, json={}))
    out = await srv.compare_settings("wall_loops", [3])
    assert out["restore_failed"]["keys"] == ["wall_loops"]
    kinds = [json.loads(c.request.content)["kind"] for c in note.calls]
    assert kinds == ["slice", "refused"]


def test_second_paragraph_never_restarts_with_the_first_lines_words():
    import anyio
    for t in anyio.run(srv.mcp.list_tools):
        paras = t.description.split("\n\n")
        if len(paras) > 1:
            first = " ".join(paras[0].split()).lower()
            second = " ".join(paras[1].split()).lower()
            assert first[:30] not in second, t.name
