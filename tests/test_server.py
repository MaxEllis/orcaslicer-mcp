import httpx, respx
import orcaslicer_mcp.server as srv


def _env(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", "http://x:13130")


@respx.mock
async def test_get_status_tool(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/status").mock(
        return_value=httpx.Response(200, json={"app": "OrcaSlicer", "slicing": False}))
    out = await srv.get_status()
    assert out["app"] == "OrcaSlicer"


@respx.mock
async def test_set_config_validation_returns_errors(monkeypatch):
    _env(monkeypatch)
    respx.put("http://x:13130/api/v1/config").mock(
        return_value=httpx.Response(422, json={"applied": [], "errors": {"k": "unknown_key"}}))
    out = await srv.set_config({"k": 1})
    assert out["errors"] == {"k": "unknown_key"}


@respx.mock
async def test_get_config_tool(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/config").mock(
        return_value=httpx.Response(200, json={"config": {"layer_height": 0.2}}))
    out = await srv.get_config()
    assert out["config"] == {"layer_height": 0.2}


@respx.mock
async def test_slice_tool(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/slice").mock(
        return_value=httpx.Response(200, json={"started": True}))
    out = await srv.slice()
    assert out["started"] is True


@respx.mock
async def test_get_slice_status_tool(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/slice/status").mock(
        return_value=httpx.Response(200, json={"state": "done", "percent": 100}))
    out = await srv.get_slice_status()
    assert out["state"] == "done"
    assert out["percent"] == 100


@respx.mock
async def test_get_status_not_reachable_returns_error(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/status").mock(side_effect=httpx.ConnectError("boom"))
    out = await srv.get_status()
    assert "error" in out
