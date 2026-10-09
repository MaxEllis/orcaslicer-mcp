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
    assert r["headline"] == "No mismatches found: 1 check passed, 6 couldn't be checked."


def test_acceleration_warning_says_the_printer_accelerates_harder():
    c = by_check(m.compare({**CFG, "travel_acceleration": "8000"}, SETTINGS))["acceleration"]
    assert c["status"] == "warn"
    assert "accelerate harder" in c["why"] and "slows" not in c["why"]
    assert "max_accel is 5000 mm/s²" in c["why"] and "(travel_acceleration)" in c["why"]


def test_speed_warning_says_klipper_slows_the_moves_unless_the_gcode_raises_the_limit():
    c = by_check(m.compare({**CFG, "travel_speed": "500"}, SETTINGS))["speed"]
    assert c["status"] == "warn"
    assert "Klipper caps speed at 300 mm/s" in c["why"] and "SET_VELOCITY_LIMIT" in c["why"]
    assert "slows those moves down" in c["why"]


def test_firmware_retraction_unknown_without_klipper_settings():
    assert by_check(m.compare({**CFG, "use_firmware_retraction": "1"}, {}))["firmware retraction"]["status"] == "unknown"


def test_firmware_retraction_unknown_when_the_profile_has_no_length():
    s = {**SETTINGS, "firmware_retraction": {"retract_length": 0.3}}
    cfg = {k: v for k, v in {**CFG, "use_firmware_retraction": "1"}.items() if k != "retraction_length"}
    assert by_check(m.compare(cfg, s))["firmware retraction"]["status"] == "unknown"


def test_firmware_retraction_unknown_when_the_klipper_section_has_no_length():
    s = {**SETTINGS, "firmware_retraction": {}}
    assert by_check(m.compare({**CFG, "use_firmware_retraction": "1"}, s))["firmware retraction"]["status"] == "unknown"


def test_nothing_could_be_checked_says_so():
    for cfg, settings in (({}, {}), ({**CFG, "use_firmware_retraction": "1"}, {})):
        r = m.compare(cfg, settings)
        assert all(c["status"] == "unknown" for c in r["checks"]), r
        assert r["headline"] == "Nothing could be checked: the profile or the printer's settings weren't available."


def test_no_mismatches_headline_counts_passed_and_unknown():
    s = {"printer": SETTINGS["printer"], "extruder": SETTINGS["extruder"]}  # no steppers, no heater_bed
    r = m.compare(CFG, s)
    assert [c["status"] for c in r["checks"]].count("unknown") == 2
    assert r["headline"] == "No mismatches found: 5 checks passed, 2 couldn't be checked."


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
    assert out["message"] == "check_printer_match couldn't read the active profile from OrcaSlicer."
    assert out["hint"] == ("Check ORCA_API_TOKEN in this MCP server's settings: it must match the token on the "
                           "Remote API page of OrcaSlicer's Preferences.")
    assert "ORCA_API_TOKEN is required" in out["detail"]


@respx.mock
async def test_check_printer_match_fork_unreachable_says_to_start_it(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", F)
    printer_routes()
    respx.get(url__regex=rf"{F}/api/v1/config.*").mock(side_effect=httpx.ConnectError("refused"))
    out = await srv.check_printer_match()
    assert out["error"] == "orca_unreachable"
    assert out["hint"] == "Start OrcaSlicer (MCP build) with the Remote API enabled."
    assert "refused" in out["detail"]


@respx.mock
async def test_check_printer_match_rejected_token_says_to_check_the_token(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    monkeypatch.setenv("ORCA_API_TOKEN", "wrong")
    monkeypatch.setenv("ORCA_API_URL", F)
    printer_routes()
    respx.get(url__regex=rf"{F}/api/v1/config.*").mock(return_value=httpx.Response(401, json={"error": "unauthorized"}))
    out = await srv.check_printer_match()
    assert out["error"] == "orca_unreachable"
    assert out["hint"].startswith("Check ORCA_API_TOKEN")


# --- an OrcaSlicer settings problem that is not the token gets its own hint ---------------------------

SETTINGS_HINT = "Check the OrcaSlicer settings of this MCP server (ORCA_API_URL, ORCA_API_TIMEOUT)."


@respx.mock
async def test_check_printer_match_with_an_invalid_timeout_does_not_blame_the_token(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_TIMEOUT", "soon")
    printer_routes()
    out = await srv.check_printer_match()
    assert out["error"] == "orca_unreachable"
    assert out["hint"] == SETTINGS_HINT and "ORCA_API_TOKEN" not in out["hint"]
    assert "soon" in out["detail"]


@respx.mock
async def test_finding_the_printer_with_an_invalid_timeout_does_not_blame_the_token(monkeypatch):
    # no ORCA_PRINTER_URL, so the printer is looked up through OrcaSlicer, whose settings are wrong
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_TIMEOUT", "soon")
    out = await srv.get_printer_status()
    assert out["error"] == "orca_unreachable"
    assert out["hint"] == SETTINGS_HINT and "soon" in out["detail"]


# --- firmware retraction: with retraction off, Klipper's side is only named when it was read --------

def test_firmware_retraction_off_says_nothing_about_klipper_when_its_settings_were_not_read():
    r = m.check_firmware_retraction({**CFG, "use_firmware_retraction": "0"}, {})
    assert r["status"] == "ok" and r["profile"] == "off" and r["printer"] is None


def test_firmware_retraction_off_reports_klippers_section_when_the_settings_were_read():
    off = {**CFG, "use_firmware_retraction": "0"}
    assert m.check_firmware_retraction(off, SETTINGS)["printer"] == "absent"
    with_section = {**SETTINGS, "firmware_retraction": {"retract_length": 0.3}}
    assert m.check_firmware_retraction(off, with_section)["printer"] == "present"
    empty_section = {**SETTINGS, "firmware_retraction": {}}
    assert m.check_firmware_retraction(off, empty_section)["printer"] == "present"  # presence is the key


# --- bed temperature: a missing bed type is not an unmapped one -------------------------------------------

@pytest.mark.parametrize("cfg", [{k: v for k, v in CFG.items() if k != "curr_bed_type"},
                                 {**CFG, "curr_bed_type": ""}, {**CFG, "curr_bed_type": None}],
                         ids=["absent", "empty", "null"])
def test_a_missing_bed_type_is_reported_as_not_reported(cfg):
    c = m.check_bed_temp(cfg, SETTINGS)
    assert c["status"] == "unknown"
    assert c["why"] == "OrcaSlicer didn't report which bed type is selected, so the bed temperature can't be checked."


def test_a_bed_type_with_no_temperature_setting_keeps_its_own_wording():
    c = m.check_bed_temp({**CFG, "curr_bed_type": "Some New Plate"}, SETTINGS)
    assert c["status"] == "unknown" and c["why"] == "The bed type isn't one OrcaSlicer maps to a temperature setting."


# --- bed: ok only when the height was compared too ------------------------------------------------------------

def test_bed_is_ok_when_x_y_and_height_were_all_compared():
    c = m.check_bed(CFG, SETTINGS)
    assert c["status"] == "ok" and c["why"] == "The profile's printable space fits the printer's travel."


@pytest.mark.parametrize("cfg,settings,reason", [
    ({k: v for k, v in CFG.items() if k != "printable_height"}, SETTINGS,
     "OrcaSlicer reported no printable height"),
    (CFG, {**SETTINGS, "stepper_z": {"position_min": -2.0}}, "Klipper reports no Z travel limit"),
    ({k: v for k, v in CFG.items() if k != "printable_height"}, {**SETTINGS, "stepper_z": {}},
     "OrcaSlicer reported no printable height and Klipper reports no Z travel limit"),
], ids=["no-height", "no-z-travel", "neither"])
def test_bed_is_not_ok_when_the_height_could_not_be_compared(cfg, settings, reason):
    c = m.check_bed(cfg, settings)
    assert c["status"] == "unknown"
    assert c["why"] == f"X and Y fit the printer's travel, but the height wasn't checked: {reason}."
    assert c["profile"]["x"] == [0.0, 235.0] and c["printer"]["y"] == [0.0, 235.0]  # the values compared stay visible


def test_bed_still_warns_about_x_y_whatever_the_height_data():
    cfg = {**{k: v for k, v in CFG.items() if k != "printable_height"}, "printable_area": "0x0,300x0,300x300,0x300"}
    assert m.check_bed(cfg, SETTINGS)["status"] == "warn"


def test_a_profile_without_a_height_is_summarised_as_a_check_that_could_not_be_made():
    r = m.compare({k: v for k, v in CFG.items() if k != "printable_height"}, SETTINGS)
    assert by_check(r)["bed"]["status"] == "unknown"
    assert r["headline"] == "No mismatches found: 6 checks passed, 1 couldn't be checked."


@respx.mock
async def test_check_printer_match_on_octoprint_says_only_klipper_reports_its_settings(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", F)
    for url in (f"{P}/server/info", f"{P}:7125/server/info"):
        respx.get(url).mock(return_value=httpx.Response(404))
    respx.get(f"{P}/api/version").mock(return_value=httpx.Response(200, json={"api": "0.1", "server": "1.10.2"}))
    fork = respx.route(url__startswith=F).mock(return_value=httpx.Response(500))  # nothing to compare: must not be asked
    out = await srv.check_printer_match()
    assert out["supported"] is False and out["reason"] == "Only Klipper printers report their settings."
    assert out["printer"]["kind"] == "octoprint" and out["printer"]["source"] == "override"
    assert "checks" not in out and "headline" not in out
    assert fork.call_count == 0
