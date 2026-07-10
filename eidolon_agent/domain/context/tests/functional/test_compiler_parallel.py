"""ContextCompiler — the three fetches (persona / memory / history) must run
concurrently. We assert that by giving each branch an artificial 100 ms delay
and checking total wall-clock < 200 ms (would be ~300 ms if serial).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.domain.context.compiler import ContextCompiler
from tests.helpers import make_turn_input

pytestmark = pytest.mark.functional

_DELAY = 0.1  # 100 ms per branch


class _SlowPersonas:
    async def realize_context(self, **_kwargs):
        await asyncio.sleep(_DELAY)
        return SimpleNamespace(system_prompt="[PERSONA]\nhi", debug_trace=())


class _SlowMemory:
    async def recall_context(self, **_kwargs):
        await asyncio.sleep(_DELAY)
        return "prior", [], False


class _SlowHistory:
    async def recent_window(self, *, conversation_id, window):
        await asyncio.sleep(_DELAY)
        return [
            ChatMessage(
                id=uuid.uuid4().hex,
                role=MessageRole.USER,
                content="earlier",
                created_at=datetime.now(timezone.utc),
            )
        ]


def _locator(_t, _u, _c):
    return ("inst", "tpl")


async def test_three_fetches_run_concurrently() -> None:
    compiler = ContextCompiler(
        personas_service=_SlowPersonas(),
        instance_locator=_locator,
        history_manager=_SlowHistory(),
        memory_port=_SlowMemory(),
    )

    start = time.perf_counter()
    msgs = await compiler.compile(make_turn_input("ask"))
    elapsed = time.perf_counter() - start

    # Serial would be ~3 * _DELAY = 300 ms. Concurrent must be ~_DELAY plus a
    # small constant. 200 ms cap gives generous headroom for CI jitter.
    assert elapsed < 2 * _DELAY, f"expected concurrent execution, took {elapsed:.3f}s"

    # All three branches produced output.
    system = msgs[0].content
    assert "[PERSONA]" in system
    assert "[RETRIEVED MEMORY]" in system
    assert "[BACKGROUND CONTEXT]" in system
    assert "earlier" in system
    assert [m.role for m in msgs] == [MessageRole.SYSTEM, MessageRole.USER]
    assert msgs[-1].content == "ask"


async def test_memory_failure_injects_degraded_notice_into_prompt() -> None:
    """Phase 29.B.1: a memory recall failure no longer degrades *silently*.

    The compiler injects a structured ``[RETRIEVED MEMORY]`` block carrying the
    degraded-backend notice so the downstream LLM is told NOT to fake
    remembering prior context. This replaces the previous behavior
    (return None, append nothing) which produced an amnesiac-but-
    confident assistant — exactly the failure mode this change fixes.

    Test pins three things:
      1. compile() does not raise — memory is still non-critical
      2. the system message HAS a [RETRIEVED MEMORY] block (the degraded notice)
      3. the user input still appears as the trailing user message
    """

    class _BoomMemory:
        async def recall_context(self, **_):
            raise RuntimeError("upstream broken")

    compiler = ContextCompiler(
        personas_service=_SlowPersonas(),
        instance_locator=_locator,
        history_manager=_SlowHistory(),
        memory_port=_BoomMemory(),
    )
    msgs = await compiler.compile(make_turn_input("ask"))
    assert "[RETRIEVED MEMORY]" in msgs[0].content  # degraded notice is present
    # The notice must clearly scope the failure to recall, not memory writes.
    assert "长期记忆召回暂不可用" in msgs[0].content
    assert msgs[-1].content == "ask"


async def test_persona_failure_propagates() -> None:
    """Persona is mandatory — a failure must surface, not silently degrade."""

    class _BoomPersonas:
        async def realize_context(self, **_):
            raise RuntimeError("persona config broken")

    compiler = ContextCompiler(
        personas_service=_BoomPersonas(),
        instance_locator=_locator,
        history_manager=_SlowHistory(),
    )
    with pytest.raises(RuntimeError, match="persona config broken"):
        await compiler.compile(make_turn_input("ask"))
