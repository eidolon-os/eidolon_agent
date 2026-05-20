"""SQLite persistence via SQLAlchemy 2.0 async.

The single source of truth for *durable, queryable* state. Volatile / short-TTL
state lives in NATS KV (see :mod:`eidolon_agent.events`); semantic memory
lives in eidolon-memory (see :mod:`eidolon_agent.memory`).

Schema migrations are managed by Alembic (``persistence/migrations``).
"""

from eidolon_agent.persistence.engine import (
    create_engine,
    create_session_factory,
    ensure_schema,
)
from eidolon_agent.persistence.unit_of_work import SqlAlchemyUnitOfWork

__all__ = [
    "SqlAlchemyUnitOfWork",
    "create_engine",
    "create_session_factory",
    "ensure_schema",
]
