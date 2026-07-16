from __future__ import annotations
import asyncio
import json
import httpx
import websockets
from .config import Config
from .errors import error_from_status, NotReachable, ApiError


class OrcaClient:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._http = httpx.AsyncClient(
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

    def _ws_url(self) -> str:
        base = self._cfg.base_url
        ws = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return f"{ws}/api/v1/events?token={self._cfg.token}"

    async def collect_events(self, seconds: float, stop_on: set[str] | None = None) -> list[dict]:
        events: list[dict] = []
        deadline = asyncio.get_event_loop().time() + seconds
        try:
            async with websockets.connect(self._ws_url()) as ws:
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    try:
                        evt = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    events.append(evt)
                    if stop_on and evt.get("event") in stop_on:
                        break
        except Exception:
            # A closed/failed WS (e.g. bad token) just yields what we have.
            pass
        return events
