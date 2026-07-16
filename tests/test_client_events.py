from orcaslicer_mcp.config import Config
from orcaslicer_mcp.client import OrcaClient

def test_ws_url_derivation():
    c = OrcaClient(Config(base_url="http://host:13130", token="tok", timeout=5))
    assert c._ws_url() == "ws://host:13130/api/v1/events?token=tok"
    c2 = OrcaClient(Config(base_url="https://host:13130", token="t", timeout=5))
    assert c2._ws_url() == "wss://host:13130/api/v1/events?token=t"
