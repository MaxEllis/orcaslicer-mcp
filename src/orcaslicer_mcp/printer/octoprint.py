"""Read-only OctoPrint client: version, printer state and the current job. GET requests only."""
from __future__ import annotations

from .http import ReadClient


class OctoPrintClient(ReadClient):
    service = "OctoPrint"

    async def identify(self) -> bool:
        async def probe():
            v = await self.version()
            return isinstance(v, dict) and "api" in v and "server" in v
        return await self._identify_with(probe)

    async def version(self) -> dict:
        return await self.get_json("/api/version") or {}

    async def printer(self) -> dict | None:
        """None when OctoPrint is running but not connected to the printer (it answers 409)."""
        return await self.get_json("/api/printer", {"exclude": "sd,history"}, none_on=(409,))

    async def job(self) -> dict:
        return await self.get_json("/api/job") or {}
