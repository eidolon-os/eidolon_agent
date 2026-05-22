"""Module-local fixtures: in-memory SQLite UoW factory."""

from __future__ import annotations

import pytest

from eidolon_agent.config.settings import SqliteSettings
from eidolon_agent.infra.persistence import (
    SqlAlchemyUnitOfWork,
    create_engine,
    create_session_factory,
    ensure_schema,
)


@pytest.fixture
async def uow_factory():
    eng = create_engine(SqliteSettings(path=":memory:"))
    await ensure_schema(eng)
    sf = create_session_factory(eng)

    def _factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(sf)

    yield _factory
    await eng.dispose()
