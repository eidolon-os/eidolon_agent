"""Adapters for body-control service."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from time import monotonic
from typing import Any
from urllib.parse import quote

import httpx
from eidolon_sdk.biz.body import (
    BodyCapability,
    BodyCommandResult,
    BodyDevice,
    OwnerDeviceBlackboardSnapshot,
    owner_device_blackboard_key,
)

from eidolon_agent.domain.body_control.errors import (
    BodyCommandRejected,
    BodyControlUnavailable,
    BodyDeviceOffline,
)
from eidolon_agent.domain.body_control.ports import BodyCommandPort, BodyDeviceStorePort

_log = logging.getLogger(__name__)


class NatsRuntimeBodyDeviceStore(BodyDeviceStorePort):
    """Read one owner's current runtime device snapshot directly from NATS KV.

    The Agent is read-only and keeps no cross-turn device cache. Missing,
    malformed or expired snapshots fail closed to an empty capability set.
    """

    def __init__(self, kv_store) -> None:
        self._kv = kv_store

    async def list_devices(
        self,
        *,
        owner_id: str,
        companion_id: str,
        source_device_id: str | None = None,
    ) -> list[BodyDevice]:
        try:
            raw = await self._kv.get(owner_device_blackboard_key(owner_id))
            if raw is None:
                return []
            snapshot = OwnerDeviceBlackboardSnapshot.from_bytes(
                raw,
                expected_owner_id=owner_id,
            )
            rows = snapshot.visible_devices(requester_companion_id=companion_id)
        except Exception as exc:
            _log.warning("runtime device blackboard read failed closed: %s", exc)
            return []
        devices: list[BodyDevice] = []
        for row in rows:
            device_id = row.device_id
            provider_companion_id = row.provider_companion_id or ""
            provider_companion_name = row.provider_companion_name.strip()
            if not device_id or not provider_companion_id or not provider_companion_name:
                continue
            capabilities = tuple(
                _runtime_capability(item.model_dump(mode="json")) for item in row.capabilities
            )
            devices.append(
                BodyDevice(
                    device_id=device_id,
                    name=row.name or device_id,
                    aliases=row.aliases,
                    provider_companion_id=provider_companion_id,
                    provider_companion_name=provider_companion_name,
                    status="online_control",
                    is_current_device=bool(source_device_id and device_id == source_device_id),
                    last_seen=row.last_seen_at,
                    control_room_name=row.room_name,
                    capabilities=capabilities,
                )
            )
        return devices


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

    async def send_command(
        self,
        *,
        owner_id: str,
        companion_id: str,
        device_id: str,
        op: str,
        capability_version: int | None,
        payload: dict,
        qos: str = "ack",
        ttl_ms: int = 30_000,
        priority: str = "normal",
        source_device_id: str | None = None,
        runtime_caller_id: str | None = None,
        runtime_session_id: str | None = None,
        runtime_trace_id: str | None = None,
        runtime_turn_id: str | None = None,
        runtime_tool_call_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> BodyCommandResult:
        body = await self._request_json(
            "POST",
            f"/api/runtime/devices/{_quote(device_id)}/commands",
            json={
                "requester_owner_id": owner_id,
                "requester_companion_id": companion_id,
                "op": op,
                "capability_version": capability_version,
                "source_device_id": source_device_id,
                "runtime_caller_id": runtime_caller_id,
                "runtime_session_id": runtime_session_id,
                "runtime_trace_id": runtime_trace_id,
                "runtime_turn_id": runtime_turn_id,
                "runtime_tool_call_id": runtime_tool_call_id,
                "idempotency_key": idempotency_key,
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
            if exc.response.status_code == 409 and (
                "not currently connected" in message or "not online" in message
            ):
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
        capability_version=_positive_int_or_none(body.get("capability_version")),
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


def _runtime_capability(value: dict[str, Any]) -> BodyCapability:
    return BodyCapability(
        name=str(value.get("name") or ""),
        version=int(value.get("version") or 1),
        description=str(value.get("description") or ""),
        input_schema=(
            value.get("input_schema") if isinstance(value.get("input_schema"), dict) else {}
        ),
        result_schema=(
            value.get("result_schema") if isinstance(value.get("result_schema"), dict) else {}
        ),
    )


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None


def _positive_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
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
