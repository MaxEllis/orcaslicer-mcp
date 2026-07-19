from __future__ import annotations
import asyncio
import json
import httpx
import websockets
from .config import Config
from .errors import error_from_status, NotReachable, ApiError, UiTimeout


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
        except httpx.TimeoutException as e:
            raise UiTimeout(f"OrcaSlicer did not respond in time: {e}") from e
        except httpx.TransportError as e:
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

    async def get_objects(self) -> dict:
        return await self._request("GET", "/api/v1/objects")

    async def delete_object(self, obj_id: int) -> dict:
        return await self._request("DELETE", f"/api/v1/objects/{obj_id}")

    async def duplicate_object(self, obj_id: int) -> dict:
        return await self._request("POST", f"/api/v1/objects/{obj_id}/duplicate")

    async def set_object_config(self, obj_id: int, changes: dict) -> dict:
        return await self._request("PUT", f"/api/v1/objects/{obj_id}/config", json=changes)

    async def transform_object(self, obj_id: int, translate=None, rotate=None, scale=None) -> dict:
        body = {}
        if translate is not None:
            body["translate"] = translate
        if rotate is not None:
            body["rotate"] = rotate
        if scale is not None:
            body["scale"] = scale
        return await self._request("POST", f"/api/v1/objects/{obj_id}/transform", json=body)

    async def arrange(self) -> dict:
        return await self._request("POST", "/api/v1/arrange")

    async def orient(self) -> dict:
        return await self._request("POST", "/api/v1/orient")

    async def job_status(self) -> dict:
        return await self._request("GET", "/api/v1/jobs/status")

    async def get_config(self, keys: list[str] | None) -> dict:
        # The fork's /config `keys` filter splits the RAW query string on ',' without
        # URL-decoding first, so httpx's percent-encoded comma (%2C) matches nothing and
        # returns {}. Rather than depend on that fragile path, fetch the full config
        # (~20KB on LAN) and filter locally — parser-independent. See test_client_config.
        data = await self._request("GET", "/api/v1/config")
        cfg = data.get("config", {})
        if keys:
            return {k: cfg[k] for k in keys if k in cfg}
        return cfg

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

    async def save_preset(self, ptype: str, name: str, detach: bool = False) -> dict:
        return await self._request("POST", "/api/v1/preset/save",
                                   json={"type": ptype, "name": name, "detach": detach})

    async def get_presets(self) -> dict:
        return await self._request("GET", "/api/v1/presets")

    async def get_preset_config(self, ptype: str, name: str) -> dict:
        return await self._request("POST", "/api/v1/preset/config", json={"type": ptype, "name": name})

    async def delete_preset(self, ptype: str, name: str) -> dict:
        return await self._request("DELETE", "/api/v1/preset", json={"type": ptype, "name": name})

    async def set_layer_height(self, obj_id: int, mode: str, quality: float = 0.5) -> dict:
        body = {"mode": mode}
        if mode == "adaptive":
            body["quality"] = quality
        return await self._request("PUT", f"/api/v1/objects/{obj_id}/layer_height", json=body)

    async def set_height_range(self, obj_id: int, min_z: float | None = None,
                               max_z: float | None = None, layer_height: float | None = None,
                               clear: bool = False) -> dict:
        body = {"clear": True} if clear else {"min_z": min_z, "max_z": max_z, "layer_height": layer_height}
        return await self._request("PUT", f"/api/v1/objects/{obj_id}/height_range", json=body)

    async def get_gcode(self) -> bytes:
        try:
            resp = await self._http.get("/api/v1/gcode")
        except httpx.TimeoutException as e:
            raise UiTimeout(f"OrcaSlicer did not respond in time: {e}") from e
        except httpx.TransportError as e:
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
                    if not isinstance(evt, dict):
                        continue
                    events.append(evt)
                    if stop_on and evt.get("event") in stop_on:
                        break
        except Exception:
            # A closed/failed WS (e.g. bad token) just yields what we have.
            pass
        return events
