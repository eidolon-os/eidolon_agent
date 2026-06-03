"""Tool dispatcher.

Readonly tools run concurrently within each contiguous readonly batch; tools
declaring side effects run serially and preserve "look at state then change
state" determinism.

Idempotency is enforced via NATS KV (``EIDOLON_TOOL_IDEMP`` bucket) when the
tool declares an ``idempotency_key_template``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import replace
from string import Template
from typing import Any

from eidolon_agent.core.errors import ToolError, ToolPermissionError, ToolTimeoutError
from eidolon_agent.core.ports.tool import ToolInvocationContext, ToolPort
from eidolon_agent.core.types.tool import ToolCall, ToolResult
from eidolon_agent.domain.tools.registry import ToolRegistry

_log = logging.getLogger(__name__)


class ToolDispatcher:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        idempotency_store=None,  # KVStore (EIDOLON_TOOL_IDEMP)
        allowed_permissions: set | None = None,
        schema_strict: bool = True,
        batch_timeout_s: float | None = None,
        require_idempotency_for_side_effect: bool = False,
    ) -> None:
        self._registry = registry
        self._idemp = idempotency_store
        self._allowed = allowed_permissions  # None = no policy enforcement here
        self._schema_strict = schema_strict
        self._batch_timeout_s = batch_timeout_s
        self._require_idempotency_for_side_effect = require_idempotency_for_side_effect

    async def dispatch_batch(
        self,
        calls: list[ToolCall],
        *,
        ctx: ToolInvocationContext,
    ) -> list[ToolResult]:
        """Run readonly calls concurrently and side-effectful calls serially.

        Results are always returned in the same order as ``calls``.
        """

        if self._batch_timeout_s is None:
            return await self._dispatch_batch_inner(calls, ctx=ctx)
        try:
            return await asyncio.wait_for(
                self._dispatch_batch_inner(calls, ctx=ctx),
                timeout=self._batch_timeout_s,
            )
        except asyncio.TimeoutError:
            return [
                ToolResult(
                    call_id=c.id,
                    name=c.name,
                    ok=False,
                    error_code=ToolTimeoutError.code,
                    error_message=f"tool batch exceeded {self._batch_timeout_s}s",
                )
                for c in calls
            ]

    async def _dispatch_batch_inner(
        self,
        calls: list[ToolCall],
        *,
        ctx: ToolInvocationContext,
    ) -> list[ToolResult]:
        results: list[ToolResult | None] = [None] * len(calls)
        readonly: list[tuple[int, ToolCall]] = []

        async def flush_readonly() -> None:
            if not readonly:
                return
            batch = list(readonly)
            readonly.clear()
            done = await asyncio.gather(
                *(self._dispatch_one(call, ctx=ctx) for _, call in batch)
            )
            for (idx, _call), result in zip(batch, done, strict=True):
                results[idx] = result

        for idx, call in enumerate(calls):
            try:
                tool = self._registry.get(call.name)
            except Exception:
                await flush_readonly()
                results[idx] = await self._dispatch_one(call, ctx=ctx)
                continue
            if tool.schema.side_effect:
                await flush_readonly()
                results[idx] = await self._dispatch_one(call, ctx=ctx)
            else:
                readonly.append((idx, call))
        await flush_readonly()
        return [r for r in results if r is not None]

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
        schema_error = _validate_json_schema(tool.schema.json_schema, call.arguments)
        if schema_error is not None:
            if self._schema_strict:
                return ToolResult(
                    call_id=call.id,
                    name=call.name,
                    ok=False,
                    error_code="invalid_tool_arguments",
                    error_message=schema_error,
                )
            _log.warning(
                "tool %s arguments failed schema validation in compatibility mode: %s",
                call.name,
                schema_error,
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
        if (
            self._require_idempotency_for_side_effect
            and tool.schema.side_effect
            and not tool.schema.idempotency_key_template
        ):
            return ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                error_code="tool_requires_idempotency",
                error_message=f"side-effect tool {call.name} requires idempotency",
            )
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
                    error_code=payload.get("error_code"),
                    error_message=payload.get("error_message"),
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
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if result.latency_ms == 0:
            result = replace(result, latency_ms=elapsed_ms)

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


def _validate_json_schema(schema: dict[str, Any], value: Any, *, path: str = "$") -> str | None:
    """Tiny validator for the JSON Schema subset our tool schemas use."""

    expected = schema.get("type")
    if expected is not None:
        ok = {
            "object": isinstance(value, dict),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "array": isinstance(value, list),
        }.get(expected, True)
        if not ok:
            return f"{path} expected {expected}"

    if expected == "object":
        required = set(schema.get("required") or [])
        missing = sorted(k for k in required if k not in value)
        if missing:
            return f"{path} missing required: {', '.join(missing)}"
        props = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            extra = sorted(k for k in value if k not in props)
            if extra:
                return f"{path} unexpected properties: {', '.join(extra)}"
        for key, subschema in props.items():
            if key in value:
                err = _validate_json_schema(subschema, value[key], path=f"{path}.{key}")
                if err is not None:
                    return err
    if expected == "array" and "items" in schema:
        for idx, item in enumerate(value):
            err = _validate_json_schema(schema["items"], item, path=f"{path}[{idx}]")
            if err is not None:
                return err
    return None
