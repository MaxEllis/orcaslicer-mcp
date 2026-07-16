from __future__ import annotations
from typing import TypedDict, Any


class SliceResult(TypedDict):
    state: str
    percent: int
    message: str
    stats: dict | None
    warnings: list


class CompareRow(TypedDict):
    value: Any
    stats: dict | None
    warnings: list
    error: str | None


def summarize_slice(status: dict) -> SliceResult:
    return {
        "state": status.get("state", "idle"),
        "percent": status.get("percent", -1),
        "message": status.get("message", ""),
        "stats": status.get("stats"),
        "warnings": status.get("warnings", []),
    }
