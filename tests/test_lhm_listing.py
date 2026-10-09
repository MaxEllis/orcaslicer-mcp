"""lhm.plugin.json lists every tool the live server registers, with text taken from the tool itself.
Regenerate it with `.venv/bin/python scripts/gen_lhm_tools.py` after changing a tool's name or docstring."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LISTING = ROOT / "lhm.plugin.json"
REGEN = "lhm.plugin.json is stale: run .venv/bin/python scripts/gen_lhm_tools.py"


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
    gen = _gen()
    doc = "Set a band (e.g. 0 to 5 mm) at 0.1 mm. Passing the same band again updates it."
    assert gen.summarize(doc, limit=500) == doc


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
        assert " - " not in d and "—" not in d and "–" not in d, f"{t['name']}: dash in {d!r}"
