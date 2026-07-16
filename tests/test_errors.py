from orcaslicer_mcp.errors import error_from_status, Unauthorized, Validation, Conflict, NotFound


def test_maps_401():
    assert isinstance(error_from_status(401, {"error": "unauthorized"}), Unauthorized)


def test_maps_422_with_errors():
    e = error_from_status(422, {"applied": [], "errors": {"k": "unknown_key"}})
    assert isinstance(e, Validation)
    assert e.errors == {"k": "unknown_key"}


def test_maps_409_and_404():
    assert isinstance(error_from_status(409, {"error": "already_slicing"}), Conflict)
    assert isinstance(error_from_status(404, {"error": "not_found"}), NotFound)
