"""HookExecutor — priority order, outcomes, exception handling."""

from __future__ import annotations

import pytest

from eidolon_agent.core.ports.hooks import (
    HookEvent,
    HookOutcome,
    HookPayload,
    HookResult,
)
from eidolon_agent.domain.hooks import CommandHook, HookExecutor

pytestmark = pytest.mark.unit


def _mk_hook(name: str, priority: int, *, outcome: HookOutcome = HookOutcome.CONTINUE, raise_exc: bool = False, log: list | None = None):
    async def fn(payload: HookPayload) -> HookResult:
        if log is not None:
            log.append(name)
        if raise_exc:
            raise RuntimeError(f"boom from {name}")
        return HookResult(outcome=outcome)

    return CommandHook(event=HookEvent.PRE_TURN, name=name, priority=priority, func=fn)


def _payload() -> HookPayload:
    return HookPayload(event=HookEvent.PRE_TURN, data={})


async def test_hooks_run_in_priority_order() -> None:
    ex = HookExecutor()
    log: list[str] = []
    ex.register(_mk_hook("third", priority=30, log=log))
    ex.register(_mk_hook("first", priority=10, log=log))
    ex.register(_mk_hook("second", priority=20, log=log))
    await ex.run(HookEvent.PRE_TURN, _payload())
    assert log == ["first", "second", "third"]


async def test_short_circuit_stops_chain() -> None:
    ex = HookExecutor()
    log: list[str] = []
    ex.register(_mk_hook("a", 10, log=log))
    ex.register(_mk_hook("b", 20, outcome=HookOutcome.SHORT_CIRCUIT, log=log))
    ex.register(_mk_hook("c", 30, log=log))
    result = await ex.run(HookEvent.PRE_TURN, _payload())
    assert result.outcome is HookOutcome.SHORT_CIRCUIT
    assert log == ["a", "b"]  # 'c' never ran


async def test_abort_stops_chain() -> None:
    ex = HookExecutor()
    log: list[str] = []
    ex.register(_mk_hook("a", 10, log=log))
    ex.register(_mk_hook("b", 20, outcome=HookOutcome.ABORT, log=log))
    ex.register(_mk_hook("c", 30, log=log))
    result = await ex.run(HookEvent.PRE_TURN, _payload())
    assert result.outcome is HookOutcome.ABORT
    assert log == ["a", "b"]


async def test_hook_exception_becomes_abort() -> None:
    ex = HookExecutor()
    log: list[str] = []
    ex.register(_mk_hook("a", 10, log=log))
    ex.register(_mk_hook("b", 20, raise_exc=True, log=log))
    ex.register(_mk_hook("c", 30, log=log))
    result = await ex.run(HookEvent.PRE_TURN, _payload())
    assert result.outcome is HookOutcome.ABORT
    assert "raised" in (result.reason or "")
    assert log == ["a", "b"]  # 'c' never ran


async def test_empty_chain_returns_continue() -> None:
    ex = HookExecutor()
    result = await ex.run(HookEvent.PRE_TURN, _payload())
    assert result.outcome is HookOutcome.CONTINUE


async def test_deregister_removes_hook() -> None:
    ex = HookExecutor()
    log: list[str] = []
    h = _mk_hook("a", 10, log=log)
    ex.register(h)
    ex.deregister(h)
    await ex.run(HookEvent.PRE_TURN, _payload())
    assert log == []


async def test_list_for_returns_only_event_hooks() -> None:
    ex = HookExecutor()
    ex.register(_mk_hook("pre", 10))
    pre = ex.list_for(HookEvent.PRE_TURN)
    post = ex.list_for(HookEvent.POST_LLM)
    assert len(pre) == 1 and post == []
