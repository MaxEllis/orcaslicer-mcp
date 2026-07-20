import math
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
async def test_get_status_normalizes_filaments_key_to_singular():
    # F4: the fork emits presets.filaments (plural) while everything else in the
    # API (type=filament, /presets, modified.filament) is singular. Normalize.
    respx.get("http://x:13130/api/v1/status").mock(
        return_value=httpx.Response(200, json={
            "app": "OrcaSlicer",
            "presets": {"filaments": ["PLA Fast"], "print": "0.5mm", "printer": "SWX2"},
        }))
    async with OrcaClient(CFG) as c:
        r = await c.get_status()
    assert r["presets"]["filament"] == ["PLA Fast"]
    assert "filaments" not in r["presets"]
    assert r["presets"]["print"] == "0.5mm" and r["presets"]["printer"] == "SWX2"

@respx.mock
async def test_get_status_without_presets_is_untouched():
    respx.get("http://x:13130/api/v1/status").mock(
        return_value=httpx.Response(200, json={"app": "OrcaSlicer"}))
    async with OrcaClient(CFG) as c:
        assert (await c.get_status()) == {"app": "OrcaSlicer"}

@respx.mock
async def test_get_objects_converts_rotation_radians_to_degrees():
    # F5: transform_object rotate takes DEGREES, but readback reports RADIANS.
    # Normalize the readback to degrees so the round-trip is consistent.
    respx.get("http://x:13130/api/v1/objects").mock(
        return_value=httpx.Response(200, json={"count": 1, "objects": [
            {"id": 1, "name": "x", "size_mm": [1, 1, 1],
             "transform": {"offset": [0, 0, 0], "rotation": [0.0, math.pi / 2, math.pi],
                           "scale": [1, 1, 1]}}]}))
    async with OrcaClient(CFG) as c:
        r = await c.get_objects()
    rot = r["objects"][0]["transform"]["rotation"]
    assert abs(rot[0] - 0.0) < 1e-9
    assert abs(rot[1] - 90.0) < 1e-6
    assert abs(rot[2] - 180.0) < 1e-6
    # other transform fields untouched
    assert r["objects"][0]["transform"]["scale"] == [1, 1, 1]

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
    route = respx.get(url__regex=r"http://x:13130/api/v1/config.*").mock(
        return_value=httpx.Response(200, json={"config": {"layer_height": "0.2", "brim_width": "5"}}))
    async with OrcaClient(CFG) as c:
        # filters locally to the requested key(s)
        assert (await c.get_config(["layer_height"])) == {"layer_height": "0.2"}
    # must NOT send a `keys=` query param: the fork can't URL-decode the %2C comma, so the
    # client fetches full config and filters in Python instead (see test_client_config).
    assert "keys=" not in str(route.calls.last.request.url)

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
