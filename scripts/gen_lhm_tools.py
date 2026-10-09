#!/usr/bin/env python3
"""Rewrite the `tools` array of lhm.plugin.json from the tools the live server registers.

    .venv/bin/python scripts/gen_lhm_tools.py           # rewrite lhm.plugin.json
    .venv/bin/python scripts/gen_lhm_tools.py --check   # exit 1 if it is stale (what the test does)

Names and order come from the server's own tool list. Each description is the first paragraph of the
tool's docstring on one line, cut after the last whole sentence that keeps it under MAX_CHARS (the
first sentence always stays, however long). Every other field of the listing is left as it is, so
this never touches the version.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

LISTING = Path(__file__).resolve().parents[1] / "lhm.plugin.json"
MAX_CHARS = 300
# A sentence ends at . ! or ? followed by a capital, digit, quote or bracket, so "e.g. 0 to 5 mm" and
# "Z 0.4" stay in one piece.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`'\"(])")


def summarize(description: str, limit: int = MAX_CHARS) -> str:
    text = " ".join(description.strip().split("\n\n")[0].split())
    sentences = _SENTENCE_END.split(text)
    out = sentences[0]
    for sentence in sentences[1:]:
        if len(out) + 1 + len(sentence) > limit:
            break
        out += " " + sentence
    return out


def listing_tools() -> list[dict]:
    from orcaslicer_mcp import server

    tools = asyncio.run(server.mcp.list_tools())
    return [{"name": t.name, "description": summarize(t.description or "")} for t in tools]


def render(listing: dict) -> str:
    return json.dumps(listing, indent=2) + "\n"


def main(argv: list[str]) -> int:
    listing = json.loads(LISTING.read_text(encoding="utf-8"))
    listing["tools"] = listing_tools()
    new = render(listing)
    if "--check" in argv:
        if LISTING.read_text(encoding="utf-8") != new:
            print("lhm.plugin.json is stale: run scripts/gen_lhm_tools.py", file=sys.stderr)
            return 1
        return 0
    LISTING.write_text(new, encoding="utf-8")
    print(f"wrote {len(listing['tools'])} tools to {LISTING.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
