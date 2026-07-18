"""Local plain-file notes store: one markdown file per scope (spec §6)."""
from __future__ import annotations
import os, re, datetime
from pathlib import Path


def _dir() -> Path:
    d = Path(os.environ.get("ORCA_MCP_NOTES_DIR", Path.home() / ".orcaslicer-mcp" / "notes"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fname(scope: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", scope)
    safe = re.sub(r"\.\.+", "_", safe)  # collapse ".." sequences (path-traversal safety)
    return safe + ".md"


def append_note(note: str, scope: str) -> Path:
    p = _dir() / _fname(scope)
    is_new = not p.exists()
    stamp = datetime.date.today().isoformat()
    with open(p, "a", encoding="utf-8") as f:
        if is_new:
            f.write(f"# scope: {scope}\n")
        f.write(f"- [{stamp}] {note.strip()}\n")
    return p


def _scope_for(p: Path, first_line: str | None) -> str:
    if first_line and first_line.startswith("# scope:"):
        return first_line[len("# scope:"):].strip()
    return p.stem


def search_notes(query: str) -> list[str]:
    words = [w for w in re.split(r"[^a-z0-9]+", query.lower()) if len(w) > 2]
    out = []
    for p in sorted(_dir().glob("*.md")):
        lines = p.read_text(encoding="utf-8").splitlines()
        first_line = lines[0] if lines else None
        scope = _scope_for(p, first_line)
        body_lines = lines[1:] if (first_line and first_line.startswith("# scope:")) else lines
        for line in body_lines:
            text = line[2:] if line.startswith("- ") else line
            text = text.split("] ", 1)[-1]
            if any(w in text.lower() for w in words):
                out.append(f"{scope}: {text}")
    return out
