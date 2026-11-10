"""Start-to-finish time forecast from past prints against their estimates (forecast.py)."""
import json

import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp import forecast, outcomes

B = "http://x:13130"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", B)
    outcomes.connect(create=True).close()
    return tmp_path


def _add(est, took, version=None, result="success", n=1):
    for i in range(n):
        settings = {"layer_height": "0.2", **({forecast.VERSION_KEY: version} if version else {})}
        rid = outcomes.record_slice(f"p{est}-{took}-{version}-{i}.gcode", "m", "h", settings, est_time_s=est)
        conn = outcomes.connect()
        conn.execute("UPDATE prints SET result=?, duration_s=?, printed_at=? WHERE id=?",
                     (result, took, 1_000_000 + rid, rid))
        conn.close()


def test_no_store_or_too_few_prints_gives_no_forecast(tmp_path, monkeypatch):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path / "none"))
    assert forecast.time_forecast(3600) is None
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    outcomes.connect(create=True).close()
    _add(3600, 3960, n=2)
    assert forecast.time_forecast(3600) is None


def test_the_median_ratio_corrects_the_estimate(store):
    _add(3600, 3960, n=3)                  # +10 %
    _add(1800, 1800)                       # on time
    _add(3600, 3600 * 4)                   # a paused job: left out
    _add(3600, 100, result="cancelled")    # not finished: left out
    fc = forecast.time_forecast(6000)
    assert fc["based_on"] == 4 and fc["ratio"] == 1.1 and fc["expected_s"] == 6600
    assert fc["summary"].startswith("Expect about 1 h 50 min from start to finish, heat-up included")
    assert "10% longer" in fc["summary"] and fc["same_version"] is False


def test_prints_from_the_same_orcaslicer_version_are_preferred(store):
    _add(3600, 3600 * 1.3, version="2.4.2", n=5)
    _add(3600, 3600 * 1.05, version="2.5.0", n=3)
    fc = forecast.time_forecast(3600, "2.5.0")
    assert fc["same_version"] is True and fc["based_on"] == 3 and fc["ratio"] == 1.05
    assert "sliced with OrcaSlicer 2.5.0" in fc["summary"]


@respx.mock
async def test_get_slice_status_carries_the_forecast(store):
    _add(3600, 3780, version="2.5.0", n=3)
    respx.get(f"{B}/api/v1/slice/status").mock(return_value=httpx.Response(200, json={
        "state": "done", "valid": True, "stats": {"estimated_time_seconds": 3600, "filament_used_g": 12}}))
    respx.get(f"{B}/api/v1/status").mock(return_value=httpx.Response(200, json={"app_version": "2.5.0"}))
    out = await srv.get_slice_status()
    assert out["forecast"]["expected_s"] == 3780 and out["forecast"]["same_version"] is True


@respx.mock
async def test_save_gcode_records_the_orcaslicer_version(store):
    respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(200, content=b"G1 X1\n"))
    respx.get(url__regex=rf"{B}/api/v1/objects.*").mock(return_value=httpx.Response(200, json={
        "objects": [{"id": 1, "name": "cube.stl", "size_mm": [20, 20, 20]}]}))
    respx.get(f"{B}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {"layer_height": "0.2"}}))
    respx.get(f"{B}/api/v1/slice/status").mock(return_value=httpx.Response(200, json={
        "state": "done", "stats": {"estimated_time_seconds": 600}}))
    respx.get(f"{B}/api/v1/status").mock(return_value=httpx.Response(200, json={
        "app_version": "2.5.0", "presets": {"printer": "P"}}))
    out = await srv.save_gcode("cube")
    conn = outcomes.connect()
    row = conn.execute("SELECT settings_json FROM prints WHERE id=?", (out["outcome_row_id"],)).fetchone()
    conn.close()
    assert json.loads(row["settings_json"])[forecast.VERSION_KEY] == "2.5.0"
