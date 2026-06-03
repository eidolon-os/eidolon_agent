"""SQLite-backed callables injected into domain services."""

from __future__ import annotations

import uuid
from datetime import datetime

from eidolon_agent.core.types import ChatMessage, MessageRole
from eidolon_agent.core.types.turn import TriageKind, TurnInput, TurnResult, TurnStatus
from eidolon_agent.infra.persistence.unit_of_work import SqlAlchemyUnitOfWork


def build_history_hydrator(session_factory):
    async def _hydrate(*, conversation_id: str, window: int) -> list[ChatMessage]:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            return await uow.chat_messages.list_for_conversation(
                conversation_id,
                limit=window,
            )

    return _hydrate


def build_turn_persister(session_factory, *, model_id_provider):
    async def _persist(
        *,
        ti: TurnInput,
        status: TurnStatus,
        triage_kind: TriageKind,
        started_at: datetime,
        finished_at: datetime,
        first_delta_ms: int | None,
        total_ms: int,
        usage_in: int,
        usage_out: int,
        error_code: str | None,
        timings: dict,
        user_text: str = "",
        assistant_text: str = "",
        is_private: bool = False,
    ) -> None:
        model_id = model_id_provider()
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            await uow.conversations.ensure_started(
                conversation_id=ti.conversation_id,
                tenant_id=ti.caller.tenant_id,
                user_id=ti.caller.user_id,
                agent_instance_id=ti.caller.agent_instance_id or "",
            )
            seq_in_conv = await uow.conversations.count_turns(ti.conversation_id)
            await uow.conversations.record_turn(
                TurnResult(
                    turn_id=ti.turn_id,
                    conversation_id=ti.conversation_id,
                    status=status,
                    triage_kind=triage_kind,
                    trigger=ti.trigger,
                    seq_count=0,
                    started_at=started_at,
                    finished_at=finished_at,
                    latency_first_delta_ms=first_delta_ms,
                    total_latency_ms=total_ms,
                    tokens_in=usage_in,
                    tokens_out=usage_out,
                    model=model_id,
                    error_code=error_code,
                    seq_in_conversation=seq_in_conv,
                    metadata=timings,
                )
            )
            if user_text:
                await uow.chat_messages.append(
                    ti.turn_id,
                    ChatMessage(
                        id=uuid.uuid4().hex,
                        role=MessageRole.USER,
                        content=user_text,
                        created_at=started_at,
                        metadata={"is_private": is_private} if is_private else {},
                    ),
                )
            if assistant_text:
                await uow.chat_messages.append(
                    ti.turn_id,
                    ChatMessage(
                        id=uuid.uuid4().hex,
                        role=MessageRole.ASSISTANT,
                        content=assistant_text,
                        tokens=usage_out or None,
                        model=model_id,
                        created_at=finished_at,
                        metadata={"is_private": is_private} if is_private else {},
                    ),
                )
            await uow.commit()

    return _persist


__all__ = ["build_history_hydrator", "build_turn_persister"]
