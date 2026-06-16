"""long task TTS summary

Revision ID: 0007_long_task_tts_summary
Revises: 0006_persona_evolution_observations
Create Date: 2026-06-16 00:00:00 UTC
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_long_task_tts_summary"
down_revision = "0006_persona_evolution_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("long_tasks", sa.Column("result_tts_summary", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("long_tasks", "result_tts_summary")
