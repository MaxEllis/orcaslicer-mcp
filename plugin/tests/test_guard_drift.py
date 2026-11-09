"""The plugin carries its own copy of orcaslicer-mcp's sensitive-key rule (a plugin is one file).
Compare the two whenever the sibling repo is present."""
import pathlib
import sys

import pytest

from conftest import mod

from conftest import MCP_SRC


@pytest.fixture(scope="module")
def guard():
    if not (MCP_SRC / "orcaslicer_mcp" / "guard.py").exists():
        pytest.skip("orcaslicer-mcp source not found")
    from orcaslicer_mcp import guard as g
    return g


def test_constants_match(guard):
    for name in ("PHYSICAL_PRINTER_KEYS", "BLOCKED_EXACT", "_GCODE_SUFFIX_EXEMPT", "UNREADABLE_KEYS",
                 "CREDENTIAL_KEYS", "URL_KEYS", "REDACTED"):
        assert getattr(mod, name) == getattr(guard, name), name


@pytest.mark.parametrize("key", ["post_process", "machine_start_gcode", "imex_mode_gcodes", "printhost_x",
                                 "plugins", "slicing_pipeline_plugin", "emit_machine_limits_to_gcode",
                                 "high_precision_gcode", "layer_height", "print_host", "filename_format"])
def test_is_sensitive_matches(guard, key):
    assert mod.is_sensitive(key) == guard.is_sensitive(key)


@pytest.mark.parametrize("url", ["http://u:p@h", "u:pa://ss@host", "https://a@b@c/x", "plain", "http://h/p@q",
                                 "HTTP+x://u@h", "1http://u@h"])
def test_strip_userinfo_matches(guard, url):
    assert mod.strip_userinfo(url) == guard._strip_userinfo(url)


@pytest.mark.parametrize("v", [True, False, 3, 0.1, 1e-7, 210.0, "x", None, [1]])
def test_as_config_text_matches(guard, v):
    assert mod.as_config_text(v) == guard.as_config_text(v)
