"""Facade for the personas module."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from eidolon_agent.core.errors import NotFoundError
from eidolon_agent.core.types.memory import MemoryHit, MemoryQueryPlan
from eidolon_agent.domain.personas.compiler import PersonaCompiler
from eidolon_agent.domain.personas.evolution import PersonaEvolutionEngine
from eidolon_agent.domain.personas.instance_store import YamlPersonaInstanceStore
from eidolon_agent.domain.personas.memory_adapter import PersonaMemoryAdapter
from eidolon_agent.domain.personas.ports import (
    NullPersonaAuditPort,
    NullPersonaEventPort,
    PersonaAuditPort,
    PersonaEventPort,
    PersonaInstanceStore,
    PersonaLLMPort,
    PersonaMemoryPort,
)
from eidolon_agent.domain.personas.registry import PersonaTemplateRegistry
from eidolon_agent.domain.personas.runtime_state import PersonaRuntimeStateStore
from eidolon_agent.domain.personas.signal_adapter import PersonaSignalAdapter
from eidolon_agent.domain.personas.types import (
    CompiledPersona,
    PersonaEvolutionEvent,
    PersonaEvolutionResult,
    PersonaInstance,
    PersonaInteractionEvent,
    PersonaMockResult,
    PersonaSignalInput,
    PersonaSnapshot,
    PersonaTemplate,
    PersonaTemplateSummary,
)
from eidolon_agent.domain.personas.worker import PersonaEvolutionWorker


class PersonasService:
    def __init__(
        self,
        *,
        registry: PersonaTemplateRegistry,
        instances: PersonaInstanceStore,
        compiler: PersonaCompiler | None = None,
        memory_adapter: PersonaMemoryAdapter | None = None,
        evolution: PersonaEvolutionEngine | None = None,
        runtime_state: PersonaRuntimeStateStore | None = None,
        signal_adapter: PersonaSignalAdapter | None = None,
        worker: PersonaEvolutionWorker | None = None,
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
        self._runtime = runtime_state or PersonaRuntimeStateStore()
        self._signal_adapter = signal_adapter or PersonaSignalAdapter()
        self._memory = memory_port
        self._llm = llm_port
        self._events = event_port or NullPersonaEventPort()
        self._audit = audit_port or NullPersonaAuditPort()
        self._memory_timeout_s = memory_timeout_s
        self._worker = worker or PersonaEvolutionWorker(
            instances=self._instances,
            runtime_state=self._runtime,
            evolution=self._evolution,
            audit_port=self._audit,
            event_port=self._events,
        )

    async def start(self) -> None:
        await self._worker.start()

    async def stop(self) -> None:
        await self._worker.stop()

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
        instance = await self._instances.create_from_template(
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
            return await self._instances.load(tenant_id, user_id, instance_id)
        except NotFoundError:
            if template_id is None:
                raise
            return await self.create_instance(
                tenant_id=tenant_id,
                user_id=user_id,
                instance_id=instance_id,
                template_id=template_id,
            )

    async def get_snapshot(
        self,
        *,
        tenant_id: str,
        user_id: str,
        instance_id: str,
        template_id: str | None = None,
    ) -> PersonaSnapshot:
        instance = await self.get_instance(
            tenant_id=tenant_id,
            user_id=user_id,
            instance_id=instance_id,
            template_id=template_id,
        )
        runtime_state = await self._runtime.snapshot(instance_id=instance_id)
        return PersonaSnapshot(
            instance=instance,
            runtime_state=runtime_state,
            prompt_hint=runtime_state.to_prompt_hint(),
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
        snapshot = await self.get_snapshot(
            tenant_id=tenant_id,
            user_id=user_id,
            instance_id=instance_id,
            template_id=template_id,
        )
        instance = snapshot.instance
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
            runtime_state=snapshot.runtime_state,
            realtime=realtime,
        )

    async def update_runtime_state(
        self,
        *,
        instance_id: str,
        emotion: str | None = None,
        emotion_delta: float = 0.0,
        energy_level: float | None = None,
        attention_target=None,
        focus_score: float | None = None,
    ):
        return await self._runtime.update(
            instance_id=instance_id,
            emotion=emotion,
            emotion_delta=emotion_delta,
            energy_level=energy_level,
            attention_target=attention_target,
            focus_score=focus_score,
        )

    async def submit_interaction(self, event: PersonaInteractionEvent) -> None:
        normalized = (
            event
            if event.created_at is not None
            else event.model_copy(update={"created_at": datetime.now(timezone.utc)})
        )
        self._worker.submit(normalized)

    async def submit_signal(self, signal: PersonaSignalInput) -> None:
        update = self._signal_adapter.to_runtime_update(signal)
        if not update:
            return
        await self._runtime.update(instance_id=signal.instance_id, **update)

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
            # Bump version every time we persist a new overlay. Single-TX
            # writes are the responsibility of the store implementation
            # (SqlPersonaInstanceStore wraps save+history in one session).
            evolved = evolved.model_copy(
                update={"overlay_version": instance.overlay_version + 1}
            )
            await self._instances.save(evolved, reason="evolve")
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

    async def evolve_now(
        self,
        *,
        tenant_id: str,
        user_id: str,
        instance_id: str,
        events: list[PersonaEvolutionEvent],
        dry_run: bool = False,
        template_id: str | None = None,
    ) -> PersonaEvolutionResult:
        return await self.evolve(
            tenant_id=tenant_id,
            user_id=user_id,
            instance_id=instance_id,
            events=events,
            dry_run=dry_run,
            template_id=template_id,
        )

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

    async def drain_evolution_queue(self) -> None:
        await self._worker.join()

    # ---- Admin surface --------------------------------------------------

    async def list_instances(self) -> list[PersonaInstance]:
        """Return every persona instance across tenants/users.

        Used by the admin UI to render the global instance table. Order is
        whatever the store returns — admin frontend sorts client-side.
        """
        return await self._instances.list_all()

    async def delete_instance(
        self, *, tenant_id: str, user_id: str, instance_id: str
    ) -> None:
        await self._instances.delete(tenant_id, user_id, instance_id)
        await self._events.publish_persona_updated(
            instance_id, {"reason": "deleted"}
        )

    async def reload_templates(self) -> int:
        """Re-scan ``templates_dir`` and return the new template count."""
        await self._registry.load_all()
        return len(self._registry.list_all())


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
    """Build a service with the legacy YAML store.

    Production callers should instead build a ``SqlPersonaInstanceStore`` and
    pass it to ``PersonasService`` directly — see ``app/runtime/bootstrap.py``.
    This helper is preserved for tests and migration scripts that work off
    raw YAML files.
    """
    registry = PersonaTemplateRegistry(templates_dir)
    await registry.load_all()
    return PersonasService(
        registry=registry,
        instances=YamlPersonaInstanceStore(instances_dir),
        memory_port=memory_port,
        llm_port=llm_port,
        event_port=event_port,
        audit_port=audit_port,
        memory_timeout_s=memory_timeout_s,
    )
