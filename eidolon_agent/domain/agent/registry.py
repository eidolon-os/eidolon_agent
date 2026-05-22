"""AgentRegistry — session-keyed map of CompanionAgents.

After the Phase 4 simplification: one agent per (tenant, user) pair, built
lazily on first ``resolve_for_caller``. No admin RPCs to manually
start/stop instances; the bootstrap chooses the default template, and any
unrecognised caller spins up an agent on demand.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from eidolon_agent.core.errors import NotFoundError
from eidolon_agent.domain.agent.companion import CompanionAgent


@dataclass(frozen=True, slots=True)
class AgentTemplate:
    """Static catalog entry. One per available persona archetype."""

    template_id: str  # matches PersonaTemplate.template_id
    name: str
    description: str = ""


@dataclass(slots=True)
class AgentInstance:
    instance_id: str
    template_id: str
    tenant_id: str
    user_id: str
    nickname_alias: str | None = None
    status: str = "active"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_active_at: datetime | None = None
    agent: CompanionAgent | None = None


class AgentRegistry:
    """Per-(tenant, user) CompanionAgent registry.

    ``resolve_for_caller`` is the single public entry point; missing
    instances are created on demand via ``instance_factory``.
    """

    def __init__(
        self,
        *,
        instance_factory,  # Callable[[AgentInstance], Awaitable[CompanionAgent]]
        default_template_id: str,
    ) -> None:
        self._templates: dict[str, AgentTemplate] = {}
        self._instances: dict[str, AgentInstance] = {}  # key: tenant_id/user_id
        self._lock = asyncio.Lock()
        self._factory = instance_factory
        self._default_template_id = default_template_id

    # ---- templates -----------------------------------------------------------

    def register_template(self, tpl: AgentTemplate) -> None:
        self._templates[tpl.template_id] = tpl

    def list_templates(self) -> list[AgentTemplate]:
        return list(self._templates.values())

    def get_template(self, template_id: str) -> AgentTemplate:
        try:
            return self._templates[template_id]
        except KeyError as exc:
            raise NotFoundError(f"agent template: {template_id}") from exc

    # ---- instances -----------------------------------------------------------

    def list_instances(self) -> list[AgentInstance]:
        return list(self._instances.values())

    async def resolve_for_caller(
        self,
        *,
        tenant_id: str,
        user_id: str,
        template_id: str | None = None,
    ) -> AgentInstance:
        """Look up or lazily create the agent for a (tenant, user) pair.

        ``template_id`` is honoured only on first creation; existing instances
        keep whatever template they were built with.
        """
        key = f"{tenant_id}/{user_id}"
        inst = self._instances.get(key)
        if inst is not None:
            return inst
        async with self._lock:
            inst = self._instances.get(key)
            if inst is not None:
                return inst
            tpl_id = template_id or self._default_template_id
            self.get_template(tpl_id)  # raises NotFoundError if not registered
            inst = AgentInstance(
                instance_id=f"inst_{uuid.uuid4().hex[:12]}",
                template_id=tpl_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            inst.agent = await self._factory(inst)
            self._instances[key] = inst
            return inst
