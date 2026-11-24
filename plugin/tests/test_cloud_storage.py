"""A plugin installed from OrcaCloud gets its data folder only once OrcaSlicer's networking is up and someone is signed in.

Stock OrcaSlicer 2.5 (PluginManager::get_storage_dir) raises for a cloud plugin until the cloud agent exists, and loads
plugins on their own thread before it starts networking, so on_load can ask too early. Plugin 0.2.5 then said "could not
start" and stayed off until OrcaSlicer was restarted (OrcaCloud comment, OrcaSlicer 2.5.2, 2026-11-24); before 0.2.5 that
notification crashed OrcaSlicer at launch (issue #27). The plugin now waits for the folder and starts when it comes.
"""
import json
import time
import types
import urllib.request

import fake_orca
import pytest
from conftest import mod

S = fake_orca.STATE
NOT_YET = "Cloud plugin storage is unavailable before networking is initialized"
NO_USER = "Cloud plugin storage is unavailable without a logged-in user"


def _until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


class Storage:
    """orca.host.plugin.storage as OrcaSlicer runs it for an OrcaCloud install: raises until `ready`."""

    def __init__(self, folder, error=NOT_YET):
        self.folder, self.error, self.ready, self.calls = str(folder), error, False, 0

    def __call__(self):
        self.calls += 1
        if not self.ready:
            raise RuntimeError(self.error)
        return self.folder


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    S.reset(storage=str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps({"port": 0}))
    st = Storage(tmp_path)
    monkeypatch.setattr(mod.orca.host.plugin, "storage", st)
    monkeypatch.setattr(mod, "load_port", lambda folder: 0)
    monkeypatch.setattr(mod, "STORAGE_RETRY_S", (0.02, 0.05), raising=False)
    cap = mod.McpCapability()
    yield cap, st, tmp_path
    cap.on_unload()


def _status(cap):
    req = urllib.request.Request(f"http://127.0.0.1:{cap._service.port}/api/v1/status")
    req.add_header("X-Api-Token", cap._token)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def test_an_orcacloud_install_waits_for_its_folder_then_starts(cloud):
    cap, st, folder = cloud
    S.plater = None  # still on the splash screen
    cap.on_load()
    assert not cap._error and cap._service is None and cap._waiting == NOT_YET
    assert S.notifications == [] and not S.crashed
    assert _until(lambda: st.calls >= 3)  # it keeps asking
    assert cap._service is None
    st.ready = True  # OrcaSlicer has started its networking
    assert _until(lambda: cap._service is not None)
    assert not cap._error and cap._waiting == ""
    assert json.loads((folder / "connection.json").read_text())["url"].endswith(f":{cap._service.port}")
    S.plater = fake_orca.Plater(S.model)  # the main window is up
    assert _status(cap) == 200
    cap.on_lifecycle_event(types.SimpleNamespace(name="PlateSelected"), None)
    assert S.notifications == []  # nothing to say: it started


def test_not_signed_in_it_keeps_waiting_and_says_why_once(cloud, monkeypatch):
    cap, st, folder = cloud
    st.error = NO_USER
    monkeypatch.setattr(mod, "STORAGE_NOTICE_AFTER_S", 0.2)
    cap.on_load()
    assert _until(lambda: len(cap._held_notes) == 1)
    time.sleep(0.3)
    assert len(cap._held_notes) == 1  # once, not every retry
    cap.on_lifecycle_event(types.SimpleNamespace(name="PlateSelected"), None)
    assert [t for _, t in S.notifications] == ["OrcaSlicer MCP: " + mod.SIGN_IN_TEXT]
    html = cap.get_config_ui()  # the Config tab says the same, not "couldn't start"
    assert "var WAIT = " + mod._inline(mod.SIGN_IN_TEXT) + ";" in html and "ERR = \"\"" in html
    assert cap._state()["api"] == "off"
    st.ready = True  # they signed in
    assert _until(lambda: cap._service is not None)
    assert _status(cap) == 200
    assert 'var WAIT = "";' in cap.get_config_ui()


def test_a_short_wait_says_nothing(cloud, monkeypatch):
    cap, st, folder = cloud
    monkeypatch.setattr(mod, "STORAGE_NOTICE_AFTER_S", 5.0)
    cap.on_load()
    assert _until(lambda: st.calls >= 2)
    st.ready = True
    assert _until(lambda: cap._service is not None)
    cap.on_lifecycle_event(types.SimpleNamespace(name="PlateSelected"), None)
    assert S.notifications == [] and cap._held_notes == []


def test_unloaded_while_waiting_it_never_starts(cloud):
    cap, st, folder = cloud
    cap.on_load()
    assert _until(lambda: st.calls >= 2)
    cap.on_unload()
    st.ready = True
    time.sleep(0.3)
    assert cap._service is None and not (folder / "connection.json").exists()
    assert not cap._storage_thread.is_alive()


def test_the_menu_item_says_it_is_starting(cloud):
    cap, st, folder = cloud
    st.error = NO_USER
    cap.on_load()
    result = cap.execute()
    assert not result.ok and mod.SIGN_IN_TEXT in result.message


def test_inside_the_mcp_build_it_stays_off_without_waiting(cloud, monkeypatch):
    cap, st, folder = cloud
    monkeypatch.setattr(mod.orca.host, "app_info", lambda: {"version": "2.5.0", "orca_mcp_version": "2.5.0-mcp.20"},
                        raising=False)
    cap.on_load()
    assert cap._in_build and cap._storage_thread is None and st.calls == 1
    assert [t for t, _ in cap._held_notes] == [mod.IN_BUILD_TEXT]


def test_unloaded_while_it_starts_late_the_starter_cleans_up(cloud, monkeypatch):
    """on_unload can come while the storage thread is starting the server: it leaves the server and the panel to that
    thread, which stops them when it is done."""
    cap, st, folder = cloud
    started, release = __import__("threading").Event(), __import__("threading").Event()
    real = cap._open_panel_at_load

    def slow_panel():
        real()
        started.set()
        release.wait(5)  # on_unload comes now, mid-start
    monkeypatch.setattr(cap, "_open_panel_at_load", slow_panel)
    cap.on_load()
    st.ready = True
    assert started.wait(5)
    cap.on_unload()
    assert cap._service is not None  # left to the starting thread
    release.set()
    assert _until(lambda: cap._service is None and not (folder / "connection.json").exists())
    assert _until(lambda: not cap._storage_thread.is_alive()) and cap._panel is None
