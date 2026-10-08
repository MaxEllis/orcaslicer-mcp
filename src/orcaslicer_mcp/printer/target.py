"""Which printer to talk to, and in which protocol.

Order: ORCA_PRINTER_URL, else OrcaSlicer's active printer profile (read through the fork), else
the printer that last answered (remembered in ~/.orcaslicer-mcp/printer.json and used only while
OrcaSlicer can't be reached)."""
from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from ..errors import ApiError, ConfigError, Unauthorized
from .errors import PrinterError
from .moonraker import MoonrakerClient
from .octoprint import OctoPrintClient

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


SET_URL_HINT = ("set ORCA_PRINTER_URL in this MCP server's settings to an address that works from here, "
                "for example the printer's IP address.")
TOKEN_HINT = ("Check ORCA_API_TOKEN in this MCP server's settings: it must match the token on the Remote API "
              "page of OrcaSlicer's Preferences.")


def token_problem(e: ApiError) -> bool:
    """True when OrcaSlicer refused or lacks the token (the fix is the token), False when it is simply not
    reachable (the fix is to start it)."""
    return isinstance(e, (ConfigError, Unauthorized))


_PROFILE_ADDRESS_HINT = ("In OrcaSlicer, open the connection settings next to the printer and enter "
                         "its address, or " + SET_URL_HINT)
_OVERRIDE_ADDRESS_HINT = ("Correct ORCA_PRINTER_URL in this MCP server's settings, or remove it to use "
                          "the printer profile in OrcaSlicer.")

# Process-wide memory of which protocol and base URL answered for a target URL, so repeat calls
# skip the probes that failed. Cleared by tests; a failed cached probe falls through to the rest.
_PROBE_CACHE: dict[str, tuple[str, str]] = {}


def _shown(raw: str) -> str:
    """The address as it may appear in a message: everything between '://' and the last '@' is
    hidden, so a password can never reach the model or a log line."""
    s = (raw or "").strip()
    if "@" not in s:
        return s
    scheme, sep, rest = s.partition("://")
    tail = rest.rsplit("@", 1)[1] if sep else s.rsplit("@", 1)[1]
    return f"{scheme}://<redacted>@{tail}" if sep else f"<redacted>@{tail}"


def _origin(source: str, profile: str | None) -> str:
    if source == "override":
        return "ORCA_PRINTER_URL"
    if source == "profile":
        return f"the printer profile '{profile}'" if profile else "the printer profile"
    return "the remembered printer"


def _bad_address(raw: str, source: str, profile: str | None = None, *, encoding: bool = False) -> PrinterError:
    shown, origin = _shown(raw), _origin(source, profile)
    if encoding:
        message = (f"The user name or password in the printer address '{shown}' from {origin} contains "
                   "characters that must be percent-encoded (for example / ? # @).")
    else:
        message = f"The printer address '{shown}' from {origin} isn't a valid URL."
    hint = _OVERRIDE_ADDRESS_HINT if source == "override" else _PROFILE_ADDRESS_HINT
    return PrinterError("not_configured", message, hint=hint)


def _parse_address(raw: str, source: str, profile: str | None = None) -> tuple[str, tuple[str, str] | None]:
    """normalise_url for an address that came from a person: anything urllib cannot handle becomes
    a not_configured PrinterError (a ValueError never escapes), and nothing is echoed unredacted."""
    s = raw.strip()
    try:
        parts = urlsplit(s if "://" in s else "http://" + s)
        # An '@' outside the authority means a '/', '?' or '#' in the user name or password was not
        # percent-encoded: urllib would drop or misread the rest, e.g. 'http://user:123?x@host'.
        loose_at = "@" in parts.path + parts.query + parts.fragment
    except ValueError:
        raise _bad_address(raw, source, profile) from None
    if loose_at:
        raise _bad_address(raw, source, profile, encoding=True)
    try:
        url, auth = normalise_url(raw)
        checked = urlsplit(url)
        checked.port  # raises ValueError for a non-numeric or out-of-range port
        valid = bool(checked.hostname)
    except ValueError:
        valid = False
    if not valid:
        raise _bad_address(raw, source, profile)
    return url, auth


async def resolve_target(fork_factory) -> PrinterTarget:
    """Find the printer: ORCA_PRINTER_URL, else OrcaSlicer's active printer profile, else the
    printer remembered from the last time OrcaSlicer could be read."""
    override = os.environ.get("ORCA_PRINTER_URL", "").strip()
    if override:
        url, auth = _parse_address(override, "override")
        return PrinterTarget(url=url, source="override", auth=auth)
    try:
        async with fork_factory() as fork:
            status = await fork.get_status()
            name = ((status or {}).get("presets") or {}).get("printer")
            if not name:
                raise PrinterError("not_configured", "OrcaSlicer reports no active printer profile.",
                                   hint="Select a printer in OrcaSlicer, or " + SET_URL_HINT)
            preset = await fork.get_preset_config("printer", name)
    except ApiError as e:
        remembered = recall_remembered()
        if remembered is not None:
            return remembered
        raise PrinterError("orca_unreachable",
                           "Couldn't read the printer profile from OrcaSlicer, and no printer has answered before.",
                           hint=(TOKEN_HINT if token_problem(e) else
                                 "Start OrcaSlicer (MCP build) with the Remote API enabled, or " + SET_URL_HINT),
                           detail=str(e)) from e
    cfg = (preset or {}).get("config") or {}
    host = str(cfg.get("print_host") or "").strip()
    model = cfg.get("printer_model") or None
    host_type = str(cfg.get("host_type") or "").strip().lower() or None
    if not host:
        if (model or "").startswith("Bambu Lab"):
            raise PrinterError("unsupported_connection", "Bambu printers aren't supported yet (it's planned).",
                               host_type="bambu")
        raise PrinterError("not_configured", f"The printer profile '{name}' has no connection set up.",
                           hint=_PROFILE_ADDRESS_HINT)
    check_host_type(host_type)
    url, auth = _parse_address(host, "profile", name)
    return PrinterTarget(url=url, source="profile", profile=name, host_type=host_type,
                         printer_model=model, auth=auth)


def _candidates(url: str, kind_hint: str | None) -> list[tuple[str, str]]:
    parts = urlsplit(url)
    out = [("klipper", url)]
    if parts.port != 7125 and parts.hostname:
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        out.append(("klipper", urlunsplit((parts.scheme or "http", f"{host}:7125", "", "", ""))))
    out.append(("octoprint", url))
    if kind_hint:
        out.sort(key=lambda c: c[0] != kind_hint)  # stable: the remembered protocol goes first
    return out


def _unreachable_hint(target: PrinterTarget) -> str:
    hint = ("Is the printer switched on and on the same network as this computer? If OrcaSlicer's "
            "address only works from another machine, " + SET_URL_HINT)
    if "centauri" in (target.printer_model or "").lower():
        hint += " Elegoo Centauri printers use a different protocol (SDCP) that isn't supported yet."
    return hint


async def open_printer(target: PrinterTarget, api_key: str | None = None):
    """Return (target with kind and url set to what answered, an open client). The caller closes the
    client. Tries Moonraker as given, Moonraker on 7125, then OctoPrint, cached per target URL."""
    try:
        candidates = _candidates(target.url, target.kind)
    except ValueError:  # a bad port or bracket in a target that did not come through resolve_target
        raise _bad_address(target.url, target.source, target.profile) from None
    cached = _PROBE_CACHE.get(target.url)
    if cached:
        candidates = [cached] + [c for c in candidates if c != cached]
    tried: list[str] = []
    auth_err: PrinterError | None = None
    for kind, base in candidates:
        cls = MoonrakerClient if kind == "klipper" else OctoPrintClient
        client = cls(base, api_key=api_key, auth=target.auth)
        try:
            ok = await client.identify()
        except PrinterError as e:  # only auth errors escape identify()
            ok = False
            auth_err = auth_err or e
        if ok:
            _PROBE_CACHE[target.url] = (kind, base)
            found = replace(target, kind=kind, url=base)
            if target.source == "profile":
                remember(found)
            return found, client
        await client.aclose()
        tried.append(f"{base} ({'Klipper' if kind == 'klipper' else 'OctoPrint'})")
    if auth_err is not None:
        raise auth_err
    raise PrinterError("not_reachable", f"The printer didn't answer at {target.url}.",
                       hint=_unreachable_hint(target), tried=tried)
