"""Memory tools exposed to the LLM through ToolRegistry."""

from __future__ import annotations

from datetime import datetime

from eidolon_agent.core.ports.memory import MemoryPort
from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.memory import MemoryHit, MemoryScope
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


def _unavailable(call_id: str, name: str) -> ToolResult:
    return ToolResult(
        call_id=call_id,
        name=name,
        ok=False,
        error_code="memory_port_unavailable",
        error_message="MemoryPort not wired",
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
