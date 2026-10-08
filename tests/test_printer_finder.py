import json

import httpx
import pytest
import respx

from orcaslicer_mcp.errors import ConfigError, NotReachable, Unauthorized
from orcaslicer_mcp.printer import http as phttp
from orcaslicer_mcp.printer import target as t
from orcaslicer_mcp.printer.errors import PrinterError

P = "http://192.0.2.10"
STATUS = {"presets": {"printer": "Test Printer", "print": "p", "filament": ["f"]}}
INFO = {"result": {"klippy_state": "ready"}}
OCTO = {"api": "0.1", "server": "1.10.2", "text": "OctoPrint 1.10.2"}


def profile(**cfg):
    base = {"print_host": P, "host_type": "moonraker", "printer_model": "Test Model",
            "printhost_apikey": "<redacted>"}
    base.update(cfg)
    return {"name": "Test Printer", "system": False, "config": base}


class FakeFork:
    def __init__(self, status=STATUS, preset=None, exc=None):
        self.status, self.preset, self.exc, self.calls = status, preset or profile(), exc, []

    async def __aenter__(self):
        if self.exc:
            raise self.exc
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_status(self):
        self.calls.append("status")
        return self.status

    async def get_preset_config(self, ptype, name):
        self.calls.append((ptype, name))
        return self.preset


def factory(fork):
    return lambda: fork


def never():
    raise AssertionError("the fork must not be asked")


def target(url=P, source="override", **kw):
    return t.PrinterTarget(url=url, source=source, **kw)


# respx treats a route URL without an explicit port as matching ANY port, and routes match in the
# order they were registered. So wherever a test registers both the :7125 route and the port-less
# one for the same host, the :7125 route goes first.


async def test_override_wins_and_never_asks_the_fork(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", "http://user:pw@192.0.2.20/")
    tgt = await t.resolve_target(never)
    assert (tgt.source, tgt.url, tgt.auth) == ("override", "http://192.0.2.20", ("user", "pw"))


async def test_profile_is_read_from_the_active_printer_preset():
    fork = FakeFork()
    tgt = await t.resolve_target(factory(fork))
    assert (tgt.source, tgt.url, tgt.profile, tgt.host_type) == ("profile", P, "Test Printer", "moonraker")
    assert fork.calls == ["status", ("printer", "Test Printer")]


async def test_redacted_userinfo_is_dropped():
    tgt = await t.resolve_target(factory(FakeFork(preset=profile(print_host="http://<redacted>@192.0.2.10/"))))
    assert tgt.url == P and tgt.auth is None


@pytest.mark.parametrize("preset,code", [
    (profile(print_host=""), "not_configured"),
    (profile(print_host="", printer_model="Bambu Lab X1 Carbon"), "unsupported_connection"),
    (profile(host_type="prusalink"), "unsupported_connection"),
])
async def test_profiles_that_cannot_be_used(preset, code):
    with pytest.raises(PrinterError) as e:
        await t.resolve_target(factory(FakeFork(preset=preset)))
    assert e.value.code == code


async def test_no_active_printer_profile():
    with pytest.raises(PrinterError) as e:
        await t.resolve_target(factory(FakeFork(status={"presets": {}})))
    assert e.value.code == "not_configured"


@pytest.mark.parametrize("exc", [NotReachable("down"), ConfigError("ORCA_API_TOKEN is required")])
async def test_orca_down_uses_the_remembered_printer(exc):
    t.remember(target(url=f"{P}:7125", source="profile", profile="Test Printer", kind="klipper"))
    tgt = await t.resolve_target(factory(FakeFork(exc=exc)))
    assert tgt.source == "remembered" and tgt.kind == "klipper" and tgt.url == f"{P}:7125"


async def test_orca_down_and_nothing_remembered():
    def no_token():
        raise ConfigError("ORCA_API_TOKEN is required")
    with pytest.raises(PrinterError) as e:
        await t.resolve_target(no_token)
    assert e.value.code == "orca_unreachable"


@pytest.mark.parametrize("exc,hint", [
    (ConfigError("ORCA_API_TOKEN is required (the OrcaSlicer Remote API token)"), t.TOKEN_HINT),
    (Unauthorized("unauthorized (check ORCA_API_TOKEN)"), t.TOKEN_HINT),
    (NotReachable("OrcaSlicer not reachable at http://127.0.0.1:13130"),
     "Start OrcaSlicer (MCP build) with the Remote API enabled, or " + t.SET_URL_HINT),
])
async def test_orca_unreachable_with_nothing_remembered_names_the_real_fix(exc, hint):
    with pytest.raises(PrinterError) as e:
        await t.resolve_target(factory(FakeFork(exc=exc)))
    assert e.value.code == "orca_unreachable"
    assert e.value.hint == hint
    assert e.value.details["detail"] == str(exc)


async def test_probe_moonraker_at_the_given_address():
    with respx.mock:
        respx.get(f"{P}/server/info").mock(return_value=httpx.Response(200, json=INFO))
        found, client = await t.open_printer(target(source="profile", profile="Test Printer"))
        await client.aclose()
    assert (found.kind, found.url) == ("klipper", P)
    assert t.recall_remembered().url == P  # profile-based finds are remembered


async def test_probe_falls_back_to_port_7125():
    with respx.mock:
        respx.get(f"{P}:7125/server/info").mock(return_value=httpx.Response(200, json=INFO))
        respx.get(f"{P}/server/info").mock(return_value=httpx.Response(404, text="<html>"))
        found, client = await t.open_printer(target())
        await client.aclose()
    assert (found.kind, found.url) == ("klipper", f"{P}:7125")
    assert t.recall_remembered() is None  # overrides are not remembered


async def test_probe_falls_back_to_octoprint():
    with respx.mock:
        respx.get(f"{P}:7125/server/info").mock(side_effect=httpx.ConnectError("refused"))
        respx.get(f"{P}/server/info").mock(return_value=httpx.Response(404))
        respx.get(f"{P}/api/version").mock(return_value=httpx.Response(200, json=OCTO))
        found, client = await t.open_printer(target())
        await client.aclose()
    assert found.kind == "octoprint"


async def test_nothing_answers_lists_every_attempt():
    with respx.mock:
        for url in (f"{P}:7125/server/info", f"{P}/server/info", f"{P}/api/version"):
            respx.get(url).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(PrinterError) as e:
            await t.open_printer(target())
    assert e.value.code == "not_reachable" and len(e.value.details["tried"]) == 3
    assert "ORCA_PRINTER_URL" in e.value.hint


async def test_a_locked_server_reports_the_key_problem():
    with respx.mock:
        respx.get(f"{P}:7125/server/info").mock(side_effect=httpx.ConnectError("refused"))
        respx.get(f"{P}/server/info").mock(return_value=httpx.Response(401))
        respx.get(f"{P}/api/version").mock(return_value=httpx.Response(401))
        with pytest.raises(PrinterError) as e:
            await t.open_printer(target())
    assert e.value.code == "auth_required"


async def test_the_working_protocol_is_cached():
    with respx.mock:
        alt = respx.get(f"{P}:7125/server/info").mock(return_value=httpx.Response(200, json=INFO))
        given = respx.get(f"{P}/server/info").mock(return_value=httpx.Response(404))
        for _ in range(2):
            _, client = await t.open_printer(target())
            await client.aclose()
    assert given.call_count == 1 and alt.call_count == 2


async def test_centauri_printers_get_a_protocol_hint():
    with respx.mock:
        for url in (f"{P}:7125/server/info", f"{P}/server/info", f"{P}/api/version"):
            respx.get(url).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(PrinterError) as e:
            await t.open_printer(target(printer_model="Elegoo Centauri Carbon"))
    assert "SDCP" in e.value.hint


async def test_an_mdns_name_that_does_not_resolve_fails_with_the_addresses_tried():  # Review Focus 1
    name = "http://printer-test.local"
    with respx.mock:
        for url in (f"{name}:7125/server/info", f"{name}/server/info", f"{name}/api/version"):
            respx.get(url).mock(side_effect=httpx.ConnectError("[Errno -2] Name or service not known"))
        with pytest.raises(PrinterError) as e:
            await t.open_printer(target(url=name))
    assert e.value.code == "not_reachable"
    assert all("printer-test.local" in tried for tried in e.value.details["tried"])
    assert "ORCA_PRINTER_URL" in e.value.hint


def test_connect_timeout_is_short():
    assert phttp.CONNECT_TIMEOUT_S == 3.0


# A printer address that cannot be parsed is a structured error, never an exception, and never
# echoes a password.

@pytest.mark.parametrize("bad", [
    "http://192.0.2.10:abc",      # non-numeric port
    "http://[2001:db8::1",        # unclosed IPv6 bracket
    "http://192.0.2.10:99999",    # port out of range
    "http://",                    # no host at all
])
async def test_a_malformed_override_is_not_configured(monkeypatch, bad):
    monkeypatch.setenv("ORCA_PRINTER_URL", bad)
    with pytest.raises(PrinterError) as e:
        await t.resolve_target(never)
    assert e.value.code == "not_configured"
    assert f"'{bad}'" in e.value.message and "ORCA_PRINTER_URL" in e.value.message
    assert "ORCA_PRINTER_URL" in e.value.hint


async def test_a_malformed_profile_address_is_not_configured():
    fork = FakeFork(preset=profile(print_host="http://192.0.2.10:abc"))
    with pytest.raises(PrinterError) as e:
        await t.resolve_target(factory(fork))
    assert e.value.code == "not_configured"
    assert "'http://192.0.2.10:abc'" in e.value.message and "'Test Printer'" in e.value.message
    assert "OrcaSlicer" in e.value.hint and "ORCA_PRINTER_URL" in e.value.hint


@pytest.mark.parametrize("bad", [
    "http://user:pa/ss-secret@192.0.2.10",    # unencoded '/' in the password
    "http://user:pa?ss-secret@192.0.2.10",    # unencoded '?'
    "http://user:pa#ss-secret@192.0.2.10",    # unencoded '#'
    "http://user:123?ss-secret@192.0.2.10",   # would otherwise parse as host 'user', port 123
])
async def test_an_unencoded_password_is_refused_without_echoing_it(monkeypatch, bad):
    monkeypatch.setenv("ORCA_PRINTER_URL", bad)
    with pytest.raises(PrinterError) as e:
        await t.resolve_target(never)
    assert e.value.code == "not_configured"
    text = json.dumps(e.value.as_dict())
    assert "ss-secret" not in text and "pa/ss" not in text
    assert "<redacted>@192.0.2.10" in e.value.message and "percent-encoded" in e.value.message


async def test_a_malformed_target_url_is_caught_before_any_request():
    with respx.mock:  # no routes: any request would fail the test
        for bad in ("http://192.0.2.10:99999", "http://[2001:db8::1", "http://192.0.2.10:abc"):
            with pytest.raises(PrinterError) as e:
                await t.open_printer(target(url=bad))
            assert e.value.code == "not_configured" and bad in e.value.message
