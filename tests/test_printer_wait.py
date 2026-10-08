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
    """Feeds take_snapshot a list of snapshots (the last one repeats) and runs a fake clock."""

    def __init__(self, monkeypatch, snaps):
        self.snaps, self.t, self.sleeps, self.reports = list(snaps), 0.0, [], []

        async def take(target, client):
            return self.snaps.pop(0) if len(self.snaps) > 1 else self.snaps[0]

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


async def test_first_layer_done_by_height(monkeypatch):
    job = {"layer": {"current": None, "total": None}, "first_layer_height": 0.3}
    s = Script(monkeypatch, [snap("printing", job=job, z=0.3), snap("printing", job=job, z=0.6)])
    assert (await s.run(KL, "first_layer_done"))["met"] is True


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
    s = Script(monkeypatch, [snap("finished")])
    assert (await s.run(KL, "finished"))["met"] is True


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
