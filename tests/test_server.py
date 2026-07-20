import httpx, respx
import orcaslicer_mcp.server as srv


def _env(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", "http://x:13130")


@respx.mock
async def test_get_status_tool(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/status").mock(
        return_value=httpx.Response(200, json={"app": "OrcaSlicer", "slicing": False}))
    out = await srv.get_status()
    assert out["app"] == "OrcaSlicer"


# --- F12: list_presets filtering (default hides the ~400 system presets) ---

def _presets_payload():
    return {
        "print": [
            {"name": "sys-A", "selected": False, "system": True, "visible": True},
            {"name": "sys-sel", "selected": True, "system": True, "visible": True},
            {"name": "user-B", "selected": False, "system": False, "visible": True},
        ],
        "filament": [{"name": "sys-F", "selected": False, "system": True, "visible": True}],
        "printer": [{"name": "user-P", "selected": True, "system": False, "visible": True}],
    }


@respx.mock
async def test_list_presets_default_hides_system_keeps_selected(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/presets").mock(
        return_value=httpx.Response(200, json=_presets_payload()))
    out = await srv.list_presets()
    assert {p["name"] for p in out["print"]} == {"sys-sel", "user-B"}  # user + selected system
    assert out["filament"] == []                                       # lone system filament dropped
    assert {p["name"] for p in out["printer"]} == {"user-P"}
    assert out["hidden_system"] == 2                                   # sys-A + sys-F


@respx.mock
async def test_list_presets_include_system_returns_all(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/presets").mock(
        return_value=httpx.Response(200, json=_presets_payload()))
    out = await srv.list_presets(include_system=True)
    assert {p["name"] for p in out["print"]} == {"sys-A", "sys-sel", "user-B"}
    assert {p["name"] for p in out["filament"]} == {"sys-F"}
    assert out["hidden_system"] == 0


@respx.mock
async def test_list_presets_type_filter(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/presets").mock(
        return_value=httpx.Response(200, json=_presets_payload()))
    out = await srv.list_presets(type="printer")
    assert set(out) == {"printer", "hidden_system"}
    assert {p["name"] for p in out["printer"]} == {"user-P"}


async def test_list_presets_invalid_type_errors(monkeypatch):
    _env(monkeypatch)
    out = await srv.list_presets(type="bogus")
    assert out["error"] == "invalid_type"


@respx.mock
async def test_set_config_validation_returns_errors(monkeypatch):
    _env(monkeypatch)
    respx.put("http://x:13130/api/v1/config").mock(
        return_value=httpx.Response(422, json={"applied": [], "errors": {"k": "unknown_key"}}))
    out = await srv.set_config({"k": 1})
    assert out["errors"] == {"k": "unknown_key"}


@respx.mock
async def test_get_config_tool(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/config").mock(
        return_value=httpx.Response(200, json={"config": {"layer_height": 0.2}}))
    out = await srv.get_config()
    assert out["config"] == {"layer_height": 0.2}


@respx.mock
async def test_slice_tool(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/slice").mock(
        return_value=httpx.Response(200, json={"started": True}))
    out = await srv.slice()
    assert out["started"] is True


@respx.mock
async def test_get_slice_status_tool(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/slice/status").mock(
        return_value=httpx.Response(200, json={"state": "done", "percent": 100}))
    out = await srv.get_slice_status()
    assert out["state"] == "done"
    assert out["percent"] == 100


@respx.mock
async def test_get_status_not_reachable_returns_error(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/status").mock(side_effect=httpx.ConnectError("boom"))
    out = await srv.get_status()
    assert "error" in out


async def test_missing_token_returns_error_dict(monkeypatch):
    monkeypatch.delenv("ORCA_API_TOKEN", raising=False)
    out = await srv.get_status()
    assert "error" in out
