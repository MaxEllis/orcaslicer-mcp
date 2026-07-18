from orcaslicer_mcp.knowledge_index import load_knowledge, KChunk


def test_load_knowledge_returns_chunks():
    chunks = load_knowledge()
    assert len(chunks) >= 1
    c = next(k for k in chunks if k.relpath.endswith("flow-limits.md"))
    assert "flow" in c.topics
    assert isinstance(c.orca_keys, list) and c.body.strip()
    assert c.title  # first markdown heading
