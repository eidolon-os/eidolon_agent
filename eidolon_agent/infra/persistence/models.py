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
    Float,
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

    __table_args__ = (Index("ix_conv_user_started", "tenant_id", "user_id", "started_at"),)


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

    __table_args__ = (UniqueConstraint("conversation_id", "seq", name="uq_turn_conv_seq"),)


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

    __table_args__ = (Index("ix_msg_turn_created", "turn_id", "created_at"),)


# ---------------------------------------------------------------------------
# Long-running coworker tasks
# ---------------------------------------------------------------------------


class LongTaskRow(Base):
    __tablename__ = "long_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), default="mementos", index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_instance_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # Owning device (denormalized from turns.device_id / caller identity). Indexed
    # so the proactive worker + Phase 4 offline buffer + admin can route/query by
    # device without a turn join (plan §3 Phase 3).
    device_id: Mapped[str | None] = mapped_column(String(64), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    turn_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(128))
    session_key: Mapped[str] = mapped_column(String(192), index=True)
    task_date: Mapped[str] = mapped_column(String(10), index=True)
    task_key: Mapped[str] = mapped_column(String(256), unique=True)
    task: Mapped[str] = mapped_column(Text)
    user_text: Mapped[str | None] = mapped_column(Text)
    task_type: Mapped[str] = mapped_column(String(64), default="other", index=True)
    urgency: Mapped[str] = mapped_column(String(32), default="normal")
    expected_output: Mapped[str | None] = mapped_column(Text)
    context_summary: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[list | None] = mapped_column(JSON)
    request_payload: Mapped[dict | None] = mapped_column(JSON)
    mementos_session_id: Mapped[str | None] = mapped_column(String(128), index=True)
    mementos_conversation_id: Mapped[str | None] = mapped_column(String(128), index=True)
    mementos_run_id: Mapped[str | None] = mapped_column(String(128), index=True)
    mementos_latest_seq: Mapped[int | None] = mapped_column(Integer)
    mementos_workspace_dir: Mapped[str | None] = mapped_column(Text)
    progress_summary: Mapped[str | None] = mapped_column(Text)
    progress_events: Mapped[list | None] = mapped_column(JSON)
    result_text: Mapped[str | None] = mapped_column(Text)
    result_tts_summary: Mapped[str | None] = mapped_column(Text)
    result_payload: Mapped[dict | None] = mapped_column(JSON)
    artifact_paths: Mapped[list | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    error_payload: Mapped[dict | None] = mapped_column(JSON)
    callback_subject: Mapped[str | None] = mapped_column(String(256))
    callback_status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    callback_attempts: Mapped[int] = mapped_column(Integer, default=0)
    callback_last_error: Mapped[str | None] = mapped_column(Text)
    callback_delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    worker_id: Mapped[str | None] = mapped_column(String(128), index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    external_status: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_long_tasks_user_created", "tenant_id", "user_id", "created_at"),
        Index("ix_long_tasks_status_updated", "status", "updated_at"),
        Index("ix_long_tasks_session_created", "session_key", "created_at"),
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


class PersonaObservationRow(Base):
    """Durable evidence for long-term personal-instance evolution."""

    __tablename__ = "persona_observations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    instance_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(96), index=True)
    source: Mapped[str] = mapped_column(String(64), default="system")
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    strength: Mapped[float] = mapped_column(Float, default=0.5)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    summary: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict | None] = mapped_column(JSON)
    memory_ids: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now, index=True)

    __table_args__ = (
        Index("ix_persona_obs_instance_created", "instance_id", "created_at"),
        Index("ix_persona_obs_instance_status", "instance_id", "status"),
    )


class PersonaEvolutionProposalRow(Base):
    """Reviewable proposal generated from persona observations."""

    __tablename__ = "persona_evolution_proposals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    instance_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    patches: Mapped[list | None] = mapped_column(JSON)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[str] = mapped_column(Text, default="")
    evidence_ids: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now, index=True)
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decision_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_persona_prop_instance_created", "instance_id", "created_at"),
        Index("ix_persona_prop_instance_status", "instance_id", "status"),
    )


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


class PersonaTemplateCustomRow(Base):
    """Operator-authored persona templates (Phase 29.D).

    Built-in templates live as YAML files under ``settings.persona.templates_dir``
    — those are deployment artifacts shipped with the agent code. Custom
    templates created via admin's UI / REST land here. The
    ``PersonaTemplateRegistry`` consults both sources at lookup time so
    rendering / instance creation works uniformly across the two.

    No foreign key from ``persona_instances.template_id`` to this table
    because instances may reference builtin templates that aren't in
    SQL. Refcount-on-delete is enforced in application code.
    """

    __tablename__ = "persona_templates_custom"

    template_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    archetype: Mapped[str] = mapped_column(String(64), default="custom")
    # The raw YAML text. Stored verbatim so the operator can round-trip
    # edits — re-serializing through PersonaTemplate.model_dump() would
    # lose comments and reorder fields.
    yaml_body: Mapped[str] = mapped_column(Text)
    # Bumps on every PUT. persona_instances persist the revision they
    # were rendered from so older agents keep working after template
    # updates — operator must explicitly trigger re-render to migrate.
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)


__all__ = [
    "Base",
    "ChatMessageRow",
    "ConversationRow",
    "DeviceRow",
    "EvolutionHistoryRow",
    "LongTaskRow",
    "PersonaEvolutionProposalRow",
    "PersonaInstanceRow",
    "PersonaObservationRow",
    "PersonaTemplateCustomRow",
    "TurnRow",
]
