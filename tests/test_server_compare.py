import httpx, respx
import orcaslicer_mcp.server as srv


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


@respx.mock
async def test_compare_restores_original(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/config").mock(
        return_value=httpx.Response(200, json={"config": {"layer_height": "0.2"}}))
    put = respx.put("http://x:13130/api/v1/config").mock(
        return_value=httpx.Response(200, json={"applied": ["layer_height"], "errors": {}}))
    respx.post("http://x:13130/api/v1/slice").mock(
        return_value=httpx.Response(202, json={"started": True}))
    respx.get("http://x:13130/api/v1/slice/status").mock(
        return_value=httpx.Response(200, json={"state": "done", "percent": 100,
                                               "message": "", "stats": {"total_cost": 0.1}, "warnings": []}))
    monkeypatch.setattr(srv.OrcaClient, "collect_events",
                        lambda self, seconds, stop_on=None: _empty())
    out = await srv.compare_settings("layer_height", [0.24, 0.28])
    assert len(out["rows"]) == 2
    # last PUT must restore the original 0.2 (assert parsed JSON, not exact bytes)
    import json
    assert json.loads(put.calls.last.request.read()) == {"layer_height": "0.2"}

async def _empty():
    return []
