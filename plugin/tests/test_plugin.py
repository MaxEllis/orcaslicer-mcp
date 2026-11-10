import json
import time

import fake_orca
from conftest import ROOT, call, mod

S = fake_orca.STATE


def test_registers_with_minimal_permissions():
    orca = fake_orca.build_module()
    import sys
    sys.modules["orca"], saved = orca, sys.modules["orca"]
    try:
        mod.OrcaSlicerMcpPlugin().register_capabilities.__func__  # noqa: B018 (exists)
    finally:
        sys.modules["orca"] = saved
    S.permissions = None
    mod.orca.registered.clear()
    saved_folders, mod.current_slice_folders = mod.current_slice_folders, lambda: []
    try:
        mod.OrcaSlicerMcpPlugin().register_capabilities()
    finally:
        mod.current_slice_folders = saved_folders
    assert S.permissions == {"threading": True, "network_socket": ["127.0.0.1"]}
    assert mod.orca.registered == [mod.McpCapability]


def test_binds_localhost_only(plugin):
    assert plugin._service.server.server_address[0] == "127.0.0.1"


def test_token_file_created_and_reused(plugin, tmp_path):
    tok = (tmp_path / "token.txt").read_text().strip()
    assert tok == plugin._token and len(tok) >= 32
    assert mod.load_or_create_token(str(tmp_path)) == tok


def test_wrong_or_missing_token_is_401(plugin):
    assert call(plugin, "GET", "/api/v1/status", token="nope") == (401, {"error": "unauthorized"})
    assert call(plugin, "GET", "/api/v1/status", token="")[0] == 401


def test_fork_only_routes_say_so(plugin):
    for method, path in [("POST", "/api/v1/slice"), ("POST", "/api/v1/model"),
                         ("GET", "/api/v1/plate/render"), ("GET", "/api/v1/plates"),
                         ("POST", "/api/v1/project/save"), ("POST", "/api/v1/preset/save"),
                         ("DELETE", "/api/v1/objects/101"), ("GET", "/api/v1/statuses")]:
        status, body = call(plugin, method, path, {} if method != "GET" else None)
        assert status == 404, path
        assert body["error"] == "not_found" and body["backend"] == "plugin"


def test_status_shape(plugin):
    status, st = call(plugin, "GET", "/api/v1/status")
    assert status == 200
    assert st["backend"] == "plugin" and st["app_version"] == "2.5.0"
    assert "slice" not in st["capabilities"] and "model" not in st["capabilities"]
    assert st["presets"] == {"printer": "Fake Printer 0.4", "print": "0.20mm Standard @Fake",
                             "filaments": ["Fake PLA"]}
    assert st["objects"] == [{"name": "cube", "size_mm": [20, 20, 20]}]
    assert st["modified"] == {"print": [], "filament": [], "printer": []}
    assert st["project_name"] == "Untitled" and st["project"] == "" and st["sensitive_keys_allowed"] is False


def test_config_hides_host_keys_and_filters(plugin):
    _, body = call(plugin, "GET", "/api/v1/config")
    cfg = body["config"]
    assert cfg["layer_height"] == "0.2" and "print_host" not in cfg and "printhost_apikey" not in cfg
    _, body = call(plugin, "GET", "/api/v1/config?keys=layer_height%2Cwall_loops,print_host")
    assert body == {"config": {"layer_height": "0.2", "wall_loops": "2"}}


def test_put_config_applies_and_marks_modified(plugin):
    status, body = call(plugin, "PUT", "/api/v1/config", {"layer_height": 0.28, "nozzle_temperature": 215})
    assert (status, body) == (200, {"applied": ["layer_height", "nozzle_temperature"], "errors": {}})
    _, st = call(plugin, "GET", "/api/v1/status")
    assert st["modified"]["print"] == ["layer_height"] and st["modified"]["filament"] == ["nozzle_temperature"]
    assert any("Layer height 0.2 \u2192 0.28" in t for _, t in S.notifications)


def test_put_config_refuses_sensitive_and_applies_nothing(plugin):
    status, body = call(plugin, "PUT", "/api/v1/config",
                        {"layer_height": 0.3, "post_process": "evil.exe", "machine_start_gcode": "M112",
                         "printer_model": "Other", "plugins": "x"})
    assert status == 422 and body["applied"] == []
    assert set(body["errors"]) == {"post_process", "machine_start_gcode", "printer_model", "plugins"}
    assert body["errors"]["plugins"] == "unknown_key"  # not a key of the fake presets
    assert body["errors"]["post_process"] == "blocked_by_remote_api_policy"
    _, cfg = call(plugin, "GET", "/api/v1/config?keys=layer_height")
    assert cfg["config"]["layer_height"] == "0.2"  # atomic: the valid key was not applied either


def test_refusal_notification_names_settings_by_their_labels(plugin):
    """Max 2026-11-10: the pop-up said "wall_loops" / "machine_start_gcode" while the panel said
    "Wall loops" / "Start G-code". A key the plugin has no label for keeps its key."""
    S.notifications.clear()
    call(plugin, "PUT", "/api/v1/config", {"post_process": "evil.exe", "machine_start_gcode": "M112",
                                           "no_such_key": 1})
    texts = [t for _, t in S.notifications if "Couldn't update settings" in t]
    assert len(texts) == 1
    names = [mod.display_label("machine_start_gcode"), mod.display_label("post_process"), "no_such_key"]
    assert names[0] != "machine_start_gcode" and names[1] != "post_process"
    assert texts[0].endswith("Couldn't update settings: " + ", ".join(sorted(names, key=str.lower)))


def test_writing_a_sensitive_key_back_unchanged_passes(plugin):
    status, body = call(plugin, "PUT", "/api/v1/config", {"machine_start_gcode": "G28"})
    assert (status, body["applied"]) == (200, ["machine_start_gcode"])


def test_redacted_placeholder_never_written(plugin):
    status, body = call(plugin, "PUT", "/api/v1/config", {"printhost_apikey": "<redacted>"})
    assert status == 422 and body["errors"]["printhost_apikey"] == "redacted_placeholder"


def test_unknown_and_project_keys(plugin):
    status, body = call(plugin, "PUT", "/api/v1/config", {"no_such_key": 1, "curr_bed_type": "Cool Plate"})
    assert status == 422
    assert body["errors"] == {"no_such_key": "unknown_key", "curr_bed_type": "not_editable_in_current_config"}


def test_bad_value_reports_the_key(plugin):
    status, body = call(plugin, "PUT", "/api/v1/config", {"wall_loops": "bad", "layer_height": 0.1})
    assert status == 422 and list(body["errors"]) == ["wall_loops"] and body["applied"] == []


def test_partial_write_across_groups_is_flagged(plugin):
    status, body = call(plugin, "PUT", "/api/v1/config", {"layer_height": 0.1, "nozzle_temperature": "bad"})
    assert status == 422 and body["partial"] is True and body["applied"] == ["layer_height"]


def test_bad_json(plugin):
    req_status, body = call(plugin, "PUT", "/api/v1/config", ["not", "an", "object"])
    assert (req_status, body) == (400, {"error": "body_must_be_object"})


def test_presets_list(plugin):
    _, body = call(plugin, "GET", "/api/v1/presets")
    assert [p["name"] for p in body["print"]] == ["0.20mm Standard @Fake", "My Fast"]  # default skipped
    assert body["print"][0] == {"name": "0.20mm Standard @Fake", "system": True, "visible": True,
                                "selected": True, "compatible": True}


def test_preset_config_redacts_credentials(plugin):
    status, body = call(plugin, "POST", "/api/v1/preset/config", {"type": "printer", "name": "Fake Printer 0.4"})
    assert status == 200
    assert body["config"]["printhost_apikey"] == "<redacted>"
    assert body["config"]["print_host"] == "http://<redacted>@printer.invalid"
    assert call(plugin, "POST", "/api/v1/preset/config", {"type": "print", "name": "nope"})[0] == 404
    assert call(plugin, "POST", "/api/v1/preset/config", {"type": "bogus", "name": "x"})[0] == 400


def test_select_preset(plugin):
    status, body = call(plugin, "PUT", "/api/v1/preset", {"type": "print", "name": "My Fast"})
    assert status == 200 and body["selected"] == "My Fast"
    _, st = call(plugin, "GET", "/api/v1/status")
    assert st["presets"]["print"] == "My Fast"
    status, _ = call(plugin, "PUT", "/api/v1/preset", {"type": "filament", "name": "Fake PETG"})
    assert status == 200 and S.bundle.filament_slots == ["Fake PETG"]


def test_select_preset_errors(plugin):
    assert call(plugin, "PUT", "/api/v1/preset", {"type": "print", "name": "nope"})[0] == 422
    assert call(plugin, "PUT", "/api/v1/preset", {"type": "bogus", "name": "x"})[0] == 400
    S.decline_selection = True
    status, body = call(plugin, "PUT", "/api/v1/preset", {"type": "print", "name": "My Fast"})
    assert status == 409 and body["error"] == "select_cancelled"


def test_objects_shape(plugin):
    _, body = call(plugin, "GET", "/api/v1/objects")
    o = body["objects"][0]
    assert body["count"] == 1
    assert o["id"] == 101 and o["index"] == 0 and o["instances"] == 1
    assert o["transform"]["offset"] == [10.0, 20.0, 5.0]
    assert o["bbox_min"] == [0, 0, 0] and o["on_plate"] is True and o["config"] == {"wall_loops": "3"}


def test_project(plugin):
    S.plater.path = "C:/prints/bracket.3mf"
    _, body = call(plugin, "GET", "/api/v1/project")
    assert body == {"name": "bracket", "path": "C:/prints/bracket.3mf", "saved": True, "dirty": False,
                    "objects": 1, "modified": []}
    _, st = call(plugin, "GET", "/api/v1/status")
    assert st["project"] == "C:/prints/bracket" and st["project_name"] == "bracket"


def test_unload_releases_the_port(tmp_path):
    fake_orca.STATE.reset(storage=str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps({"port": 0}))  # any free port: 13130 may be in use here
    cap = mod.McpCapability()
    cap.on_load()
    port = cap._service.port
    cap.on_unload()
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", port))  # would raise if the server still held it
    s.close()


def test_settings_port(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"port": 13135}))
    assert mod.load_port(str(tmp_path)) == 13135
    (tmp_path / "settings.json").write_text(json.dumps({"port": 80}))
    assert mod.load_port(str(tmp_path)) == mod.DEFAULT_PORT
    (tmp_path / "settings.json").write_text("{nope")
    assert mod.load_port(str(tmp_path)) == mod.DEFAULT_PORT


def test_failed_thread_start_closes_the_socket(tmp_path, monkeypatch):
    # The symptom of a server thread that never started is a port that accepts and never answers.
    fake_orca.STATE.reset(storage=str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps({"port": 0}))  # any free port: 13130 may be in use here

    def refuse(self):
        raise PermissionError("threading not granted")
    monkeypatch.setattr(mod.threading.Thread, "start", refuse)
    seen = {}
    orig_init = mod.Service.__init__

    def spy(self, *a, **k):
        orig_init(self, *a, **k)
        seen["port"] = self.port
    monkeypatch.setattr(mod.Service, "__init__", spy)
    cap = mod.McpCapability()
    cap.on_load()
    assert "PermissionError" in cap._error and cap._service is None
    assert any("could not start" in t for _, t in S.notifications)
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", seen["port"]))  # free again
    s.close()
    cap.on_unload()



def test_status_reports_the_plugins_own_version(plugin):
    """orcaslicer-mcp compares it with the newest release to say "a newer plugin is out" (the plugin itself
    never goes online), so the header, PLUGIN_VERSION and the status reply must agree."""
    import re
    text = (ROOT / "orcaslicer_mcp_plugin.py").read_text(encoding="utf-8")
    header = re.search(r'^# version = "([^"]+)"$', text, re.M).group(1)
    assert header == mod.PLUGIN_VERSION
    status, body = call(plugin, "GET", "/api/v1/status")
    assert status == 200 and body["backend"] == "plugin" and body["plugin_version"] == header


def test_orcaslicer_mcp_knows_this_version():
    import pytest
    try:
        from orcaslicer_mcp import plugin_update
    except ImportError:
        pytest.skip("orcaslicer-mcp without plugin_update (needs branch plugin-update)")
    assert plugin_update.LATEST_PLUGIN_VERSION == mod.PLUGIN_VERSION, \
        "raise LATEST_PLUGIN_VERSION in orcaslicer-mcp's plugin_update.py with the plugin's version"


def _register_with(fake_request):
    """Run register_capabilities against a request_permissions stand-in; returns the calls it saw."""
    calls = []

    def request(**kw):
        calls.append(kw)
        fake_request(kw)
    saved_req, mod.orca.request_permissions = mod.orca.request_permissions, request
    saved_folders, mod.current_slice_folders = mod.current_slice_folders, lambda: ["/tmp/orcaslicer_model"]
    mod.orca.registered.clear()
    try:
        mod.OrcaSlicerMcpPlugin().register_capabilities()
    finally:
        mod.orca.request_permissions = saved_req
        mod.current_slice_folders = saved_folders
    return calls


def test_permissions_asked_in_one_call_when_the_build_takes_them():
    calls = _register_with(lambda kw: None)
    assert calls == [{"threading": True, "network_socket": ["127.0.0.1"], "fs_read": ["/tmp/orcaslicer_model/*"]}]
    assert mod.orca.registered == [mod.McpCapability]


def test_a_build_that_refuses_a_keyword_gets_a_smaller_request():
    """Some 2.5 builds refuse a keyword they do not know (TypeError from the binding): the plugin asks again
    without it instead of failing to load, and OrcaSlicer asks about the rest on first use."""
    def older_build(kw):
        if "fs_read" in kw or "threading" in kw:
            raise TypeError("request_permissions(): incompatible function arguments.")
    calls = _register_with(older_build)
    assert [sorted(c) for c in calls] == [["fs_read", "network_socket", "threading"], ["network_socket", "threading"],
                                          ["network_socket"]]
    assert mod.orca.registered == [mod.McpCapability]


def test_a_refusal_that_is_not_a_keyword_problem_still_stops_loading():
    def refused(kw):
        raise PermissionError("the user said No")
    try:
        _register_with(refused)
    except PermissionError:
        pass
    else:
        raise AssertionError("a refusal must still stop the plugin loading")
    assert mod.orca.registered == []


def test_a_host_without_a_needed_call_says_which_orcaslicer_is_needed(tmp_path):
    fake_orca.STATE.reset(storage=str(tmp_path))
    saved = mod.orca.host.ui.create_dock_panel
    del mod.orca.host.ui.create_dock_panel
    cap = mod.McpCapability()
    try:
        fake_orca.UI.run_blocking(cap.on_load)
        assert cap._service is None
        assert cap._error.startswith("needs OrcaSlicer 2.5.0 or later") and "orca.host.ui.create_dock_panel" in cap._error
        assert any("needs OrcaSlicer 2.5.0 or later" in text for _lvl, text in S.notifications)
    finally:
        mod.orca.host.ui.create_dock_panel = saved
        cap.on_unload()


def test_the_connection_file_lets_the_connector_find_the_plugin(tmp_path, monkeypatch):
    """The plugin writes connection.json in its data folder; orcaslicer-mcp, with no token set, reads it."""
    storage = tmp_path / "orca_plugins" / "plugin_data" / "orcaslicer_mcp_plugin"
    storage.mkdir(parents=True)
    fake_orca.STATE.reset(storage=str(storage))
    (storage / "settings.json").write_text(json.dumps({"port": 0}))
    (storage / "connection.json").write_text(json.dumps({"product": "orcaslicer-mcp-plugin", "url": "x", "token": "y"}))
    cap = mod.McpCapability()
    saved, mod.load_port = mod.load_port, (lambda folder: 0)
    try:
        cap.on_load()
    finally:
        mod.load_port = saved
    try:
        data = json.loads((storage / "connection.json").read_text())
        assert data["product"] == "orcaslicer-mcp-plugin" and data["token"] == cap._token
        assert data["url"] == f"http://127.0.0.1:{cap._service.port}" and data["version"] == mod.PLUGIN_VERSION
        import sys as _sys
        if _sys.platform != "win32":
            assert (storage / "connection.json").stat().st_mode & 0o077 == 0  # owner only
        from orcaslicer_mcp.config import load_config
        monkeypatch.setenv("ORCA_DATADIR", str(tmp_path))
        monkeypatch.delenv("ORCA_API_TOKEN", raising=False)
        monkeypatch.delenv("ORCA_API_URL", raising=False)
        cfg = load_config()
        assert cfg.token == cap._token and cfg.base_url == data["url"] and cfg.source == "plugin"
        # a new token rewrites the file
        fake_orca.UI.run_blocking(cap._renew_token)
        assert json.loads((storage / "connection.json").read_text())["token"] == cap._token != data["token"]
    finally:
        cap.on_unload()
    assert not (storage / "connection.json").exists()  # gone when the plugin stops


def test_requests_record_which_app_connected_for_the_config_tab(plugin):
    """Each accepted request notes the time and the app's name; the UI-thread poll saves it in the plugin's
    settings (where the Config tab reads it), at most once a minute unless the app changes."""
    import urllib.request
    def get(app=None, token=None):
        req = urllib.request.Request(plugin.base + "/api/v1/status")
        req.add_header("X-Api-Token", token or plugin._token)
        if app:
            req.add_header("X-OrcaSlicer-MCP-Client", app)
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass
    get(token="wrong")  # a refused request records nothing
    fake_orca.UI.run_blocking(plugin._poll_config)
    assert "status" not in S.config
    get(app="Claude Code")
    fake_orca.UI.run_blocking(plugin._poll_config)
    first = S.config["status"]
    assert first["app"] == "Claude Code" and abs(first["seen"] - time.time()) < 5
    get()  # no header: keeps the app it knew; same app within a minute: not saved again
    fake_orca.UI.run_blocking(plugin._poll_config)
    assert S.config["status"] == first
    get(app="Codex")  # another app: saved at once
    fake_orca.UI.run_blocking(plugin._poll_config)
    assert S.config["status"]["app"] == "Codex"
    assert S.config.get("paused") is not True  # the rest of the settings are kept
    codex = S.config["status"]
    S.config = {**S.config, "status": first}  # the Config tab saved its copy from when it opened
    fake_orca.UI.run_blocking(plugin._poll_config)
    assert S.config["status"] == codex  # put back at once


def test_a_plugin_that_cannot_start_leaves_no_connection_file(tmp_path, monkeypatch):
    """A file left by a crash is removed at load; if the server then can't start, none is written, so the connector
    doesn't send this plugin's token to whatever owns the port now."""
    storage = tmp_path / "orca_plugins" / "plugin_data" / "orcaslicer_mcp_plugin"
    storage.mkdir(parents=True)
    fake_orca.STATE.reset(storage=str(storage))
    (storage / "connection.json").write_text(json.dumps({"product": "orcaslicer-mcp-plugin", "url": "x", "token": "y"}))

    class Broken:
        def __init__(self, *a, **k):
            raise OSError("address already in use")
    monkeypatch.setattr(mod, "Service", Broken)
    cap = mod.McpCapability()
    cap.on_load()
    try:
        assert cap._error and not (storage / "connection.json").exists()
    finally:
        cap.on_unload()


def test_the_connection_file_is_written_through_its_path(tmp_path, monkeypatch):
    """OrcaSlicer lets a plugin write in its own folder without asking only when its audit check sees the path; a write
    through a file descriptor (os.fdopen) made it ask the user about an unnamed "open" on a real OrcaSlicer 2.5."""
    import builtins
    opened, real = [], builtins.open
    monkeypatch.setattr(mod, "open", lambda f, *a, **k: opened.append(f) or real(f, *a, **k), raising=False)
    monkeypatch.setattr(mod.os, "fdopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("os.fdopen used")))
    mod.write_connection_file(str(tmp_path), 13130, "t" * 48)
    assert opened and all(isinstance(f, str) for f in opened)
    assert json.loads((tmp_path / "connection.json").read_text())["url"] == "http://127.0.0.1:13130"
    assert not (tmp_path / "connection.json.tmp").exists()


def test_a_failed_connection_write_leaves_nothing_behind(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise PermissionError("refused by OrcaSlicer")
    monkeypatch.setattr(mod.json, "dump", refuse)
    import pytest
    with pytest.raises(PermissionError):
        mod.write_connection_file(str(tmp_path), 13130, "t" * 48)
    assert list(tmp_path.iterdir()) == []
    (tmp_path / "connection.json.tmp").write_text("{}")
    mod.remove_connection_file(str(tmp_path))  # a .tmp left by an older version goes too
    assert list(tmp_path.iterdir()) == []


def test_activated_inside_the_mcp_build_it_starts_nothing(tmp_path, monkeypatch):
    storage = tmp_path / "orca_plugins" / "plugin_data" / "orcaslicer_mcp_plugin"
    storage.mkdir(parents=True)
    fake_orca.STATE.reset(storage=str(storage))
    monkeypatch.setattr(mod.orca.host, "app_info", lambda: {"version": "2.5.0", "orca_mcp_version": "2.5.0-mcp.17"},
                        raising=False)
    notes = []
    cap = mod.McpCapability()
    monkeypatch.setattr(cap, "_notify", lambda text, *a, **k: notes.append(text))
    made = []
    monkeypatch.setattr(mod.orca.host.ui, "create_dock_panel", lambda *a, **k: made.append(1), raising=False)
    cap.on_load()
    try:
        assert cap._service is None and not made and not (storage / "connection.json").exists()
        assert notes == [mod.IN_BUILD_TEXT] and not cap._error
        assert cap.get_config_ui() == mod.IN_BUILD_HTML and "Not needed in this OrcaSlicer" in mod.IN_BUILD_HTML
    finally:
        cap.on_unload()


def test_inside_the_mcp_build_it_asks_for_no_permissions(monkeypatch):
    """The build has the connection built in and the plugin starts nothing there, so OrcaSlicer shows no permission
    prompt for it (seen on the real build 2026-11-11: it asked, then the plugin stood down)."""
    S.permissions = None
    mod.orca.registered.clear()
    monkeypatch.setattr(mod.orca.host, "app_info", lambda: {"version": "2.5.0", "orca_mcp_version": "2.5.0-mcp.17"},
                        raising=False)
    mod.OrcaSlicerMcpPlugin().register_capabilities()
    assert S.permissions is None and mod.orca.registered == [mod.McpCapability]
