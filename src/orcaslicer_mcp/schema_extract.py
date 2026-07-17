from __future__ import annotations
import re

# Strict: a real, named setting definition -> creates a record.
_ADD_RE = re.compile(r'def\s*=\s*this->add\(\s*"([^"]+)"\s*,\s*(co\w+)\s*\)')
# Any def = this->add / add_nullable -> block boundary (stops one setting's
# block from absorbing a following add_nullable loop body, etc.).
_BOUND_RE = re.compile(r'def\s*=\s*this->add(?:_nullable)?\(')
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


def _strip_line_comments(s: str) -> str:
    """Remove C++ `//` line comments, honoring string literals (a `//` inside a
    double-quoted string is kept)."""
    out, in_str, esc, i, n = [], False, False, 0, len(s)
    while i < n:
        ch = s[i]
        if esc:
            out.append(ch); esc = False; i += 1; continue
        if ch == "\\":
            out.append(ch); esc = True; i += 1; continue
        if ch == '"':
            in_str = not in_str; out.append(ch); i += 1; continue
        if not in_str and ch == "/" and i + 1 < n and s[i + 1] == "/":
            while i < n and s[i] != "\n":
                i += 1
            continue
        out.append(ch); i += 1
    return "".join(out)


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

    def uncommented(m: re.Match) -> bool:
        return "//" not in text[text.rfind("\n", 0, m.start()) + 1: m.start()]

    create = [m for m in _ADD_RE.finditer(text) if uncommented(m)]
    bounds = sorted(m.start() for m in _BOUND_RE.finditer(text) if uncommented(m))

    settings: dict[str, dict] = {}
    for m in create:
        key, ctype = m.group(1), m.group(2)
        start = m.end()
        end = next((b for b in bounds if b > m.start()), len(text))
        block = _strip_line_comments(text[start:end])
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
                    val = _join_strings(s)
                    if val is not None:  # never clobber a set value with a non-literal RHS
                        rec[_STRING_FIELDS[field]] = val
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
