from __future__ import annotations
import httpx
import json
from .config import Config
from .errors import error_from_status, NotReachable, ApiError


class _CustomAsyncClient(httpx.AsyncClient):
    """AsyncClient that serializes JSON with spaces for readability."""
    async def request(self, method, url, **kwargs):
        # If json parameter is present, serialize it with separators
        if 'json' in kwargs and kwargs['json'] is not None:
            kwargs['content'] = json.dumps(kwargs['json'], separators=(', ', ': '))
            kwargs['headers'] = {**(kwargs.get('headers') or {}), 'Content-Type': 'application/json'}
            kwargs.pop('json')
        return await super().request(method, url, **kwargs)


class OrcaClient:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._http = _CustomAsyncClient(
            base_url=cfg.base_url,
            headers={"X-Api-Token": cfg.token},
            timeout=cfg.timeout,
        )

    async def __aenter__(self) -> "OrcaClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, *, json=None, params=None) -> dict:
        try:
            resp = await self._http.request(method, path, json=json, params=params)
        except httpx.ConnectError as e:
            raise NotReachable(f"OrcaSlicer not reachable at {self._cfg.base_url}: {e}") from e
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = {}
            raise error_from_status(resp.status_code, body)
        return resp.json()

    async def get_status(self) -> dict:
        return await self._request("GET", "/api/v1/status")

    async def get_config(self, keys: list[str] | None) -> dict:
        params = {"keys": ",".join(keys)} if keys else None
        data = await self._request("GET", "/api/v1/config", params=params)
        return data.get("config", {})

    async def put_config(self, changes: dict) -> dict:
        return await self._request("PUT", "/api/v1/config", json=changes)

    async def slice(self) -> dict:
        return await self._request("POST", "/api/v1/slice")

    async def slice_status(self) -> dict:
        return await self._request("GET", "/api/v1/slice/status")

    async def load_model(self, path: str) -> dict:
        return await self._request("POST", "/api/v1/model", json={"path": path})

    async def select_preset(self, ptype: str, name: str) -> dict:
        return await self._request("PUT", "/api/v1/preset", json={"type": ptype, "name": name})

    async def get_gcode(self) -> bytes:
        try:
            resp = await self._http.get("/api/v1/gcode")
        except httpx.ConnectError as e:
            raise NotReachable(f"OrcaSlicer not reachable at {self._cfg.base_url}: {e}") from e
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = {}
            raise error_from_status(resp.status_code, body)
        return resp.content
