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


def test_printer_id_helper_has_one_rule_for_both_writers(monkeypatch):
    assert t.printer_id("Test Printer") == "Test Printer"
    assert t.printer_id(None) == "unknown" and t.printer_id("") == "unknown" and t.printer_id("  ") == "unknown"
    monkeypatch.setenv("ORCA_PRINTER_URL", "http://test-user:pw-secret@Printer-Test.local:7125/")
    assert t.printer_id("Test Printer") == "printer-test.local"  # the host name only, never the login
    assert t.printer_id(None) == "printer-test.local"
    monkeypatch.setenv("ORCA_PRINTER_ID", "bench")
    assert t.printer_id("Test Printer") == "bench"
    monkeypatch.setenv("ORCA_PRINTER_ID", "   ")  # whitespace counts as unset
    assert t.printer_id("Test Printer") == "printer-test.local"


@pytest.mark.parametrize("raw", ["http://[2001:db8::1", "http://", "   "])
def test_printer_id_never_raises_on_an_unusable_printer_url(monkeypatch, raw):
    monkeypatch.setenv("ORCA_PRINTER_URL", raw)
    assert t.printer_id("Test Printer") == "Test Printer"
    assert t.printer_id(None) == "unknown"


@pytest.mark.parametrize("raw", [
    "http://user:pa/ss@192.0.2.10",
    "http://test-user:123?x@192.0.2.10",
    "http://tok/en@192.0.2.10",
])
def test_printer_id_never_contains_part_of_a_login(monkeypatch, raw):
    # An unencoded '/', '?' or '#' in the user name or password makes urllib read the credentials as
    # the host ('user', 'test-user', 'tok'). The address is rejected, so the id falls through.
    monkeypatch.setenv("ORCA_PRINTER_URL", raw)
    assert t.printer_id("Test Printer") == "Test Printer"
    assert t.printer_id(None) == "unknown"
    assert t.printer_id(None, fallback_url=raw) == "unknown"


def test_printer_id_still_reads_the_host_of_a_well_formed_login(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", "http://test-user:pw@192.0.2.10")
    assert t.printer_id("Test Printer") == "192.0.2.10"
    monkeypatch.delenv("ORCA_PRINTER_URL")
    assert t.printer_id(None, fallback_url="http://test-user:pw@192.0.2.10") == "192.0.2.10"


def test_printer_id_ignores_a_profile_name_that_is_not_text():
    # a hand-edited printer.json can say "profile": 5
    assert t.printer_id(5) == "unknown"
    assert t.printer_id(["Test Printer"]) == "unknown"
    assert t.printer_id(5, fallback_url="http://192.0.2.10:7125") == "192.0.2.10"


def test_printer_id_for_follows_the_same_rule(monkeypatch):
    monkeypatch.setenv("ORCA_PRINTER_URL", "http://user:pw@192.0.2.10:7125")
    override = t.PrinterTarget(url="http://192.0.2.10:7125", source="override")
    assert t.printer_id_for(override) == t.printer_id("Test Printer") == "192.0.2.10"
    monkeypatch.delenv("ORCA_PRINTER_URL")
    profile = t.PrinterTarget(url="http://192.0.2.10:7125", source="profile", profile="Test Printer")
    assert t.printer_id_for(profile) == t.printer_id("Test Printer") == "Test Printer"
    # a target with no profile and no setting still gets its host rather than "unknown"
    assert t.printer_id_for(t.PrinterTarget(url="http://192.0.2.10:7125", source="remembered")) == "192.0.2.10"


def test_error_codes_are_checked_and_serialise():
    with pytest.raises(ValueError):
        PrinterError("made_up", "x")
    e = PrinterError("not_reachable", "No answer.", hint="Switch it on.", tried=["a"])
    assert e.as_dict() == {"error": "not_reachable", "message": "No answer.", "hint": "Switch it on.", "tried": ["a"]}
    assert auth_error("OctoPrint", key_set=False).code == "auth_required"
    assert auth_error("OctoPrint", key_set=True).code == "auth_rejected"


def test_basic_auth_refusal_wording_never_echoes_credentials():
    e = auth_error("OctoPrint", key_set=False, basic_auth=True)
    assert e.code == "auth_rejected"
    assert e.message == "OctoPrint refused the user name and password in the printer address."
    assert e.hint == "Check the user name and password in ORCA_PRINTER_URL (special characters must be percent-encoded)."
    assert "profile" not in e.hint  # credentials in OrcaSlicer's own address are redacted and never read
    text = e.message + " " + e.hint + json.dumps(e.as_dict())
    assert "—" not in text and "–" not in text


@pytest.mark.parametrize("raw,shown", [
    ("http://192.0.2.10:7125", "http://192.0.2.10:7125"),
    ("http://test-user:pa/ss@192.0.2.10", "http://<redacted>@192.0.2.10"),
    ("  https://test-user:p@ss@192.0.2.10/  ", "https://<redacted>@192.0.2.10/"),
    ("test-user:pa/ss@192.0.2.10", "<redacted>@192.0.2.10"),
    # No scheme, and the password holds "://": "test-user:pa" must not be shown as one.
    ("test-user:pa://ss@192.0.2.10", "<redacted>@192.0.2.10"),
])
def test_shown_address_never_carries_the_login(raw, shown):
    assert t._shown(raw) == shown


def test_unreadable_address_error_never_echoes_the_login():
    with pytest.raises(PrinterError) as e:
        t._parse_address("test-user:pa://ss@192.0.2.10", "override")
    text = json.dumps(e.value.as_dict())
    assert "<redacted>@192.0.2.10" in text and "test-user" not in text and "pa:" not in text


# --- PrinterError.as_dict: the core keys always win ---------------------------------------------

def test_as_dict_core_keys_win_over_a_detail_of_the_same_name():
    e = PrinterError("not_reachable", "No answer.", hint="Switch it on.", error="shadow-code", tried=["a"])
    e.details.update(message="shadow message", hint="shadow hint")
    assert e.as_dict() == {"error": "not_reachable", "message": "No answer.", "hint": "Switch it on.",
                           "tried": ["a"]}


def test_a_detail_named_hint_never_stands_in_for_a_missing_hint():
    e = PrinterError("not_reachable", "No answer.", error="shadow-code")
    e.details["hint"] = "a guess"
    assert e.as_dict() == {"error": "not_reachable", "message": "No answer."}  # hints never guess


# --- normalise_url: query, fragment, IPv6 --------------------------------------------------------

@pytest.mark.parametrize("raw,url,auth", [
    ("http://192.0.2.10/octo?x=1#frag", "http://192.0.2.10/octo", None),
    ("192.0.2.10:7125?x=1", "http://192.0.2.10:7125", None),
    ("http://192.0.2.10/#only-a-fragment", "http://192.0.2.10", None),
    ("http://[2001:db8::1]:7125/", "http://[2001:db8::1]:7125", None),
    ("[2001:db8::1]", "http://[2001:db8::1]", None),
    ("http://test-user:pw@[2001:db8::1]:7125/path/?q=1#f", "http://[2001:db8::1]:7125/path",
     ("test-user", "pw")),
])
def test_normalise_url_drops_query_and_fragment_and_handles_ipv6(raw, url, auth):
    assert t.normalise_url(raw) == (url, auth)


def test_an_ipv6_address_passes_the_address_check():
    assert t._parse_address("http://[2001:db8::1]:7125", "override") == ("http://[2001:db8::1]:7125", None)


def test_a_redacted_login_marker_is_the_guards_placeholder():
    from orcaslicer_mcp.guard import REDACTED
    assert t.normalise_url(f"http://{REDACTED}@192.0.2.10") == ("http://192.0.2.10", None)


# --- remember(): atomic ---------------------------------------------------------------------------

def _remembered_target(url="http://192.0.2.10", kind="klipper"):
    return t.PrinterTarget(url=url, source="profile", profile="Test Printer", host_type="moonraker", kind=kind)


def test_remember_goes_through_a_temp_file_and_a_replace(monkeypatch):
    import os
    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append((str(src), str(dst), os.path.exists(src)))
        return real(src, dst)
    monkeypatch.setattr(t.os, "replace", spy)
    t.remember(_remembered_target())
    assert len(seen) == 1
    src, dst, src_existed = seen[0]
    assert dst == str(t.REMEMBERED_PATH) and src != dst and src_existed
    assert os.path.dirname(src) == os.path.dirname(dst)  # same folder, so the replace is atomic
    assert json.loads(t.REMEMBERED_PATH.read_text())["url"] == "http://192.0.2.10"
    assert [p.name for p in t.REMEMBERED_PATH.parent.iterdir()] == ["printer.json"]


def test_a_failed_remember_leaves_the_old_file_whole_and_no_temp_file(monkeypatch):
    t.remember(_remembered_target(url="http://192.0.2.10"))
    before = t.REMEMBERED_PATH.read_text()

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(t.os, "replace", boom)
    t.remember(_remembered_target(url="http://192.0.2.99"))  # a convenience: never raises
    assert t.REMEMBERED_PATH.read_text() == before
    assert [p.name for p in t.REMEMBERED_PATH.parent.iterdir()] == ["printer.json"]


# --- recall_remembered(): validates what it reads --------------------------------------------------

GOOD = {"url": "http://192.0.2.10", "kind": "klipper", "profile": "Test Printer", "host_type": "moonraker",
        "printer_model": "Test Model", "found_at": 1_700_000_000.0}


def _saved(**over):
    d = dict(GOOD)
    d.update(over)
    t.REMEMBERED_PATH.write_text(json.dumps(d))


def test_recall_returns_a_well_formed_file_whole():
    _saved()
    back = t.recall_remembered()
    assert (back.url, back.kind, back.profile, back.host_type, back.printer_model, back.remembered_at) == (
        "http://192.0.2.10", "klipper", "Test Printer", "moonraker", "Test Model", 1_700_000_000.0)
    assert back.source == "remembered" and back.auth is None


@pytest.mark.parametrize("url", ["http://192.0.2.10:abc", "http://192.0.2.10:99999", "http://", "", "   ", 5, None,
                                 ["http://192.0.2.10"], "http://user:pa/ss-secret@192.0.2.10"])
def test_recall_refuses_an_address_that_would_not_pass_the_address_check(url):
    _saved(url=url)
    assert t.recall_remembered() is None


def test_recall_normalises_the_address_and_never_keeps_a_login():
    _saved(url="test-user:pw-secret@192.0.2.10:7125/")
    back = t.recall_remembered()
    assert back.url == "http://192.0.2.10:7125" and back.auth is None
    assert "pw-secret" not in repr(back) and "test-user" not in repr(back)


@pytest.mark.parametrize("bad", ['"soon"', "true", "null", "[1]", '{"a": 1}', "NaN", "Infinity", "-Infinity",
                                 "1" + "0" * 400])
def test_recall_reads_a_found_at_that_is_not_a_finite_number_as_missing(bad):
    rest = json.dumps({k: v for k, v in GOOD.items() if k != "found_at"})
    t.REMEMBERED_PATH.write_text(rest[:-1] + ', "found_at": ' + bad + "}")  # raw text: NaN is not valid JSON
    back = t.recall_remembered()
    assert back is not None and back.remembered_at is None and back.url == "http://192.0.2.10"


@pytest.mark.parametrize("good,expected", [(1_700_000_000, 1_700_000_000.0), (0, 0.0), (1.5, 1.5)])
def test_recall_keeps_a_finite_found_at(good, expected):
    _saved(found_at=good)
    assert t.recall_remembered().remembered_at == expected


@pytest.mark.parametrize("kind", ["bambu", "Klipper", "", 5, True, None, ["klipper"]])
def test_recall_reads_an_unknown_kind_as_missing(kind):
    _saved(kind=kind)
    back = t.recall_remembered()
    assert back is not None and back.kind is None


@pytest.mark.parametrize("kind", ["klipper", "octoprint"])
def test_recall_keeps_a_known_kind(kind):
    _saved(kind=kind)
    assert t.recall_remembered().kind == kind


@pytest.mark.parametrize("field", ["profile", "host_type", "printer_model"])
@pytest.mark.parametrize("bad", [5, True, ["Test Printer"], {"name": "x"}])
def test_recall_reads_a_text_field_that_is_not_text_as_missing(field, bad):
    _saved(**{field: bad})
    back = t.recall_remembered()
    assert back is not None and getattr(back, field) is None


@pytest.mark.parametrize("content", ["[]", '"text"', "null", "5"])
def test_recall_ignores_a_file_that_is_not_an_object(content):
    t.REMEMBERED_PATH.write_text(content)
    assert t.recall_remembered() is None
