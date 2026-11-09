#!/usr/bin/env python3
"""Write the two generated blocks inside orcaslicer_mcp_plugin.py (the plugin must stay one file):

  PANEL_HTML  the shared OrcaSlicer MCP panel page, byte for byte, from the fork
              (resources/web/orcaslicer_mcp/panel.html on remote-api-250);
  LABELS      OrcaSlicer 2.5's setting labels and units, from orcaslicer-mcp's settings schema
              (full_label, else label, plus sidetext: the fork's api_config_label/api_config_unit);
  VALUE_KINDS which settings are On/Off switches and which show an enum label instead of the stored
              value (the fork's McpRecorder::config_value_text), from the same schema.

Both are written as plain, readable Python literals, never encoded. tests/test_embedded.py fails when
either block differs from its source.

Usage: python3 -I tools/embed.py <panel.html> <print_settings_schema.json>
"""
import json
import pathlib
import re
import sys

PLUGIN = pathlib.Path(__file__).resolve().parent.parent / "orcaslicer_mcp_plugin.py"


def _lit(s: str) -> str:
    # A JSON string with ensure_ascii=False is also a valid Python string literal.
    return json.dumps(s, ensure_ascii=False)


def panel_block(html: str) -> str:
    lines = html.splitlines(keepends=True)
    body = "\n".join("    " + _lit(line) for line in lines)
    return "PANEL_HTML = (\n" + body + "\n)\n"


def _decode(text):
    if not text:
        return ""
    return text.encode("utf-8").decode("unicode_escape") if "\\u" in text else text


def label_map(schema: dict) -> dict:
    out = {}
    for key, entry in sorted(schema.items()):
        if key.startswith("_") or not isinstance(entry, dict) or not entry.get("label"):
            continue
        # full_label when OrcaSlicer has one ("Nozzle temperature", not the tab's "Other layers"),
        # else label: the same rule as the build's api_config_label (ruling R16).
        label = entry.get("full_label") or entry["label"]
        out[key] = (_decode(label), _decode(entry.get("unit") or ""))
    return out


def value_kinds(schema: dict) -> dict:
    """key -> "bool" for an On/Off switch (coBool), or {stored value: label} for an enum whose values and
    labels line up (coEnum): what config_value_text shows instead of the raw value."""
    out = {}
    for key, entry in sorted(schema.items()):
        if key.startswith("_") or not isinstance(entry, dict):
            continue
        if entry.get("type") == "coBool":
            out[key] = "bool"
        elif entry.get("type") == "coEnum":
            values, labels = entry.get("enum_values") or [], entry.get("enum_labels") or []
            if values and len(values) == len(labels):
                out[key] = {v: _decode(l) for v, l in zip(values, labels) if l}
    return out


def value_kinds_block(kinds: dict) -> str:
    lines = []
    for k, v in kinds.items():
        if v == "bool":
            lines.append(f"    {_lit(k)}: \"bool\",")
        else:
            inner = ", ".join(f"{_lit(a)}: {_lit(b)}" for a, b in v.items())
            lines.append(f"    {_lit(k)}: {{{inner}}},")
    return "VALUE_KINDS = {\n" + "\n".join(lines) + "\n}\n"


def labels_block(labels: dict) -> str:
    body = "\n".join(f"    {_lit(k)}: ({_lit(l)}, {_lit(u)})," for k, (l, u) in labels.items())
    return "LABELS = {\n" + body + "\n}\n"


def replace_block(source: str, name: str, block: str) -> str:
    begin, end = f"# >>> generated {name} (tools/embed.py)\n", f"# <<< generated {name}\n"
    pattern = re.compile(re.escape(begin) + ".*?" + re.escape(end), re.S)
    if not pattern.search(source):
        raise SystemExit(f"markers for {name} not found in {PLUGIN.name}")
    return pattern.sub(lambda _m: begin + block + end, source)


def main(panel_path: str, schema_path: str) -> None:
    html = pathlib.Path(panel_path).read_text(encoding="utf-8")
    schema = json.loads(pathlib.Path(schema_path).read_text(encoding="utf-8"))
    src = PLUGIN.read_text(encoding="utf-8")
    src = replace_block(src, "PANEL_HTML", panel_block(html))
    src = replace_block(src, "LABELS", labels_block(label_map(schema)))
    src = replace_block(src, "VALUE_KINDS", value_kinds_block(value_kinds(schema)))
    PLUGIN.write_text(src, encoding="utf-8")
    print(f"embedded panel ({len(html)} chars), {len(label_map(schema))} labels and "
          f"{len(value_kinds(schema))} value kinds")


if __name__ == "__main__":
    main(*sys.argv[1:3])
