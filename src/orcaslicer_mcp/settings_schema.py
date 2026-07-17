from __future__ import annotations
import functools
import json
from importlib.resources import files


@functools.lru_cache(maxsize=1)
def _doc() -> dict:
    p = files("orcaslicer_mcp").joinpath("data/print_settings_schema.json")
    return json.loads(p.read_text(encoding="utf-8"))


def _settings() -> dict:
    return {k: v for k, v in _doc().items() if not k.startswith("_")}


def describe(key: str) -> dict | None:
    """Full record for one setting key, or None if unknown."""
    rec = _settings().get(key)
    return None if rec is None else {"key": key, **rec}


def _rank(key: str, label: str | None, tooltip: str | None, q: str) -> int | None:
    """Lower is better; None means no match. Prefer key/label matches over tooltip-only."""
    k, lb, tt = key.lower(), (label or "").lower(), (tooltip or "").lower()
    if k == q:
        return 0
    if q in k:
        return 1
    if q in lb:
        return 2
    if q in tt:
        return 3
    return None


def search(query: str, limit: int = 25) -> list[dict]:
    """Compact matches (key/label/category/short tooltip), ranked so settings
    matched by key or label rank above those matched only in the tooltip body."""
    q = query.lower().strip()
    hits = []
    if not q:
        return hits
    for key, rec in _settings().items():
        rank = _rank(key, rec.get("label"), rec.get("tooltip"), q)
        if rank is None:
            continue
        tip = rec.get("tooltip") or ""
        hits.append((rank, key, {
            "key": key,
            "label": rec.get("label"),
            "category": rec.get("category"),
            "tooltip": tip[:120] + ("…" if len(tip) > 120 else ""),
        }))
    hits.sort(key=lambda h: (h[0], h[1]))  # by rank, then key name
    return [h[2] for h in hits[:limit]]
