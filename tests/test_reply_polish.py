"""m7 and m12 (usability 2026-11-09): get_status says build or plugin, internal keys stay out of
`modified`, a remembered printer is announced first, every error has a readable detail with no
HTTP routes, and every tool's first description line is a whole sentence."""
import anyio
import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp.printer import target as ptarget

BASE = "http://x:13130"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", BASE)


@respx.mock
async def test_get_status_says_build_and_hides_internal_keys():
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json={
        "modified": {"print": ["wall_loops"], "filament": [], "printer": ["extruder_nozzle_stats#0"]}}))
    out = await srv.get_status()
    assert out["connected_to"].startswith("the OrcaSlicer MCP build")
    assert out["modified"] == {"print": ["wall_loops"], "filament": [], "printer": []}


@respx.mock
async def test_get_status_says_plugin():
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json={"backend": "plugin"}))
    out = await srv.get_status()
    assert out["connected_to"] == "the OrcaSlicer MCP plugin on stock OrcaSlicer"


@respx.mock
async def test_remembered_printer_is_named_first_in_the_headline(monkeypatch):
    P = "http://192.0.2.10"
    respx.get(f"{BASE}/api/v1/status").mock(side_effect=httpx.ConnectError("refused"))
    ptarget.remember(ptarget.PrinterTarget(url=P, source="profile", profile="Test Printer",
                                           host_type="moonraker", kind="klipper"))
    respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json={"result": {
        "klippy_state": "ready", "klippy_connected": True, "warnings": [], "failed_components": []}}))
    respx.get(url__startswith=f"{P}/printer/objects/query").mock(return_value=httpx.Response(200, json={
        "result": {"eventtime": 1.0, "status": {
            "webhooks": {"state": "ready", "state_message": "Printer is ready"},
            "print_stats": {"state": "standby", "filename": "", "print_duration": 0.0, "total_duration": 0.0,
                            "filament_used": 0.0, "message": "", "info": {}},
            "extruder": {"temperature": 24.9, "target": 0.0}, "heater_bed": {"temperature": 23.1, "target": 0.0},
            "configfile": {"warnings": []}}}}))
    respx.get(url__startswith=f"{P}/server/gcode_store").mock(
        return_value=httpx.Response(200, json={"result": {"gcode_store": []}}))
    out = await srv.get_printer_status()
    assert out["headline"].startswith(
        "The printer you last used (Test Printer), not necessarily the one selected in OrcaSlicer now")


@respx.mock
async def test_open_project_on_an_stl_names_the_tool_not_the_route():
    respx.post(f"{BASE}/api/v1/project/open").mock(return_value=httpx.Response(
        422, json={"error": "not_a_project", "detail": "open takes a .3mf project; POST /model imports geometry"}))
    out = await srv.open_project("C:/parts/cube.stl")
    assert out["detail"] == "open takes a .3mf project; load_model imports geometry"
    assert "/model" not in str(out)


@respx.mock
async def test_open_project_missing_file_gets_a_detail():
    respx.post(f"{BASE}/api/v1/project/open").mock(return_value=httpx.Response(
        404, json={"error": "file_not_found", "path": "C:/gone.3mf"}))
    out = await srv.open_project("C:/gone.3mf")
    assert out["detail"].startswith("There is no file at C:/gone.3mf on the OrcaSlicer machine")


@respx.mock
async def test_load_model_relative_path_asks_for_a_full_one():
    respx.post(f"{BASE}/api/v1/model").mock(return_value=httpx.Response(404, json={"error": "file_not_found"}))
    out = await srv.load_model("bunny.stl")
    assert "Give the full path" in out["detail"]
    out = await srv.load_model("/home/me/bunny.stl")
    assert "Give the full path" not in out["detail"]


@respx.mock
async def test_bare_tokens_get_a_detail():
    respx.post(f"{BASE}/api/v1/arrange").mock(return_value=httpx.Response(409, json={"error": "job_running"}))
    out = await srv.arrange_plate()
    assert out["error"] == "job_running" and "get_job_status" in out["detail"]
    out = await srv.compare_slices([{"name": "a", "changes": {}}])
    assert out["detail"].startswith("compare_slices needs at least two")


def test_describe_setting_unknown_key_has_a_detail():
    out = srv.describe_setting("nope")
    assert out["error"] == "unknown_setting" and "search_settings" in out["detail"]


def test_every_first_description_line_is_a_whole_sentence():
    tools = anyio.run(srv.mcp.list_tools)
    assert len(tools) == 66
    for t in tools:
        first = t.description.split("\n", 1)[0].strip()
        assert first.endswith("."), t.name
        assert first[0].isupper(), t.name


def test_annotations_unchanged_and_complete():
    assert set(srv._TOOL_ANNOTATIONS) == {t.name for t in anyio.run(srv.mcp.list_tools)}
