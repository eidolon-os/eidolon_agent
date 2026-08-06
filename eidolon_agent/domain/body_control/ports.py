"""Ports for body-device control."""

from __future__ import annotations

from typing import Protocol

from eidolon_sdk.biz.body import BodyCommandResult, BodyDevice


class BodyDeviceStorePort(Protocol):
    async def list_devices(
        self,
        *,
        owner_id: str,
        companion_id: str,
        source_device_id: str | None = None,
    ) -> list[BodyDevice]: ...


class BodyCommandPort(Protocol):
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
        runtime_session_id: str | None = None,
        runtime_trace_id: str | None = None,
        runtime_turn_id: str | None = None,
        runtime_tool_call_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> BodyCommandResult: ...

    async def get_command_status(
        self,
        *,
        owner_id: str,
        companion_id: str,
        command_id: str,
    ) -> BodyCommandResult: ...
