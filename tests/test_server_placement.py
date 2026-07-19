import httpx, respx
import orcaslicer_mcp.server as srv


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


@respx.mock
async def test_check_placement_live(monkeypatch):
    _env(monkeypatch)
    objs = {"count": 1, "objects": [
        {"id": 91, "index": 0, "name": "Case Bottom.stl", "size_mm": [249.9, 269.9, 94.0],
         "instances": 1, "transform": {"offset": [150, 150, 47], "rotation": [0, 0, 0], "scale": [1, 1, 1]}}]}
    cfg = {"config": {"printable_area": "0x0,300x0,300x300,0x300", "brim_type": "outer_only",
                      "brim_width": "10", "brim_object_gap": "0.1", "skirt_loops": "0"}}
    respx.get(url__regex=r"http://x:13130/api/v1/objects.*").mock(
        return_value=httpx.Response(200, json=objs))
    respx.get(url__regex=r"http://x:13130/api/v1/config.*").mock(
        return_value=httpx.Response(200, json=cfg))
    out = await srv.check_placement()
    assert out["all_fit"] is True
    assert out["objects"][0]["fits"] is True
    assert out["bed"]["max"] == [300.0, 300.0]


@respx.mock
async def test_check_placement_needs_m4b(monkeypatch):
    _env(monkeypatch)
    respx.get(url__regex=r"http://x:13130/api/v1/objects.*").mock(
        return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.check_placement()
    assert "M4b" in out["error"]
