"""Online capability contracts -> Companion-targeted dynamic LLM tools."""

from __future__ import annotations

from eidolon_sdk.biz.body import BodyCapability, BodyCommandResult, BodyDevice

from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.identity import CallerContext, CallerKind, Identity
from eidolon_agent.core.types.tool import ToolCall
from eidolon_agent.domain.tools.body_capability_provider import RuntimeCapabilityToolProvider


def _caller(*, trace_id: str = "t") -> CallerContext:
    return CallerContext(
        identity=Identity(
            owner_id="o1",
            companion_id="c1",
            device_id="source-device",
            memory_realm_id="r1",
            genome_id="g1",
        ),
        caller_kind=CallerKind.WEB_CHAT,
        trace_id=trace_id,
        request_id="r",
        runtime_caller_id="rc",
        runtime_session_id="rs",
        actor_kind="web_chat",
        actor_id="a",
        display_name="A",
        transport="test",
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

    schemas, ports = await provider.assemble(_caller())

    assert [schema.name for schema in schemas] == ["cap_lighting_set_state_v1"]
    assert list(ports) == ["cap_lighting_set_state_v1"]
    parameters = schemas[0].json_schema
    assert parameters["properties"]["target_companion"]["enum"] == ["卧室", "客厅"]
    assert parameters["properties"]["enabled"] == {"type": "boolean"}
    assert parameters["required"] == ["enabled", "target_companion"]
    assert "device id" in schemas[0].description


async def test_dispatch_resolves_companion_at_invocation_and_returns_receipt():
    body = _FakeBody([_device("light-a", companion_id="ca", companion_name="客厅")])
    _schemas, ports = await RuntimeCapabilityToolProvider(body).assemble(_caller())
    tool = ports["cap_lighting_set_state_v1"]

    result = await tool.invoke(
        ToolCall(
            id="1",
            name="cap_lighting_set_state_v1",
            arguments={"target_companion": "客厅", "enabled": True},
        ),
        ctx=ToolInvocationContext(caller=_caller(), turn_id="turn-1"),
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
    _schemas, ports = await RuntimeCapabilityToolProvider(body).assemble(_caller())
    tool = ports["cap_lighting_set_state_v1"]
    arguments = {"target_companion": "客厅", "enabled": True}

    for call_id, turn_id in (("call-1", "turn-1"), ("call-2", "turn-retry")):
        await tool.invoke(
            ToolCall(
                id=call_id,
                name="cap_lighting_set_state_v1",
                arguments=arguments,
            ),
            ctx=ToolInvocationContext(caller=_caller(trace_id="logical-trace"), turn_id=turn_id),
        )
    await tool.invoke(
        ToolCall(
            id="call-3",
            name="cap_lighting_set_state_v1",
            arguments=arguments,
        ),
        ctx=ToolInvocationContext(caller=_caller(trace_id="new-logical-trace"), turn_id="turn-3"),
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

    assert await RuntimeCapabilityToolProvider(body).assemble(_caller()) == ([], {})


async def test_no_online_capabilities_means_no_dynamic_tool():
    assert await RuntimeCapabilityToolProvider(None).assemble(_caller()) == ([], {})
    assert await RuntimeCapabilityToolProvider(_FakeBody([])).assemble(_caller()) == ([], {})
