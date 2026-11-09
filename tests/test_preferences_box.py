"""R44 (Max, 2026-11-10): the build's Preferences box alone allows script, G-code and connection
edits; the plugin never does; an unreadable /status refuses; ORCA_MCP_ALLOW_KEYS still allows."""
import json

import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp import client as cl

BASE = "http://x:13130"
NOTE = f"{BASE}/api/v1/activity/note"
KEY = {"machine_start_gcode": "G28 X"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", BASE)
    monkeypatch.delenv("ORCA_MCP_ALLOW_KEYS", raising=False)
    monkeypatch.setattr(cl, "_NOTE_UNSUPPORTED", {})
    monkeypatch.setattr(cl, "_NOTE_STUCK", {})
    monkeypatch.setattr(cl, "_BACKENDS", {})


def _fork(status):
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(
        200, json={"config": {"machine_start_gcode": "G28"}}))
    if isinstance(status, Exception):
        st = respx.get(f"{BASE}/api/v1/status").mock(side_effect=status)
    else:
        st = respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json=status))
    put = respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(
        200, json={"applied": ["machine_start_gcode"], "errors": {}}))
    respx.post(NOTE).mock(return_value=httpx.Response(200, json={}))
    return st, put


@respx.mock
async def test_box_on_passes_through():
    _, put = _fork({"sensitive_keys_allowed": True})
    out = await srv.set_config(KEY)
    assert put.called and out["applied"] == ["machine_start_gcode"]


@respx.mock
async def test_box_off_gives_the_plain_refusal():
    _, put = _fork({"sensitive_keys_allowed": False})
    out = await srv.set_config(KEY)
    assert not put.called
    assert ("Ask the user to tick 'Allow script, G-code and connection edits' in OrcaSlicer's "
            "Preferences > Remote API, then ask again") in out["error"]
    assert "ORCA_MCP_ALLOW_KEYS" not in out["error"] and "switch" not in out["error"]


@respx.mock
async def test_plugin_is_refused_even_if_it_claimed_the_box():
    _, put = _fork({"backend": "plugin", "sensitive_keys_allowed": True})
    out = await srv.set_config(KEY)
    assert not put.called and "can't be changed through the OrcaSlicer MCP plugin" in out["error"]


@respx.mock
async def test_unreadable_status_refuses():
    _, put = _fork(httpx.ReadTimeout(""))
    out = await srv.set_config(KEY)
    assert not put.called
    assert "blocked_by_local_policy" in out["error"] and "couldn't be read just now" in out["error"]


@respx.mock
async def test_env_var_still_allows(monkeypatch):
    monkeypatch.setenv("ORCA_MCP_ALLOW_KEYS", "machine_start_gcode")
    st, put = _fork({"sensitive_keys_allowed": False})
    out = await srv.set_config(KEY)
    assert put.called and out["applied"] == ["machine_start_gcode"]


@respx.mock
async def test_box_on_for_object_config():
    respx.post(f"{BASE}/api/v1/preset/config").mock(return_value=httpx.Response(
        200, json={"config": {"machine_start_gcode": "G28"}}))
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json={"sensitive_keys_allowed": True}))
    obj = respx.put(f"{BASE}/api/v1/objects/1/config").mock(return_value=httpx.Response(200, json={"applied": []}))
    out = await srv.set_object_config(1, {"filament_start_gcode": "M117"})
    assert obj.called and "error" not in out


@respx.mock
async def test_status_is_kept_at_most_five_seconds(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(cl.time, "monotonic", lambda: clock[0])
    st, put = _fork({"sensitive_keys_allowed": True})
    await srv.set_config(KEY)
    reads = st.call_count
    clock[0] += 2.0
    await srv.set_config(KEY)
    assert st.call_count == reads          # within 5 s: the cached answer
    st.mock(return_value=httpx.Response(200, json={"sensitive_keys_allowed": False}))
    clock[0] += 4.0                          # 6 s after the read: asked again, the box is now off
    out = await srv.set_config(KEY)
    assert st.call_count > reads
    assert "blocked_by_local_policy" in out["error"]
