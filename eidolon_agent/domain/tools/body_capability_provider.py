"""Project online capability contracts into Companion-targeted LLM tools."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from copy import deepcopy

from eidolon_sdk.biz.body import BodyCapability, BodyDevice

from eidolon_agent.core.ports.tool import ToolInvocationContext, ToolPort
from eidolon_agent.core.types.tool import Permission, ToolCall, ToolResult, ToolSchema
from eidolon_agent.core.types.turn_context import TurnContext
from eidolon_agent.domain.body_control.errors import BodyControlError

_log = logging.getLogger(__name__)
_PERMISSIONS = frozenset({Permission.SYSTEM, Permission.USER_DATA})
_TARGET_FIELD = "target_companion"
_TOOL_NAME_LIMIT = 64


class _RuntimeCapabilityContractTool:
    def __init__(
        self,
        body_control,
        *,
        capability: BodyCapability,
        provider_names: tuple[str, ...],
    ) -> None:
        self._body = body_control
        self._capability = capability
        tool_name = _contract_tool_name(capability)
        self.schema = ToolSchema(
            name=tool_name,
            description=(
                f"{capability.description.strip()} "
                "Execute this capability on one currently online Companion. "
                "The target is a Companion name, not a device id. Do not claim that "
                "the external action succeeded unless this tool returns a completed receipt."
            ).strip(),
            json_schema=_tool_input_schema(capability, provider_names),
            permissions=_PERMISSIONS,
            side_effect=True,
            timeout_s=6.0,
            idempotency_key_template=(
                f"runtime-capability:{tool_name}:${{owner_id}}:${{trace_id}}:${{arguments_json}}"
            ),
        )

    async def invoke(self, call: ToolCall, *, ctx: ToolInvocationContext) -> ToolResult:
        target_companion = str(call.arguments.get(_TARGET_FIELD) or "").strip()
        payload = {key: value for key, value in call.arguments.items() if key != _TARGET_FIELD}
        try:
            result = await self._body.send_companion_capability(
                owner_id=ctx.turn_context.owner_id,
                companion_id=ctx.turn_context.companion_id,
                source_device_id=ctx.turn_context.device_id,
                runtime_session_id=ctx.session_id,
                runtime_trace_id=ctx.turn_context.trace_id,
                runtime_turn_id=ctx.turn_id,
                runtime_tool_call_id=call.id,
                idempotency_key=_runtime_idempotency_key(
                    owner_id=ctx.turn_context.owner_id,
                    logical_trace_id=ctx.turn_context.trace_id or ctx.turn_id,
                    capability=self._capability,
                    arguments=call.arguments,
                ),
                target_companion=target_companion,
                capability_name=self._capability.name,
                capability_version=self._capability.version,
                payload=payload,
                qos="result",
            )
        except BodyControlError as exc:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code=getattr(exc, "code", "body_control_error"),
                error_message=str(exc),
                metadata={"external_action": True, "completed": False},
            )

        completed = result.status == "done"
        if completed and result.capability_version != self._capability.version:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="body_contract_version_mismatch",
                error_message=(
                    "Hub completed a different capability contract version: "
                    f"expected v{self._capability.version}, got v{result.capability_version}"
                ),
                metadata={"external_action": True, "completed": False},
            )
        receipt = {
            "kind": "external_action_receipt",
            "contract": self._capability.name,
            "contract_version": self._capability.version,
            "target_companion": target_companion,
            "resolved_device_id": result.device_id,
            "command_id": result.command_id,
            "status": result.status,
            "completed": completed,
            "result": result.result,
            "error": result.error or result.message,
        }
        return ToolResult(
            call_id=call.id,
            name=self.schema.name,
            ok=completed,
            content=receipt,
            error_code=None if completed else result.status,
            error_message=None if completed else (result.error or result.message or result.status),
            metadata={"external_action": True, "completed": completed},
        )


class RuntimeCapabilityToolProvider:
    """Build one LLM tool per online capability contract.

    Devices never become tool names.  Devices implementing the same compatible
    contract are aggregated behind one tool, and their Companion names become
    the tool's dynamic target enum.  Invocation re-reads the owner blackboard
    through ``BodyControlService`` before resolving the physical provider.
    """

    def __init__(self, body_control) -> None:
        self._body = body_control

    async def assemble(self, context: TurnContext) -> tuple[list[ToolSchema], dict[str, ToolPort]]:
        if self._body is None or not context.owner_id or not context.companion_id:
            return [], {}
        try:
            devices = await self._body.list_devices(
                owner_id=context.owner_id,
                companion_id=context.companion_id,
                source_device_id=context.device_id,
                include_offline=False,
            )
        except Exception as exc:
            _log.warning("runtime capability contracts load failed: %s", exc)
            return [], {}

        schemas: list[ToolSchema] = []
        ports: dict[str, ToolPort] = {}
        for capability, providers in _compatible_contracts(devices):
            tool = _RuntimeCapabilityContractTool(
                self._body,
                capability=capability,
                provider_names=providers,
            )
            if tool.schema.name in ports:
                _log.warning(
                    "runtime capability tool-name collision: %s",
                    tool.schema.name,
                )
                continue
            schemas.append(tool.schema)
            ports[tool.schema.name] = tool
        return schemas, ports


def _compatible_contracts(
    devices: list[BodyDevice],
) -> list[tuple[BodyCapability, tuple[str, ...]]]:
    grouped: dict[tuple[str, int], list[tuple[BodyCapability, BodyDevice]]] = {}
    for device in devices:
        if not device.provider_companion_name.strip():
            continue
        for capability in device.capabilities:
            grouped.setdefault((capability.name, capability.version), []).append(
                (capability, device)
            )

    contracts: list[tuple[BodyCapability, tuple[str, ...]]] = []
    for contract_key in sorted(grouped):
        bindings = grouped[contract_key]
        schema_fingerprints = {_schema_fingerprint(capability) for capability, _device in bindings}
        if len(schema_fingerprints) != 1:
            _log.warning(
                "runtime capability contract conflict; hidden from LLM: %s.v%s",
                contract_key[0],
                contract_key[1],
            )
            continue
        capability = bindings[0][0]
        if _TARGET_FIELD in (capability.input_schema.get("properties") or {}):
            _log.warning(
                "runtime capability contract uses reserved input field %s: %s.v%s",
                _TARGET_FIELD,
                capability.name,
                capability.version,
            )
            continue
        providers = tuple(
            sorted(
                {
                    device.provider_companion_name.strip()
                    for _capability, device in bindings
                    if device.provider_companion_name.strip()
                },
                key=str.casefold,
            )
        )
        if providers:
            contracts.append((capability, providers))
    return contracts


def _tool_input_schema(
    capability: BodyCapability,
    provider_names: tuple[str, ...],
) -> dict:
    schema = deepcopy(capability.input_schema)
    properties = dict(schema.get("properties") or {})
    properties[_TARGET_FIELD] = {
        "type": "string",
        "enum": list(provider_names),
        "description": "Exact name of the online Companion that should perform the action.",
    }
    required = list(dict.fromkeys([*(schema.get("required") or []), _TARGET_FIELD]))
    schema["type"] = "object"
    schema["properties"] = properties
    schema["required"] = required
    return schema


def _schema_fingerprint(capability: BodyCapability) -> str:
    payload = {
        "input_schema": capability.input_schema,
        "result_schema": capability.result_schema,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _contract_tool_name(capability: BodyCapability) -> str:
    raw = f"cap_{capability.name.replace('.', '_')}_v{capability.version}"
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", raw)
    if len(normalized) <= _TOOL_NAME_LIMIT:
        return normalized
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{normalized[: _TOOL_NAME_LIMIT - 11]}_{digest}"


def _runtime_idempotency_key(
    *,
    owner_id: str,
    logical_trace_id: str,
    capability: BodyCapability,
    arguments: dict,
) -> str:
    payload = {
        "owner_id": owner_id,
        "logical_trace_id": logical_trace_id,
        "contract": capability.name,
        "contract_version": capability.version,
        "arguments": arguments,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "runtime-capability:" + hashlib.sha256(canonical).hexdigest()
