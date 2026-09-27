"""Config key guard: keep the assistant away from config keys that run code, drive the printer
directly, or redirect uploads.

The OrcaSlicer MCP build's PUT /api/v1/config accepts any print, filament or printer key.
Some of those keys are not slicing settings at all:

- post_process: shell commands OrcaSlicer runs after export (arbitrary code execution).
- *_gcode templates: raw G-code sent to the printer (on Klipper, macros can reach the host).
- print_host / printhost_* / host_type: where "send to printer" uploads, and its credentials.
- printer_model / printer_technology: can switch OrcaSlicer onto code paths that run
  post_process on every slice.

An assistant holding the API token is steerable by text it reads (model names, G-code,
web pages), so these keys are refused here, before the request reaches OrcaSlicer. Writing
a key back to the value it already has is allowed, so restore paths keep working. Edit these
settings by hand in the OrcaSlicer GUI. To let the MCP write specific ones anyway, list them
in ORCA_MCP_ALLOW_KEYS (comma separated).
"""
from __future__ import annotations
import json
import os

from .errors import ApiError

BLOCKED_EXACT = frozenset({
    "post_process",
    "print_host",
    "print_host_webui",
    "printhost_apikey",
    "printhost_authorization_type",
    "printhost_cafile",
    "printhost_password",
    "printhost_port",
    "printhost_ssl_ignore_revoke",
    "printhost_user",
    "host_type",
    "bbl_use_printhost",
    "printer_model",
    "printer_technology",
    "filename_format",
})

# Keys ending in _gcode that are plain toggles, not G-code templates.
_GCODE_SUFFIX_EXEMPT = frozenset({"emit_machine_limits_to_gcode"})

SECRET_KEYS = frozenset({"printhost_apikey", "printhost_password", "printhost_user"})
REDACTED = "<redacted by orcaslicer-mcp>"


class BlockedKey(ApiError):
    def __init__(self, keys: list[str]):
        self.keys = sorted(keys)
        super().__init__(
            "blocked_by_local_policy: the MCP refuses to write "
            + ", ".join(self.keys)
            + " (post-processing scripts, custom G-code, printer host/credentials and printer "
              "model can run code or drive the printer). Ask the user to change these in the "
              "OrcaSlicer GUI, or to allow them explicitly via ORCA_MCP_ALLOW_KEYS.")


def _allowed_override() -> frozenset[str]:
    raw = os.environ.get("ORCA_MCP_ALLOW_KEYS", "")
    return frozenset(k.strip() for k in raw.split(",") if k.strip())


def is_sensitive(key: str) -> bool:
    if key in BLOCKED_EXACT:
        return True
    return key.endswith("_gcode") and key not in _GCODE_SUFFIX_EXEMPT


def sensitive_keys(changes: dict) -> list[str]:
    allow = _allowed_override()
    return [k for k in changes if is_sensitive(k) and k not in allow]


def _norm(v) -> str:
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, sort_keys=True)
    except (TypeError, ValueError):
        return str(v)


def unchanged(new, current) -> bool:
    if current is None:
        return False
    if _norm(new) == _norm(current):
        return True
    # The fork serializes list options as strings; accept a list that joins to the same text.
    if isinstance(new, list) and isinstance(current, str):
        return ";".join(map(str, new)) == current or ",".join(map(str, new)) == current
    return False


def redact_secrets(cfg: dict) -> dict:
    """Blank credentials in a preset config before it reaches the model."""
    if not isinstance(cfg, dict):
        return cfg
    out = {}
    for k, v in cfg.items():
        if k in SECRET_KEYS and v not in (None, "", []):
            out[k] = REDACTED
        elif isinstance(v, dict):
            out[k] = redact_secrets(v)
        else:
            out[k] = v
    return out
