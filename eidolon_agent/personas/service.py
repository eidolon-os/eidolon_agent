"""Facade for the personas module."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from eidolon_agent.core.errors import NotFoundError
from eidolon_agent.core.types.memory import MemoryHit, MemoryQueryPlan
from eidolon_agent.personas.compiler import PersonaCompiler
from eidolon_agent.personas.evolution import PersonaEvolutionEngine
from eidolon_agent.personas.instance_store import PersonaInstanceStore
from eidolon_agent.personas.memory_adapter import PersonaMemoryAdapter
from eidolon_agent.personas.ports import (
    NullPersonaAuditPort,
    NullPersonaEventPort,
    PersonaAuditPort,
    PersonaEventPort,
    PersonaLLMPort,
    PersonaMemoryPort,
)
from eidolon_agent.personas.registry import PersonaTemplateRegistry
from eidolon_agent.personas.types import (
    CompiledPersona,
    PersonaEvolutionEvent,
    PersonaEvolutionResult,
    PersonaInstance,
    PersonaMockResult,
    PersonaTemplate,
    PersonaTemplateSummary,
)


class PersonasService:
    def __init__(
        self,
        *,
        registry: PersonaTemplateRegistry,
        instances: PersonaInstanceStore,
        compiler: PersonaCompiler | None = None,
        memory_adapter: PersonaMemoryAdapter | None = None,
        evolution: PersonaEvolutionEngine | None = None,
        memory_port: PersonaMemoryPort | None = None,
        llm_port: PersonaLLMPort | None = None,
        event_port: PersonaEventPort | None = None,
        audit_port: PersonaAuditPort | None = None,
        memory_timeout_s: float = 0.2,
    ) -> None:
        self._registry = registry
        self._instances = instances
        self._compiler = compiler or PersonaCompiler()
        self._memory_adapter = memory_adapter or PersonaMemoryAdapter()
        self._evolution = evolution or PersonaEvolutionEngine()
        self._memory = memory_port
        self._llm = llm_port
        self._events = event_port or NullPersonaEventPort()
        self._audit = audit_port or NullPersonaAuditPort()
        self._memory_timeout_s = memory_timeout_s

    async def list_templates(self) -> list[PersonaTemplateSummary]:
        return self._registry.list_templates()

    async def get_template(self, template_id: str) -> PersonaTemplate:
        return self._registry.get(template_id)

    async def create_instance(
        self,
        *,
        tenant_id: str,
        user_id: str,
        instance_id: str,
        template_id: str,
    ) -> PersonaInstance:
        template = self._registry.get(template_id)
        instance = self._instances.create_from_template(
            template=template,
            tenant_id=tenant_id,
            user_id=user_id,
            instance_id=instance_id,
        )
        await self._events.publish_persona_updated(
            instance.instance_id,
            {"reason": "created", "template_id": template_id},
        )
        return instance

    async def get_instance(
        self,
        *,
        tenant_id: str,
        user_id: str,
        instance_id: str,
        template_id: str | None = None,
    ) -> PersonaInstance:
        try:
            return self._instances.load(tenant_id, user_id, instance_id)
        except NotFoundError:
            if template_id is None:
                raise
            return await self.create_instance(
                tenant_id=tenant_id,
                user_id=user_id,
                instance_id=instance_id,
                template_id=template_id,
            )

    async def compile_prompt(
        self,
        *,
        tenant_id: str,
        user_id: str,
        instance_id: str,
        user_text: str,
        template_id: str | None = None,
        realtime: dict | None = None,
        dry_run_memory: list[MemoryHit] | None = None,
    ) -> CompiledPersona:
        instance = await self.get_instance(
            tenant_id=tenant_id,
            user_id=user_id,
            instance_id=instance_id,
            template_id=template_id,
        )
        formatted_context = ""
        hits: list[MemoryHit] = []
        degraded = False
        if dry_run_memory is not None:
            hits = dry_run_memory
            formatted_context = "\n".join(hit.content for hit in hits)
        elif self._memory is not None and user_text:
            formatted_context, hits, degraded = await self._memory.recall_context(
                user_id=user_id,
                query=user_text,
                plan=MemoryQueryPlan(
                    episodic_query=user_text,
                    semantic_query=user_text,
                    episodic_k=3,
                    semantic_k=5,
                    voice=True,
                ),
                timeout_s=self._memory_timeout_s,
            )

        adapted = self._memory_adapter.adapt(
            instance=instance,
            formatted_context=formatted_context,
            hits=hits,
            degraded=degraded,
        )
        return self._compiler.compile(
            instance=instance,
            adapted_memory=adapted,
            realtime=realtime,
        )

    async def evolve(
        self,
        *,
        tenant_id: str,
        user_id: str,
        instance_id: str,
        events: list[PersonaEvolutionEvent],
        dry_run: bool = False,
        template_id: str | None = None,
    ) -> PersonaEvolutionResult:
        instance = await self.get_instance(
            tenant_id=tenant_id,
            user_id=user_id,
            instance_id=instance_id,
            template_id=template_id,
        )
        normalized = [
            event
            if event.created_at is not None
            else event.model_copy(update={"created_at": datetime.now(timezone.utc)})
            for event in events
        ]
        evolved, result = self._evolution.evolve(
            instance=instance,
            events=normalized,
            dry_run=dry_run,
        )
        if result.applied:
            self._instances.save(evolved, reason="evolve")
            await self._audit.record_evolution(result)
            await self._events.publish_evolution_applied(
                instance_id,
                result.model_dump(mode="json"),
            )
            await self._events.publish_persona_updated(
                instance_id,
                {"reason": "evolution", "changes": result.model_dump(mode="json")["changes"]},
            )
        return result

    async def mock_memory_trigger(
        self,
        *,
        tenant_id: str,
        user_id: str,
        instance_id: str,
        user_text: str,
        memory_hits: list[MemoryHit],
        apply: bool = False,
        template_id: str | None = None,
    ) -> PersonaMockResult:
        compiled = await self.compile_prompt(
            tenant_id=tenant_id,
            user_id=user_id,
            instance_id=instance_id,
            template_id=template_id,
            user_text=user_text,
            dry_run_memory=memory_hits,
        )
        instance = await self.get_instance(
            tenant_id=tenant_id,
            user_id=user_id,
            instance_id=instance_id,
            template_id=template_id,
        )
        adapted = self._memory_adapter.adapt(
            instance=instance,
            formatted_context="\n".join(hit.content for hit in memory_hits),
            hits=memory_hits,
        )
        events = [
            PersonaEvolutionEvent(kind=kind, source="mock_memory")
            for kind in adapted.triggered_events
        ]
        evolution = None
        if events:
            evolution = await self.evolve(
                tenant_id=tenant_id,
                user_id=user_id,
                instance_id=instance_id,
                template_id=template_id,
                events=events,
                dry_run=not apply,
            )
        return PersonaMockResult(compiled=compiled, evolution=evolution)


async def build_default_personas_service(
    *,
    templates_dir: Path,
    instances_dir: Path,
    memory_port: PersonaMemoryPort | None = None,
    llm_port: PersonaLLMPort | None = None,
    event_port: PersonaEventPort | None = None,
    audit_port: PersonaAuditPort | None = None,
    memory_timeout_s: float = 0.2,
) -> PersonasService:
    registry = PersonaTemplateRegistry(templates_dir)
    await registry.load_all()
    return PersonasService(
        registry=registry,
        instances=PersonaInstanceStore(instances_dir),
        memory_port=memory_port,
        llm_port=llm_port,
        event_port=event_port,
        audit_port=audit_port,
        memory_timeout_s=memory_timeout_s,
    )

