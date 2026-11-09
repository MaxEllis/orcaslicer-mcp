"""M3, M15, m7 and m12 (usability 2026-11-09): slice replies say when a result is out of date,
why a slice would not start, what a warning means, and when the plate is empty."""
import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp.models import summarize_slice

BASE = "http://x:13130"
DONE = {"state": "done", "percent": 100, "message": "",
        "stats": {"estimated_time_seconds": 797, "filament_used_g": 2.2}, "warnings": []}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", BASE)

    async def _nosleep(_):
        pass
    monkeypatch.setattr(srv.asyncio, "sleep", _nosleep)


# --- M3: out of date ------------------------------------------------------------------

@respx.mock
async def test_slice_status_reports_stale_from_the_new_fields():
    respx.get(f"{BASE}/api/v1/slice/status").mock(
        return_value=httpx.Response(200, json={**DONE, "valid": False, "stale": True}))
    out = await srv.get_slice_status()
    assert out["out_of_date"] is True and "Slice again" in out["note"]


@respx.mock
async def test_slice_status_fresh_result_has_no_note():
    respx.get(f"{BASE}/api/v1/slice/status").mock(
        return_value=httpx.Response(200, json={**DONE, "valid": True, "stale": False}))
    out = await srv.get_slice_status()
    assert "out_of_date" not in out and "note" not in out


@respx.mock
async def test_slice_status_on_an_older_build_asks_status():
    respx.get(f"{BASE}/api/v1/slice/status").mock(return_value=httpx.Response(200, json=DONE))
    respx.get(f"{BASE}/api/v1/status").mock(
        return_value=httpx.Response(200, json={"slice_result_valid": False}))
    out = await srv.get_slice_status()
    assert out["out_of_date"] is True


def test_slicing_in_progress_is_never_out_of_date():
    assert "out_of_date" not in summarize_slice({"state": "slicing", "valid": False}, False)


@respx.mock
async def test_breakdown_says_out_of_date():
    respx.get(f"{BASE}/api/v1/slice/status").mock(
        return_value=httpx.Response(200, json={**DONE, "valid": False}))
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {}}))
    out = await srv.get_slice_breakdown()
    assert out["out_of_date"] is True


# --- M15: a slice that will not start -----------------------------------------------

@respx.mock
async def test_slice_not_started_relays_orcaslicers_message():
    respx.post(f"{BASE}/api/v1/slice").mock(return_value=httpx.Response(
        422, json={"error": "slice_not_started",
                   "validation": "Layer height cannot exceed nozzle diameter."}))
    out = await srv.slice_and_wait(timeout=5)
    assert out["error"] == "slice_not_started"
    assert out["detail"] == "OrcaSlicer won't slice this plate: Layer height cannot exceed nozzle diameter."


@respx.mock
async def test_slice_not_started_reads_the_message_from_slice_status():
    respx.post(f"{BASE}/api/v1/slice").mock(
        return_value=httpx.Response(422, json={"error": "slice_not_started", "errors": {}}))
    respx.get(f"{BASE}/api/v1/slice/status").mock(return_value=httpx.Response(200, json={
        "state": "error", "message": "plate cannot be sliced (object outside bed or invalid state)",
        "validation": "Layer height 0.8 mm is larger than the 0.4 mm nozzle."}))
    out = await srv.slice()
    assert "0.8 mm is larger than the 0.4 mm nozzle" in out["detail"]


@respx.mock
async def test_slice_not_started_without_a_reason_runs_the_physics_check():
    respx.post(f"{BASE}/api/v1/slice").mock(
        return_value=httpx.Response(422, json={"error": "slice_not_started", "errors": {}}))
    respx.get(f"{BASE}/api/v1/slice/status").mock(return_value=httpx.Response(200, json={
        "state": "error", "message": "plate cannot be sliced (object outside bed or invalid state)"}))
    respx.get(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(200, json={"config": {
        "layer_height": "0.8", "nozzle_diameter": "0.4", "initial_layer_print_height": "0.8"}}))
    out = await srv.slice_and_wait(timeout=5)
    names = {f["name"] for f in out["physics_fails"]}
    assert "first_layer_height" in names
    assert "object outside bed" not in out["detail"]
    assert out["detail"].startswith("OrcaSlicer won't start this slice and gave no reason.")


@respx.mock
async def test_apply_and_slice_relays_the_refusal_in_result():
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json={}))
    respx.put(f"{BASE}/api/v1/config").mock(return_value=httpx.Response(
        200, json={"applied": ["layer_height"], "errors": {}}))
    respx.post(f"{BASE}/api/v1/slice").mock(return_value=httpx.Response(
        422, json={"error": "slice_not_started", "detail": "Too thick."}))
    out = await srv.apply_and_slice({"layer_height": 0.3})
    assert out["applied"] == ["layer_height"]
    assert out["result"]["detail"].endswith("Too thick.")


# --- m12: empty plate ------------------------------------------------------------------

@respx.mock
async def test_nothing_to_slice_says_the_plate_is_empty():
    respx.post(f"{BASE}/api/v1/slice").mock(
        return_value=httpx.Response(422, json={"error": "nothing_to_slice"}))
    out = await srv.slice_and_wait(timeout=5)
    assert out == {"error": "nothing_to_slice",
                   "detail": "The plate has no objects; load a model first (load_model)."}


@respx.mock
async def test_render_preview_on_an_empty_plate_says_load_a_model():
    respx.get(f"{BASE}/api/v1/plate/render").mock(
        return_value=httpx.Response(409, json={"error": "no_slice_result"}))
    respx.get(f"{BASE}/api/v1/objects").mock(return_value=httpx.Response(200, json={"objects": []}))
    out = await srv.render_plate(view="preview")
    assert out["detail"].startswith("The plate has no objects")
    assert "load a model" in out["hint"]


@respx.mock
async def test_render_preview_with_objects_says_slice_first():
    respx.get(f"{BASE}/api/v1/plate/render").mock(
        return_value=httpx.Response(409, json={"error": "no_slice_result"}))
    respx.get(f"{BASE}/api/v1/objects").mock(return_value=httpx.Response(
        200, json={"objects": [{"id": 1, "name": "cube"}]}))
    out = await srv.render_plate(view="preview")
    assert "slice first" in out["hint"]


@respx.mock
async def test_describe_plate_on_an_empty_plate():
    respx.get(f"{BASE}/api/v1/gcode").mock(return_value=httpx.Response(409, json={"error": "not_sliced"}))
    respx.get(f"{BASE}/api/v1/objects").mock(return_value=httpx.Response(200, json={"objects": []}))
    out = await srv.describe_plate()
    assert out["detail"].startswith("The plate has no objects")


# --- m7: warning sentences ---------------------------------------------------------------

@respx.mock
async def test_warnings_carry_orcaslicers_sentence_and_a_level_word():
    respx.get(f"{BASE}/api/v1/slice/status").mock(return_value=httpx.Response(200, json={
        **DONE, "valid": True, "warnings": [
            {"level": 3, "message": "bed_temperature_too_high_than_filament", "code": "1000C001"},
            {"level": 3, "message": "A G-code path goes beyond the plate boundaries.", "code": "TOOLPATH_OUTSIDE"}]}))
    out = await srv.get_slice_status()
    w0, w1 = out["warnings"]
    assert w0["message"] == "bed_temperature_too_high_than_filament"   # the code stays
    assert w0["text"].startswith("The current heatbed temperature is relatively high.")
    assert w0["severity"] == "warning"
    assert w1["text"] == "A G-code path goes beyond the plate boundaries."


def test_string_warnings_pass_through():
    assert summarize_slice({"state": "done", "warnings": ["thin wall"]})["warnings"] == ["thin wall"]
