import sqlite3

import httpx
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp import outcomes as oc
from orcaslicer_mcp.printer import history as h

P = "http://192.0.2.10"
INFO = {"result": {"klippy_state": "ready", "klippy_connected": True, "warnings": []}}
JOB_OK = {"job_id": "000101", "filename": "bracket.gcode", "status": "completed", "start_time": 5000.0,
          "end_time": 8600.0, "print_duration": 3300.0, "total_duration": 3600.0, "filament_used": 5000.0,
          "metadata": {"estimated_time": 3000, "filament_weight_total": 15.0}}
JOB_ERR = {"job_id": "000102", "filename": "pads.gcode", "status": "error", "start_time": 9000.0,
           "end_time": 9127.0, "print_duration": 60.0, "total_duration": 127.0, "filament_used": 10.0,
           "metadata": {"estimated_time": 8880, "filament_weight_total": 19.5}}
JOB_RUNNING = {"job_id": "000103", "filename": "next.gcode", "status": "in_progress", "start_time": 9500.0,
               "end_time": None}
REASON = "Must home axis first: 1.000 2.000 3.000 [0.000]"
CONSOLE = [{"message": f"!! {REASON}", "time": 9120.0, "type": "response"},
           {"message": "// echo", "time": 9121.0, "type": "response"}]


def test_failure_reason_takes_the_last_console_error_inside_the_job():
    job = {"status": "error", "start_time": 1000.0, "end_time": 1100.0}
    console = [{"message": "!! before", "time": 900.0}, {"message": "!! first", "time": 1010.0},
               {"message": "!! last", "time": 1105.0}, {"message": "!! after", "time": 1200.0}]
    assert h.failure_reason(job, console) == "last"


def test_completed_jobs_and_missing_times_have_no_reason():
    line = [{"message": "!! x", "time": 5.0}]
    assert h.failure_reason({"status": "completed", "start_time": 0.0, "end_time": 9.0}, line) is None
    assert h.failure_reason({"status": "error", "start_time": None, "end_time": 9.0}, line) is None


def test_shape_job_prefers_the_slice_estimate():
    row = {"id": 7, "sliced_at": 1.0, "est_time_s": 3000.0, "est_filament_g": 14.0, "model_name": "bracket",
           "settings_summary": {"layer_height": "0.2"}, "failure_reason": None}
    j = h.shape_job(JOB_OK, None, row)
    assert j["estimate"] == {"time_s": 3000, "filament_g": 14.0, "source": "slice"}
    assert j["vs_estimate_pct"] == 10 and j["result"] == "success" and j["slice"]["model_name"] == "bracket"
    assert j["filament_used_mm"] == 5000 and j["outcome_row_id"] == 7


def test_shape_job_falls_back_to_the_file_estimate():
    j = h.shape_job(JOB_OK, None, None)
    assert j["estimate"] == {"time_s": 3000, "filament_g": 15.0, "source": "file"} and j["slice"] is None


def test_the_estimate_percentage_is_only_for_successful_jobs():
    # A cancelled 60 s run against a 3600 s estimate must not read as "98% faster than estimated".
    cancelled = {**JOB_ERR, "status": "cancelled", "print_duration": 60.0, "metadata": {"estimated_time": 3600}}
    interrupted = {**JOB_ERR, "status": "interrupted", "print_duration": 60.0}
    for job in (JOB_ERR, cancelled, interrupted):
        shaped = h.shape_job(job, None, None)
        assert shaped["result"] != "success" and shaped["vs_estimate_pct"] is None
        assert shaped["estimate"]["time_s"] is not None  # the estimate itself is still shown
    assert h.shape_job(JOB_OK, None, None)["vs_estimate_pct"] == 10


def test_estimate_summary():
    jobs = [{"result": "success", "print_duration_s": d, "estimate": {"time_s": 1000}} for d in (1050, 1100, 1200)]
    assert h.estimate_summary(jobs[:2]) is None
    assert h.estimate_summary(jobs) == "Your last 3 finished prints ran 10% longer than OrcaSlicer estimated (median)."
    close = [{"result": "success", "print_duration_s": 990, "estimate": {"time_s": 1000}}] * 3
    assert h.estimate_summary(close) == "Your last 3 finished prints matched OrcaSlicer's time estimate (median)."


def history_routes(jobs, console=CONSOLE):
    respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json=INFO))
    respx.get(url__startswith=f"{P}/server/history/list").mock(
        return_value=httpx.Response(200, json={"result": {"count": len(jobs), "jobs": jobs}}))
    respx.get(url__startswith=f"{P}/server/gcode_store").mock(
        return_value=httpx.Response(200, json={"result": {"gcode_store": console}}))


def _env(m, tmp_path):
    m.setenv("ORCA_PRINTER_URL", P)
    m.setenv("ORCA_PRINTER_ID", "test-printer")
    m.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))


@respx.mock
async def test_history_syncs_finished_jobs_and_fills_a_missing_reason(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    rid = oc.record_outcome(JOB_ERR, "test-printer")  # what a recorder stored, without a reason
    history_routes([JOB_RUNNING, JOB_ERR, JOB_OK])
    out = await srv.list_print_history(5)
    assert [j["file"] for j in out["jobs"]] == ["pads.gcode", "bracket.gcode"]
    assert out["jobs"][0]["failure_reason"] == REASON
    assert oc.get(rid)["failure_reason"] == REASON
    assert out["synced_to_store"] == 2 and "store_error" not in out
    await srv.list_print_history(5)
    assert len(oc.recall(limit=50)) == 2  # repeat calls add no rows


@respx.mock
async def test_history_survives_a_locked_store(monkeypatch, tmp_path):  # Review Focus 5
    _env(monkeypatch, tmp_path)
    history_routes([JOB_OK])

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(h._outcomes, "record_outcome", locked)
    out = await srv.list_print_history(5)
    assert out["jobs"][0]["file"] == "bracket.gcode" and out["synced_to_store"] == 0
    assert "locked" in out["store_error"]


@respx.mock
async def test_history_is_klipper_only(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    # Registration order matters (respx 0.23 matches a port-less route against any port, in order):
    # the :7125 route must come first so it answers the :7125 probe.
    respx.get(f"{P}:7125/server/info").mock(side_effect=httpx.ConnectError("refused"))
    respx.get(f"{P}/server/info").mock(return_value=httpx.Response(404))
    respx.get(f"{P}/api/version").mock(return_value=httpx.Response(200, json={"api": "0.1", "server": "1.10.2"}))
    out = await srv.list_print_history()
    assert out["supported"] is False and "OctoPrint" in out["reason"]


@respx.mock
async def test_history_names_the_printer_by_its_host_when_only_the_url_is_set(monkeypatch, tmp_path):
    # The same rule save_gcode uses, so the slice row and the job row carry one printer_id.
    monkeypatch.setenv("ORCA_PRINTER_URL", f"http://test-user:pw-secret@192.0.2.10")
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    history_routes([JOB_OK])
    out = await srv.list_print_history(5)
    assert oc.get(out["jobs"][0]["outcome_row_id"])["printer_id"] == "192.0.2.10"
