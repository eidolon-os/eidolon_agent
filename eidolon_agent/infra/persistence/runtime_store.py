"""Agent-owned SQLite authority for runtime history and long-running jobs.

The store deliberately contains no Owner, Companion, Persona, Device, Hub, or
Kernel tables. Their identifiers are immutable references copied from the
verified runtime context; cross-authority invariants are checked at application
boundaries, never with cross-database foreign keys on the turn hot path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

from eidolon_sdk.biz.audit import AuditEnvelope
from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    event,
    func,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from eidolon_agent.core.types.conversation import CONVERSATION_ID_MAX_LENGTH

JsonDict = dict[str, Any]
_SCHEMA_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(UTC)


class RuntimeBase(DeclarativeBase):
    type_annotation_map: ClassVar = {JsonDict: JSON}


class RuntimeSessionRow(RuntimeBase):
    __tablename__ = "runtime_sessions"

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    companion_id: Mapped[str] = mapped_column(String(64), index=True)
    source_device_id: Mapped[str | None] = mapped_column(String(128), index=True)
    transport: Mapped[str] = mapped_column(String(64), default="", index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    metadata_json: Mapped[JsonDict] = mapped_column(default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("ix_agent_runtime_sessions_owner_last_seen", "owner_id", "last_seen_at"),
    )


class ConversationRow(RuntimeBase):
    __tablename__ = "conversations"

    conversation_id: Mapped[str] = mapped_column(
        String(CONVERSATION_ID_MAX_LENGTH), primary_key=True
    )
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    companion_id: Mapped[str] = mapped_column(String(64), index=True)
    runtime_session_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("runtime_sessions.session_id", ondelete="SET NULL"),
        index=True,
    )
    source_device_id: Mapped[str | None] = mapped_column(String(128), index=True)
    title: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[JsonDict] = mapped_column(default=dict)

    __table_args__ = (Index("ix_agent_conversations_owner_started", "owner_id", "started_at"),)


class TurnRow(RuntimeBase):
    __tablename__ = "turns"

    turn_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(CONVERSATION_ID_MAX_LENGTH),
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        index=True,
    )
    seq: Mapped[int] = mapped_column(Integer)
    runtime_session_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("runtime_sessions.session_id", ondelete="SET NULL"),
        index=True,
    )
    source_device_id: Mapped[str | None] = mapped_column(String(128), index=True)
    trigger: Mapped[str] = mapped_column(String(32), default="user")
    status: Mapped[str] = mapped_column(String(32), default="completed", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    trace_json: Mapped[JsonDict] = mapped_column(default=dict)
    metrics_json: Mapped[JsonDict] = mapped_column(default=dict)
    metadata_json: Mapped[JsonDict] = mapped_column(default=dict)

    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "seq",
            name="uq_agent_turns_conversation_seq",
        ),
    )


class MessageRow(RuntimeBase):
    __tablename__ = "messages"

    message_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    turn_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("turns.turn_id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(32), index=True)
    content: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(64), default="text/plain")
    visibility: Mapped[str] = mapped_column(String(32), default="normal", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    metadata_json: Mapped[JsonDict] = mapped_column(default=dict)

    __table_args__ = (
        UniqueConstraint("turn_id", "seq", name="uq_agent_messages_turn_seq"),
        Index("ix_agent_messages_turn_created", "turn_id", "created_at"),
    )


class JobRow(RuntimeBase):
    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    companion_id: Mapped[str | None] = mapped_column(String(64), index=True)
    conversation_id: Mapped[str | None] = mapped_column(
        String(CONVERSATION_ID_MAX_LENGTH), index=True
    )
    turn_id: Mapped[str | None] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    input_json: Mapped[JsonDict] = mapped_column(default=dict)
    provider_ref_json: Mapped[JsonDict] = mapped_column(default=dict)
    progress_json: Mapped[JsonDict] = mapped_column(default=dict)
    result_json: Mapped[JsonDict] = mapped_column(default=dict)
    error_json: Mapped[JsonDict] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentAuditOutboxRow(RuntimeBase):
    __tablename__ = "audit_outbox"

    outbox_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True)
    producer: Mapped[str] = mapped_column(String(64), default="eidolon-agent")
    category: Mapped[str] = mapped_column(String(16))
    owner_id: Mapped[str | None] = mapped_column(String(64), index=True)
    subject_type: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(16), default="success")
    severity: Mapped[str] = mapped_column(String(16), default="info")
    reason: Mapped[str | None] = mapped_column(String(256))
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    data_classification: Mapped[str] = mapped_column(String(16), default="safe")
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    payload_json: Mapped[JsonDict] = mapped_column(default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_error: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index(
            "ix_agent_audit_outbox_delivery",
            "published_at",
            "next_attempt_at",
            "outbox_id",
        ),
    )


@dataclass(frozen=True)
class PendingAuditBatch:
    events: list[AuditEnvelope]
    max_attempt_count: int


class AgentAuditOutbox:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    @staticmethod
    def build_row(
        *,
        category: str,
        subject_type: str,
        subject_id: str,
        action: str,
        owner_id: str | None = None,
        outcome: str = "success",
        severity: str = "info",
        reason: str | None = None,
        trace_id: str | None = None,
        payload: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
        event_id: str | None = None,
    ) -> AgentAuditOutboxRow:
        return AgentAuditOutboxRow(
            event_id=event_id or f"audit-{uuid4().hex}",
            category=category,
            owner_id=owner_id,
            subject_type=subject_type,
            subject_id=subject_id,
            action=action,
            outcome=outcome,
            severity=severity,
            reason=reason,
            trace_id=trace_id,
            payload_json=payload or {},
            occurred_at=occurred_at or utc_now(),
        )

    async def list_pending(self, *, limit: int = 200) -> list[AuditEnvelope]:
        return (await self.pending_batch(limit=limit)).events

    async def pending_batch(self, *, limit: int = 200) -> PendingAuditBatch:
        now = utc_now()
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(AgentAuditOutboxRow)
                    .where(AgentAuditOutboxRow.published_at.is_(None))
                    .where(AgentAuditOutboxRow.next_attempt_at <= now)
                    .order_by(AgentAuditOutboxRow.outbox_id)
                    .limit(limit)
                )
            )
            return PendingAuditBatch(
                events=[_audit_envelope(row) for row in rows],
                max_attempt_count=max((row.attempt_count for row in rows), default=0),
            )

    async def mark_published(self, event_ids: set[str]) -> int:
        if not event_ids:
            return 0
        async with self._session_factory() as session:
            result = await session.execute(
                update(AgentAuditOutboxRow)
                .where(AgentAuditOutboxRow.event_id.in_(event_ids))
                .where(AgentAuditOutboxRow.published_at.is_(None))
                .values(published_at=utc_now(), last_error="")
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def mark_failed(
        self,
        event_ids: set[str],
        *,
        error: str,
        retry_after: timedelta,
    ) -> int:
        if not event_ids:
            return 0
        async with self._session_factory() as session:
            result = await session.execute(
                update(AgentAuditOutboxRow)
                .where(AgentAuditOutboxRow.event_id.in_(event_ids))
                .where(AgentAuditOutboxRow.published_at.is_(None))
                .values(
                    attempt_count=AgentAuditOutboxRow.attempt_count + 1,
                    last_error=error[:2_000],
                    next_attempt_at=utc_now() + retry_after,
                )
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def purge_published(self, *, before: datetime) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(AgentAuditOutboxRow)
                .where(AgentAuditOutboxRow.published_at.is_not(None))
                .where(AgentAuditOutboxRow.published_at < before)
            )
            await session.commit()
            return int(result.rowcount or 0)


class AgentRuntimeStore:
    """One writer pool and one schema lifecycle owned by the Agent process."""

    def __init__(
        self,
        *,
        sqlite_path: Path,
        engine: AsyncEngine,
        session_factory: async_sessionmaker,
        read_engine: AsyncEngine,
        read_session_factory: async_sessionmaker,
    ) -> None:
        self.sqlite_path = sqlite_path
        self.engine = engine
        self.session_factory = session_factory
        self.read_engine = read_engine
        self.read_session_factory = read_session_factory

    @classmethod
    def open(
        cls,
        sqlite_path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        wal_autocheckpoint_pages: int = 1_000,
    ) -> AgentRuntimeStore:
        path = Path(sqlite_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{path}",
            connect_args={"timeout": busy_timeout_ms / 1_000},
            pool_size=1,
            max_overflow=0,
        )
        # Runtime writes intentionally stay serialized through ``engine``.  A
        # management read must not queue behind a turn that is holding that one
        # connection while it persists live state, though: the Admin contract
        # has a short authority timeout and conversation history is read-only.
        # A distinct, query-only connection preserves the single-writer rule
        # while letting observability reads proceed from SQLite's WAL snapshot.
        read_engine = create_async_engine(
            f"sqlite+aiosqlite:///{path}",
            connect_args={"timeout": busy_timeout_ms / 1_000},
            pool_size=1,
            max_overflow=0,
        )

        @event.listens_for(engine.sync_engine, "connect")
        def _configure(connection, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=FULL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                cursor.execute(f"PRAGMA wal_autocheckpoint={wal_autocheckpoint_pages}")
            finally:
                cursor.close()

        @event.listens_for(read_engine.sync_engine, "connect")
        def _configure_reader(connection, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA query_only=ON")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            finally:
                cursor.close()

        return cls(
            sqlite_path=path,
            engine=engine,
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            read_engine=read_engine,
            read_session_factory=async_sessionmaker(read_engine, expire_on_commit=False),
        )

    async def init_schema(self) -> None:
        """Apply the clean V1 baseline; legacy Data tables are never imported."""

        async with self.engine.begin() as connection:
            version = int((await connection.execute(text("PRAGMA user_version"))).scalar_one())
            if version == 0:
                await connection.run_sync(RuntimeBase.metadata.create_all)
                await connection.execute(text(f"PRAGMA user_version={_SCHEMA_VERSION}"))
            elif version != _SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported agent runtime schema {version}; expected {_SCHEMA_VERSION}"
                )

    async def close(self) -> None:
        await self.read_engine.dispose()
        await self.engine.dispose()

    @property
    def audit_outbox(self) -> AgentAuditOutbox:
        return AgentAuditOutbox(self.session_factory)

    async def delete_owner_runtime(self, owner_id: str) -> dict[str, int]:
        """Delete private runtime content without treating audit as source data."""

        async with self.session_factory() as session:
            counts: dict[str, int] = {
                "messages": int(
                    await session.scalar(
                        select(func.count())
                        .select_from(MessageRow)
                        .join(TurnRow, MessageRow.turn_id == TurnRow.turn_id)
                        .join(
                            ConversationRow,
                            TurnRow.conversation_id == ConversationRow.conversation_id,
                        )
                        .where(ConversationRow.owner_id == owner_id)
                    )
                    or 0
                ),
                "turns": int(
                    await session.scalar(
                        select(func.count())
                        .select_from(TurnRow)
                        .join(
                            ConversationRow,
                            TurnRow.conversation_id == ConversationRow.conversation_id,
                        )
                        .where(ConversationRow.owner_id == owner_id)
                    )
                    or 0
                ),
            }
            for name, model in (
                ("jobs", JobRow),
                ("conversations", ConversationRow),
                ("runtime_sessions", RuntimeSessionRow),
            ):
                result = await session.execute(delete(model).where(model.owner_id == owner_id))
                counts[name] = int(result.rowcount or 0)
            await session.commit()
            return counts

    async def delete_companion_runtime(self, owner_id: str, companion_id: str) -> dict[str, int]:
        """Delete runtime rows for one companion within an owner boundary."""
        async with self.session_factory() as session:
            scope = (
                ConversationRow.owner_id == owner_id,
                ConversationRow.companion_id == companion_id,
            )
            counts = {
                "messages": int(
                    await session.scalar(
                        select(func.count())
                        .select_from(MessageRow)
                        .join(TurnRow, MessageRow.turn_id == TurnRow.turn_id)
                        .join(
                            ConversationRow,
                            TurnRow.conversation_id == ConversationRow.conversation_id,
                        )
                        .where(*scope)
                    )
                    or 0
                ),
                "turns": int(
                    await session.scalar(
                        select(func.count())
                        .select_from(TurnRow)
                        .join(
                            ConversationRow,
                            TurnRow.conversation_id == ConversationRow.conversation_id,
                        )
                        .where(*scope)
                    )
                    or 0
                ),
            }
            job_result = await session.execute(
                delete(JobRow)
                .where(JobRow.owner_id == owner_id)
                .where(JobRow.companion_id == companion_id)
            )
            conversation_result = await session.execute(delete(ConversationRow).where(*scope))
            session_result = await session.execute(
                delete(RuntimeSessionRow)
                .where(RuntimeSessionRow.owner_id == owner_id)
                .where(RuntimeSessionRow.companion_id == companion_id)
            )
            counts.update(
                jobs=int(job_result.rowcount or 0),
                conversations=int(conversation_result.rowcount or 0),
                runtime_sessions=int(session_result.rowcount or 0),
            )
            await session.commit()
            return counts


def _audit_envelope(row: AgentAuditOutboxRow) -> AuditEnvelope:
    return AuditEnvelope(
        event_id=row.event_id,
        producer=row.producer,
        producer_seq=row.outbox_id,
        category=row.category,
        owner_id=row.owner_id,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        action=row.action,
        outcome=row.outcome,
        severity=row.severity,
        reason=row.reason,
        trace_id=row.trace_id,
        data_classification=row.data_classification,
        schema_version=row.schema_version,
        payload=dict(row.payload_json or {}),
        occurred_at=row.occurred_at,
    )


__all__ = [
    "AgentAuditOutbox",
    "AgentAuditOutboxRow",
    "AgentRuntimeStore",
    "ConversationRow",
    "JobRow",
    "MessageRow",
    "RuntimeSessionRow",
    "TurnRow",
]
