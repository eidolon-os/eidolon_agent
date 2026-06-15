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
from eidolon_agent.core.types.long_task import (
    CallbackStatus,
    LongTaskRecord,
    LongTaskStatus,
)
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.turn import TurnResult
from eidolon_agent.domain.personas.types import (
    PersonaEvolutionProposal,
    PersonaEvolutionResult,
    PersonaInstance,
    PersonaObservation,
    PersonaProposalPatch,
)
from eidolon_agent.infra.persistence.models import (
    ChatMessageRow,
    ConversationRow,
    DeviceRow,
    EvolutionHistoryRow,
    LongTaskRow,
    PersonaEvolutionProposalRow,
    PersonaInstanceRow,
    PersonaObservationRow,
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
            (
                await self._session.execute(
                    select(ChatMessageRow)
                    .where(ChatMessageRow.turn_id == turn_id)
                    .order_by(ChatMessageRow.created_at)
                )
            )
            .scalars()
            .all()
        )
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
        if result.metadata is not None:
            row.metadata_ = result.metadata
        if result.started_at:
            row.started_at = result.started_at
        if result.finished_at:
            row.finished_at = result.finished_at

    async def ensure_started(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
        agent_instance_id: str,
    ) -> None:
        """Idempotent ``start``: no-op if the conversation row already exists."""
        existing = await self._session.get(ConversationRow, conversation_id)
        if existing is not None:
            return
        await self.start(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_instance_id=agent_instance_id,
        )

    async def list_turns_by_user(
        self,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[dict]:
        """Browse turns for the admin "conversations" view.

        Joins ``turns`` to ``conversations`` so we can filter by
        ``tenant_id`` / ``user_id`` (those live on the conversation,
        not the turn). Newest-first; ``before`` lets the UI page
        backwards through history (cursor on ``started_at``). Returns a
        list of dicts to avoid leaking ORM rows past the repository
        boundary; the router/schema layer composes the response.

        We intentionally DON'T fetch messages here — the master list
        only needs cheap turn-level columns. Use ``get_turn_with_
        messages`` for detail view.
        """
        stmt = (
            select(
                TurnRow.id,
                TurnRow.conversation_id,
                TurnRow.seq,
                TurnRow.trigger,
                TurnRow.caller_kind,
                TurnRow.device_id,
                TurnRow.started_at,
                TurnRow.finished_at,
                TurnRow.status,
                TurnRow.triage_kind,
                TurnRow.latency_first_delta_ms,
                TurnRow.total_latency_ms,
                TurnRow.tokens_in,
                TurnRow.tokens_out,
                TurnRow.model,
                TurnRow.error_code,
                TurnRow.metadata_,
                ConversationRow.tenant_id,
                ConversationRow.user_id,
                ConversationRow.agent_instance_id,
            )
            .join(ConversationRow, TurnRow.conversation_id == ConversationRow.id)
            .order_by(TurnRow.started_at.desc())
            .limit(limit)
        )
        if user_id is not None:
            stmt = stmt.where(ConversationRow.user_id == user_id)
        if tenant_id is not None:
            stmt = stmt.where(ConversationRow.tenant_id == tenant_id)
        if before is not None:
            stmt = stmt.where(TurnRow.started_at < before)
        rows = (await self._session.execute(stmt)).mappings().all()
        return [dict(r) for r in rows]

    async def get_turn(self, turn_id: str) -> dict | None:
        """Fetch one turn joined with its conversation context.

        Returns None when the turn doesn't exist. Used by the admin
        conversations detail endpoint together with
        :meth:`SqlChatMessageRepository.list_for_turn`.
        """
        stmt = (
            select(
                TurnRow.id,
                TurnRow.conversation_id,
                TurnRow.seq,
                TurnRow.trigger,
                TurnRow.caller_kind,
                TurnRow.device_id,
                TurnRow.started_at,
                TurnRow.finished_at,
                TurnRow.status,
                TurnRow.triage_kind,
                TurnRow.latency_first_delta_ms,
                TurnRow.total_latency_ms,
                TurnRow.tokens_in,
                TurnRow.tokens_out,
                TurnRow.cost_usd_micro,
                TurnRow.model,
                TurnRow.trace_id,
                TurnRow.error_code,
                TurnRow.metadata_,
                ConversationRow.tenant_id,
                ConversationRow.user_id,
                ConversationRow.agent_instance_id,
                ConversationRow.title.label("conversation_title"),
            )
            .join(ConversationRow, TurnRow.conversation_id == ConversationRow.id)
            .where(TurnRow.id == turn_id)
        )
        row = (await self._session.execute(stmt)).mappings().first()
        return dict(row) if row else None

    async def count_turns(self, conversation_id: str) -> int:
        """Number of turns already recorded for a conversation.

        Used to assign ``seq_in_conversation`` (the table has a UNIQUE
        constraint on ``(conversation_id, seq)``).
        """
        from sqlalchemy import func, select

        result = await self._session.execute(
            select(func.count())
            .select_from(TurnRow)
            .where(TurnRow.conversation_id == conversation_id)
        )
        return int(result.scalar_one())


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


class SqlLongTaskRepository:
    """Persistence for async coworker tasks.

    The repository is deliberately provider-aware but not provider-coupled:
    mementos is the first provider, while callback/runner code can update the
    same row through task id, mementos ids, or session key later.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, record: LongTaskRecord) -> None:
        now = datetime.now(timezone.utc)
        self._session.add(_long_task_to_row(record, now=now))

    async def get(self, task_id: str) -> LongTaskRecord | None:
        row = await self._session.get(LongTaskRow, task_id)
        return _row_to_long_task(row) if row is not None else None

    async def list_by_user(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int = 50,
    ) -> list[LongTaskRecord]:
        rows = (
            (
                await self._session.execute(
                    select(LongTaskRow)
                    .where(
                        LongTaskRow.tenant_id == tenant_id,
                        LongTaskRow.user_id == user_id,
                    )
                    .order_by(LongTaskRow.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_row_to_long_task(row) for row in rows]

    async def list_for_admin(
        self,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        status: str | None = None,
        provider: str | None = None,
        task_type: str | None = None,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[LongTaskRecord]:
        stmt = select(LongTaskRow)
        if tenant_id:
            stmt = stmt.where(LongTaskRow.tenant_id == tenant_id)
        if user_id:
            stmt = stmt.where(LongTaskRow.user_id == user_id)
        if status:
            stmt = stmt.where(LongTaskRow.status == status)
        if provider:
            stmt = stmt.where(LongTaskRow.provider == provider)
        if task_type:
            stmt = stmt.where(LongTaskRow.task_type == task_type)
        if before is not None:
            stmt = stmt.where(LongTaskRow.created_at < before)
        rows = (
            (await self._session.execute(stmt.order_by(LongTaskRow.created_at.desc()).limit(limit)))
            .scalars()
            .all()
        )
        return [_row_to_long_task(row) for row in rows]

    async def find_latest_mementos_session(
        self,
        session_key: str,
    ) -> str | None:
        row = (
            await self._session.execute(
                select(LongTaskRow)
                .where(
                    LongTaskRow.session_key == session_key,
                    LongTaskRow.mementos_session_id.is_not(None),
                )
                .order_by(LongTaskRow.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return row.mementos_session_id if row is not None else None

    async def update_status(
        self,
        task_id: str,
        status: LongTaskStatus,
        *,
        progress_summary: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        error_payload: dict | None = None,
    ) -> LongTaskRecord | None:
        row = await self._session.get(LongTaskRow, task_id)
        if row is None:
            return None
        now = datetime.now(timezone.utc)
        row.status = status.value
        row.updated_at = now
        if status is LongTaskStatus.SUBMITTED:
            row.submitted_at = row.submitted_at or now
        if status is LongTaskStatus.QUEUED:
            row.submitted_at = row.submitted_at or now
        if status is LongTaskStatus.RUNNING:
            row.started_at = row.started_at or now
            row.last_progress_at = now
        if status in {
            LongTaskStatus.SUCCEEDED,
            LongTaskStatus.FAILED,
            LongTaskStatus.CANCELLED,
            LongTaskStatus.TIMED_OUT,
        }:
            row.completed_at = row.completed_at or now
            row.external_status = status.value
        if progress_summary is not None:
            row.progress_summary = progress_summary
        if error_code is not None:
            row.error_code = error_code
        if error_message is not None:
            row.error_message = error_message
        if error_payload is not None:
            row.error_payload = error_payload
        return _row_to_long_task(row)

    async def mark_worker_claimed(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_until: datetime | None = None,
    ) -> LongTaskRecord | None:
        row = await self._session.get(LongTaskRow, task_id)
        if row is None:
            return None
        now = datetime.now(timezone.utc)
        row.status = LongTaskStatus.QUEUED.value
        row.worker_id = worker_id
        row.lease_until = lease_until
        row.attempt_count = (row.attempt_count or 0) + 1
        row.submitted_at = row.submitted_at or now
        row.updated_at = now
        return _row_to_long_task(row)

    async def attach_mementos_run(
        self,
        task_id: str,
        *,
        mementos_session_id: str | None = None,
        mementos_conversation_id: str | None = None,
        mementos_run_id: str | None = None,
        latest_seq: int | None = None,
        workspace_dir: str | None = None,
        external_status: str | None = None,
    ) -> LongTaskRecord | None:
        row = await self._session.get(LongTaskRow, task_id)
        if row is None:
            return None
        now = datetime.now(timezone.utc)
        row.status = LongTaskStatus.RUNNING.value
        row.started_at = row.started_at or now
        row.last_progress_at = now
        row.updated_at = now
        if mementos_session_id is not None:
            row.mementos_session_id = mementos_session_id
        if mementos_conversation_id is not None:
            row.mementos_conversation_id = mementos_conversation_id
        if mementos_run_id is not None:
            row.mementos_run_id = mementos_run_id
        if latest_seq is not None:
            row.mementos_latest_seq = latest_seq
        if workspace_dir is not None:
            row.mementos_workspace_dir = workspace_dir
        if external_status is not None:
            row.external_status = external_status
        return _row_to_long_task(row)

    async def append_progress(
        self,
        task_id: str,
        event: dict,
        *,
        summary: str | None = None,
        latest_seq: int | None = None,
    ) -> LongTaskRecord | None:
        row = await self._session.get(LongTaskRow, task_id)
        if row is None:
            return None
        now = datetime.now(timezone.utc)
        row.status = LongTaskStatus.RUNNING.value
        row.started_at = row.started_at or now
        row.last_progress_at = now
        row.last_polled_at = now
        row.updated_at = now
        events = list(row.progress_events or [])
        events.append(event)
        row.progress_events = events
        if summary is not None:
            row.progress_summary = summary
        if latest_seq is not None:
            row.mementos_latest_seq = latest_seq
        return _row_to_long_task(row)

    async def complete(
        self,
        task_id: str,
        *,
        result_text: str | None = None,
        result_payload: dict | None = None,
        artifact_paths: list[str] | None = None,
    ) -> LongTaskRecord | None:
        row = await self._session.get(LongTaskRow, task_id)
        if row is None:
            return None
        now = datetime.now(timezone.utc)
        row.status = LongTaskStatus.SUCCEEDED.value
        row.external_status = LongTaskStatus.SUCCEEDED.value
        row.result_text = result_text
        row.result_payload = result_payload
        row.artifact_paths = artifact_paths or []
        row.completed_at = now
        row.last_polled_at = now
        row.updated_at = now
        return _row_to_long_task(row)

    async def touch_poll(
        self,
        task_id: str,
        *,
        latest_seq: int | None = None,
        external_status: str | None = None,
    ) -> LongTaskRecord | None:
        row = await self._session.get(LongTaskRow, task_id)
        if row is None:
            return None
        now = datetime.now(timezone.utc)
        row.last_polled_at = now
        row.updated_at = now
        if latest_seq is not None:
            row.mementos_latest_seq = latest_seq
        if external_status is not None:
            row.external_status = external_status
        return _row_to_long_task(row)

    async def fail(
        self,
        task_id: str,
        *,
        error_code: str,
        error_message: str,
        error_payload: dict | None = None,
        status: LongTaskStatus = LongTaskStatus.FAILED,
    ) -> LongTaskRecord | None:
        return await self.update_status(
            task_id,
            status,
            error_code=error_code,
            error_message=error_message,
            error_payload=error_payload,
        )


def _long_task_to_row(record: LongTaskRecord, *, now: datetime) -> LongTaskRow:
    return LongTaskRow(
        id=record.id,
        provider=record.provider,
        status=record.status.value,
        tenant_id=record.tenant_id,
        user_id=record.user_id,
        agent_instance_id=record.agent_instance_id,
        conversation_id=record.conversation_id,
        turn_id=record.turn_id,
        session_id=record.session_id,
        trace_id=record.trace_id,
        tool_call_id=record.tool_call_id,
        session_key=record.session_key,
        task_date=record.task_date,
        task_key=record.task_key,
        task=record.task,
        user_text=record.user_text,
        task_type=record.task_type,
        urgency=record.urgency,
        expected_output=record.expected_output,
        context_summary=record.context_summary,
        attachments=record.attachments,
        request_payload=record.request_payload,
        mementos_session_id=record.mementos_session_id,
        mementos_conversation_id=record.mementos_conversation_id,
        mementos_run_id=record.mementos_run_id,
        mementos_latest_seq=record.mementos_latest_seq,
        mementos_workspace_dir=record.mementos_workspace_dir,
        progress_summary=record.progress_summary,
        progress_events=record.progress_events,
        result_text=record.result_text,
        result_payload=record.result_payload,
        artifact_paths=record.artifact_paths,
        error_code=record.error_code,
        error_message=record.error_message,
        error_payload=record.error_payload,
        callback_subject=record.callback_subject,
        callback_status=record.callback_status.value,
        callback_attempts=record.callback_attempts,
        callback_last_error=record.callback_last_error,
        callback_delivered_at=record.callback_delivered_at,
        worker_id=record.worker_id,
        lease_until=record.lease_until,
        attempt_count=record.attempt_count,
        next_retry_at=record.next_retry_at,
        external_status=record.external_status,
        created_at=record.created_at or now,
        updated_at=record.updated_at or now,
        started_at=record.started_at,
        submitted_at=record.submitted_at,
        last_progress_at=record.last_progress_at,
        last_polled_at=record.last_polled_at,
        completed_at=record.completed_at,
    )


def _row_to_long_task(row: LongTaskRow) -> LongTaskRecord:
    return LongTaskRecord(
        id=row.id,
        provider=row.provider,
        status=LongTaskStatus(row.status),
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        agent_instance_id=row.agent_instance_id,
        conversation_id=row.conversation_id,
        turn_id=row.turn_id,
        session_id=row.session_id,
        trace_id=row.trace_id,
        tool_call_id=row.tool_call_id,
        session_key=row.session_key,
        task_date=row.task_date,
        task_key=row.task_key,
        task=row.task,
        user_text=row.user_text or "",
        task_type=row.task_type,
        urgency=row.urgency,
        expected_output=row.expected_output or "",
        context_summary=row.context_summary or "",
        attachments=list(row.attachments or []),
        request_payload=dict(row.request_payload or {}),
        mementos_session_id=row.mementos_session_id,
        mementos_conversation_id=row.mementos_conversation_id,
        mementos_run_id=row.mementos_run_id,
        mementos_latest_seq=row.mementos_latest_seq,
        mementos_workspace_dir=row.mementos_workspace_dir,
        progress_summary=row.progress_summary,
        progress_events=list(row.progress_events or []),
        result_text=row.result_text,
        result_payload=row.result_payload,
        artifact_paths=list(row.artifact_paths or []),
        error_code=row.error_code,
        error_message=row.error_message,
        error_payload=row.error_payload,
        callback_subject=row.callback_subject,
        callback_status=CallbackStatus(row.callback_status),
        callback_attempts=row.callback_attempts,
        callback_last_error=row.callback_last_error,
        callback_delivered_at=row.callback_delivered_at,
        worker_id=row.worker_id,
        lease_until=row.lease_until,
        attempt_count=row.attempt_count,
        next_retry_at=row.next_retry_at,
        external_status=row.external_status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        submitted_at=row.submitted_at,
        last_progress_at=row.last_progress_at,
        last_polled_at=row.last_polled_at,
        completed_at=row.completed_at,
    )


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
            (
                await self._session.execute(
                    select(EvolutionHistoryRow)
                    .where(EvolutionHistoryRow.instance_id == instance_id)
                    .order_by(EvolutionHistoryRow.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
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


class SqlPersonaObservationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, observation: PersonaObservation) -> None:
        self._session.add(_observation_to_row(observation))

    async def list_for_instance(
        self,
        instance_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[PersonaObservation]:
        stmt = (
            select(PersonaObservationRow)
            .where(PersonaObservationRow.instance_id == instance_id)
            .order_by(PersonaObservationRow.created_at.desc())
            .limit(limit)
        )
        if status is not None:
            stmt = stmt.where(PersonaObservationRow.status == status)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_row_to_observation(row) for row in rows]

    async def get(self, observation_id: str) -> PersonaObservation | None:
        row = await self._session.get(PersonaObservationRow, observation_id)
        return _row_to_observation(row) if row is not None else None

    async def set_status(self, observation_id: str, status: str) -> None:
        row = await self._session.get(PersonaObservationRow, observation_id)
        if row is not None:
            row.status = status


class SqlPersonaEvolutionProposalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, proposal: PersonaEvolutionProposal) -> None:
        self._session.add(_proposal_to_row(proposal))

    async def save(self, proposal: PersonaEvolutionProposal) -> None:
        row = await self._session.get(PersonaEvolutionProposalRow, proposal.id)
        if row is None:
            self._session.add(_proposal_to_row(proposal))
            return
        row.status = proposal.status
        row.patches = [patch.model_dump(mode="json") for patch in proposal.patches]
        row.confidence = proposal.confidence
        row.rationale = proposal.rationale
        row.evidence_ids = list(proposal.evidence_ids)
        row.updated_at = proposal.updated_at
        row.decided_by = proposal.decided_by
        row.decided_at = proposal.decided_at
        row.decision_reason = proposal.decision_reason

    async def list_for_instance(
        self,
        instance_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[PersonaEvolutionProposal]:
        stmt = (
            select(PersonaEvolutionProposalRow)
            .where(PersonaEvolutionProposalRow.instance_id == instance_id)
            .order_by(PersonaEvolutionProposalRow.created_at.desc())
            .limit(limit)
        )
        if status is not None:
            stmt = stmt.where(PersonaEvolutionProposalRow.status == status)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_row_to_proposal(row) for row in rows]

    async def get(self, proposal_id: str) -> PersonaEvolutionProposal | None:
        row = await self._session.get(PersonaEvolutionProposalRow, proposal_id)
        return _row_to_proposal(row) if row is not None else None


class SqlPersonaInstanceRepository:
    """CRUD for the ``persona_instances`` table.

    Translates between the domain ``PersonaInstance`` dataclass and the
    ``PersonaInstanceRow`` ORM. The full overlay is stored as JSON; columns
    next to the blob are indexed/denormalised for admin list queries.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, tenant_id: str, user_id: str, instance_id: str) -> PersonaInstance | None:
        row = await self._session.get(PersonaInstanceRow, instance_id)
        if row is None or row.tenant_id != tenant_id or row.user_id != user_id:
            return None
        return _row_to_persona_instance(row)

    async def load(self, tenant_id: str, user_id: str, instance_id: str) -> PersonaInstance:
        instance = await self.get(tenant_id, user_id, instance_id)
        if instance is None:
            raise NotFoundError(f"persona instance not found: {tenant_id}/{user_id}/{instance_id}")
        return instance

    async def upsert(self, instance: PersonaInstance, *, mark_active: bool = True) -> None:
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

    async def list_all(self, *, limit: int = 500, offset: int = 0) -> list[PersonaInstance]:
        rows = (
            (
                await self._session.execute(
                    select(PersonaInstanceRow)
                    .order_by(PersonaInstanceRow.last_active_at.desc().nulls_last())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return [_row_to_persona_instance(r) for r in rows]

    async def touch_last_active(self, instance_id: str) -> None:
        row = await self._session.get(PersonaInstanceRow, instance_id)
        if row is not None:
            row.last_active_at = datetime.now(timezone.utc)


def _row_to_persona_instance(row: PersonaInstanceRow) -> PersonaInstance:
    return PersonaInstance.model_validate(row.overlay_json)


def _observation_to_row(observation: PersonaObservation) -> PersonaObservationRow:
    return PersonaObservationRow(
        id=observation.id,
        tenant_id=observation.tenant_id,
        user_id=observation.user_id,
        instance_id=observation.instance_id,
        kind=observation.kind,
        source=observation.source,
        status=observation.status,
        strength=observation.strength,
        confidence=observation.confidence,
        summary=observation.summary,
        evidence=observation.evidence,
        memory_ids=list(observation.memory_ids),
        created_at=observation.created_at,
    )


def _row_to_observation(row: PersonaObservationRow) -> PersonaObservation:
    return PersonaObservation(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        instance_id=row.instance_id,
        kind=row.kind,
        source=row.source,
        status=row.status,
        strength=row.strength,
        confidence=row.confidence,
        summary=row.summary or "",
        evidence=dict(row.evidence or {}),
        memory_ids=tuple(row.memory_ids or ()),
        created_at=row.created_at,
    )


def _proposal_to_row(proposal: PersonaEvolutionProposal) -> PersonaEvolutionProposalRow:
    return PersonaEvolutionProposalRow(
        id=proposal.id,
        tenant_id=proposal.tenant_id,
        user_id=proposal.user_id,
        instance_id=proposal.instance_id,
        status=proposal.status,
        patches=[patch.model_dump(mode="json") for patch in proposal.patches],
        confidence=proposal.confidence,
        rationale=proposal.rationale,
        evidence_ids=list(proposal.evidence_ids),
        created_at=proposal.created_at,
        updated_at=proposal.updated_at,
        decided_by=proposal.decided_by,
        decided_at=proposal.decided_at,
        decision_reason=proposal.decision_reason,
    )


def _row_to_proposal(row: PersonaEvolutionProposalRow) -> PersonaEvolutionProposal:
    return PersonaEvolutionProposal(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        instance_id=row.instance_id,
        status=row.status,
        patches=tuple(PersonaProposalPatch.model_validate(patch) for patch in (row.patches or ())),
        confidence=row.confidence,
        rationale=row.rationale or "",
        evidence_ids=tuple(row.evidence_ids or ()),
        created_at=row.created_at,
        updated_at=row.updated_at,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        decision_reason=row.decision_reason,
    )


__all__ = [
    "SqlChatMessageRepository",
    "SqlConversationRepository",
    "SqlDeviceRepository",
    "SqlEvolutionHistoryRepository",
    "SqlLongTaskRepository",
    "SqlPersonaEvolutionProposalRepository",
    "SqlPersonaInstanceRepository",
    "SqlPersonaObservationRepository",
]
