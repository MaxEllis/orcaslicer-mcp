"""Which printer to talk to, and in which protocol.

Order: ORCA_PRINTER_URL, else OrcaSlicer's active printer profile (read through the fork), else
the printer that last answered (remembered in ~/.orcaslicer-mcp/printer.json and used only while
OrcaSlicer can't be reached)."""
from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from .errors import PrinterError

REMEMBERED_PATH = Path.home() / ".orcaslicer-mcp" / "printer.json"

# OrcaSlicer host_type values we cannot read status from yet, with the name users see.
UNSUPPORTED_HOST_TYPES = {
    "prusalink": "PrusaLink", "prusaconnect": "Prusa Connect", "duet": "Duet", "flashair": "FlashAir",
    "astrobox": "AstroBox", "repetier": "Repetier", "mks": "MKS", "esp3d": "ESP3D", "obico": "Obico",
    "flashforge": "Flashforge", "simplyprint": "SimplyPrint", "3dprinteros": "3DPrinterOS",
}


@dataclass(frozen=True)
class PrinterTarget:
    url: str                              # base URL, no userinfo, no trailing slash: safe to report
    source: str                           # "override" | "profile" | "remembered"
    profile: str | None = None
    host_type: str | None = None
    printer_model: str | None = None
    kind: str | None = None               # "klipper" | "octoprint" once a probe answered
    remembered_at: float | None = None
    auth: tuple[str, str] | None = field(default=None, repr=False)  # basic auth from ORCA_PRINTER_URL; never reported or repr'd

    def public(self) -> dict:
        """What the model may see. Never the auth pair."""
        return {"profile": self.profile, "url": self.url, "kind": self.kind,
                "source": self.source, "remembered_at": self.remembered_at}


def normalise_url(raw: str) -> tuple[str, tuple[str, str] | None]:
    """'192.0.2.10' -> ('http://192.0.2.10', None). Userinfo comes back separately; a '<redacted>@'
    left by credential redaction is dropped. Query strings and the trailing slash are dropped."""
    s = (raw or "").strip()
    if not s:
        return "", None
    if "://" not in s:
        s = "http://" + s
    parts = urlsplit(s)
    netloc, auth = parts.netloc, None
    if "@" in netloc:
        userinfo, netloc = netloc.rsplit("@", 1)
        if userinfo and "<redacted>" not in userinfo:
            user, _, password = userinfo.partition(":")
            auth = (unquote(user), unquote(password))
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", "")), auth


def check_host_type(host_type: str | None) -> None:
    ht = (host_type or "").strip().lower()
    if ht in UNSUPPORTED_HOST_TYPES:
        raise PrinterError(
            "unsupported_connection",
            f"The printer profile connects via {UNSUPPORTED_HOST_TYPES[ht]}, which isn't supported yet. "
            "Klipper (Moonraker) and OctoPrint are.",
            host_type=ht)


def remember(target: PrinterTarget) -> None:
    """Save the printer that answered, without credentials. A convenience: failures are ignored."""
    try:
        REMEMBERED_PATH.parent.mkdir(parents=True, exist_ok=True)
        REMEMBERED_PATH.write_text(json.dumps({
            "url": target.url, "kind": target.kind, "profile": target.profile,
            "host_type": target.host_type, "printer_model": target.printer_model, "found_at": time.time()}))
    except OSError:
        pass


def recall_remembered() -> PrinterTarget | None:
    try:
        d = json.loads(REMEMBERED_PATH.read_text())
    except (OSError, ValueError):
        return None
    url = d.get("url") if isinstance(d, dict) else None
    if not isinstance(url, str) or not url:
        return None
    return PrinterTarget(url=url, source="remembered", profile=d.get("profile"), host_type=d.get("host_type"),
                         printer_model=d.get("printer_model"), kind=d.get("kind"), remembered_at=d.get("found_at"))


def printer_id_for(target: PrinterTarget) -> str:
    env = os.environ.get("ORCA_PRINTER_ID", "").strip()
    if env:
        return env
    if target.profile:
        return target.profile
    return urlsplit(target.url).hostname or target.url
