"""M6 / R44 and m12 (usability 2026-11-09): the sensitive-key refusal explains both switches in
plain words, and connector-side refusals reach OrcaSlicer's MCP panel as a 'refused' note."""
import json

import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp import client as cl
from orcaslicer_mcp import guard

BASE = "http://x:13130"
NOTE = f"{BASE}/api/v1/activity/note"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", BASE)
    monkeypatch.delenv("ORCA_MCP_ALLOW_KEYS", raising=False)
    monkeypatch.setattr(cl, "_NOTE_UNSUPPORTED", {})


def _flat(s):
    return " ".join(s.split())


def test_refusal_names_both_switches_and_where_they_live():
    text = _flat(str(guard.BlockedKey(["machine_start_gcode"])))
    assert text.startswith("blocked_by_local_policy:")
    assert "TWO separate switches" in text
    assert "Preferences > Remote API > 'Allow script, G-code and connection edits'" in text
    assert "ORCA_MCP_ALLOW_KEYS" in text
    assert "Settings > Extensions > OrcaSlicer MCP > Configure" in text
    assert "'Allow sensitive config keys'" in text      # the .mcpb field title
    assert "env block" in text and "Restart the app" in text
    assert "Simplest: ask the user to make this change themselves" in text


def test_mcpb_field_title_matches_the_refusal():
    import pathlib
    manifest = json.loads((pathlib.Path(__file__).parent.parent / "mcpb" / "manifest.json").read_text())
    field = manifest["user_config"]["allow_keys"]
    assert field["title"] == "Allow sensitive config keys"
    assert manifest["server"]["mcp_config"]["env"]["ORCA_MCP_ALLOW_KEYS"] == "${user_config.allow_keys}"


@respx.mock
async def test_refusal_says_when_the_preferences_box_is_already_on():
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {"machine_start_gcode": "G28"}}))
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json={"sensitive_keys_allowed": True}))
    respx.post(NOTE).mock(return_value=httpx.Response(200, json={}))
    out = await srv.set_config({"machine_start_gcode": "G28 X"})
    assert "(already on)" in out["error"]


@respx.mock
async def test_refusal_is_posted_to_the_panel():
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {"machine_start_gcode": "G28"}}))
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json={}))
    note = respx.post(NOTE).mock(return_value=httpx.Response(200, json={}))
    put = respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={}))
    out = await srv.set_config({"machine_start_gcode": "G28 X"})
    assert "blocked_by_local_policy" in out["error"] and not put.called
    body = json.loads(note.calls.last.request.content)
    assert body["kind"] == "refused"
    assert body["text"] == "Start G-code" and body["value"] == "Not allowed"
    assert "locked" in body["detail"] and "Preferences > Remote API" in body["detail"]
    assert "X-Orca-Mcp-Trial" not in note.calls.last.request.headers


@respx.mock
async def test_out_of_range_refusal_is_posted_to_the_panel():
    note = respx.post(NOTE).mock(return_value=httpx.Response(200, json={}))
    out = await srv.set_config({"layer_height": -1})
    assert out["error"].startswith("out_of_range")
    body = json.loads(note.calls.last.request.content)
    assert body == {"kind": "refused", "text": "Layer height", "value": "Out of range",
                    "detail": "Layer height (layer_height) must be greater than 0 mm; -1 is outside that range"}


@respx.mock
async def test_older_build_without_the_note_route_is_silent_and_not_asked_again():
    note = respx.post(NOTE).mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    out1 = await srv.set_config({"layer_height": -1})
    out2 = await srv.set_config({"layer_height": -2})
    assert out1["error"].startswith("out_of_range") and out2["error"].startswith("out_of_range")
    assert note.call_count == 1


@respx.mock
async def test_a_failing_note_never_changes_the_reply():
    respx.post(NOTE).mock(side_effect=httpx.ConnectError("down"))
    out = await srv.set_config({"layer_height": -1})
    assert out["error"].startswith("out_of_range")


def test_fork_policy_hint_names_the_second_switch():
    from orcaslicer_mcp.errors import error_from_status
    e = error_from_status(422, {"applied": [], "errors": {"machine_start_gcode": "blocked_by_remote_api_policy"}})
    out = srv._err(e)
    assert "second of the two switches" in out["hint"]
