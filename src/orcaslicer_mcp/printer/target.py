"""Which printer to talk to, and in which protocol.

Order: ORCA_PRINTER_URL, else OrcaSlicer's active printer profile (read through the fork), else
the printer that last answered (remembered in ~/.orcaslicer-mcp/printer.json and used only while
OrcaSlicer can't be reached)."""
from __future__ import annotations
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from ..errors import ApiError, ConfigError, Unauthorized
from ..guard import REDACTED, _strip_userinfo
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
    left by credential redaction is dropped. Query strings, fragments and the trailing slash are dropped."""
    s = (raw or "").strip()
    if not s:
        return "", None
    if "://" not in s:
        s = "http://" + s
    parts = urlsplit(s)
    netloc, auth = parts.netloc, None
    if "@" in netloc:
        userinfo, netloc = netloc.rsplit("@", 1)
        if userinfo and REDACTED not in userinfo:
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
    """Save the printer that answered, without credentials. A convenience: failures are ignored.
    Written to a temp file beside the real one and renamed over it, so a crash or a full disk never
    leaves a half-written file for the next start to read."""
    tmp = None
    try:
        REMEMBERED_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=REMEMBERED_PATH.parent, prefix=REMEMBERED_PATH.name + ".", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump({"url": target.url, "kind": target.kind, "profile": target.profile,
                       "host_type": target.host_type, "printer_model": target.printer_model,
                       "found_at": time.time()}, f)
        os.replace(tmp, REMEMBERED_PATH)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _text_or_none(v) -> str | None:
    return v if isinstance(v, str) else None


def _finite_number_or_none(v) -> float | None:
    """v as a float when it is a real, finite number: not a bool, not NaN or Infinity (json.loads
    accepts both), not an integer too big for a float."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


def recall_remembered() -> PrinterTarget | None:
    """The printer that last answered, or None. The file is hand-editable, so every field is checked:
    the address must pass the same check as any other, and a field of the wrong type reads as unknown."""
    try:
        d = json.loads(REMEMBERED_PATH.read_text())
    except (OSError, ValueError):
        return None
    raw = d.get("url") if isinstance(d, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        url, _ = _parse_address(raw, "remembered")  # a login typed into the file is dropped, never used
    except PrinterError:
        return None
    kind = d.get("kind")
    return PrinterTarget(url=url, source="remembered", profile=_text_or_none(d.get("profile")),
                         host_type=_text_or_none(d.get("host_type")),
                         printer_model=_text_or_none(d.get("printer_model")),
                         kind=kind if kind in ("klipper", "octoprint") else None,
                         remembered_at=_finite_number_or_none(d.get("found_at")))


def _host_of(raw: str | None) -> str | None:
    """The host name of an address, never any part of its user name or password. None when there
    isn't one, and also when the address is malformed: an unencoded '/', '?' or '#' in a user name or
    password makes urllib read part of the login as the host, so such an address is refused (the same
    check the printer tools use) rather than guessed at."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        url, _auth = _parse_address(raw, "override")
    except PrinterError:
        return None
    return urlsplit(url).hostname or None


def printer_id(profile_name: str | None = None, *, fallback_url: str | None = None) -> str:
    """The one rule for naming the printer in the outcome store, used by save_gcode and
    list_print_history alike: ORCA_PRINTER_ID, else the host of ORCA_PRINTER_URL, else the printer
    profile's name, else the host of `fallback_url` (a target found some other way), else "unknown"."""
    env = os.environ.get("ORCA_PRINTER_ID", "").strip()
    if env:
        return env
    host = _host_of(os.environ.get("ORCA_PRINTER_URL", ""))
    if host:
        return host
    if isinstance(profile_name, str) and profile_name.strip():
        return profile_name
    return _host_of(fallback_url) or "unknown"


def printer_id_for(target: PrinterTarget) -> str:
    return printer_id(target.profile, fallback_url=target.url)


SET_URL_HINT = ("set ORCA_PRINTER_URL in this MCP server's settings to an address that works from here, "
                "for example the printer's IP address.")
TOKEN_HINT = ("Check ORCA_API_TOKEN in this MCP server's settings: it must match the token on the Remote API "
              "page of OrcaSlicer's Preferences.")


SETTINGS_HINT = "Check the OrcaSlicer settings of this MCP server (ORCA_API_URL, ORCA_API_TIMEOUT)."


def token_problem(e: ApiError) -> bool:
    """True when OrcaSlicer refused the token or this server has none (the fix is the token). A
    ConfigError is a missing token only when it names ORCA_API_TOKEN (load_config says so); any other
    one, an invalid ORCA_API_TIMEOUT for instance, is not the token's fault."""
    return isinstance(e, Unauthorized) or (isinstance(e, ConfigError) and "ORCA_API_TOKEN" in str(e))


def orca_read_hint(e: ApiError, start_hint: str) -> str:
    """The hint for a failed read of OrcaSlicer: the token when that is the problem, the settings for any
    other configuration error, else `start_hint` (OrcaSlicer is simply not running)."""
    if token_problem(e):
        return TOKEN_HINT
    return SETTINGS_HINT if isinstance(e, ConfigError) else start_hint


_PROFILE_ADDRESS_HINT = ("In OrcaSlicer, open the connection settings next to the printer and enter "
                         "its address, or " + SET_URL_HINT)
_OVERRIDE_ADDRESS_HINT = ("Correct ORCA_PRINTER_URL in this MCP server's settings, or remove it to use "
                          "the printer profile in OrcaSlicer.")

# Process-wide memory of which protocol and base URL answered for a target URL, so repeat calls
# skip the probes that failed. Cleared by tests; a failed cached probe falls through to the rest.
_PROBE_CACHE: dict[str, tuple[str, str]] = {}


def _shown(raw: str) -> str:
    """The address as it may appear in a message: everything between the scheme and the last '@'
    is hidden, so a password can never reach the model or a log line. Same rule as
    get_preset_config, so a scheme only counts at the start ('user:pa://ss@host' shows no login)."""
    return _strip_userinfo((raw or "").strip())


def _origin(source: str, profile: str | None) -> str:
    if source == "override":
        return "ORCA_PRINTER_URL"
    if source == "profile":
        return f"the printer profile '{profile}'" if profile else "the printer profile"
    return "the remembered printer"


def _bad_address(raw: str, source: str, profile: str | None = None, *, encoding: bool = False) -> PrinterError:
    shown, origin = _shown(raw), _origin(source, profile)
    if encoding and source == "override":
        message = (f"The user name or password in the printer address '{shown}' from {origin} contains "
                   "characters that must be percent-encoded (for example / ? # @).")
    elif encoding:
        # OrcaSlicer's own address: its credentials are never read, so encoding them would not help.
        message = (f"The printer address '{shown}' from {origin} contains a user name or password that "
                   "can't be read. Credentials in OrcaSlicer's printer address aren't used: remove them "
                   "there, and set ORCA_PRINTER_URL or ORCA_PRINTER_API_KEY in this MCP server's "
                   "settings instead.")
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
        port = checked.port  # raises ValueError for a non-numeric port or one outside 0-65535
        valid = bool(checked.hostname) and (port is None or 1 <= port <= 65535)
    except ValueError:
        valid = False
    if not valid:
        raise _bad_address(raw, source, profile)
    return url, auth


async def resolve_target(fork_factory) -> PrinterTarget:
    """Find the printer: ORCA_PRINTER_URL, else OrcaSlicer's active printer profile, else the
    printer remembered from the last time OrcaSlicer could be read. An error raised on the way
    carries a partial `printer` block ({"source", and "profile" once known}) so the reply still says
    where the address came from."""
    where: dict = {"source": "override"}
    try:
        return await _resolve_target(fork_factory, where)
    except PrinterError as e:
        e.details.setdefault("printer", dict(where))
        raise


async def _resolve_target(fork_factory, where: dict) -> PrinterTarget:
    override = os.environ.get("ORCA_PRINTER_URL", "").strip()
    if override:
        url, auth = _parse_address(override, "override")
        return PrinterTarget(url=url, source="override", auth=auth)
    where["source"] = "profile"
    try:
        async with fork_factory() as fork:
            status = await fork.get_status()
            name = ((status or {}).get("presets") or {}).get("printer")
            if not name:
                raise PrinterError("not_configured", "OrcaSlicer reports no active printer profile.",
                                   hint="Select a printer in OrcaSlicer, or " + SET_URL_HINT)
            where["profile"] = str(name)
            preset = await fork.get_preset_config("printer", name)
    except ApiError as e:
        remembered = recall_remembered()
        if remembered is not None:
            return remembered
        raise PrinterError("orca_unreachable",
                           "Couldn't read the printer profile from OrcaSlicer, and no printer has answered before.",
                           hint=orca_read_hint(
                               e, "Start OrcaSlicer (MCP build) with the Remote API enabled, or " + SET_URL_HINT),
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
    # Any user name or password in OrcaSlicer's own address is dropped, even one that slipped past
    # redaction: credentials come only from ORCA_PRINTER_URL or ORCA_PRINTER_API_KEY (spec section 10).
    url, _ = _parse_address(host, "profile", name)
    return PrinterTarget(url=url, source="profile", profile=name, host_type=host_type,
                         printer_model=model, auth=None)


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
    client. Tries Moonraker as given, Moonraker on 7125, then OctoPrint, cached per target URL. A
    client that is not returned is closed on every path, including cancellation."""
    kind_hint = target.kind
    if kind_hint is None and target.source == "profile":
        # A new process has no probe cache: the protocol that answered at this very address last time
        # (printer.json) goes first. Only a hint: a printer that has changed is still found.
        remembered = recall_remembered()
        if remembered is not None and remembered.url == target.url:
            kind_hint = remembered.kind
    try:
        candidates = _candidates(target.url, kind_hint)
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
        keep = False
        try:
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
                keep = True
                return found, client
        finally:
            if not keep:
                await client.aclose()
        tried.append(f"{base} ({'Klipper' if kind == 'klipper' else 'OctoPrint'})")
    if auth_err is not None:
        raise auth_err
    raise PrinterError("not_reachable", f"The printer didn't answer at {target.url}.",
                       hint=_unreachable_hint(target), tried=tried)
