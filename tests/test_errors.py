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


# --- F7: distinguish resource-404 (unknown_object/unknown_preset) from route-404 ---

def test_404_with_resource_error_preserves_message_and_is_not_route_missing():
    e = error_from_status(404, {"error": "unknown_object"})
    assert isinstance(e, NotFound)
    assert str(e) == "unknown_object"
    assert e.route_missing is False


def test_404_route_level_is_route_missing():
    # dispatch fallback body {"error":"not_found"} and empty body both mean
    # "this endpoint doesn't exist on this build"
    for body in ({"error": "not_found"}, {}):
        e = error_from_status(404, body)
        assert isinstance(e, NotFound)
        assert e.route_missing is True
