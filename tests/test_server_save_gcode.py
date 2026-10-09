import httpx, respx
import orcaslicer_mcp.server as srv
from orcaslicer_mcp import outcomes as oc

B = "http://x:13130"
OBJS = {"count": 1, "objects": [{"id": 1, "index": 0, "name": "cube20", "size_mm": [20, 20, 20], "instances": 1,
                                 "transform": {"offset": [0, 0, 10], "rotation": [0, 0, 0], "scale": [1, 1, 1]}}]}
CFG = {"config": {"layer_height": "0.5", "fan_max_speed": "60", "unrelated": "x"}}
STATUS = {"presets": {"printer": "Test Printer", "print": "p", "filaments": ["f"]}}
SLICE = {"state": "done", "percent": 100, "stats": {"estimated_time_seconds": 1234.0, "filament_used_g": 5.6}}


def _routes(slice_resp=None):
    respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(200, content=b"G28\nG1 X1\n"))
    respx.get(f"{B}/api/v1/objects").mock(return_value=httpx.Response(200, json=OBJS))
    respx.get(url__regex=rf"{B}/api/v1/config.*").mock(return_value=httpx.Response(200, json=CFG))
    respx.get(f"{B}/api/v1/slice/status").mock(return_value=slice_resp or httpx.Response(200, json=SLICE))
    respx.get(f"{B}/api/v1/status").mock(return_value=httpx.Response(200, json=STATUS))


def _env(m, tmp_path, slice_resp=None):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", B)
    m.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    _routes(slice_resp)


@respx.mock
async def test_save_gcode_writes_file_and_records_slice(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    out = await srv.save_gcode("cube_test.gcode")
    assert out["filename"] == "cube_test.gcode" and out["bytes"] == 10
    assert (tmp_path / "gcode" / "cube_test.gcode").read_bytes() == b"G28\nG1 X1\n"
    assert out["outcome_recorded"] is True and out["model_name"] == "cube20"
    row = oc.recall(model_name="cube20")[0]
    assert row["gcode_filename"] == "cube_test.gcode"
    assert row["settings_summary"] == {"layer_height": "0.5", "fan_max_speed": "60"}
    assert row["geometry_hash"] == oc.geometry_hash_for(OBJS["objects"])


@respx.mock
async def test_save_gcode_creates_the_store_and_records_estimates(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    assert oc.is_available() is False
    out = await srv.save_gcode("a.gcode")
    assert (tmp_path / "gcode" / "a.gcode").exists()
    assert out["outcome_recorded"] is True and oc.is_available() is True
    row = oc.get(out["outcome_row_id"])
    assert row["est_time_s"] == 1234.0 and row["est_filament_g"] == 5.6
    assert row["printer_id"] == "Test Printer"


@respx.mock
async def test_save_gcode_uses_orca_printer_id(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("ORCA_PRINTER_ID", "bench")
    out = await srv.save_gcode("b.gcode")
    assert oc.get(out["outcome_row_id"])["printer_id"] == "bench"


@respx.mock
async def test_save_gcode_names_the_printer_by_its_host_when_orca_printer_url_is_set(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("ORCA_PRINTER_URL", "http://test-user:pw-secret@192.0.2.10:7125")
    out = await srv.save_gcode("host.gcode")
    assert oc.get(out["outcome_row_id"])["printer_id"] == "192.0.2.10"


@respx.mock
async def test_orca_printer_id_beats_orca_printer_url(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("ORCA_PRINTER_URL", "http://192.0.2.10")
    monkeypatch.setenv("ORCA_PRINTER_ID", "bench")
    out = await srv.save_gcode("both.gcode")
    assert oc.get(out["outcome_row_id"])["printer_id"] == "bench"


@respx.mock
async def test_a_whitespace_orca_printer_id_counts_as_unset(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("ORCA_PRINTER_ID", "   ")
    out = await srv.save_gcode("blank.gcode")
    assert oc.get(out["outcome_row_id"])["printer_id"] == "Test Printer"
    monkeypatch.setenv("ORCA_PRINTER_URL", "http://192.0.2.10")
    out = await srv.save_gcode("blank2.gcode")
    assert oc.get(out["outcome_row_id"])["printer_id"] == "192.0.2.10"


@respx.mock
async def test_save_gcode_with_no_printer_name_at_all_records_unknown(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/status").mock(return_value=httpx.Response(200, json={"presets": {}}))
    out = await srv.save_gcode("nameless.gcode")
    assert oc.get(out["outcome_row_id"])["printer_id"] == "unknown"


@respx.mock
async def test_save_gcode_still_records_without_an_estimate(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, slice_resp=httpx.Response(500, json={"error": "boom"}))
    out = await srv.save_gcode("c.gcode")
    row = oc.get(out["outcome_row_id"])
    assert out["outcome_recorded"] is True and row["est_time_s"] is None and row["printer_id"] == "Test Printer"


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
    # The module path constants are fixed at import time, so patch them (not HOME alone): this test
    # can then never land on a real store whatever the import order.
    monkeypatch.delenv("PRINT_OUTCOMES_DIR", raising=False)
    fake_shared = tmp_path / "would_be_shared" / "print-outcomes"
    monkeypatch.setattr(oc, "LEGACY_DIR", fake_shared)
    monkeypatch.setattr(oc, "DEFAULT_DIR", tmp_path / ".orcaslicer-mcp" / "outcomes")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ORCA_API_TOKEN", "tok"); monkeypatch.setenv("ORCA_API_URL", B)
    _routes()

    out = await srv.save_gcode("a.gcode")

    assert out["outcome_recorded"] is True
    assert (tmp_path / ".orcaslicer-mcp" / "gcode" / "a.gcode").exists()
    assert (tmp_path / ".orcaslicer-mcp" / "outcomes" / "outcomes.db").exists()
    assert not fake_shared.exists()

    # The first save created the store folder. The G-code folder must not flip to it: the rule is
    # "the shared folder exists", not "a store folder exists", so a second save lands beside the first.
    second = await srv.save_gcode("b.gcode")

    assert second["outcome_recorded"] is True
    assert (tmp_path / ".orcaslicer-mcp" / "gcode" / "b.gcode").exists()
    assert not (tmp_path / ".orcaslicer-mcp" / "outcomes" / "gcode").exists()
    assert not fake_shared.exists()


@respx.mock
async def test_whitespace_only_outcomes_dir_counts_as_unset(monkeypatch, tmp_path):
    # A whitespace-only PRINT_OUTCOMES_DIR is unset (outcomes.store_dir() strips it too). chdir so a
    # regression that used the raw value as a relative path lands inside tmp_path and is caught.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", "   ")
    fake_shared = tmp_path / "would_be_shared" / "print-outcomes"
    monkeypatch.setattr(oc, "LEGACY_DIR", fake_shared)
    monkeypatch.setattr(oc, "DEFAULT_DIR", tmp_path / ".orcaslicer-mcp" / "outcomes")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ORCA_API_TOKEN", "tok"); monkeypatch.setenv("ORCA_API_URL", B)
    _routes()

    out = await srv.save_gcode("w.gcode")

    assert out["outcome_recorded"] is True
    assert (tmp_path / ".orcaslicer-mcp" / "gcode" / "w.gcode").exists()
    assert (tmp_path / ".orcaslicer-mcp" / "outcomes" / "outcomes.db").exists()
    assert not (tmp_path / "   ").exists()


@respx.mock
async def test_save_gcode_uses_shared_store_when_it_already_exists(monkeypatch, tmp_path):
    monkeypatch.delenv("PRINT_OUTCOMES_DIR", raising=False)
    fake_shared = tmp_path / "would_be_shared" / "print-outcomes"
    fake_shared.mkdir(parents=True)
    monkeypatch.setattr(oc, "LEGACY_DIR", fake_shared)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ORCA_API_TOKEN", "tok"); monkeypatch.setenv("ORCA_API_URL", B)
    _routes()

    out = await srv.save_gcode("a.gcode")

    assert (fake_shared / "gcode" / "a.gcode").exists()
    assert (fake_shared / "outcomes.db").exists() and out["outcome_recorded"] is True
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
