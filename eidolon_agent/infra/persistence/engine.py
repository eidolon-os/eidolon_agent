"""Async SQLAlchemy engine + session factory + SQLite pragmas.

Centralises SQLite-specific tuning (WAL, busy_timeout, foreign_keys=ON). Any
caller who imports this module will get a properly-configured engine without
needing to know the pragmas.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.event import listens_for
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from eidolon_agent.config.settings import SqliteSettings
from eidolon_agent.infra.persistence.models import Base


def _build_url(path: Path | str) -> str:
    path_str = str(path) if isinstance(path, Path) else path
    if path_str == ":memory:":
        return "sqlite+aiosqlite:///:memory:"
    p = Path(path_str).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{p}"


def create_engine(settings: SqliteSettings) -> AsyncEngine:
    """Create the global :class:`AsyncEngine`.

    Pragmas are applied at the synchronous DBAPI layer because aiosqlite shares
    the same underlying ``sqlite3`` connection — they propagate to async use.
    """
    url = _build_url(settings.path)
    in_memory = url.endswith(":memory:")
    kwargs: dict = {"future": True, "echo": False}
    if in_memory:
        # Share the single in-memory DB across all "connections" (test mode).
        kwargs.update(connect_args={"check_same_thread": False}, poolclass=StaticPool)
    engine = create_async_engine(url, **kwargs)

    @listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_conn, _connection_record):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA foreign_keys = ON")
            cur.execute(f"PRAGMA busy_timeout = {settings.busy_timeout_ms}")
            if settings.enable_wal and not in_memory:
                cur.execute("PRAGMA journal_mode = WAL")
            cur.execute(f"PRAGMA synchronous = {settings.journal_synchronous}")
        finally:
            cur.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


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
    }
    for name, ddl in columns.items():
        if name not in existing:
            await conn.execute(text(f"ALTER TABLE long_tasks ADD COLUMN {name} {ddl}"))

    indexes = {
        "ix_long_tasks_worker_id": "worker_id",
        "ix_long_tasks_lease_until": "lease_until",
        "ix_long_tasks_next_retry_at": "next_retry_at",
        "ix_long_tasks_external_status": "external_status",
    }
    for name, column in indexes.items():
        await conn.execute(
            text(f"CREATE INDEX IF NOT EXISTS {name} ON long_tasks ({column})")
        )
