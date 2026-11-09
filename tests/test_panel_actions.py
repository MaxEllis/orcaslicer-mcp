import asyncio

import httpx
import respx

import orcaslicer_mcp.server as srv

BASE = "http://x:13130/api/v1"


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok")
    m.setenv("ORCA_API_URL", "http://x:13130")
    m.setattr(srv, "_last_user_seq", None)
    m.setattr(srv, "_unsupported_url", None)
    m.setattr(srv, "_seq_url", "http://x:13130")


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
    assert uc["note"].startswith("Since your last action the user changed: Wall loops 2 → 4; Moved bracket.stl.")
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


@respx.mock
async def test_cap_boundary(monkeypatch):
    _env(monkeypatch)
    _status()
    e8 = [_entry(i, f"c{i}") for i in range(1, 9)]
    e9 = [_entry(i, f"c{i}") for i in range(1, 10)]
    _journal({"seq": 0, "changes": []}, {"seq": 8, "changes": e8}, {"seq": 17, "changes": e9})
    await srv.get_status()
    assert "\u2026and" not in (await srv.get_status())["user_changes"]["note"]
    assert "\u2026and 1 more (Print: 1)" in (await srv.get_status())["user_changes"]["note"]


@respx.mock
async def test_no_request_no_fetch_even_if_peer_in_flight(monkeypatch):
    _env(monkeypatch)
    srv._last_user_seq = 1
    route = _journal({"seq": 2, "changes": [_entry(2, "x")]})
    gate = asyncio.Event()

    async def slow(_request):
        await gate.wait()
        return httpx.Response(200, json={"app_version": "2.5.0"})
    respx.get(f"{BASE}/status").mock(side_effect=slow)

    async def quiet_tool():
        return {"ok": True}
    quiet = srv._with_user_changes(quiet_tool)
    peer = asyncio.ensure_future(srv.get_status())
    await asyncio.sleep(0.05)
    assert await quiet() == {"ok": True}
    assert route.call_count == 0
    gate.set()
    assert "user_changes" in await peer


@respx.mock
async def test_concurrent_calls_attach_each_entry_once(monkeypatch):
    _env(monkeypatch)
    srv._last_user_seq = 1
    _status()

    def journal(request):
        since = int(request.url.params["since"])
        ch = [_entry(s, f"c{s}") for s in (2, 3) if s > since]
        return httpx.Response(200, json={"seq": 3, "changes": ch})
    respx.get(f"{BASE}/user_changes").mock(side_effect=journal)
    a, b = await asyncio.gather(srv.get_status(), srv.get_status())
    seen = [c["seq"] for r in (a, b) for c in r.get("user_changes", {}).get("changes", [])]
    assert sorted(seen) == [2, 3]


@respx.mock
async def test_fetch_failures_leave_reply_untouched(monkeypatch):
    for resp in (httpx.Response(401, json={"error": "unauthorized"}),
                 httpx.Response(200, content=b"not json"),
                 httpx.Response(200, json={"changes": []}),
                 httpx.ConnectTimeout("slow")):
        respx.clear()
        _env(monkeypatch)
        srv._last_user_seq = 1
        _status()
        if isinstance(resp, Exception):
            respx.get(f"{BASE}/user_changes").mock(side_effect=resp)
        else:
            respx.get(f"{BASE}/user_changes").mock(return_value=resp)
        out = await srv.get_status()
        out.pop("connected_to", None)  # get_status's own field, not the journal's
        assert out == {"app_version": "2.5.0"}
        assert srv._last_user_seq == 1


@respx.mock
async def test_404_remembered_per_url(monkeypatch):
    _env(monkeypatch)
    _status()
    route = respx.get(f"{BASE}/user_changes").mock(
        return_value=httpx.Response(404, json={"error": "not_found"}))
    await srv.get_status()
    await srv.get_status()
    assert route.call_count == 1


@respx.mock
async def test_seq_never_moves_backwards(monkeypatch):
    _env(monkeypatch)
    srv._last_user_seq = 10
    _status()
    _journal({"seq": 7, "changes": []})
    await srv.get_status()
    assert srv._last_user_seq == 10


@respx.mock
async def test_404_expires_and_reprobes(monkeypatch):
    _env(monkeypatch)
    _status()
    route = respx.get(f"{BASE}/user_changes").mock(
        return_value=httpx.Response(404, json={"error": "not_found"}))
    await srv.get_status()
    await srv.get_status()
    assert route.call_count == 1  # remembered
    srv._unsupported_at -= srv._UNSUPPORTED_TTL_S + 1  # the 404 is now old
    route.mock(return_value=httpx.Response(200, json={"seq": 4, "changes": []}))
    await srv.get_status()
    assert route.call_count == 2  # probed once more
    assert srv._last_user_seq == 4
    assert srv._unsupported_url is None


@respx.mock
async def test_url_change_reseeds(monkeypatch):
    _env(monkeypatch)
    _status()
    srv._last_user_seq = 5
    srv._seq_url = "http://other:13130"
    route = _journal({"seq": 9000, "changes": [_entry(9000, "old journal entry")]})
    out = await srv.get_status()
    assert "user_changes" not in out
    assert route.calls[0].request.url.params["since"] == "0"
    assert srv._last_user_seq == 9000
    assert srv._seq_url == "http://x:13130"
