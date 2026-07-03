"""Ports used by the personas module.

Concrete runtime adapters live outside the module so personas can be tested in
isolation with mocks.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from eidolon_agent.domain.personas.types import (
    CompanionPersona,
    PersonaEvolutionProposal,
    PersonaEvolutionResult,
    PersonaObservation,
    PersonaTemplate,
)


@runtime_checkable
class PersonaLLMPort(Protocol):
    async def summarize(self, prompt: str, *, request_id: str) -> str:
        ...


@runtime_checkable
class PersonaEventPort(Protocol):
    async def publish_persona_updated(self, companion_id: str, payload: dict) -> None:
        ...

    async def publish_evolution_applied(self, companion_id: str, payload: dict) -> None:
        ...


@runtime_checkable
class PersonaAuditPort(Protocol):
    async def record_evolution(self, result: PersonaEvolutionResult) -> None:
        ...


@runtime_checkable
class CompanionPersonaStore(Protocol):
    """Persistence boundary for per-owner companion persona copies.

    Implementations include:
      * ``YamlCompanionPersonaStore`` (legacy / migration source) reads one
        YAML file per companion from ``settings.persona.instances_dir``.
      * ``EidolonDataCompanionPersonaStore`` (production) stores versioned
        personas as persona genomes owned by ``eidolon_data``.

    Both implementations are async so the service layer can call them without
    knowing the backing store.
    """

    async def exists(self, owner_id: str, companion_id: str) -> bool: ...

    async def load(self, owner_id: str, companion_id: str) -> CompanionPersona: ...

    async def save(self, persona: CompanionPersona, *, reason: str = "") -> None: ...

    async def create_from_template(
        self,
        *,
        template: PersonaTemplate,
        owner_id: str,
        companion_id: str,
    ) -> CompanionPersona: ...

    async def list_all(self) -> list[CompanionPersona]:
        """List every companion persona across owners — used by the admin UI."""
        ...

    async def delete(self, owner_id: str, companion_id: str) -> None: ...


@runtime_checkable
class PersonaEvolutionRepository(Protocol):
    """SQLite-backed repository for evolution history.

    Lives in the personas module (not core/ports) because its signature
    references the persona-domain type ``PersonaEvolutionResult``. Concrete
    impl is wired by ``infra/persistence`` (currently ``persistence/``).
    """

    async def record(self, result: PersonaEvolutionResult) -> None: ...

    async def list_for_instance(
        self, companion_id: str, *, limit: int = 50
    ) -> list[PersonaEvolutionResult]: ...

    async def get(self, delta_id: str) -> PersonaEvolutionResult | None: ...


@runtime_checkable
class PersonaObservationRepository(Protocol):
    async def add(self, observation: PersonaObservation) -> None: ...

    async def list_for_instance(
        self,
        companion_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[PersonaObservation]: ...

    async def get(self, observation_id: str) -> PersonaObservation | None: ...

    async def set_status(self, observation_id: str, status: str) -> None: ...


@runtime_checkable
class PersonaEvolutionProposalRepository(Protocol):
    async def add(self, proposal: PersonaEvolutionProposal) -> None: ...

    async def save(self, proposal: PersonaEvolutionProposal) -> None: ...

    async def list_for_instance(
        self,
        companion_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[PersonaEvolutionProposal]: ...

    async def get(self, proposal_id: str) -> PersonaEvolutionProposal | None: ...


class NullPersonaEventPort:
    async def publish_persona_updated(self, companion_id: str, payload: dict) -> None:
        return None

    async def publish_evolution_applied(self, companion_id: str, payload: dict) -> None:
        return None


class NullPersonaAuditPort:
    async def record_evolution(self, result: PersonaEvolutionResult) -> None:
        return None
