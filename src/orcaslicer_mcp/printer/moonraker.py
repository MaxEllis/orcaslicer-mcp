"""Read-only Moonraker (Klipper) client. GET requests only: nothing here can heat, move or start
anything. Started from klipper-mcp's client, which keeps the control half until the merge phase."""
from __future__ import annotations
import time

from .errors import PrinterError
from .http import ReadClient

_monotonic = time.monotonic  # a seam for tests

# How long identify()'s /server/info reply stays usable by take_probe_info(). A snapshot follows the
# probe within milliseconds; the bound only stops a client that sat unused from serving old news.
PROBE_INFO_MAX_AGE_S = 5.0

# Moonraker answers 404 on /server/history/list when its [history] component is not enabled.
HISTORY_DISABLED_HINT = ("Moonraker's [history] component isn't enabled: add a [history] section to "
                         "moonraker.conf and restart Moonraker.")


class MoonrakerClient(ReadClient):
    service = "Klipper (Moonraker)"
    _probe_info: tuple[dict, float] | None = None  # identify()'s reply and when it arrived

    async def _result(self, path: str, params: dict | None = None, *, none_on: tuple[int, ...] = (),
                      hints: dict[int, str] | None = None) -> dict | None:
        """Moonraker's `result` object. None only for a status listed in none_on; any other reply
        whose `result` is not an object is a protocol_error. `hints`: see get_json."""
        body = await self.get_json(path, params, none_on=none_on, hints=hints)
        if body is None and none_on:
            return None
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise PrinterError("protocol_error", f"{self.service} sent a reply to {path} without the expected result.")
        return result

    async def _field(self, path: str, key: str, kind: type, params: dict | None = None, *,
                     hints: dict[int, str] | None = None):
        """result[key] as a `kind`: an absent key is an empty one, a value of any other type is a
        protocol_error (and so is a list holding anything but objects: every caller reads them as such)."""
        value = (await self._result(path, params, hints=hints)).get(key)
        if value is None:
            return kind()
        if not isinstance(value, kind) or (kind is list and not all(isinstance(v, dict) for v in value)):
            raise PrinterError("protocol_error", f"{self.service} sent a reply to {path} with an unexpected '{key}'.")
        return value

    async def identify(self) -> bool:
        async def probe():
            self._probe_info = None  # a reply from an earlier probe must not outlive a failed one
            info = await self.server_info()
            ok = isinstance(info, dict) and "klippy_state" in info
            if ok:
                self._probe_info = (info, _monotonic())
            return ok
        return await self._identify_with(probe)

    def take_probe_info(self) -> dict | None:
        """The /server/info reply that identify() just got, so a snapshot need not ask again, or None.

        Single use: the first call returns the reply and clears it, so a second snapshot on the same
        client (the later polls of wait_for_printer) always asks the printer afresh and sees
        klippy_connected and the warnings as they are now. None too when identify() has not
        succeeded, or its reply is older than PROBE_INFO_MAX_AGE_S."""
        kept, self._probe_info = self._probe_info, None
        if kept is None:
            return None
        info, at = kept
        return info if _monotonic() - at <= PROBE_INFO_MAX_AGE_S else None

    async def server_info(self) -> dict:
        return await self._result("/server/info")

    async def objects_query(self, objects: list[str]) -> dict:
        # Moonraker takes each object as a bare query key (?extruder&heater_bed); key=value such as
        # configfile=settings limits the fields returned. Tokens must not need URL-encoding.
        return await self._field("/printer/objects/query?" + "&".join(objects), "status", dict)

    async def gcode_store(self, count: int = 100) -> list[dict]:
        return await self._field("/server/gcode_store", "gcode_store", list, {"count": count})

    async def history_list(self, limit: int = 10) -> list[dict]:
        return await self._field("/server/history/list", "jobs", list, {"limit": limit, "order": "desc"},
                                 hints={404: HISTORY_DISABLED_HINT})

    async def file_metadata(self, filename: str) -> dict | None:
        return await self._result("/server/files/metadata", {"filename": filename}, none_on=(404,))
