"""Find OrcaSlicer on this computer without a token in the setup.

When ORCA_API_TOKEN is not set (and ORCA_API_URL, if set, names this computer), the connector looks in OrcaSlicer's
settings folder for the address and token itself, the way OrcaSlicer's own upstream MCP work lets its bridge read
`<datadir>/mcp/server.json`. So the line a user pastes into their AI app carries no secret.

Two sources, in this order:
1. The OrcaSlicer MCP plugin writes `connection.json` in its data folder while it runs
   (`<datadir>/orca_plugins/plugin_data/<plugin>/connection.json`) and removes it when it stops.
2. The OrcaSlicer MCP build keeps its Remote API settings in `<datadir>/OrcaSlicer.conf` (`app.remote_api_*`).

The token only ever protects OrcaSlicer from browsers and other users of this computer; any program the user runs
can already read these files, so reading them here adds no new access.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

PLUGIN_GLOB = "orca_plugins/plugin_data/*/connection.json"
PLUGIN_PRODUCT = "orcaslicer-mcp-plugin"
DEFAULT_PORT = 13130


@dataclass(frozen=True)
class Found:
    url: str
    token: str
    source: str  # "plugin" or "build"
    path: str


def datadir_candidates() -> list[Path]:
    """OrcaSlicer's settings folders: ORCA_DATADIR if set (a custom --datadir), else the standard one per OS."""
    env = os.environ.get("ORCA_DATADIR", "").strip()
    if env:
        return [Path(env).expanduser()]
    home = Path.home()
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        return [Path(appdata) / "OrcaSlicer"] if appdata else [home / "AppData" / "Roaming" / "OrcaSlicer"]
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "OrcaSlicer"]
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return [xdg / "OrcaSlicer",
            home / ".var" / "app" / "com.orcaslicer.OrcaSlicer" / "config" / "OrcaSlicer",
            home / ".var" / "app" / "io.github.softfever.OrcaSlicer" / "config" / "OrcaSlicer"]


def _from_plugin(datadir: Path) -> Found | None:
    best = None
    for f in datadir.glob(PLUGIN_GLOB):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            mtime = f.stat().st_mtime
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("product") != PLUGIN_PRODUCT:
            continue
        url, token = str(data.get("url") or "").rstrip("/"), str(data.get("token") or "").strip()
        if url and token and (best is None or mtime > best[0]):
            best = (mtime, Found(url, token, "plugin", str(f)))
    return best[1] if best else None


def _read_appconfig(path: Path) -> dict:
    """OrcaSlicer.conf is JSON followed by a '# MD5 checksum ...' line."""
    text = path.read_text(encoding="utf-8", errors="replace")
    body = "\n".join(line for line in text.splitlines() if not line.startswith("# MD5 checksum"))
    data = json.loads(body)
    return data if isinstance(data, dict) else {}


def _from_build(datadir: Path) -> Found | None:
    conf = datadir / "OrcaSlicer.conf"
    try:
        app = _read_appconfig(conf).get("app") or {}
    except (OSError, ValueError):
        return None
    if str(app.get("remote_api_enabled", "")).lower() not in ("1", "true"):
        return None
    token = str(app.get("remote_api_token") or "").strip()
    try:
        port = int(str(app.get("remote_api_port") or DEFAULT_PORT))
    except ValueError:
        port = DEFAULT_PORT
    return Found(f"http://127.0.0.1:{port}", token, "build", str(conf)) if token else None


def discover() -> Found | None:
    dirs = datadir_candidates()
    for d in dirs:
        hit = _from_plugin(d)
        if hit:
            return hit
    for d in dirs:
        hit = _from_build(d)
        if hit:
            return hit
    return None


def names_this_computer(url: str) -> bool:
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")
