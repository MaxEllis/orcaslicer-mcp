import pytest
from orcaslicer_mcp.config import load_config

def test_requires_token(monkeypatch):
    monkeypatch.delenv("ORCA_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        load_config()

def test_defaults_and_strip(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "abc")
    monkeypatch.setenv("ORCA_API_URL", "http://host:13130/")
    monkeypatch.delenv("ORCA_API_TIMEOUT", raising=False)
    cfg = load_config()
    assert cfg.token == "abc"
    assert cfg.base_url == "http://host:13130"  # trailing slash stripped
    assert cfg.timeout == 30.0
