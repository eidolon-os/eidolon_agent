"""Adapters for body-control service."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from time import monotonic
from typing import Any
from urllib.parse import quote

import httpx
from eidolon_sdk.biz.body import (
    BodyCommandResult,
    BodyDevice,
    capabilities_from_json,
)

from eidolon_agent.domain.body_control.errors import (
    BodyCommandRejected,
    BodyControlUnavailable,
    BodyDeviceOffline,
)
from eidolon_agent.domain.body_control.ports import BodyCommandPort, BodyDeviceStorePort


class EidolonDataBodyDeviceStore(BodyDeviceStorePort):
    def __init__(self, data_store, *, runtime_client: HubBodyCommandClient | None = None) -> None:
        self._data = data_store
        self._runtime = runtime_client

    async def list_devices(
        self,
        *,
        owner_id: str,
        companion_id: str,
        source_device_id: str | None = None,
    ) -> list[BodyDevice]:
        rows = await self._data.devices.list_devices_for_owner(owner_id)
        runtime_by_id = await self._runtime_device_map()
        companion_names: dict[str, str] = {}
        devices: list[BodyDevice] = []
        for row in rows:
            if row.revoked_at is not None or row.status in {"disabled", "revoked"}:
                continue
            provider_companion_id = str(row.bound_companion_id or "")
            if not provider_companion_id:
                continue
            policy = getattr(row, "access_policy_json", None) or {}
            visibility = str(policy.get("capability_visibility") or "owner")
            if visibility == "bound_companion" and provider_companion_id != companion_id:
                continue
            if visibility not in {"owner", "bound_companion"}:
                continue
            if provider_companion_id not in companion_names:
                companion_names[provider_companion_id] = await self._companion_display_name(
                    provider_companion_id
                )
            runtime = runtime_by_id.get(row.device_id, {})
            capabilities = capabilities_from_json(
                row.capabilities_json or {},
                device_kind=row.kind or "unknown",
                known_only=True,
            )
            status = _body_status_from_runtime(runtime.get("status"))
            devices.append(
                BodyDevice(
                    device_id=row.device_id,
                    name=row.name or row.device_id,
                    aliases=_aliases_from_metadata(
                        row.metadata_json or {},
                        companion_display_name=companion_names[provider_companion_id],
                    ),
                    kind=row.kind or "unknown",
                    status=status,
                    is_current_device=bool(
                        source_device_id and row.device_id == source_device_id
                    ),
                    last_seen=_latest_datetime(row.last_seen_at, _parse_datetime(runtime.get("last_seen"))),
                    control_room_name=str(runtime.get("room_name") or ""),
                    capabilities=capabilities,
                )
            )
        return devices

    async def _runtime_device_map(self) -> dict[str, dict[str, Any]]:
        if self._runtime is None:
            return {}
        try:
            devices = await self._runtime.list_runtime_devices()
        except Exception:
            return {}
        return {str(item.get("device_id") or ""): item for item in devices if item.get("device_id")}

    async def _companion_display_name(self, companion_id: str) -> str:
        companions = getattr(self._data, "companions", None)
        get_companion = getattr(companions, "get", None)
        if get_companion is None:
            return ""
        try:
            companion = await get_companion(companion_id)
        except Exception:
            return ""
        return str(getattr(companion, "display_name", "") or "").strip()


class HubBodyCommandClient(BodyCommandPort):
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str,
        service_token: str = "",
        timeout_s: float = 5.0,
    ) -> None:
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token.strip()
        self._timeout_s = timeout_s

    async def list_runtime_devices(self) -> list[dict[str, Any]]:
        body = await self._request_json("GET", "/api/admin/devices")
        rows = body.get("devices") if isinstance(body, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    async def send_command(
        self,
        *,
        owner_id: str,
        companion_id: str,
        device_id: str,
        op: str,
        payload: dict,
        qos: str = "ack",
        ttl_ms: int = 30_000,
        priority: str = "normal",
        source_device_id: str | None = None,
        runtime_caller_id: str | None = None,
        runtime_session_id: str | None = None,
    ) -> BodyCommandResult:
        body = await self._request_json(
            "POST",
            f"/api/runtime/devices/{_quote(device_id)}/commands",
            json={
                "requester_owner_id": owner_id,
                "requester_companion_id": companion_id,
                "op": op,
                "source_device_id": source_device_id,
                "runtime_caller_id": runtime_caller_id,
                "runtime_session_id": runtime_session_id,
                "payload": payload,
                "ttl_ms": ttl_ms,
                "qos": qos,
                "priority": priority,
            },
        )
        result = _command_result_from_json(
            body,
            fallback_device_id=device_id,
            fallback_op=op,
        )
        if qos == "fire_and_forget" or result.status in _TERMINAL_COMMAND_STATUSES:
            return result
        return await self._wait_for_terminal(
            owner_id=owner_id,
            companion_id=companion_id,
            command_id=result.command_id,
            fallback_device_id=device_id,
            fallback_op=op,
            ttl_ms=ttl_ms,
        )

    async def get_command_status(
        self,
        *,
        owner_id: str,
        companion_id: str,
        command_id: str,
    ) -> BodyCommandResult:
        body = await self._request_json(
            "GET",
            f"/api/runtime/commands/{_quote(command_id)}",
            params={
                "requester_owner_id": owner_id,
                "requester_companion_id": companion_id,
            },
        )
        return _command_result_from_json(body)

    async def _wait_for_terminal(
        self,
        *,
        owner_id: str,
        companion_id: str,
        command_id: str,
        fallback_device_id: str,
        fallback_op: str,
        ttl_ms: int,
    ) -> BodyCommandResult:
        deadline = monotonic() + min(self._timeout_s, ttl_ms / 1000)
        last = BodyCommandResult(
            command_id=command_id,
            device_id=fallback_device_id,
            op=fallback_op,
            status="sent",
        )
        while monotonic() < deadline:
            await asyncio.sleep(0.1)
            last = await self.get_command_status(
                owner_id=owner_id,
                companion_id=companion_id,
                command_id=command_id,
            )
            if last.status in _TERMINAL_COMMAND_STATUSES:
                return last
        return BodyCommandResult(
            command_id=last.command_id,
            device_id=last.device_id or fallback_device_id,
            op=last.op or fallback_op,
            status="timeout",
            message="device did not return a terminal result before the tool timeout",
            ack=last.ack,
            result=last.result,
            updated_at=last.updated_at,
        )

    async def _request_json(self, method: str, path: str, **kwargs) -> Any:
        if not self._base_url:
            raise BodyControlUnavailable("body control base_url is not configured")
        try:
            if self._service_token:
                headers = dict(kwargs.pop("headers", {}) or {})
                headers["X-Eidolon-Service-Token"] = self._service_token
                kwargs["headers"] = headers
            response = await self._http.request(
                method,
                f"{self._base_url}{path}",
                timeout=self._timeout_s,
                **kwargs,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            message = _response_detail(exc.response)
            if exc.response.status_code == 409 and "not currently connected" in message:
                raise BodyDeviceOffline(message) from exc
            if exc.response.status_code in {403, 404, 409, 422}:
                raise BodyCommandRejected(message) from exc
            raise BodyControlUnavailable(message) from exc
        except httpx.HTTPError as exc:
            raise BodyControlUnavailable(str(exc)) from exc
        return response.json()


_TERMINAL_COMMAND_STATUSES = frozenset(
    {"done", "failed", "timeout", "offline", "unsupported", "rejected"}
)


def _command_result_from_json(
    data: Any,
    *,
    fallback_device_id: str = "",
    fallback_op: str = "",
) -> BodyCommandResult:
    body = data if isinstance(data, dict) else {}
    status = _command_status(str(body.get("status") or "failed"))
    return BodyCommandResult(
        command_id=str(body.get("command_id") or ""),
        device_id=str(body.get("device_id") or fallback_device_id),
        op=str(body.get("op") or fallback_op),
        status=status,
        message=str(body.get("error") or body.get("message") or ""),
        ack=body.get("ack") if isinstance(body.get("ack"), dict) else None,
        result=body.get("result"),
        error=str(body.get("error") or ""),
        updated_at=_parse_datetime(body.get("updated_at")),
    )


def _command_status(value: str):
    normalized = value.lower()
    if normalized in {"succeeded", "completed"}:
        return "done"
    if normalized == "expired":
        return "timeout"
    if normalized in {
        "accepted",
        "sent",
        "done",
        "running",
        "failed",
        "timeout",
        "offline",
        "unsupported",
        "queued",
        "rejected",
    }:
        return normalized
    return "failed"


def _body_status_from_runtime(value: Any):
    status = str(value or "").lower()
    if status == "online":
        return "online_control"
    if status in {"in_voice", "offline", "unknown"}:
        return status
    return "offline"


def _aliases_from_metadata(
    metadata: dict[str, Any],
    *,
    companion_display_name: str = "",
) -> tuple[str, ...]:
    raw = metadata.get("aliases") or metadata.get("alias")
    aliases: list[str] = []
    if isinstance(raw, str):
        aliases.append(raw)
    elif isinstance(raw, list):
        aliases.extend(str(item) for item in raw if item)
    if companion_display_name:
        aliases.append(companion_display_name)
    return tuple(dict.fromkeys(item for item in aliases if item))


def _latest_datetime(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return _aware_utc(right)
    if right is None:
        return _aware_utc(left)
    return max(_aware_utc(left), _aware_utc(right))


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
    return response.text


def _quote(value: str) -> str:
    return quote(value, safe="")
