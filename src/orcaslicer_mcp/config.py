import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    base_url: str
    token: str
    timeout: float


def load_config() -> Config:
    token = os.environ.get("ORCA_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("ORCA_API_TOKEN is required (the OrcaSlicer Remote API token)")
    base_url = os.environ.get("ORCA_API_URL", "http://127.0.0.1:13130").rstrip("/")
    timeout = float(os.environ.get("ORCA_API_TIMEOUT", "30"))
    return Config(base_url=base_url, token=token, timeout=timeout)
