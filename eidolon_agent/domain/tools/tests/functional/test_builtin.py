"""Built-in tools — emit_event end-to-end via the dispatcher."""

from __future__ import annotations

import asyncio

import pytest

from eidolon_agent.core.types.memory import (
    MemoryForgetCandidate,
    MemoryForgetOutcome,
    MemoryForgetPreview,
    MemoryHit,
    MemoryKind,
    MemoryWriteOutcome,
)
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


async def test_memory_tools_use_memory_port(tool_ctx) -> None:
    tool_ctx.user_text = "请记住：我喜欢乌龙茶"
    memory = _FakeMemoryPort()
    reg = ToolRegistry()
    reg.register(MemorySearchTool(memory))
    reg.register(MemoryAssertFactTool(memory))
    reg.register(MemoryForgetTool(memory))
    disp = ToolDispatcher(reg)

    [search] = await disp.dispatch_batch(
        [_call("memory_search", {"query": "乌龙茶", "top_k": 2, "scope": "semantic"})],
        ctx=tool_ctx,
    )
    [asserted] = await disp.dispatch_batch(
        [
            _call(
                "memory_assert_fact",
                {"claim": "我喜欢乌龙茶"},
            )
        ],
        ctx=tool_ctx,
    )
    [forgotten] = await disp.dispatch_batch(
        [_call("memory_forget", {"query": "乌龙茶"})],
        ctx=tool_ctx,
    )

    assert search.ok
    assert search.content["records"][0]["content"] == "用户喜欢乌龙茶"
    assert memory.search_calls[0]["scope"] == "semantic"
    assert memory.search_calls[0]["companion_id"] == "companion-1"
    assert memory.search_calls[0]["memory_realm_id"] == "realm-1"
    assert memory.search_calls[0]["device_id"] == "device-1"
    assert asserted.ok
    assert asserted.content["status"] == "accepted"
    assert asserted.content["request_id"] == "request-1"
    assert memory.asserted == []
    assert memory.confirmed_facts == [
        (
            "owner-1",
            "companion-1",
            "realm-1",
            "device-1",
            "rs-test",
            "我喜欢乌龙茶",
            "turn-1",
            "c",
            0.99,
            ["memory_assert_fact", "verbatim", "current_request_grounded"],
        )
    ]
    assert forgotten.ok
    assert forgotten.content["status"] == "applied"
    assert forgotten.content["request_id"] == "request-1"
    assert forgotten.content["affected"] == 1
    assert memory.forgotten == [
        (
            "owner-1",
            "companion-1",
            "realm-1",
            "device-1",
                "乌龙茶",
                "archive",
                "rs-test",
            )
    ]


async def test_memory_tool_without_port_returns_error(tool_ctx) -> None:
    reg = ToolRegistry()
    reg.register(MemorySearchTool(None))

    [res] = await ToolDispatcher(reg).dispatch_batch(
        [_call("memory_search", {"query": "x"})],
        ctx=tool_ctx,
    )

    assert res.ok is False
    assert res.error_code == "memory_port_unavailable"


@pytest.mark.parametrize("claim", ["我在某个城市工作", "你记得我在哪工作吗？"])
async def test_memory_assert_fact_rejects_claim_sourced_from_background(
    tool_ctx,
    claim,
) -> None:
    tool_ctx.user_text = "你记得我在哪工作吗？"
    memory = _FakeMemoryPort()
    reg = ToolRegistry()
    reg.register(MemoryAssertFactTool(memory))

    [res] = await ToolDispatcher(reg).dispatch_batch(
        [
            _call(
                "memory_assert_fact",
                {"claim": claim},
            )
        ],
        ctx=tool_ctx,
    )

    assert res.ok is False
    assert res.error_code == "ungrounded_memory_claim"
    assert memory.asserted == []
    assert memory.confirmed_facts == []


async def test_explicit_sensitive_memory_request_is_direct_authorization(tool_ctx) -> None:
    claim = "我的家庭住址是北京市朝阳区测试路 1 号"
    tool_ctx.user_text = f"请记住：{claim}"
    memory = _FakeMemoryPort()
    reg = ToolRegistry()
    reg.register(MemoryAssertFactTool(memory))
    disp = ToolDispatcher(reg)

    [direct] = await disp.dispatch_batch(
        [_call("memory_assert_fact", {"claim": claim})],
        ctx=tool_ctx,
    )
    assert direct.ok
    assert direct.content["status"] == "accepted"
    assert memory.confirmed_facts[0][5] == claim
    assert "current_request_grounded" in memory.confirmed_facts[0][9]


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
        source_event_id,
        tool_call_id,
        confidence=0.9,
    ) -> str:
        self.asserted.append(
            (
                owner_id,
                companion_id,
                memory_realm_id,
                subject,
                predicate,
                object_,
                source_event_id,
                tool_call_id,
                confidence,
            )
        )
        return "request-1"

    async def write_confirmed_fact(
        self,
        owner_id,
        companion_id,
        memory_realm_id,
        device_id,
        session_id,
        text,
        *,
        source_event_id,
        tool_call_id,
        confidence=0.99,
        tags=None,
        wait_applied_seconds=0.75,
    ) -> MemoryWriteOutcome:
        self.confirmed_facts.append(
            (
                owner_id,
                companion_id,
                memory_realm_id,
                device_id,
                session_id,
                text,
                source_event_id,
                tool_call_id,
                confidence,
                list(tags or []),
            )
        )
        return MemoryWriteOutcome(status="accepted", request_id="request-1")

    async def preview_forget(
        self,
        owner_id,
        companion_id,
        memory_realm_id,
        device_id,
        query,
        *,
        action="archive",
        session_id="default",
    ) -> MemoryForgetPreview:
        self.forgotten.append(
            (owner_id, companion_id, memory_realm_id, device_id, query, action, session_id)
        )
        return MemoryForgetPreview(
            status="preview",
            target=query,
            action="archive",
            candidates=[MemoryForgetCandidate("drawer-1", query, 1.0)],
            confirmation_token="token-1",
        )

    async def confirm_forget(
        self,
        owner_id,
        companion_id,
        memory_realm_id,
        device_id,
        confirmation_token,
        *,
        session_id="default",
        wait_applied_seconds=2.0,
    ) -> MemoryForgetOutcome:
        return MemoryForgetOutcome(
            status="applied",
            action="archive",
            request_id="request-1",
            drawer_ids=["drawer-1"],
        )
