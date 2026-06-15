"""SQL-backed persona observations and evolution proposal stores."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eidolon_agent.domain.personas.types import (
    PersonaEvolutionProposal,
    PersonaObservation,
)
from eidolon_agent.infra.persistence.repositories import (
    SqlPersonaEvolutionProposalRepository,
    SqlPersonaObservationRepository,
)


class SqlPersonaObservationStore:
    """Repository adapter for durable persona evolution evidence."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def add(self, observation: PersonaObservation) -> None:
        async with self._session_factory() as session, session.begin():
            repo = SqlPersonaObservationRepository(session)
            await repo.add(observation)

    async def list_for_instance(
        self,
        instance_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[PersonaObservation]:
        async with self._session_factory() as session:
            repo = SqlPersonaObservationRepository(session)
            return await repo.list_for_instance(
                instance_id,
                status=status,
                limit=limit,
            )

    async def get(self, observation_id: str) -> PersonaObservation | None:
        async with self._session_factory() as session:
            repo = SqlPersonaObservationRepository(session)
            return await repo.get(observation_id)

    async def set_status(self, observation_id: str, status: str) -> None:
        async with self._session_factory() as session, session.begin():
            repo = SqlPersonaObservationRepository(session)
            await repo.set_status(observation_id, status)


class SqlPersonaEvolutionProposalStore:
    """Repository adapter for admin-reviewable evolution proposals."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def add(self, proposal: PersonaEvolutionProposal) -> None:
        async with self._session_factory() as session, session.begin():
            repo = SqlPersonaEvolutionProposalRepository(session)
            await repo.add(proposal)

    async def save(self, proposal: PersonaEvolutionProposal) -> None:
        async with self._session_factory() as session, session.begin():
            repo = SqlPersonaEvolutionProposalRepository(session)
            await repo.save(proposal)

    async def list_for_instance(
        self,
        instance_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[PersonaEvolutionProposal]:
        async with self._session_factory() as session:
            repo = SqlPersonaEvolutionProposalRepository(session)
            return await repo.list_for_instance(
                instance_id,
                status=status,
                limit=limit,
            )

    async def get(self, proposal_id: str) -> PersonaEvolutionProposal | None:
        async with self._session_factory() as session:
            repo = SqlPersonaEvolutionProposalRepository(session)
            return await repo.get(proposal_id)


__all__ = ["SqlPersonaEvolutionProposalStore", "SqlPersonaObservationStore"]
