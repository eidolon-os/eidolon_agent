"""SQLite persistence via SQLAlchemy 2.0 async.

The single source of truth for *durable, queryable* state. Volatile / short-TTL
state lives in NATS KV (see :mod:`eidolon_agent.infra.events`); semantic memory
lives in eidolon-memory (see :mod:`eidolon_agent.infra.memory`).

Schema migrations are managed by Alembic (``persistence/migrations``).
"""

from eidolon_agent.infra.persistence.engine import (
    create_engine,
    create_session_factory,
    ensure_schema,
)
from eidolon_agent.infra.persistence.sql_evolution_history_store import (
    SqlEvolutionHistoryStore,
)
from eidolon_agent.infra.persistence.sql_persona_instance_store import (
    SqlPersonaInstanceStore,
)
from eidolon_agent.infra.persistence.unit_of_work import SqlAlchemyUnitOfWork

__all__ = [
    "SqlAlchemyUnitOfWork",
    "SqlEvolutionHistoryStore",
    "SqlPersonaInstanceStore",
    "create_engine",
    "create_session_factory",
    "ensure_schema",
]
