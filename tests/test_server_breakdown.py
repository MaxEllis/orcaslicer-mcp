import httpx, respx
import orcaslicer_mcp.server as srv


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


_BREAKDOWN = {
    "mode": "normal", "total_time_s": 26854.9,
    "roles": [{"role": "inner_wall", "time_s": 100.0, "time_pct": 10.0, "filament_g": 1.0,
               "speed_mm_s": {"min": 10, "max": 46, "mean": 44},
               "flow_mm3_s": {"min": 1.0, "max": 19.9, "mean": 18.0}}],
    "metrics": {}, "layers": [],
}
_CONFIG = {"config": {"layer_height": "0.5", "nozzle_diameter": "0.8", "line_width": "0.85",
                      "filament_max_volumetric_speed": "20", "inner_wall_speed": "55"}}


@respx.mock
async def test_get_slice_breakdown_available_with_prediction(monkeypatch):
    _env(monkeypatch)
    respx.get(url__regex=r"http://x:13130/api/v1/slice/status.*").mock(
        return_value=httpx.Response(200, json={"state": "done", "breakdown": _BREAKDOWN}))
    respx.get(url__regex=r"http://x:13130/api/v1/config.*").mock(
        return_value=httpx.Response(200, json=_CONFIG))
    out = await srv.get_slice_breakdown()
    assert out["available"] is True
    assert out["total_time_s"] == 26854.9
    # inner_wall predicted ~20.4 vs observed 19.9 pinned at ceiling 20 -> clamped
    assert out["prediction_check"][0]["verdict"] == "clamped"


@respx.mock
async def test_get_slice_breakdown_degrades_on_old_fork(monkeypatch):
    _env(monkeypatch)
    respx.get(url__regex=r"http://x:13130/api/v1/slice/status.*").mock(
        return_value=httpx.Response(200, json={"state": "done"}))  # no breakdown key
    respx.get(url__regex=r"http://x:13130/api/v1/config.*").mock(
        return_value=httpx.Response(200, json=_CONFIG))
    out = await srv.get_slice_breakdown()
    assert out["available"] is False
    assert "reason" in out
