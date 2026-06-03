"""Admin: read-only browse over the local conversations / turns / messages.

Phase 34.A — exposes the SQLite turn log so the admin UI can show
"what did this user actually talk to the agent about?" Pure reads; no
mutation endpoints in this router (the chat path is the only writer).

Endpoints:
  GET  /api/admin/conversations/turns
       List turns newest-first. Filterable by user_id / tenant_id;
       cursor-paginated via ``before`` (an ISO timestamp; we hand
       back ``next_before`` so the UI doesn't need to know the cursor
       column).

  GET  /api/admin/conversations/turns/{turn_id}
       One turn with all its chat messages (user / assistant / tool
       calls) ordered by created_at. Returns 404 if the turn doesn't
       exist.

Two practical notes:
  - ``user_id`` here is admin's canonical user_id (the same key in
    memory / hub / channel JWT) — so the operator can filter using
    the same dropdown they use elsewhere in the UI.
  - We deliberately don't expose message bodies on the list endpoint;
    that would balloon a page-of-50 to many MB once realistic
    conversations exist. The detail endpoint is the only place that
    returns raw message text.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from eidolon_agent.infra.persistence.repositories import (
    SqlChatMessageRepository,
    SqlConversationRepository,
)

router = APIRouter()


# ── schemas ────────────────────────────────────────────────────────────────


class TurnSummary(BaseModel):
    """List-row shape. Cheap columns only — no message bodies here."""

    turn_id: str
    conversation_id: str
    seq: int
    tenant_id: str
    user_id: str
    agent_instance_id: str
    trigger: str
    caller_kind: str | None
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


class ListTurnsResponse(BaseModel):
    turns: list[TurnSummary]
    # Cursor for the next page (oldest started_at in the current page);
    # null when ``len(turns) < limit`` i.e. last page reached.
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
    tenant_id: str
    user_id: str
    agent_instance_id: str
    trigger: str
    caller_kind: str | None
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
    messages: list[ChatMessageView]


# ── endpoints ──────────────────────────────────────────────────────────────


def _session_factory(request: Request):
    """Pull the SQLAlchemy session factory off app.state.

    Wired in :func:`eidolon_agent.app.admin.build_admin_app`. We resolve
    it per-request rather than via Depends because every other admin
    router uses the ``app.state`` convention; switching here would
    diverge for no benefit."""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(503, "session_factory not configured on admin app")
    return factory


@router.get("/conversations/turns", response_model=ListTurnsResponse)
async def list_turns(
    request: Request,
    user_id: str | None = Query(default=None, description="Filter by admin user_id"),
    tenant_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    before: datetime | None = Query(
        default=None,
        description="ISO timestamp; only return turns started strictly before this",
    ),
) -> ListTurnsResponse:
    """Page of turns, newest-first. Pure read; no side-effects."""
    factory = _session_factory(request)
    async with factory() as session:
        repo = SqlConversationRepository(session)
        rows = await repo.list_turns_by_user(
            user_id=user_id,
            tenant_id=tenant_id,
            limit=limit,
            before=before,
        )
    turns = [
        TurnSummary(
            turn_id=r["id"],
            conversation_id=r["conversation_id"],
            seq=r["seq"],
            tenant_id=r["tenant_id"],
            user_id=r["user_id"],
            agent_instance_id=r["agent_instance_id"],
            trigger=r["trigger"],
            caller_kind=r["caller_kind"],
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
        )
        for r in rows
    ]
    # Cursor for the next page is the oldest started_at we just returned;
    # null when this page wasn't full (means we hit the tail).
    next_before = turns[-1].started_at if len(turns) == limit else None
    return ListTurnsResponse(turns=turns, next_before=next_before)


@router.get("/conversations/turns/{turn_id}", response_model=TurnDetail)
async def get_turn(turn_id: str, request: Request) -> TurnDetail:
    """One turn + its chat messages."""
    factory = _session_factory(request)
    async with factory() as session:
        convo_repo = SqlConversationRepository(session)
        msg_repo = SqlChatMessageRepository(session)
        row = await convo_repo.get_turn(turn_id)
        if row is None:
            raise HTTPException(404, f"turn {turn_id!r} not found")
        messages = await msg_repo.list_for_turn(turn_id)

    return TurnDetail(
        turn_id=row["id"],
        conversation_id=row["conversation_id"],
        conversation_title=row["conversation_title"],
        seq=row["seq"],
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        agent_instance_id=row["agent_instance_id"],
        trigger=row["trigger"],
        caller_kind=row["caller_kind"],
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
        metadata=row["metadata_"],
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
