#!/usr/bin/env python3
"""Drive the plugin inside a REAL stock OrcaSlicer 2.5 (through an SSH tunnel) and check the routes.

  ORCA_API_URL=http://127.0.0.1:13135 ORCA_API_TOKEN=... python -I run_live.py [phase]

phase "read" (default): read-only checks plus a 404 check. phase "write": settings writes
(restored afterwards) and the sensitive-key refusal. phase "select": switch the print preset to
another one and back; run it while the edited presets are clean, or OrcaSlicer will (rightly)
ask on screen about unsaved edits. phase "slice": the slice the user makes (plugin 0.2.0). With
ORCA_FORK_URL and ORCA_FORK_TOKEN set (an OrcaSlicer MCP build running the plugin too) the script slices
through the build's own API and compares; otherwise it asks you to press Slice plate. Never prints the token or
credential values.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("ORCA_API_URL", "http://127.0.0.1:13135").rstrip("/")
TOKEN = os.environ["ORCA_API_TOKEN"]
results = []


def call(method, path, body=None, token=TOKEN, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method)
    req.add_header("X-Api-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        out = e.code, json.loads(e.read() or b"{}")
    return out + (time.monotonic() - t0,)


def get_raw(path, base=URL, token=TOKEN, timeout=60):
    req = urllib.request.Request(base + path)
    req.add_header("X-Api-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def check(name, ok, info=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  [{info}]" if info else ""))


def phase_read():
    s, st, dt = call("GET", "/api/v1/status")
    check("status 200", s == 200, f"{dt:.2f}s")
    check("status says backend=plugin", st.get("backend") == "plugin")
    check("status app_version 2.5.x", str(st.get("app_version", "")).startswith("2.5"), st.get("app_version"))
    check("status presets present", bool(st.get("presets", {}).get("print")), json.dumps(st.get("presets")))
    print("   objects:", st.get("objects"), " modified:", st.get("modified"), " project:", st.get("project_name"))
    s, _, _ = call("GET", "/api/v1/status", token="wrong")
    check("wrong token -> 401", s == 401)
    s, cfg, dt = call("GET", "/api/v1/config")
    c = cfg.get("config", {})
    check("config 200 with many keys", s == 200 and len(c) > 300, f"{len(c)} keys, {dt:.2f}s")
    check("config hides host keys", not any(k in c for k in ("print_host", "printhost_apikey", "printhost_password")))
    s, cfg, dt = call("GET", "/api/v1/config?keys=layer_height,wall_loops")
    check("config ?keys filter", s == 200 and set(cfg.get("config", {})) == {"layer_height", "wall_loops"},
          f"{cfg.get('config')}, {dt:.2f}s")
    s, pr, dt = call("GET", "/api/v1/presets")
    check("presets lists all three groups", s == 200 and all(pr.get(g) for g in ("print", "filament", "printer")),
          f"{[len(pr.get(g, [])) for g in ('print', 'filament', 'printer')]}, {dt:.2f}s")
    printer = next((p["name"] for p in pr.get("printer", []) if p["selected"]), None)
    s, pc, _ = call("POST", "/api/v1/preset/config", {"type": "printer", "name": printer})
    cfgp = pc.get("config", {})
    cred = [k for k in ("printhost_apikey", "printhost_password", "printhost_user") if cfgp.get(k)]
    check("preset/config 200", s == 200, f"{printer}, {len(cfgp)} keys")
    check("preset/config credentials redacted", all(cfgp[k] == "<redacted>" for k in cred),
          f"{len(cred)} credential keys set")
    s, ob, dt = call("GET", "/api/v1/objects")
    objs = ob.get("objects", [])
    check("objects 200", s == 200, f"{len(objs)} objects, {dt:.2f}s")
    if objs:
        o = objs[0]
        print("   first object:", {k: o.get(k) for k in ("id", "name", "size_mm", "bbox_min", "on_plate")})
        check("object has id/size/bbox", all(k in o for k in ("id", "size_mm", "bbox_min", "transform")))
    s, pj, _ = call("GET", "/api/v1/project")
    check("project 200", s == 200 and "dirty" in pj, json.dumps(pj))
    s, nf, _ = call("POST", "/api/v1/slice", {})
    check("slice -> 404 backend=plugin", s == 404 and nf.get("backend") == "plugin")
    s, nf, _ = call("GET", "/api/v1/plate/render")
    check("render -> 404 backend=plugin", s == 404 and nf.get("backend") == "plugin")


def phase_write():
    _, cfg, _ = call("GET", "/api/v1/config?keys=layer_height,wall_loops,nozzle_temperature")
    old = cfg["config"]
    lh = "0.24" if old["layer_height"] != "0.24" else "0.28"
    s, body, dt = call("PUT", "/api/v1/config", {"layer_height": lh})
    check("PUT layer_height 200", s == 200 and body.get("applied") == ["layer_height"], f"{body}, {dt:.2f}s")
    _, cfg, _ = call("GET", "/api/v1/config?keys=layer_height")
    check("layer_height reads back", cfg["config"].get("layer_height") == lh, cfg["config"].get("layer_height"))
    _, st, _ = call("GET", "/api/v1/status")
    check("status shows print modified", "layer_height" in st.get("modified", {}).get("print", []),
          json.dumps(st.get("modified")))
    s, body, _ = call("PUT", "/api/v1/config", {"layer_height": "0.2", "post_process": "notepad.exe"})
    check("post_process refused, batch not applied", s == 422 and body.get("applied") == []
          and body.get("errors", {}).get("post_process") == "blocked_by_remote_api_policy", json.dumps(body))
    s, body, _ = call("PUT", "/api/v1/config", {"machine_start_gcode": "M112"})
    check("machine_start_gcode refused", s == 422, json.dumps(body.get("errors")))
    s, body, _ = call("PUT", "/api/v1/config", {"no_such_key_xyz": 1})
    check("unknown key 422", s == 422 and body["errors"].get("no_such_key_xyz") == "unknown_key")
    s, body, _ = call("PUT", "/api/v1/config", {"wall_loops": "abc"})
    check("bad value 422", s == 422, json.dumps(body.get("errors")))
    s, body, _ = call("PUT", "/api/v1/config", {"layer_height": old["layer_height"]})
    check("restore layer_height", s == 200)
    _, cfg, _ = call("GET", "/api/v1/config?keys=layer_height")
    check("restored value reads back", cfg["config"].get("layer_height") == old["layer_height"])


def phase_select():
    _, pr, _ = call("GET", "/api/v1/presets")
    cur = next(p["name"] for p in pr["print"] if p["selected"])
    other = next((p["name"] for p in pr["print"]
                  if not p["selected"] and p["visible"] and p.get("compatible", True)), None)
    if other is None:
        check("another visible print preset exists", False)
        return
    s, body, dt = call("PUT", "/api/v1/preset", {"type": "print", "name": other}, timeout=90)
    check(f"select print preset '{other}'", s == 200, f"{body}, {dt:.2f}s")
    _, st, _ = call("GET", "/api/v1/status")
    check("status shows the new print preset", st["presets"]["print"] == other, st["presets"]["print"])
    s, body, _ = call("PUT", "/api/v1/preset", {"type": "print", "name": cur}, timeout=90)
    check("select back", s == 200, json.dumps(body))
    s, _, _ = call("PUT", "/api/v1/preset", {"type": "print", "name": "No Such Preset"})
    check("unknown preset 422", s == 422)
    bad = next((p["name"] for p in pr["print"] if not p.get("compatible", True)), None)
    if bad:
        s, body, _ = call("PUT", "/api/v1/preset", {"type": "print", "name": bad})
        check("incompatible preset refused with a reason", s == 422 and "not offered" in body.get("detail", ""),
              body.get("detail", "")[:90])


FORK = os.environ.get("ORCA_FORK_URL", "").rstrip("/")
FORK_TOKEN = os.environ.get("ORCA_FORK_TOKEN", "")


def fork(method, path, body=None):
    """The same request to the build's API (call() reads URL at call time; its token default was bound at def)."""
    global URL
    saved = URL
    URL = FORK
    try:
        return call(method, path, body, token=FORK_TOKEN)
    finally:
        URL = saved


def slice_id():
    _, body, _ = call("GET", "/api/v1/slice/status")
    return body.get("slice_id")


def wait_new(before, seconds=240):
    """The next finished slice: a new slice_id, or an error."""
    end = time.monotonic() + seconds
    body = {}
    while time.monotonic() < end:
        _, body, _ = call("GET", "/api/v1/slice/status")
        if body.get("state") == "error" or (body.get("state") == "done" and body.get("slice_id") != before):
            return body
        time.sleep(1)
    return body


def user_slices(prompt="press Slice plate in OrcaSlicer now"):
    before = slice_id()
    if FORK:
        s, body, _ = fork("POST", "/api/v1/slice", {})
        if s not in (200, 202) or body.get("already_valid"):
            print("   build did not start a slice:", s, body)
    else:
        print(f"   >>> {prompt}")
    return wait_new(before)


def phase_slice():
    _, st, _ = call("GET", "/api/v1/status")
    check("plugin 0.2.0 says slice_read", "slice_read" in st.get("capabilities", []), st.get("plugin_version"))
    _, cfg, _ = call("GET", "/api/v1/config?keys=layer_height")
    lh = cfg["config"]["layer_height"]
    call("PUT", "/api/v1/config", {"layer_height": round(float(lh) + 0.04, 2)})  # so the first slice really slices
    body = user_slices()
    check("slice done", body.get("state") == "done" and body.get("valid") is True, body.get("message", "")[:80])
    stats = body.get("stats") or {}
    check("stats from the G-code", stats.get("estimated_time_seconds", 0) > 0 and stats.get("filament_used_g", 0) > 0
          and stats.get("layers", 0) > 0, json.dumps(stats))
    s, ctype, data = get_raw("/api/v1/gcode")
    check("gcode 200 text/plain", s == 200 and ctype.startswith("text/plain") and data.startswith(b"; HEADER_BLOCK"),
          f"{len(data)} bytes")
    if FORK:
        _, fb, _ = fork("GET", "/api/v1/slice/status")
        fs = fb.get("stats") or {}
        check("time matches the build's", abs(fs.get("estimated_time_seconds", -9) - stats.get("estimated_time_seconds", 0)) < 1.5,
              f"{fs.get('estimated_time_seconds')} vs {stats.get('estimated_time_seconds')}")
        check("filament matches the build's", abs(fs.get("filament_used_g", -9) - stats.get("filament_used_g", 0)) < 0.02,
              f"{fs.get('filament_used_g')} vs {stats.get('filament_used_g')}")
        s, _, fdata = get_raw("/api/v1/gcode", base=FORK, token=FORK_TOKEN)
        check("same G-code as the build serves", s == 200 and fdata == data, f"{len(fdata)} vs {len(data)} bytes")
    s, _, _ = call("PUT", "/api/v1/config", {"layer_height": lh})  # put it back: a change since the slice
    _, b2, _ = call("GET", "/api/v1/slice/status")
    check("a settings change makes it out of date", s == 200 and b2.get("valid") is False and b2.get("stale") is True,
          b2.get("message", "")[:60])
    check("gcode 409 when out of date", get_raw("/api/v1/gcode")[0] == 409)
    body = user_slices("press Slice plate again")
    check("slice again: done and valid", body.get("state") == "done" and body.get("valid") is True)
    if FORK:
        fork("POST", "/api/v1/plates", {})
        _, pl, _ = fork("GET", "/api/v1/plates")
        last = len(pl.get("plates", [])) - 1
        s, _, _ = fork("POST", f"/api/v1/plates/{last}/select", {})
        _, b4, _ = call("GET", "/api/v1/slice/status")
        check("another plate: nothing sliced there", s == 200 and b4.get("state") == "idle", f"{s} {b4.get('state')}")
        check("another plate: gcode 409", get_raw("/api/v1/gcode")[0] == 409)
        fork("POST", "/api/v1/plates/0/select", {})
        fork("DELETE", f"/api/v1/plates/{last}")
        # A second plate widens OrcaSlicer's plate grid, which clears every plate's slice flag although the first
        # plate's print stays the same: the build's status says idle until asked to slice, then answers
        # already_valid with the same G-code. The plugin keeps the slice throughout.
        _, b5, _ = call("GET", "/api/v1/slice/status")
        check("back on the first plate: its slice is kept", b5.get("state") == "done" and b5.get("valid") is True,
              f"{b5.get('state')}/{b5.get('valid')}")
        _, fs5, _ = fork("POST", "/api/v1/slice", {})
        check("the build has nothing to slice again", fs5.get("already_valid") is True, json.dumps(fs5))
        _, _, pdata = get_raw("/api/v1/gcode")
        _, _, fdata = get_raw("/api/v1/gcode", base=FORK, token=FORK_TOKEN)
        check("and serves the same G-code as the plugin", pdata == fdata and len(pdata) > 0,
              f"{len(pdata)} vs {len(fdata)} bytes")

phase = sys.argv[1] if len(sys.argv) > 1 else "read"
{"read": phase_read, "write": phase_write, "select": phase_select, "slice": phase_slice}[phase]()
print(f"\n{phase}: {sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
