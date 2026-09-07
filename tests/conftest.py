import lzma
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def gcode_fixture():
    """Return the decompressed text of tests/fixtures/<name>.gcode.xz (cached per session)."""
    cache: dict[str, str] = {}

    def load(name: str) -> str:
        if name not in cache:
            with lzma.open(FIXTURES / f"{name}.gcode.xz", "rt", encoding="utf-8", errors="replace") as fh:
                cache[name] = fh.read()
        return cache[name]
    return load
