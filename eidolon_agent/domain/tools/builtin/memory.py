"""Memory tools exposed to the LLM through ToolRegistry."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime

from eidolon_agent.core.ports.memory import MemoryPort
from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.memory import MemoryHit, MemoryScope, MemoryWriteOutcome
from eidolon_agent.core.types.tool import Permission, ToolCall, ToolResult, ToolSchema

_SENSITIVE_MEMORY_MARKERS = (
    "身份证",
    "银行卡",
    "password",
    "密码",
    "详细地址",
    "具体住址",
    "家庭住址",
    "手机号",
    "电话号码",
    "邮箱",
    "确诊",
    "疾病",
    "用药",
    "过敏",
    "收入",
    "工资",
)
_AFFIRMATIVE_MARKERS = ("好", "好的", "可以", "同意", "确认", "记下来", "记住")
_NEGATIVE_PREFIXES = ("不", "别", "不要", "取消", "算了")


@dataclass(frozen=True, slots=True)
class PendingMemoryCandidate:
    candidate_id: str
    claims: tuple[str, ...]
    expires_at: float


class PendingMemoryCandidateStore:
    """Session-bound proof for a later consent turn."""

    def __init__(self, *, ttl_seconds: float = 300.0) -> None:
        self._ttl_seconds = max(30.0, ttl_seconds)
        self._items: dict[tuple[str, str, str], PendingMemoryCandidate] = {}

    def stage(
        self,
        *,
        key: tuple[str, str, str],
        claims: list[str],
    ) -> PendingMemoryCandidate:
        material = "\x1f".join((*key, *claims))
        candidate = PendingMemoryCandidate(
            candidate_id=hashlib.sha256(material.encode("utf-8")).hexdigest()[:24],
            claims=tuple(claims),
            expires_at=time.monotonic() + self._ttl_seconds,
        )
        self._items[key] = candidate
        return candidate

    def current(self, key: tuple[str, str, str]) -> PendingMemoryCandidate | None:
        candidate = self._items.get(key)
        if candidate is not None and candidate.expires_at <= time.monotonic():
            self._items.pop(key, None)
            return None
        return candidate

    def discard(self, key: tuple[str, str, str]) -> None:
        self._items.pop(key, None)


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
            ctx.caller.owner_id,
            query,
            companion_id=ctx.caller.companion_id,
            memory_realm_id=ctx.caller.memory_realm_id,
            device_id=ctx.caller.device_id,
            top_k=top_k,
            scope=scope,
            voice=ctx.caller.caller_kind.value == "livekit_voice",
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
                "Store a non-sensitive verbatim claim only when the CURRENT REQUEST "
                "explicitly asks you to remember it. Copy it exactly from the current "
                "message. Only result status applied means remembered; accepted or "
                "retrying means the write is still processing."
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
        if _is_sensitive_memory(claim):
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="memory_requires_consent",
                error_message=(
                    "sensitive memory requires memory_stage_candidate and a separate "
                    "affirmative user turn"
                ),
            )
        outcome = await self._memory.write_confirmed_fact(
            ctx.caller.owner_id,
            ctx.caller.companion_id,
            ctx.caller.memory_realm_id,
            ctx.caller.device_id,
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


class MemoryStageCandidateTool:
    """Stage sensitive or ambiguous claims for a separate consent turn."""

    def __init__(
        self,
        candidates: PendingMemoryCandidateStore,
        *,
        timeout_s: float = 0.5,
    ) -> None:
        self.schema = ToolSchema(
            name="memory_stage_candidate",
            description=(
                "Stage exact sensitive or ambiguous claims from the CURRENT REQUEST "
                "before asking for separate consent. Never use this for ordinary "
                "non-sensitive profile facts; those are handled automatically."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "claims": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 5,
                        "description": (
                            "Exact contiguous claims copied from the current user message."
                        ),
                    }
                },
                "required": ["claims"],
                "additionalProperties": False,
            },
            permissions=frozenset({Permission.MEMORY_WRITE, Permission.USER_DATA}),
            side_effect=True,
            timeout_s=timeout_s,
        )
        self._candidates = candidates

    async def invoke(self, call: ToolCall, *, ctx: ToolInvocationContext) -> ToolResult:
        current_request = str(ctx.user_text or "").strip()
        claims = [
            str(value).strip()
            for value in (call.arguments.get("claims") or [])
            if str(value).strip()
        ]
        if (
            not current_request
            or not claims
            or any(claim not in current_request for claim in claims)
        ):
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="ungrounded_memory_candidate",
                error_message="every staged claim must be exact text inside the current request",
            )
        candidate = self._candidates.stage(key=_candidate_key(ctx), claims=claims)
        return ToolResult(
            call_id=call.id,
            name=self.schema.name,
            ok=True,
            content={
                "status": "pending_consent",
                "candidate_id": candidate.candidate_id,
                "claim_count": len(candidate.claims),
                "expires_in_seconds": int(
                    max(0.0, candidate.expires_at - time.monotonic())
                ),
            },
        )


class MemoryConfirmPendingTool:
    """Confirm a staged candidate without reconstructing conversation history."""

    def __init__(
        self,
        memory_port: MemoryPort | None,
        candidates: PendingMemoryCandidateStore,
        *,
        timeout_s: float = 5.0,
    ) -> None:
        self.schema = ToolSchema(
            name="memory_confirm_pending",
            description=(
                "Confirm this session's staged memory candidate only when the CURRENT "
                "REQUEST is an explicit affirmative response to the prior consent "
                "question. Takes no claim and never reconstructs history."
            ),
            json_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            permissions=frozenset({Permission.MEMORY_WRITE, Permission.USER_DATA}),
            side_effect=True,
            timeout_s=timeout_s,
        )
        self._memory = memory_port
        self._candidates = candidates

    async def invoke(self, call: ToolCall, *, ctx: ToolInvocationContext) -> ToolResult:
        if self._memory is None:
            return _unavailable(call.id, self.schema.name)
        user_text = str(ctx.user_text or "").strip()
        if not _is_affirmative_confirmation(user_text):
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="memory_confirmation_required",
                error_message="the current request is not an explicit affirmative response",
            )
        key = _candidate_key(ctx)
        candidate = self._candidates.current(key)
        if candidate is None:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="no_pending_memory_candidate",
                error_message="there is no unexpired memory candidate in this session",
            )
        outcomes = await asyncio.gather(
            *(
                self._memory.write_confirmed_fact(
                    ctx.caller.owner_id,
                    ctx.caller.companion_id,
                    ctx.caller.memory_realm_id,
                    ctx.caller.device_id,
                    ctx.session_id,
                    claim,
                    source_event_id=f"memory-candidate:{candidate.candidate_id}",
                    tool_call_id=f"candidate:{candidate.candidate_id}:{index}",
                    confidence=0.99,
                    tags=[
                        "memory_confirm_pending",
                        "separate_consent",
                        f"candidate:{candidate.candidate_id}",
                    ],
                    wait_applied_seconds=0.75,
                )
                for index, claim in enumerate(candidate.claims)
            )
        )
        failed = next(
            (
                outcome
                for outcome in outcomes
                if outcome.status in {"failed", "unknown"}
            ),
            None,
        )
        if failed is not None:
            return _write_failure(call, self.schema.name, failed)
        self._candidates.discard(key)
        status = (
            "applied"
            if all(outcome.status == "applied" for outcome in outcomes)
            else "accepted"
        )
        return ToolResult(
            call_id=call.id,
            name=self.schema.name,
            ok=True,
            content={
                "status": status,
                "candidate_id": candidate.candidate_id,
                "claims": list(candidate.claims),
                "request_ids": [outcome.request_id for outcome in outcomes],
                "resource_ids": [outcome.resource_id for outcome in outcomes],
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
            ctx.caller.owner_id,
            ctx.caller.companion_id,
            ctx.caller.memory_realm_id,
            ctx.caller.device_id,
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
            ctx.caller.owner_id,
            ctx.caller.companion_id,
            ctx.caller.memory_realm_id,
            ctx.caller.device_id,
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


def _candidate_key(ctx: ToolInvocationContext) -> tuple[str, str, str]:
    return (
        ctx.caller.memory_realm_id,
        ctx.caller.companion_id,
        ctx.session_id or "default",
    )


def _is_sensitive_memory(text: str) -> bool:
    normalized = text.casefold()
    return any(marker.casefold() in normalized for marker in _SENSITIVE_MEMORY_MARKERS)


def _is_affirmative_confirmation(text: str) -> bool:
    normalized = text.strip().lower().strip("，。,.!?！？ ")
    if not normalized or normalized.startswith(_NEGATIVE_PREFIXES):
        return False
    return normalized in _AFFIRMATIVE_MARKERS or any(
        normalized.startswith(prefix)
        for prefix in ("好的", "可以", "同意", "确认", "帮我记", "记下来", "记住")
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
