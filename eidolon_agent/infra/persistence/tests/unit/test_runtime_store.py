from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from eidolon_agent.core.types.long_task import LongTaskRecord, LongTaskStatus
from eidolon_agent.core.types.turn import TriageKind, TurnInput, TurnStatus, TurnTrigger
from eidolon_agent.core.types.turn_context import TurnContext
from eidolon_agent.infra.persistence.agent_runtime import (
    AgentLongTaskStore,
    build_agent_history_hydrator,
    build_agent_turn_persister,
)
from eidolon_agent.infra.persistence.audit_dispatch import AgentAuditDispatcher
from eidolon_agent.infra.persistence.memory_turn_dispatch import MemoryTurnDispatcher
from eidolon_agent.infra.persistence.runtime_store import AgentRuntimeStore


async def _store(tmp_path) -> AgentRuntimeStore:
    store = AgentRuntimeStore.open(tmp_path / "eidolon-agent.sqlite3")
    await store.init_schema()
    return store


def _turn_input() -> TurnInput:
    return TurnInput(
        turn_id="turn-1",
        conversation_id="conversation-1",
        session_id="session-1",
        context=TurnContext(
            owner_id="owner-1",
            companion_id="companion-1",
            device_id="device-1",
            memory_realm_id="realm-1",
            genome_id="genome-1",
            trace_id="trace-1",
            request_id="request-1",
        ),
        input_modality="text",
        trigger=TurnTrigger.USER_UTTERANCE,
        text="hello",
    )


async def test_runtime_store_contains_only_agent_authority_tables(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        async with store.session_factory() as session:
            names = set(
                await session.scalars(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    )
                )
            )
            journal_mode = await session.scalar(text("PRAGMA journal_mode"))
            synchronous = await session.scalar(text("PRAGMA synchronous"))
            foreign_keys = await session.scalar(text("PRAGMA foreign_keys"))
            schema_version = await session.scalar(text("PRAGMA user_version"))

        assert names == {
            "audit_outbox",
            "conversations",
            "jobs",
            "memory_turn_outbox",
            "messages",
            "runtime_sessions",
            "turns",
        }
        assert journal_mode == "wal"
        assert synchronous == 2  # FULL
        assert foreign_keys == 1
        assert schema_version == 2
    finally:
        await store.close()


async def test_memory_turn_outbox_survives_process_restart(tmp_path) -> None:
    path = tmp_path / "eidolon-agent.sqlite3"
    store = AgentRuntimeStore.open(path)
    await store.init_schema()
    await store.memory_turn_outbox.enqueue(
        turn_id="turn-memory-1",
        owner_id="owner-1",
        companion_id="companion-1",
        subject="eidolon.memory.turn.realm",
        payload={"schema_version": 1, "payload": {"turn_id": "turn-memory-1"}},
        trace_id="trace-memory-1",
    )
    await store.close()

    reopened = AgentRuntimeStore.open(path)
    await reopened.init_schema()

    class _Bus:
        def __init__(self) -> None:
            self.events = []

        async def publish(self, event, *, persistent=False):
            assert persistent is True
            self.events.append(event)

    bus = _Bus()
    try:
        dispatcher = MemoryTurnDispatcher(reopened, bus)
        assert await dispatcher.dispatch_once() == 1
        assert [event.payload["payload"]["turn_id"] for event in bus.events] == ["turn-memory-1"]
        assert await reopened.memory_turn_outbox.pending_count() == 0
    finally:
        await reopened.close()


async def test_memory_turn_outbox_rejects_turn_identity_collision(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        await store.memory_turn_outbox.enqueue(
            turn_id="turn-memory-1",
            owner_id="owner-1",
            companion_id="companion-1",
            subject="eidolon.memory.turn.realm",
            payload={"value": 1},
            trace_id="trace-memory-1",
        )
        await store.memory_turn_outbox.enqueue(
            turn_id="turn-memory-1",
            owner_id="owner-1",
            companion_id="companion-1",
            subject="eidolon.memory.turn.realm",
            payload={"value": 1},
            trace_id="trace-memory-1",
        )

        with pytest.raises(RuntimeError, match="identity collision"):
            await store.memory_turn_outbox.enqueue(
                turn_id="turn-memory-1",
                owner_id="owner-1",
                companion_id="companion-1",
                subject="eidolon.memory.turn.realm",
                payload={"value": 2},
                trace_id="trace-memory-1",
            )
        assert await store.memory_turn_outbox.pending_count() == 1
    finally:
        await store.close()


async def test_runtime_schema_v1_migrates_memory_outbox_without_rebuilding(tmp_path) -> None:
    path = tmp_path / "eidolon-agent.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=1")
    connection.close()

    store = AgentRuntimeStore.open(path)
    try:
        await store.init_schema()
        async with store.session_factory() as session:
            version = await session.scalar(text("PRAGMA user_version"))
            table = await session.scalar(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='memory_turn_outbox'"
                )
            )
        assert version == 2
        assert table == "memory_turn_outbox"
    finally:
        await store.close()


async def test_memory_turn_outbox_concurrent_enqueue_is_idempotent(tmp_path) -> None:
    store = await _store(tmp_path)
    try:

        async def _enqueue() -> None:
            await store.memory_turn_outbox.enqueue(
                turn_id="turn-concurrent",
                owner_id="owner-1",
                companion_id="companion-1",
                subject="eidolon.memory.turn.realm-1",
                payload={"value": 1},
                trace_id="trace-concurrent",
            )

        await asyncio.gather(*(_enqueue() for _ in range(20)))
        assert await store.memory_turn_outbox.pending_count() == 1
    finally:
        await store.close()


async def test_memory_turn_dispatch_preserves_realm_order_and_isolates_failures(
    tmp_path,
) -> None:
    store = await _store(tmp_path)
    try:
        for turn_id, subject in (
            ("realm-a-1", "eidolon.memory.turn.realm-a"),
            ("realm-a-2", "eidolon.memory.turn.realm-a"),
            ("realm-b-1", "eidolon.memory.turn.realm-b"),
        ):
            await store.memory_turn_outbox.enqueue(
                turn_id=turn_id,
                owner_id=f"owner-{subject[-1]}",
                companion_id=f"companion-{subject[-1]}",
                subject=subject,
                payload={"turn_id": turn_id},
                trace_id=f"trace-{turn_id}",
            )

        class _Bus:
            def __init__(self) -> None:
                self.attempts: list[str] = []
                self.published: list[str] = []

            async def publish(self, event, *, persistent=False):
                assert persistent is True
                turn_id = event.payload["turn_id"]
                self.attempts.append(turn_id)
                if turn_id == "realm-a-1":
                    raise ConnectionError("NATS unavailable for this publish")
                self.published.append(turn_id)

        bus = _Bus()
        dispatcher = MemoryTurnDispatcher(store, bus)
        assert await dispatcher.dispatch_once() == 1
        assert bus.attempts == ["realm-a-1", "realm-b-1"]
        assert bus.published == ["realm-b-1"]
        assert await store.memory_turn_outbox.pending_count() == 2

        # The failed head is in backoff, so a new dispatcher iteration must not
        # let the later correction overtake it.
        assert await dispatcher.dispatch_once() == 0
        assert bus.attempts == ["realm-a-1", "realm-b-1"]
    finally:
        await store.close()


async def test_owner_runtime_delete_removes_pending_memory_turns(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        for turn_id, owner_id in (("turn-delete", "owner-delete"), ("turn-keep", "owner-keep")):
            await store.memory_turn_outbox.enqueue(
                turn_id=turn_id,
                owner_id=owner_id,
                companion_id="companion-1",
                subject=f"eidolon.memory.turn.{owner_id}",
                payload={"turn_id": turn_id},
                trace_id=f"trace-{turn_id}",
            )

        counts = await store.delete_owner_runtime("owner-delete")
        assert counts["memory_turn_outbox"] == 1
        pending = await store.memory_turn_outbox.pending_batch()
        assert [row.turn_id for row in pending] == ["turn-keep"]
    finally:
        await store.close()


async def test_turn_history_persists_without_opening_system_data(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        persist = build_agent_turn_persister(store, model_id_provider=lambda: "fake")
        now = datetime.now(UTC)
        await persist(
            ti=_turn_input(),
            status=TurnStatus.OK,
            triage_kind=TriageKind.SIMPLE,
            started_at=now,
            finished_at=now,
            first_delta_ms=10,
            total_ms=20,
            usage_in=3,
            usage_out=4,
            error_code=None,
            timings={},
            user_text="hello",
            assistant_text="hi",
        )

        messages = await build_agent_history_hydrator(store)(
            conversation_id="conversation-1",
            window=10,
        )
        assert [message.content for message in messages] == ["hello", "hi"]
        assert not (tmp_path / "eidolon-system.sqlite3").exists()
    finally:
        await store.close()


async def test_only_terminal_job_receipt_is_published_from_local_outbox(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        jobs = AgentLongTaskStore(store)
        await jobs.accept(
            LongTaskRecord(
                id="job-1",
                provider="mementos",
                status=LongTaskStatus.ACCEPTED,
                owner_id="owner-1",
                companion_id="companion-1",
                conversation_id="conversation-1",
                turn_id="turn-1",
                session_id="session-1",
                trace_id="trace-1",
                session_key="session-key",
                task_date="2026-08-05",
                task_key="task-key",
                task="write report",
            )
        )
        await jobs.mark_queued("job-1", worker_id="worker-1")
        assert await store.audit_outbox.list_pending() == []
        await jobs.complete("job-1", result_text="done")

        class _Publisher:
            def __init__(self) -> None:
                self.actions: list[str] = []

            async def publish_many(self, events):
                self.actions.extend(event.action for event in events)
                return {event.event_id for event in events}

        publisher = _Publisher()
        dispatcher = AgentAuditDispatcher(store, publisher)
        assert await dispatcher.dispatch_once() == 1
        assert publisher.actions == ["job.succeeded"]
        assert await store.audit_outbox.list_pending() == []
    finally:
        await store.close()
