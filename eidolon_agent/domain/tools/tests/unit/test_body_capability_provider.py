"""Runtime blackboard catalog -> one stable LLM tool."""

from __future__ import annotations

from eidolon_sdk.biz.body import BodyCapability, BodyCommandResult, BodyDevice

from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.identity import CallerContext, CallerKind, Identity
from eidolon_agent.core.types.tool import ToolCall
from eidolon_agent.domain.tools.body_capability_provider import RuntimeCapabilityToolProvider


def _caller() -> CallerContext:
    return CallerContext(
        identity=Identity(
            owner_id="o1",
            companion_id="c1",
            device_id="box-3",
            memory_realm_id="r1",
            genome_id="g1",
        ),
        caller_kind=CallerKind.WEB_CHAT,
        trace_id="t",
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

    async def send_command(self, **kwargs):
        self.sent.append(kwargs)
        return BodyCommandResult(
            command_id="cmd", device_id=kwargs["target"], op=kwargs["op"], status="done"
        )


def _guard() -> BodyDevice:
    return BodyDevice(
        device_id="atk-guard",
        name="ATK Guard",
        aliases=("guard",),
        provider_companion_id="guard-companion",
        status="online_control",
        capabilities=(
            BodyCapability(
                name="device.roll_call",
                version=1,
                description="Play the local roll-call response",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
        ),
    )


async def test_assemble_builds_one_tool_and_structured_online_catalog():
    provider = RuntimeCapabilityToolProvider(_FakeBody([_guard()]))
    schemas, ports, catalog = await provider.assemble(_caller())

    assert [schema.name for schema in schemas] == ["invoke_device_capability"]
    assert list(ports) == ["invoke_device_capability"]
    assert '"device_id":"atk-guard"' in catalog
    assert '"name":"device.roll_call"' in catalog
    assert '"provider_companion_id":"guard-companion"' in catalog
    assert "invoke it instead of role-playing" in catalog
    assert "instead of speaking on the device's behalf" in schemas[0].description


async def test_dispatch_routes_exact_catalog_identifiers_and_arguments():
    body = _FakeBody([_guard()])
    _schemas, ports, _catalog = await RuntimeCapabilityToolProvider(body).assemble(_caller())
    tool = ports["invoke_device_capability"]
    result = await tool.invoke(
        ToolCall(
            id="1",
            name="invoke_device_capability",
            arguments={
                "target_device_id": "atk-guard",
                "capability_name": "device.roll_call",
                "arguments": {},
            },
        ),
        ctx=ToolInvocationContext(caller=_caller(), turn_id="turn-1"),
    )

    assert result.ok is True
    assert body.sent[0]["target"] == "atk-guard"
    assert body.sent[0]["op"] == "device.roll_call"
    assert body.sent[0]["payload"] == {}
    assert body.sent[0]["qos"] == "result"


async def test_no_online_capabilities_means_no_catalog_or_tool():
    assert await RuntimeCapabilityToolProvider(None).assemble(_caller()) == ([], {}, "")
    assert await RuntimeCapabilityToolProvider(_FakeBody([])).assemble(_caller()) == ([], {}, "")
