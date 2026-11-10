"""The static read of the printer's start and end G-code (gcode_audit.py, inside check_profile_physics)."""
from orcaslicer_mcp.gcode_audit import audit
from orcaslicer_mcp.physics_check import run_checks

AREA = "0x0,235x0,235x235,0x235"
GOOD_START = "G28 ; home\\nM190 S[bed_temperature_initial_layer_single]\\nM109 S{nozzle_temperature_initial_layer[0]}\\nG90\\nG1 X5 Y5 Z0.3\\n"
GOOD_END = "G91\\nG1 Z10\\nG90\\nM104 S0\\nM140 S0\\nM84\\n"


def test_a_good_pair_passes():
    assert audit({"machine_start_gcode": GOOD_START, "machine_end_gcode": GOOD_END, "printable_area": AREA}) == ("pass", [])


def test_macros_are_trusted_to_home_and_cool_down():
    cfg = {"machine_start_gcode": "PRINT_START BED=[bed_temperature_initial_layer_single] EXTRUDER=[nozzle_temperature_initial_layer]",
           "machine_end_gcode": "print_end", "printable_area": AREA}
    assert audit(cfg) == ("pass", [])


def test_a_fixed_temperature_is_named():
    status, f = audit({"machine_start_gcode": "G28\\nM109 S215\\nM190 S60", "machine_end_gcode": GOOD_END})
    assert status == "warn"
    assert any("fixed nozzle temperature (M109 S215)" in x for x in f)
    assert any("fixed bed temperature (M190 S60)" in x for x in f)


def test_relative_positioning_left_on():
    status, f = audit({"machine_start_gcode": "G28\\nG91\\nG1 Z5", "machine_end_gcode": GOOD_END})
    assert status == "warn" and any("G91" in x and "never back" in x for x in f)


def test_no_homing_and_no_heaters_off():
    status, f = audit({"machine_start_gcode": "G1 Z5", "machine_end_gcode": "G1 Z10\\nM84"})
    assert any("no homing" in x for x in f) and any("turns no heater off" in x for x in f)


def test_a_purge_line_outside_the_area_is_one_finding_with_the_farthest_point():
    start = "G28\\nG1 X-2 Y20\\nG1 X-3 Y120 E10\\nG1 X-1 Y200"
    status, f = audit({"machine_start_gcode": start, "machine_end_gcode": GOOD_END, "printable_area": AREA})
    outs = [x for x in f if "outside the printable area" in x]
    assert len(outs) == 1 and "X-3" in outs[0] and "purge lines" in outs[0]


def test_comments_and_placeholders_do_not_count():
    start = "G28\\n; M109 S300 in a comment\\nM104 S[nozzle_temperature]\\n{if bed_temperature[0] > 0}M190 S{bed_temperature[0]}{endif}"
    assert audit({"machine_start_gcode": start, "machine_end_gcode": GOOD_END}) == ("pass", [])


def test_missing_scripts_are_insufficient_data():
    assert audit({})[0] == "warn"


def test_it_never_fails_inside_the_physics_checks():
    cfg = {"machine_start_gcode": "G91", "machine_end_gcode": "G1 Z1"}
    gc = [r for r in run_checks(cfg) if r.name == "gcode_scripts"][0]
    assert gc.status == "warn" and "G91" in gc.detail
