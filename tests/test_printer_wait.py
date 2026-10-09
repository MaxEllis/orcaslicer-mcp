import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp.printer import wait as w
from orcaslicer_mcp.printer.errors import PrinterError
from orcaslicer_mcp.printer.target import PrinterTarget

KL = PrinterTarget(url="http://192.0.2.10", source="override", kind="klipper")
OP = PrinterTarget(url="http://192.0.2.10", source="override", kind="octoprint")
FATAL = {"severity": "fatal", "source": "klipper", "message": "Lost communication with MCU 'mcu'"}


def snap(state="idle", nozzle=(25, 0), bed=(23, 0), job=None, problems=(), z=None):
    return {"state": state, "headline": f"state {state}",
            "temps": {"nozzle": {"actual": nozzle[0], "target": nozzle[1]},
                      "bed": {"actual": bed[0], "target": bed[1]}},
            "job": job, "problems": list(problems), "klipper": {"z_mm": z}}


class Script:
    """Feeds take_snapshot a list of snapshots (the last one repeats; an Exception in the list is
    raised instead) and runs a fake clock."""

    def __init__(self, monkeypatch, snaps):
        self.snaps, self.t, self.sleeps, self.reports = list(snaps), 0.0, [], []

        async def take(target, client):
            item = self.snaps.pop(0) if len(self.snaps) > 1 else self.snaps[0]
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(w._snapshot, "take_snapshot", take)

    def clock(self):
        return self.t

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds

    async def report(self, done, total, message):
        self.reports.append((done, total, message))

    async def run(self, target, until, timeout_s=300):
        return await w.run_wait(target, None, until, timeout_s, self.report,
                                poll_s=5.0, sleep=self.sleep, clock=self.clock)


async def test_heated_after_a_poll(monkeypatch):
    s = Script(monkeypatch, [snap("heating", (150, 215), (40, 60)), snap("heating", (214, 215), (60, 60))])
    out = await s.run(KL, "heated")
    assert out["met"] is True and out["waited_s"] == 5 and len(s.reports) == 1


async def test_times_out_with_the_latest_status(monkeypatch):
    s = Script(monkeypatch, [snap("heating", (150, 215), (40, 60))])
    out = await s.run(KL, "heated", timeout_s=12)
    assert out["met"] is False and "timed out" in out["note"] and s.sleeps == [5.0, 5.0, 2.0]


async def test_stops_early_on_a_fault(monkeypatch):
    s = Script(monkeypatch, [snap("heating", (150, 215)), snap("shutdown", problems=[FATAL])])
    out = await s.run(KL, "heated")
    assert out["met"] is False and out["stopped_early"] == "Lost communication with MCU 'mcu'"


async def test_printing_needs_extrusion_on_klipper(monkeypatch):
    s = Script(monkeypatch, [snap("printing", job={"filament_used_mm": 0}), snap("printing", job={"filament_used_mm": 12})])
    assert (await s.run(KL, "printing"))["met"] is True and len(s.sleeps) == 1


async def test_printing_on_octoprint_waits_for_the_heaters_not_for_file_progress(monkeypatch):
    # OctoPrint's completion is a file position, so it is above zero while the start G-code heats.
    job = {"progress_percent": 28}
    s = Script(monkeypatch, [snap("heating", (60, 200), (60, 60), job=job),
                             snap("printing", (200, 200), (60, 60), job=job)])
    out = await s.run(OP, "printing")
    assert out["met"] is True and out["waited_s"] == 5 and len(s.sleeps) == 1


async def test_printing_on_octoprint_is_met_at_once_when_already_printing_at_temperature(monkeypatch):
    s = Script(monkeypatch, [snap("printing", (200, 200), (60, 60), job={"progress_percent": 0})])
    out = await s.run(OP, "printing")
    assert out["met"] is True and s.sleeps == []


async def test_first_layer_done_by_layer_count(monkeypatch):
    s = Script(monkeypatch, [snap("printing", job={"layer": {"current": 1, "total": 80}}),
                             snap("printing", job={"layer": {"current": 2, "total": 80}})])
    assert (await s.run(KL, "first_layer_done"))["met"] is True


NO_LAYERS = {"layer": {"current": None, "total": None}, "first_layer_height": 0.3, "filament_used_mm": 40}


async def test_first_layer_done_by_height_needs_two_polls_in_a_row(monkeypatch):
    s = Script(monkeypatch, [snap("printing", job=NO_LAYERS, z=0.3), snap("printing", job=NO_LAYERS, z=0.6),
                             snap("printing", job=NO_LAYERS, z=0.8)])
    out = await s.run(KL, "first_layer_done")
    assert out["met"] is True and len(s.sleeps) == 2


async def test_first_layer_done_by_height_ignores_z_before_extrusion(monkeypatch):
    job = {**NO_LAYERS, "filament_used_mm": 0}
    s = Script(monkeypatch, [snap("printing", job=job, z=5.0)])
    out = await s.run(KL, "first_layer_done", timeout_s=12)
    assert out["met"] is False and "timed out" in out["note"]


async def test_first_layer_done_by_height_ignores_a_single_z_hop(monkeypatch):
    s = Script(monkeypatch, [snap("printing", job=NO_LAYERS, z=0.3), snap("printing", job=NO_LAYERS, z=0.9),
                             snap("printing", job=NO_LAYERS, z=0.3)])
    out = await s.run(KL, "first_layer_done", timeout_s=12)
    assert out["met"] is False and "timed out" in out["note"]


async def test_first_layer_done_is_klipper_only(monkeypatch):
    s = Script(monkeypatch, [snap("printing")])
    with pytest.raises(PrinterError) as e:
        await s.run(OP, "first_layer_done")
    assert e.value.code == "unsupported_for_connection"


async def test_finished_when_nothing_is_printing(monkeypatch):
    s = Script(monkeypatch, [snap("idle")])
    out = await s.run(KL, "finished")
    assert out["met"] is False and out["note"] == "nothing is printing" and s.sleeps == []


async def test_finished_when_already_done(monkeypatch):
    s = Script(monkeypatch, [snap("finished", job={"state": "complete"})])
    out = await s.run(KL, "finished")
    assert out["met"] is True and out["stopped_early"] is None and s.sleeps == []


async def test_finished_on_klipper_in_an_error_state_without_a_job_is_not_met(monkeypatch):
    s = Script(monkeypatch, [snap("error")])
    out = await s.run(KL, "finished")
    assert out["met"] is False and out["stopped_early"] == "the printer reports an error"
    assert "note" not in out and s.sleeps == []


async def test_finished_by_a_klipper_job_error_still_says_why(monkeypatch):
    s = Script(monkeypatch, [snap("printing", job={"state": "printing"}), snap("error", job={"state": "error"})])
    out = await s.run(KL, "finished")
    assert out["met"] is True and out["stopped_early"] == "the printer reports an error"


async def test_finished_by_a_cancelled_klipper_job_has_no_fault(monkeypatch):
    s = Script(monkeypatch, [snap("printing", job={"state": "printing"}), snap("cancelled", job={"state": "cancelled"})])
    out = await s.run(KL, "finished")
    assert out["met"] is True and out["stopped_early"] is None


async def test_finished_on_octoprint_losing_the_printer_says_why(monkeypatch):
    s = Script(monkeypatch, [snap("printing"), snap("offline", problems=[FATAL])])
    out = await s.run(OP, "finished")
    assert out["met"] is True and out["stopped_early"] == FATAL["message"]


async def test_finished_on_a_shut_down_printer_says_why(monkeypatch):
    s = Script(monkeypatch, [snap("shutdown", problems=[FATAL])])
    out = await s.run(KL, "finished")
    assert out["met"] is False and out["stopped_early"] == FATAL["message"] and "note" not in out


async def test_finished_on_octoprint_after_printing(monkeypatch):
    s = Script(monkeypatch, [snap("printing"), snap("idle")])
    assert (await s.run(OP, "finished"))["met"] is True


async def test_finished_on_octoprint_with_an_old_finished_job_is_not_met(monkeypatch):
    s = Script(monkeypatch, [snap("finished")])
    out = await s.run(OP, "finished")
    assert out["met"] is False and out["note"] == "nothing is printing" and s.sleeps == []


# --- the wait ends when the job ends -----------------------------------------------------------

CANCELLED = {"state": "cancelled"}
JOB_ERROR = {"severity": "error", "source": "job", "message": "Heater extruder not heating at expected rate",
             "hint": None, "at": None}


@pytest.mark.parametrize("until,first,text", [
    ("heated", snap("heating", (150, 215), (40, 60), job={"state": "printing"}),
     "The job ended before the heaters reached their targets."),
    ("printing", snap("heating", (150, 215), (40, 60), job={"state": "printing", "filament_used_mm": 0}),
     "The job ended before extrusion started."),
    ("first_layer_done", snap("printing", job={"state": "printing", "layer": {"current": 1, "total": 80}}),
     "The job ended before the first layer was done."),
])
async def test_a_cancelled_job_ends_the_wait_at_once(monkeypatch, until, first, text):
    s = Script(monkeypatch, [first, snap("cancelled", job=CANCELLED)])
    out = await s.run(KL, until)
    assert out["met"] is False and out["stopped_early"] == text
    assert out["status"]["state"] == "cancelled" and out["waited_s"] == 5
    assert s.sleeps == [5.0] and "note" not in out


async def test_a_cancel_that_clears_the_heater_targets_still_ends_a_heated_wait(monkeypatch):
    # After a cancel the targets drop to zero, so heated() is False for ever: only the job-ended
    # rule can end this wait.
    s = Script(monkeypatch, [snap("heating", (150, 215), (40, 60)), snap("cancelled", (140, 0), (38, 0), job=CANCELLED)])
    out = await s.run(KL, "heated", timeout_s=1800)
    assert out["met"] is False and out["stopped_early"].startswith("The job ended") and s.sleeps == [5.0]


async def test_a_cancelled_octoprint_job_ends_a_printing_wait_too(monkeypatch):
    s = Script(monkeypatch, [snap("heating", (150, 215), (40, 60)), snap("cancelled")])
    out = await s.run(OP, "printing")
    assert out["met"] is False and out["stopped_early"] == "The job ended before extrusion started."


async def test_the_job_ended_text_gives_way_to_the_printers_own_fault_words(monkeypatch):
    s = Script(monkeypatch, [snap("heating", (150, 215)), snap("error", job={"state": "error"}, problems=[JOB_ERROR])])
    out = await s.run(KL, "heated")
    assert out["met"] is False and out["stopped_early"] == JOB_ERROR["message"]


async def test_an_idle_printer_is_not_a_job_that_ended(monkeypatch):
    # The job may simply not have started yet: keep waiting until it has, then wait for the point.
    s = Script(monkeypatch, [snap("idle"), snap("idle"), snap("heating", (150, 215), (40, 60)),
                             snap("heating", (214, 215), (60, 60))])
    out = await s.run(KL, "heated")
    assert out["met"] is True and out["stopped_early"] is None and len(s.sleeps) == 3


async def test_an_old_cancelled_job_does_not_end_a_wait_for_the_next_one(monkeypatch):
    s = Script(monkeypatch, [snap("cancelled", job=CANCELLED), snap("cancelled", job=CANCELLED)])
    out = await s.run(KL, "printing", timeout_s=12)
    assert out["met"] is False and "timed out" in out["note"] and out["stopped_early"] is None


async def test_a_firmware_restart_ends_a_wait_for_finished(monkeypatch):
    # print_stats goes back to standby: Klipper reports no job at all, and "idle" is not "finished".
    s = Script(monkeypatch, [snap("printing", job={"state": "printing"}), snap("idle")])
    out = await s.run(KL, "finished")
    assert out["met"] is False and out["status"]["state"] == "idle" and s.sleeps == [5.0]
    assert out["stopped_early"] == "The job stopped without finishing (the printer is now idle)."
    assert "note" not in out


async def test_a_klipper_job_error_uses_the_jobs_own_words(monkeypatch):
    s = Script(monkeypatch, [snap("printing", job={"state": "printing"}),
                             snap("error", job={"state": "error"}, problems=[JOB_ERROR])])
    out = await s.run(KL, "finished")
    assert out["met"] is True and out["stopped_early"] == JOB_ERROR["message"]


def test_the_jobs_words_beat_a_console_line_but_not_a_fatal_problem():
    console = {"severity": "error", "source": "console", "message": "Move out of range", "hint": None, "at": 1.0}
    job_error = snap("error", job={"state": "error"}, problems=[console, JOB_ERROR])
    assert w.stop_reason(job_error) == JOB_ERROR["message"]
    assert w.stop_reason(snap("error", problems=[console])) == "the printer reports an error"
    assert w.stop_reason(snap("shutdown", problems=[JOB_ERROR, FATAL])) == FATAL["message"]


# --- a job that completes while we wait for it to get going is a success, not "ended before" ----

COMPLETE = {"state": "complete"}


@pytest.mark.parametrize("until,first", [
    ("printing", snap("heating", (150, 215), (40, 60), job={"state": "printing", "filament_used_mm": 0})),
    ("first_layer_done", snap("printing", job={"state": "printing", "layer": {"current": 1, "total": 1}})),
])
async def test_a_klipper_job_that_completes_while_waiting_for_it_to_get_going_is_met(monkeypatch, until, first):
    # A one-layer print: it was running, and the next poll finds it complete.
    s = Script(monkeypatch, [first, snap("finished", job=COMPLETE)])
    out = await s.run(KL, until)
    assert out["met"] is True and out["stopped_early"] is None and "note" not in out
    assert out["status"]["state"] == "finished" and out["waited_s"] == 5 and s.sleeps == [5.0]


async def test_an_octoprint_job_that_runs_to_the_end_while_waiting_for_printing_is_met(monkeypatch):
    s = Script(monkeypatch, [snap("heating", (150, 215), (40, 60), job={"progress_percent": 5}),
                             snap("finished", job={"progress_percent": 100})])
    out = await s.run(OP, "printing")
    assert out["met"] is True and out["stopped_early"] is None and out["status"]["state"] == "finished"


async def test_a_job_seen_printing_that_completes_between_polls_counts_even_after_several_polls(monkeypatch):
    s = Script(monkeypatch, [snap("heating", (150, 215), (40, 60), job={"state": "printing", "filament_used_mm": 0}),
                             snap("heating", (200, 215), (60, 60), job={"state": "printing", "filament_used_mm": 0}),
                             snap("finished", job=COMPLETE)])
    out = await s.run(KL, "printing")
    assert out["met"] is True and len(s.sleeps) == 2


@pytest.mark.parametrize("until", ["printing", "first_layer_done"])
async def test_a_klipper_job_that_errors_or_is_cancelled_is_still_not_met(monkeypatch, until):
    running = snap("printing", job={"state": "printing", "filament_used_mm": 0, "layer": {"current": 1, "total": 80}})
    cancelled = await Script(monkeypatch, [running, snap("cancelled", job=CANCELLED)]).run(KL, until)
    assert cancelled["met"] is False and cancelled["stopped_early"].startswith("The job ended before")
    errored = await Script(monkeypatch, [running, snap("error", job={"state": "error"}, problems=[JOB_ERROR])]).run(KL, until)
    assert errored["met"] is False and errored["stopped_early"] == JOB_ERROR["message"]


async def test_an_octoprint_job_that_stops_without_finishing_is_still_not_met(monkeypatch):
    # Cancelled, or reset to idle (no completed job to see): neither is a success.
    for end in (snap("cancelled"), snap("idle")):
        s = Script(monkeypatch, [snap("heating", (150, 215), (40, 60)), end])
        out = await s.run(OP, "printing")
        assert out["met"] is False and out["stopped_early"] == "The job ended before extrusion started."


@pytest.mark.parametrize("until,kind,target", [("printing", "klipper", KL), ("printing", "octoprint", OP),
                                               ("first_layer_done", "klipper", KL)])
async def test_a_job_that_was_already_complete_when_the_wait_began_is_not_the_one_waited_for(monkeypatch, until, kind, target):
    done = snap("finished", job=COMPLETE) if kind == "klipper" else snap("finished", job={"progress_percent": 100})
    s = Script(monkeypatch, [done])
    out = await s.run(target, until, timeout_s=12)
    assert out["met"] is False and "timed out" in out["note"] and out["stopped_early"] is None


async def test_a_completed_job_still_ends_a_wait_for_the_heaters_as_ended_before(monkeypatch):
    # Only "printing" and "first_layer_done" read a completed job as success: reaching the heaters'
    # targets is not something a finished job can be said to have done.
    s = Script(monkeypatch, [snap("heating", (150, 215), (40, 60), job={"state": "printing"}),
                             snap("finished", (30, 0), (25, 0), job=COMPLETE)])
    out = await s.run(KL, "heated")
    assert out["met"] is False and out["stopped_early"] == "The job ended before the heaters reached their targets."


def lost(code="not_reachable"):
    return PrinterError(code, "The printer didn't answer.")


async def test_a_network_blip_mid_wait_is_ridden_out(monkeypatch):
    s = Script(monkeypatch, [snap("printing", job={"filament_used_mm": 0}), lost(),
                             snap("printing", job={"filament_used_mm": 12})])
    out = await s.run(KL, "printing")
    assert out["met"] is True and out["stopped_early"] is None and len(s.sleeps) == 2


async def test_a_protocol_error_blip_is_ridden_out_too(monkeypatch):
    s = Script(monkeypatch, [snap("printing", job={"filament_used_mm": 0}), lost("protocol_error"),
                             snap("printing", job={"filament_used_mm": 12})])
    assert (await s.run(KL, "printing"))["met"] is True


async def test_a_good_poll_resets_the_failure_count(monkeypatch):
    hot = snap("heating", (150, 215))
    s = Script(monkeypatch, [hot, lost(), hot, lost(), hot, lost(), snap("heating", (214, 215), (23, 0))])
    out = await s.run(KL, "heated")
    assert out["met"] is True and len(s.sleeps) == 6


def headed(text, *args, **kw):
    return {**snap(*args, **kw), "headline": text}


async def test_no_progress_notification_goes_out_with_the_status_of_a_failed_poll(monkeypatch):
    # After a poll fails, the snapshot in hand is the one already reported: say nothing rather than
    # repeat it as if it were news.
    s = Script(monkeypatch, [headed("Heating 1", "heating", (100, 215)), lost(),
                             headed("Heating 2", "heating", (150, 215)), headed("Heating 3", "heating", (214, 215))])
    out = await s.run(KL, "heated")
    assert out["met"] is True and len(s.sleeps) == 3
    assert [m for _, _, m in s.reports] == ["Heating 1", "Heating 2"]
    assert [d for d, _, _ in s.reports] == [0.0, 10.0]  # the report after the failed poll is the one missing


async def test_progress_resumes_after_the_blip_with_the_fresh_status(monkeypatch):
    hot = headed("Heating", "heating", (150, 215))
    s = Script(monkeypatch, [hot, lost(), lost(), headed("Still heating", "heating", (160, 215)),
                             headed("Heated", "heating", (214, 215))])
    out = await s.run(KL, "heated")
    assert out["met"] is True
    assert [m for _, _, m in s.reports] == ["Heating", "Still heating"]


async def test_a_wait_that_keeps_failing_polls_still_times_out_without_notifying(monkeypatch):
    s = Script(monkeypatch, [headed("Heating", "heating", (150, 215)), lost(), lost(), headed("Heating", "heating", (150, 215))])
    out = await s.run(KL, "heated", timeout_s=12)
    assert out["met"] is False and "timed out" in out["note"] and out["waited_s"] == 12
    assert [d for d, _, _ in s.reports] == [0.0]  # nothing while the polls were failing


async def test_three_failed_polls_in_a_row_end_the_wait_with_the_last_good_status(monkeypatch):
    first = snap("heating", (150, 215))
    s = Script(monkeypatch, [first, lost()])
    out = await s.run(KL, "heated")
    assert out["met"] is False and out["stopped_early"].startswith("Lost contact with the printer")
    assert "The printer didn't answer." in out["stopped_early"]
    assert out["status"] is first and out["until"] == "heated" and out["waited_s"] == 15
    assert s.sleeps == [5.0, 5.0, 5.0]


async def test_a_non_transient_error_mid_wait_ends_the_wait_at_once(monkeypatch):
    first = snap("heating", (150, 215))
    s = Script(monkeypatch, [first, PrinterError("auth_rejected", "The printer refused the API key.")])
    out = await s.run(KL, "heated")
    assert out["met"] is False and out["status"] is first and len(s.sleeps) == 1
    assert out["stopped_early"] == "Lost contact with the printer: The printer refused the API key."


async def test_a_failure_on_the_first_poll_still_raises(monkeypatch):
    s = Script(monkeypatch, [lost()])
    with pytest.raises(PrinterError) as e:
        await s.run(KL, "heated")
    assert e.value.code == "not_reachable"


async def test_timeout_is_capped(monkeypatch):
    s = Script(monkeypatch, [snap("heating", (150, 215))])
    out = await s.run(KL, "heated", timeout_s=99999)
    assert out["met"] is False and sum(s.sleeps) == w.MAX_TIMEOUT_S


INFO = {"result": {"klippy_state": "ready", "klippy_connected": True, "warnings": []}}


def heat_query(nozzle):
    return {"result": {"status": {
        "webhooks": {"state": "ready"},
        "print_stats": {"state": "printing", "filename": "a.gcode", "filament_used": 0.0,
                        "print_duration": 0.0, "total_duration": 10.0, "info": {}},
        "extruder": {"temperature": nozzle, "target": 215.0},
        "heater_bed": {"temperature": 60.0, "target": 60.0}}}}


@respx.mock
async def test_wait_for_printer_tool_reports_progress(monkeypatch):
    P = "http://192.0.2.10"
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    monkeypatch.setattr(w, "POLL_S", 0.0)
    respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json=INFO))
    respx.get(url__startswith=f"{P}/server/gcode_store").mock(
        return_value=httpx.Response(200, json={"result": {"gcode_store": []}}))
    respx.get(url__startswith=f"{P}/server/files/metadata").mock(return_value=httpx.Response(404))
    respx.get(url__startswith=f"{P}/printer/objects/query").mock(
        side_effect=[httpx.Response(200, json=heat_query(150.0)), httpx.Response(200, json=heat_query(214.0))])

    class Ctx:
        def __init__(self):
            self.calls = []

        async def report_progress(self, progress, total=None, message=None):
            self.calls.append((progress, total, message))

    ctx = Ctx()
    out = await srv.wait_for_printer("heated", 60, ctx)
    assert out["met"] is True and len(ctx.calls) == 1


@respx.mock
async def test_wait_for_printer_tool_returns_when_the_print_is_cancelled_while_heating(monkeypatch):
    # The whole path with real snapshot shapes: Klipper reports "printing" with the heaters short of
    # their targets (heating), then print_stats goes to "cancelled" and the targets drop to zero.
    P = "http://192.0.2.10"
    monkeypatch.setenv("ORCA_PRINTER_URL", P)
    monkeypatch.setattr(w, "POLL_S", 0.0)
    cancelled = heat_query(140.0)
    cancelled["result"]["status"]["print_stats"]["state"] = "cancelled"
    cancelled["result"]["status"]["extruder"]["target"] = 0.0
    cancelled["result"]["status"]["heater_bed"]["target"] = 0.0
    respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json=INFO))
    respx.get(url__startswith=f"{P}/server/gcode_store").mock(
        return_value=httpx.Response(200, json={"result": {"gcode_store": []}}))
    respx.get(url__startswith=f"{P}/server/files/metadata").mock(return_value=httpx.Response(404))
    query = respx.get(url__startswith=f"{P}/printer/objects/query").mock(
        side_effect=[httpx.Response(200, json=heat_query(150.0)), httpx.Response(200, json=cancelled)])
    out = await srv.wait_for_printer("heated", 2)  # a regression fails in 2 s, not 30 minutes
    assert out["met"] is False and out["stopped_early"] == "The job ended before the heaters reached their targets."
    assert out["status"]["state"] == "cancelled" and query.call_count == 2 and "note" not in out


def test_first_layer_done_description_says_it_can_be_approximate():
    props = srv.mcp._tool_manager._tools["wait_for_printer"].parameters["properties"]
    desc = props["until"]["description"]
    assert "judged from the nozzle height, so it is approximate" in desc and chr(0x2014) not in desc


def test_the_tool_description_says_when_it_returns_early():
    doc = " ".join(srv.wait_for_printer.__doc__.split())
    for phrase in ("stopped_early", "when the printer reports a fault", "when the job ends before the point it waits for",
                   "Lost contact with the printer", "A met result can carry stopped_early too",
                   "completes successfully while the wait is for 'printing' or 'first_layer_done' counts as met"):
        assert phrase in doc
    assert chr(0x2014) not in doc
