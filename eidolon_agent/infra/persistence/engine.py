"""Async SQLAlchemy engine + session factory for agent-owned persistence.

The generic SQLite URL/session/PRAGMA setup lives in ``eidolon_sdk.core.db``; this
module keeps only agent-specific schema bootstrap and compatibility repairs.
"""

from __future__ import annotations

from eidolon_sdk.core.db import (
    SqliteSettings as SdkSqliteSettings,
)
from eidolon_sdk.core.db import (
    create_sqlite_engine,
    create_sqlite_session_factory,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from eidolon_agent.config.settings import SqliteSettings
from eidolon_agent.infra.persistence.models import Base


def _sdk_settings(settings: SqliteSettings) -> SdkSqliteSettings:
    return SdkSqliteSettings(
        path=settings.path,
        enable_wal=settings.enable_wal,
        busy_timeout_ms=settings.busy_timeout_ms,
        synchronous=settings.journal_synchronous,
    )


def create_engine(settings: SqliteSettings) -> AsyncEngine:
    """Create the global :class:`AsyncEngine` for the agent DB."""
    return create_sqlite_engine(_sdk_settings(settings))


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sqlite_session_factory(engine)


async def ensure_schema(engine: AsyncEngine) -> None:
    """Create all tables if they don't exist.

    Used in tests + dev bootstrap. Production must run Alembic migrations.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_long_tasks_schema(conn)


async def _ensure_long_tasks_schema(conn) -> None:
    """Patch dev DBs created by an earlier long_tasks draft.

    ``create_all`` intentionally does not mutate existing tables. During local
    iteration the first long_tasks table may predate worker/lease/poll columns,
    so keep a narrow additive repair here. Real deployments should still use
    Alembic for versioned migrations.
    """

    result = await conn.execute(text("PRAGMA table_info(long_tasks)"))
    existing = {str(row[1]) for row in result.fetchall()}
    if not existing:
        return

    columns = {
        "worker_id": "VARCHAR(128)",
        "lease_until": "DATETIME",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "next_retry_at": "DATETIME",
        "external_status": "VARCHAR(64)",
        "last_polled_at": "DATETIME",
        "result_tts_summary": "TEXT",
        "device_id": "VARCHAR(64)",
    }
    for name, ddl in columns.items():
        if name not in existing:
            await conn.execute(text(f"ALTER TABLE long_tasks ADD COLUMN {name} {ddl}"))

    indexes = {
        "ix_long_tasks_worker_id": "worker_id",
        "ix_long_tasks_lease_until": "lease_until",
        "ix_long_tasks_next_retry_at": "next_retry_at",
        "ix_long_tasks_external_status": "external_status",
        "ix_long_tasks_device_id": "device_id",
    }
    for name, column in indexes.items():
        await conn.execute(
            text(f"CREATE INDEX IF NOT EXISTS {name} ON long_tasks ({column})")
        )
