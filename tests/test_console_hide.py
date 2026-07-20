import sys
from orcaslicer_mcp import server


def test_hide_windows_console_is_safe_noop_off_windows(monkeypatch):
    # On non-Windows it must return without touching anything and never raise.
    monkeypatch.setattr(sys, "platform", "linux")
    assert server._hide_windows_console() is None


def test_hide_windows_console_swallows_errors(monkeypatch):
    # Even if the platform looks like Windows but ctypes is unavailable/raises,
    # the server must still start — the helper must never propagate.
    monkeypatch.setattr(sys, "platform", "win32")
    import builtins

    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == "ctypes":
            raise RuntimeError("no ctypes")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert server._hide_windows_console() is None
