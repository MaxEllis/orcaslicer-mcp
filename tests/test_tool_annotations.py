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
        assert ann.read_only_hint in (True, False), name
        if ann.read_only_hint:
            assert ann.destructive_hint is None, name
        else:
            assert ann.destructive_hint in (True, False), name


def test_annotations_surface_in_list_tools():
    tools = anyio.run(srv.mcp.list_tools)
    by_name = {t.name: t for t in tools}
    assert by_name["get_status"].annotations.read_only_hint is True
    assert by_name["delete_preset"].annotations.read_only_hint is False
    assert by_name["delete_preset"].annotations.destructive_hint is True
    assert by_name["load_model"].annotations.destructive_hint is False
    assert all(t.annotations and t.annotations.title for t in tools)


def test_printer_tools_are_read_only_and_open_world():
    ann = srv.mcp._tool_manager._tools["get_printer_status"].annotations
    assert ann.read_only_hint is True and ann.open_world_hint is True


def test_wait_for_printer_is_read_only_and_open_world():
    ann = srv.mcp._tool_manager._tools["wait_for_printer"].annotations
    assert ann.read_only_hint is True and ann.open_world_hint is True


def test_list_print_history_writes_locally_but_is_safe_to_repeat():
    ann = srv.mcp._tool_manager._tools["list_print_history"].annotations
    assert ann.read_only_hint is False and ann.destructive_hint is False
    assert ann.idempotent_hint is True and ann.open_world_hint is True


def test_check_printer_match_is_read_only_and_open_world():
    tool = srv.mcp._tool_manager._tools["check_printer_match"]
    ann = tool.annotations
    assert tool.title == "Check the profile against the printer"
    assert ann.title == "Check the profile against the printer"
    assert ann.read_only_hint is True and ann.destructive_hint is None
    assert ann.idempotent_hint is None and ann.open_world_hint is True
