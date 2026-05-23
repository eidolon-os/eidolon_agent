"""persona instances table

Revision ID: 0003_persona_instances
Revises: 0002_drop_unused
Create Date: 2026-05-24 00:00:00 UTC

Phase 3 of the realtime-response optimization plan: move per-user persona
instance overlays out of ``~/eidolon/personas/instances/<t>/<u>/*.yaml`` and
into SQLite. The full overlay is serialised into the ``overlay_json``
column; sibling columns (template_id, overlay_version, timestamps) exist so
the admin UI can paginate + sort with SQL without parsing the JSON.

Templates remain on disk — they are deployment artifacts, not user state.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_persona_instances"
down_revision = "0002_drop_unused"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "persona_instances",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("template_id", sa.String(128), nullable=False),
        sa.Column("template_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("overlay_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("overlay_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("last_active_at", sa.DateTime),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "id", name="uq_persona_inst_tuid"
        ),
    )
    op.create_index(
        "ix_persona_instances_tenant_id", "persona_instances", ["tenant_id"]
    )
    op.create_index("ix_persona_instances_user_id", "persona_instances", ["user_id"])
    op.create_index(
        "ix_persona_instances_template_id", "persona_instances", ["template_id"]
    )
    op.create_index(
        "ix_persona_inst_last_active", "persona_instances", ["last_active_at"]
    )


def downgrade() -> None:
    for name in (
        "ix_persona_inst_last_active",
        "ix_persona_instances_template_id",
        "ix_persona_instances_user_id",
        "ix_persona_instances_tenant_id",
    ):
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.execute("DROP TABLE IF EXISTS persona_instances")
