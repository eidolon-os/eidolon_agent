"""AgentRegistry keyed by companion runtime identity."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from eidolon_agent.core.errors import NotFoundError
from eidolon_agent.domain.agent.companion import CompanionAgent


@dataclass(frozen=True, slots=True)
class AgentTemplate:
    """Static catalog entry. One per available persona archetype/genome family."""

    genome_id: str
    name: str
    description: str = ""


@dataclass(slots=True)
class AgentInstance:
    owner_id: str
    companion_id: str
    genome_id: str
    nickname_alias: str | None = None
    status: str = "active"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_active_at: datetime | None = None
    agent: CompanionAgent | None = None


class AgentRegistry:
    """Companion runtime registry.

    The token already carries owner, companion, and genome ids. The registry
    therefore never chooses an active companion for an owner.
    """

    def __init__(
        self,
        *,
        instance_factory,
        default_genome_id: str = "",
    ) -> None:
        self._templates: dict[str, AgentTemplate] = {}
        self._instances: dict[str, AgentInstance] = {}
        self._lock = asyncio.Lock()
        self._factory = instance_factory
        self._default_genome_id = default_genome_id

    def register_template(self, tpl: AgentTemplate) -> None:
        self._templates[tpl.genome_id] = tpl

    def list_templates(self) -> list[AgentTemplate]:
        return list(self._templates.values())

    def get_template(self, genome_id: str) -> AgentTemplate:
        try:
            return self._templates[genome_id]
        except KeyError as exc:
            raise NotFoundError(f"agent genome: {genome_id}") from exc

    def list_instances(self) -> list[AgentInstance]:
        return list(self._instances.values())

    async def resolve_for_caller(
        self,
        *,
        owner_id: str,
        companion_id: str,
        genome_id: str | None = None,
    ) -> AgentInstance:
        resolved_genome_id = genome_id or self._default_genome_id
        key = f"{companion_id}:{resolved_genome_id}"
        inst = self._instances.get(key)
        if inst is not None:
            return inst
        async with self._lock:
            inst = self._instances.get(key)
            if inst is not None:
                return inst
            self.get_template(resolved_genome_id)
            inst = AgentInstance(
                owner_id=owner_id,
                companion_id=companion_id,
                genome_id=resolved_genome_id,
            )
            inst.agent = await self._factory(inst)
            self._instances[key] = inst
            return inst
