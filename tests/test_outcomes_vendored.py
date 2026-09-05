import hashlib
from pathlib import Path
from orcaslicer_mcp import outcomes as oc

KLIPPER_COPY = Path("/home/max/projects/klipper-mcp/src/klipper_mcp/outcomes.py")


def _body(p: Path) -> str:
    # drop the one-line vendored header so the two files compare byte-equal
    lines = p.read_text().splitlines()
    return "\n".join(l for l in lines if not l.startswith("# VENDORED"))


def test_vendored_copy_matches_owner_when_owner_present():
    if not KLIPPER_COPY.exists():
        return  # dev box without the sibling repo: nothing to compare against
    assert _body(Path(oc.__file__)) == _body(KLIPPER_COPY), "outcomes.py drifted from klipper-mcp; re-copy it"


def test_absent_store_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path / "nope"))
    assert oc.is_available() is False
    assert oc.recall(model_name="cube") == []


def test_record_slice_then_recall(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    rid = oc.record_slice("cube_x.gcode", "cube20", "h1", {"layer_height": "0.5", "junk": "1"})
    rows = oc.recall(model_name="cube")
    assert rows[0]["id"] == rid and rows[0]["result"] is None
    assert rows[0]["settings_summary"] == {"layer_height": "0.5"}
