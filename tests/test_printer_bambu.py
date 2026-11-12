"""A Bambu Lab printer read through OrcaSlicer: the fork's status reply as a snapshot (status.bambu_snapshot),
the wait predicates on it, the client's errors, and the history refusal. The replies are invented."""
import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp.printer import wait as w
from orcaslicer_mcp.printer.bambu import BambuClient
from orcaslicer_mcp.printer.errors import PrinterError
from orcaslicer_mcp.printer.history import print_history
from orcaslicer_mcp.printer.snapshot import take_snapshot
from orcaslicer_mcp.printer.status import bambu_hms_wiki, bambu_snapshot, bambu_state, headline
from orcaslicer_mcp.printer.target import PrinterTarget

BB = PrinterTarget(url="", source="profile", profile="Bambu Lab P1S 0.4 nozzle", host_type="bambu",
                   printer_model="Bambu Lab P1S", kind="bambu")
PUB = BB.public()
F = "http://x:13130"


def reply(state="IDLE", stage=0, stage_text="Printing", nozzle=(25, 0), bed=(23, 0), chamber=None,
          name="benchy", progress=0, remaining=0, layer=(0, 0), print_error=None, hms=(), age=1.2, fans=None):
    return {"supported": True, "kind": "bambu", "connected": True,
            "printer": {"name": "P1S", "model": "Bambu Lab P1S", "connection": "lan"},
            "updated_s_ago": age, "state": state, "stage": stage, "stage_text": stage_text, "stage_remaining_s": 0,
            "job": {"name": name, "file": f"{name}.gcode.3mf", "progress_percent": progress, "remaining_s": remaining,
                    "layer": {"current": layer[0], "total": layer[1]} if layer else {"current": None, "total": None},
                    "plate_index": 0, "source": "local"},
            "print_error": print_error,
            "temps": {"nozzle": {"actual": nozzle[0], "target": nozzle[1]},
                      "nozzles": [{"extruder": 0, "actual": nozzle[0], "target": nozzle[1], "diameter": 0.4}],
                      "bed": {"actual": bed[0], "target": bed[1]},
                      "chamber": None if chamber is None else {"actual": chamber[0], "target": chamber[1]}},
            "fans": fans or {"part": 0, "aux": 0, "chamber": 0}, "speed_percent": 100, "hms": list(hms)}


def snap(**kw):
    return bambu_snapshot(reply(**kw), target_public=PUB)


# --- states -----------------------------------------------------------------------------------------------------

def test_idle_keeps_the_last_job_out_of_the_way():
    s = snap(state="IDLE", name="benchy")
    assert s["state"] == "idle" and s["job"] is None and s["connected"] is True
    assert s["headline"] == "Idle. Nozzle 25 °C, bed 23 °C. No problems."
    assert s["printer"] == PUB and s["bambu"]["gcode_state"] == "IDLE" and s["bambu"]["connection"] == "lan"


def test_prepare_is_preparing():
    s = snap(state="PREPARE", stage=0)
    assert s["state"] == "preparing" and s["job"]["state"] == "preparing"
    assert s["headline"].startswith("Preparing to print benchy.")


@pytest.mark.parametrize("stage, text", [(2, "Heatbed preheating"), (7, "Heating nozzle")])
def test_running_in_a_heating_stage_is_heating(stage, text):
    s = snap(state="RUNNING", stage=stage, stage_text=text, nozzle=(120, 255), bed=(40, 90))
    assert s["state"] == "heating" and s["headline"].startswith("Heating up to print benchy.")


def test_running_below_target_before_the_first_layer_is_heating():
    s = snap(state="RUNNING", stage=1, stage_text="Auto bed leveling", nozzle=(140, 255), bed=(60, 90))
    assert s["state"] == "heating"


def test_running_at_temperature_in_a_prep_stage_is_printing_with_the_stage_named():
    s = snap(state="RUNNING", stage=1, stage_text="Auto bed leveling", nozzle=(255, 255), bed=(90, 90),
             progress=0, remaining=2520, layer=(0, 150))
    assert s["state"] == "printing" and s["job"]["stage"] == "Auto bed leveling"
    assert s["headline"].startswith("Printing benchy: 0%, about 42 min left, auto bed leveling.")


def test_running_mid_print_reports_layer_and_the_printers_own_time_left():
    s = snap(state="RUNNING", nozzle=(255, 255), bed=(90, 90), progress=42, remaining=2520, layer=(63, 150))
    assert s["state"] == "printing"
    job = s["job"]
    assert job["file"] == "benchy" and job["state"] == "running" and job["progress_percent"] == 42
    assert job["layer"] == {"current": 63, "total": 150} and job["remaining_s"] == 2520
    assert job["remaining_basis"] == "printer_estimate" and job["elapsed_s"] is None and job["filament_used_mm"] is None
    assert s["headline"] == "Printing benchy: 42% (layer 63/150), about 42 min left. Nozzle 255/255 °C, bed 90/90 °C. No problems."


def test_running_reheating_mid_print_stays_printing_in_the_headline():
    s = snap(state="RUNNING", stage=4, stage_text="Changing filament", nozzle=(200, 255), bed=(90, 90),
             progress=42, layer=(63, 150))
    assert s["state"] == "printing" and ", changing filament." in s["headline"]


def test_pause_is_paused_with_the_stage():
    s = snap(state="PAUSE", stage=6, stage_text="Paused (filament ran out)", progress=42, layer=(63, 150))
    assert s["state"] == "paused" and s["job"]["state"] == "paused" and s["job"]["remaining_s"] == 0
    assert s["headline"].startswith("Paused benchy: 42% (layer 63/150).")


def test_finish_is_finished_and_the_job_complete():
    s = snap(state="FINISH", progress=100, layer=(150, 150))
    assert s["state"] == "finished" and s["job"]["state"] == "complete"
    assert s["headline"].startswith("Finished benchy.")


def test_failed_without_a_print_error_is_cancelled():
    s = snap(state="FAILED", progress=42)
    assert s["state"] == "cancelled" and s["job"]["state"] == "cancelled" and s["problems"] == []
    assert s["headline"].startswith("Cancelled benchy.")


def test_failed_with_a_print_error_is_an_error_in_the_printers_words():
    s = snap(state="FAILED", progress=42, print_error={"value": 50331905, "code": "0300-0101"})
    assert s["state"] == "error" and s["job"]["state"] == "error"
    assert s["problems"] == [{"severity": "error", "source": "job", "at": None, "hint": None,
                              "message": "The print stopped with printer error 0300-0101."}]
    assert s["headline"].startswith("Stopped with an error while printing benchy.")
    assert "Problem: The print stopped with printer error 0300-0101" in s["headline"]


def test_an_unknown_gcode_state_is_idle():
    assert bambu_state("", None, None, None, None, None) == "idle"
    assert bambu_state("SOMETHING_NEW", 0, None, None, None, None) == "idle"


def test_slicing_counts_as_preparing():
    assert bambu_state("SLICING", 0, None, None, None, None) == "preparing"


# --- extras: chamber, HMS, staleness, offline -----------------------------------------------------------------

def test_chamber_temperature_is_reported_when_the_printer_has_one():
    s = snap(chamber=(35.5, 0))
    assert s["temps"]["chamber"] == {"actual": 35.5, "target": 0.0}
    assert snap()["temps"]["chamber"] is None


def test_hms_items_become_problems_with_a_wiki_link():
    s = snap(hms=[{"code": "0700200000020001", "level": "common", "read": False},
                  {"code": "0300010000010002", "level": "fatal", "read": False},
                  {"code": "", "level": "info"}])
    assert [p["severity"] for p in s["problems"]] == ["warning", "fatal"]
    assert s["problems"][0]["message"] == "HMS 0700200000020001: a common AMS fault the printer reports."
    assert s["problems"][0]["hint"] == ("Look the code up at "
                                        "https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/0700_2000_0002_0001")
    assert s["problems"][1]["message"].startswith("HMS 0300010000010002: a fatal motion controller fault")
    assert s["headline"].endswith("Problem: HMS 0300010000010002: a fatal motion controller fault the printer reports.")


def test_hms_wiki_needs_a_full_code():
    assert bambu_hms_wiki("0700200000020001") == "https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/0700_2000_0002_0001"
    assert bambu_hms_wiki("0700-2000") is None and bambu_hms_wiki("") is None


def test_a_quiet_connection_is_a_warning_not_a_state():
    s = snap(state="RUNNING", nozzle=(255, 255), bed=(90, 90), progress=42, layer=(63, 150), age=95.0)
    assert s["state"] == "printing" and s["connected"] is True
    assert s["problems"][0]["severity"] == "warning"
    assert s["problems"][0]["message"] == "OrcaSlicer last heard from the printer 2 min ago: this may be stale."
    assert snap(age=29.9)["problems"] == []


def test_printer_offline_is_the_offline_state_with_the_message_and_a_hint():
    s = bambu_snapshot({"supported": False, "kind": "bambu", "reason": "printer_offline",
                        "message": "P1S is not connected to OrcaSlicer right now."}, target_public=PUB)
    assert s["state"] == "offline" and s["connected"] is False and s["job"] is None
    assert s["problems"][0]["severity"] == "fatal" and s["problems"][0]["source"] == "orcaslicer"
    assert s["problems"][0]["message"] == "P1S is not connected to OrcaSlicer right now."
    assert "Device tab" in s["problems"][0]["hint"]
    assert s["headline"] == "The printer is offline. Problem: P1S is not connected to OrcaSlicer right now."


def test_garbage_in_the_reply_does_not_break_the_snapshot():
    r = reply(state="RUNNING", nozzle=(255, 255), bed=(90, 90), layer=(63, 150))
    r["job"]["layer"] = "nope"; r["stage"] = "x"; r["hms"] = "none"; r["updated_s_ago"] = "soon"; r["temps"]["nozzles"] = 3
    s = bambu_snapshot(r, target_public=PUB)
    assert s["state"] == "printing" and s["job"]["layer"] == {"current": None, "total": None}
    assert s["bambu"]["stage"] is None and s["bambu"]["nozzles"] == [] and s["problems"] == []


# --- wait predicates ----------------------------------------------------------------------------------------------

def test_printing_needs_the_first_layer_on_a_bambu():
    before = snap(state="RUNNING", stage=1, stage_text="Auto bed leveling", nozzle=(255, 255), bed=(90, 90), layer=(0, 150))
    after = snap(state="RUNNING", nozzle=(255, 255), bed=(90, 90), layer=(1, 150))
    assert before["state"] == "printing" and not w.printing(before, "bambu")
    assert w.printing(after, "bambu")


def test_printing_without_layers_falls_back_to_the_stage():
    levelling = snap(state="RUNNING", stage=1, stage_text="Auto bed leveling", nozzle=(255, 255), bed=(90, 90), layer=None)
    printing = snap(state="RUNNING", stage=0, nozzle=(255, 255), bed=(90, 90), layer=None)
    assert not w.printing(levelling, "bambu") and w.printing(printing, "bambu")


def test_first_layer_done_uses_the_layer_counter():
    assert not w.first_layer_done(snap(state="RUNNING", nozzle=(255, 255), bed=(90, 90), layer=(1, 150)))
    assert w.first_layer_done(snap(state="RUNNING", nozzle=(255, 255), bed=(90, 90), layer=(2, 150)))


def test_finished_and_completed_read_the_job_state():
    done = snap(state="FINISH", progress=100, layer=(150, 150))
    gone = snap(state="FAILED", progress=42)
    assert w.finished(done, "printing", "bambu") and w.completed(done, "bambu")
    assert w.finished(gone, "printing", "bambu") and not w.completed(gone, "bambu")


def test_a_fatal_hms_stops_a_wait():
    s = snap(state="RUNNING", nozzle=(255, 255), bed=(90, 90), layer=(63, 150),
             hms=[{"code": "0300010000010002", "level": "fatal"}])
    assert w.stop_reason(s).startswith("HMS 0300010000010002")


# --- the client -----------------------------------------------------------------------------------------------------

def factory(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok"); monkeypatch.setenv("ORCA_API_URL", F)
    return srv._client


@respx.mock
async def test_take_snapshot_reads_the_forks_status_route(monkeypatch):
    route = respx.get(f"{F}/api/v1/printer/status").mock(return_value=httpx.Response(200, json=reply(
        state="RUNNING", nozzle=(255, 255), bed=(90, 90), progress=42, layer=(63, 150))))
    async with BambuClient(factory(monkeypatch)) as c:
        s = await take_snapshot(BB, c)
    assert s["state"] == "printing" and s["printer"]["kind"] == "bambu" and route.call_count == 1


@respx.mock
async def test_a_build_without_the_route_is_an_unsupported_connection(monkeypatch):
    respx.get(f"{F}/api/v1/printer/status").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    with pytest.raises(PrinterError) as e:
        await BambuClient(factory(monkeypatch)).status()
    assert e.value.code == "unsupported_connection" and "bambu_status" in e.value.message
    assert e.value.details["host_type"] == "bambu"


@respx.mock
async def test_orcaslicer_down_is_orca_unreachable(monkeypatch):
    respx.get(f"{F}/api/v1/printer/status").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(PrinterError) as e:
        await BambuClient(factory(monkeypatch)).status()
    assert e.value.code == "orca_unreachable" and "Device tab" in e.value.hint


@respx.mock
async def test_no_printer_selected_is_not_configured_with_the_device_tab_hint(monkeypatch):
    respx.get(f"{F}/api/v1/printer/status").mock(return_value=httpx.Response(200, json={
        "supported": False, "kind": "bambu", "reason": "no_printer_selected",
        "message": "No Bambu Lab printer is selected in OrcaSlicer's Device tab."}))
    with pytest.raises(PrinterError) as e:
        await BambuClient(factory(monkeypatch)).status()
    assert e.value.code == "not_configured" and "Device tab" in e.value.message and "Device tab" in e.value.hint


@respx.mock
async def test_a_not_bambu_answer_from_the_route_is_unsupported(monkeypatch):
    respx.get(f"{F}/api/v1/printer/status").mock(return_value=httpx.Response(200, json={
        "supported": False, "kind": "bambu", "reason": "not_supported", "message": "not a Bambu Lab one"}))
    with pytest.raises(PrinterError) as e:
        await BambuClient(factory(monkeypatch)).status()
    assert e.value.code == "unsupported_connection" and e.value.message == "not a Bambu Lab one"


@respx.mock
async def test_printer_offline_comes_back_as_a_reply_not_an_error(monkeypatch):
    respx.get(f"{F}/api/v1/printer/status").mock(return_value=httpx.Response(200, json={
        "supported": False, "kind": "bambu", "reason": "printer_offline", "message": "P1S is not connected"}))
    async with BambuClient(factory(monkeypatch)) as c:
        s = await take_snapshot(BB, c)
    assert s["state"] == "offline"


@respx.mock
async def test_a_non_object_reply_is_a_protocol_error(monkeypatch):
    respx.get(f"{F}/api/v1/printer/status").mock(return_value=httpx.Response(200, json=[1]))
    with pytest.raises(PrinterError) as e:
        await BambuClient(factory(monkeypatch)).status()
    assert e.value.code == "protocol_error"


async def test_history_is_refused_with_a_pointer_to_the_local_store():
    out = await print_history(BB, None, 10, "p1s")
    assert out["supported"] is False and "Bambu" in out["reason"] and "recall_prints" in out["hint"]
    assert out["printer"]["kind"] == "bambu"
