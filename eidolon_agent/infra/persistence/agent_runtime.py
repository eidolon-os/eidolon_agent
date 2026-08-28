"""Agent runtime persistence backed by the Agent-owned SQLite authority."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from eidolon_agent.core.types import ChatMessage, MessageRole
from eidolon_agent.core.types.long_task import CallbackStatus, LongTaskRecord, LongTaskStatus
from eidolon_agent.core.types.turn import TriageKind, TurnInput, TurnResult, TurnStatus
from eidolon_agent.infra.persistence.runtime_store import (
    AgentAuditOutbox,
    AgentRuntimeStore,
    ConversationRow,
    JobRow,
    MessageRow,
    RuntimeSessionRow,
    TurnRow,
)

_LONG_TASK_PAYLOAD_KEY = "eidolon_agent_long_task"

# Only terminal asynchronous outcomes are global receipts. Queueing, polling,
# progress, and RUNNING transitions remain local runtime state/telemetry.
_JOB_STATUS_EVENT = {
    LongTaskStatus.SUCCEEDED: "job.succeeded",
    LongTaskStatus.FAILED: "job.failed",
    LongTaskStatus.CANCELLED: "job.cancelled",
    LongTaskStatus.TIMED_OUT: "job.timed_out",
}


def _emit_job_transition(session, *, before: LongTaskStatus, after: LongTaskRecord) -> None:
    """Emit a terminal receipt in the same Agent authority transaction."""
    event_type = _JOB_STATUS_EVENT.get(after.status)
    if event_type is None or after.status == before:
        return
    payload = {"provider": after.provider, "kind": after.task_type, "status": after.status.value}
    if after.error_code:
        payload["error_code"] = after.error_code
    session.add(
        AgentAuditOutbox.build_row(
            category="receipt",
            owner_id=after.owner_id,
            subject_type="job",
            subject_id=after.id,
            action=event_type,
            trace_id=getattr(after, "trace_id", None),
            outcome="failure"
            if after.status
            in {
                LongTaskStatus.FAILED,
                LongTaskStatus.CANCELLED,
                LongTaskStatus.TIMED_OUT,
            }
            else "success",
            reason=after.error_code,
            payload=payload,
        )
    )


def build_agent_history_hydrator(runtime_store: AgentRuntimeStore):
    async def _hydrate(*, conversation_id: str, window: int) -> list[ChatMessage]:
        async with runtime_store.session_factory() as session:
            pairs = (
                await session.execute(
                    select(MessageRow, TurnRow.seq)
                    .join(TurnRow, MessageRow.turn_id == TurnRow.turn_id)
                    .where(TurnRow.conversation_id == conversation_id)
                    .order_by(TurnRow.seq.desc(), MessageRow.seq.desc())
                    .limit(window)
                )
            ).all()
            ordered = sorted(
                pairs,
                key=lambda pair: (
                    pair[1],
                    pair[0].created_at,
                    _message_seq_in_turn(pair[0]),
                    pair[0].message_id,
                ),
            )
            return [_row_to_message(row) for row, _seq in ordered]

    return _hydrate


def build_agent_turn_persister(runtime_store: AgentRuntimeStore, *, model_id_provider):
    async def _persist_once(
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
        companion_id = ti.context.companion_id
        runtime_session_id = _runtime_session_id(ti)
        async with runtime_store.session_factory() as session:
            await _upsert_runtime_session(
                session,
                ti,
                runtime_session_id=runtime_session_id,
            )
            await _insert_ignore(
                session,
                ConversationRow,
                {
                    "conversation_id": ti.conversation_id,
                    "owner_id": ti.context.owner_id,
                    "companion_id": companion_id,
                    "runtime_session_id": runtime_session_id,
                    "source_device_id": ti.context.device_id,
                    "started_at": started_at,
                    "updated_at": finished_at,
                    "metadata_json": {
                        "owner_id": ti.context.owner_id,
                        "companion_id": companion_id,
                        "runtime_session_id": runtime_session_id,
                        "memory_realm_id": ti.context.memory_realm_id,
                        "genome_id": ti.context.genome_id,
                        "genome_hash": ti.context.genome_hash,
                        "schema_version": ti.context.schema_version,
                        "realizer_version": ti.context.realizer_version,
                        "session_id": ti.session_id,
                    },
                },
                index_elements=["conversation_id"],
            )
            conversation = await session.get(ConversationRow, ti.conversation_id)
            if conversation is None:
                raise RuntimeError(f"conversation insert failed: {ti.conversation_id}")
            conversation.updated_at = finished_at
            conversation.runtime_session_id = runtime_session_id
            conversation.source_device_id = ti.context.device_id

            existing_turn = await session.get(TurnRow, ti.turn_id)
            if existing_turn is None:
                seq = await _next_turn_seq(session, ti.conversation_id)
                turn = TurnRow(
                    turn_id=ti.turn_id,
                    conversation_id=ti.conversation_id,
                    seq=seq,
                    runtime_session_id=runtime_session_id,
                    source_device_id=ti.context.device_id,
                    trigger=ti.trigger.value,
                    status=status.value,
                    started_at=started_at,
                    finished_at=finished_at,
                    trace_id=ti.context.trace_id or None,
                    trace_json=_trace_json(timings),
                    metrics_json=_turn_metrics_json(
                        result=TurnResult(
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
                            seq_in_conversation=seq,
                            device_id=ti.context.device_id,
                            input_modality=ti.input_modality,
                            metadata=timings,
                        )
                    ),
                    metadata_json=_turn_metadata_json(ti, triage_kind, timings),
                )
                session.add(turn)
            else:
                existing_turn.trigger = ti.trigger.value
                existing_turn.status = status.value
                existing_turn.started_at = started_at
                existing_turn.finished_at = finished_at
                existing_turn.runtime_session_id = runtime_session_id
                existing_turn.source_device_id = ti.context.device_id
                existing_turn.trace_id = ti.context.trace_id or None
                existing_turn.trace_json = _trace_json(timings)
                existing_turn.metrics_json = {
                    **(existing_turn.metrics_json or {}),
                    "latency_first_delta_ms": first_delta_ms,
                    "total_latency_ms": total_ms,
                    "tokens_in": usage_in,
                    "tokens_out": usage_out,
                    "model": model_id,
                    "error_code": error_code,
                }
                existing_turn.metadata_json = _turn_metadata_json(ti, triage_kind, timings)
            if user_text:
                await _upsert_message_row(
                    session,
                    turn_id=ti.turn_id,
                    seq=0,
                    message=ChatMessage(
                        id=_turn_message_id(ti.turn_id, 0),
                        role=MessageRole.USER,
                        content=user_text,
                        created_at=started_at,
                        metadata=_message_metadata(
                            turn_id=ti.turn_id,
                            seq_in_turn=0,
                            is_private=is_private,
                        ),
                    ),
                )
            if assistant_text:
                await _upsert_message_row(
                    session,
                    turn_id=ti.turn_id,
                    seq=1,
                    message=ChatMessage(
                        id=_turn_message_id(ti.turn_id, 1),
                        role=MessageRole.ASSISTANT,
                        content=assistant_text,
                        tokens=usage_out or None,
                        model=model_id,
                        created_at=finished_at,
                        metadata=_message_metadata(
                            turn_id=ti.turn_id,
                            seq_in_turn=1,
                            is_private=is_private,
                        ),
                    ),
                )
            await session.commit()

    async def _persist(**kwargs) -> None:
        for attempt in range(5):
            try:
                await _persist_once(**kwargs)
                return
            except IntegrityError:
                if attempt >= 4:
                    raise
                await asyncio.sleep(0)

    return _persist


class AgentLongTaskStore:
    """Long-task store backed by the Agent authority's ``jobs`` table."""

    def __init__(self, runtime_store: AgentRuntimeStore) -> None:
        self._runtime_store = runtime_store

    async def accept(self, record: LongTaskRecord) -> None:
        now = datetime.now(timezone.utc)
        record = replace(
            record, created_at=record.created_at or now, updated_at=record.updated_at or now
        )
        async with self._runtime_store.session_factory() as session:
            row = await session.get(JobRow, record.id)
            if row is None:
                session.add(_record_to_job_row(record))
            else:
                _apply_record_to_job_row(row, record)
            await session.commit()

    async def create(self, record: LongTaskRecord) -> None:
        await self.accept(record)

    async def get(self, task_id: str) -> LongTaskRecord | None:
        async with self._runtime_store.session_factory() as session:
            row = await session.get(JobRow, task_id)
            return _job_row_to_record(row) if row is not None else None

    async def list_for_admin(
        self,
        *,
        owner_id: str | None = None,
        companion_id: str | None = None,
        status: str | None = None,
        provider: str | None = None,
        task_type: str | None = None,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[LongTaskRecord]:
        async with self._runtime_store.session_factory() as session:
            stmt = select(JobRow).order_by(JobRow.created_at.desc()).limit(limit)
            if owner_id:
                stmt = stmt.where(JobRow.owner_id == owner_id)
            if companion_id:
                stmt = stmt.where(JobRow.companion_id == companion_id)
            if status:
                stmt = stmt.where(JobRow.status == status)
            if provider:
                stmt = stmt.where(JobRow.provider == provider)
            if task_type:
                stmt = stmt.where(JobRow.kind == task_type)
            if before is not None:
                stmt = stmt.where(JobRow.created_at < before)
            rows = (await session.execute(stmt)).scalars().all()
            return [_job_row_to_record(row) for row in rows]

    async def mark_queued(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_until: datetime | None = None,
    ) -> LongTaskRecord | None:
        return await self._update(
            task_id,
            lambda record, now: replace(
                record,
                status=LongTaskStatus.QUEUED,
                worker_id=worker_id,
                lease_until=lease_until,
                attempt_count=(record.attempt_count or 0) + 1,
                submitted_at=record.submitted_at or now,
                updated_at=now,
            ),
        )

    async def mark_submitted(self, task_id: str) -> LongTaskRecord | None:
        return await self._update(
            task_id,
            lambda record, now: replace(
                record,
                status=LongTaskStatus.SUBMITTED,
                submitted_at=record.submitted_at or now,
                updated_at=now,
            ),
        )

    async def find_mementos_session_id(self, session_key: str) -> str | None:
        async with self._runtime_store.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(JobRow)
                        .where(JobRow.provider == "mementos")
                        .order_by(JobRow.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                record = _job_row_to_record(row)
                if record.session_key == session_key and record.mementos_session_id:
                    return record.mementos_session_id
            return None

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
        return await self._update(
            task_id,
            lambda record, now: replace(
                record,
                status=LongTaskStatus.RUNNING,
                started_at=record.started_at or now,
                last_progress_at=now,
                updated_at=now,
                mementos_session_id=mementos_session_id or record.mementos_session_id,
                mementos_conversation_id=mementos_conversation_id
                or record.mementos_conversation_id,
                mementos_run_id=mementos_run_id or record.mementos_run_id,
                mementos_latest_seq=latest_seq
                if latest_seq is not None
                else record.mementos_latest_seq,
                mementos_workspace_dir=workspace_dir or record.mementos_workspace_dir,
                external_status=external_status or record.external_status,
            ),
        )

    async def append_progress(
        self,
        task_id: str,
        event: dict,
        *,
        summary: str | None = None,
        latest_seq: int | None = None,
    ) -> LongTaskRecord | None:
        def mutate(record: LongTaskRecord, now: datetime) -> LongTaskRecord:
            events = [*record.progress_events, event]
            return replace(
                record,
                status=LongTaskStatus.RUNNING,
                started_at=record.started_at or now,
                last_progress_at=now,
                last_polled_at=now,
                updated_at=now,
                progress_events=events,
                progress_summary=summary if summary is not None else record.progress_summary,
                mementos_latest_seq=latest_seq
                if latest_seq is not None
                else record.mementos_latest_seq,
            )

        return await self._update(task_id, mutate)

    async def touch_poll(
        self,
        task_id: str,
        *,
        latest_seq: int | None = None,
        external_status: str | None = None,
    ) -> LongTaskRecord | None:
        return await self._update(
            task_id,
            lambda record, now: replace(
                record,
                last_polled_at=now,
                updated_at=now,
                mementos_latest_seq=latest_seq
                if latest_seq is not None
                else record.mementos_latest_seq,
                external_status=external_status or record.external_status,
            ),
        )

    async def complete(
        self,
        task_id: str,
        *,
        result_text: str | None = None,
        result_payload: dict | None = None,
        artifact_paths: list[str] | None = None,
    ) -> LongTaskRecord | None:
        return await self._update(
            task_id,
            lambda record, now: replace(
                record,
                status=LongTaskStatus.SUCCEEDED,
                external_status=LongTaskStatus.SUCCEEDED.value,
                result_text=result_text,
                result_payload=result_payload,
                artifact_paths=artifact_paths or [],
                completed_at=now,
                last_polled_at=now,
                updated_at=now,
            ),
        )

    async def set_result_tts_summary(self, task_id: str, summary: str) -> LongTaskRecord | None:
        return await self._update(
            task_id,
            lambda record, now: replace(record, result_tts_summary=summary, updated_at=now),
        )

    async def claim_callback_delivery(self, task_id: str, *, subject: str) -> bool:
        async with self._runtime_store.session_factory() as session:
            row = await session.get(JobRow, task_id)
            if row is None:
                return False
            record = _job_row_to_record(row)
            if record.callback_status is not CallbackStatus.PENDING:
                return False
            now = datetime.now(timezone.utc)
            updated = replace(
                record,
                callback_status=CallbackStatus.DELIVERED,
                callback_subject=subject,
                callback_attempts=(record.callback_attempts or 0) + 1,
                callback_delivered_at=now,
                updated_at=now,
            )
            _apply_record_to_job_row(row, updated)
            await session.commit()
            return True

    async def mark_failed(
        self,
        task_id: str,
        *,
        error_code: str,
        error_message: str,
        error_payload: dict | None = None,
        status: LongTaskStatus = LongTaskStatus.FAILED,
    ) -> LongTaskRecord | None:
        return await self._update(
            task_id,
            lambda record, now: replace(
                record,
                status=status,
                error_code=error_code,
                error_message=error_message,
                error_payload=error_payload,
                completed_at=record.completed_at
                or (
                    now
                    if status
                    in {
                        LongTaskStatus.FAILED,
                        LongTaskStatus.CANCELLED,
                        LongTaskStatus.TIMED_OUT,
                    }
                    else None
                ),
                updated_at=now,
            ),
        )

    async def retry_from_admin(self, task_id: str) -> LongTaskRecord | None:
        """Return a terminal task to the accepted queue state.

        Clears the whole of the previous run, not only its error. A record left
        at ``accepted`` still carrying the last attempt's ``result_text`` and
        artifacts is a task that reads as "starting" and answers as "finished" —
        and the answer is the one a person would be shown.

        ``attempt_count`` is deliberately *not* cleared: this is the same task
        being tried again, and how many times it has been tried is the fact a
        retry adds to the record rather than erases from it.
        """

        return await self._update(
            task_id,
            lambda record, now: replace(
                record,
                status=LongTaskStatus.ACCEPTED,
                external_status=None,
                progress_summary=None,
                progress_events=[],
                result_text=None,
                result_tts_summary=None,
                result_payload=None,
                artifact_paths=[],
                error_code=None,
                error_message=None,
                error_payload=None,
                worker_id=None,
                lease_until=None,
                next_retry_at=None,
                completed_at=None,
                updated_at=now,
            ),
        )

    async def _update(self, task_id: str, mutator) -> LongTaskRecord | None:
        async with self._runtime_store.session_factory() as session:
            row = await session.get(JobRow, task_id)
            if row is None:
                return None
            record = _job_row_to_record(row)
            updated = mutator(record, datetime.now(timezone.utc))
            _apply_record_to_job_row(row, updated)
            _emit_job_transition(session, before=record.status, after=updated)
            await session.commit()
            return updated


class AgentConversationReader:
    """Admin/query reader over the Agent authority."""

    def __init__(self, runtime_store: AgentRuntimeStore) -> None:
        self._runtime_store = runtime_store

    async def list_conversations(
        self,
        *,
        owner_id: str | None = None,
        companion_id: str | None = None,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[ConversationRow]:
        async with self._runtime_store.read_session_factory() as session:
            stmt = select(ConversationRow).order_by(
                ConversationRow.updated_at.desc(),
                ConversationRow.conversation_id.desc(),
            )
            if owner_id is not None:
                stmt = stmt.where(ConversationRow.owner_id == owner_id)
            if companion_id is not None:
                stmt = stmt.where(ConversationRow.companion_id == companion_id)
            if before is not None:
                stmt = stmt.where(ConversationRow.updated_at < before)
            rows = await session.scalars(stmt.limit(limit))
            return list(rows)

    async def list_turns_by_owner(
        self,
        *,
        owner_id: str | None = None,
        companion_id: str | None = None,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[dict]:
        async with self._runtime_store.read_session_factory() as session:
            stmt = (
                select(TurnRow, ConversationRow)
                .join(ConversationRow, TurnRow.conversation_id == ConversationRow.conversation_id)
                .order_by(TurnRow.started_at.desc())
                .limit(limit)
            )
            if owner_id is not None:
                stmt = stmt.where(ConversationRow.owner_id == owner_id)
            if companion_id is not None:
                stmt = stmt.where(ConversationRow.companion_id == companion_id)
            if before is not None:
                stmt = stmt.where(TurnRow.started_at < before)
            rows = (await session.execute(stmt)).all()
            return [_turn_row_to_admin_dict(turn, conversation) for turn, conversation in rows]

    async def list_turns_for_conversation(
        self,
        conversation_id: str,
        *,
        owner_id: str,
        companion_id: str,
        limit: int = 20,
        before: datetime | None = None,
    ) -> list[dict] | None:
        """One conversation's turns, newest first, or ``None`` if it is not this
        Owner's and Companion's.

        ``None`` rather than an empty list, because those are different answers:
        a conversation with no turns yet is a real state, and "that is not yours"
        must not be reported as one — an id would then be probeable for
        existence.

        Newest first, matching the cursor the other list uses. A transcript reads
        forward, but "load earlier" is the gesture, so the order a person sees is
        the client's business.
        """

        async with self._runtime_store.read_session_factory() as session:
            conversation = await session.get(ConversationRow, conversation_id)
            if (
                conversation is None
                or conversation.owner_id != owner_id
                or conversation.companion_id != companion_id
            ):
                return None
            stmt = (
                select(TurnRow)
                .where(TurnRow.conversation_id == conversation_id)
                .order_by(TurnRow.started_at.desc())
                .limit(limit)
            )
            if before is not None:
                stmt = stmt.where(TurnRow.started_at < before)
            turns = (await session.execute(stmt)).scalars().all()
            return [_turn_row_to_admin_dict(turn, conversation) for turn in turns]

    async def get_turn(self, turn_id: str) -> dict | None:
        async with self._runtime_store.read_session_factory() as session:
            row = (
                await session.execute(
                    select(TurnRow, ConversationRow)
                    .join(
                        ConversationRow, TurnRow.conversation_id == ConversationRow.conversation_id
                    )
                    .where(TurnRow.turn_id == turn_id)
                )
            ).first()
            if row is None:
                return None
            turn, conversation = row
            return _turn_row_to_admin_dict(turn, conversation)

    async def list_for_turns(self, turn_ids: list[str]) -> dict[str, list[ChatMessage]]:
        """Messages for a page of turns, in one query.

        One query rather than one per turn: a transcript page is twenty turns,
        and twenty round trips to the same SQLite file for something a single
        ``IN`` answers is the kind of cost that only shows up on the Host.
        """

        if not turn_ids:
            return {}
        async with self._runtime_store.read_session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MessageRow)
                        .where(MessageRow.turn_id.in_(turn_ids))
                        .order_by(MessageRow.turn_id, MessageRow.seq)
                    )
                )
                .scalars()
                .all()
            )
        grouped: dict[str, list[ChatMessage]] = {turn_id: [] for turn_id in turn_ids}
        for row in rows:
            grouped.setdefault(row.turn_id, []).append(_row_to_message(row))
        return grouped

    async def list_for_turn(self, turn_id: str) -> list[ChatMessage]:
        async with self._runtime_store.read_session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MessageRow)
                        .where(MessageRow.turn_id == turn_id)
                        .order_by(MessageRow.seq)
                    )
                )
                .scalars()
                .all()
            )
            return [_row_to_message(row) for row in rows]


async def _upsert_runtime_session(
    session,
    ti: TurnInput,
    *,
    runtime_session_id: str,
) -> None:
    row = await session.get(RuntimeSessionRow, runtime_session_id)
    now = datetime.now(timezone.utc)
    if row is None:
        row = RuntimeSessionRow(
            session_id=runtime_session_id,
            owner_id=ti.context.owner_id,
            companion_id=ti.context.companion_id,
            source_device_id=ti.context.device_id,
            started_at=now,
        )
        session.add(row)
    row.owner_id = ti.context.owner_id
    row.companion_id = ti.context.companion_id
    row.source_device_id = ti.context.device_id
    row.transport = "grpc_chat"
    row.status = "active"
    row.metadata_json = {
        "input_modality": ti.input_modality,
        "conversation_id": ti.conversation_id,
        "trace_id": ti.context.trace_id,
        "request_id": ti.context.request_id,
    }
    row.last_seen_at = now
    row.updated_at = now
    await session.flush()


def _runtime_session_id(ti: TurnInput) -> str:
    if ti.session_id:
        return ti.session_id
    raise RuntimeError("runtime_session_id was not built at transport boundary")


async def _insert_ignore(
    session, model, values: dict[str, Any], *, index_elements: list[str]
) -> None:
    """Insert a row if absent without turning first-writer races into failures."""

    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        stmt = (
            sqlite_insert(model)
            .values(**values)
            .on_conflict_do_nothing(index_elements=index_elements)
        )
        await session.execute(stmt)
        return
    if dialect == "postgresql":
        stmt = (
            pg_insert(model).values(**values).on_conflict_do_nothing(index_elements=index_elements)
        )
        await session.execute(stmt)
        return
    if len(index_elements) == 1:
        pk_value = values.get(index_elements[0])
        if pk_value is not None and await session.get(model, pk_value) is None:
            session.add(model(**values))
        return
    session.add(model(**values))


async def _next_turn_seq(session, conversation_id: str) -> int:
    result = await session.execute(
        select(func.count()).select_from(TurnRow).where(TurnRow.conversation_id == conversation_id)
    )
    return int(result.scalar_one())


def _turn_metadata_json(ti: TurnInput, triage_kind: TriageKind, timings: dict) -> dict[str, Any]:
    return {
        **(timings or {}),
        "owner_id": ti.context.owner_id,
        "companion_id": ti.context.companion_id,
        "runtime_session_id": _runtime_session_id(ti),
        "memory_realm_id": ti.context.memory_realm_id,
        "genome_id": ti.context.genome_id,
        "genome_hash": ti.context.genome_hash,
        "schema_version": ti.context.schema_version,
        "realizer_version": ti.context.realizer_version,
        "session_id": ti.session_id,
        "input_modality": ti.input_modality,
        "device_id": ti.context.device_id,
        "triage_kind": triage_kind.value,
        "trace_id": ti.context.trace_id,
        "request_id": ti.context.request_id,
    }


def _trace_json(timings: dict) -> dict[str, Any]:
    return dict((timings or {}).get("turn_trace") or {})


def _turn_metrics_json(result: TurnResult) -> dict[str, Any]:
    return {
        "latency_first_delta_ms": result.latency_first_delta_ms,
        "total_latency_ms": result.total_latency_ms,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "cost_usd_micro": result.cost_usd_micro,
        "model": result.model,
        "error_code": result.error_code,
        "device_id": result.device_id,
        "input_modality": result.input_modality,
        "triage_kind": result.triage_kind.value,
    }


def _message_to_row(turn_id: str, message: ChatMessage, *, seq: int | None = None) -> MessageRow:
    metadata = dict(message.metadata or {})
    metadata.setdefault("turn_id", turn_id)
    if message.tokens is not None:
        metadata["tokens"] = message.tokens
    if message.model is not None:
        metadata["model"] = message.model
    if message.tool_call_id is not None:
        metadata["tool_call_id"] = message.tool_call_id
    if message.tool_name is not None:
        metadata["tool_name"] = message.tool_name
    if message.tool_arguments is not None:
        metadata["tool_arguments"] = message.tool_arguments
    return MessageRow(
        message_id=message.id,
        turn_id=turn_id,
        seq=seq if seq is not None else _seq_from_message_metadata(metadata, message.role.value),
        role=message.role.value,
        content=message.content,
        content_type=message.content_type,
        visibility="private" if metadata.get("is_private") else "normal",
        created_at=message.created_at,
        metadata_json=metadata,
    )


async def _upsert_message_row(session, *, turn_id: str, seq: int, message: ChatMessage) -> None:
    existing = (
        await session.execute(
            select(MessageRow).where(MessageRow.turn_id == turn_id, MessageRow.seq == seq)
        )
    ).scalar_one_or_none()
    row = _message_to_row(turn_id, message, seq=seq)
    if existing is None:
        session.add(row)
        return
    existing.role = row.role
    existing.content = row.content
    existing.content_type = row.content_type
    existing.visibility = row.visibility
    existing.created_at = row.created_at
    existing.metadata_json = row.metadata_json


def _message_metadata(*, turn_id: str, seq_in_turn: int, is_private: bool) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "turn_id": turn_id,
        "seq_in_turn": seq_in_turn,
    }
    if is_private:
        metadata["is_private"] = True
    return metadata


def _turn_message_id(turn_id: str, seq_in_turn: int) -> str:
    return f"msg_{turn_id}_{seq_in_turn}"


def _message_seq_in_turn(row: MessageRow) -> int:
    if isinstance(row.seq, int):
        return row.seq
    metadata = row.metadata_json or {}
    value = metadata.get("seq_in_turn") if isinstance(metadata, dict) else None
    if isinstance(value, int):
        return value
    return {
        MessageRole.USER.value: 0,
        MessageRole.ASSISTANT.value: 1,
        MessageRole.PROACTIVE.value: 1,
        MessageRole.TOOL.value: 2,
        MessageRole.SYSTEM.value: -1,
    }.get(row.role, 99)


def _seq_from_message_metadata(metadata: dict[str, Any], role: str) -> int:
    value = metadata.get("seq_in_turn")
    if isinstance(value, int):
        return value
    return {
        MessageRole.USER.value: 0,
        MessageRole.ASSISTANT.value: 1,
        MessageRole.PROACTIVE.value: 1,
        MessageRole.TOOL.value: 2,
        MessageRole.SYSTEM.value: -1,
    }.get(role, 99)


def _row_to_message(row: MessageRow) -> ChatMessage:
    metadata = dict(row.metadata_json or {})
    if row.visibility == "private":
        metadata["is_private"] = True
    return ChatMessage(
        id=row.message_id,
        role=MessageRole(row.role),
        content=row.content,
        content_type=row.content_type,
        tokens=metadata.get("tokens"),
        model=metadata.get("model"),
        tool_call_id=metadata.get("tool_call_id"),
        tool_name=metadata.get("tool_name"),
        tool_arguments=metadata.get("tool_arguments"),
        created_at=row.created_at,
        metadata=metadata,
    )


def _turn_row_to_admin_dict(turn: TurnRow, conversation: ConversationRow) -> dict[str, Any]:
    metadata = dict(turn.metadata_json or {})
    metrics = dict(turn.metrics_json or {})
    conversation_meta = dict(conversation.metadata_json or {})
    return {
        "id": turn.turn_id,
        "conversation_id": turn.conversation_id,
        "seq": turn.seq,
        "trigger": turn.trigger,
        "input_modality": metrics.get("input_modality") or metadata.get("input_modality"),
        "runtime_session_id": (
            turn.runtime_session_id
            or metadata.get("runtime_session_id")
            or conversation.runtime_session_id
        ),
        "device_id": turn.source_device_id
        or metrics.get("device_id")
        or metadata.get("device_id")
        or conversation.source_device_id,
        "started_at": turn.started_at,
        "finished_at": turn.finished_at,
        "status": turn.status,
        "triage_kind": metrics.get("triage_kind") or metadata.get("triage_kind"),
        "latency_first_delta_ms": metrics.get("latency_first_delta_ms"),
        "total_latency_ms": metrics.get("total_latency_ms"),
        "tokens_in": metrics.get("tokens_in") or 0,
        "tokens_out": metrics.get("tokens_out") or 0,
        "cost_usd_micro": metrics.get("cost_usd_micro") or 0,
        "model": metrics.get("model"),
        "trace_id": turn.trace_id or metadata.get("trace_id"),
        "error_code": metrics.get("error_code"),
        "metadata_": metadata,
        "owner_id": conversation.owner_id,
        "companion_id": conversation.companion_id,
        "memory_realm_id": conversation_meta.get("memory_realm_id")
        or metadata.get("memory_realm_id"),
        "genome_id": conversation_meta.get("genome_id") or metadata.get("genome_id"),
        "genome_hash": conversation_meta.get("genome_hash") or metadata.get("genome_hash"),
        "schema_version": conversation_meta.get("schema_version") or metadata.get("schema_version"),
        "realizer_version": conversation_meta.get("realizer_version")
        or metadata.get("realizer_version"),
        "conversation_title": conversation.title,
    }


def _record_to_job_row(record: LongTaskRecord) -> JobRow:
    return JobRow(
        job_id=record.id,
        owner_id=record.owner_id,
        companion_id=record.companion_id,
        conversation_id=record.conversation_id,
        turn_id=record.turn_id,
        provider=record.provider,
        kind=record.task_type,
        status=record.status.value,
        input_json={_LONG_TASK_PAYLOAD_KEY: _record_to_json(record)},
        provider_ref_json=_provider_ref_json(record),
        progress_json=_progress_json(record),
        result_json=_result_json(record),
        error_json=_error_json(record),
        created_at=record.created_at or datetime.now(timezone.utc),
        updated_at=record.updated_at or datetime.now(timezone.utc),
        completed_at=record.completed_at,
    )


def _apply_record_to_job_row(row: JobRow, record: LongTaskRecord) -> None:
    row.owner_id = record.owner_id
    row.companion_id = record.companion_id
    row.conversation_id = record.conversation_id
    row.turn_id = record.turn_id
    row.provider = record.provider
    row.kind = record.task_type
    row.status = record.status.value
    row.input_json = {_LONG_TASK_PAYLOAD_KEY: _record_to_json(record)}
    row.provider_ref_json = _provider_ref_json(record)
    row.progress_json = _progress_json(record)
    row.result_json = _result_json(record)
    row.error_json = _error_json(record)
    row.updated_at = record.updated_at or datetime.now(timezone.utc)
    row.completed_at = record.completed_at


def _job_row_to_record(row: JobRow) -> LongTaskRecord:
    payload = dict((row.input_json or {}).get(_LONG_TASK_PAYLOAD_KEY) or {})
    payload["id"] = row.job_id
    payload["provider"] = row.provider
    payload["status"] = row.status
    payload["owner_id"] = row.owner_id
    payload["companion_id"] = row.companion_id
    payload["conversation_id"] = row.conversation_id
    payload["turn_id"] = row.turn_id
    payload["task_type"] = row.kind
    payload["created_at"] = payload.get("created_at") or _dt_to_json(row.created_at)
    payload["updated_at"] = payload.get("updated_at") or _dt_to_json(row.updated_at)
    payload["completed_at"] = payload.get("completed_at") or _dt_to_json(row.completed_at)
    return _record_from_json(payload)


def _record_to_json(record: LongTaskRecord) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in record.__dataclass_fields__:
        value = getattr(record, field)
        if isinstance(value, (LongTaskStatus, CallbackStatus)):
            out[field] = value.value
        elif isinstance(value, datetime):
            out[field] = _dt_to_json(value)
        else:
            out[field] = value
    return out


def _record_from_json(data: dict[str, Any]) -> LongTaskRecord:
    values = dict(data)
    values["status"] = LongTaskStatus(values.get("status") or LongTaskStatus.ACCEPTED.value)
    values["callback_status"] = CallbackStatus(
        values.get("callback_status") or CallbackStatus.PENDING.value
    )
    for key in (
        "created_at",
        "updated_at",
        "started_at",
        "submitted_at",
        "last_progress_at",
        "last_polled_at",
        "completed_at",
        "callback_delivered_at",
        "lease_until",
        "next_retry_at",
    ):
        values[key] = _dt_from_json(values.get(key))
    values.setdefault("session_key", "")
    values.setdefault("task_date", "")
    values.setdefault("task_key", "")
    values.setdefault("task", "")
    return LongTaskRecord(**values)


def _provider_ref_json(record: LongTaskRecord) -> dict[str, Any]:
    return {
        "mementos_session_id": record.mementos_session_id,
        "mementos_conversation_id": record.mementos_conversation_id,
        "mementos_run_id": record.mementos_run_id,
        "mementos_latest_seq": record.mementos_latest_seq,
        "mementos_workspace_dir": record.mementos_workspace_dir,
        "worker_id": record.worker_id,
        "lease_until": _dt_to_json(record.lease_until),
        "attempt_count": record.attempt_count,
        "next_retry_at": _dt_to_json(record.next_retry_at),
        "external_status": record.external_status,
    }


def _progress_json(record: LongTaskRecord) -> dict[str, Any]:
    return {
        "progress_summary": record.progress_summary,
        "progress_events": record.progress_events,
        "last_progress_at": _dt_to_json(record.last_progress_at),
        "last_polled_at": _dt_to_json(record.last_polled_at),
    }


def _result_json(record: LongTaskRecord) -> dict[str, Any]:
    return {
        "result_text": record.result_text,
        "result_tts_summary": record.result_tts_summary,
        "result_payload": record.result_payload,
        "artifact_paths": record.artifact_paths,
        "callback_subject": record.callback_subject,
        "callback_status": record.callback_status.value,
        "callback_attempts": record.callback_attempts,
        "callback_last_error": record.callback_last_error,
        "callback_delivered_at": _dt_to_json(record.callback_delivered_at),
    }


def _error_json(record: LongTaskRecord) -> dict[str, Any]:
    return {
        "error_code": record.error_code,
        "error_message": record.error_message,
        "error_payload": record.error_payload,
    }


def _dt_to_json(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt_from_json(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


__all__ = [
    "AgentConversationReader",
    "AgentLongTaskStore",
    "build_agent_history_hydrator",
    "build_agent_turn_persister",
]
