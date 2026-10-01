"""Config key guard: keep the assistant away from config keys that run code, drive the printer
directly, or redirect uploads.

The OrcaSlicer MCP build's PUT /api/v1/config accepts any print, filament or printer key.
Some of those keys are not slicing settings at all:

- post_process: shell commands OrcaSlicer runs after export (arbitrary code execution).
- *_gcode templates: raw G-code sent to the printer (on Klipper, macros can reach the host).
- the physical-printer keys (print_host, printhost_*, host_type, printer_agent, ...): where
  "send to printer" uploads, how it connects, and with which credentials.
- printer_model / printer_technology: can switch OrcaSlicer onto code paths that run
  post_process on every slice.

An assistant holding the API token is steerable by text it reads (model names, G-code,
web pages), so these keys are refused here, before the request reaches OrcaSlicer. Writing
a key back to the value it already has is allowed, so restore paths keep working; the
connection keys OrcaSlicer never reports (UNREADABLE_KEYS) cannot be shown unchanged, so
they are always refused. Edit these settings by hand in the OrcaSlicer GUI. To let the MCP
write specific ones anyway, list them in ORCA_MCP_ALLOW_KEYS (comma separated, wildcards
such as *_gcode allowed).

This guard only covers the MCP's own tools. A client that can run shell commands can still
reach OrcaSlicer directly with the token; OrcaSlicer MCP v2.4.2-mcp.10 and later enforce the
same rule inside the slicer, unlocked only from its Preferences.
"""
from __future__ import annotations
import fnmatch
import os
import re

from .errors import ApiError

# OrcaSlicer's physical-printer options (s_PhysicalPrinter_opts in libslic3r/Preset.cpp,
# 2.4.2, minus the preset_name(s) bookkeeping): where and how "send to printer" connects.
PHYSICAL_PRINTER_KEYS = frozenset({
    "printer_technology",
    "bbl_use_printhost",
    "host_type",
    "printer_agent",
    "print_host",
    "print_host_webui",
    "printhost_apikey",
    "flashforge_serial_number",
    "printhost_cafile",
    "printhost_port",
    "printhost_authorization_type",
    "printhost_user",
    "printhost_password",
    "printhost_ssl_ignore_revoke",
})

BLOCKED_EXACT = PHYSICAL_PRINTER_KEYS | {"post_process", "printer_model", "filename_format"}

# Keys ending in _gcode that are plain toggles, not G-code templates.
_GCODE_SUFFIX_EXEMPT = frozenset({"emit_machine_limits_to_gcode"})

# GET /config never reports these (PresetBundle::full_config_secure erases them; the G-code
# footer bans the same list), so a write can never be shown to leave them unchanged.
UNREADABLE_KEYS = frozenset({
    "print_host", "print_host_webui", "printhost_apikey", "printhost_cafile",
    "printhost_user", "printhost_password", "printhost_port",
})

# What get_preset_config hides: credentials outright, and any user:password inside a host
# URL (the print_host tooltip documents https://user:password@host/). The host itself, the
# CA file path and the port stay visible so upload problems remain diagnosable.
CREDENTIAL_KEYS = frozenset({"printhost_apikey", "printhost_password", "printhost_user"})
URL_KEYS = frozenset({"print_host", "print_host_webui"})
REDACTED = "<redacted>"

# Greedy up to the last "@" before the first "/", so a password containing "@" is covered.
_URL_USERINFO = re.compile(r"^((?:[A-Za-z][A-Za-z0-9+.-]*://)?)[^/\s]+@")


class BlockedKey(ApiError):
    def __init__(self, keys: list[str]):
        self.keys = sorted(keys)
        super().__init__(
            "blocked_by_local_policy: the MCP refuses to write "
            + ", ".join(self.keys)
            + " (post-processing scripts, custom G-code, printer connection settings and printer "
              "model can run code or drive the printer). Ask the user to change these in the "
              "OrcaSlicer GUI, or to allow them explicitly via ORCA_MCP_ALLOW_KEYS.")


class RedactedValue(ApiError):
    def __init__(self, keys: list[str]):
        self.keys = sorted(keys)
        super().__init__(
            "redacted_placeholder: the value for " + ", ".join(self.keys) + " contains "
            + REDACTED + ", which get_preset_config shows in place of a credential. Writing "
              "it back would replace the real value; ask the user to enter it in the "
              "OrcaSlicer GUI.")


def _allow_entries() -> list[str]:
    raw = os.environ.get("ORCA_MCP_ALLOW_KEYS", "")
    if raw.strip().startswith("${"):  # Claude Desktop left the optional setting unsubstituted
        return []
    return [k.strip() for k in raw.split(",") if k.strip()]


def _allowed(key: str, entries: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(key, e) for e in entries)


def allow_override_warnings() -> list[str]:
    """ORCA_MCP_ALLOW_KEYS entries that can never take effect: a plain name that is not a
    protected key (usually a typo). Wildcard entries are taken as written."""
    return [f"ORCA_MCP_ALLOW_KEYS: {e!r} is not a protected config key, so it has no effect"
            for e in _allow_entries()
            if not any(c in e for c in "*?[") and not is_sensitive(e)]


def is_sensitive(key: str) -> bool:
    if key in BLOCKED_EXACT or key.startswith("printhost_"):
        return True
    return key.endswith("_gcode") and key not in _GCODE_SUFFIX_EXEMPT


def sensitive_keys(changes: dict) -> list[str]:
    allow = _allow_entries()
    return [k for k in changes if is_sensitive(k) and not _allowed(k, allow)]


def check_placeholders(changes: dict) -> None:
    """Refuse any value carrying the redaction marker (a model round-tripping a redacted
    preset), whatever the key, so a placeholder never overwrites a real credential."""
    hits = [k for k, v in (changes or {}).items() if isinstance(v, str) and REDACTED in v]
    if hits:
        raise RedactedValue(hits)


def as_config_text(v) -> str | None:
    """The text the fork's PUT /config deserializes for this JSON value (its
    json_value_to_config_string), or None for a type it rejects (lists, objects, null)."""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return format(v, "g")  # C++ ostream default: 6 significant digits, like %g
    if isinstance(v, str):
        return v
    return None


def unchanged(new, current) -> bool:
    """True only when the write would hand OrcaSlicer exactly the text it already reports
    for the key. Anything else (including a value that merely means the same) counts as a
    change, so the guard errs towards refusing."""
    text = as_config_text(new)
    return text is not None and current is not None and text == current


def changed_keys(changes: dict, hits: list[str], current: dict) -> list[str]:
    return [k for k in hits if k in UNREADABLE_KEYS or not unchanged(changes[k], current.get(k))]


def _strip_userinfo(url: str) -> str:
    return _URL_USERINFO.sub(lambda m: m.group(1) + REDACTED + "@", url, count=1)


def redact_secrets(cfg: dict) -> dict:
    """Hide credentials in a preset config before it reaches the model."""
    if not isinstance(cfg, dict):
        return cfg
    out = {}
    for k, v in cfg.items():
        if k in CREDENTIAL_KEYS and v not in (None, "", []):
            out[k] = REDACTED
        elif k in URL_KEYS and isinstance(v, str):
            out[k] = _strip_userinfo(v)
        elif isinstance(v, dict):
            out[k] = redact_secrets(v)
        else:
            out[k] = v
    return out
