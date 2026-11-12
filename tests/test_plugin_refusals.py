"""M9 (usability 2026-11-09): on the OrcaSlicer MCP plugin, a refusal names the action that
failed and gives the way to do it by hand before it mentions the build."""
import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp.plugin_text import _ACTIONS, plugin_refusal

BASE = "http://x:13130"
PLUGIN_404 = httpx.Response(404, json={"error": "not_found", "backend": "plugin"})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", BASE)


def _check(out, action, by_hand_words):
    err = out["error"]
    assert err.startswith(f"not_available_in_plugin: {action} isn't available")
    assert by_hand_words in out["by_hand"]
    # The by-hand way comes before the build.
    assert err.index("By hand:") < err.index("OrcaSlicer MCP build")


@respx.mock
async def test_slice_names_slice_plate_first():
    respx.post(f"{BASE}/api/v1/slice").mock(return_value=PLUGIN_404)
    out = await srv.slice_and_wait()
    _check(out, "Slicing", "Slice plate")
    assert out["action"] == "slice_and_wait"


@respx.mock
async def test_arrange_names_the_toolbar_button():
    respx.post(f"{BASE}/api/v1/arrange").mock(return_value=PLUGIN_404)
    _check(await srv.arrange_plate(), "Arranging the plate", "Arrange all objects")


@respx.mock
async def test_render_asks_for_a_look_or_screenshot():
    respx.get(f"{BASE}/api/v1/plate/render").mock(return_value=PLUGIN_404)
    _check(await srv.render_plate(), "Rendering a picture of the plate", "screenshot")


@respx.mock
async def test_save_preset_names_the_save_icon():
    respx.post(f"{BASE}/api/v1/preset/save").mock(return_value=PLUGIN_404)
    _check(await srv.save_preset("print", "Mine"), "Saving a preset", "save icon")


@respx.mock
async def test_up_front_refusals_name_the_tool_too():
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json={"backend": "plugin"}))
    _check(await srv.apply_and_slice({"layer_height": 0.2}), "Slicing", "Slice plate")
    _check(await srv.edit_preset("print", "P", {"layer_height": 0.2}), "Saving a preset", "set_config")


def test_every_listed_tool_exists():
    # "tool:argument" keys word the refusal of one argument of a tool the plugin otherwise serves
    names = {t.name for t in srv.mcp._tool_manager.list_tools()}
    assert {k.split(":", 1)[0] for k in _ACTIONS} <= names


def test_unknown_tool_still_reads_well():
    out = plugin_refusal(None)
    assert out["error"].startswith("not_available_in_plugin: That isn't available")
