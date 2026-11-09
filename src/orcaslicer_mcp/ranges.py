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


def _number(el, or_percent: bool) -> float | None:
    """The number the sidebar would check, or None when this is not a plain number (left for
    OrcaSlicer to judge: it refuses unparsable text itself)."""
    if isinstance(el, bool):
        return None
    if isinstance(el, (int, float)):
        return float(el) if math.isfinite(el) else None
    if not isinstance(el, str):
        return None
    s = el.strip().replace(" ", "")
    if s.endswith("%"):
        s = s[:-1]
    elif or_percent and s.endswith("mm"):
        s = s[:-2]
    try:
        n = float(s)
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def _elements(value, vector: bool) -> list:
    if isinstance(value, (list, tuple)):
        return list(value)
    if vector and isinstance(value, str):
        return value.split(",")
    return [value]


def _fmt(n: float) -> str:
    return format(n, "g")


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


def describe_problem(key: str, value, lo: float | None, hi: float | None,
                     unit: str | None = None) -> str:
    """One plain sentence for a refused value."""
    rec = settings_schema.describe(key) or {}
    label = rec.get("full_label") or rec.get("label")
    name = f"{label} ({key})" if label else key
    if unit is None:
        unit = _unit(rec)
    return f"{name} must be {range_text(key, lo, hi, unit)}; {_shown(value)} is outside that range"


def schema_bounds(key: str) -> tuple[float | None, float | None, str]:
    rec = settings_schema.describe(key) or {}
    return _bound(rec.get("min")), _bound(rec.get("max")), _unit(rec)


def out_of_range(changes: dict) -> dict[str, dict]:
    """{key: {value, min, max, unit, detail}} for every value outside its setting's range.
    Unknown keys, non-numeric settings and values that are not plain numbers are left alone:
    OrcaSlicer judges those itself."""
    found: dict[str, dict] = {}
    for key, value in (changes or {}).items():
        rec = settings_schema.describe(key)
        if not rec or rec.get("type") not in _NUMERIC_TYPES:
            continue
        lo, hi, unit = _bound(rec.get("min")), _bound(rec.get("max")), _unit(rec)
        or_percent = "OrPercent" in rec["type"]
        positive = key in _MUST_BE_POSITIVE
        for el in _elements(value, rec["type"] in _VECTOR_TYPES):
            n = _number(el, or_percent)
            if n is None:
                continue
            if (positive and n <= 0) or (lo is not None and n < lo) or (hi is not None and n > hi):
                found[key] = {"value": value, "min": lo, "max": hi, "unit": unit,
                              "detail": describe_problem(key, value, lo, hi, unit)}
                break
    return found
