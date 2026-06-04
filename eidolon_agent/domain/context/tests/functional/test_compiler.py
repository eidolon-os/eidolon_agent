"""ContextCompiler — direct prompt assembly.

No more pluggable providers; tests verify the fixed shape:
  [system: persona + memory + realtime] + [history] + [user_input]
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

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

    def __init__(self, formatted: str = "", *, degraded: bool = False) -> None:
        self._formatted = formatted
        self._degraded = degraded
        self.calls: list[dict] = []

    async def recall_context(self, *, user_id, query, plan, timeout_s):
        self.calls.append({"user_id": user_id, "query": query})
        return self._formatted, [SimpleNamespace(id="mem-1")], self._degraded


def _locator(_t, _u, _c):
    return ("inst-test", "tpl-x")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def test_assembles_system_history_user_in_order() -> None:
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

    assert [m.role for m in msgs] == [
        MessageRole.SYSTEM,
        MessageRole.USER,       # earlier-q
        MessageRole.ASSISTANT,  # earlier-a
        MessageRole.USER,       # current
    ]
    assert "[PERSONA]" in msgs[0].content
    assert msgs[-1].content == "当前问题"


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
    assert "[MEMORY]\nprior_episode_summary" in msgs[0].content
    assert memory.calls and memory.calls[0]["query"] == "帮我回忆一下"


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
    assert "[MEMORY]" in system
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
    assert {s["kind"] for s in ledger["segments"]} >= {"persona", "memory", "current_user"}
    assert "secret-system-prompt" not in str(ledger)
    assert "recalled-private-detail" not in str(ledger)


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
    assert trace["context_injected"] is True
    assert "private recalled sentence" not in str(trace)


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
        context_budget_tokens=35,
    )

    ti = make_turn_input("now")
    msgs = await compiler.compile(ti)

    assert "new " in "\n".join(m.content for m in msgs)
    assert "old " not in "\n".join(m.content for m in msgs)
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

    assert "[MEMORY]" not in msgs[0].content
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

    assert "[MEMORY]" in msgs[0].content
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
