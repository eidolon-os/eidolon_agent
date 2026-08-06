"""Admin: read-only browse over the local conversations / turns / messages.

Phase 34.A — exposes the SQLite turn log so the admin UI can show
"what did this user actually talk to the agent about?" Pure reads; no
mutation endpoints in this router (the chat path is the only writer).

Endpoints:
  GET  /api/admin/conversations/turns
       List turns newest-first. Filterable by owner_id / companion_id;
       cursor-paginated via ``before`` (an ISO timestamp; we hand
       back ``next_before`` so the UI doesn't need to know the cursor
       column).

  GET  /api/admin/conversations/turns/{turn_id}
       One turn with all its chat messages (user / assistant / tool
       calls) ordered by created_at. Returns 404 if the turn doesn't
       exist.

Two practical notes:
  - ``owner_id`` is the data ownership boundary and ``companion_id`` is the
    runtime agent boundary.
  - We deliberately don't expose message bodies on the list endpoint;
    that would balloon a page-of-50 to many MB once realistic
    conversations exist. The detail endpoint is the only place that
    returns raw message text.
"""

# ruff: noqa: B008

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from eidolon_agent.infra.observability import build_turn_observability_summary
from eidolon_agent.infra.persistence import AgentConversationReader

router = APIRouter()


# ── schemas ────────────────────────────────────────────────────────────────


class TurnSummary(BaseModel):
    """List-row shape. Cheap columns only — no message bodies here."""

    turn_id: str
    trace_id: str | None = None
    conversation_id: str
    seq: int
    owner_id: str
    companion_id: str
    memory_realm_id: str | None
    genome_id: str | None
    genome_hash: str | None
    trigger: str
    input_modality: str | None
    runtime_session_id: str | None
    device_id: str | None
    started_at: datetime
    finished_at: datetime | None
    status: str
    triage_kind: str | None
    latency_first_delta_ms: int | None
    total_latency_ms: int | None
    tokens_in: int
    tokens_out: int
    model: str | None
    error_code: str | None
    observability_summary: dict[str, Any] | None = Field(default=None)


class ListTurnsResponse(BaseModel):
    turns: list[TurnSummary]
    # Cursor for the next page (oldest started_at in the current page);
    # null when ``len(turns) < limit`` i.e. last page reached.
    next_before: datetime | None = None


class ConversationSummary(BaseModel):
    conversation_id: str
    owner_id: str
    companion_id: str
    runtime_session_id: str | None = None
    device_id: str | None = None
    title: str | None = None
    status: str
    started_at: datetime
    updated_at: datetime
    ended_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ListConversationsResponse(BaseModel):
    conversations: list[ConversationSummary]
    next_before: datetime | None = None


class MemoryAuditRow(BaseModel):
    turn_id: str
    conversation_id: str
    seq: int
    owner_id: str
    companion_id: str
    started_at: datetime
    trace_kind: str | None = None
    durable_result: str | None = None
    disposition: str | None
    reason: str | None
    policy_version: str | None
    fanout_allowed: bool
    skipped_reason: str | None
    privacy_mode: str | None
    fanout_publish_state: str | None = None
    fanout_subject: str | None = None
    fanout_error: str | None = None
    fanout_recorded_at: str | None = None


class MemoryAuditResponse(BaseModel):
    rows: list[MemoryAuditRow]
    next_before: datetime | None = None


class ChatMessageView(BaseModel):
    id: str
    role: str
    content: str
    content_type: str
    tokens: int | None
    model: str | None
    tool_call_id: str | None
    tool_name: str | None
    tool_arguments: dict[str, Any] | None
    created_at: datetime


class TurnDetail(BaseModel):
    """Detail-view shape: turn-level columns + the full message list."""

    turn_id: str
    conversation_id: str
    conversation_title: str | None
    seq: int
    owner_id: str
    companion_id: str
    memory_realm_id: str | None
    genome_id: str | None
    genome_hash: str | None
    trigger: str
    input_modality: str | None
    runtime_session_id: str | None
    device_id: str | None
    started_at: datetime
    finished_at: datetime | None
    status: str
    triage_kind: str | None
    latency_first_delta_ms: int | None
    total_latency_ms: int | None
    tokens_in: int
    tokens_out: int
    cost_usd_micro: int
    model: str | None
    trace_id: str | None
    error_code: str | None
    metadata: dict[str, Any] | None = Field(default=None)
    turn_trace: dict[str, Any] | None = Field(default=None)
    observability_summary: dict[str, Any] | None = Field(default=None)
    messages: list[ChatMessageView]


# ── endpoints ──────────────────────────────────────────────────────────────


def _runtime_reader(request: Request) -> AgentConversationReader:
    runtime_store = getattr(request.app.state, "runtime_store", None)
    if runtime_store is None:
        raise HTTPException(503, "runtime_store not configured on admin app")
    return AgentConversationReader(runtime_store)


@router.get("/conversations", response_model=ListConversationsResponse)
async def list_conversations(
    request: Request,
    owner_id: str | None = Query(default=None),
    companion_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    before: datetime | None = Query(default=None),
) -> ListConversationsResponse:
    """Page of Agent-owned conversations for control-plane composition."""
    rows = await _runtime_reader(request).list_conversations(
        owner_id=owner_id,
        companion_id=companion_id,
        limit=limit,
        before=before,
    )
    conversations = [
        ConversationSummary(
            conversation_id=row.conversation_id,
            owner_id=row.owner_id,
            companion_id=row.companion_id,
            runtime_session_id=row.runtime_session_id,
            device_id=row.source_device_id,
            title=row.title,
            status=row.status,
            started_at=row.started_at,
            updated_at=row.updated_at,
            ended_at=row.ended_at,
            metadata=dict(row.metadata_json or {}),
        )
        for row in rows
    ]
    return ListConversationsResponse(
        conversations=conversations,
        next_before=conversations[-1].updated_at if len(conversations) == limit else None,
    )


@router.get("/conversations/turns", response_model=ListTurnsResponse)
async def list_turns(
    request: Request,
    owner_id: str | None = Query(default=None),
    companion_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    before: datetime | None = Query(
        default=None,
        description="ISO timestamp; only return turns started strictly before this",
    ),
) -> ListTurnsResponse:
    """Page of turns, newest-first. Pure read; no side-effects."""
    reader = _runtime_reader(request)
    rows = await reader.list_turns_by_owner(
        owner_id=owner_id,
        companion_id=companion_id,
        limit=limit,
        before=before,
    )
    turns = [
        TurnSummary(
            turn_id=r["id"],
            trace_id=r.get("trace_id"),
            conversation_id=r["conversation_id"],
            seq=r["seq"],
            owner_id=r["owner_id"],
            companion_id=r["companion_id"],
            memory_realm_id=r["memory_realm_id"],
            genome_id=r["genome_id"],
            genome_hash=r.get("genome_hash"),
            trigger=r["trigger"],
            input_modality=r["input_modality"],
            runtime_session_id=r["runtime_session_id"],
            device_id=r["device_id"],
            started_at=r["started_at"],
            finished_at=r["finished_at"],
            status=r["status"],
            triage_kind=r["triage_kind"],
            latency_first_delta_ms=r["latency_first_delta_ms"],
            total_latency_ms=r["total_latency_ms"],
            tokens_in=r["tokens_in"],
            tokens_out=r["tokens_out"],
            model=r["model"],
            error_code=r["error_code"],
            observability_summary=build_turn_observability_summary(
                r.get("metadata_"),
                latency_first_delta_ms=r["latency_first_delta_ms"],
                total_latency_ms=r["total_latency_ms"],
            ),
        )
        for r in rows
    ]
    # Cursor for the next page is the oldest started_at we just returned;
    # null when this page wasn't full (means we hit the tail).
    next_before = turns[-1].started_at if len(turns) == limit else None
    return ListTurnsResponse(turns=turns, next_before=next_before)


@router.get("/conversations/memory-audit", response_model=MemoryAuditResponse)
async def list_memory_audit(
    request: Request,
    owner_id: str | None = Query(default=None),
    companion_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    before: datetime | None = Query(default=None),
) -> MemoryAuditResponse:
    """Prompt-safe memory write candidates from local turn traces."""
    reader = _runtime_reader(request)
    rows = await reader.list_turns_by_owner(
        owner_id=owner_id,
        companion_id=companion_id,
        limit=limit,
        before=before,
    )
    out: list[MemoryAuditRow] = []
    for row in rows:
        trace = ((row.get("metadata_") or {}).get("turn_trace") or {})
        write = trace.get("memory_write_trace") or {}
        if not write:
            continue
        out.append(
            MemoryAuditRow(
                turn_id=row["id"],
                conversation_id=row["conversation_id"],
                seq=row["seq"],
                owner_id=row["owner_id"],
                companion_id=row["companion_id"],
                started_at=row["started_at"],
                trace_kind=write.get("trace_kind"),
                durable_result=write.get("durable_result"),
                disposition=write.get("disposition"),
                reason=write.get("reason"),
                policy_version=write.get("policy_version"),
                fanout_allowed=bool(write.get("fanout_allowed")),
                skipped_reason=write.get("skipped_reason"),
                privacy_mode=write.get("privacy_mode"),
                fanout_publish_state=None,
                fanout_subject=None,
                fanout_error=None,
                fanout_recorded_at=None,
            )
        )
    next_before = out[-1].started_at if len(rows) == limit and out else None
    return MemoryAuditResponse(rows=out, next_before=next_before)


@router.get("/conversations/turns/{turn_id}", response_model=TurnDetail)
async def get_turn(turn_id: str, request: Request) -> TurnDetail:
    """One turn + its chat messages."""
    reader = _runtime_reader(request)
    row = await reader.get_turn(turn_id)
    if row is None:
        raise HTTPException(404, f"turn {turn_id!r} not found")
    messages = await reader.list_for_turn(turn_id)

    metadata = row["metadata_"]
    return TurnDetail(
        turn_id=row["id"],
        conversation_id=row["conversation_id"],
        conversation_title=row["conversation_title"],
        seq=row["seq"],
        owner_id=row["owner_id"],
        companion_id=row["companion_id"],
        memory_realm_id=row["memory_realm_id"],
        genome_id=row["genome_id"],
        genome_hash=row.get("genome_hash"),
        trigger=row["trigger"],
        input_modality=row["input_modality"],
        runtime_session_id=row["runtime_session_id"],
        device_id=row["device_id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        triage_kind=row["triage_kind"],
        latency_first_delta_ms=row["latency_first_delta_ms"],
        total_latency_ms=row["total_latency_ms"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        cost_usd_micro=row["cost_usd_micro"],
        model=row["model"],
        trace_id=row["trace_id"],
        error_code=row["error_code"],
        metadata=metadata,
        turn_trace=(metadata or {}).get("turn_trace"),
        observability_summary=build_turn_observability_summary(
            metadata,
            latency_first_delta_ms=row["latency_first_delta_ms"],
            total_latency_ms=row["total_latency_ms"],
        ),
        messages=[
            ChatMessageView(
                id=m.id,
                role=m.role.value,
                content=m.content,
                content_type=m.content_type,
                tokens=m.tokens,
                model=m.model,
                tool_call_id=m.tool_call_id,
                tool_name=m.tool_name,
                tool_arguments=m.tool_arguments,
                created_at=m.created_at,
            )
            for m in messages
        ],
    )
