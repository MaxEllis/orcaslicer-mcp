"""get_printer_status, wait_for_printer and list_print_history on a Bambu Lab printer profile: the fork's
status route through the tools, with OrcaSlicer mocked (respx) and nothing else on the network."""
import httpx
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp.printer import target as ptarget
from orcaslicer_mcp.printer import wait as pwait
from tests.test_printer_bambu import reply

F = "http://x:13130"
U = f"{F}/api/v1/printer"


def _env(m, tmp_path):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", F)
    m.delenv("ORCA_PRINTER_URL", raising=False)
    m.setattr(ptarget, "REMEMBERED_PATH", tmp_path / "printer.json")


class FakeTime:
    """run_wait binds asyncio.sleep and time.monotonic as defaults: hand it these instead."""

    def __init__(self, monkeypatch):
        self.t, self.sleeps = 0.0, []
        orig = pwait.run_wait

        async def fast(t, c, until, timeout_s, report=None, **kw):
            return await orig(t, c, until, timeout_s, report, sleep=self.sleep, clock=lambda: self.t, **kw)
        monkeypatch.setattr(srv._pwait, "run_wait", fast)

    async def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def bambu_profile():
    respx.get(f"{F}/api/v1/status").mock(return_value=httpx.Response(200, json={
        "presets": {"printer": "Bambu Lab P1S 0.4 nozzle", "print": "p", "filaments": ["f"]}}))
    respx.post(f"{F}/api/v1/preset/config").mock(return_value=httpx.Response(200, json={
        "name": "Bambu Lab P1S 0.4 nozzle", "system": True,
        "config": {"print_host": "", "printer_model": "Bambu Lab P1S"}}))


@respx.mock
async def test_status_through_the_orcaslicer_profile(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    bambu_profile()
    respx.get(f"{U}/status").mock(return_value=httpx.Response(200, json=reply(
        state="RUNNING", nozzle=(255, 255), bed=(90, 90), progress=42, remaining=2520, layer=(63, 150))))
    out = await srv.get_printer_status()
    assert out["state"] == "printing" and out["job"]["layer"] == {"current": 63, "total": 150}
    assert out["printer"] == {"profile": "Bambu Lab P1S 0.4 nozzle", "url": None, "kind": "bambu",
                              "source": "profile", "remembered_at": None}
    assert out["headline"].startswith("Printing benchy: 42% (layer 63/150), about 42 min left.")
    assert "notes" not in out
    # read through OrcaSlicer: no address to remember, so nothing is written for the fallback
    assert ptarget.recall_remembered() is None


@respx.mock
async def test_offline_printer_is_a_status_with_the_hint(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    bambu_profile()
    respx.get(f"{U}/status").mock(return_value=httpx.Response(200, json={
        "supported": False, "kind": "bambu", "reason": "printer_offline",
        "message": "P1S is not connected to OrcaSlicer right now."}))
    out = await srv.get_printer_status()
    assert out["state"] == "offline" and out["problems"][0]["hint"]
    assert out["headline"] == "The printer is offline. Problem: P1S is not connected to OrcaSlicer right now."


@respx.mock
async def test_no_printer_selected_is_an_error_naming_the_printer(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    bambu_profile()
    respx.get(f"{U}/status").mock(return_value=httpx.Response(200, json={
        "supported": False, "kind": "bambu", "reason": "no_printer_selected",
        "message": "No Bambu Lab printer is selected in OrcaSlicer's Device tab."}))
    out = await srv.get_printer_status()
    assert out["error"] == "not_configured" and "Device tab" in out["hint"]
    assert out["printer"]["kind"] == "bambu" and out["printer"]["profile"] == "Bambu Lab P1S 0.4 nozzle"


@respx.mock
async def test_orcaslicer_going_away_mid_read_is_orca_unreachable(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    bambu_profile()
    respx.get(f"{U}/status").mock(side_effect=httpx.ConnectError("down"))
    out = await srv.get_printer_status()
    assert out["error"] == "orca_unreachable" and out["printer"]["kind"] == "bambu"


@respx.mock
async def test_wait_for_finished_polls_the_fork(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    bambu_profile()
    running = reply(state="RUNNING", nozzle=(255, 255), bed=(90, 90), progress=98, remaining=60, layer=(149, 150))
    done = reply(state="FINISH", nozzle=(200, 0), bed=(60, 0), progress=100, remaining=0, layer=(150, 150))
    route = respx.get(f"{U}/status").mock(side_effect=[httpx.Response(200, json=running),
                                                       httpx.Response(200, json=running),
                                                       httpx.Response(200, json=done)])
    ft = FakeTime(monkeypatch)
    out = await srv.wait_for_printer("finished", timeout_s=60)
    assert out["met"] is True and out["status"]["state"] == "finished" and route.call_count == 3
    assert ft.sleeps == [5.0, 5.0] and out["waited_s"] == 10 and out["stopped_early"] is None


@respx.mock
async def test_wait_for_printing_needs_the_first_layer(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    bambu_profile()
    levelling = reply(state="RUNNING", stage=1, stage_text="Auto bed leveling", nozzle=(255, 255), bed=(90, 90), layer=(0, 150))
    first = reply(state="RUNNING", nozzle=(255, 255), bed=(90, 90), layer=(1, 150))
    respx.get(f"{U}/status").mock(side_effect=[httpx.Response(200, json=levelling), httpx.Response(200, json=first)])
    ft = FakeTime(monkeypatch)
    out = await srv.wait_for_printer("printing", timeout_s=30)
    assert out["met"] is True and ft.sleeps == [5.0] and out["status"]["job"]["layer"]["current"] == 1


@respx.mock
async def test_first_layer_done_is_allowed_on_a_bambu(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    bambu_profile()
    respx.get(f"{U}/status").mock(return_value=httpx.Response(200, json=reply(
        state="RUNNING", nozzle=(255, 255), bed=(90, 90), layer=(2, 150))))
    out = await srv.wait_for_printer("first_layer_done", timeout_s=10)
    assert out["met"] is True


@respx.mock
async def test_history_is_refused_on_a_bambu(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    bambu_profile()
    out = await srv.list_print_history()
    assert out["supported"] is False and "Bambu" in out["reason"] and out["printer"]["kind"] == "bambu"


@respx.mock
async def test_the_override_still_wins_over_a_bambu_profile(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("ORCA_PRINTER_URL", "http://192.0.2.10")
    status = respx.get(f"{U}/status").mock(return_value=httpx.Response(200, json=reply()))
    for url in ("http://192.0.2.10/server/info", "http://192.0.2.10:7125/server/info", "http://192.0.2.10/api/version"):
        respx.get(url).mock(side_effect=httpx.ConnectError("down"))
    out = await srv.get_printer_status()
    assert out["error"] == "not_reachable" and status.call_count == 0
