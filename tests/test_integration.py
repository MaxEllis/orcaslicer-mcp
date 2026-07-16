# tests/test_integration.py
import os
import pytest
import orcaslicer_mcp.server as srv

pytestmark = pytest.mark.skipif(
    not os.environ.get("ORCA_API_TOKEN") or not os.environ.get("ORCA_API_URL"),
    reason="live OrcaSlicer API env not set")


async def test_status_and_apply_slice():
    st = await srv.get_status()
    assert "app" in st
    out = await srv.apply_and_slice({"layer_height": 0.2})
    assert "result" in out and out["result"]["state"] in {"done", "slicing", "idle", "error"}
