"""Range check for config writes: refuse a value outside the setting's own min/max before it
reaches OrcaSlicer.

OrcaSlicer's sidebar refuses such values as you type them, but its API took them as given: a
layer height of -1 was applied and the next slice ran out of memory and closed OrcaSlicer, and
250 % infill went through silently (usability test, 2026-11-09). The limits come from the
settings schema this package ships (the same min/max describe_setting shows), checked the way
the sidebar's field checks them (Field.cpp, ConfigOptionDef::is_value_valid): the number is
compared with min and max inclusively, a percentage by its number ("250%" is 250), and every
element of a list setting on its own.
"""
from __future__ import annotations
import math
import re

from . import settings_schema

_NUMERIC_TYPES = frozenset({
    "coFloat", "coFloats", "coInt", "coInts", "coPercent", "coPercents",
    "coFloatOrPercent", "coFloatsOrPercents"})
_VECTOR_TYPES = frozenset({"coFloats", "coInts", "coPercents", "coFloatsOrPercents"})

# Settings whose schema minimum is 0 although 0 itself cannot print: a zero or negative layer
# height has no meaning, and the slicer does not survive it.
_MUST_BE_POSITIVE = frozenset({"layer_height", "initial_layer_print_height"})

# The schema extractor kept a few units with the first letter doubled, and some as \u escapes.
_UNIT_FIXES = {"ssecond": "s", "NNewton": "N", "ggram": "g"}


def _bound(raw) -> float | None:
    """A schema bound as a number: 0, 2.0, "0.5f" or "5.f" (C++ float literals the extractor
    kept as text). Named constants such as "max_temp" are left unchecked."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if math.isfinite(raw) else None
    if isinstance(raw, str):
        m = re.fullmatch(r"\s*(-?\d+(?:\.\d*)?)f?\s*", raw)
        if m:
            return float(m.group(1))
    return None


def _unit(rec: dict) -> str:
    u = rec.get("unit") or ""
    if "\\u" in u:
        try:
            u = u.encode("ascii", "backslashreplace").decode("unicode_escape")
        except UnicodeDecodeError:
            pass
    return _UNIT_FIXES.get(u, u)


def _parse(el, or_percent: bool) -> tuple[float, bool] | None:
    """(number, given as a percentage) as the sidebar would read it, or None when this is not a
    plain number (left for OrcaSlicer to judge: it refuses unparsable text itself)."""
    if isinstance(el, bool):
        return None
    if isinstance(el, (int, float)):
        return (float(el), False) if math.isfinite(el) else None
    if not isinstance(el, str):
        return None
    s = el.strip().replace(" ", "")
    pct = s.endswith("%")
    if pct:
        s = s[:-1]
    elif or_percent and s.endswith("mm"):
        s = s[:-2]
    try:
        n = float(s)
    except ValueError:
        return None
    return (n, pct) if math.isfinite(n) else None


def _number(el, or_percent: bool) -> float | None:
    p = _parse(el, or_percent)
    return None if p is None else p[0]


def _elements(value, vector: bool) -> list:
    if isinstance(value, (list, tuple)):
        return list(value)
    if vector and isinstance(value, str):
        return value.split(",")
    return [value]


def _fmt(n: float) -> str:
    return format(n, "g")


_EPS = 1e-4  # ConfigOptionDef::is_value_valid with max_precision 4


def in_range(key: str, n: float, lo: float | None, hi: float | None) -> bool:
    if key in _MUST_BE_POSITIVE and n <= 0:
        return False
    if lo == 0 and n < 0:
        return False
    if (lo is not None and abs(n - lo) < _EPS) or (hi is not None and abs(n - hi) < _EPS):
        return True
    return (lo is None or n >= lo) and (hi is None or n <= hi)


def range_text(key: str, lo: float | None, hi: float | None, unit: str = "") -> str:
    """'from 0 to 100 %', 'greater than 0 mm', 'at least 1 mm/s', 'at most 2'."""
    u = f" {unit}" if unit else ""
    if key in _MUST_BE_POSITIVE and (lo is None or lo <= 0):
        return f"greater than 0{u}" + (f" and at most {_fmt(hi)}{u}" if hi is not None else "")
    if lo is not None and hi is not None:
        return f"from {_fmt(lo)} to {_fmt(hi)}{u}"
    if lo is not None:
        return f"at least {_fmt(lo)}{u}"
    if hi is not None:
        return f"at most {_fmt(hi)}{u}"
    return "within its range"


def _shown(value) -> str:
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


def _display_unit(rec: dict, unit: str, percent: bool) -> str:
    """A "mm or %" setting states its bounds in the form the value was given in."""
    if "OrPercent" in str(rec.get("type")):
        return "%" if percent else ""
    return unit


def describe_problem(key: str, value, lo: float | None, hi: float | None,
                     unit: str | None = None, index: int | None = None) -> str:
    """One plain sentence for a refused value (`value` is the offending element)."""
    rec = settings_schema.describe(key) or {}
    label = rec.get("full_label") or rec.get("label")
    name = f"{label} ({key})" if label else key
    if unit is None:
        unit = _unit(rec)
    where = f" (entry {index + 1})" if index is not None else ""
    return f"{name} must be {range_text(key, lo, hi, unit)}; {_shown(value)}{where} is outside that range"


def schema_bounds(key: str) -> tuple[float | None, float | None, str]:
    rec = settings_schema.describe(key) or {}
    return _bound(rec.get("min")), _bound(rec.get("max")), _unit(rec)


def _same(a: tuple[float, bool] | None, b: tuple[float, bool] | None) -> bool:
    return a is not None and b is not None and a[1] == b[1] and abs(a[0] - b[0]) < 1e-9


def out_of_range(changes: dict, current: dict | None = None) -> dict[str, dict]:
    """{key: {value, element, index, min, max, min_exclusive, unit, detail}} for every value outside
    its setting's range. Unknown keys, non-numeric settings and values that are not plain numbers
    are left alone, and so is an element equal to what `current` (the setting's present value
    text, as get_config reports it) already holds: OrcaSlicer judges those itself."""
    found: dict[str, dict] = {}
    current = current or {}
    for key, value in (changes or {}).items():
        rec = settings_schema.describe(key)
        if not rec or rec.get("type") not in _NUMERIC_TYPES:
            continue
        lo, hi, unit = _bound(rec.get("min")), _bound(rec.get("max")), _unit(rec)
        or_percent = "OrPercent" in rec["type"]
        vector = rec["type"] in _VECTOR_TYPES
        elems = _elements(value, vector)
        now = [_parse(e, or_percent) for e in _elements(current.get(key), vector)] if key in current else []
        for i, el in enumerate(elems):
            p = _parse(el, or_percent)
            if p is None or in_range(key, p[0], lo, hi):
                continue
            if (i < len(now) and _same(p, now[i])) or (len(elems) == 1 and any(_same(p, c) for c in now)):
                continue  # already there: writing it back changes nothing
            idx = i if len(elems) > 1 else None
            shown_unit = _display_unit(rec, unit, p[1])
            found[key] = {"value": value, "element": el, "index": idx, "min": lo, "max": hi,
                          "min_exclusive": key in _MUST_BE_POSITIVE, "unit": shown_unit,
                          "detail": describe_problem(key, el, lo, hi, shown_unit, idx)}
            break
    return found
