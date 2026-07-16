from __future__ import annotations
from mcp.server.fastmcp import FastMCP
from .config import load_config
from .client import OrcaClient
from .errors import ApiError, Validation
from .models import summarize_slice

mcp = FastMCP("orcaslicer")


def _client() -> OrcaClient:
    return OrcaClient(load_config())


def _err(e: ApiError) -> dict:
    out = {"error": str(e)}
    if isinstance(e, Validation):
        out["errors"] = e.errors
    return out


@mcp.tool()
async def get_status() -> dict:
    """App/project/preset status, dirty keys, slice validity, and whether a slice is running."""
    try:
        async with _client() as c:
            return await c.get_status()
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_config(keys: list[str] | None = None) -> dict:
    """Read merged config values (optionally filtered to `keys`)."""
    try:
        async with _client() as c:
            return {"config": await c.get_config(keys)}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def set_config(changes: dict) -> dict:
    """Apply config changes atomically. Returns {applied, errors}. On any invalid key, nothing is applied."""
    try:
        async with _client() as c:
            return await c.put_config(changes)
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def slice() -> dict:
    """Start slicing the current plate. Returns started / already_valid / conflict."""
    try:
        async with _client() as c:
            return await c.slice()
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_slice_status() -> dict:
    """Current/last slice state, stats, and warnings."""
    try:
        async with _client() as c:
            return summarize_slice(await c.slice_status())
    except ApiError as e:
        return _err(e)


_TERMINAL = {"slice.done", "slice.error", "slice.cancelled"}


@mcp.tool()
async def slice_and_wait(timeout: int = 300) -> dict:
    """Slice (or reuse a valid result) and wait for completion; return final stats + warnings."""
    try:
        async with _client() as c:
            started = await c.slice()
            if started.get("already_valid"):
                return summarize_slice(await c.slice_status())
            await c.collect_events(seconds=timeout, stop_on=_TERMINAL)
            return summarize_slice(await c.slice_status())
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def apply_and_slice(changes: dict) -> dict:
    """Apply config changes, then slice and report the resulting stats/warnings."""
    try:
        async with _client() as c:
            applied = await c.put_config(changes)
            started = await c.slice()
            if not started.get("already_valid"):
                await c.collect_events(seconds=300, stop_on=_TERMINAL)
            result = summarize_slice(await c.slice_status())
            return {"applied": applied.get("applied", []),
                    "errors": applied.get("errors", {}), "result": result}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def compare_settings(key: str, values: list, extra: dict | None = None) -> dict:
    """For each value of `key`, slice and collect stats/warnings; restore the original when done.

    Non-destructive: the original value of `key` is put back even on error.
    """
    try:
        async with _client() as c:
            original = (await c.get_config([key])).get(key)
            rows = []
            try:
                for v in values:
                    row = {"value": v, "stats": None, "warnings": [], "error": None}
                    try:
                        await c.put_config({key: v, **(extra or {})})
                        await c.slice()
                        await c.collect_events(seconds=300, stop_on=_TERMINAL)
                        s = summarize_slice(await c.slice_status())
                        row["stats"] = s["stats"]
                        row["warnings"] = s["warnings"]
                    except ApiError as e:
                        row["error"] = str(e)
                    rows.append(row)
            finally:
                if original is not None:
                    await c.put_config({key: original})
            return {"key": key, "rows": rows}
    except ApiError as e:
        return _err(e)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
