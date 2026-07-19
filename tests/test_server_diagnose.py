import httpx, respx
import orcaslicer_mcp.server as srv


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


# ---- B1 consumer: get_slice_warnings -------------------------------------

@respx.mock
async def test_get_slice_warnings_surfaces_warning(monkeypatch):
    _env(monkeypatch)
    respx.get(url__regex=r"http://x:13130/api/v1/slice/status.*").mock(
        return_value=httpx.Response(200, json={
            "state": "sliced",
            "warnings": ["A G-code path goes beyond plate boundaries"],
            "errors": [],
        }))
    respx.get(url__regex=r"http://x:13130/api/v1/status.*").mock(
        return_value=httpx.Response(200, json={"slice_result_valid": False}))
    out = await srv.get_slice_warnings()
    assert out["warnings"] == ["A G-code path goes beyond plate boundaries"]
    assert out["valid"] is False
    assert out["state"] == "sliced"


@respx.mock
async def test_get_slice_warnings_empty_when_absent(monkeypatch):
    _env(monkeypatch)
    # Today's fork: no warnings key -> degrades to [] (not an error).
    respx.get(url__regex=r"http://x:13130/api/v1/slice/status.*").mock(
        return_value=httpx.Response(200, json={"state": "sliced"}))
    respx.get(url__regex=r"http://x:13130/api/v1/status.*").mock(
        return_value=httpx.Response(200, json={"slice_result_valid": True}))
    out = await srv.get_slice_warnings()
    assert out["warnings"] == []
    assert out["valid"] is True


# ---- B3: diagnose_plate ---------------------------------------------------

def _status(valid=True):
    return {"app": "OrcaSlicer", "app_version": "2.3.2", "slice_result_valid": valid,
            "slicing": False, "capabilities": ["status", "objects", "config", "slice"]}

def _objects():
    return {"count": 1, "objects": [
        {"id": 91, "index": 0, "name": "Case Bottom.stl", "size_mm": [249.9, 269.9, 94.0],
         "instances": 1, "transform": {"offset": [150, 150, 47], "rotation": [0, 0, 0], "scale": [1, 1, 1]}}]}

def _cfg():
    # config values arrive as strings from the fork
    return {"config": {"printable_area": "0x0,300x0,300x300,0x300", "brim_type": "auto_brim",
                       "brim_width": "5", "skirt_loops": "2", "skirt_distance": "3"}}


@respx.mock
async def test_diagnose_plate_composes_all(monkeypatch):
    _env(monkeypatch)
    respx.get(url__regex=r"http://x:13130/api/v1/status.*").mock(
        return_value=httpx.Response(200, json=_status(valid=False)))
    respx.get(url__regex=r"http://x:13130/api/v1/objects.*").mock(
        return_value=httpx.Response(200, json=_objects()))
    respx.get(url__regex=r"http://x:13130/api/v1/config.*").mock(
        return_value=httpx.Response(200, json=_cfg()))
    respx.get(url__regex=r"http://x:13130/api/v1/slice/status.*").mock(
        return_value=httpx.Response(200, json={"state": "sliced",
                                               "warnings": ["A G-code path goes beyond plate boundaries"]}))
    out = await srv.diagnose_plate()
    assert set(out) == {"status", "objects", "adhesion_bed", "slice"}
    assert out["objects"]["count"] == 1
    assert out["adhesion_bed"]["printable_area"] == "0x0,300x0,300x300,0x300"
    assert out["slice"]["warnings"] == ["A G-code path goes beyond plate boundaries"]
    assert out["slice"]["valid"] is False


@respx.mock
async def test_diagnose_plate_degrades_when_objects_missing(monkeypatch):
    _env(monkeypatch)
    respx.get(url__regex=r"http://x:13130/api/v1/status.*").mock(
        return_value=httpx.Response(200, json=_status()))
    respx.get(url__regex=r"http://x:13130/api/v1/objects.*").mock(
        return_value=httpx.Response(404, json={"error": "not_found"}))
    respx.get(url__regex=r"http://x:13130/api/v1/config.*").mock(
        return_value=httpx.Response(200, json=_cfg()))
    respx.get(url__regex=r"http://x:13130/api/v1/slice/status.*").mock(
        return_value=httpx.Response(200, json={"state": "sliced"}))
    out = await srv.diagnose_plate()
    assert out["objects"]["count"] == 0
    assert out["objects"]["objects"] == []
    assert "note" in out["objects"]
    assert out["slice"]["warnings"] == []
