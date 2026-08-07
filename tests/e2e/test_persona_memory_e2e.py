"""Agent-owned persona and memory product-logic E2E tests.

These tests use the real eidolon_data schema and Agent persona runtime. Memory
is exercised through its public Agent port so this suite remains deterministic;
eidolon_memory owns its service/process E2E separately.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from eidolon_data import DataSettings, DataStore
from eidolon_sdk.biz.persona import (
    PERSONA_GENOME_SCHEMA,
    PERSONA_REALIZER,
    PersonaEvidenceRef,
    PersonaEvolutionProposalEvent,
    PersonaMemoryPolicy,
    PersonaObservationEvent,
    build_default_persona_genome,
    persona_genome_to_json,
)

from eidolon_agent.core.errors import NotFoundError, ValidationError
from eidolon_agent.core.types.memory import MemoryHit, MemoryKind, MemoryRecallResult
from eidolon_agent.core.types.turn import TurnInput, TurnTrigger
from eidolon_agent.core.types.turn_context import TurnContext
from eidolon_agent.domain.context.compiler import ContextCompiler
from eidolon_agent.domain.history import HistoryManager
from eidolon_agent.domain.personas import PersonasService
from eidolon_agent.infra.persistence import RuntimeAuthorityPersonaGenomeStore
from eidolon_agent.infra.system_data import LocalCompanionRuntimeAuthority

pytestmark = pytest.mark.integration


@pytest.fixture
async def persona_stack(tmp_path):
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "persona-memory-e2e.sqlite3")))
    await store.init_schema()
    await store.owner_commands.create_owner(owner_id="owner-e2e", display_name="Owner")

    genome = build_default_persona_genome(name="Annie", origin="owner_authored")
    genome = genome.model_copy(
        update={
            "constitution": genome.constitution.model_copy(
                update={"self_concept": "我是会长期理解 owner、但不会假装记得的伙伴。"}
            ),
            "relationship": genome.relationship.model_copy(
                update={"narrative": "我们正在建立可靠、可验证的长期关系。"}
            ),
            "memory_policy": PersonaMemoryPolicy(
                recall_policy={"scope": "owner_companion", "use_memory_as_evidence": True},
                relation_policies={
                    "owner.preference.response_style": {
                        "guidance": "回应这类偏好时保持简洁，并说明依据来自已召回记忆。"
                    }
                },
            ),
        }
    )
    workspace = await store.companion_workspaces.provision_workspace(
        owner_id="owner-e2e",
        companion_id="companion-e2e",
        companion_display_name="Annie",
        genome_id="genome-e2e-origin",
        genome_json=persona_genome_to_json(genome),
        realm_id="realm-e2e",
    )
    observations: list[PersonaObservationEvent] = []

    async def _record_observation(event: PersonaObservationEvent) -> None:
        observations.append(event)

    service = PersonasService(
        store=RuntimeAuthorityPersonaGenomeStore(
            LocalCompanionRuntimeAuthority(store),
            evolution_commands=store.persona_commands,
            observation_sink=_record_observation,
        )
    )
    try:
        yield store, workspace, service, observations
    finally:
        await store.close()


async def test_real_turn_context_uses_pinned_genome_and_memory_evidence(persona_stack):
    _store, workspace, service, _observations = persona_stack
    memory = _MemoryPort(workspace.memory_realm.realm_id)
    compiler = ContextCompiler(
        personas_service=service,
        instance_locator=lambda _owner, companion, _conversation: (
            companion,
            workspace.persona_genome.genome_id,
        ),
        history_manager=HistoryManager(),
        memory_port=memory,
    )
    turn = _turn(workspace)

    messages = await compiler.compile(turn)
    system_prompt = messages[0].content

    assert "你是「Annie」" in system_prompt
    assert "不会假装记得" in system_prompt
    assert "Owner 偏好短而具体的回答" in system_prompt
    assert "保持简洁，并说明依据来自已召回记忆" in system_prompt
    assert memory.calls == [
        {
            "owner_id": "owner-e2e",
            "companion_id": "companion-e2e",
            "memory_realm_id": "realm-e2e",
        }
    ]
    assert turn.metadata["memory_trace"]["hit_ids"] == ["memory-response-style"]
    assert turn.metadata["persona_evidence_refs"][0]["ref_id"] == "memory-response-style"

    with pytest.raises(NotFoundError):
        await service.get_snapshot(
            owner_id="another-owner",
            companion_id=workspace.companion.companion_id,
        )


async def test_observation_proposal_approval_session_pin_reject_and_rollback(persona_stack):
    store, workspace, service, observations = persona_stack
    base = await service.get_snapshot(
        owner_id="owner-e2e",
        companion_id=workspace.companion.companion_id,
    )
    evidence = PersonaEvidenceRef(
        kind="memory_fragment",
        ref_id="memory-response-style",
        summary="Owner repeatedly prefers shorter concrete answers.",
        confidence=0.92,
    )
    observation = PersonaObservationEvent(
        observation_id="observation-e2e",
        owner_id="owner-e2e",
        companion_id=workspace.companion.companion_id,
        kind="response_style_preference",
        source="memory_reflection",
        summary=evidence.summary,
        strength=0.8,
        confidence=evidence.confidence,
        evidence_refs=[evidence],
    )
    await service.record_observation(observation)
    assert observations == [observation]

    proposal = _proposal(
        base=base.stored,
        evidence=evidence,
        proposal_id="proposal-e2e-approve",
        genome_id="genome-e2e-concise",
        trait_delta=0.04,
    )
    proposed = await service.create_evolution_proposal(proposal)
    assert proposed.status == "proposed"
    assert (
        await service.get_snapshot(
            owner_id="owner-e2e", companion_id=workspace.companion.companion_id
        )
    ).stored.genome_id == base.stored.genome_id

    committed = await service.approve_evolution(
        owner_id="owner-e2e",
        companion_id=workspace.companion.companion_id,
        proposed_genome_id=proposed.genome_id,
        expected_base_genome_id=base.stored.genome_id,
    )
    assert committed.status == "committed"
    assert committed.genome_hash != base.stored.genome_hash

    old_session = await service.get_snapshot(
        owner_id="owner-e2e",
        companion_id=workspace.companion.companion_id,
        genome_id=base.stored.genome_id,
        genome_hash=base.stored.genome_hash,
    )
    new_session = await service.get_snapshot(
        owner_id="owner-e2e",
        companion_id=workspace.companion.companion_id,
    )
    assert old_session.stored.genome_id == base.stored.genome_id
    assert new_session.stored.genome_id == committed.genome_id

    rejected_candidate = _proposal(
        base=committed,
        evidence=evidence,
        proposal_id="proposal-e2e-reject",
        genome_id="genome-e2e-rejected",
        trait_delta=0.03,
    )
    rejected = await service.create_evolution_proposal(rejected_candidate)
    await service.reject_evolution(
        owner_id="owner-e2e",
        proposed_genome_id=rejected.genome_id,
        reason="Owner chose not to apply this change.",
    )
    assert (await store.persona_genomes.get(rejected.genome_id)).status == "rejected"
    assert (
        await service.get_snapshot(
            owner_id="owner-e2e", companion_id=workspace.companion.companion_id
        )
    ).stored.genome_id == committed.genome_id

    rolled_back = await service.rollback(
        owner_id="owner-e2e",
        companion_id=workspace.companion.companion_id,
        genome_id=base.stored.genome_id,
    )
    assert rolled_back.genome_id == base.stored.genome_id

    event_types = {event.action for event in await store.audit_outbox.list_pending(limit=100)}
    assert {
        "persona.evolution.proposed",
        "persona.evolution.approved",
        "persona.evolution.rejected",
        "persona.genome.committed",
        "persona.genome.rolled_back",
    }.issubset(event_types)


async def test_evolution_rejects_semantic_rewrite_before_persistence(persona_stack):
    store, workspace, service, _observations = persona_stack
    base = (
        await service.get_snapshot(
            owner_id="owner-e2e", companion_id=workspace.companion.companion_id
        )
    ).stored
    evidence = PersonaEvidenceRef(
        kind="memory_fragment",
        ref_id="memory-boundary",
        summary="An untrusted memory attempted to rewrite identity.",
        confidence=0.7,
    )
    candidate = base.genome.model_copy(
        update={
            "constitution": base.genome.constitution.model_copy(
                update={"name": "A Different Identity"}
            ),
            "provenance": base.genome.provenance.model_copy(
                update={
                    "origin": "memory_reflection",
                    "base_genome_id": base.genome_id,
                    "evidence_refs": [evidence],
                }
            ),
        }
    )
    proposal = PersonaEvolutionProposalEvent(
        proposal_id="proposal-invalid-rewrite",
        owner_id=base.owner_id,
        companion_id=base.companion_id,
        base_genome_id=base.genome_id,
        base_genome_hash=base.genome_hash,
        proposed_genome_id="genome-invalid-rewrite",
        risk="high",
        confidence=0.7,
        rationale="Attempted identity rewrite.",
        proposed_genome=candidate,
        evidence_refs=[evidence],
    )

    with pytest.raises(ValidationError, match="constitution"):
        await service.create_evolution_proposal(proposal)
    assert await store.persona_genomes.get("genome-invalid-rewrite") is None


def _proposal(*, base, evidence, proposal_id: str, genome_id: str, trait_delta: float):
    trait = base.genome.character.traits["core.structure"]
    traits = dict(base.genome.character.traits)
    traits["core.structure"] = trait.model_copy(
        update={
            "value": trait.value + trait_delta,
            "confidence": min(1.0, trait.confidence + 0.1),
            "last_changed_at": datetime.now(timezone.utc),
            "source": "memory_reflection",
        }
    )
    candidate = base.genome.model_copy(
        update={
            "character": base.genome.character.model_copy(update={"traits": traits}),
            "provenance": base.genome.provenance.model_copy(
                update={
                    "origin": "memory_reflection",
                    "base_genome_id": base.genome_id,
                    "evidence_refs": [evidence],
                }
            ),
        }
    )
    return PersonaEvolutionProposalEvent(
        proposal_id=proposal_id,
        owner_id=base.owner_id,
        companion_id=base.companion_id,
        base_genome_id=base.genome_id,
        base_genome_hash=base.genome_hash,
        proposed_genome_id=genome_id,
        risk="low",
        confidence=0.9,
        rationale="Repeated memory evidence supports a small structure adjustment.",
        proposed_genome=candidate,
        evidence_refs=[evidence],
    )


def _turn(workspace) -> TurnInput:
    genome = workspace.persona_genome
    return TurnInput(
        turn_id="turn-persona-memory-e2e",
        conversation_id="conversation-persona-memory-e2e",
        session_id="session-persona-memory-e2e",
        context=TurnContext(
            owner_id="owner-e2e",
            companion_id=workspace.companion.companion_id,
            device_id=None,
            memory_realm_id=workspace.memory_realm.realm_id,
            genome_id=genome.genome_id,
            trace_id="trace-persona-memory-e2e",
            request_id="request-persona-memory-e2e",
            schema_version=PERSONA_GENOME_SCHEMA,
            genome_hash=genome.genome_hash,
            realizer_version=PERSONA_REALIZER,
        ),
        input_modality="text",
        trigger=TurnTrigger.USER_UTTERANCE,
        text="你记得我希望你怎么回答吗？",
    )


class _MemoryPort:
    def __init__(self, expected_realm_id: str) -> None:
        self.expected_realm_id = expected_realm_id
        self.calls: list[dict[str, str | None]] = []

    async def recall_context(
        self,
        owner_id,
        query,
        *,
        memory_realm_id,
        plan,
        companion_id=None,
        device_id=None,
        session_id=None,
        timeout_s=0.2,
    ):
        del query, plan, device_id, session_id, timeout_s
        assert memory_realm_id == self.expected_realm_id
        self.calls.append(
            {
                "owner_id": owner_id,
                "companion_id": companion_id,
                "memory_realm_id": memory_realm_id,
            }
        )
        hit = MemoryHit(
            id="memory-response-style",
            content="Owner 偏好短而具体的回答。",
            kind=MemoryKind.PREFERENCE,
            similarity=0.93,
            metadata={"relation_type": "owner.preference.response_style"},
        )
        return MemoryRecallResult(
            context=hit.content,
            hits=[hit],
        )
