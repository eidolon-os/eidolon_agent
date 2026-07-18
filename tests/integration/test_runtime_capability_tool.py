"""LLM-facing contract for dynamic Companion capability tools."""

from __future__ import annotations

from eidolon_sdk.biz.body import BodyCapability, BodyCommandResult, BodyDevice

from eidolon_agent.domain.tools.body_capability_provider import RuntimeCapabilityToolProvider
from eidolon_agent.infra.llm.providers.fake import FakeLLM
from tests.helpers import make_turn_input

_TOOL_NAME = "cap_lighting_set_state_v1"


class _CapturingLLM(FakeLLM):
    def __init__(self) -> None:
        super().__init__(
            script=[
                [
                    {
                        "kind": "tool_call",
                        "name": _TOOL_NAME,
                        "arguments": {
                            "target_companion": "客厅",
                            "enabled": True,
                        },
                    }
                ],
                [{"kind": "text", "text": "客厅已经完成操作。"}],
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
                device_id="light-controller",
                name="Light Controller",
                provider_companion_id="companion-living-room",
                provider_companion_name="客厅",
                status="online_control",
                capabilities=(
                    BodyCapability(
                        name="lighting.set_state",
                        version=1,
                        description="Set the light state.",
                        input_schema={
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
        ]

    async def send_companion_capability(self, **kwargs):
        self.sent.append(kwargs)
        return BodyCommandResult(
            command_id="cmd-1",
            device_id="light-controller",
            op="lighting.set_state",
            status="done",
            capability_version=1,
            result={"applied": True},
        )


async def test_voice_turn_uses_contract_tool_and_companion_target(
    turn_engine_factory,
) -> None:
    llm = _CapturingLLM()
    body = _RuntimeBody()
    engine = turn_engine_factory(
        llm=llm,
        body_capability_provider=RuntimeCapabilityToolProvider(body),
    )

    events = [event async for event in engine.run(make_turn_input("让客厅打开灯"))]

    first_system = llm.seen_messages[0][0].content
    visible = {schema.name: schema for schema in llm.seen_tools[0]}
    assert "[RUNTIME_DEVICE_CAPABILITY_CATALOG]" not in first_system
    assert _TOOL_NAME in visible
    assert visible[_TOOL_NAME].json_schema["properties"]["target_companion"]["enum"] == ["客厅"]
    assert body.sent[0]["target_companion"] == "客厅"
    assert body.sent[0]["capability_name"] == "lighting.set_state"
    assert body.sent[0]["payload"] == {"enabled": True}
    assert any(
        event.kind.value == "tool_result"
        and event.data["name"] == _TOOL_NAME
        and event.data["ok"] is True
        and event.data["content"]["completed"] is True
        for event in events
    )
