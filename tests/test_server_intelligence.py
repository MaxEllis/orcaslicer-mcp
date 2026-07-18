import json, httpx, respx
from orcaslicer_mcp import server


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


async def test_consult_returns_chunks_and_notes(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCA_MCP_NOTES_DIR", str(tmp_path))
    r = await server.consult("stringing on travel moves")
    assert any("retraction" in c["file"] or "stringing" in c["file"] for c in r["chunks"])
    assert isinstance(r["notes"], list)


@respx.mock
async def test_check_profile_physics_overlays_changes(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {
        "layer_height": "0.4", "line_width": "0.85", "nozzle_diameter": "0.8",
        "inner_wall_speed": "45", "inner_wall_line_width": "0.88",
        "filament_max_volumetric_speed": "16", "filament_type": "PLA",
        "nozzle_temperature": "215"}}))
    r = await server.check_profile_physics({"nozzle_temperature": "205"})
    assert r["verdict"] == "blocked"
    assert any("sustains" in f["detail"] for f in r["fail"])


async def test_remember_saves_note(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCA_MCP_NOTES_DIR", str(tmp_path))
    r = await server.remember("note text", "user")
    assert r["saved"].endswith("user.md")
