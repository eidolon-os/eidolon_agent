"""Real Data + Agent boundaries for versioned edits and live preferences."""

import asyncio
from dataclasses import replace

import pytest
from eidolon_data import DataSettings, DataStore
from eidolon_data.repositories.persona import PersonaGenomeConflict
from eidolon_sdk.biz.persona import (
    ConversationPreferences,
    PersonaAuthoring,
    PersonaEditRequest,
    build_default_persona_genome,
    normalize_persona_genome,
    persona_genome_to_json,
)

from eidolon_agent.domain.context.compiler import ContextCompiler
from eidolon_agent.domain.history import HistoryManager
from eidolon_agent.domain.personas import PersonasService
from eidolon_agent.infra.persistence import RuntimeAuthorityPersonaGenomeStore
from eidolon_agent.infra.system_data import LocalCompanionRuntimeAuthority
from tests.e2e.test_persona_memory_e2e import _turn

pytestmark = pytest.mark.integration


@pytest.fixture
async def settings_stack(tmp_path):
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "settings.sqlite3")))
    await store.init_schema()
    await store.owner_commands.create_owner(owner_id="owner-e2e")
    genome = build_default_persona_genome(name="Original")
    genome.relationship.stage = "trusted"
    genome.character.tensions = ["keep tension"]
    genome.expression.signature_phrases = {"test": "keep phrase"}
    genome.expression.modality_notes = {"voice": "VOICE_ONLY", "text": "TEXT_ONLY"}
    genome.evolution_policy.enabled = False
    workspace = await store.companion_workspaces.provision_workspace(
        owner_id="owner-e2e",
        companion_id="companion-e2e",
        companion_display_name="Original",
        genome_id="origin",
        genome_json=persona_genome_to_json(genome),
        realm_id="realm-e2e",
    )
    service = PersonasService(
        store=RuntimeAuthorityPersonaGenomeStore(LocalCompanionRuntimeAuthority(store))
    )
    try:
        yield store, workspace, service
    finally:
        await store.close()


def edit(base, operation, **fields):
    return PersonaEditRequest(
        expected_base_genome_id=base.genome_id,
        expected_preference_revision=base.preference_revision,
        operation_id=operation,
        persona=PersonaAuthoring(**fields),
    )


async def test_edit_preserves_hidden_fields_and_old_snapshot(settings_stack):
    store, workspace, _ = settings_stack
    before = await store.persona_commands.read_edit_snapshot("companion-e2e")
    result = await store.persona_commands.edit(
        companion_id="companion-e2e", request=edit(before, "a", voice_portrait="Short")
    )
    saved = normalize_persona_genome(
        (await store.persona_genomes.get(result.genome_id)).genome_json
    )
    original = normalize_persona_genome(workspace.persona_genome.genome_json)
    assert saved.relationship == original.relationship
    assert saved.character == original.character
    assert saved.evolution_policy == original.evolution_policy
    assert saved.expression.signature_phrases == original.expression.signature_phrases
    assert saved.expression.voice_portrait == "Short"
    assert (
        await store.persona_genomes.get("origin")
    ).genome_json == workspace.persona_genome.genome_json


async def test_cas_concurrency_and_late_retry(settings_stack):
    store, _, _ = settings_stack
    base = await store.persona_commands.read_edit_snapshot("companion-e2e")
    a, b = edit(base, "a", voice_portrait="A"), edit(base, "b", character_portrait="B")
    results = await asyncio.gather(
        *(store.persona_commands.edit(companion_id="companion-e2e", request=r) for r in (a, b)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, PersonaGenomeConflict) for r in results) == 1
    winner_index = next(i for i, r in enumerate(results) if not isinstance(r, Exception))
    winner = results[winner_index]
    request = (a, b)[winner_index]
    latest = await store.persona_commands.edit(
        companion_id="companion-e2e", request=edit(winner, "later", voice_portrait="latest")
    )
    replay = await store.persona_commands.edit(companion_id="companion-e2e", request=request)
    assert replay == winner
    assert (await store.persona_genomes.get_current("companion-e2e")).genome_id == latest.genome_id
    with pytest.raises(PersonaGenomeConflict, match="different input"):
        await store.persona_commands.edit(
            companion_id="companion-e2e",
            request=request.model_copy(
                update={"persona": PersonaAuthoring(voice_portrait="changed")}
            ),
        )


async def test_noop_receipt_survives_later_edits(settings_stack):
    store, _, _ = settings_stack
    base = await store.persona_commands.read_edit_snapshot("companion-e2e")
    request = edit(base, "noop")
    first = await store.persona_commands.edit(companion_id="companion-e2e", request=request)
    assert first.genome_id == base.genome_id
    await store.persona_commands.edit(
        companion_id="companion-e2e", request=edit(first, "later", voice_portrait="Changed")
    )
    assert await store.persona_commands.edit(companion_id="companion-e2e", request=request) == first


async def test_versioned_rename_restore_and_lost_response_replay(settings_stack):
    store, _, _ = settings_stack
    before = await store.persona_commands.read_edit_snapshot("companion-e2e")
    rename = PersonaEditRequest(
        **{
            **edit(before, "rename").model_dump(exclude_unset=True),
            "action": "rename",
            "display_name": "New Name",
        }
    )
    named = await store.persona_commands.edit(companion_id="companion-e2e", request=rename)
    assert named.display_name == "New Name"
    assert named.companion_revision == before.companion_revision + 1
    with pytest.raises(PersonaGenomeConflict, match="changed"):
        await store.persona_commands.edit(
            companion_id="companion-e2e", request=edit(before, "stale", voice_portrait="stale")
        )
    modified = await store.persona_commands.edit(
        companion_id="companion-e2e",
        request=PersonaEditRequest(
            **{
                **edit(named, "changed", voice_portrait="New voice").model_dump(exclude_unset=True),
                "preferences": ConversationPreferences(response_length="detailed"),
            }
        ),
    )
    restore = PersonaEditRequest(
        **{
            **edit(modified, "restore").model_dump(exclude_unset=True),
            "action": "restore",
            "restore_genome_id": before.genome_id,
        }
    )
    restored = await store.persona_commands.edit(companion_id="companion-e2e", request=restore)
    assert restored.display_name == "New Name"
    assert restored.persona == before.persona
    assert restored.preferences.response_length == "detailed"
    assert restored.genome_id not in {before.genome_id, modified.genome_id}
    assert await store.persona_commands.edit(companion_id="companion-e2e", request=rename) == named
    assert (
        await store.persona_commands.edit(companion_id="companion-e2e", request=restore) == restored
    )
    assert (
        await store.persona_genomes.get_current("companion-e2e")
    ).genome_id == restored.genome_id


@pytest.mark.parametrize("action", ["rename", "restore"])
async def test_actions_reject_stale_preferences(settings_stack, action):
    store, _, _ = settings_stack
    before = await store.persona_commands.read_edit_snapshot("companion-e2e")
    await store.persona_commands.edit(
        companion_id="companion-e2e",
        request=PersonaEditRequest(
            **{
                **edit(before, "prefs").model_dump(exclude_unset=True),
                "preferences": ConversationPreferences(response_length="detailed"),
            }
        ),
    )
    request = PersonaEditRequest(
        **{
            **edit(before, action).model_dump(exclude_unset=True),
            "action": action,
            **(
                {"display_name": "Other"}
                if action == "rename"
                else {"restore_genome_id": before.genome_id}
            ),
        }
    )
    with pytest.raises(PersonaGenomeConflict, match="preferences changed"):
        await store.persona_commands.edit(companion_id="companion-e2e", request=request)


async def test_rename_and_restore_preserve_current_name(settings_stack):
    store, _, _ = settings_stack
    await store.companions.rename("companion-e2e", "New Name")
    base = await store.persona_commands.read_edit_snapshot("companion-e2e")
    changed = await store.persona_commands.edit(
        companion_id="companion-e2e", request=edit(base, "voice", voice_portrait="Changed")
    )
    restored = await store.persona_commands.restore_chapter(
        companion_id="companion-e2e", genome_id="origin"
    )
    saved = normalize_persona_genome(restored.genome_json)
    assert saved.constitution.name == "New Name"
    assert restored.genome_id not in {"origin", changed.genome_id}
    assert restored.base_genome_id == changed.genome_id
    assert restored.source_json["restored_from"] == "origin"
    assert restored.applied_event_id


async def test_voice_and_preference_change_on_next_turn_keep_persona_pin(settings_stack):
    store, workspace, service = settings_stack
    compiler = ContextCompiler(
        personas_service=service,
        instance_locator=lambda *_: ("companion-e2e", "origin"),
        history_manager=HistoryManager(),
    )
    turn = replace(_turn(workspace), input_modality="voice")
    first = (await compiler.compile(turn))[0].content
    assert "VOICE_ONLY" in first and "TEXT_ONLY" not in first
    assert "1–3" in first
    base = await store.persona_commands.read_edit_snapshot("companion-e2e")
    req = edit(base, "preferences").model_copy(
        update={"preferences": ConversationPreferences(response_length="detailed")}
    )
    result = await store.persona_commands.edit(companion_id="companion-e2e", request=req)
    assert result.genome_id == "origin"
    second = (await compiler.compile(replace(turn, turn_id="next", metadata={})))[0].content
    assert "提供充分细节" in second
    assert first.split("[RESPONSE POLICY]")[0] == second.split("[RESPONSE POLICY]")[0]
    assert len(await store.persona_genomes.list_for_companion("companion-e2e")) == 1


async def test_stale_preference_edit_is_rejected_without_changing_genome(settings_stack):
    store, _, _ = settings_stack
    base = await store.persona_commands.read_edit_snapshot("companion-e2e")
    request = edit(base, "prefs-a").model_copy(
        update={"preferences": ConversationPreferences(response_length="detailed")}
    )
    first = await store.persona_commands.edit(companion_id="companion-e2e", request=request)
    assert first.preference_revision == base.preference_revision + 1
    assert first.genome_id == base.genome_id
    stale = edit(base, "prefs-b").model_copy(
        update={"preferences": ConversationPreferences(advice="proactive")}
    )
    with pytest.raises(PersonaGenomeConflict) as error:
        await store.persona_commands.edit(companion_id="companion-e2e", request=stale)
    assert error.value.code == "preferences_changed"
    assert await store.persona_commands.read_edit_snapshot("companion-e2e") == first


async def test_creation_persists_preset_and_preferences_together(settings_stack):
    from uuid import uuid4

    from eidolon_data.services.persona_presets import load_persona_presets

    store, _, _ = settings_stack
    preset = next(p for p in load_persona_presets().presets if p.preset_id == "direct")
    preferences = ConversationPreferences(response_length="balanced")
    request = dict(
        owner_id="owner-e2e",
        operation_id=str(uuid4()),
        request_fingerprint="sha256:" + "a" * 64,
        companion_display_name="New Companion",
        persona=preset.persona,
        preferences=preferences,
    )
    created = await store.companion_workspaces.provision_companion(**request)
    replayed = await store.companion_workspaces.provision_companion(**request)
    assert replayed.replayed
    assert created.companion.companion_id == replayed.companion.companion_id
    snapshot = await store.persona_commands.read_edit_snapshot(created.companion.companion_id)
    assert snapshot.preferences == preferences
    assert snapshot.persona.character_portrait == preset.persona.character_portrait


@pytest.mark.parametrize("memory_state", ["none", "empty", "kg_only", "hits"])
async def test_voice_policy_does_not_depend_on_memory_hits(settings_stack, memory_state):
    from eidolon_agent.core.types.memory import MemoryRecallResult
    from eidolon_agent.domain.context.tests.functional.test_compiler import _StubMemory
    from tests.e2e.test_persona_memory_e2e import _MemoryPort

    class KGOnlyMemory:
        async def recall_context(self, **kwargs):
            return MemoryRecallResult(
                context="知识图谱事实：测试事实", hits=[], kg_triples=[{"id": "test-kg"}]
            )

    _, workspace, service = settings_stack
    memory = {
        "none": None,
        "empty": _StubMemory(),
        "kg_only": KGOnlyMemory(),
        "hits": _MemoryPort("realm-e2e"),
    }[memory_state]
    compiler = ContextCompiler(
        personas_service=service,
        instance_locator=lambda *_: ("companion-e2e", "origin"),
        history_manager=HistoryManager(),
        memory_port=memory,
    )
    turn = replace(_turn(workspace), input_modality="voice")
    messages = await compiler.compile(turn)
    assert "VOICE_ONLY" in messages[0].content
    assert "TEXT_ONLY" not in messages[0].content
    assert "1–3" in messages[0].content
