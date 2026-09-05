# VENDORED from klipper-mcp (src/klipper_mcp/outcomes.py). klipper-mcp owns the schema; do not edit here, re-copy.
"""Shared print-outcome store (SQLite, WAL). One row per print job.

klipper-mcp OWNS this schema. orcaslicer-mcp carries a verbatim vendored copy and treats the
store as optional: if the DB file does not exist, its advisor is a silent no-op. Keep this
module stdlib-only so the copy has no dependency footprint.
"""
from __future__ import annotations
import hashlib, json, os, sqlite3, time
from contextlib import closing
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_DIR = Path.home() / "projects" / "_shared" / "print-outcomes"

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
  printer_id TEXT NOT NULL DEFAULT 'swx2',
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


def store_dir() -> Path:
    return Path(os.environ.get("PRINT_OUTCOMES_DIR") or DEFAULT_DIR)


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
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    if create:
        conn.executescript(_SCHEMA)
    return conn


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
                 printer_id: str = "swx2", sliced_at: float | None = None) -> int:
    with closing(connect(create=True)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO prints(printer_id, sliced_at, model_name, geometry_hash, gcode_filename, settings_json)"
                " VALUES (?,?,?,?,?,?)",
                (printer_id, time.time() if sliced_at is None else sliced_at, model_name, geometry_hash,
                 gcode_filename, json.dumps(settings, sort_keys=True)))
            conn.commit()
            return int(cur.lastrowid)
        except Exception:
            conn.rollback()
            raise


def record_outcome(job: dict, printer_id: str = "swx2") -> int:
    """Record a FINISHED Moonraker history job. Idempotent on job_id. Joins to the newest
    slice row for the same filename that has not been printed yet; otherwise inserts a new row
    whose settings come from Moonraker's gcode metadata (a print sliced outside the agent).

    The whole read-then-write is one BEGIN IMMEDIATE transaction so two concurrent recorders
    can't both claim the same candidate slice row (lost update)."""
    fn = job.get("filename") or ""
    job_id = job.get("job_id")
    meta = job.get("metadata") or {}
    vals = (job.get("end_time"), job_id, result_for_status(job.get("status")),
            job.get("total_duration"), meta.get("filament_weight_total"))
    with closing(connect(create=True)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if job_id:
                hit = conn.execute("SELECT id FROM prints WHERE job_id=?", (job_id,)).fetchone()
                if hit:
                    conn.commit()
                    return int(hit["id"])
            row = conn.execute(
                "SELECT id FROM prints WHERE gcode_filename=? AND printed_at IS NULL AND job_id IS NULL"
                " ORDER BY sliced_at DESC, id DESC LIMIT 1", (fn,)).fetchone()
            if row:
                conn.execute("UPDATE prints SET printed_at=?, job_id=?, result=?, duration_s=?, filament_g=?"
                             " WHERE id=?", (*vals, row["id"]))
                conn.commit()
                return int(row["id"])
            subset = settings_summary(meta) or None
            cur = conn.execute(
                "INSERT INTO prints(printer_id, model_name, gcode_filename, settings_json, printed_at, job_id,"
                " result, duration_s, filament_g) VALUES (?,?,?,?,?,?,?,?,?)",
                (printer_id, Path(fn).stem, fn, json.dumps(subset) if subset else None, *vals))
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


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    raw = d.pop("settings_json")
    try:
        settings = json.loads(raw) if raw is not None else None
    except (ValueError, TypeError):
        settings = None
    d["settings_summary"] = settings_summary(settings) if isinstance(settings, dict) else {}
    return d
