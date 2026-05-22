"""AgentRegistry — runtime catalog of AgentTemplates and live AgentInstances.

Templates declare a Persona template_id + tool whitelist + model preferences.
Instances bind a template to a (tenant, user, instance_id) tuple at start time.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from eidolon_agent.core.errors import ConflictError, NotFoundError
from eidolon_agent.domain.agent.companion import CompanionAgent


@dataclass(frozen=True, slots=True)
class AgentTemplate:
    """Static catalog entry. One per available persona archetype."""

    template_id: str  # matches PersonaTemplate.template_id
    name: str
    description: str = ""
    tool_whitelist: tuple[str, ...] = ()
    proactive_enabled: bool = True


@dataclass(slots=True)
class AgentInstance:
    instance_id: str
    template_id: str
    tenant_id: str
    user_id: str
    nickname_alias: str | None
    status: str = "active"  # active | stopped | degraded
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_active_at: datetime | None = None
    agent: CompanionAgent | None = None  # wired by factory


class AgentRegistry:
    """Holds templates + live instances. The factory callable builds CompanionAgents."""

    def __init__(
        self,
        *,
        instance_factory,  # Callable[[AgentInstance], Awaitable[CompanionAgent]]
    ) -> None:
        self._templates: dict[str, AgentTemplate] = {}
        self._instances: dict[str, AgentInstance] = {}
        self._lock = asyncio.Lock()
        self._factory = instance_factory

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

    async def start_instance(
        self,
        *,
        template_id: str,
        tenant_id: str,
        user_id: str,
        nickname_alias: str | None = None,
        explicit_instance_id: str | None = None,
    ) -> AgentInstance:
        self.get_template(template_id)  # raises if missing
        inst_id = explicit_instance_id or f"inst_{uuid.uuid4().hex[:12]}"
        async with self._lock:
            if inst_id in self._instances:
                raise ConflictError(f"instance already exists: {inst_id}")
            inst = AgentInstance(
                instance_id=inst_id,
                template_id=template_id,
                tenant_id=tenant_id,
                user_id=user_id,
                nickname_alias=nickname_alias,
            )
            inst.agent = await self._factory(inst)
            self._instances[inst_id] = inst
            return inst

    async def stop_instance(self, instance_id: str) -> None:
        async with self._lock:
            inst = self._instances.get(instance_id)
            if inst is None:
                return
            inst.status = "stopped"
            self._instances.pop(instance_id, None)

    def list_instances(self) -> list[AgentInstance]:
        return list(self._instances.values())

    def get_instance(self, instance_id: str) -> AgentInstance:
        try:
            return self._instances[instance_id]
        except KeyError as exc:
            raise NotFoundError(f"agent instance: {instance_id}") from exc

    def resolve_for_caller(
        self,
        *,
        tenant_id: str,
        user_id: str,
        instance_id: str | None,
    ) -> AgentInstance:
        if instance_id:
            return self.get_instance(instance_id)
        for inst in self._instances.values():
            if inst.tenant_id == tenant_id and inst.user_id == user_id:
                return inst
        raise NotFoundError(f"no active instance for {tenant_id}/{user_id}")
