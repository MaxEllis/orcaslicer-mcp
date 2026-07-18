import httpx, json, pytest, respx
from orcaslicer_mcp.config import Config
from orcaslicer_mcp.client import OrcaClient
from orcaslicer_mcp.errors import Conflict

CFG = Config(base_url="http://x:13130", token="tok", timeout=5)

@respx.mock
async def test_slice_202():
    respx.post("http://x:13130/api/v1/slice").mock(
        return_value=httpx.Response(202, json={"started": True}))
    async with OrcaClient(CFG) as c:
        assert (await c.slice())["started"] is True

@respx.mock
async def test_slice_409_conflict():
    respx.post("http://x:13130/api/v1/slice").mock(
        return_value=httpx.Response(409, json={"error": "already_slicing"}))
    async with OrcaClient(CFG) as c:
        with pytest.raises(Conflict):
            await c.slice()

@respx.mock
async def test_get_gcode_bytes():
    respx.get("http://x:13130/api/v1/gcode").mock(
        return_value=httpx.Response(200, content=b"G28\nG1 X0\n"))
    async with OrcaClient(CFG) as c:
        assert (await c.get_gcode()).startswith(b"G28")

@respx.mock
async def test_layer_height_adaptive_body():
    route = respx.put("http://x:13130/api/v1/objects/7/layer_height").mock(
        return_value=httpx.Response(200, json={"id": 7, "mode": "adaptive", "points": 42}))
    async with OrcaClient(CFG) as c:
        r = await c.set_layer_height(7, "adaptive", 0.8)
    assert json.loads(route.calls.last.request.read()) == {"mode": "adaptive", "quality": 0.8}
    assert r["points"] == 42

@respx.mock
async def test_layer_height_reset_omits_quality():
    route = respx.put("http://x:13130/api/v1/objects/7/layer_height").mock(
        return_value=httpx.Response(200, json={"id": 7, "mode": "reset"}))
    async with OrcaClient(CFG) as c:
        await c.set_layer_height(7, "reset")
    assert json.loads(route.calls.last.request.read()) == {"mode": "reset"}

@respx.mock
async def test_height_range_body():
    route = respx.put("http://x:13130/api/v1/objects/7/height_range").mock(
        return_value=httpx.Response(200, json={"id": 7, "height_ranges": [
            {"min_z": 0.0, "max_z": 5.0, "layer_height": 0.1}]}))
    async with OrcaClient(CFG) as c:
        r = await c.set_height_range(7, 0.0, 5.0, 0.1)
    assert json.loads(route.calls.last.request.read()) == {"min_z": 0.0, "max_z": 5.0, "layer_height": 0.1}
    assert r["height_ranges"][0]["layer_height"] == 0.1

@respx.mock
async def test_height_range_clear():
    route = respx.put("http://x:13130/api/v1/objects/7/height_range").mock(
        return_value=httpx.Response(200, json={"id": 7, "height_ranges": []}))
    async with OrcaClient(CFG) as c:
        await c.set_height_range(7, clear=True)
    assert json.loads(route.calls.last.request.read()) == {"clear": True}

@respx.mock
async def test_get_presets():
    respx.get("http://x:13130/api/v1/presets").mock(
        return_value=httpx.Response(200, json={"print": [], "filament": [
            {"name": "My PLA", "system": False, "visible": True, "selected": True}], "printer": []}))
    async with OrcaClient(CFG) as c:
        r = await c.get_presets()
    assert r["filament"][0]["name"] == "My PLA"

@respx.mock
async def test_delete_preset_body():
    route = respx.delete("http://x:13130/api/v1/preset").mock(
        return_value=httpx.Response(200, json={"deleted": "Old PLA"}))
    async with OrcaClient(CFG) as c:
        r = await c.delete_preset("filament", "Old PLA")
    assert json.loads(route.calls.last.request.read()) == {"type": "filament", "name": "Old PLA"}
    assert r["deleted"] == "Old PLA"

@respx.mock
async def test_get_preset_config_body():
    route = respx.post("http://x:13130/api/v1/preset/config").mock(
        return_value=httpx.Response(200, json={"name": "X", "system": True,
                                               "config": {"layer_height": "0.2"}}))
    async with OrcaClient(CFG) as c:
        r = await c.get_preset_config("print", "X")
    assert json.loads(route.calls.last.request.read()) == {"type": "print", "name": "X"}
    assert r["config"]["layer_height"] == "0.2"

@respx.mock
async def test_save_preset_body():
    route = respx.post("http://x:13130/api/v1/preset/save").mock(
        return_value=httpx.Response(200, json={"saved": "My PLA", "created": True}))
    async with OrcaClient(CFG) as c:
        r = await c.save_preset("filament", "My PLA")
    assert json.loads(route.calls.last.request.read()) == {
        "type": "filament", "name": "My PLA", "detach": False}
    assert r["created"] is True

@respx.mock
async def test_select_preset_body():
    route = respx.put("http://x:13130/api/v1/preset").mock(
        return_value=httpx.Response(200, json={"selected": {"name": "0.2 Std"}}))
    async with OrcaClient(CFG) as c:
        await c.select_preset("print", "0.2 Std")
    assert json.loads(route.calls.last.request.read()) == {"type": "print", "name": "0.2 Std"}
