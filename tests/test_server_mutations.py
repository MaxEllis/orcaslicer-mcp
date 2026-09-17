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


# F15: edit_preset must run the physics gate and refuse changes that INTRODUCE a fail
@respx.mock
async def test_edit_preset_blocks_new_physics_fail(monkeypatch):
    _env(monkeypatch)
    respx.put("http://x:13130/api/v1/preset").mock(return_value=httpx.Response(200, json={"selected": True}))
    respx.get("http://x:13130/api/v1/config").mock(return_value=httpx.Response(
        200, json={"config": {"layer_height": "0.2", "nozzle_diameter": "0.4"}}))
    put_cfg = respx.put("http://x:13130/api/v1/config").mock(return_value=httpx.Response(200, json={"applied": ["layer_height"]}))
    save = respx.post("http://x:13130/api/v1/preset/save").mock(return_value=httpx.Response(200, json={"saved": True}))
    out = await srv.edit_preset("print", "P", {"layer_height": 0.9})
    assert out["error"] == "physics_blocked"
    assert any(f["name"] == "layer_height_ratio" for f in out["fails"])
    assert put_cfg.call_count == 0 and save.call_count == 0


@respx.mock
async def test_edit_preset_allows_unrelated_change_on_already_failing_preset(monkeypatch):
    _env(monkeypatch)
    respx.put("http://x:13130/api/v1/preset").mock(return_value=httpx.Response(200, json={"selected": True}))
    respx.get("http://x:13130/api/v1/config").mock(return_value=httpx.Response(
        200, json={"config": {"layer_height": "0.9", "nozzle_diameter": "0.4"}}))
    respx.put("http://x:13130/api/v1/config").mock(return_value=httpx.Response(200, json={"applied": ["retraction_length"]}))
    respx.post("http://x:13130/api/v1/preset/save").mock(return_value=httpx.Response(200, json={"saved": True}))
    out = await srv.edit_preset("print", "P", {"retraction_length": 1})
    assert out.get("error") is None
    assert out["saved"] == {"saved": True}


# --- physics gate is layer-aware (filament edits are not judged against the active print preset) ---

_FAST_PLA = {  # outer wall 200mm/s x 0.45mm x 0.2mm = 16.3mm3/s; PLA@230C sustains ~29 (pass), @205C ~8 (fail)
    "layer_height": "0.2", "nozzle_diameter": "0.4", "line_width": "0.45",
    "outer_wall_speed": "200", "filament_max_volumetric_speed": "20",
    "filament_type": "PLA", "nozzle_temperature": "230",
}

def _mock_preset_roundtrip(cfg, applied):
    respx.put("http://x:13130/api/v1/preset").mock(return_value=httpx.Response(200, json={"selected": True}))
    respx.get("http://x:13130/api/v1/config").mock(return_value=httpx.Response(200, json={"config": cfg}))
    put_cfg = respx.put("http://x:13130/api/v1/config").mock(return_value=httpx.Response(200, json={"applied": applied}))
    save = respx.post("http://x:13130/api/v1/preset/save").mock(return_value=httpx.Response(200, json={"saved": True}))
    return put_cfg, save

@respx.mock
async def test_edit_preset_filament_temp_is_not_blocked_by_active_print_speeds(monkeypatch):
    _env(monkeypatch)
    put_cfg, save = _mock_preset_roundtrip(_FAST_PLA, ["nozzle_temperature"])
    out = await srv.edit_preset("filament", "PLA Basic", {"nozzle_temperature": 205})
    assert out.get("error") is None, out
    assert out["saved"] == {"saved": True} and put_cfg.call_count == 1
    warn = out["cross_layer_warnings"]
    assert [w["name"] for w in warn] == ["temp_vs_flow"]
    assert "print" in warn[0]["hint"]

@respx.mock
async def test_edit_preset_print_speed_is_still_blocked_by_filament_ceiling(monkeypatch):
    _env(monkeypatch)
    put_cfg, save = _mock_preset_roundtrip({**_FAST_PLA, "outer_wall_speed": "100"}, ["outer_wall_speed"])
    out = await srv.edit_preset("print", "Fast", {"outer_wall_speed": 300})
    assert out["error"] == "physics_blocked"
    assert any(f["name"] == "flow_ceiling" for f in out["fails"])
    assert put_cfg.call_count == 0 and save.call_count == 0

@respx.mock
async def test_edit_preset_filament_is_blocked_by_a_filament_only_check(monkeypatch):
    _env(monkeypatch)
    put_cfg, save = _mock_preset_roundtrip({**_FAST_PLA, "fan_min_speed": "20", "fan_max_speed": "100"}, ["fan_min_speed"])
    out = await srv.edit_preset("filament", "PLA Basic", {"fan_min_speed": 150})
    assert out["error"] == "physics_blocked"
    assert any(f["name"] == "cooling_sanity" for f in out["fails"])
    assert put_cfg.call_count == 0 and save.call_count == 0
