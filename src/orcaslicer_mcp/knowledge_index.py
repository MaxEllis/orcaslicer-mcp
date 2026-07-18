"""Load and search the curated knowledge base (spec 2026-07-18 §4-5)."""
from __future__ import annotations
import functools, re
from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True)
class KChunk:
    relpath: str
    title: str
    topics: tuple[str, ...]
    orca_keys: tuple[str, ...]
    body: str


def _parse_list(line: str) -> tuple[str, ...]:
    m = re.search(r"\[(.*)\]", line)
    return tuple(x.strip() for x in m.group(1).split(",") if x.strip()) if m else ()


def _parse(relpath: str, text: str) -> KChunk:
    topics, keys, body = (), (), text
    if text.startswith("---"):
        head, _, body = text[3:].partition("---")
        for line in head.splitlines():
            if line.strip().startswith("topics:"):    topics = _parse_list(line)
            if line.strip().startswith("orca_keys:"): keys = _parse_list(line)
    m = re.search(r"^#\s+(.+)$", body, re.M)
    title = m.group(1).strip() if m else relpath
    return KChunk(relpath, title, topics, keys, body.strip())


@functools.lru_cache(maxsize=1)
def _load_knowledge_cached() -> tuple[KChunk, ...]:
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
    return tuple(out)


def load_knowledge() -> list[KChunk]:
    """Load knowledge chunks. Returns a fresh list each call over cached shared chunks."""
    return list(_load_knowledge_cached())


def search_knowledge(query: str, limit: int = 6) -> list[KChunk]:
    """Search knowledge chunks by ranking relevance to query.

    Ranks chunks by topic match (+10), title match (+5), orca_keys match (+3),
    and body word occurrences (up to +5). Returns top results by score.
    """
    q = query.lower().strip()
    if not q:
        return []
    words = [w for w in re.split(r"[^a-z0-9_]+", q) if len(w) > 2]
    scored = []
    for c in load_knowledge():
        score = 0
        for w in words:
            if any(w in t for t in c.topics):        score += 10
            if w in c.title.lower():                 score += 5
            if any(w in k for k in c.orca_keys):     score += 3
            score += min(c.body.lower().count(w), 5)
        # keep chunks with any signal
        if score:
            scored.append((score, c))
    scored.sort(key=lambda t: -t[0])
    return [c for _, c in scored[:limit]]
