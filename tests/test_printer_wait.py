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


async def test_printing_needs_progress_on_octoprint(monkeypatch):
    s = Script(monkeypatch, [snap("printing", job={"progress_percent": 0}), snap("printing", job={"progress_percent": 3})])
    assert (await s.run(OP, "printing"))["met"] is True


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


def test_first_layer_done_description_says_it_can_be_approximate():
    props = srv.mcp._tool_manager._tools["wait_for_printer"].parameters["properties"]
    desc = props["until"]["description"]
    assert "judged from the nozzle height, so it is approximate" in desc and chr(0x2014) not in desc
