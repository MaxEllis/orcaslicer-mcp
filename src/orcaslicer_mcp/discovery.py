"""Find OrcaSlicer on this computer without a token in the setup.

When ORCA_API_TOKEN is not set (and ORCA_API_URL, if set, names this computer), the connector looks in OrcaSlicer's
settings folder for the address and token itself, the way OrcaSlicer's own upstream MCP work lets its bridge read
`<datadir>/mcp/server.json`. So the line a user pastes into their AI app carries no secret.

Two sources, in this order:
1. The OrcaSlicer MCP plugin writes `connection.json` in its data folder while it runs and removes it when it stops:
   `<datadir>/orca_plugins/plugin_data/<plugin>/` for a plugin installed from a file, and
   `<datadir>/orca_plugins/plugin_data/_subscribed/<user>/<plugin>/` for one installed from OrcaCloud.
2. The OrcaSlicer MCP build keeps its Remote API settings in `<datadir>/OrcaSlicer.conf` (`app.remote_api_*`).

Only an address on this computer is ever used. When both are open (a stock OrcaSlicer with the plugin and the build,
each on its own port since plugin 0.2.4), the build comes first: it can do everything the plugin can and more. The
build counts as open when something listens on its port that no running plugin claims. A plugin file whose OrcaSlicer
is no longer running (it crashed or was killed before it could remove the file) comes after the build's settings, so
it can't hide an OrcaSlicer that is.
With a port to match (an ORCA_API_URL on this computer), the source running on that port is chosen.

The token only ever protects OrcaSlicer from browsers and other users of this computer; any program the user runs
can already read these files, so reading them here adds no new access.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

PLUGIN_GLOBS = ("orca_plugins/plugin_data/*/connection.json",
                "orca_plugins/plugin_data/_subscribed/*/*/connection.json")
PLUGIN_PRODUCT = "orcaslicer-mcp-plugin"
DEFAULT_PORT = 13130


@dataclass(frozen=True)
class Found:
    url: str
    token: str
    source: str  # "plugin" or "build"
    path: str
    running: bool = True  # False: the plugin's OrcaSlicer process is gone (a file left behind), or nothing serves the
    # build's port


def env_value(name: str) -> str:
    """An environment value, with an unexpanded placeholder (a Claude Desktop extension field left empty can arrive
    as the literal "${user_config.api_token}") read as not set."""
    v = os.environ.get(name, "").strip()
    return "" if v.startswith("${") and v.endswith("}") else v


def datadir_candidates() -> list[Path]:
    """OrcaSlicer's settings folders: ORCA_DATADIR if set (a custom --datadir), else the standard one per OS."""
    env = env_value("ORCA_DATADIR")
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


def pid_alive(pid) -> bool | None:
    """Whether a process with this id exists: True, False, or None when it can't be told."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return True if ctypes.get_last_error() == 5 else False  # access denied: it exists
            try:
                code = wintypes.DWORD()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return None
                return code.value == 259  # STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except (OSError, AttributeError, ValueError):
            return None
    try:
        os.kill(pid, 0)  # signal 0 only checks; never used on Windows, where it would send Ctrl+C
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _plugin_files(datadir: Path) -> list[tuple[float, Found]]:
    found = []
    for pattern in PLUGIN_GLOBS:
        for f in datadir.glob(pattern):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                mtime = f.stat().st_mtime
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("product") != PLUGIN_PRODUCT:
                continue
            url, token = str(data.get("url") or "").rstrip("/"), str(data.get("token") or "").strip()
            if not (url and token and names_this_computer(url)):
                continue  # never send a token to an address a file names elsewhere
            running = pid_alive(data.get("pid")) is not False
            found.append((mtime, Found(url, token, "plugin", str(f), running)))
    return found


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


_REFUSED: dict[int, float] = {}  # port -> until when its refusal stands
REFUSAL_KEPT_S = 10.0


def listening(port: int | None, timeout: float = 0.25) -> bool:
    """Something accepts connections on this computer's port (connected and closed at once, nothing sent). Every tool
    call asks, so a refusal is kept for a few seconds: Windows takes about 2 s to refuse a closed loopback port, and the
    timeout cuts that short (a server on this computer accepts in well under a millisecond)."""
    if not port:
        return False
    now = time.monotonic()
    if _REFUSED.get(port, 0.0) > now:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            _REFUSED.pop(port, None)
            return True
    except OSError:
        _REFUSED[port] = now + REFUSAL_KEPT_S
        return False


def candidates() -> list[Found]:
    """Every source found, best first: a running build, running plugins (newest file first), a build that isn't
    running, then plugin files whose OrcaSlicer is gone."""
    dirs = datadir_candidates()
    plugins = sorted((hit for d in dirs for hit in _plugin_files(d)), key=lambda t: t[0], reverse=True)
    live = [f for _, f in plugins if f.running]
    claimed = {port_of(f.url) for f in live}  # a port a running plugin serves is the plugin's, not the build's
    builds = [replace(b, running=port_of(b.url) not in claimed and listening(port_of(b.url)))
              for b in (_from_build(d) for d in dirs) if b]
    return [b for b in builds if b.running] + live + [b for b in builds if not b.running] + \
        [f for _, f in plugins if not f.running]


def discover(port: int | None = None) -> Found | None:
    """The best source; with a port, the best one running on that port. When nothing runs there, the source on that
    port is still chosen, except for the old default 13130 while another source runs: the Claude extension used to
    send that address always, and the plugin moved off it in 0.2.4."""
    found = candidates()
    if port is not None:
        on_port = [f for f in found if port_of(f.url) == port]
        running = [f for f in on_port if f.running]
        if running:
            return running[0]
        if on_port and not (port == DEFAULT_PORT and any(f.running for f in found)):
            return on_port[0]
    return found[0] if found else None


def port_of(url: str) -> int | None:
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
        return parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    except ValueError:
        return None


def names_this_computer(url: str) -> bool:
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("127.0.0.1", "localhost", "::1")
