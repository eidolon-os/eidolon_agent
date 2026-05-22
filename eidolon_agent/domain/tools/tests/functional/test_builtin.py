"""Built-in tools — get_time and emit_event end-to-end via the dispatcher."""

from __future__ import annotations

import pytest

from eidolon_agent.core.types.tool import ToolCall
from eidolon_agent.domain.tools import ToolDispatcher, ToolRegistry
from eidolon_agent.domain.tools.builtin import EmitEventTool, GetTimeTool

pytestmark = pytest.mark.functional


def _call(name: str, args: dict | None = None) -> ToolCall:
    return ToolCall(id="c", name=name, arguments=args or {})


async def test_get_time_with_explicit_tz(caller_ctx) -> None:
    reg = ToolRegistry()
    reg.register(GetTimeTool())
    [res] = await ToolDispatcher(reg).dispatch_batch(
        [_call("get_time", {"timezone": "Asia/Shanghai"})], ctx=caller_ctx
    )
    assert res.ok
    assert res.content["tz"] == "Asia/Shanghai"
    assert "T" in res.content["iso"]


async def test_get_time_with_locale_fallback(caller_ctx) -> None:
    reg = ToolRegistry()
    reg.register(GetTimeTool())
    [res] = await ToolDispatcher(reg).dispatch_batch([_call("get_time")], ctx=caller_ctx)
    assert res.ok
    # default caller_ctx fixture uses locale="zh-CN" → Asia/Shanghai
    assert res.content["tz"] == "Asia/Shanghai"


async def test_get_time_invalid_tz_returns_error(caller_ctx) -> None:
    reg = ToolRegistry()
    reg.register(GetTimeTool())
    [res] = await ToolDispatcher(reg).dispatch_batch(
        [_call("get_time", {"timezone": "Made/Up"})], ctx=caller_ctx
    )
    assert res.ok is False
    assert res.error_code == "invalid_timezone"


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
