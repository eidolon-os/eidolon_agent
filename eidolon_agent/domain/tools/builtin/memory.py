"""Memory tools exposed to the LLM through ToolRegistry."""

from __future__ import annotations

from datetime import datetime

from eidolon_agent.core.ports.memory import MemoryPort
from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.memory import MemoryHit, MemoryScope, MemoryWriteOutcome
from eidolon_agent.core.types.tool import Permission, ToolCall, ToolResult, ToolSchema


class MemorySearchTool:
    def __init__(self, memory_port: MemoryPort | None, *, timeout_s: float = 0.5) -> None:
        self.schema = ToolSchema(
            name="memory_search",
            description=(
                "Search the user's long-term memory when the user explicitly asks what "
                "you remember, asks you to retrieve a past preference/fact, or needs "
                "memory evidence beyond the automatic context. Do not use for ordinary "
                "conversation."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language memory search query.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of memory records to return. Defaults to 5.",
                    },
                    "scope": {
                        "type": "string",
                        "description": "Memory scope: all, semantic, episodic, promise, or working.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            permissions=frozenset({Permission.MEMORY_READ, Permission.USER_DATA}),
            timeout_s=timeout_s,
        )
        self._memory = memory_port

    async def invoke(self, call: ToolCall, *, ctx: ToolInvocationContext) -> ToolResult:
        if self._memory is None:
            return _unavailable(call.id, self.schema.name)
        query = str(call.arguments.get("query") or "").strip()
        if not query:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="invalid_memory_query",
                error_message="query is required",
            )
        top_k = _bounded_int(call.arguments.get("top_k"), default=5, minimum=1, maximum=20)
        scope = _scope(call.arguments.get("scope"))
        hits = await self._memory.search(
            ctx.turn_context.owner_id,
            query,
            companion_id=ctx.turn_context.companion_id,
            memory_realm_id=ctx.turn_context.memory_realm_id,
            device_id=ctx.turn_context.device_id,
            top_k=top_k,
            scope=scope,
            voice=ctx.input_modality == "voice",
            timeout_s=self.schema.timeout_s,
            session_id=ctx.session_id or "default",
        )
        return ToolResult(
            call_id=call.id,
            name=self.schema.name,
            ok=True,
            content={"records": [_hit_to_dict(hit) for hit in hits], "count": len(hits)},
        )


class MemoryAssertFactTool:
    def __init__(self, memory_port: MemoryPort | None, *, timeout_s: float = 2.0) -> None:
        self.schema = ToolSchema(
            name="memory_assert_fact",
            description=(
                "Store a verbatim claim only when the CURRENT REQUEST explicitly asks "
                "you to remember it. That explicit request is the write authorization, "
                "including for sensitive claims; do not ask for a second confirmation. "
                "Copy it exactly from the current message. Only result status applied "
                "means remembered; accepted or retrying means the write is still processing."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "claim": {
                        "type": "string",
                        "description": (
                            "Exact contiguous text copied from inside the current user "
                            "message; do not paraphrase or fill values from history."
                        ),
                    },
                },
                "required": ["claim"],
                "additionalProperties": False,
            },
            permissions=frozenset({Permission.MEMORY_WRITE, Permission.USER_DATA}),
            side_effect=True,
            timeout_s=timeout_s,
            idempotency_key_template="${owner_id}:${companion_id}:memory_assert_fact:${claim}",
        )
        self._memory = memory_port

    async def invoke(self, call: ToolCall, *, ctx: ToolInvocationContext) -> ToolResult:
        if self._memory is None:
            return _unavailable(call.id, self.schema.name)
        claim = str(call.arguments.get("claim") or "").strip()
        current_request = str(ctx.user_text or "").strip()
        if not claim:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="invalid_memory_claim",
                error_message="claim is required",
            )
        if not current_request or claim == current_request or claim not in current_request:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="ungrounded_memory_claim",
                error_message=(
                    "claim must be exact text inside the current request; "
                    "background and retrieved memory cannot authorize a write"
                ),
            )
        outcome = await self._memory.write_confirmed_fact(
            ctx.turn_context.owner_id,
            ctx.turn_context.companion_id,
            ctx.turn_context.memory_realm_id,
            ctx.turn_context.device_id,
            ctx.session_id,
            claim,
            source_event_id=ctx.turn_id,
            tool_call_id=call.id,
            confidence=0.99,
            tags=["memory_assert_fact", "verbatim", "current_request_grounded"],
            wait_applied_seconds=0.75,
        )
        if outcome.status in {"failed", "unknown"}:
            return _write_failure(call, self.schema.name, outcome)
        return ToolResult(
            call_id=call.id,
            name=self.schema.name,
            ok=True,
            content={
                "status": outcome.status,
                "request_id": outcome.request_id,
                "resource_id": outcome.resource_id,
                "kind": "confirmed_fact",
                "text": claim,
                "confidence": 0.99,
            },
        )


class MemoryForgetTool:
    def __init__(self, memory_port: MemoryPort | None, *, timeout_s: float = 0.5) -> None:
        self.schema = ToolSchema(
            name="memory_forget",
            description=(
                "Archive memories matching a user-provided query so they no longer "
                "participate in recall. Use only when the user explicitly asks to forget "
                "remembered information. Irreversible deletion is handled by the explicit "
                "preview/confirmation privacy flow, not this LLM tool. If the result status "
                "is accepted, say it is still processing; only applied means completed."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The memory content or topic the user wants forgotten.",
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            permissions=frozenset({Permission.MEMORY_WRITE, Permission.USER_DATA}),
            side_effect=True,
            timeout_s=timeout_s,
            idempotency_key_template="${owner_id}:${companion_id}:memory_forget:${query}",
        )
        self._memory = memory_port

    async def invoke(self, call: ToolCall, *, ctx: ToolInvocationContext) -> ToolResult:
        if self._memory is None:
            return _unavailable(call.id, self.schema.name)
        query = str(call.arguments.get("query") or "").strip()
        if not query:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="invalid_memory_query",
                error_message="query is required",
            )
        preview = await self._memory.preview_forget(
            ctx.turn_context.owner_id,
            ctx.turn_context.companion_id,
            ctx.turn_context.memory_realm_id,
            ctx.turn_context.device_id,
            query,
            action="archive",
            session_id=ctx.session_id or "default",
        )
        if preview.status != "preview" or not preview.confirmation_token:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code=f"memory_forget_{preview.status}",
                error_message=preview.error or "memory forget preview did not resolve",
                content={
                    "status": preview.status,
                    "candidate_count": len(preview.candidates),
                    "query": query,
                },
            )
        outcome = await self._memory.confirm_forget(
            ctx.turn_context.owner_id,
            ctx.turn_context.companion_id,
            ctx.turn_context.memory_realm_id,
            ctx.turn_context.device_id,
            preview.confirmation_token,
            session_id=ctx.session_id or "default",
        )
        return ToolResult(
            call_id=call.id,
            name=self.schema.name,
            ok=outcome.status in {"accepted", "applied"},
            error_code=None if outcome.status in {"accepted", "applied"} else "memory_forget_failed",
            error_message=outcome.error or None,
            content={
                "status": outcome.status,
                "request_id": outcome.request_id,
                "affected": len(outcome.drawer_ids),
                "query": query,
            },
        )


def _unavailable(call_id: str, name: str) -> ToolResult:
    return ToolResult(
        call_id=call_id,
        name=name,
        ok=False,
        error_code="memory_port_unavailable",
        error_message="MemoryPort not wired",
    )


def _write_failure(
    call: ToolCall,
    name: str,
    outcome: MemoryWriteOutcome,
) -> ToolResult:
    return ToolResult(
        call_id=call.id,
        name=name,
        ok=False,
        error_code=f"memory_write_{outcome.status}",
        error_message=outcome.error or "memory write completion could not be verified",
        content={
            "status": outcome.status,
            "request_id": outcome.request_id,
            "resource_id": outcome.resource_id,
        },
    )


def _scope(value: object) -> MemoryScope:
    raw = str(value or MemoryScope.ALL.value).strip().lower()
    try:
        return MemoryScope(raw)
    except ValueError:
        return MemoryScope.ALL


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _hit_to_dict(hit: MemoryHit) -> dict:
    return {
        "id": hit.id,
        "content": hit.content,
        "kind": hit.kind.value,
        "similarity": hit.similarity,
        "memory_time": _dt(hit.memory_time),
        "memory_time_source": hit.memory_time_source,
        "valid_from": _dt(hit.valid_from),
        "valid_to": _dt(hit.valid_to),
        "metadata": hit.metadata,
    }


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
