from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from eidolon_data import DataSettings, DataStore

from eidolon_agent.core.types.identity import CallerContext, CallerKind, Identity
from eidolon_agent.core.types.long_task import LongTaskRecord, LongTaskStatus
from eidolon_agent.core.types.messages import MessageRole
from eidolon_agent.core.types.turn import TriageKind, TurnInput, TurnStatus, TurnTrigger
from eidolon_agent.domain.history.fanout import MemoryFanoutStatus
from eidolon_agent.infra.persistence.eidolon_data_runtime import (
    EidolonDataConversationReader,
    EidolonDataLongTaskStore,
    EidolonDataMemoryFanoutStatusSink,
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


async def _provision_runtime_identity(
    store: DataStore,
    *,
    owner_id: str,
    companion_id: str,
    device_id: str,
    genome_id: str = "genome-1",
    realm_id: str = "realm-1",
) -> None:
    await store.owner_service.create_owner(owner_id=owner_id, display_name=owner_id)
    await store.workspace_provisioning.provision_workspace(
        owner_id=owner_id,
        companion_id=companion_id,
        genome_id=genome_id,
        realm_id=realm_id,
    )
    await store.devices.create_device(
        device_id=device_id,
        owner_id=owner_id,
        bound_companion_id=companion_id,
        auth_type="token",
        secret_ref="test",
    )


def _caller(
    *,
    owner_id: str,
    companion_id: str,
    device_id: str | None,
    realm_id: str,
    genome_id: str,
    caller_kind: CallerKind = CallerKind.WEB_CHAT,
    trace_id: str = "trace-1",
    request_id: str = "request-1",
    runtime_caller_id: str = "rc-test",
    runtime_session_id: str = "session-1",
    actor_kind: str = "web_chat",
    actor_id: str = "actor-1",
) -> CallerContext:
    return CallerContext(
        identity=Identity(
            owner_id=owner_id,
            companion_id=companion_id,
            device_id=device_id,
            memory_realm_id=realm_id,
            genome_id=genome_id,
        ),
        caller_kind=caller_kind,
        trace_id=trace_id,
        request_id=request_id,
        runtime_caller_id=runtime_caller_id,
        runtime_session_id=runtime_session_id,
        actor_kind=actor_kind,
        actor_id=actor_id,
        display_name=actor_kind,
        transport="test",
    )


@pytest.mark.asyncio
async def test_memory_fanout_status_sink_records_event(data_store: DataStore) -> None:
    await data_store.owner_service.create_owner(
        owner_id="owner-1",
        display_name="Owner 1",
    )
    sink = EidolonDataMemoryFanoutStatusSink(data_store)

    await sink.record_memory_fanout(
        MemoryFanoutStatus(
            turn_id="turn-1",
            owner_id="owner-1",
            companion_id="companion-1",
            memory_realm_id="realm-1",
            memory_space_id="realm-1",
            subject="eidolon.memory.turn.realm-1",
            state="published",
            error=None,
            recorded_at="2026-06-29T00:00:00+00:00",
        )
    )

    events = await data_store.events.list_for_subject(
        subject_type="turn",
        subject_id="turn-1",
    )
    assert len(events) == 1
    assert events[0].event_type == "eidolon.memory.fanout.status"
    assert events[0].payload_json["state"] == "published"
    assert events[0].payload_json["memory_space_id"] == "realm-1"


@pytest.mark.asyncio
async def test_turn_persister_writes_eidolon_data_history(data_store: DataStore) -> None:
    await _provision_runtime_identity(
        data_store,
        owner_id="owner-1",
        companion_id="companion-1",
        device_id="device-1",
        genome_id="genome-1",
        realm_id="realm-1",
    )
    now = datetime.now(timezone.utc)
    ti = TurnInput(
        turn_id="turn-1",
        conversation_id="conversation-1",
        session_id="session-1",
        caller=_caller(
            owner_id="owner-1",
            companion_id="companion-1",
            device_id="device-1",
            realm_id="realm-1",
            genome_id="genome-1",
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

    rows = await EidolonDataConversationReader(data_store).list_turns_by_owner(owner_id="owner-1")
    assert rows[0]["owner_id"] == "owner-1"
    assert rows[0]["companion_id"] == "companion-1"
    assert rows[0]["runtime_caller_id"] == "rc-test"
    assert rows[0]["runtime_session_id"] == "session-1"
    assert rows[0]["tokens_out"] == 6
    assert rows[0]["metadata_"]["turn_trace"]["memory_write_trace"]["disposition"] == "skip"
    assert await data_store.runtime_callers.get("rc-test") is not None
    assert await data_store.runtime_sessions.get("session-1") is not None


@pytest.mark.asyncio
async def test_turn_persister_records_admin_test_without_device_row(
    data_store: DataStore,
) -> None:
    await data_store.owner_service.create_owner(
        owner_id="owner-admin",
        display_name="Owner Admin",
    )
    await data_store.workspace_provisioning.provision_workspace(
        owner_id="owner-admin",
        companion_id="companion-admin",
        genome_id="genome-admin",
        realm_id="realm-admin",
    )
    now = datetime.now(timezone.utc)
    ti = TurnInput(
        turn_id="turn-admin",
        conversation_id="conversation-admin",
        session_id="session-admin",
        caller=_caller(
            owner_id="owner-admin",
            companion_id="companion-admin",
            device_id=None,
            realm_id="realm-admin",
            genome_id="genome-admin",
            caller_kind=CallerKind.ADMIN_TEST,
            trace_id="trace-admin",
            request_id="request-admin",
            runtime_caller_id="rc-admin",
            runtime_session_id="session-admin",
            actor_kind="admin_console",
            actor_id="admin-chat-test",
        ),
        trigger=TurnTrigger.USER_UTTERANCE,
        text="hello from admin",
    )
    persist = build_eidolon_data_turn_persister(data_store, model_id_provider=lambda: "fake")

    await persist(
        ti=ti,
        status=TurnStatus.OK,
        triage_kind=TriageKind.SIMPLE,
        started_at=now,
        finished_at=now,
        first_delta_ms=1,
        total_ms=2,
        usage_in=3,
        usage_out=4,
        error_code=None,
        timings={},
        user_text="hello from admin",
        assistant_text="hi",
    )

    rows = await EidolonDataConversationReader(data_store).list_turns_by_owner(
        owner_id="owner-admin"
    )
    assert rows[0]["device_id"] is None
    assert rows[0]["runtime_caller_id"] == "rc-admin"
    assert rows[0]["runtime_session_id"] == "session-admin"


@pytest.mark.asyncio
async def test_turn_persister_is_idempotent_under_concurrent_first_writes(
    data_store: DataStore,
) -> None:
    await _provision_runtime_identity(
        data_store,
        owner_id="owner-race",
        companion_id="companion-race",
        device_id="device-race",
        genome_id="genome-race",
        realm_id="realm-race",
    )
    persist = build_eidolon_data_turn_persister(data_store, model_id_provider=lambda: "fake")
    now = datetime.now(timezone.utc)

    async def _write(index: int) -> None:
        ti = TurnInput(
            turn_id=f"turn-{index}",
            conversation_id="conversation-race",
            session_id="session-1",
            caller=_caller(
                owner_id="owner-race",
                companion_id="companion-race",
                device_id="device-race",
                realm_id="realm-race",
                genome_id="genome-race",
                trace_id=f"trace-{index}",
                request_id=f"request-{index}",
                runtime_caller_id="rc-race",
                runtime_session_id="session-1",
                actor_id="device-race",
            ),
            trigger=TurnTrigger.USER_UTTERANCE,
            text=f"hello {index}",
        )
        await persist(
            ti=ti,
            status=TurnStatus.OK,
            triage_kind=TriageKind.SIMPLE,
            started_at=now,
            finished_at=now,
            first_delta_ms=None,
            total_ms=1,
            usage_in=1,
            usage_out=1,
            error_code=None,
            timings={},
            user_text=f"hello {index}",
            assistant_text=f"hi {index}",
        )

    await asyncio.gather(*(_write(i) for i in range(12)))

    rows = await EidolonDataConversationReader(data_store).list_turns_by_owner(owner_id="owner-race")
    assert len(rows) == 12
    assert sorted(row["seq"] for row in rows) == list(range(12))


@pytest.mark.asyncio
async def test_long_task_store_maps_records_to_jobs(data_store: DataStore) -> None:
    await _provision_runtime_identity(
        data_store,
        owner_id="owner-1",
        companion_id="companion-1",
        device_id="device-1",
        genome_id="genome-1",
        realm_id="realm-1",
    )
    store = EidolonDataLongTaskStore(data_store)
    record = LongTaskRecord(
        id="task-1",
        provider="mementos",
        status=LongTaskStatus.ACCEPTED,
        owner_id="owner-1",
        companion_id="companion-1",
        memory_realm_id="realm-1",
        genome_id="genome-1",
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

    rows = await store.list_for_admin(owner_id="owner-1", task_type="writing")
    assert [row.id for row in rows] == ["task-1"]
