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
async def test_select_preset_body():
    route = respx.put("http://x:13130/api/v1/preset").mock(
        return_value=httpx.Response(200, json={"selected": {"name": "0.2 Std"}}))
    async with OrcaClient(CFG) as c:
        await c.select_preset("print", "0.2 Std")
    assert json.loads(route.calls.last.request.read()) == {"type": "print", "name": "0.2 Std"}
