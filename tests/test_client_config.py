import httpx, respx
from orcaslicer_mcp.client import OrcaClient
from orcaslicer_mcp.config import Config


FULL = {"config": {"printable_area": "0x0,300x0,300x300,0x300", "brim_width": "10",
                   "skirt_loops": "2", "unrelated": "x"}}


def _cfg():
    return Config(base_url="http://x:13130", token="t", timeout=5)


@respx.mock
async def test_get_config_filters_locally_without_comma_param():
    # The fork's /config splits the raw query on ',' without URL-decoding, so httpx's
    # %2C-encoded comma returns {}. The client must NOT depend on that: fetch-all + filter.
    route = respx.get(url__regex=r"http://x:13130/api/v1/config.*").mock(
        return_value=httpx.Response(200, json=FULL))
    async with OrcaClient(_cfg()) as c:
        out = await c.get_config(["printable_area", "brim_width"])
    assert out == {"printable_area": "0x0,300x0,300x300,0x300", "brim_width": "10"}
    # regression guard: must never send the encoded-comma keys param the fork can't parse
    sent = str(route.calls.last.request.url)
    assert "%2C" not in sent
    assert "keys=" not in sent


@respx.mock
async def test_get_config_none_returns_all():
    respx.get(url__regex=r"http://x:13130/api/v1/config.*").mock(
        return_value=httpx.Response(200, json=FULL))
    async with OrcaClient(_cfg()) as c:
        out = await c.get_config(None)
    assert out == FULL["config"]


@respx.mock
async def test_get_config_missing_keys_omitted():
    respx.get(url__regex=r"http://x:13130/api/v1/config.*").mock(
        return_value=httpx.Response(200, json=FULL))
    async with OrcaClient(_cfg()) as c:
        out = await c.get_config(["printable_area", "does_not_exist"])
    assert out == {"printable_area": "0x0,300x0,300x300,0x300"}
