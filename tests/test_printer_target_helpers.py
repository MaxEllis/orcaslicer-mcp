import json

import pytest

from orcaslicer_mcp.printer import target as t
from orcaslicer_mcp.printer.errors import PrinterError, auth_error


@pytest.mark.parametrize("raw,url,auth", [
    ("192.0.2.10", "http://192.0.2.10", None),
    ("http://192.0.2.10/", "http://192.0.2.10", None),
    ("https://printer.example:7125/", "https://printer.example:7125", None),
    ("192.0.2.10:8080/octo/", "http://192.0.2.10:8080/octo", None),
    ("http://user:secret@192.0.2.10", "http://192.0.2.10", ("user", "secret")),
    ("http://<redacted>@192.0.2.10", "http://192.0.2.10", None),
    ("http://us%65r:p%40ss@192.0.2.10", "http://192.0.2.10", ("user", "p@ss")),
    ("   ", "", None),
])
def test_normalise_url(raw, url, auth):
    assert t.normalise_url(raw) == (url, auth)


def test_public_view_never_includes_auth():
    tgt = t.PrinterTarget(url="http://192.0.2.10", source="override", auth=("u", "pw"))
    assert set(tgt.public()) == {"profile", "url", "kind", "source", "remembered_at"}
    assert "pw" not in json.dumps(tgt.public())


def test_repr_hides_the_password():
    tgt = t.PrinterTarget(url="http://192.0.2.10", source="override", auth=("user", "pw-secret"))
    assert "pw-secret" not in repr(tgt)
    assert "user" not in repr(tgt)


@pytest.mark.parametrize("ht", ["prusalink", "duet", "3dprinteros", "PrusaLink"])
def test_unsupported_connection_types(ht):
    with pytest.raises(PrinterError) as e:
        t.check_host_type(ht)
    assert e.value.code == "unsupported_connection" and e.value.details["host_type"] == ht.lower()


@pytest.mark.parametrize("ht", ["moonraker", "octoprint", "elegoolink", "crealityprint", "", None, "something-new"])
def test_probeable_connection_types_pass(ht):
    t.check_host_type(ht)


def test_remember_then_recall():
    t.remember(t.PrinterTarget(url="http://192.0.2.10", source="profile", profile="Test Printer",
                               host_type="moonraker", kind="klipper", auth=("u", "pw")))
    saved = json.loads(t.REMEMBERED_PATH.read_text())
    assert "auth" not in saved and "pw" not in json.dumps(saved)
    back = t.recall_remembered()
    assert (back.source, back.kind, back.profile, back.url) == ("remembered", "klipper", "Test Printer", "http://192.0.2.10")
    assert back.remembered_at is not None and back.auth is None


def test_recall_ignores_a_missing_or_corrupt_file():
    assert t.recall_remembered() is None
    t.REMEMBERED_PATH.write_text("{not json")
    assert t.recall_remembered() is None


def test_printer_id_precedence(monkeypatch):
    tgt = t.PrinterTarget(url="http://192.0.2.10:7125", source="profile", profile="Test Printer")
    assert t.printer_id_for(tgt) == "Test Printer"
    monkeypatch.setenv("ORCA_PRINTER_ID", "bench")
    assert t.printer_id_for(tgt) == "bench"
    monkeypatch.setenv("ORCA_PRINTER_ID", "  ")
    assert t.printer_id_for(t.PrinterTarget(url="http://192.0.2.10:7125", source="override")) == "192.0.2.10"


def test_error_codes_are_checked_and_serialise():
    with pytest.raises(ValueError):
        PrinterError("made_up", "x")
    e = PrinterError("not_reachable", "No answer.", hint="Switch it on.", tried=["a"])
    assert e.as_dict() == {"error": "not_reachable", "message": "No answer.", "hint": "Switch it on.", "tried": ["a"]}
    assert auth_error("OctoPrint", key_set=False).code == "auth_required"
    assert auth_error("OctoPrint", key_set=True).code == "auth_rejected"
