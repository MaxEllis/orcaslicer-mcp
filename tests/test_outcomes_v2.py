import sqlite3
from contextlib import closing

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
