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
        device_id: str,
        op: str,
        payload: dict,
        qos: str = "ack",
        ttl_ms: int = 30_000,
        priority: str = "normal",
    ) -> BodyCommandResult: ...

    async def get_command_status(self, *, command_id: str) -> BodyCommandResult: ...
