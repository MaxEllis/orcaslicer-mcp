import httpx, respx
import orcaslicer_mcp.server as srv
from orcaslicer_mcp import outcomes as oc

B = "http://x:13130"
OBJS = {"count": 1, "objects": [{"id": 1, "index": 0, "name": "cube20", "size_mm": [20, 20, 20], "instances": 1,
                                 "transform": {"offset": [0, 0, 10], "rotation": [0, 0, 0], "scale": [1, 1, 1]}}]}


def _env(m, tmp_path):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", B)
    m.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))


async def test_no_store_is_silent_no_op(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path / "absent")
    out = await srv.recall_prints()
    assert out == {"available": False, "matched_by": None, "prints": [], "summary": "no outcome store"}


@respx.mock
async def test_matches_current_plate_by_geometry_then_summarises(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/objects").mock(return_value=httpx.Response(200, json=OBJS))
    h = oc.geometry_hash_for(OBJS["objects"])
    oc.record_slice("c1.gcode", "cube20", h, {"layer_height": "0.5", "fan_max_speed": "60"}, sliced_at=1.0)
    oc.record_outcome({"job_id": "1", "filename": "c1.gcode", "status": "completed", "end_time": 2.0,
                       "total_duration": 600, "metadata": {"filament_weight_total": 6.3}})
    oc.set_verdict("warped", "c1.gcode")
    oc.record_slice("c2.gcode", "cube20", h, {"layer_height": "0.3"}, sliced_at=3.0)
    oc.record_outcome({"job_id": "2", "filename": "c2.gcode", "status": "completed", "end_time": 4.0})
    oc.record_slice("other.gcode", "peg", "zzz", {}, sliced_at=5.0)
    out = await srv.recall_prints()
    assert out["available"] is True and out["matched_by"] == "geometry"
    names = [p["gcode_filename"] for p in out["prints"]]
    assert names == ["c2.gcode", "c1.gcode"]  # newest first, 'peg' excluded
    assert out["prints"][1]["human_verdict"] == "warped"
    assert out["prints"][1]["settings_summary"]["fan_max_speed"] == "60"
    assert out["summary"] == "2 past prints of cube20: 2 success (1 marked warped)"


@respx.mock
async def test_falls_back_to_name_when_slicer_offline(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/objects").mock(side_effect=httpx.ConnectError("down"))
    oc.record_slice("c1.gcode", "cube20", "h", {}, sliced_at=1.0)
    out = await srv.recall_prints(model_name="cube")
    assert out["matched_by"] == "name" and len(out["prints"]) == 1


@respx.mock
async def test_recent_when_nothing_to_match_on(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/objects").mock(return_value=httpx.Response(200, json={"count": 0, "objects": []}))
    oc.record_outcome({"job_id": "9", "filename": "x.gcode", "status": "cancelled", "end_time": 1.0})
    out = await srv.recall_prints(limit=3)
    assert out["matched_by"] == "recent" and out["prints"][0]["result"] == "cancelled"
    assert out["summary"] == "1 recent print: 1 cancelled"
