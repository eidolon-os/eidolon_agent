"""Agent's narrow client for the Hub device execution authority."""

from __future__ import annotations

import httpx
from eidolon_sdk.biz.smarthome import ExecuteRequest, ExecuteResult, Registry
from eidolon_sdk.core.http import create_async_client

from eidolon_agent.domain.smarthome.errors import SmartHomeUnavailable
from eidolon_agent.domain.smarthome.ports import DeviceStatus, HomeSnapshot


class HubSmartHomeClient:
    def __init__(self, *, base_url: str, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._owns_client = client is None
        self._client = client if client is not None else create_async_client(trust_env=False, timeout=5)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post(self, action: str, body: dict) -> dict:
        response = await self._client.post(
            f"{self._base_url}/api/smarthome/v1/{action}",
            headers={"Authorization": f"Bearer {self._token}"},
            json=body,
        )
        response.raise_for_status()
        return response.json()

    async def snapshot(self, owner_id: str) -> HomeSnapshot:
        try:
            body = await self._post("snapshot", {"owner_id": owner_id})
            return HomeSnapshot(
                registry=Registry.model_validate(body["registry"]),
                status={
                    device_id: DeviceStatus(**status)
                    for device_id, status in body["status"].items()
                },
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise SmartHomeUnavailable("smart home snapshot unavailable") from exc

    async def execute(self, owner_id: str, request: ExecuteRequest) -> ExecuteResult:
        try:
            body = await self._post(
                "execute",
                {"owner_id": owner_id, "request": request.model_dump(mode="json")},
            )
            return ExecuteResult.model_validate(body)
        except httpx.ConnectError as exc:
            raise SmartHomeUnavailable("smart home runtime is not connected") from exc
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            # A reply lost after submission may follow a real device change.
            # The command use case reports that uncertainty; it never retries.
            raise TimeoutError("smart home execution outcome unknown") from exc
