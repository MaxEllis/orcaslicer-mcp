"""take_snapshot: the requests it makes and how it turns a failed secondary one into a note.
Everything goes through respx; the addresses and names are invented."""
import json

import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp.printer.errors import PrinterError
from orcaslicer_mcp.printer.moonraker import MoonrakerClient
from orcaslicer_mcp.printer.octoprint import OctoPrintClient
from orcaslicer_mcp.printer.snapshot import IDLE_CONSOLE_WINDOW_S, take_snapshot
from orcaslicer_mcp.printer.target import PrinterTarget

P = "http://192.0.2.10"
KL = PrinterTarget(url=P, source="override", kind="klipper")
OP = PrinterTarget(url=P, source="override", kind="octoprint")
NOW = 1_000_000.0  # the printer's clock, passed in so no test depends on the wall clock
INFO = {"result": {"klippy_state": "ready", "klippy_connected": True, "warnings": [], "failed_components": []}}


def status(print_stats=None):
    return {"result": {"eventtime": 1.0, "status": {
        "webhooks": {"state": "ready", "state_message": "Printer is ready"},
        "print_stats": print_stats or {"state": "standby", "filename": "", "print_duration": 0.0,
                                       "total_duration": 0.0, "filament_used": 0.0, "message": "", "info": {}},
        "extruder": {"temperature": 215.0, "target": 215.0},
        "heater_bed": {"temperature": 60.0, "target": 60.0},
        "configfile": {"warnings": []}}}}


def console_line(message, ago):
    return {"message": message, "time": NOW - ago, "type": "response"}


def klipper_routes(print_stats=None, store=(), info=INFO):
    respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json=info))
    objects = respx.get(url__startswith=f"{P}/printer/objects/query").mock(
        return_value=httpx.Response(200, json=status(print_stats)))
    respx.get(url__startswith=f"{P}/server/gcode_store").mock(
        return_value=httpx.Response(200, json={"result": {"gcode_store": list(store)}}))
    return objects


def console_messages(snap):
    return [p["message"] for p in snap["problems"] if p["source"] == "console"]


# --- an active Klipper print ------------------------------------------------------------------------

def active(state):
    return {"state": state, "filename": "test-part.gcode", "print_duration": 100.0,
            "total_duration": 110.0, "filament_used": 50.0, "message": "", "info": {}}


@pytest.mark.parametrize("state", ["printing", "paused"])
@respx.mock
async def test_an_active_print_reads_the_console_from_the_job_start_and_uses_the_slicer_estimate(state):
    # The job has run for 110 s (total_duration), so the window opens 115 s ago: 5 s of slack for
    # lines stamped just before Klipper started counting.
    store = [console_line("!! Before the job", 300), console_line("!! Just outside the slack", 116),
             console_line("!! Inside the slack", 113), console_line("!! During the job", 100)]
    klipper_routes(active(state), store)
    meta = respx.get(url__startswith=f"{P}/server/files/metadata").mock(return_value=httpx.Response(
        200, json={"result": {"estimated_time": 1000, "first_layer_height": 0.2}}))
    async with MoonrakerClient(P) as c:
        snap = await take_snapshot(KL, c, now=NOW)
    assert console_messages(snap) == ["Inside the slack", "During the job"]
    assert meta.call_count == 1 and meta.calls[0].request.url.params["filename"] == "test-part.gcode"
    job = snap["job"]
    assert job["remaining_s"] == 900 and job["remaining_basis"] == "slicer_estimate"
    assert job["first_layer_height"] == 0.2
    assert "notes" not in snap


@respx.mock
async def test_an_active_print_whose_file_metadata_cannot_be_read_is_a_note_and_falls_back():
    klipper_routes(active("printing"))
    respx.get(url__startswith=f"{P}/server/files/metadata").mock(return_value=httpx.Response(500))
    async with MoonrakerClient(P) as c:
        snap = await take_snapshot(KL, c, now=NOW)
    assert len(snap["notes"]) == 1 and snap["notes"][0].startswith("The slicer's time estimate for this file couldn't be read")
    assert snap["job"]["remaining_basis"] != "slicer_estimate" and snap["state"] == "printing"


# --- no job running: the last ten minutes of the console ----------------------------------------------

@respx.mock
async def test_an_idle_printer_reads_the_console_for_the_last_ten_minutes_only():
    store = [console_line("!! Eleven minutes ago", 660), console_line("!! Exactly ten minutes ago", IDLE_CONSOLE_WINDOW_S),
             console_line("!! Nine minutes ago", 540)]
    klipper_routes(store=store)
    meta = respx.get(url__startswith=f"{P}/server/files/metadata")
    async with MoonrakerClient(P) as c:
        snap = await take_snapshot(KL, c, now=NOW)
    assert console_messages(snap) == ["Exactly ten minutes ago", "Nine minutes ago"]
    assert meta.call_count == 0  # no job, so no file to ask about


@respx.mock
async def test_a_finished_job_is_not_an_active_one_so_the_ten_minute_window_applies():
    done = {**active("complete"), "total_duration": 5000.0}
    klipper_routes(done, [console_line("!! During the long job", 3000), console_line("!! Just now", 60)])
    meta = respx.get(url__startswith=f"{P}/server/files/metadata")
    async with MoonrakerClient(P) as c:
        snap = await take_snapshot(KL, c, now=NOW)
    assert console_messages(snap) == ["Just now"] and meta.call_count == 0
    assert snap["state"] == "finished" and snap["job"]["remaining_s"] is None


# --- Moonraker's own warnings ---------------------------------------------------------------------------

FAILURES = [
    pytest.param({"return_value": httpx.Response(500)}, id="an error status"),
    pytest.param({"return_value": httpx.Response(200, content=b"not json")}, id="a reply that is not JSON"),
    pytest.param({"return_value": httpx.Response(401)}, id="a refused key"),
    pytest.param({"side_effect": httpx.ConnectError("refused")}, id="a dropped connection"),
]


@pytest.mark.parametrize("failure", FAILURES)
@respx.mock
async def test_a_failed_server_info_is_a_note_and_the_status_is_still_read(failure):
    objects = klipper_routes()
    respx.get(f"{P}/server/info").mock(**failure)
    async with MoonrakerClient(P) as c:  # a client that never probed: the snapshot asks itself
        snap = await take_snapshot(KL, c, now=NOW)
    assert len(snap["notes"]) == 1
    assert snap["notes"][0].startswith("Moonraker's own warnings couldn't be read: ")
    assert snap["state"] == "idle" and objects.call_count == 1
    assert not any("\u2014" in n for n in snap["notes"])


@respx.mock
async def test_a_failed_status_query_is_not_a_note_it_fails_the_snapshot():
    klipper_routes()
    respx.get(url__startswith=f"{P}/printer/objects/query").mock(return_value=httpx.Response(500))
    async with MoonrakerClient(P) as c:
        with pytest.raises(PrinterError) as e:
            await take_snapshot(KL, c, now=NOW)
    assert e.value.code == "protocol_error"


# --- OctoPrint ---------------------------------------------------------------------------------------------

OCTO_PRINTER = {"temperature": {"tool0": {"actual": 200.0, "target": 200.0}, "bed": {"actual": 60.0, "target": 60.0}},
                "state": {"text": "Printing", "flags": {"printing": True, "operational": True}}}
OCTO_JOB = {"job": {"file": {"name": "test-part.gcode"}}, "state": "Printing",
            "progress": {"completion": 42.0, "printTime": 600, "printTimeLeft": 900}}


def octo_routes(printer=OCTO_PRINTER, job=OCTO_JOB):
    """printer and job are a JSON body, an httpx.Response, or (job only) mock kwargs for a failure."""
    def mock(route, reply):
        if isinstance(reply, dict) and ("side_effect" in reply or "return_value" in reply):
            return route.mock(**reply)
        return route.mock(return_value=reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply))
    return mock(respx.get(f"{P}/api/printer"), printer), mock(respx.get(f"{P}/api/job"), job)


@respx.mock
async def test_an_octoprint_snapshot_joins_the_printer_and_the_job():
    printer_route, job_route = octo_routes()
    async with OctoPrintClient(P) as c:
        snap = await take_snapshot(OP, c)
    assert snap["state"] == "printing" and snap["printer"] == OP.public()
    assert snap["job"]["file"] == "test-part.gcode" and snap["job"]["progress_percent"] == 42
    assert snap["job"]["remaining_s"] == 900 and snap["job"]["remaining_basis"] == "printer_estimate"
    assert snap["temps"]["nozzle"] == {"actual": 200.0, "target": 200.0}
    assert "notes" not in snap and printer_route.call_count == 1 and job_route.call_count == 1
    assert printer_route.calls[0].request.url.params["exclude"] == "sd,history"


@respx.mock
async def test_octoprint_not_connected_to_the_printer_is_an_offline_snapshot():
    octo_routes(printer=httpx.Response(409))
    async with OctoPrintClient(P) as c:
        snap = await take_snapshot(OP, c)
    assert snap["state"] == "offline" and snap["connected"] is False
    assert snap["problems"][0]["severity"] == "fatal"


# A refused key is not "job details that can't be read": it must surface (see the auth tests below).
JOB_FAILURES = [f for f in FAILURES if f.id != "a refused key"]


@pytest.mark.parametrize("failure", JOB_FAILURES)
@respx.mock
async def test_octoprint_job_details_that_cannot_be_read_are_a_note_not_a_failed_snapshot(failure):
    octo_routes(job=failure)
    async with OctoPrintClient(P) as c:
        snap = await take_snapshot(OP, c)
    assert snap["state"] == "printing"  # what the printer endpoint said still comes through
    assert snap["temps"]["nozzle"]["actual"] == 200.0 and snap["job"] is None
    assert len(snap["notes"]) == 1 and snap["notes"][0].startswith("The current job couldn't be read: ")
    assert not any("\u2014" in n for n in snap["notes"])
    assert snap["headline"].startswith("Printing a job.")


@respx.mock
async def test_octoprint_printer_state_that_cannot_be_read_still_fails_the_snapshot():
    octo_routes(printer=httpx.Response(500))
    async with OctoPrintClient(P) as c:
        with pytest.raises(PrinterError) as e:
            await take_snapshot(OP, c)
    assert e.value.code == "protocol_error"


# --- OctoPrint: a refused key always surfaces, whichever request it came on ----------------------------------------

AUTH_CASES = [(401, None, "auth_required"), (403, None, "auth_required"),
              (401, "test-key", "auth_rejected"), (403, "test-key", "auth_rejected")]


@pytest.mark.parametrize("status,key,code", AUTH_CASES)
@respx.mock
async def test_octoprint_printer_endpoint_auth_failures_keep_their_code(status, key, code):
    octo_routes(printer=httpx.Response(status))
    async with OctoPrintClient(P, api_key=key) as c:
        with pytest.raises(PrinterError) as e:
            await c.printer()
        assert e.value.code == code
        with pytest.raises(PrinterError) as e:  # and the snapshot does not turn it into something softer
            await take_snapshot(OP, c)
    assert e.value.code == code and e.value.hint


@pytest.mark.parametrize("status,key,code", AUTH_CASES)
@respx.mock
async def test_octoprint_job_only_auth_failure_is_not_swallowed_into_a_note(status, key, code):
    octo_routes(job=httpx.Response(status))  # the printer endpoint answers; only /api/job is refused
    async with OctoPrintClient(P, api_key=key) as c:
        with pytest.raises(PrinterError) as e:
            await take_snapshot(OP, c)
    assert e.value.code == code
    assert e.value.hint and "API key" in e.value.hint + e.value.message


@pytest.mark.parametrize("status,key,code", AUTH_CASES)
@respx.mock
async def test_get_printer_status_returns_the_job_only_auth_error_with_its_hint(monkeypatch, status, key, code):
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    if key:
        monkeypatch.setenv("ORCA_PRINTER_API_KEY", key)
    for url in (f"{P}/server/info", f"{P}:7125/server/info"):
        respx.get(url).mock(return_value=httpx.Response(404))
    respx.get(f"{P}/api/version").mock(return_value=httpx.Response(200, json={"api": "0.1", "server": "1.10.2"}))
    octo_routes(job=httpx.Response(status))
    out = await srv.get_printer_status()
    assert out["error"] == code and out["hint"] and "notes" not in out
    assert out["printer"]["kind"] == "octoprint"


# --- OctoPrint replies of the wrong JSON shape ---------------------------------------------------------------------

@pytest.mark.parametrize("body", [[1], "text", 5, True], ids=["list", "text", "number", "bool"])
@respx.mock
async def test_an_octoprint_printer_reply_of_the_wrong_shape_fails_the_snapshot_cleanly(body):
    octo_routes(printer=httpx.Response(200, content=json.dumps(body)))
    async with OctoPrintClient(P) as c:
        with pytest.raises(PrinterError) as e:  # a PrinterError, not a raw AttributeError
            await take_snapshot(OP, c)
    assert e.value.code == "protocol_error" and "/api/printer" in e.value.message


@pytest.mark.parametrize("body", [[1], "text", 5, True, None], ids=["list", "text", "number", "bool", "null"])
@respx.mock
async def test_an_octoprint_job_reply_of_the_wrong_shape_is_a_note(body):
    octo_routes(job=httpx.Response(200, content=json.dumps(body)))
    async with OctoPrintClient(P) as c:
        snap = await take_snapshot(OP, c)
    assert snap["state"] == "printing" and snap["job"] is None
    assert len(snap["notes"]) == 1 and snap["notes"][0].startswith("The current job couldn't be read: ")
    assert "/api/job" in snap["notes"][0]


@respx.mock
async def test_get_printer_status_on_octoprint_returns_the_note_through_the_tool(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    for url in (f"{P}/server/info", f"{P}:7125/server/info"):
        respx.get(url).mock(return_value=httpx.Response(404))
    respx.get(f"{P}/api/version").mock(return_value=httpx.Response(200, json={"api": "0.1", "server": "1.10.2"}))
    octo_routes(job={"return_value": httpx.Response(500)})
    out = await srv.get_printer_status()
    assert out["printer"]["kind"] == "octoprint" and out["state"] == "printing"
    assert any("current job couldn't be read" in n for n in out["notes"])
