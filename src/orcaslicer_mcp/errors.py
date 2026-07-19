from __future__ import annotations


class ApiError(Exception):
    pass


class Unauthorized(ApiError):
    pass


class NotFound(ApiError):
    """404 from the fork. `route_missing=True` means the endpoint itself doesn't exist
    (older build, e.g. pre-M4); False means the route exists but the resource doesn't
    (unknown_object / unknown_preset / missing file)."""

    def __init__(self, message: str, route_missing: bool = False):
        super().__init__(message)
        self.route_missing = route_missing


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
    return ServerError(msg)
