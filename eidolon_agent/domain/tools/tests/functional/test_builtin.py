"""Built-in tools — emit_event end-to-end via the dispatcher."""

from __future__ import annotations

import asyncio

import pytest

from eidolon_agent.core.types.memory import MemoryHit, MemoryKind
from eidolon_agent.core.types.tool import ToolCall
from eidolon_agent.domain.tools import ToolDispatcher, ToolRegistry
from eidolon_agent.domain.tools.builtin import (
    EmitEventTool,
    GetTimeTool,
    GetWeatherTool,
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
                    "predicate": "prefers_drink",
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
    assert asserted.ok
    assert memory.asserted == [("u", "user", "prefers_drink", "乌龙茶", 0.8)]
    assert forgotten.ok
    assert forgotten.content["removed"] == 3
    assert memory.forgotten == [("u", "乌龙茶")]


async def test_memory_tool_without_port_returns_error(caller_ctx) -> None:
    reg = ToolRegistry()
    reg.register(MemorySearchTool(None))

    [res] = await ToolDispatcher(reg).dispatch_batch(
        [_call("memory_search", {"query": "x"})],
        ctx=caller_ctx,
    )

    assert res.ok is False
    assert res.error_code == "memory_port_unavailable"


class _FakeMemoryPort:
    def __init__(self) -> None:
        self.search_calls: list[dict] = []
        self.asserted: list[tuple] = []
        self.forgotten: list[tuple] = []

    async def search(
        self,
        user_id,
        query,
        *,
        top_k=5,
        scope=None,
        voice=True,
        timeout_s=0.2,
    ):
        self.search_calls.append(
            {
                "user_id": user_id,
                "query": query,
                "top_k": top_k,
                "scope": getattr(scope, "value", scope),
                "voice": voice,
                "timeout_s": timeout_s,
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
        user_id,
        subject,
        predicate,
        object_,
        *,
        confidence=0.9,
        tenant_id=None,
        persona_id=None,
    ) -> None:
        self.asserted.append((user_id, subject, predicate, object_, confidence))

    async def forget(self, user_id, query) -> int:
        self.forgotten.append((user_id, query))
        return 3
