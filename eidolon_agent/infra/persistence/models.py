"""SQLAlchemy 2.0 ORM models — only what we actively read/write.

Six tables after the Phase 3 persona-instance migration:
    conversations / turns / chat_messages — conversation event source
    devices                                — pairing & revocation
    evolution_history                      — persona evolution audit
    persona_instances                      — per-user persona overlay storage
                                             (replaces the old YAML files)

Dropped (see migrations/versions/...drop_unused_tables.py):
    tenants, users, agent_instances        — never written; multi-tenancy
                                             primitives now live entirely in
                                             memory + config
    pairing_codes                          — coordinator keeps codes in memory
    audit_log                              — no admin actions audit-logged yet
    publish_outbox                         — outbox unused; rely on JetStream
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Shared declarative base. Naming convention enforces predictable FK/idx names."""

    type_annotation_map: dict[type, type] = {  # noqa: RUF012 - SQLAlchemy reads this once at class build
        dict: JSON,
    }


# ---------------------------------------------------------------------------
# Conversation event source
# ---------------------------------------------------------------------------


class ConversationRow(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(64))
    agent_instance_id: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    title: Mapped[str | None] = mapped_column(String(256))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    turns: Mapped[list[TurnRow]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_conv_user_started", "tenant_id", "user_id", "started_at"),
    )


class TurnRow(Base):
    __tablename__ = "turns"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[str] = mapped_column(String(32))
    caller_kind: Mapped[str | None] = mapped_column(String(32))
    device_id: Mapped[str | None] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16))
    triage_kind: Mapped[str | None] = mapped_column(String(16))
    latency_first_delta_ms: Mapped[int | None] = mapped_column(Integer)
    total_latency_ms: Mapped[int | None] = mapped_column(Integer)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd_micro: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    error_code: Mapped[str | None] = mapped_column(String(64))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    conversation: Mapped[ConversationRow] = relationship(back_populates="turns")
    messages: Mapped[list[ChatMessageRow]] = relationship(
        back_populates="turn", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_turn_conv_seq"),
    )


class ChatMessageRow(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    turn_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("turns.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), index=True)
    content: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(32), default="text/plain")
    tokens: Mapped[int | None] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(String(64))
    tool_call_id: Mapped[str | None] = mapped_column(String(64))
    tool_name: Mapped[str | None] = mapped_column(String(64))
    tool_arguments: Mapped[dict | None] = mapped_column("tool_arguments", JSON)
    realtime_signals: Mapped[dict | None] = mapped_column("realtime_signals", JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)

    turn: Mapped[TurnRow] = relationship(back_populates="messages")

    __table_args__ = (
        Index("ix_msg_turn_created", "turn_id", "created_at"),
    )


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


class DeviceRow(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str | None] = mapped_column(String(128))
    token_hash: Mapped[str] = mapped_column(String(128))
    scopes: Mapped[dict | None] = mapped_column("scopes", JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


# ---------------------------------------------------------------------------
# Persona evolution audit
# ---------------------------------------------------------------------------


class EvolutionHistoryRow(Base):
    __tablename__ = "evolution_history"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(64), index=True)
    from_overlay_version: Mapped[int] = mapped_column(Integer)
    to_overlay_version: Mapped[int] = mapped_column(Integer)
    proposed_by: Mapped[str] = mapped_column(String(32))
    rationale: Mapped[str] = mapped_column(Text)
    delta: Mapped[dict | None] = mapped_column("delta", JSON)
    requires_human_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by: Mapped[str | None] = mapped_column(String(64))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime)
    git_commit: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now, index=True)


# ---------------------------------------------------------------------------
# Persona instances (per-user overlay snapshots)
# ---------------------------------------------------------------------------


class PersonaInstanceRow(Base):
    """A user's persona overlay copy.

    The full ``PersonaInstance`` model_dump() is serialised into ``overlay_json``.
    The denormalised columns above the JSON blob (template_id, overlay_version,
    timestamps) exist solely so the admin UI can paginate + sort with SQL
    without parsing the JSON. Keep them in sync with the JSON on every save —
    SqlPersonaInstanceRepository.upsert is the single writer.
    """

    __tablename__ = "persona_instances"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    template_id: Mapped[str] = mapped_column(String(128), index=True)
    template_version: Mapped[int] = mapped_column(Integer, default=1)
    overlay_version: Mapped[int] = mapped_column(Integer, default=1)
    overlay_json: Mapped[dict] = mapped_column("overlay_json", JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "id", name="uq_persona_inst_tuid"),
        Index("ix_persona_inst_last_active", "last_active_at"),
    )


__all__ = [
    "Base",
    "ChatMessageRow",
    "ConversationRow",
    "DeviceRow",
    "EvolutionHistoryRow",
    "PersonaInstanceRow",
    "TurnRow",
]
