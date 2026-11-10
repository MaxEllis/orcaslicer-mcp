import importlib
import os
import json
import pathlib
import sys
import urllib.request
import urllib.error

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
# orcaslicer-mcp source for the client, drift and tool-matrix tests: ORCASLICER_MCP_SRC, or this
# repo's src. Chosen once here so every test imports the same copy.
MCP_SRC = pathlib.Path(os.environ.get("ORCASLICER_MCP_SRC", ROOT.parent / "src"))
if (MCP_SRC / "orcaslicer_mcp").is_dir():
    sys.path.insert(0, str(MCP_SRC))

import fake_orca  # noqa: E402

sys.modules["orca"] = fake_orca.build_module()
mod = importlib.import_module("orcaslicer_mcp_plugin")


@pytest.fixture
def plugin(tmp_path):
    """A loaded plugin on a free port, as OrcaSlicer would run it."""
    fake_orca.STATE.reset(storage=str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps({"port": 0}))  # 0 is refused -> default
    cap = mod.McpCapability()
    # Bind an ephemeral port instead of 13130 so tests never collide.
    orig = mod.load_port
    mod.load_port = lambda folder: 0
    try:
        cap.on_load()
    finally:
        mod.load_port = orig
    assert not cap._error, cap._error
    cap.base = f"http://127.0.0.1:{cap._service.port}"
    yield cap
    cap.on_unload()


def call(cap, method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(cap.base + path, data=data, method=method)
    req.add_header("X-Api-Token", cap._token if token is None else token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ---- the user's own slice (plugin 0.2.0): a slice folder the plugin may read, and the events of a slice
@pytest.fixture
def folder(tmp_path, monkeypatch):
    """The plugin's slice folder, set before the plugin loads (request `folder` before `plugin`)."""
    f = tmp_path / "temp" / "orcaslicer_model"
    (f / "Tue_Nov_10" / "10_51_04#1#44" / "Metadata").mkdir(parents=True)
    monkeypatch.setattr(mod, "current_slice_folders", lambda: [str(f)])
    return f


def gcode_file(folder, body: str, name: str = ".1.0.gcode") -> str:
    p = folder / "Tue_Nov_10" / "10_51_04#1#44" / "Metadata" / name
    p.write_text(body)
    return str(p)


def event_ctx(code="Ok", msg="", index=-1):
    import types
    codes = fake_orca.build_module().LifecycleEvtCode
    return types.SimpleNamespace(code=getattr(codes, code), msg=msg, index=index, name="", id="")


def slice_now(cap, path, code="Ok", msg=""):
    """What OrcaSlicer sends when the user presses Slice plate: two events on the slicing worker, one on the UI
    thread."""
    ev = fake_orca.build_module().LifecycleEvent
    cap.on_lifecycle_event(ev.SliceStarted, event_ctx())
    cap.on_lifecycle_event(ev.GCodeExportFinished, event_ctx(msg=path))
    fake_orca.UI.run_blocking(lambda: cap.on_lifecycle_event(ev.SlicingJobComplete, event_ctx(code, msg)))
