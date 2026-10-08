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


def test_console_problems_keep_only_recent_errors():
    store = [{"message": "!! early", "time": 50.0}, {"message": "// info", "time": 150.0},
             *({"message": f"!! err {i}", "time": 200.0 + i} for i in range(7)),
             {"message": "!! no time"}]
    probs = pr.console_problems(store, since=100.0)
    assert [p["message"] for p in probs] == [f"err {i}" for i in range(2, 7)]
    assert all(p["source"] == "console" and p["severity"] == "error" and p["at"] for p in probs)


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
