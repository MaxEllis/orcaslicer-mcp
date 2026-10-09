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


STARTING_UP = "Klipper is starting up; check again in a minute."


def test_klipper_starting_up_is_offline_with_no_temperatures():
    # Klipper hasn't read its sensors yet and reports 0 for both heaters: that is not a measurement.
    s = snap(kstatus(webhooks={"state": "startup"}, extruder={"temperature": 0.0, "target": 0.0},
                     heater_bed={"temperature": 0.0, "target": 0.0}))
    assert s["state"] == "offline" and s["connected"] is False
    assert s["temps"] == {"nozzle": None, "bed": None}
    assert [p["message"] for p in s["problems"]] == [STARTING_UP]
    assert s["problems"][0]["severity"] == "warning" and s["problems"][0]["hint"] is None
    assert s["headline"] == STARTING_UP


def test_the_starting_up_headline_stands_alone_even_with_other_warnings():
    info = {**INFO, "warnings": ["Moonraker not authorized for PolicyKit action: [org.example.a]"]}
    s = snap(kstatus(webhooks={"state": "startup"}, extruder={"temperature": 0.0}), info=info)
    assert s["headline"] == STARTING_UP
    assert len(s["problems"]) == 2  # the warnings are still reported in full


def test_starting_up_with_no_heater_sections_at_all():
    s = snap({"webhooks": {"state": "startup"}})
    assert s["temps"] == {"nozzle": None, "bed": None} and s["headline"] == STARTING_UP


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


NOT_A_NUMBER = [float("nan"), float("inf"), float("-inf")]


@pytest.mark.parametrize("bad", NOT_A_NUMBER)
def test_nan_and_infinity_from_a_printer_do_not_crash(bad):  # json.loads accepts NaN and Infinity
    s = snap(kstatus(print_stats={"state": "printing", "filename": "a.gcode", "print_duration": bad,
                                  "filament_used": bad},
                     display_status={"progress": bad},
                     virtual_sdcard={"progress": bad},
                     extruder={"temperature": bad, "target": 215.0},
                     heater_bed={"temperature": 60.0, "target": bad},
                     fan={"speed": bad},
                     toolhead={"position": [0.0, 0.0, bad, 0.0]},
                     gcode_move={"homing_origin": [0.0, 0.0, bad, 0.0], "speed_factor": bad}),
             meta={"estimated_time": bad, "first_layer_height": bad})
    assert s["job"]["progress_percent"] is None and s["job"]["remaining_s"] is None
    assert s["temps"]["nozzle"] == {"actual": None, "target": 215.0}
    assert s["temps"]["bed"] == {"actual": 60.0, "target": None}
    assert s["klipper"]["fan_percent"] is None and s["klipper"]["z_mm"] is None
    assert isinstance(s["headline"], str) and not any(w in s["headline"].lower() for w in ("nan", "inf"))
    octo = st.octoprint_snapshot(
        {"state": {"text": "Printing", "flags": {"printing": True}},
         "temperature": {"tool0": {"actual": bad, "target": 215.0}, "bed": {"actual": 60.0, "target": bad}}},
        {"job": {"file": {"name": "a.gcode"}}, "progress": {"completion": bad, "printTime": bad, "printTimeLeft": bad}},
        target_public=OP)
    assert octo["job"]["progress_percent"] is None and octo["job"]["remaining_s"] is None
    assert octo["temps"]["nozzle"]["actual"] is None


def test_a_huge_integer_is_not_a_number_either():
    assert st._f(10 ** 400) is None and st._f(float("nan")) is None and st._f("12.34") == 12.3


@pytest.mark.parametrize("section", ["print_stats", "display_status", "virtual_sdcard", "toolhead", "gcode_move",
                                     "fan", "extruder", "heater_bed", "configfile", "webhooks"])
def test_a_status_section_that_is_not_an_object_counts_as_missing(section):
    status = kstatus()
    status[section] = "oops"
    s = snap(status)
    assert isinstance(s["headline"], str) and s["connected"] is True


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


def _octo_printing(nozzle_actual, completion=28.0, left=900):
    """A job OctoPrint calls "printing", with the nozzle at nozzle_actual against a 200 target. Its
    completion is a position in the file, so it is above zero while the header and start G-code are
    read and the heaters are still warming."""
    printer = {"state": {"text": "Printing", "flags": {"operational": True, "printing": True}},
               "temperature": {"tool0": {"actual": nozzle_actual, "target": 200.0},
                               "bed": {"actual": 60.0, "target": 60.0}}}
    job = {"job": {"file": {"name": "test-part.gcode"}},
           "progress": {"completion": completion, "printTime": 30, "printTimeLeft": left}, "state": "Printing"}
    return st.octoprint_snapshot(printer, job, target_public=OP)


def test_octoprint_is_heating_while_a_heater_is_below_target_even_with_file_progress():
    s = _octo_printing(60.0)
    assert s["state"] == "heating"
    assert s["headline"] == ("Printing test-part: 28%, about 15 min left, waiting for the heaters. "
                             "Nozzle 60/200 °C, bed 60/60 °C. No problems.")


def test_a_mid_print_temperature_dip_reads_the_same_as_the_first_heat_up():
    """OctoPrint shows file progress above 0 both during the first heat-up and when a running print
    reheats, so the wording must be true for both."""
    s = _octo_printing(150.0, completion=55.0, left=1200)
    assert s["state"] == "heating"
    assert s["headline"] == ("Printing test-part: 55%, about 20 min left, waiting for the heaters. "
                             "Nozzle 150/200 °C, bed 60/60 °C. No problems.")


def test_heating_with_progress_and_no_time_left_estimate():
    s = _octo_printing(60.0, left=None)
    assert s["state"] == "heating" and s["job"]["remaining_s"] is None
    assert s["headline"] == ("Printing test-part: 28%, waiting for the heaters. "
                             "Nozzle 60/200 °C, bed 60/60 °C. No problems.")


@pytest.mark.parametrize("completion", [0.0, 0.3, None])
def test_octoprint_heating_before_any_progress_is_a_plain_heat_up(completion):
    s = _octo_printing(60.0, completion=completion)
    assert s["state"] == "heating"
    assert s["headline"] == "Heating up to print test-part. Nozzle 60/200 °C, bed 60/60 °C. No problems."


def test_octoprint_bed_only_heating():
    printer = {"state": {"text": "Printing", "flags": {"operational": True, "printing": True}},
               "temperature": {"tool0": {"actual": 200.0, "target": 200.0}, "bed": {"actual": 30.0, "target": 60.0}}}
    job = {"job": {"file": {"name": "test-part.gcode"}}, "progress": {"completion": 0.0, "printTimeLeft": 900}}
    s = st.octoprint_snapshot(printer, job, target_public=OP)
    assert s["state"] == "heating"
    assert s["headline"] == "Heating up to print test-part. Nozzle 200/200 °C, bed 30/60 °C. No problems."


def test_heating_with_progress_keeps_layers_problems_and_warnings():
    job = {"file": "a.gcode", "progress_percent": 5, "layer": {"current": 3, "total": 90}, "remaining_s": 3000}
    temps = {"nozzle": {"actual": 100.0, "target": 215.0}, "bed": None}
    base = {"state": "heating", "job": job, "temps": temps}
    warn = {"severity": "warning", "message": "Heads up"}
    fatal = {"severity": "fatal", "message": "It broke."}
    error = {"severity": "error", "message": "Another."}
    assert st.headline({**base, "problems": [warn]}) == (
        "Printing a: 5% (layer 3/90), about 50 min left, waiting for the heaters. Nozzle 100/215 °C. 1 warning.")
    assert st.headline({**base, "problems": [fatal, error, warn]}).endswith(
        "waiting for the heaters. Nozzle 100/215 °C. Problem: It broke (+1 more).")


def test_klipper_heating_with_file_progress_is_still_the_first_heat_up():
    """Klipper reports exactly 0 mm of filament before the first extrusion, so its heating is the
    initial heat-up even when the file position is above 0 (a large header or thumbnails)."""
    s = snap(kstatus(print_stats={"state": "printing", "filename": "benchy.gcode", "filament_used": 0.0},
                     display_status={"progress": 0.12},
                     extruder={"temperature": 150.0, "target": 215.0}))
    assert s["state"] == "heating" and s["job"]["progress_percent"] == 12
    assert s["headline"].startswith("Heating up to print benchy. Nozzle 150/215 °C")


def test_octoprint_is_printing_once_the_heaters_are_at_target():
    s = _octo_printing(199.0)
    assert s["state"] == "printing"
    assert s["headline"].startswith("Printing test-part: 28%")


@pytest.mark.parametrize("flags", [{"error": True}, {"closedOrError": True}])
def test_octoprint_error(flags):
    printer = {"state": {"text": "Offline after error", "flags": flags}, "temperature": {}}
    s = st.octoprint_snapshot(printer, {}, target_public=OP)
    assert s["state"] == "error" and s["problems"][0]["message"] == "Offline after error"
    assert s["problems"][0]["severity"] == "fatal"


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


# --- layers are whole numbers or None ---------------------------------------------------------

def _with_layers(current, total):
    return snap(kstatus(print_stats={"state": "printing", "filename": "a.gcode", "filament_used": 50.0,
                                     "info": {"current_layer": current, "total_layer": total}}))


@pytest.mark.parametrize("current,total,want", [
    (30, 80, (30, 80)),
    ("30", "80", (30, 80)),
    (30.0, 80.0, (30, 80)),
    (" 7 ", "80", (7, 80)),
    ("abc", 80, (None, 80)),
    (30, [80], (30, None)),
    (30, {"n": 80}, (30, None)),
    (None, None, (None, None)),
    (float("nan"), float("inf"), (None, None)),
    (float("-inf"), 10 ** 400, (None, None)),
    (True, False, (None, None)),
])
def test_layer_numbers_are_whole_numbers_or_none(current, total, want):
    s = _with_layers(current, total)
    layer = s["job"]["layer"]
    assert (layer["current"], layer["total"]) == want
    assert all(v is None or type(v) is int for v in layer.values())
    assert isinstance(s["headline"], str)


def test_layer_numbers_sent_as_text_still_show_in_the_headline():
    assert "(layer 30/80)" in _with_layers("30", "80")["headline"]
    assert "layer" not in _with_layers("abc", "80")["headline"]


# --- "almost done" ------------------------------------------------------------------------------

def test_klipper_at_100_percent_is_almost_done():
    s = snap(kstatus(print_stats={"state": "printing", "filename": "a.gcode", "print_duration": 2000.0,
                                  "filament_used": 300.0},
                     display_status={"progress": 1.0}))
    assert s["job"]["remaining_s"] == 0
    assert s["headline"] == "Printing a: 100%, almost done. Nozzle 25 °C, bed 23 °C. No problems."


def test_octoprint_with_zero_time_left_is_almost_done():
    s = _octo_printing(199.0, completion=99.6, left=0)
    assert s["state"] == "printing" and s["job"]["remaining_s"] == 0
    assert "about" not in s["headline"] and s["headline"].startswith("Printing test-part: 100%, almost done.")


def test_octoprint_at_100_percent_with_no_time_left_estimate_is_almost_done():
    s = _octo_printing(199.0, completion=100.0, left=None)
    assert s["headline"].startswith("Printing test-part: 100%, almost done.")


def test_100_percent_wins_over_a_stale_time_left():
    s = _octo_printing(199.0, completion=100.0, left=600)
    assert s["job"]["remaining_s"] == 600 and "almost done" in s["headline"] and "10 min" not in s["headline"]


def test_a_short_time_left_still_says_so():
    s = _octo_printing(199.0, completion=96.0, left=40)
    assert "about 1 min left" in s["headline"] and "almost done" not in s["headline"]


# --- shape ------------------------------------------------------------------------------------

def _all_shapes():
    return {
        "klipper idle": snap(kstatus()),
        "klipper starting": snap(kstatus(webhooks={"state": "startup"})),
        "klipper disconnected": snap({}, info={**INFO, "klippy_connected": False}),
        "klipper printing": snap(kstatus(print_stats={"state": "printing", "filename": "a.gcode",
                                                      "filament_used": 5.0})),
        "octoprint offline": st.octoprint_snapshot(None, {}, target_public=OP),
        "octoprint printing": _octo_printing(199.0),
    }


def test_headline_is_the_first_key_of_every_snapshot():
    for name, s in _all_shapes().items():
        assert list(s)[0] == "headline", name


def test_connected_is_false_exactly_when_the_state_is_offline():
    for name, s in _all_shapes().items():
        assert s["connected"] is (s["state"] != "offline"), name
    shapes = _all_shapes()
    assert shapes["klipper starting"]["connected"] is False
    assert shapes["klipper disconnected"]["connected"] is False
    assert shapes["octoprint offline"]["connected"] is False
    assert shapes["klipper idle"]["connected"] is True and shapes["octoprint printing"]["connected"] is True


def test_a_shut_down_printer_is_still_connected():
    s = snap(kstatus(webhooks={"state": "shutdown", "state_message": MCU_LOST}))
    assert s["state"] == "shutdown" and s["connected"] is True


def _octo_ended(flags, text, *, completion=100.0, file="test-part.gcode"):
    printer = {"state": {"text": text, "flags": {"operational": True, **flags}}, "temperature": {}}
    job = {"job": {"file": {"name": file} if file else {}}, "progress": {"completion": completion}, "state": text}
    return st.octoprint_snapshot(printer, job, target_public=OP)


@pytest.mark.parametrize("flags,text,state", [
    ({"ready": True}, "Operational", "finished"),
    ({"cancelling": True}, "Cancelling", "cancelled"),
    ({"error": True}, "Offline after error", "error"),
    ({"closedOrError": True}, "Offline after error", "error"),
])
def test_octoprint_keeps_the_job_for_every_ended_state(flags, text, state):
    s = _octo_ended(flags, text)
    assert s["state"] == state
    assert s["job"]["file"] == "test-part.gcode"
    assert s["job"]["remaining_s"] is None and s["job"]["remaining_basis"] is None


def test_octoprint_idle_with_a_half_done_file_has_no_job():
    s = _octo_ended({"ready": True}, "Operational", completion=30.0)
    assert s["state"] == "idle" and s["job"] is None


def test_octoprint_error_headline_names_the_file():
    s = _octo_ended({"error": True}, "Offline after error")
    assert s["headline"] == "Stopped with an error while printing test-part. Problem: Offline after error."


# --- states that had no test -------------------------------------------------------------------

def test_klipper_paused():
    s = snap(kstatus(print_stats={"state": "paused", "filename": "benchy.gcode", "print_duration": 1200.0,
                                  "filament_used": 300.0, "info": {"current_layer": 30, "total_layer": 80}},
                     display_status={"progress": 0.42}),
             meta={"estimated_time": 2280})
    assert s["state"] == "paused" and s["job"]["state"] == "paused"
    assert s["job"]["remaining_s"] == 1080  # still reported as data
    assert s["headline"] == "Paused benchy: 42% (layer 30/80). Nozzle 25 °C, bed 23 °C. No problems."


@pytest.mark.parametrize("flags", [{"paused": True}, {"pausing": True}])
def test_octoprint_paused(flags):
    printer = {"state": {"text": "Paused", "flags": {"operational": True, **flags}},
               "temperature": {"tool0": {"actual": 215.0, "target": 215.0}, "bed": {"actual": 60.0, "target": 60.0}}}
    job = {"job": {"file": {"name": "bracket.gcode"}},
           "progress": {"completion": 42.4, "printTime": 1200, "printTimeLeft": 1080}, "state": "Paused"}
    s = st.octoprint_snapshot(printer, job, target_public=OP)
    assert s["state"] == "paused" and s["job"]["file"] == "bracket.gcode"
    assert s["headline"] == "Paused bracket: 42%. Nozzle 215/215 °C, bed 60/60 °C. No problems."


def test_klipper_cancelled_job():
    s = snap(kstatus(print_stats={"state": "cancelled", "filename": "benchy.gcode", "print_duration": 300.0}))
    assert s["state"] == "cancelled" and s["job"]["file"] == "benchy.gcode" and s["job"]["remaining_s"] is None
    assert s["headline"] == "Cancelled benchy. Nozzle 25 °C, bed 23 °C. No problems."


def test_klipper_cancelled_with_no_file_name():
    s = snap(kstatus(print_stats={"state": "cancelled", "filename": ""}))
    assert s["state"] == "cancelled" and s["job"] is None
    assert s["headline"] == "The last print was cancelled. Nozzle 25 °C, bed 23 °C. No problems."


def test_octoprint_cancelled():
    s = _octo_ended({"cancelling": True}, "Cancelling", completion=12.0)
    assert s["state"] == "cancelled" and s["headline"] == "Cancelled test-part. No problems."
    s = _octo_ended({"cancelling": True}, "Cancelling", completion=12.0, file=None)
    assert s["job"] is None and s["headline"] == "The last print was cancelled. No problems."


def test_klipper_job_error_is_reported_with_the_printers_words():
    s = snap(kstatus(print_stats={"state": "error", "filename": "benchy.gcode", "print_duration": 300.0,
                                  "message": "File not found"}))
    assert s["state"] == "error" and s["job"]["file"] == "benchy.gcode" and s["job"]["state"] == "error"
    assert [(p["severity"], p["source"], p["message"], p["hint"]) for p in s["problems"]] == [
        ("error", "job", "File not found", None)]
    assert s["headline"] == ("Stopped with an error while printing benchy. Nozzle 25 °C, bed 23 °C. "
                             "Problem: File not found.")


def test_klipper_job_error_message_is_trimmed_and_hinted():
    s = snap(kstatus(print_stats={"state": "error", "filename": "", "message": MCU_LOST}))
    assert s["problems"][0]["message"] == "Lost communication with MCU 'mcu'"
    assert "switched off" in s["problems"][0]["hint"]
    assert s["headline"].startswith("The printer reports an error.")


def test_klipper_bed_only_heating():
    s = snap(kstatus(print_stats={"state": "printing", "filename": "benchy.gcode", "filament_used": 0.0},
                     heater_bed={"temperature": 30.0, "target": 60.0}))
    assert s["state"] == "heating"
    assert s["headline"] == "Heating up to print benchy. Nozzle 25 °C, bed 30/60 °C. No problems."


def test_klipper_nozzle_at_target_but_bed_still_far_is_heating_and_close_enough_is_printing():
    printing = {"state": "printing", "filename": "benchy.gcode", "filament_used": 0.0}
    nozzle = {"temperature": 215.0, "target": 215.0}
    assert snap(kstatus(print_stats=printing, extruder=nozzle,
                        heater_bed={"temperature": 40.0, "target": 60.0}))["state"] == "heating"
    assert snap(kstatus(print_stats=printing, extruder=nozzle,
                        heater_bed={"temperature": 58.0, "target": 60.0}))["state"] == "printing"


# --- odd strings from a printer ---------------------------------------------------------------

def test_numbers_sent_as_strings_are_read():
    s = snap(kstatus(print_stats={"state": "printing", "filename": "a.gcode", "print_duration": "1200",
                                  "filament_used": "300"},
                     display_status={"progress": "0.42"}),
             meta={"estimated_time": "2280"})
    assert s["job"]["progress_percent"] == 42 and s["job"]["elapsed_s"] == 1200.0
    assert s["job"]["remaining_s"] == 1080 and s["job"]["remaining_basis"] == "slicer_estimate"


def test_text_where_a_number_belongs_counts_as_missing():
    s = snap(kstatus(print_stats={"state": "printing", "filename": "a.gcode", "print_duration": "soon",
                                  "filament_used": "lots"},
                     display_status={"progress": "half"}, virtual_sdcard={"progress": "half"}),
             meta={"estimated_time": "long"})
    assert s["job"]["progress_percent"] is None and s["job"]["elapsed_s"] is None
    assert s["job"]["remaining_s"] is None and s["job"]["filament_used_mm"] is None
    assert s["headline"].startswith("Printing a.") and "None" not in s["headline"]


@pytest.mark.parametrize("args,want", [
    (("1200", "0.42", "2280"), (1080, "slicer_estimate")),
    (("1200", "0.5", None), (1200, "progress")),
    (("1200", "0.5", "abc"), (1200, "progress")),
    (("abc", "0.5", "abc"), (None, None)),
    ((None, None, None), (None, None)),
    (("1200", "half", None), (None, None)),
    ((float("nan"), float("inf"), float("-inf")), (None, None)),
    ((3000, 0.6, 2280), (2000, "progress")),
])
def test_remaining_reads_odd_inputs(args, want):
    assert st.remaining(*args) == want


def test_octoprint_numbers_sent_as_strings():
    printer = {"state": {"text": "Printing", "flags": {"operational": True, "printing": True}},
               "temperature": {"tool0": {"actual": "215", "target": "215"}, "bed": {"actual": "60", "target": "60"}}}
    job = {"job": {"file": {"name": "a.gcode"}},
           "progress": {"completion": "42.4", "printTime": "1200", "printTimeLeft": "1080"}}
    s = st.octoprint_snapshot(printer, job, target_public=OP)
    assert s["state"] == "printing" and s["job"]["progress_percent"] == 42
    assert s["job"]["elapsed_s"] == 1200.0 and s["job"]["remaining_s"] == 1080
    odd = {"job": {"file": {"name": "a.gcode"}},
           "progress": {"completion": "half", "printTime": "soon", "printTimeLeft": "later"}}
    s = st.octoprint_snapshot(printer, odd, target_public=OP)
    assert s["job"]["progress_percent"] is None and s["job"]["elapsed_s"] is None
    assert s["job"]["remaining_s"] is None and s["job"]["remaining_basis"] is None
    assert "None" not in s["headline"]


@pytest.mark.parametrize("seconds,text", [(0, "1 min"), (29, "1 min"), (90, "2 min"), (5400, "1 h 30 min"),
                                          (7200, "2 h"), (86400, "24 h")])
def test_more_durations(seconds, text):
    assert st._dur(seconds) == text
