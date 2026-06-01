"""persona_templates_custom table

Revision ID: 0004_persona_templates_custom
Revises: 0003_persona_instances
Create Date: 2026-06-01 00:00:00 UTC

Phase 29.D — operator-authored custom persona templates. Built-in
templates remain as YAML files under ``settings.persona.templates_dir``
(read-only deployment artifacts); this table stores the templates an
operator created or forked through admin's Templates UI.

Schema:
    template_id   PK, max 128 chars (NATS key charset to match builtin)
    tenant_id     scope; defaults to ``"default"`` for single-tenant
    display_name  human-friendly name
    archetype     short tag for "kind of persona" (caretaker/playful/...)
    yaml_body     the full template doc as raw YAML text. Stored verbatim
                  so the operator can round-trip edits exactly.
    revision      bumps on every PUT; persona_instances reference the
                  template_revision they were rendered from so older
                  agents keep working when a template is updated.
    created_at, updated_at

Note: there is NO foreign key from persona_instances.template_id to this
table — instances may reference builtin templates that live on disk, not
here. Refcount during DELETE is enforced in application code by querying
persona_instances directly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_persona_templates_custom"
down_revision = "0003_persona_instances"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "persona_templates_custom",
        sa.Column("template_id", sa.String(128), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            nullable=False,
            server_default="default",
        ),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column(
            "archetype",
            sa.String(64),
            nullable=False,
            server_default="custom",
        ),
        sa.Column("yaml_body", sa.Text, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index(
        "ix_persona_templates_custom_tenant",
        "persona_templates_custom",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_persona_templates_custom_tenant")
    op.execute("DROP TABLE IF EXISTS persona_templates_custom")
