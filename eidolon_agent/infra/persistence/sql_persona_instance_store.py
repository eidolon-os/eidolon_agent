"""SQL-backed ``PersonaInstanceStore`` — production storage for instances.

Each call opens a short-lived transaction. Evolution writes (worker /
admin rollback) go through the same path so the row update + audit append
land in a single TX, leaving no half-applied state on crash. Templates
remain on disk — only the per-user overlay is stored here.

Layering note: this lives in ``infra/`` and is the only place that bridges
the personas domain to SQLAlchemy. The store satisfies the
``PersonaInstanceStore`` Protocol declared in ``domain/personas/ports.py``
so the domain layer never sees a SQLAlchemy import.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eidolon_agent.domain.personas.types import (
    PersonaEvolutionResult,
    PersonaInstance,
    PersonaTemplate,
)
from eidolon_agent.infra.persistence.repositories import (
    SqlEvolutionHistoryRepository,
    SqlPersonaInstanceRepository,
)


class SqlPersonaInstanceStore:
    """Async ``PersonaInstanceStore`` backed by the ``persona_instances`` table."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def exists(self, tenant_id: str, user_id: str, instance_id: str) -> bool:
        async with self._session_factory() as session:
            repo = SqlPersonaInstanceRepository(session)
            return await repo.get(tenant_id, user_id, instance_id) is not None

    async def load(
        self, tenant_id: str, user_id: str, instance_id: str
    ) -> PersonaInstance:
        async with self._session_factory() as session:
            repo = SqlPersonaInstanceRepository(session)
            return await repo.load(tenant_id, user_id, instance_id)

    async def save(self, instance: PersonaInstance, *, reason: str = "") -> None:
        async with self._session_factory() as session, session.begin():
            repo = SqlPersonaInstanceRepository(session)
            # Mark active only when the caller is the hot path / evolution
            # worker; the migration script passes reason="migration" to
            # avoid bumping last_active_at on import.
            mark_active = reason != "migration"
            await repo.upsert(instance, mark_active=mark_active)

    async def create_from_template(
        self,
        *,
        template: PersonaTemplate,
        tenant_id: str,
        user_id: str,
        instance_id: str,
    ) -> PersonaInstance:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        instance = PersonaInstance(
            instance_id=instance_id,
            tenant_id=tenant_id,
            user_id=user_id,
            origin_template_id=template.metadata.template_id,
            origin_template_revision=template.metadata.template_revision,
            overlay_version=1,
            created_at=now,
            updated_at=now,
            metadata=template.metadata,
            identity_core=template.identity_core,
            behavioral_knobs=template.behavioral_knobs,
            style_compiler=template.style_compiler,
            memory_adapter=template.memory_adapter,
            evolution_rules=template.evolution_rules,
            assets=template.assets,
        )
        await self.save(instance, reason="create_from_template")
        return instance

    async def list_all(self) -> list[PersonaInstance]:
        async with self._session_factory() as session:
            repo = SqlPersonaInstanceRepository(session)
            return await repo.list_all()

    async def delete(self, tenant_id: str, user_id: str, instance_id: str) -> None:
        async with self._session_factory() as session, session.begin():
            repo = SqlPersonaInstanceRepository(session)
            await repo.delete(tenant_id, user_id, instance_id)

    # ---- Single-TX evolution write -------------------------------------

    async def save_with_history(
        self,
        instance: PersonaInstance,
        result: PersonaEvolutionResult,
    ) -> None:
        """Persist a new overlay + append a row to ``evolution_history`` in one TX.

        This is the single-writer entry point used by the evolution worker and
        by admin-initiated evolution. A crash between the two writes is
        impossible — both rows land together or neither does.
        """
        async with self._session_factory() as session, session.begin():
            inst_repo = SqlPersonaInstanceRepository(session)
            hist_repo = SqlEvolutionHistoryRepository(session)
            await inst_repo.upsert(instance, mark_active=True)
            await hist_repo.record(result)


__all__ = ["SqlPersonaInstanceStore"]
