"""One protocol-neutral status shape, and the one-line headline the model relays as-is. Headlines
never use em dashes (the same rule as compare_slices' headline)."""
from __future__ import annotations
import math
from pathlib import PurePosixPath

from .bambu import model_key
from .problems import console_problems, job_problem, klippy_problem, moonraker_warnings, problem

ACTIVE = frozenset({"preparing", "heating", "printing", "paused"})  # preparing: Bambu only
DONE = frozenset({"finished", "cancelled", "error"})
NEAR_TARGET_C = 3.0
_PROGRESS_FLOOR = 0.05  # below 5 %, extrapolating time left from progress is noise
STARTING_UP = "Klipper is starting up; check again in a minute."


def _f(v, nd: int = 1) -> float | None:
    """A number from a printer, or None for anything else. json.loads accepts NaN and Infinity, and
    neither can be shown or rounded to a whole number, so they count as missing."""
    try:
        x = None if v is None else float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(x, nd) if x is not None and math.isfinite(x) else None


def _obj(v) -> dict:
    """A Moonraker status section is an object; anything else counts as missing."""
    return v if isinstance(v, dict) else {}


def _seq(v) -> list:
    return list(v) if isinstance(v, (list, tuple)) else []


def _heater(obj) -> dict | None:
    if not isinstance(obj, dict) or ("temperature" not in obj and "target" not in obj):
        return None
    return {"actual": _f(obj.get("temperature")), "target": _f(obj.get("target"))}


def _octo_heater(obj) -> dict | None:
    if not isinstance(obj, dict):
        return None
    return {"actual": _f(obj.get("actual")), "target": _f(obj.get("target"))}


def _below_target(*heaters) -> bool:
    for h in heaters:
        if h and h["target"] and h["actual"] is not None and h["actual"] < h["target"] - NEAR_TARGET_C:
            return True
    return False


def _percent(fraction) -> int | None:
    v = _f(fraction, 4)
    return None if v is None else int(round(v * 100))


def _whole(v) -> int | None:
    """A layer number: a whole number from a printer, or None for anything else (text, NaN, a bool)."""
    if isinstance(v, bool):
        return None
    x = _f(v, 0)
    return None if x is None else int(x)


def klipper_state(webhooks: dict, print_stats: dict, nozzle: dict | None, bed: dict | None) -> str:
    ks = (webhooks or {}).get("state")
    if ks == "shutdown":
        return "shutdown"
    if ks == "error":
        return "error"
    if ks and ks != "ready":
        return "offline"
    ps = (print_stats or {}).get("state")
    if ps == "printing":
        used = _f(print_stats.get("filament_used"), 3) or 0
        return "heating" if used <= 0 and _below_target(nozzle, bed) else "printing"
    return {"paused": "paused", "complete": "finished", "cancelled": "cancelled", "error": "error"}.get(ps, "idle")


def remaining(print_duration, progress, estimate_s) -> tuple[int | None, str | None]:
    pd = _f(print_duration, 0)
    est = _f(estimate_s, 0)
    if est and est > 0 and pd is not None and est - pd > 0:
        return max(0, int(round(est - pd))), "slicer_estimate"
    pr = _f(progress, 4)
    if pd and pr and pr >= _PROGRESS_FLOOR:
        return max(0, int(round(pd / pr - pd))), "progress"
    return None, None


def klipper_snapshot(status: dict, server_info: dict, console: list[dict], *, since: float,
                     file_meta: dict | None, target_public: dict) -> dict:
    status = status or {}
    wh, ps = _obj(status.get("webhooks")), _obj(status.get("print_stats"))
    ds, vs = _obj(status.get("display_status")), _obj(status.get("virtual_sdcard"))
    th, gm = _obj(status.get("toolhead")), _obj(status.get("gcode_move"))
    fan, ex = _obj(status.get("fan")), _obj(status.get("extruder"))
    # While Klipper starts up its sensors have not been read: it reports 0, which is not a measurement.
    starting = wh.get("state") == "startup"
    nozzle, bed = (None, None) if starting else (_heater(ex), _heater(_obj(status.get("heater_bed"))))
    disconnected = bool(server_info) and server_info.get("klippy_connected") is False
    state = "offline" if disconnected else klipper_state(wh, ps, nozzle, bed)
    progress = ds.get("progress") if ds.get("progress") is not None else vs.get("progress")
    job = None
    if ps.get("filename") and (state in ACTIVE or state in DONE):
        info = _obj(ps.get("info"))
        meta = _obj(file_meta)
        rem, basis = (remaining(ps.get("print_duration"), progress, meta.get("estimated_time"))
                      if state in ACTIVE else (None, None))
        job = {"file": ps.get("filename"), "state": ps.get("state"), "progress_percent": _percent(progress),
               "layer": {"current": _whole(info.get("current_layer")), "total": _whole(info.get("total_layer"))},
               "elapsed_s": _f(ps.get("print_duration"), 0), "remaining_s": rem, "remaining_basis": basis,
               "filament_used_mm": _f(ps.get("filament_used"), 0),
               "first_layer_height": _f(meta.get("first_layer_height"), 3)}
    problems = []
    if disconnected:
        problems.append(problem("fatal", "moonraker",
                                "Klipper isn't connected to Moonraker: it may be starting up or may have crashed."))
    if starting:
        problems.append(problem("warning", "klipper", STARTING_UP))
    problems += [p for p in (klippy_problem(wh), job_problem(ps)) if p]
    problems += console_problems(console, since)
    problems += moonraker_warnings(server_info, _obj(status.get("configfile")).get("warnings"))
    origin, pos = _seq(gm.get("homing_origin")), _seq(th.get("position"))
    body = {
        "printer": target_public, "connected": state != "offline", "state": state,
        "temps": {"nozzle": nozzle, "bed": bed}, "job": job, "problems": problems,
        "klipper": {"pressure_advance": _f(ex.get("pressure_advance"), 4),
                    "z_offset": _f(origin[2], 3) if len(origin) > 2 else None,
                    "speed_factor": _f(gm.get("speed_factor"), 3), "flow_factor": _f(gm.get("extrude_factor"), 3),
                    "fan_percent": _percent(fan.get("speed")), "homed_axes": th.get("homed_axes"),
                    "z_mm": _f(pos[2], 2) if len(pos) > 2 else None},
    }
    # While starting up the one sentence is the whole answer: no temperatures, no warning count.
    return {"headline": STARTING_UP if starting else headline(body), **body}


def octoprint_snapshot(printer: dict | None, job: dict | None, *, target_public: dict) -> dict:
    job = job or {}
    jb = job.get("job") or {}
    pr = job.get("progress") or {}
    completion = _f(pr.get("completion"), 1)
    fname = (jb.get("file") or {}).get("name")
    problems, nozzle, bed = [], None, None
    if printer is None:
        state = "offline"
        problems.append(problem("fatal", "octoprint", "OctoPrint is running but not connected to the printer."))
    else:
        temps = printer.get("temperature") or {}
        nozzle, bed = _octo_heater(temps.get("tool0")), _octo_heater(temps.get("bed"))
        st = printer.get("state") or {}
        flags = st.get("flags") or {}
        if flags.get("error") or flags.get("closedOrError"):
            state = "error"
            problems.append(problem("fatal", "octoprint", (st.get("text") or "").strip() or "OctoPrint reports an error."))
        elif flags.get("paused") or flags.get("pausing"):
            state = "paused"
        elif flags.get("printing"):
            # OctoPrint's completion is a position in the file, above zero while the header and start
            # G-code are read and M109/M190 still wait, so it cannot tell heating from printing. The
            # heaters can: a brief "heating" during a mid-print temperature change is truthful too.
            state = "heating" if _below_target(nozzle, bed) else "printing"
        elif flags.get("cancelling"):
            state = "cancelled"
        elif fname and completion is not None and completion >= 100:
            state = "finished"
        else:
            state = "idle"
    out_job = None
    if fname and (state in ACTIVE or state in DONE):
        left = _f(pr.get("printTimeLeft"), 0)
        out_job = {"file": fname, "state": job.get("state"),
                   "progress_percent": None if completion is None else int(round(completion)),
                   "layer": {"current": None, "total": None}, "elapsed_s": _f(pr.get("printTime"), 0),
                   "remaining_s": None if left is None or state not in ACTIVE else int(left),
                   "remaining_basis": "printer_estimate" if left is not None and state in ACTIVE else None,
                   "filament_used_mm": None, "first_layer_height": None}
    body = {"printer": target_public, "connected": state != "offline", "state": state,
            "temps": {"nozzle": nozzle, "bed": bed}, "job": out_job, "problems": problems}
    return {"headline": headline(body), **body}


# ---- Bambu Lab, through OrcaSlicer (printer/bambu.py) ------------------------------------------------------------
# The fork hands over the printer's own words; this turns them into the shape above. gcode_state RUNNING covers
# everything from bed levelling to the last layer, so the stage number tells heating apart; a stopped job is FAILED
# whether cancelled or faulted, and the print error code tells those apart (none, or Bambu's own "The task was
# canceled." code: cancelled).
BAMBU_HEATING_STAGES = frozenset({2, 7})  # heatbed preheating, heating nozzle
BAMBU_CANCEL_ERRORS = frozenset({"0300-400C", "0300400C", 50348044})  # Bambu's table: "The task was canceled."
BAMBU_HMS_SEVERITY = {"fatal": "fatal", "serious": "error", "common": "warning", "info": "warning", "unknown": "warning"}
BAMBU_HMS_MODULES = {"03": "motion controller", "05": "mainboard", "07": "AMS", "08": "toolhead", "0C": "camera"}
BAMBU_STALE_S = 30.0
BAMBU_OFFLINE_HINT = ("Check the printer is on and on the network; OrcaSlicer reconnects on its own. If it stays "
                      "offline, select it again in OrcaSlicer's Device tab.")


def bambu_hms_wiki(code: str) -> str | None:
    """The Bambu wiki page for a 16-hex-digit HMS code (0700200000020001 -> 0700_2000_0002_0001)."""
    c = (code or "").strip().upper()
    if len(c) != 16 or any(ch not in "0123456789ABCDEF" for ch in c):
        return None
    return f"https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/{c[0:4]}_{c[4:8]}_{c[8:12]}_{c[12:16]}"


def _bambu_hms_problem(item: dict) -> dict | None:
    code = str(item.get("code") or "").strip().upper()
    if not code:
        return None
    level = str(item.get("level") or "unknown").lower()
    module = BAMBU_HMS_MODULES.get(code[:2], "printer")
    # The Device tab's own sentence for the code when the build sends it (fork mcp.19); the code alone otherwise.
    text = str(item.get("message") or "").strip()
    p = problem(BAMBU_HMS_SEVERITY.get(level, "warning"), "hms",
                f"HMS {code}: {text}" if text else f"HMS {code}: a {level} {module} fault the printer reports.")
    url = bambu_hms_wiki(code)
    p["hint"] = (f"Look the code up at {url}" if url else "Look the code up in OrcaSlicer's HMS panel.")
    return p


def bambu_cancelled(print_error: dict | None) -> bool:
    """A stop the printer reports as a cancel: no code, or the code Bambu's table calls "The task was canceled."."""
    if not print_error:
        return True
    return (str(print_error.get("code") or "").strip().upper() in BAMBU_CANCEL_ERRORS
            or print_error.get("value") in BAMBU_CANCEL_ERRORS)


def bambu_state(gcode_state: str, stage: int | None, print_error: dict | None,
                nozzle: dict | None, bed: dict | None, layer_current: int | None) -> str:
    s = (gcode_state or "").upper()
    if s == "PAUSE":
        return "paused"
    if s == "FINISH":
        return "finished"
    if s == "FAILED":
        return "cancelled" if bambu_cancelled(print_error) else "error"
    if s in ("PREPARE", "SLICING"):
        return "preparing"
    if s == "RUNNING":
        if stage in BAMBU_HEATING_STAGES:
            return "heating"
        if not layer_current and _below_target(nozzle, bed):
            return "heating"
        if layer_current == 0 and stage not in (None, 0):
            return "preparing"  # bed levelling, homing, nozzle cleaning, ...: the first layer has not begun
        return "printing"
    return "idle"


_BAMBU_JOB_STATE = {"RUNNING": "running", "PAUSE": "paused", "PREPARE": "preparing", "SLICING": "preparing",
                    "FINISH": "complete"}


def _bambu_device(reply: dict) -> dict:
    p = _obj(reply.get("printer"))
    return {k: (str(p[k]).strip() or None) if p.get(k) is not None else None for k in ("name", "model", "connection")}


def _bambu_model_mismatch(device: dict, profile: str | None, profile_model: str | None) -> dict | None:
    """The printer read is another model than the printer profile is for (the Device tab's selection and the profile
    are independent): the readings are right, slicing with that profile is not."""
    model, name = device.get("model"), device.get("name") or "the printer"
    if not model or not profile_model or model.lower() == "unknown" or model_key(model) == model_key(profile_model):
        return None
    p = problem("warning", "orcaslicer",
                f"The printer profile{f' {profile!r}' if profile else ''} is for a {profile_model}; the printer "
                f"selected in OrcaSlicer's Device tab, {name}, is a {model}.")
    p["hint"] = (f"These readings are {name}'s. Before slicing for it, select_preset a printer profile for the "
                 f"{model}; select_printer switches the Device tab instead.")
    return p


def bambu_snapshot(reply: dict, *, target_public: dict, profile_model: str | None = None) -> dict:
    """The fork's GET /printer/status reply as a snapshot. A printer_offline reply is the offline state.
    `profile_model` is the printer profile's printer_model, to say when the printer read is another model."""
    reply = reply or {}
    if reply.get("supported") is False:
        msg = str(reply.get("message") or "The printer is not connected to OrcaSlicer right now.")
        p = problem("fatal", "orcaslicer", msg)
        p["hint"] = BAMBU_OFFLINE_HINT
        body = {"printer": target_public, "connected": False, "state": "offline",
                "temps": {"nozzle": None, "bed": None}, "job": None, "problems": [p]}
        return {"headline": headline(body), **body}
    temps_in, job_in = _obj(reply.get("temps")), _obj(reply.get("job"))
    nozzle, bed = _octo_heater(temps_in.get("nozzle")), _octo_heater(temps_in.get("bed"))
    chamber = _octo_heater(temps_in.get("chamber"))
    gcode_state = str(reply.get("state") or "")
    stage = _whole(reply.get("stage"))
    print_error = reply.get("print_error") if isinstance(reply.get("print_error"), dict) else None
    layer_in = _obj(job_in.get("layer"))
    layer = {"current": _whole(layer_in.get("current")), "total": _whole(layer_in.get("total"))}
    state = bambu_state(gcode_state, stage, print_error, nozzle, bed, layer["current"])
    stage_text = str(reply.get("stage_text") or "").strip() or None
    job = None
    name = job_in.get("name") or job_in.get("file")
    if name and (state in ACTIVE or state in DONE):
        s = gcode_state.upper()
        remaining = _f(job_in.get("remaining_s"), 0) if state in ACTIVE else None
        job = {"file": name, "state": _BAMBU_JOB_STATE.get(s, state if state in DONE else "unknown"),
               "progress_percent": _whole(job_in.get("progress_percent")), "layer": layer,
               "elapsed_s": None, "remaining_s": None if remaining is None else int(remaining),
               "remaining_basis": "printer_estimate" if remaining is not None else None,
               "filament_used_mm": None, "first_layer_height": None, "stage": stage_text}
    problems = []
    if print_error and not bambu_cancelled(print_error):
        # A stopped job's reason, or why a paused one waits (a filament runout, say). A cancel is no problem.
        code = str(print_error.get("code") or print_error.get("value") or "").strip()
        text = str(print_error.get("message") or "").strip()  # the Device tab's sentence (fork mcp.19)
        lead = "The print stopped with printer error" if state == "error" else "The printer reports error"
        if code:
            msg = f"{lead} {code}: {text}" if text else f"{lead} {code}."
        else:
            msg = "The print stopped with an error the printer did not name."
        problems.append(problem("error", "job", msg))
    problems += [p for p in (_bambu_hms_problem(h) for h in _seq(reply.get("hms"))) if p]
    device = _bambu_device(reply)
    if (mismatch := _bambu_model_mismatch(device, target_public.get("profile"), profile_model)) is not None:
        problems.append(mismatch)
    age = _f(reply.get("updated_s_ago"), 1)
    if age is not None and age > BAMBU_STALE_S:
        problems.append(problem("warning", "orcaslicer",
                                f"OrcaSlicer last heard from the printer {_dur(age)} ago: this may be stale."))
    body = {
        "printer": target_public, "connected": True, "state": state,
        "temps": {"nozzle": nozzle, "bed": bed, "chamber": chamber}, "job": job, "problems": problems,
        "bambu": {"device": device, "gcode_state": gcode_state, "stage": stage, "stage_text": stage_text,
                  "nozzles": _seq(temps_in.get("nozzles")), "fans": reply.get("fans"),
                  "speed_percent": _whole(reply.get("speed_percent")), "updated_s_ago": age,
                  "connection": device.get("connection")},
    }
    return {"headline": headline(body), **body}


def _name(file: str | None) -> str:
    if not file:
        return "a job"
    stem = PurePosixPath(file).name
    return stem[:-6] if stem.lower().endswith(".gcode") else stem


def _dur(seconds: float) -> str:
    m = max(1, int(round(seconds / 60)))
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def _temps_text(temps: dict) -> str | None:
    parts = []
    for label, key in (("Nozzle", "nozzle"), ("bed", "bed")):
        h = temps.get(key)
        if not h or h.get("actual") is None:
            continue
        actual, target = int(round(h["actual"])), h.get("target")
        parts.append(f"{label} {actual}/{int(round(target))} °C" if target else f"{label} {actual} °C")
    if not parts:
        return None
    text = ", ".join(parts)
    return text[0].upper() + text[1:] + "."


def _left_text(job: dict) -> str | None:
    """The end of a job reads "almost done"; a rounded-up "about 1 min left" would be wrong there."""
    if job.get("remaining_s") == 0 or (job.get("progress_percent") or 0) >= 100:
        return "almost done"
    if job.get("remaining_s") is not None:
        return f"about {_dur(job['remaining_s'])} left"
    return None


def _job_line(verb: str, name: str, job: dict, *, left: bool, tail: str = "") -> str:
    line = f"{verb} {name}"
    if job.get("progress_percent") is not None:
        line += f": {job['progress_percent']}%"
    layer = job.get("layer") or {}
    if layer.get("current") and layer.get("total"):
        line += f" (layer {layer['current']}/{layer['total']})"
    if left and (text := _left_text(job)):
        line += f", {text}"
    return line + tail + "."


def _heating_mid_job(job: dict) -> bool:
    """Heating with the job already under way: a running print reheating after a temperature change,
    or OctoPrint's first heat-up, which shows file progress above 0 too (it cannot tell the two
    apart), so the headline words it to be true for both. Klipper reports exactly 0 mm of filament
    before the first extrusion, so a Klipper job at 0 mm is the first heat-up, even when the file
    position is above 0 because of a large header."""
    return (job.get("progress_percent") or 0) > 0 and job.get("filament_used_mm") != 0


def headline(snap: dict) -> str:
    state = snap.get("state")
    job = snap.get("job") or {}
    name = _name(job.get("file"))
    if state == "printing":
        # A Bambu printer says what it is doing before the first layer (bed levelling, say) and mid-print
        # (changing filament): worth a word when it is not plain printing.
        stage = job.get("stage")
        tail = f", {stage[0].lower() + stage[1:]}" if stage and stage.lower() != "printing" else ""
        first = _job_line("Printing", name, job, left=True, tail=tail)
    elif state == "preparing":
        stage = job.get("stage")
        tail = f": {stage[0].lower() + stage[1:]}" if stage and stage.lower() != "printing" else ""
        first = f"Preparing to print {name}{tail}."
    elif state == "paused":
        first = _job_line("Paused", name, job, left=False)
    elif state == "heating":
        if _heating_mid_job(job):
            first = _job_line("Printing", name, job, left=True, tail=", waiting for the heaters")
        else:
            first = f"Heating up to print {name}."
    elif state == "finished":
        first = f"Finished {name}."
    elif state == "cancelled":
        first = f"Cancelled {name}." if job.get("file") else "The last print was cancelled."
    elif state == "error":
        first = f"Stopped with an error while printing {name}." if job.get("file") else "The printer reports an error."
    elif state == "shutdown":
        first = "The printer is shut down."
    elif state == "offline":
        first = "The printer is offline."
    else:
        first = "Idle."
    # A Bambu printer is the one selected in OrcaSlicer's Device tab, which may not be the one the profile is for:
    # say which one was read.
    device = ((snap.get("bambu") or {}).get("device") or {}).get("name")
    if device:
        first = f"{device}: {first}"
    parts = [first]
    temps = _temps_text(snap.get("temps") or {})
    if temps:
        parts.append(temps)
    probs = snap.get("problems") or []
    serious = [p for p in probs if p.get("severity") in ("fatal", "error")]
    if serious:
        more = f" (+{len(serious) - 1} more)" if len(serious) > 1 else ""
        parts.append(f"Problem: {serious[0]['message'].rstrip('.')}{more}.")
    elif probs:
        parts.append(f"{len(probs)} warning{'s' if len(probs) != 1 else ''}.")
    else:
        parts.append("No problems.")
    return " ".join(parts)
