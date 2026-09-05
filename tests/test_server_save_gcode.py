import httpx, respx
import orcaslicer_mcp.server as srv
from orcaslicer_mcp import outcomes as oc

B = "http://x:13130"
OBJS = {"count": 1, "objects": [{"id": 1, "index": 0, "name": "cube20", "size_mm": [20, 20, 20], "instances": 1,
                                 "transform": {"offset": [0, 0, 10], "rotation": [0, 0, 0], "scale": [1, 1, 1]}}]}
CFG = {"config": {"layer_height": "0.5", "fan_max_speed": "60", "unrelated": "x"}}


def _env(m, tmp_path):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", B)
    m.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(200, content=b"G28\nG1 X1\n"))
    respx.get(f"{B}/api/v1/objects").mock(return_value=httpx.Response(200, json=OBJS))
    respx.get(url__regex=rf"{B}/api/v1/config.*").mock(return_value=httpx.Response(200, json=CFG))


@respx.mock
async def test_save_gcode_writes_file_and_records_slice_when_store_present(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    oc.connect(create=True).close()
    out = await srv.save_gcode("cube_test.gcode")
    assert out["filename"] == "cube_test.gcode" and out["bytes"] == 10
    assert (tmp_path / "gcode" / "cube_test.gcode").read_bytes() == b"G28\nG1 X1\n"
    assert out["outcome_recorded"] is True and out["model_name"] == "cube20"
    row = oc.recall(model_name="cube20")[0]
    assert row["gcode_filename"] == "cube_test.gcode"
    assert row["settings_summary"] == {"layer_height": "0.5", "fan_max_speed": "60"}
    assert row["geometry_hash"] == oc.geometry_hash_for(OBJS["objects"])


@respx.mock
async def test_save_gcode_without_store_still_saves_but_does_not_record(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    out = await srv.save_gcode("a.gcode")
    assert out["outcome_recorded"] is False and out["outcome_row_id"] is None
    assert (tmp_path / "gcode" / "a.gcode").exists()
    assert oc.is_available() is False


@respx.mock
async def test_save_gcode_default_name_and_sanitising(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    out = await srv.save_gcode()
    assert out["filename"].startswith("cube20_") and out["filename"].endswith(".gcode")
    out2 = await srv.save_gcode("../evil name?.gcode")
    assert "/" not in out2["filename"] and ".." not in out2["filename"] and " " not in out2["filename"]


@respx.mock
async def test_save_gcode_not_sliced(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(409, json={"error": "not_sliced"}))
    assert (await srv.save_gcode("x.gcode"))["error"] == "not_sliced"


@respx.mock
async def test_save_gcode_unicode_only_name_falls_back_to_print_prefix(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    out = await srv.save_gcode("日本語.gcode")
    assert out["filename"].startswith("print_")


@respx.mock
async def test_save_gcode_degrades_when_store_write_fails(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    oc.connect(create=True).close()

    def _boom(*args, **kwargs):
        raise __import__("sqlite3").OperationalError("database is locked")

    monkeypatch.setattr(srv._outcomes, "record_slice", _boom)
    out = await srv.save_gcode("locked.gcode")
    assert (tmp_path / "gcode" / "locked.gcode").exists()
    assert out["outcome_recorded"] is False
    assert out["outcome_row_id"] is None
    assert "locked" in out["outcome_error"]


@respx.mock
async def test_save_gcode_falls_back_to_home_dotfolder_when_no_shared_store(monkeypatch, tmp_path):
    # outcomes.DEFAULT_DIR is a module-level constant fixed at import time, not re-read from
    # HOME per call - patch it directly (rather than relying on HOME alone) so this test can't
    # ever land on the real shared store regardless of import order in the test session.
    monkeypatch.delenv("PRINT_OUTCOMES_DIR", raising=False)
    fake_shared = tmp_path / "would_be_shared" / "print-outcomes"
    monkeypatch.setattr(oc, "DEFAULT_DIR", fake_shared)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ORCA_API_TOKEN", "tok"); monkeypatch.setenv("ORCA_API_URL", B)
    respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(200, content=b"G28\nG1 X1\n"))
    respx.get(f"{B}/api/v1/objects").mock(return_value=httpx.Response(200, json=OBJS))
    respx.get(url__regex=rf"{B}/api/v1/config.*").mock(return_value=httpx.Response(200, json=CFG))
    assert not fake_shared.exists()

    out = await srv.save_gcode("a.gcode")

    assert out["outcome_recorded"] is False
    assert (tmp_path / ".orcaslicer-mcp" / "gcode" / "a.gcode").exists()
    assert not fake_shared.exists()


@respx.mock
async def test_save_gcode_uses_shared_store_when_it_already_exists(monkeypatch, tmp_path):
    monkeypatch.delenv("PRINT_OUTCOMES_DIR", raising=False)
    fake_shared = tmp_path / "would_be_shared" / "print-outcomes"
    fake_shared.mkdir(parents=True)
    monkeypatch.setattr(oc, "DEFAULT_DIR", fake_shared)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ORCA_API_TOKEN", "tok"); monkeypatch.setenv("ORCA_API_URL", B)
    respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(200, content=b"G28\nG1 X1\n"))
    respx.get(f"{B}/api/v1/objects").mock(return_value=httpx.Response(200, json=OBJS))
    respx.get(url__regex=rf"{B}/api/v1/config.*").mock(return_value=httpx.Response(200, json=CFG))

    out = await srv.save_gcode("a.gcode")

    assert (fake_shared / "gcode" / "a.gcode").exists()
    assert not (tmp_path / ".orcaslicer-mcp").exists()


@respx.mock
async def test_save_gcode_never_overwrites_existing_file(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    out1 = await srv.save_gcode("dup.gcode")
    assert out1["filename"] == "dup.gcode"

    respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(200, content=b"G28\nG1 X2\n"))
    out2 = await srv.save_gcode("dup.gcode")
    assert out2["filename"] == "dup-2.gcode"

    gdir = tmp_path / "gcode"
    assert (gdir / "dup.gcode").read_bytes() == b"G28\nG1 X1\n"
    assert (gdir / "dup-2.gcode").read_bytes() == b"G28\nG1 X2\n"
