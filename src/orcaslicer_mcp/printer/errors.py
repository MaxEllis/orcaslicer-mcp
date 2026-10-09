"""Errors the printer tools hand back to the model: a stable code, a plain message and a hint."""
from __future__ import annotations

CODES = frozenset({
    "not_configured", "unsupported_connection", "not_reachable", "auth_required",
    "auth_rejected", "orca_unreachable", "unsupported_for_connection", "protocol_error",
})

_CORE_KEYS = frozenset({"error", "message", "hint"})


class PrinterError(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None, **details):
        if code not in CODES:
            raise ValueError(f"unknown printer error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details

    def as_dict(self) -> dict:
        out = {"error": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        # The core keys always win: a detail of the same name must not replace the code or the
        # message, or stand in for a hint the error does not have.
        out.update({k: v for k, v in self.details.items() if k not in _CORE_KEYS})
        return out


def auth_error(service: str, key_set: bool, *, basic_auth: bool = False) -> PrinterError:
    if basic_auth:
        return PrinterError("auth_rejected", f"{service} refused the user name and password in the printer address.",
                            hint="Check the user name and password in ORCA_PRINTER_URL "
                                 "(special characters must be percent-encoded).")
    if key_set:
        return PrinterError("auth_rejected", f"{service} refused the API key in ORCA_PRINTER_API_KEY.",
                            hint="Check the key in the printer's web interface and update ORCA_PRINTER_API_KEY.")
    return PrinterError("auth_required", f"{service} needs an API key.",
                        hint="Create an API key in the printer's web interface and set it as "
                             "ORCA_PRINTER_API_KEY in this MCP server's settings.")
