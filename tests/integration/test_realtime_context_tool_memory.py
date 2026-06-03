"""Cross-module regressions for realtime/context/tool/memory guardrails."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason
from eidolon_agent.core.types.messages import ChatMessage
from eidolon_agent.core.types.turn import TurnEventKind
from eidolon_agent.domain.tools import ToolDispatcher, ToolRegistry
from eidolon_agent.domain.tools.builtin import EmitEventTool, GetTimeTool
from eidolon_agent.infra.llm.providers.fake import FakeLLM
from tests.helpers import make_turn_input

pytestmark = pytest.mark.integration


async def test_tool_turn_dispatches_result_and_continues_llm(turn_engine_factory) -> None:
    llm = FakeLLM(
        script=[
            [{"kind": "tool_call", "name": "get_time", "arguments": {"timezone": "Asia/Shanghai"}}],
            [{"kind": "text", "text": "工具完成。"}],
        ],
        per_token_delay_s=0,
    )
    engine = turn_engine_factory(llm=llm)

    events = [ev async for ev in engine.run(make_turn_input("现在几点？"))]

    assert any(ev.kind is TurnEventKind.TOOL_CALL for ev in events)
    tool_results = [ev for ev in events if ev.kind is TurnEventKind.TOOL_RESULT]
    assert tool_results and tool_results[0].data["ok"] is True
    assert "工具完成" in "".join(
        ev.data.get("text", "") for ev in events if ev.kind is TurnEventKind.DELTA
    )


async def test_tool_permission_error_is_returned_to_llm_without_side_effect(
    turn_engine_factory, event_bus,
) -> None:
    received = []

    async def _on(ev):
        received.append(ev)

    await event_bus.subscribe("agent.test.blocked", _on)
    reg = ToolRegistry()
    reg.register(EmitEventTool(event_bus=event_bus))
    dispatcher = ToolDispatcher(reg, allowed_permissions=set())
    llm = FakeLLM(
        script=[
            [{
                "kind": "tool_call",
                "name": "emit_event",
                "arguments": {"subject": "agent.test.blocked"},
            }],
            [{"kind": "text", "text": "权限不足。"}],
        ],
        per_token_delay_s=0,
    )
    engine = turn_engine_factory(llm=llm, tool_dispatcher=dispatcher)

    events = [ev async for ev in engine.run(make_turn_input("发个事件"))]

    tool_results = [ev for ev in events if ev.kind is TurnEventKind.TOOL_RESULT]
    assert tool_results[0].data["ok"] is False
    assert tool_results[0].data["error"] == "eidolon.tool_permission_denied"
    await asyncio.sleep(0)
    assert received == []


async def test_memory_backend_down_injects_degraded_notice(turn_engine_factory) -> None:
    class _BoomMemory:
        async def recall_context(self, **_):
            raise RuntimeError("MCP down")

    llm = _CapturingLLM()
    engine = turn_engine_factory(llm=llm, memory_port=_BoomMemory())

    events = [ev async for ev in engine.run(make_turn_input("你还记得我喜欢什么吗？"))]

    assert events[-1].kind is TurnEventKind.DONE
    assert "memory backend" in llm.messages[0].content
    assert "不要假装" in llm.messages[0].content


async def test_private_turn_does_not_fanout_to_memory(turn_engine_factory, event_bus) -> None:
    received = []

    async def _on(ev):
        received.append(ev)

    await event_bus.subscribe("agent.memory.conversation.turn.alice", _on)
    ti = make_turn_input("这段别记")
    ti.metadata["temporary"] = True
    engine = turn_engine_factory()

    events = [ev async for ev in engine.run(ti)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert events[-1].kind is TurnEventKind.DONE
    assert received == []


async def test_forget_intent_calls_memory_port(turn_engine_factory) -> None:
    class _Memory:
        def __init__(self):
            self.calls = []

        async def forget(self, user_id: str, query: str) -> int:
            self.calls.append((user_id, query))
            return 3

    memory = _Memory()
    engine = turn_engine_factory(memory_port=memory)

    events = [ev async for ev in engine.run(make_turn_input("请忘记这件事"))]

    done = [ev for ev in events if ev.kind is TurnEventKind.DONE][0]
    assert done.data["action"] == "memory_forget"
    assert done.data["removed"] == 3
    assert memory.calls == [("alice", "请忘记这件事")]


async def test_memory_replay_remembers_call_name_preference(
    turn_engine_factory,
    event_bus,
) -> None:
    memory = _ReplayMemory()
    received = []

    async def _ingest(ev):
        received.append(ev.payload)
        if ev.payload["metadata"]["memory_write_disposition"] == "semantic_upsert":
            memory.context = "称呼偏好: 用户希望被叫作小满"

    await event_bus.subscribe("agent.memory.conversation.turn.alice", _ingest)
    engine = turn_engine_factory(memory_port=memory)
    first = replace(make_turn_input("以后叫我小满"), turn_id="pref-1")

    events = [ev async for ev in engine.run(first)]
    await _drain_background_tasks()

    assert events[-1].kind is TurnEventKind.DONE
    assert received[0]["metadata"]["memory_write_disposition"] == "semantic_upsert"
    assert received[0]["metadata"]["source_turn_id"] == "pref-1"

    llm = _CapturingLLM()
    followup = replace(make_turn_input("你应该怎么称呼我？"), turn_id="pref-2")
    engine = turn_engine_factory(llm=llm, memory_port=memory)
    events = [ev async for ev in engine.run(followup)]

    assert events[-1].kind is TurnEventKind.DONE
    assert "小满" in llm.messages[0].content


async def test_memory_replay_user_correction_replaces_old_fact(
    turn_engine_factory,
    event_bus,
) -> None:
    memory = _ReplayMemory()

    async def _ingest(ev):
        text = ev.payload["user_text"]
        if ev.payload["metadata"]["memory_write_disposition"] == "semantic_upsert":
            if "阿满" in text:
                memory.context = "称呼偏好: 用户希望被叫作阿满"
            elif "小满" in text:
                memory.context = "称呼偏好: 用户希望被叫作小满"

    await event_bus.subscribe("agent.memory.conversation.turn.alice", _ingest)
    engine = turn_engine_factory(memory_port=memory)
    first = replace(make_turn_input("以后叫我小满"), turn_id="correction-1")
    second = replace(make_turn_input("更正一下，以后叫我阿满"), turn_id="correction-2")

    [ev async for ev in engine.run(first)]
    await _drain_background_tasks()
    [ev async for ev in engine.run(second)]
    await _drain_background_tasks()

    llm = _CapturingLLM()
    followup = replace(make_turn_input("现在该怎么叫我？"), turn_id="correction-3")
    engine = turn_engine_factory(llm=llm, memory_port=memory)
    events = [ev async for ev in engine.run(followup)]

    assert events[-1].kind is TurnEventKind.DONE
    assert "阿满" in llm.messages[0].content
    assert "小满" not in llm.messages[0].content


async def test_memory_replay_promise_is_labeled_and_forced_into_recall(
    turn_engine_factory,
    event_bus,
) -> None:
    memory = _ReplayMemory()
    received = []

    async def _ingest(ev):
        received.append(ev.payload)
        if ev.payload["metadata"]["memory_write_disposition"] == "promise_create":
            memory.context = "强制承诺: 明天提醒用户喝水"

    await event_bus.subscribe("agent.memory.conversation.turn.alice", _ingest)
    engine = turn_engine_factory(memory_port=memory)
    promise = replace(make_turn_input("明天提醒我喝水"), turn_id="promise-1")

    [ev async for ev in engine.run(promise)]
    await _drain_background_tasks()

    assert received[0]["metadata"]["memory_write_disposition"] == "promise_create"
    assert received[0]["metadata"]["memory_write_reason"] == "explicit_promise_or_reminder"

    llm = _CapturingLLM()
    followup = replace(make_turn_input("我有什么提醒吗？"), turn_id="promise-2")
    engine = turn_engine_factory(llm=llm, memory_port=memory)
    events = [ev async for ev in engine.run(followup)]

    assert events[-1].kind is TurnEventKind.DONE
    assert "提醒用户喝水" in llm.messages[0].content


async def test_memory_replay_forget_removes_recalled_context(turn_engine_factory) -> None:
    memory = _ReplayMemory(context="称呼偏好: 用户希望被叫作小满")
    engine = turn_engine_factory(memory_port=memory)
    forget = replace(make_turn_input("请忘记叫我小满"), turn_id="forget-1")

    events = [ev async for ev in engine.run(forget)]

    assert events[-1].data["action"] == "memory_forget"
    assert memory.context == ""

    llm = _CapturingLLM()
    followup = replace(make_turn_input("你记得怎么叫我吗？"), turn_id="forget-2")
    engine = turn_engine_factory(llm=llm, memory_port=memory)
    events = [ev async for ev in engine.run(followup)]

    assert events[-1].kind is TurnEventKind.DONE
    assert "小满" not in llm.messages[0].content


async def test_temporary_turn_does_not_create_memory_replay(
    turn_engine_factory,
    event_bus,
) -> None:
    memory = _ReplayMemory()
    received = []

    async def _ingest(ev):
        received.append(ev.payload)
        memory.context = "should not be written"

    await event_bus.subscribe("agent.memory.conversation.turn.alice", _ingest)
    ti = replace(make_turn_input("以后叫我临时名字"), turn_id="temporary-1")
    ti.metadata["temporary"] = True
    engine = turn_engine_factory(memory_port=memory)

    events = [ev async for ev in engine.run(ti)]
    await _drain_background_tasks()

    assert events[-1].kind is TurnEventKind.DONE
    assert received == []
    assert memory.context == ""


async def test_sensitive_memory_candidate_requires_consent_before_fanout(
    turn_engine_factory,
    event_bus,
) -> None:
    received = []

    async def _ingest(ev):
        received.append(ev.payload)

    await event_bus.subscribe("agent.memory.conversation.turn.alice", _ingest)
    engine = turn_engine_factory()

    events = [ev async for ev in engine.run(make_turn_input("我的身份证是123"))]
    await _drain_background_tasks()

    assert events[-1].kind is TurnEventKind.DONE
    assert received == []


class _ReplayMemory:
    def __init__(self, context: str = "") -> None:
        self.context = context
        self.forget_calls: list[tuple[str, str]] = []

    async def recall_context(self, **_):
        return self.context, [], False

    async def forget(self, user_id: str, query: str) -> int:
        self.forget_calls.append((user_id, query))
        removed = 1 if self.context else 0
        self.context = ""
        return removed


async def _drain_background_tasks() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class _CapturingLLM:
    model_id = "fake:capturing"

    def __init__(self) -> None:
        self.messages: list[ChatMessage] = []

    async def stream(
        self,
        messages: list[ChatMessage],
        **_,
    ) -> AsyncIterator[LLMDelta]:
        self.messages = messages
        yield LLMDelta(text_delta="我在。")
        yield LLMDelta(finish=LLMFinishReason.STOP)

    async def count_tokens(self, messages: list[ChatMessage]) -> int:
        return sum(max(1, len(m.content) // 3) for m in messages)
