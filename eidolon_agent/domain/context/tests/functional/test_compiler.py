"""ContextCompiler — direct prompt assembly.

No more pluggable providers; tests verify the fixed structured shape:
  [system: instructions + reference/background + current request] + [user_input]
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from eidolon_agent.core.types.memory import (
    ActiveCommitment,
    ActiveCommitmentReadResult,
    MemoryRecallResult,
)
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.domain.context.compiler import ContextCompiler
from eidolon_agent.domain.history.manager import HistoryManager
from eidolon_agent.domain.personas.realizer import PersonaRealizer
from tests.helpers import make_turn_input

pytestmark = pytest.mark.functional


class _StubPersonas:
    """Minimal PersonasService stand-in returning a canned system prompt."""

    def __init__(self, prompt: str = "[PERSONA]\nyou are an assistant") -> None:
        self._prompt = prompt
        self._realizer = PersonaRealizer()
        self.calls: list[dict] = []

    async def realize_context(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(system_prompt=self._prompt, debug_trace=())

    def realize_commitment_context(self, commitments):
        return self._realizer.realize_commitment_context(commitments)


class _StubMemory:
    """Memory port stub used by recall integration tests."""

    def __init__(
        self,
        formatted: str = "",
        *,
        degraded: bool = False,
        degraded_reason: str | None = None,
        kg_triples: list[dict] | None = None,
        diagnostics: dict[str, float] | None = None,
    ) -> None:
        self._formatted = formatted
        self._degraded = degraded
        self._degraded_reason = degraded_reason
        self._kg_triples = list(kg_triples or [])
        self._diagnostics = dict(diagnostics or {})
        self.calls: list[dict] = []

    async def recall_context(
        self,
        *,
        owner_id,
        companion_id,
        memory_realm_id,
        device_id,
        session_id,
        query,
        plan,
        timeout_s,
    ):
        self.calls.append(
            {
                "owner_id": owner_id,
                "companion_id": companion_id,
                "memory_realm_id": memory_realm_id,
                "device_id": device_id,
                "session_id": session_id,
                "query": query,
                "plan": plan,
                "timeout_s": timeout_s,
            }
        )
        return MemoryRecallResult(
            context=self._formatted,
            hits=[SimpleNamespace(id="mem-1")],
            kg_triples=self._kg_triples,
            degraded=self._degraded,
            degraded_reason=self._degraded_reason,
            diagnostics=self._diagnostics,
        )


class _StubSummary:
    def __init__(self, summary: str | None) -> None:
        self._summary = summary
        self.calls: list[str] = []

    async def latest_summary(self, *, conversation_id: str) -> str | None:
        self.calls.append(conversation_id)
        return self._summary


def _locator(_t, _u, _c):
    return ("inst-test", "tpl-x")


def _now() -> datetime:
    return datetime.now(UTC)


async def test_assembles_structured_system_background_and_current_user() -> None:
    history = HistoryManager()
    await history.append(
        conversation_id="c1",
        message=ChatMessage(
            id=uuid.uuid4().hex,
            role=MessageRole.USER,
            content="earlier-q",
            created_at=_now(),
        ),
    )
    await history.append(
        conversation_id="c1",
        message=ChatMessage(
            id=uuid.uuid4().hex,
            role=MessageRole.ASSISTANT,
            content="earlier-a",
            created_at=_now(),
        ),
    )

    compiler = ContextCompiler(
        personas_service=_StubPersonas("[PERSONA]\nhi"),
        instance_locator=_locator,
        history_manager=history,
        memory_port=None,
    )
    msgs = await compiler.compile(make_turn_input("当前问题"))

    assert [m.role for m in msgs] == [MessageRole.SYSTEM, MessageRole.USER]
    system = msgs[0].content
    assert "[SYSTEM INSTRUCTIONS]" in system
    assert "[PERSONA]" in system
    assert "[BACKGROUND CONTEXT]" in system
    assert "authority=background" in system
    assert "actionability=must_not_execute" in system
    assert "earlier-q" in system
    assert "earlier-a" in system
    assert "[CURRENT REQUEST]" in system
    assert "authority=current_request" in system
    assert "actionability=may_execute" in system
    assert "当前问题" in system
    assert msgs[-1].content == "当前问题"
    assert msgs[-1].role is MessageRole.USER


async def _seed_history(history: HistoryManager, n_turns: int) -> None:
    for i in range(n_turns):
        for role, tag in ((MessageRole.USER, "u"), (MessageRole.ASSISTANT, "a")):
            await history.append(
                conversation_id="c1",
                message=ChatMessage(
                    id=uuid.uuid4().hex,
                    role=role,
                    content=f"turn-{i}-{tag}",
                    created_at=_now(),
                ),
            )


async def test_history_window_stays_tight_when_memory_healthy() -> None:
    history = HistoryManager()
    await _seed_history(history, 8)
    compiler = ContextCompiler(
        personas_service=_StubPersonas("[PERSONA]\nhi"),
        instance_locator=_locator,
        history_manager=history,
        memory_port=_StubMemory("some memory", degraded=False),
        history_window=4,
        degraded_history_window=12,
    )
    ti = make_turn_input("现在的问题")
    system = (await compiler.compile(ti))[0].content
    # Healthy memory → only the tight window (last 4 messages = turns 6,7).
    assert "turn-7-a" in system
    assert "turn-6-u" in system
    assert "turn-5-a" not in system
    assert ti.metadata["history_window_applied"]["effective"] == 4
    assert ti.metadata["history_window_applied"]["expanded_for_degraded_memory"] is False


async def test_history_window_expands_when_memory_degraded() -> None:
    history = HistoryManager()
    # 6 turns = 12 messages, exactly the degraded window.
    await _seed_history(history, 6)
    compiler = ContextCompiler(
        personas_service=_StubPersonas("[PERSONA]\nhi"),
        instance_locator=_locator,
        history_manager=history,
        memory_port=_StubMemory("", degraded=True, degraded_reason="memory_unavailable"),
        history_window=4,
        degraded_history_window=12,
        context_budget_mode="disabled",
    )
    ti = make_turn_input("现在的问题")
    system = (await compiler.compile(ti))[0].content
    # Degraded memory → widen the window so a long chat doesn't go amnesiac.
    assert "turn-0-u" in system
    assert "turn-5-a" in system
    assert ti.metadata["history_window_applied"]["effective"] == 12
    assert ti.metadata["history_window_applied"]["expanded_for_degraded_memory"] is True


async def test_ledger_records_segment_volatility() -> None:
    history = HistoryManager()
    await _seed_history(history, 1)
    compiler = ContextCompiler(
        personas_service=_StubPersonas("[PERSONA]\nhi"),
        instance_locator=_locator,
        history_manager=history,
        memory_port=_StubMemory("mem"),
    )
    ti = make_turn_input("问题")
    await compiler.compile(ti)
    ledger = ti.metadata["context_ledger"]
    vol = {s["kind"]: s["volatility"] for s in ledger["segments"]}
    assert vol["persona"] == "stable"
    assert vol["harness_policy"] == "stable"
    assert vol.get("memory") == "volatile"
    assert vol["current_user"] == "current"


async def test_topic_switch_fences_off_prior_topic_history() -> None:
    history = HistoryManager()
    for i in range(4):
        await history.append(
            conversation_id="c1",
            message=ChatMessage(
                id=uuid.uuid4().hex,
                role=MessageRole.USER,
                content=f"old-topic-user-{i}",
                created_at=_now(),
            ),
        )
        await history.append(
            conversation_id="c1",
            message=ChatMessage(
                id=uuid.uuid4().hex,
                role=MessageRole.ASSISTANT,
                content=f"old-topic-answer-{i}",
                created_at=_now(),
            ),
        )

    compiler = ContextCompiler(
        personas_service=_StubPersonas("[PERSONA]\nhi"),
        instance_locator=_locator,
        history_manager=history,
        memory_port=None,
        summary_provider=_StubSummary("这是旧话题的滚动摘要"),
    )

    ti = make_turn_input("我们换个话题吧")
    ti.metadata["topic_switch"] = True
    system = (await compiler.compile(ti))[0].content

    # The rolling summary describes the old topic and must be fenced off.
    assert "旧话题的滚动摘要" not in system
    # Deep history from the old topic must not bleed into the new one; only
    # the immediately-preceding turn survives for referential continuity.
    assert "old-topic-answer-0" not in system
    assert "old-topic-answer-1" not in system
    assert "old-topic-answer-3" in system


async def test_persona_locator_args_match_turn_input() -> None:
    personas = _StubPersonas()
    compiler = ContextCompiler(
        personas_service=personas,
        instance_locator=lambda t, u, c: (f"{t}/{u}", "tpl"),
        history_manager=HistoryManager(),
    )
    ti = make_turn_input("hi")
    # Override turn-context fields via direct attribute (TurnInput is frozen, so this
    # just sanity-checks the default args go through).
    await compiler.compile(ti)
    call = personas.calls[0]
    assert call["owner_id"] == ti.context.owner_id
    assert call["companion_id"] == "alice/companion-test"
    assert call["genome_id"] == "tpl"
    assert call["user_text"] == "hi"


async def test_memory_recall_appended_when_port_present() -> None:
    memory = _StubMemory(formatted="prior_episode_summary")
    compiler = ContextCompiler(
        personas_service=_StubPersonas(),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        memory_port=memory,
    )
    msgs = await compiler.compile(make_turn_input("帮我回忆一下"))
    assert "[RETRIEVED MEMORY]" in msgs[0].content
    assert "authority=retrieved_memory" in msgs[0].content
    assert "actionability=may_use_as_reference" in msgs[0].content
    assert "prior_episode_summary" in msgs[0].content
    assert memory.calls and memory.calls[0]["query"] == "帮我回忆一下"


async def test_memory_recall_uses_turn_identity_as_companion_partition() -> None:
    memory = _StubMemory(formatted="prior_episode_summary")
    compiler = ContextCompiler(
        personas_service=_StubPersonas(),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        memory_port=memory,
    )

    await compiler.compile(make_turn_input("帮我回忆一下"))

    call = memory.calls[0]
    assert call["owner_id"] == "alice"
    assert call["companion_id"] == "companion-test"
    assert call["memory_realm_id"] == "realm-test"
    assert call["device_id"] == "device-test"
    assert call["session_id"] == "s1"


async def test_active_commitments_route_as_bounded_non_actionable_context() -> None:
    class _CommitmentMemory(_StubMemory):
        def __init__(self) -> None:
            super().__init__(formatted="")
            self.commitment_calls: list[dict] = []

        async def read_active_commitments(self, **kwargs):
            self.commitment_calls.append(kwargs)
            return ActiveCommitmentReadResult(
                commitments=[
                    ActiveCommitment(
                        commitment_id="commitment-1",
                        promisor="小忆",
                        predicate="promised",
                        action="周六陪 owner 去恐龙园",
                        status="confirmed",
                        participants=("朋友甲", "朋友乙"),
                        due_at="2026-07-18T09:00:00+08:00",
                    )
                ],
                total=7,
                truncated=True,
            )

    memory = _CommitmentMemory()
    ti = make_turn_input("今天聊点别的")
    compiler = ContextCompiler(
        personas_service=_StubPersonas(),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        memory_port=memory,
        active_commitment_limit=3,
        active_commitment_timeout_s=0.15,
    )

    system = (await compiler.compile(ti))[0].content

    assert "[ACTIVE COMMITMENTS]" in system
    assert "actionability=must_not_execute" in system
    assert "周六陪 owner 去恐龙园" in system
    assert "Do not execute, fulfil, cancel, or modify" in system
    call = memory.commitment_calls[0]
    assert call["owner_id"] == "alice"
    assert call["companion_id"] == "companion-test"
    assert call["memory_realm_id"] == "realm-test"
    assert call["device_id"] == "device-test"
    assert call["session_id"] == "s1"
    assert call["limit"] == 3
    assert call["timeout_s"] == pytest.approx(0.15)
    assert ti.metadata["commitment_context_trace"] == {
        "attempted": True,
        "skipped_reason": None,
        "degraded": False,
        "degraded_reason": None,
        "elapsed_ms": ti.metadata["commitment_context_trace"]["elapsed_ms"],
        "timeout_ms": 150,
        "limit": 3,
        "commitment_ids": ["commitment-1"],
        "count": 1,
        "total": 7,
        "truncated": True,
        "context_injected": True,
    }
    assert any(
        segment["kind"] == "commitment" for segment in ti.metadata["context_ledger"]["segments"]
    )


async def test_empty_or_degraded_commitments_are_not_injected() -> None:
    class _UnavailableCommitmentMemory(_StubMemory):
        async def read_active_commitments(self, **_kwargs):
            return ActiveCommitmentReadResult(
                degraded=True,
                degraded_reason="timeout",
            )

    ti = make_turn_input("继续聊")
    compiler = ContextCompiler(
        personas_service=_StubPersonas(),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        memory_port=_UnavailableCommitmentMemory(),
    )

    system = (await compiler.compile(ti))[0].content

    assert "[ACTIVE COMMITMENTS]" not in system
    assert ti.metadata["commitment_context_trace"]["degraded"] is True
    assert ti.metadata["commitment_context_trace"]["context_injected"] is False


async def test_ordinary_memory_recall_uses_soft_timeout() -> None:
    memory = _StubMemory(formatted="prior_episode_summary")
    ti = make_turn_input("我在常州工作")
    compiler = ContextCompiler(
        personas_service=_StubPersonas(),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        memory_port=memory,
        memory_timeout_s=0.2,
    )

    await compiler.compile(ti)

    assert memory.calls[0]["timeout_s"] == pytest.approx(0.2, abs=0.001)
    assert ti.metadata["memory_trace"]["timeout_ms"] == 200


@pytest.mark.parametrize(
    "query",
    [
        "我最喜欢的水果是什么？",
        "我叫什么？",
        "我的生日是哪天？",
        "我在哪里读书？",
        "我的宠物叫什么？",
        "我应该怎么做？",
        "我今天吃什么？",
        "我能不能换工作？",
        "你最喜欢什么水果？",
    ],
)
async def test_all_natural_queries_share_one_recall_contract(
    query: str,
) -> None:
    memory = _StubMemory()
    compiler = ContextCompiler(
        personas_service=_StubPersonas(),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        memory_port=memory,
        memory_timeout_s=0.5,
    )

    ti = make_turn_input(query)
    await compiler.compile(ti)

    assert memory.calls[0]["timeout_s"] == pytest.approx(0.5, abs=0.001)
    assert memory.calls[0]["plan"].kg_subjects == ("self",)
    assert ti.metadata["memory_trace"]["timeout_ms"] == 500
    assert ti.metadata["memory_recall_query"]["source"] in {
        "current_only",
        "current_plus_recent_history",
    }


async def test_natural_memory_lookup_uses_single_combined_result() -> None:
    class _SlotMemory:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def recall_context(self, **kwargs):
            self.calls.append(kwargs)
            return MemoryRecallResult(
                context=(
                    "个人画像与健康:\n- 用户的名字是曼森。\n"
                    "- 用户在北京化工大学就读，学校位于北京。"
                ),
                hits=[
                    SimpleNamespace(id="name-hit"),
                    SimpleNamespace(id="school-hit"),
                ],
                kg_triples=[{"id": "kg-name"}, {"id": "kg-school"}],
            )

    memory = _SlotMemory()
    ti = make_turn_input("我叫什么？我在哪里读书？")
    compiler = ContextCompiler(
        personas_service=_StubPersonas(),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        memory_port=memory,
    )

    msgs = await compiler.compile(ti)

    system = msgs[0].content
    assert "用户的名字是曼森" in system
    assert "用户在北京化工大学就读" in system
    assert ti.metadata["memory_trace"]["hit_ids"] == ["name-hit", "school-hit"]
    assert ti.metadata["memory_trace"]["kg_triple_ids"] == ["kg-name", "kg-school"]


async def test_memory_recall_query_includes_recent_history_for_anaphora() -> None:
    history = HistoryManager()
    await history.append(
        conversation_id="c1",
        message=ChatMessage(
            id=uuid.uuid4().hex,
            role=MessageRole.USER,
            content="跟你了解一下铁锤。",
            created_at=_now(),
        ),
    )
    await history.append(
        conversation_id="c1",
        message=ChatMessage(
            id=uuid.uuid4().hex,
            role=MessageRole.ASSISTANT,
            content="铁锤是那只很特别的狗狗。",
            created_at=_now(),
        ),
    )
    memory = _StubMemory(formatted="铁锤是一只边境牧羊犬")
    ti = make_turn_input("我想了解它是什么品种。")
    compiler = ContextCompiler(
        personas_service=_StubPersonas(),
        instance_locator=_locator,
        history_manager=history,
        memory_port=memory,
    )

    await compiler.compile(ti)

    query = memory.calls[0]["query"]
    assert "铁锤" in query
    assert "我想了解它是什么品种" in query
    assert ti.metadata["memory_recall_query"]["source"] == "current_plus_recent_history"


async def test_memory_failure_does_not_break_turn() -> None:
    class _Boom:
        async def recall_context(self, **_):
            raise RuntimeError("upstream broken")

    compiler = ContextCompiler(
        personas_service=_StubPersonas(),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        memory_port=_Boom(),
    )
    # Should NOT raise; system message is still produced.
    msgs = await compiler.compile(make_turn_input("hi"))
    assert msgs[0].role is MessageRole.SYSTEM


async def test_memory_failure_injects_degraded_notice_into_prompt() -> None:
    """Regression: silent fallback used to give the LLM no signal that
    memory was down, so it would happily confabulate "as you mentioned
    earlier..." answers. The new behavior is to inject an in-prompt
    notice telling the LLM not to fake having access to memory.

    See compiler.ContextCompiler._MEMORY_DEGRADED_NOTICE.
    """

    class _Boom:
        async def recall_context(self, **_):
            raise RuntimeError("MemoryUnavailableError: no reachable MCP endpoint")

    compiler = ContextCompiler(
        personas_service=_StubPersonas("[PERSONA]\nyou are helpful"),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        memory_port=_Boom(),
    )
    msgs = await compiler.compile(make_turn_input("你还记得铁锤这个词吗？"))
    system = msgs[0].content
    # The persona prompt still ships, AND the degraded notice rides along.
    assert "[PERSONA]" in system
    assert "长期记忆召回暂不可用" in system  # part of the notice
    # The notice explicitly tells the LLM not to fake memory access.
    assert "不要假装" in system
    assert "不要据此判断 memory 写入工具是否可用" in system


async def test_memory_success_does_not_inject_degraded_notice() -> None:
    """Sanity check: when memory works, the degraded notice MUST NOT appear,
    otherwise every healthy turn would tell the LLM "memory is down".
    """
    memory = _StubMemory(formatted="user mentioned 铁锤 last week")
    compiler = ContextCompiler(
        personas_service=_StubPersonas(),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        memory_port=memory,
    )
    msgs = await compiler.compile(make_turn_input("聊聊铁锤"))
    system = msgs[0].content
    assert "[RETRIEVED MEMORY]" in system
    assert "暂不可达" not in system  # the notice keyword must not appear
    assert "长期记忆召回暂不可用" not in system  # the notice keyword must not appear


async def test_memory_soft_degraded_injects_degraded_notice() -> None:
    memory = _StubMemory(formatted="", degraded=True)
    ti = make_turn_input("你还记得什么？")
    compiler = ContextCompiler(
        personas_service=_StubPersonas(),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        memory_port=memory,
    )

    msgs = await compiler.compile(ti)

    assert "长期记忆召回暂不可用" in msgs[0].content
    assert "memory" in ti.metadata["context_ledger"]["degraded_sources"]


async def test_context_ledger_metadata_is_written_without_prompt_text() -> None:
    ti = make_turn_input("当前问题")
    compiler = ContextCompiler(
        personas_service=_StubPersonas("[PERSONA]\nsecret-system-prompt"),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        memory_port=_StubMemory(formatted="recalled-private-detail"),
    )

    await compiler.compile(ti)

    ledger = ti.metadata["context_ledger"]
    assert ledger["total_token_estimate"] > 0
    assert {s["kind"] for s in ledger["segments"]} >= {
        "persona",
        "harness_policy",
        "memory",
        "current_user",
    }
    assert "secret-system-prompt" not in str(ledger)
    assert "recalled-private-detail" not in str(ledger)
    snapshot = ti.metadata["harness_snapshot"]
    assert snapshot["kind"] == "realtime_agent_harness"
    assert "harness_policy" in snapshot["segment_kinds"]
    assert snapshot["memory"]["hit_count"] == 1
    assert ti.metadata["context_structure_version"] == "context_structure.v2"
    assert ti.metadata["history_presentation"] == "background_context"
    assert {tag["authority"] for tag in ti.metadata["context_tags"]} >= {
        "instruction",
        "current_request",
        "retrieved_memory",
    }


async def test_summary_provider_injects_existing_summary_without_moving_current_turn() -> None:
    summary = _StubSummary("之前用户在比较两个方案，希望保持简短。")
    ti = make_turn_input("那现在你建议选哪个？")
    compiler = ContextCompiler(
        personas_service=_StubPersonas("[PERSONA]\nhi"),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        summary_provider=summary,
    )

    msgs = await compiler.compile(ti)

    assert "[BACKGROUND CONTEXT]" in msgs[0].content
    assert "之前用户在比较两个方案" in msgs[0].content
    assert msgs[-1].role is MessageRole.USER
    assert msgs[-1].content == "那现在你建议选哪个？"
    assert ti.metadata["summary_trace"]["attempted"] is True
    assert ti.metadata["summary_trace"]["context_injected"] is True
    assert ti.metadata["context_focus"] == {
        "current_user_last": True,
        "current_user_token_estimate": 3,
        "summary_injected": True,
        "raw_history_message_count": 0,
        "background_history_message_count": 0,
        "history_presentation": "background_context",
    }
    assert "之前用户在比较" not in str(ti.metadata["summary_trace"])
    assert summary.calls == ["c1"]


async def test_summary_provider_is_skipped_when_history_context_is_private() -> None:
    summary = _StubSummary("should-not-appear")
    ti = make_turn_input("private turn")
    ti.metadata["private"] = True
    compiler = ContextCompiler(
        personas_service=_StubPersonas("[PERSONA]\nhi"),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        summary_provider=summary,
    )

    msgs = await compiler.compile(ti)

    assert "should-not-appear" not in msgs[0].content
    assert summary.calls == []
    assert ti.metadata["summary_trace"]["attempted"] is False


async def test_budget_can_drop_summary_before_current_turn() -> None:
    ti = make_turn_input("当前问题")
    compiler = ContextCompiler(
        personas_service=_StubPersonas("[P]"),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        summary_provider=_StubSummary("summary " * 120),
        context_budget_tokens=20,
    )

    msgs = await compiler.compile(ti)

    assert "summary " not in msgs[0].content
    assert msgs[-1].content == "当前问题"
    assert ti.metadata["summary_trace"]["context_injected"] is False
    dropped = ti.metadata["context_ledger"]["dropped_segments"]
    assert any(s["kind"] == "summary" for s in dropped)


async def test_memory_trace_records_ids_and_degraded_without_content() -> None:
    ti = make_turn_input("帮我回忆一下")
    compiler = ContextCompiler(
        personas_service=_StubPersonas(),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        memory_port=_StubMemory(
            formatted="private recalled sentence",
            diagnostics={"embedding_ms": 12.5, "service_total_ms": 41.0},
        ),
    )

    await compiler.compile(ti)

    trace = ti.metadata["memory_trace"]
    assert trace["attempted"] is True
    assert trace["degraded"] is False
    assert trace["hit_ids"] == ["mem-1"]
    assert trace["hit_count"] == 1
    assert trace["kg_triple_ids"] == []
    assert trace["kg_triple_count"] == 0
    assert trace["context_injected"] is True
    assert trace["backend_trace"] == {
        "embedding_ms": 12.5,
        "service_total_ms": 41.0,
    }
    assert "private recalled sentence" not in str(trace)


async def test_memory_trace_records_kg_triple_ids_without_content() -> None:
    ti = make_turn_input("详细介绍铁锤")
    compiler = ContextCompiler(
        personas_service=_StubPersonas(),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        memory_port=_StubMemory(
            formatted="知识图谱事实：\n- [KG] 铁锤 的品种/身份是 边境牧羊犬",
            kg_triples=[
                {
                    "id": "t_pet_tiechui_role",
                    "subject": "pet:铁锤",
                    "predicate": "holds_role",
                    "object": "边境牧羊犬",
                }
            ],
        ),
    )

    await compiler.compile(ti)

    trace = ti.metadata["memory_trace"]
    assert trace["kg_triple_ids"] == ["t_pet_tiechui_role"]
    assert trace["kg_triple_count"] == 1
    assert "边境牧羊犬" not in str(trace)


async def test_memory_trace_records_degraded_reason_without_internal_prompt_detail() -> None:
    ti = make_turn_input("帮我回忆一下")
    compiler = ContextCompiler(
        personas_service=_StubPersonas(),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        memory_port=_StubMemory(degraded=True, degraded_reason="no_memory_route"),
    )

    msgs = await compiler.compile(ti)

    trace = ti.metadata["memory_trace"]
    assert trace["attempted"] is True
    assert trace["degraded"] is True
    assert trace["degraded_reason"] == "no_memory_route"
    assert trace["context_injected"] is True
    assert "no_memory_route" not in msgs[0].content


async def test_empty_text_skips_trailing_user_message() -> None:
    compiler = ContextCompiler(
        personas_service=_StubPersonas(),
        instance_locator=_locator,
        history_manager=HistoryManager(),
    )
    msgs = await compiler.compile(make_turn_input(""))
    assert msgs[-1].role is MessageRole.SYSTEM  # only system, no user


async def test_temporary_turn_skips_memory_and_history_context() -> None:
    history = HistoryManager()
    await history.append(
        conversation_id="c1",
        message=ChatMessage(
            id=uuid.uuid4().hex,
            role=MessageRole.USER,
            content="should-not-appear",
            created_at=_now(),
        ),
    )
    memory = _StubMemory(formatted="should-not-appear-memory")
    compiler = ContextCompiler(
        personas_service=_StubPersonas(),
        instance_locator=_locator,
        history_manager=history,
        memory_port=memory,
    )
    ti = make_turn_input("这段临时聊聊")
    ti.metadata["temporary"] = True

    msgs = await compiler.compile(ti)

    assert memory.calls == []
    assert [m.role for m in msgs] == [MessageRole.SYSTEM, MessageRole.USER]
    assert "should-not-appear" not in "\n".join(m.content for m in msgs)
    assert ti.metadata["memory_trace"]["attempted"] is False
    assert ti.metadata["memory_trace"]["skipped_reason"] == "privacy_policy"


async def test_interrupted_history_is_background_only_with_status_tag() -> None:
    history = HistoryManager()
    await history.append(
        conversation_id="c1",
        message=ChatMessage(
            id="interrupted-weather",
            role=MessageRole.ASSISTANT,
            content="我先查一下天气。",
            created_at=_now(),
            metadata={"interrupted": True, "tool_relation": "abandoned_due_to_interrupt"},
        ),
    )
    compiler = ContextCompiler(
        personas_service=_StubPersonas("[PERSONA]\nhi"),
        instance_locator=_locator,
        history_manager=history,
        memory_port=None,
    )
    ti = make_turn_input("当前问题")

    msgs = await compiler.compile(ti)

    assert [m.role for m in msgs] == [MessageRole.SYSTEM, MessageRole.USER]
    system = msgs[0].content
    assert "[BACKGROUND CONTEXT]" in system
    assert "status=interrupted" in system
    assert "tool_relation=abandoned_due_to_interrupt" in system
    assert "actionability=must_not_execute" in system
    assert any(
        tag["kind"] == "history"
        and tag["status"] == "interrupted"
        and tag["tool_relation"] == "abandoned_due_to_interrupt"
        for tag in ti.metadata["context_tags"]
    )


async def test_budget_keeps_recent_history_before_older_history() -> None:
    history = HistoryManager()
    await history.append(
        conversation_id="c1",
        message=ChatMessage(
            id="old-history",
            role=MessageRole.USER,
            content="old " * 24,
            created_at=_now(),
        ),
    )
    await history.append(
        conversation_id="c1",
        message=ChatMessage(
            id="new-history",
            role=MessageRole.ASSISTANT,
            content="new " * 24,
            created_at=_now(),
        ),
    )
    compiler = ContextCompiler(
        personas_service=_StubPersonas("[P]"),
        instance_locator=_locator,
        history_manager=history,
        # Leave room for exactly one 32-token history item after the current
        # persona, harness policy, and request segments.
        context_budget_tokens=282,
    )

    ti = make_turn_input("now")
    msgs = await compiler.compile(ti)

    joined = "\n".join(m.content for m in msgs)
    assert "new new" in joined
    assert "old old" not in joined
    dropped = ti.metadata["context_ledger"]["dropped_segments"]
    assert dropped == [
        {
            "kind": "history",
            "source": "history_manager",
            "token_estimate": 32,
            "reason": "token_budget_exceeded",
        }
    ]


async def test_budget_drops_oversized_memory_and_records_ledger() -> None:
    ti = make_turn_input("当前问题")
    compiler = ContextCompiler(
        personas_service=_StubPersonas("[P]"),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        memory_port=_StubMemory(formatted="memory " * 120),
        context_budget_tokens=20,
    )

    msgs = await compiler.compile(ti)

    assert "\n[RETRIEVED MEMORY]\n" not in f"\n{msgs[0].content}\n"
    assert ti.metadata["memory_trace"]["context_injected"] is False
    dropped = ti.metadata["context_ledger"]["dropped_segments"]
    assert any(s["kind"] == "memory" for s in dropped)


async def test_budget_shadow_records_would_drop_without_changing_prompt() -> None:
    ti = make_turn_input("当前问题")
    compiler = ContextCompiler(
        personas_service=_StubPersonas("[P]"),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        memory_port=_StubMemory(formatted="memory " * 120),
        context_budget_tokens=20,
        context_budget_mode="shadow",
    )

    msgs = await compiler.compile(ti)

    assert "[RETRIEVED MEMORY]" in msgs[0].content
    assert ti.metadata["memory_trace"]["context_injected"] is True
    guard = ti.metadata["development_guards"]["context_budget"]
    assert guard["mode"] == "shadow"
    assert guard["applied"] is False
    assert guard["dropped_count"] == 0
    assert guard["shadow_dropped_count"] > 0
    assert "memory" in guard["shadow_dropped_kinds"]


async def test_budget_keeps_degraded_memory_notice_even_over_budget() -> None:
    memory = _StubMemory(formatted="", degraded=True)
    ti = make_turn_input("你还记得什么？")
    compiler = ContextCompiler(
        personas_service=_StubPersonas("[P]"),
        instance_locator=_locator,
        history_manager=HistoryManager(),
        memory_port=memory,
        context_budget_tokens=5,
    )

    msgs = await compiler.compile(ti)

    assert "长期记忆召回暂不可用" in msgs[0].content
    assert ti.metadata["memory_trace"]["context_injected"] is True
    assert "memory" in ti.metadata["context_ledger"]["degraded_sources"]
