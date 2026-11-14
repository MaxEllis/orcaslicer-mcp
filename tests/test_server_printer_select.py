"""The Device tab's printers: list_printers and select_printer read and switch OrcaSlicer's own selection."""
import httpx, respx
import pytest
import orcaslicer_mcp.server as srv

F = "http://x:13130"
U = f"{F}/api/v1"

P1S = {"id": "01P", "name": "P1S workshop", "model": "Bambu Lab P1S", "model_id": "C12", "connection": "lan",
       "ip": "192.168.1.40", "online": True, "connected": True, "printing": False, "selected": True}
X1C = {"id": "03W", "name": "X1C office", "model": "Bambu Lab X1 Carbon", "model_id": "BL-P001", "connection": "cloud",
       "ip": None, "online": True, "connected": False, "printing": False, "selected": False}


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", F)
    m.delenv("ORCA_PRINTER_URL", raising=False)


def _status(caps=("printer_select",), backend="build", printer="Bambu Lab P1S 0.4 nozzle", model="Bambu Lab P1S"):
    respx.get(f"{U}/status").mock(return_value=httpx.Response(200, json={
        "backend": backend, "capabilities": list(caps),
        "presets": {"printer": printer, "print": "p", "filaments": ["f"]}}))
    respx.post(f"{U}/preset/config").mock(return_value=httpx.Response(200, json={
        "name": printer, "system": True, "config": {"print_host": "", "printer_model": model}}))


def _devices(*rows, selected="01P"):
    return {"agent": "bbl", "selected": selected, "devices": list(rows)}


def _no_wait(monkeypatch):
    async def sleep(_s): pass
    t = [0.0]
    def clock():
        t[0] += 1.0
        return t[0]
    monkeypatch.setattr(srv, "_select_sleep", sleep)
    monkeypatch.setattr(srv, "_select_clock", clock)


# --- list_printers -------------------------------------------------------------------------------------------

@respx.mock
async def test_list_printers_passes_the_list_through_with_a_headline(monkeypatch):
    _env(monkeypatch); _status()
    respx.get(f"{U}/printer/devices").mock(return_value=httpx.Response(200, json=_devices(P1S, X1C)))
    out = await srv.list_printers()
    assert [d["id"] for d in out["devices"]] == ["01P", "03W"] and out["selected"] == "01P"
    assert out["headline"] == "2 printers in OrcaSlicer's Device tab; P1S workshop (Bambu Lab P1S) is selected, connected."


@respx.mock
@pytest.mark.parametrize("rows, selected, head", [
    ((), None, "OrcaSlicer's Device tab lists no printers."),
    ((dict(P1S, selected=False), dict(X1C)), None, "2 printers in OrcaSlicer's Device tab, none selected."),
    ((dict(P1S, connected=False),), "01P",
     "1 printer in OrcaSlicer's Device tab; P1S workshop (Bambu Lab P1S) is selected, online, not connected."),
    ((dict(P1S, connected=False, online=False),), "01P",
     "1 printer in OrcaSlicer's Device tab; P1S workshop (Bambu Lab P1S) is selected, offline."),
    ((dict(P1S, printing=True),), "01P",
     "1 printer in OrcaSlicer's Device tab; P1S workshop (Bambu Lab P1S) is selected, printing."),
])
async def test_list_printers_headlines(monkeypatch, rows, selected, head):
    _env(monkeypatch); _status()
    respx.get(f"{U}/printer/devices").mock(return_value=httpx.Response(200, json=_devices(*rows, selected=selected)))
    assert (await srv.list_printers())["headline"] == head


@respx.mock
async def test_list_printers_on_a_build_without_the_routes_says_so(monkeypatch):
    _env(monkeypatch); _status(caps=("bambu_ams",))
    out = await srv.list_printers()
    assert "printer_select" in out["error"] and "Device tab" in out["error"]


@respx.mock
async def test_list_printers_on_the_plugin_says_what_to_do_by_hand(monkeypatch):
    _env(monkeypatch); _status(caps=(), backend="plugin")
    out = await srv.list_printers()
    assert out["error"].startswith("not_available_in_plugin") and "Device tab" in out["by_hand"]


@respx.mock
async def test_list_printers_garbage_reply_is_a_protocol_error(monkeypatch):
    _env(monkeypatch); _status()
    respx.get(f"{U}/printer/devices").mock(return_value=httpx.Response(200, json=[1, 2]))
    assert (await srv.list_printers())["error"].startswith("protocol_error")


# --- select_printer ------------------------------------------------------------------------------------------

@respx.mock
async def test_select_printer_by_name_selects_its_id_and_waits_for_the_connection(monkeypatch):
    _env(monkeypatch); _status(); _no_wait(monkeypatch)
    after_put = dict(X1C, selected=True, connected=False)
    devices = respx.get(f"{U}/printer/devices").mock(side_effect=[
        httpx.Response(200, json=_devices(P1S, X1C)),
        httpx.Response(200, json=_devices(dict(P1S, selected=False), after_put, selected="03W")),
        httpx.Response(200, json=_devices(dict(P1S, selected=False), dict(after_put, connected=True), selected="03W")),
    ])
    put = respx.put(f"{U}/printer/device").mock(return_value=httpx.Response(200, json={
        "changed": True, "before": "01P", "selected": True, "device": after_put}))
    out = await srv.select_printer("x1c OFFICE")
    assert put.calls[0].request.content == b'{"id":"03W"}'
    assert out["changed"] and out["selected"] and out["connected"] and out["device"]["connected"]
    assert out["before"] == {"id": "01P", "name": "P1S workshop", "model": "Bambu Lab P1S"}
    assert devices.call_count == 3
    assert out["profile_check"]["printer_model"] == "Bambu Lab X1 Carbon"
    assert out["headline"] == ("Selected X1C office (Bambu Lab X1 Carbon) in OrcaSlicer's Device tab; connected. "
                               "The active printer profile 'Bambu Lab P1S 0.4 nozzle' is for a Bambu Lab P1S; "
                               "the selected printer X1C office is a Bambu Lab X1 Carbon.")
    assert "select_preset" in out["profile_check"]["hint"]


@respx.mock
async def test_select_printer_gives_up_waiting_and_says_not_connected(monkeypatch):
    _env(monkeypatch); _status(model="Bambu Lab X1 Carbon"); _no_wait(monkeypatch)
    after_put = dict(X1C, selected=True)
    respx.get(f"{U}/printer/devices").mock(return_value=httpx.Response(200, json=_devices(dict(P1S, selected=False), after_put, selected="03W")))
    respx.put(f"{U}/printer/device").mock(return_value=httpx.Response(200, json={
        "changed": True, "before": "01P", "selected": True, "device": after_put}))
    out = await srv.select_printer("03W", wait_s=5)
    assert out["connected"] is False and "profile_check" not in out
    assert out["headline"] == "Selected X1C office (Bambu Lab X1 Carbon) in OrcaSlicer's Device tab; not connected yet after 5 s."


@respx.mock
async def test_select_printer_with_no_wait_returns_at_once(monkeypatch):
    _env(monkeypatch); _status(model="Bambu Lab X1 Carbon")
    devices = respx.get(f"{U}/printer/devices").mock(return_value=httpx.Response(200, json=_devices(P1S, X1C)))
    respx.put(f"{U}/printer/device").mock(return_value=httpx.Response(200, json={
        "changed": True, "before": "01P", "selected": True, "device": dict(X1C, selected=True)}))
    out = await srv.select_printer("03W", wait_s=0)
    assert devices.call_count == 1 and out["connected"] is False
    assert out["headline"].endswith("not connected yet.")


@respx.mock
async def test_select_printer_already_selected_changes_nothing(monkeypatch):
    _env(monkeypatch); _status()
    respx.get(f"{U}/printer/devices").mock(return_value=httpx.Response(200, json=_devices(P1S, X1C)))
    respx.put(f"{U}/printer/device").mock(return_value=httpx.Response(200, json={
        "changed": False, "before": "01P", "selected": True, "device": P1S}))
    out = await srv.select_printer("P1S workshop")
    assert out["changed"] is False and out["connected"] and "before" not in out
    assert out["headline"] == "P1S workshop (Bambu Lab P1S) was already selected in OrcaSlicer's Device tab, connected."


@respx.mock
async def test_select_printer_not_switched_yet_says_so(monkeypatch):
    _env(monkeypatch); _status(model="Bambu Lab X1 Carbon")
    respx.get(f"{U}/printer/devices").mock(return_value=httpx.Response(200, json=_devices(P1S, X1C)))
    respx.put(f"{U}/printer/device").mock(return_value=httpx.Response(200, json={
        "changed": True, "before": "01P", "selected": False, "device": X1C}))
    out = await srv.select_printer("03W", wait_s=0)
    assert out["selected"] is False
    assert out["headline"] == "Asked OrcaSlicer to select X1C office (Bambu Lab X1 Carbon); the Device tab has not switched yet."


@respx.mock
async def test_select_printer_unknown_name_lists_the_printers(monkeypatch):
    _env(monkeypatch); _status()
    respx.get(f"{U}/printer/devices").mock(return_value=httpx.Response(200, json=_devices(P1S, X1C)))
    put = respx.put(f"{U}/printer/device")
    out = await srv.select_printer("A1 mini")
    assert out["error"].startswith("unknown_printer") and "P1S workshop (01P), X1C office (03W)" == out["devices"]
    assert "list_printers" in out["hint"] and put.call_count == 0


@respx.mock
async def test_select_printer_ambiguous_name_asks_for_the_id(monkeypatch):
    _env(monkeypatch); _status()
    twin = dict(X1C, id="03X", name="P1S workshop")
    respx.get(f"{U}/printer/devices").mock(return_value=httpx.Response(200, json=_devices(P1S, twin)))
    out = await srv.select_printer("p1s workshop")
    assert out["error"].startswith("ambiguous_printer") and [d["id"] for d in out["devices"]] == ["01P", "03X"]


@respx.mock
async def test_select_printer_empty_name_is_refused(monkeypatch):
    _env(monkeypatch)
    assert "required" in (await srv.select_printer("  "))["error"]


@respx.mock
async def test_select_printer_forks_not_found_carries_the_detail(monkeypatch):
    _env(monkeypatch); _status()
    respx.get(f"{U}/printer/devices").mock(return_value=httpx.Response(200, json=_devices(P1S, X1C)))
    respx.put(f"{U}/printer/device").mock(return_value=httpx.Response(404, json={
        "error": "device_not_found", "detail": "No printer with that id is in the Device tab's list.",
        "devices": [{"id": "01P", "name": "P1S workshop"}]}))
    out = await srv.select_printer("03W", wait_s=0)
    assert "device_not_found" in out["error"] and "Device tab" in out["error"]
    assert out["devices"] == [{"id": "01P", "name": "P1S workshop"}]


@respx.mock
async def test_select_printer_on_a_build_without_the_routes_says_so(monkeypatch):
    _env(monkeypatch); _status(caps=("bambu_ams",))
    out = await srv.select_printer("03W")
    assert "printer_select" in out["error"]


@pytest.mark.parametrize("profile_model, printer_model, mismatch", [
    ("Bambu Lab P1S", "Bambu Lab P1S", False),
    ("Bambu Lab P1S", "P1S", False),
    ("Bambu Lab X1 Carbon", "Bambu Lab X1 Carbon", False),
    ("Bambu Lab P1S", "Bambu Lab P1P", True),
    ("Voron 2.4 350", "Bambu Lab P1S", True),
    ("Bambu Lab P1S", "Unknown", False),
    ("", "Bambu Lab P1S", False),
])
def test_profile_check(profile_model, printer_model, mismatch):
    out = srv._profile_check("prof", profile_model, {"name": "n", "model": printer_model})
    assert (out is not None) == mismatch
