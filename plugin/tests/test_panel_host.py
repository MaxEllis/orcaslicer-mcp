"""The plugin's host side behind the shared panel page: rows, live values, undo, history per project, Config tab."""
import json
import os
import time

import fake_orca
from conftest import call, mod

S = fake_orca.STATE
UI = fake_orca.UI


def page(plugin, msg):
    """What the panel page posts (runs on the UI thread, like OrcaSlicer's on_message)."""
    UI.run_blocking(lambda: plugin._on_message(msg))


def posts(plugin, kind):
    return [p for p in S.panels[-1].posts if p.get("type") == kind]


def last(plugin, kind):
    found = posts(plugin, kind)
    assert found, f"no {kind} message"
    return found[-1]


def wait_result(plugin, row, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        res = [p for p in posts(plugin, "result") if p.get("row") == row]
        if res:
            return res[-1]
        time.sleep(0.02)
    raise AssertionError("no result for row %s" % row)


def history_rows(plugin):
    return last(plugin, "history")["rows"]


def test_ready_sends_history_state_and_live_with_the_plugins_capabilities(plugin):
    call(plugin, "GET", "/api/v1/status")  # opens the panel; the page says ready
    time.sleep(0.2)
    h = last(plugin, "history")
    assert h["version"] == 1 and h["rows"] == [] and h["groups"] == []
    assert h["capabilities"] == {"restore": False, "kinds": ["setting", "preset", "refused", "slice"],
                                 "open_settings": False,
                                 "settings_hint": "Main menu (beside File) > Plugins > OrcaSlicer MCP > Config",
                                 "settings_label": "Assistant settings",
                                 "off_text": "Assistant access is off.", "tracked_live": True}
    assert h["restored_to"] is None and h["restore_failed"] is False and h["can_undo_restore"] is False
    assert last(plugin, "state") == {"type": "state", "api": "on"}
    assert last(plugin, "live")["values"] == {}


def test_no_reveal_the_plugin_api_cannot_focus_a_setting(plugin):
    """The page sends reveal only with capabilities.reveal (the plugin never sends it: 2.5's plugin API has no
    call that shows a setting in the sidebar). A stray one is ignored, like any message the host does not know."""
    call(plugin, "PUT", "/api/v1/config", {"layer_height": 0.28})
    time.sleep(0.2)
    h = last(plugin, "history")
    assert "reveal" not in h["capabilities"] and all("reveal" not in r for r in h["rows"])
    before = len(S.panels[-1].posts)
    page(plugin, {"action": "reveal", "row": h["rows"][0]["id"]})
    time.sleep(0.2)
    assert len(S.panels[-1].posts) == before


def test_setting_rows_use_orcaslicer_labels_and_units(plugin):
    call(plugin, "PUT", "/api/v1/config", {"layer_height": 0.28, "wall_loops": 3})
    rows = history_rows(plugin)
    lh = next(r for r in rows if r["label"] == "Layer height")
    # value and before as the build's config_value_text: unit added (raws stay in the target)
    assert lh["kind"] == "setting" and lh["value"] == "0.28 mm" and lh["before"] == "0.2 mm"
    assert lh["where"] == "0.20mm Standard @Fake (unsaved)"
    assert lh["undo"] == {"verb": "Revert", "tip": "Set Layer height back to 0.2 mm"}
    assert lh["time"] and len(lh["time"]) == 5 and lh["error"] == "" and lh["on_disk"] is False
    wl = next(r for r in rows if r["label"] == "Wall loops")
    assert wl["value"] == "3" and wl["group"] == lh["group"]


def test_refusals_become_red_rows(plugin):
    call(plugin, "PUT", "/api/v1/config", {"post_process": "evil.exe", "no_such_key": 1})
    rows = {r["label"]: r for r in history_rows(plugin)}
    pp = rows["Post-processing Scripts"] if "Post-processing Scripts" in rows else \
        next(r for r in rows.values() if r["value"] == "Not allowed")
    assert pp["kind"] == "refused" and pp["value"] == "Not allowed" and "scripts" in pp["where"]
    assert rows["no_such_key"]["value"] == "Unknown setting" and rows["no_such_key"]["undo"] is None


def test_preset_switch_row_and_switch_back(plugin):
    call(plugin, "PUT", "/api/v1/preset", {"type": "print", "name": "My Fast"})
    row = history_rows(plugin)[-1]
    assert (row["kind"], row["label"], row["value"], row["before"]) == \
        ("preset", "Print preset", "My Fast", "0.20mm Standard @Fake")
    assert row["undo"]["verb"] == "Switch back"
    page(plugin, {"action": "undo", "row": row["id"]})
    assert wait_result(plugin, row["id"])["status"] == "ok"
    assert S.bundle.prints.sel == "0.20mm Standard @Fake"
    assert history_rows(plugin)[-1]["undone_at"]


def test_setting_undo_writes_the_old_value_back(plugin):
    call(plugin, "PUT", "/api/v1/config", {"layer_height": 0.28})
    row = history_rows(plugin)[-1]
    page(plugin, {"action": "undo", "row": row["id"]})
    res = wait_result(plugin, row["id"])
    assert res == {"type": "result", "action": "undo", "row": row["id"], "status": "ok"}
    assert S.bundle.prints._by("0.20mm Standard @Fake")._cfg["layer_height"] == "0.2"
    assert any("reverted Layer height" in t for _, t in S.notifications)


def test_undo_refused_after_the_user_changed_it(plugin):
    call(plugin, "PUT", "/api/v1/config", {"layer_height": 0.28})
    row = history_rows(plugin)[-1]
    S.bundle.prints._by("0.20mm Standard @Fake")._cfg["layer_height"] = "0.32"  # edited by hand
    page(plugin, {"action": "undo", "row": row["id"]})
    res = wait_result(plugin, row["id"])
    assert res["status"] == "failed" and res["reason"] == "You changed it since"
    assert S.bundle.prints._by("0.20mm Standard @Fake")._cfg["layer_height"] == "0.32"


def test_preset_undo_declined_on_screen_is_cancelled(plugin):
    call(plugin, "PUT", "/api/v1/preset", {"type": "print", "name": "My Fast"})
    row = history_rows(plugin)[-1]
    S.decline_selection = True
    page(plugin, {"action": "undo", "row": row["id"]})
    assert wait_result(plugin, row["id"])["status"] == "cancelled"
    assert S.bundle.prints.sel == "My Fast"


def test_live_values_follow_hand_edits(plugin):
    call(plugin, "PUT", "/api/v1/config", {"wall_loops": 3})
    row = history_rows(plugin)[-1]
    page(plugin, {"action": "live"})
    assert last(plugin, "live")["values"][str(row["id"])] == {"value": "3", "present": True}
    S.bundle.prints._by("0.20mm Standard @Fake")._cfg["wall_loops"] = "4"
    page(plugin, {"action": "live"})
    assert last(plugin, "live")["values"][str(row["id"])]["value"] == "4"


def test_bursts_split_after_a_minute(plugin):
    clock = [1_000_000.0]
    plugin.host.clock = lambda: clock[0]
    call(plugin, "PUT", "/api/v1/config", {"wall_loops": 3})
    clock[0] += 30
    call(plugin, "PUT", "/api/v1/config", {"layer_height": 0.3})
    clock[0] += 61
    call(plugin, "PUT", "/api/v1/config", {"layer_height": 0.32})
    groups = [r["group"] for r in history_rows(plugin)]
    assert groups == [1, 1, 2] and [g["id"] for g in last(plugin, "history")["groups"]] == [1, 2]


def test_restore_is_answered_as_unavailable(plugin):
    call(plugin, "GET", "/api/v1/status")
    page(plugin, {"action": "restore", "group": 1})
    assert posts(plugin, "result") == [{"type": "result", "action": "restore", "group": 1, "status": "failed",
                                        "reason": "Restore points need the OrcaSlicer MCP build"}]
    page(plugin, {"action": "undo_restore"})  # carries neither row nor group
    assert last(plugin, "result") == {"type": "result", "action": "undo_restore", "status": "failed",
                                      "reason": "Restore points need the OrcaSlicer MCP build"}


def test_ready_answers_state_setup_then_history_and_repeats_are_cheap(plugin):
    call(plugin, "GET", "/api/v1/status")
    time.sleep(0.2)
    S.page_silent = True  # look at what the host sends, without the page answering
    panel = S.panels[-1]
    panel.posts.clear()
    page(plugin, {"action": "ready"})
    page(plugin, {"action": "ready"})  # the page repeats it until its first history
    assert [m.get("type") for m in panel.posts] == ["state", "setup", "history"] * 2


def test_every_undo_gets_exactly_one_result(plugin):
    call(plugin, "GET", "/api/v1/status")
    for row in (None, 9999):
        page(plugin, {"action": "undo", "row": row})
    assert [(r["row"], r["status"]) for r in posts(plugin, "result")] == [(None, "failed"), (9999, "failed")]


def test_an_undo_that_breaks_still_answers(plugin, monkeypatch):
    call(plugin, "PUT", "/api/v1/config", {"wall_loops": 3})
    row = history_rows(plugin)[-1]
    monkeypatch.setattr(plugin.host, "live_for", lambda target: 1 / 0)
    page(plugin, {"action": "undo", "row": row["id"]})
    res = [r for r in posts(plugin, "result") if r.get("row") == row["id"]]
    assert res == [{"type": "result", "action": "undo", "row": row["id"], "status": "failed",
                    "reason": "Couldn't undo it"}]


def test_history_is_kept_per_project(plugin, tmp_path):
    UI.run_blocking(lambda: plugin.host.project_event("ProjectOpened", "C:/prints/bracket.3mf"))
    call(plugin, "PUT", "/api/v1/config", {"wall_loops": 3})
    key = mod.history_key("C:/prints/bracket.3mf")
    saved = json.loads((tmp_path / "mcp_history" / key / "history.json").read_text())
    assert saved["version"] == 1 and saved["rows"][0]["label"] == "Wall loops"
    UI.run_blocking(lambda: plugin.host.project_event("NewProject", ""))
    assert history_rows(plugin) == []
    UI.run_blocking(lambda: plugin.host.project_event("ProjectOpened", "C:/prints/bracket.3mf"))
    assert [r["label"] for r in history_rows(plugin)] == ["Wall loops"]


def test_untitled_history_moves_to_the_first_saved_path(plugin, tmp_path):
    call(plugin, "PUT", "/api/v1/config", {"wall_loops": 3})
    assert (tmp_path / "mcp_history" / "untitled" / "history.json").exists()
    UI.run_blocking(lambda: plugin.host.project_event("ProjectAfterSave", "C:/prints/new.3mf"))
    key = mod.history_key("C:/prints/new.3mf")
    assert (tmp_path / "mcp_history" / key / "history.json").exists()
    assert not (tmp_path / "mcp_history" / "untitled" / "history.json").exists()


def test_history_json_has_the_builds_shape(plugin):
    call(plugin, "PUT", "/api/v1/config", {"wall_loops": 3})
    j = plugin.host.hist.to_json()
    assert set(j) == {"version", "rows", "groups", "start", "closed", "last_ts", "next_row"}
    assert set(j["rows"][0]) == {"id", "group", "ts", "time", "kind", "label", "value", "before", "where",
                                 "undo_verb", "undo_tip", "undone_at", "title_hint", "target", "result",
                                 "error", "on_disk", "value_tip", "before_tip"}  # as McpHistory::to_json (b007932e5f)
    assert set(j["groups"][0]) == {"id", "ts", "time", "checkpoint", "checkpoint_failed"}
    again = mod.McpHistory.from_json(json.loads(json.dumps(j)))
    assert again.to_json() == j


def test_history_key_matches_the_build():
    # FNV-1a 64 over the normalised UTF-8 path, as mcp_history_key in McpHistory.cpp.
    assert mod.history_key("") == "untitled"
    assert mod.history_key("C:\\prints\\.\\a.3mf") == mod.history_key("C:/prints/a.3mf")
    h = 14695981039346656037
    for c in "C:/prints/a.3mf".encode():
        h = ((h ^ c) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    assert mod.history_key("C:/prints/a.3mf") == f"{h:016x}"


def test_config_tab_shows_token_and_port(plugin):
    html = plugin.get_config_ui()
    assert json.dumps(plugin._token) in html and str(plugin._port) in html
    assert "__TOKEN__" not in html and plugin.has_config_ui() is True
    assert plugin.get_default_config() == {"port": 13130, "paused": False, "renew": 0}


def test_config_tab_turns_access_off_and_on(plugin):
    call(plugin, "GET", "/api/v1/status")
    S.config = {"paused": True}
    plugin._last_tick = 0
    page(plugin, {"t": "tick"})
    status, body = call(plugin, "GET", "/api/v1/status")
    assert status == 503 and body["error"] == "paused" and "Config" in body["detail"]
    assert last(plugin, "state") == {"type": "state", "api": "off"}
    S.config = {"paused": False}
    plugin._last_tick = 0
    page(plugin, {"t": "tick"})
    assert call(plugin, "GET", "/api/v1/status")[0] == 200


def test_config_tab_makes_a_new_token(plugin, tmp_path):
    call(plugin, "GET", "/api/v1/status")
    old = plugin._token
    S.config = {"renew": 1}
    plugin._last_tick = 0
    page(plugin, {"t": "tick"})
    assert plugin._token != old and (tmp_path / "token.txt").read_text().strip() == plugin._token
    assert call(plugin, "GET", "/api/v1/status", token=old)[0] == 401
    assert call(plugin, "GET", "/api/v1/status")[0] == 200
    page(plugin, {"t": "tick"})  # the same renew count does nothing again
    assert (tmp_path / "token.txt").read_text().strip() == plugin._token


def test_panel_page_is_the_shared_page_plus_the_pump():
    html = mod.panel_page()
    assert html.startswith(mod.PANEL_HTML[:200]) and mod.PUMP_JS in html
    assert html.index(mod.PUMP_JS) < html.rindex("</body>")


def test_labels_prefer_the_full_label():
    assert mod.LABELS["nozzle_temperature"][0] == "Nozzle temperature"
    assert mod.LABELS["layer_height"] == ("Layer height", "mm")


def test_row_wording_matches_the_build(plugin):
    """Labels, tips and refusal words as RemoteAPIController.cpp records them."""
    S.bundle.filament_slots = ["Fake PLA", "Fake PETG", "Fake PLA"]
    S.bundle.filaments._by("Fake PLA")._cfg["filament_colour"] = "#F2754E"
    call(plugin, "PUT", "/api/v1/config", {"nozzle_temperature": 215})
    row = history_rows(plugin)[-1]
    assert row["label"] == "Nozzle temperature (filaments 1, 3)"
    call(plugin, "PUT", "/api/v1/config", {"filament_colour": "#1FB8A6"})
    row = history_rows(plugin)[-1]
    assert (row["label"], row["value"], row["before"]) == ("Filament 1 colour", "#1FB8A6", "#F2754E")
    call(plugin, "PUT", "/api/v1/preset", {"type": "print", "name": "My Fast"})
    assert history_rows(plugin)[-1]["undo"] == {"verb": "Switch back",
                                                "tip": "Switch Print preset back to 0.20mm Standard @Fake"}
    call(plugin, "PUT", "/api/v1/preset", {"type": "print", "name": "Nope"})
    call(plugin, "PUT", "/api/v1/config", {"no_such_key": 1})
    call(plugin, "PUT", "/api/v1/config", {"post_process": "x"})
    rows = history_rows(plugin)[-3:]
    assert [(r["label"], r["value"], r["where"]) for r in rows] == [
        ("Print preset", "No such preset", "unknown_preset: Nope"),
        ("no_such_key", "Unknown setting", "no_such_key: unknown_key"),
        (mod.LABELS["post_process"][0], "Not allowed",
         "The plugin never changes scripts, G-code or printer connection settings")]


def test_unchanged_value_is_applied_but_not_listed(plugin):
    """Like the build (RemoteAPIController: `if (c.oldv == c.newv) continue;`), a write that leaves a value as it was
    adds no row and no notification: found in the real-app run, where an assistant set a preset's own layer height."""
    status, body = call(plugin, "PUT", "/api/v1/config", {"layer_height": 0.2, "wall_loops": 3})
    assert status == 200 and sorted(body["applied"]) == ["layer_height", "wall_loops"]
    assert [r["label"] for r in history_rows(plugin)] == ["Wall loops"]
    assert not any("Layer height" in text for _lvl, text in S.notifications if "AI changed" in text)


def test_write_that_changes_nothing_adds_no_group(plugin):
    call(plugin, "PUT", "/api/v1/config", {"wall_loops": 3})
    status, body = call(plugin, "PUT", "/api/v1/config", {"wall_loops": 3})
    assert status == 200 and body["applied"] == ["wall_loops"]
    h = last(plugin, "history")
    assert len(h["groups"]) == 1 and [r["label"] for r in h["rows"]] == ["Wall loops"]


# -- usability fixes 2026-11-09 (SYNTHESIS.md: R43/M2, M4, m1, m8) ----------------------------------------------
def test_live_tracks_only_the_latest_row_of_each_setting(plugin):
    """R43: the assistant changing a setting twice must not make the first row read "changed by you" (Rita's F1)."""
    call(plugin, "PUT", "/api/v1/config", {"outer_wall_speed": 40, "wall_loops": 3})
    first = {r["label"]: r["id"] for r in history_rows(plugin)}
    call(plugin, "PUT", "/api/v1/config", {"outer_wall_speed": 90})
    second = history_rows(plugin)[-1]["id"]
    page(plugin, {"action": "live"})
    values = last(plugin, "live")["values"]
    assert str(first["Outer wall speed"]) not in values
    assert values[str(second)]["value"] == "90 mm/s" and str(first["Wall loops"]) in values


def test_superseded_row_cannot_be_reverted(plugin):
    call(plugin, "PUT", "/api/v1/config", {"outer_wall_speed": 40})
    first = history_rows(plugin)[-1]["id"]
    call(plugin, "PUT", "/api/v1/config", {"outer_wall_speed": 90})
    page(plugin, {"action": "undo", "row": first})
    res = wait_result(plugin, first)
    assert res["status"] == "failed" and res["reason"] == "Changed again since"
    assert S.bundle.prints._by("0.20mm Standard @Fake")._cfg["outer_wall_speed"] == "90"


def test_reverting_the_later_row_hands_tracking_back(plugin):
    call(plugin, "PUT", "/api/v1/config", {"outer_wall_speed": 40})
    first = history_rows(plugin)[-1]["id"]
    call(plugin, "PUT", "/api/v1/config", {"outer_wall_speed": 90})
    second = history_rows(plugin)[-1]["id"]
    page(plugin, {"action": "undo", "row": second})
    assert wait_result(plugin, second)["status"] == "ok"
    page(plugin, {"action": "live"})
    values = last(plugin, "live")["values"]
    assert values[str(first)] == {"value": "40 mm/s", "present": True} and str(second) not in values


def test_preset_rows_are_tracked_per_type(plugin):
    call(plugin, "PUT", "/api/v1/preset", {"type": "print", "name": "My Fast"})
    first = history_rows(plugin)[-1]["id"]
    call(plugin, "PUT", "/api/v1/preset", {"type": "print", "name": "0.20mm Standard @Fake"})
    second = history_rows(plugin)[-1]["id"]
    page(plugin, {"action": "live"})
    values = last(plugin, "live")["values"]
    assert str(first) not in values and str(second) in values


def test_trial_requests_record_no_rows(plugin):
    """M4: compare_slices sends X-Orca-Mcp-Trial: 1 on its trial writes; the panel lists none of them."""
    import urllib.request
    req = urllib.request.Request(plugin.base + "/api/v1/config", data=json.dumps({"layer_height": 0.3}).encode(),
                                 method="PUT", headers={"X-Api-Token": plugin._token, "X-Orca-Mcp-Trial": "1",
                                                        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        assert json.loads(r.read())["applied"] == ["layer_height"]
    assert S.bundle.prints._by("0.20mm Standard @Fake")._cfg["layer_height"] == "0.3"
    assert not posts(plugin, "history") or history_rows(plugin) == []
    assert not any("AI changed" in t for _, t in S.notifications)
    status, uc = call(plugin, "GET", "/api/v1/user_changes?since=0")
    assert status == 200 and uc["changes"] == []  # an API change, not the user's


def test_shared_labels_name_their_section(plugin):
    """m1: three different settings all read "Outer wall" in OrcaSlicer's own labels."""
    assert mod.display_label("outer_wall_speed") == "Outer wall speed"
    assert mod.display_label("outer_wall_acceleration") == "Outer wall acceleration"
    assert mod.display_label("outer_wall_jerk") == "Outer wall jerk"
    assert mod.display_label("outer_wall_line_width") == "Outer wall line width"
    assert mod.display_label("layer_height") == "Layer height"
    assert mod.display_label("top_shell_layers") == "Top solid layers"
    assert mod.display_label("textured_plate_temp") == "Bed temperature (Textured PEI Plate)"
    assert mod.display_label("hot_plate_temp_initial_layer") == "First layer bed temperature (Smooth High Temp Plate)"
    assert mod.display_label("no_such_key") == "no_such_key"
    shared = [k for k in mod.LABELS if mod.display_label(k) != mod.LABELS[k][0]]
    by_label = {}
    for k in shared:
        by_label.setdefault(mod.display_label(k), []).append(k)
    assert all(len(v) == 1 for v in by_label.values()), {k: v for k, v in by_label.items() if len(v) > 1}


def test_panel_opens_when_the_plugin_is_activated(tmp_path):
    """m8: the empty panel shows right after Activate, not at the assistant's first request (Rita's F7)."""
    fake_orca.STATE.reset(storage=str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps({"port": 0}))
    cap = mod.McpCapability()
    UI.run_blocking(cap.on_load)
    try:
        assert len(S.panels) == 1
        time.sleep(0.2)
        assert last(None, "state") == {"type": "state", "api": "on"} and last(None, "history")["rows"] == []
    finally:
        cap.on_unload()


def test_config_tab_speaks_plainly():
    html = mod.CONFIG_HTML
    prose = html.split("<script>", 1)[0]  # the setup text the user copies lives in the script, not in the prose
    assert "<port>" not in html and "&lt;port&gt;" not in html and "ORCA_API" not in prose
    assert "Connect your AI app" in prose and "Token and port" in prose
    # the order a person needs it: is it working, then connect, then the rarely needed token and port
    assert html.index('id="status"') < html.index("Connect your AI app") < html.index("Token and port")
    assert "\\" not in html  # the page sits in a Python string: no escapes that Python would swallow
    assert mod.SETTINGS_HINT == "Main menu (beside File) > Plugins > OrcaSlicer MCP > Config"


def test_tracked_live_capability_and_superseded_rows(plugin):
    """R43 in the protocol (fork ef0e90a766): the host says it sends live entries for tracked rows only, and marks the
    earlier rows of a setting the assistant changed again as superseded (the page then offers no undo on them)."""
    call(plugin, "PUT", "/api/v1/config", {"outer_wall_speed": 40, "wall_loops": 3})
    call(plugin, "PUT", "/api/v1/config", {"outer_wall_speed": 90})
    h = last(plugin, "history")
    assert h["capabilities"]["tracked_live"] is True
    sup = {r["label"]: r["superseded"] for r in h["rows"]}
    rows = h["rows"]
    assert [r["superseded"] for r in rows if r["label"] == "Outer wall speed"] == [True, False]
    assert sup["Wall loops"] is False
    page(plugin, {"action": "undo", "row": rows[-1]["id"]})
    assert wait_result(plugin, rows[-1]["id"])["status"] == "ok"
    after = last(plugin, "history")["rows"]
    assert [r["superseded"] for r in after if r["label"] == "Outer wall speed"] == [False, False]


def test_activity_note_adds_a_slice_note_that_names_its_group(plugin):
    """POST /activity/note (fork ef0e90a766): one row the connector adds itself, e.g. after compare_slices' trials."""
    status, body = call(plugin, "POST", "/api/v1/activity/note",
                        {"kind": "slice", "text": "Compared 3 options", "value": "nothing kept",
                         "detail": "layer height 0.2,\n0.24, 0.28 mm"})
    assert (status, body) == (200, {"recorded": True})
    row = history_rows(plugin)[-1]
    assert (row["kind"], row["label"], row["value"], row["title_hint"], row["undo"]) == \
        ("slice", "Compared 3 options", "nothing kept", "note", None)
    assert row["where"] == "layer height 0.2, 0.24, 0.28 mm"
    call(plugin, "PUT", "/api/v1/config", {"wall_loops": 3})  # a note ends its group, as a slice does
    rows = history_rows(plugin)
    assert rows[-1]["group"] == row["group"] + 1


def test_activity_note_refused_row_and_validation(plugin):
    status, _ = call(plugin, "POST", "/api/v1/activity/note",
                     {"kind": "refused", "text": "Post-processing Scripts", "detail": "Blocked by the connector"})
    assert status == 200
    row = history_rows(plugin)[-1]
    assert (row["kind"], row["label"], row["value"], row["where"]) == \
        ("refused", "Post-processing Scripts", "Not allowed", "Blocked by the connector")
    assert call(plugin, "POST", "/api/v1/activity/note", {"kind": "setting", "text": "x"}) == \
        (400, {"error": "bad_kind"})
    assert call(plugin, "POST", "/api/v1/activity/note", {"kind": "slice", "text": "  "}) == \
        (400, {"error": "missing_text"})
    status, _ = call(plugin, "POST", "/api/v1/activity/note", {"kind": "slice", "text": "é" * 200})
    assert status == 200 and len(history_rows(plugin)[-1]["label"].encode()) <= 160
    assert call(plugin, "GET", "/api/v1/user_changes?since=0")[1]["changes"] == []  # changes nothing


def test_activity_note_refused_while_access_is_off(plugin):
    plugin._paused = True
    status, body = call(plugin, "POST", "/api/v1/activity/note", {"kind": "slice", "text": "Compared 2 options"})
    plugin._paused = False
    assert status == 503 and body["error"] == "paused"
    assert not posts(plugin, "history") or all(r["label"] != "Compared 2 options" for r in history_rows(plugin))


def test_activity_note_always_starts_its_own_group(plugin):
    """Fork 669fb36929: a note never joins a group left open by an earlier change."""
    call(plugin, "PUT", "/api/v1/config", {"wall_loops": 3})
    call(plugin, "POST", "/api/v1/activity/note", {"kind": "slice", "text": "Compared 2 options", "value": "nothing kept"})
    rows = history_rows(plugin)
    assert rows[-1]["group"] == rows[-2]["group"] + 1


def test_activity_note_drops_control_and_format_characters(plugin):
    call(plugin, "POST", "/api/v1/activity/note",
         {"kind": "refused", "text": "Post‮-processing\tScripts​\x07", "detail": "﻿Blocked\x85 here"})
    row = history_rows(plugin)[-1]
    assert row["label"] == "Post-processing Scripts" and row["where"] == "Blocked here"


def _switch_rows(plugin, mode):
    S.switch_mode = mode
    call(plugin, "PUT", "/api/v1/config", {"wall_loops": 3})
    call(plugin, "PUT", "/api/v1/preset", {"type": "print", "name": "My Fast"})
    rows = last(plugin, "history")["rows"]
    return next(r for r in rows if r["label"] == "Wall loops"), next(r for r in rows if r["kind"] == "preset")


def test_preset_switch_that_discards_marks_the_row_dropped(plugin):
    """Fork 669fb36929: an edit the assistant's own preset switch dropped is shown as dropped, never as current."""
    row, _ = _switch_rows(plugin, "discard")
    assert row["dropped"] is True and row["superseded"] is True
    page(plugin, {"action": "live"})
    assert str(row["id"]) not in last(plugin, "live")["values"]
    page(plugin, {"action": "undo", "row": row["id"]})
    res = wait_result(plugin, row["id"])
    assert res["status"] == "failed" and res["reason"] == "Dropped by a preset switch"


def test_preset_switch_that_transfers_or_saves_is_not_dropped(plugin):
    for mode in ("transfer", "save"):
        S.bundle.prints._by("0.20mm Standard @Fake")._cfg["wall_loops"] = "2"
        S.bundle.prints._by("0.20mm Standard @Fake")._saved["wall_loops"] = "2"
        S.bundle.prints.sel = "0.20mm Standard @Fake"
        row, _ = _switch_rows(plugin, mode)
        assert row["dropped"] is False and row["superseded"] is True, mode  # written to the preset left behind


def test_switching_back_tracks_the_rows_again(plugin):
    row, preset_row = _switch_rows(plugin, "keep")
    page(plugin, {"action": "undo", "row": preset_row["id"]})
    assert wait_result(plugin, preset_row["id"])["status"] == "ok"
    again = next(r for r in last(plugin, "history")["rows"] if r["id"] == row["id"])
    assert again["superseded"] is False and again["dropped"] is False


def test_a_switch_the_user_made_keeps_the_rows_tracked(plugin):
    call(plugin, "PUT", "/api/v1/config", {"wall_loops": 3})
    row = history_rows(plugin)[-1]
    S.bundle.prints.sel = "My Fast"  # by hand: no row
    page(plugin, {"action": "live"})
    assert str(row["id"]) in last(plugin, "live")["values"]
    assert last(plugin, "history")["rows"][-1].get("superseded") is False


def test_trial_header_does_not_hide_a_preset_switch(plugin):
    """The fork counts the trial header only on settings writes and slices; a preset switch records as usual."""
    import urllib.request
    req = urllib.request.Request(plugin.base + "/api/v1/preset", method="PUT",
                                 data=json.dumps({"type": "print", "name": "My Fast"}).encode(),
                                 headers={"X-Api-Token": plugin._token, "X-Orca-Mcp-Trial": "1",
                                          "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15).read()
    assert history_rows(plugin)[-1]["kind"] == "preset"


# -- panel closed banner (Max: "show one of the small banners ... where to get it back and also have an undo") ------
def test_closing_the_panel_shows_a_banner_with_undo(plugin):
    call(plugin, "GET", "/api/v1/status")  # the panel is open
    panel = S.panels[-1]
    UI.run_blocking(panel.close)  # the user closes the pane
    text, link, on_click = S.links[-1]
    assert link == "Undo" and "panel closed" in text and "Space" in text
    count = len(S.panels)
    assert UI.run_blocking(on_click) is True  # the banner closes itself
    assert len(S.panels) == count + 1 and S.panels[-1].is_open()


def test_no_banner_when_the_plugin_closes_its_own_panel(tmp_path):
    fake_orca.STATE.reset(storage=str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps({"port": 0}))
    cap = mod.McpCapability()
    UI.run_blocking(cap.on_load)
    cap.on_unload()  # Activate unticked: the plugin closes the pane itself
    assert S.links == []


# -- multi-line values (fork b007932e5f, followups-report.md section 2: the exact format) ------------------------------
def test_multiline_formatter_matches_the_build():
    d, t = mod.multiline_display, mod.multiline_tip
    g4 = "G28 ; home\nG1 Z5\nM104 S200\nG92 E0"
    g4b = "G28 ; home\nG1 Z5\nM104 S210\nG92 E0"
    assert d(g4b, g4) == "line 3: M104 S210 (4 lines)" and d(g4, g4b) == "line 3: M104 S200 (4 lines)"
    assert t(g4b, g4) == "Lines 3 to 4 of 4:\nM104 S210\nG92 E0"
    assert d("G28 ; home\nG1 Z5\nM104 S200\nG92 E0\nM84", g4) == "line 5: M84 (5 lines)"
    assert d(g4, "G28 ; home\nG1 Z5\nM104 S200\nG92 E0\nM84") == "G28 ; home … (4 lines)"  # no line 5 here
    fourteen = "\n".join(["G28 ; home"] + [f"G1 X{i}" for i in range(13)])
    assert d(fourteen, None) == "G28 ; home … (14 lines)"
    assert t(fourteen, None) == "Lines 1 to 5 of 14:\nG28 ; home\nG1 X0\nG1 X1\nG1 X2\nG1 X3\n…"
    assert d("x" * 50 + "\nb", None) == "x" * 40 + "… (2 lines)"  # one ellipsis when line 1 was cut
    assert d("\t \nsecond", None) == "(empty line) … (2 lines)"
    assert d("a  \r\nb\n", None) == "a … (2 lines)"  # \r\n is one break, a final break only ends the last line
    assert d("single", None) == "" and t("single", None) == ""
    assert t("a\n" + "y" * 70, "a\nz") == "Line 2 of 2:\n" + "y" * 60 + "…"


def test_config_text_unescapes_like_orcaslicer():
    assert mod.config_text("notes", "a\\nb\\\\c\\r") == "a\nb\\c\r"
    assert mod.config_text("filament_notes", '"first\\nsecond";"third"') == "first\nsecond\nthird"
    assert mod.config_text("notes", "bad\\") is None


def test_multiline_setting_row(plugin):
    status, _ = call(plugin, "PUT", "/api/v1/config", {"notes": "Line one\nLine two\nLine three"})
    assert status == 200
    row = history_rows(plugin)[-1]
    assert row["value"] == "Line one … (3 lines)" and row["before"] == ""
    assert row["value_tip"] == "Lines 1 to 3 of 3:\nLine one\nLine two\nLine three" and "before_tip" not in row
    target = plugin.host.hist.row(row["id"])["target"]
    assert target["multiline"] is True
    page(plugin, {"action": "live"})
    assert last(plugin, "live")["values"][str(row["id"])]["value"] == row["value"]  # just written: its own value


def test_multiline_live_value_sees_an_edit_past_the_cut(plugin):
    base = "Line one\nLine two\n" + "z" * 45
    call(plugin, "PUT", "/api/v1/config", {"notes": base})
    call(plugin, "PUT", "/api/v1/config", {"notes": "Line one\nLine two\n" + "z" * 45 + "!"})
    row = history_rows(plugin)[-1]
    assert row["value"] == "line 3: " + "z" * 40 + "… (3 lines)"
    cfg = S.bundle.prints._by("0.20mm Standard @Fake")._cfg
    cfg["notes"] = ("Line one\nLine two\n" + "z" * 45 + "?").replace("\n", "\\n")  # edited by hand past the cut
    page(plugin, {"action": "live"})
    assert last(plugin, "live")["values"][str(row["id"])]["value"] == "changed since"
    page(plugin, {"action": "undo", "row": row["id"]})
    assert wait_result(plugin, row["id"])["reason"] == "You changed it since"
    cfg["notes"] = base.replace("\n", "\\n")  # set back by hand
    page(plugin, {"action": "live"})
    assert last(plugin, "live")["values"][str(row["id"])]["value"] == row["before"]


def test_multiline_undo_writes_the_raw_back(plugin):
    call(plugin, "PUT", "/api/v1/config", {"notes": "a\nb"})
    row = history_rows(plugin)[-1]
    page(plugin, {"action": "undo", "row": row["id"]})
    assert wait_result(plugin, row["id"])["status"] == "ok"
    assert S.bundle.prints._by("0.20mm Standard @Fake")._cfg["notes"] == ""


def test_multiline_cases_ported_from_the_builds_standalone_test():
    """Ported from the OrcaSlicer MCP build's standalone history tests (multiline_values)."""
    d, t, E = mod.multiline_display, mod.multiline_tip, "…"
    a = "G28 ; home\nG1 Z5\nM104 S200\nG92 E0\n"
    b = "G28 ; home\nG1 Z5\nM104 S210\nG92 E0\n"
    assert d("G28 ; home", None) == "" and d("G28\n", None) == "" and t("G28", None) == ""
    assert d(a, None) == "G28 ; home " + E + " (4 lines)"
    assert d(a, "M117 hi\nG28") == "G28 ; home " + E + " (4 lines)"
    assert d(b, a) == "line 3: M104 S210 (4 lines)" and d(a, b) == "line 3: M104 S200 (4 lines)"
    a5 = a + "M84"
    assert d(a5, a) == "line 5: M84 (5 lines)" and d(a, a5) == "G28 ; home " + E + " (4 lines)"
    assert d(a, a) == "G28 ; home " + E + " (4 lines)"
    assert d("G28  \r\nG1", None) == "G28 " + E + " (2 lines)"
    assert d("\nG1", None) == "(empty line) " + E + " (2 lines)"
    assert d("x" * 50 + "\nG1", None) == "x" * 40 + E + " (2 lines)"
    assert d("é" * 45 + "\nG1", None) == "é" * 40 + E + " (2 lines)"  # cut by characters, not bytes
    assert t(b, a) == "Lines 3 to 4 of 4:\nM104 S210\nG92 E0"
    assert t(a, None) == "Lines 1 to 4 of 4:\nG28 ; home\nG1 Z5\nM104 S200\nG92 E0"
    assert t(a5, a) == "Line 5 of 5:\nM84"
    seven = "".join(f"L{i}\n" for i in range(1, 8))
    assert t(seven, None) == "Lines 1 to 5 of 7:\nL1\nL2\nL3\nL4\nL5\n" + E


def _routes(datadir=None):
    return {a["id"]: a for a in mod.setup_apps(datadir)}


def test_config_tab_offers_five_companies_and_no_token_anywhere(plugin):
    apps = _routes()
    assert list(apps) == ["claude", "openai", "cursor", "vscode", "other"]
    claude = {r["label"]: r for r in apps["claude"]["routes"]}
    assert claude["Claude app"]["open"] == "https://github.com/MaxEllis/orcaslicer-mcp/releases/latest"
    assert claude["Claude Code"]["copy"] == "claude mcp add --scope user orcaslicer -- uvx orcaslicer-mcp"
    openai = {r["label"]: r for r in apps["openai"]["routes"]}
    assert openai["ChatGPT desktop app"]["copy"] == "uvx orcaslicer-mcp" and openai["ChatGPT desktop app"]["also"] == ""
    assert openai["Codex in a terminal or editor"]["copy"] == "codex mcp add orcaslicer -- uvx orcaslicer-mcp"
    assert openai["ChatGPT on the web or phone"]["off"] is True
    assert apps["openai"]["ask"] == "In a Codex chat, ask"
    assert json.loads(apps["other"]["routes"][0]["copy"]) == {
        "mcpServers": {"orcaslicer": {"command": "uvx", "args": ["orcaslicer-mcp"]}}}
    assert json.loads(apps["vscode"]["routes"][0]["copy"]) == {
        "servers": {"orcaslicer": {"type": "stdio", "command": "uvx", "args": ["orcaslicer-mcp"]}}}
    blob = json.dumps(apps) + plugin.get_config_ui().split("var APPS", 1)[1]
    assert plugin._token not in blob and "ORCA_API_TOKEN" not in blob  # the connector finds the token by itself


def test_config_tab_one_click_links_carry_the_server_and_no_secret():
    import base64
    import urllib.parse
    apps = _routes()
    cur = apps["cursor"]["routes"][0]["open"]
    assert cur.startswith("cursor://anysphere.cursor-deeplink/mcp/install?name=orcaslicer&config=")
    assert json.loads(base64.b64decode(urllib.parse.unquote(cur.split("config=", 1)[1]))) == {
        "command": "uvx", "args": ["orcaslicer-mcp"]}
    vs = apps["vscode"]["routes"][0]["open"]
    assert vs.startswith("https://vscode.dev/redirect/mcp/install?name=orcaslicer&config=")
    assert json.loads(urllib.parse.unquote(vs.split("config=", 1)[1])) == {
        "type": "stdio", "command": "uvx", "args": ["orcaslicer-mcp"]}
    assert mod.openable(cur) and mod.openable(vs) and mod.openable(apps["claude"]["routes"][0]["open"])
    assert not mod.openable("https://example.com/") and not mod.openable("file:///etc/passwd")
    # only the links offered now, whole: one that merely starts like them installs something else
    assert not mod.openable("cursor://anysphere.cursor-deeplink/mcp/install?name=x&config=e30=")
    assert not mod.openable(vs + "&x=1") and not mod.openable(None)
    own = _routes("D:/portable/data")["cursor"]["routes"][0]["open"]
    assert own != cur and not mod.openable(own) and mod.openable(own, "D:/portable/data")


def test_config_tab_names_only_what_the_connector_cant_find_by_itself():
    """The settings folder, when OrcaSlicer uses its own. Never the port: the connector reads it from the plugin's
    connection file, so moving the port later breaks no app set up before."""
    apps = _routes(datadir="D:/Orca Data/profile")
    assert apps["claude"]["routes"][1]["copy"] == (
        'claude mcp add --scope user orcaslicer --env ORCA_DATADIR="D:/Orca Data/profile" -- uvx orcaslicer-mcp')
    assert json.loads(apps["other"]["routes"][0]["copy"])["mcpServers"]["orcaslicer"]["env"] == {
        "ORCA_DATADIR": "D:/Orca Data/profile"}
    assert "ORCA_DATADIR = D:/Orca Data/profile" in apps["openai"]["routes"][0]["also"]
    # the Claude extension has a field for it
    assert apps["claude"]["routes"][0]["also"] == (
        "In the extension's settings, set OrcaSlicer settings folder to D:/Orca Data/profile.")
    assert _routes()["claude"]["routes"][0]["also"] == "" and "ORCA_API_URL" not in json.dumps(_routes())


def test_config_tab_script_runs_on_the_data(plugin):
    """The page's script parses with its values filled in (node), so a data shape change can't break it."""
    import re
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node") or ("/snap/bin/node" if os.path.exists("/snap/bin/node") else None)
    if not node:
        pytest.skip("node not installed")
    js = re.search(r"<script>(.*?)</script>", plugin.get_config_ui(), re.S).group(1)
    out = subprocess.run([node, "-e", "new Function(" + json.dumps(js) + ")"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr


def test_custom_datadir_is_named_only_when_orcaslicer_uses_its_own(monkeypatch, tmp_path):
    std = tmp_path / "std" / "OrcaSlicer"
    monkeypatch.setattr(mod, "standard_datadir", lambda: str(std))
    assert mod.custom_datadir(str(std / "orca_plugins" / "plugin_data" / "orcaslicer_mcp_plugin")) is None
    own = tmp_path / "own-datadir"
    assert mod.custom_datadir(str(own / "orca_plugins" / "plugin_data" / "orcaslicer_mcp_plugin")) == str(own)
    assert mod.custom_datadir("") is None
    # installed from OrcaCloud: plugin_data/_subscribed/<user>/<plugin>
    cloud = ("orca_plugins", "plugin_data", "_subscribed", "user-1", "16d116a6bc41")
    assert mod.custom_datadir(str(std.joinpath(*cloud))) is None
    assert mod.custom_datadir(str(own.joinpath(*cloud))) == str(own)
    assert mod.custom_datadir(str(tmp_path / "elsewhere" / "plugin")) is None


def test_config_ui_fills_in_its_values(plugin):
    html = plugin.get_config_ui()
    for marker in ("__TOKEN__", "__PORT__", "__SETUP__", "__ASK__", "__ICONS__"):
        assert marker not in html
    assert "LISTENING = " in html and json.dumps(plugin._token) in html


def test_config_tab_asks_the_plugin_to_open_links_and_hears_back(plugin, monkeypatch):
    opened = []
    monkeypatch.setattr(mod, "open_url", lambda url: opened.append(url) or True)
    url = "https://github.com/MaxEllis/orcaslicer-mcp/releases/latest"
    S.config = {**S.config, "open": {"n": 1, "url": url, "at": time.time()}}
    UI.run_blocking(plugin._poll_config)
    assert opened == [url] and S.config["opened"] == {"n": 1, "ok": True}
    UI.run_blocking(plugin._poll_config)  # the same request is not opened twice
    assert opened == [url]
    S.config = {**S.config, "open": {"n": 2, "url": "https://example.com/phish", "at": time.time()}}
    UI.run_blocking(plugin._poll_config)  # anything off the list is refused, and the page hears so
    assert opened == [url] and S.config["opened"] == {"n": 2, "ok": False}
    # a request made while the panel was closed (nobody polled) is dropped when the panel opens, not opened late
    S.config = {**S.config, "open": {"n": 3, "url": url, "at": time.time() - 60}}
    UI.run_blocking(plugin._poll_config)
    assert opened == [url] and S.config["opened"] == {"n": 3, "ok": False}
    S.config = {**S.config, "open": {"n": 4, "url": url}}  # no time: an old page's request
    UI.run_blocking(plugin._poll_config)
    assert opened == [url] and S.config["opened"] == {"n": 4, "ok": False}


def test_the_panel_gets_the_setup_until_an_app_connects_then_goals(plugin):
    """First run: with no change yet, the panel shows how to connect (setup message); the first request from an app
    switches it to 'Connected to <app>' with goals to try."""
    page(plugin, {"action": "ready"})
    time.sleep(0.2)
    first = last(plugin, "setup")
    assert [a["id"] for a in first["apps"]] == ["claude", "openai", "cursor", "vscode", "other"]
    assert first["status"] is None and first["ask"] == mod.SETUP_ASK and len(first["goals"]) == 3
    assert all(a["icon"].startswith("<svg") for a in first["apps"])
    assert plugin._token not in json.dumps(first)
    import urllib.request
    req = urllib.request.Request(plugin.base + "/api/v1/status")
    req.add_header("X-Api-Token", plugin._token)
    req.add_header("X-OrcaSlicer-MCP-Client", "Claude Code")
    urllib.request.urlopen(req, timeout=10).read()
    UI.run_blocking(plugin._poll_config)
    after = last(plugin, "setup")
    assert after["status"]["app"] == "Claude Code" and after["status"]["seen"]


def test_the_panel_opens_only_listed_links(plugin, monkeypatch):
    opened = []
    monkeypatch.setattr(mod, "open_url", lambda url: opened.append(url) or True)
    page(plugin, {"action": "open", "url": "https://github.com/MaxEllis/orcaslicer-mcp/releases/latest"})
    page(plugin, {"action": "open", "url": "https://example.com/elsewhere"})
    assert opened == ["https://github.com/MaxEllis/orcaslicer-mcp/releases/latest"]
