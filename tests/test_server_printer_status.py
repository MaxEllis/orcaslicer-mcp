import json

import httpx
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp.printer import target as ptarget

P = "http://192.0.2.10"
F = "http://x:13130"
INFO = {"result": {"klippy_state": "ready", "klippy_connected": True, "warnings": [], "failed_components": []}}
MCU_LOST = ("Lost communication with MCU 'mcu'\nOnce the underlying issue is corrected, use the\n"
            "\"FIRMWARE_RESTART\" command to reset the firmware, reload the\n"
            "config, and restart the host software.\nPrinter is shutdown\n")


def query(webhooks=None, print_stats=None):
    return {"result": {"eventtime": 1.0, "status": {
        "webhooks": webhooks or {"state": "ready", "state_message": "Printer is ready"},
        "print_stats": print_stats or {"state": "standby", "filename": "", "print_duration": 0.0,
                                       "total_duration": 0.0, "filament_used": 0.0, "message": "", "info": {}},
        "extruder": {"temperature": 24.9, "target": 0.0},
        "heater_bed": {"temperature": 23.1, "target": 0.0},
        "configfile": {"warnings": []}}}}


def printer_routes(q=None, info=INFO):
    respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json=info))
    respx.get(url__startswith=f"{P}/printer/objects/query").mock(return_value=httpx.Response(200, json=q or query()))
    respx.get(url__startswith=f"{P}/server/gcode_store").mock(
        return_value=httpx.Response(200, json={"result": {"gcode_store": []}}))


@respx.mock
async def test_status_through_the_override(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    printer_routes()
    out = await srv.get_printer_status()
    assert out["state"] == "idle" and out["printer"]["source"] == "override" and out["printer"]["kind"] == "klipper"
    assert out["headline"].startswith("Idle.")


@respx.mock
async def test_status_found_through_the_orcaslicer_profile(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", F)
    respx.get(f"{F}/api/v1/status").mock(return_value=httpx.Response(200, json={"presets": {"printer": "Test Printer"}}))
    respx.post(f"{F}/api/v1/preset/config").mock(return_value=httpx.Response(200, json={
        "name": "Test Printer", "system": False,
        "config": {"print_host": "http://test-user:pw-secret@192.0.2.10", "host_type": "moonraker",
                   "printhost_apikey": "not-for-the-model"}}))
    printer_routes()
    out = await srv.get_printer_status()
    assert out["printer"]["source"] == "profile" and out["printer"]["profile"] == "Test Printer"
    assert out["printer"]["url"] == P
    dumped = json.dumps(out)
    assert "not-for-the-model" not in dumped
    assert "pw-secret" not in dumped and "test-user" not in dumped
    assert ptarget.recall_remembered().url == P
    assert "pw-secret" not in ptarget.REMEMBERED_PATH.read_text()


@respx.mock
async def test_shutdown_comes_back_fatal_with_its_hint(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    printer_routes(query(webhooks={"state": "shutdown", "state_message": MCU_LOST}))
    out = await srv.get_printer_status()
    assert out["state"] == "shutdown"
    assert out["problems"][0]["severity"] == "fatal" and "switched off" in out["problems"][0]["hint"]


@respx.mock
async def test_klipper_disconnected_skips_the_objects_query(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json={"result": {
        "klippy_state": "disconnected", "klippy_connected": False, "warnings": []}}))
    objects = respx.get(url__startswith=f"{P}/printer/objects/query")
    respx.get(url__startswith=f"{P}/server/gcode_store").mock(
        return_value=httpx.Response(200, json={"result": {"gcode_store": []}}))
    out = await srv.get_printer_status()
    assert out["state"] == "offline" and objects.call_count == 0


@respx.mock
async def test_a_missing_console_log_is_a_note_not_a_failure(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json=INFO))
    respx.get(url__startswith=f"{P}/printer/objects/query").mock(return_value=httpx.Response(200, json=query()))
    respx.get(url__startswith=f"{P}/server/gcode_store").mock(return_value=httpx.Response(500))
    out = await srv.get_printer_status()
    assert out["state"] == "idle" and any("console" in n for n in out["notes"])


@respx.mock
async def test_an_unreachable_printer_is_a_structured_error(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    for url in (f"{P}/server/info", f"{P}:7125/server/info", f"{P}/api/version"):
        respx.get(url).mock(side_effect=httpx.ConnectError("refused"))
    out = await srv.get_printer_status()
    assert out["error"] == "not_reachable" and len(out["tried"]) == 3 and out["hint"]
