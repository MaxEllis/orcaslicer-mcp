from __future__ import annotations


class ApiError(Exception):
    body: dict = {}


class Unauthorized(ApiError):
    pass


# What the OrcaSlicer MCP plugin (stock OrcaSlicer 2.5) can and cannot do. Its missing
# routes answer {"error": "not_found", "backend": "plugin"}; this text replaces the generic
# "not available on this build" so the assistant can tell the user exactly why.
PLUGIN_LIMITS = (
    "the OrcaSlicer MCP plugin (stock OrcaSlicer) cannot do this. It can read settings, "
    "presets, the objects on the plate and the project, and change settings. Loading or moving "
    "models, slicing, slice results and G-code, plate renders, plates, opening and saving "
    "projects, and saving or deleting presets need the OrcaSlicer MCP build: "
    "https://github.com/MaxEllis/OrcaSlicer/releases")


class NotFound(ApiError):
    """404 from the fork. `route_missing=True` means the endpoint itself doesn't exist
    (older build, e.g. pre-M4); False means the route exists but the resource doesn't
    (unknown_object / unknown_preset / missing file). `plugin=True` means the endpoint is
    missing because OrcaSlicer runs the OrcaSlicer MCP plugin, not the OrcaSlicer MCP build."""

    def __init__(self, message: str, route_missing: bool = False, plugin: bool = False):
        super().__init__(message)
        self.route_missing = route_missing
        self.plugin = plugin


class BadRequest(ApiError):
    pass


class Conflict(ApiError):
    pass


class UiTimeout(ApiError):
    pass


class NotReachable(ApiError):
    pass


class ConfigError(ApiError):
    pass


class ServerError(ApiError):
    pass


class Validation(ApiError):
    def __init__(self, message: str, errors: dict | None = None):
        super().__init__(message)
        self.errors = errors or {}


def error_from_status(status: int, body: dict) -> ApiError:
    err = _error_from_status(status, body)
    err.body = body if isinstance(body, dict) else {}
    return err


def _error_from_status(status: int, body: dict) -> ApiError:
    msg = body.get("error") or body.get("detail") or f"HTTP {status}"
    if status == 401:
        return Unauthorized("unauthorized (check ORCA_API_TOKEN)")
    if status == 404:
        # The fork's dispatch fallback answers {"error":"not_found"} (or nothing) for a
        # route that doesn't exist; resource-level 404s carry a specific token such as
        # unknown_object / unknown_preset. Keep them distinguishable (F7).
        err = body.get("error")
        if err and err != "not_found":
            return NotFound(err)
        if body.get("backend") == "plugin":
            return NotFound("not available: " + PLUGIN_LIMITS, route_missing=True, plugin=True)
        return NotFound("endpoint not found / not available on this OrcaSlicer build",
                        route_missing=True)
    if status == 400:
        return BadRequest(msg)
    if status == 409:
        return Conflict(msg)
    if status == 422:
        return Validation(msg, body.get("errors"))
    if status == 504:
        return UiTimeout("OrcaSlicer GUI did not respond in time; retry")
    # The plugin answers 503 with a reason the user can act on ("paused": its panel was closed).
    if status == 503 and body.get("error") and body.get("detail"):
        return ServerError(f"{body['error']}: {body['detail']}")
    return ServerError(msg)
