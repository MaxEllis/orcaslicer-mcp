from orcaslicer_mcp.knowledge_index import load_knowledge, KChunk, _parse


def test_load_knowledge_returns_chunks():
    chunks = load_knowledge()
    assert len(chunks) >= 1
    c = next(k for k in chunks if k.relpath.endswith("flow-limits.md"))
    assert "flow" in c.topics
    assert isinstance(c.orca_keys, tuple) and c.body.strip()
    assert c.title  # first markdown heading


def test_load_knowledge_returns_fresh_list_over_shared_chunks():
    a, b = load_knowledge(), load_knowledge()
    assert a is not b          # caller can't corrupt the cache via the list
    assert a[0] is b[0]        # chunks themselves are cached, not re-parsed


def test_parse_without_frontmatter_yields_tuples():
    c = _parse("x.md", "# No Frontmatter\n\nbody")
    assert isinstance(c.topics, tuple) and isinstance(c.orca_keys, tuple) and c.topics == ()
