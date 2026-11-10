"""Lessons from OrcaSlicer's own MCP work (plugin landscape review 2026-11-11): arguments agents commonly
send under another name are read in the tool's form, get_config explains the keys it was asked for, a key set
to null goes back to its saved preset value, and the server sends instructions."""
import json

import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp import settings_schema
from orcaslicer_mcp.normalize import normalize_arguments

BASE = "http://x:13130"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", BASE)


def _schema(tool):
    return srv.mcp._tool_manager.get_tool(tool).parameters


# ---- normalize_arguments ------------------------------------------------------------------

def test_a_singular_key_reaches_a_list_argument_as_a_one_item_list():
    args, notes = normalize_arguments(_schema("get_config"), {"key": "layer_height"})
    assert args == {"keys": ["layer_height"]} and notes


def test_a_comma_separated_string_becomes_a_list_of_keys():
    args, _ = normalize_arguments(_schema("get_config"), {"keys": "layer_height, wall_loops"})
    assert args == {"keys": ["layer_height", "wall_loops"]}


def test_a_json_list_in_a_string_is_left_for_the_sdk():
    args, _ = normalize_arguments(_schema("get_config"), {"keys": '["layer_height"]'})
    assert args == {"keys": '["layer_height"]'}


def test_aliases_and_camel_case():
    assert normalize_arguments(_schema("set_config"), {"settings": {"layer_height": 0.2}})[0] == {
        "changes": {"layer_height": 0.2}}
    assert normalize_arguments(_schema("describe_setting"), {"name": "brim_width"})[0] == {"key": "brim_width"}
    assert normalize_arguments(_schema("delete_object"), {"objectId": 3})[0] == {"object_id": 3}
    assert normalize_arguments(_schema("delete_object"), {"id": 3})[0] == {"object_id": 3}
    assert normalize_arguments(_schema("select_plate"), {"plate": 1})[0] == {"index": 1}
    assert normalize_arguments(_schema("load_model"), {"file_path": "/m/a.stl"})[0] == {"path": "/m/a.stl"}
    assert normalize_arguments(_schema("search_settings"), {"q": "brim"})[0] == {"query": "brim"}


def test_a_one_item_list_reaches_a_single_value_argument():
    assert normalize_arguments(_schema("describe_setting"), {"keys": ["brim_width"]})[0] == {"key": "brim_width"}


def test_the_tools_own_names_win_and_nothing_is_overwritten():
    # move_object_to_plate takes `plate` itself: it must not be read as anything else
    args, notes = normalize_arguments(_schema("move_object_to_plate"), {"object_id": 1, "plate": 2})
    assert args == {"object_id": 1, "plate": 2} and notes == []
    # both the alias and the real name sent: the real one is kept, the alias left for the validator
    args, _ = normalize_arguments(_schema("set_config"), {"changes": {"a": 1}, "settings": {"b": 2}})
    assert args["changes"] == {"a": 1} and args["settings"] == {"b": 2}


def test_unknown_names_are_left_alone():
    args, notes = normalize_arguments(_schema("search_settings"), {"query": "brim", "colour": "red"})
    assert args == {"query": "brim", "colour": "red"} and notes == []


def test_bad_input_never_raises():
    assert normalize_arguments(None, {"a": 1}) == ({"a": 1}, [])
    assert normalize_arguments({"properties": {}}, {"a": 1}) == ({"a": 1}, [])


async def test_a_tool_call_through_the_server_is_normalized():
    """describe_setting(name=...) used to fail with 'key: Field required'."""
    result = await srv.mcp.call_tool("describe_setting", {"name": "brim_width"})
    payload = json.loads(result.content[0].text)
    assert payload["key"] == "brim_width" and payload["label"]


# ---- get_config explains the keys it was asked for ---------------------------------------

@respx.mock
async def test_get_config_with_keys_says_what_each_means():
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {
        "sparse_infill_density": "15%", "layer_height": "0.2", "other": "x"}}))
    out = await srv.get_config(["sparse_infill_density", "layer_height", "not_a_key"])
    assert out["config"] == {"sparse_infill_density": "15%", "layer_height": "0.2"}
    about = out["about"]
    assert set(about) == {"sparse_infill_density", "layer_height"}
    assert about["sparse_infill_density"]["label"] == "Sparse infill density"
    assert about["sparse_infill_density"]["unit"] == "%" and about["sparse_infill_density"]["range"] == [0, 100]
    assert "infill" in about["sparse_infill_density"]["meaning"].lower()


@respx.mock
async def test_get_config_without_keys_adds_no_explanations():
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {"a": "1"}}))
    assert await srv.get_config() == {"config": {"a": "1"}}


def test_about_is_capped():
    keys = list(settings_schema._settings())[:40]
    assert len(srv._about(keys)) == srv._ABOUT_MAX_KEYS


# ---- null puts a key back to its saved preset value ----------------------------------------

def _status(modified, filaments=("PLA Basic",)):
    return {"presets": {"print": "0.20mm Standard", "printer": "My Printer", "filaments": list(filaments)},
            "modified": modified}


@respx.mock
async def test_null_reverts_a_print_key_to_the_saved_value():
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json=_status(
        {"print": ["wall_loops"], "filament": [], "printer": []})))
    preset = respx.post(f"{BASE}/api/v1/preset/config").mock(return_value=httpx.Response(200, json={
        "config": {"wall_loops": "2", "layer_height": "0.2"}}))
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {"wall_loops": "5"}}))
    put = respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={
        "applied": ["wall_loops", "sparse_infill_density"], "errors": {}}))
    out = await srv.set_config({"wall_loops": None, "sparse_infill_density": "20%"})
    assert json.loads(preset.calls[0].request.content) == {"type": "print", "name": "0.20mm Standard"}
    assert json.loads(put.calls[0].request.content) == {"wall_loops": "2", "sparse_infill_density": "20%"}
    assert out["reverted"] == ["wall_loops"]


@respx.mock
async def test_null_on_a_key_without_an_edit_changes_nothing():
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json=_status(
        {"print": [], "filament": [], "printer": []})))
    put = respx.put(f"{BASE}/api/v1/config")
    out = await srv.set_config({"wall_loops": None})
    assert not put.called
    assert out["already_saved"] == ["wall_loops"] and out["applied"] == []


@respx.mock
async def test_null_on_a_filament_key_with_several_filaments_is_refused_whole():
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json=_status(
        {"print": ["wall_loops"], "filament": ["nozzle_temperature"], "printer": []},
        filaments=("PLA Basic", "PETG Basic"))))
    respx.post(f"{BASE}/api/v1/preset/config").mock(return_value=httpx.Response(200, json={
        "config": {"wall_loops": "2"}}))
    put = respx.put(f"{BASE}/api/v1/config")
    out = await srv.set_config({"wall_loops": None, "nozzle_temperature": None})
    assert not put.called  # all or nothing
    assert "nozzle_temperature" in out["errors"] and out["applied"] == []


@respx.mock
async def test_null_on_a_filament_key_with_one_filament_uses_that_preset():
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json=_status(
        {"print": [], "filament": ["nozzle_temperature"], "printer": []}, filaments=("PLA Basic", "PLA Basic"))))
    preset = respx.post(f"{BASE}/api/v1/preset/config").mock(return_value=httpx.Response(200, json={
        "config": {"nozzle_temperature": "215"}}))
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {}}))
    put = respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={
        "applied": ["nozzle_temperature"], "errors": {}}))
    out = await srv.set_config({"nozzle_temperature": None})
    assert json.loads(preset.calls[0].request.content) == {"type": "filament", "name": "PLA Basic"}
    assert json.loads(put.calls[0].request.content) == {"nozzle_temperature": "215"}
    assert out["reverted"] == ["nozzle_temperature"]


@respx.mock
async def test_null_never_puts_back_a_script_or_connection_key():
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json=_status(
        {"print": [], "filament": [], "printer": ["machine_start_gcode"]})))
    put = respx.put(f"{BASE}/api/v1/config")
    out = await srv.set_config({"machine_start_gcode": None})
    assert not put.called and out["errors"]["machine_start_gcode"].startswith("not_revertible")


@respx.mock
async def test_a_set_without_nulls_reads_no_status():
    status = respx.get(f"{BASE}/api/v1/status")
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {}}))
    respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={
        "applied": ["layer_height"], "errors": {}}))
    out = await srv.set_config({"layer_height": 0.2})
    assert out["applied"] == ["layer_height"] and not status.called


# ---- instructions and search ---------------------------------------------------------------

def test_the_server_sends_instructions_that_fit_what_clients_keep():
    text = srv.mcp.instructions
    assert text and len(text) < 4096
    for name in ("get_status", "search_settings", "describe_setting", "check_profile_physics", "null"):
        assert name in text
    for tool in ("get_status", "search_settings", "describe_setting", "consult", "check_profile_physics",
                 "get_slice_status", "describe_plate", "compare_slices", "list_objects", "recall_prints"):
        assert srv.mcp._tool_manager.get_tool(tool) is not None, tool


def test_everyday_settings_rank_before_advanced_ones_on_a_tie():
    keys = [h["key"] for h in settings_schema.search("infill density", 3)]
    assert keys[0] == "sparse_infill_density"
