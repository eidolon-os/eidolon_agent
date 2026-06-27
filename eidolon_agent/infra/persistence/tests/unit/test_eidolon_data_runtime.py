from __future__ import annotations

from datetime import datetime, timezone

import pytest
from eidolon_data import DataSettings, DataStore

from eidolon_agent.core.types.identity import CallerContext, CallerKind, Identity
from eidolon_agent.core.types.long_task import LongTaskRecord, LongTaskStatus
from eidolon_agent.core.types.messages import MessageRole
from eidolon_agent.core.types.turn import TriageKind, TurnInput, TurnStatus, TurnTrigger
from eidolon_agent.infra.persistence.eidolon_data_runtime import (
    EidolonDataConversationReader,
    EidolonDataLongTaskStore,
    build_eidolon_data_history_hydrator,
    build_eidolon_data_turn_persister,
)


@pytest.fixture
async def data_store(tmp_path):
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    await store.init_schema()
    try:
        yield store
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_turn_persister_writes_eidolon_data_history(data_store: DataStore) -> None:
    now = datetime.now(timezone.utc)
    ti = TurnInput(
        turn_id="turn-1",
        conversation_id="conversation-1",
        session_id="session-1",
        caller=CallerContext(
            identity=Identity(
                tenant_id="tenant-1",
                user_id="user-1",
                agent_instance_id="companion-1",
                device_id="device-1",
            ),
            caller_kind=CallerKind.WEB_CHAT,
            trace_id="trace-1",
            request_id="request-1",
        ),
        trigger=TurnTrigger.USER_UTTERANCE,
        text="hello",
    )
    persist = build_eidolon_data_turn_persister(data_store, model_id_provider=lambda: "fake")

    await persist(
        ti=ti,
        status=TurnStatus.OK,
        triage_kind=TriageKind.SIMPLE,
        started_at=now,
        finished_at=now,
        first_delta_ms=12,
        total_ms=34,
        usage_in=5,
        usage_out=6,
        error_code=None,
        timings={"turn_trace": {"memory_write_trace": {"disposition": "skip"}}},
        user_text="hello",
        assistant_text="hi",
    )

    messages = await build_eidolon_data_history_hydrator(data_store)(
        conversation_id="conversation-1",
        window=10,
    )
    assert [message.role for message in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert [message.content for message in messages] == ["hello", "hi"]

    rows = await EidolonDataConversationReader(data_store).list_turns_by_user(user_id="user-1")
    assert rows[0]["tenant_id"] == "tenant-1"
    assert rows[0]["agent_instance_id"] == "companion-1"
    assert rows[0]["tokens_out"] == 6
    assert rows[0]["metadata_"]["turn_trace"]["memory_write_trace"]["disposition"] == "skip"


@pytest.mark.asyncio
async def test_long_task_store_maps_records_to_jobs(data_store: DataStore) -> None:
    store = EidolonDataLongTaskStore(data_store)
    record = LongTaskRecord(
        id="task-1",
        provider="mementos",
        status=LongTaskStatus.ACCEPTED,
        tenant_id="tenant-1",
        user_id="user-1",
        agent_instance_id="companion-1",
        device_id="device-1",
        conversation_id="conversation-1",
        turn_id="turn-1",
        session_id="session-1",
        trace_id="trace-1",
        session_key="session-key",
        task_date="2026-06-27",
        task_key="task-key",
        task="write report",
        task_type="writing",
    )

    await store.accept(record)
    queued = await store.mark_queued("task-1", worker_id="worker-1")
    assert queued is not None
    assert queued.status is LongTaskStatus.QUEUED
    assert queued.worker_id == "worker-1"

    running = await store.attach_mementos_run("task-1", mementos_session_id="mem-session")
    assert running is not None
    assert running.mementos_session_id == "mem-session"
    assert await store.find_mementos_session_id("session-key") == "mem-session"

    assert await store.claim_callback_delivery("task-1", subject="subject-1") is True
    assert await store.claim_callback_delivery("task-1", subject="subject-1") is False

    completed = await store.complete("task-1", result_text="done")
    assert completed is not None
    assert completed.status is LongTaskStatus.SUCCEEDED
    assert completed.result_text == "done"

    rows = await store.list_for_admin(user_id="user-1", task_type="writing")
    assert [row.id for row in rows] == ["task-1"]
