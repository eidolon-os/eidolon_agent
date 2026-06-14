"""SQLite + chat-messages end-to-end roundtrip.

Per-repo unit tests live in ``../unit/``. This file keeps a single
end-to-end test that exercises the UoW + commit + reopen flow.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.config.settings import SqliteSettings
from eidolon_agent.infra.persistence import create_engine, create_session_factory, ensure_schema
from eidolon_agent.infra.persistence.repositories import SqlLongTaskRepository

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
            ChatMessage(id=uuid.uuid4().hex, role=MessageRole.USER, content="你好",
                       created_at=datetime.now(timezone.utc)),
        )
        await uow.chat_messages.append(
            turn_id,
            ChatMessage(id=uuid.uuid4().hex, role=MessageRole.ASSISTANT, content="嗨",
                       created_at=datetime.now(timezone.utc)),
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
        }.issubset(columns)

        factory = create_session_factory(engine)
        async with factory() as session:
            assert await SqlLongTaskRepository(session).list_for_admin(limit=5) == []
    finally:
        await engine.dispose()
