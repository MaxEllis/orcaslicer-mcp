import json
import httpx, respx
import orcaslicer_mcp.server as srv


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


_PLATE = {"index": 1, "name": "", "current": True, "locked": False, "empty": True,
          "slice_result_valid": False, "origin": [281, 0, 0],
          "bbox_min": [281, 0, 0], "bbox_max": [537, 256, 250], "objects": []}


@respx.mock
async def test_list_plates(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/plates").mock(return_value=httpx.Response(200, json={
        "plates": [dict(_PLATE, index=0, current=True), dict(_PLATE, current=False)],
        "count": 2, "current": 0, "max": 36, "can_add": True, "can_delete": True}))
    out = await srv.list_plates()
    assert out["count"] == 2 and out["current"] == 0
    assert [p["index"] for p in out["plates"]] == [0, 1]


@respx.mock
async def test_list_plates_needs_plates_build(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/plates").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.list_plates()
    assert "fork v2.4.2-mcp.13" in out["error"]


@respx.mock
async def test_add_plate_sends_select(monkeypatch):
    _env(monkeypatch)
    route = respx.post("http://x:13130/api/v1/plates").mock(return_value=httpx.Response(200, json={
        "created": True, "index": 1, "count": 2, "current": 1, "plate": _PLATE}))
    out = await srv.add_plate()
    assert out["created"] is True and out["index"] == 1
    assert json.loads(route.calls.last.request.content) == {"select": True}
    await srv.add_plate(select=False)
    assert json.loads(route.calls.last.request.content) == {"select": False}


@respx.mock
async def test_add_plate_limit_422(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/plates").mock(return_value=httpx.Response(422, json={"error": "max_plates", "max": 36}))
    out = await srv.add_plate()
    assert out["error"] == "max_plates"


@respx.mock
async def test_select_plate(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/plates/1/select").mock(return_value=httpx.Response(200, json={
        "selected": True, "current": 1, "plate": _PLATE}))
    out = await srv.select_plate(1)
    assert out["selected"] is True and out["current"] == 1


@respx.mock
async def test_select_plate_unknown_is_not_a_capability_gap(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/plates/7/select").mock(return_value=httpx.Response(404, json={"error": "unknown_plate", "count": 2}))
    out = await srv.select_plate(7)
    assert out["error"] == "unknown_plate"


@respx.mock
async def test_delete_plate_reports_parked_objects(monkeypatch):
    _env(monkeypatch)
    respx.delete("http://x:13130/api/v1/plates/1").mock(return_value=httpx.Response(200, json={
        "deleted": True, "index": 1, "count": 1, "current": 0, "objects_moved_off_plate": 2}))
    out = await srv.delete_plate(1)
    assert out["deleted"] is True and out["objects_moved_off_plate"] == 2


@respx.mock
async def test_delete_last_plate_422(monkeypatch):
    _env(monkeypatch)
    respx.delete("http://x:13130/api/v1/plates/0").mock(return_value=httpx.Response(422, json={"error": "last_plate"}))
    out = await srv.delete_plate(0)
    assert out["error"] == "last_plate"


@respx.mock
async def test_move_object_to_plate_composes_body(monkeypatch):
    _env(monkeypatch)
    route = respx.post("http://x:13130/api/v1/objects/832/plate").mock(return_value=httpx.Response(200, json={
        "moved": True, "id": 832, "instance": 0, "plate": 1, "from_plate": 0, "offset": [409, 128, 6]}))
    out = await srv.move_object_to_plate(832, 1)
    assert out["moved"] is True and out["plate"] == 1
    assert json.loads(route.calls.last.request.content) == {"plate": 1, "instance": 0}
    await srv.move_object_to_plate(832, 1, instance=2)
    assert json.loads(route.calls.last.request.content) == {"plate": 1, "instance": 2}


@respx.mock
async def test_move_object_slicing_409(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/objects/832/plate").mock(return_value=httpx.Response(409, json={"error": "slicing"}))
    out = await srv.move_object_to_plate(832, 1)
    assert out["error"] == "slicing"
