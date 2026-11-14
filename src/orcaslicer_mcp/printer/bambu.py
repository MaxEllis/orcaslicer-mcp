"""A Bambu Lab printer, read through OrcaSlicer. Bambu printers speak MQTT, not HTTP, and OrcaSlicer's own
connection (Bambu's network plugin) already holds their state, so the fork serves it on GET /printer/status
and this client reads that: nothing here talks to the printer. It fits the shape take_snapshot and run_wait
expect of a printer client (an async context manager with the reads they call)."""
from __future__ import annotations

from ..errors import ApiError, NotFound
from .errors import PrinterError

NEEDS_BUILD = ("Reading a Bambu Lab printer's status needs the OrcaSlicer MCP build with the bambu_status "
               "capability (get_printer_slots and get_filament_slot_mapping need only bambu_ams).")
SELECT_HINT = "Select the printer in OrcaSlicer's Device tab and wait for it to connect."


class BambuClient:
    """`factory` opens the fork client (the server's _client); one fork request per read."""
    service = "OrcaSlicer"

    def __init__(self, factory):
        self._factory = factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        return None

    async def status(self) -> dict:
        """The fork's GET /printer/status reply: the state OrcaSlicer holds, or the route's own
        `supported: false` reply (printer_offline is a status in itself, the others are errors)."""
        from .target import orca_read_hint  # target imports the clients; keep this one-way at import time
        try:
            async with self._factory() as fork:
                out = await fork.get_printer_status()
        except NotFound as e:
            if e.route_missing:
                raise PrinterError("unsupported_connection", NEEDS_BUILD, host_type="bambu") from e
            raise PrinterError("protocol_error", f"OrcaSlicer answered the printer status read with: {e}") from e
        except ApiError as e:
            raise PrinterError("orca_unreachable",
                               "Couldn't read the printer from OrcaSlicer, which holds a Bambu Lab printer's state.",
                               hint=orca_read_hint(e, "Start OrcaSlicer (MCP build) with the Remote API enabled, "
                                                      "select the printer in its Device tab and let it connect."),
                               detail=str(e)) from e
        if not isinstance(out, dict):
            raise PrinterError("protocol_error", "OrcaSlicer's printer status reply was not an object.")
        if out.get("supported") is False and out.get("reason") != "printer_offline":
            reason, message = out.get("reason"), str(out.get("message") or "OrcaSlicer cannot read this printer.")
            if reason == "no_printer_selected":
                raise PrinterError("not_configured", message, hint=SELECT_HINT, host_type="bambu")
            raise PrinterError("unsupported_connection", message, host_type="bambu")
        return out
