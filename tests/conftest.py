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


@pytest.fixture(autouse=True)
def _isolate_outcome_store(monkeypatch, tmp_path_factory):
    """No test may touch a real outcome store: both fallback locations point at temp dirs.
    Tests that want a store set PRINT_OUTCOMES_DIR themselves."""
    from orcaslicer_mcp import outcomes
    base = tmp_path_factory.mktemp("outcomes")
    monkeypatch.setattr(outcomes, "LEGACY_DIR", base / "legacy-absent")
    monkeypatch.setattr(outcomes, "DEFAULT_DIR", base / "default")
    monkeypatch.delenv("PRINT_OUTCOMES_DIR", raising=False)
