"""Read-only OctoPrint client: version, printer state and the current job. GET requests only."""
from __future__ import annotations

from .http import ReadClient, _ABSENT


class OctoPrintClient(ReadClient):
    service = "OctoPrint"

    async def identify(self) -> bool:
        async def probe():
            v = await self.version()
            return "api" in v and "server" in v
        return await self._identify_with(probe)

    async def version(self) -> dict:
        return self._object(await self.get_json("/api/version"), "/api/version")

    async def printer(self) -> dict | None:
        """None when OctoPrint is running but not connected to the printer (it answers 409)."""
        body = await self.get_json("/api/printer", {"exclude": "sd,history"}, none_on=(409,))
        return None if body is _ABSENT else self._object(body, "/api/printer")

    async def job(self) -> dict:
        return self._object(await self.get_json("/api/job"), "/api/job")
