"""Ports used by the personas module.

Concrete runtime adapters live outside the module so personas can be tested in
isolation with mocks.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from eidolon_agent.core.types.memory import MemoryQueryPlan, MemoryRecallResult
from eidolon_agent.domain.personas.types import (
    PersonaEvolutionResult,
    PersonaInstance,
    PersonaTemplate,
)


@runtime_checkable
class PersonaMemoryPort(Protocol):
    async def recall_context(
        self,
        user_id: str,
        query: str,
        *,
        plan: MemoryQueryPlan,
        timeout_s: float = 0.2,
    ) -> MemoryRecallResult:
        ...


@runtime_checkable
class PersonaLLMPort(Protocol):
    async def summarize(self, prompt: str, *, request_id: str) -> str:
        ...


@runtime_checkable
class PersonaEventPort(Protocol):
    async def publish_persona_updated(self, instance_id: str, payload: dict) -> None:
        ...

    async def publish_evolution_applied(self, instance_id: str, payload: dict) -> None:
        ...


@runtime_checkable
class PersonaAuditPort(Protocol):
    async def record_evolution(self, result: PersonaEvolutionResult) -> None:
        ...


@runtime_checkable
class PersonaInstanceStore(Protocol):
    """Persistence boundary for per-user persona instance copies.

    Two implementations exist:
      * ``YamlPersonaInstanceStore`` (legacy / migration source) reads one
        YAML file per instance from ``settings.persona.instances_dir``.
      * ``SqlPersonaInstanceStore`` (production) wraps a SQLAlchemy
        ``async_sessionmaker`` and stores instances as JSON blobs in the
        ``persona_instances`` table; supports single-TX evolution writes.

    Both implementations are async so the service layer can call them without
    knowing the backing store.
    """

    async def exists(self, tenant_id: str, user_id: str, instance_id: str) -> bool: ...

    async def load(
        self, tenant_id: str, user_id: str, instance_id: str
    ) -> PersonaInstance: ...

    async def save(self, instance: PersonaInstance, *, reason: str = "") -> None: ...

    async def create_from_template(
        self,
        *,
        template: PersonaTemplate,
        tenant_id: str,
        user_id: str,
        instance_id: str,
    ) -> PersonaInstance: ...

    async def list_all(self) -> list[PersonaInstance]:
        """List every instance across tenants/users — used by the admin UI."""
        ...

    async def delete(self, tenant_id: str, user_id: str, instance_id: str) -> None: ...


@runtime_checkable
class PersonaEvolutionRepository(Protocol):
    """SQLite-backed repository for evolution history.

    Lives in the personas module (not core/ports) because its signature
    references the persona-domain type ``PersonaEvolutionResult``. Concrete
    impl is wired by ``infra/persistence`` (currently ``persistence/``).
    """

    async def record(self, result: PersonaEvolutionResult) -> None: ...

    async def list_for_instance(
        self, instance_id: str, *, limit: int = 50
    ) -> list[PersonaEvolutionResult]: ...

    async def get(self, delta_id: str) -> PersonaEvolutionResult | None: ...


class NullPersonaEventPort:
    async def publish_persona_updated(self, instance_id: str, payload: dict) -> None:
        return None

    async def publish_evolution_applied(self, instance_id: str, payload: dict) -> None:
        return None


class NullPersonaAuditPort:
    async def record_evolution(self, result: PersonaEvolutionResult) -> None:
        return None
