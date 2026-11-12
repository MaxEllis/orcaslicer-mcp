"""The panel in OrcaSlicer's language (orcaslicer-mcp #21): which language the plugin tells the page, OrcaSlicer's own
words for setting rows (from its translation file), the plugin's own phrases in the page's tables, and the
assistant's note rows (the build's mcp.17 `note` kind)."""
import json
import os
import re
import shutil
import struct
import subprocess

import pytest

import fake_orca
from conftest import call, mod

S = fake_orca.STATE
UI = fake_orca.UI


def write_mo(path, messages: dict) -> None:
    """A GNU gettext .mo file holding messages (what msgfmt makes of OrcaSlicer's .po files)."""
    entries = sorted({"": "Content-Type: text/plain; charset=UTF-8\n", **messages}.items())
    ids = [k.encode("utf-8") for k, _ in entries]
    strs = [v.encode("utf-8") for _, v in entries]
    n = len(entries)
    start = 28 + 16 * n
    blob, offsets = b"", []
    for s in ids + strs:
        offsets.append((len(s), start + len(blob)))
        blob += s + b"\0"
    head = struct.pack("<7I", 0x950412DE, 0, n, 28, 28 + 8 * n, 0, start)
    table = b"".join(struct.pack("<2I", *o) for o in offsets)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(head + table + blob)


CATALAN = {"Layer height": "Alçada de capa", "Wall loops": "Bucles de paret", "Seam position": "Posició de la costura",
           "Aligned": "Alineat", "Back": "Posterior", "Acceleration": "Acceleració", "Normal printing": "Impressió normal",
           "Nozzle temperature": "Temperatura del broquet", "Other layers": "Altres capes"}


@pytest.fixture
def resources(tmp_path):
    res = tmp_path / "resources"
    write_mo(str(res / "i18n" / "ca" / "OrcaSlicer.mo"), CATALAN)
    write_mo(str(res / "i18n" / "zh_TW" / "OrcaSlicer.mo"), {"Layer height": "層高"})
    write_mo(str(res / "i18n" / "zh_CN" / "OrcaSlicer.mo"), {"Layer height": "层高"})
    return str(res)


@pytest.mark.parametrize("app, c_locale, expected", [
    ("de_DE", "C", "de_DE"),
    ("de_DE", "de_DE.UTF-8", "de_DE"),
    ("ja_JP", "", "ja_JP"),
    ("pt_BR", "Portuguese_Brazil.1252", "pt_BR"),
    # app_info says en_US for Catalan, Basque and Hungarian: the C locale OrcaSlicer set tells them apart
    ("en_US", "ca_ES.UTF-8", "ca_ES"),
    ("en_US", "Catalan_Spain.1252", "ca_ES"),
    ("en_US", "ca-ES", "ca_ES"),
    ("en_US", "eu_ES.UTF-8", "eu_ES"),
    ("en_US", "Basque_Spain.1252", "eu_ES"),
    ("en_US", "hu_HU.UTF-8", "hu_HU"),
    ("en_US", "Hungarian_Hungary.1250", "hu_HU"),
    # ...and English stays English, whatever else the C locale says
    ("en_US", "English_United States.1252", ""),
    ("en_US", "en_GB.UTF-8", ""),
    ("en_US", "de_DE.UTF-8", ""),
    ("en_US", "C", ""),
    ("en_US", "", ""),
    # app_info says zh_CN for Traditional Chinese
    ("zh_CN", "zh_TW.UTF-8", "zh_TW"),
    ("zh_CN", "Chinese (Traditional)_Taiwan.950", "zh_TW"),
    ("zh_CN", "zh-Hant-TW", "zh_TW"),
    ("zh_CN", "zh_CN.UTF-8", "zh_CN"),
    ("zh_CN", "Chinese (Simplified)_China.936", "zh_CN"),
    # an older OrcaSlicer, or no app_info at all
    (None, "ca_ES.UTF-8", "ca_ES"),
    (None, "C", ""),
])
def test_ui_language(app, c_locale, expected):
    info = {"version": "2.5.0"} if app is None else {"version": "2.5.0", "language": app}
    assert mod.ui_language(info, c_locale) == expected


def test_c_locale_is_only_read():
    import locale
    before = {c: locale.setlocale(getattr(locale, c)) for c in ("LC_NUMERIC", "LC_TIME", "LC_COLLATE", "LC_MONETARY")}
    mod._c_locale_name()
    assert {c: locale.setlocale(getattr(locale, c)) for c in before} == before


def test_orcaslicer_words_from_its_translation_file(resources):
    w = mod.OrcaWords("ca_ES", [resources])  # no ca_ES folder: OrcaSlicer's is i18n/ca
    assert w.available
    assert w.word("Layer height") == "Alçada de capa" and w.word("Infill") == "Infill"
    assert w.setting_name("Wall loops") == "Bucles de paret"
    assert w.setting_name("Acceleration: Normal printing") == "Acceleració: Impressió normal"
    assert w.setting_name("Nozzle temperature \u2013 Other layers") == "Temperatura del broquet \u2013 Altres capes"
    assert w.setting_name("Infill") == ""  # nothing changed
    assert mod.OrcaWords("zh_TW", [resources]).word("Layer height") == "層高"  # the exact folder first
    missing = mod.OrcaWords("de_DE", [resources])
    assert not missing.available and missing.word("Layer height") == "Layer height"
    assert not mod.OrcaWords("", [resources]).available


def test_local_words_for_a_setting_row_and_its_choices(resources):
    w = mod.OrcaWords("ca_ES", [resources])
    row = {"kind": "setting", "label": "Seam position", "target": {"kind": "setting", "key": "seam_position"}}
    assert mod.local_words(row, w) == {"Seam position": "Posició de la costura", "Aligned": "Alineat",
                                       "Back": "Posterior"}
    slots = {"kind": "setting", "label": "Layer height (filaments 1, 3)", "target": {"kind": "setting", "key": "x"}}
    assert mod.local_words(slots, w) == {"Layer height": "Alçada de capa"}
    listed = {"kind": "refused", "label": "Layer height, Wall loops, post_process", "target": None}
    assert mod.local_words(listed, w) == {"Layer height": "Alçada de capa", "Wall loops": "Bucles de paret"}
    assert mod.local_words({"kind": "preset", "label": "Print preset", "target": None}, w) is None
    assert mod.local_words({"kind": "note", "label": "Layer height", "target": None}, w) is None
    assert mod.local_words(row, mod.OrcaWords("", [resources])) is None


def test_history_tells_the_page_the_language_and_orcaslicers_words(plugin, resources, monkeypatch):
    monkeypatch.setattr(mod, "resources_dirs", lambda: [resources])
    monkeypatch.setattr(mod, "ui_language", lambda info, c_locale=None: "ca_ES")
    plugin._ui_language = None
    call(plugin, "PUT", "/api/v1/config", {"layer_height": 0.28})
    call(plugin, "GET", "/api/v1/status")
    UI.run_blocking(plugin._send_all)
    h = [p for p in S.panels[-1].posts if p.get("type") == "history"][-1]
    assert h["capabilities"]["language"] == "ca_ES"
    row = h["rows"][-1]
    assert row["label"] == "Layer height" and row["local"] == {"Layer height": "Alçada de capa"}
    # GET /activity-like data stays English: the stored row has no local words
    assert "local" not in plugin.host.hist.rows[-1]


def test_language_is_read_on_the_ui_thread_and_kept(plugin, monkeypatch):
    seen = []
    monkeypatch.setattr(mod, "app_info", lambda: (seen.append(1), {"language": "de_DE"})[1])
    plugin._ui_language = None
    assert UI.run_blocking(plugin._language) == "de_DE"
    assert UI.run_blocking(plugin._language) == "de_DE" and len(seen) == 1
    monkeypatch.setattr(mod, "app_info", lambda: None)  # off the UI thread: nothing is kept
    plugin._ui_language = None
    assert plugin._language() == "" and plugin._ui_language is None


def test_page_draws_orcaslicers_words_in_the_language(plugin, resources, monkeypatch):
    """End to end: the shared page's own McpPanelCore, in Catalan, over the plugin's history."""
    node = shutil.which("node") or ("/snap/bin/node" if os.path.exists("/snap/bin/node") else None)
    if not node:
        pytest.skip("node not available")
    monkeypatch.setattr(mod, "resources_dirs", lambda: [resources])
    plugin.host.set_language("ca_ES")
    call(plugin, "PUT", "/api/v1/config", {"layer_height": 0.28})
    call(plugin, "PUT", "/api/v1/config", {"post_process": "x"})
    hist = plugin.host.history_msg()
    i18n = mod.PANEL_HTML.split('<script id="panel-i18n">', 1)[1].split("</script>", 1)[0]
    core = mod.PANEL_HTML.split('<script id="panel-core">', 1)[1].split("</script>", 1)[0]
    script = (
        "const vm=require('vm');const ctx={window:{}};vm.createContext(ctx);const src=require('fs').readFileSync(0,'utf8');"
        "const [a,b]=src.split('\\u0000');vm.runInContext(a,ctx);vm.runInContext(b,ctx);"
        "const C=ctx.window.McpPanelCore;const h=JSON.parse(process.argv[1]);C.setLanguage(h.capabilities.language);"
        "console.log(JSON.stringify(h.rows.map(r=>({label:C.rowLabel(r),value:C.valueText(r,r.value),"
        "where:C.ht(r.where,r)}))));")
    r = subprocess.run([node, "-e", script, json.dumps(hist)], input=(i18n + "\0" + core).encode(),
                       capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr.decode()
    setting, refused = json.loads(r.stdout)
    assert setting["label"] == "Alçada de capa"
    assert refused["value"] != "Not allowed"  # the build's own phrase, in the page's Catalan table
    assert refused["where"] != mod.NEVER_SENSITIVE  # the plugin's own phrase, in the page's tables too


def test_note_row_joins_the_open_group_without_undo(plugin):
    call(plugin, "PUT", "/api/v1/config", {"wall_loops": 3})
    status, body = call(plugin, "POST", "/api/v1/activity/note",
                        {"kind": "note", "text": "Thicker walls for the clip", "value": "3 loops"})
    assert (status, body) == (200, {"recorded": True})
    rows = plugin.host.history_msg()["rows"]
    assert (rows[-1]["kind"], rows[-1]["label"], rows[-1]["value"], rows[-1]["undo"]) == \
        ("note", "Thicker walls for the clip", "3 loops", None)
    assert rows[-1]["group"] == rows[-2]["group"]  # joins the change it explains
    call(plugin, "PUT", "/api/v1/config", {"wall_loops": 4})  # and does not end the group
    assert plugin.host.history_msg()["rows"][-1]["group"] == rows[-1]["group"]
    assert "note" in plugin.host.history_msg()["capabilities"]["kinds"]


def test_closed_banner_in_the_language(plugin):
    call(plugin, "GET", "/api/v1/status")
    plugin._ui_language = "de_DE"
    UI.run_blocking(S.panels[-1].close)
    text, link, _ = S.links[-1]
    assert text == mod.panel_phrase(mod.PANEL_CLOSED_TEXT, "de_DE") != mod.PANEL_CLOSED_TEXT
    assert link == mod.panel_phrase("Undo", "de_DE") != "Undo"


def test_panel_phrase_picks_a_table_like_the_page():
    assert mod.panel_phrase("Undo", "") == "Undo"
    assert mod.panel_phrase("Undo", "pt") == mod.panel_phrase("Undo", "pt_BR") != "Undo"
    assert mod.panel_phrase("Undo", "ca_ES") == mod.panel_phrase("Undo", "ca") != "Undo"
    assert mod.panel_phrase("not a panel phrase", "de_DE") == "not a panel phrase"
    assert mod.panel_phrase("Undo", "xx_YY") == "Undo"


# Names shown as they are in every language (companies and their apps).
BRANDS = {"Claude", "Claude Code", "OpenAI", "Cursor", "VS Code"}


def plugin_phrases() -> dict:
    """Every text the plugin hands the page or shows in its banner, by where it comes from."""
    out = {mod.NEVER_SENSITIVE: "NEVER_SENSITIVE", mod.NEEDS_BUILD: "NEEDS_BUILD", mod.SETTINGS_HINT: "SETTINGS_HINT",
           mod.PANEL_CLOSED_TEXT: "PANEL_CLOSED_TEXT", mod.CAPS["settings_label"]: "CAPS",
           mod.CAPS["off_text"]: "CAPS"}
    out.update({v: "REASONS" for v in mod.REASONS.values()})
    src = open(mod.__file__, encoding="utf-8").read()
    out.update({s: "undo result" for s in re.findall(r'_result\(row_id, "\w+", "([^"]+)"\)', src)})
    out.update({s: "reason" for s in re.findall(r'"reason": "([^"]+)"', src)})
    out.update({s: "verb" for s in re.findall(r'"undo_verb": "([^"]+)"', src)})
    for a in mod.setup_apps("D:/Orca Data/profile"):
        out.update({a[f]: "setup" for f in ("name", "where")})
        for r in a["routes"]:
            out.update({r[f]: "setup" for f in ("label", "hint", "act", "copy_label") if r[f]})
    for b in BRANDS:
        out.pop(b, None)
    return out


def test_every_plugin_phrase_is_in_the_pages_tables():
    """A new or reworded plugin text must go into the fork's i18n/external.json (and every language file), then the
    page is embedded again: otherwise it shows in English in a translated OrcaSlicer."""
    data = mod._panel_tables()
    keys, langs = data["keys"], data["langs"]
    templates = [re.compile("^" + re.sub(r"\\\{[a-z_]+\\\}", ".+", re.escape(k)) + "$") for k in data.get("host", [])]
    missing = [en for en in plugin_phrases() if en not in keys and not any(t.match(en) for t in templates)]
    assert missing == [], f"not in the page's tables: {missing}"
    assert len(langs) == 22
    for code, table in langs.items():
        empty = [en for en in plugin_phrases() if en in keys and not table[keys.index(en)]]
        assert empty == [], f"{code}: no translation for {empty}"
