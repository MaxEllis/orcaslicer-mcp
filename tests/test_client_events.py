import json
from orcaslicer_mcp.config import Config
from orcaslicer_mcp.client import OrcaClient

CFG = Config(base_url="http://host:13130", token="tok", timeout=5)


def test_ws_url_derivation():
    c = OrcaClient(Config(base_url="http://host:13130", token="tok", timeout=5))
    assert c._ws_url() == "ws://host:13130/api/v1/events?token=tok"
    c2 = OrcaClient(Config(base_url="https://host:13130", token="t", timeout=5))
    assert c2._ws_url() == "wss://host:13130/api/v1/events?token=t"


class _FakeWS:
    """Minimal stand-in for a websockets connection: async context manager whose
    recv() yields queued frames then raises to signal the socket closed."""

    def __init__(self, frames):
        self._frames = list(frames)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def recv(self):
        if not self._frames:
            raise ConnectionError("closed")
        return self._frames.pop(0)


def _patch_ws(monkeypatch, frames):
    monkeypatch.setattr(
        "orcaslicer_mcp.client.websockets.connect",
        lambda url, *a, **k: _FakeWS(frames),
    )


async def test_collect_events_stop_on_short_circuits(monkeypatch):
    frames = [
        json.dumps({"event": "slice.progress", "percent": 10}),
        json.dumps({"event": "slice.done", "percent": 100}),
        json.dumps({"event": "slice.after", "percent": 100}),  # should never be read
    ]
    _patch_ws(monkeypatch, frames)
    async with OrcaClient(CFG) as c:
        events = await c.collect_events(seconds=5, stop_on={"slice.done"})
    assert [e["event"] for e in events] == ["slice.progress", "slice.done"]


async def test_collect_events_skips_non_dict_frame(monkeypatch):
    frames = ["[1,2,3]", json.dumps({"event": "slice.done"})]
    _patch_ws(monkeypatch, frames)
    async with OrcaClient(CFG) as c:
        events = await c.collect_events(seconds=5, stop_on={"slice.done"})
    assert events == [{"event": "slice.done"}]


async def test_collect_events_skips_malformed_frame(monkeypatch):
    frames = ["not json{", json.dumps({"event": "slice.done"})]
    _patch_ws(monkeypatch, frames)
    async with OrcaClient(CFG) as c:
        events = await c.collect_events(seconds=5, stop_on={"slice.done"})
    assert events == [{"event": "slice.done"}]
