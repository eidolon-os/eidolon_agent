"""AgentRegistry keyed by companion runtime identity."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from eidolon_agent.core.errors import NotFoundError
from eidolon_agent.domain.agent.companion import CompanionAgent


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
    ) -> None:
        self._instances: dict[str, AgentInstance] = {}
        self._lock = asyncio.Lock()
        self._factory = instance_factory

    def list_instances(self) -> list[AgentInstance]:
        return list(self._instances.values())

    async def resolve_for_caller(
        self,
        *,
        owner_id: str,
        companion_id: str,
        genome_id: str | None = None,
    ) -> AgentInstance:
        resolved_genome_id = (genome_id or "").strip()
        if not resolved_genome_id:
            raise NotFoundError("runtime identity does not pin a persona genome")
        key = f"{owner_id}:{companion_id}:{resolved_genome_id}"
        inst = self._instances.get(key)
        if inst is not None:
            return inst
        async with self._lock:
            inst = self._instances.get(key)
            if inst is not None:
                return inst
            inst = AgentInstance(
                owner_id=owner_id,
                companion_id=companion_id,
                genome_id=resolved_genome_id,
            )
            inst.agent = await self._factory(inst)
            self._instances[key] = inst
            return inst
