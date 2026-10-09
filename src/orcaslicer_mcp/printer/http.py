"""Shared plumbing for the read-only printer clients: timeouts, the optional API key, and turning
HTTP failures into PrinterError codes the tools can explain. GET only."""
from __future__ import annotations
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpx

from ..guard import REDACTED, _strip_userinfo
from .errors import PrinterError, auth_error

CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 10.0


def _without_userinfo(url: str) -> str:
    """The URL with any user name and password removed. Same cut as guard._strip_userinfo (everything
    between the scheme and the last '@', so a password holding '/', '?' or '@' goes too)."""
    if "@" not in url:
        return url
    return _strip_userinfo(url).replace(REDACTED + "@", "", 1)


class ReadClient:
    service = "The printer"

    def __init__(self, base_url: str, api_key: str | None = None, auth: tuple[str, str] | None = None):
        # Credentials travel in the headers and `auth`, never in the address: messages echo base_url.
        self.base_url = _without_userinfo(base_url).rstrip("/")
        api_key = (api_key or "").strip() or None  # a pasted key often carries a trailing newline
        self._key_set = bool(api_key)
        self._basic_set = auth is not None
        # The printer's own clock (epoch seconds) from the Date header of the last successful reply,
        # or None before one arrives. Moonraker stamps its console lines with this clock, not ours.
        self.server_time: float | None = None
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            headers={"X-Api-Key": api_key} if api_key else {}, auth=auth)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_json(self, path: str, params: dict | None = None, *, none_on: tuple[int, ...] = ()):
        try:
            resp = await self._http.get(self.base_url + path, params=params)
        except httpx.TransportError as e:
            raise PrinterError("not_reachable", f"{self.service} did not answer at {self.base_url}.") from e
        except httpx.InvalidURL as e:
            raise PrinterError("not_configured", f"The printer address {self.base_url} isn't a valid URL.") from e
        except httpx.HTTPError as e:  # DecodingError, TooManyRedirects and anything httpx adds later
            raise PrinterError("protocol_error", f"{self.service} sent a reply to {path} that couldn't be read.") from e
        if resp.status_code in none_on:
            return None
        if resp.status_code in (401, 403):
            raise auth_error(self.service, self._key_set, basic_auth=self._basic_set and not self._key_set)
        if resp.status_code >= 400:
            raise PrinterError("protocol_error", f"{self.service} answered HTTP {resp.status_code} for {path}.")
        self._note_clock(resp)
        try:
            return resp.json()
        except ValueError as e:
            raise PrinterError("protocol_error", f"{self.service} sent a reply to {path} that wasn't JSON.") from e

    def _note_clock(self, resp: httpx.Response) -> None:
        """Remember the server's clock from the Date header; a missing or unreadable one changes nothing."""
        raw = resp.headers.get("Date")
        if not raw:
            return
        try:
            when = parsedate_to_datetime(raw)
            if when.tzinfo is None:  # HTTP dates are GMT; a zone-less parse must not fall back to local time
                when = when.replace(tzinfo=timezone.utc)
            self.server_time = when.timestamp()
        except (TypeError, ValueError, IndexError, OverflowError):
            pass

    async def _identify_with(self, probe) -> bool:
        """True if probe() answers and looks right. A locked server raises (it IS the right one)."""
        try:
            return bool(await probe())
        except PrinterError as e:
            if e.code in ("auth_required", "auth_rejected"):
                raise
            return False
