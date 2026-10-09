"""Shared print-outcome store (SQLite, WAL). One row per print job.

orcaslicer-mcp OWNS this schema. klipper-mcp carries a verbatim vendored copy (its recorder service
writes finished jobs here); edit this file in orcaslicer-mcp, then re-copy it. Readers treat the
store as optional: if the DB file does not exist, recall is a silent no-op. Keep this module
stdlib-only so the copy has no dependency footprint.
"""
from __future__ import annotations
import hashlib, json, os, sqlite3, time
from contextlib import closing
from pathlib import Path

SCHEMA_VERSION = 2
# The original shared store. Still used whenever it exists, so nothing moves on a machine that
# already has one (klipper-mcp's sandboxed recorder may only write there).
LEGACY_DIR = Path.home() / "projects" / "_shared" / "print-outcomes"
# Everyone else's store, created on first write.
DEFAULT_DIR = Path.home() / ".orcaslicer-mcp" / "outcomes"
# A job may join a slice row saved up to this long after the job started: the slicer machine and
# the printer do not share a clock.
JOIN_CLOCK_SKEW_S = 120

# Settings worth surfacing when recalling a past print (Orca config keys).
SUMMARY_KEYS = ["layer_height", "nozzle_temperature", "nozzle_temperature_initial_layer",
                "hot_plate_temp", "textured_plate_temp", "fan_max_speed", "fan_min_speed",
                "sparse_infill_density", "wall_loops", "enable_support", "filament_type",
                "outer_wall_speed", "default_acceleration",
                # Moonraker-metadata names (for prints sliced outside the agent)
                "first_layer_extr_temp", "first_layer_bed_temp", "nozzle_diameter", "filament_name"]

_RESULT = {"completed": "success", "cancelled": "cancelled"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  printer_id TEXT NOT NULL DEFAULT 'unknown',
  sliced_at REAL,
  model_name TEXT,
  geometry_hash TEXT,
  gcode_filename TEXT NOT NULL,
  settings_json TEXT,
  printed_at REAL,
  job_id TEXT UNIQUE,
  result TEXT,
  duration_s REAL,
  filament_g REAL,
  human_verdict TEXT
);
CREATE INDEX IF NOT EXISTS prints_gcode ON prints(gcode_filename);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1');
"""

# Columns added in schema v2. Additive only, so an older reader keeps working on a migrated file.
_V2_COLUMNS = (("failure_reason", "TEXT"), ("est_time_s", "REAL"), ("est_filament_g", "REAL"))
_V2_KEYS = tuple(name for name, _ in _V2_COLUMNS)


def store_dir() -> Path:
    env = os.environ.get("PRINT_OUTCOMES_DIR", "").strip()
    if env:
        return Path(env)
    if LEGACY_DIR.exists():
        return LEGACY_DIR
    return DEFAULT_DIR


def db_path() -> Path:
    return store_dir() / "outcomes.db"


def is_available() -> bool:
    return db_path().exists()


def connect(create: bool = False) -> sqlite3.Connection:
    p = db_path()
    if create:
        p.parent.mkdir(parents=True, exist_ok=True)
    elif not p.exists():
        # Absent store = silent no-op for every in-repo read path (all guard with
        # is_available() first). Never let sqlite3 silently create an empty,
        # table-less DB here - that would flip is_available() True forever.
        raise FileNotFoundError(str(p))
    conn = sqlite3.connect(p, timeout=5.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        if create:
            conn.executescript(_SCHEMA)
            _migrate(conn)
    except BaseException:
        conn.close()  # callers only close a connection they were handed
        raise
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a v1 store to v2: add the v2 columns and stamp the version. Only writers call this."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(prints)")}
    if all(name in have for name in _V2_KEYS):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        have = {r["name"] for r in conn.execute("PRAGMA table_info(prints)")}  # re-read under the write lock
        for name, typ in _V2_COLUMNS:
            if name not in have:
                conn.execute(f"ALTER TABLE prints ADD COLUMN {name} {typ}")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _num(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def result_for_status(status: str | None) -> str:
    return _RESULT.get(status or "", "error")


def settings_summary(settings: dict | None) -> dict:
    return {k: settings[k] for k in SUMMARY_KEYS if settings and k in settings}


def geometry_hash_for(objects: list[dict]) -> str:
    parts = []
    for o in objects or []:
        size = o.get("size_mm") or []
        parts.append(f"{o.get('name','')}:" + ",".join(f"{float(v):.1f}" for v in size))
    return hashlib.sha1("|".join(sorted(parts)).encode()).hexdigest()[:16]


def record_slice(gcode_filename: str, model_name: str, geometry_hash: str, settings: dict,
                 printer_id: str = "unknown", sliced_at: float | None = None,
                 est_time_s: float | None = None, est_filament_g: float | None = None) -> int:
    with closing(connect(create=True)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO prints(printer_id, sliced_at, model_name, geometry_hash, gcode_filename, settings_json,"
                " est_time_s, est_filament_g) VALUES (?,?,?,?,?,?,?,?)",
                (printer_id, time.time() if sliced_at is None else sliced_at, model_name, geometry_hash,
                 gcode_filename, json.dumps(settings, sort_keys=True), _num(est_time_s), _num(est_filament_g)))
            conn.commit()
            return int(cur.lastrowid)
        except Exception:
            conn.rollback()
            raise


def _fill_gaps(conn: sqlite3.Connection, row_id: int, failure_reason: str | None,
               est_time_s: float | None, est_filament_g: float | None) -> None:
    """Fill NULL columns only; a value already recorded is never overwritten."""
    conn.execute(
        "UPDATE prints SET failure_reason=COALESCE(failure_reason, ?), est_time_s=COALESCE(est_time_s, ?),"
        " est_filament_g=COALESCE(est_filament_g, ?) WHERE id=?",
        (failure_reason, est_time_s, est_filament_g, row_id))


def record_outcome(job: dict, printer_id: str = "unknown", failure_reason: str | None = None) -> int:
    """Record a FINISHED Moonraker history job. Idempotent on job_id. Joins to the newest
    unprinted slice row for the same filename that was saved before the job started; otherwise
    inserts a new row whose settings come from Moonraker's gcode metadata (a print sliced outside
    the agent). On a row that already exists it only fills gaps (failure_reason, estimates).

    The whole read-then-write is one BEGIN IMMEDIATE transaction so two concurrent recorders
    can't both claim the same candidate slice row (lost update)."""
    fn = job.get("filename") or ""
    job_id = job.get("job_id")
    meta = job.get("metadata") or {}
    est_t, est_f = _num(meta.get("estimated_time")), _num(meta.get("filament_weight_total"))
    started = _num(job.get("start_time"))
    if started is None:
        started = _num(job.get("end_time"))
    vals = (job.get("end_time"), job_id, result_for_status(job.get("status")),
            job.get("total_duration"), meta.get("filament_weight_total"))
    with closing(connect(create=True)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if job_id:
                hit = conn.execute("SELECT id FROM prints WHERE job_id=?", (job_id,)).fetchone()
                if hit:
                    _fill_gaps(conn, hit["id"], failure_reason, est_t, est_f)
                    conn.commit()
                    return int(hit["id"])
            sql = "SELECT id FROM prints WHERE gcode_filename=? AND printed_at IS NULL AND job_id IS NULL"
            args: list = [fn]
            if started is not None:
                sql += " AND (sliced_at IS NULL OR sliced_at <= ?)"
                args.append(started + JOIN_CLOCK_SKEW_S)
            row = conn.execute(sql + " ORDER BY sliced_at DESC, id DESC LIMIT 1", args).fetchone()
            if row:
                conn.execute("UPDATE prints SET printed_at=?, job_id=?, result=?, duration_s=?, filament_g=?"
                             " WHERE id=?", (*vals, row["id"]))
                _fill_gaps(conn, row["id"], failure_reason, est_t, est_f)
                conn.commit()
                return int(row["id"])
            subset = settings_summary(meta) or None
            cur = conn.execute(
                "INSERT INTO prints(printer_id, model_name, gcode_filename, settings_json, printed_at, job_id,"
                " result, duration_s, filament_g, failure_reason, est_time_s, est_filament_g)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (printer_id, Path(fn).stem, fn, json.dumps(subset) if subset else None, *vals,
                 failure_reason, est_t, est_f))
            conn.commit()
            return int(cur.lastrowid)
        except Exception:
            conn.rollback()
            raise


def set_verdict(verdict: str, gcode_filename: str | None = None) -> dict:
    with closing(connect(create=True)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if gcode_filename:
                row = conn.execute("SELECT id FROM prints WHERE gcode_filename=? AND printed_at IS NOT NULL"
                                   " ORDER BY printed_at DESC, id DESC LIMIT 1", (gcode_filename,)).fetchone()
            else:
                row = conn.execute("SELECT id FROM prints WHERE printed_at IS NOT NULL"
                                   " ORDER BY printed_at DESC, id DESC LIMIT 1").fetchone()
            if not row:
                conn.commit()
                return {"error": "no_printed_job_found"}
            conn.execute("UPDATE prints SET human_verdict=? WHERE id=?", (verdict.strip(), row["id"]))
            r = conn.execute("SELECT * FROM prints WHERE id=?", (row["id"],)).fetchone()
            conn.commit()
            return _row(r)
        except Exception:
            conn.rollback()
            raise


def last_job_end_time() -> float:
    if not is_available():
        return 0.0
    with closing(connect()) as conn:
        v = conn.execute("SELECT MAX(printed_at) AS m FROM prints").fetchone()["m"]
    return float(v or 0.0)


def recall(model_name: str | None = None, geometry_hash: str | None = None, limit: int = 10) -> list[dict]:
    if not is_available():
        return []
    where, args = [], []
    if geometry_hash:
        where.append("geometry_hash = ?"); args.append(geometry_hash)
    if model_name:
        where.append("model_name LIKE ?"); args.append(f"%{model_name}%")
    clause = (" WHERE " + " OR ".join(where)) if where else ""
    with closing(connect()) as conn:
        rows = conn.execute(
            f"SELECT * FROM prints{clause} ORDER BY COALESCE(printed_at, sliced_at) DESC, id DESC LIMIT ?",
            (*args, limit)).fetchall()
    return [_row(r) for r in rows]


def get(row_id: int) -> dict | None:
    """One row by id, or None (also when there is no store yet)."""
    if not is_available():
        return None
    with closing(connect()) as conn:
        r = conn.execute("SELECT * FROM prints WHERE id=?", (row_id,)).fetchone()
    return _row(r) if r else None


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    raw = d.pop("settings_json")
    try:
        settings = json.loads(raw) if raw is not None else None
    except (ValueError, TypeError):
        settings = None
    d["settings_summary"] = settings_summary(settings) if isinstance(settings, dict) else {}
    for key in _V2_KEYS:
        d.setdefault(key, None)  # a v1 file read before any writer migrated it
    return d
