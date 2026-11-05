import json
import httpx, respx
import orcaslicer_mcp.server as srv
from orcaslicer_mcp.client import OrcaClient
from orcaslicer_mcp.config import load_config


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


_PROJECT = {"name": "float_spinner", "path": "/u/float_spinner.3mf", "saved": True,
            "dirty": False, "plates": 4, "objects": 9}
_RECENT = {"recent": [
    {"index": 0, "path": "/u/float_spinner.3mf", "name": "float_spinner", "exists": True, "open": True,
     "size_bytes": 1834021, "modified": "2026-11-04T10:00:00Z"},
    {"index": 1, "path": "/u/old.3mf", "name": "old", "exists": False, "open": False}], "count": 2}


def _body(route):
    return json.loads(route.calls.last.request.content)


# --- get_status: project folded into {name, path, dirty} -------------------------------

@respx.mock
async def test_get_status_folds_project_fields(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/status").mock(return_value=httpx.Response(200, json={
        "app": "OrcaSlicer", "project": "/u/float_spinner.3mf", "project_name": "float_spinner",
        "project_dirty": True, "presets": {"filaments": ["A"], "print": "P", "printer": "M"}}))
    out = await srv.get_status()
    assert out["project"] == {"name": "float_spinner", "path": "/u/float_spinner.3mf", "dirty": True}
    assert "project_name" not in out and "project_dirty" not in out


@respx.mock
async def test_get_status_old_build_keeps_project_string(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/status").mock(return_value=httpx.Response(200, json={
        "app": "OrcaSlicer", "project": "", "presets": {"filaments": [], "print": "P", "printer": "M"}}))
    out = await srv.get_status()
    assert out["project"] == ""


@respx.mock
async def test_get_status_unsaved_project_has_empty_path(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/status").mock(return_value=httpx.Response(200, json={
        "app": "OrcaSlicer", "project": "", "project_name": "Untitled", "project_dirty": False}))
    async with OrcaClient(load_config()) as c:
        out = await c.get_status()
    assert out["project"] == {"name": "Untitled", "path": "", "dirty": False}


# --- get_project -----------------------------------------------------------------------

@respx.mock
async def test_get_project(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/project").mock(return_value=httpx.Response(200, json=dict(
        _PROJECT, modified=["print"], recent_count=2)))
    out = await srv.get_project()
    assert out["dirty"] is False and out["modified"] == ["print"] and out["recent_count"] == 2


@respx.mock
async def test_get_project_needs_fork_15(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/project").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.get_project()
    assert "fork v2.4.2-mcp.15" in out["error"]


# --- save_project ----------------------------------------------------------------------

@respx.mock
async def test_save_project_default_saves_to_own_path(monkeypatch):
    _env(monkeypatch)
    route = respx.post("http://x:13130/api/v1/project/save").mock(return_value=httpx.Response(200, json={
        "saved": "/u/float_spinner.3mf", "bytes": 1834021, "project": _PROJECT}))
    out = await srv.save_project()
    assert out["saved"] == "/u/float_spinner.3mf" and out["bytes"] == 1834021
    assert _body(route) == {"path": None, "overwrite": False}


@respx.mock
async def test_save_project_as_sends_path_and_overwrite(monkeypatch):
    _env(monkeypatch)
    route = respx.post("http://x:13130/api/v1/project/save").mock(return_value=httpx.Response(200, json={
        "saved": "/u/b.3mf", "bytes": 10, "project": dict(_PROJECT, path="/u/b.3mf")}))
    await srv.save_project("/u/b.3mf", overwrite=True)
    assert _body(route) == {"path": "/u/b.3mf", "overwrite": True}


@respx.mock
async def test_save_project_never_saved_422(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/project/save").mock(return_value=httpx.Response(422, json={
        "error": "no_project_path", "detail": "the project has never been saved; pass a path"}))
    out = await srv.save_project()
    assert out["error"] == "no_project_path"


@respx.mock
async def test_save_project_exists_409_passes_through(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/project/save").mock(return_value=httpx.Response(409, json={
        "error": "exists", "path": "/u/b.3mf"}))
    out = await srv.save_project("/u/b.3mf")
    assert out["error"] == "exists"


@respx.mock
async def test_save_project_route_missing_names_the_build(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/project/save").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.save_project()
    assert "fork v2.4.2-mcp.15" in out["error"]


# --- open_project / open_recent_project ------------------------------------------------

@respx.mock
async def test_open_project_defaults(monkeypatch):
    _env(monkeypatch)
    route = respx.post("http://x:13130/api/v1/project/open").mock(return_value=httpx.Response(200, json={
        "opened": "/u/float_spinner.3mf", "plates": 4, "objects": 9, "had_unsaved_changes": False,
        "discarded_changes": [], "project": _PROJECT}))
    out = await srv.open_project("/u/float_spinner.3mf")
    assert out["opened"] == "/u/float_spinner.3mf"
    assert _body(route) == {"path": "/u/float_spinner.3mf", "if_unsaved": "fail", "presets": "project"}


@respx.mock
async def test_open_project_passes_choices(monkeypatch):
    _env(monkeypatch)
    route = respx.post("http://x:13130/api/v1/project/open").mock(return_value=httpx.Response(200, json={
        "opened": "/u/a.3mf", "project": _PROJECT, "presets_restored": ["printer"]}))
    await srv.open_project("/u/a.3mf", if_unsaved="save", presets="keep_current")
    assert _body(route) == {"path": "/u/a.3mf", "if_unsaved": "save", "presets": "keep_current"}


@respx.mock
async def test_open_project_unsaved_changes_422(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/project/open").mock(return_value=httpx.Response(422, json={
        "error": "unsaved_changes", "detail": "pass if_unsaved: \"save\" or \"discard\"", "modified": ["print"]}))
    out = await srv.open_project("/u/a.3mf")
    assert out["error"] == "unsaved_changes"


@respx.mock
async def test_open_project_file_not_found_keeps_its_name(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/project/open").mock(return_value=httpx.Response(404, json={
        "error": "file_not_found", "path": "/u/missing.3mf"}))
    out = await srv.open_project("/u/missing.3mf")
    assert out["error"] == "file_not_found"


@respx.mock
async def test_open_recent_project_sends_index_not_path(monkeypatch):
    _env(monkeypatch)
    route = respx.post("http://x:13130/api/v1/project/open").mock(return_value=httpx.Response(200, json={
        "opened": "/u/old.3mf", "project": dict(_PROJECT, path="/u/old.3mf")}))
    out = await srv.open_recent_project(1, if_unsaved="discard")
    assert out["opened"] == "/u/old.3mf"
    assert _body(route) == {"recent": 1, "if_unsaved": "discard", "presets": "project"}


@respx.mock
async def test_open_recent_project_unknown_index_404(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/project/open").mock(return_value=httpx.Response(404, json={
        "error": "unknown_recent", "index": 7, "count": 2}))
    out = await srv.open_recent_project(7)
    assert out["error"] == "unknown_recent"


# --- new_project -----------------------------------------------------------------------

@respx.mock
async def test_new_project_default_fails_on_unsaved(monkeypatch):
    _env(monkeypatch)
    route = respx.post("http://x:13130/api/v1/project/new").mock(return_value=httpx.Response(200, json={
        "ok": True, "had_unsaved_changes": False, "kept_changes": [],
        "project": {"name": "Untitled", "path": "", "saved": False, "dirty": False, "plates": 1, "objects": 0}}))
    out = await srv.new_project()
    assert out["ok"] is True and out["project"]["name"] == "Untitled"
    assert _body(route) == {"if_unsaved": "fail"}


@respx.mock
async def test_new_project_with_name(monkeypatch):
    _env(monkeypatch)
    route = respx.post("http://x:13130/api/v1/project/new").mock(return_value=httpx.Response(200, json={
        "ok": True, "project": {"name": "bracket v2", "path": ""}}))
    out = await srv.new_project(if_unsaved="discard", name="bracket v2")
    assert out["project"]["name"] == "bracket v2"
    assert _body(route) == {"if_unsaved": "discard", "name": "bracket v2"}


# --- recent list -----------------------------------------------------------------------

@respx.mock
async def test_list_recent_projects(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/project/recent").mock(return_value=httpx.Response(200, json=_RECENT))
    out = await srv.list_recent_projects()
    assert out["count"] == 2 and out["recent"][1]["exists"] is False


@respx.mock
async def test_forget_recent_project_by_path(monkeypatch):
    _env(monkeypatch)
    route = respx.delete("http://x:13130/api/v1/project/recent").mock(return_value=httpx.Response(200, json=dict(
        recent=_RECENT["recent"][:1], count=1, forgotten=["/u/old.3mf"])))
    out = await srv.forget_recent_project(path="/u/old.3mf")
    assert out["forgotten"] == ["/u/old.3mf"] and out["count"] == 1
    assert _body(route) == {"path": "/u/old.3mf"}


@respx.mock
async def test_forget_recent_project_by_index_and_all(monkeypatch):
    _env(monkeypatch)
    route = respx.delete("http://x:13130/api/v1/project/recent").mock(return_value=httpx.Response(200, json={
        "recent": [], "count": 0, "forgotten": []}))
    await srv.forget_recent_project(index=1)
    assert _body(route) == {"index": 1}
    await srv.forget_recent_project(all=True)
    assert _body(route) == {"all": True}


async def test_forget_recent_project_needs_a_target(monkeypatch):
    _env(monkeypatch)
    out = await srv.forget_recent_project()
    assert out["error"] == "nothing_to_forget"


@respx.mock
async def test_forget_recent_project_not_in_list_404(monkeypatch):
    _env(monkeypatch)
    respx.delete("http://x:13130/api/v1/project/recent").mock(return_value=httpx.Response(404, json={
        "error": "not_in_recent", "path": "/u/x.3mf"}))
    out = await srv.forget_recent_project(path="/u/x.3mf")
    assert out["error"] == "not_in_recent"


# --- browse ----------------------------------------------------------------------------

@respx.mock
async def test_browse_projects_sends_dir(monkeypatch):
    _env(monkeypatch)
    route = respx.get("http://x:13130/api/v1/project/browse").mock(return_value=httpx.Response(200, json={
        "dir": "/u/projects", "files": [{"path": "/u/projects/a.3mf", "name": "a", "exists": True, "open": False,
                                         "size_bytes": 5, "modified": "2026-11-01T00:00:00Z"}],
        "count": 1, "truncated": False}))
    out = await srv.browse_projects("/u/projects")
    assert out["count"] == 1 and out["files"][0]["name"] == "a"
    assert route.calls.last.request.url.params["dir"] == "/u/projects"


@respx.mock
async def test_browse_projects_default_dir_sends_no_param(monkeypatch):
    _env(monkeypatch)
    route = respx.get("http://x:13130/api/v1/project/browse").mock(return_value=httpx.Response(200, json={
        "dir": "/u", "files": [], "count": 0, "truncated": False}))
    await srv.browse_projects()
    assert "dir" not in route.calls.last.request.url.params


@respx.mock
async def test_browse_projects_directory_not_found_404(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/project/browse").mock(return_value=httpx.Response(404, json={
        "error": "directory_not_found", "dir": "/nowhere"}))
    out = await srv.browse_projects("/nowhere")
    assert out["error"] == "directory_not_found"


# --- move / delete ---------------------------------------------------------------------

@respx.mock
async def test_move_project_file(monkeypatch):
    _env(monkeypatch)
    route = respx.post("http://x:13130/api/v1/project/move").mock(return_value=httpx.Response(200, json={
        "moved": "/u/a.3mf", "to": "/u/b.3mf", "bytes": 10, "was_open": True, "recent_updated": True,
        "project": dict(_PROJECT, path="/u/b.3mf", name="b")}))
    out = await srv.move_project_file("/u/a.3mf", "/u/b.3mf")
    assert out["to"] == "/u/b.3mf" and out["was_open"] is True
    assert _body(route) == {"from": "/u/a.3mf", "to": "/u/b.3mf", "overwrite": False}


@respx.mock
async def test_move_project_file_exists_409(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/project/move").mock(return_value=httpx.Response(409, json={
        "error": "exists", "path": "/u/b.3mf"}))
    out = await srv.move_project_file("/u/a.3mf", "/u/b.3mf")
    assert out["error"] == "exists"


@respx.mock
async def test_delete_project_file(monkeypatch):
    _env(monkeypatch)
    route = respx.delete("http://x:13130/api/v1/project/file").mock(return_value=httpx.Response(200, json={
        "deleted": "/u/old.3mf", "bytes": 10, "recent_updated": True}))
    out = await srv.delete_project_file("/u/old.3mf")
    assert out["deleted"] == "/u/old.3mf"
    assert _body(route) == {"path": "/u/old.3mf"}


@respx.mock
async def test_delete_project_file_refuses_open_project(monkeypatch):
    _env(monkeypatch)
    respx.delete("http://x:13130/api/v1/project/file").mock(return_value=httpx.Response(409, json={
        "error": "project_is_open", "path": "/u/float_spinner.3mf"}))
    out = await srv.delete_project_file("/u/float_spinner.3mf")
    assert out["error"] == "project_is_open"


# --- annotations -----------------------------------------------------------------------

def test_project_tool_annotations():
    tools = srv.mcp._tool_manager._tools
    for name in ("get_project", "list_recent_projects", "browse_projects"):
        assert tools[name].annotations.read_only_hint is True, name
    for name in ("open_project", "open_recent_project", "new_project", "delete_project_file"):
        ann = tools[name].annotations
        assert ann.read_only_hint is False and ann.destructive_hint is True, name
    for name in ("save_project", "forget_recent_project", "move_project_file"):
        ann = tools[name].annotations
        assert ann.read_only_hint is False and ann.destructive_hint is False, name
