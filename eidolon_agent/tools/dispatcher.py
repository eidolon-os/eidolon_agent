"""Parallel-with-serial-tail tool dispatcher.

Side-effect-free tools (``side_effect=False``) run concurrently. Side-effect
tools run serially in submission order, AFTER all parallel tools resolve. This
keeps "look at state then change state" workflows deterministic without
sacrificing the common-case parallelism win.

Idempotency is enforced via NATS KV (``EIDOLON_TOOL_IDEMP`` bucket) when the
tool declares an ``idempotency_key_template``.
"""

from __future__ import annotations

import asyncio
import time
from string import Template

from eidolon_agent.core.errors import ToolError, ToolPermissionError, ToolTimeoutError
from eidolon_agent.core.ports.tool import ToolInvocationContext, ToolPort
from eidolon_agent.core.types.tool import ToolCall, ToolResult
from eidolon_agent.tools.registry import ToolRegistry


class ToolDispatcher:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        idempotency_store=None,  # KVStore (EIDOLON_TOOL_IDEMP)
        allowed_permissions: set | None = None,
    ) -> None:
        self._registry = registry
        self._idemp = idempotency_store
        self._allowed = allowed_permissions  # None = no policy enforcement here

    async def dispatch_batch(
        self,
        calls: list[ToolCall],
        *,
        ctx: ToolInvocationContext,
    ) -> list[ToolResult]:
        """Run a batch. Returns results in the SAME order as ``calls``."""
        parallel_idx: list[int] = []
        parallel_tasks: list[asyncio.Task[ToolResult]] = []
        serial_idx: list[int] = []
        results: list[ToolResult | None] = [None] * len(calls)

        for i, call in enumerate(calls):
            try:
                tool = self._registry.get(call.name)
            except Exception as exc:
                results[i] = ToolResult(
                    call_id=call.id,
                    name=call.name,
                    ok=False,
                    error_code="tool_not_found",
                    error_message=str(exc),
                )
                continue
            try:
                self._check_permissions(tool)
            except ToolPermissionError as exc:
                results[i] = ToolResult(
                    call_id=call.id,
                    name=call.name,
                    ok=False,
                    error_code=exc.code,
                    error_message=exc.message,
                )
                continue
            if tool.schema.side_effect:
                serial_idx.append(i)
            else:
                parallel_idx.append(i)
                parallel_tasks.append(
                    asyncio.create_task(self._invoke_one(tool, call, ctx=ctx))
                )

        # Parallel phase
        if parallel_tasks:
            done_results = await asyncio.gather(*parallel_tasks, return_exceptions=True)
            for idx, res in zip(parallel_idx, done_results, strict=True):
                results[idx] = self._normalize_result(calls[idx], res)

        # Serial phase
        for idx in serial_idx:
            call = calls[idx]
            tool = self._registry.get(call.name)
            try:
                res = await self._invoke_one(tool, call, ctx=ctx)
            except Exception as exc:
                res = exc
            results[idx] = self._normalize_result(call, res)

        # No None should remain.
        return [r for r in results if r is not None]

    # ---- Internals -----------------------------------------------------------

    async def _invoke_one(
        self,
        tool: ToolPort,
        call: ToolCall,
        *,
        ctx: ToolInvocationContext,
    ) -> ToolResult:
        # Idempotency check
        idemp_key = self._maybe_idemp_key(tool, call, ctx)
        if idemp_key is not None and self._idemp is not None:
            cached = await self._idemp.get(idemp_key)
            if cached is not None:
                # Stored as raw JSON; deserialize lazily
                import json

                payload = json.loads(cached.decode())
                return ToolResult(
                    call_id=call.id,
                    name=call.name,
                    ok=payload.get("ok", True),
                    content=payload.get("content"),
                    metadata={"idempotent_cache": True},
                )

        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                tool.invoke(call, ctx=ctx), timeout=tool.schema.timeout_s
            )
        except asyncio.TimeoutError:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                error_code=ToolTimeoutError.code,
                error_message=f"tool {call.name} exceeded {tool.schema.timeout_s}s",
                latency_ms=int((time.monotonic() - start) * 1000),
            )

        # Cache idempotent successful result
        if (
            idemp_key is not None
            and self._idemp is not None
            and result.ok
        ):
            import json

            await self._idemp.put(
                idemp_key,
                json.dumps({"ok": True, "content": result.content}, default=str).encode(),
            )

        return result

    def _check_permissions(self, tool: ToolPort) -> None:
        if self._allowed is None:
            return
        missing = set(tool.schema.permissions) - self._allowed
        if missing:
            raise ToolPermissionError(
                f"tool {tool.schema.name} needs {sorted(p.value for p in missing)} but instance lacks them"
            )

    def _maybe_idemp_key(
        self, tool: ToolPort, call: ToolCall, ctx: ToolInvocationContext
    ) -> str | None:
        tmpl = tool.schema.idempotency_key_template
        if not tmpl:
            return None
        try:
            return Template(tmpl).safe_substitute(
                user_id=ctx.caller.user_id,
                tenant_id=ctx.caller.tenant_id,
                turn_id=ctx.turn_id,
                **call.arguments,
            )
        except Exception:
            return None

    @staticmethod
    def _normalize_result(call: ToolCall, res) -> ToolResult:  # type: ignore[no-untyped-def]
        if isinstance(res, ToolResult):
            return res
        if isinstance(res, Exception):
            err = res
            return ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                error_code=getattr(err, "code", ToolError.code),
                error_message=str(err),
            )
        # Unexpected return — wrap defensively
        return ToolResult(
            call_id=call.id,
            name=call.name,
            ok=False,
            error_code="tool_invalid_return",
            error_message=f"tool returned {type(res).__name__}",
        )
