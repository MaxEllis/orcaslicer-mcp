import httpx, respx
from mcp.server.fastmcp import Image
import orcaslicer_mcp.server as srv

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")

@respx.mock
async def test_render_plate_returns_image(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/plate/render").mock(
        return_value=httpx.Response(200, content=PNG,
                                    headers={"content-type": "image/png"}))
    out = await srv.render_plate()
    assert isinstance(out, Image)
    assert out.data == PNG

@respx.mock
async def test_render_plate_forwards_params(monkeypatch):
    _env(monkeypatch)
    route = respx.get("http://x:13130/api/v1/plate/render").mock(
        return_value=httpx.Response(200, content=PNG))
    await srv.render_plate(view="preview", angle="top", width=1024, height=768)
    params = route.calls.last.request.url.params
    assert params["view"] == "preview" and params["angle"] == "top"
    assert params["width"] == "1024" and params["height"] == "768"

@respx.mock
async def test_render_plate_409_means_slice_first(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/plate/render").mock(
        return_value=httpx.Response(409, json={"error": "no_slice_result"}))
    out = await srv.render_plate(view="preview")
    assert out["error"] == "no_slice_result"
    assert "slice" in out["hint"]

@respx.mock
async def test_render_plate_route_missing_means_old_fork(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/plate/render").mock(
        return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.render_plate()
    assert "not available on this OrcaSlicer build" in out["error"]
