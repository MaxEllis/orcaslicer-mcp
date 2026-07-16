import httpx, pytest, respx
from orcaslicer_mcp.config import Config
from orcaslicer_mcp.client import OrcaClient
from orcaslicer_mcp.errors import Unauthorized, Validation, NotReachable, UiTimeout

CFG = Config(base_url="http://x:13130", token="tok", timeout=5)

@respx.mock
async def test_get_status_sends_token():
    route = respx.get("http://x:13130/api/v1/status").mock(
        return_value=httpx.Response(200, json={"app": "OrcaSlicer"}))
    async with OrcaClient(CFG) as c:
        assert (await c.get_status())["app"] == "OrcaSlicer"
    assert route.calls.last.request.headers["X-Api-Token"] == "tok"

@respx.mock
async def test_401_raises_unauthorized():
    respx.get("http://x:13130/api/v1/status").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"}))
    async with OrcaClient(CFG) as c:
        with pytest.raises(Unauthorized):
            await c.get_status()

@respx.mock
async def test_put_config_422_raises_validation():
    respx.put("http://x:13130/api/v1/config").mock(
        return_value=httpx.Response(422, json={"applied": [], "errors": {"k": "unknown_key"}}))
    async with OrcaClient(CFG) as c:
        with pytest.raises(Validation) as ei:
            await c.put_config({"k": 1})
    assert ei.value.errors == {"k": "unknown_key"}

@respx.mock
async def test_get_config_keys_query():
    route = respx.get("http://x:13130/api/v1/config").mock(
        return_value=httpx.Response(200, json={"config": {"layer_height": "0.2"}}))
    async with OrcaClient(CFG) as c:
        assert (await c.get_config(["layer_height"])) == {"layer_height": "0.2"}
    assert "keys=layer_height" in str(route.calls.last.request.url)

@respx.mock
async def test_connect_error_maps():
    respx.get("http://x:13130/api/v1/status").mock(side_effect=httpx.ConnectError("refused"))
    async with OrcaClient(CFG) as c:
        with pytest.raises(NotReachable):
            await c.get_status()

@respx.mock
async def test_timeout_maps_to_uitimeout():
    respx.get("http://x:13130/api/v1/status").mock(side_effect=httpx.ReadTimeout("slow"))
    async with OrcaClient(CFG) as c:
        with pytest.raises(UiTimeout):
            await c.get_status()
