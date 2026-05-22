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


async def test_side_effect_tools_run_after_pure_ones(stub_tool_factory, caller_ctx) -> None:
    order: list[str] = []

    async def pure_invoke(call, ctx):
        await asyncio.sleep(0.02)  # finish before serial
        order.append(f"pure:{call.id}")
        return ToolResult(call_id=call.id, name="pure", ok=True, content={})

    async def side_invoke(call, ctx):
        order.append(f"side:{call.id}")
        return ToolResult(call_id=call.id, name="side", ok=True, content={})

    reg = ToolRegistry()
    reg.register(stub_tool_factory("pure", side_effect=False, invoke=pure_invoke))
    reg.register(stub_tool_factory("side", side_effect=True, invoke=side_invoke))
    disp = ToolDispatcher(reg)
    # Submit side BEFORE pure; dispatcher still must defer side until pure resolves.
    await disp.dispatch_batch(
        [_call("side", "s1"), _call("pure", "p1")], ctx=caller_ctx
    )
    assert order.index("pure:p1") < order.index("side:s1")


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
