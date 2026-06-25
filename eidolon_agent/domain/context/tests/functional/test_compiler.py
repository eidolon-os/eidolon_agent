"""ContextCompiler — direct prompt assembly.

No more pluggable providers; tests verify the fixed structured shape:
  [system: instructions + reference/background + current request] + [user_input]
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from eidolon_agent.core.types.memory import MemoryRecallResult
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.domain.context.compiler import ContextCompiler
from eidolon_agent.domain.history.manager import HistoryManager
from tests.helpers import make_turn_input

pytestmark = pytest.mark.functional


class _StubPersonas:
    """Minimal PersonasService stand-in returning a canned system prompt."""

    def __init__(self, prompt: str = "[PERSONA]\nyou are an assistant") -> None:
        self._prompt = prompt
        self.calls: list[dict] = []

    async def compile_prompt(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(system_prompt=self._prompt, debug_trace=())


class _StubMemory:
    """Memory port stub used by recall integration tests."""

    def __init__(
        self,
        formatted: str = "",
        *,
        degraded: bool = False,
        degraded_reason: str | None = None,
        kg_triples: list[dict] | None = None,
    ) -> None:
        self._formatted = formatted
        self._degraded = degraded
        self._degraded_reason = degraded_reason
        self._kg_triples = list(kg_triples or [])
        self.calls: list[dict] = []

    async def recall_context(self, *, user_id, query, plan, timeout_s, **identity):
        self.calls.append({"user_id": user_id, "query": query, "identity": identity})
        return MemoryRecallResult(
            context=self._formatted,
            hits=[SimpleNamespace(id="mem-1")],
            kg_triples=self._kg_triples,
            degraded=self._degraded,
            degraded_reason=self._degraded_reason,
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
    return datetime.now(timezone.utc)


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


async def test_persona_locator_args_match_turn_input() -> None:
    personas = _StubPersonas()
    compiler = ContextCompiler(
        personas_service=personas,
        instance_locator=lambda t, u, c: (f"{t}/{u}", "tpl"),
        history_manager=HistoryManager(),
    )
    ti = make_turn_input("hi")
    # Override caller fields via direct attribute (TurnInput is frozen, so this
    # just sanity-checks the default args go through).
    await compiler.compile(ti)
    call = personas.calls[0]
    assert call["user_id"] == ti.caller.user_id
    assert call["instance_id"] == f"{ti.caller.tenant_id}/{ti.caller.user_id}"
    assert call["template_id"] == "tpl"
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
    assert "memory backend" in system  # part of the notice
    # The notice explicitly tells the LLM not to fake memory access.
    assert "不要假装" in system or "如实承认" in system


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
    assert "memory backend" not in system  # the notice keyword must not appear


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

    assert "memory backend" in msgs[0].content
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
    assert {
        tag["authority"] for tag in ti.metadata["context_tags"]
    } >= {"instruction", "current_request", "retrieved_memory"}


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
        memory_port=_StubMemory(formatted="private recalled sentence"),
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
        context_budget_tokens=260,
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

    assert "memory backend" in msgs[0].content
    assert ti.metadata["memory_trace"]["context_injected"] is True
    assert "memory" in ti.metadata["context_ledger"]["degraded_sources"]
