"""Sequential tool dispatcher.

A turn invokes 1-2 tools per round at most; parallelism doesn't help when the
LLM call dominates wall-clock. Sequential keeps "look at state then change
state" workflows deterministic by construction.

Idempotency is enforced via NATS KV (``EIDOLON_TOOL_IDEMP`` bucket) when the
tool declares an ``idempotency_key_template``.
"""

from __future__ import annotations

import asyncio
import json
import time
from string import Template

from eidolon_agent.core.errors import ToolError, ToolPermissionError, ToolTimeoutError
from eidolon_agent.core.ports.tool import ToolInvocationContext, ToolPort
from eidolon_agent.core.types.tool import ToolCall, ToolResult
from eidolon_agent.domain.tools.registry import ToolRegistry


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
        """Run calls one after another. Order preserved."""
        out: list[ToolResult] = []
        for call in calls:
            out.append(await self._dispatch_one(call, ctx=ctx))
        return out

    # ---- Internals -----------------------------------------------------------

    async def _dispatch_one(
        self, call: ToolCall, *, ctx: ToolInvocationContext
    ) -> ToolResult:
        try:
            tool = self._registry.get(call.name)
        except Exception as exc:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                error_code="tool_not_found",
                error_message=str(exc),
            )
        try:
            self._check_permissions(tool)
        except ToolPermissionError as exc:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                error_code=exc.code,
                error_message=exc.message,
            )
        try:
            return await self._invoke_one(tool, call, ctx=ctx)
        except Exception as exc:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                error_code=getattr(exc, "code", ToolError.code),
                error_message=str(exc),
            )

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

        if idemp_key is not None and self._idemp is not None and result.ok:
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
