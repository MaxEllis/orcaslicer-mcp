import json
from email.utils import formatdate

import httpx
import pytest
import respx

from orcaslicer_mcp.printer.errors import PrinterError
from orcaslicer_mcp.printer.moonraker import MoonrakerClient
from orcaslicer_mcp.printer.octoprint import OctoPrintClient

P = "http://192.0.2.10"
READY = {"result": {"klippy_state": "ready"}}


async def test_moonraker_identifies_itself():
    with respx.mock:
        respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json=READY))
        async with MoonrakerClient(P) as c:
            assert await c.identify() is True


@pytest.mark.parametrize("resp", [httpx.Response(404, text="<html>"), httpx.Response(200, json={"result": {}}),
                                  httpx.Response(200, text="not json")])
async def test_moonraker_identify_is_false_for_other_servers(resp):
    with respx.mock:
        respx.get(f"{P}/server/info").mock(return_value=resp)
        async with MoonrakerClient(P) as c:
            assert await c.identify() is False


async def test_moonraker_unreachable():
    with respx.mock:
        respx.get(f"{P}/server/info").mock(side_effect=httpx.ConnectError("refused"))
        async with MoonrakerClient(P) as c:
            assert await c.identify() is False
            with pytest.raises(PrinterError) as e:
                await c.server_info()
    assert e.value.code == "not_reachable" and P in e.value.message


@pytest.mark.parametrize("key,code", [(None, "auth_required"), ("test-key", "auth_rejected")])
async def test_auth_failures_propagate_from_identify(key, code):
    with respx.mock:
        respx.get(f"{P}/server/info").mock(return_value=httpx.Response(401))
        async with MoonrakerClient(P, api_key=key) as c:
            with pytest.raises(PrinterError) as e:
                await c.identify()
    assert e.value.code == code


async def test_the_api_key_is_sent():
    with respx.mock:
        route = respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json=READY))
        async with MoonrakerClient(P, api_key="test-key") as c:
            await c.server_info()
    assert route.calls.last.request.headers["X-Api-Key"] == "test-key"


async def test_moonraker_reads():
    with respx.mock:
        q = respx.get(url__startswith=f"{P}/printer/objects/query").mock(return_value=httpx.Response(
            200, json={"result": {"eventtime": 1.0, "status": {"webhooks": {"state": "ready"}}}}))
        g = respx.get(url__startswith=f"{P}/server/gcode_store").mock(return_value=httpx.Response(
            200, json={"result": {"gcode_store": [{"message": "ok", "time": 1.0, "type": "response"}]}}))
        h = respx.get(url__startswith=f"{P}/server/history/list").mock(return_value=httpx.Response(
            200, json={"result": {"count": 1, "jobs": [{"job_id": "1"}]}}))
        m = respx.get(url__startswith=f"{P}/server/files/metadata").mock(return_value=httpx.Response(404))
        async with MoonrakerClient(P) as c:
            assert await c.objects_query(["webhooks", "configfile=settings"]) == {"webhooks": {"state": "ready"}}
            assert (await c.gcode_store(5))[0]["message"] == "ok"
            assert (await c.history_list(3))[0]["job_id"] == "1"
            assert await c.file_metadata("missing.gcode") is None
    assert str(q.calls.last.request.url).endswith("/printer/objects/query?webhooks&configfile=settings")
    assert g.calls.last.request.url.params["count"] == "5"
    assert dict(h.calls.last.request.url.params) == {"limit": "3", "order": "desc"}
    assert m.calls.last.request.url.params["filename"] == "missing.gcode"


async def test_server_errors_are_protocol_errors():
    with respx.mock:
        respx.get(f"{P}/server/info").mock(return_value=httpx.Response(500))
        async with MoonrakerClient(P) as c:
            with pytest.raises(PrinterError) as e:
                await c.server_info()
    assert e.value.code == "protocol_error"


async def test_octoprint_reads():
    with respx.mock:
        respx.get(f"{P}/api/version").mock(return_value=httpx.Response(200, json={"api": "0.1", "server": "1.10.2"}))
        respx.get(url__startswith=f"{P}/api/printer").mock(
            return_value=httpx.Response(409, text="Printer is not operational"))
        respx.get(f"{P}/api/job").mock(return_value=httpx.Response(200, json={"state": "Offline"}))
        async with OctoPrintClient(P, api_key="test-key") as c:
            assert await c.identify() is True
            assert await c.printer() is None
            assert (await c.job())["state"] == "Offline"


async def test_octoprint_wrong_key():
    with respx.mock:
        respx.get(f"{P}/api/version").mock(return_value=httpx.Response(403))
        async with OctoPrintClient(P, api_key="wrong") as c:
            with pytest.raises(PrinterError) as e:
                await c.identify()
    assert e.value.code == "auth_rejected" and "OctoPrint" in e.value.message


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("call", ["server_info", "identify"])
async def test_refused_basic_auth_says_so_and_never_echoes_the_login(status, call):
    with respx.mock:
        respx.get(f"{P}/server/info").mock(return_value=httpx.Response(status))
        async with MoonrakerClient(P, auth=("test-user", "test-pass")) as c:
            with pytest.raises(PrinterError) as e:
                await getattr(c, call)()
    assert e.value.code == "auth_rejected"
    assert "user name and password" in e.value.message
    assert "API key" not in e.value.message and "ORCA_PRINTER_API_KEY" not in e.value.hint
    dumped = json.dumps(e.value.as_dict())
    assert "test-user" not in dumped and "test-pass" not in dumped


async def test_refused_api_key_wording_wins_when_both_are_set():
    with respx.mock:
        respx.get(f"{P}/server/info").mock(return_value=httpx.Response(401))
        async with MoonrakerClient(P, api_key="test-key", auth=("test-user", "test-pass")) as c:
            with pytest.raises(PrinterError) as e:
                await c.server_info()
    assert e.value.code == "auth_rejected" and "API key" in e.value.message


@pytest.mark.parametrize("bad", ["http://192.0.2.10:abc", "http://[::1"])
async def test_malformed_address_is_not_configured(bad):
    with respx.mock:
        async with MoonrakerClient(bad) as c:
            with pytest.raises(PrinterError) as e:
                await c.server_info()
    assert e.value.code == "not_configured" and bad in e.value.message


async def test_malformed_address_identify_is_false_not_a_raw_exception():
    with respx.mock:
        async with OctoPrintClient("http://192.0.2.10:abc") as c:
            assert await c.identify() is False


@pytest.mark.parametrize("cls", [MoonrakerClient, OctoPrintClient])
async def test_clients_record_the_servers_clock_from_the_date_header(cls):
    when = 1_700_000_000.0  # an invented moment
    path = "/server/info" if cls is MoonrakerClient else "/api/version"
    with respx.mock:
        route = respx.get(f"{P}{path}")
        async with cls(P) as c:
            assert c.server_time is None
            route.mock(return_value=httpx.Response(200, json=READY, headers={"Date": formatdate(when, usegmt=True)}))
            await c.get_json(path)
            assert c.server_time == when
            route.mock(return_value=httpx.Response(200, json=READY, headers={"Date": "not a date"}))
            await c.get_json(path)
            assert c.server_time == when  # an unreadable header leaves the last good value
            route.mock(return_value=httpx.Response(200, json=READY))
            await c.get_json(path)
            assert c.server_time == when  # so does a missing one


async def test_a_response_without_a_date_leaves_the_server_clock_unknown():
    with respx.mock:
        respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json=READY))
        async with MoonrakerClient(P) as c:
            await c.server_info()
            assert c.server_time is None


# --- the API key is trimmed; a login in the address is never kept -----------------------------------

@pytest.mark.parametrize("key", ["", "   ", "\n", None])
async def test_a_blank_api_key_counts_as_no_key(key):
    with respx.mock:
        route = respx.get(f"{P}/server/info").mock(return_value=httpx.Response(401))
        async with MoonrakerClient(P, api_key=key) as c:
            with pytest.raises(PrinterError) as e:
                await c.server_info()
    assert "x-api-key" not in route.calls.last.request.headers
    assert e.value.code == "auth_required"  # no key was sent, so it is not "rejected"


async def test_a_pasted_api_key_is_trimmed_before_it_is_sent():
    with respx.mock:
        route = respx.get(f"{P}/server/info").mock(return_value=httpx.Response(401))
        async with MoonrakerClient(P, api_key="  test-key\n") as c:
            with pytest.raises(PrinterError) as e:
                await c.server_info()
    assert route.calls.last.request.headers["X-Api-Key"] == "test-key"
    assert e.value.code == "auth_rejected"


@pytest.mark.parametrize("raw", [
    "http://test-user:pw-secret@192.0.2.10",
    "http://test-user:pw-secret@192.0.2.10/",
    "http://test-user:pa/ss-secret@192.0.2.10",
    "http://<redacted>@192.0.2.10",
])
@pytest.mark.parametrize("cls", [MoonrakerClient, OctoPrintClient])
async def test_a_login_in_the_base_url_is_dropped_before_any_message_can_echo_it(raw, cls):
    path = "/server/info" if cls is MoonrakerClient else "/api/version"
    with respx.mock:
        route = respx.get(f"{P}{path}").mock(side_effect=httpx.ConnectError("refused"))
        async with cls(raw) as c:
            assert c.base_url == P
            with pytest.raises(PrinterError) as e:
                await c.get_json(path)
    text = json.dumps(e.value.as_dict())
    assert P in e.value.message
    assert "secret" not in text and "test-user" not in text and "<redacted>" not in text
    assert "authorization" not in route.calls.last.request.headers  # nothing was sent as a login either


# --- every httpx failure becomes a PrinterError ------------------------------------------------------

@pytest.mark.parametrize("exc,code", [
    (httpx.DecodingError("bad gzip"), "protocol_error"),
    (httpx.TooManyRedirects("too many redirects"), "protocol_error"),
    (httpx.HTTPError("some other httpx failure"), "protocol_error"),
    (httpx.RemoteProtocolError("Server disconnected without sending a response."), "not_reachable"),
    (httpx.ReadTimeout("slow"), "not_reachable"),
    (httpx.ConnectError("refused"), "not_reachable"),
])
@pytest.mark.parametrize("cls", [MoonrakerClient, OctoPrintClient])
async def test_every_httpx_failure_is_a_printer_error_never_a_raw_exception(exc, code, cls):
    path = "/server/info" if cls is MoonrakerClient else "/api/version"
    with respx.mock:
        respx.get(f"{P}{path}").mock(side_effect=exc)
        async with cls(P) as c:
            with pytest.raises(PrinterError) as e:
                await c.get_json(path)
            assert await c.identify() is False  # a server that answers nonsense is simply not this one
    assert e.value.code == code
    assert path in e.value.message or P in e.value.message


async def test_a_reply_that_cannot_be_decoded_is_a_protocol_error():
    with respx.mock:
        # a stream, not content=: the body must be decoded while the request runs, not when the mock is built
        respx.get(f"{P}/server/info").mock(return_value=httpx.Response(
            200, stream=httpx.ByteStream(b"this is not gzip"), headers={"Content-Encoding": "gzip"}))
        async with MoonrakerClient(P) as c:
            with pytest.raises(PrinterError) as e:
                await c.server_info()
    assert e.value.code == "protocol_error" and "/server/info" in e.value.message


# --- a Moonraker reply whose result is not the expected object is a protocol_error --------------------

CALLS = {
    "server_info": lambda c: c.server_info(),
    "objects_query": lambda c: c.objects_query(["webhooks"]),
    "gcode_store": lambda c: c.gcode_store(5),
    "history_list": lambda c: c.history_list(3),
    "file_metadata": lambda c: c.file_metadata("test-part.gcode"),
}
PATHS = {"server_info": "/server/info", "objects_query": "/printer/objects/query",
         "gcode_store": "/server/gcode_store", "history_list": "/server/history/list",
         "file_metadata": "/server/files/metadata"}


@pytest.mark.parametrize("body", [{"result": "ok"}, {"result": [1, 2]}, {"result": None}, {"result": 5},
                                  {}, {"error": "x"}, [1, 2], "just text"])
@pytest.mark.parametrize("name", list(CALLS))
async def test_a_result_that_is_not_an_object_is_a_protocol_error(name, body):
    with respx.mock:
        respx.get(url__startswith=f"{P}{PATHS[name]}").mock(return_value=httpx.Response(200, json=body))
        async with MoonrakerClient(P) as c:
            with pytest.raises(PrinterError) as e:
                await CALLS[name](c)
    assert e.value.code == "protocol_error" and PATHS[name] in e.value.message


@pytest.mark.parametrize("name,inner", [
    ("objects_query", {"status": [1]}), ("objects_query", {"status": "x"}),
    ("gcode_store", {"gcode_store": {"a": 1}}), ("gcode_store", {"gcode_store": "x"}),
    ("history_list", {"jobs": {"a": 1}}), ("history_list", {"jobs": "x"}),
    ("gcode_store", {"gcode_store": [1, 2]}), ("history_list", {"jobs": ["x"]}),  # items are objects too
])
async def test_a_result_field_of_the_wrong_type_is_a_protocol_error(name, inner):
    with respx.mock:
        respx.get(url__startswith=f"{P}{PATHS[name]}").mock(
            return_value=httpx.Response(200, json={"result": inner}))
        async with MoonrakerClient(P) as c:
            with pytest.raises(PrinterError) as e:
                await CALLS[name](c)
    assert e.value.code == "protocol_error"


async def test_an_empty_result_object_is_still_an_answer():
    with respx.mock:
        for path in PATHS.values():
            respx.get(url__startswith=f"{P}{path}").mock(return_value=httpx.Response(200, json={"result": {}}))
        async with MoonrakerClient(P) as c:
            assert await c.server_info() == {}
            assert await c.objects_query(["webhooks"]) == {}
            assert await c.gcode_store(5) == []
            assert await c.history_list(3) == []
            assert await c.file_metadata("test-part.gcode") == {}


async def test_a_wrong_shaped_result_does_not_make_a_server_identify_as_moonraker():
    # identify() is the probe: odd shapes must read as "not Moonraker", never raise
    with respx.mock:
        respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json={"result": [1]}))
        async with MoonrakerClient(P) as c:
            assert await c.identify() is False


# --- identify() keeps the /server/info reply it got, for one use --------------------------------------

async def test_identify_keeps_its_reply_for_one_use():
    with respx.mock:
        route = respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json=READY))
        async with MoonrakerClient(P) as c:
            assert c.take_probe_info() is None            # nothing probed yet
            assert await c.identify() is True
            assert c.take_probe_info() == READY["result"]
            assert c.take_probe_info() is None            # a second snapshot must ask again
    assert route.call_count == 1


@pytest.mark.parametrize("body", [{"result": {}}, {"result": {"klippy_connected": True}}])
async def test_a_reply_that_is_not_klippers_is_not_kept(body):
    with respx.mock:
        respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json=body))
        async with MoonrakerClient(P) as c:
            assert await c.identify() is False
            assert c.take_probe_info() is None


async def test_a_failed_probe_keeps_nothing_even_after_an_earlier_good_one():
    with respx.mock:
        route = respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json=READY))
        async with MoonrakerClient(P) as c:
            assert await c.identify() is True
            route.mock(return_value=httpx.Response(500))
            assert await c.identify() is False
            assert c.take_probe_info() is None            # the earlier reply is not served after a failure


async def test_the_kept_reply_goes_stale(monkeypatch):
    from orcaslicer_mcp.printer import moonraker
    now = [1000.0]
    monkeypatch.setattr(moonraker, "_monotonic", lambda: now[0])
    with respx.mock:
        respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json=READY))
        async with MoonrakerClient(P) as c:
            assert await c.identify() is True
            now[0] += moonraker.PROBE_INFO_MAX_AGE_S + 0.1
            assert c.take_probe_info() is None
            assert await c.identify() is True
            now[0] += moonraker.PROBE_INFO_MAX_AGE_S - 0.1
            assert c.take_probe_info() == READY["result"]


async def test_octoprint_keeps_nothing_for_the_snapshot():
    with respx.mock:
        respx.get(f"{P}/api/version").mock(return_value=httpx.Response(200, json={"api": "0.1", "server": "1.10.2"}))
        async with OctoPrintClient(P) as c:
            assert await c.identify() is True
            assert not hasattr(c, "take_probe_info")
