"""Stdio transport that keeps handshake-era clients connected.

The MCP SDK (mcp >= 2.2) serves stdio in two protocol eras and lets the
client's first request decide, once, for the whole connection. Some clients
(Claude Desktop among them, as of late 2026) open with a 2026-07-28
``server/discover`` probe and then send the legacy ``initialize`` handshake on
the same connection. The probe has already locked the connection into the
modern era, so the SDK refuses the handshake with -32022 ("connection is
serving the 2026-07-28 protocol; the initialize handshake is not accepted")
and the client never sees any tools.

In ``legacy`` mode (the default) this module answers ``server/discover`` probes
that arrive before the first other request with METHOD_NOT_FOUND (-32601), the
reply the specification tells clients to treat as "fall back to initialize".
The probe never reaches the SDK, so ``initialize`` becomes the connection's
first request and opens a handshake-era session. Clients that only speak the
handshake are unaffected; clients in auto mode fall back as designed.

Set ``ORCA_MCP_PROTOCOL=auto`` to hand the connection to the SDK untouched.
"""
from __future__ import annotations

import os

import anyio
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp_types.jsonrpc import ErrorData, JSONRPCError, JSONRPCRequest

PROTOCOL_ENV = "ORCA_MCP_PROTOCOL"
PROBE_METHOD = "server/discover"
METHOD_NOT_FOUND = -32601
PROBE_REFUSAL = (
    "server/discover is not served on this stdio connection; use the initialize handshake "
    f"(set {PROTOCOL_ENV}=auto to serve the 2026-07-28 protocol)"
)


def protocol_mode() -> str:
    """'legacy' (default) or 'auto'. Unknown values fall back to 'legacy'."""
    value = os.environ.get(PROTOCOL_ENV, "legacy").strip().lower()
    return value if value in ("legacy", "auto") else "legacy"


def is_era_probe(item: object) -> bool:
    return (
        isinstance(item, SessionMessage)
        and isinstance(item.message, JSONRPCRequest)
        and item.message.method == PROBE_METHOD
    )


def probe_refusal(request: JSONRPCRequest) -> SessionMessage:
    return SessionMessage(
        JSONRPCError(
            jsonrpc="2.0",
            id=request.id,
            error=ErrorData(code=METHOD_NOT_FOUND, message=PROBE_REFUSAL),
        )
    )


async def refuse_probes_until_first_request(read_stream, forward_send, write_stream) -> None:
    """Relay `read_stream` to `forward_send`, answering era probes that come before the
    first other request with METHOD_NOT_FOUND. Everything after that request passes through."""
    decided = False
    async with forward_send:
        try:
            async for item in read_stream:
                if not decided and is_era_probe(item):
                    await write_stream.send(probe_refusal(item.message))
                    continue
                if isinstance(item, SessionMessage) and isinstance(item.message, JSONRPCRequest):
                    decided = True
                await forward_send.send(item)
        except anyio.ClosedResourceError:
            pass


async def run_stdio(lowlevel_server) -> None:
    """Serve `lowlevel_server` over stdio, keeping handshake-era clients connected."""
    async with stdio_server() as (read_stream, write_stream):
        options = lowlevel_server.create_initialization_options()
        if protocol_mode() == "auto":
            await lowlevel_server.run(read_stream, write_stream, options)
            return
        forward_send, forward_receive = anyio.create_memory_object_stream(0)
        async with anyio.create_task_group() as tg:
            tg.start_soon(refuse_probes_until_first_request, read_stream, forward_send, write_stream)
            await lowlevel_server.run(forward_receive, write_stream, options)
            tg.cancel_scope.cancel()
