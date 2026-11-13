"""OrcaSlicer 2.5's temperature safety check on a filament preset save: the fork refuses with the pairs
(capability preset_temperature_check) unless allow_temperature_pairs is set; earlier 2.5 builds wait behind the
dialog, which reads as a timeout."""
import json

import httpx, respx
import orcaslicer_mcp.server as srv

URL = "http://x:13130"
PAIR = {"label": "Smooth High Temp Plate", "first_layer_key": "hot_plate_temp_initial_layer",
        "other_layers_key": "hot_plate_temp", "first_layer": 60, "other_layers": 77, "max_delta": 15, "delta": 17}


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", URL)


def _refused():
    return httpx.Response(422, json={"error": "temperature_pairs_unsafe", "pairs": [PAIR]})


@respx.mock
async def test_save_preset_refusal_names_the_pair_and_the_way_out(monkeypatch):
    _env(monkeypatch)
    save = respx.post(f"{URL}/api/v1/preset/save").mock(return_value=_refused())
    out = await srv.save_preset("filament", "My PLA")
    assert out["error"].startswith("temperature_pairs_unsafe") and "nothing was saved" in out["error"]
    assert out["pairs"][0]["first_layer_key"] == "hot_plate_temp_initial_layer"
    assert out["pairs"][0]["text"] == ("Smooth High Temp Plate: first layer 60 °C, other layers 77 °C "
                                       "(17 °C apart, at most 15 °C)")
    assert "allow_temperature_pairs=true" in out["hint"] and "set_config" in out["hint"]
    assert "allow_temperature_pairs" not in json.loads(save.calls.last.request.read())  # not sent unless asked


@respx.mock
async def test_save_preset_allow_sends_the_flag_and_returns_the_allowed_pairs(monkeypatch):
    _env(monkeypatch)
    save = respx.post(f"{URL}/api/v1/preset/save").mock(return_value=httpx.Response(
        200, json={"saved": "My PLA", "created": False, "temperature_pairs_allowed": [PAIR]}))
    out = await srv.save_preset("filament", "My PLA", allow_temperature_pairs=True)
    assert json.loads(save.calls.last.request.read())["allow_temperature_pairs"] is True
    assert out["temperature_pairs_allowed"] == [PAIR]


@respx.mock
async def test_save_preset_slot_refusal_is_the_same(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{URL}/api/v1/status").mock(return_value=httpx.Response(
        200, json={"capabilities": ["filament_slots", "preset_temperature_check"], "presets": {}, "modified": {}}))
    save = respx.post(f"{URL}/api/v1/preset/save").mock(return_value=_refused())
    out = await srv.save_preset("filament", "My PLA", filament_slot=2, allow_temperature_pairs=False)
    assert out["error"].startswith("temperature_pairs_unsafe") and out["pairs"][0]["delta"] == 17
    assert json.loads(save.calls.last.request.read())["slot"] == 1


@respx.mock
async def test_old_build_timeout_on_a_filament_save_points_at_the_dialog(monkeypatch):
    _env(monkeypatch)
    respx.post(f"{URL}/api/v1/preset/save").mock(return_value=httpx.Response(504, json={"error": "ui_timeout"}))
    out = await srv.save_preset("filament", "My PLA")
    assert out["error"].startswith("ui_timeout") and "Temperature Safety Check" in out["hint"]
    out = await srv.save_preset("print", "My print")
    assert "Temperature Safety Check" not in out.get("hint", "")


def _edit_mocks():
    respx.get(f"{URL}/api/v1/status").mock(return_value=httpx.Response(200, json={}))  # the fork, not the plugin
    respx.post(f"{URL}/api/v1/preset/config").mock(return_value=httpx.Response(
        200, json={"config": {"hot_plate_temp": "60", "hot_plate_temp_initial_layer": "60"}}))
    respx.put(f"{URL}/api/v1/preset").mock(return_value=httpx.Response(200, json={"selected": True}))
    respx.get(f"{URL}/api/v1/config").mock(return_value=httpx.Response(
        200, json={"config": {"hot_plate_temp": "60", "hot_plate_temp_initial_layer": "60"}}))
    respx.put(f"{URL}/api/v1/config").mock(return_value=httpx.Response(200, json={"applied": ["hot_plate_temp"]}))


@respx.mock
async def test_edit_preset_refusal_says_the_edit_is_still_unsaved(monkeypatch):
    _env(monkeypatch)
    _edit_mocks()
    respx.post(f"{URL}/api/v1/preset/save").mock(return_value=_refused())
    out = await srv.edit_preset("filament", "My PLA", {"hot_plate_temp": 77})
    assert out["error"].startswith("temperature_pairs_unsafe")
    assert "unsaved edits" in out["hint"] and "set_config with null" in out["hint"]


@respx.mock
async def test_edit_preset_passes_allow_through(monkeypatch):
    _env(monkeypatch)
    _edit_mocks()
    save = respx.post(f"{URL}/api/v1/preset/save").mock(return_value=httpx.Response(
        200, json={"saved": "My PLA", "created": False, "temperature_pairs_allowed": [PAIR]}))
    out = await srv.edit_preset("filament", "My PLA", {"hot_plate_temp": 77}, allow_temperature_pairs=True)
    assert out.get("error") is None and out["saved"]["temperature_pairs_allowed"] == [PAIR]
    assert json.loads(save.calls.last.request.read())["allow_temperature_pairs"] is True
