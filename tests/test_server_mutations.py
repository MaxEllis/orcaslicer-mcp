import httpx, respx
import orcaslicer_mcp.server as srv


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


@respx.mock
async def test_delete_object(monkeypatch):
    _env(monkeypatch)
    respx.delete("http://x:13130/api/v1/objects/42").mock(
        return_value=httpx.Response(200, json={"deleted": True, "id": 42, "count": 0}))
    out = await srv.delete_object(42)
    assert out["deleted"] is True and out["count"] == 0


@respx.mock
async def test_delete_object_needs_m4b(monkeypatch):
    _env(monkeypatch)
    respx.delete("http://x:13130/api/v1/objects/42").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.delete_object(42)
    assert "M4b" in out["error"]


@respx.mock
async def test_transform_object_composes_body(monkeypatch):
    _env(monkeypatch)
    route = respx.post("http://x:13130/api/v1/objects/7/transform").mock(
        return_value=httpx.Response(200, json={"id": 7, "transform": {"offset": [10, 0, 0]}}))
    out = await srv.transform_object(7, translate=[10, 0, 0], scale=[2, 2, 2])
    assert out["id"] == 7
    import json as _j
    sent = _j.loads(route.calls.last.request.content)
    assert sent == {"translate": [10, 0, 0], "scale": [2, 2, 2]}  # rotate omitted


@respx.mock
async def test_arrange_and_orient_202(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/arrange").mock(return_value=httpx.Response(202, json={"started": True}))
    respx.post("http://x:13130/api/v1/orient").mock(return_value=httpx.Response(202, json={"started": True}))
    assert (await srv.arrange_plate())["started"] is True
    assert (await srv.auto_orient())["started"] is True


@respx.mock
async def test_arrange_job_running_409(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/arrange").mock(return_value=httpx.Response(409, json={"error": "job_running"}))
    out = await srv.arrange_plate()
    assert "error" in out and "M4b" not in out["error"]  # conflict surfaced, not a capability gap


@respx.mock
async def test_job_status(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/jobs/status").mock(return_value=httpx.Response(200, json={"idle": True}))
    assert (await srv.get_job_status())["idle"] is True


@respx.mock
async def test_duplicate_object(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/objects/5/duplicate").mock(
        return_value=httpx.Response(200, json={"duplicated": True, "id": 5, "instances": 2}))
    out = await srv.duplicate_object(5)
    assert out["duplicated"] is True and out["instances"] == 2


@respx.mock
async def test_set_object_config(monkeypatch):
    _env(monkeypatch)
    respx.put("http://x:13130/api/v1/objects/9/config").mock(
        return_value=httpx.Response(200, json={"applied": ["wall_loops"], "errors": {}, "object": "cube20"}))
    out = await srv.set_object_config(9, {"wall_loops": 4})
    assert out["applied"] == ["wall_loops"] and out["object"] == "cube20"


@respx.mock
async def test_set_object_config_needs_m4c(monkeypatch):
    _env(monkeypatch)
    respx.put("http://x:13130/api/v1/objects/9/config").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.set_object_config(9, {"wall_loops": 4})
    assert "M4c" in out["error"]
