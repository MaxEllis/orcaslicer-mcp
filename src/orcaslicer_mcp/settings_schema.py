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


def _word_score(key: str, label: str | None, tooltip: str | None, words: list[str]) -> int:
    """Per-word OR-match score (F16): a multi-word query like "plate temp" must
    reach underscored keys like hot_plate_temp even though the whole phrase never
    appears verbatim in any field. Higher is better; 0 means no word matched."""
    k, lb, tt = key.lower(), (label or "").lower(), (tooltip or "").lower()
    ktokens = [t for t in k.split("_") if len(t) > 2]
    score = 0
    for w in words:
        # prefix-match key tokens too, so "temperature" reaches keys spelt "temp"
        if w in k or any(t.startswith(w) or w.startswith(t) for t in ktokens):
            score += 3
        elif w in lb:
            score += 2
        elif w in tt:
            score += 1
    return score


def search(query: str, limit: int = 25) -> list[dict]:
    """Compact matches (key/label/category/short tooltip), ranked so settings
    matched by key or label rank above those matched only in the tooltip body,
    with a per-word fallback so multi-word queries match underscored keys."""
    q = query.lower().strip()
    hits = []
    if not q:
        return hits
    words = [w for w in q.replace("_", " ").split() if len(w) > 2]
    for key, rec in _settings().items():
        rank = _rank(key, rec.get("label"), rec.get("tooltip"), q)
        score = _word_score(key, rec.get("label"), rec.get("tooltip"), words)
        if rank is None:
            if not score:
                continue
            rank = 4  # word-level matches rank below any whole-phrase match
        tip = rec.get("tooltip") or ""
        hits.append((rank, -score, key, {
            "key": key,
            "label": rec.get("label"),
            "category": rec.get("category"),
            "tooltip": tip[:120] + ("…" if len(tip) > 120 else ""),
        }))
    hits.sort(key=lambda h: (h[0], h[1], h[2]))  # rank, then word score, then key
    return [h[3] for h in hits[:limit]]
