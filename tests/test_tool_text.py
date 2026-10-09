"""Tool and module text that the README and the listings repeat: the wording has to stay true."""
import anyio

import orcaslicer_mcp.server as srv
from orcaslicer_mcp.printer import history


def _flat(text: str) -> str:
    return " ".join(text.split())  # a docstring wraps lines mid-phrase


def _tools():
    return {t.name: t for t in anyio.run(srv.mcp.list_tools)}


def test_the_until_text_explains_what_printing_means_on_octoprint():
    until = _flat(_tools()["wait_for_printer"].input_schema["properties"]["until"]["description"])
    assert "OctoPrint reports no extrusion" in until
    assert "running with the heaters at temperature" in until
    assert "reports none" not in until  # the old, awkward parenthesis


def test_estimate_wording_names_no_slicer_where_the_estimate_may_come_from_a_file():
    # list_print_history and recall_prints also show estimates read from a G-code file's own
    # metadata, which any slicer wrote, so they say "the slicer's".
    tools = _tools()
    for name in ("list_print_history", "recall_prints"):
        text = _flat(tools[name].description)
        assert "OrcaSlicer's estimate" not in text, name
        assert "OrcaSlicer's time estimates" not in text, name
        assert "the slicer's estimate" in text, name
    doc = _flat(history.__doc__)
    assert "OrcaSlicer's estimate" not in doc
    assert "the slicer's estimate" in doc


def test_save_gcode_says_it_keeps_a_fingerprint_not_the_geometry():
    text = _flat(_tools()["save_gcode"].description)
    assert "fingerprint" in text
    assert "geometry," not in text
