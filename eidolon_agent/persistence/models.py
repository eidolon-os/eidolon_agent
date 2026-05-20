"""SQLAlchemy 2.0 ORM models.

Schema is intentionally narrow: each table maps to one concept in the
architecture (conversation, turn, message, instance, device, audit, etc).
JSON columns are used sparingly — strongly-typed columns are preferred so
queries and dashboards can rely on them.
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
# Tenants & users — minimal multi-tenancy primitives
# ---------------------------------------------------------------------------


class TenantRow(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    region: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    # Resource quotas
    max_concurrent_streams: Mapped[int] = mapped_column(Integer, default=16)
    max_agent_instances: Mapped[int] = mapped_column(Integer, default=10)


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), index=True
    )
    display_name: Mapped[str] = mapped_column(String(128))
    locale: Mapped[str] = mapped_column(String(16), default="zh-CN")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)


# ---------------------------------------------------------------------------
# Agent lifecycle
# ---------------------------------------------------------------------------


class AgentInstanceRow(Base):
    __tablename__ = "agent_instances"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    template_id: Mapped[str] = mapped_column(String(128), index=True)
    template_version: Mapped[int] = mapped_column(Integer)
    overlay_version: Mapped[int] = mapped_column(Integer, default=1)
    nickname_alias: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(16), default="active"
    )  # active|stopped|degraded
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime)


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
# Devices & pairing
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


class PairingCodeRow(Base):
    __tablename__ = "pairing_codes"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(64))
    default_template_id: Mapped[str | None] = mapped_column(String(128))
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    issued_by_actor: Mapped[str] = mapped_column(String(64))


# ---------------------------------------------------------------------------
# Audit & evolution
# ---------------------------------------------------------------------------


class AuditLogRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now, index=True)
    actor: Mapped[str] = mapped_column(String(64))  # admin user id or "system"
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(128))
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True)
    payload: Mapped[dict | None] = mapped_column("payload", JSON)


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


class TokenUsageOutboxRow(Base):
    """Out-of-band publish outbox for NATS turn fanout when NATS is degraded."""

    __tablename__ = "publish_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column("payload", JSON)
    headers: Mapped[dict | None] = mapped_column("headers", JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


__all__ = [
    "AgentInstanceRow",
    "AuditLogRow",
    "Base",
    "ChatMessageRow",
    "ConversationRow",
    "DeviceRow",
    "EvolutionHistoryRow",
    "PairingCodeRow",
    "TenantRow",
    "TokenUsageOutboxRow",
    "TurnRow",
    "UserRow",
]
