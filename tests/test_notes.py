from orcaslicer_mcp import notes


def test_append_and_search(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCA_MCP_NOTES_DIR", str(tmp_path))
    notes.append_note("X2 0.8 PLA: 215C cured weak layers", "machine:X2/PLA")
    hits = notes.search_notes("weak layers")
    assert hits == ["machine:X2/PLA: X2 0.8 PLA: 215C cured weak layers"]


def test_scope_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCA_MCP_NOTES_DIR", str(tmp_path))
    p = notes.append_note("n", "machine:../evil")
    assert ".." not in p.name
