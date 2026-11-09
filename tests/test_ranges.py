"""B1 (usability 2026-11-09): values outside a setting's min/max are refused before they reach
OrcaSlicer, and OrcaSlicer's own 422 out_of_range reads the same way."""
import json

import httpx
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp import ranges
from orcaslicer_mcp.errors import OutOfRange, error_from_status

BASE = "http://x:13130"


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok")
    m.setenv("ORCA_API_URL", BASE)


def test_negative_layer_height_is_out_of_range():
    found = ranges.out_of_range({"layer_height": -1})
    assert found["layer_height"]["min"] == 0
    assert "greater than 0 mm" in found["layer_height"]["detail"]
    assert "Layer height" in found["layer_height"]["detail"]


def test_zero_layer_height_is_refused_too():
    assert "layer_height" in ranges.out_of_range({"layer_height": 0})
    assert ranges.out_of_range({"layer_height": 0.2}) == {}


def test_percent_string_checked_by_its_number():
    found = ranges.out_of_range({"sparse_infill_density": "250%"})
    assert found["sparse_infill_density"]["max"] == 100
    assert "from 0 to 100 %" in found["sparse_infill_density"]["detail"]
    assert ranges.out_of_range({"sparse_infill_density": "25%"}) == {}
    assert ranges.out_of_range({"sparse_infill_density": 25}) == {}


def test_float_or_percent_both_forms():
    # top_surface_line_width: 0..1000, mm or %
    assert ranges.out_of_range({"top_surface_line_width": "150%"}) == {}
    assert ranges.out_of_range({"top_surface_line_width": "0.45"}) == {}
    assert "top_surface_line_width" in ranges.out_of_range({"top_surface_line_width": "1200%"})
    assert "top_surface_line_width" in ranges.out_of_range({"top_surface_line_width": -1})


def test_vector_values_checked_per_element():
    # fan_max_speed is a per-filament list, 0..100
    assert "fan_max_speed" in ranges.out_of_range({"fan_max_speed": "100,150"})
    assert "fan_max_speed" in ranges.out_of_range({"fan_max_speed": [50, 101]})
    assert ranges.out_of_range({"fan_max_speed": "40,100"}) == {}
    assert "outer_wall_speed" in ranges.out_of_range({"outer_wall_speed": 0})  # min 1


def test_non_numbers_and_unknown_keys_are_left_to_orcaslicer():
    assert ranges.out_of_range({"wall_loops": "lots", "nope": -5, "enable_support": True}) == {}
    # A named bound such as max_temp is not checked here.
    assert ranges.out_of_range({"nozzle_temperature": 9999}) == {}


def test_unit_escapes_are_decoded():
    lo, hi, unit = ranges.schema_bounds("nozzle_temperature")
    assert unit == "℃"


@respx.mock
async def test_set_config_refuses_before_sending(monkeypatch):
    _env(monkeypatch)
    put = respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={}))
    out = await srv.set_config({"layer_height": -1, "wall_loops": 3})
    assert put.call_count == 0
    assert out["error"].startswith("out_of_range: ")
    assert "Nothing was changed" in out["error"]
    assert set(out["errors"]) == {"layer_height"}
    assert out["allowed"]["layer_height"]["min"] == 0


@respx.mock
async def test_set_object_config_and_edit_preset_refuse_before_sending(monkeypatch):
    _env(monkeypatch)
    obj = respx.put(f"{BASE}/api/v1/objects/1/config").mock(return_value=httpx.Response(200, json={}))
    select = respx.put(f"{BASE}/api/v1/preset").mock(return_value=httpx.Response(200, json={}))
    out = await srv.set_object_config(1, {"sparse_infill_density": "250%"})
    assert obj.call_count == 0 and "sparse_infill_density" in out["errors"]
    out = await srv.edit_preset("print", "P", {"wall_loops": 5000})
    assert select.call_count == 0 and "wall_loops" in out["errors"]


def test_fork_422_out_of_range_reads_the_same():
    e = error_from_status(422, {"error": "out_of_range", "key": "layer_height",
                                "value": -1, "min": 0, "max": None})
    assert isinstance(e, OutOfRange)
    assert "Layer height (layer_height) must be greater than 0 mm" in str(e)
    assert e.errors["layer_height"]


def test_fork_422_per_key_out_of_range_uses_schema_bounds():
    e = error_from_status(422, {"applied": [], "errors": {"sparse_infill_density": "value out of range"}})
    assert isinstance(e, OutOfRange)
    assert e.ranges["sparse_infill_density"]["max"] == 100


def test_plain_422_gets_a_readable_message():
    e = error_from_status(422, {"applied": [], "errors": {"wall_loops": "Invalid value"}})
    assert "HTTP 422" not in str(e)
    assert "wall_loops: Invalid value" in str(e) and "Nothing was changed" in str(e)


@respx.mock
async def test_fork_out_of_range_through_set_config(monkeypatch):
    _env(monkeypatch)
    respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(
        422, json={"error": "out_of_range", "key": "bridge_flow", "value": 5, "min": 0, "max": 2}))
    out = await srv.set_config({"bridge_flow": 1.5})
    assert out["error"].startswith("out_of_range")
    assert out["allowed"]["bridge_flow"] == {"min": 0, "max": 2, "unit": ""}
