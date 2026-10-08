"""Read-only Moonraker (Klipper) client. GET requests only: nothing here can heat, move or start
anything. Started from klipper-mcp's client, which keeps the control half until the merge phase."""
from __future__ import annotations

from .http import ReadClient


class MoonrakerClient(ReadClient):
    service = "Klipper (Moonraker)"

    async def _result(self, path: str, params: dict | None = None, *, none_on: tuple[int, ...] = ()):
        body = await self.get_json(path, params, none_on=none_on)
        return body.get("result") if isinstance(body, dict) else None

    async def identify(self) -> bool:
        async def probe():
            info = await self.server_info()
            return isinstance(info, dict) and "klippy_state" in info
        return await self._identify_with(probe)

    async def server_info(self) -> dict:
        return await self._result("/server/info") or {}

    async def objects_query(self, objects: list[str]) -> dict:
        # Moonraker takes each object as a bare query key (?extruder&heater_bed); key=value such as
        # configfile=settings limits the fields returned. Tokens must not need URL-encoding.
        res = await self._result("/printer/objects/query?" + "&".join(objects))
        return (res or {}).get("status") or {}

    async def gcode_store(self, count: int = 100) -> list[dict]:
        res = await self._result("/server/gcode_store", {"count": count})
        return (res or {}).get("gcode_store") or []

    async def history_list(self, limit: int = 10) -> list[dict]:
        res = await self._result("/server/history/list", {"limit": limit, "order": "desc"})
        return (res or {}).get("jobs") or []

    async def file_metadata(self, filename: str) -> dict | None:
        return await self._result("/server/files/metadata", {"filename": filename}, none_on=(404,))
