"""A static read of the printer's start and end G-code, for mistakes that quietly spoil prints.

Idea from the PrinterAssist plugin on OrcaCloud (plugin review 2026-11-11), which audits the custom G-code text.
Every finding is a heuristic on the script text, never a measurement, so the check only ever warns: a script that
calls a printer macro (Klipper's PRINT_START and the like) does its work on the printer, where this cannot see.

Findings:
- a fixed nozzle or bed temperature (M104/M109/M140/M190 S<number>) where a placeholder would follow the filament;
- relative positioning (G91) switched on and never switched back (G90) in the start G-code;
- no homing in the start G-code (no G28 and no macro call);
- no heaters turned off in the end G-code (no M104 S0 / M140 S0 / TURN_OFF_HEATERS and no macro call);
- a move to an X or Y outside the printable area (fine when the printer travels there, as purge lines often do).
"""
from __future__ import annotations

import re

START_KEY, END_KEY = "machine_start_gcode", "machine_end_gcode"
_PLACEHOLDER = re.compile(r"\[[^\]]*\]|\{[^}]*\}")
_GCODE_WORD = re.compile(r"^[GMT]\d+(\.\d+)?$", re.I)
# A bare word that is not a G/M/T code: a Klipper macro or extended command (PRINT_START, start_print, ...).
_MACRO = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,}$")
_HEATER_CMDS = {"M104": "nozzle", "M109": "nozzle", "M140": "bed", "M190": "bed"}
TOLERANCE_MM = 1.0


def _lines(script: str) -> list[list[str]]:
    """Each non-empty line as words, comments and placeholders removed ("M109 S[temp]" -> ["M109", "S"])."""
    text = str(script or "").replace("\\n", "\n")  # OrcaSlicer stores scripts with escaped newlines
    out = []
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        words = _PLACEHOLDER.sub("", line).split()
        if words:
            out.append(words)
    return out


def _calls_macro(lines: list[list[str]]) -> bool:
    return any(_MACRO.match(w[0]) and not _GCODE_WORD.match(w[0]) for w in lines)


def _number(word: str, letter: str) -> float | None:
    if len(word) > 1 and word[0].upper() == letter:
        try:
            return float(word[1:])
        except ValueError:
            return None
    return None


def _bounds(printable_area) -> tuple[float, float, float, float] | None:
    pts = []
    for part in str(printable_area or "").split(","):
        a, sep, b = part.partition("x")
        if sep:
            try:
                pts.append((float(a), float(b)))
            except ValueError:
                pass
    if not pts:
        return None
    return min(p[0] for p in pts), max(p[0] for p in pts), min(p[1] for p in pts), max(p[1] for p in pts)


def audit(cfg: dict) -> tuple[str, list[str]]:
    """('pass' | 'warn', findings in words). Missing scripts give ('warn', ['insufficient data ...'])."""
    start, end = cfg.get(START_KEY), cfg.get(END_KEY)
    if start is None and end is None:
        return "warn", ["insufficient data (machine_start_gcode / machine_end_gcode)"]
    findings: list[str] = []
    s_lines, e_lines = _lines(start), _lines(end)
    bounds = _bounds(cfg.get("printable_area"))
    outside: dict[tuple, list[float]] = {}  # (script, axis, area min, area max) -> the values past it

    for key, lines in ((START_KEY, s_lines), (END_KEY, e_lines)):
        for words in lines:
            cmd = words[0].upper()
            if cmd in _HEATER_CMDS:
                for w in words[1:]:
                    val = _number(w, "S")
                    if val is not None and val > 0:
                        what = _HEATER_CMDS[cmd]
                        hint = "[nozzle_temperature_initial_layer]" if what == "nozzle" else "[bed_temperature_initial_layer_single]"
                        findings.append(f"{key} sets a fixed {what} temperature ({cmd} S{val:g}): every filament gets "
                                        f"{val:g} C there; a placeholder such as {hint} follows the filament")
            if cmd in ("G0", "G1") and bounds:
                x0, x1, y0, y1 = bounds
                for w in words[1:]:
                    for letter, lo, hi in (("X", x0, x1), ("Y", y0, y1)):
                        v = _number(w, letter)
                        if v is not None and (v < lo - TOLERANCE_MM or v > hi + TOLERANCE_MM):
                            outside.setdefault((key, letter, lo, hi), []).append(v)

    for (key, letter, lo, hi), vals in outside.items():
        far = sorted(set(vals), key=lambda v: -max(lo - v, v - hi))[0]
        findings.append(f"{key} moves to {letter}{far:g}, outside the printable area ({lo:g} to {hi:g} mm); fine if "
                        "the printer travels there, as purge lines often do")

    # relative positioning left on at the end of the start G-code
    mode = None
    for words in s_lines:
        c = words[0].upper()
        if c in ("G90", "G91"):
            mode = c
    if mode == "G91":
        findings.append(f"{START_KEY} switches to relative positioning (G91) and never back (G90): the print's "
                        "moves would be read as relative")

    macro_start, macro_end = _calls_macro(s_lines), _calls_macro(e_lines)
    if start is not None and s_lines and not macro_start and not any(w[0].upper() == "G28" for w in s_lines):
        findings.append(f"{START_KEY} has no homing (G28) and calls no printer macro")
    if end is not None and e_lines and not macro_end:
        off = any(w[0].upper() in ("M104", "M140", "M109", "M190") and any(_number(x, "S") == 0 for x in w[1:])
                  for w in e_lines) or any(w[0].upper() in ("TURN_OFF_HEATERS", "M81") for w in e_lines)
        if not off:
            findings.append(f"{END_KEY} turns no heater off (no M104 S0, M140 S0 or TURN_OFF_HEATERS) and calls "
                            "no printer macro")
    # one finding per distinct text: a purge line has many points outside the area
    seen, unique = set(), []
    for f in findings:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return ("warn" if unique else "pass"), unique
