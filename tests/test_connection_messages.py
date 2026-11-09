"""M5 (usability 2026-11-09): a connection failure says what to check, and a timeout says why."""
import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp import client as cl
from orcaslicer_mcp.config import Config
from orcaslicer_mcp.errors import NotReachable, UiTimeout, error_from_status

BASE = "http://x:13130"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", BASE)
    monkeypatch.setattr(cl, "_BACKENDS", {})


@respx.mock
async def test_unreachable_before_any_status_mentions_both_switches():
    respx.get(f"{BASE}/api/v1/config").mock(side_effect=httpx.ConnectError(""))
    out = await srv.get_config(None)
    assert out["error"].startswith("not_reachable: ")
    assert "Preferences > Remote API turned on" in out["error"]
    assert "OrcaSlicer MCP plugin" in out["error"]
    assert "(ConnectError)" in out["error"]  # an empty reason still names what happened


@respx.mock
async def test_unreachable_after_the_build_answered_names_only_the_remote_api():
    status = respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json={"presets": {}}))
    await srv.get_status()
    status.mock(side_effect=httpx.ConnectError("refused"))
    out = await srv.get_status()
    assert "Preferences > Remote API turned on" in out["error"]
    assert "plugin" not in out["error"]


@respx.mock
async def test_unreachable_after_the_plugin_answered_names_the_plugin():
    status = respx.get(f"{BASE}/api/v1/status").mock(
        return_value=httpx.Response(200, json={"backend": "plugin", "presets": {}}))
    await srv.get_status()
    status.mock(side_effect=httpx.ConnectError("refused"))
    out = await srv.get_status()
    assert "OrcaSlicer MCP plugin turned on" in out["error"]
    assert "Remote API" not in out["error"]


@respx.mock
async def test_timeout_with_an_empty_reason_gets_a_real_one():
    respx.get(f"{BASE}/api/v1/config").mock(side_effect=httpx.ReadTimeout(""))
    out = await srv.get_config(None)
    assert out["error"] == ("ui_timeout: OrcaSlicer didn't answer within 30 s; a dialog may be open "
                            "in its window, ask the user to look.")


@respx.mock
async def test_timeout_names_the_per_call_allowance():
    respx.post(f"{BASE}/api/v1/model").mock(side_effect=httpx.ReadTimeout(""))
    async with cl.OrcaClient(Config(BASE, "tok", 30.0)) as c:
        with pytest.raises(UiTimeout) as ei:
            await c.load_model("/a.stl")
    assert "within 130 s" in str(ei.value)


@respx.mock
async def test_gcode_and_render_share_the_messages():
    respx.get(f"{BASE}/api/v1/gcode").mock(side_effect=httpx.ConnectError(""))
    async with cl.OrcaClient(Config(BASE, "tok", 30.0)) as c:
        with pytest.raises(NotReachable) as ei:
            await c.get_gcode()
    assert "Is OrcaSlicer open" in str(ei.value)


def test_504_from_orcaslicer_says_to_look_for_a_dialog():
    e = error_from_status(504, {"error": "ui_timeout"})
    assert isinstance(e, UiTimeout) and "dialog may be open" in str(e)


def test_401_says_where_the_token_comes_from():
    e = error_from_status(401, {"error": "unauthorized"})
    assert "Preferences > Remote API" in str(e) and "ORCA_API_TOKEN" in str(e)
