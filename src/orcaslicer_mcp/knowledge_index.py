"""Load and search the curated knowledge base (spec 2026-07-18 §4-5)."""
from __future__ import annotations
import functools, re
from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True)
class KChunk:
    relpath: str
    title: str
    topics: list[str]
    orca_keys: list[str]
    body: str


def _parse_list(line: str) -> list[str]:
    m = re.search(r"\[(.*)\]", line)
    return [x.strip() for x in m.group(1).split(",") if x.strip()] if m else []


def _parse(relpath: str, text: str) -> KChunk:
    topics, keys, body = [], [], text
    if text.startswith("---"):
        head, _, body = text[3:].partition("---")
        for line in head.splitlines():
            if line.strip().startswith("topics:"):    topics = _parse_list(line)
            if line.strip().startswith("orca_keys:"): keys = _parse_list(line)
    m = re.search(r"^#\s+(.+)$", body, re.M)
    title = m.group(1).strip() if m else relpath
    return KChunk(relpath, title, topics, keys, body.strip())


@functools.lru_cache(maxsize=1)
def load_knowledge() -> list[KChunk]:
    root = resources.files("orcaslicer_mcp") / "knowledge"
    out = []
    # Fallback to pathlib if rglob is not available on Traversable
    try:
        md_files = root.rglob("*.md")
    except AttributeError:
        import pathlib
        root = pathlib.Path(str(root))
        md_files = root.rglob("*.md")

    for p in sorted(md_files):
        rel = str(p).split("knowledge/")[-1]
        out.append(_parse(rel, p.read_text(encoding="utf-8")))
    return out
