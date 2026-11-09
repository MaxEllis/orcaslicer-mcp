"""orcaslicer-mcp's own HTTP client, unchanged, against the plugin (fake host)."""
import asyncio
import pathlib
import sys

import pytest

from conftest import MCP_SRC

if not (MCP_SRC / "orcaslicer_mcp" / "client.py").exists():
    pytest.skip("orcaslicer-mcp source not found", allow_module_level=True)

from orcaslicer_mcp.client import OrcaClient  # noqa: E402
from orcaslicer_mcp.config import Config  # noqa: E402
from orcaslicer_mcp.errors import NotFound, Validation  # noqa: E402
from orcaslicer_mcp.guard import BlockedKey  # noqa: E402


def run(plugin, coro_fn):
    async def go():
        cfg = Config(base_url=plugin.base, token=plugin._token, timeout=15.0)
        async with OrcaClient(cfg) as c:
            return await coro_fn(c)
    return asyncio.run(go())


def test_status_and_presets(plugin):
    st = run(plugin, lambda c: c.get_status())
    assert st["presets"]["filament"] == ["Fake PLA"]
    assert st["project"] == {"name": "Untitled", "path": "", "dirty": False}
    presets = run(plugin, lambda c: c.get_presets())
    assert "print" in presets


def test_config_roundtrip(plugin):
    async def go(c):
        await c.put_config({"layer_height": 0.24})
        return await c.get_config(["layer_height"])
    assert run(plugin, go) == {"layer_height": "0.24"}


def test_client_guard_blocks_before_the_plugin_does(plugin):
    with pytest.raises(BlockedKey):
        run(plugin, lambda c: c.put_config({"post_process": "x"}))


def test_validation_error_surfaces(plugin):
    with pytest.raises(Validation):
        run(plugin, lambda c: c.put_config({"no_such_key": 1}))


def test_objects_rotation_converted(plugin):
    objs = run(plugin, lambda c: c.get_objects())
    assert objs["objects"][0]["transform"]["rotation"] == [0.0, 0.0, 0.0]


def test_preset_config_and_select(plugin):
    cfg = run(plugin, lambda c: c.get_preset_config("printer", "Fake Printer 0.4"))
    assert "<redacted>" in str(cfg)
    run(plugin, lambda c: c.select_preset("print", "My Fast"))
    assert run(plugin, lambda c: c.get_status())["presets"]["print"] == "My Fast"


def test_fork_only_route_is_route_missing(plugin):
    with pytest.raises(NotFound) as e:
        run(plugin, lambda c: c.slice())
    assert e.value.route_missing
