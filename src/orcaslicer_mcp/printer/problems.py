"""Turn what a printer reports into a short list of problems: a severity, where it came from, the
printer's own words, and for common Klipper messages a plain-English hint. A message that is not
in the table passes through word for word with no hint: never guess a cause."""
from __future__ import annotations

# (lower-case needle, hint): the first needle found in the message wins. The needles are distinctive
# phrases from Klipper's own wording and none contains another, so the order is not load-bearing.
# Keep them specific: a broad needle would attach a wrong hint to an unrelated message, such as the
# config error "Option 'max_temp' in section 'extruder' must be specified". A message that matches
# nothing is better left without a hint.
HINTS: tuple[tuple[str, str], ...] = (
    ("lost communication with mcu",
     "The printer's control board isn't talking to the Pi: usually the printer is switched off or its "
     "USB cable is unplugged. Turn it on, then restart the firmware from the printer's web page "
     "(Mainsail or Fluidd)."),
    ("must home axis first",
     "Something tried to move before the printer homed: usually the start G-code or a macro skips G28."),
    ("not heating at expected rate",
     "Klipper's heater safety check tripped: a heater, temperature sensor or wiring fault. Check it "
     "before restarting."),
    ("move out of range",
     "A move went past the printer's limits: run check_printer_match to compare the profile's bed size "
     "with the printer."),
    ("timer too close",
     "The Pi fell behind while sending moves: usually it is overloaded or its SD card is slow."),
    ("adc out of range",
     "A temperature sensor reported an impossible value: it is probably unplugged or shorted."),
    ("requested temperature",
     "The profile asks for more heat than the printer allows: run check_printer_match."),
)

_PERMISSION_MARKERS = ("PolicyKit", "PolKit")


def hint_for(message: str) -> str | None:
    low = (message or "").lower()
    for needle, hint in HINTS:
        if needle in low:
            return hint
    return None


def trim_klipper_message(msg: str) -> str:
    """Klipper appends the same restart instructions to every fault; the hint covers them."""
    keep = []
    for line in (msg or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("Once the underlying issue is corrected"):
            break
        if s in ("Printer is shutdown", "Printer is halted"):
            continue
        keep.append(s)
    return " ".join(keep)


def problem(severity: str, source: str, message: str, at: float | None = None) -> dict:
    return {"severity": severity, "source": source, "message": message, "hint": hint_for(message), "at": at}


def klippy_problem(webhooks: dict) -> dict | None:
    state = (webhooks or {}).get("state")
    if state not in ("shutdown", "error"):
        return None
    msg = trim_klipper_message(webhooks.get("state_message") or "") or f"Klipper is in the {state} state."
    return problem("fatal", "klipper", msg)


def job_problem(print_stats: dict) -> dict | None:
    if (print_stats or {}).get("state") != "error":
        return None
    msg = trim_klipper_message(print_stats.get("message") or "") or "The print stopped with an error."
    return problem("error", "job", msg)


def console_problems(store: list[dict], since: float) -> list[dict]:
    """Error lines (Klipper prefixes them with '!!') at or after `since`, newest five. The store is
    oldest first. Messages are trimmed like the rest of Klipper's text, a line with nothing left
    (just the restart boilerplate) is dropped, and identical messages collapse into their newest
    occurrence, so a fault that repeats cannot push every other error out of the five."""
    newest: dict[str, dict] = {}
    for line in store or []:
        msg, t = str(line.get("message") or ""), line.get("time")
        if not msg.startswith("!!") or not isinstance(t, (int, float)) or t < since:
            continue
        text = trim_klipper_message(msg[2:])
        if not text:
            continue
        newest.pop(text, None)  # re-inserting moves it to the end: the newest occurrence stays
        newest[text] = problem("error", "console", text, at=float(t))
    return list(newest.values())[-5:]


def moonraker_warnings(server_info: dict, config_warnings: list | None = None) -> list[dict]:
    warnings = [str(w) for w in (server_info or {}).get("warnings") or []]
    permission = [w for w in warnings if any(m in w for m in _PERMISSION_MARKERS)]
    out = []
    if permission:
        n = len(permission)
        out.append(problem("warning", "moonraker",
                           f"{n} Moonraker setup warning{'s' if n != 1 else ''} (system permissions), "
                           "not print-related."))
    out += [problem("warning", "moonraker", w) for w in warnings if w not in permission]
    for w in config_warnings or []:
        msg = w.get("message") if isinstance(w, dict) else str(w)
        if msg:
            out.append(problem("warning", "klipper", msg))
    for comp in (server_info or {}).get("failed_components") or []:
        out.append(problem("warning", "moonraker", f"Moonraker component failed to load: {comp}"))
    return out
