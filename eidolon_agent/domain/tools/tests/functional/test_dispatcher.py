"""ToolDispatcher — parallel-with-serial-tail semantics, timeout, idempotency."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace

import pytest

from eidolon_agent.core.errors import ToolTimeoutError
from eidolon_agent.core.types.tool import Permission, ToolCall, ToolResult
from eidolon_agent.domain.tools import ToolDispatcher, ToolRegistry

pytestmark = pytest.mark.functional


class _FakeIdempotencyStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.keys_seen: list[str] = []

    async def get(self, key: str) -> bytes | None:
        self.keys_seen.append(key)
        assert re.fullmatch(r"idemp_[0-9a-f]{64}", key)
        return self.values.get(key)

    async def put(self, key: str, value: bytes) -> None:
        self.keys_seen.append(key)
        assert re.fullmatch(r"idemp_[0-9a-f]{64}", key)
        self.values[key] = value


def _call(name: str, cid: str = "c", args: dict | None = None) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args or {})


async def test_unknown_tool_returns_tool_not_found(tool_ctx) -> None:
    disp = ToolDispatcher(ToolRegistry())
    results = await disp.dispatch_batch([_call("ghost")], ctx=tool_ctx)
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].error_code == "tool_not_found"


async def test_extra_tools_overlay_resolves_before_registry(stub_tool_factory, tool_ctx) -> None:
    # A per-turn dynamic tool (not in the registry) dispatches via the overlay.
    overlay = stub_tool_factory("body__dev__display_update")
    ctx = replace(tool_ctx, extra_tools={"body__dev__display_update": overlay})
    disp = ToolDispatcher(ToolRegistry())  # empty registry
    results = await disp.dispatch_batch([_call("body__dev__display_update")], ctx=ctx)
    assert len(results) == 1 and results[0].ok is True
    assert len(overlay.calls) == 1  # the overlay tool ran


async def test_denied_tool_is_not_dispatched(stub_tool_factory, tool_ctx) -> None:
    # A denied tool must not actuate even though it is a real registered tool.
    real = stub_tool_factory("reboot", side_effect=True)
    reg = ToolRegistry()
    reg.register(real)
    ctx = replace(tool_ctx, denied_tools=frozenset({"reboot"}))
    disp = ToolDispatcher(reg)
    results = await disp.dispatch_batch([_call("reboot")], ctx=ctx)
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].error_code == "tool_not_found"
    assert real.calls == []  # never invoked


async def test_dispatch_returns_results_in_input_order(stub_tool_factory, tool_ctx) -> None:
    reg = ToolRegistry()
    reg.register(stub_tool_factory("alpha"))
    reg.register(stub_tool_factory("beta"))
    reg.register(stub_tool_factory("gamma"))
    disp = ToolDispatcher(reg)
    results = await disp.dispatch_batch(
        [_call("alpha", "1"), _call("beta", "2"), _call("gamma", "3")], ctx=tool_ctx
    )
    assert [r.call_id for r in results] == ["1", "2", "3"]
    assert all(r.ok for r in results)


async def test_readonly_calls_run_concurrently_but_results_keep_order(
    stub_tool_factory, tool_ctx
) -> None:
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
    reg.register(stub_tool_factory("fast", side_effect=False, invoke=fast_invoke))
    disp = ToolDispatcher(reg)
    results = await disp.dispatch_batch([_call("slow", "s1"), _call("fast", "f1")], ctx=tool_ctx)
    assert order == ["fast:f1", "slow:s1"]
    assert [r.call_id for r in results] == ["s1", "f1"]
    assert results[0].latency_ms >= 15


async def test_side_effect_calls_wait_for_prior_readonly_batch(
    stub_tool_factory, tool_ctx
) -> None:
    order: list[str] = []

    async def slow(call, ctx):
        await asyncio.sleep(0.02)
        order.append(call.name)
        return ToolResult(call_id=call.id, name=call.name, ok=True)

    async def effect(call, ctx):
        order.append(call.name)
        return ToolResult(call_id=call.id, name=call.name, ok=True)

    reg = ToolRegistry()
    reg.register(stub_tool_factory("slow", invoke=slow))
    reg.register(stub_tool_factory("effect", side_effect=True, invoke=effect))

    await ToolDispatcher(reg).dispatch_batch(
        [_call("slow", "1"), _call("effect", "2")], ctx=tool_ctx
    )

    assert order == ["slow", "effect"]


async def test_timeout_returns_error(stub_tool_factory, tool_ctx) -> None:
    async def slow(call, ctx):
        await asyncio.sleep(1.0)  # >> timeout_s
        return ToolResult(call_id=call.id, name="slow", ok=True, content={})

    reg = ToolRegistry()
    reg.register(stub_tool_factory("slow", invoke=slow, timeout_s=0.05))
    disp = ToolDispatcher(reg)
    [res] = await disp.dispatch_batch([_call("slow")], ctx=tool_ctx)
    assert res.ok is False
    assert res.error_code == ToolTimeoutError.code


async def test_strict_schema_rejects_invalid_arguments(stub_tool_factory, tool_ctx) -> None:
    reg = ToolRegistry()
    reg.register(
        stub_tool_factory(
            "needs_subject",
            json_schema={
                "type": "object",
                "properties": {"subject": {"type": "string"}},
                "required": ["subject"],
                "additionalProperties": False,
            },
        )
    )

    [res] = await ToolDispatcher(reg, schema_strict=True).dispatch_batch(
        [_call("needs_subject", args={"extra": 1})],
        ctx=tool_ctx,
    )

    assert res.ok is False
    assert res.error_code == "invalid_tool_arguments"


async def test_schema_compat_mode_warns_and_invokes(stub_tool_factory, tool_ctx, caplog) -> None:
    reg = ToolRegistry()
    reg.register(
        stub_tool_factory(
            "compat",
            json_schema={
                "type": "object",
                "properties": {"subject": {"type": "string"}},
                "required": ["subject"],
                "additionalProperties": False,
            },
        )
    )

    [res] = await ToolDispatcher(reg, schema_strict=False).dispatch_batch(
        [_call("compat", args={})],
        ctx=tool_ctx,
    )

    assert res.ok is True
    assert "compatibility mode" in caplog.text


async def test_permission_denied_when_instance_lacks_permission(
    stub_tool_factory, tool_ctx
) -> None:
    reg = ToolRegistry()
    reg.register(stub_tool_factory("system_tool", permissions={Permission.SYSTEM}))

    [res] = await ToolDispatcher(reg, allowed_permissions=set()).dispatch_batch(
        [_call("system_tool")],
        ctx=tool_ctx,
    )

    assert res.ok is False
    assert res.error_code == "eidolon.tool_permission_denied"


async def test_side_effect_tool_can_require_idempotency(stub_tool_factory, tool_ctx) -> None:
    reg = ToolRegistry()
    reg.register(stub_tool_factory("effect", side_effect=True))

    [res] = await ToolDispatcher(
        reg,
        require_idempotency_for_side_effect=True,
    ).dispatch_batch([_call("effect")], ctx=tool_ctx)

    assert res.ok is False
    assert res.error_code == "tool_requires_idempotency"


async def test_idempotency_key_hashes_runtime_identity_and_arguments(
    stub_tool_factory,
    tool_ctx,
) -> None:
    reg = ToolRegistry()
    reg.register(
        stub_tool_factory(
            "remember",
            side_effect=True,
            idempotency_key_template="${owner_id}:${companion_id}:${subject}:${predicate}:${object}",
        )
    )
    store = _FakeIdempotencyStore()

    [res] = await ToolDispatcher(reg, idempotency_store=store).dispatch_batch(
        [
            _call(
                "remember",
                args={
                    "subject": "用户",
                    "predicate": "工作记录",
                    "object": "在常州工作-异常定位-20260628-1950",
                },
            )
        ],
        ctx=tool_ctx,
    )

    assert res.ok is True
    assert len(set(store.keys_seen)) == 1


async def test_idempotency_cache_uses_logical_trace_across_retry_turn_ids(
    stub_tool_factory,
    tool_ctx,
) -> None:
    tool = stub_tool_factory(
        "external_action",
        side_effect=True,
        idempotency_key_template="${owner_id}:${trace_id}:${arguments_json}",
    )
    reg = ToolRegistry()
    reg.register(tool)
    store = _FakeIdempotencyStore()
    dispatcher = ToolDispatcher(reg, idempotency_store=store)
    first_ctx = replace(tool_ctx, turn_id="turn-1")
    retry_ctx = replace(tool_ctx, turn_id="turn-retry")

    [first] = await dispatcher.dispatch_batch(
        [_call("external_action", "call-1", {"enabled": True})],
        ctx=first_ctx,
    )
    [retry] = await dispatcher.dispatch_batch(
        [_call("external_action", "call-2", {"enabled": True})],
        ctx=retry_ctx,
    )
    [changed] = await dispatcher.dispatch_batch(
        [_call("external_action", "call-3", {"enabled": False})],
        ctx=retry_ctx,
    )

    assert first.ok and retry.ok and changed.ok
    assert retry.metadata["idempotent_cache"] is True
    assert len(tool.calls) == 2


async def test_batch_timeout_returns_stable_errors(stub_tool_factory, tool_ctx) -> None:
    async def slow(call, ctx):
        await asyncio.sleep(1)
        return ToolResult(call_id=call.id, name=call.name, ok=True)

    reg = ToolRegistry()
    reg.register(stub_tool_factory("slow", invoke=slow, timeout_s=2.0))

    results = await ToolDispatcher(reg, batch_timeout_s=0.01).dispatch_batch(
        [_call("slow", "1"), _call("slow", "2")],
        ctx=tool_ctx,
    )

    assert [r.error_code for r in results] == [ToolTimeoutError.code, ToolTimeoutError.code]
