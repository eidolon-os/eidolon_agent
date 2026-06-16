"""SQLite + chat-messages end-to-end roundtrip.

Per-repo unit tests live in ``../unit/``. This file keeps a single
end-to-end test that exercises the UoW + commit + reopen flow.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from eidolon_agent.config.settings import SqliteSettings
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.domain.personas.types import (
    PersonaEvolutionProposal,
    PersonaObservation,
    PersonaProposalPatch,
)
from eidolon_agent.infra.persistence import create_engine, create_session_factory, ensure_schema
from eidolon_agent.infra.persistence.repositories import SqlLongTaskRepository
from eidolon_agent.infra.persistence.sql_persona_evolution_store import (
    SqlPersonaEvolutionProposalStore,
    SqlPersonaObservationStore,
)

pytestmark = pytest.mark.functional


async def test_chat_message_roundtrip(uow_factory):
    conv_id = uuid.uuid4().hex
    turn_id = uuid.uuid4().hex
    async with uow_factory() as uow:
        await uow.conversations.start(
            conversation_id=conv_id, tenant_id="t", user_id="u", agent_instance_id="i"
        )
        # Need a Turn row before chat_messages (FK)
        from eidolon_agent.core.types.turn import (
            TriageKind,
            TurnResult,
            TurnStatus,
            TurnTrigger,
        )

        await uow.conversations.record_turn(
            TurnResult(
                turn_id=turn_id,
                conversation_id=conv_id,
                status=TurnStatus.OK,
                triage_kind=TriageKind.SIMPLE,
                trigger=TurnTrigger.USER_UTTERANCE,
                seq_count=2,
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
                latency_first_delta_ms=120,
                total_latency_ms=350,
                model="fake:scripted",
                seq_in_conversation=1,
            )
        )
        await uow.chat_messages.append(
            turn_id,
            ChatMessage(
                id=uuid.uuid4().hex,
                role=MessageRole.USER,
                content="你好",
                created_at=datetime.now(timezone.utc),
            ),
        )
        await uow.chat_messages.append(
            turn_id,
            ChatMessage(
                id=uuid.uuid4().hex,
                role=MessageRole.ASSISTANT,
                content="嗨",
                created_at=datetime.now(timezone.utc),
            ),
        )
        await uow.commit()

    async with uow_factory() as uow:
        msgs = await uow.chat_messages.list_for_turn(turn_id)
    assert [m.role for m in msgs] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert [m.content for m in msgs] == ["你好", "嗨"]


async def test_ensure_schema_patches_early_long_tasks_table(tmp_path):
    engine = create_engine(SqliteSettings(path=tmp_path / "agent.sqlite3"))
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    CREATE TABLE long_tasks (
                        id VARCHAR(64) PRIMARY KEY,
                        provider VARCHAR(32) NOT NULL,
                        status VARCHAR(24) NOT NULL,
                        tenant_id VARCHAR(64) NOT NULL,
                        user_id VARCHAR(128) NOT NULL,
                        agent_instance_id VARCHAR(64),
                        conversation_id VARCHAR(64),
                        turn_id VARCHAR(64) NOT NULL,
                        session_id VARCHAR(64),
                        trace_id VARCHAR(64),
                        tool_call_id VARCHAR(128),
                        session_key VARCHAR(192) NOT NULL,
                        task_date VARCHAR(10) NOT NULL,
                        task_key VARCHAR(256) NOT NULL UNIQUE,
                        task TEXT NOT NULL,
                        user_text TEXT,
                        task_type VARCHAR(64) NOT NULL,
                        urgency VARCHAR(32) NOT NULL,
                        expected_output TEXT,
                        context_summary TEXT,
                        attachments JSON,
                        request_payload JSON,
                        mementos_session_id VARCHAR(128),
                        mementos_conversation_id VARCHAR(128),
                        mementos_run_id VARCHAR(128),
                        mementos_latest_seq INTEGER,
                        mementos_workspace_dir TEXT,
                        progress_summary TEXT,
                        progress_events JSON,
                        result_text TEXT,
                        result_payload JSON,
                        artifact_paths JSON,
                        error_code VARCHAR(64),
                        error_message TEXT,
                        error_payload JSON,
                        callback_subject VARCHAR(256),
                        callback_status VARCHAR(24) NOT NULL DEFAULT 'pending',
                        callback_attempts INTEGER NOT NULL DEFAULT 0,
                        callback_last_error TEXT,
                        callback_delivered_at DATETIME,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        started_at DATETIME,
                        submitted_at DATETIME,
                        last_progress_at DATETIME,
                        completed_at DATETIME
                    )
                    """
                )
            )
        await ensure_schema(engine)

        async with engine.begin() as conn:
            columns = {
                str(row[1])
                for row in (await conn.execute(text("PRAGMA table_info(long_tasks)"))).fetchall()
            }
        assert {
            "worker_id",
            "lease_until",
            "attempt_count",
            "next_retry_at",
            "external_status",
            "last_polled_at",
            "result_tts_summary",
        }.issubset(columns)

        factory = create_session_factory(engine)
        async with factory() as session:
            assert await SqlLongTaskRepository(session).list_for_admin(limit=5) == []
    finally:
        await engine.dispose()


async def test_persona_observations_and_proposals_roundtrip(tmp_path):
    engine = create_engine(SqliteSettings(path=tmp_path / "agent.sqlite3"))
    try:
        await ensure_schema(engine)
        factory = create_session_factory(engine)
        observations = SqlPersonaObservationStore(factory)
        proposals = SqlPersonaEvolutionProposalStore(factory)

        observation = PersonaObservation(
            id="obs-1",
            tenant_id="t",
            user_id="u",
            instance_id="i",
            kind="positive_feedback_received",
            source="test",
            strength=0.8,
            confidence=0.9,
            summary="user liked the current tone",
            evidence={"turn_id": "turn-1"},
            memory_ids=("m1",),
        )
        await observations.add(observation)
        listed_obs = await observations.list_for_instance("i", status="active")
        assert len(listed_obs) == 1
        assert listed_obs[0].id == observation.id
        assert listed_obs[0].evidence == {"turn_id": "turn-1"}
        assert listed_obs[0].memory_ids == ("m1",)

        await observations.set_status("obs-1", "converted")
        assert await observations.list_for_instance("i", status="active") == []
        converted = await observations.get("obs-1")
        assert converted is not None
        assert converted.status == "converted"

        proposal = PersonaEvolutionProposal(
            id="proposal-1",
            tenant_id="t",
            user_id="u",
            instance_id="i",
            patches=(
                PersonaProposalPatch(
                    type="knob_delta",
                    target="behavioral_knobs.intimacy",
                    delta=0.03,
                    rationale="positive feedback",
                ),
            ),
            confidence=0.9,
            rationale="raise intimacy slightly",
            evidence_ids=("obs-1",),
        )
        await proposals.add(proposal)
        listed_props = await proposals.list_for_instance("i", status="pending")
        assert len(listed_props) == 1
        assert listed_props[0].id == proposal.id
        assert listed_props[0].patches == proposal.patches
        assert listed_props[0].evidence_ids == ("obs-1",)

        applied = proposal.model_copy(update={"status": "applied"})
        await proposals.save(applied)
        assert await proposals.list_for_instance("i", status="pending") == []
        saved = await proposals.get("proposal-1")
        assert saved is not None
        assert saved.status == "applied"
    finally:
        await engine.dispose()
