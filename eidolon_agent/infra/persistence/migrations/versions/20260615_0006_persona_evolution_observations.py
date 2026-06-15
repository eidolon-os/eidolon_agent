"""persona observations and evolution proposals

Revision ID: 0006_persona_evolution_observations
Revises: 0005_long_tasks
Create Date: 2026-06-15 00:00:00 UTC
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_persona_evolution_observations"
down_revision = "0005_long_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "persona_observations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("instance_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(96), nullable=False),
        sa.Column("source", sa.String(64), nullable=False, server_default="system"),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("strength", sa.Float, nullable=False, server_default="0.5"),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0.5"),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("evidence", sa.JSON, nullable=True),
        sa.Column("memory_ids", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_persona_observations_tenant_id", "persona_observations", ["tenant_id"])
    op.create_index("ix_persona_observations_user_id", "persona_observations", ["user_id"])
    op.create_index("ix_persona_observations_instance_id", "persona_observations", ["instance_id"])
    op.create_index("ix_persona_observations_kind", "persona_observations", ["kind"])
    op.create_index("ix_persona_observations_status", "persona_observations", ["status"])
    op.create_index("ix_persona_observations_created_at", "persona_observations", ["created_at"])
    op.create_index(
        "ix_persona_obs_instance_created",
        "persona_observations",
        ["instance_id", "created_at"],
    )
    op.create_index(
        "ix_persona_obs_instance_status",
        "persona_observations",
        ["instance_id", "status"],
    )

    op.create_table(
        "persona_evolution_proposals",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("instance_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("patches", sa.JSON, nullable=True),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0"),
        sa.Column("rationale", sa.Text, nullable=False, server_default=""),
        sa.Column("evidence_ids", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decided_at", sa.DateTime, nullable=True),
        sa.Column("decision_reason", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_persona_evolution_proposals_tenant_id", "persona_evolution_proposals", ["tenant_id"]
    )
    op.create_index(
        "ix_persona_evolution_proposals_user_id", "persona_evolution_proposals", ["user_id"]
    )
    op.create_index(
        "ix_persona_evolution_proposals_instance_id", "persona_evolution_proposals", ["instance_id"]
    )
    op.create_index(
        "ix_persona_evolution_proposals_status", "persona_evolution_proposals", ["status"]
    )
    op.create_index(
        "ix_persona_evolution_proposals_created_at", "persona_evolution_proposals", ["created_at"]
    )
    op.create_index(
        "ix_persona_evolution_proposals_updated_at", "persona_evolution_proposals", ["updated_at"]
    )
    op.create_index(
        "ix_persona_prop_instance_created",
        "persona_evolution_proposals",
        ["instance_id", "created_at"],
    )
    op.create_index(
        "ix_persona_prop_instance_status",
        "persona_evolution_proposals",
        ["instance_id", "status"],
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_persona_prop_instance_status")
    op.execute("DROP INDEX IF EXISTS ix_persona_prop_instance_created")
    op.execute("DROP INDEX IF EXISTS ix_persona_evolution_proposals_updated_at")
    op.execute("DROP INDEX IF EXISTS ix_persona_evolution_proposals_created_at")
    op.execute("DROP INDEX IF EXISTS ix_persona_evolution_proposals_status")
    op.execute("DROP INDEX IF EXISTS ix_persona_evolution_proposals_instance_id")
    op.execute("DROP INDEX IF EXISTS ix_persona_evolution_proposals_user_id")
    op.execute("DROP INDEX IF EXISTS ix_persona_evolution_proposals_tenant_id")
    op.execute("DROP TABLE IF EXISTS persona_evolution_proposals")
    op.execute("DROP INDEX IF EXISTS ix_persona_obs_instance_status")
    op.execute("DROP INDEX IF EXISTS ix_persona_obs_instance_created")
    op.execute("DROP INDEX IF EXISTS ix_persona_observations_created_at")
    op.execute("DROP INDEX IF EXISTS ix_persona_observations_status")
    op.execute("DROP INDEX IF EXISTS ix_persona_observations_kind")
    op.execute("DROP INDEX IF EXISTS ix_persona_observations_instance_id")
    op.execute("DROP INDEX IF EXISTS ix_persona_observations_user_id")
    op.execute("DROP INDEX IF EXISTS ix_persona_observations_tenant_id")
    op.execute("DROP TABLE IF EXISTS persona_observations")
