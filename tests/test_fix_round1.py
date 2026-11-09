"""Review fix round 1 (2026-11-09): plugin refusals, connect timeouts, range check on unchanged
values, OrcaSlicer's own refusal sentences, method-aware route names, quiet notes in a trial."""
import json

import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp import client as cl
from orcaslicer_mcp import ranges
from orcaslicer_mcp.config import Config
from orcaslicer_mcp.errors import NotReachable, OutOfRange, UiTimeout, error_from_status

BASE = "http://x:13130"
NOTE = f"{BASE}/api/v1/activity/note"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", BASE)
    monkeypatch.delenv("ORCA_MCP_ALLOW_KEYS", raising=False)
    monkeypatch.setattr(cl, "_NOTE_UNSUPPORTED", {})
    monkeypatch.setattr(cl, "_NOTE_STUCK", {})
    monkeypatch.setattr(cl, "_BACKENDS", {})

    async def _nosleep(_):
        pass
    monkeypatch.setattr(srv.asyncio, "sleep", _nosleep)


# --- I1: the plugin has no Preferences box and never allows these keys ----------------------

@respx.mock
async def test_plugin_refusal_names_no_switches():
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {"machine_start_gcode": "G28"}}))
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(
        200, json={"backend": "plugin", "sensitive_keys_allowed": False}))
    note = respx.post(NOTE).mock(return_value=httpx.Response(200, json={}))
    out = await srv.set_config({"machine_start_gcode": "G28 X"})
    err = out["error"]
    assert "can't be changed through the OrcaSlicer MCP plugin" in err
    assert "OrcaSlicer window" in err
    for gone in ("Preferences", "TWO", "ORCA_MCP_ALLOW_KEYS", "switch"):
        assert gone not in err, gone
    body = json.loads(note.calls.last.request.content)
    assert "Preferences" not in body["detail"] and "plugin" in body["detail"]


@respx.mock
async def test_plugin_policy_422_hint_names_no_box(monkeypatch):
    # ORCA_MCP_ALLOW_KEYS lets the key past the connector; the plugin refuses it itself.
    monkeypatch.setenv("ORCA_MCP_ALLOW_KEYS", "machine_start_gcode")
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {"machine_start_gcode": "G28"}}))
    respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(
        422, json={"applied": [], "errors": {"machine_start_gcode": "blocked_by_remote_api_policy"}}))
    status = respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json={"backend": "plugin"}))
    out = await srv.set_config({"machine_start_gcode": "G28 X"})
    assert status.called  # the backend was unknown, so the client asked once
    assert "plugin never lets the assistant" in out["hint"]
    assert "Preferences" not in out["hint"] and "switch" not in out["hint"]
    assert "describe_setting" not in out["error"]  # m4: a policy refusal, not a bad value


@respx.mock
async def test_build_policy_422_hint_is_one_plain_step(monkeypatch):
    # ORCA_MCP_ALLOW_KEYS lets the key past the connector; the build's own box is off.
    monkeypatch.setenv("ORCA_MCP_ALLOW_KEYS", "machine_start_gcode")
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {"machine_start_gcode": "G28"}}))
    respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(
        422, json={"applied": [], "errors": {"machine_start_gcode": "blocked_by_remote_api_policy"}}))
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json={}))
    out = await srv.set_config({"machine_start_gcode": "G28 X"})
    assert "Ask the user to tick it, then ask again" in out["hint"]


# --- I2: a connect or pool timeout means not reachable ----------------------------------------

@respx.mock
@pytest.mark.parametrize("exc", [httpx.ConnectTimeout(""), httpx.PoolTimeout("")])
async def test_connect_timeouts_say_not_reachable(exc):
    respx.get(f"{BASE}/api/v1/config").mock(side_effect=exc)
    out = await srv.get_config(None)
    assert out["error"].startswith("not_reachable: ")
    assert "dialog" not in out["error"]
    assert "on and awake" in out["error"]  # x:13130 is another computer


@respx.mock
async def test_local_address_does_not_mention_another_computer():
    respx.get("http://127.0.0.1:13130/api/v1/status").mock(side_effect=httpx.ConnectTimeout(""))
    async with cl.OrcaClient(Config("http://127.0.0.1:13130", "tok", 30.0)) as c:
        with pytest.raises(NotReachable) as ei:
            await c.get_status()
    assert "another computer" not in str(ei.value)


@respx.mock
async def test_read_timeout_keeps_the_dialog_wording():
    respx.get(f"{BASE}/api/v1/config").mock(side_effect=httpx.ReadTimeout(""))
    out = await srv.get_config(None)
    assert out["error"].startswith("ui_timeout: ") and "dialog may be open" in out["error"]


# --- m1: never refuse writing back what is already there --------------------------------------

def test_unchanged_out_of_range_values_pass():
    assert ranges.out_of_range({"support_object_xy_distance": "60%"},
                               {"support_object_xy_distance": "60%"}) == {}
    assert ranges.out_of_range({"fan_max_speed": "100,150"}, {"fan_max_speed": "100,150"}) == {}
    # A changed element is still judged.
    assert "fan_max_speed" in ranges.out_of_range({"fan_max_speed": "150,160"}, {"fan_max_speed": "100,150"})


def test_tolerance_and_negative_rule_follow_orcaslicer():
    assert ranges.out_of_range({"sparse_infill_density": "100.00005%"}) == {}
    assert "sparse_infill_density" in ranges.out_of_range({"sparse_infill_density": "-0.00001%"})


def test_mm_or_percent_value_in_mm_is_judged_by_min_max_only():
    # outer_wall_line_width: 0..1000; a 2 mm width is not refused here (the sidebar only asks).
    assert ranges.out_of_range({"outer_wall_line_width": 2}) == {}


@respx.mock
async def test_set_config_writes_back_a_shipped_out_of_range_value():
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(
        200, json={"config": {"support_object_xy_distance": "60%"}}))
    put = respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(
        200, json={"applied": ["support_object_xy_distance"], "errors": {}}))
    out = await srv.set_config({"support_object_xy_distance": "60%"})
    assert put.called and out["applied"] == ["support_object_xy_distance"]


@respx.mock
async def test_edit_preset_judges_against_the_named_presets_values():
    stored = respx.post(f"{BASE}/api/v1/preset/config").mock(return_value=httpx.Response(
        200, json={"config": {"support_object_xy_distance": "60%"}}))
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json={"backend": "plugin"}))
    out = await srv.edit_preset("print", "RatRig", {"support_object_xy_distance": "60%"})
    assert stored.called
    assert "out_of_range" not in out["error"]  # got past the range check (then the plugin refuses)


# --- m2: OrcaSlicer's own sentence, untouched -----------------------------------------------

def test_top_level_fork_sentence_is_relayed():
    e = error_from_status(422, {"applied": [], "error": "out_of_range", "key": "outer_wall_line_width",
                                "value": "40", "min": 0, "max": 10,
                                "detail": "Outer wall must be between 0 mm and 10 mm (got 40 mm)",
                                "errors": {"outer_wall_line_width": "out_of_range: Outer wall must be between 0 mm and 10 mm (got 40 mm)"}})
    assert isinstance(e, OutOfRange)
    assert e.ranges["outer_wall_line_width"]["detail"] == "Outer wall must be between 0 mm and 10 mm (got 40 mm)"
    assert "or %" not in str(e)


def test_rebuilt_sentence_for_mm_or_percent_states_the_number_only():
    e = error_from_status(422, {"applied": [], "errors": {"outer_wall_line_width": "value out of range"}})
    assert "mm or %" not in str(e)


# --- m3: routes by method, in errors too -----------------------------------------------------

@respx.mock
async def test_get_and_put_config_become_their_own_tools():
    text = ("GET /config reports this key merged across every loaded filament slot; "
            "PUT /config writes one preset")
    respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(
        422, json={"applied": [], "errors": {"hot_plate_temp": text}}))
    out = await srv.set_config({"hot_plate_temp": "70,100"})
    expect = ("get_config reports this key merged across every loaded filament slot; "
              "set_config writes one preset")
    assert out["errors"]["hot_plate_temp"] == expect
    assert expect in out["error"] and "/config" not in out["error"]


# --- m4: no double period ---------------------------------------------------------------------

def test_invalid_values_message_has_no_double_period():
    e = error_from_status(422, {"applied": [], "errors": {"wall_loops": "Invalid value."}})
    assert ".." not in str(e) and "describe_setting" in str(e)


# --- m5 / m6: quiet notes ------------------------------------------------------------------------

@respx.mock
async def test_refusals_inside_a_comparison_add_no_panel_rows():
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {"layer_height": "0.2"}}))
    respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"applied": [], "errors": {}}))
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json={"slice_result_valid": True}))
    respx.post(f"{BASE}/api/v1/slice").mock(return_value=httpx.Response(200, json={"already_valid": False}))
    respx.get(f"{BASE}/api/v1/slice/status").mock(return_value=httpx.Response(200, json={
        "state": "done", "warnings": [], "stats": {"estimated_time_seconds": 600, "filament_used_g": 3.0}}))
    note = respx.post(NOTE).mock(return_value=httpx.Response(200, json={}))
    out = await srv.compare_slices([{"name": "now", "changes": {}},
                                    {"name": "bad", "changes": {"layer_height": -1}}])
    bad = next(v for v in out["variants"] if v["name"] == "bad")
    assert bad["error"].startswith("out_of_range")
    kinds = [json.loads(c.request.content)["kind"] for c in note.calls]
    assert kinds == ["slice"]  # only the comparison's own line


async def test_compare_settings_without_values_is_refused_up_front():
    out = await srv.compare_settings("layer_height", [])
    assert out["error"] == "no_values" and out["detail"]


@respx.mock
async def test_several_out_of_range_keys_make_one_short_note():
    note = respx.post(NOTE).mock(return_value=httpx.Response(200, json={}))
    out = await srv.set_config({"layer_height": -1, "sparse_infill_density": "250%"})
    assert set(out["errors"]) == {"layer_height", "sparse_infill_density"}
    assert note.call_count == 1
    assert note.calls.last.request.extensions["timeout"]["read"] == pytest.approx(1.5)
    body = json.loads(note.calls.last.request.content)
    assert body["text"] == "Layer height, Sparse infill density"


@respx.mock
async def test_a_stuck_orcaslicer_is_not_asked_for_notes_again():
    note = respx.post(NOTE).mock(side_effect=httpx.ReadTimeout(""))
    await srv.set_config({"layer_height": -1})
    await srv.set_config({"layer_height": -2})
    assert note.call_count == 1


@respx.mock
async def test_a_second_comparison_does_not_ask_for_a_missing_note_route_again():
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {"layer_height": "0.2"}}))
    respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"applied": [], "errors": {}}))
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json={"slice_result_valid": True}))
    respx.post(f"{BASE}/api/v1/slice").mock(return_value=httpx.Response(200, json={"already_valid": False}))
    respx.get(f"{BASE}/api/v1/slice/status").mock(return_value=httpx.Response(200, json={
        "state": "done", "warnings": [], "stats": {"estimated_time_seconds": 600, "filament_used_g": 3.0}}))
    note = respx.post(NOTE).mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    v = [{"name": "a", "changes": {}}, {"name": "b", "changes": {"layer_height": 0.3}}]
    await srv.compare_slices(v)
    await srv.compare_slices(v)
    assert note.call_count == 1


@respx.mock
async def test_variant_error_reads_carry_the_trial_header():
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {
        "layer_height": "0.2", "nozzle_diameter": "0.4"}}))
    respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"applied": [], "errors": {}}))
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{BASE}/api/v1/slice").mock(return_value=httpx.Response(422, json={"error": "slice_not_started"}))
    st = respx.get(f"{BASE}/api/v1/slice/status").mock(return_value=httpx.Response(200, json={"state": "error"}))
    respx.post(NOTE).mock(return_value=httpx.Response(200, json={}))
    out = await srv.compare_slices([{"name": "a", "changes": {}}, {"name": "b", "changes": {"layer_height": 0.3}}])
    assert all(v["error"].startswith("slice_not_started") for v in out["variants"])
    assert st.called and all(c.request.headers.get("X-Orca-Mcp-Trial") == "1" for c in st.calls)


# --- m7: small relays ---------------------------------------------------------------------------

@respx.mock
async def test_slice_not_started_relays_the_key():
    respx.post(f"{BASE}/api/v1/slice").mock(return_value=httpx.Response(422, json={
        "error": "slice_not_started", "detail": "Layer height is too big.", "key": "layer_height"}))
    out = await srv.slice_and_wait(timeout=5)
    assert out["key"] == "layer_height" and out["setting"] == "Layer height"


@respx.mock
async def test_height_range_limits_are_relayed():
    respx.put(f"{BASE}/api/v1/objects/1/height_range").mock(return_value=httpx.Response(422, json={
        "error": "layer_height_out_of_range", "key": "layer_height", "value": 1.2, "min": 0.08,
        "max": 0.6, "detail": "layer height must be within 0.08 to 0.6 mm"}))
    out = await srv.set_height_range(1, 0, 5, 1.2)
    assert out["error"].startswith("layer_height_out_of_range: layer height must be within 0.08 to 0.6 mm")
    assert out["allowed"]["layer_height"]["min"] == 0.08 and out["allowed"]["layer_height"]["max"] == 0.6


@respx.mock
async def test_vector_refusal_names_the_entry_and_exclusive_minimum_is_flagged():
    respx.post(NOTE).mock(return_value=httpx.Response(200, json={}))
    out = await srv.set_config({"fan_max_speed": "20,150", "layer_height": 0})
    assert "150 (entry 2)" in out["errors"]["fan_max_speed"]
    assert out["allowed"]["fan_max_speed"]["index"] == 1
    assert out["allowed"]["layer_height"]["min_exclusive"] is True
