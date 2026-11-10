"""The AI app's name from the MCP handshake goes to OrcaSlicer, so its Config tab can say which app connected."""
import types

import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp import client as client_mod

B = "http://x:13130"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", B)
    monkeypatch.setattr(client_mod, "CLIENT_NAME", [None])


def ctx(name, title=None):
    info = types.SimpleNamespace(name=name, title=title)
    return types.SimpleNamespace(session=types.SimpleNamespace(client_params=types.SimpleNamespace(client_info=info)))


def test_known_apps_get_the_name_people_know():
    assert srv._client_label(ctx("claude-code")) == "Claude Code"
    assert srv._client_label(ctx("claude-ai")) == "Claude"
    assert srv._client_label(ctx("codex-mcp-client")) == "Codex"
    assert srv._client_label(ctx("Visual Studio Code")) == "VS Code"


def test_unknown_apps_keep_their_own_name_made_header_safe():
    assert srv._client_label(ctx("my-agent", title="My Agent")) == "My Agent"
    assert srv._client_label(ctx("café\nbot")) == "cafbot"
    assert srv._client_label(ctx("x" * 200)) == "x" * 60
    assert srv._client_label(None) is None
    assert srv._client_label(types.SimpleNamespace(session=types.SimpleNamespace(client_params=None))) is None


@respx.mock
async def test_the_name_rides_along_with_requests_after_a_tool_call():
    route = respx.get(f"{B}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {}}))
    await srv._call_tool_normalized("get_config", {"keys": ["layer_height"]}, ctx("claude-code"))
    assert route.calls[-1].request.headers.get("X-OrcaSlicer-MCP-Client") == "Claude Code"
