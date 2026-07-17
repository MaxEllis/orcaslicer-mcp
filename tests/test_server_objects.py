import httpx, respx
import orcaslicer_mcp.server as srv


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


@respx.mock
async def test_list_objects_live(monkeypatch):
    _env(monkeypatch)
    payload = {"count": 1, "objects": [
        {"id": 42, "index": 0, "name": "cube20", "size_mm": [20, 20, 20],
         "instances": 1, "transform": {"offset": [0, 0, 10], "rotation": [0, 0, 0], "scale": [1, 1, 1]}}]}
    respx.get("http://x:13130/api/v1/objects").mock(return_value=httpx.Response(200, json=payload))
    out = await srv.list_objects()
    assert out["count"] == 1
    assert out["objects"][0]["id"] == 42
    assert out["objects"][0]["name"] == "cube20"


@respx.mock
async def test_list_objects_needs_m4b(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/objects").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.list_objects()
    assert "M4b" in out["error"]
