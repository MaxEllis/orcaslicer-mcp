import pytest

from orcaslicer_mcp.printer import problems as pr

MCU_LOST = ("Lost communication with MCU 'mcu'\nOnce the underlying issue is corrected, use the\n"
            "\"FIRMWARE_RESTART\" command to reset the firmware, reload the\n"
            "config, and restart the host software.\nPrinter is shutdown\n")


def test_trim_drops_klipper_boilerplate():
    assert pr.trim_klipper_message(MCU_LOST) == "Lost communication with MCU 'mcu'"


def test_trim_keeps_lines_before_the_boilerplate():
    msg = ("MCU 'mcu' shutdown: Timer too close\nThis often indicates the host computer is overloaded.\n"
           "Once the underlying issue is corrected, use the")
    assert pr.trim_klipper_message(msg) == ("MCU 'mcu' shutdown: Timer too close "
                                            "This often indicates the host computer is overloaded.")


@pytest.mark.parametrize("msg,needle", [
    ("Lost communication with MCU 'mcu'", "switched off"),
    ("Must home axis first: 1.000 2.000 3.000 [0.000]", "G28"),
    ("Heater extruder not heating at expected rate", "heater"),
    ("Move out of range: 400.000 1.000 0.300 [0.000]", "check_printer_match"),
    ("MCU 'mcu' shutdown: Timer too close", "overloaded"),
    ("Requested temperature (310.0) out of range (0.0:280.0)", "check_printer_match"),
    ("ADC out of range", "sensor"),
])
def test_hints(msg, needle):
    assert needle in pr.hint_for(msg)


def test_unknown_messages_get_no_hint():
    assert pr.hint_for("Something nobody has seen before") is None


@pytest.mark.parametrize("msg", [
    "Option 'max_temp' in section 'extruder' must be specified",
    "Option 'max_temp' in section 'extruder' must have minimum of 0.0",
    "Unknown config object 'heater_bed'",
])
def test_config_style_messages_get_no_hint(msg):
    """Hint needles must stay specific: a message about a config option is not a heater or move fault."""
    assert pr.hint_for(msg) is None
    assert pr.problem("error", "console", msg)["hint"] is None


def test_no_hint_uses_an_em_dash():
    assert all("—" not in hint for _, hint in pr.HINTS)


def test_klippy_problem():
    assert pr.klippy_problem({"state": "ready"}) is None
    p = pr.klippy_problem({"state": "shutdown", "state_message": MCU_LOST})
    assert (p["severity"], p["source"], p["message"]) == ("fatal", "klipper", "Lost communication with MCU 'mcu'")
    assert "switched off" in p["hint"]
    assert pr.klippy_problem({"state": "error", "state_message": ""})["message"] == "Klipper is in the error state."


def test_job_problem():
    assert pr.job_problem({"state": "printing"}) is None
    p = pr.job_problem({"state": "error", "message": "File not found"})
    assert (p["severity"], p["source"], p["message"]) == ("error", "job", "File not found")


def test_job_problem_trims_klipper_boilerplate():
    p = pr.job_problem({"state": "error", "message": MCU_LOST})
    assert p["message"] == "Lost communication with MCU 'mcu'"
    assert "switched off" in p["hint"]


@pytest.mark.parametrize("print_stats", [
    {"state": "error"},
    {"state": "error", "message": None},
    {"state": "error", "message": ""},
    {"state": "error", "message": "  \n "},
    {"state": "error", "message": "Printer is shutdown\n"},  # nothing left once the boilerplate is gone
])
def test_job_problem_falls_back_when_the_printer_gave_no_reason(print_stats):
    p = pr.job_problem(print_stats)
    assert (p["severity"], p["source"], p["message"]) == ("error", "job", "The print stopped with an error.")
    assert p["hint"] is None and p["at"] is None


def test_job_problem_is_none_without_a_state():
    assert pr.job_problem({}) is None and pr.job_problem(None) is None


def test_console_problems_keep_only_recent_errors():
    store = [{"message": "!! early", "time": 50.0}, {"message": "// info", "time": 150.0},
             *({"message": f"!! err {i}", "time": 200.0 + i} for i in range(7)),
             {"message": "!! no time"}]
    probs = pr.console_problems(store, since=100.0)
    assert [p["message"] for p in probs] == [f"err {i}" for i in range(2, 7)]
    assert all(p["source"] == "console" and p["severity"] == "error" and p["at"] is not None for p in probs)


def test_console_errors_get_the_same_trimming_and_hints_as_other_klipper_messages():
    probs = pr.console_problems([{"message": "!! " + MCU_LOST, "time": 200.0}], since=100.0)
    assert [p["message"] for p in probs] == ["Lost communication with MCU 'mcu'"]
    assert "switched off" in probs[0]["hint"] and probs[0]["at"] == 200.0


def test_a_console_error_that_is_only_boilerplate_is_dropped():
    store = [{"message": "!! Printer is shutdown", "time": 200.0}, {"message": "!!", "time": 201.0},
             {"message": "!! Move out of range: 400.000 1.000 0.300 [0.000]", "time": 202.0}]
    assert [p["message"] for p in pr.console_problems(store, since=100.0)] == [
        "Move out of range: 400.000 1.000 0.300 [0.000]"]


def test_repeated_console_errors_collapse_to_the_newest_occurrence():
    store = [{"message": "!! other", "time": 120.0},
             *({"message": "!! same", "time": 200.0 + i} for i in range(6))]
    probs = pr.console_problems(store, since=100.0)
    assert [(p["message"], p["at"]) for p in probs] == [("other", 120.0), ("same", 205.0)]


def test_collapsing_keeps_the_newest_occurrence_in_its_place():
    store = [{"message": "!! a", "time": 101.0}, {"message": "!! b", "time": 102.0},
             {"message": "!! a", "time": 103.0}]
    assert [(p["message"], p["at"]) for p in pr.console_problems(store, since=100.0)] == [("b", 102.0), ("a", 103.0)]


def test_messages_that_trim_to_the_same_text_collapse():
    store = [{"message": "!! Move out of range\nPrinter is shutdown", "time": 101.0},
             {"message": "!! Move out of range", "time": 102.0}]
    assert [(p["message"], p["at"]) for p in pr.console_problems(store, since=100.0)] == [("Move out of range", 102.0)]


def test_the_newest_five_are_taken_after_collapsing():
    """Four different errors, then one of them four more times: the window holds five different
    messages, not the same one four times."""
    store = [*({"message": f"!! err {i}", "time": 120.0 + i} for i in range(4)),
             *({"message": "!! err 0", "time": 200.0 + i} for i in range(4))]
    probs = pr.console_problems(store, since=100.0)
    assert [p["message"] for p in probs] == ["err 1", "err 2", "err 3", "err 0"]
    assert probs[-1]["at"] == 203.0


def test_a_console_line_exactly_at_since_counts():
    store = [{"message": "!! just before", "time": 99.9}, {"message": "!! on the line", "time": 100.0}]
    probs = pr.console_problems(store, since=100.0)
    assert [(p["message"], p["at"]) for p in probs] == [("on the line", 100.0)]


def test_warnings_are_grouped():
    info = {"warnings": [f"Moonraker not authorized for PolicyKit action: [org.example.{i}]" for i in range(8)]
                        + ["PolKit warnings detected. See the docs.", "Some other warning"],
            "failed_components": ["test_component"]}
    probs = pr.moonraker_warnings(info, [{"type": "deprecated_option", "message": "Option 'x' is deprecated"}])
    msgs = [p["message"] for p in probs]
    assert msgs[0] == "9 Moonraker setup warnings (system permissions), not print-related."
    assert "Some other warning" in msgs and "Option 'x' is deprecated" in msgs
    assert any("test_component" in m for m in msgs)
    assert all(p["severity"] == "warning" for p in probs)


def test_no_warnings_no_problems():
    assert pr.moonraker_warnings({}, None) == []


def test_a_single_permission_warning_is_singular():
    info = {"warnings": ["Moonraker not authorized for PolicyKit action: [org.example.a]"]}
    probs = pr.moonraker_warnings(info)
    assert [p["message"] for p in probs] == ["1 Moonraker setup warning (system permissions), not print-related."]
    assert probs[0]["severity"] == "warning" and probs[0]["source"] == "moonraker"
