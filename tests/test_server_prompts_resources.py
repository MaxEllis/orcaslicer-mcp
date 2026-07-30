"""LobeHub/directory-visible surface: prompts and resources must exist and read."""
import anyio
import pytest

import orcaslicer_mcp.server as srv
from orcaslicer_mcp.knowledge_index import load_knowledge


def test_prompts_registered_and_render():
    prompts = anyio.run(srv.mcp.list_prompts)
    names = {p.name for p in prompts}
    assert {"slice-a-model", "optimize-print-time", "edit-preset-safely"} <= names
    msgs = anyio.run(lambda: srv.mcp.get_prompt("slice-a-model", {"model_path": "/tmp/cube.stl"}))
    text = msgs.messages[0].content.text
    assert "/tmp/cube.stl" in text and "check_placement" in text
    msgs = anyio.run(lambda: srv.mcp.get_prompt("edit-preset-safely", {"change": "z_hop to 0.6"}))
    assert "check_profile_physics" in msgs.messages[0].content.text


def test_optimize_prompt_optional_constraints():
    plain = anyio.run(lambda: srv.mcp.get_prompt("optimize-print-time", {}))
    assert "get_slice_breakdown" in plain.messages[0].content.text
    con = anyio.run(lambda: srv.mcp.get_prompt("optimize-print-time", {"constraints": "keep walls strong"}))
    assert "keep walls strong" in con.messages[0].content.text


def _read(uri: str) -> str:
    out = anyio.run(lambda: srv.mcp.read_resource(uri))
    return list(out)[0].content


def test_knowledge_index_resource_lists_every_chunk():
    listed = anyio.run(srv.mcp.list_resources)
    assert any(str(r.uri) == "orca://knowledge" for r in listed)
    body = _read("orca://knowledge")
    chunks = load_knowledge()
    assert chunks, "knowledge base should not be empty"
    for c in chunks:
        assert c.title in body
    assert "orca://knowledge/" in body


def test_knowledge_chunk_resource_roundtrip():
    c = load_knowledge()[0]
    rel = c.relpath[:-3] if c.relpath.endswith(".md") else c.relpath
    body = _read(f"orca://knowledge/{rel.replace('/', '__')}")
    assert body == c.body


def test_setting_resource_real_and_unknown():
    body = _read("orca://setting/layer_height")
    assert '"layer_height"' in body or "layer_height" in body
    with pytest.raises(Exception):
        _read("orca://setting/definitely_not_a_setting")
