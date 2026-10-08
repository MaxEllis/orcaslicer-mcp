import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp.printer import match as m

P = "http://192.0.2.10"
F = "http://x:13130"
SETTINGS = {
    "printer": {"max_accel": 5000.0, "max_velocity": 300.0},
    "extruder": {"nozzle_diameter": 0.4, "max_temp": 280.0},
    "heater_bed": {"max_temp": 110.0},
    "stepper_x": {"position_min": 0.0, "position_max": 235.0},
    "stepper_y": {"position_min": 0.0, "position_max": 235.0},
    "stepper_z": {"position_min": -2.0, "position_max": 250.0},
}
CFG = {
    "nozzle_diameter": "0.4", "printable_area": "0x0,235x0,235x235,0x235", "printable_height": "250",
    "default_acceleration": "3000", "outer_wall_acceleration": "2000", "travel_acceleration": "4000",
    "outer_wall_speed": "120", "travel_speed": "250",
    "nozzle_temperature": "210", "nozzle_temperature_initial_layer": "215",
    "curr_bed_type": "Textured PEI Plate", "textured_plate_temp": "60", "textured_plate_temp_initial_layer": "65",
    "use_firmware_retraction": "0", "retraction_length": "0.8",
}


def by_check(result):
    return {c["check"]: c for c in result["checks"]}


def test_parsers():
    assert m.floats("0.4,0.6") == [0.4, 0.6] and m.floats("nil") == [] and m.floats(3) == [3.0]
    assert m.points("0x0,235x0,235x235") == [(0.0, 0.0), (235.0, 0.0), (235.0, 235.0)]


def test_a_matching_profile_passes_every_check():
    r = m.compare(CFG, SETTINGS)
    assert all(c["status"] == "ok" for c in r["checks"]), r
    assert len(r["checks"]) == 7 and r["headline"] == "The profile matches the printer on 7 of 7 checks."


@pytest.mark.parametrize("change,check", [
    ({"nozzle_diameter": "0.8"}, "nozzle"),
    ({"printable_area": "0x0,300x0,300x300,0x300"}, "bed"),
    ({"printable_height": "400"}, "bed"),
    ({"travel_acceleration": "8000"}, "acceleration"),
    ({"travel_speed": "500"}, "speed"),
    ({"nozzle_temperature": "300"}, "nozzle temperature"),
    ({"textured_plate_temp_initial_layer": "120"}, "bed temperature"),
    ({"use_firmware_retraction": "1"}, "firmware retraction"),
])
def test_each_mismatch_warns_and_nothing_else_does(change, check):
    r = by_check(m.compare({**CFG, **change}, SETTINGS))
    assert r[check]["status"] == "warn"
    assert all(c["status"] == "ok" for name, c in r.items() if name != check)


def test_firmware_retraction_length_mismatch_names_klippers_value():
    s = {**SETTINGS, "firmware_retraction": {"retract_length": 0.3}}
    c = by_check(m.compare({**CFG, "use_firmware_retraction": "1"}, s))["firmware retraction"]
    assert c["status"] == "warn" and "0.3 mm is used, not the profile's 0.8 mm" in c["why"]


def test_bed_type_names_match_case_insensitively():
    cfg = {**CFG, "curr_bed_type": "SuperTack Plate", "supertack_plate_temp": "200"}
    assert by_check(m.compare(cfg, SETTINGS))["bed temperature"]["status"] == "warn"


def test_headline_lists_warnings():
    r = m.compare({**CFG, "nozzle_diameter": "0.8", "travel_speed": "500"}, SETTINGS)
    assert r["headline"] == "2 warnings: nozzle, speed."


def test_missing_printer_settings_are_unknown_not_errors():
    r = m.compare(CFG, {})
    statuses = {c["check"]: c["status"] for c in r["checks"]}
    assert [name for name, s in statuses.items() if s != "unknown"] == ["firmware retraction"]  # off needs no Klipper data
    assert r["headline"] == "The profile matches the printer on 1 of 7 checks. 6 couldn't be checked."


@pytest.mark.parametrize("value,expected", [("220,230", "warn"), ("", "unknown"), ("nil", "unknown"), (None, "unknown")])
def test_odd_config_values(value, expected):  # Review Focus 4
    cfg = {**CFG, "nozzle_temperature": value, "nozzle_temperature_initial_layer": value}
    settings = {**SETTINGS, "extruder": {**SETTINGS["extruder"], "max_temp": 225.0}}
    assert by_check(m.compare(cfg, settings))["nozzle temperature"]["status"] == expected


def test_no_why_uses_an_em_dash():
    r = m.compare({**CFG, "nozzle_diameter": "0.8", "use_firmware_retraction": "1"}, SETTINGS)
    assert all("—" not in c["why"] for c in r["checks"]) and "—" not in r["headline"]


def printer_routes():
    respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json={"result": {"klippy_state": "ready"}}))
    respx.get(url__startswith=f"{P}/printer/objects/query").mock(
        return_value=httpx.Response(200, json={"result": {"status": {"configfile": {"settings": SETTINGS}}}}))


@respx.mock
async def test_check_printer_match_tool(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", F)
    printer_routes()
    respx.get(url__regex=rf"{F}/api/v1/config.*").mock(
        return_value=httpx.Response(200, json={"config": {**CFG, "nozzle_diameter": "0.8"}}))
    out = await srv.check_printer_match()
    assert out["headline"] == "1 warning: nozzle." and out["printer"]["kind"] == "klipper"


@respx.mock
async def test_check_printer_match_needs_orcaslicer(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    monkeypatch.delenv("ORCA_API_TOKEN", raising=False)
    printer_routes()
    out = await srv.check_printer_match()
    assert out["error"] == "orca_unreachable"
