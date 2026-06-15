"""Facade for the personas module."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from eidolon_agent.core.errors import (
    ConflictError,
    EvolutionGuardError,
    NotFoundError,
    ValidationError,
)
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
    PersonaEvolutionProposalRepository,
    PersonaEvolutionRepository,
    PersonaInstanceStore,
    PersonaLLMPort,
    PersonaMemoryPort,
    PersonaObservationRepository,
)
from eidolon_agent.domain.personas.reflection import PersonaReflectionEngine
from eidolon_agent.domain.personas.registry import PersonaTemplateRegistry
from eidolon_agent.domain.personas.runtime_state import PersonaRuntimeStateStore
from eidolon_agent.domain.personas.signal_adapter import PersonaSignalAdapter
from eidolon_agent.domain.personas.types import (
    CompiledPersona,
    PersonaEvolutionChange,
    PersonaEvolutionEvent,
    PersonaEvolutionProposal,
    PersonaEvolutionResult,
    PersonaInstance,
    PersonaInteractionEvent,
    PersonaMockResult,
    PersonaObservation,
    PersonaSignalInput,
    PersonaSnapshot,
    PersonaTemplate,
    PersonaTemplateSummary,
)
from eidolon_agent.domain.personas.worker import PersonaEvolutionWorker

_log = logging.getLogger(__name__)


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
        evolution_repo: PersonaEvolutionRepository | None = None,
        observation_repo: PersonaObservationRepository | None = None,
        proposal_repo: PersonaEvolutionProposalRepository | None = None,
        reflection: PersonaReflectionEngine | None = None,
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
        self._evolution_repo = evolution_repo
        self._observation_repo = observation_repo
        self._proposal_repo = proposal_repo
        self._reflection = reflection or PersonaReflectionEngine()
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
        if (
            dry_run_memory is None
            and adapted.triggered_events
            and self._observation_repo is not None
        ):
            try:
                await self._record_triggered_observations(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    instance_id=instance_id,
                    kinds=adapted.triggered_events,
                    source="memory_adapter",
                    summary="memory policy triggered persona observation",
                    evidence={"query": user_text[:500], "degraded": degraded},
                    memory_ids=tuple(hit.id for hit in hits),
                )
            except Exception:
                _log.exception("record persona memory observation failed")
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
        observation = _interaction_to_observation(normalized)
        if observation is not None and self._observation_repo is not None:
            await self._observation_repo.add(observation)
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
            evolved = evolved.model_copy(update={"overlay_version": instance.overlay_version + 1})
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

    async def delete_instance(self, *, tenant_id: str, user_id: str, instance_id: str) -> None:
        await self._instances.delete(tenant_id, user_id, instance_id)
        await self._events.publish_persona_updated(instance_id, {"reason": "deleted"})

    async def reload_templates(self) -> int:
        """Re-scan ``templates_dir`` and return the new template count."""
        await self._registry.load_all()
        return len(self._registry.list_all())

    async def get_template_raw(self, template_id: str) -> str:
        """Return the original YAML source for a template."""
        return self._registry.raw_yaml(template_id)

    async def rollback_evolution(
        self,
        *,
        tenant_id: str,
        user_id: str,
        instance_id: str,
        delta_id: str,
    ) -> PersonaEvolutionResult:
        """Reverse the changes recorded under ``delta_id``.

        Reads the audit row, applies its inverse (each ``old`` value restored
        onto the corresponding knob), bumps ``overlay_version``, and persists.
        The original audit row remains; a new audit row marks the rollback
        as a separate event so the timeline reads forward only.
        """
        if self._evolution_repo is None:
            raise NotFoundError("evolution repository not wired; cannot rollback")
        original = await self._evolution_repo.get(delta_id)
        if original is None:
            raise NotFoundError(f"evolution delta not found: {delta_id}")
        instance = await self.get_instance(
            tenant_id=tenant_id, user_id=user_id, instance_id=instance_id
        )
        # Apply inverse: each change[].path → restore old value on the knob.
        knobs = dict(instance.behavioral_knobs)
        reverse_changes: list = []
        for change in original.changes:
            path = change.path
            if not path.startswith("behavioral_knobs."):
                continue
            knob_name = path[len("behavioral_knobs.") :]
            knob = knobs.get(knob_name)
            if knob is None:
                continue
            knobs[knob_name] = knob.model_copy(update={"current": float(change.old)})
            reverse_changes.append(change.model_copy(update={"old": change.new, "new": change.old}))
        rolled = instance.model_copy(
            update={
                "behavioral_knobs": knobs,
                "overlay_version": instance.overlay_version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        await self._instances.save(rolled, reason=f"rollback:{delta_id}")
        result = PersonaEvolutionResult(
            instance_id=instance_id,
            applied=True,
            changes=tuple(reverse_changes),
            rationale=f"rollback of {delta_id}",
        )
        await self._audit.record_evolution(result)
        await self._events.publish_persona_updated(
            instance_id,
            {"reason": "rollback", "delta_id": delta_id},
        )
        return result

    async def list_evolution_history(
        self, instance_id: str, *, limit: int = 50
    ) -> list[PersonaEvolutionResult]:
        """Return the recent applied evolution rows for an instance.

        Admin uses this to render the per-instance history view. Requires a
        ``PersonaEvolutionRepository`` to have been wired; without one the
        method returns an empty list (the audit trail simply isn't persisted).
        """
        if self._evolution_repo is None:
            return []
        return await self._evolution_repo.list_for_instance(instance_id, limit=limit)

    async def record_observation(self, observation: PersonaObservation) -> None:
        """Record durable evidence for a personal instance.

        This is intentionally a service method so admin/runtime callers do not
        import persistence repositories or write observation rows directly.
        """
        if self._observation_repo is None:
            return
        await self._observation_repo.add(observation)

    async def _record_triggered_observations(
        self,
        *,
        tenant_id: str,
        user_id: str,
        instance_id: str,
        kinds: tuple[str, ...],
        source: str,
        summary: str,
        evidence: dict,
        memory_ids: tuple[str, ...] = (),
    ) -> None:
        if self._observation_repo is None:
            return
        now = datetime.now(timezone.utc)
        for kind in kinds:
            await self._observation_repo.add(
                PersonaObservation(
                    id=f"obs-{uuid.uuid4().hex}",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    instance_id=instance_id,
                    kind=kind,
                    source=source,
                    strength=0.55,
                    confidence=0.65,
                    summary=summary,
                    evidence=evidence,
                    memory_ids=memory_ids,
                    created_at=now,
                )
            )

    async def list_observations(
        self,
        instance_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[PersonaObservation]:
        if self._observation_repo is None:
            return []
        return await self._observation_repo.list_for_instance(
            instance_id,
            status=status,
            limit=limit,
        )

    async def run_reflection(
        self,
        *,
        tenant_id: str,
        user_id: str,
        instance_id: str,
        dry_run: bool = False,
        limit: int = 50,
    ) -> list[PersonaEvolutionProposal]:
        """Aggregate observations into bounded evolution proposals."""
        instance = await self.get_instance(
            tenant_id=tenant_id,
            user_id=user_id,
            instance_id=instance_id,
        )
        observations = await self.list_observations(
            instance_id,
            status="active",
            limit=limit,
        )
        proposals = self._reflection.reflect(
            instance=instance,
            observations=observations,
            limit=limit,
        )
        if dry_run or self._proposal_repo is None:
            return proposals
        for proposal in proposals:
            await self._proposal_repo.add(proposal)
            if self._observation_repo is not None:
                for observation_id in proposal.evidence_ids:
                    await self._observation_repo.set_status(
                        observation_id,
                        "converted",
                    )
        return proposals

    async def list_evolution_proposals(
        self,
        instance_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[PersonaEvolutionProposal]:
        if self._proposal_repo is None:
            return []
        return await self._proposal_repo.list_for_instance(
            instance_id,
            status=status,
            limit=limit,
        )

    async def get_evolution_proposal(self, proposal_id: str) -> PersonaEvolutionProposal:
        if self._proposal_repo is None:
            raise NotFoundError("evolution proposal repository not wired")
        proposal = await self._proposal_repo.get(proposal_id)
        if proposal is None:
            raise NotFoundError(f"evolution proposal not found: {proposal_id}")
        return proposal

    async def approve_evolution_proposal(
        self,
        proposal_id: str,
        *,
        actor: str = "admin",
    ) -> PersonaEvolutionResult:
        if self._proposal_repo is None:
            raise NotFoundError("evolution proposal repository not wired")
        proposal = await self.get_evolution_proposal(proposal_id)
        if proposal.status != "pending":
            raise ConflictError(f"proposal is not pending: {proposal.status}")
        instance = await self.get_instance(
            tenant_id=proposal.tenant_id,
            user_id=proposal.user_id,
            instance_id=proposal.instance_id,
        )
        evolved, result = _apply_proposal(instance=instance, proposal=proposal)
        decided = proposal.model_copy(
            update={
                "status": "applied",
                "updated_at": datetime.now(timezone.utc),
                "decided_by": actor,
                "decided_at": datetime.now(timezone.utc),
            }
        )
        if result.applied:
            evolved = evolved.model_copy(update={"overlay_version": instance.overlay_version + 1})
            await self._instances.save(evolved, reason=f"proposal:{proposal.id}")
            await self._audit.record_evolution(result)
            await self._events.publish_evolution_applied(
                proposal.instance_id,
                result.model_dump(mode="json"),
            )
            await self._events.publish_persona_updated(
                proposal.instance_id,
                {"reason": "proposal_approved", "proposal_id": proposal.id},
            )
        await self._proposal_repo.save(decided)
        return result

    async def reject_evolution_proposal(
        self,
        proposal_id: str,
        *,
        actor: str = "admin",
        reason: str | None = None,
    ) -> PersonaEvolutionProposal:
        if self._proposal_repo is None:
            raise NotFoundError("evolution proposal repository not wired")
        proposal = await self.get_evolution_proposal(proposal_id)
        if proposal.status != "pending":
            raise ConflictError(f"proposal is not pending: {proposal.status}")
        rejected = proposal.model_copy(
            update={
                "status": "rejected",
                "updated_at": datetime.now(timezone.utc),
                "decided_by": actor,
                "decided_at": datetime.now(timezone.utc),
                "decision_reason": reason,
            }
        )
        await self._proposal_repo.save(rejected)
        return rejected


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


def _interaction_to_observation(
    event: PersonaInteractionEvent,
) -> PersonaObservation | None:
    kinds: list[str] = []
    if event.kind in {
        "positive_feedback_received",
        "user_requests_less_advice",
        "boundary_correction_received",
        "goal_progress_shared",
    }:
        kinds.append(event.kind)
    for raw in event.payload.get("evolution_events", ()):
        kinds.append(str(raw))
    if not kinds:
        return None
    kind = kinds[0]
    evidence = {
        "event_kind": event.kind,
        "payload": event.payload,
    }
    if event.user_text:
        evidence["user_text"] = event.user_text[:500]
    if event.assistant_text:
        evidence["assistant_text"] = event.assistant_text[:500]
    return PersonaObservation(
        id=f"obs-{uuid.uuid4().hex}",
        tenant_id=event.tenant_id,
        user_id=event.user_id,
        instance_id=event.instance_id,
        kind=kind,
        source=event.kind,
        strength=_clamp01(float(event.payload.get("strength", 0.6))),
        confidence=_clamp01(float(event.payload.get("confidence", 0.7))),
        summary=str(event.payload.get("summary") or kind),
        evidence=evidence,
        created_at=event.created_at or datetime.now(timezone.utc),
    )


def _apply_proposal(
    *,
    instance: PersonaInstance,
    proposal: PersonaEvolutionProposal,
) -> tuple[PersonaInstance, PersonaEvolutionResult]:
    knobs = dict(instance.behavioral_knobs)
    changes: list[PersonaEvolutionChange] = []
    now = datetime.now(timezone.utc)
    for patch in proposal.patches:
        if patch.type != "knob_delta":
            continue
        if patch.delta is None:
            raise ValidationError(f"proposal patch missing delta: {patch.target}")
        prefix = "behavioral_knobs."
        if not patch.target.startswith(prefix):
            raise EvolutionGuardError(
                f"proposal knob_delta target must start with {prefix!r}: {patch.target}"
            )
        knob_name = patch.target[len(prefix) :]
        knob = knobs.get(knob_name)
        if knob is None:
            raise EvolutionGuardError(f"unknown behavioral knob: {knob_name}")
        delta = max(-knob.step_limit, min(knob.step_limit, patch.delta))
        new_current = min(knob.max, max(knob.min, knob.current + delta))
        if new_current == knob.current:
            continue
        knobs[knob_name] = knob.model_copy(update={"current": new_current, "last_changed_at": now})
        changes.append(
            PersonaEvolutionChange(
                path=patch.target,
                old=knob.current,
                new=new_current,
                rule_id=proposal.id,
            )
        )
    result = PersonaEvolutionResult(
        instance_id=instance.instance_id,
        applied=bool(changes),
        changes=tuple(changes),
        rationale=f"approved proposal {proposal.id}: {proposal.rationale}",
    )
    if not changes:
        return instance, result
    evolved = instance.model_copy(
        update={
            "behavioral_knobs": knobs,
            "updated_at": now,
        }
    )
    return evolved, result


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
