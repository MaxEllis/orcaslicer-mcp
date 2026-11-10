import os
from dataclasses import dataclass

from .discovery import discover, names_this_computer

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


def _env(name: str) -> str:
    """An environment value, with an unexpanded placeholder (a Claude Desktop extension field left empty can arrive
    as the literal "${user_config.api_token}") read as not set."""
    v = os.environ.get(name, "").strip()
    return "" if v.startswith("${") and v.endswith("}") else v


def load_config() -> Config:
    token = _env("ORCA_API_TOKEN")
    url = _env("ORCA_API_URL").rstrip("/")
    timeout = float(os.environ.get("ORCA_API_TIMEOUT", "30"))
    if token:
        return Config(base_url=url or "http://127.0.0.1:13130", token=token, timeout=timeout)
    # No token: find OrcaSlicer on this computer (discovery.py). Never for an address elsewhere, whose token
    # this computer's files can't know.
    if not url or names_this_computer(url):
        found = discover()
        if found:
            return Config(base_url=url or found.url, token=found.token, timeout=timeout, source=found.source)
    raise RuntimeError(NOT_FOUND)
