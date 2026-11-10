import os
from dataclasses import dataclass

from .discovery import discover, env_value as _env, names_this_computer, port_of

NOT_FOUND = ("ORCA_API_TOKEN is not set and OrcaSlicer wasn't found on this computer: open OrcaSlicer with the "
             "OrcaSlicer MCP plugin activated (or the OrcaSlicer MCP build with its Remote API on). If OrcaSlicer uses "
             "its own settings folder (--datadir), set ORCA_DATADIR to it; for OrcaSlicer on another computer set "
             "ORCA_API_URL and ORCA_API_TOKEN.")


@dataclass(frozen=True)
class Config:
    base_url: str
    token: str
    timeout: float
    source: str = "env"  # env, plugin or build: where the token came from


def load_config() -> Config:
    token = _env("ORCA_API_TOKEN")
    url = _env("ORCA_API_URL").rstrip("/")
    timeout = float(os.environ.get("ORCA_API_TIMEOUT", "30"))
    if token:
        return Config(base_url=url or "http://127.0.0.1:13130", token=token, timeout=timeout)
    # No token: find OrcaSlicer on this computer (discovery.py). Never for an address elsewhere, whose token
    # this computer's files can't know.
    # A local ORCA_API_URL picks the source on its port; when none is there (an extension that still sends the old
    # default 13130 while the plugin moved to another port), the address found wins, since the token belongs to it.
    if not url or names_this_computer(url):
        found = discover(port_of(url) if url else None)
        if found:
            base = url if url and port_of(url) == port_of(found.url) else found.url
            return Config(base_url=base, token=found.token, timeout=timeout, source=found.source)
    raise RuntimeError(NOT_FOUND)
