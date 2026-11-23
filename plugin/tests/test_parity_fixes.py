"""Plugin 0.2.5: the plugin-side gaps the parity audit of 2026-11-24 found between the plugin and the OrcaSlicer MCP
build (rows 1, 2, 3, 5, 9, 11, 13 and 17 of that audit), one block per row. All values are invented."""
import json
import os
import socket
import sys
import time
import types

import pytest

import fake_orca
from conftest import call, mod
from test_panel_host import history_rows, last

S = fake_orca.STATE
UI = fake_orca.UI


# -- row 1: the off switch holds while the panel is closed ---------------------------------------------------------
def test_turning_access_off_holds_for_the_next_request_with_the_panel_closed(plugin):
    """The switch was read only while the panel's page ticked: with the panel closed, the first request after the
    user turned access off was served, writes included."""
    call(plugin, "GET", "/api/v1/status")
    UI.run_blocking(S.panels[-1].close)  # the user closes the panel, then turns access off in the Config tab
    S.config["paused"] = True
    status, body = call(plugin, "PUT", "/api/v1/config", {"wall_loops": 4})
    assert (status, body["error"]) == (503, "paused")
    assert S.bundle.prints._by("0.20mm Standard @Fake")._cfg["wall_loops"] == "2"  # nothing written
    S.config["paused"] = False  # and back on
    assert call(plugin, "PUT", "/api/v1/config", {"wall_loops": 4})[0] == 200


def test_the_switch_is_announced_once(plugin):
    call(plugin, "GET", "/api/v1/status")
    S.config["paused"] = True
    call(plugin, "GET", "/api/v1/status")
    call(plugin, "GET", "/api/v1/status")
    assert [t for _, t in S.notifications].count("OrcaSlicer MCP: assistant access turned off") == 1


# -- row 2: a preset switch never opens OrcaSlicer's unsaved-changes window ----------------------------------------
def test_a_switch_with_unsaved_changes_is_refused_and_names_them(plugin):
    """OrcaSlicer would open its Transfer / Discard / Save window and hold every request until someone answers."""
    call(plugin, "PUT", "/api/v1/config", {"layer_height": 0.28, "nozzle_temperature": 215})
    status, body = call(plugin, "PUT", "/api/v1/preset", {"type": "print", "name": "My Fast"})
    assert status == 409 and body["error"] == "unsaved_edits"
    assert body["modified"] == {"print": ["layer_height"], "filament": ["nozzle_temperature"]}
    assert "print (Layer height) and filament (Nozzle temperature)" in body["detail"]
    assert "Nothing was switched" in body["detail"]
    assert S.bundle.prints.sel == "0.20mm Standard @Fake"  # nothing switched, the edits are kept
    assert S.bundle.prints._by("0.20mm Standard @Fake")._cfg["layer_height"] == "0.28"
    row = history_rows(plugin)[-1]
    assert (row["kind"], row["label"], row["value"]) == ("refused", "Print preset", "Unsaved changes")
    assert row["where"].startswith("Unsaved changes to print (Layer height) and filament (Nozzle temperature).")


def test_a_switch_without_unsaved_changes_goes_through(plugin):
    assert call(plugin, "PUT", "/api/v1/preset", {"type": "print", "name": "My Fast"})[0] == 200
    assert S.bundle.prints.sel == "My Fast"


def test_a_session_only_difference_is_no_unsaved_change(plugin):
    printer = S.bundle.printers._by("Fake Printer 0.4")
    printer._saved["extruder_nozzle_stats"], printer._cfg["extruder_nozzle_stats"] = "", "1"
    assert call(plugin, "PUT", "/api/v1/preset", {"type": "print", "name": "My Fast"})[0] == 200


def test_an_unknown_preset_is_still_named_first(plugin):
    call(plugin, "PUT", "/api/v1/config", {"layer_height": 0.28})
    assert call(plugin, "PUT", "/api/v1/preset", {"type": "print", "name": "Nope"}) == (422, {"error": "unknown_preset"})


def test_unsaved_groups_text_keeps_it_short():
    keys = ["layer_height", "wall_loops", "outer_wall_speed", "sparse_infill_density", "top_shell_layers",
            "bottom_shell_layers"]
    text = mod.unsaved_groups_text({"print": keys})
    assert text.startswith("print (Layer height, ") and text.endswith(", 2 more)")


# -- row 3: values OrcaSlicer questions in a window of its own are refused first -----------------------------------
@pytest.fixture
def limits(plugin):
    """A printer with layer height limits 0.08 to 0.32 mm, and the filament and ironing settings in their presets."""
    for coll, values in ((S.bundle.printers, {"min_layer_height": "0.08", "max_layer_height": "0.32"}),
                         (S.bundle.filaments, {"filament_max_volumetric_speed": "12", "filament_ironing_spacing": "nil"}),
                         (S.bundle.prints, {"ironing_spacing": "0.15", "initial_layer_print_height": "0.2"})):
        for p in coll.presets:
            p._cfg.update(values)
            p._saved.update(values)
    return plugin


def test_a_layer_height_below_the_printers_lowest_is_refused_in_the_builds_words(limits):
    status, body = call(limits, "PUT", "/api/v1/config", {"layer_height": 0.01, "wall_loops": 3})
    assert status == 422 and body["applied"] == []
    assert body["error"] == "out_of_range" and body["key"] == "layer_height"
    assert (body["value"], body["min"], body["max"]) == ("0.01", 0.08, 0.32)
    assert body["detail"] == ("Layer height must be between 0.08 mm and 0.32 mm (got 0.01 mm); the printer's limits are "
                              "set in Printer Settings > Layer height limits")
    assert body["errors"] == {"layer_height": "out_of_range: " + body["detail"]}
    assert S.bundle.prints._by("0.20mm Standard @Fake")._cfg["wall_loops"] == "2"  # the whole batch is refused
    row = history_rows(limits)[-1]
    assert (row["label"], row["value"], row["where"]) == ("Layer height", "Out of range", body["detail"])


def test_a_layer_height_above_the_printers_highest_is_refused(limits):
    status, body = call(limits, "PUT", "/api/v1/config", {"layer_height": 0.4})
    assert status == 422 and "(got 0.4 mm)" in body["detail"]


def test_a_layer_height_within_the_limits_this_batch_sets_is_taken(limits):
    """One request may lower the printer's limit and the layer height together, as the build judges it."""
    status, _ = call(limits, "PUT", "/api/v1/config", {"layer_height": 0.06, "min_layer_height": "0.04"})
    assert status == 200 and S.bundle.prints._by("0.20mm Standard @Fake")._cfg["layer_height"] == "0.06"


def test_a_printer_without_limits_still_refuses_a_layer_height_of_zero(plugin):
    status, body = call(plugin, "PUT", "/api/v1/config", {"layer_height": 0})
    assert status == 422 and body["detail"].startswith("Layer height must be at least 0.01 mm (got 0 mm); the lowest")


def test_the_first_layer_is_held_to_the_lowest_only(limits):
    assert call(limits, "PUT", "/api/v1/config", {"initial_layer_print_height": 0.5})[0] == 200
    status, body = call(limits, "PUT", "/api/v1/config", {"initial_layer_print_height": 0.05})
    label = mod.display_label("initial_layer_print_height")
    assert status == 422 and body["detail"].startswith(f"{label} must be at least 0.08 mm (got 0.05 mm); the lowest")


def test_a_max_volumetric_speed_orcaslicer_would_reset_is_refused(limits):
    status, body = call(limits, "PUT", "/api/v1/config", {"filament_max_volumetric_speed": "0.3"})
    assert status == 422 and body["min"] == 0.5 and "(got 0.3 mm³/s)" in body["detail"]
    status, body = call(limits, "PUT", "/api/v1/config", {"filament_max_volumetric_speed": "abc"})
    assert status == 422 and "must be a number (got abc)" in body["detail"]


def test_ironing_spacing_below_its_minimum_is_refused_and_nil_passes(limits):
    status, body = call(limits, "PUT", "/api/v1/config", {"ironing_spacing": 0.01})
    assert status == 422 and body["key"] == "ironing_spacing" and body["min"] == 0.05
    assert call(limits, "PUT", "/api/v1/config", {"filament_ironing_spacing": "nil"})[0] == 200
    status, body = call(limits, "PUT", "/api/v1/config", {"filament_ironing_spacing": "0.2,0.01"})
    assert status == 422 and body["index"] == 1


def test_a_value_written_back_unchanged_is_not_judged(limits):
    p = S.bundle.prints._by("0.20mm Standard @Fake")
    p._cfg["layer_height"] = p._saved["layer_height"] = "0.05"  # below the limit already, from before
    assert call(limits, "PUT", "/api/v1/config", {"layer_height": "0.05"})[0] == 200


# -- row 5: notifications follow the build's rules ---------------------------------------------------------------
def test_refusals_show_at_most_one_notification_per_five_seconds(plugin):
    for _ in range(4):
        call(plugin, "PUT", "/api/v1/config", {"no_such_key": 1})
    refused = [t for _, t in S.notifications if "Couldn't update settings" in t]
    assert len(refused) == 1
    assert len([r for r in history_rows(plugin) if r["kind"] == "refused"]) == 4  # the panel lists every one


def test_a_change_shows_no_notification_while_the_panel_lists_it(plugin):
    call(plugin, "PUT", "/api/v1/config", {"wall_loops": 4})
    assert not any("AI changed" in t for _, t in S.notifications)


def test_request_notices_are_plain_ascii_when_shown(plugin):
    plugin._panel = None
    plugin._request_notice("AI changed: Wall loops 2 -> 4")
    assert S.notifications[-1][1] == "OrcaSlicer MCP: AI changed: Wall loops 2 -> 4"
    src = open(mod.__file__, encoding="utf-8").read()
    put_config = src[src.index("    def _put_config("):src.index("    def _select_preset(")]
    assert "\\u2192" not in put_config and "→" not in put_config


# -- row 9: a silent connection no longer holds up the server ----------------------------------------------------
# (row 20, the build's bad_kind detail, is in test_panel_host)
def test_the_reply_keeps_the_builds_15_seconds(plugin):
    assert plugin._service.server.RequestHandlerClass.timeout == 15.0 and mod.REQUEST_READ_TIMEOUT_S == 3.0


def test_a_silent_connection_does_not_hold_up_the_next_request(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "REQUEST_READ_TIMEOUT_S", 0.5)
    api = types.SimpleNamespace(handle=lambda method, path, body, trial=False: (200, {"ok": True}))
    svc = mod.Service(api, "t" * 48, 0)
    svc.start()
    try:
        silent = socket.create_connection(("127.0.0.1", svc.port))  # connects, sends nothing
        half = socket.create_connection(("127.0.0.1", svc.port))
        half.sendall(b"GET /api/v1/status HTTP/1.1\r\n")  # half a request
        start = time.monotonic()
        s = socket.create_connection(("127.0.0.1", svc.port))
        s.sendall(b"GET /api/v1/status HTTP/1.1\r\nHost: x\r\nX-Api-Token: " + b"t" * 48 + b"\r\n\r\n")
        s.settimeout(10)
        reply = s.recv(4096)
        assert reply.startswith(b"HTTP/1.1 200") and time.monotonic() - start < 5
        for c in (silent, half, s):
            c.close()
    finally:
        svc.stop()


# -- row 11: the panel says the plugin did not start, not that access is off --------------------------------------
def test_the_panel_says_a_failed_start_is_not_the_switch(tmp_path):
    fake_orca.STATE.reset(storage=str(tmp_path))
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    (tmp_path / "settings.json").write_text(json.dumps({"port": port}))
    cap = mod.McpCapability()
    try:
        cap.on_load()
        assert cap._service is None and cap._port_busy
        caps = cap.host.history_msg()["capabilities"]
        assert caps["off_text"].startswith(f"OrcaSlicer MCP couldn't start: port {port} may be in use")
        assert mod.SETTINGS_HINT in caps["off_text"] and cap._state() == {"type": "state", "api": "off"}
    finally:
        cap.on_unload()
        taken.close()


def test_the_panel_keeps_the_switch_wording_when_it_started(plugin):
    assert plugin.host.history_msg()["capabilities"]["off_text"] == "Assistant access is off."


# -- row 13: the Config tab calls an app connected only since OrcaSlicer started ----------------------------------
def test_the_config_tab_knows_when_this_session_started(plugin):
    html = plugin.get_config_ui()
    assert f"STARTED = {int(plugin._started)};" in html
    assert "st.seen && st.seen >= STARTED" in html and "Last connected" in html


# -- row 17: the token file is owner-only -------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no such file modes")
def test_a_new_token_file_is_owner_only(tmp_path):
    mod.load_or_create_token(str(tmp_path))
    assert os.stat(tmp_path / "token.txt").st_mode & 0o777 == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no such file modes")
def test_an_older_shared_token_file_is_made_owner_only_and_kept(tmp_path):
    path = tmp_path / "token.txt"
    path.write_text("a" * 48 + "\n")
    os.chmod(path, 0o664)  # as plugins up to 0.2.4 left it
    assert mod.load_or_create_token(str(tmp_path)) == "a" * 48
    assert os.stat(path).st_mode & 0o777 == 0o600 and path.read_text() == "a" * 48 + "\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["token.txt"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no such file modes")
def test_a_renewed_token_file_is_owner_only(tmp_path):
    mod.load_or_create_token(str(tmp_path))
    os.chmod(tmp_path / "token.txt", 0o644)
    mod.load_or_create_token(str(tmp_path), renew=True)
    assert os.stat(tmp_path / "token.txt").st_mode & 0o777 == 0o600
