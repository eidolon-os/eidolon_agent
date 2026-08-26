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
    #: The last time anything addressed this Companion — a turn, a signal, any
    #: resolve. Not "the last time it spoke": what a person wants to know from a
    #: list of their Eidolons is which ones are in use, and being spoken *to* is
    #: as much use as speaking.
    #:
    #: Maintained in :meth:`AgentRegistry.resolve_runtime` rather than at the
    #: call sites, so a new way of addressing a Companion cannot forget to keep
    #: it true. It was a field nobody wrote for as long as nobody read it.
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

    def for_owner(self, owner_id: str) -> list[AgentInstance]:
        """This Owner's live runtimes, newest use first.

        Filtering here rather than at a route: the registry holds every Owner on
        this Host, and a caller assembling its own comprehension over
        :meth:`list_instances` is one typo away from showing somebody else's
        Eidolons. There is one place that can leak, so there is one place to get
        right.

        What this answers is "which of this Owner's Companions does this Host
        have a live runtime for", which is a genuine per-Companion fact and can
        be several at once. What it does **not** answer is whether any body is
        reachable — nothing on this Host tracks device presence, and reading
        this as "online" would be the same guess the home screen used to make.
        """

        return sorted(
            (inst for inst in self._instances.values() if inst.owner_id == owner_id),
            key=lambda inst: inst.last_active_at or inst.created_at,
            reverse=True,
        )

    async def resolve_runtime(
        self,
        *,
        owner_id: str,
        companion_id: str,
        genome_id: str | None = None,
    ) -> AgentInstance:
        resolved_genome_id = (genome_id or "").strip()
        if not resolved_genome_id:
            raise NotFoundError("runtime binding does not pin a persona genome")
        key = f"{owner_id}:{companion_id}:{resolved_genome_id}"
        inst = self._instances.get(key)
        if inst is not None:
            inst.last_active_at = datetime.now(timezone.utc)
            return inst
        async with self._lock:
            inst = self._instances.get(key)
            if inst is not None:
                inst.last_active_at = datetime.now(timezone.utc)
                return inst
            now = datetime.now(timezone.utc)
            inst = AgentInstance(
                owner_id=owner_id,
                companion_id=companion_id,
                genome_id=resolved_genome_id,
                created_at=now,
                last_active_at=now,
            )
            inst.agent = await self._factory(inst)
            self._instances[key] = inst
            return inst
