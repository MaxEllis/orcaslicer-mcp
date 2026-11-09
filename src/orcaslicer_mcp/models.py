from __future__ import annotations
from typing import TypedDict, Any


class SliceResult(TypedDict):
    state: str
    percent: int
    message: str
    stats: dict | None
    warnings: list


class CompareRow(TypedDict):
    value: Any
    stats: dict | None
    warnings: list
    error: str | None


# OrcaSlicer's own sentences for its slice-warning codes (Plater::get_slice_warning_string),
# so the assistant relays a sentence, not "bed_temperature_too_high_than_filament".
WARNING_TEXT = {
    "bed_temperature_too_high_than_filament": (
        "The current heatbed temperature is relatively high. The nozzle may clog when printing "
        "this filament in a closed environment. Please open the front door and/or remove the "
        "upper glass. (OrcaSlicer raises this when the bed temperature is at or above the "
        "filament's Softening temperature, temperature_vitrification.)"),
    "the_actual_nozzle_hrc_smaller_than_the_required_nozzle_hrc": (
        "The nozzle hardness required by the filament is higher than the default nozzle "
        "hardness of the printer. Please replace the hardened nozzle or filament, otherwise, "
        "the nozzle will be worn down or damaged."),
    "not_support_traditional_timelapse": (
        "Enabling traditional timelapse photography may cause surface imperfections. It is "
        "recommended to change to smooth mode."),
    "not_generate_timelapse": "No timelapse video can be made for this print.",
    "smooth_timelapse_without_prime_tower": (
        "Smooth mode for timelapse is enabled, but the prime tower is off, which may cause print "
        "defects. Please enable the prime tower, re-slice and print again."),
    "activate_long_retraction_when_cut": "Long retraction when cutting the filament is turned on.",
}

# OrcaSlicer's warning levels as words. None of them stops a slice; 0 is a tip.
WARNING_LEVELS = {0: "tip", 1: "warning", 2: "warning", 3: "warning"}

OUT_OF_DATE_NOTE = ("Out of date: something changed after this slice, so these numbers no longer "
                    "match the plate. Slice again before quoting them.")


def describe_warning(w):
    """A warning dict gains `text` (OrcaSlicer's sentence) and `severity` (the level as a word)
    beside its code; other shapes pass through unchanged."""
    if not isinstance(w, dict):
        return w
    out = dict(w)
    msg = str(w.get("message") or "")
    out["text"] = WARNING_TEXT.get(msg, msg)
    lvl = w.get("level")
    if isinstance(lvl, int) and not isinstance(lvl, bool):
        out["severity"] = WARNING_LEVELS.get(lvl, "warning")
    return out


def is_out_of_date(status: dict, status_valid=None) -> bool:
    """A finished slice that a later change has made stale. OrcaSlicer MCP builds that report it
    say so on /slice/status (valid false or stale true); for older ones the caller passes
    /status's slice_result_valid."""
    if status.get("state") != "done":
        return False
    if status.get("stale") is True or status.get("valid") is False:
        return True
    if "valid" in status or "stale" in status:
        return False
    return status_valid is False


def summarize_slice(status: dict, status_valid=None) -> SliceResult:
    out = {
        "state": status.get("state", "idle"),
        "percent": status.get("percent", -1),
        "message": status.get("message", ""),
        "stats": status.get("stats"),
        "warnings": [describe_warning(w) for w in (status.get("warnings") or [])],
    }
    if is_out_of_date(status, status_valid):
        out["out_of_date"] = True
        out["note"] = OUT_OF_DATE_NOTE
    return out
