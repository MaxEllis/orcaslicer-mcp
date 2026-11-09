import httpx
import respx

import orcaslicer_mcp.server as srv

BASE = "http://x:13130/api/v1"


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok")
    m.setenv("ORCA_API_URL", "http://x:13130")
    m.setattr(srv, "_last_user_seq", None)


def _entry(seq, text, group="Print"):
    return {"seq": seq, "time": "10:00", "kind": "setting", "group": group, "text": text}


def _status():
    respx.get(f"{BASE}/status").mock(return_value=httpx.Response(200, json={"app_version": "2.5.0"}))


def _journal(*responses):
    return respx.get(f"{BASE}/user_changes").mock(
        side_effect=[httpx.Response(200, json=r) for r in responses])


@respx.mock
async def test_first_call_seeds_only(monkeypatch):
    _env(monkeypatch)
    _status()
    _journal({"seq": 3, "changes": [_entry(3, "Wall loops 2 → 4")]})
    out = await srv.get_status()
    assert "user_changes" not in out
    assert srv._last_user_seq == 3


@respx.mock
async def test_changes_attached_once(monkeypatch):
    _env(monkeypatch)
    _status()
    route = _journal(
        {"seq": 3, "changes": []},
        {"seq": 5, "changes": [_entry(4, "Wall loops 2 → 4"), _entry(5, "Moved bracket.stl", "Model")]},
        {"seq": 5, "changes": []},
    )
    await srv.get_status()
    out = await srv.get_status()
    uc = out["user_changes"]
    assert [c["seq"] for c in uc["changes"]] == [4, 5]
    assert "Wall loops 2 → 4; Moved bracket.stl" in uc["note"]
    assert "don't undo them without asking" in uc["note"]
    assert route.calls[1].request.url.params["since"] == "3"
    again = await srv.get_status()
    assert "user_changes" not in again
    assert route.calls[2].request.url.params["since"] == "5"


@respx.mock
async def test_no_field_when_none(monkeypatch):
    _env(monkeypatch)
    _status()
    _journal({"seq": 3, "changes": []}, {"seq": 3, "changes": []})
    await srv.get_status()
    assert "user_changes" not in await srv.get_status()


@respx.mock
async def test_old_build_404_ignored(monkeypatch):
    _env(monkeypatch)
    _status()
    respx.get(f"{BASE}/user_changes").mock(
        return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.get_status()
    assert out["app_version"] == "2.5.0"
    assert "user_changes" not in out
    assert srv._last_user_seq is None


@respx.mock
async def test_non_dict_result_untouched(monkeypatch):
    _env(monkeypatch)
    route = _journal({"seq": 3, "changes": [_entry(3, "x")]})
    result = await srv._attach_user_changes(["a", "b"])
    assert result == ["a", "b"]
    assert route.call_count == 0
    assert srv._last_user_seq is None


@respx.mock
async def test_reply_caps_at_8(monkeypatch):
    _env(monkeypatch)
    _status()
    groups = ["Print"] * 3 + ["Filament"] * 2 + ["Model"] * 5 + ["Print"] * 1
    entries = [_entry(i + 1, f"change {i + 1}", g) for i, g in enumerate(groups)]  # 11
    _journal({"seq": 0, "changes": []}, {"seq": 11, "changes": entries})
    await srv.get_status()
    uc = (await srv.get_status())["user_changes"]
    assert len(uc["changes"]) == 11
    note = uc["note"]
    assert "change 8" in note and "change 9" not in note
    # remaining entries 9..11 are Model, Model, Print (first-appearance order)
    assert "…and 3 more (Model: 2, Print: 1)" in note


@respx.mock
async def test_reset_reseeds_and_reports(monkeypatch):
    _env(monkeypatch)
    _status()
    route = _journal(
        {"seq": 9000, "changes": []},
        {"seq": 100, "reset": True, "changes": [_entry(99, "Wall loops 2 → 4"), _entry(100, "New project", "Project")]},
        {"seq": 100, "changes": []},
    )
    await srv.get_status()
    out = await srv.get_status()
    assert [c["seq"] for c in out["user_changes"]["changes"]] == [99, 100]
    assert srv._last_user_seq == 100
    await srv.get_status()
    assert route.calls[2].request.url.params["since"] == "100"
