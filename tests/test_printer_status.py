import pytest

from orcaslicer_mcp.printer import status as st

TP = {"profile": "Test Printer", "url": "http://192.0.2.10", "kind": "klipper", "source": "profile",
      "remembered_at": None}
OP = {**TP, "kind": "octoprint"}
INFO = {"klippy_state": "ready", "klippy_connected": True, "warnings": [], "failed_components": []}
MCU_LOST = ("Lost communication with MCU 'mcu'\nOnce the underlying issue is corrected, use the\n"
            "\"FIRMWARE_RESTART\" command to reset the firmware, reload the\n"
            "config, and restart the host software.\nPrinter is shutdown\n")


def kstatus(**over):
    s = {
        "webhooks": {"state": "ready", "state_message": "Printer is ready"},
        "print_stats": {"state": "standby", "filename": "", "print_duration": 0.0, "total_duration": 0.0,
                        "filament_used": 0.0, "message": "", "info": {"current_layer": None, "total_layer": None}},
        "display_status": {"progress": 0.0},
        "virtual_sdcard": {"progress": 0.0},
        "extruder": {"temperature": 24.9, "target": 0.0, "pressure_advance": 0.04},
        "heater_bed": {"temperature": 23.1, "target": 0.0},
        "toolhead": {"homed_axes": "", "position": [0.0, 0.0, 0.0, 0.0]},
        "gcode_move": {"homing_origin": [0.0, 0.0, -0.05, 0.0], "speed_factor": 1.0, "extrude_factor": 1.0},
        "fan": {"speed": 0.0},
    }
    for key, val in over.items():
        if val is None:
            s.pop(key, None)
        else:
            s[key] = {**s.get(key, {}), **val}
    return s


def snap(status, info=INFO, console=(), since=0.0, meta=None):
    return st.klipper_snapshot(status, info, list(console), since=since, file_meta=meta, target_public=TP)


def test_idle():
    s = snap(kstatus())
    assert s["state"] == "idle" and s["job"] is None and s["connected"] is True
    assert s["headline"] == "Idle. Nozzle 25 °C, bed 23 °C. No problems."


def test_heating_before_extrusion():
    s = snap(kstatus(print_stats={"state": "printing", "filename": "benchy.gcode"},
                     extruder={"temperature": 150.0, "target": 215.0}))
    assert s["state"] == "heating"
    assert s["headline"].startswith("Heating up to print benchy.")


def test_printing_with_the_slicer_estimate():
    s = snap(kstatus(print_stats={"state": "printing", "filename": "benchy.gcode", "print_duration": 1200.0,
                                  "total_duration": 1300.0, "filament_used": 300.0,
                                  "info": {"current_layer": 30, "total_layer": 80}},
                     display_status={"progress": 0.42},
                     extruder={"temperature": 215.1, "target": 215.0},
                     heater_bed={"temperature": 60.0, "target": 60.0}),
             meta={"estimated_time": 2280, "first_layer_height": 0.3})
    assert s["state"] == "printing"
    assert s["job"]["remaining_s"] == 1080 and s["job"]["remaining_basis"] == "slicer_estimate"
    assert s["job"]["layer"] == {"current": 30, "total": 80} and s["job"]["first_layer_height"] == 0.3
    assert s["headline"] == ("Printing benchy: 42% (layer 30/80), about 18 min left. "
                             "Nozzle 215/215 °C, bed 60/60 °C. No problems.")


def test_printing_without_an_estimate_uses_progress():
    s = snap(kstatus(print_stats={"state": "printing", "filename": "a.gcode", "print_duration": 600.0,
                                  "filament_used": 50.0},
                     display_status={"progress": 0.5}))
    assert s["job"]["remaining_s"] == 600 and s["job"]["remaining_basis"] == "progress"


def test_too_early_to_guess_time_left():
    s = snap(kstatus(print_stats={"state": "printing", "filename": "a.gcode", "print_duration": 30.0,
                                  "filament_used": 5.0},
                     display_status={"progress": 0.01}))
    assert s["job"]["remaining_s"] is None and s["job"]["remaining_basis"] is None


def test_an_overrun_estimate_falls_back_to_progress():
    s = snap(kstatus(print_stats={"state": "printing", "filename": "benchy.gcode", "print_duration": 3000.0,
                                  "filament_used": 300.0},
                     display_status={"progress": 0.6}),
             meta={"estimated_time": 2280})
    assert s["job"]["remaining_s"] == 2000 and s["job"]["remaining_basis"] == "progress"
    assert "about 1 min left" not in s["headline"] and "about 33 min left" in s["headline"]
    s = snap(kstatus(print_stats={"state": "printing", "filename": "benchy.gcode", "print_duration": 3000.0,
                                  "filament_used": 300.0},
                     display_status={"progress": 0.0}),
             meta={"estimated_time": 2280})
    assert s["job"]["remaining_s"] is None and s["job"]["remaining_basis"] is None
    assert "left" not in s["headline"]


def test_finished_job_is_kept():
    s = snap(kstatus(print_stats={"state": "complete", "filename": "benchy.gcode", "print_duration": 2000.0}))
    assert s["state"] == "finished" and s["job"]["file"] == "benchy.gcode" and s["job"]["remaining_s"] is None
    assert s["headline"].startswith("Finished benchy.")


def test_shutdown_is_fatal_with_a_hint():
    s = snap(kstatus(webhooks={"state": "shutdown", "state_message": MCU_LOST},
                     print_stats={"state": "complete", "filename": "old.gcode"}))
    assert s["state"] == "shutdown" and s["job"] is None
    assert s["problems"][0]["severity"] == "fatal"
    assert s["problems"][0]["message"] == "Lost communication with MCU 'mcu'"
    assert "switched off" in s["problems"][0]["hint"]
    assert s["headline"].startswith("The printer is shut down.")
    assert s["headline"].endswith("Problem: Lost communication with MCU 'mcu'.")


def test_klipper_starting_up_is_offline():
    s = snap(kstatus(webhooks={"state": "startup"}))
    assert s["state"] == "offline"
    assert [p["message"] for p in s["problems"]] == ["Klipper is starting up; check again in a minute."]
    assert s["problems"][0]["severity"] == "warning" and s["problems"][0]["hint"] is None
    assert "No problems" not in s["headline"]


def test_klipper_disconnected_from_moonraker_is_offline_and_fatal():
    s = snap({}, info={**INFO, "klippy_connected": False})
    assert s["state"] == "offline"
    assert s["problems"][0]["severity"] == "fatal" and "isn't connected" in s["problems"][0]["message"]


def test_missing_objects_are_tolerated():  # Review Focus 2
    s = snap(kstatus(heater_bed=None, display_status=None, fan=None, virtual_sdcard=None))
    assert s["temps"]["bed"] is None and s["klipper"]["fan_percent"] is None
    assert s["headline"] == "Idle. Nozzle 25 °C. No problems."


def test_odd_values_do_not_crash():  # Review Focus 3
    s = snap(kstatus(print_stats={"state": "printing", "filename": "a.gcode", "print_duration": None,
                                  "filament_used": None},
                     display_status={"progress": None},
                     virtual_sdcard={"progress": None},
                     extruder={"temperature": None, "target": "abc"},
                     fan={"speed": "fast"}))
    assert s["state"] == "printing"
    assert s["job"]["remaining_s"] is None and s["job"]["progress_percent"] is None
    assert s["temps"]["nozzle"] == {"actual": None, "target": None}
    assert s["klipper"]["fan_percent"] is None


def test_console_errors_and_warnings_become_problems():
    console = [{"message": "!! Move out of range: 400.000 1.000 0.300 [0.000]", "time": 500.0},
               {"message": "!! old one", "time": 10.0}]
    info = {**INFO, "warnings": ["Moonraker not authorized for PolicyKit action: [org.example.a]",
                                 "Moonraker not authorized for PolicyKit action: [org.example.b]"]}
    s = snap(kstatus(), info=info, console=console, since=100.0)
    msgs = [p["message"] for p in s["problems"]]
    assert msgs[0].startswith("Move out of range") and "old one" not in " ".join(msgs)
    assert msgs[-1] == "2 Moonraker setup warnings (system permissions), not print-related."
    assert s["headline"].endswith("Problem: Move out of range: 400.000 1.000 0.300 [0.000].")


def test_octoprint_not_connected_to_the_printer():
    s = st.octoprint_snapshot(None, {}, target_public=OP)
    assert s["state"] == "offline" and s["problems"][0]["severity"] == "fatal"


def test_octoprint_printing():
    printer = {"state": {"text": "Printing", "flags": {"operational": True, "printing": True}},
               "temperature": {"tool0": {"actual": 215.0, "target": 215.0}, "bed": {"actual": 60.0, "target": 60.0}}}
    job = {"job": {"file": {"name": "bracket.gcode"}},
           "progress": {"completion": 42.4, "printTime": 1200, "printTimeLeft": 1080}, "state": "Printing"}
    s = st.octoprint_snapshot(printer, job, target_public=OP)
    assert s["state"] == "printing" and s["job"]["remaining_basis"] == "printer_estimate"
    assert s["headline"] == "Printing bracket: 42%, about 18 min left. Nozzle 215/215 °C, bed 60/60 °C. No problems."


def test_octoprint_error():
    printer = {"state": {"text": "Offline after error", "flags": {"error": True, "closedOrError": True}},
               "temperature": {}}
    s = st.octoprint_snapshot(printer, {}, target_public=OP)
    assert s["state"] == "error" and s["problems"][0]["message"] == "Offline after error"


def test_octoprint_finished_job():
    printer = {"state": {"text": "Operational", "flags": {"operational": True, "ready": True}}, "temperature": {}}
    job = {"job": {"file": {"name": "bracket.gcode"}}, "progress": {"completion": 100.0}, "state": "Operational"}
    assert st.octoprint_snapshot(printer, job, target_public=OP)["state"] == "finished"


@pytest.mark.parametrize("seconds,text", [(59, "1 min"), (1080, "18 min"), (3600, "1 h"), (3900, "1 h 5 min")])
def test_durations(seconds, text):
    assert st._dur(seconds) == text


def test_headlines_never_use_em_dashes():
    for s in (snap(kstatus()), snap(kstatus(webhooks={"state": "shutdown", "state_message": MCU_LOST})),
              st.octoprint_snapshot(None, {}, target_public=OP)):
        assert "—" not in s["headline"]
