import pytest

from orcaslicer_mcp.knowledge_index import load_knowledge, KChunk, _parse, search_knowledge


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


def test_search_ranks_topic_match_first():
    hits = search_knowledge("volumetric flow ceiling")
    assert hits and hits[0].relpath.endswith("flow-limits.md")


def test_search_empty_query_returns_nothing():
    assert search_knowledge("") == []


@pytest.mark.parametrize("query,expect", [
    ("stringing on travel moves", "failures/stringing.md"),
    ("corners lifting off the bed", "failures/warping.md"),
    ("layers splitting when part is stressed", "failures/layer-splitting.md"),
])
def test_failure_retrieval_goldens(query, expect):
    assert any(c.relpath == expect for c in search_knowledge(query)), query


@pytest.mark.parametrize("query,expect", [
    ("part must survive vibration and bolts", "considerations/strength.md"),
    ("what should I ask the user first", "interview.md"),
    ("printing miniatures with fine detail", "considerations/small-features.md"),
])
def test_consideration_retrieval_goldens(query, expect):
    assert any(c.relpath == expect for c in search_knowledge(query)), query
