"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-20 00:00:00 UTC

Mirrors :mod:`eidolon_agent.infra.persistence.models`. Future migrations should be
generated via ``alembic revision --autogenerate``; this initial one is
hand-written so a fresh repo can ``alembic upgrade head`` without an existing DB.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("region", sa.String(32)),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("metadata", sa.JSON),
        sa.Column("max_concurrent_streams", sa.Integer, nullable=False, server_default="16"),
        sa.Column("max_agent_instances", sa.Integer, nullable=False, server_default="10"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("locale", sa.String(16), nullable=False, server_default="zh-CN"),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    op.create_table(
        "agent_instances",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("template_id", sa.String(128), nullable=False),
        sa.Column("template_version", sa.Integer, nullable=False),
        sa.Column("overlay_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("nickname_alias", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("last_active_at", sa.DateTime),
    )
    op.create_index("ix_agent_instances_tenant_id", "agent_instances", ["tenant_id"])
    op.create_index("ix_agent_instances_user_id", "agent_instances", ["user_id"])
    op.create_index("ix_agent_instances_template_id", "agent_instances", ["template_id"])

    op.create_table(
        "conversations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("agent_instance_id", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime, nullable=False),
        sa.Column("ended_at", sa.DateTime),
        sa.Column("title", sa.String(256)),
        sa.Column("metadata", sa.JSON),
    )
    op.create_index("ix_conversations_agent_instance_id", "conversations", ["agent_instance_id"])
    op.create_index("ix_conv_user_started", "conversations", ["tenant_id", "user_id", "started_at"])

    op.create_table(
        "turns",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("caller_kind", sa.String(32)),
        sa.Column("device_id", sa.String(64)),
        sa.Column("started_at", sa.DateTime, nullable=False),
        sa.Column("finished_at", sa.DateTime),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("triage_kind", sa.String(16)),
        sa.Column("latency_first_delta_ms", sa.Integer),
        sa.Column("total_latency_ms", sa.Integer),
        sa.Column("tokens_in", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd_micro", sa.Integer, nullable=False, server_default="0"),
        sa.Column("model", sa.String(64)),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("metadata", sa.JSON),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_turn_conv_seq"),
    )
    op.create_index("ix_turns_conversation_id", "turns", ["conversation_id"])
    op.create_index("ix_turns_device_id", "turns", ["device_id"])
    op.create_index("ix_turns_trace_id", "turns", ["trace_id"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "turn_id",
            sa.String(64),
            sa.ForeignKey("turns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_type", sa.String(32), nullable=False, server_default="text/plain"),
        sa.Column("tokens", sa.Integer),
        sa.Column("model", sa.String(64)),
        sa.Column("tool_call_id", sa.String(64)),
        sa.Column("tool_name", sa.String(64)),
        sa.Column("tool_arguments", sa.JSON),
        sa.Column("realtime_signals", sa.JSON),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("is_private", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_chat_messages_turn_id", "chat_messages", ["turn_id"])
    op.create_index("ix_chat_messages_role", "chat_messages", ["role"])
    op.create_index("ix_msg_turn_created", "chat_messages", ["turn_id", "created_at"])

    op.create_table(
        "devices",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128)),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("scopes", sa.JSON),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("last_seen_at", sa.DateTime),
        sa.Column("revoked_at", sa.DateTime),
    )
    op.create_index("ix_devices_tenant_id", "devices", ["tenant_id"])
    op.create_index("ix_devices_user_id", "devices", ["user_id"])

    op.create_table(
        "pairing_codes",
        sa.Column("code", sa.String(16), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("default_template_id", sa.String(128)),
        sa.Column("issued_at", sa.DateTime, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("used_at", sa.DateTime),
        sa.Column("issued_by_actor", sa.String(64), nullable=False),
    )
    op.create_index("ix_pairing_codes_expires_at", "pairing_codes", ["expires_at"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime, nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(128)),
        sa.Column("tenant_id", sa.String(64)),
        sa.Column("payload", sa.JSON),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_tenant_id", "audit_log", ["tenant_id"])

    op.create_table(
        "evolution_history",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("instance_id", sa.String(64), nullable=False),
        sa.Column("from_overlay_version", sa.Integer, nullable=False),
        sa.Column("to_overlay_version", sa.Integer, nullable=False),
        sa.Column("proposed_by", sa.String(32), nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("delta", sa.JSON),
        sa.Column(
            "requires_human_approval", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column("approved_by", sa.String(64)),
        sa.Column("applied_at", sa.DateTime),
        sa.Column("rolled_back_at", sa.DateTime),
        sa.Column("git_commit", sa.String(64)),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_evolution_history_instance_id", "evolution_history", ["instance_id"])
    op.create_index("ix_evolution_history_created_at", "evolution_history", ["created_at"])

    op.create_table(
        "publish_outbox",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("subject", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("headers", sa.JSON),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("sent_at", sa.DateTime),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
    )
    op.create_index("ix_publish_outbox_created_at", "publish_outbox", ["created_at"])


def downgrade() -> None:
    for tbl in (
        "publish_outbox",
        "evolution_history",
        "audit_log",
        "pairing_codes",
        "devices",
        "chat_messages",
        "turns",
        "conversations",
        "agent_instances",
        "users",
        "tenants",
    ):
        op.drop_table(tbl)
