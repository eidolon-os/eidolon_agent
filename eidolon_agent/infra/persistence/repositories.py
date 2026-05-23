"""Repository implementations.

Translate between domain types (``core.types``) and ORM rows. No business
logic; if you find yourself writing ``if/else`` here, the logic belongs in a
service.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from eidolon_agent.core.errors import NotFoundError
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.turn import TurnResult
from eidolon_agent.domain.personas.types import PersonaEvolutionResult, PersonaInstance
from eidolon_agent.infra.persistence.models import (
    ChatMessageRow,
    ConversationRow,
    DeviceRow,
    EvolutionHistoryRow,
    PersonaInstanceRow,
    TurnRow,
)


def _message_to_row(turn_id: str, m: ChatMessage) -> ChatMessageRow:
    return ChatMessageRow(
        id=m.id,
        turn_id=turn_id,
        role=m.role.value,
        content=m.content,
        content_type=m.content_type,
        tokens=m.tokens,
        model=m.model,
        tool_call_id=m.tool_call_id,
        tool_name=m.tool_name,
        tool_arguments=m.tool_arguments,
        created_at=m.created_at,
        is_private=bool(m.metadata.get("is_private", False)),
    )


def _row_to_message(r: ChatMessageRow) -> ChatMessage:
    return ChatMessage(
        id=r.id,
        role=MessageRole(r.role),
        content=r.content,
        content_type=r.content_type,
        tokens=r.tokens,
        model=r.model,
        tool_call_id=r.tool_call_id,
        tool_name=r.tool_name,
        tool_arguments=r.tool_arguments,
        created_at=r.created_at,
        metadata={"is_private": r.is_private} if r.is_private else {},
    )


class SqlChatMessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, turn_id: str, message: ChatMessage) -> None:
        self._session.add(_message_to_row(turn_id, message))

    async def list_for_turn(self, turn_id: str) -> list[ChatMessage]:
        rows = (
            await self._session.execute(
                select(ChatMessageRow)
                .where(ChatMessageRow.turn_id == turn_id)
                .order_by(ChatMessageRow.created_at)
            )
        ).scalars().all()
        return [_row_to_message(r) for r in rows]

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 200,
        before: datetime | None = None,
    ) -> list[ChatMessage]:
        stmt = (
            select(ChatMessageRow)
            .join(TurnRow, ChatMessageRow.turn_id == TurnRow.id)
            .where(TurnRow.conversation_id == conversation_id)
            .order_by(ChatMessageRow.created_at.desc())
            .limit(limit)
        )
        if before is not None:
            stmt = stmt.where(ChatMessageRow.created_at < before)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_row_to_message(r) for r in reversed(rows)]

    async def delete_for_user(self, user_id: str) -> int:
        # Delete all messages from conversations owned by user. Uses CASCADE.
        result = await self._session.execute(
            delete(ConversationRow).where(ConversationRow.user_id == user_id)
        )
        return result.rowcount or 0


class SqlConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
        agent_instance_id: str,
    ) -> None:
        self._session.add(
            ConversationRow(
                id=conversation_id,
                tenant_id=tenant_id,
                user_id=user_id,
                agent_instance_id=agent_instance_id,
            )
        )

    async def finish(self, conversation_id: str, *, title: str | None = None) -> None:
        row = await self._session.get(ConversationRow, conversation_id)
        if row is None:
            return
        row.ended_at = datetime.now(timezone.utc)
        if title is not None:
            row.title = title

    async def record_turn(self, result: TurnResult) -> None:
        # Idempotent upsert via PK
        row = await self._session.get(TurnRow, result.turn_id)
        if row is None:
            row = TurnRow(
                id=result.turn_id,
                conversation_id=result.conversation_id,
                seq=result.seq_in_conversation,
                trigger=result.trigger.value,
                status=result.status.value,
                started_at=result.started_at,
            )
            self._session.add(row)
        # Update fields
        row.status = result.status.value
        row.triage_kind = result.triage_kind.value
        row.trigger = result.trigger.value
        row.latency_first_delta_ms = result.latency_first_delta_ms
        row.total_latency_ms = result.total_latency_ms
        row.tokens_in = result.tokens_in
        row.tokens_out = result.tokens_out
        row.cost_usd_micro = result.cost_usd_micro
        row.model = result.model
        row.error_code = result.error_code
        if result.started_at:
            row.started_at = result.started_at
        if result.finished_at:
            row.finished_at = result.finished_at


class SqlDeviceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register(
        self,
        *,
        device_id: str,
        tenant_id: str,
        user_id: str,
        token_hash: str,
        scopes: list[str],
    ) -> None:
        self._session.add(
            DeviceRow(
                id=device_id,
                tenant_id=tenant_id,
                user_id=user_id,
                token_hash=token_hash,
                scopes={"scopes": scopes},
            )
        )

    async def revoke(self, device_id: str) -> None:
        row = await self._session.get(DeviceRow, device_id)
        if row is not None and row.revoked_at is None:
            row.revoked_at = datetime.now(timezone.utc)

    async def is_revoked(self, device_id: str) -> bool:
        row = await self._session.get(DeviceRow, device_id)
        return row is None or row.revoked_at is not None

    async def touch_last_seen(self, device_id: str) -> None:
        row = await self._session.get(DeviceRow, device_id)
        if row is not None:
            row.last_seen_at = datetime.now(timezone.utc)


class SqlEvolutionHistoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, result: PersonaEvolutionResult) -> None:
        now = datetime.now(timezone.utc)
        self._session.add(
            EvolutionHistoryRow(
                id=uuid.uuid4().hex,
                instance_id=result.instance_id,
                from_overlay_version=0,
                to_overlay_version=0,
                proposed_by="personas",
                rationale=result.rationale,
                delta=json.loads(result.model_dump_json()),
                requires_human_approval=False,
                approved_by=None,
                applied_at=now if result.applied else None,
                rolled_back_at=None,
                git_commit=None,
            )
        )

    async def list_for_instance(
        self, instance_id: str, *, limit: int = 50
    ) -> list[PersonaEvolutionResult]:
        rows = (
            await self._session.execute(
                select(EvolutionHistoryRow)
                .where(EvolutionHistoryRow.instance_id == instance_id)
                .order_by(EvolutionHistoryRow.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        return [_row_to_evolution(r) for r in rows]

    async def get(self, delta_id: str) -> PersonaEvolutionResult | None:
        row = await self._session.get(EvolutionHistoryRow, delta_id)
        return _row_to_evolution(row) if row is not None else None


def _row_to_evolution(r: EvolutionHistoryRow) -> PersonaEvolutionResult:
    if isinstance(r.delta, dict) and "instance_id" in r.delta:
        return PersonaEvolutionResult.model_validate(r.delta)
    return PersonaEvolutionResult(
        instance_id=r.instance_id,
        applied=r.applied_at is not None,
        rationale=r.rationale,
    )


class SqlPersonaInstanceRepository:
    """CRUD for the ``persona_instances`` table.

    Translates between the domain ``PersonaInstance`` dataclass and the
    ``PersonaInstanceRow`` ORM. The full overlay is stored as JSON; columns
    next to the blob are indexed/denormalised for admin list queries.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, tenant_id: str, user_id: str, instance_id: str
    ) -> PersonaInstance | None:
        row = await self._session.get(PersonaInstanceRow, instance_id)
        if row is None or row.tenant_id != tenant_id or row.user_id != user_id:
            return None
        return _row_to_persona_instance(row)

    async def load(
        self, tenant_id: str, user_id: str, instance_id: str
    ) -> PersonaInstance:
        instance = await self.get(tenant_id, user_id, instance_id)
        if instance is None:
            raise NotFoundError(
                f"persona instance not found: {tenant_id}/{user_id}/{instance_id}"
            )
        return instance

    async def upsert(
        self, instance: PersonaInstance, *, mark_active: bool = True
    ) -> None:
        now = datetime.now(timezone.utc)
        row = await self._session.get(PersonaInstanceRow, instance.instance_id)
        overlay = instance.model_dump(mode="json")
        if row is None:
            self._session.add(
                PersonaInstanceRow(
                    id=instance.instance_id,
                    tenant_id=instance.tenant_id,
                    user_id=instance.user_id,
                    template_id=instance.origin_template_id,
                    template_version=instance.origin_template_revision,
                    overlay_version=instance.overlay_version,
                    overlay_json=overlay,
                    created_at=instance.created_at,
                    updated_at=now,
                    last_active_at=now if mark_active else None,
                )
            )
            return
        row.template_id = instance.origin_template_id
        row.template_version = instance.origin_template_revision
        row.overlay_version = instance.overlay_version
        row.overlay_json = overlay
        row.updated_at = now
        if mark_active:
            row.last_active_at = now

    async def delete(self, tenant_id: str, user_id: str, instance_id: str) -> None:
        await self._session.execute(
            delete(PersonaInstanceRow).where(
                PersonaInstanceRow.id == instance_id,
                PersonaInstanceRow.tenant_id == tenant_id,
                PersonaInstanceRow.user_id == user_id,
            )
        )

    async def list_all(
        self, *, limit: int = 500, offset: int = 0
    ) -> list[PersonaInstance]:
        rows = (
            await self._session.execute(
                select(PersonaInstanceRow)
                .order_by(PersonaInstanceRow.last_active_at.desc().nulls_last())
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
        return [_row_to_persona_instance(r) for r in rows]

    async def touch_last_active(self, instance_id: str) -> None:
        row = await self._session.get(PersonaInstanceRow, instance_id)
        if row is not None:
            row.last_active_at = datetime.now(timezone.utc)


def _row_to_persona_instance(row: PersonaInstanceRow) -> PersonaInstance:
    return PersonaInstance.model_validate(row.overlay_json)


__all__ = [
    "SqlChatMessageRepository",
    "SqlConversationRepository",
    "SqlDeviceRepository",
    "SqlEvolutionHistoryRepository",
    "SqlPersonaInstanceRepository",
]
