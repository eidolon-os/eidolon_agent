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
    MemorySearchTool,
)

pytestmark = pytest.mark.functional


def _call(name: str, args: dict | None = None) -> ToolCall:
    return ToolCall(id="c", name=name, arguments=args or {})


async def test_emit_event_publishes_to_bus(event_bus, tool_ctx) -> None:
    received: list = []

    async def _handler(ev) -> None:
        received.append(ev)

    await event_bus.subscribe("agent.test.fired", _handler)
    reg = ToolRegistry()
    reg.register(EmitEventTool(event_bus=event_bus))
    [res] = await ToolDispatcher(reg).dispatch_batch(
        [_call("emit_event", {"subject": "agent.test.fired", "payload": {"k": "v"}})],
        ctx=tool_ctx,
    )
    await asyncio.sleep(0)
    assert res.ok
    assert len(received) == 1
    assert received[0].payload == {"k": "v"}


async def test_emit_event_without_bus_returns_error(tool_ctx) -> None:
    reg = ToolRegistry()
    reg.register(EmitEventTool(event_bus=None))
    [res] = await ToolDispatcher(reg).dispatch_batch(
        [_call("emit_event", {"subject": "x"})], ctx=tool_ctx
    )
    assert res.ok is False
    assert res.error_code == "event_bus_unavailable"


async def test_get_time_returns_locale_timezone(tool_ctx) -> None:
    reg = ToolRegistry()
    reg.register(GetTimeTool())

    [res] = await ToolDispatcher(reg).dispatch_batch([_call("get_time")], ctx=tool_ctx)

    assert res.ok
    assert res.content["timezone"] == "Asia/Shanghai"
    assert res.content["locale"] == "zh-CN"
    assert "iso_datetime" in res.content


async def test_get_time_rejects_unknown_timezone(tool_ctx) -> None:
    reg = ToolRegistry()
    reg.register(GetTimeTool())

    [res] = await ToolDispatcher(reg).dispatch_batch(
        [_call("get_time", {"timezone": "Mars/Olympus"})],
        ctx=tool_ctx,
    )

    assert res.ok is False
    assert res.error_code == "invalid_timezone"


async def test_get_weather_uses_injected_fetcher(tool_ctx) -> None:
    async def fetcher(location: str, lang: str) -> dict:
        return {"location": location, "lang": lang, "temperature": 26}

    reg = ToolRegistry()
    reg.register(GetWeatherTool(fetcher=fetcher, default_location="上海"))

    [res] = await ToolDispatcher(reg).dispatch_batch(
        [_call("get_weather", {"location": "杭州"})],
        ctx=tool_ctx,
    )

    assert res.ok
    assert res.content == {"location": "杭州", "lang": "zh", "temperature": 26}


async def test_memory_search_uses_memory_port(tool_ctx) -> None:
    memory = _FakeMemoryPort()
    reg = ToolRegistry()
    reg.register(MemorySearchTool(memory))
    disp = ToolDispatcher(reg)

    [search] = await disp.dispatch_batch(
        [_call("memory_search", {"query": "乌龙茶", "top_k": 2, "scope": "semantic"})],
        ctx=tool_ctx,
    )
    assert search.ok
    assert search.content["records"][0]["content"] == "用户喜欢乌龙茶"
    assert memory.search_calls[0]["scope"] == "semantic"
    assert memory.search_calls[0]["companion_id"] == "companion-1"
    assert memory.search_calls[0]["memory_realm_id"] == "realm-1"
    assert memory.search_calls[0]["device_id"] == "device-1"


async def test_memory_tool_without_port_returns_error(tool_ctx) -> None:
    reg = ToolRegistry()
    reg.register(MemorySearchTool(None))

    [res] = await ToolDispatcher(reg).dispatch_batch(
        [_call("memory_search", {"query": "x"})],
        ctx=tool_ctx,
    )

    assert res.ok is False
    assert res.error_code == "memory_port_unavailable"


class _FakeMemoryPort:
    def __init__(self) -> None:
        self.search_calls: list[dict] = []

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
