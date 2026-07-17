"""Body-device control service used by agent tools."""

from __future__ import annotations

from eidolon_sdk.biz.body import (
    BodyCommandResult,
    BodyDevice,
    validate_capability_arguments,
)

from eidolon_agent.domain.body_control.errors import (
    BodyCapabilityUnsupported,
    BodyCommandRejected,
    BodyDeviceOffline,
)
from eidolon_agent.domain.body_control.ports import BodyCommandPort, BodyDeviceStorePort
from eidolon_agent.domain.body_control.resolver import resolve_body_device


class BodyControlService:
    def __init__(
        self,
        *,
        device_store: BodyDeviceStorePort,
        command_port: BodyCommandPort,
    ) -> None:
        self._devices = device_store
        self._commands = command_port

    async def list_devices(
        self,
        *,
        owner_id: str,
        companion_id: str,
        source_device_id: str | None = None,
        include_offline: bool = True,
    ) -> list[BodyDevice]:
        devices = await self._devices.list_devices(
            owner_id=owner_id,
            companion_id=companion_id,
            source_device_id=source_device_id,
        )
        if include_offline:
            return devices
        return [device for device in devices if device.status != "offline"]

    async def send_command(
        self,
        *,
        owner_id: str,
        companion_id: str,
        source_device_id: str | None,
        target: str,
        op: str,
        payload: dict,
        qos: str = "ack",
        ttl_ms: int = 30_000,
        priority: str = "normal",
        confirmed: bool = False,
        runtime_caller_id: str | None = None,
        runtime_session_id: str | None = None,
    ) -> BodyCommandResult:
        devices = await self.list_devices(
            owner_id=owner_id,
            companion_id=companion_id,
            source_device_id=source_device_id,
            include_offline=True,
        )
        qos = _validate_choice(qos, {"fire_and_forget", "ack", "result"}, "qos")
        priority = _validate_choice(priority, {"low", "normal", "high", "urgent"}, "priority")
        if ttl_ms < 1_000 or ttl_ms > 600_000:
            raise BodyCommandRejected("ttl_ms must be between 1000 and 600000")
        device = resolve_body_device(devices, target, source_device_id=source_device_id)
        capability = device.capability(op)
        if capability is None:
            raise BodyCapabilityUnsupported(f"{device.name or device.device_id} does not support {op}")
        if device.status in {"offline", "unknown"}:
            raise BodyDeviceOffline(f"{device.name or device.device_id} is not online")
        schema_error = validate_capability_arguments(capability.input_schema, payload)
        if schema_error:
            raise BodyCommandRejected(schema_error)

        return await self._commands.send_command(
            owner_id=owner_id,
            companion_id=companion_id,
            device_id=device.device_id,
            source_device_id=source_device_id,
            runtime_caller_id=runtime_caller_id,
            runtime_session_id=runtime_session_id,
            op=op,
            payload=payload,
            qos=qos,
            ttl_ms=ttl_ms,
            priority=priority,
        )

    async def get_command_status(
        self,
        *,
        owner_id: str,
        companion_id: str,
        command_id: str,
    ) -> BodyCommandResult:
        result = await self._commands.get_command_status(
            owner_id=owner_id,
            companion_id=companion_id,
            command_id=command_id,
        )
        devices = await self.list_devices(
            owner_id=owner_id,
            companion_id=companion_id,
            include_offline=True,
        )
        allowed_device_ids = {device.device_id for device in devices}
        if result.device_id not in allowed_device_ids:
            raise BodyCommandRejected("command does not belong to a visible body device")
        return result

def _validate_choice(value: str, allowed: set[str], field: str) -> str:
    normalized = str(value or "").strip()
    if normalized not in allowed:
        raise BodyCommandRejected(f"{field} must be one of: {', '.join(sorted(allowed))}")
    return normalized
