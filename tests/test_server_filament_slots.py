"""The filament slot tools: get_filament_slots, set_filament_slot_preset, the filament_slot argument of
set_config and save_preset, and get_config by_slot."""
import json

import httpx, respx
import orcaslicer_mcp.server as srv
from orcaslicer_mcp.client import OrcaClient
from orcaslicer_mcp.config import Config

URL = "http://x:13130"


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", URL)


def _status(caps, filaments=("Generic ASA", "Generic ASA", "Generic PLA")):
    return httpx.Response(200, json={"app": "OrcaSlicer", "capabilities": caps,
                                     "presets": {"filaments": list(filaments)}, "modified": {}})


FILAMENTS = {
    "count": 3, "multi_filament": True,
    "filaments": [
        {"index": 0, "preset": "Generic ASA", "filament_type": "ASA", "vendor": "Generic", "colour": "#FFFFFF",
         "system": True, "open_in_tab": True, "modified": True, "modified_keys": ["hot_plate_temp", "extruder_nozzle_stats"]},
        {"index": 1, "preset": "Generic ASA", "filament_type": "ASA", "vendor": "Generic", "colour": "#3C3C3C",
         "system": True, "open_in_tab": True, "modified": True, "modified_keys": ["hot_plate_temp", "extruder_nozzle_stats"]},
        {"index": 2, "preset": "Generic PLA", "filament_type": "PLA", "vendor": "Generic", "colour": "#FF0000",
         "system": True, "open_in_tab": False, "modified": False, "modified_keys": []}],
    "tab": {"preset": "Generic ASA", "slot": 0, "modified": True},
}


# --- get_filament_slots ---------------------------------------------------------------

@respx.mock
async def test_get_filament_slots_numbers_from_one_and_names_shared_slots(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/filaments").mock(return_value=httpx.Response(200, json=FILAMENTS))
    out = await srv.get_filament_slots()
    assert out["count"] == 3 and out["multi_filament"] is True
    slots = out["slots"]
    assert [s["slot"] for s in slots] == [1, 2, 3]
    assert slots[0]["shares_preset_with"] == [2] and slots[1]["shares_preset_with"] == [1]
    assert "shares_preset_with" not in slots[2]
    assert slots[0]["modified_keys"] == ["hot_plate_temp"]  # the internal key is hidden, as get_status hides it
    assert slots[2]["filament_type"] == "PLA" and slots[2]["colour"] == "#FF0000" and slots[2]["modified"] is False
    assert out["filament_tab"] == {"preset": "Generic ASA", "slot": 1, "modified": True}


@respx.mock
async def test_get_filament_slots_on_a_build_without_the_route(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/filaments").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.get_filament_slots()
    assert "not available" in out["error"] and "filament slot" in out["error"]


# --- set_config(filament_slot=) --------------------------------------------------------

@respx.mock
async def test_client_put_config_sends_the_slot_as_a_query():
    route = respx.put(f"{URL}/api/v1/config").mock(
        return_value=httpx.Response(200, json={"applied": ["hot_plate_temp"], "errors": {}, "slot": 1, "preset": "Generic ASA"}))
    async with OrcaClient(Config(token="t", base_url=URL, timeout=5)) as c:
        await c.put_config({"hot_plate_temp": 100}, slot=1)
    assert route.calls.last.request.url.query == b"slot=1"
    assert json.loads(route.calls.last.request.read()) == {"hot_plate_temp": 100}


@respx.mock
async def test_set_config_slot_is_one_based_and_reply_too(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/status").mock(return_value=_status(["config", "filament_slots"]))
    put = respx.put(f"{URL}/api/v1/config").mock(
        return_value=httpx.Response(200, json={"applied": ["hot_plate_temp"], "errors": {}, "slot": 2, "preset": "Generic PLA"}))
    out = await srv.set_config({"hot_plate_temp": 60}, filament_slot=3)
    assert put.calls.last.request.url.query == b"slot=2"
    assert out["slot"] == 3 and out["preset"] == "Generic PLA" and out["applied"] == ["hot_plate_temp"]


@respx.mock
async def test_set_config_slot_refused_without_the_capability(monkeypatch):
    # An older build ignores ?slot= and would write the Filament tab's preset instead.
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/status").mock(return_value=_status(["config", "preset_slot"]))
    put = respx.put(f"{URL}/api/v1/config")
    out = await srv.set_config({"hot_plate_temp": 60}, filament_slot=2)
    assert "not available" in out["error"] and "filament_slots" in out["error"]
    assert not put.called


async def test_set_config_slot_zero_rejected(monkeypatch):
    _env(monkeypatch)
    assert "1-based" in (await srv.set_config({"hot_plate_temp": 60}, filament_slot=0))["error"]


@respx.mock
async def test_set_config_without_slot_sends_no_query(monkeypatch):
    _env(monkeypatch)
    put = respx.put(f"{URL}/api/v1/config").mock(return_value=httpx.Response(200, json={"applied": ["wall_loops"], "errors": {}}))
    out = await srv.set_config({"wall_loops": 3})
    assert put.calls.last.request.url.query == b"" and "slot" not in out


@respx.mock
async def test_set_config_slot_unsaved_edits_elsewhere(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/status").mock(return_value=_status(["config", "filament_slots"]))
    respx.put(f"{URL}/api/v1/config").mock(return_value=httpx.Response(409, json={
        "applied": [], "error": "filament_edits_unsaved", "preset": "Generic ASA", "modified": ["hot_plate_temp"]}))
    out = await srv.set_config({"hot_plate_temp": 60}, filament_slot=3)
    assert out["error"].startswith("filament_edits_unsaved")
    assert out["preset"] == "Generic ASA" and out["modified"] == ["hot_plate_temp"]
    # both ways out name filament_slot: a null revert without it is refused as ambiguous on a multi-slot project
    assert "save_preset" in out["hint"] and out["hint"].count("filament_slot") == 2 and "new name" in out["hint"]


@respx.mock
async def test_set_config_slot_null_reverts_to_that_slots_preset(monkeypatch):
    _env(monkeypatch)
    # Slot 2's preset is the one open in the Filament tab and holds the unsaved edit, so the fork binds the tab to
    # slot 2 with no switch (a dirty preset elsewhere would be a 409, tested above).
    respx.get(f"{URL}/api/v1/status").mock(return_value=httpx.Response(200, json={
        "app": "OrcaSlicer", "capabilities": ["config", "filament_slots"],
        "presets": {"filaments": ["Generic ASA", "Generic PLA"]},
        "modified": {"filament": ["hot_plate_temp#1"]}}))
    respx.get(f"{URL}/api/v1/filaments").mock(return_value=httpx.Response(200, json={
        "count": 2, "filaments": [{"index": 0, "preset": "Generic ASA", "open_in_tab": False},
                                  {"index": 1, "preset": "Generic PLA", "open_in_tab": True, "modified": True}],
        "tab": {"preset": "Generic PLA", "slot": 1, "modified": True}}))
    pc = respx.post(f"{URL}/api/v1/preset/config").mock(
        return_value=httpx.Response(200, json={"config": {"hot_plate_temp": "60"}}))
    put = respx.put(f"{URL}/api/v1/config").mock(
        return_value=httpx.Response(200, json={"applied": ["hot_plate_temp"], "errors": {}, "slot": 1, "preset": "Generic PLA"}))
    out = await srv.set_config({"hot_plate_temp": None}, filament_slot=2)
    assert json.loads(pc.calls.last.request.read())["name"] == "Generic PLA"  # the slot's preset, not the open one
    assert json.loads(put.calls.last.request.read()) == {"hot_plate_temp": "60"}
    assert out["reverted"] == ["hot_plate_temp"] and out["slot"] == 2


# --- save_preset(filament_slot=) -------------------------------------------------------

@respx.mock
async def test_save_preset_slot_moves_one_slot(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/status").mock(return_value=_status(["preset", "filament_slots"]))
    post = respx.post(f"{URL}/api/v1/preset/save").mock(return_value=httpx.Response(200, json={
        "saved": "My LW-ASA", "created": True, "slot": 1, "slots_reassigned": [1], "slots_kept": [0],
        "filaments": ["Generic ASA", "My LW-ASA", "Generic PLA"]}))
    out = await srv.save_preset("filament", "My LW-ASA", filament_slot=2)
    assert json.loads(post.calls.last.request.read()) == {"type": "filament", "name": "My LW-ASA", "detach": False, "slot": 1}
    assert out["slot"] == 2 and out["slots_reassigned"] == [2] and out["slots_kept"] == [1]
    assert out["filaments"] == {"1": "Generic ASA", "2": "My LW-ASA", "3": "Generic PLA"}


async def test_save_preset_slot_needs_filament_type_and_one_based(monkeypatch):
    _env(monkeypatch)
    assert "filament" in (await srv.save_preset("print", "x", filament_slot=1))["error"]
    assert "1-based" in (await srv.save_preset("filament", "x", filament_slot=0))["error"]


@respx.mock
async def test_save_preset_slot_refused_without_the_capability(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/status").mock(return_value=_status(["preset"]))
    post = respx.post(f"{URL}/api/v1/preset/save")
    out = await srv.save_preset("filament", "x", filament_slot=1)
    assert "not available" in out["error"] and not post.called


@respx.mock
async def test_save_preset_without_slot_body_unchanged(monkeypatch):
    _env(monkeypatch)
    post = respx.post(f"{URL}/api/v1/preset/save").mock(return_value=httpx.Response(200, json={"saved": "x", "created": True}))
    await srv.save_preset("filament", "x")
    assert json.loads(post.calls.last.request.read()) == {"type": "filament", "name": "x", "detach": False}


# --- get_config(by_slot=) ---------------------------------------------------------------

def test_split_config_strings_handles_quotes_and_empties():
    assert srv._split_config_strings("#FFFFFF;#3C3C3C;#FF0000") == ["#FFFFFF", "#3C3C3C", "#FF0000"]
    assert srv._split_config_strings('"a;b";c') == ["a;b", "c"]
    assert srv._split_config_strings('"line\\nnext";x') == ["line\nnext", "x"]
    assert srv._split_config_strings("a;") == ["a", ""]
    assert srv._split_config_strings("") == [""]


@respx.mock
async def test_get_config_by_slot_splits_filament_keys_only(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {
        "hot_plate_temp": "70,100,60", "filament_colour": "#FFFFFF;#3C3C3C;#FF0000", "filament_type": "ASA;ASA;PLA",
        "layer_height": "0.2", "compatible_printers": "A;B", "nozzle_diameter": "0.4,0.4,0.4"}}))
    respx.get(f"{URL}/api/v1/status").mock(return_value=_status(["config"]))
    respx.post(f"{URL}/api/v1/preset/config").mock(return_value=httpx.Response(200, json={"config": {
        "hot_plate_temp": "70", "filament_type": "ASA", "compatible_printers": "A;B"}}))
    out = await srv.get_config(by_slot=True)
    cfg = out["config"]
    assert out["slots"] == 3
    assert cfg["hot_plate_temp"] == {"1": "70", "2": "100", "3": "60"}
    assert cfg["filament_colour"] == {"1": "#FFFFFF", "2": "#3C3C3C", "3": "#FF0000"}
    assert cfg["filament_type"] == {"1": "ASA", "2": "ASA", "3": "PLA"}
    assert cfg["layer_height"] == "0.2"
    assert cfg["nozzle_diameter"] == "0.4,0.4,0.4"  # a printer key, not a filament one
    # compatible_printers lists printers, not one entry per slot: left as text, whatever its item count
    assert cfg["compatible_printers"] == "A;B" and "not_per_slot" not in out


@respx.mock
async def test_get_config_by_slot_keeps_compatible_printers_whole_even_when_counts_match(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {
        "hot_plate_temp": "70,100,60", "compatible_printers": "A;B;C"}}))
    respx.get(f"{URL}/api/v1/status").mock(return_value=_status(["config"]))
    respx.post(f"{URL}/api/v1/preset/config").mock(return_value=httpx.Response(200, json={"config": {
        "hot_plate_temp": "70", "compatible_printers": "A;B;C"}}))
    cfg = (await srv.get_config(by_slot=True))["config"]
    assert cfg["compatible_printers"] == "A;B;C" and cfg["hot_plate_temp"]["3"] == "60"


@respx.mock
async def test_get_config_by_slot_survives_a_failed_preset_read(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {"hot_plate_temp": "70,100,60"}}))
    respx.get(f"{URL}/api/v1/status").mock(return_value=_status(["config"]))
    respx.post(f"{URL}/api/v1/preset/config").mock(return_value=httpx.Response(422, json={"error": "unknown_preset"}))
    out = await srv.get_config(by_slot=True)
    assert out["config"]["hot_plate_temp"] == "70,100,60" and out["slots"] == 3


@respx.mock
async def test_get_config_without_by_slot_unchanged(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {"hot_plate_temp": "70,100"}}))
    status = respx.get(f"{URL}/api/v1/status")
    out = await srv.get_config(["hot_plate_temp"])
    assert out["config"]["hot_plate_temp"] == "70,100" and "slots" not in out and not status.called


# --- set_filament_slot_preset -------------------------------------------------------------

@respx.mock
async def test_set_filament_slot_preset_keeps_colour_by_default(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/status").mock(return_value=_status(["preset", "preset_slot", "filament_slots"]))
    put = respx.put(f"{URL}/api/v1/preset").mock(
        return_value=httpx.Response(200, json={"selected": "Generic PETG", "slot": 1, "filaments": ["Generic ASA", "Generic PETG", "Generic PLA"]}))
    cfg = respx.put(f"{URL}/api/v1/config")
    respx.get(f"{URL}/api/v1/filaments").mock(return_value=httpx.Response(200, json=FILAMENTS))
    out = await srv.set_filament_slot_preset(2, "Generic PETG")
    assert json.loads(put.calls.last.request.read()) == {"type": "filament", "name": "Generic PETG", "slot": 1}
    assert out["slot"] == 2 and out["selected"] == "Generic PETG" and out["discarded_changes"] == []
    assert out["colour"] == "kept" and not cfg.called
    assert [s["slot"] for s in out["filaments"]] == [1, 2, 3]


@respx.mock
async def test_set_filament_slot_preset_sets_the_presets_default_colour(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/status").mock(return_value=_status(["preset", "preset_slot"]))
    respx.put(f"{URL}/api/v1/preset").mock(return_value=httpx.Response(200, json={"selected": "Generic PETG", "slot": 1}))
    respx.post(f"{URL}/api/v1/preset/config").mock(
        return_value=httpx.Response(200, json={"config": {"default_filament_colour": "#00FF00"}}))
    respx.get(f"{URL}/api/v1/config").mock(
        return_value=httpx.Response(200, json={"config": {"filament_colour": "#FFFFFF;#3C3C3C;#FF0000"}}))
    cfg = respx.put(f"{URL}/api/v1/config").mock(return_value=httpx.Response(200, json={"applied": ["filament_colour"], "errors": {}}))
    respx.get(f"{URL}/api/v1/filaments").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.set_filament_slot_preset(2, "Generic PETG", keep_colour=False)
    assert json.loads(cfg.calls.last.request.read()) == {"filament_colour": "#FFFFFF;#00FF00;#FF0000"}
    assert out["colour"] == {"set": "#00FF00", "applied": ["filament_colour"], "errors": {}}
    assert "filaments" not in out  # a build without GET /filaments: the switch still happened


@respx.mock
async def test_set_filament_slot_preset_needs_preset_slot(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/status").mock(return_value=_status(["preset"]))
    put = respx.put(f"{URL}/api/v1/preset")
    out = await srv.set_filament_slot_preset(2, "Generic PETG")
    assert "not available" in out["error"] and not put.called
    assert "1-based" in (await srv.set_filament_slot_preset(0, "x"))["error"]


# --- review fixes (2026-11) -----------------------------------------------------------

@respx.mock
async def test_slot_conflict_names_plain_setting_keys(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/status").mock(return_value=_status(["config", "filament_slots"]))
    respx.put(f"{URL}/api/v1/config").mock(return_value=httpx.Response(409, json={
        "applied": [], "error": "filament_edits_unsaved", "preset": "Generic ASA",
        "modified": ["hot_plate_temp#0", "hot_plate_temp#1", "extruder_nozzle_stats"]}))
    out = await srv.set_config({"hot_plate_temp": 60}, filament_slot=3)
    assert out["modified"] == ["hot_plate_temp"]


def _one_filament_status(caps, modified):
    return httpx.Response(200, json={"app": "OrcaSlicer", "capabilities": caps,
                                     "presets": {"filaments": ["Generic PLA"]}, "modified": modified})


@respx.mock
async def test_set_filament_slot_preset_one_filament_refuses_while_edits_are_unsaved(monkeypatch):
    # One filament: the fork switches the Filament tab, which drops every unsaved edit. Refused, nothing sent.
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/status").mock(return_value=_one_filament_status(
        ["config", "preset_slot"], {"print": ["wall_loops"], "filament": [], "printer": ["extruder_nozzle_stats"]}))
    sel = respx.put(f"{URL}/api/v1/preset").mock(return_value=httpx.Response(200, json={"selected": "Generic PETG"}))
    out = await srv.set_filament_slot_preset(1, "Generic PETG")
    assert out["error"].startswith("unsaved_edits") and "print (wall_loops)" in out["error"]
    assert out["modified"] == {"print": ["wall_loops"]}  # OrcaSlicer's own bookkeeping key is no user edit
    assert not sel.called


@respx.mock
async def test_set_filament_slot_preset_one_filament_without_edits_switches(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/status").mock(return_value=_one_filament_status(["config", "preset_slot"], {}))
    sel = respx.put(f"{URL}/api/v1/preset").mock(return_value=httpx.Response(200, json={
        "selected": "Generic PETG", "discarded_changes": []}))
    respx.get(f"{URL}/api/v1/filaments").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.set_filament_slot_preset(1, "Generic PETG")
    assert sel.called and out["selected"] == "Generic PETG" and out["discarded_changes"] == []


@respx.mock
async def test_set_filament_slot_preset_colour_failure_does_not_hide_the_switch(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/status").mock(return_value=_status(["config", "preset_slot"]))
    respx.put(f"{URL}/api/v1/preset").mock(return_value=httpx.Response(200, json={
        "selected": "Generic PETG", "slot": 1, "filaments": ["Generic ASA", "Generic PETG", "Generic PLA"]}))
    respx.post(f"{URL}/api/v1/preset/config").mock(return_value=httpx.Response(504, json={"error": "ui_timeout"}))
    respx.get(f"{URL}/api/v1/filaments").mock(return_value=httpx.Response(200, json=FILAMENTS))
    out = await srv.set_filament_slot_preset(2, "Generic PETG", keep_colour=False)
    assert out["selected"] == "Generic PETG" and out["slot"] == 2
    assert out["colour"]["error"].startswith("kept: the colour could not be changed")


@respx.mock
async def test_set_config_slot_sensitive_key_is_refused_before_any_write(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/status").mock(return_value=httpx.Response(200, json={
        "app": "OrcaSlicer", "capabilities": ["config", "filament_slots", "sensitive_key_policy"],
        "sensitive_keys_allowed": False, "presets": {"filaments": ["Generic ASA", "Generic PLA"]}, "modified": {}}))
    respx.get(f"{URL}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {
        "filament_start_gcode": "M104 S200;M104 S210"}}))
    put = respx.put(f"{URL}/api/v1/config").mock(return_value=httpx.Response(200, json={"applied": [], "errors": {}}))
    out = await srv.set_config({"filament_start_gcode": "M104 S0"}, filament_slot=2)
    assert not put.called
    assert "filament_start_gcode" in json.dumps(out)


@respx.mock
async def test_set_config_slot_on_the_plugin_says_how_to_do_it_by_hand(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/status").mock(return_value=httpx.Response(200, json={
        "app": "OrcaSlicer", "backend": "plugin", "capabilities": ["status", "config"],
        "presets": {"filaments": ["Generic ASA", "Generic PLA"]}, "modified": {}}))
    put = respx.put(f"{URL}/api/v1/config").mock(return_value=httpx.Response(200, json={"applied": [], "errors": {}}))
    out = await srv.set_config({"hot_plate_temp": 60}, filament_slot=2)
    assert not put.called
    assert "Writing one filament slot's preset" in out["error"] and "edit button" in out["by_hand"]
