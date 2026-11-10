"""Tells a user whose OrcaSlicer MCP plugin is older than the newest one.

A plugin installed from a downloaded .py file never updates itself (only an OrcaCloud install does), and the
plugin asks for no network access, so this server, which updates through PyPI, does the telling. The OrcaSlicer
MCP build is never flagged: it has its own update notifier.

LATEST_PLUGIN_VERSION is the plugin version released with this version of orcaslicer-mcp. Raise it together
with the plugin's own version (its PEP 723 header); tests/test_plugin_update.py checks the two agree when the
plugin source is next to this checkout.
"""
from __future__ import annotations

import re

LATEST_PLUGIN_VERSION = "0.2.1"
PLUGIN_DOWNLOAD_URL = "https://github.com/MaxEllis/orcaslicer-mcp/releases/latest"

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_version(v) -> tuple[int, int, int] | None:
    """X.Y.Z as a tuple; None for anything else (then nothing is said)."""
    m = _VERSION.fullmatch(str(v).strip()) if isinstance(v, str) else None
    return tuple(int(x) for x in m.groups()) if m else None


def plugin_update(status: dict, latest: str = LATEST_PLUGIN_VERSION) -> dict | None:
    """{installed, latest, url, note} when `status` comes from an OrcaSlicer MCP plugin older than `latest`;
    None for the build, an up-to-date plugin, or a version that can't be read."""
    if not isinstance(status, dict) or status.get("backend") != "plugin":
        return None
    installed = status.get("plugin_version")
    have, newest = parse_version(installed), parse_version(latest)
    if have is None or newest is None or have >= newest:
        return None
    return {"installed": installed, "latest": latest, "url": PLUGIN_DOWNLOAD_URL,
            "note": (f"Your OrcaSlicer MCP plugin is version {installed}; version {latest} is out. "
                     f"Download it from {PLUGIN_DOWNLOAD_URL}, or install it from OrcaCloud to get "
                     "updates automatically.")}
