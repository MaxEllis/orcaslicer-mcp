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


def test_tradeoff_recommends_nothing():
    # A fastest but heaviest; B lightest but slowest. Neither is at least as good on everything.
    res = [_r("A", t=5000, g=57.0), _r("B", t=8000, g=41.0)]
    out = compare.compute_comparison(res, baseline=None, detail=False)
    assert out["recommended"] is None and out["recommended_is_dominant"] is False
    tags = out["tradeoff"]
    assert tags["fastest"] == "A" and tags["lightest"] == "B"
    assert out["headline"] == "No clear winner: A is fastest, B is lightest."
    assert "★" not in out["table_markdown"]


def test_tradeoff_on_warnings_only():
    # A is faster and lighter but carries a warning; B is warning-free.
    res = [_r("A", t=4000, g=50.0, warnings=["thin wall"]), _r("B", t=6000, g=52.0)]
    out = compare.compute_comparison(res, baseline=None, detail=False)
    assert out["recommended"] is None
    assert out["tradeoff"]["fastest_without_warnings"] == "B"
    assert out["headline"] == ("No clear winner: A is fastest and lightest but has a warning, "
                               "B has no warnings.")


def test_tradeoff_names_the_fastest_clean_option_when_it_is_neither_extreme():
    res = [_r("fast", t=3000, g=60.0, warnings=["w"]), _r("light", t=9000, g=30.0, warnings=["w"]),
           _r("clean", t=6000, g=50.0)]
    out = compare.compute_comparison(res, baseline=None, detail=False)
    assert out["headline"] == ("No clear winner: fast is fastest, light is lightest. "
                               "clean is the fastest with no warnings.")


def test_star_when_at_least_as_good_on_everything_even_with_ties():
    # Same shown filament (3.2 g): A is faster and no worse, so it is starred.
    res = [_r("A", t=960, g=3.21), _r("B", t=1200, g=3.24)]
    out = compare.compute_comparison(res, baseline=None, detail=False)
    assert out["recommended"] == "A" and out["recommended_is_dominant"] is True
    assert "★" in out["table_markdown"]


def test_deltas_use_the_shown_rounded_values():
    # 20m 10s vs 16m 20s shows 20m vs 16m: the delta reads -4m (-20%), not -3m (-19%).
    res = [_r("base", t=1210, g=3.24, changes={}), _r("fast", t=980, g=3.16, changes={"x": "1"})]
    out = compare.compute_comparison(res, baseline=None, detail=False)
    d = {v["name"]: v for v in out["variants"]}["fast"]["delta_vs_baseline"]
    assert d["time_delta"] == "-4m" and d["time_pct"] == -20
    # 3.2 g vs 3.2 g shown: no change and 0%, not "0.0 g (-2%)".
    assert d["filament_delta"] == "0.0 g" and d["filament_pct"] == 0


def test_all_warnings_but_one_variant_dominates():
    res = [_r("A", t=4000, g=50.0, warnings=["w"]), _r("B", t=6000, g=52.0, warnings=["w"])]
    out = compare.compute_comparison(res, baseline=None, detail=False)
    assert out["recommended"] == "A"  # faster and lighter, warnings alike: A dominates
    assert out["recommended_is_dominant"] is True


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


def test_signed_mass_avoids_negative_zero():
    assert compare._signed_mass(-0.04) == "0.0 g"
    assert compare._signed_mass(0.0) == "0.0 g"
    assert compare._signed_mass(5.4) == "+5.4 g"
    assert compare._signed_mass(-7.0) == "-7.0 g"


def test_signed_time_avoids_negative_zero():
    assert compare._signed_time(-10) == "0m"
    assert compare._signed_time(-1200) == "-20m"
    assert compare._signed_time(330) == "+6m"
