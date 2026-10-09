"""lhm.plugin.json lists every tool the live server registers, with text taken from the tool itself.
Regenerate it with `.venv/bin/python scripts/gen_lhm_tools.py` after changing a tool's name or docstring."""
import importlib.util
import json
import re
from pathlib import Path

import anyio

import orcaslicer_mcp.server as srv

ROOT = Path(__file__).resolve().parents[1]
LISTING = ROOT / "lhm.plugin.json"
REGEN = "lhm.plugin.json is stale: run .venv/bin/python scripts/gen_lhm_tools.py"
ABBREVIATION_END = re.compile(r"\b(?:e\.g|i\.e|etc|vs)\.$", re.IGNORECASE)  # a summary that stops here was cut


def _gen():
    spec = importlib.util.spec_from_file_location("gen_lhm_tools", ROOT / "scripts" / "gen_lhm_tools.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_summary_is_the_first_paragraph_on_one_line():
    gen = _gen()
    doc = "Do the thing.\nIt wraps across\nlines.\n\nSecond paragraph, never listed."
    assert gen.summarize(doc, limit=500) == "Do the thing. It wraps across lines."


def test_summary_stops_at_a_sentence_end_under_the_limit():
    gen = _gen()
    doc = "First sentence is here. Second sentence follows it. Third one goes past the limit for sure."
    assert gen.summarize(doc, limit=55) == "First sentence is here. Second sentence follows it."
    assert gen.summarize(doc, limit=30) == "First sentence is here."


def test_summary_keeps_a_long_first_sentence_whole():
    gen = _gen()
    doc = "A single sentence that is much longer than the limit but must never be cut in two. Next."
    assert gen.summarize(doc, limit=20) == "A single sentence that is much longer than the limit but must never be cut in two."


def test_summary_does_not_split_after_an_abbreviation():
    # the limit is small enough that a split after "e.g." would be the cut
    gen = _gen()
    band = "Set a band (e.g. 0 to 5 mm) at 0.1 mm. Passing the same band again updates it."
    assert gen.summarize(band, limit=20) == "Set a band (e.g. 0 to 5 mm) at 0.1 mm."
    quoted = "Show the verdict (e.g. 'warped') you gave it. More text follows here."
    assert gen.summarize(quoted, limit=20) == "Show the verdict (e.g. 'warped') you gave it."
    assert gen.summarize("Reset (i.e. 0 or none) all layers. Then stop.", limit=10) == "Reset (i.e. 0 or none) all layers."
    assert gen.summarize("Fits (vs. 2 others) well. Then stop.", limit=10) == "Fits (vs. 2 others) well."
    assert gen.summarize("Keeps a, b, etc. 3 of them stay. Then stop.", limit=10) == "Keeps a, b, etc. 3 of them stay."
    assert gen.summarize("Use E.g. 0 to 5 mm here. Then stop.", limit=10) == "Use E.g. 0 to 5 mm here."


def test_real_tool_text_is_not_cut_at_an_abbreviation():
    gen = _gen()
    tools = {t.name: t for t in anyio.run(srv.mcp.list_tools)}
    for name in ("set_height_range", "recall_prints", "cancel_slice"):
        out = gen.summarize(tools[name].description, limit=70)
        assert not ABBREVIATION_END.search(out), f"{name}: {out!r}"


def test_listing_matches_the_live_server():
    gen = _gen()
    listed = json.loads(LISTING.read_text(encoding="utf-8"))["tools"]
    live = gen.listing_tools()
    assert [t["name"] for t in listed] == [t["name"] for t in live], REGEN
    assert listed == live, REGEN


def test_listing_text_ends_each_summary_and_has_no_dashes():
    for t in json.loads(LISTING.read_text(encoding="utf-8"))["tools"]:
        d = t["description"]
        assert d and d[-1] in ".!?", f"{t['name']}: summary does not end a sentence: {d!r}"
        assert not ABBREVIATION_END.search(d), f"{t['name']}: summary was cut at an abbreviation: {d!r}"
        assert " - " not in d and "—" not in d and "–" not in d, f"{t['name']}: dash in {d!r}"
