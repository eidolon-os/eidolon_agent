"""Body-device control tools exposed through ToolRegistry."""

from __future__ import annotations

from eidolon_sdk.biz.body import command_result_to_dict, device_to_dict

from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.tool import Permission, ToolCall, ToolResult, ToolSchema
from eidolon_agent.domain.body_control.errors import BodyControlError


class ListBodyDevicesTool:
    schema = ToolSchema(
        name="list_body_devices",
        description=(
            "List the user's visible body devices, including names, online state, "
            "aliases, and supported capabilities. Use before controlling a device "
            "when the user names a body device, for example '点名一下小王'."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "include_offline": {
                    "type": "boolean",
                    "description": "Whether to include offline devices. Defaults to true.",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
        permissions=frozenset({Permission.SYSTEM, Permission.USER_DATA}),
        timeout_s=6.0,
    )

    def __init__(self, body_control) -> None:
        self._body = body_control

    async def invoke(self, call: ToolCall, *, ctx: ToolInvocationContext) -> ToolResult:
        if self._body is None:
            return _unavailable(call, self.schema.name)
        include_offline = bool(call.arguments.get("include_offline", True))
        try:
            devices = await self._body.list_devices(
                owner_id=ctx.caller.owner_id,
                companion_id=ctx.caller.companion_id,
                source_device_id=ctx.caller.device_id,
                include_offline=include_offline,
            )
        except BodyControlError as exc:
            return _error(call, self.schema.name, exc)
        return ToolResult(
            call_id=call.id,
            name=self.schema.name,
            ok=True,
            content={"devices": [device_to_dict(device) for device in devices]},
        )


class ControlBodyDeviceTool:
    schema = ToolSchema(
        name="control_body_device",
        description=(
            "Send a capability command to one of the user's body devices. Use for "
            "actions like device.identify/点名, sound.play, display.update, "
            "room.join, playback.stop, room.leave, or volume.set after choosing "
            "a target device."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Device name, alias, device_id, or '当前设备'.",
                },
                "op": {
                    "type": "string",
                    "description": "Capability operation, for example device.identify or sound.play.",
                },
                "payload": {
                    "type": "object",
                    "description": "Operation-specific JSON payload.",
                },
                "qos": {
                    "type": "string",
                    "enum": ["fire_and_forget", "ack", "result"],
                },
                "ttl_ms": {
                    "type": "integer",
                    "description": "Command TTL in milliseconds.",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "True only after the user explicitly confirmed a risky action.",
                },
            },
            "required": ["target", "op"],
            "additionalProperties": False,
        },
        permissions=frozenset({Permission.SYSTEM, Permission.USER_DATA}),
        side_effect=True,
        timeout_s=6.0,
        idempotency_key_template=(
            "body:${owner_id}:${companion_id}:${target}:${op}:${payload}:${turn_id}"
        ),
    )

    def __init__(self, body_control) -> None:
        self._body = body_control

    async def invoke(self, call: ToolCall, *, ctx: ToolInvocationContext) -> ToolResult:
        if self._body is None:
            return _unavailable(call, self.schema.name)
        payload = call.arguments.get("payload") or {}
        try:
            result = await self._body.send_command(
                owner_id=ctx.caller.owner_id,
                companion_id=ctx.caller.companion_id,
                source_device_id=ctx.caller.device_id,
                target=str(call.arguments.get("target") or ""),
                op=str(call.arguments.get("op") or ""),
                payload=payload if isinstance(payload, dict) else {},
                qos=str(call.arguments.get("qos") or "ack"),
                ttl_ms=int(call.arguments.get("ttl_ms") or 30_000),
                confirmed=bool(call.arguments.get("confirmed", False)),
            )
        except BodyControlError as exc:
            return _error(call, self.schema.name, exc)
        return ToolResult(
            call_id=call.id,
            name=self.schema.name,
            ok=result.ok,
            content=command_result_to_dict(result),
            error_code=None if result.ok else result.status,
            error_message=result.error or result.message or None,
        )


class GetBodyCommandStatusTool:
    schema = ToolSchema(
        name="get_body_command_status",
        description="Get the latest status for a previously sent body-device command.",
        json_schema={
            "type": "object",
            "properties": {"command_id": {"type": "string"}},
            "required": ["command_id"],
            "additionalProperties": False,
        },
        permissions=frozenset({Permission.SYSTEM, Permission.USER_DATA}),
        timeout_s=6.0,
    )

    def __init__(self, body_control) -> None:
        self._body = body_control

    async def invoke(self, call: ToolCall, *, ctx: ToolInvocationContext) -> ToolResult:
        if self._body is None:
            return _unavailable(call, self.schema.name)
        try:
            result = await self._body.get_command_status(
                owner_id=ctx.caller.owner_id,
                companion_id=ctx.caller.companion_id,
                command_id=str(call.arguments.get("command_id") or ""),
            )
        except BodyControlError as exc:
            return _error(call, self.schema.name, exc)
        return ToolResult(
            call_id=call.id,
            name=self.schema.name,
            ok=result.ok,
            content=command_result_to_dict(result),
            error_code=None if result.ok else result.status,
            error_message=result.error or result.message or None,
        )


def _unavailable(call: ToolCall, name: str) -> ToolResult:
    return ToolResult(
        call_id=call.id,
        name=name,
        ok=False,
        error_code="body_control_unavailable",
        error_message="body control service is not configured",
    )


def _error(call: ToolCall, name: str, exc: BodyControlError) -> ToolResult:
    return ToolResult(
        call_id=call.id,
        name=name,
        ok=False,
        error_code=getattr(exc, "code", "body_control_error"),
        error_message=str(exc),
    )
