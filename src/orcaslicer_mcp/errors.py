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


class OutOfRange(Validation):
    """A value outside its setting's min/max, refused before OrcaSlicer applies anything:
    by this server's own check (ranges.py) or by OrcaSlicer (422 out_of_range).
    `ranges` is {key: {value, min, max, unit, detail}}."""

    def __init__(self, ranges: dict[str, dict]):
        self.ranges = ranges
        details = [r["detail"] for r in ranges.values()]
        super().__init__("out_of_range: " + "; ".join(details) + ". Nothing was changed.",
                         {k: r["detail"] for k, r in ranges.items()})


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _out_of_range_from_body(body: dict) -> "OutOfRange | None":
    """OrcaSlicer's own range refusal: {"error":"out_of_range","key","value","min","max"},
    or per-key entries of `errors` that say out of range. The bounds OrcaSlicer sends win;
    without them the shipped schema's are used."""
    from .ranges import describe_problem, schema_bounds
    found: dict[str, dict] = {}

    def add(key, value, lo, hi, detail=None):
        if key in found:  # the top-level entry and errors[key] describe the same refusal
            return
        s_lo, s_hi, unit = schema_bounds(key)
        lo = _num(lo) if _num(lo) is not None else s_lo
        hi = _num(hi) if _num(hi) is not None else s_hi
        found[key] = {"value": value, "min": lo, "max": hi, "unit": unit,
                      "detail": detail or describe_problem(key, value, lo, hi, unit)}

    if body.get("error") == "out_of_range" and isinstance(body.get("key"), str):
        add(body["key"], body.get("value"), body.get("min"), body.get("max"))
    errors = body.get("errors")
    if isinstance(errors, dict):
        for key, why in errors.items():
            if isinstance(why, dict) and (why.get("error") == "out_of_range"
                                          or "min" in why or "max" in why):
                add(key, why.get("value"), why.get("min"), why.get("max"))
            elif isinstance(why, str) and why.startswith("out_of_range: "):
                # OrcaSlicer MCP builds: "out_of_range: Layer height must be at least 0 mm (got -1mm)"
                add(key, "the value sent", None, None, detail=why[len("out_of_range: "):])
            elif isinstance(why, str) and why.replace("_", " ").lower().find("out of range") >= 0:
                add(key, "the value sent", None, None)
    return OutOfRange(found) if found else None


def _invalid_values_message(errors: dict) -> str:
    parts = [f"{k}: {v}" for k, v in errors.items()]
    return ("invalid_values: OrcaSlicer refused " + "; ".join(parts)
            + ". Nothing was changed; describe_setting shows each setting's type and allowed values.")


def error_from_status(status: int, body: dict) -> ApiError:
    err = _error_from_status(status, body)
    err.body = body if isinstance(body, dict) else {}
    return err


def _error_from_status(status: int, body: dict) -> ApiError:
    msg = body.get("error") or body.get("detail") or f"HTTP {status}"
    if status == 401:
        return Unauthorized("unauthorized: OrcaSlicer refused the token. Ask the user to copy it again "
                            "from Preferences > Remote API (or the OrcaSlicer MCP plugin's Config tab) "
                            "into this connector's ORCA_API_TOKEN setting.")
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
        oor = _out_of_range_from_body(body)
        if oor is not None:
            return oor
        errors = body.get("errors")
        if not body.get("error") and not body.get("detail") and isinstance(errors, dict) and errors:
            return Validation(_invalid_values_message(errors), errors)
        return Validation(msg, errors)
    if status == 504:
        return UiTimeout("ui_timeout: OrcaSlicer's window didn't answer in time; a dialog may be open "
                         "in it, ask the user to look, then try again.")
    # The plugin answers 503 with a reason the user can act on ("paused": its panel was closed).
    if status == 503 and body.get("error") and body.get("detail"):
        return ServerError(f"{body['error']}: {body['detail']}")
    return ServerError(msg)
