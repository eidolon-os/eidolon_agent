"""SQL-backed evolution-history store — audit writer + repository reader.

A single adapter that satisfies both Protocols declared in
``domain/personas/ports.py``:

  * ``PersonaAuditPort.record_evolution`` (write side, used by the worker
    + admin-initiated evolution + rollback)
  * ``PersonaEvolutionRepository`` (read side, used by the admin UI to
    paginate the per-instance audit timeline)

The write path opens a fresh transaction per call; the read path uses a
session-per-query pattern (no shared state, safe under concurrent admin
requests). Tests can substitute ``NullPersonaAuditPort`` and ``None`` for
the repository — the service degrades gracefully.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eidolon_agent.domain.personas.types import PersonaEvolutionResult
from eidolon_agent.infra.persistence.repositories import SqlEvolutionHistoryRepository


class SqlEvolutionHistoryStore:
    """Audit + repository adapter — see module docstring."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    # ---- PersonaAuditPort ----------------------------------------------

    async def record_evolution(self, result: PersonaEvolutionResult) -> None:
        async with self._session_factory() as session, session.begin():
            repo = SqlEvolutionHistoryRepository(session)
            await repo.record(result)

    # ---- PersonaEvolutionRepository ------------------------------------

    async def record(self, result: PersonaEvolutionResult) -> None:
        await self.record_evolution(result)

    async def list_for_instance(
        self, instance_id: str, *, limit: int = 50
    ) -> list[PersonaEvolutionResult]:
        async with self._session_factory() as session:
            repo = SqlEvolutionHistoryRepository(session)
            return await repo.list_for_instance(instance_id, limit=limit)

    async def get(self, delta_id: str) -> PersonaEvolutionResult | None:
        async with self._session_factory() as session:
            repo = SqlEvolutionHistoryRepository(session)
            return await repo.get(delta_id)


__all__ = ["SqlEvolutionHistoryStore"]
