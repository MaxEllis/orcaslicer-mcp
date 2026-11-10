"""Verified slicing: the settings in the G-code's config block against the live ones (verify_slice.py)."""
import lzma
from pathlib import Path

import httpx
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp.verify_slice import check, same

B = "http://x:13130"
CUBE = lzma.open(Path(__file__).parent / "fixtures" / "cube_flat.gcode.xz", "rb").read()
OBJECTS = {"objects": [{"id": 61, "name": "cube.stl", "instances": 1, "size_mm": [20, 20, 20]}]}


def test_format_only_differences_count_as_the_same():
    assert same("0.2", "0.20") and same("15%", "15") and same("true", "1") and same("220,230", "220, 230")
    assert same("Textured PEI Plate", "textured pei plate")
    assert not same("0.2", "0.28") and not same("220,230", "220,240") and not same("by layer", "by object")


def test_differences_are_named_and_sensitive_or_missing_keys_skipped():
    sliced = {"layer_height": "0.2", "print_sequence": "by object", "wall_loops": "2",
              "machine_start_gcode": "G28", "printhost_apikey": "x"}
    live = {"layer_height": "0.20", "print_sequence": "by layer", "wall_loops": "3",
            "machine_start_gcode": "G28 X", "printhost_apikey": "y"}
    r = check(sliced, live, extra_keys=["machine_start_gcode", "printhost_apikey"])
    assert r["checked"] == 3
    assert {d["key"] for d in r["differ"]} == {"print_sequence", "wall_loops"}
    assert "2 of 3" in r["summary"] and "print sequence" in r["summary"]


def test_keys_with_unsaved_edits_are_compared_too():
    r = check({"skirt_loops": "1"}, {"skirt_loops": "3"}, extra_keys=["skirt_loops#0"])
    assert r["differ"] == [{"key": "skirt_loops", "sliced": "1", "now": "3"}]


def test_a_match_says_so():
    r = check({"layer_height": "0.2", "wall_loops": "2"}, {"layer_height": "0.2", "wall_loops": "2"})
    assert r["differ"] == [] and r["summary"] == "Sliced with the settings that are set now (2 compared)."


def _gcode_with(block: dict) -> bytes:
    lines = "\n".join(f"; {k} = {v}" for k, v in block.items())
    return CUBE + f"\n; CONFIG_BLOCK_START\n{lines}\n; CONFIG_BLOCK_END\n".encode()


@respx.mock
async def test_describe_plate_reports_a_plate_setting_the_slice_used(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", B)
    srv._DESCRIBE_CACHE.clear()
    respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(200, content=_gcode_with(
        {"layer_height": "0.2", "spiral_mode": "1", "wall_loops": "2"})))
    respx.get(url__regex=rf"{B}/api/v1/objects.*").mock(return_value=httpx.Response(200, json=OBJECTS))
    respx.get(f"{B}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {
        "layer_height": "0.2", "spiral_mode": "0", "wall_loops": "2"}}))
    respx.get(f"{B}/api/v1/status").mock(return_value=httpx.Response(200, json={"modified": {"print": []}}))
    out = await srv.describe_plate()
    assert out["settings_check"]["differ"] == [{"key": "spiral_mode", "sliced": "1", "now": "0"}]
    again = await srv.describe_plate()  # cached parse, fresh check
    assert again["cached"] is True and again["settings_check"]["differ"][0]["key"] == "spiral_mode"


@respx.mock
async def test_describe_plate_leaves_the_check_out_when_settings_cannot_be_read(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", B)
    srv._DESCRIBE_CACHE.clear()
    respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(200, content=_gcode_with({"layer_height": "0.2"})))
    respx.get(url__regex=rf"{B}/api/v1/objects.*").mock(return_value=httpx.Response(200, json=OBJECTS))
    respx.get(f"{B}/api/v1/config").mock(return_value=httpx.Response(500, json={"error": "boom"}))
    out = await srv.describe_plate()
    assert "settings_check" not in out and out["objects"]
