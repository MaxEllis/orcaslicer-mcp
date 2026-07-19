import math
from orcaslicer_mcp import placement


def _objs(size, offset, instances=1, oid=91, name="Case Bottom.stl"):
    return {"count": 1, "objects": [
        {"id": oid, "index": 0, "name": name, "size_mm": size, "instances": instances,
         "transform": {"offset": offset, "rotation": [0, 0, 0], "scale": [1, 1, 1]}}]}


BED = "0x0,300x0,300x300,0x300"  # live fork returns comma-separated
LIVE_SIZE = [249.927, 269.924, 94.0]
LIVE_OFFSET = [150.0, 150.0, 47.0]


def test_bed_parse_comma_and_semicolon():
    for s in ("0x0,300x0,300x300,0x300", "0x0;300x0;300x300;0x300"):
        out = placement.check_placement(_objs(LIVE_SIZE, LIVE_OFFSET), {"printable_area": s})
        assert out["bed"]["min"] == [0.0, 0.0]
        assert out["bed"]["max"] == [300.0, 300.0]


def test_state_a_outer_brim_only_fits():
    # brim 10 (+0.1 gap), no skirt -> ring 10.1; tight Y edge clears ~4.94mm
    cfg = {"printable_area": BED, "brim_type": "outer_only", "brim_width": "10",
           "brim_object_gap": "0.1", "skirt_loops": "0"}
    out = placement.check_placement(_objs(LIVE_SIZE, LIVE_OFFSET), cfg)
    assert out["all_fit"] is True
    assert math.isclose(out["ring_mm"], 10.1, abs_tol=1e-6)
    o = out["objects"][0]
    assert o["fits"] is True
    assert math.isclose(o["clearances"]["back"], 4.938, abs_tol=0.05)
    assert math.isclose(o["clearances"]["front"], 4.938, abs_tol=0.05)
    assert math.isclose(o["clearances"]["left"], 14.9365, abs_tol=0.05)


def test_state_b_documents_accuracy_floor():
    # brim 5 + 2 skirt loops @ dist 3, line 0.9 -> ring 9.9; predicts +5.14mm (FITS)
    # even though the real slice ERRORED. The static check cannot arbitrate ~mm margins.
    cfg = {"printable_area": BED, "brim_type": "auto_brim", "brim_width": "5",
           "brim_object_gap": "0.1", "skirt_loops": "2", "skirt_distance": "3",
           "skirt_line_width": "0.9"}
    out = placement.check_placement(_objs(LIVE_SIZE, LIVE_OFFSET), cfg)
    assert math.isclose(out["ring_mm"], 9.9, abs_tol=1e-6)
    o = out["objects"][0]
    assert o["fits"] is True  # <-- under-flags the real error; B1 warnings are the arbiter
    assert math.isclose(o["clearances"]["back"], 5.138, abs_tol=0.05)


def test_object_over_edge_flagged():
    cfg = {"printable_area": BED, "brim_type": "no_brim", "skirt_loops": "0"}
    out = placement.check_placement(_objs([250, 270, 90], [290, 150, 45]), cfg)
    o = out["objects"][0]
    assert o["fits"] is False
    assert o["overflow_mm"] > 0
    assert out["all_fit"] is False
    # object-only clearance also negative on the right
    assert o["object_only_clearances"]["right"] < 0


def test_missing_bed_degrades():
    out = placement.check_placement(_objs(LIVE_SIZE, LIVE_OFFSET), {"brim_type": "no_brim"})
    assert out["bed"] is None
    assert out["all_fit"] is None


def test_multi_instance_flagged_unsupported():
    cfg = {"printable_area": BED, "skirt_loops": "0", "brim_type": "no_brim"}
    out = placement.check_placement(_objs([20, 20, 20], [150, 150, 10], instances=3), cfg)
    o = out["objects"][0]
    assert o.get("supported") is False
    assert "note" in o
