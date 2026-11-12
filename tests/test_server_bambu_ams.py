"""Bambu Lab AMS through the fork's tray routes: tray reading, the Send dialog's mapping with a match per
filament, and check_printer_match taking the AMS path on a Bambu printer."""
import httpx, respx
import pytest
import orcaslicer_mcp.server as srv
from orcaslicer_mcp.printer.ams_match import base_material, classify

U = "http://x:13130/api/v1/printer"


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")
    m.delenv("ORCA_PRINTER_URL", raising=False)


def tray(ams_id, tray_id, label, ftype, name, color="#FFFFFF", ready=True, status="loaded"):
    return {"ams_id": ams_id, "tray_id": tray_id, "index": int(ams_id) * 4 + int(tray_id) if ams_id != "255" else 255,
            "label": label, "status": status if ready else "empty", "ready": ready, "active": False,
            "unknown_filament": False, "filament_type": ftype if ready else "", "filament_name": name if ready else "",
            "brand": "Bambu Lab" if name.startswith("Bambu") else "Polymaker", "filament_id": "GFB00",
            "bambu_filament": name.startswith("Bambu"), "filament_color": color if ready else "",
            "remain_percent": 60 if ready else None, "min_nozzle_temp": 240, "max_nozzle_temp": 280}


SLOTS = {"supported": True, "kind": "bambu", "connected": True,
         "printer": {"name": "P1S", "model": "Bambu Lab P1S", "connection": "lan"},
         "active": {"ams_id": "0", "tray_id": "0"},
         "ams": [{"ams_id": "0", "type": "AMS", "extruder": 0, "humidity_level": 2, "humidity_percent": 31,
                  "temperature": 24.5, "trays": ["A1", "A2", "A3", "A4"]}],
         "trays": [tray("0", "0", "A1", "ASA", "Bambu ASA"),
                   tray("0", "1", "A2", "ASA-Aero", "Polymaker PolyLite LW-ASA", "#3C3C3C"),
                   tray("0", "2", "A3", "PLA", "Bambu PLA Basic", "#FF0000"),
                   tray("0", "3", "A4", "", "", ready=False),
                   tray("255", "0", "Ext", "", "", ready=False)]}


def fil(index, ftype, name, tray_=None, vendor="Generic", color="#FFFFFF"):
    row = {"index": index, "type": ftype, "name": name, "vendor": vendor, "color": color, "tray": tray_,
           "source": "matched" if tray_ else "none", "warnings": [] if tray_ else [f"No tray holds {ftype}."]}
    return row


A1 = {"ams_id": "0", "tray_id": "0", "index": 0, "label": "A1", "filament_type": "ASA",
      "filament_name": "Bambu ASA", "brand": "Bambu Lab", "filament_color": "#FFFFFF"}
A3 = {"ams_id": "0", "tray_id": "2", "index": 2, "label": "A3", "filament_type": "PLA",
      "filament_name": "Bambu PLA Basic", "brand": "Bambu Lab", "filament_color": "#FF0000"}


def mapping(*rows, valid=True):
    return {"supported": True, "kind": "bambu", "plate": 0,
            "printer": {"name": "P1S", "model": "Bambu Lab P1S", "connection": "lan"},
            "filaments": list(rows), "valid": valid,
            "problems": [] if valid else ["Filament 2 (Polymaker LW-ASA) has no tray: the Send dialog needs a choice."]}


# --- base_material ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("ftype, base", [
    ("ASA", "ASA"), ("LW-ASA", "ASA"), ("ASA-Aero", "ASA"), ("ASA-CF", "ASA"), ("asa aero", "ASA"),
    ("PLA", "PLA"), ("PLA-CF", "PLA"), ("PLA Basic", "PLA"), ("PETG", "PETG"), ("PETG-HF", "PETG"), ("PET-CF", "PET"),
    ("PA6-CF", "PA6"), ("PAHT-CF", "PAHT"), ("PA-CF", "PA"), ("TPU 95A", "TPU"),
    ("PLA-S", "PLA-S"), ("Sup.PLA", "SUP.PLA"), ("", ""), (None, ""), ("Mystery", "MYSTERY")])
def test_base_material(ftype, base):
    assert base_material(ftype) == base


# --- classify ------------------------------------------------------------------------------------------------

def test_classify_exact_match_notes_a_different_filament_of_the_same_type():
    out = classify(mapping(fil(0, "ASA", "Generic ASA", A1)), SLOTS)
    row = out["filaments"][0]
    assert row["match"] == "exact" and row["slot"] == 1
    assert row["note"] == "A1 holds Bambu ASA; the slot's preset is Generic ASA (both ASA)."
    assert out["warnings"] == [] and out["headline"] == "The filament has a tray holding its type."


def test_classify_exact_same_name_has_no_note():
    a1 = {**A1, "filament_name": "Generic ASA"}
    row = classify(mapping(fil(0, "ASA", "Generic ASA", a1)), SLOTS)["filaments"][0]
    assert row["match"] == "exact" and "note" not in row


def test_classify_soft_match_names_both_filaments():
    # Slot 2 is "Polymaker LW-ASA" with type ASA; the AMS calls the Polymaker spool ASA-Aero, so OrcaSlicer left it unmapped.
    out = classify(mapping(fil(0, "ASA", "Generic ASA", A1), fil(1, "ASA", "Polymaker LW-ASA", vendor="Polymaker"),
                           valid=False), SLOTS)
    row = out["filaments"][1]
    assert row["match"] == "soft" and row["slot"] == 2
    assert [c["label"] for c in row["candidates"]] == ["A2"]
    assert row["candidates"][0]["filament_name"] == "Polymaker PolyLite LW-ASA"
    assert out["warnings"] == [
        "No tray holds ASA for slot 2 (Polymaker LW-ASA). A2 holds Polymaker PolyLite LW-ASA (ASA-Aero): the same "
        "base material (ASA), close enough that the Send dialog will take it if you choose it, but check it is the "
        "filament you meant."]
    assert out["headline"] == ("1 of 2 filaments have a tray holding their type; 1 with only a near match (same base "
                               "material, a different filament). The Send dialog will wait for a choice on those.")
    assert out["valid"] is False and out["problems"]  # the fork's own verdict is kept


def test_classify_soft_match_skips_trays_already_taken():
    # A1 (the only ASA-family tray besides A2) is taken by filament 1; A2 remains and is the candidate.
    out = classify(mapping(fil(0, "ASA", "Generic ASA", A1), fil(1, "LW-ASA", "Polymaker LW-ASA")), SLOTS)
    assert [c["label"] for c in out["filaments"][1]["candidates"]] == ["A2"]
    slots = {**SLOTS, "trays": [t for t in SLOTS["trays"] if t["label"] != "A2"]}
    assert classify(mapping(fil(0, "ASA", "Generic ASA", A1), fil(1, "LW-ASA", "Polymaker LW-ASA")), slots)[
        "filaments"][1]["match"] == "none"


def test_classify_none_says_load_or_change_preset():
    out = classify(mapping(fil(0, "PETG", "Generic PETG")), SLOTS)
    row = out["filaments"][0]
    assert row["match"] == "none" and "candidates" not in row
    assert out["warnings"] == ["No tray holds PETG, or any other PETG, for slot 1 (Generic PETG): load one, or point "
                               "the slot at a preset for a filament that is loaded."]
    assert out["headline"].startswith("0 of 1 filaments have a tray holding their type; 1 with nothing loaded that fits")


def test_classify_support_type_never_soft_matches_its_base():
    out = classify(mapping(fil(0, "PLA-S", "Bambu Support for PLA")), SLOTS)
    assert out["filaments"][0]["match"] == "none"


def test_classify_without_slots_has_no_soft_matches():
    out = classify(mapping(fil(0, "LW-ASA", "Polymaker LW-ASA")), None)
    assert out["filaments"][0]["match"] == "none"


def test_classify_unknown_type():
    out = classify(mapping(fil(0, "", "Odd preset")), SLOTS)
    assert out["filaments"][0]["match"] == "unknown"
    assert out["warnings"] == ["slot 1 (Odd preset) has no filament type in its preset, so no tray can be matched to it."]


def test_classify_empty_plate():
    out = classify(mapping(), SLOTS)
    assert out["headline"] == "The plate uses no filament." and out["filaments"] == []


# --- the tools -----------------------------------------------------------------------------------------------

@respx.mock
async def test_get_printer_slots_bambu_passes_through(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{U}/slots").mock(return_value=httpx.Response(200, json=SLOTS))
    assert await srv.get_printer_slots() == SLOTS


@respx.mock
async def test_get_printer_slots_old_build_hints_bambu_capability(monkeypatch):
    _env(monkeypatch)
    body = {"supported": False, "reason": "not_elegoo_cc2", "message": "The selected printer profile is not an Elegoo Centauri Carbon 2."}
    respx.get(f"{U}/slots").mock(return_value=httpx.Response(200, json=body))
    out = await srv.get_printer_slots()
    assert out["reason"] == "not_elegoo_cc2" and "bambu_ams" in out["hint"]
    respx.get(f"{U}/slot_mapping").mock(return_value=httpx.Response(200, json=body))
    assert "bambu_ams" in (await srv.get_filament_slot_mapping())["hint"]


@respx.mock
async def test_get_printer_slots_bambu_unsupported_reasons_relayed(monkeypatch):
    _env(monkeypatch)
    body = {"supported": False, "kind": "bambu", "reason": "no_printer_selected",
            "message": "No Bambu Lab printer is selected in OrcaSlicer's Device tab."}
    respx.get(f"{U}/slots").mock(return_value=httpx.Response(200, json=body))
    out = await srv.get_printer_slots()
    assert out == body and "hint" not in out


@respx.mock
async def test_get_filament_slot_mapping_bambu_adds_match(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{U}/slot_mapping").mock(return_value=httpx.Response(200, json=mapping(
        fil(0, "ASA", "Generic ASA", A1), fil(1, "ASA", "Polymaker LW-ASA", vendor="Polymaker"), valid=False)))
    respx.get(f"{U}/slots").mock(return_value=httpx.Response(200, json=SLOTS))
    out = await srv.get_filament_slot_mapping()
    assert [f["match"] for f in out["filaments"]] == ["exact", "soft"]
    assert out["filaments"][1]["candidates"][0]["label"] == "A2"
    assert out["kind"] == "bambu" and out["headline"] and len(out["warnings"]) == 1


@respx.mock
async def test_get_filament_slot_mapping_bambu_without_slots_still_answers(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{U}/slot_mapping").mock(return_value=httpx.Response(200, json=mapping(fil(0, "LW-ASA", "Polymaker LW-ASA"))))
    respx.get(f"{U}/slots").mock(return_value=httpx.Response(500, json={"error": "ui_timeout"}))
    out = await srv.get_filament_slot_mapping()
    assert out["filaments"][0]["match"] == "none"


@respx.mock
async def test_get_filament_slot_mapping_canvas_unchanged(monkeypatch):
    _env(monkeypatch)
    body = {"supported": True, "kind": "canvas", "plate": 0,
            "filaments": [{"index": 0, "type": "PLA", "name": "x", "color": "#000000",
                           "tray": {"canvas_id": 0, "tray_id": 0, "label": "A1"}, "source": "matched", "warnings": []}],
            "valid": True, "problems": []}
    respx.get(f"{U}/slot_mapping").mock(return_value=httpx.Response(200, json=body))
    assert await srv.get_filament_slot_mapping() == body


F = "http://x:13130"


def bambu_profile(model="Bambu Lab P1S"):
    """The fork says the selected printer profile is a Bambu one with no address: the AMS path."""
    respx.get(f"{F}/api/v1/status").mock(return_value=httpx.Response(200, json={
        "presets": {"printer": "Bambu Lab P1S 0.4 nozzle", "print": "p", "filaments": ["f"]}}))
    respx.post(f"{F}/api/v1/preset/config").mock(return_value=httpx.Response(200, json={
        "name": "Bambu Lab P1S 0.4 nozzle", "system": True, "config": {"print_host": "", "printer_model": model}}))


@respx.mock
async def test_check_printer_match_bambu_takes_the_ams_path(monkeypatch):
    _env(monkeypatch)
    bambu_profile()
    respx.get(f"{U}/slot_mapping").mock(return_value=httpx.Response(200, json=mapping(fil(0, "PLA", "Generic PLA", A3))))
    respx.get(f"{U}/slots").mock(return_value=httpx.Response(200, json=SLOTS))
    out = await srv.check_printer_match()
    assert out["kind"] == "bambu" and out["filaments"][0]["match"] == "exact"
    assert out["headline"] == "The filament has a tray holding its type."
    assert out["printer"]["model"] == "Bambu Lab P1S"


@respx.mock
async def test_check_printer_match_bambu_printer_offline(monkeypatch):
    _env(monkeypatch)
    bambu_profile()
    respx.get(f"{U}/slot_mapping").mock(return_value=httpx.Response(200, json={
        "supported": False, "kind": "bambu", "reason": "printer_offline", "message": "P1S is not connected to OrcaSlicer right now."}))
    out = await srv.check_printer_match()
    assert out["supported"] is False and out["reason"] == "printer_offline"
    assert out["message"] == "P1S is not connected to OrcaSlicer right now." and out["hint"]


@respx.mock
async def test_check_printer_match_bambu_on_old_build_says_what_is_needed(monkeypatch):
    _env(monkeypatch)
    bambu_profile()
    respx.get(f"{U}/slot_mapping").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.check_printer_match()
    assert out["error"] == "unsupported_connection" and out["host_type"] == "bambu"
    assert "bambu_ams" in out["message"]


@respx.mock
async def test_check_printer_match_bambu_route_answers_not_supported_keeps_usual_reply(monkeypatch):
    # The fork's tray route decides by its own preset check; should it disagree, the model still hears the plain answer.
    _env(monkeypatch)
    bambu_profile()
    respx.get(f"{U}/slot_mapping").mock(return_value=httpx.Response(200, json={
        "supported": False, "kind": "bambu", "reason": "not_supported", "message": "neither"}))
    out = await srv.check_printer_match()
    assert out["error"] == "unsupported_connection"


@respx.mock
async def test_check_printer_match_klipper_profile_never_asks_for_trays(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("ORCA_PRINTER_URL", "http://192.0.2.10")
    trays = respx.get(f"{U}/slot_mapping").mock(return_value=httpx.Response(200, json=SLOTS))
    for url in ("http://192.0.2.10/server/info", "http://192.0.2.10:7125/server/info", "http://192.0.2.10/api/version"):
        respx.get(url).mock(side_effect=httpx.ConnectError("down"))
    out = await srv.check_printer_match()
    assert out["error"] == "not_reachable" and trays.call_count == 0


@respx.mock
async def test_get_printer_status_bambu_profile_points_at_the_tray_tools(monkeypatch):
    _env(monkeypatch)
    bambu_profile()
    out = await srv.get_printer_status()
    assert out["error"] == "unsupported_connection"
    assert "get_filament_slot_mapping" in out["message"] and "check_printer_match" in out["message"]


# --- review fixes (2026-11) -----------------------------------------------------------------------------------

def test_classify_exact_system_preset_name_with_printer_suffix_has_no_note():
    # A system preset carries its printer; the AMS names the spool without it. Same filament: no note.
    a3 = {**A3, "filament_name": "Bambu PLA Basic"}
    row = classify(mapping(fil(0, "PLA", "Bambu PLA Basic @BBL P1S", a3)), SLOTS)["filaments"][0]
    assert row["match"] == "exact" and "note" not in row


def test_classify_exact_compares_filament_ids_when_both_carry_one():
    same = classify(mapping({**fil(0, "PLA", "My PLA", {**A3, "filament_id": "GFA00"}), "filament_id": "GFA00"}),
                    SLOTS)["filaments"][0]
    assert "note" not in same
    other = classify(mapping({**fil(0, "PLA", "Bambu PLA Basic @BBL P1S", {**A3, "filament_id": "GFA01"}),
                              "filament_id": "GFA00"}), SLOTS)["filaments"][0]
    assert other["note"] == ("A3 holds Bambu PLA Basic; the slot's preset is Bambu PLA Basic @BBL P1S (both PLA).")


@pytest.mark.parametrize("ftype", ["PETG-S", "PLA Support", "PVA-S"])
def test_support_types_stay_their_own(ftype):
    assert base_material(ftype) == ftype.upper()


def test_classify_fibre_filled_tray_is_no_stand_in_for_an_unfilled_slot():
    slots = {**SLOTS, "trays": [tray("0", "0", "A1", "PLA-CF", "Bambu PLA-CF")]}
    out = classify(mapping(fil(0, "PLA", "Generic PLA"), valid=False), slots)
    row = out["filaments"][0]
    assert row["match"] == "none" and "candidates" not in row
    assert "fibre filled, which wears a standard nozzle" in out["warnings"][0]
