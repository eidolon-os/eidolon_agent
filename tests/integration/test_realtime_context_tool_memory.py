"""Cross-module regressions for realtime/context/tool/memory guardrails."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
from eidolon_memory_contracts import conversation_turn_subject, unwrap_memory_payload
from sqlalchemy import select

from eidolon_agent.core.types.companion_runtime import CompanionRuntimeConfig
from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason
from eidolon_agent.core.types.memory import MemoryRecallResult
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.turn import TurnEventKind
from eidolon_agent.domain.history import HistoryManager
from eidolon_agent.domain.tools import ToolDispatcher, ToolRegistry
from eidolon_agent.domain.tools.builtin import EmitEventTool, GetWeatherTool
from eidolon_agent.infra.llm.providers.fake import FakeLLM
from eidolon_agent.infra.persistence.runtime_store import AgentRuntimeStore, JobRow, TurnRow
from tests.helpers import make_turn_input

pytestmark = pytest.mark.integration

MEMORY_SUBJECT = conversation_turn_subject("realm-test")


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
        ev.data.get("text", "") for ev in events[:tool_call_idx] if ev.kind is TurnEventKind.DELTA
    ]
    assert prior_deltas[-1] == "收到，我已交给后台 coworker 处理，会继续跟进。"
    tool_results = [ev for ev in events if ev.kind is TurnEventKind.TOOL_RESULT]
    assert tool_results[0].data["ok"] is True
    assert tool_results[0].data["name"] == "delegate_to_coworker"
    assert len(llm.messages_by_call) == 2
    tool_messages = [
        msg
        for msg in llm.messages_by_call[1]
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

    events = [ev async for ev in engine.run(make_turn_input("帮我订下周三去上海的机票"))]
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
    assert handoff.data["progress_subject"] == tool_result.data["content"]["progress_subject"]
    content = tool_result.data["content"]
    assert content["session_key"].startswith("e.alice.")
    assert content["task_key"].startswith(f"{content['session_key']}.")


async def test_llm_selected_long_task_persists_minimal_receipt_record(
    turn_engine_factory,
    tmp_path,
) -> None:
    runtime_store = await _runtime_store(tmp_path)
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
    engine = turn_engine_factory(llm=llm, runtime_store=runtime_store)

    events = [ev async for ev in engine.run(make_turn_input("整理我最近的项目资料"))]
    await _drain_background_tasks()

    tool_result = next(ev for ev in events if ev.kind is TurnEventKind.TOOL_RESULT)
    content = tool_result.data["content"]
    async with runtime_store.session_factory() as session:
        row = (
            await session.execute(select(JobRow).where(JobRow.job_id == content["task_id"]))
        ).scalar_one_or_none()
    await runtime_store.close()

    assert row is not None
    assert row.status == "accepted"
    payload = row.input_json["eidolon_agent_long_task"]
    assert payload["companion_id"] == "companion-test"
    assert row.owner_id == "alice"
    assert row.conversation_id == "c1"
    assert row.turn_id == "t1"
    assert payload["trace_id"] == "tr"
    assert payload["session_key"] == content["session_key"]
    assert payload["task_key"] == content["task_key"]
    assert payload["task_date"] == content["task_date"]
    assert payload["session_key"].startswith("e.alice.")
    assert len(payload["session_key"].removeprefix("e.alice.")) == 8
    assert row.provider_ref_json["mementos_session_id"] is None
    assert row.kind == "document_work"
    assert payload["expected_output"] == "一份结构化行动清单"
    assert payload["context_summary"] == "用户在测试 coworker 委托任务。"
    assert payload["request_payload"]["session_key"] == payload["session_key"]
    assert payload["request_payload"]["task_key"] == payload["task_key"]
    assert payload["request_payload"]["mementos_session_id"] == payload["session_key"]
    assert payload["request_payload"]["title"] == "整理项目资料"
    assert payload["request_payload"]["instruction"] == "整理我最近的项目资料并给出行动清单"
    assert payload["request_payload"]["expected_result"] == "一份结构化行动清单"
    assert payload["request_payload"]["context"] == "用户在测试 coworker 委托任务。"
    assert row.result_json["callback_subject"] == content["progress_subject"]


async def test_temporary_long_task_does_not_fanout_to_memory(
    turn_engine_factory,
    event_bus,
) -> None:
    memory_fanout = []

    async def _on_memory(ev):
        memory_fanout.append(ev.payload)

    await event_bus.subscribe(MEMORY_SUBJECT, _on_memory)
    llm = _ScriptedCapturingLLM(
        [
            [
                {
                    "kind": "tool_call",
                    "name": "delegate_to_coworker",
                    "arguments": {"instruction": "整理资料"},
                }
            ],
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
    assert "仅在本轮工具成功后声称完成" in system_prompt


async def test_builtin_tool_schemas_describe_usage_boundaries(turn_engine_factory) -> None:
    engine = turn_engine_factory()
    visible, _dynamic = await engine._tool_schemas(make_turn_input(), CompanionRuntimeConfig())
    schemas = {schema.name: schema for schema in visible}

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
    runtime_store = await _runtime_store(tmp_path)
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
    engine = turn_engine_factory(llm=llm, runtime_store=runtime_store)

    events = [ev async for ev in engine.run(make_turn_input("帮我整理资料"))]
    await _drain_background_tasks()

    assert any(ev.kind is TurnEventKind.HANDOFF for ev in events)
    row = None
    for _ in range(50):
        async with runtime_store.session_factory() as session:
            row = await session.get(TurnRow, "t1")
        if row is not None:
            break
        await asyncio.sleep(0.01)
    await runtime_store.close()

    assert row is not None
    trace = row.trace_json
    harness = trace["harness"]
    assert harness["kind"] == "realtime_agent_harness"
    assert "harness_policy" in harness["segment_kinds"]
    assert "delegate_to_coworker" in harness["tools"]["visible_names"]
    assert "emit_event" not in harness["tools"]["visible_names"]
    assert harness["handoffs"][0]["tool_name"] == "delegate_to_coworker"
    assert harness["handoffs"][0]["accepted"] is True


async def test_tool_permission_error_is_returned_to_llm_without_side_effect(
    turn_engine_factory,
    event_bus,
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
            [
                {
                    "kind": "tool_call",
                    "name": "emit_event",
                    "arguments": {"subject": "agent.test.blocked"},
                }
            ],
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
    assert "长期记忆召回暂不可用" in llm.messages[0].content
    assert "不要假装" in llm.messages[0].content


async def test_private_turn_does_not_fanout_to_memory(turn_engine_factory, event_bus) -> None:
    received = []

    async def _on(ev):
        received.append(ev)

    await event_bus.subscribe(MEMORY_SUBJECT, _on)
    ti = make_turn_input("这段别记")
    ti.metadata["temporary"] = True
    engine = turn_engine_factory()

    events = [ev async for ev in engine.run(ti)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert events[-1].kind is TurnEventKind.DONE
    assert received == []


async def test_memory_replay_remembers_call_name_preference(
    turn_engine_factory,
    event_bus,
) -> None:
    memory = _ReplayMemory()
    received = []

    async def _ingest(ev):
        payload = unwrap_memory_payload(ev.payload)
        received.append(payload)
        memory.context = "称呼偏好: 用户希望被叫作小满"

    await event_bus.subscribe(MEMORY_SUBJECT, _ingest)
    engine = turn_engine_factory(memory_port=memory)
    first = replace(make_turn_input("以后叫我小满"), turn_id="pref-1")

    events = [ev async for ev in engine.run(first)]
    await _drain_background_tasks()

    assert events[-1].kind is TurnEventKind.DONE
    assert received[0]["metadata"]["memory_ingest_policy"] == "semantic_steward"
    assert received[0]["metadata"]["source_turn_id"] == "pref-1"
    assert received[0]["assistant_text"] == ""

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
        payload = unwrap_memory_payload(ev.payload)
        text = payload["user_text"]
        if "阿满" in text:
            memory.context = "称呼偏好: 用户希望被叫作阿满"
        elif "小满" in text:
            memory.context = "称呼偏好: 用户希望被叫作小满"

    await event_bus.subscribe(MEMORY_SUBJECT, _ingest)
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
        payload = unwrap_memory_payload(ev.payload)
        received.append(payload)
        if "提醒" in payload["user_text"]:
            memory.context = "强制承诺: 明天提醒用户喝水"

    await event_bus.subscribe(MEMORY_SUBJECT, _ingest)
    engine = turn_engine_factory(memory_port=memory)
    promise = replace(make_turn_input("明天提醒我喝水"), turn_id="promise-1")

    [ev async for ev in engine.run(promise)]
    await _drain_background_tasks()

    assert received[0]["metadata"]["memory_ingest_policy"] == "semantic_steward"

    llm = _CapturingLLM()
    followup = replace(make_turn_input("我有什么提醒吗？"), turn_id="promise-2")
    engine = turn_engine_factory(llm=llm, memory_port=memory)
    events = [ev async for ev in engine.run(followup)]

    assert events[-1].kind is TurnEventKind.DONE
    assert "提醒用户喝水" in llm.messages[0].content


async def test_temporary_turn_does_not_create_memory_replay(
    turn_engine_factory,
    event_bus,
) -> None:
    memory = _ReplayMemory()
    received = []

    async def _ingest(ev):
        received.append(unwrap_memory_payload(ev.payload))
        memory.context = "should not be written"

    await event_bus.subscribe(MEMORY_SUBJECT, _ingest)
    ti = replace(make_turn_input("以后叫我临时名字"), turn_id="temporary-1")
    ti.metadata["temporary"] = True
    engine = turn_engine_factory(memory_port=memory)

    events = [ev async for ev in engine.run(ti)]
    await _drain_background_tasks()

    assert events[-1].kind is TurnEventKind.DONE
    assert received == []
    assert memory.context == ""


async def test_agent_does_not_apply_keyword_sensitive_gating_before_steward(
    turn_engine_factory,
    event_bus,
) -> None:
    received = []

    async def _ingest(ev):
        received.append(ev.payload)

    await event_bus.subscribe(MEMORY_SUBJECT, _ingest)
    engine = turn_engine_factory()

    events = [ev async for ev in engine.run(make_turn_input("我的身份证是123"))]
    await _drain_background_tasks()

    assert events[-1].kind is TurnEventKind.DONE
    assert len(received) == 1


async def test_multiturn_weather_then_counting_keeps_weather_as_background(
    turn_engine_factory,
) -> None:
    history = HistoryManager()
    first = turn_engine_factory(
        llm=FakeLLM(script=[{"kind": "text", "text": "常州今天有点热。"}], per_token_delay_s=0),
        history=history,
    )
    [ev async for ev in first.run(replace(make_turn_input("查一下常州天气"), turn_id="weather-1"))]

    llm = _AnsweringCaptureLLM("好，一二三。")
    second = turn_engine_factory(llm=llm, history=history)
    events = [
        ev
        async for ev in second.run(
            replace(make_turn_input("你帮我数三个数，123"), turn_id="count-2")
        )
    ]

    answer = "".join(ev.data.get("text", "") for ev in events if ev.kind is TurnEventKind.DELTA)
    system = llm.messages_by_call[0][0].content
    assert "一二三" in answer
    assert "[CURRENT REQUEST]" in system
    assert "你帮我数三个数，123" in system
    assert "[BACKGROUND CONTEXT]" in system
    assert "常州今天有点热" in system
    assert "actionability=must_not_execute" in system


async def test_weather_failure_then_correction_answers_current_request(
    turn_engine_factory,
) -> None:
    async def _always_fail(_location: str, _lang: str):
        raise RuntimeError("weather backend unavailable")

    registry = ToolRegistry()
    registry.register(GetWeatherTool(fetcher=_always_fail))
    dispatcher = ToolDispatcher(registry)
    history = HistoryManager()
    first_llm = FakeLLM(
        script=[
            [{"kind": "tool_call", "name": "get_weather", "arguments": {"location": "常州"}}],
            [{"kind": "tool_call", "name": "get_weather", "arguments": {"location": "常州"}}],
            [{"kind": "text", "text": "天气接口暂时失败。"}],
        ],
        per_token_delay_s=0,
    )
    first = turn_engine_factory(
        llm=first_llm,
        tool_dispatcher=dispatcher,
        history=history,
    )
    first_events = [
        ev
        async for ev in first.run(
            replace(make_turn_input("帮我查今天常州的天气"), turn_id="weather-fail-1")
        )
    ]
    first_errors = [
        ev.data.get("error") for ev in first_events if ev.kind is TurnEventKind.TOOL_RESULT
    ]
    assert first_errors == ["weather_lookup_failed", "tool_repeat_suppressed"]

    correction_llm = _AnsweringCaptureLLM("抱歉刚才想岔了。好，一二三。")
    second = turn_engine_factory(llm=correction_llm, history=history)
    second_events = [
        ev
        async for ev in second.run(
            replace(
                make_turn_input("没让你查天气，我让你数三个数一二三"),
                turn_id="correction-2",
            )
        )
    ]

    answer = "".join(
        ev.data.get("text", "") for ev in second_events if ev.kind is TurnEventKind.DELTA
    )
    system = correction_llm.messages_by_call[0][0].content
    assert "一二三" in answer
    assert "[CURRENT REQUEST]" in system
    assert "没让你查天气，我让你数三个数一二三" in system
    assert "[BACKGROUND CONTEXT]" in system
    assert "天气接口暂时失败" in system
    assert "must_not_execute" in system


async def test_multiturn_reference_uses_background_without_reexecution(
    turn_engine_factory,
) -> None:
    history = HistoryManager()
    first = turn_engine_factory(
        llm=FakeLLM(
            script=[{"kind": "text", "text": "常州今天白天偏热，傍晚可能有阵雨。"}],
            per_token_delay_s=0,
        ),
        history=history,
    )
    [ev async for ev in first.run(replace(make_turn_input("常州天气怎么样？"), turn_id="cz-1"))]

    llm = _AnsweringCaptureLLM("明天常州也要留意降雨。")
    second = turn_engine_factory(llm=llm, history=history)
    events = [ev async for ev in second.run(replace(make_turn_input("那明天呢？"), turn_id="cz-2"))]

    answer = "".join(ev.data.get("text", "") for ev in events if ev.kind is TurnEventKind.DELTA)
    system = llm.messages_by_call[0][0].content
    assert "明天常州" in answer
    assert "常州今天白天偏热" in system
    assert "那明天呢？" in system
    assert "actionability=must_not_execute" in system


class _ReplayMemory:
    def __init__(self, context: str = "") -> None:
        self.context = context

    async def recall_context(self, **_):
        return MemoryRecallResult(context=self.context)


async def _drain_background_tasks() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def _runtime_store(tmp_path) -> AgentRuntimeStore:
    store = AgentRuntimeStore.open(tmp_path / "eidolon-agent.sqlite3")
    await store.init_schema()
    return store


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


class _AnsweringCaptureLLM:
    model_id = "fake:answering-capture"

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.messages_by_call: list[list[ChatMessage]] = []

    async def stream(
        self,
        messages: list[ChatMessage],
        **_,
    ) -> AsyncIterator[LLMDelta]:
        self.messages_by_call.append(messages)
        yield LLMDelta(text_delta=self.answer)
        yield LLMDelta(finish=LLMFinishReason.STOP)

    async def count_tokens(self, messages: list[ChatMessage]) -> int:
        return sum(max(1, len(m.content) // 3) for m in messages)
