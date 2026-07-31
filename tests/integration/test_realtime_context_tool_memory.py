"""Cross-module regressions for realtime/context/tool/memory guardrails."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
from eidolon_data import DataSettings, DataStore
from eidolon_data.schema.models import JobRow, TurnRow
from eidolon_memory_contracts import conversation_turn_subject, unwrap_memory_payload
from sqlalchemy import select

from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason
from eidolon_agent.core.types.memory import (
    MemoryForgetCandidate,
    MemoryForgetOutcome,
    MemoryForgetPreview,
    MemoryRecallResult,
)
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.turn import TurnEventKind
from eidolon_agent.domain.agent.companion_config import CompanionRuntimeConfig
from eidolon_agent.domain.history import HistoryManager
from eidolon_agent.domain.tools import ToolDispatcher, ToolRegistry
from eidolon_agent.domain.tools.builtin import EmitEventTool, GetWeatherTool
from eidolon_agent.infra.llm.providers.fake import FakeLLM
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
    data_store = await _data_store(tmp_path)
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
    engine = turn_engine_factory(llm=llm, data_store=data_store)

    events = [
        ev async for ev in engine.run(make_turn_input("整理我最近的项目资料"))
    ]
    await _drain_background_tasks()

    tool_result = next(ev for ev in events if ev.kind is TurnEventKind.TOOL_RESULT)
    content = tool_result.data["content"]
    async with data_store.session_factory() as session:
        row = (
            await session.execute(
                select(JobRow).where(JobRow.job_id == content["task_id"])
            )
        ).scalar_one_or_none()
    await data_store.close()

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
    assert "仅在本轮工具成功后声称完成" in system_prompt


async def test_builtin_tool_schemas_describe_usage_boundaries(turn_engine_factory) -> None:
    engine = turn_engine_factory()
    visible, _dynamic = await engine._tool_schemas(
        make_turn_input(), CompanionRuntimeConfig()
    )
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
    data_store = await _data_store(tmp_path)
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
    engine = turn_engine_factory(llm=llm, data_store=data_store)

    events = [ev async for ev in engine.run(make_turn_input("帮我整理资料"))]
    await _drain_background_tasks()

    assert any(ev.kind is TurnEventKind.HANDOFF for ev in events)
    row = None
    for _ in range(50):
        async with data_store.session_factory() as session:
            row = await session.get(TurnRow, "t1")
        if row is not None:
            break
        await asyncio.sleep(0.01)
    await data_store.close()

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


async def test_forget_intent_calls_memory_port(turn_engine_factory) -> None:
    class _Memory:
        def __init__(self):
            self.calls = []

        async def preview_forget(
            self,
            owner_id: str,
            companion_id: str,
            memory_realm_id: str,
            device_id: str | None,
            query: str,
            *,
            action: str = "archive",
            session_id: str = "default",
        ) -> MemoryForgetPreview:
            self.calls.append(
                (
                    owner_id,
                    companion_id,
                    memory_realm_id,
                    device_id,
                    query,
                    action,
                    session_id,
                )
            )
            return MemoryForgetPreview(
                status="preview",
                target=query,
                action="archive",
                candidates=[MemoryForgetCandidate("drawer-1", query, 1.0)],
                confirmation_token="token-1",
            )

        async def confirm_forget(self, *args, **kwargs) -> MemoryForgetOutcome:
            return MemoryForgetOutcome(
                status="applied",
                action="archive",
                request_id="request-1",
                drawer_ids=["drawer-1"],
            )

    memory = _Memory()
    engine = turn_engine_factory(memory_port=memory)

    events = [ev async for ev in engine.run(make_turn_input("请忘记这件事"))]

    done = next(ev for ev in events if ev.kind is TurnEventKind.DONE)
    assert done.data["action"] == "memory_forget_preview"
    assert done.data["memory_status"] == "applied"
    assert done.data["request_id"] == "request-1"
    assert memory.calls == [
        (
            "alice",
            "companion-test",
            "realm-test",
            "device-test",
            "请忘记这件事",
            "archive",
            "s1",
        )
    ]


async def test_ambiguous_delete_requires_second_turn_and_terminal_status(
    turn_engine_factory,
) -> None:
    class _DeleteMemory:
        def __init__(self) -> None:
            self.confirm_calls = 0

        async def preview_forget(self, *args, **kwargs) -> MemoryForgetPreview:
            return MemoryForgetPreview(
                status="preview",
                target="常州",
                action="delete",
                candidates=[
                    MemoryForgetCandidate("drawer-1", "在常州工作", 1.0),
                    MemoryForgetCandidate("drawer-2", "去常州旅行", 0.8),
                ],
                requires_explicit_confirmation=True,
                confirmation_token="signed-token",
            )

        async def confirm_forget(self, *args, **kwargs) -> MemoryForgetOutcome:
            self.confirm_calls += 1
            return MemoryForgetOutcome(
                status="applied",
                action="delete",
                request_id="delete-request-1",
                drawer_ids=["drawer-1", "drawer-2"],
            )

    memory = _DeleteMemory()
    engine = turn_engine_factory(memory_port=memory)
    preview_turn = replace(
        make_turn_input("请删除关于常州的记忆"),
        turn_id="forget-preview",
    )

    preview_events = [ev async for ev in engine.run(preview_turn)]
    preview_done = preview_events[-1]
    preview_text = "".join(
        ev.data.get("text", "")
        for ev in preview_events
        if ev.kind is TurnEventKind.DELTA
    )
    assert preview_done.data["memory_status"] == "confirmation_required"
    assert preview_done.data["candidate_count"] == 2
    assert "确认删除" in preview_text
    assert "已删除" not in preview_text
    assert memory.confirm_calls == 0

    confirm_turn = replace(make_turn_input("确认删除"), turn_id="forget-confirm")
    confirm_events = [ev async for ev in engine.run(confirm_turn)]
    confirm_done = confirm_events[-1]
    confirm_text = "".join(
        ev.data.get("text", "")
        for ev in confirm_events
        if ev.kind is TurnEventKind.DELTA
    )
    assert confirm_done.data["action"] == "memory_forget_confirm"
    assert confirm_done.data["memory_status"] == "applied"
    assert confirm_done.data["request_id"] == "delete-request-1"
    assert "已删除" in confirm_text
    assert memory.confirm_calls == 1


async def test_accepted_forget_never_claims_terminal_completion(
    turn_engine_factory,
) -> None:
    class _AcceptedMemory:
        async def preview_forget(self, *args, **kwargs) -> MemoryForgetPreview:
            return MemoryForgetPreview(
                status="preview",
                target="小满",
                action="archive",
                candidates=[MemoryForgetCandidate("drawer-1", "称呼小满", 1.0)],
                confirmation_token="signed-token",
            )

        async def confirm_forget(self, *args, **kwargs) -> MemoryForgetOutcome:
            return MemoryForgetOutcome(
                status="accepted",
                action="archive",
                request_id="archive-request-1",
                drawer_ids=["drawer-1"],
            )

    engine = turn_engine_factory(memory_port=_AcceptedMemory())
    events = [ev async for ev in engine.run(make_turn_input("请忘记叫我小满"))]
    text = "".join(
        ev.data.get("text", "") for ev in events if ev.kind is TurnEventKind.DELTA
    )

    assert events[-1].data["memory_status"] == "accepted"
    assert events[-1].data["request_id"] == "archive-request-1"
    assert "仍在处理中" in text
    assert "已经归档" not in text


async def test_memory_replay_remembers_call_name_preference(
    turn_engine_factory,
    event_bus,
) -> None:
    memory = _ReplayMemory()
    received = []

    async def _ingest(ev):
        payload = unwrap_memory_payload(ev.payload)
        received.append(payload)
        if payload["metadata"]["memory_write_disposition"] == "semantic_upsert":
            memory.context = "称呼偏好: 用户希望被叫作小满"

    await event_bus.subscribe(MEMORY_SUBJECT, _ingest)
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
        payload = unwrap_memory_payload(ev.payload)
        text = payload["user_text"]
        if payload["metadata"]["memory_write_disposition"] == "semantic_upsert":
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
        if payload["metadata"]["memory_write_disposition"] == "promise_create":
            memory.context = "强制承诺: 明天提醒用户喝水"

    await event_bus.subscribe(MEMORY_SUBJECT, _ingest)
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

    assert events[-1].data["action"] == "memory_forget_preview"
    assert events[-1].data["memory_status"] == "applied"
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


async def test_sensitive_memory_candidate_requires_consent_before_fanout(
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
    assert received == []


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

    answer = "".join(
        ev.data.get("text", "") for ev in events if ev.kind is TurnEventKind.DELTA
    )
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
        ev.data.get("error")
        for ev in first_events
        if ev.kind is TurnEventKind.TOOL_RESULT
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
    events = [
        ev async for ev in second.run(replace(make_turn_input("那明天呢？"), turn_id="cz-2"))
    ]

    answer = "".join(
        ev.data.get("text", "") for ev in events if ev.kind is TurnEventKind.DELTA
    )
    system = llm.messages_by_call[0][0].content
    assert "明天常州" in answer
    assert "常州今天白天偏热" in system
    assert "那明天呢？" in system
    assert "actionability=must_not_execute" in system


class _ReplayMemory:
    def __init__(self, context: str = "") -> None:
        self.context = context
        self.forget_calls: list[tuple] = []

    async def recall_context(self, **_):
        return MemoryRecallResult(context=self.context)

    async def preview_forget(
        self,
        owner_id: str,
        companion_id: str,
        memory_realm_id: str,
        device_id: str | None,
        query: str,
        *,
        action: str = "archive",
        session_id: str = "default",
    ) -> MemoryForgetPreview:
        self.forget_calls.append(
            (owner_id, companion_id, memory_realm_id, device_id, query, action, session_id)
        )
        if not self.context:
            return MemoryForgetPreview(status="not_found", target=query, action="archive")
        return MemoryForgetPreview(
            status="preview",
            target=query,
            action="delete" if action == "delete" else "archive",
            candidates=[MemoryForgetCandidate("drawer-1", self.context, 1.0)],
            confirmation_token="token-1",
        )

    async def confirm_forget(self, *args, **kwargs) -> MemoryForgetOutcome:
        self.context = ""
        return MemoryForgetOutcome(
            status="applied",
            action="archive",
            request_id="request-1",
            drawer_ids=["drawer-1"],
        )


async def _drain_background_tasks() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def _data_store(tmp_path) -> DataStore:
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    await store.init_schema()
    # Seed the identity used by tests.helpers.make_turn_input — the runtime
    # persistence layer validates owner/companion existence before writing.
    await store.owner_service.create_owner(owner_id="alice", display_name="alice")
    await store.workspace_provisioning.provision_workspace(
        owner_id="alice",
        companion_id="companion-test",
        genome_id="genome-test",
        realm_id="realm-test",
    )
    await store.devices.create_device(
        device_id="device-test",
        owner_id="alice",
        bound_companion_id="companion-test",
        auth_type="token",
        secret_ref="test",
    )
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
