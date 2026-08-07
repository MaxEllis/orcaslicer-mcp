"""Pure-logic tests for compare.py — no live API. See spec 2026-08-07-compare-slices-design.md."""
from orcaslicer_mcp import compare


def _r(name, t=None, g=None, warnings=None, valid=True, changes=None, error=None, roles=None):
    return {"name": name, "changes": changes or {}, "time_s": t, "filament_g": g,
            "warnings": warnings or [], "valid": valid, "error": error, "roles": roles}


# --- formatting / precision -------------------------------------------------

def test_fmt_time_drops_seconds_under_hour():
    assert compare._fmt_time(2700) == "45m"        # exactly 45 min


def test_fmt_time_rounds_to_whole_minute():
    assert compare._fmt_time(2701) == "45m"        # 45m01s -> seconds dropped
    assert compare._fmt_time(2730) == "46m"        # 45m30s -> nearest minute


def test_fmt_time_hours_zero_pad_minutes():
    assert compare._fmt_time(3660) == "1h 01m"
    assert compare._fmt_time(24540) == "6h 49m"


def test_pct_signed_whole_numbers():
    assert compare._pct(57.0, 48.0) == (19, "+19%")
    assert compare._pct(41.0, 48.0) == (-15, "-15%")
    assert compare._pct(48.0, 48.0) == (0, "0%")


# --- dominance / recommendation --------------------------------------------

def test_single_dominant_variant():
    # A is faster, lighter, and warning-free vs B -> A dominates
    res = [_r("A", t=6000, g=40.0), _r("B", t=8000, g=50.0)]
    out = compare.compute_comparison(res, baseline=None, detail=False)
    assert out["recommended"] == "A"
    assert out["recommended_is_dominant"] is True
    assert out["tradeoff"] is None


def test_tradeoff_no_dominant_uses_house_rule():
    # A fastest but heaviest; B lightest but slowest. Neither dominates.
    res = [_r("A", t=5000, g=57.0), _r("B", t=8000, g=41.0)]
    out = compare.compute_comparison(res, baseline=None, detail=False)
    assert out["recommended_is_dominant"] is False
    # house rule = fastest with no warnings -> A (both warning-free, A faster)
    assert out["recommended"] == "A"
    tags = out["tradeoff"]
    assert tags["fastest"] == "A" and tags["lightest"] == "B"


def test_house_rule_skips_variant_with_warnings():
    # Fastest variant has a warning -> recommended falls to fastest warning-free
    res = [_r("A", t=4000, g=50.0, warnings=["thin wall"]), _r("B", t=6000, g=52.0)]
    out = compare.compute_comparison(res, baseline=None, detail=False)
    assert out["recommended"] == "B"
    assert out["recommended_is_dominant"] is False


def test_all_warnings_falls_back_to_fastest():
    res = [_r("A", t=4000, g=50.0, warnings=["w"]), _r("B", t=6000, g=52.0, warnings=["w"])]
    out = compare.compute_comparison(res, baseline=None, detail=False)
    assert out["recommended"] == "A"  # fastest, since none are warning-free


# --- baseline + deltas ------------------------------------------------------

def test_baseline_defaults_to_empty_changeset():
    res = [_r("fast", t=5000, g=57.0, changes={"layer_height": "0.6"}),
           _r("current", t=6000, g=48.0, changes={})]
    out = compare.compute_comparison(res, baseline=None, detail=False)
    assert out["baseline"] == "current"
    row = {v["name"]: v for v in out["variants"]}
    assert row["current"]["delta_vs_baseline"]["time_formatted"] == "baseline"
    assert row["fast"]["delta_vs_baseline"]["time_pct"] < 0  # faster than baseline


def test_baseline_defaults_to_first_when_no_empty():
    res = [_r("a", t=6000, g=48.0, changes={"x": "1"}), _r("b", t=5000, g=50.0, changes={"x": "2"})]
    out = compare.compute_comparison(res, baseline=None, detail=False)
    assert out["baseline"] == "a"


def test_variants_keep_input_order():
    res = [_r("0.3", t=8000, g=41.0), _r("0.4", t=6000, g=48.0), _r("0.5", t=5000, g=53.0)]
    out = compare.compute_comparison(res, baseline="0.4", detail=False)
    assert [v["name"] for v in out["variants"]] == ["0.3", "0.4", "0.5"]


# --- rendering --------------------------------------------------------------

def test_headline_names_recommended_and_reason():
    res = [_r("A", t=6000, g=40.0), _r("B", t=8000, g=50.0)]
    out = compare.compute_comparison(res, baseline=None, detail=False)
    assert "A" in out["headline"]
    assert out["recommendation_reason"]


def test_table_marks_recommended_and_warning():
    res = [_r("A", t=6000, g=40.0), _r("B", t=8000, g=50.0, warnings=["thin wall"])]
    out = compare.compute_comparison(res, baseline="A", detail=False)
    tbl = out["table_markdown"]
    assert "★" in tbl                       # recommended row marked
    assert "A" in tbl and "B" in tbl
    assert "warning" in tbl.lower()          # warning surfaced in the table


# --- detail opt-in ----------------------------------------------------------

def test_detail_false_omits_roles():
    res = [_r("A", t=6000, g=40.0, roles=[{"role": "wall"}]), _r("B", t=8000, g=50.0)]
    out = compare.compute_comparison(res, baseline=None, detail=False)
    assert all("roles" not in v for v in out["variants"])


def test_detail_true_includes_roles():
    res = [_r("A", t=6000, g=40.0, roles=[{"role": "wall"}]), _r("B", t=8000, g=50.0, roles=[])]
    out = compare.compute_comparison(res, baseline=None, detail=True)
    row = {v["name"]: v for v in out["variants"]}
    assert row["A"]["roles"] == [{"role": "wall"}]


# --- error rows ------------------------------------------------------------

def test_errored_variant_shown_but_excluded_from_recommendation():
    res = [_r("A", t=6000, g=40.0), _r("bad", error="slice_failed", valid=False)]
    out = compare.compute_comparison(res, baseline="A", detail=False)
    names = {v["name"] for v in out["variants"]}
    assert "bad" in names                    # still reported
    assert out["recommended"] == "A"         # only sliceable variants considered
    bad = next(v for v in out["variants"] if v["name"] == "bad")
    assert bad["error"] == "slice_failed"
