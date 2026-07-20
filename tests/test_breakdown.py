from orcaslicer_mcp.breakdown import prediction_check, build_breakdown

def _role(name, flow_max):
    return {"role": name, "time_s": 100.0, "time_pct": 10.0, "filament_g": 1.0,
            "speed_mm_s": {"min": 10, "max": 45, "mean": 40},
            "flow_mm3_s": {"min": 1.0, "max": flow_max, "mean": flow_max * 0.9}}

_CFG = {
    "layer_height": "0.5", "nozzle_diameter": "0.8", "line_width": "0.85",
    "filament_max_volumetric_speed": "20",
    "outer_wall_speed": "45",          # predicted ~16.7
    "inner_wall_speed": "55",          # predicted ~20.4 (over ceiling)
}

def test_prediction_check_flags_clamped():
    # inner_wall predicted ~20.4 but observed pinned at ceiling 20 -> clamped
    bd = {"roles": [_role("inner_wall", 19.9)]}
    out = prediction_check(_CFG, bd)
    entry = next(e for e in out if e["role"] == "inner_wall")
    assert entry["verdict"] == "clamped"

def test_prediction_check_matches():
    # outer_wall predicted ~16.7, observed ~16.5 -> matches
    bd = {"roles": [_role("outer_wall", 16.5)]}
    out = prediction_check(_CFG, bd)
    assert out[0]["verdict"] == "matches"

def test_prediction_check_anomaly():
    # outer_wall predicted ~16.7 but observed 25 (well above, not clamped) -> anomaly
    bd = {"roles": [_role("outer_wall", 25.0)]}
    out = prediction_check(_CFG, bd)
    assert out[0]["verdict"] == "anomaly"

def test_prediction_check_skips_roles_without_prediction():
    # travel/support have no predicted flow -> not in output
    bd = {"roles": [_role("travel", 0.0), _role("support", 5.0)]}
    assert prediction_check(_CFG, bd) == []

def test_build_breakdown_available():
    status = {"state": "done", "breakdown": {
        "mode": "normal", "total_time_s": 26854.9,
        "roles": [_role("outer_wall", 16.5)],
        "metrics": {"speed": {"unit": "mm/s", "min": 0, "max": 300, "mean": 210, "buckets": []}},
        "layers": [{"z": 0.4, "time_s": 61.2, "filament_g": 1.1, "top_role": "internal_solid_infill"}],
    }}
    out = build_breakdown(status, _CFG)
    assert out["available"] is True
    assert out["total_time_s"] == 26854.9
    assert out["roles"][0]["role"] == "outer_wall"
    assert out["prediction_check"][0]["verdict"] == "matches"

def test_build_breakdown_unavailable_when_absent():
    out = build_breakdown({"state": "done"}, _CFG)
    assert out["available"] is False
    assert "reason" in out
