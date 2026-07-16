from __future__ import annotations
import asyncio
from mcp.server.fastmcp import FastMCP
from .config import load_config
from .client import OrcaClient
from .errors import ApiError, Validation, NotFound, Conflict, ConfigError
from .models import summarize_slice

mcp = FastMCP("orcaslicer")


def _client() -> OrcaClient:
    try:
        return OrcaClient(load_config())
    except (RuntimeError, ValueError) as e:
        raise ConfigError(str(e))


async def _wait_for_slice(c, timeout: int) -> dict:
    """Poll slice_status until the slice leaves the 'slicing' state or timeout. Returns the final status dict."""
    deadline = asyncio.get_running_loop().time() + timeout
    s = await c.slice_status()
    while s.get("state") == "slicing" and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(1.0)
        s = await c.slice_status()
    return s


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


@mcp.tool()
async def slice_and_wait(timeout: int = 300) -> dict:
    """Slice (or reuse a valid result) and wait for completion; return final stats + warnings."""
    try:
        async with _client() as c:
            started = await c.slice()
            if started.get("already_valid"):
                return summarize_slice(await c.slice_status())
            return summarize_slice(await _wait_for_slice(c, timeout))
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
                await _wait_for_slice(c, 300)
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
            snapshot_keys = [key] + list((extra or {}).keys())
            originals = await c.get_config(snapshot_keys)
            rows = []
            restore_error = None
            try:
                for v in values:
                    row = {"value": v, "stats": None, "warnings": [], "error": None}
                    try:
                        await c.put_config({key: v, **(extra or {})})
                        started = await c.slice()
                        if not started.get("already_valid"):
                            await _wait_for_slice(c, 300)
                        s = summarize_slice(await c.slice_status())
                        row["stats"] = s["stats"]
                        row["warnings"] = s["warnings"]
                    except ApiError as e:
                        row["error"] = str(e)
                    rows.append(row)
            finally:
                if originals:
                    try:
                        await c.put_config(originals)
                    except ApiError as e:
                        restore_error = str(e)  # preserve collected rows even if restore fails
            result = {"key": key, "rows": rows}
            if restore_error is not None:
                result["restore_error"] = restore_error
            return result
    except ApiError as e:
        return _err(e)


def _m4a_err(e: ApiError) -> dict:
    if isinstance(e, NotFound):
        return {"error": "not available on this OrcaSlicer build (needs M4a)"}
    return _err(e)


@mcp.tool()
async def watch_events(seconds: int = 10) -> dict:
    """Collect live events (slice.*/config.changed/project.opened) over a bounded window."""
    try:
        async with _client() as c:
            return {"events": await c.collect_events(seconds=seconds)}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def find_config_keys(substring: str) -> dict:
    """Find config keys containing `substring` (helps discover among the ~600 keys)."""
    try:
        async with _client() as c:
            cfg = await c.get_config(None)
            return {"keys": sorted(k for k in cfg if substring in k)}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def load_model(path: str) -> dict:
    """Load a model file (path on the OrcaSlicer host) onto the current plate. [needs M4a]"""
    try:
        async with _client() as c:
            return await c.load_model(path)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def select_preset(type: str, name: str) -> dict:
    """Select a named preset. type = print|filament|printer. [needs M4a]"""
    try:
        async with _client() as c:
            return await c.select_preset(type, name)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def get_gcode() -> dict:
    """Retrieve the last successful slice's G-code as text. [needs M4a]"""
    try:
        async with _client() as c:
            data = await c.get_gcode()
            return {"bytes": len(data), "gcode": data.decode("utf-8", errors="replace")}
    except Conflict:
        return {"error": "not_sliced"}
    except ApiError as e:
        return _m4a_err(e)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
