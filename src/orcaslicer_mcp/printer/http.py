"""Shared plumbing for the read-only printer clients: timeouts, the optional API key, and turning
HTTP failures into PrinterError codes the tools can explain. GET only."""
from __future__ import annotations
import httpx

from .errors import PrinterError, auth_error

CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 10.0


class ReadClient:
    service = "The printer"

    def __init__(self, base_url: str, api_key: str | None = None, auth: tuple[str, str] | None = None):
        self.base_url = base_url.rstrip("/")
        self._key_set = bool(api_key)
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
        if resp.status_code in none_on:
            return None
        if resp.status_code in (401, 403):
            raise auth_error(self.service, self._key_set)
        if resp.status_code >= 400:
            raise PrinterError("protocol_error", f"{self.service} answered HTTP {resp.status_code} for {path}.")
        try:
            return resp.json()
        except ValueError as e:
            raise PrinterError("protocol_error", f"{self.service} sent a reply to {path} that wasn't JSON.") from e

    async def _identify_with(self, probe) -> bool:
        """True if probe() answers and looks right. A locked server raises (it IS the right one)."""
        try:
            return bool(await probe())
        except PrinterError as e:
            if e.code in ("auth_required", "auth_rejected"):
                raise
            return False
