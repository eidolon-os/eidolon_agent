"""Mementos long-task worker behavior."""

from __future__ import annotations

import asyncio

import pytest

from eidolon_agent.config.settings import SqliteSettings
from eidolon_agent.core.types.long_task import (
    LongTaskRecord,
    LongTaskStatus,
    session_key_for,
    task_key_for,
)
from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.long_task import CallbackStatus
from eidolon_agent.infra.events.adapters.inmem import InMemoryEventBus
from eidolon_agent.infra.long_tasks.mementos import (
    MementosLongTaskWorker,
    MementosWorkerConfig,
)
from eidolon_agent.infra.persistence import (
    SqlAlchemyUnitOfWork,
    SqlLongTaskStore,
    create_engine,
    create_session_factory,
    ensure_schema,
)

pytestmark = pytest.mark.asyncio


async def test_worker_keeps_tool_path_to_accepted_then_completes(tmp_path) -> None:
    engine = create_engine(SqliteSettings(path=tmp_path / "agent.sqlite3"))
    await ensure_schema(engine)
    session_factory = create_session_factory(engine)
    store = SqlLongTaskStore(session_factory)
    client = _FakeMementosClient()
    worker = MementosLongTaskWorker(
        store=store,
        client=client,
        config=MementosWorkerConfig(poll_interval_s=0.01, task_timeout_s=5),
        result_summarizer=_FakeResultSummarizer(),
        worker_id="worker-test",
    )
    record = _record("task-1")

    await worker.submit(record)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        accepted = await uow.long_tasks.get("task-1")

    assert accepted is not None
    assert accepted.status is LongTaskStatus.ACCEPTED
    assert accepted.mementos_session_id is None

    drained = await worker.drain_once()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        completed = await uow.long_tasks.get("task-1")
    await client.close()
    await engine.dispose()

    assert drained is True
    assert completed is not None
    assert completed.status is LongTaskStatus.SUCCEEDED
    assert completed.worker_id == "worker-test"
    assert completed.attempt_count == 1
    assert completed.mementos_session_id == "m-session-1"
    assert completed.mementos_conversation_id == "m-conv-1"
    assert completed.result_text == "mementos coworker 已收到 eidolon_agent 的测试任务。"
    assert completed.result_tts_summary == "测试任务已完成，Mementos 已确认收到。"
    assert client.prompts == [
        "测试任务\n期望输出：确认收到\n上下文摘要：端到端测试"
    ]


async def test_worker_publishes_proactive_report_on_success(tmp_path) -> None:
    engine = create_engine(SqliteSettings(path=tmp_path / "agent.sqlite3"))
    await ensure_schema(engine)
    session_factory = create_session_factory(engine)
    store = SqlLongTaskStore(session_factory)
    client = _FakeMementosClient()
    bus = InMemoryEventBus()
    received: list[Event] = []

    async def _handler(event: Event) -> None:
        received.append(event)

    await bus.subscribe("agent.proactive.triggered.>", _handler)

    worker = MementosLongTaskWorker(
        store=store,
        client=client,
        config=MementosWorkerConfig(poll_interval_s=0.01, task_timeout_s=5),
        result_summarizer=_FakeResultSummarizer(),
        event_bus=bus,
        worker_id="worker-test",
    )

    await worker.submit(_record("task-1", agent_instance_id="inst_abc"))
    await worker.drain_once()
    # InMemoryEventBus delivers handlers on a scheduled task; let them run.
    await asyncio.sleep(0)

    assert len(received) == 1
    event = received[0]
    assert event.subject == "agent.proactive.triggered.inst_abc"
    assert event.payload == {
        "instance_id": "inst_abc",
        # conversation_id "c1" isn't a livekit triple and no device_id on the
        # record → unresolved (None). Resolution covered by
        # test_device_id_resolution.py.
        "device_id": None,
        "intent": "long_task_done",
        "text": "测试任务已完成，Mementos 已确认收到。",
        "style_hint": "report",
    }

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        completed = await uow.long_tasks.get("task-1")
    await client.close()
    await engine.dispose()

    assert completed is not None
    assert completed.callback_status is CallbackStatus.DELIVERED
    assert completed.callback_subject == "agent.proactive.triggered.inst_abc"
    assert completed.callback_attempts == 1
    assert completed.callback_delivered_at is not None


async def test_worker_skips_proactive_report_without_instance_id(tmp_path) -> None:
    engine = create_engine(SqliteSettings(path=tmp_path / "agent.sqlite3"))
    await ensure_schema(engine)
    session_factory = create_session_factory(engine)
    store = SqlLongTaskStore(session_factory)
    client = _FakeMementosClient()
    bus = InMemoryEventBus()
    received: list[Event] = []

    async def _handler(event: Event) -> None:
        received.append(event)

    await bus.subscribe("agent.proactive.triggered.>", _handler)

    worker = MementosLongTaskWorker(
        store=store,
        client=client,
        config=MementosWorkerConfig(poll_interval_s=0.01, task_timeout_s=5),
        result_summarizer=_FakeResultSummarizer(),
        event_bus=bus,
        worker_id="worker-test",
    )

    await worker.submit(_record("task-1"))
    await worker.drain_once()
    await asyncio.sleep(0)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        completed = await uow.long_tasks.get("task-1")
    await client.close()
    await engine.dispose()

    assert received == []
    assert completed is not None
    # No announcement claimed → callback stays pending.
    assert completed.callback_status is CallbackStatus.PENDING


def _record(task_id: str, *, agent_instance_id: str | None = None) -> LongTaskRecord:
    session_key = session_key_for("alice", "2026-06-14")
    return LongTaskRecord(
        id=task_id,
        provider="mementos",
        status=LongTaskStatus.ACCEPTED,
        tenant_id="t",
        user_id="alice",
        conversation_id="c1",
        turn_id="turn-1",
        session_id="s1",
        trace_id="trace-1",
        session_key=session_key,
        task_date="2026-06-14",
        task_key=task_key_for(session_key, task_id),
        task="测试任务",
        agent_instance_id=agent_instance_id,
        expected_output="确认收到",
        context_summary="端到端测试",
    )


class _FakeMementosClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def close(self) -> None:
        return None

    async def create_session(self, *, title: str) -> dict:
        assert title == "e.alice.20260614"
        return {"session": {"id": "m-session-1"}}

    async def post_message(
        self,
        *,
        session_id: str,
        prompt: str,
        file_list: list[str] | None = None,
    ) -> dict:
        assert session_id == "m-session-1"
        assert file_list == []
        self.prompts.append(prompt)
        return {
            "conversation_id": "m-conv-1",
            "latest_seq": 0,
            "session_state": "running",
        }

    async def get_messages(self, *, session_id: str, limit: int = 500) -> dict:
        assert session_id == "m-session-1"
        return {
            "messages": [
                {
                    "event_type": "RUN_END",
                    "status": "finish",
                    "payload": {
                        "content": "mementos coworker 已收到 eidolon_agent 的测试任务。"
                    },
                }
            ]
        }


class _FakeResultSummarizer:
    async def summarize(self, record: LongTaskRecord, result_text: str) -> str:
        assert record.id == "task-1"
        assert result_text == "mementos coworker 已收到 eidolon_agent 的测试任务。"
        return "测试任务已完成，Mementos 已确认收到。"
