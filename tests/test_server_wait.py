import httpx, respx
import orcaslicer_mcp.server as srv


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


@respx.mock
async def test_apply_and_slice_composes(monkeypatch):
    _env(monkeypatch)
    respx.put("http://x:13130/api/v1/config").mock(
        return_value=httpx.Response(200, json={"applied": ["layer_height"], "errors": {}}))
    respx.post("http://x:13130/api/v1/slice").mock(
        return_value=httpx.Response(200, json={"already_valid": True, "started": False}))
    respx.get("http://x:13130/api/v1/slice/status").mock(
        return_value=httpx.Response(200, json={"state": "done", "percent": 100,
                                               "message": "", "stats": {"total_cost": 0.1}, "warnings": []}))
    # collect_events returns [] instantly (no WS server) -> falls back to slice_status
    monkeypatch.setattr(srv.OrcaClient, "collect_events",
                        lambda self, seconds, stop_on=None: _empty())
    out = await srv.apply_and_slice({"layer_height": 0.28})
    assert out["applied"] == ["layer_height"]
    assert out["result"]["state"] == "done"

async def _empty():
    return []


@respx.mock
async def test_slice_and_wait_polls_until_done(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/slice").mock(
        return_value=httpx.Response(202, json={"started": True}))
    respx.get("http://x:13130/api/v1/slice/status").mock(
        side_effect=[
            httpx.Response(200, json={"state": "slicing", "percent": 50}),
            httpx.Response(200, json={"state": "done", "percent": 100,
                                      "message": "", "stats": {"total_cost": 0.1}, "warnings": []}),
        ])
    out = await srv.slice_and_wait(timeout=5)
    assert out["state"] == "done"


@respx.mock
async def test_apply_and_slice_skips_wait_when_already_valid(monkeypatch):
    _env(monkeypatch)
    respx.put("http://x:13130/api/v1/config").mock(
        return_value=httpx.Response(200, json={"applied": ["layer_height"], "errors": {}}))
    respx.post("http://x:13130/api/v1/slice").mock(
        return_value=httpx.Response(200, json={"already_valid": True, "started": False}))
    respx.get("http://x:13130/api/v1/slice/status").mock(
        return_value=httpx.Response(200, json={"state": "done", "percent": 100,
                                               "message": "", "stats": {"total_cost": 0.1}, "warnings": []}))

    async def _should_not_be_called(self, seconds, stop_on=None):
        raise AssertionError("collect_events should not be called when already_valid")

    monkeypatch.setattr(srv.OrcaClient, "collect_events", _should_not_be_called)
    out = await srv.apply_and_slice({"layer_height": 0.28})
    assert out["applied"] == ["layer_height"]
    assert out["result"]["state"] == "done"


# --- F1 regression: _wait_for_slice must wait for a TERMINAL state, not just "not slicing" ---

@respx.mock
async def test_slice_and_wait_does_not_return_early_on_nonterminal_state(monkeypatch):
    # First poll catches an intermediate/unknown state (e.g. "starting"): the old
    # `while state == "slicing"` loop returned it as final -> stale result (F1).
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/slice").mock(
        return_value=httpx.Response(202, json={"started": True}))
    respx.get("http://x:13130/api/v1/slice/status").mock(
        side_effect=[
            httpx.Response(200, json={"state": "starting", "percent": 0}),
            httpx.Response(200, json={"state": "slicing", "percent": 40}),
            httpx.Response(200, json={"state": "done", "percent": 100,
                                      "message": "", "stats": {"total_cost": 0.1}, "warnings": []}),
        ])
    async def _nosleep(_): pass
    monkeypatch.setattr(srv.asyncio, "sleep", _nosleep)
    out = await srv.slice_and_wait(timeout=5)
    assert out["state"] == "done"


@respx.mock
async def test_slice_and_wait_treats_idle_as_terminal_cancelled(monkeypatch):
    # fork sets state="idle" (message "cancelled") when a slice is cancelled;
    # the waiter must return it rather than poll until timeout.
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/slice").mock(
        return_value=httpx.Response(202, json={"started": True}))
    respx.get("http://x:13130/api/v1/slice/status").mock(
        side_effect=[
            httpx.Response(200, json={"state": "slicing", "percent": 10}),
            httpx.Response(200, json={"state": "idle", "percent": -1, "message": "cancelled"}),
        ])
    async def _nosleep(_): pass
    monkeypatch.setattr(srv.asyncio, "sleep", _nosleep)
    out = await srv.slice_and_wait(timeout=5)
    assert out["state"] == "idle"


# --- F6 regression: set_layer_height must accept the documented 'default' spelling ---

@respx.mock
async def test_set_layer_height_default_aliases_to_reset(monkeypatch):
    # The fork only knows adaptive|reset; 'default' (and 'none') were documented
    # as the clear path and returned unknown_mode (F6). Alias them to 'reset'.
    _env(monkeypatch)
    route = respx.put("http://x:13130/api/v1/objects/7/layer_height").mock(
        return_value=httpx.Response(200, json={"id": 7, "mode": "reset"}))
    import json as _json
    out = await srv.set_layer_height(7, "default")
    assert _json.loads(route.calls.last.request.read()) == {"mode": "reset"}
    assert out["mode"] == "reset"
    out = await srv.set_layer_height(7, "none")
    assert _json.loads(route.calls.last.request.read()) == {"mode": "reset"}


# --- F7 regression: resource 404s must NOT read as "needs M4x build" ---

@respx.mock
async def test_bad_object_id_is_not_reported_as_capability_missing(monkeypatch):
    _env(monkeypatch)
    respx.put("http://x:13130/api/v1/objects/9999/layer_height").mock(
        return_value=httpx.Response(404, json={"error": "unknown_object"}))
    out = await srv.set_layer_height(9999, "adaptive")
    assert "needs M4" not in out["error"]
    assert "unknown_object" in out["error"]


@respx.mock
async def test_missing_route_still_reports_capability_missing(monkeypatch):
    _env(monkeypatch)
    respx.put("http://x:13130/api/v1/objects/7/layer_height").mock(
        return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.set_layer_height(7, "adaptive")
    assert "needs M4" in out["error"]


# --- F3: lock the rename_preset composite contract (save new -> select new -> delete old) ---

@respx.mock
async def test_rename_preset_deletes_source_after_saving_copy(monkeypatch):
    _env(monkeypatch)
    calls = []
    respx.put("http://x:13130/api/v1/preset").mock(
        side_effect=lambda req: (calls.append(("select", req.read().decode())),
                                 httpx.Response(200, json={"selected": "x"}))[1])
    respx.post("http://x:13130/api/v1/preset/save").mock(
        side_effect=lambda req: (calls.append(("save", req.read().decode())),
                                 httpx.Response(200, json={"created": True, "saved": "new"}))[1])
    respx.delete("http://x:13130/api/v1/preset").mock(
        side_effect=lambda req: (calls.append(("delete", req.read().decode())),
                                 httpx.Response(200, json={"deleted": "old"}))[1])
    out = await srv.rename_preset("print", "old", "new")
    assert out == {"renamed": "old", "to": "new", "deleted": {"deleted": "old"}}
    ops = [op for op, _ in calls]
    # the old preset must be deleted, and only after the copy is saved & selected
    assert "delete" in ops
    assert ops.index("delete") > ops.index("save")
    assert '"old"' in calls[[op for op, _ in calls].index("delete")][1]
