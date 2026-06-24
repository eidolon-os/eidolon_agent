"""long task device_id

Denormalize the owning device's id onto long_tasks (Phase 3 proactive wake): the
identity dimension already lives first-class on turns.device_id (indexed); copy
it onto the task at creation so the proactive worker can route a finished report
to the right device (hub send_command(room.join)) without a turn join, and so
Phase 4 offline buffering / admin can query long_tasks by device_id. Immutable
fact (the device that owned the task) → no denorm-staleness risk.

Revision ID: 0008_long_task_device_id
Revises: 0007_long_task_tts_summary
Create Date: 2026-06-22 00:00:00 UTC
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_long_task_device_id"
down_revision = "0007_long_task_tts_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("long_tasks", sa.Column("device_id", sa.String(length=64), nullable=True))
    op.create_index("ix_long_tasks_device_id", "long_tasks", ["device_id"])


def downgrade() -> None:
    op.drop_index("ix_long_tasks_device_id", table_name="long_tasks")
    op.drop_column("long_tasks", "device_id")
