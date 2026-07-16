from orcaslicer_mcp.models import summarize_slice


def test_summarize_slice_defaults():
    r = summarize_slice({"state": "done", "percent": 100, "message": "",
                         "stats": {"estimated_time": "1m"}, "warnings": []})
    assert r["state"] == "done" and r["stats"]["estimated_time"] == "1m"


def test_summarize_slice_missing_fields():
    r = summarize_slice({"state": "idle"})
    assert r["percent"] == -1 and r["warnings"] == [] and r["stats"] is None
