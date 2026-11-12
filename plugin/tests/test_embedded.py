"""The plugin is one file, so it carries copies: the shared panel page (home: the OrcaSlicer MCP build,
resources/web/orcaslicer_mcp/panel.html on remote-api-250) and OrcaSlicer's setting labels (from
orcaslicer-mcp's settings schema, with which settings are switches or enums). These tests fail when a copy differs from its source; fix it with
`python3 -I tools/embed.py <panel.html> <print_settings_schema.json>`.

Sources, first found wins: MCP_PANEL_SRC (a file), else PANEL_COMMIT (the page that is final for the
2.5 release, remote-api-250), else the branch remote-api-250 (origin first), in a clone of the fork named by
MCP_FORK_REPO (default: OrcaSlicer next to this repo). Move PANEL_COMMIT forward when the fork changes the page. The schema comes
from MCP_SRC/orcaslicer_mcp/data.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

from conftest import MCP_SRC, ROOT, mod

sys.path.insert(0, str(ROOT / "tools"))
import embed  # noqa: E402

RELAY = pathlib.Path(os.environ.get("MCP_FORK_REPO") or ROOT.parent.parent / "OrcaSlicer")
PANEL_PATH = "resources/web/orcaslicer_mcp/panel.html"
PANEL_COMMIT = "c63814a6f3fcd1e3241333f2ac509ab41a0142d7"  # remote-api-250: the plugin's phrases + setup boxes


def fork_panel():
    src = os.environ.get("MCP_PANEL_SRC")
    if src:
        return pathlib.Path(src).read_text(encoding="utf-8"), src
    for ref in (PANEL_COMMIT, "origin/remote-api-250", "remote-api-250"):
        r = subprocess.run(["git", "-C", str(RELAY), "show", f"{ref}:{PANEL_PATH}"], capture_output=True)
        if r.returncode == 0:
            return r.stdout.decode("utf-8"), ref
    pytest.skip("the fork's panel.html is not reachable (set MCP_FORK_REPO or MCP_PANEL_SRC)")


def test_panel_matches_the_fork():
    html, where = fork_panel()
    assert mod.PANEL_HTML == html, f"PANEL_HTML differs from {where}: run tools/embed.py"


def test_protocol_version_is_the_one_this_host_implements():
    m = re.search(r"var PROTOCOL_VERSION = (\d+);", mod.PANEL_HTML)
    assert m, "PROTOCOL_VERSION not found in the page"
    assert int(m.group(1)) == mod.PANEL_PROTOCOL_VERSION, \
        "the page's protocol changed: update the host before shipping this copy"


def test_labels_match_the_schema():
    schema_path = MCP_SRC / "orcaslicer_mcp" / "data" / "print_settings_schema.json"
    if not schema_path.exists():
        pytest.skip("orcaslicer-mcp's settings schema not found")
    expected = embed.label_map(json.loads(schema_path.read_text(encoding="utf-8")))
    assert mod.LABELS == expected, "LABELS differ from the schema: run tools/embed.py"


def test_value_kinds_match_the_schema():
    schema_path = MCP_SRC / "orcaslicer_mcp" / "data" / "print_settings_schema.json"
    if not schema_path.exists():
        pytest.skip("orcaslicer-mcp's settings schema not found")
    expected = embed.value_kinds(json.loads(schema_path.read_text(encoding="utf-8")))
    assert mod.VALUE_KINDS == expected, "VALUE_KINDS differ from the schema: run tools/embed.py"


def test_page_core_groups_and_titles_plugin_rows(plugin):
    """Run the shared page's own McpPanelCore (node) over history the plugin produced."""
    node = shutil.which("node") or ("/snap/bin/node" if os.path.exists("/snap/bin/node") else None)
    if not node:
        pytest.skip("node not available")
    from conftest import call
    call(plugin, "PUT", "/api/v1/config", {"layer_height": 0.28, "wall_loops": 3})
    call(plugin, "PUT", "/api/v1/config", {"post_process": "x"})
    hist = plugin.host.history_msg()
    core = mod.PANEL_HTML.split('<script id="panel-core">', 1)[1].split("</script>", 1)[0]
    script = (
        "const vm=require('vm');const ctx={window:{}};vm.createContext(ctx);"
        "vm.runInContext(require('fs').readFileSync(0,'utf8'),ctx);"
        "const C=ctx.window.McpPanelCore;const h=JSON.parse(process.argv[1]);"
        "const gs=C.groupRows({rows:h.rows,groups:h.groups});"
        "console.log(JSON.stringify(gs.map(g=>({id:g.id,title:C.groupTitle(g),warn:C.groupResult(g).warn}))));")
    r = subprocess.run([node, "-e", script, json.dumps(hist)], input=core.encode(), capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr.decode()
    view = json.loads(r.stdout)
    assert view == [{"id": 1, "title": "Layer height and 1 more", "warn": True}]


def test_page_core_never_blames_the_user_for_a_superseded_plugin_row(plugin):
    """R43 end to end: the plugin's history + live, read by the shared page's own McpPanelCore."""
    node = shutil.which("node") or ("/snap/bin/node" if os.path.exists("/snap/bin/node") else None)
    if not node:
        pytest.skip("node not available")
    from conftest import call
    call(plugin, "PUT", "/api/v1/config", {"outer_wall_speed": 40})
    call(plugin, "PUT", "/api/v1/config", {"outer_wall_speed": 90})
    hist = plugin.host.history_msg()
    live = plugin._pump.call(plugin.host.live_values)
    core = mod.PANEL_HTML.split('<script id="panel-core">', 1)[1].split("</script>", 1)[0]
    script = (
        "const vm=require('vm');const ctx={window:{}};vm.createContext(ctx);"
        "vm.runInContext(require('fs').readFileSync(0,'utf8'),ctx);"
        "const C=ctx.window.McpPanelCore;const h=JSON.parse(process.argv[1]);const live=JSON.parse(process.argv[2]);"
        "const t=h.capabilities.tracked_live===true;"
        "console.log(JSON.stringify(h.rows.map(r=>({d:C.liveDisplay(r,live,true,t),u:C.undoState(r,live,true,t)}))));")
    r = subprocess.run([node, "-e", script, json.dumps(hist), json.dumps(live)], input=core.encode(),
                       capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr.decode()
    first, second = json.loads(r.stdout)
    assert first["d"]["note"] == "" and first["u"]["available"] is False
    assert first["u"]["why"].startswith("Changed again since")
    assert second["d"] == {"value": "90 mm/s", "note": ""} and second["u"]["available"] is True


def test_page_core_says_set_back_by_you_for_a_plugin_row(plugin):
    """afd2f92b89: the user resets a setting the assistant changed (OrcaSlicer's reset arrow); the plugin's live
    value must come back in the same form as the row's `before` for the page to say "set back by you"."""
    node = shutil.which("node") or ("/snap/bin/node" if os.path.exists("/snap/bin/node") else None)
    if not node:
        pytest.skip("node not available")
    import fake_orca
    from conftest import call
    call(plugin, "PUT", "/api/v1/config", {"layer_height": 0.28})
    fake_orca.STATE.bundle.prints._by("0.20mm Standard @Fake")._cfg["layer_height"] = "0.2"  # reset by hand
    hist = plugin.host.history_msg()
    live = plugin._pump.call(plugin.host.live_values)
    core = mod.PANEL_HTML.split('<script id="panel-core">', 1)[1].split("</script>", 1)[0]
    script = (
        "const vm=require('vm');const ctx={window:{}};vm.createContext(ctx);"
        "vm.runInContext(require('fs').readFileSync(0,'utf8'),ctx);"
        "const C=ctx.window.McpPanelCore;const h=JSON.parse(process.argv[1]);const live=JSON.parse(process.argv[2]);"
        "const r=h.rows[0];console.log(JSON.stringify({d:C.liveDisplay(r,live,true,true),u:C.undoState(r,live,true,true)}));")
    r = subprocess.run([node, "-e", script, json.dumps(hist), json.dumps(live)], input=core.encode(),
                       capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr.decode()
    out = json.loads(r.stdout)
    assert out["d"] == {"value": "0.2 mm", "note": "set back by you"}
    assert out["u"] == {"available": False, "why": "Already back to 0.2 mm"}
