import datetime
import sqlite3
import threading
import time

import httpx
import pytest
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
    assert h.estimate_summary(jobs) == (
        "Your last 3 successful prints with an estimate ran 10% longer than estimated (median).")
    shorter = [{"result": "success", "print_duration_s": d, "estimate": {"time_s": 1000}} for d in (850, 900, 950)]
    assert h.estimate_summary(shorter) == (
        "Your last 3 successful prints with an estimate ran 10% shorter than estimated (median).")
    close = [{"result": "success", "print_duration_s": 990, "estimate": {"time_s": 1000}}] * 3
    assert h.estimate_summary(close) == (
        "Your last 3 successful prints with an estimate matched the estimate (median).")


def test_the_summary_does_not_name_a_slicer():
    # The estimate can come from another slicer's file metadata, so the sentence must not credit OrcaSlicer.
    for d in (1100, 1000, 900):
        jobs = [{"result": "success", "print_duration_s": d, "estimate": {"time_s": 1000}}] * 3
        assert "OrcaSlicer" not in h.estimate_summary(jobs)


# --- a job's shown failure reason is the stored one -----------------------------------------------------

def test_the_stored_failure_reason_wins_over_the_console_one():
    row = {"id": 7, "failure_reason": "stored words"}
    assert h.shape_job(JOB_ERR, "console words", row)["failure_reason"] == "stored words"


def test_the_console_reason_is_used_when_the_row_has_none():
    assert h.shape_job(JOB_ERR, "console words", {"id": 7, "failure_reason": None})["failure_reason"] == "console words"
    assert h.shape_job(JOB_ERR, "console words", None)["failure_reason"] == "console words"
    assert h.shape_job(JOB_ERR, None, {"id": 7, "failure_reason": None})["failure_reason"] is None


# --- ended_at carries the timezone offset ----------------------------------------------------------------

def test_ended_at_names_its_utc_offset():
    shaped = h.shape_job(JOB_OK, None, None)
    parsed = datetime.datetime.fromisoformat(shaped["ended_at"])
    assert parsed.utcoffset() is not None  # a zone-less time cannot be told apart from another zone's
    assert parsed == datetime.datetime.fromtimestamp(JOB_OK["end_time"]).astimezone().replace(second=0, microsecond=0)


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs POSIX tzset")
def test_ended_at_shows_the_local_offset_in_the_text(monkeypatch):
    try:
        with monkeypatch.context() as m:
            m.setenv("TZ", "TST-12")  # a fixed UTC+12 zone, so the expected text does not depend on tz data
            time.tzset()
            # 8600 s after the epoch is 02:23 UTC, which is 14:23 at UTC+12
            assert h.shape_job(JOB_OK, None, None)["ended_at"] == "1970-01-01T14:23+12:00"
    finally:
        time.tzset()  # back to the real zone once the variable is restored


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


@respx.mock
async def test_history_shows_the_reason_already_stored_and_the_row_agrees(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    rid = oc.record_outcome(JOB_ERR, "test-printer", failure_reason="Reason stored by an earlier call")
    history_routes([JOB_ERR])  # the console now holds a different line for the same job
    out = await srv.list_print_history(5)
    assert out["jobs"][0]["failure_reason"] == "Reason stored by an earlier call"
    assert oc.get(rid)["failure_reason"] == out["jobs"][0]["failure_reason"]


@respx.mock
async def test_history_reports_the_console_reason_when_the_store_is_locked(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    history_routes([JOB_ERR])

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(h._outcomes, "record_outcome", locked)
    out = await srv.list_print_history(5)
    assert out["jobs"][0]["failure_reason"] == REASON  # no row to prefer, so the console's words stand


# --- the sync order and where the writes run ------------------------------------------------------------

@respx.mock
async def test_sync_walks_oldest_first_so_the_earliest_reprint_claims_the_slice(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    slice_id = oc.record_slice("bracket.gcode", "bracket", "hash-one", {"layer_height": "0.2"},
                               printer_id="test-printer", sliced_at=1000.0, est_time_s=3000.0)
    older = {**JOB_OK, "job_id": "000101", "start_time": 5000.0, "end_time": 8600.0}
    newer = {**JOB_OK, "job_id": "000105", "start_time": 20000.0, "end_time": 23600.0}
    history_routes([newer, older])  # Moonraker answers newest first
    out = await srv.list_print_history(5)
    assert oc.get(slice_id)["job_id"] == "000101"  # as the live recorder would have done it
    # the reply keeps Moonraker's order (newest first) and each job shows the row it was synced to
    assert [j["job_id"] for j in out["jobs"]] == ["000105", "000101"]
    assert out["jobs"][0]["slice"] is None and out["jobs"][1]["slice"]["model_name"] == "bracket"
    assert out["jobs"][1]["outcome_row_id"] == slice_id


@respx.mock
async def test_store_writes_run_off_the_event_loop(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    history_routes([JOB_ERR, JOB_OK])
    loop_thread = threading.get_ident()
    seen = []
    real_record, real_get = oc.record_outcome, oc.get

    def record(*args, **kwargs):
        seen.append(("record_outcome", threading.get_ident()))
        return real_record(*args, **kwargs)

    def get(*args, **kwargs):
        seen.append(("get", threading.get_ident()))
        return real_get(*args, **kwargs)

    monkeypatch.setattr(h._outcomes, "record_outcome", record)
    monkeypatch.setattr(h._outcomes, "get", get)
    out = await srv.list_print_history(5)
    assert out["synced_to_store"] == 2
    assert [name for name, _ in seen].count("record_outcome") == 2 and "get" in {name for name, _ in seen}
    # no lock involved: a blocking sqlite call on the loop's own thread is what this would catch
    assert all(thread != loop_thread for _, thread in seen)


@respx.mock
async def test_a_missing_history_component_says_how_to_enable_it(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json=INFO))
    respx.get(url__startswith=f"{P}/server/history/list").mock(return_value=httpx.Response(404))
    out = await srv.list_print_history(5)
    assert out["error"] == "protocol_error"
    assert out["hint"] == ("Moonraker versions before 0.9 need a [history] section in moonraker.conf; "
                           "add one and restart Moonraker.")
