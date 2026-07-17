"""P4: device capabilities -> per-companion synthetic tools."""

from __future__ import annotations

from eidolon_sdk.biz.body import BodyCapability, BodyCommandResult, BodyDevice

from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.identity import CallerContext, CallerKind, Identity
from eidolon_agent.core.types.tool import ToolCall
from eidolon_agent.domain.tools.body_capability_provider import BodyCapabilityToolProvider


def _caller() -> CallerContext:
    return CallerContext(
        identity=Identity(
            owner_id="o1",
            companion_id="c1",
            device_id="dev-1",
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

    async def list_devices(self, *, owner_id, companion_id, source_device_id=None, include_offline=True):
        return self._devices

    async def send_command(self, **kwargs):
        self.sent.append(kwargs)
        return BodyCommandResult(
            command_id="cmd", device_id=kwargs["target"], op=kwargs["op"], status="done"
        )


def _device(device_id="dev-1", name="Living Room", *, caps, current=False, status="online"):
    return BodyDevice(
        device_id=device_id,
        name=name,
        status=status,
        is_current_device=current,
        capabilities=tuple(caps),
    )


async def test_assemble_builds_one_tool_per_capability():
    dev = _device(
        caps=[
            BodyCapability(
                name="display.update",
                description="Update the screen",
                input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            ),
            BodyCapability(name="sound.play", description="Play a sound"),
        ],
        current=True,
    )
    provider = BodyCapabilityToolProvider(_FakeBody([dev]))
    schemas, ports = await provider.assemble(_caller())
    names = {s.name for s in schemas}
    assert names == {"body__dev_1__display_update", "body__dev_1__sound_play"}
    assert set(ports) == names


async def test_requires_confirmation_adds_confirmed_property():
    dev = _device(caps=[BodyCapability(name="device.reboot", requires_confirmation=True, risk_level="high")])
    provider = BodyCapabilityToolProvider(_FakeBody([dev]))
    schemas, _ports = await provider.assemble(_caller())
    assert "confirmed" in schemas[0].json_schema.get("properties", {})


async def test_dispatch_routes_to_send_command_with_target_op_payload():
    dev = _device(caps=[BodyCapability(name="display.update")])
    body = _FakeBody([dev])
    provider = BodyCapabilityToolProvider(body)
    _schemas, ports = await provider.assemble(_caller())
    tool = ports["body__dev_1__display_update"]
    ctx = ToolInvocationContext(caller=_caller(), turn_id="turn-1")
    call = ToolCall(
        id="1",
        name="body__dev_1__display_update",
        arguments={"text": "hi", "confirmed": True},
    )
    res = await tool.invoke(call, ctx=ctx)
    assert res.ok is True
    assert len(body.sent) == 1
    sent = body.sent[0]
    assert sent["target"] == "dev-1"
    assert sent["op"] == "display.update"
    assert sent["payload"] == {"text": "hi"}  # confirmed is popped out of the payload
    assert sent["confirmed"] is True
    assert sent["qos"] == "result"


async def test_budget_truncates_and_none_body_is_empty():
    caps = [BodyCapability(name=f"op.{i}") for i in range(20)]
    provider = BodyCapabilityToolProvider(_FakeBody([_device(caps=caps)]), budget_tokens=240)
    schemas, ports = await provider.assemble(_caller())
    assert len(schemas) == 2  # 240 // 120 approx tokens-per-tool
    assert len(ports) == 2

    empty = BodyCapabilityToolProvider(None)
    assert await empty.assemble(_caller()) == ([], {})
