from orcaslicer_mcp.physics_check import run_checks, cross_section

BASE = {
    "nozzle_diameter": "0.8", "layer_height": "0.4", "line_width": "0.85",
    "outer_wall_line_width": "0.8", "inner_wall_line_width": "0.88",
    "outer_wall_speed": "35", "inner_wall_speed": "45",
    "filament_max_volumetric_speed": "16", "filament_type": "PLA",
    "nozzle_temperature": "215", "retraction_length": "1",
    "fan_min_speed": "80", "fan_max_speed": "100",
    "slow_down_layer_time": "8", "initial_layer_print_height": "0.4",
}

def _by(results, name):
    return next(r for r in results if r.name == name)

def test_cross_section_matches_rounded_rectangle_model():
    assert abs(cross_section(0.85, 0.4) - 0.30567) < 1e-4

def test_tuned_sidewinder_profile_passes():
    rs = run_checks(BASE)
    assert all(r.status != "fail" for r in rs), [r.detail for r in rs if r.status == "fail"]

def test_cold_pla_at_high_flow_fails_temp_adequacy():
    cfg = BASE | {"nozzle_temperature": "205"}
    r = _by(run_checks(cfg), "temp_vs_flow")
    assert r.status == "fail" and "8.3" in r.detail  # (205-195)/1.2 sustainable

def test_flow_ceiling_violation_fails():
    cfg = BASE | {"inner_wall_speed": "80"}  # 80*0.3169=25.4 > 16
    r = _by(run_checks(cfg), "flow_ceiling")
    assert r.status == "fail" and "inner_wall" in r.detail

def test_layer_height_over_80pct_of_nozzle_fails():
    cfg = BASE | {"layer_height": "0.7"}
    assert _by(run_checks(cfg), "layer_height_ratio").status == "fail"

def test_long_retraction_warns():
    cfg = BASE | {"retraction_length": "4"}
    assert _by(run_checks(cfg), "retraction_range").status == "warn"

def test_missing_data_warns_not_raises():
    r = _by(run_checks({}), "flow_ceiling")
    assert r.status == "warn" and "insufficient" in r.detail
