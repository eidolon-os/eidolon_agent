"""Cross-module regressions for realtime/context/tool/memory guardrails."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
from sqlalchemy import select

from eidolon_agent.config.settings import SqliteSettings
from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.turn import TurnEventKind
from eidolon_agent.domain.tools import ToolDispatcher, ToolRegistry
from eidolon_agent.domain.tools.builtin import EmitEventTool
from eidolon_agent.infra.llm.providers.fake import FakeLLM
from eidolon_agent.infra.persistence import (
    create_engine,
    create_session_factory,
    ensure_schema,
)
from eidolon_agent.infra.persistence.models import LongTaskRow, TurnRow
from tests.helpers import make_turn_input

pytestmark = pytest.mark.integration


async def test_tool_call_announces_real_tool_before_dispatch_and_feeds_result_to_llm(
    turn_engine_factory,
) -> None:
    llm = _ScriptedCapturingLLM(
        [
            [
                {
                    "kind": "tool_call",
                    "name": "delegate_to_coworker",
                    "arguments": {"instruction": "整理资料"},
                }
            ],
            [{"kind": "text", "text": "我看到了工具结果。"}],
        ]
    )
    engine = turn_engine_factory(llm=llm)

    events = [ev async for ev in engine.run(make_turn_input("帮我整理资料"))]
    await _drain_background_tasks()

    tool_call_idx = next(i for i, ev in enumerate(events) if ev.kind is TurnEventKind.TOOL_CALL)
    prior_deltas = [
        ev.data.get("text", "")
        for ev in events[:tool_call_idx]
        if ev.kind is TurnEventKind.DELTA
    ]
    assert prior_deltas[-1] == "收到，我已交给后台 coworker 处理，会继续跟进。"
    tool_results = [ev for ev in events if ev.kind is TurnEventKind.TOOL_RESULT]
    assert tool_results[0].data["ok"] is True
    assert tool_results[0].data["name"] == "delegate_to_coworker"
    assert len(llm.messages_by_call) == 2
    tool_messages = [
        msg for msg in llm.messages_by_call[1]
        if msg.role is MessageRole.TOOL and msg.tool_name == "delegate_to_coworker"
    ]
    assert tool_messages
    assert "task_id" in tool_messages[0].content
    assert "我看到了工具结果" in "".join(
        ev.data.get("text", "") for ev in events if ev.kind is TurnEventKind.DELTA
    )


async def test_llm_selected_long_task_returns_handoff_without_waiting_for_worker(
    turn_engine_factory,
) -> None:
    llm = _ScriptedCapturingLLM(
        [
            [
                {
                    "kind": "tool_call",
                    "name": "delegate_to_coworker",
                    "arguments": {
                        "title": "预订上海机票",
                        "instruction": "帮我订下周三去上海的机票",
                        "task_type": "booking",
                        "urgency": "normal",
                        "expected_result": "可选航班和预订进度",
                        "context": "用户希望处理订机票事项。",
                    },
                }
            ],
            [{"kind": "text", "text": "我已经交给后台处理，会继续跟进。"}],
        ]
    )
    engine = turn_engine_factory(llm=llm)

    events = [
        ev async for ev in engine.run(make_turn_input("帮我订下周三去上海的机票"))
    ]
    await _drain_background_tasks()

    assert any(
        ev.kind is TurnEventKind.DELTA
        and ev.data.get("text") == "收到，我已交给后台 coworker 处理，会继续跟进。"
        for ev in events
    )
    tool_result = next(ev for ev in events if ev.kind is TurnEventKind.TOOL_RESULT)
    assert tool_result.data["name"] == "delegate_to_coworker"
    assert tool_result.data["ok"] is True
    assert tool_result.data["content"]["accepted"] is True
    handoff = next(ev for ev in events if ev.kind is TurnEventKind.HANDOFF)
    assert handoff.data["task_id"] == tool_result.data["content"]["task_id"]
    assert (
        handoff.data["progress_subject"]
        == tool_result.data["content"]["progress_subject"]
    )
    content = tool_result.data["content"]
    assert content["session_key"].startswith("e.alice.")
    assert content["task_key"].startswith(f"{content['session_key']}.")


async def test_llm_selected_long_task_persists_minimal_receipt_record(
    turn_engine_factory,
    tmp_path,
) -> None:
    sql_engine = create_engine(SqliteSettings(path=tmp_path / "agent.sqlite3"))
    await ensure_schema(sql_engine)
    session_factory = create_session_factory(sql_engine)
    llm = _ScriptedCapturingLLM(
        [
            [
                {
                    "kind": "tool_call",
                    "name": "delegate_to_coworker",
                    "arguments": {
                        "title": "整理项目资料",
                        "instruction": "整理我最近的项目资料并给出行动清单",
                        "task_type": "document_work",
                        "expected_result": "一份结构化行动清单",
                        "context": "用户在测试 coworker 委托任务。",
                    },
                }
            ],
            [{"kind": "text", "text": "已经开始处理，我会继续跟进。"}],
        ]
    )
    engine = turn_engine_factory(llm=llm, session_factory=session_factory)

    events = [
        ev async for ev in engine.run(make_turn_input("整理我最近的项目资料"))
    ]
    await _drain_background_tasks()

    tool_result = next(ev for ev in events if ev.kind is TurnEventKind.TOOL_RESULT)
    content = tool_result.data["content"]
    async with session_factory() as session:
        row = (
            await session.execute(
                select(LongTaskRow).where(LongTaskRow.id == content["task_id"])
            )
        ).scalar_one_or_none()
    await sql_engine.dispose()

    assert row is not None
    assert row.status == "accepted"
    assert row.tenant_id == "t"
    assert row.user_id == "alice"
    assert row.conversation_id == "c1"
    assert row.turn_id == "t1"
    assert row.trace_id == "tr"
    assert row.session_key == content["session_key"]
    assert row.task_key == content["task_key"]
    assert row.task_date == content["task_date"]
    assert row.session_key.startswith("e.alice.")
    assert len(row.session_key.removeprefix("e.alice.")) == 8
    assert row.mementos_session_id is None
    assert row.task_type == "document_work"
    assert row.expected_output == "一份结构化行动清单"
    assert row.context_summary == "用户在测试 coworker 委托任务。"
    assert row.request_payload["session_key"] == row.session_key
    assert row.request_payload["task_key"] == row.task_key
    assert row.request_payload["mementos_session_id"] == row.session_key
    assert row.request_payload["title"] == "整理项目资料"
    assert row.request_payload["instruction"] == "整理我最近的项目资料并给出行动清单"
    assert row.request_payload["expected_result"] == "一份结构化行动清单"
    assert row.request_payload["context"] == "用户在测试 coworker 委托任务。"
    assert row.callback_subject == content["progress_subject"]


async def test_temporary_long_task_does_not_fanout_to_memory(
    turn_engine_factory,
    event_bus,
) -> None:
    memory_fanout = []

    async def _on_memory(ev):
        memory_fanout.append(ev.payload)

    await event_bus.subscribe("agent.memory.conversation.turn.alice", _on_memory)
    llm = _ScriptedCapturingLLM(
        [
            [{"kind": "tool_call", "name": "delegate_to_coworker", "arguments": {"instruction": "整理资料"}}],
            [{"kind": "text", "text": "已开始处理。"}],
        ]
    )
    ti = make_turn_input("临时模式下帮我整理资料")
    ti.metadata["temporary"] = True
    engine = turn_engine_factory(llm=llm)

    events = [ev async for ev in engine.run(ti)]
    await _drain_background_tasks()

    assert any(ev.kind is TurnEventKind.HANDOFF for ev in events)
    assert memory_fanout == []


async def test_compiled_prompt_contains_tool_policy(turn_engine_factory) -> None:
    llm = _CapturingLLM()
    engine = turn_engine_factory(llm=llm)

    events = [ev async for ev in engine.run(make_turn_input("你好"))]

    assert events[-1].kind is TurnEventKind.DONE
    system_prompt = llm.messages[0].content
    assert "Realtime Agent Harness 策略" in system_prompt
    assert "delegate_to_coworker" in system_prompt
    assert "realtime agent" in system_prompt
    assert "cowork" in system_prompt
    assert "不要编造最终结果" in system_prompt


async def test_builtin_tool_schemas_describe_usage_boundaries(turn_engine_factory) -> None:
    engine = turn_engine_factory()
    schemas = {schema.name: schema for schema in engine._tool_schemas()}

    assert "delegate_to_coworker" in schemas
    assert "emit_event" not in schemas
    long_task_spec = schemas["delegate_to_coworker"].to_openai_function()
    description = long_task_spec["function"]["description"]
    assert "background coworker" in description
    assert "Do not use it for ordinary conversation" in description
    assert "instruction" in long_task_spec["function"]["parameters"]["required"]


async def test_turn_trace_contains_harness_snapshot_for_coworker_handoff(
    turn_engine_factory,
    tmp_path,
) -> None:
    sql_engine = create_engine(SqliteSettings(path=tmp_path / "agent.sqlite3"))
    await ensure_schema(sql_engine)
    session_factory = create_session_factory(sql_engine)
    llm = _ScriptedCapturingLLM(
        [
            [
                {
                    "kind": "tool_call",
                    "name": "delegate_to_coworker",
                    "arguments": {"instruction": "整理资料"},
                }
            ],
            [{"kind": "text", "text": "已交给后台。"}],
        ]
    )
    engine = turn_engine_factory(llm=llm, session_factory=session_factory)

    events = [ev async for ev in engine.run(make_turn_input("帮我整理资料"))]
    await _drain_background_tasks()

    assert any(ev.kind is TurnEventKind.HANDOFF for ev in events)
    row = None
    for _ in range(50):
        async with session_factory() as session:
            row = (
                await session.execute(select(TurnRow).where(TurnRow.id == "t1"))
            ).scalar_one_or_none()
        if row is not None:
            break
        await asyncio.sleep(0.01)
    await sql_engine.dispose()

    assert row is not None
    trace = (row.metadata_ or {})["turn_trace"]
    harness = trace["harness"]
    assert harness["kind"] == "realtime_agent_harness"
    assert "harness_policy" in harness["segment_kinds"]
    assert "delegate_to_coworker" in harness["tools"]["visible_names"]
    assert "emit_event" not in harness["tools"]["visible_names"]
    assert harness["handoffs"][0]["tool_name"] == "delegate_to_coworker"
    assert harness["handoffs"][0]["accepted"] is True


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

    done = next(ev for ev in events if ev.kind is TurnEventKind.DONE)
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


class _ScriptedCapturingLLM:
    model_id = "fake:scripted-capturing"

    def __init__(self, scripts: list[list[dict]]) -> None:
        self.scripts = scripts
        self.calls = 0
        self.messages_by_call: list[list[ChatMessage]] = []

    async def stream(
        self,
        messages: list[ChatMessage],
        **_,
    ) -> AsyncIterator[LLMDelta]:
        self.messages_by_call.append(messages)
        idx = min(self.calls, len(self.scripts) - 1)
        self.calls += 1
        script = self.scripts[idx]
        for step in script:
            if step["kind"] == "tool_call":
                from eidolon_agent.core.types.tool import ToolCall

                yield LLMDelta(
                    tool_call=ToolCall(
                        id=step.get("call_id") or f"call-{self.calls}",
                        name=step["name"],
                        arguments=step.get("arguments") or {},
                    )
                )
            elif step["kind"] == "text":
                yield LLMDelta(text_delta=step["text"])
        finish = (
            LLMFinishReason.TOOL_CALLS
            if script and script[-1].get("kind") == "tool_call"
            else LLMFinishReason.STOP
        )
        yield LLMDelta(finish=finish)

    async def count_tokens(self, messages: list[ChatMessage]) -> int:
        return sum(max(1, len(m.content) // 3) for m in messages)
