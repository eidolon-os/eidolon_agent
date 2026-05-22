"""Ports used by the personas module.

Concrete runtime adapters live outside the module so personas can be tested in
isolation with mocks.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from eidolon_agent.core.types.memory import MemoryHit, MemoryQueryPlan
from eidolon_agent.personas.types import PersonaEvolutionResult


@runtime_checkable
class PersonaMemoryPort(Protocol):
    async def recall_context(
        self,
        user_id: str,
        query: str,
        *,
        plan: MemoryQueryPlan,
        timeout_s: float = 0.2,
    ) -> tuple[str, list[MemoryHit], bool]:
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


class NullPersonaEventPort:
    async def publish_persona_updated(self, instance_id: str, payload: dict) -> None:
        return None

    async def publish_evolution_applied(self, instance_id: str, payload: dict) -> None:
        return None


class NullPersonaAuditPort:
    async def record_evolution(self, result: PersonaEvolutionResult) -> None:
        return None
