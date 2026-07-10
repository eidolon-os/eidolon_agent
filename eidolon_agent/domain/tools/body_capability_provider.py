"""P4: device-declared capabilities become individually callable LLM tools.

A companion's bound devices declare capabilities (``devices.capabilities_json``,
already per-companion filtered by the body device store). This provider turns
each ``(device, capability)`` into a distinct ``ToolSchema`` + ``ToolPort`` so the
LLM can call e.g. ``body__<device>__display_update`` directly, instead of the one
generic ``control_body_device(op=...)`` escape hatch. Dispatch routes back through
the existing ``BodyControlService.send_command`` (which validates capability /
online / confirmation / payload schema) — the actuation path is unchanged.

Assembly is per-turn and caller-aware (the F1/F2 junction) but cheap: it reads the
3s-TTL body device store, so a turn only touches the store on a cache miss. A
schema-token budget caps how many synthetic tools are exposed (current + online
devices first) so the tools array never blows the LLM prompt budget; anything
dropped is logged rather than silently truncated.
"""

from __future__ import annotations

import logging
import re

from eidolon_sdk.biz.body import command_result_to_dict

from eidolon_agent.core.ports.tool import ToolInvocationContext, ToolPort
from eidolon_agent.core.types.identity import CallerContext
from eidolon_agent.core.types.tool import Permission, ToolCall, ToolResult, ToolSchema
from eidolon_agent.domain.body_control.errors import BodyControlError

_log = logging.getLogger(__name__)

_PERMISSIONS = frozenset({Permission.SYSTEM, Permission.USER_DATA})
# Rough token cost of one synthetic tool schema (name + description + input schema).
_APPROX_TOKENS_PER_TOOL = 120


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return s or "x"


def _tool_name(device, capability_name: str) -> str:
    return f"body__{_slug(device.name or device.device_id)}__{_slug(capability_name)}"


def _sanitize(text: str) -> str:
    # Baked into a string.Template literal → strip ``$`` so it can't be read as a placeholder.
    return (text or "").replace("$", "")


class _BodyCapabilityTool:
    """One (device, capability) exposed as a ToolPort; dispatches via send_command."""

    def __init__(self, body_control, *, device, capability, schema: ToolSchema) -> None:
        self._body = body_control
        self._device = device
        self._capability = capability
        self.schema = schema

    async def invoke(self, call: ToolCall, *, ctx: ToolInvocationContext) -> ToolResult:
        payload = dict(call.arguments or {})
        confirmed = bool(payload.pop("confirmed", False))
        try:
            result = await self._body.send_command(
                owner_id=ctx.caller.owner_id,
                companion_id=ctx.caller.companion_id,
                source_device_id=ctx.caller.device_id,
                runtime_caller_id=ctx.caller.runtime_caller_id,
                runtime_session_id=ctx.caller.runtime_session_id,
                target=self._device.device_id,
                op=self._capability.name,
                payload=payload,
                confirmed=confirmed,
            )
        except BodyControlError as exc:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code=getattr(exc, "code", "body_control_error"),
                error_message=str(exc),
            )
        return ToolResult(
            call_id=call.id,
            name=self.schema.name,
            ok=result.ok,
            content=command_result_to_dict(result),
            error_code=None if result.ok else result.status,
            error_message=result.error or result.message or None,
        )


class BodyCapabilityToolProvider:
    def __init__(self, body_control, *, budget_tokens: int = 800) -> None:
        self._body = body_control
        self._budget_tokens = budget_tokens

    async def assemble(
        self, caller: CallerContext
    ) -> tuple[list[ToolSchema], dict[str, ToolPort]]:
        if self._body is None or not caller.owner_id or not caller.companion_id:
            return [], {}
        try:
            devices = await self._body.list_devices(
                owner_id=caller.owner_id,
                companion_id=caller.companion_id,
                source_device_id=caller.device_id,
                include_offline=True,
            )
        except Exception as exc:
            _log.warning("body capability assemble failed: %s", exc)
            return [], {}

        # Spend the token budget on the most likely targets: current device, then
        # online devices, then the rest.
        def _priority(device):
            return (0 if device.is_current_device else 1, 0 if device.status == "online" else 1)

        max_tools = max(0, self._budget_tokens // _APPROX_TOKENS_PER_TOOL)
        schemas: list[ToolSchema] = []
        ports: dict[str, ToolPort] = {}
        dropped = 0
        for device in sorted(devices, key=_priority):
            for capability in device.capabilities:
                name = _tool_name(device, capability.name)
                if name in ports:  # deterministic first-wins on cross-device name collision
                    dropped += 1
                    continue
                if len(schemas) >= max_tools:
                    dropped += 1
                    continue
                schema = self._schema_for(device, capability, name)
                schemas.append(schema)
                ports[name] = _BodyCapabilityTool(
                    self._body, device=device, capability=capability, schema=schema
                )
        if dropped:
            _log.info(
                "body capability tools truncated: exposed=%d dropped=%d budget_tokens=%d",
                len(schemas),
                dropped,
                self._budget_tokens,
            )
        return schemas, ports

    def _schema_for(self, device, capability, name: str) -> ToolSchema:
        base = capability.input_schema if isinstance(capability.input_schema, dict) else {}
        json_schema: dict = dict(base) if base else {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        }
        if capability.requires_confirmation:
            props = dict(json_schema.get("properties") or {})
            props["confirmed"] = {
                "type": "boolean",
                "description": "True only after the user explicitly confirmed this risky action.",
            }
            json_schema["properties"] = props

        target = device.name or device.device_id
        desc = f"{capability.description or capability.name} — device: {target}."
        if capability.requires_confirmation:
            desc += " Risky: ask the user to confirm, then pass confirmed=true."

        idem = (
            f"body:${{owner_id}}:${{companion_id}}:"
            f"{_sanitize(device.device_id)}:{_sanitize(capability.name)}:${{turn_id}}"
        )
        return ToolSchema(
            name=name,
            description=desc,
            json_schema=json_schema,
            permissions=_PERMISSIONS,
            side_effect=capability.side_effect,
            timeout_s=6.0,
            idempotency_key_template=idem,
        )
