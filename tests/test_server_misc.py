import httpx, respx
import orcaslicer_mcp.server as srv
from orcaslicer_mcp.errors import NotFound

def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")

@respx.mock
async def test_find_config_keys(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/config").mock(
        return_value=httpx.Response(200, json={"config": {"layer_height": "0.2",
                                                          "first_layer_height": "0.25", "wall_loops": "2"}}))
    out = await srv.find_config_keys("layer_height")
    assert set(out["keys"]) == {"layer_height", "first_layer_height"}

@respx.mock
async def test_get_gcode_not_sliced(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/gcode").mock(
        return_value=httpx.Response(409, json={"error": "not_sliced"}))
    out = await srv.get_gcode()
    assert out["error"] == "not_sliced"

@respx.mock
async def test_load_model_not_available(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/model").mock(
        return_value=httpx.Response(404, json={"error": "not_found"}))
    respx.get("http://x:13130/api/v1/status").mock(
        return_value=httpx.Response(200, json={"capabilities": ["status", "config"]}))
    out = await srv.load_model("/tmp/x.stl")
    assert "M4a" in out["error"]

@respx.mock
async def test_load_model_missing_file_old_fork(monkeypatch):
    # F18: pre-mcp.14 forks answer a missing file with the route-missing token;
    # a build that advertises the model capability means the FILE is missing.
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/model").mock(
        return_value=httpx.Response(404, json={"error": "not_found"}))
    respx.get("http://x:13130/api/v1/status").mock(
        return_value=httpx.Response(200, json={"capabilities": ["status", "model"]}))
    out = await srv.load_model("/tmp/gone.stl")
    assert out["error"] == "file_not_found"
    assert "/tmp/gone.stl" in out["detail"]

@respx.mock
async def test_load_model_missing_file_new_fork(monkeypatch):
    _env(monkeypatch)
    status = respx.get("http://x:13130/api/v1/status")
    respx.post("http://x:13130/api/v1/model").mock(
        return_value=httpx.Response(404, json={"error": "file_not_found"}))
    out = await srv.load_model("/tmp/gone.stl")
    assert out["error"] == "file_not_found"
    assert not status.called
