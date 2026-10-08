#!/usr/bin/env python3
"""Runtime checks for the three Remote API fixes (orcaslicer-mcp #9, #10, #11).

Usage: ORCA_BASE=http://127.0.0.1:13131 ORCA_TOKEN=... python3 orca-runtime-test.py
Read-mostly: every write is restored, and each check asserts the BEFORE state too,
so a check cannot pass by accident on an already-broken value.
"""
import json, os, sys, urllib.request, urllib.error

BASE  = os.environ.get("ORCA_BASE", "http://127.0.0.1:13131")
TOKEN = os.environ["ORCA_TOKEN"]
results = []

def call(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Token", TOKEN)
    if data: req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try: return e.code, json.loads(raw or "{}")
        except Exception: return e.code, {"_raw": raw}

def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (("  | " + detail) if detail else ""))

def get_key(k):
    s, j = call("GET", "/api/v1/config?keys=" + k)
    return j.get("config", {}).get(k)

print("=== connectivity ===")
s, st = call("GET", "/api/v1/status")
check("GET /status reachable", s == 200, "status=%d" % s)
if s != 200:
    sys.exit("cannot reach OrcaSlicer Remote API at " + BASE)
print(json.dumps({k: st.get(k) for k in ("presets", "modified") if k in st}, indent=2)[:800])

# ---------------------------------------------------------------- #9
print("\n=== #9 per-filament length guard ===")
KEY = "hot_plate_temp"
before = get_key(KEY)
check("baseline readable", before is not None, "%s=%r" % (KEY, before))
n = len(before.split(",")) if before else 0

# a write with one MORE entry than the preset holds must be refused
bad = ",".join((before.split(",") + ["70"])) if before else "70,70"
s, j = call("PUT", "/api/v1/config", {KEY: bad})
errs = j.get("errors", {})
check("over-long write rejected with 422", s == 422, "status=%d" % s)
check("error names per_filament_length_mismatch",
      "per_filament_length_mismatch" in str(errs.get(KEY, "")), str(errs.get(KEY, ""))[:160])
check("nothing applied", j.get("applied") == [], repr(j.get("applied")))
after = get_key(KEY)
check("value UNCHANGED after rejected write", after == before, "before=%r after=%r" % (before, after))

# a single-value write to the edited preset must still work (N==1 case)
if n == 1:
    s, j = call("PUT", "/api/v1/config", {KEY: before})
    check("write-back of unchanged single value still accepted", s == 200, "status=%d" % s)
    check("value still unchanged", get_key(KEY) == before, "")
else:
    s, j = call("PUT", "/api/v1/config", {KEY: before})
    check("merged %d-entry write-back rejected (the #9 bug)" % n, s == 422,
          "status=%d err=%s" % (s, str(j.get("errors", {}).get(KEY, ""))[:120]))
    check("value UNCHANGED after merged write-back", get_key(KEY) == before, "")

# ---------------------------------------------------------------- #10
print("\n=== #10 select_preset reports discarded groups ===")
PK = "wall_loops"
orig_wl = get_key(PK)
s, j = call("PUT", "/api/v1/config", {PK: str(int(orig_wl) + 1)})
check("print-group override applied", s == 200, "status=%d %s" % (s, j.get("applied")))
s, st2 = call("GET", "/api/v1/status")
mod_print = (st2.get("modified") or {}).get("print", [])
check("status shows print dirty BEFORE select", PK in mod_print, repr(mod_print)[:160])

cur_fil = ((st2.get("presets") or {}).get("filaments") or [None])[0]
check("current filament preset known", bool(cur_fil), repr(cur_fil))
s, j = call("PUT", "/api/v1/preset", {"type": "filament", "name": cur_fil})
check("select_preset succeeded", s == 200, "status=%d" % s)
dc = j.get("discarded_changes")
check("response carries discarded_changes", dc is not None, repr(dc))
check("discarded_changes names the print group", bool(dc) and "print" in dc, repr(dc))

s, st3 = call("GET", "/api/v1/status")
check("print overrides were in fact discarded",
      PK not in ((st3.get("modified") or {}).get("print", [])), "")
now_wl = get_key(PK)
if now_wl != orig_wl:
    call("PUT", "/api/v1/config", {PK: orig_wl})
check("wall_loops restored", get_key(PK) == orig_wl, "orig=%r now=%r" % (orig_wl, get_key(PK)))

# ---------------------------------------------------------------- summary
print("\n=== summary ===")
bad_n = sum(1 for _, ok, _ in results if not ok)
for nme, ok, d in results:
    if not ok: print("  FAILED: " + nme + "  | " + d)
print("%d checks, %d failed" % (len(results), bad_n))
sys.exit(1 if bad_n else 0)
