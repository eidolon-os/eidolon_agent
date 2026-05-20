"""Async SQLAlchemy engine + session factory + SQLite pragmas.

Centralises SQLite-specific tuning (WAL, busy_timeout, foreign_keys=ON). Any
caller who imports this module will get a properly-configured engine without
needing to know the pragmas.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.event import listens_for
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from eidolon_agent.config.settings import SqliteSettings
from eidolon_agent.persistence.models import Base


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
