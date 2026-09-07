import lzma
from pathlib import Path

import httpx, respx
import orcaslicer_mcp.server as srv

B = "http://x:13130"
CUBE = lzma.open(Path(__file__).parent / "fixtures" / "cube20_flat.gcode.xz", "rb").read()
OBJECTS = {"objects": [{"id": 61, "name": "cube20.stl", "instances": 1, "size_mm": [20, 20, 20]}]}


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", B)
    srv._DESCRIBE_CACHE.clear()


@respx.mock
async def test_describe_plate_not_sliced(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(409, json={"error": "no_slice"}))
    assert await srv.describe_plate() == {"error": "not_sliced"}


@respx.mock
async def test_describe_plate_on_cube_and_cache_hit(monkeypatch):
    _env(monkeypatch)
    gcode = respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(200, content=CUBE))
    respx.get(url__regex=rf"{B}/api/v1/objects.*").mock(return_value=httpx.Response(200, json=OBJECTS))
    out = await srv.describe_plate()
    assert out["per_object"] is True and out["not_in_gcode"] == []
    (o,) = out["objects"]
    assert o["name"] == "cube20.stl" and o["copies"] == 1 and o["orientation"]["class"] == "flat"
    assert o["seam"]["dominant"] == "+Y" and o["support"]["present"] is False
    assert out["summary"].startswith("cube20.stl lies flat")
    assert isinstance(out["parse_seconds"], float) and out["cached"] is False
    again = await srv.describe_plate()
    assert again["cached"] is True and again["objects"] == out["objects"]
    assert gcode.call_count == 2          # the G-code is still fetched (that is how we know the slice is unchanged)


@respx.mock
async def test_describe_plate_objects_endpoint_missing_still_describes(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(200, content=CUBE))
    respx.get(url__regex=rf"{B}/api/v1/objects.*").mock(return_value=httpx.Response(404, json={"error": "not found"}))
    out = await srv.describe_plate()
    assert out["objects"][0]["copies"] == 1 and out["not_in_gcode"] == []


def test_gcode_cache_key_is_stable_and_length_sensitive():
    data = b"same bytes " * 100
    k1 = srv._gcode_cache_key(data)
    k2 = srv._gcode_cache_key(data)
    assert k1 == k2
    assert k1.startswith(f"{len(data)}:")
    k3 = srv._gcode_cache_key(data + b"!")
    assert k3 != k1
    assert k3.startswith(f"{len(data) + 1}:")


@respx.mock
async def test_describe_plate_parse_failure_is_a_structured_error(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(200, content=CUBE))
    respx.get(url__regex=rf"{B}/api/v1/objects.*").mock(return_value=httpx.Response(200, json=OBJECTS))

    def _boom(text):
        raise ValueError("bad gcode")

    monkeypatch.setattr(srv._plate, "parse_gcode", _boom)
    out = await srv.describe_plate()
    assert out["error"] == "parse_failed" and "bad gcode" in out["detail"]


def test_describe_plate_is_annotated_read_only():
    tool = srv.mcp._tool_manager._tools["describe_plate"]
    assert tool.annotations.read_only_hint is True and tool.annotations.destructive_hint is None
