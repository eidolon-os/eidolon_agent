"""drop unused tables

Revision ID: 0002_drop_unused
Revises: 0001_initial
Create Date: 2026-05-23 00:00:00 UTC

Phase 4 of the simplification plan: six tables created by the initial
migration were never actually written by any repository:

    tenants, users, agent_instances  — multi-tenancy primitives now in
                                       memory + config
    pairing_codes                    — PairingCoordinator keeps codes
                                       in memory
    audit_log                        — no admin actions audit-logged
    publish_outbox                   — outbox unused; JetStream provides
                                       at-least-once delivery

We drop them rather than leaving orphan tables to keep the schema in
sync with the ORM models. Downgrade re-creates them by re-running the
initial schema for those tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_drop_unused"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop indexes first where they exist.
    for name in (
        "ix_audit_log_at",
        "ix_audit_log_action",
        "ix_audit_log_tenant_id",
        "ix_publish_outbox_created_at",
        "ix_users_tenant_id",
        "ix_agent_instances_tenant_id",
        "ix_agent_instances_user_id",
        "ix_agent_instances_template_id",
        "ix_pairing_codes_expires_at",
    ):
        op.execute(f"DROP INDEX IF EXISTS {name}")
    for tbl in (
        "audit_log",
        "publish_outbox",
        "pairing_codes",
        "agent_instances",
        "users",
        "tenants",
    ):
        op.execute(f"DROP TABLE IF EXISTS {tbl}")


def downgrade() -> None:
    # Recreate tables as they were in 0001_initial (best-effort; the data is
    # gone). Kept terse — see 0001_initial for the full column definitions.
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
