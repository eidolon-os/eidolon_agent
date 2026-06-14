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
from eidolon_agent.infra.persistence.long_task_store import SqlLongTaskStore
from eidolon_agent.infra.persistence.sql_evolution_history_store import (
    SqlEvolutionHistoryStore,
)
from eidolon_agent.infra.persistence.repositories import (
    SqlChatMessageRepository,
    SqlConversationRepository,
    SqlLongTaskRepository,
)
from eidolon_agent.infra.persistence.sql_custom_template_store import (
    CustomTemplateAlreadyExists,
    CustomTemplateNotFound,
    SqlCustomTemplateStore,
)
from eidolon_agent.infra.persistence.sql_persona_instance_store import (
    SqlPersonaInstanceStore,
)
from eidolon_agent.infra.persistence.turn_io import build_history_hydrator, build_turn_persister
from eidolon_agent.infra.persistence.unit_of_work import SqlAlchemyUnitOfWork

__all__ = [
    "SqlAlchemyUnitOfWork",
    "CustomTemplateAlreadyExists",
    "CustomTemplateNotFound",
    "SqlChatMessageRepository",
    "SqlConversationRepository",
    "SqlCustomTemplateStore",
    "SqlEvolutionHistoryStore",
    "SqlLongTaskRepository",
    "SqlLongTaskStore",
    "SqlPersonaInstanceStore",
    "build_history_hydrator",
    "build_turn_persister",
    "create_engine",
    "create_session_factory",
    "ensure_schema",
]
