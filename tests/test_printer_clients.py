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
