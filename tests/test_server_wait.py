import httpx, respx
import orcaslicer_mcp.server as srv


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


@respx.mock
async def test_apply_and_slice_composes(monkeypatch):
    _env(monkeypatch)
    respx.put("http://x:13130/api/v1/config").mock(
        return_value=httpx.Response(200, json={"applied": ["layer_height"], "errors": {}}))
    respx.post("http://x:13130/api/v1/slice").mock(
        return_value=httpx.Response(200, json={"already_valid": True, "started": False}))
    respx.get("http://x:13130/api/v1/slice/status").mock(
        return_value=httpx.Response(200, json={"state": "done", "percent": 100,
                                               "message": "", "stats": {"total_cost": 0.1}, "warnings": []}))
    # collect_events returns [] instantly (no WS server) -> falls back to slice_status
    monkeypatch.setattr(srv.OrcaClient, "collect_events",
                        lambda self, seconds, stop_on=None: _empty())
    out = await srv.apply_and_slice({"layer_height": 0.28})
    assert out["applied"] == ["layer_height"]
    assert out["result"]["state"] == "done"

async def _empty():
    return []
