import json

import httpx, respx
import orcaslicer_mcp.server as srv
from orcaslicer_mcp.client import OrcaClient
from orcaslicer_mcp.config import Config


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


def _status(caps):
    return httpx.Response(200, json={"app": "OrcaSlicer", "capabilities": caps, "presets": {}})


@respx.mock
async def test_client_select_preset_sends_slot():
    route = respx.put("http://x:13130/api/v1/preset").mock(
        return_value=httpx.Response(200, json={"selected": "Generic PETG", "slot": 1}))
    async with OrcaClient(Config(token="t", base_url="http://x:13130", timeout=5)) as c:
        await c.select_preset("filament", "Generic PETG", 1)
    assert json.loads(route.calls.last.request.read()) == {
        "type": "filament", "name": "Generic PETG", "slot": 1}


@respx.mock
async def test_slot_is_one_based_and_needs_capability(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/status").mock(return_value=_status(["preset", "preset_slot"]))
    put = respx.put("http://x:13130/api/v1/preset").mock(
        return_value=httpx.Response(200, json={"selected": "Generic PETG", "slot": 3}))
    out = await srv.select_preset("filament", "Generic PETG", slot=4)
    assert out["slot"] == 3
    assert json.loads(put.calls.last.request.read())["slot"] == 3


@respx.mock
async def test_slot_refused_on_build_without_capability(monkeypatch):
    # An older build would ignore "slot" and switch the Filament tab instead.
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/status").mock(return_value=_status(["preset"]))
    put = respx.put("http://x:13130/api/v1/preset")
    out = await srv.select_preset("filament", "Generic PETG", slot=2)
    assert "not available" in out["error"]
    assert not put.called


async def test_slot_rejected_for_non_filament_and_zero(monkeypatch):
    _env(monkeypatch)
    assert "filament" in (await srv.select_preset("print", "0.20mm Standard", slot=1))["error"]
    assert "1-based" in (await srv.select_preset("filament", "Generic PLA", slot=0))["error"]


@respx.mock
async def test_without_slot_body_unchanged(monkeypatch):
    _env(monkeypatch)
    put = respx.put("http://x:13130/api/v1/preset").mock(
        return_value=httpx.Response(200, json={"selected": "Generic PLA", "discarded_changes": []}))
    await srv.select_preset("filament", "Generic PLA")
    assert json.loads(put.calls.last.request.read()) == {"type": "filament", "name": "Generic PLA"}
