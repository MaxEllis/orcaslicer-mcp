"""The user-change journal (GET /api/v1/user_changes).

The first half ports the OrcaSlicer MCP build's own standalone journal tests case by case, so both hosts word and coalesce
entries the same way. The build's three arrange/orient job-guard cases have no counterpart here (the plugin
cannot arrange or orient). The second half drives the route through the plugin."""
import copy
import json
import math
import time

import fake_orca
from conftest import call, mod

ARROW = "\u2192"
S = fake_orca.STATE
UI = fake_orca.UI


# ---------------------------------------------------------------- ported from test_mcp_journal.cpp
class Fns:
    @staticmethod
    def label(k):
        return {"wall_loops": "Wall loops", "sparse_infill_density": "Sparse infill density",
                "outer_wall_speed": "Outer wall speed", "printhost_apikey": "API Key",
                "nozzle_temperature": "Nozzle temperature", "curr_bed_type": "Plate type",
                "filament_colour": "Filament colour", "wipe_tower_x": "Wipe tower X",
                "flush_volumes_matrix": "Flushing volumes"}.get(k, k)

    @staticmethod
    def value(k, raw):
        if k == "outer_wall_speed":
            return raw + " mm/s"
        if k == "nozzle_temperature":
            return raw + " \u00b0C"
        return raw

    @staticmethod
    def sensitive(k):
        return k == "printhost_apikey"


def snap(preset, values, saved=None):
    return {"preset": preset, "values": dict(values), "saved": dict(saved or {})}


def base():
    return {"project_gen": 0, "project_text": "",
            "print": snap("0.20mm Standard", {"wall_loops": "3", "sparse_infill_density": "15%",
                                              "outer_wall_speed": "100"}),
            "printer": snap("Printer A", {"printhost_apikey": "secret-old", "nozzle_diameter": "0.8"}),
            "filament_slots": ["PLA"], "filaments": {"PLA": snap("PLA", {"nozzle_temperature": "210"})},
            "objects": [], "plates": 1}


def place(tx=0.0, ty=0.0, tz=0.0, rot=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0)):
    return ((tx, ty, tz), rot, scale, (1.0, 1.0, 1.0))


def obj(oid, name, p=None):
    return {"id": oid, "name": name, "instance_ids": [oid * 100], "transforms": [p or place()], "config": {}}


def journal(b):
    j = mod.Journal(first_seq=0, text=Fns)
    j.begin_request(copy.deepcopy(b), "10:00")
    return j


def texts(r):
    return [c["text"] for c in r["changes"]]


def begin(j, b, hhmm):
    j.begin_request(copy.deepcopy(b), hhmm)


def test_first_request_only_baselines():
    j = journal(base())
    assert j.since(0)["changes"] == [] and j.since(0)["seq"] == 0


def test_setting_change_text():
    j, b = journal(base()), base()
    b["print"]["values"]["wall_loops"] = "4"
    begin(j, b, "10:05")
    r = j.since(0)
    assert r["changes"] == [{"seq": 1, "time": "10:05", "kind": "setting", "group": "Print",
                             "text": f"Wall loops 3 {ARROW} 4", "key": "wall_loops"}] and r["seq"] == 1
    b["print"]["values"]["outer_wall_speed"] = "120"
    begin(j, b, "10:06")
    assert texts(j.since(1)) == [f"Outer wall speed 100 mm/s {ARROW} 120 mm/s"]


def test_net_change_only():
    j, b = journal(base()), base()
    b["print"]["values"]["wall_loops"] = "5"
    begin(j, b, "10:01")  # a read-only API request between the two hand edits
    j.end_request(copy.deepcopy(b))
    b["print"]["values"]["wall_loops"] = "4"
    begin(j, b, "10:02")
    r = j.since(0)
    assert texts(r) == [f"Wall loops 3 {ARROW} 4"]
    assert r["changes"][0]["seq"] == 2 and r["changes"][0]["time"] == "10:02" and r["seq"] == 2


def test_changed_back_disappears():
    j, b = journal(base()), base()
    b["print"]["values"]["wall_loops"] = "5"
    begin(j, b, "10:01")
    b["print"]["values"]["wall_loops"] = "3"
    begin(j, b, "10:02")
    r = j.since(0)
    assert r["changes"] == [] and r["seq"] == 2


def test_preset_switch_is_one_entry():
    j, b = journal(base()), base()
    b["print"]["values"]["sparse_infill_density"] = "20%"  # edited on the old preset, then the switch drops it
    begin(j, b, "10:01")
    b["print"] = snap("0.30mm Draft", {"wall_loops": "2", "sparse_infill_density": "10%", "outer_wall_speed": "200"},
                      {"outer_wall_speed": "150"})
    begin(j, b, "10:02")
    r = j.since(0)
    assert texts(r) == ["Print preset switched to 0.30mm Draft", f"Outer wall speed 150 mm/s {ARROW} 200 mm/s"]
    assert r["changes"][0]["kind"] == "preset" and r["changes"][0]["group"] == "Print"
    # Switching back: the switch disappears (net), the dirty value on the old preset stays reported.
    b["print"] = snap("0.20mm Standard", dict(base()["print"]["values"], wall_loops="6"), {"wall_loops": "3"})
    begin(j, b, "10:03")
    assert texts(j.since(0)) == [f"Wall loops 3 {ARROW} 6"]


def test_filament_slots_named():
    b0 = base()
    b0["filament_slots"] = ["PLA", "PETG", "PLA"]
    b0["filaments"]["PETG"] = snap("PETG", {"nozzle_temperature": "240"})
    j, b = journal(b0), copy.deepcopy(b0)
    b["filaments"]["PLA"]["values"]["nozzle_temperature"] = "215"
    begin(j, b, "10:01")
    assert texts(j.since(0)) == [f"Nozzle temperature 210 \u00b0C {ARROW} 215 \u00b0C (filaments 1, 3)"]
    # Slot 3 switched: one entry, and the earlier edit now applies to slot 1 only.
    b["filament_slots"][2] = "PETG"
    begin(j, b, "10:02")
    assert texts(j.since(0)) == [f"Nozzle temperature 210 \u00b0C {ARROW} 215 \u00b0C (filament 1)",
                                 "Filament preset switched to PETG (filament 3)"]
    # A single filament: no slot in the text.
    j1, b1 = journal(base()), base()
    b1["filaments"]["PLA"]["values"]["nozzle_temperature"] = "200"
    begin(j1, b1, "10:03")
    assert texts(j1.since(0)) == [f"Nozzle temperature 210 \u00b0C {ARROW} 200 \u00b0C"]
    # A filament added.
    b1["filament_slots"].append("PLA")
    begin(j1, b1, "10:04")
    assert texts(j1.since(1)) == ["Added filament 2"]


def test_project_open_replaces_earlier_entries():
    j, b = journal(base()), base()
    b["print"]["values"]["wall_loops"] = "5"
    b["objects"] = [obj(1, "a.stl")]
    begin(j, b, "10:01")
    j.add("panel", "Panel", "Reverted in the panel: Wall loops back to 3", "10:02")
    j.note_project("Opened project bracket.3mf")
    b["project_gen"], b["project_text"] = j.project_gen, j.project_text
    b["print"]["values"]["wall_loops"] = "2"
    b["objects"] = [obj(7, "bracket.stl"), obj(8, "lid.stl")]
    b["plates"] = 3
    begin(j, b, "10:03")
    r = j.since(0)
    assert texts(r) == ["Opened project bracket.3mf"]
    assert r["changes"][0]["kind"] == "project" and r["changes"][0]["group"] == "Project"
    b["print"]["values"]["wall_loops"] = "4"
    begin(j, b, "10:04")
    assert texts(j.since(0)) == ["Opened project bracket.3mf", f"Wall loops 2 {ARROW} 4"]
    j.note_project("New project")
    b["project_gen"], b["project_text"] = j.project_gen, j.project_text
    b["objects"] = []
    begin(j, b, "10:05")
    assert texts(j.since(0)) == ["New project"]


def test_object_added_removed_moved_rotated_scaled():
    b0 = base()
    b0["objects"] = [obj(1, "a.stl"), obj(2, "b.stl"), obj(3, "c.stl"), obj(4, "d.stl")]
    j, b = journal(b0), copy.deepcopy(b0)
    b["objects"][0]["transforms"][0] = place(10)                                # moved
    b["objects"][1]["transforms"][0] = place(5, 5, rot=(0.0, 0.0, math.pi / 2))  # rotated (and moved)
    b["objects"][2]["transforms"][0] = place(scale=(2.0, 2.0, 2.0))             # scaled
    del b["objects"][3]                                                         # d removed
    b["objects"].append(obj(5, "e.stl"))                                        # e added
    begin(j, b, "10:01")
    r = j.since(0)
    assert texts(r) == ["Moved a.stl", "Rotated b.stl", "Scaled c.stl", "Added e.stl", "Removed d.stl"]
    assert all(c["kind"] == "object" and c["group"] == "Model" and "key" not in c for c in r["changes"])
    # Added then removed: nothing; moved twice: one entry; a copy added.
    b["objects"].pop()
    b["objects"][0]["transforms"][0] = place(20)
    b["objects"][2]["instance_ids"].append(999)
    b["objects"][2]["transforms"].append(place(50))
    begin(j, b, "10:02")
    assert texts(j.since(0)) == ["Rotated b.stl", "Scaled c.stl", "Removed d.stl", "Moved a.stl",
                                 "Added a copy of c.stl"]


def test_mirror_counts_as_rotated():
    b0 = base()
    b0["objects"] = [obj(1, "a.stl")]
    j, b = journal(b0), copy.deepcopy(b0)
    b["objects"][0]["transforms"][0] = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (-1.0, 1.0, 1.0))
    begin(j, b, "10:01")
    assert texts(j.since(0)) == ["Rotated a.stl"]


def test_plates_added():
    # The plugin cannot count plates (it passes None), but the logic matches the build when it can.
    j, b = journal(base()), base()
    b["plates"] = 2
    begin(j, b, "10:01")
    r = j.since(0)
    assert texts(r) == ["Added a plate"] and r["changes"][0]["kind"] == "plate" and r["changes"][0]["group"] == "Plates"
    b["plates"] = 4
    begin(j, b, "10:02")
    assert texts(j.since(0)) == ["Added 3 plates"] and texts(j.since(1)) == ["Added 2 plates"]
    b["plates"] = 1
    begin(j, b, "10:03")
    assert j.since(0)["changes"] == [] and texts(j.since(2)) == ["Removed 3 plates"]
    b["plates"] = 2
    j.end_request(copy.deepcopy(b))
    b["plates"] = 1
    begin(j, b, "10:04")
    assert texts(j.since(3)) == ["Removed a plate"]


def test_no_plate_count_no_plate_entries():
    j, b = journal(dict(base(), plates=None)), dict(base(), plates=None)
    begin(j, b, "10:01")
    assert j.since(0)["changes"] == []


def test_no_change_no_entries():
    j, b = journal(base()), base()
    b["objects"] = [obj(1, "a.stl")]
    j.end_request(copy.deepcopy(b))
    begin(j, b, "10:01")
    begin(j, b, "10:02")
    r = j.since(0)
    assert r["changes"] == [] and r["seq"] == 0


def test_transform_within_tolerance_is_no_change():
    b0 = base()
    b0["objects"] = [obj(1, "a.stl", place(100, 50))]
    j, b = journal(b0), copy.deepcopy(b0)
    b["objects"][0]["transforms"][0] = place(100 * (1 + 1e-7), 50, scale=(1 + 1e-7, 1.0, 1.0))
    begin(j, b, "10:01")
    assert j.since(0)["changes"] == []
    b["objects"][0]["transforms"][0] = place(100.5, 50)
    begin(j, b, "10:02")
    assert texts(j.since(0)) == ["Moved a.stl"]


def test_coalesce_per_key():
    j, b = journal(base()), base()
    b["print"]["values"]["wall_loops"] = "4"
    b["print"]["values"]["sparse_infill_density"] = "20%"
    begin(j, b, "10:01")  # seq 1 infill, 2 wall (key order)
    b["print"]["values"]["wall_loops"] = "5"
    begin(j, b, "10:02")  # seq 3 wall 4 -> 5
    r = j.since(0)
    assert texts(r) == [f"Sparse infill density 15% {ARROW} 20%", f"Wall loops 3 {ARROW} 5"]
    assert r["changes"][1]["seq"] == 3 and r["seq"] == 3
    assert texts(j.since(2)) == [f"Wall loops 4 {ARROW} 5"]
    assert j.since(3)["changes"] == [] and j.since(3)["seq"] == 3
    # The API set it in between (7, not reported): the hand edit after it starts over from 7.
    b["print"]["values"]["wall_loops"] = "7"
    j.end_request(copy.deepcopy(b))
    b["print"]["values"]["wall_loops"] = "3"
    begin(j, b, "10:03")
    assert texts(j.since(0)) == [f"Sparse infill density 15% {ARROW} 20%", f"Wall loops 7 {ARROW} 3"]


def test_sensitive_key_hides_values():
    j, b = journal(base()), base()
    b["printer"]["values"]["printhost_apikey"] = "secret-new"
    begin(j, b, "10:01")
    r = j.since(0)
    assert texts(r) == ["API Key changed"]
    assert r["changes"][0]["group"] == "Printer" and r["changes"][0]["key"] == "printhost_apikey"
    assert "secret" not in json.dumps(r)
    for e in j._entries:
        assert "secret" not in e.old_cmp + e.new_cmp + e.text + e.old_text + e.new_text
    b["printer"]["values"]["printhost_apikey"] = "secret-old"
    begin(j, b, "10:02")
    assert j.since(0)["changes"] == []


def test_panel_entries_and_limit():
    j = journal(base())
    j.add("panel", "Panel", "Restored in the panel to 10:31", "10:40")
    r = j.since(0)
    assert r["changes"] == [{"seq": 1, "time": "10:40", "kind": "panel", "group": "Panel",
                             "text": "Restored in the panel to 10:31"}]
    for i in range(250):
        j.add("panel", "Panel", f"x{i}", "10:41")
    assert len(j._entries) == mod.JOURNAL_MAX and j.seq == 251
    r = j.since(0)
    assert len(r["changes"]) == mod.JOURNAL_MAX and r["changes"][0]["seq"] == 52


def test_invalidate_skips_one_diff():
    j, b = journal(base()), base()
    b["print"]["values"]["wall_loops"] = "9"  # the API's change, whose end-of-request baseline failed
    j.invalidate()
    begin(j, b, "10:01")
    assert j.since(0)["changes"] == []


def test_seq_seeded_and_reset():
    j = mod.Journal(first_seq=1000, text=Fns)
    assert j.seq == 1000 and j.since(0) == {"seq": 1000, "changes": []}
    j.add("panel", "Panel", "Restored in the panel to 10:31", "10:40")
    r = j.since(500)  # a seq from an earlier run
    assert r["seq"] == 1001 and len(r["changes"]) == 1 and r["changes"][0]["seq"] == 1001 and "reset" not in r
    assert j.since(1001)["changes"] == [] and "reset" not in j.since(1001)
    r = j.since(5000)  # newer than anything here (the clock stepped back): everything, marked reset
    assert r["reset"] is True and r["seq"] == 1001 and len(r["changes"]) == 1


def test_default_seq_starts_at_the_clock():
    before = int(time.time() * 1000)
    assert mod.Journal().seq >= before


# ---------------------------------------------------------------- value text (McpRecorder::config_value_text)
def test_project_settings_are_compared():
    """I3: the project settings PUT /config writes are compared too (group "Project"), filament colours per slot;
    keys sized by the filament or plate count only while that count stays."""
    b0 = base()
    b0["project"] = {"curr_bed_type": "Cool Plate", "wipe_tower_x": "15", "flush_volumes_matrix": "0"}
    b0["filament_colours"] = ["#FF0000"]
    j, b = journal(b0), copy.deepcopy(b0)
    b["project"]["curr_bed_type"] = "Textured PEI Plate"
    b["filament_colours"] = ["#00FF00"]
    begin(j, b, "10:01")
    r = j.since(0)
    assert texts(r) == [f"Plate type Cool Plate {ARROW} Textured PEI Plate", f"Filament colour #FF0000 {ARROW} #00FF00"]
    assert all(c["kind"] == "setting" and c["group"] == "Project" for c in r["changes"])
    assert r["changes"][0]["key"] == "curr_bed_type" and r["changes"][1]["key"] == "filament_colour"
    # Two filaments: the colour names its slot, and coalesces per slot.
    c0 = copy.deepcopy(b)
    c0["filament_slots"] = ["PLA", "PLA"]
    c0["filament_colours"] = ["#00FF00", "#0000FF"]
    k, c = journal(c0), copy.deepcopy(c0)
    c["filament_colours"][1] = "#FFFFFF"
    begin(k, c, "10:02")
    c["filament_colours"][1] = "#000000"
    begin(k, c, "10:03")
    assert texts(k.since(0)) == [f"Filament colour #0000FF {ARROW} #000000 (filament 2)"]
    # A filament added: its colour and the flush matrix resize with it, which is no edit of their own.
    d = copy.deepcopy(c)
    d["filament_slots"] = ["PLA", "PLA", "PLA"]
    d["filament_colours"] = ["#00FF00", "#000000", "#123456"]
    d["project"]["flush_volumes_matrix"] = "0,1,1,0"
    begin(k, d, "10:04")
    allt = texts(k.since(0))
    assert allt[-1] == "Added filament 3" and len(allt) == 2
    # A plate added: the wipe tower vectors grow, not reported; moved on its own, it is.
    e = copy.deepcopy(d)
    e["plates"] = 2
    e["project"]["wipe_tower_x"] = "15,15"
    m = journal(d)
    begin(m, e, "10:05")
    assert texts(m.since(0)) == ["Added a plate"]
    e["project"]["wipe_tower_x"] = "15,80"
    begin(m, e, "10:06")
    assert texts(m.since(0))[-1] == f"Wipe tower X 15,15 {ARROW} 15,80"


def test_wipe_tower_without_a_plate_count():
    """The plugin cannot count plates: a per-plate list that changed length is a plate added, not an edit."""
    b0 = dict(base(), plates=None, project={"wipe_tower_x": "15"})
    j, b = journal(b0), copy.deepcopy(b0)
    b["project"]["wipe_tower_x"] = "15,15"
    begin(j, b, "10:01")
    assert j.since(0)["changes"] == []
    b["project"]["wipe_tower_x"] = "15,80"
    begin(j, b, "10:02")
    assert texts(j.since(0)) == [f"Wipe tower X 15,15 {ARROW} 15,80"]


def test_object_settings_are_compared():
    """I3: an object's own settings: "<label> of <object>", "default" when it does not set the key. (The build's
    third part, settings compared while the API's arrange job runs, has no counterpart: no jobs here.)"""
    b0 = base()
    b0["objects"] = [obj(1, "bracket.stl"), obj(2, "lid.stl")]
    b0["objects"][0]["config"] = {"wall_loops": "2"}
    j, b = journal(b0), copy.deepcopy(b0)
    b["objects"][0]["config"]["wall_loops"] = "4"
    b["objects"][1]["config"]["sparse_infill_density"] = "40%"
    begin(j, b, "10:01")
    r = j.since(0)
    assert texts(r) == [f"Wall loops of bracket.stl 2 {ARROW} 4", f"Sparse infill density of lid.stl default {ARROW} 40%"]
    assert r["changes"][0]["group"] == "Model" and r["changes"][0]["kind"] == "setting" \
        and r["changes"][0]["key"] == "wall_loops"
    # Taken off again: back to default, coalesced away.
    del b["objects"][1]["config"]["sparse_infill_density"]
    del b["objects"][0]["config"]["wall_loops"]
    begin(j, b, "10:02")
    assert texts(j.since(0)) == [f"Wall loops of bracket.stl 2 {ARROW} default"]


def test_object_sensitive_setting_hides_values():
    b0 = base()
    b0["objects"] = [obj(1, "a.stl")]
    j, b = journal(b0), copy.deepcopy(b0)
    b["objects"][0]["config"]["printhost_apikey"] = "x"
    begin(j, b, "10:01")
    assert texts(j.since(0)) == ["API Key of a.stl changed"]


def test_value_text_matches_the_build():
    vt = mod.value_text
    assert vt("layer_height", "0.2") == "0.2 mm"
    assert vt("enable_support", "1") == "On" and vt("enable_support", "0") == "Off"
    assert vt("seam_position", "aligned_back") == "Aligned back"
    assert vt("sparse_infill_density", "15%") == "15%"
    assert vt("wall_loops", "3") == "3"
    assert vt("unknown_key", "x") == "x" and vt("layer_height", "") == ""
    assert vt("machine_start_gcode", "G28\nG1 Z5") == "G28\u2026"
    long = "a" * 39 + "\u00e9"  # 41 bytes: cut at the character boundary
    assert vt("filament_notes", long) == "a" * 39 + "\u2026"
    either = next(k for k, (_, u) in mod.LABELS.items() if " or " in u and u.split(" or ")[0] not in ("", "%"))
    assert vt(either, "0.45") == "0.45 " + mod.LABELS[either][1].split(" or ")[0]
    assert vt(either, "110%") == "110%"


# ---------------------------------------------------------------- the route, through the plugin
def edit(preset_coll, name, key, value):
    preset_coll._by(name)._cfg[key] = value


def changes(plugin, since=0):
    status, body = call(plugin, "GET", f"/api/v1/user_changes?since={since}")
    assert status == 200, body
    return body


def test_route_reports_hand_edits_not_the_apis_own(plugin):
    call(plugin, "GET", "/api/v1/status")                                  # the first request: baseline only
    assert changes(plugin)["changes"] == []
    edit(S.bundle.prints, "0.20mm Standard @Fake", "wall_loops", "4")     # by hand
    call(plugin, "PUT", "/api/v1/config", {"layer_height": 0.28})          # the API's own change
    body = changes(plugin)
    assert [(c["kind"], c["group"], c["text"], c.get("key")) for c in body["changes"]] == \
        [("setting", "Print", f"Wall loops 2 {ARROW} 4", "wall_loops")]
    assert body["seq"] == body["changes"][-1]["seq"] and len(body["changes"][0]["time"]) == 5
    assert changes(plugin, body["seq"])["changes"] == []


def test_route_reports_a_hand_preset_switch_as_one_entry(plugin):
    call(plugin, "GET", "/api/v1/status")
    S.bundle.prints.sel = "My Fast"
    assert [c["text"] for c in changes(plugin)["changes"]] == ["Print preset switched to My Fast"]


def test_route_reports_hand_object_moves(plugin):
    call(plugin, "GET", "/api/v1/objects")
    S.model.objs[0]._insts[0]._o = (40.0, 20.0, 5.0)
    assert [c["text"] for c in changes(plugin)["changes"]] == ["Moved cube"]


def test_route_hides_sensitive_values(plugin):
    call(plugin, "GET", "/api/v1/status")
    edit(S.bundle.printers, "Fake Printer 0.4", "printhost_apikey", "another-fake-key")
    body = changes(plugin)
    assert [c["text"] for c in body["changes"]] == [mod.LABELS.get("printhost_apikey", ("printhost_apikey",))[0]
                                                    + " changed"]
    assert "fake-key" not in json.dumps(body)


def test_route_reports_hand_edited_project_and_object_settings(plugin):
    call(plugin, "GET", "/api/v1/objects")
    S.bundle.project["curr_bed_type"] = "Cool Plate"
    S.model.objs[0].config["wall_loops"] = "5"
    by = {c["group"]: c for c in changes(plugin)["changes"]}
    # OrcaSlicer's own label and enum labels, as the build words it
    assert by["Project"]["text"] == f"Bed type Textured PEI Plate {ARROW} Smooth Cool Plate"
    assert by["Project"]["key"] == "curr_bed_type" and by["Model"]["key"] == "wall_loops"
    assert by["Model"]["text"] == f"Wall loops of cube 3 {ARROW} 5"


def test_route_bad_since(plugin):
    assert call(plugin, "GET", "/api/v1/user_changes?since=abc") == \
        (400, {"error": "bad_since", "detail": "since must be an integer seq"})
    assert call(plugin, "GET", "/api/v1/user_changes")[0] == 200


def test_panel_undo_is_its_own_entry_never_a_hand_edit(plugin):
    """Ruling R19."""
    from test_panel_host import history_rows, page, wait_result
    call(plugin, "PUT", "/api/v1/config", {"wall_loops": 4})
    row = history_rows(plugin)[-1]
    page(plugin, {"action": "undo", "row": row["id"]})
    assert wait_result(plugin, row["id"])["status"] == "ok"
    body = changes(plugin)
    assert [(c["kind"], c["group"], c["text"]) for c in body["changes"]] == \
        [("panel", "Panel", "Reverted in the panel: Wall loops back to 2")]


def test_panel_switch_back_entry(plugin):
    from test_panel_host import history_rows, page, wait_result
    call(plugin, "PUT", "/api/v1/preset", {"type": "print", "name": "My Fast"})
    row = history_rows(plugin)[-1]
    page(plugin, {"action": "undo", "row": row["id"]})
    assert wait_result(plugin, row["id"])["status"] == "ok"
    assert [c["text"] for c in changes(plugin)["changes"]] == \
        ["Switched back in the panel: Print preset to 0.20mm Standard @Fake"]


def test_hand_edit_before_a_panel_undo_stays_the_users(plugin):
    from test_panel_host import history_rows, page, wait_result
    call(plugin, "PUT", "/api/v1/config", {"wall_loops": 4})
    row = history_rows(plugin)[-1]
    edit(S.bundle.prints, "0.20mm Standard @Fake", "layer_height", "0.3")  # by hand, then the panel undo
    page(plugin, {"action": "undo", "row": row["id"]})
    assert wait_result(plugin, row["id"])["status"] == "ok"
    assert [c["text"] for c in changes(plugin)["changes"]] == \
        [f"Layer height 0.2 mm {ARROW} 0.3 mm", "Reverted in the panel: Wall loops back to 2"]


def test_project_open_and_new_project(plugin):
    call(plugin, "GET", "/api/v1/status")
    edit(S.bundle.prints, "0.20mm Standard @Fake", "wall_loops", "4")
    S.plater.path = "C:/prints/bracket.3mf"
    UI.run_blocking(lambda: plugin.on_lifecycle_event(mod.orca.LifecycleEvent.ProjectOpened, None))
    assert [(c["kind"], c["text"]) for c in changes(plugin)["changes"]] == [("project", "Opened project bracket")]
    UI.run_blocking(lambda: plugin.on_lifecycle_event(mod.orca.LifecycleEvent.NewProject, None))
    assert [c["text"] for c in changes(plugin)["changes"]] == ["New project"]


def test_fork_only_routes_leave_the_journal_alone(plugin):
    call(plugin, "GET", "/api/v1/status")
    edit(S.bundle.prints, "0.20mm Standard @Fake", "wall_loops", "4")
    assert call(plugin, "POST", "/api/v1/slice", {})[0] == 404
    assert [c["text"] for c in changes(plugin)["changes"]] == [f"Wall loops 2 {ARROW} 4"]
