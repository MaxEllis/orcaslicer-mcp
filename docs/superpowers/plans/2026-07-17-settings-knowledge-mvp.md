# Settings Knowledge (static-extraction MVP) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the AI an authoritative, offline lookup of OrcaSlicer's ~700 setting definitions (label, tooltip, valid range, enum values, default) via two new MCP tools, extracted once from the fork's `PrintConfig.cpp`.

**Architecture:** A pure parser (`schema_extract.py`) turns `PrintConfig.cpp` source text into a dict of setting records. A CLI (`scripts/extract_settings_schema.py`) runs it against the relay checkout and writes a bundled JSON artifact. A thin runtime loader (`settings_schema.py`) reads that JSON and backs two `@mcp.tool` functions in `server.py`. Everything is offline — no network, no live Remote-API call.

**Tech Stack:** Python ≥3.11, FastMCP (`mcp>=1.2.0`), pytest + pytest-asyncio (`asyncio_mode=auto`), hatchling build. No new runtime dependencies.

## Global Constraints

- Python ≥3.11; no new runtime dependencies (stdlib only for the new code).
- Tools must be fully offline: no `_client()`, no network, work when OrcaSlicer is not running.
- English source strings only (no localization of labels/tooltips).
- Parsing never silently drops a key — any key yielding neither label nor tooltip is reported in `_meta.unparsed_keys`.
- `default` is best-effort: capture the raw source expression (may be a macro like `INITIAL_LAYER_HEIGHT`); do not resolve it.
- Keep the existing `find_config_keys` tool (live keys); `search_settings` is additive (offline definitions).
- New package data lives at `src/orcaslicer_mcp/data/print_settings_schema.json` and is loaded via `importlib.resources`.
- Extraction source default: `/home/max/orca-relay/src/libslic3r/PrintConfig.cpp` (overridable via `--src`).

---

### Task 1: Source parser (`schema_extract.py`)

**Files:**
- Create: `src/orcaslicer_mcp/schema_extract.py`
- Test: `tests/test_schema_extract.py`

**Interfaces:**
- Produces: `parse_print_config(text: str) -> tuple[dict[str, dict], list[str]]`. First element maps setting key → record `{label, category, tooltip, unit, type, min, max, mode, enum_values, enum_labels, default}` (missing values are `None`; `enum_*` are lists or `None`). Second element is the list of keys that yielded neither label nor tooltip.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schema_extract.py
from orcaslicer_mcp.schema_extract import parse_print_config

FIXTURE = r'''
    def           = this->add("layer_height", coFloat);
    def->label    = L("Layer height");
    def->category = L("Quality");
    def->tooltip  = L("Slicing height for each layer; smaller is more accurate.");
    def->sidetext = L("mm");
    def->min      = 0;
    def->mode     = comSimple;
    def->set_default_value(new ConfigOptionFloat(INITIAL_LAYER_HEIGHT));

    def = this->add("sparse_infill_pattern", coEnum);
    def->label = L("Sparse infill pattern");
    def->category = L("Strength");
    def->tooltip = L("Line pattern for internal "
                     "sparse infill.");
    def->enum_values.push_back("grid");
    def->enum_values.push_back("gyroid");
    def->enum_labels.push_back(L("Grid"));
    def->enum_labels.push_back(L("Gyroid"));
    def->set_default_value(new ConfigOptionEnum<InfillPattern>(ipGrid));

    def = this->add("printable_area", coPoints);
    def->label = L("Printable area");

    def = this->add("compatible_printers", coStrings);
'''

def test_parses_float_setting_with_semicolon_in_tooltip():
    settings, unparsed = parse_print_config(FIXTURE)
    s = settings["layer_height"]
    assert s["type"] == "coFloat"
    assert s["label"] == "Layer height"
    assert s["category"] == "Quality"
    # tooltip preserves the embedded semicolon (string-aware split)
    assert s["tooltip"] == "Slicing height for each layer; smaller is more accurate."
    assert s["unit"] == "mm"
    assert s["min"] == 0
    assert s["mode"] == "comSimple"
    assert s["default"] == "INITIAL_LAYER_HEIGHT"
    assert s["enum_values"] is None

def test_parses_enum_and_joins_multiline_tooltip():
    settings, _ = parse_print_config(FIXTURE)
    s = settings["sparse_infill_pattern"]
    assert s["type"] == "coEnum"
    assert s["tooltip"] == "Line pattern for internal sparse infill."
    assert s["enum_values"] == ["grid", "gyroid"]
    assert s["enum_labels"] == ["Grid", "Gyroid"]
    assert s["default"] == "ipGrid"

def test_label_without_tooltip_is_not_unparsed():
    settings, unparsed = parse_print_config(FIXTURE)
    assert settings["printable_area"]["label"] == "Printable area"
    assert settings["printable_area"]["tooltip"] is None
    assert "printable_area" not in unparsed

def test_key_with_no_metadata_is_reported_unparsed():
    settings, unparsed = parse_print_config(FIXTURE)
    assert "compatible_printers" in settings
    assert "compatible_printers" in unparsed

def test_setting_count():
    settings, _ = parse_print_config(FIXTURE)
    assert len(settings) == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/max/projects/orcaslicer-mcp && uv run pytest tests/test_schema_extract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orcaslicer_mcp.schema_extract'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/orcaslicer_mcp/schema_extract.py
from __future__ import annotations
import re

_ADD_RE = re.compile(r'def\s*=\s*this->add\(\s*"([^"]+)"\s*,\s*(co\w+)\s*\)')
_STR_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_FIELD_RE = re.compile(r'def->(\w+)\s*=\s*(.*)', re.DOTALL)
_DEFAULT_RE = re.compile(r'set_default_value\(\s*new\s+\w+[^(]*\((.*)\)\s*\)\s*$', re.DOTALL)

# def-> string field name -> output record key
_STRING_FIELDS = {"label": "label", "full_label": "full_label",
                  "category": "category", "tooltip": "tooltip",
                  "sidetext": "unit"}


def _unescape(s: str) -> str:
    return (s.replace('\\"', '"').replace("\\n", "\n")
             .replace("\\t", " ").replace("\\\\", "\\"))


def _join_strings(stmt: str) -> str | None:
    parts = _STR_RE.findall(stmt)
    if not parts:
        return None
    return _unescape("".join(parts)).strip()


def _split_statements(block: str) -> list[str]:
    """Split C++ source into `;`-terminated statements, ignoring `;` inside string literals."""
    out, buf, in_str, esc = [], [], False, False
    for ch in block:
        if esc:
            buf.append(ch); esc = False; continue
        if ch == "\\":
            buf.append(ch); esc = True; continue
        if ch == '"':
            in_str = not in_str; buf.append(ch); continue
        if ch == ";" and not in_str:
            out.append("".join(buf)); buf = []; continue
        buf.append(ch)
    tail = "".join(buf)
    if tail.strip():
        out.append(tail)
    return out


def _num(raw: str):
    raw = raw.strip()
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw or None  # keep macros/expressions verbatim


def parse_print_config(text: str) -> tuple[dict[str, dict], list[str]]:
    """Parse PrintConfig.cpp source text into (settings, unparsed_keys)."""
    settings: dict[str, dict] = {}
    matches = list(_ADD_RE.finditer(text))
    for i, m in enumerate(matches):
        key, ctype = m.group(1), m.group(2)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end]
        rec = {"label": None, "category": None, "tooltip": None, "unit": None,
               "type": ctype, "min": None, "max": None, "mode": None,
               "enum_values": None, "enum_labels": None, "default": None}
        for stmt in _split_statements(block):
            s = stmt.strip()
            if s.startswith("def->enum_values.push_back"):
                v = _join_strings(s)
                if v is not None:
                    rec["enum_values"] = (rec["enum_values"] or []) + [v]
                continue
            if s.startswith("def->enum_labels.push_back"):
                v = _join_strings(s)
                if v is not None:
                    rec["enum_labels"] = (rec["enum_labels"] or []) + [v]
                continue
            fm = _FIELD_RE.match(s)
            if fm:
                field, rhs = fm.group(1), fm.group(2)
                if field in _STRING_FIELDS:
                    rec[_STRING_FIELDS[field]] = _join_strings(s)
                elif field in ("min", "max"):
                    rec[field] = _num(rhs)
                elif field == "mode":
                    rec["mode"] = rhs.strip().rstrip(";").strip() or None
                continue
            dm = _DEFAULT_RE.search(s)
            if dm:
                rec["default"] = dm.group(1).strip() or None
        settings.setdefault(key, rec)
    unparsed = [k for k, r in settings.items() if not r["label"] and not r["tooltip"]]
    return settings, unparsed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/max/projects/orcaslicer-mcp && uv run pytest tests/test_schema_extract.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
cd /home/max/projects/orcaslicer-mcp
git add src/orcaslicer_mcp/schema_extract.py tests/test_schema_extract.py
git commit -m "feat(settings): PrintConfig.cpp parser (label/tooltip/range/enum/default)"
```

---

### Task 2: Extraction CLI + generated artifact

**Files:**
- Create: `scripts/extract_settings_schema.py`
- Create (generated): `src/orcaslicer_mcp/data/print_settings_schema.json`
- Modify: `pyproject.toml` (ensure package data is shipped)
- Test: `tests/test_settings_artifact.py`

**Interfaces:**
- Consumes: `parse_print_config` (Task 1).
- Produces: the bundled JSON `{"_meta": {...}, "<key>": {record}, ...}` at `src/orcaslicer_mcp/data/print_settings_schema.json`. `_meta` carries `source`, `source_git_sha`, `extracted` (YYYY-MM-DD), `setting_count`, `with_label`, `with_label_and_tooltip`, `unparsed_keys`.

- [ ] **Step 1: Write the CLI**

```python
# scripts/extract_settings_schema.py
#!/usr/bin/env python3
"""Extract OrcaSlicer setting definitions from PrintConfig.cpp into a bundled JSON."""
from __future__ import annotations
import argparse, datetime, json, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from orcaslicer_mcp.schema_extract import parse_print_config  # noqa: E402

DEFAULT_SRC = Path("/home/max/orca-relay/src/libslic3r/PrintConfig.cpp")
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "src/orcaslicer_mcp/data/print_settings_schema.json"


def git_sha(src: Path) -> str:
    try:
        r = subprocess.run(["git", "-C", str(src.parent), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, check=True)
        return r.stdout.strip()
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--min-count", type=int, default=600)
    ap.add_argument("--date", default=None, help="override extraction date (YYYY-MM-DD)")
    args = ap.parse_args()

    text = args.src.read_text(encoding="utf-8", errors="replace")
    settings, unparsed = parse_print_config(text)
    with_label = sum(1 for r in settings.values() if r.get("label"))
    with_tip = sum(1 for r in settings.values() if r.get("label") and r.get("tooltip"))
    meta = {
        "source": args.src.name,
        "source_git_sha": git_sha(args.src),
        "extracted": args.date or datetime.date.today().isoformat(),
        "setting_count": len(settings),
        "with_label": with_label,
        "with_label_and_tooltip": with_tip,
        "unparsed_keys": sorted(unparsed),
    }
    doc = {"_meta": meta}
    doc.update(dict(sorted(settings.items())))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    cov = (with_tip / len(settings) * 100) if settings else 0.0
    print(f"parsed {len(settings)} settings; label+tooltip {with_tip} ({cov:.1f}%); "
          f"unparsed {len(unparsed)} -> {args.out}")
    if len(settings) < args.min_count:
        print(f"ERROR: only {len(settings)} settings (< {args.min_count})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Ensure package data is shipped (pyproject.toml)**

Add this block to `pyproject.toml` (after the `[build-system]` block):

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/orcaslicer_mcp"]
```

(Hatchling includes non-Python files inside the package dir by default; this makes the package root explicit for the src layout.)

- [ ] **Step 3: Generate the artifact**

Run: `cd /home/max/projects/orcaslicer-mcp && uv run python scripts/extract_settings_schema.py`
Expected: a line like `parsed 7xx settings; label+tooltip 6xx (9x.x%); unparsed NN -> .../print_settings_schema.json` and exit 0. If exit 1 (count < 600), inspect the printed numbers and the `--src` path before proceeding.

- [ ] **Step 4: Write the artifact test**

```python
# tests/test_settings_artifact.py
import json
from importlib.resources import files

def _doc():
    p = files("orcaslicer_mcp").joinpath("data/print_settings_schema.json")
    return json.loads(p.read_text(encoding="utf-8"))

def test_artifact_has_meta_and_many_settings():
    doc = _doc()
    assert "_meta" in doc
    settings = {k: v for k, v in doc.items() if not k.startswith("_")}
    assert len(settings) >= 600
    assert doc["_meta"]["setting_count"] == len(settings)

def test_known_float_setting():
    s = _doc()["layer_height"]
    assert s["type"] == "coFloat"
    assert s["label"]
    assert s["tooltip"]
    assert s["unit"] == "mm"

def test_known_enum_setting_has_values():
    s = _doc()["sparse_infill_pattern"]
    assert s["type"] == "coEnum"
    assert isinstance(s["enum_values"], list) and "gyroid" in s["enum_values"]
```

- [ ] **Step 5: Run the artifact test**

Run: `cd /home/max/projects/orcaslicer-mcp && uv run pytest tests/test_settings_artifact.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
cd /home/max/projects/orcaslicer-mcp
git add scripts/extract_settings_schema.py src/orcaslicer_mcp/data/print_settings_schema.json pyproject.toml tests/test_settings_artifact.py
git commit -m "feat(settings): extraction CLI + generated schema artifact"
```

---

### Task 3: Runtime loader + MCP tools

**Files:**
- Create: `src/orcaslicer_mcp/settings_schema.py`
- Modify: `src/orcaslicer_mcp/server.py` (add two tools; import the loader)
- Test: `tests/test_server_settings.py`

**Interfaces:**
- Consumes: the bundled JSON (Task 2).
- Produces: loader functions `describe(key: str) -> dict | None` and `search(query: str, limit: int = 25) -> list[dict]`; MCP tools `describe_setting(key: str) -> dict` and `search_settings(query: str, limit: int = 25) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_settings.py
import orcaslicer_mcp.server as srv

def test_describe_setting_known():
    out = srv.describe_setting("layer_height")
    assert out["key"] == "layer_height"
    assert out["type"] == "coFloat"
    assert out["tooltip"]

def test_describe_setting_unknown():
    out = srv.describe_setting("no_such_setting_xyz")
    assert out["error"] == "unknown_setting"
    assert out["key"] == "no_such_setting_xyz"

def test_search_settings_finds_infill():
    out = srv.search_settings("infill")
    keys = [r["key"] for r in out["results"]]
    assert "sparse_infill_pattern" in keys
    # results are compact records
    r = out["results"][0]
    assert set(r) == {"key", "label", "category", "tooltip"}

def test_search_settings_respects_limit():
    out = srv.search_settings("a", limit=3)
    assert len(out["results"]) <= 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/max/projects/orcaslicer-mcp && uv run pytest tests/test_server_settings.py -v`
Expected: FAIL — `AttributeError: module 'orcaslicer_mcp.server' has no attribute 'describe_setting'`

- [ ] **Step 3: Write the loader**

```python
# src/orcaslicer_mcp/settings_schema.py
from __future__ import annotations
import functools, json
from importlib.resources import files


@functools.lru_cache(maxsize=1)
def _doc() -> dict:
    p = files("orcaslicer_mcp").joinpath("data/print_settings_schema.json")
    return json.loads(p.read_text(encoding="utf-8"))


def _settings() -> dict:
    return {k: v for k, v in _doc().items() if not k.startswith("_")}


def describe(key: str) -> dict | None:
    rec = _settings().get(key)
    return None if rec is None else {"key": key, **rec}


def search(query: str, limit: int = 25) -> list[dict]:
    q = query.lower().strip()
    hits = []
    for key, rec in _settings().items():
        hay = " ".join(x for x in (key, rec.get("label"), rec.get("tooltip")) if x).lower()
        if q and q in hay:
            tip = rec.get("tooltip") or ""
            hits.append({
                "key": key,
                "label": rec.get("label"),
                "category": rec.get("category"),
                "tooltip": tip[:120] + ("…" if len(tip) > 120 else ""),
            })
    hits.sort(key=lambda h: (h["key"] != query, h["key"]))  # exact key match first
    return hits[:limit]
```

- [ ] **Step 4: Add the tools to server.py**

Add near the top of `src/orcaslicer_mcp/server.py`, with the other imports:

```python
from . import settings_schema
```

Add these two tools (e.g. just after the `find_config_keys` tool):

```python
@mcp.tool()
def describe_setting(key: str) -> dict:
    """Authoritative definition of one OrcaSlicer setting: label, tooltip, type, unit, range, enum values, default. Offline; works even if OrcaSlicer is not running."""
    rec = settings_schema.describe(key)
    if rec is None:
        return {"error": "unknown_setting", "key": key}
    return rec


@mcp.tool()
def search_settings(query: str, limit: int = 25) -> dict:
    """Search settings by keyword across key/label/tooltip; returns compact matches (key, label, category, short tooltip). Offline."""
    return {"results": settings_schema.search(query, limit)}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/max/projects/orcaslicer-mcp && uv run pytest tests/test_server_settings.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Run the full suite + import smoke**

Run: `cd /home/max/projects/orcaslicer-mcp && uv run pytest -q`
Expected: all tests pass (existing + new).

- [ ] **Step 7: Commit**

```bash
cd /home/max/projects/orcaslicer-mcp
git add src/orcaslicer_mcp/settings_schema.py src/orcaslicer_mcp/server.py tests/test_server_settings.py
git commit -m "feat(settings): describe_setting + search_settings MCP tools"
```

---

## Verification (whole feature)

- [ ] `uv run pytest -q` — entire suite green.
- [ ] `uv run python -c "import orcaslicer_mcp.server"` — server imports (tools registered) with no live API.
- [ ] `uv run python -c "import orcaslicer_mcp.settings_schema as s; import json; print(json.dumps(s.describe('layer_height'), indent=2)); print([r['key'] for r in s.search('infill', 5)])"` — spot-check a real describe + search against the generated artifact.
