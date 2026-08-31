from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from eidolon_agent.core.types.long_task import LongTaskRecord, LongTaskStatus
from eidolon_agent.core.types.messages import MessageRole
from eidolon_agent.core.types.turn import TriageKind, TurnInput, TurnStatus, TurnTrigger
from eidolon_agent.core.types.turn_context import TurnContext
from eidolon_agent.infra.persistence.agent_runtime import (
    AgentConversationReader,
    AgentLongTaskStore,
    build_agent_history_hydrator,
    build_agent_turn_persister,
)
from eidolon_agent.infra.persistence.runtime_store import (
    AgentRuntimeStore,
    RuntimeSessionRow,
    TurnRow,
)


@pytest.fixture
async def runtime_store(tmp_path):
    store = AgentRuntimeStore.open(tmp_path / "eidolon-agent.sqlite3")
    await store.init_schema()
    try:
        yield store
    finally:
        await store.close()


async def _provision_runtime_identity(
    store: AgentRuntimeStore,
    *,
    owner_id: str,
    companion_id: str,
    device_id: str,
    genome_id: str = "genome-1",
    realm_id: str = "realm-1",
) -> None:
    # Authority references arrive in a verified TurnContext. Agent persistence
    # intentionally performs no system-Data lookup or cross-database FK check.
    del store, owner_id, companion_id, device_id, genome_id, realm_id


def _context(
    *,
    owner_id: str,
    companion_id: str,
    device_id: str | None,
    realm_id: str,
    genome_id: str,
    trace_id: str = "trace-1",
    request_id: str = "request-1",
    runtime_session_id: str | None = None,
) -> TurnContext:
    del runtime_session_id
    return TurnContext(
        owner_id=owner_id,
        companion_id=companion_id,
        device_id=device_id,
        memory_realm_id=realm_id,
        genome_id=genome_id,
        trace_id=trace_id,
        request_id=request_id,
    )


@pytest.mark.asyncio
async def test_turn_persister_writes_agent_runtime_history(
    runtime_store: AgentRuntimeStore,
) -> None:
    await _provision_runtime_identity(
        runtime_store,
        owner_id="owner-1",
        companion_id="companion-1",
        device_id="device-1",
        genome_id="genome-1",
        realm_id="realm-1",
    )
    now = datetime.now(UTC)
    ti = TurnInput(
        turn_id="turn-1",
        conversation_id="conversation-1",
        session_id="session-1",
        context=_context(
            owner_id="owner-1",
            companion_id="companion-1",
            device_id="device-1",
            realm_id="realm-1",
            genome_id="genome-1",
        ),
        input_modality="text",
        trigger=TurnTrigger.USER_UTTERANCE,
        text="hello",
    )
    persist = build_agent_turn_persister(runtime_store, model_id_provider=lambda: "fake")

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
        timings={"turn_trace": {"memory_write_trace": {"ingest_policy": "semantic_steward"}}},
        user_text="hello",
        assistant_text="hi",
    )

    messages = await build_agent_history_hydrator(runtime_store)(
        conversation_id="conversation-1",
        window=10,
    )
    assert [message.role for message in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert [message.content for message in messages] == ["hello", "hi"]

    rows = await AgentConversationReader(runtime_store).list_turns_by_owner(owner_id="owner-1")
    assert rows[0]["owner_id"] == "owner-1"
    assert rows[0]["companion_id"] == "companion-1"
    assert rows[0]["runtime_session_id"] == "session-1"
    assert rows[0]["tokens_out"] == 6
    # trace_id lands in the first-class indexed column (not just trace_json).
    assert rows[0]["trace_id"] == "trace-1"
    async with runtime_store.session_factory() as session:
        turn_row = await session.get(TurnRow, "turn-1")
    assert turn_row is not None and turn_row.trace_id == "trace-1"
    assert (
        rows[0]["metadata_"]["turn_trace"]["memory_write_trace"]["ingest_policy"]
        == "semantic_steward"
    )
    async with runtime_store.session_factory() as session:
        assert await session.get(RuntimeSessionRow, "session-1") is not None


@pytest.mark.asyncio
async def test_turn_failure_stays_in_runtime_history_not_global_audit(
    runtime_store: AgentRuntimeStore,
) -> None:
    """Operational turn errors are queryable locally without audit amplification."""
    await _provision_runtime_identity(
        runtime_store,
        owner_id="owner-e",
        companion_id="companion-e",
        device_id="device-e",
        genome_id="genome-e",
        realm_id="realm-e",
    )
    now = datetime.now(UTC)

    def _ti(turn_id: str, text: str) -> TurnInput:
        return TurnInput(
            turn_id=turn_id,
            conversation_id="conv-e",
            session_id="sess-e",
            context=_context(
                owner_id="owner-e",
                companion_id="companion-e",
                device_id="device-e",
                realm_id="realm-e",
                genome_id="genome-e",
            ),
            input_modality="text",
            trigger=TurnTrigger.USER_UTTERANCE,
            text=text,
        )

    persist = build_agent_turn_persister(runtime_store, model_id_provider=lambda: "fake")

    await persist(
        ti=_ti("turn-err", "boom?"),
        status=TurnStatus.ERRORED,
        triage_kind=TriageKind.SIMPLE,
        started_at=now,
        finished_at=now,
        first_delta_ms=None,
        total_ms=42,
        usage_in=0,
        usage_out=0,
        error_code="llm_timeout",
        timings={},
        user_text="boom?",
        assistant_text="",
    )
    async with runtime_store.session_factory() as session:
        failed_turn = await session.get(TurnRow, "turn-err")
    assert failed_turn is not None
    assert failed_turn.status == TurnStatus.ERRORED.value
    assert failed_turn.metrics_json["error_code"] == "llm_timeout"
    pending = await runtime_store.audit_outbox.list_pending()
    assert not any(event.subject_id == "turn-err" for event in pending)

    # a normal (OK) turn must NOT emit agent.turn.failed
    await persist(
        ti=_ti("turn-ok", "hi"),
        status=TurnStatus.OK,
        triage_kind=TriageKind.SIMPLE,
        started_at=now,
        finished_at=now,
        first_delta_ms=1,
        total_ms=2,
        usage_in=1,
        usage_out=1,
        error_code=None,
        timings={},
        user_text="hi",
        assistant_text="ok",
    )
    pending = await runtime_store.audit_outbox.list_pending()
    assert not any(event.subject_id == "turn-ok" for event in pending)


@pytest.mark.asyncio
async def test_turn_persister_records_admin_test_without_device_row(
    runtime_store: AgentRuntimeStore,
) -> None:
    now = datetime.now(UTC)
    ti = TurnInput(
        turn_id="turn-admin",
        conversation_id="conversation-admin",
        session_id="session-admin",
        context=_context(
            owner_id="owner-admin",
            companion_id="companion-admin",
            device_id=None,
            realm_id="realm-admin",
            genome_id="genome-admin",
            trace_id="trace-admin",
            request_id="request-admin",
        ),
        input_modality="text",
        trigger=TurnTrigger.USER_UTTERANCE,
        text="hello from admin",
    )
    persist = build_agent_turn_persister(runtime_store, model_id_provider=lambda: "fake")

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

    rows = await AgentConversationReader(runtime_store).list_turns_by_owner(owner_id="owner-admin")
    assert rows[0]["device_id"] is None
    assert rows[0]["runtime_session_id"] == "session-admin"


@pytest.mark.asyncio
async def test_turn_persister_is_idempotent_under_concurrent_first_writes(
    runtime_store: AgentRuntimeStore,
) -> None:
    await _provision_runtime_identity(
        runtime_store,
        owner_id="owner-race",
        companion_id="companion-race",
        device_id="device-race",
        genome_id="genome-race",
        realm_id="realm-race",
    )
    persist = build_agent_turn_persister(runtime_store, model_id_provider=lambda: "fake")
    now = datetime.now(UTC)

    async def _write(index: int) -> None:
        ti = TurnInput(
            turn_id=f"turn-{index}",
            conversation_id="conversation-race",
            session_id="session-1",
            context=_context(
                owner_id="owner-race",
                companion_id="companion-race",
                device_id="device-race",
                realm_id="realm-race",
                genome_id="genome-race",
                trace_id=f"trace-{index}",
                request_id=f"request-{index}",
                runtime_session_id="session-1",
            ),
            input_modality="text",
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

    rows = await AgentConversationReader(runtime_store).list_turns_by_owner(owner_id="owner-race")
    assert len(rows) == 12
    assert sorted(row["seq"] for row in rows) == list(range(12))


@pytest.mark.asyncio
async def test_conversation_reader_does_not_wait_for_the_runtime_writer_pool(
    runtime_store: AgentRuntimeStore,
) -> None:
    """History browsing remains available while live runtime owns its writer."""

    # Occupying the only writer connection models a live turn without coupling
    # this regression to the much larger turn pipeline.  Before the dedicated
    # query-only pool, the reader queued here until the Admin's timeout expired.
    async with runtime_store.engine.connect():
        rows = await asyncio.wait_for(
            AgentConversationReader(runtime_store).list_conversations(
                owner_id="owner-1",
            ),
            timeout=0.5,
        )

    assert rows == []


@pytest.mark.asyncio
async def test_long_task_store_maps_records_to_jobs(
    runtime_store: AgentRuntimeStore,
) -> None:
    await _provision_runtime_identity(
        runtime_store,
        owner_id="owner-1",
        companion_id="companion-1",
        device_id="device-1",
        genome_id="genome-1",
        realm_id="realm-1",
    )
    store = AgentLongTaskStore(runtime_store)
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


async def test_long_task_transitions_emit_job_lifecycle_events(
    runtime_store: AgentRuntimeStore,
) -> None:
    """Only a terminal job outcome enters global audit; progress remains local."""
    await _provision_runtime_identity(
        runtime_store,
        owner_id="owner-j",
        companion_id="companion-j",
        device_id="device-j",
        genome_id="genome-j",
        realm_id="realm-j",
    )
    store = AgentLongTaskStore(runtime_store)
    record = LongTaskRecord(
        id="job-lc",
        provider="mementos",
        status=LongTaskStatus.ACCEPTED,
        owner_id="owner-j",
        companion_id="companion-j",
        memory_realm_id="realm-j",
        genome_id="genome-j",
        device_id="device-j",
        conversation_id="conv-j",
        turn_id="turn-j",
        session_id="sess-j",
        trace_id="trace-j",
        session_key="sk",
        task_date="2026-06-27",
        task_key="tk",
        task="write report",
        task_type="writing",
    )

    await store.accept(record)
    await store.mark_queued("job-lc", worker_id="w1")
    await store.attach_mementos_run("job-lc", mementos_session_id="m1")
    await store.append_progress("job-lc", {"seq": 1})
    await store.complete("job-lc", result_text="done")

    receipts = [
        event
        for event in await runtime_store.audit_outbox.list_pending()
        if event.subject_type == "job" and event.subject_id == "job-lc"
    ]
    assert [event.action for event in receipts] == ["job.succeeded"]
    assert receipts[0].producer == "eidolon-agent"
    assert receipts[0].category == "receipt"
    assert receipts[0].trace_id == "trace-j"
    assert receipts[0].outcome == "success"


async def test_long_task_failure_emits_job_failed(
    runtime_store: AgentRuntimeStore,
) -> None:
    await _provision_runtime_identity(
        runtime_store,
        owner_id="owner-f",
        companion_id="companion-f",
        device_id="device-f",
        genome_id="genome-f",
        realm_id="realm-f",
    )
    store = AgentLongTaskStore(runtime_store)
    record = LongTaskRecord(
        id="job-f",
        provider="mementos",
        status=LongTaskStatus.ACCEPTED,
        owner_id="owner-f",
        companion_id="companion-f",
        memory_realm_id="realm-f",
        genome_id="genome-f",
        device_id="device-f",
        conversation_id="conv-f",
        turn_id="turn-f",
        session_id="sess-f",
        trace_id="trace-f",
        session_key="sk",
        task_date="2026-06-27",
        task_key="tk",
        task="write report",
        task_type="writing",
    )

    await store.accept(record)
    await store.mark_failed("job-f", error_code="boom", error_message="kaboom")

    receipts = [
        event
        for event in await runtime_store.audit_outbox.list_pending()
        if event.subject_type == "job" and event.subject_id == "job-f"
    ]
    assert [event.action for event in receipts] == ["job.failed"]
    assert receipts[0].outcome == "failure"
    assert receipts[0].reason == "boom"
    assert receipts[0].payload.get("error_code") == "boom"
