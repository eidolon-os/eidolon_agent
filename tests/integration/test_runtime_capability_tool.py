"""LLM-facing contract for the runtime blackboard capability path."""

from __future__ import annotations

from eidolon_sdk.biz.body import BodyCapability, BodyCommandResult, BodyDevice

from eidolon_agent.domain.tools.body_capability_provider import RuntimeCapabilityToolProvider
from eidolon_agent.infra.llm.providers.fake import FakeLLM
from tests.helpers import make_turn_input


class _CapturingLLM(FakeLLM):
    def __init__(self) -> None:
        super().__init__(
            script=[
                [
                    {
                        "kind": "tool_call",
                        "name": "invoke_device_capability",
                        "arguments": {
                            "target_device_id": "atk-guard",
                            "capability_name": "device.roll_call",
                            "arguments": {},
                        },
                    }
                ],
                [{"kind": "text", "text": "Guard 已经回应。"}],
            ],
            per_token_delay_s=0,
        )
        self.seen_messages = []
        self.seen_tools = []

    async def stream(self, messages, *, tools=None, **kwargs):
        self.seen_messages.append(list(messages))
        self.seen_tools.append(list(tools or []))
        async for delta in super().stream(messages, tools=tools, **kwargs):
            yield delta


class _RuntimeBody:
    def __init__(self) -> None:
        self.sent = []

    async def list_devices(self, **_kwargs):
        return [
            BodyDevice(
                device_id="atk-guard",
                name="ATK Guard",
                aliases=("guard",),
                provider_companion_id="companion-guard",
                status="online_control",
                capabilities=(
                    BodyCapability(
                        name="device.roll_call",
                        version=1,
                        description="Play local roll-call response",
                        input_schema={
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    ),
                ),
            )
        ]

    async def send_command(self, **kwargs):
        self.sent.append(kwargs)
        return BodyCommandResult(
            command_id="cmd-roll-call",
            device_id="atk-guard",
            op="device.roll_call",
            status="done",
            result={"played": True},
        )


async def test_voice_turn_sees_catalog_and_uses_only_stable_capability_tool(
    turn_engine_factory,
) -> None:
    llm = _CapturingLLM()
    body = _RuntimeBody()
    engine = turn_engine_factory(
        llm=llm,
        body_capability_provider=RuntimeCapabilityToolProvider(body),
    )

    events = [event async for event in engine.run(make_turn_input("guard在吗，点名一下"))]

    first_system = llm.seen_messages[0][0].content
    visible_names = {schema.name for schema in llm.seen_tools[0]}
    assert "[RUNTIME_DEVICE_CAPABILITY_CATALOG]" in first_system
    assert '"device_id":"atk-guard"' in first_system
    assert '"name":"device.roll_call"' in first_system
    assert "invoke_device_capability" in visible_names
    assert "control_body_device" not in visible_names
    assert not any(name.startswith("body__") for name in visible_names)
    assert body.sent[0]["target"] == "atk-guard"
    assert body.sent[0]["op"] == "device.roll_call"
    assert any(
        event.kind.value == "tool_result"
        and event.data["name"] == "invoke_device_capability"
        and event.data["ok"] is True
        for event in events
    )
