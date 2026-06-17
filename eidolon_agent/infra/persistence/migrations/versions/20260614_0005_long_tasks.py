"""long_tasks table

Revision ID: 0005_long_tasks
Revises: 0004_persona_templates_custom
Create Date: 2026-06-14 00:00:00 UTC

Durable records for long-running coworker tasks submitted through
``delegate_to_coworker``. Mementos is the first provider, but the table stores the
agent-side lifecycle and callback metadata independently so later progress
callbacks can update one row without rehydrating turn history.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_long_tasks"
down_revision = "0004_persona_templates_custom"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "long_tasks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False, server_default="mementos"),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("agent_instance_id", sa.String(64), nullable=True),
        sa.Column("conversation_id", sa.String(64), nullable=True),
        sa.Column("turn_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("tool_call_id", sa.String(128), nullable=True),
        sa.Column("session_key", sa.String(192), nullable=False),
        sa.Column("task_date", sa.String(10), nullable=False),
        sa.Column("task_key", sa.String(256), nullable=False, unique=True),
        sa.Column("task", sa.Text, nullable=False),
        sa.Column("user_text", sa.Text, nullable=True),
        sa.Column("task_type", sa.String(64), nullable=False, server_default="other"),
        sa.Column("urgency", sa.String(32), nullable=False, server_default="normal"),
        sa.Column("expected_output", sa.Text, nullable=True),
        sa.Column("context_summary", sa.Text, nullable=True),
        sa.Column("attachments", sa.JSON, nullable=True),
        sa.Column("request_payload", sa.JSON, nullable=True),
        sa.Column("mementos_session_id", sa.String(128), nullable=True),
        sa.Column("mementos_conversation_id", sa.String(128), nullable=True),
        sa.Column("mementos_run_id", sa.String(128), nullable=True),
        sa.Column("mementos_latest_seq", sa.Integer, nullable=True),
        sa.Column("mementos_workspace_dir", sa.Text, nullable=True),
        sa.Column("progress_summary", sa.Text, nullable=True),
        sa.Column("progress_events", sa.JSON, nullable=True),
        sa.Column("result_text", sa.Text, nullable=True),
        sa.Column("result_payload", sa.JSON, nullable=True),
        sa.Column("artifact_paths", sa.JSON, nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("error_payload", sa.JSON, nullable=True),
        sa.Column("callback_subject", sa.String(256), nullable=True),
        sa.Column(
            "callback_status",
            sa.String(24),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("callback_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("callback_last_error", sa.Text, nullable=True),
        sa.Column("callback_delivered_at", sa.DateTime, nullable=True),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("lease_until", sa.DateTime, nullable=True),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime, nullable=True),
        sa.Column("external_status", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("submitted_at", sa.DateTime, nullable=True),
        sa.Column("last_progress_at", sa.DateTime, nullable=True),
        sa.Column("last_polled_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_long_tasks_provider", "long_tasks", ["provider"])
    op.create_index("ix_long_tasks_status", "long_tasks", ["status"])
    op.create_index("ix_long_tasks_tenant_id", "long_tasks", ["tenant_id"])
    op.create_index("ix_long_tasks_user_id", "long_tasks", ["user_id"])
    op.create_index(
        "ix_long_tasks_agent_instance_id",
        "long_tasks",
        ["agent_instance_id"],
    )
    op.create_index("ix_long_tasks_conversation_id", "long_tasks", ["conversation_id"])
    op.create_index("ix_long_tasks_turn_id", "long_tasks", ["turn_id"])
    op.create_index("ix_long_tasks_trace_id", "long_tasks", ["trace_id"])
    op.create_index("ix_long_tasks_session_key", "long_tasks", ["session_key"])
    op.create_index("ix_long_tasks_task_date", "long_tasks", ["task_date"])
    op.create_index("ix_long_tasks_task_type", "long_tasks", ["task_type"])
    op.create_index(
        "ix_long_tasks_mementos_session_id",
        "long_tasks",
        ["mementos_session_id"],
    )
    op.create_index(
        "ix_long_tasks_mementos_conversation_id",
        "long_tasks",
        ["mementos_conversation_id"],
    )
    op.create_index("ix_long_tasks_mementos_run_id", "long_tasks", ["mementos_run_id"])
    op.create_index("ix_long_tasks_callback_status", "long_tasks", ["callback_status"])
    op.create_index("ix_long_tasks_worker_id", "long_tasks", ["worker_id"])
    op.create_index("ix_long_tasks_lease_until", "long_tasks", ["lease_until"])
    op.create_index("ix_long_tasks_next_retry_at", "long_tasks", ["next_retry_at"])
    op.create_index("ix_long_tasks_external_status", "long_tasks", ["external_status"])
    op.create_index("ix_long_tasks_created_at", "long_tasks", ["created_at"])
    op.create_index("ix_long_tasks_updated_at", "long_tasks", ["updated_at"])
    op.create_index(
        "ix_long_tasks_user_created",
        "long_tasks",
        ["tenant_id", "user_id", "created_at"],
    )
    op.create_index(
        "ix_long_tasks_status_updated",
        "long_tasks",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_long_tasks_session_created",
        "long_tasks",
        ["session_key", "created_at"],
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_session_created")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_status_updated")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_user_created")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_updated_at")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_created_at")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_external_status")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_next_retry_at")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_lease_until")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_worker_id")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_callback_status")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_mementos_run_id")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_mementos_conversation_id")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_mementos_session_id")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_task_type")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_task_date")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_session_key")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_trace_id")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_turn_id")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_conversation_id")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_agent_instance_id")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_user_id")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_tenant_id")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_status")
    op.execute("DROP INDEX IF EXISTS ix_long_tasks_provider")
    op.execute("DROP TABLE IF EXISTS long_tasks")
