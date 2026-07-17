import orcaslicer_mcp.server as srv


def test_describe_setting_known():
    out = srv.describe_setting("layer_height")
    assert out["key"] == "layer_height"
    assert out["type"] == "coFloat"
    assert out["tooltip"]
    assert out["unit"] == "mm"


def test_describe_setting_unknown():
    out = srv.describe_setting("no_such_setting_xyz")
    assert out["error"] == "unknown_setting"
    assert out["key"] == "no_such_setting_xyz"


def test_search_exact_key_ranks_first():
    out = srv.search_settings("sparse_infill_pattern")
    assert out["results"][0]["key"] == "sparse_infill_pattern"


def test_search_ranks_key_matches_first():
    out = srv.search_settings("infill")
    assert len(out["results"]) > 0
    # top results should be settings whose KEY contains the query, not tooltip-only
    assert "infill" in out["results"][0]["key"]


def test_search_result_shape():
    r = srv.search_settings("infill")["results"][0]
    assert set(r) == {"key", "label", "category", "tooltip"}


def test_search_respects_limit():
    out = srv.search_settings("e", limit=3)
    assert len(out["results"]) <= 3


def test_search_empty_query_returns_nothing():
    assert srv.search_settings("")["results"] == []
