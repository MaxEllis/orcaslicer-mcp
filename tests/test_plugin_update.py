"""An OrcaSlicer MCP plugin older than this release's is told about, once; the build never is."""
import os
import pathlib
import re

import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp import plugin_update as pu

BASE = "http://x:13130/api/v1"
URL = "https://github.com/MaxEllis/orcaslicer-mcp/releases/latest"


def _env(m, url="http://x:13130"):
    m.setenv("ORCA_API_TOKEN", "tok")
    m.setenv("ORCA_API_URL", url)
    m.setattr(srv, "_plugin_checked_url", None)
    m.setattr(srv, "_last_user_seq", None)
    m.setattr(srv, "_unsupported_url", None)
    m.setattr(srv, "_seq_url", None)
    m.setattr(pu, "LATEST_PLUGIN_VERSION", "0.2.0")
    m.setattr(srv, "_plugin_update", lambda status: pu.plugin_update(status, "0.2.0"))


def _plugin_status(version="0.1.0", base=BASE):
    return respx.get(f"{base}/status").mock(return_value=httpx.Response(
        200, json={"app_version": "2.5.0", "backend": "plugin", "plugin_version": version}))


def _no_changes(base=BASE):
    respx.get(f"{base}/user_changes").mock(return_value=httpx.Response(200, json={"seq": 1, "changes": []}))


def _config(base=BASE):
    respx.get(f"{base}/config").mock(return_value=httpx.Response(200, json={"config": {"layer_height": "0.2"}}))


NOTE = ("Your OrcaSlicer MCP plugin is version 0.1.0; version 0.2.0 is out. Download it from "
        f"{URL}, or install it from OrcaCloud to get updates automatically.")


# ---------------------------------------------------------------- the comparison
def test_parse_version():
    assert pu.parse_version("1.2.3") == (1, 2, 3) and pu.parse_version(" 0.10.0 ") == (0, 10, 0)
    for bad in ("1.2", "1.2.3.4", "v1.2.3", "", None, 1.2, "0.1.0-dev"):
        assert pu.parse_version(bad) is None, bad


def test_only_an_older_plugin_is_flagged():
    older = {"backend": "plugin", "plugin_version": "0.1.0"}
    assert pu.plugin_update(older, "0.2.0") == {"installed": "0.1.0", "latest": "0.2.0", "url": URL, "note": NOTE}
    assert pu.plugin_update(older, "0.1.0") is None                                  # up to date
    assert pu.plugin_update({"backend": "plugin", "plugin_version": "0.3.0"}, "0.2.0") is None
    assert pu.plugin_update({"backend": "plugin", "plugin_version": "0.1.10"}, "0.1.9") is None  # numeric order
    assert pu.plugin_update({"app_version": "2.5.0"}, "9.9.9") is None              # the build: never
    assert pu.plugin_update({"backend": "plugin"}, "0.2.0") is None                  # no version: say nothing
    assert pu.plugin_update({"backend": "plugin", "plugin_version": "dev"}, "0.2.0") is None
    assert pu.plugin_update(None, "0.2.0") is None


# ---------------------------------------------------------------- through the tools
@respx.mock
async def test_get_status_says_so_every_time(monkeypatch):
    _env(monkeypatch)
    _plugin_status()
    _no_changes()
    for _ in range(2):
        out = await srv.get_status()
        assert out["plugin_update"]["note"] == NOTE and out["backend"] == "plugin"


@respx.mock
async def test_first_reply_carries_a_one_time_note(monkeypatch):
    _env(monkeypatch)
    status = _plugin_status()
    _no_changes()
    _config()
    first = await srv.get_config()
    assert first["plugin_update"]["note"] == NOTE and first["config"] == {"layer_height": "0.2"}
    second = await srv.get_config()
    assert "plugin_update" not in second
    assert status.call_count == 1  # checked once per session


@respx.mock
async def test_no_one_time_note_after_get_status_said_it(monkeypatch):
    _env(monkeypatch)
    status = _plugin_status()
    _no_changes()
    _config()
    assert "plugin_update" in await srv.get_status()
    assert "plugin_update" not in await srv.get_config()
    assert status.call_count == 1


@respx.mock
async def test_the_build_is_never_flagged(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/status").mock(return_value=httpx.Response(200, json={"app_version": "2.5.0"}))
    _no_changes()
    _config()
    assert "plugin_update" not in await srv.get_status()
    assert "plugin_update" not in await srv.get_config()


@respx.mock
async def test_an_up_to_date_plugin_gets_nothing(monkeypatch):
    _env(monkeypatch)
    _plugin_status("0.2.0")
    _no_changes()
    _config()
    assert "plugin_update" not in await srv.get_config()
    assert "plugin_update" not in await srv.get_status()


@respx.mock
async def test_a_failed_check_is_tried_again(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/status").mock(side_effect=[
        httpx.Response(500, json={"error": "internal"}),
        httpx.Response(200, json={"backend": "plugin", "plugin_version": "0.1.0"})])
    _no_changes()
    _config()
    assert "plugin_update" not in await srv.get_config()
    assert (await srv.get_config())["plugin_update"]["installed"] == "0.1.0"


@respx.mock
async def test_another_orcaslicer_is_checked_again(monkeypatch):
    _env(monkeypatch)
    _plugin_status()
    _no_changes()
    _config()
    assert "plugin_update" in await srv.get_config()
    other = "http://y:13130/api/v1"
    monkeypatch.setenv("ORCA_API_URL", "http://y:13130")
    _plugin_status(base=other)
    _no_changes(other)
    _config(other)
    assert "plugin_update" in await srv.get_config()


async def test_sync_tools_and_errors_are_left_alone(monkeypatch):
    _env(monkeypatch)
    assert await srv._attach_plugin_update(["a"]) == ["a"]
    keep = {"plugin_update": {"note": "x"}}
    assert await srv._attach_plugin_update(keep) is keep


# ---------------------------------------------------------------- kept in step with the plugin
def _plugin_source() -> pathlib.Path | None:
    env = os.environ.get("ORCASLICER_MCP_PLUGIN_SRC")
    candidates = [pathlib.Path(env)] if env else []
    candidates.append(pathlib.Path(__file__).resolve().parents[1] / "plugin" / "orcaslicer_mcp_plugin.py")
    return next((p for p in candidates if p.is_file()), None)


def test_latest_plugin_version_matches_the_plugin():
    src = _plugin_source()
    if src is None:
        pytest.skip("plugin source not found (set ORCASLICER_MCP_PLUGIN_SRC)")
    text = src.read_text(encoding="utf-8")
    header = re.search(r'^# version = "([^"]+)"$', text, re.M)
    constant = re.search(r'^PLUGIN_VERSION = "([^"]+)"$', text, re.M)
    assert header and constant and header.group(1) == constant.group(1)
    assert pu.LATEST_PLUGIN_VERSION == header.group(1), \
        "the plugin's version moved: raise LATEST_PLUGIN_VERSION in plugin_update.py with it"
