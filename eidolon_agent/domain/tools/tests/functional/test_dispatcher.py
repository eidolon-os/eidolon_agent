"""ToolDispatcher — parallel-with-serial-tail semantics, timeout, idempotency."""

from __future__ import annotations

import asyncio

import pytest

from eidolon_agent.core.errors import ToolTimeoutError
from eidolon_agent.core.types.tool import ToolCall, ToolResult
from eidolon_agent.domain.tools import ToolDispatcher, ToolRegistry

pytestmark = pytest.mark.functional


def _call(name: str, cid: str = "c", args: dict | None = None) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args or {})


async def test_unknown_tool_returns_tool_not_found(caller_ctx) -> None:
    disp = ToolDispatcher(ToolRegistry())
    results = await disp.dispatch_batch([_call("ghost")], ctx=caller_ctx)
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].error_code == "tool_not_found"


async def test_dispatch_returns_results_in_input_order(stub_tool_factory, caller_ctx) -> None:
    reg = ToolRegistry()
    reg.register(stub_tool_factory("alpha"))
    reg.register(stub_tool_factory("beta"))
    reg.register(stub_tool_factory("gamma"))
    disp = ToolDispatcher(reg)
    results = await disp.dispatch_batch(
        [_call("alpha", "1"), _call("beta", "2"), _call("gamma", "3")], ctx=caller_ctx
    )
    assert [r.call_id for r in results] == ["1", "2", "3"]
    assert all(r.ok for r in results)


async def test_calls_run_strictly_in_submission_order(stub_tool_factory, caller_ctx) -> None:
    """Sequential dispatcher: tools run one at a time in input order regardless
    of side-effect flag. (Replaced earlier parallel-with-serial-tail semantics.)"""
    order: list[str] = []

    async def slow_invoke(call, ctx):
        await asyncio.sleep(0.02)
        order.append(f"{call.name}:{call.id}")
        return ToolResult(call_id=call.id, name=call.name, ok=True, content={})

    async def fast_invoke(call, ctx):
        order.append(f"{call.name}:{call.id}")
        return ToolResult(call_id=call.id, name=call.name, ok=True, content={})

    reg = ToolRegistry()
    reg.register(stub_tool_factory("slow", side_effect=False, invoke=slow_invoke))
    reg.register(stub_tool_factory("fast", side_effect=True, invoke=fast_invoke))
    disp = ToolDispatcher(reg)
    # 'slow' is submitted first → must complete before 'fast' runs.
    await disp.dispatch_batch(
        [_call("slow", "s1"), _call("fast", "f1")], ctx=caller_ctx
    )
    assert order == ["slow:s1", "fast:f1"]


async def test_timeout_returns_error(stub_tool_factory, caller_ctx) -> None:
    async def slow(call, ctx):
        await asyncio.sleep(1.0)  # >> timeout_s
        return ToolResult(call_id=call.id, name="slow", ok=True, content={})

    reg = ToolRegistry()
    reg.register(stub_tool_factory("slow", invoke=slow, timeout_s=0.05))
    disp = ToolDispatcher(reg)
    [res] = await disp.dispatch_batch([_call("slow")], ctx=caller_ctx)
    assert res.ok is False
    assert res.error_code == ToolTimeoutError.code
