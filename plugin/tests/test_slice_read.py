"""The plugin serves the slice the user made in OrcaSlicer: lifecycle events say where the G-code went,
the G-code's closing comments give the numbers. All paths and values are invented."""
import json
import os
import types
import urllib.error
import urllib.request

import pytest

import fake_orca
from conftest import call, mod, slice_now
from conftest import gcode_file as _gcode_file

S = fake_orca.STATE
EV = fake_orca.build_module().LifecycleEvent
CODE = fake_orca.build_module().LifecycleEvtCode

TAIL = """; CONFIG_BLOCK_START
; layer_height = 0.2
; CONFIG_BLOCK_END

; filament used [mm] = 2337.36
; filament used [cm3] = 5.62
; filament used [g] = 6.97
; filament cost = 0.14
; total filament used [g] = 6.97
; total filament cost = 0.14
; total layers count = 17
; estimated printing time (normal mode) = 6m 42s
; estimated first layer printing time (normal mode) = 0.934549s
"""


# ------------------------------------------------------------------ pure parts

@pytest.mark.parametrize("text,seconds", [("6m 42s", 402), ("1h 2m 3s", 3723), ("2d 1h", 176400),
                                          ("0.934549s", 0.934549), ("59s", 59)])
def test_parse_duration(text, seconds):
    assert mod.parse_duration(text) == pytest.approx(seconds)


@pytest.mark.parametrize("text", ["", "soon", "6x"])
def test_parse_duration_rejects(text):
    assert mod.parse_duration(text) is None


def test_summary_from_the_closing_comments():
    assert mod.parse_gcode_summary(TAIL) == {
        "estimated_time": "6m 42s", "estimated_time_seconds": 402.0, "filament_used_mm": 2337.36,
        "filament_used_g": 6.97, "total_cost": 0.14, "layers": 17}


def test_summary_adds_up_several_filaments():
    tail = "; filament used [mm] = 100.5, 20.25\n; filament used [g] = 1.5, 0.5\n"
    out = mod.parse_gcode_summary(tail)
    assert out["filament_used_mm"] == pytest.approx(120.75)
    assert out["filament_used_g"] == pytest.approx(2.0)


def test_summary_of_a_file_without_comments_is_empty():
    assert mod.parse_gcode_summary("G1 X10 Y10\nG1 X20\n") == {}


def test_slice_folder_follows_the_temp_variables_off_windows():
    env = {"TMPDIR": "/var/tmp/x/", "TMP": "/ignored"}
    assert mod.slice_folders(env, windows=False, uid=501) == ["/var/tmp/x/orcaslicer_501/orcaslicer_model"]
    assert mod.slice_folders({}, windows=False, uid=1000) == ["/tmp/orcaslicer_1000/orcaslicer_model"]


def test_slice_folder_on_windows_has_no_user_folder():
    env = {"TMP": "C:\\Users\\someone\\AppData\\Local\\Temp\\", "TEMP": "D:\\other"}
    assert mod.slice_folders(env, windows=True, uid=None) == [
        "C:\\Users\\someone\\AppData\\Local\\Temp\\orcaslicer_model"]
    assert mod.slice_folders({"TEMP": "D:\\t"}, windows=True, uid=None) == ["D:\\t\\orcaslicer_model"]
    assert mod.slice_folders({}, windows=True, uid=None) == []


def test_in_slice_folder():
    folders = ["/tmp/orcaslicer_1000/orcaslicer_model"]
    inside = "/tmp/orcaslicer_1000/orcaslicer_model/Tue_Nov_10/10_51_04#1#44/Metadata/.1.0.gcode"
    assert mod.in_slice_folder(inside, folders)
    assert not mod.in_slice_folder("/tmp/orcaslicer_1000/orcaslicer_model_x/a.gcode", folders)
    assert not mod.in_slice_folder("/tmp/orcaslicer_1000/orcaslicer_model/../secret.gcode", folders)
    assert not mod.in_slice_folder("relative/a.gcode", folders)
    assert not mod.in_slice_folder(inside, [])


def test_registers_read_access_to_the_slice_folder(monkeypatch):
    monkeypatch.setattr(mod, "current_slice_folders", lambda: ["/tmp/orcaslicer_7/orcaslicer_model"])
    S.permissions = None
    mod.orca.registered.clear()
    mod.OrcaSlicerMcpPlugin().register_capabilities()
    assert S.permissions == {"threading": True, "network_socket": ["127.0.0.1"],
                             "fs_read": ["/tmp/orcaslicer_7/orcaslicer_model/*"]}


def test_registers_without_read_access_when_there_is_no_folder(monkeypatch):
    monkeypatch.setattr(mod, "current_slice_folders", lambda: [])
    S.permissions = None
    mod.OrcaSlicerMcpPlugin().register_capabilities()
    assert S.permissions == {"threading": True, "network_socket": ["127.0.0.1"]}


# ------------------------------------------------------------------ the event bookkeeping

def ctx(code="Ok", msg="", index=-1):
    return types.SimpleNamespace(code=getattr(CODE, code), msg=msg, index=index, name="", id="")


class Fp:
    def __init__(self):
        self.value = "a"

    def __call__(self):
        return self.value


def sliced(w, fp, path="/s/a.gcode", started=True):
    if started:
        w.event("SliceStarted", ctx(), fp)
    w.event("GCodeExportFinished", ctx(msg=path), fp)
    w.event("SlicingJobComplete", ctx(), fp)


def test_watch_starts_idle():
    assert mod.SliceWatch().current(Fp()) == {"state": "idle"}


def test_watch_follows_a_slice():
    w, fp = mod.SliceWatch(), Fp()
    w.event("SliceStarted", ctx(), fp)
    assert w.current(fp)["state"] == "slicing"
    w.event("GCodeExportFinished", ctx(msg="/s/a.gcode"), fp)
    w.event("SlicingJobComplete", ctx(), fp)
    assert w.current(fp) == {"state": "done", "path": "/s/a.gcode", "valid": True}


def test_watch_change_makes_it_out_of_date():
    w, fp = mod.SliceWatch(), Fp()
    sliced(w, fp)
    fp.value = "b"
    assert w.current(fp)["valid"] is False
    fp.value = "a"
    assert w.current(fp)["valid"] is True
    w.event("ObjectChanged", ctx(), fp)
    assert w.current(fp)["valid"] is False


@pytest.mark.parametrize("name", ["ObjectAdded", "ObjectDeleted", "ObjectTransformed", "ObjectChanged"])
def test_watch_object_events_make_it_out_of_date(name):
    w, fp = mod.SliceWatch(), Fp()
    sliced(w, fp)
    w.event(name, ctx(), fp)
    assert w.current(fp)["valid"] is False


def test_watch_cancel_drops_the_previous_slice():
    w, fp = mod.SliceWatch(), Fp()
    sliced(w, fp)
    w.event("SliceStarted", ctx(), fp)
    w.event("SlicingJobComplete", ctx("Warn", "cancelled"), fp)
    assert w.current(fp) == {"state": "idle"}


def test_watch_failed_slice():
    w, fp = mod.SliceWatch(), Fp()
    sliced(w, fp)
    w.event("SliceStarted", ctx(), fp)
    w.event("SlicingJobComplete", ctx("Error", "Nothing to be sliced"), fp)
    assert w.current(fp) == {"state": "error", "message": "Nothing to be sliced"}


def test_watch_ignores_a_failed_export():
    w, fp = mod.SliceWatch(), Fp()
    w.event("SliceStarted", ctx(), fp)
    w.event("GCodeExportFinished", ctx("Error", "/s/a.gcode\ndisk full"), fp)
    w.event("SlicingJobComplete", ctx(), fp)
    assert w.current(fp) == {"state": "idle"}


@pytest.mark.parametrize("name", ["NewProject", "ProjectOpened", "ProjectClosed"])
def test_watch_project_change_forgets_slices(name):
    w, fp = mod.SliceWatch(), Fp()
    sliced(w, fp)
    w.event(name, ctx(), fp)
    assert w.current(fp) == {"state": "idle"}


def test_watch_export_without_start_still_counts():
    w, fp = mod.SliceWatch(), Fp()
    sliced(w, fp, started=False)
    assert w.current(fp)["state"] == "done"


def test_watch_keeps_one_slice_per_plate():
    w, fp = mod.SliceWatch(), Fp()
    sliced(w, fp, "/s/plate-first.gcode")  # the plate shown when the project opened
    w.event("PlateSelected", ctx(index=1), fp)
    assert w.current(fp) == {"state": "idle"}
    sliced(w, fp, "/s/plate-two.gcode")
    assert w.current(fp)["path"] == "/s/plate-two.gcode"
    w.event("PlateSelected", ctx(index=0), fp)
    assert w.current(fp) == {"state": "idle"}  # index 0 is not known to be the opening plate
    w.event("PlateSelected", ctx(index=1), fp)
    assert w.current(fp)["path"] == "/s/plate-two.gcode"


def test_watch_slice_all_files_each_plate():
    w, fp = mod.SliceWatch(), Fp()
    for i in (0, 1):
        w.event("PlateSelected", ctx(index=i), fp)
        sliced(w, fp, f"/s/p{i}.gcode")
    w.event("PlateSelected", ctx(index=0), fp)
    assert w.current(fp)["path"] == "/s/p0.gcode"


def test_watch_fingerprint_only_when_needed():
    calls = []
    w = mod.SliceWatch()
    w.current(lambda: calls.append(1) or "a")
    w.event("SliceStarted", ctx(), lambda: calls.append(1) or "a")
    assert calls == []


# ------------------------------------------------------------------ over HTTP

@pytest.fixture
def live(folder, plugin):
    return plugin


def gcode_file(folder, body="G1 X1\n" + TAIL, name=".1.0.gcode"):
    return _gcode_file(folder, body, name)


def raw(cap, path):
    req = urllib.request.Request(cap.base + path)
    req.add_header("X-Api-Token", cap._token)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()


def test_status_says_it_can_read_slices(live):
    _, st = call(live, "GET", "/api/v1/status")
    assert "slice_read" in st["capabilities"] and st["slice_result_valid"] is False


def test_nothing_sliced_yet(live):
    status, body = call(live, "GET", "/api/v1/slice/status")
    assert status == 200 and body["state"] == "idle" and "Slice plate" in body["message"]
    assert raw(live, "/api/v1/gcode")[0] == 409


def test_your_slice_is_served(live, folder):
    path = gcode_file(folder)
    slice_now(live, path)
    status, body = call(live, "GET", "/api/v1/slice/status")
    assert status == 200
    assert body["state"] == "done" and body["valid"] is True and body["stale"] is False
    assert body["stats"]["estimated_time_seconds"] == 402.0 and body["stats"]["filament_used_g"] == 6.97
    assert body["warnings"] == [] and "warnings" in body["message"]
    code, ctype, data = raw(live, "/api/v1/gcode")
    assert code == 200 and ctype.startswith("text/plain") and data.decode() == open(path).read()
    _, st = call(live, "GET", "/api/v1/status")
    assert st["slice_result_valid"] is True


def test_a_settings_change_makes_it_out_of_date(live, folder):
    slice_now(live, gcode_file(folder))
    assert call(live, "PUT", "/api/v1/config", {"layer_height": 0.24})[0] == 200
    _, body = call(live, "GET", "/api/v1/slice/status")
    assert body["state"] == "done" and body["valid"] is False and body["stale"] is True
    assert "out of date" in body["message"]
    code, _, data = raw(live, "/api/v1/gcode")
    assert code == 409 and json.loads(data) == {"error": "not_sliced"}


def test_a_file_outside_the_folder_is_never_read(live, tmp_path):
    other = tmp_path / "elsewhere.gcode"
    other.write_text("G1\n" + TAIL)
    slice_now(live, str(other))
    _, body = call(live, "GET", "/api/v1/slice/status")
    assert body["state"] == "done" and body.get("stats") is None and "outside" in body["message"]
    code, _, data = raw(live, "/api/v1/gcode")
    assert code == 500 and json.loads(data)["error"] == "gcode_not_readable"


def test_a_vanished_file(live, folder):
    path = gcode_file(folder)
    slice_now(live, path)
    os.remove(path)
    code, _, data = raw(live, "/api/v1/gcode")
    assert code == 500 and json.loads(data)["error"] == "gcode_file_missing"
    _, body = call(live, "GET", "/api/v1/slice/status")
    assert body.get("stats") is None and "slice again" in body["message"]


def test_a_failed_slice_reports_the_error(live, folder):
    slice_now(live, gcode_file(folder))
    fake_orca.UI.run_blocking(lambda: live.on_lifecycle_event(EV.SliceStarted, ctx()))
    fake_orca.UI.run_blocking(lambda: live.on_lifecycle_event(EV.SlicingJobComplete,
                                                              ctx("Error", "Nothing to be sliced")))
    _, body = call(live, "GET", "/api/v1/slice/status")
    assert body["state"] == "error" and body["message"] == "Nothing to be sliced"
    assert body["errors"] == ["Nothing to be sliced"]
    assert raw(live, "/api/v1/gcode")[0] == 409


@pytest.mark.parametrize("change", ["modifier", "paint", "layering", "volume_setting"])
def test_part_changes_make_it_out_of_date(live, folder, change):
    slice_now(live, gcode_file(folder))
    obj = S.model.objs[0]
    if change == "modifier":
        obj._volumes.append(fake_orca.Volume("ParameterModifier", (1.0, 0.0, 0.0), {"sparse_infill_density": "50%"}))
    elif change == "paint":
        obj._volumes[0].support_painted = True
    elif change == "layering":
        obj.custom_layering = True
    else:
        obj._volumes[0].config["wall_loops"] = "5"
    _, body = call(live, "GET", "/api/v1/slice/status")
    assert body["valid"] is False, change


def test_project_switch_never_serves_the_old_slice(live, folder):
    slice_now(live, gcode_file(folder))
    fake_orca.UI.run_blocking(lambda: live.on_lifecycle_event(EV.ProjectOpened, ctx()))
    _, body = call(live, "GET", "/api/v1/slice/status")
    assert body["state"] == "idle"
    assert raw(live, "/api/v1/gcode")[0] == 409
