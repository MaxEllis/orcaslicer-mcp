"""The stdio transport keeps handshake-era clients connected when they probe first.

Real subprocess over real stdio, because the bug lives in the transport: the SDK lets
the first request lock the connection's protocol era, so a client that probes with
server/discover and then sends initialize was refused with -32022.
"""
import json
import os
import subprocess
import sys

import pytest

from orcaslicer_mcp import stdio_compat

MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}
DISCOVER = {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": MODERN_META}}
INITIALIZE = {
    "jsonrpc": "2.0", "id": 2, "method": "initialize",
    "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}


class StdioServer:
    def __init__(self, **env):
        full_env = dict(os.environ, ORCA_API_TOKEN="test-token")
        full_env.pop(stdio_compat.PROTOCOL_ENV, None)
        full_env.update(env)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "orcaslicer_mcp.server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=full_env, text=True,
        )

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        if "id" in message:
            line = self.proc.stdout.readline()
            assert line, "server closed the connection"
            return json.loads(line)
        return None

    def close(self):
        self.proc.kill()
        self.proc.wait(timeout=10)


@pytest.fixture
def server():
    s = StdioServer()
    yield s
    s.close()


def test_probe_then_initialize_opens_a_handshake_session(server):
    probe = server.send(DISCOVER)
    assert probe["error"]["code"] == stdio_compat.METHOD_NOT_FOUND

    init = server.send(INITIALIZE)
    assert "error" not in init, init
    assert init["result"]["protocolVersion"] == "2025-11-25"

    server.send(INITIALIZED)
    tools = server.send(TOOLS_LIST)["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"check_profile_physics", "remember", "slice"} <= names


def test_initialize_alone_still_works(server):
    init = server.send(INITIALIZE)
    assert init["result"]["protocolVersion"] == "2025-11-25"


def test_probe_after_handshake_is_left_to_the_sdk(server):
    server.send(INITIALIZE)
    server.send(INITIALIZED)
    late = server.send({**DISCOVER, "id": 9})
    # Not our refusal text: once the era is decided, requests pass through untouched.
    assert stdio_compat.PROBE_REFUSAL not in json.dumps(late)


def test_auto_mode_keeps_the_sdk_behaviour():
    s = StdioServer(**{stdio_compat.PROTOCOL_ENV: "auto"})
    try:
        assert "result" in s.send(DISCOVER)
        refused = s.send(INITIALIZE)
        assert refused["error"]["code"] == -32022
    finally:
        s.close()


@pytest.mark.parametrize("value, expected", [
    (None, "legacy"), ("legacy", "legacy"), ("AUTO", "auto"), (" auto ", "auto"), ("bogus", "legacy"),
])
def test_protocol_mode(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(stdio_compat.PROTOCOL_ENV, raising=False)
    else:
        monkeypatch.setenv(stdio_compat.PROTOCOL_ENV, value)
    assert stdio_compat.protocol_mode() == expected
