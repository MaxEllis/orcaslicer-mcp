import sqlite3
from contextlib import closing

import pytest

from orcaslicer_mcp import outcomes as oc

V1_DDL = """
CREATE TABLE prints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  printer_id TEXT NOT NULL DEFAULT 'unknown',
  sliced_at REAL, model_name TEXT, geometry_hash TEXT,
  gcode_filename TEXT NOT NULL, settings_json TEXT, printed_at REAL,
  job_id TEXT UNIQUE, result TEXT, duration_s REAL, filament_g REAL, human_verdict TEXT
);
CREATE INDEX prints_gcode ON prints(gcode_filename);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
INSERT INTO meta(key, value) VALUES ('schema_version', '1');
INSERT INTO prints(printer_id, gcode_filename, printed_at, job_id, result)
  VALUES ('test-printer', 'old.gcode', 100.0, '000001', 'success');
"""

JOB = {"job_id": "000042", "filename": "part.gcode", "status": "error", "start_time": 1000.0,
       "end_time": 1200.0, "total_duration": 200.0, "print_duration": 150.0,
       "metadata": {"estimated_time": 3600, "filament_weight_total": 12.5}}


def _make_v1(dirpath):
    dirpath.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(dirpath / "outcomes.db")) as conn:
        conn.executescript(V1_DDL)


def _columns(dirpath):
    with closing(sqlite3.connect(dirpath / "outcomes.db")) as conn:
        return {r[1] for r in conn.execute("PRAGMA table_info(prints)")}


def test_store_dir_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path / "env"))
    (tmp_path / "legacy").mkdir()
    monkeypatch.setattr(oc, "LEGACY_DIR", tmp_path / "legacy")
    assert oc.store_dir() == tmp_path / "env"


def test_store_dir_keeps_an_existing_legacy_store(monkeypatch, tmp_path):
    (tmp_path / "legacy").mkdir()
    monkeypatch.setattr(oc, "LEGACY_DIR", tmp_path / "legacy")
    monkeypatch.setattr(oc, "DEFAULT_DIR", tmp_path / "default")
    assert oc.store_dir() == tmp_path / "legacy"


def test_store_dir_falls_back_to_the_public_default(monkeypatch, tmp_path):
    monkeypatch.setattr(oc, "LEGACY_DIR", tmp_path / "absent")
    monkeypatch.setattr(oc, "DEFAULT_DIR", tmp_path / "default")
    assert oc.store_dir() == tmp_path / "default"
    oc.record_outcome(JOB, "test-printer")
    assert (tmp_path / "default" / "outcomes.db").exists()


def test_v1_store_is_migrated_on_first_write_and_keeps_old_rows(monkeypatch, tmp_path):
    _make_v1(tmp_path)
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    oc.record_outcome(JOB, "test-printer", failure_reason="Must home axis first")
    assert {"failure_reason", "est_time_s", "est_filament_g"} <= _columns(tmp_path)
    with closing(sqlite3.connect(tmp_path / "outcomes.db")) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
    rows = {r["gcode_filename"]: r for r in oc.recall(limit=10)}
    assert rows["old.gcode"]["result"] == "success" and rows["old.gcode"]["failure_reason"] is None
    assert rows["part.gcode"]["failure_reason"] == "Must home axis first"


def test_reader_tolerates_an_unmigrated_v1_store(monkeypatch, tmp_path):
    _make_v1(tmp_path)
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    rows = oc.recall(limit=10)
    assert rows[0]["gcode_filename"] == "old.gcode"
    assert rows[0]["failure_reason"] is None and rows[0]["est_time_s"] is None
    assert "failure_reason" not in _columns(tmp_path)  # readers never migrate


def test_record_outcome_fills_gaps_but_never_overwrites(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    rid = oc.record_outcome(JOB, "test-printer")
    assert oc.get(rid)["failure_reason"] is None
    assert oc.record_outcome(JOB, "test-printer", failure_reason="first") == rid
    assert oc.record_outcome(JOB, "test-printer", failure_reason="second") == rid
    assert oc.get(rid)["failure_reason"] == "first"


def test_estimates_come_from_the_file_when_no_slice_was_recorded(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    row = oc.get(oc.record_outcome(JOB, "test-printer"))
    assert row["est_time_s"] == 3600 and row["est_filament_g"] == 12.5
    assert row["printer_id"] == "test-printer"


def test_slice_estimates_win_over_the_file(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    sid = oc.record_slice("part.gcode", "part", "h1", {"layer_height": "0.2"}, printer_id="test-printer",
                          sliced_at=900.0, est_time_s=3000, est_filament_g=11.0)
    assert oc.record_outcome(JOB, "test-printer") == sid
    row = oc.get(sid)
    assert row["est_time_s"] == 3000 and row["est_filament_g"] == 11.0 and row["result"] == "error"


def test_printer_id_default_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    assert oc.get(oc.record_outcome({**JOB, "job_id": "7"}))["printer_id"] == "unknown"


def test_get_returns_none_without_a_store(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path / "absent"))
    assert oc.get(1) is None


def test_an_old_job_does_not_join_a_newer_slice(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    sid = oc.record_slice("part.gcode", "part", "h1", {"layer_height": "0.2"}, printer_id="test-printer",
                          sliced_at=5000.0)
    old_job = oc.record_outcome(JOB, "test-printer")  # started at 1000.0, long before the slice
    assert old_job != sid
    row = oc.get(sid)
    assert row["result"] is None and row["printed_at"] is None and row["job_id"] is None
    later = {**JOB, "job_id": "000043", "start_time": 6000.0, "end_time": 6200.0}
    assert oc.record_outcome(later, "test-printer") == sid


def test_whitespace_store_env_counts_as_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", "   ")
    monkeypatch.setattr(oc, "LEGACY_DIR", tmp_path / "absent")
    monkeypatch.setattr(oc, "DEFAULT_DIR", tmp_path / "default")
    assert oc.store_dir() == tmp_path / "default"


# --- the join window: a slice saved shortly after the job's start still joins ---------------------

def _slice_at(sliced_at):
    return oc.record_slice("part.gcode", "part", "h1", {"layer_height": "0.2"}, printer_id="test-printer",
                           sliced_at=sliced_at)


def test_a_slice_saved_60s_after_the_job_started_still_joins(monkeypatch, tmp_path):
    # the slicer machine and the printer do not share a clock
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    sid = _slice_at(JOB["start_time"] + 60)
    assert oc.record_outcome(JOB, "test-printer") == sid
    assert oc.get(sid)["job_id"] == "000042"


def test_a_slice_saved_200s_after_the_job_started_does_not_join(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    sid = _slice_at(JOB["start_time"] + 200)
    rid = oc.record_outcome(JOB, "test-printer")
    assert rid != sid
    row = oc.get(sid)
    assert row["job_id"] is None and row["printed_at"] is None and row["result"] is None


def test_the_join_window_edge_is_the_published_skew(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    assert oc.JOIN_CLOCK_SKEW_S == 120
    sid = _slice_at(JOB["start_time"] + oc.JOIN_CLOCK_SKEW_S)
    assert oc.record_outcome(JOB, "test-printer") == sid  # exactly at the edge still joins


def test_a_job_with_no_start_time_joins_by_its_end_time(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    no_start = {k: v for k, v in JOB.items() if k != "start_time"}  # end_time is 1200.0
    near = _slice_at(no_start["end_time"] - 30)
    assert oc.record_outcome(no_start, "test-printer") == near
    # and a slice saved well after the end does not join a second such job
    later = _slice_at(no_start["end_time"] + 500)
    other = {**no_start, "job_id": "000043"}
    rid = oc.record_outcome(other, "test-printer")
    assert rid not in (near, later)
    assert oc.get(later)["job_id"] is None


def test_a_job_with_a_null_start_time_joins_by_its_end_time(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    sid = _slice_at(5000.0)
    job = {**JOB, "start_time": None, "end_time": 6000.0}
    assert oc.record_outcome(job, "test-printer") == sid
    # a job whose end_time is long before the slice does not claim it
    sid2 = _slice_at(9000.0)
    early = {**JOB, "job_id": "000044", "start_time": None, "end_time": 4000.0}
    assert oc.record_outcome(early, "test-printer") not in (sid, sid2)
    assert oc.get(sid2)["job_id"] is None


# --- connect(create=True) must not leak the connection when setup fails ---------------------------

class _Tracker:
    """Wraps sqlite3.connect so a test can see every connection connect() opened."""
    def __init__(self, monkeypatch):
        self.conns = []
        real = sqlite3.connect

        def tracked(*a, **kw):
            c = real(*a, **kw)
            self.conns.append(c)
            return c
        monkeypatch.setattr(oc.sqlite3, "connect", tracked)

    def all_closed(self):
        for c in self.conns:
            try:
                c.cursor()  # runs no SQL; a closed connection refuses it
            except sqlite3.ProgrammingError:
                continue  # "Cannot operate on a closed database."
            return False
        return bool(self.conns)


def test_connect_closes_the_connection_when_schema_setup_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    monkeypatch.setattr(oc, "_SCHEMA", "THIS IS NOT SQL;")
    seen = _Tracker(monkeypatch)
    with pytest.raises(sqlite3.Error):
        oc.connect(create=True)
    assert seen.all_closed()


def test_connect_closes_the_connection_when_migration_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))

    def boom(conn):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(oc, "_migrate", boom)
    seen = _Tracker(monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        oc.connect(create=True)
    assert seen.all_closed()


def test_connect_closes_the_connection_when_the_file_is_not_a_database(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    (tmp_path / "outcomes.db").write_bytes(b"this is not a sqlite file at all" * 20)
    seen = _Tracker(monkeypatch)
    with pytest.raises(sqlite3.DatabaseError):
        oc.connect(create=True)
    assert seen.all_closed()
    # a reader hitting the same file must not leak either
    seen2 = _Tracker(monkeypatch)
    with pytest.raises(sqlite3.DatabaseError):
        oc.connect()
    assert seen2.all_closed()


def test_a_failed_record_does_not_leave_a_connection_behind(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))

    def boom(conn):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(oc, "_migrate", boom)
    seen = _Tracker(monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        oc.record_outcome(JOB, "test-printer")
    assert seen.all_closed()


# --- a row deleted by hand: what comes back (the README says exactly this) ------------------------

def _delete_row(dirpath, row_id):
    with closing(sqlite3.connect(dirpath / "outcomes.db")) as conn:
        conn.execute("DELETE FROM prints WHERE id=?", (row_id,))
        conn.commit()


def test_a_finished_print_row_deleted_by_hand_comes_back_without_its_slice_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    sid = oc.record_slice("part.gcode", "part", "h1", {"layer_height": "0.2", "wall_loops": "3"},
                          printer_id="test-printer", sliced_at=900.0)
    job = {**JOB, "metadata": {**JOB["metadata"], "layer_height": 0.3}}
    assert oc.record_outcome(job, "test-printer") == sid  # the finished job joined the saved slice
    assert oc.get(sid)["settings_summary"] == {"layer_height": "0.2", "wall_loops": "3"}
    _delete_row(tmp_path, sid)
    assert oc.get(sid) is None
    again = oc.record_outcome(job, "test-printer")  # the next sync of the job still in the printer's history
    row = oc.get(again)
    assert row["job_id"] == "000042" and row["result"] == "error"
    assert row["sliced_at"] is None  # no longer tied to the slice
    assert row["settings_summary"] == {"layer_height": 0.3}  # only what the printer's file metadata carries


def test_a_slice_row_with_no_job_stays_deleted(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    sid = oc.record_slice("part.gcode", "part", "h1", {"layer_height": "0.2"}, printer_id="test-printer",
                          sliced_at=900.0)
    _delete_row(tmp_path, sid)
    oc.record_outcome({**JOB, "filename": "other.gcode"}, "test-printer")  # a sync of some other job
    assert oc.get(sid) is None
    assert [r["gcode_filename"] for r in oc.recall(limit=10)] == ["other.gcode"]
