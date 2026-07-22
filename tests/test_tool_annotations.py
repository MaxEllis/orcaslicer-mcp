# tests/test_tool_annotations.py
import anyio

import orcaslicer_mcp.server as srv


def test_annotation_table_matches_registered_tools_exactly():
    registered = set(srv.mcp._tool_manager._tools)
    declared = set(srv._TOOL_ANNOTATIONS)
    assert registered == declared


def test_every_tool_has_title_and_hints():
    for name, tool in srv.mcp._tool_manager._tools.items():
        ann = tool.annotations
        assert ann is not None, name
        assert ann.title, name
        assert ann.readOnlyHint in (True, False), name
        if ann.readOnlyHint:
            assert ann.destructiveHint is None, name
        else:
            assert ann.destructiveHint in (True, False), name


def test_annotations_surface_in_list_tools():
    tools = anyio.run(srv.mcp.list_tools)
    by_name = {t.name: t for t in tools}
    assert by_name["get_status"].annotations.readOnlyHint is True
    assert by_name["delete_preset"].annotations.readOnlyHint is False
    assert by_name["delete_preset"].annotations.destructiveHint is True
    assert by_name["load_model"].annotations.destructiveHint is False
    assert all(t.annotations and t.annotations.title for t in tools)
