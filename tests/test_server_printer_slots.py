import json
import httpx, respx
import orcaslicer_mcp.server as srv

U = "http://x:13130/api/v1/printer"


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


_SLOTS = {"supported": True, "connected": True, "active": {"canvas_id": 0, "tray_id": 0},
          "auto_refill": False, "firmware": "1.0.0",
          "trays": [{"canvas_id": 0, "tray_id": 0, "label": "A1", "status": "loaded",
                     "ready": True, "unknown_filament": False, "filament_type": "PLA",
                     "filament_name": "Test PLA", "brand": "Acme",
                     "filament_color": "#112233", "min_nozzle_temp": 190,
                     "max_nozzle_temp": 230}]}


@respx.mock
async def test_get_printer_slots(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{U}/slots").mock(return_value=httpx.Response(200, json=_SLOTS))
    out = await srv.get_printer_slots()
    assert out == _SLOTS


@respx.mock
async def test_get_printer_slots_unsupported_relays_reason(monkeypatch):
    _env(monkeypatch)
    body = {"supported": False, "reason": "no_canvas", "message": "No CANVAS attached."}
    respx.get(f"{U}/slots").mock(return_value=httpx.Response(200, json=body))
    out = await srv.get_printer_slots()
    assert out["supported"] is False and out["reason"] == "no_canvas"


@respx.mock
async def test_slots_route_missing_names_build(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{U}/slots").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    respx.get(f"{U}/slot_mapping").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    assert "fork v2.4.2-mcp.14" in (await srv.get_printer_slots())["error"]
    assert "fork v2.4.2-mcp.14" in (await srv.get_filament_slot_mapping())["error"]


@respx.mock
async def test_get_filament_slot_mapping(monkeypatch):
    _env(monkeypatch)
    body = {"plate": 0, "filaments": [{"index": 0, "type": "PLA", "name": "x", "color": "#000000",
                                       "tray": {"canvas_id": 0, "tray_id": 0, "label": "A1"},
                                       "source": "matched", "warnings": []}],
            "valid": True, "problems": []}
    respx.get(f"{U}/slot_mapping").mock(return_value=httpx.Response(200, json=body))
    assert await srv.get_filament_slot_mapping() == body


@respx.mock
async def test_set_filament_slot_mapping_sends_body(monkeypatch):
    _env(monkeypatch)
    route = respx.put(f"{U}/slot_mapping").mock(return_value=httpx.Response(200, json={
        "plate": 0, "filaments": [], "valid": True, "problems": []}))
    m = [{"filament": 0, "canvas_id": 0, "tray_id": 1}]
    out = await srv.set_filament_slot_mapping(m)
    assert out["valid"] is True
    assert json.loads(route.calls.last.request.content) == {"mapping": m}


@respx.mock
async def test_set_filament_slot_mapping_duplicate_tray_422(monkeypatch):
    _env(monkeypatch)
    respx.put(f"{U}/slot_mapping").mock(return_value=httpx.Response(422, json={
        "error": "duplicate_tray", "entry": {"filament": 1, "canvas_id": 0, "tray_id": 0}}))
    out = await srv.set_filament_slot_mapping([{"filament": 1, "canvas_id": 0, "tray_id": 0}])
    assert "duplicate_tray" in out["error"] and '"filament": 1' in out["error"]


@respx.mock
async def test_duplicate_tray_error_shows_entry(monkeypatch):
    _env(monkeypatch)
    respx.put(f"{U}/slot_mapping").mock(return_value=httpx.Response(422, json={
        "error": "slots_unavailable", "message": "printer busy",
        "entry": {"filament": 1, "canvas_id": 0, "tray_id": 2}}))
    out = await srv.set_filament_slot_mapping([{"filament": 1, "canvas_id": 0, "tray_id": 2}])
    assert '"tray_id": 2' in out["error"] and "printer busy" in out["error"]


def test_slot_timeout_at_least_25_and_keeps_larger(monkeypatch):
    from orcaslicer_mcp.client import OrcaClient
    from orcaslicer_mcp.config import Config
    assert OrcaClient(Config(base_url="http://x", token="t", timeout=5.0))._slot_timeout() >= 25.0
    assert OrcaClient(Config(base_url="http://x", token="t", timeout=60.0))._slot_timeout() == 60.0
