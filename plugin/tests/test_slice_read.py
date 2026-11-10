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
    yes = lambda d: True  # noqa: E731
    assert mod.slice_folders(env, windows=False, uid=501, isdir=yes) == ["/var/tmp/x/orcaslicer_501/orcaslicer_model"]
    assert mod.slice_folders({}, windows=False, uid=1000, isdir=yes) == ["/tmp/orcaslicer_1000/orcaslicer_model"]


def test_slice_folder_on_windows_has_no_user_folder():
    env = {"TMP": "C:\\Users\\someone\\AppData\\Local\\Temp\\", "TEMP": "D:\\other"}
    yes = lambda d: True  # noqa: E731
    assert mod.slice_folders(env, windows=True, uid=None, isdir=yes) == [
        "C:\\Users\\someone\\AppData\\Local\\Temp\\orcaslicer_model"]
    assert mod.slice_folders({"TEMP": "D:\\t"}, windows=True, uid=None, isdir=yes) == ["D:\\t\\orcaslicer_model"]
    assert mod.slice_folders({}, windows=True, uid=None, isdir=yes) == []


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


class Host:
    """The two UI-thread reads SliceWatch makes: the current plate and the fingerprint."""

    def __init__(self):
        self.plate, self.value, self.fp_calls, self.broken = 0, "a", 0, False

    def plate_index(self):
        return self.plate

    def fingerprint(self):
        self.fp_calls += 1
        if self.broken:
            raise RuntimeError("host went away")
        return self.value


def watch():
    h = Host()
    return mod.SliceWatch(h.plate_index, h.fingerprint), h


def sliced(w, path="/s/a.gcode", started=True, code="Ok"):
    if started:
        w.event("SliceStarted", ctx())
    w.event("GCodeExportFinished", ctx(msg=path))
    w.event("SlicingJobComplete", ctx(code))


def test_watch_starts_idle():
    w, _ = watch()
    assert w.current() == {"state": "idle"}


def test_watch_follows_a_slice():
    w, _ = watch()
    w.event("SliceStarted", ctx())
    assert w.current()["state"] == "slicing"
    w.event("GCodeExportFinished", ctx(msg="/s/a.gcode"))
    w.event("SlicingJobComplete", ctx())
    assert w.current() == {"state": "done", "path": "/s/a.gcode", "valid": True, "serial": 1}


def test_watch_change_makes_it_out_of_date():
    w, h = watch()
    sliced(w)
    h.value = "b"
    assert w.current()["valid"] is False
    h.value = "a"
    assert w.current()["valid"] is True
    w.event("ObjectChanged", ctx())
    assert w.current()["valid"] is False


@pytest.mark.parametrize("name", ["ObjectAdded", "ObjectDeleted", "ObjectTransformed", "ObjectChanged"])
def test_watch_object_events_make_it_out_of_date(name):
    w, _ = watch()
    sliced(w)
    w.event(name, ctx())
    assert w.current()["valid"] is False


def test_watch_cancel_drops_the_previous_slice():
    w, _ = watch()
    sliced(w)
    w.event("SliceStarted", ctx())
    w.event("SlicingJobComplete", ctx("Warn", "cancelled"))
    assert w.current() == {"state": "idle"}


def test_watch_cancel_with_nothing_running_keeps_the_slice():
    w, _ = watch()
    sliced(w)
    w.event("SlicingJobComplete", ctx("Warn", "cancelled"))
    assert w.current()["state"] == "done"


def test_watch_failed_slice():
    w, _ = watch()
    sliced(w)
    w.event("SliceStarted", ctx())
    w.event("SlicingJobComplete", ctx("Error", "Nothing to be sliced"))
    assert w.current() == {"state": "error", "message": "Nothing to be sliced"}


def test_watch_failed_export_keeps_a_finished_slice():
    """OrcaSlicer keeps a complete slice valid when a later export or upload fails, and sends no SliceStarted."""
    w, _ = watch()
    sliced(w)
    w.event("SlicingJobComplete", ctx("Error", "Failed to copy the G-code"))
    assert w.current()["state"] == "done" and w.current()["valid"] is True


def test_watch_error_after_the_gcode_was_written_keeps_it():
    w, _ = watch()
    sliced(w, "/s/b.gcode", code="Error")
    assert w.current()["path"] == "/s/b.gcode"


def test_watch_ignores_a_failed_gcode_write():
    w, _ = watch()
    w.event("SliceStarted", ctx())
    w.event("GCodeExportFinished", ctx("Error", "/s/a.gcode\ndisk full"))
    w.event("SlicingJobComplete", ctx())
    assert w.current() == {"state": "idle"}


@pytest.mark.parametrize("name", ["NewProject", "ProjectOpened", "ProjectClosed"])
def test_watch_project_change_forgets_slices(name):
    w, _ = watch()
    sliced(w)
    w.event(name, ctx())
    assert w.current() == {"state": "idle"}


def test_watch_export_without_start_still_counts():
    w, _ = watch()
    sliced(w, started=False)
    assert w.current()["state"] == "done"


def test_watch_keeps_one_slice_per_plate():
    w, h = watch()
    sliced(w, "/s/p0.gcode")
    h.plate = 1  # a plate switch, or "Move plate to front", which sends no PlateSelected
    assert w.current() == {"state": "idle"}
    sliced(w, "/s/p1.gcode")
    assert w.current()["path"] == "/s/p1.gcode"
    h.plate = 0
    assert w.current()["path"] == "/s/p0.gcode"


def test_watch_new_slice_gets_a_new_serial():
    w, _ = watch()
    sliced(w)
    first = w.current()["serial"]
    sliced(w)
    assert w.current()["serial"] == first + 1 and w.current()["path"] == "/s/a.gcode"


def test_watch_fingerprint_only_when_needed():
    w, h = watch()
    w.current()
    w.event("SliceStarted", ctx())
    w.event("SlicingJobComplete", ctx("Error", "x"))
    assert h.fp_calls == 0


def test_watch_survives_a_failing_fingerprint():
    w, h = watch()
    h.broken = True
    sliced(w)
    assert w.current()["valid"] is False
    h.broken = False
    assert w.current()["valid"] is False  # no fingerprint taken at the slice: stays out of date


def test_summary_from_a_bambu_header():
    head = ("; HEADER_BLOCK_START\n; model printing time: 5m 2s; total estimated time: 5m 56s\n"
            "; total layer number: 30\n; HEADER_BLOCK_END\n")
    tail = "; filament used [mm] = 100.5\n; filament used [g] = 1.5, 0.5\n"
    out = mod.parse_gcode_summary(tail, head)
    assert out["estimated_time_seconds"] == 356 and out["layers"] == 30 and out["filament_used_g"] == 2.0


def test_slice_folder_skips_a_variable_whose_folder_is_missing():
    env = {"TMPDIR": "/gone", "TMP": "/var/tmp/y"}
    assert mod.slice_folders(env, windows=False, uid=5, isdir=lambda d: d != "/gone") == [
        "/var/tmp/y/orcaslicer_5/orcaslicer_model"]


def test_in_slice_folder_windows_paths():
    import ntpath
    folders = ["G:\\orca-dev\\tmp\\orcaslicer_model"]
    reported = "G:\\orca-dev\\tmp/orcaslicer_model/Tue_Nov_10/10_51_04#13368#44/Metadata/.13368.0.gcode"
    assert mod.in_slice_folder(reported, folders, ntpath)
    assert mod.in_slice_folder(reported.upper(), folders, ntpath)
    assert not mod.in_slice_folder("H:\\orca-dev\\tmp\\orcaslicer_model\\x.gcode", folders, ntpath)
    assert not mod.in_slice_folder("G:\\orca-dev\\tmp\\orcaslicer_model\\..\\x.gcode", folders, ntpath)


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


def test_a_reslice_of_the_same_size_is_read_again(live, folder):
    path = gcode_file(folder)
    slice_now(live, path)
    assert call(live, "GET", "/api/v1/slice/status")[1]["stats"]["total_cost"] == 0.14
    with open(path, "w") as f:
        f.write(("G1 X1\n" + TAIL).replace("total filament cost = 0.14", "total filament cost = 0.19"))
    slice_now(live, path)
    assert call(live, "GET", "/api/v1/slice/status")[1]["stats"]["total_cost"] == 0.19


def test_a_plate_switch_without_an_event(live, folder):
    slice_now(live, gcode_file(folder))
    S.model.plate = 1
    _, body = call(live, "GET", "/api/v1/slice/status")
    assert body["state"] == "idle"
    S.model.plate = 0
    assert call(live, "GET", "/api/v1/slice/status")[1]["state"] == "done"


def test_status_survives_a_failing_read(live, folder, monkeypatch):
    slice_now(live, gcode_file(folder))

    def boom():
        raise RuntimeError("no model")
    monkeypatch.setattr(live.slices, "_fingerprint", boom)
    status, st = call(live, "GET", "/api/v1/status")
    assert status == 200 and st["slice_result_valid"] is False
