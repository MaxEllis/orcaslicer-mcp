from orcaslicer_mcp.physics_check import run_checks, cross_section, predicted_flows

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

def test_all_seven_checks_always_present():
    names = {r.name for r in run_checks({})}
    assert names == {"flow_ceiling", "temp_vs_flow", "layer_height_ratio",
                     "line_width_ratio", "retraction_range", "cooling_sanity",
                     "first_layer_height"}
    assert all(r.status == "warn" for r in run_checks({}))

def test_zero_flow_demand_passes_temp_check():
    cfg = {"layer_height": "0.4", "line_width": "0.85", "nozzle_diameter": "0.8",
           "filament_max_volumetric_speed": "16", "filament_type": "PLA",
           "nozzle_temperature": "215", "outer_wall_speed": "0",
           "outer_wall_line_width": "0.8"}
    r = _by(run_checks(cfg), "temp_vs_flow")
    assert r.status == "pass"

def test_subbase_temp_reports_zero_not_negative():
    cfg = {"layer_height": "0.4", "line_width": "0.85", "nozzle_diameter": "0.8",
           "filament_max_volumetric_speed": "16", "filament_type": "PLA",
           "nozzle_temperature": "180", "inner_wall_speed": "45",
           "inner_wall_line_width": "0.88"}
    r = _by(run_checks(cfg), "temp_vs_flow")
    assert r.status == "fail" and "~0.0mm3/s" in r.detail

def test_line_width_ratio_bounds():
    base = {"nozzle_diameter": "0.8", "layer_height": "0.4"}
    assert _by(run_checks(base | {"line_width": "0.5"}), "line_width_ratio").status == "warn"   # 0.625x
    assert _by(run_checks(base | {"line_width": "0.4"}), "line_width_ratio").status == "fail"   # 0.5x
    assert _by(run_checks(base | {"line_width": "0.9"}), "line_width_ratio").status == "pass"   # 1.125x

def test_cooling_fan_inversion_fails():
    cfg = {"fan_min_speed": "100", "fan_max_speed": "80"}
    assert _by(run_checks(cfg), "cooling_sanity").status == "fail"

def test_first_layer_height_cap():
    cfg = {"nozzle_diameter": "0.4", "initial_layer_print_height": "0.36"}
    assert _by(run_checks(cfg), "first_layer_height").status == "fail"  # 90% > 80%

def test_percent_line_width_resolves_against_nozzle():
    cfg = {"nozzle_diameter": "0.8", "layer_height": "0.4", "line_width": "105%",
           "inner_wall_speed": "45", "filament_max_volumetric_speed": "16",
           "filament_type": "PLA", "nozzle_temperature": "215"}
    r = _by(run_checks(cfg), "flow_ceiling")
    assert r.status == "pass"  # 105% of 0.8 = 0.84mm, flow ~14.2 <= 16
    assert _by(run_checks(cfg), "line_width_ratio").status == "pass"  # 1.05x

def test_zero_auto_width_treated_as_missing():
    cfg = {"nozzle_diameter": "0.8", "layer_height": "0.4", "line_width": "0",
           "inner_wall_speed": "45", "filament_max_volumetric_speed": "16"}
    assert _by(run_checks(cfg), "flow_ceiling").status == "warn"
    assert _by(run_checks(cfg), "line_width_ratio").status == "warn"

def test_vector_value_first_element_used():
    cfg = {"fan_min_speed": "80,80", "fan_max_speed": "100,100", "slow_down_layer_time": "8"}
    assert _by(run_checks(cfg), "cooling_sanity").status == "pass"

def test_predicted_flows_per_feature():
    cfg = {
        "layer_height": "0.5", "nozzle_diameter": "0.8", "line_width": "0.85",
        "outer_wall_speed": "45", "inner_wall_speed": "50",
        "sparse_infill_speed": "50", "sparse_infill_line_width": "0.9",
    }
    flows = predicted_flows(cfg)
    # cross_section(0.85, 0.5) = (0.85 - 0.5*(1-pi/4))*0.5 = 0.371 mm^2 -> 45*0.371 = 16.7
    assert abs(flows["outer_wall"] - 16.7) < 0.1
    assert abs(flows["inner_wall"] - 18.6) < 0.2      # 50 * 0.371
    assert abs(flows["sparse_infill"] - 19.8) < 0.2   # 50 * cross_section(0.9,0.5)=0.396
    assert "bridge" not in flows                        # no bridge_speed given

def test_predicted_flows_skips_unparseable():
    cfg = {"layer_height": "0.5", "nozzle_diameter": "0.8", "outer_wall_speed": ""}
    assert predicted_flows(cfg) == {}
