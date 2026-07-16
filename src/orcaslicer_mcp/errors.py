from __future__ import annotations


class ApiError(Exception):
    pass


class Unauthorized(ApiError):
    pass


class NotFound(ApiError):
    """Unknown route, or an endpoint this OrcaSlicer build doesn't implement yet (e.g. M4)."""


class BadRequest(ApiError):
    pass


class Conflict(ApiError):
    pass


class UiTimeout(ApiError):
    pass


class NotReachable(ApiError):
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
        return NotFound("endpoint not found / not available on this OrcaSlicer build")
    if status == 400:
        return BadRequest(msg)
    if status == 409:
        return Conflict(msg)
    if status == 422:
        return Validation(msg, body.get("errors"))
    if status == 504:
        return UiTimeout("OrcaSlicer GUI did not respond in time; retry")
    return ServerError(msg)
