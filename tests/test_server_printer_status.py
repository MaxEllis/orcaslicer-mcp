import json
import time
from email.utils import formatdate

import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp.printer import target as ptarget
from orcaslicer_mcp.printer.moonraker import MoonrakerClient
from orcaslicer_mcp.printer.snapshot import take_snapshot

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


def printer_routes(q=None, info=INFO, store=None, headers=None):
    respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json=info, headers=headers))
    respx.get(url__startswith=f"{P}/printer/objects/query").mock(
        return_value=httpx.Response(200, json=q or query(), headers=headers))
    respx.get(url__startswith=f"{P}/server/gcode_store").mock(
        return_value=httpx.Response(200, json={"result": {"gcode_store": store or []}}, headers=headers))


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
    # credentials in OrcaSlicer's own address are never used: nothing sent to the printer carries them
    sent = [c.request for c in respx.calls if c.request.url.host == "192.0.2.10"]
    assert sent and all("authorization" not in r.headers for r in sent)


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
    assert out["printer"]["source"] == "override" and out["printer"]["url"] == P


@respx.mock
async def test_an_error_after_the_printer_answered_still_names_it(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    printer_routes()
    respx.get(url__startswith=f"{P}/printer/objects/query").mock(return_value=httpx.Response(500))
    out = await srv.get_printer_status()
    assert out["error"] == "protocol_error"
    assert out["printer"]["source"] == "override" and out["printer"]["kind"] == "klipper"


def _orca_down(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", F)
    respx.get(f"{F}/api/v1/status").mock(side_effect=httpx.ConnectError("refused"))


@respx.mock
async def test_a_remembered_printer_says_so_and_how_long_ago(monkeypatch):
    _orca_down(monkeypatch)
    ptarget.remember(ptarget.PrinterTarget(url=P, source="profile", profile="Test Printer",
                                           host_type="moonraker", kind="klipper"))
    printer_routes()
    out = await srv.get_printer_status()
    assert out["printer"]["source"] == "remembered" and out["state"] == "idle"
    assert any("remembered" in n and "isn't running" in n and "ago" in n for n in out["notes"])
    assert not any("\u2014" in n for n in out["notes"])


@respx.mock
async def test_a_remembered_printer_without_a_timestamp_keeps_other_notes(monkeypatch):
    _orca_down(monkeypatch)
    ptarget.REMEMBERED_PATH.write_text(json.dumps({"url": P, "kind": "klipper"}))
    respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json=INFO))
    respx.get(url__startswith=f"{P}/printer/objects/query").mock(return_value=httpx.Response(200, json=query()))
    respx.get(url__startswith=f"{P}/server/gcode_store").mock(return_value=httpx.Response(500))
    out = await srv.get_printer_status()
    assert out["printer"]["source"] == "remembered"
    assert any("console" in n for n in out["notes"])
    assert any("remembered earlier" in n and "ago" not in n for n in out["notes"])


@respx.mock
async def test_a_garbled_remembered_timestamp_does_not_break_the_reply(monkeypatch):
    _orca_down(monkeypatch)
    ptarget.REMEMBERED_PATH.write_text(json.dumps({"url": P, "kind": "klipper", "found_at": "yesterday"}))
    printer_routes()
    out = await srv.get_printer_status()
    assert out["state"] == "idle" and any("remembered earlier" in n for n in out["notes"])


@pytest.mark.parametrize("bad", ["-Infinity", "Infinity", "NaN"])
@respx.mock
async def test_a_non_finite_remembered_timestamp_reads_as_a_missing_one(monkeypatch, bad):
    _orca_down(monkeypatch)
    ptarget.REMEMBERED_PATH.write_text('{"url": "%s", "kind": "klipper", "found_at": %s}' % (P, bad))
    printer_routes()
    out = await srv.get_printer_status()
    assert out["state"] == "idle" and out["printer"]["source"] == "remembered"
    assert any("remembered earlier" in n and "ago" not in n for n in out["notes"])


@respx.mock
async def test_an_unreachable_remembered_printer_is_named_in_the_error(monkeypatch):
    _orca_down(monkeypatch)
    ptarget.REMEMBERED_PATH.write_text(json.dumps({"url": P, "kind": "klipper"}))
    for url in (f"{P}/server/info", f"{P}:7125/server/info", f"{P}/api/version"):
        respx.get(url).mock(side_effect=httpx.ConnectError("refused"))
    out = await srv.get_printer_status()
    assert out["error"] == "not_reachable" and out["printer"]["source"] == "remembered"


# --- the console is read on the printer's own clock --------------------------------------------

def _dated(t):
    return {"Date": formatdate(t, usegmt=True)}


@respx.mock
async def test_console_errors_use_the_printers_clock_when_it_runs_behind(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    printer_now = time.time() - 3600
    line = {"message": "!! Test error", "time": printer_now - 120, "type": "response"}
    printer_routes(store=[line], headers=_dated(printer_now))
    out = await srv.get_printer_status()
    assert [p["message"] for p in out["problems"] if p["source"] == "console"] == ["Test error"]


@respx.mock
async def test_old_console_errors_stay_old_when_the_printer_clock_runs_ahead(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    printer_now = time.time() + 3600
    line = {"message": "!! Test error", "time": printer_now - 3600, "type": "response"}  # an hour old
    printer_routes(store=[line], headers=_dated(printer_now))
    out = await srv.get_printer_status()
    assert [p for p in out["problems"] if p["source"] == "console"] == []


@respx.mock
async def test_an_explicit_now_beats_the_printers_clock():
    host_now = time.time()
    printer_now = host_now - 3600
    line = {"message": "!! Test error", "time": printer_now - 120, "type": "response"}
    printer_routes(store=[line], headers=_dated(printer_now))
    target = ptarget.PrinterTarget(url=P, source="override", kind="klipper")
    async with MoonrakerClient(P) as c:
        by_host_clock = await take_snapshot(target, c, now=host_now)
        by_printer_clock = await take_snapshot(target, c, now=printer_now)
    assert [p for p in by_host_clock["problems"] if p["source"] == "console"] == []
    assert len([p for p in by_printer_clock["problems"] if p["source"] == "console"]) == 1


# --- file metadata is read fresh each time ------------------------------------------------------

PRINTING = {"state": "printing", "filename": "test-part.gcode", "print_duration": 100.0,
            "total_duration": 110.0, "filament_used": 50.0, "message": "", "info": {}}


def _meta(est):
    return httpx.Response(200, json={"result": {"estimated_time": est, "first_layer_height": 0.2}})


@respx.mock
async def test_a_reuploaded_file_is_measured_against_its_new_estimate(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    printer_routes(query(print_stats=PRINTING))
    meta = respx.get(url__startswith=f"{P}/server/files/metadata").mock(side_effect=[_meta(1000), _meta(2000)])
    first = await srv.get_printer_status()
    second = await srv.get_printer_status()
    assert meta.call_count == 2
    assert first["job"]["remaining_s"] == 900 and first["job"]["remaining_basis"] == "slicer_estimate"
    assert second["job"]["remaining_s"] == 1900 and second["job"]["remaining_basis"] == "slicer_estimate"


@respx.mock
async def test_a_missing_file_record_is_not_remembered(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    printer_routes(query(print_stats=PRINTING))
    respx.get(url__startswith=f"{P}/server/files/metadata").mock(side_effect=[httpx.Response(404), _meta(1000)])
    first = await srv.get_printer_status()
    second = await srv.get_printer_status()
    assert first["job"]["remaining_basis"] != "slicer_estimate"
    assert second["job"]["remaining_s"] == 900 and second["job"]["remaining_basis"] == "slicer_estimate"
