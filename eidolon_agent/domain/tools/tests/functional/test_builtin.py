"""Built-in tools — emit_event end-to-end via the dispatcher."""

from __future__ import annotations

import asyncio

import pytest
from eidolon_sdk.biz.body import BodyCapability, BodyCommandResult, BodyDevice

from eidolon_agent.core.types.memory import MemoryHit, MemoryKind
from eidolon_agent.core.types.tool import ToolCall
from eidolon_agent.domain.tools import ToolDispatcher, ToolRegistry
from eidolon_agent.domain.tools.builtin import (
    ControlBodyDeviceTool,
    EmitEventTool,
    GetBodyCommandStatusTool,
    GetTimeTool,
    GetWeatherTool,
    ListBodyDevicesTool,
    MemoryAssertFactTool,
    MemoryForgetTool,
    MemorySearchTool,
)

pytestmark = pytest.mark.functional


def _call(name: str, args: dict | None = None) -> ToolCall:
    return ToolCall(id="c", name=name, arguments=args or {})


async def test_emit_event_publishes_to_bus(event_bus, caller_ctx) -> None:
    received: list = []

    async def _handler(ev) -> None:
        received.append(ev)

    await event_bus.subscribe("agent.test.fired", _handler)
    reg = ToolRegistry()
    reg.register(EmitEventTool(event_bus=event_bus))
    [res] = await ToolDispatcher(reg).dispatch_batch(
        [_call("emit_event", {"subject": "agent.test.fired", "payload": {"k": "v"}})],
        ctx=caller_ctx,
    )
    await asyncio.sleep(0)
    assert res.ok
    assert len(received) == 1
    assert received[0].payload == {"k": "v"}


async def test_emit_event_without_bus_returns_error(caller_ctx) -> None:
    reg = ToolRegistry()
    reg.register(EmitEventTool(event_bus=None))
    [res] = await ToolDispatcher(reg).dispatch_batch(
        [_call("emit_event", {"subject": "x"})], ctx=caller_ctx
    )
    assert res.ok is False
    assert res.error_code == "event_bus_unavailable"


async def test_get_time_returns_locale_timezone(caller_ctx) -> None:
    reg = ToolRegistry()
    reg.register(GetTimeTool())

    [res] = await ToolDispatcher(reg).dispatch_batch([_call("get_time")], ctx=caller_ctx)

    assert res.ok
    assert res.content["timezone"] == "Asia/Shanghai"
    assert res.content["locale"] == "zh-CN"
    assert "iso_datetime" in res.content


async def test_get_time_rejects_unknown_timezone(caller_ctx) -> None:
    reg = ToolRegistry()
    reg.register(GetTimeTool())

    [res] = await ToolDispatcher(reg).dispatch_batch(
        [_call("get_time", {"timezone": "Mars/Olympus"})],
        ctx=caller_ctx,
    )

    assert res.ok is False
    assert res.error_code == "invalid_timezone"


async def test_get_weather_uses_injected_fetcher(caller_ctx) -> None:
    async def fetcher(location: str, lang: str) -> dict:
        return {"location": location, "lang": lang, "temperature": 26}

    reg = ToolRegistry()
    reg.register(GetWeatherTool(fetcher=fetcher, default_location="上海"))

    [res] = await ToolDispatcher(reg).dispatch_batch(
        [_call("get_weather", {"location": "杭州"})],
        ctx=caller_ctx,
    )

    assert res.ok
    assert res.content == {"location": "杭州", "lang": "zh", "temperature": 26}


async def test_memory_tools_use_memory_port(caller_ctx) -> None:
    memory = _FakeMemoryPort()
    reg = ToolRegistry()
    reg.register(MemorySearchTool(memory))
    reg.register(MemoryAssertFactTool(memory))
    reg.register(MemoryForgetTool(memory))
    disp = ToolDispatcher(reg)

    [search] = await disp.dispatch_batch(
        [_call("memory_search", {"query": "乌龙茶", "top_k": 2, "scope": "semantic"})],
        ctx=caller_ctx,
    )
    [asserted] = await disp.dispatch_batch(
        [
            _call(
                "memory_assert_fact",
                {
                    "subject": "user",
                    "predicate": "工作地点",
                    "object": "乌龙茶",
                    "confidence": 0.8,
                },
            )
        ],
        ctx=caller_ctx,
    )
    [forgotten] = await disp.dispatch_batch(
        [_call("memory_forget", {"query": "乌龙茶"})],
        ctx=caller_ctx,
    )

    assert search.ok
    assert search.content["records"][0]["content"] == "用户喜欢乌龙茶"
    assert memory.search_calls[0]["scope"] == "semantic"
    assert memory.search_calls[0]["companion_id"] == "companion-1"
    assert memory.search_calls[0]["memory_realm_id"] == "realm-1"
    assert memory.search_calls[0]["device_id"] == "device-1"
    assert asserted.ok
    assert memory.asserted == [
        ("owner-1", "companion-1", "realm-1", "user", "works_at", "乌龙茶", 0.8)
    ]
    assert forgotten.ok
    assert forgotten.content["removed"] == 3
    assert memory.forgotten == [
        (
            "owner-1",
            "companion-1",
            "realm-1",
            "device-1",
            "乌龙茶",
            "default",
        )
    ]


async def test_memory_tool_without_port_returns_error(caller_ctx) -> None:
    reg = ToolRegistry()
    reg.register(MemorySearchTool(None))

    [res] = await ToolDispatcher(reg).dispatch_batch(
        [_call("memory_search", {"query": "x"})],
        ctx=caller_ctx,
    )

    assert res.ok is False
    assert res.error_code == "memory_port_unavailable"


async def test_body_control_tools_use_body_service(caller_ctx) -> None:
    body = _FakeBodyControl()
    reg = ToolRegistry()
    reg.register(ListBodyDevicesTool(body))
    reg.register(ControlBodyDeviceTool(body))
    reg.register(GetBodyCommandStatusTool(body))
    disp = ToolDispatcher(reg)

    [listed] = await disp.dispatch_batch(
        [_call("list_body_devices", {"include_offline": True})],
        ctx=caller_ctx,
    )
    [sent] = await disp.dispatch_batch(
        [
            _call(
                "control_body_device",
                {
                    "target": "box-3",
                    "op": "sound.play",
                    "payload": {"sound": "ping"},
                },
            )
        ],
        ctx=caller_ctx,
    )
    [status] = await disp.dispatch_batch(
        [_call("get_body_command_status", {"command_id": "cmd-1"})],
        ctx=caller_ctx,
    )

    assert listed.ok
    assert listed.content["devices"][0]["capabilities"][0]["name"] == "sound.play"
    assert sent.ok
    assert sent.content["command_id"] == "cmd-1"
    assert body.sent[0]["source_device_id"] == "device-1"
    assert body.sent[0]["runtime_caller_id"] == "rc-test"
    assert body.sent[0]["runtime_session_id"] == "rs-test"
    assert status.ok
    assert status.content["status"] == "done"


async def test_body_control_tool_without_service_returns_error(caller_ctx) -> None:
    reg = ToolRegistry()
    reg.register(ControlBodyDeviceTool(None))

    [res] = await ToolDispatcher(reg).dispatch_batch(
        [_call("control_body_device", {"target": "box-3", "op": "sound.play"})],
        ctx=caller_ctx,
    )

    assert res.ok is False
    assert res.error_code == "body_control_unavailable"


async def test_memory_assert_fact_falls_back_to_confirmed_fact_for_unknown_predicate(
    caller_ctx,
) -> None:
    memory = _FakeMemoryPort()
    reg = ToolRegistry()
    reg.register(MemoryAssertFactTool(memory))

    [res] = await ToolDispatcher(reg).dispatch_batch(
        [
            _call(
                "memory_assert_fact",
                {"subject": "user", "predicate": "神秘关系", "object": "x"},
            )
        ],
        ctx=caller_ctx,
    )

    assert res.ok is True
    assert res.content["kind"] == "confirmed_fact"
    assert res.content["text"] == "user 神秘关系 x"
    assert memory.asserted == []
    assert memory.confirmed_facts == [
        (
            "owner-1",
            "companion-1",
            "realm-1",
            "device-1",
            None,
            "user 神秘关系 x",
            0.9,
            ["memory_assert_fact", "kg_fallback"],
        )
    ]


class _FakeBodyControl:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def list_devices(
        self,
        *,
        owner_id: str,
        companion_id: str,
        source_device_id: str | None,
        include_offline: bool = True,
    ):
        return [
            BodyDevice(
                device_id="box-3",
                name="box-3",
                status="online_control",
                is_current_device=False,
                capabilities=(BodyCapability(name="sound.play"),),
            )
        ]

    async def send_command(self, **kwargs):
        self.sent.append(kwargs)
        return BodyCommandResult(
            command_id="cmd-1",
            device_id="box-3",
            op="sound.play",
            status="sent",
        )

    async def get_command_status(
        self,
        *,
        owner_id: str,
        companion_id: str,
        command_id: str,
    ):
        return BodyCommandResult(
            command_id=command_id,
            device_id="box-3",
            op="sound.play",
            status="done",
        )


class _FakeMemoryPort:
    def __init__(self) -> None:
        self.search_calls: list[dict] = []
        self.asserted: list[tuple] = []
        self.confirmed_facts: list[tuple] = []
        self.forgotten: list[tuple] = []

    async def search(
        self,
        owner_id,
        query,
        *,
        companion_id,
        memory_realm_id,
        device_id,
        top_k=5,
        scope=None,
        voice=True,
        timeout_s=0.2,
        session_id="default",
    ):
        self.search_calls.append(
            {
                "owner_id": owner_id,
                "companion_id": companion_id,
                "memory_realm_id": memory_realm_id,
                "device_id": device_id,
                "query": query,
                "top_k": top_k,
                "scope": getattr(scope, "value", scope),
                "voice": voice,
                "timeout_s": timeout_s,
                "session_id": session_id,
            }
        )
        return [
            MemoryHit(
                id="m1",
                content="用户喜欢乌龙茶",
                kind=MemoryKind.PREFERENCE,
                similarity=0.91,
            )
        ]

    async def assert_fact(
        self,
        owner_id,
        companion_id,
        memory_realm_id,
        subject,
        predicate,
        object_,
        *,
        confidence=0.9,
    ) -> None:
        self.asserted.append(
            (owner_id, companion_id, memory_realm_id, subject, predicate, object_, confidence)
        )

    async def write_confirmed_fact(
        self,
        owner_id,
        companion_id,
        memory_realm_id,
        device_id,
        session_id,
        text,
        *,
        confidence=0.99,
        tags=None,
    ) -> None:
        self.confirmed_facts.append(
            (
                owner_id,
                companion_id,
                memory_realm_id,
                device_id,
                session_id,
                text,
                confidence,
                list(tags or []),
            )
        )

    async def forget(
        self,
        owner_id,
        companion_id,
        memory_realm_id,
        device_id,
        query,
        *,
        session_id="default",
    ) -> int:
        self.forgotten.append(
            (owner_id, companion_id, memory_realm_id, device_id, query, session_id)
        )
        return 3
