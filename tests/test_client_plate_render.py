import httpx, pytest, respx
from orcaslicer_mcp.config import Config
from orcaslicer_mcp.client import OrcaClient
from orcaslicer_mcp.errors import Conflict

CFG = Config(base_url="http://x:13130", token="tok", timeout=5)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # magic + stub body

@respx.mock
async def test_plate_render_returns_png_bytes():
    route = respx.get("http://x:13130/api/v1/plate/render").mock(
        return_value=httpx.Response(200, content=PNG,
                                    headers={"content-type": "image/png"}))
    async with OrcaClient(CFG) as c:
        data = await c.get_plate_render()
    assert data.startswith(b"\x89PNG")
    params = route.calls.last.request.url.params
    assert params["view"] == "editor" and params["angle"] == "iso"
    assert params["width"] == "800" and params["height"] == "600"

@respx.mock
async def test_plate_render_passes_params():
    route = respx.get("http://x:13130/api/v1/plate/render").mock(
        return_value=httpx.Response(200, content=PNG))
    async with OrcaClient(CFG) as c:
        await c.get_plate_render(view="preview", angle="top", width=1024, height=768)
    params = route.calls.last.request.url.params
    assert params["view"] == "preview" and params["angle"] == "top"
    assert params["width"] == "1024" and params["height"] == "768"

@respx.mock
async def test_plate_render_409_no_slice():
    respx.get("http://x:13130/api/v1/plate/render").mock(
        return_value=httpx.Response(409, json={"error": "no_slice_result"}))
    async with OrcaClient(CFG) as c:
        with pytest.raises(Conflict):
            await c.get_plate_render(view="preview")
