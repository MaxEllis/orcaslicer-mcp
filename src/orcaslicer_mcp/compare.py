"""Pure shaping + comparison logic for compare_slices (spec 2026-08-07).

No live API here: the orchestrator in server.py produces one result dict per
variant and this module turns them into the research-backed output shape
(headline + table + structured rows), computing every derived number so the
model never has to. See docs/superpowers/specs/2026-08-07-compare-slices-design.md.
"""
from __future__ import annotations
import math


def _minutes(seconds: float | None) -> int | None:
    """The whole minutes the table shows (a slice estimate is not second-accurate)."""
    if seconds is None:
        return None
    return int(math.floor(seconds / 60.0 + 0.5))


def _grams(g: float | None) -> float | None:
    """The tenth of a gram the table shows."""
    return None if g is None else round(g, 1)


def _fmt_minutes(total_min: int) -> str:
    h, m = divmod(total_min, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _fmt_time(seconds: float | None) -> str:
    """Whole minutes, seconds dropped."""
    if seconds is None:
        return "?"
    return _fmt_minutes(_minutes(seconds))


def _signed_minutes(delta_min: int) -> str:
    if delta_min == 0:
        return "0m"
    return f"{'+' if delta_min > 0 else '-'}{_fmt_minutes(abs(delta_min))}"


def _signed_time(delta_s: float) -> str:
    return _signed_minutes(_minutes(abs(delta_s)) * (1 if delta_s >= 0 else -1))


def _signed_mass(delta_g: float) -> str:
    r = round(delta_g, 1)
    if r == 0:  # avoid "-0.0 g" when the difference rounds away
        return "0.0 g"
    return f"{'+' if r > 0 else '-'}{abs(r):.1f} g"


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
    """Only variants that actually sliced can be recommended or compared numerically. Each gets
    the shown (rounded) minutes and grams: every comparison and delta uses those, so what the
    table says adds up (usability test 2026-11-09: '20m vs 16m' next to a '-3m' delta)."""
    out = []
    for r in results:
        if r.get("error") is None and r.get("time_s") is not None and r.get("filament_g") is not None:
            out.append({**r, "_min": _minutes(r["time_s"]), "_g": _grams(r["filament_g"])})
    return out


def _no_worse(a: dict, b: dict) -> bool:
    """a is at least as good as b on time, filament and warnings (as shown)."""
    return a["_min"] <= b["_min"] and a["_g"] <= b["_g"] and _has_warn(a) <= _has_warn(b)


def _extremes(cand: list[dict]) -> tuple[str | None, str | None]:
    if not cand:
        return (None, None)
    fastest = min(cand, key=lambda r: r["_min"])["name"]
    lightest = min(cand, key=lambda r: r["_g"])["name"]
    return (fastest, lightest)


def _recommend(cand: list[dict]) -> tuple[str | None, bool, dict | None, str]:
    """Return (recommended_name, is_dominant, tradeoff, reason).

    A variant is recommended (and starred) only when it is at least as good as every other on
    time, filament and warnings. In a trade-off nothing is recommended: the reply names the
    fastest and the lightest and leaves the choice to the user."""
    if not cand:
        return (None, False, None, "no variant sliced successfully")

    fastest, lightest = _extremes(cand)

    for a in cand:
        if all(a is b or _no_worse(a, b) for b in cand):
            parts = []
            if a["name"] == fastest:
                parts.append("fastest")
            if a["name"] == lightest:
                parts.append("lightest")
            if not _has_warn(a):
                parts.append("no warnings")
            reason = ", ".join(parts) if parts else "no worse than the alternatives on any metric"
            return (a["name"], True, None, reason)

    tradeoff = {"fastest": fastest, "lightest": lightest}
    warning_free = [r for r in cand if not _has_warn(r)]
    if warning_free and len(warning_free) < len(cand):
        tradeoff["fastest_without_warnings"] = min(warning_free, key=lambda r: r["_min"])["name"]
    return (None, False, tradeoff, "no single option wins on time, filament and warnings")


def _tradeoff_headline(tradeoff: dict) -> str:
    fastest, lightest = tradeoff["fastest"], tradeoff["lightest"]
    clean = tradeoff.get("fastest_without_warnings")
    if fastest == lightest:
        # The trade-off is the warning: the fastest and lightest option has one.
        return (f"No clear winner: {fastest} is fastest and lightest but has a warning, "
                f"{clean} has no warnings.")
    head = f"No clear winner: {fastest} is fastest, {lightest} is lightest."
    if clean and clean not in (fastest, lightest):
        head += f" {clean} is the fastest with no warnings."
    return head


def _delta_vs(r: dict, base: dict | None) -> dict:
    if base is None or r.get("time_s") is None or base.get("time_s") is None:
        return {"time_pct": None, "time_formatted": "n/a",
                "filament_pct": None, "filament_formatted": "n/a"}
    if r["name"] == base["name"]:
        return {"time_pct": 0, "time_formatted": "baseline",
                "filament_pct": 0, "filament_formatted": "baseline"}
    r_min, b_min = _minutes(r["time_s"]), _minutes(base["time_s"])
    r_g, b_g = _grams(r["filament_g"]), _grams(base["filament_g"])
    t_pct, t_pct_s = _pct(r_min, b_min)
    g_pct, g_pct_s = _pct(r_g, b_g)
    t_delta, g_delta = _signed_minutes(r_min - b_min), _signed_mass(r_g - b_g)
    return {
        "time_pct": t_pct,
        "time_delta": t_delta,
        "time_formatted": f"{t_delta} ({t_pct_s})",
        "filament_pct": g_pct,
        "filament_delta": g_delta,
        "filament_formatted": f"{g_delta} ({g_pct_s})",
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
            lines.append(f"| {label} | - | - | - | {v['_notes']} |")
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

    if recommended is not None:
        headline = f"Recommended: {recommended} - {reason}."
    elif tradeoff is not None:
        headline = _tradeoff_headline(tradeoff)
    else:
        headline = "No variant sliced successfully."

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
