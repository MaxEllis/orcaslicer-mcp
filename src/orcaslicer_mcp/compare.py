"""Pure shaping + comparison logic for compare_slices (spec 2026-08-07).

No live API here: the orchestrator in server.py produces one result dict per
variant and this module turns them into the research-backed output shape
(headline + table + structured rows), computing every derived number so the
model never has to. See docs/superpowers/specs/2026-08-07-compare-slices-design.md.
"""
from __future__ import annotations
import math


def _fmt_time(seconds: float | None) -> str:
    """Whole minutes, seconds dropped (a slice estimate is not second-accurate)."""
    if seconds is None:
        return "?"
    total_min = int(math.floor(seconds / 60.0 + 0.5))
    h, m = divmod(total_min, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _signed_time(delta_s: float) -> str:
    sign = "+" if delta_s >= 0 else "-"
    return f"{sign}{_fmt_time(abs(delta_s))}"


def _signed_mass(delta_g: float) -> str:
    return f"{'+' if delta_g >= 0 else '-'}{abs(round(delta_g, 1)):.1f} g"


def _pct(value: float, base: float) -> tuple[int, str]:
    """Whole-number signed percentage of `value` relative to `base`."""
    if not base:
        return (0, "0%")
    p = (value - base) / base * 100.0
    pi = int(math.floor(abs(p) + 0.5))
    pi = -pi if p < 0 else pi
    if pi > 0:
        return (pi, f"+{pi}%")
    if pi < 0:
        return (pi, f"{pi}%")
    return (0, "0%")


def _has_warn(r: dict) -> int:
    return 1 if r.get("warnings") else 0


def _candidates(results: list[dict]) -> list[dict]:
    """Only variants that actually sliced can be recommended or compared numerically."""
    return [r for r in results
            if r.get("error") is None and r.get("time_s") is not None
            and r.get("filament_g") is not None]


def _dominates(a: dict, b: dict) -> bool:
    """a dominates b: no worse on time, filament, warnings; strictly better on >=1."""
    no_worse = (a["time_s"] <= b["time_s"] and a["filament_g"] <= b["filament_g"]
                and _has_warn(a) <= _has_warn(b))
    strictly = (a["time_s"] < b["time_s"] or a["filament_g"] < b["filament_g"]
                or _has_warn(a) < _has_warn(b))
    return no_worse and strictly


def _extremes(cand: list[dict]) -> tuple[str | None, str | None]:
    if not cand:
        return (None, None)
    fastest = min(cand, key=lambda r: r["time_s"])["name"]
    lightest = min(cand, key=lambda r: r["filament_g"])["name"]
    return (fastest, lightest)


def _recommend(cand: list[dict]) -> tuple[str | None, bool, dict | None, str]:
    """Return (recommended_name, is_dominant, tradeoff, reason)."""
    if not cand:
        return (None, False, None, "no variant sliced successfully")

    fastest, lightest = _extremes(cand)

    # A single variant that dominates every other is the only honest "winner".
    for a in cand:
        if all(a is b or _dominates(a, b) for b in cand):
            parts = []
            if a["name"] == fastest:
                parts.append("fastest")
            if a["name"] == lightest:
                parts.append("lightest")
            if not _has_warn(a):
                parts.append("no warnings")
            reason = ", ".join(parts) if parts else "no worse than the alternatives on any metric"
            return (a["name"], True, None, reason)

    # Trade-off: satisficing default under a stated house rule.
    warning_free = [r for r in cand if not _has_warn(r)]
    pool = warning_free or cand
    pick = min(pool, key=lambda r: r["time_s"])["name"]
    rule = "the fastest option with no warnings" if warning_free else "the fastest option (all variants flag a warning)"
    reason = f"no single option wins on everything; suggested as {rule}"
    return (pick, False, {"fastest": fastest, "lightest": lightest}, reason)


def _delta_vs(r: dict, base: dict | None) -> dict:
    if base is None or r.get("time_s") is None or base.get("time_s") is None:
        return {"time_pct": None, "time_formatted": "n/a",
                "filament_pct": None, "filament_formatted": "n/a"}
    if r["name"] == base["name"]:
        return {"time_pct": 0, "time_formatted": "baseline",
                "filament_pct": 0, "filament_formatted": "baseline"}
    t_pct, t_pct_s = _pct(r["time_s"], base["time_s"])
    g_pct, g_pct_s = _pct(r["filament_g"], base["filament_g"])
    return {
        "time_pct": t_pct,
        "time_delta": _signed_time(r["time_s"] - base["time_s"]),
        "time_formatted": f"{_signed_time(r['time_s'] - base['time_s'])} ({t_pct_s})",
        "filament_pct": g_pct,
        "filament_delta": _signed_mass(r["filament_g"] - base["filament_g"]),
        "filament_formatted": f"{_signed_mass(r['filament_g'] - base['filament_g'])} ({g_pct_s})",
    }


def _row_notes(r: dict, fastest: str | None, lightest: str | None) -> str:
    if r.get("error"):
        return f"error: {r['error']}"
    tags = []
    if r["name"] == fastest:
        tags.append("fastest")
    if r["name"] == lightest:
        tags.append("lightest")
    n = len(r.get("warnings") or [])
    if n:
        tags.append(f"⚠ {n} warning" + ("s" if n > 1 else ""))
    return ", ".join(tags)


def _render_table(rows: list[dict], base_name: str, recommended: str | None) -> str:
    head = f"| Variant | Time | Filament | vs {base_name} | Notes |\n|---|---|---|---|---|"
    lines = [head]
    for v in rows:
        star = " ★" if v["name"] == recommended else ""
        label = f"**{v['name']}{star}**" if v["name"] == recommended else v["name"]
        if v.get("error"):
            lines.append(f"| {label} | — | — | — | {v['_notes']} |")
        else:
            d = v["delta_vs_baseline"]
            vs = "baseline" if d["time_formatted"] == "baseline" else f"{d['time_formatted']}, {d['filament_formatted']}"
            lines.append(f"| {label} | {v['time']['formatted']} | {v['filament']['formatted']} | {vs} | {v['_notes']} |")
    return "\n".join(lines)


def compute_comparison(results: list[dict], baseline: str | None, detail: bool) -> dict:
    """Turn per-variant slice results into the compare_slices output shape."""
    names = [r["name"] for r in results]
    if baseline is not None and baseline not in names:
        return {"error": "unknown_baseline", "baseline": baseline, "variants_available": names}

    if baseline is not None:
        base_name = baseline
    else:
        empty = next((r["name"] for r in results if not r.get("changes")), None)
        base_name = empty if empty is not None else (names[0] if names else None)
    base = next((r for r in results if r["name"] == base_name), None)

    cand = _candidates(results)
    fastest, lightest = _extremes(cand)
    recommended, is_dominant, tradeoff, reason = _recommend(cand)

    variants = []
    for r in results:
        row = {
            "name": r["name"],
            "changes": r.get("changes") or {},
            "warnings": r.get("warnings") or [],
            "valid": bool(r.get("valid")),
            "delta_vs_baseline": _delta_vs(r, base),
            "_notes": _row_notes(r, fastest, lightest),
        }
        if r.get("error"):
            row["error"] = r["error"]
        else:
            row["time"] = {"min": int(math.floor((r["time_s"] or 0) / 60.0 + 0.5)),
                           "formatted": _fmt_time(r["time_s"])}
            row["filament"] = {"g": round(r["filament_g"], 1),
                               "formatted": f"{round(r['filament_g'], 1):.1f} g"}
        if detail:
            row["roles"] = r.get("roles")
        variants.append(row)

    if recommended is None:
        headline = "No variant sliced successfully."
    elif is_dominant:
        headline = f"Recommended: {recommended} — {reason}."
    else:
        headline = (f"Recommended: {recommended} — {reason}. "
                    f"Trade-off: fastest is {fastest}, lightest is {lightest}.")

    table = _render_table(variants, base_name or "?", recommended)
    for v in variants:
        v.pop("_notes", None)

    return {
        "headline": headline,
        "baseline": base_name,
        "recommended": recommended,
        "recommended_is_dominant": is_dominant,
        "recommendation_reason": reason,
        "tradeoff": tradeoff,
        "variants": variants,
        "table_markdown": table,
    }
