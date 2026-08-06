"""Online capability contracts -> Companion-targeted dynamic LLM tools."""

from __future__ import annotations

from eidolon_sdk.biz.body import BodyCapability, BodyCommandResult, BodyDevice

from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.tool import ToolCall
from eidolon_agent.core.types.turn_context import TurnContext
from eidolon_agent.domain.tools.body_capability_provider import RuntimeCapabilityToolProvider


def _context(*, trace_id: str = "t") -> TurnContext:
    return TurnContext(
        owner_id="o1",
        companion_id="c1",
        device_id="source-device",
        memory_realm_id="r1",
        genome_id="g1",
        trace_id=trace_id,
        request_id="r",
    )


class _FakeBody:
    def __init__(self, devices):
        self._devices = devices
        self.sent: list[dict] = []

    async def list_devices(self, **_kwargs):
        return self._devices

    async def send_companion_capability(self, **kwargs):
        self.sent.append(kwargs)
        return BodyCommandResult(
            command_id="cmd",
            device_id="resolved-device",
            op=kwargs["capability_name"],
            status="done",
            capability_version=kwargs["capability_version"],
            result={"applied": True},
        )


def _device(
    device_id: str,
    *,
    companion_id: str,
    companion_name: str,
    schema: dict | None = None,
) -> BodyDevice:
    return BodyDevice(
        device_id=device_id,
        name=device_id,
        provider_companion_id=companion_id,
        provider_companion_name=companion_name,
        status="online_control",
        capabilities=(
            BodyCapability(
                name="lighting.set_state",
                version=1,
                description="Set the light state.",
                input_schema=schema
                or {
                    "type": "object",
                    "properties": {"enabled": {"type": "boolean"}},
                    "required": ["enabled"],
                    "additionalProperties": False,
                },
                result_schema={
                    "type": "object",
                    "properties": {"applied": {"type": "boolean"}},
                },
            ),
        ),
    )


async def test_assemble_groups_same_contract_across_companions_into_one_tool():
    provider = RuntimeCapabilityToolProvider(
        _FakeBody(
            [
                _device("light-a", companion_id="ca", companion_name="客厅"),
                _device("light-b", companion_id="cb", companion_name="卧室"),
            ]
        )
    )

    schemas, ports = await provider.assemble(_context())

    assert [schema.name for schema in schemas] == ["cap_lighting_set_state_v1"]
    assert list(ports) == ["cap_lighting_set_state_v1"]
    parameters = schemas[0].json_schema
    assert parameters["properties"]["target_companion"]["enum"] == ["卧室", "客厅"]
    assert parameters["properties"]["enabled"] == {"type": "boolean"}
    assert parameters["required"] == ["enabled", "target_companion"]
    assert "device id" in schemas[0].description


async def test_dispatch_resolves_companion_at_invocation_and_returns_receipt():
    body = _FakeBody([_device("light-a", companion_id="ca", companion_name="客厅")])
    _schemas, ports = await RuntimeCapabilityToolProvider(body).assemble(_context())
    tool = ports["cap_lighting_set_state_v1"]

    result = await tool.invoke(
        ToolCall(
            id="1",
            name="cap_lighting_set_state_v1",
            arguments={"target_companion": "客厅", "enabled": True},
        ),
        ctx=ToolInvocationContext(
            turn_context=_context(),
            input_modality="text",
            turn_id="turn-1",
            session_id="rs",
        ),
    )

    assert result.ok is True
    assert body.sent[0]["target_companion"] == "客厅"
    assert body.sent[0]["capability_name"] == "lighting.set_state"
    assert body.sent[0]["capability_version"] == 1
    assert body.sent[0]["payload"] == {"enabled": True}
    assert result.content == {
        "kind": "external_action_receipt",
        "contract": "lighting.set_state",
        "contract_version": 1,
        "target_companion": "客厅",
        "resolved_device_id": "resolved-device",
        "command_id": "cmd",
        "status": "done",
        "completed": True,
        "result": {"applied": True},
        "error": "",
    }


async def test_runtime_idempotency_follows_logical_trace_not_agent_turn_id():
    body = _FakeBody([_device("light-a", companion_id="ca", companion_name="客厅")])
    _schemas, ports = await RuntimeCapabilityToolProvider(body).assemble(_context())
    tool = ports["cap_lighting_set_state_v1"]
    arguments = {"target_companion": "客厅", "enabled": True}

    for call_id, turn_id in (("call-1", "turn-1"), ("call-2", "turn-retry")):
        await tool.invoke(
            ToolCall(
                id=call_id,
                name="cap_lighting_set_state_v1",
                arguments=arguments,
            ),
            ctx=ToolInvocationContext(
                turn_context=_context(trace_id="logical-trace"),
                input_modality="text",
                turn_id=turn_id,
                session_id="rs",
            ),
        )
    await tool.invoke(
        ToolCall(
            id="call-3",
            name="cap_lighting_set_state_v1",
            arguments=arguments,
        ),
        ctx=ToolInvocationContext(
            turn_context=_context(trace_id="new-logical-trace"),
            input_modality="text",
            turn_id="turn-3",
            session_id="rs",
        ),
    )

    assert body.sent[0]["idempotency_key"] == body.sent[1]["idempotency_key"]
    assert body.sent[2]["idempotency_key"] != body.sent[0]["idempotency_key"]
    assert body.sent[0]["runtime_turn_id"] == "turn-1"
    assert body.sent[1]["runtime_turn_id"] == "turn-retry"


async def test_conflicting_contract_schemas_are_hidden_fail_closed():
    body = _FakeBody(
        [
            _device("light-a", companion_id="ca", companion_name="客厅"),
            _device(
                "light-b",
                companion_id="cb",
                companion_name="卧室",
                schema={
                    "type": "object",
                    "properties": {"level": {"type": "integer"}},
                    "required": ["level"],
                    "additionalProperties": False,
                },
            ),
        ]
    )

    assert await RuntimeCapabilityToolProvider(body).assemble(_context()) == ([], {})


async def test_no_online_capabilities_means_no_dynamic_tool():
    assert await RuntimeCapabilityToolProvider(None).assemble(_context()) == ([], {})
    assert await RuntimeCapabilityToolProvider(_FakeBody([])).assemble(_context()) == ([], {})
