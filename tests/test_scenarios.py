# tests/test_scenarios.py
"""Canonical regression scenarios. The 2026-07-18 Sidewinder X2 0.8mm case."""
from orcaslicer_mcp.physics_check import run_checks

PRE_TUNE = {  # as found on the PC before optimization
    "nozzle_diameter": "0.8", "layer_height": "0.4", "line_width": "0.85",
    "inner_wall_line_width": "0.88", "inner_wall_speed": "45",
    "outer_wall_line_width": "0.8", "outer_wall_speed": "35",
    "sparse_infill_line_width": "0.9", "sparse_infill_speed": "50",
    "filament_type": "PLA", "nozzle_temperature": "205",
    "filament_max_volumetric_speed": "25", "retraction_length": "0.8",
    "fan_min_speed": "80", "fan_max_speed": "100", "slow_down_layer_time": "5",
}
POST_TUNE = PRE_TUNE | {
    "nozzle_temperature": "215", "filament_max_volumetric_speed": "16",
    "sparse_infill_speed": "45", "retraction_length": "1", "slow_down_layer_time": "8",
}

def test_pre_tune_profile_is_caught():
    fails = [r for r in run_checks(PRE_TUNE) if r.status == "fail"]
    assert any(r.name == "temp_vs_flow" for r in fails), "205C@high-flow must be blocked"

def test_post_tune_profile_is_clean():
    rs = run_checks(POST_TUNE)
    assert not [r for r in rs if r.status == "fail"], [r.detail for r in rs if r.status == "fail"]
