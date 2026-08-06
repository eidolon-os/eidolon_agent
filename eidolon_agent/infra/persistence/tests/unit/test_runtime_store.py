from __future__ import annotations

from datetime import UTC, datetime

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
            "messages",
            "runtime_sessions",
            "turns",
        }
        assert journal_mode == "wal"
        assert synchronous == 2  # FULL
        assert foreign_keys == 1
        assert schema_version == 1
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
