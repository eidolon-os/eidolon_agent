"""SQLite repository coverage for long-running coworker tasks."""

from __future__ import annotations

import pytest

from eidolon_agent.core.types.long_task import (
    LongTaskRecord,
    LongTaskStatus,
    session_key_for,
    task_key_for,
)

pytestmark = pytest.mark.asyncio


async def test_long_task_repository_records_lifecycle(uow_factory) -> None:
    session_key = session_key_for("alice", "2026-06-14")
    record = LongTaskRecord(
        id="task-1",
        provider="mementos",
        status=LongTaskStatus.ACCEPTED,
        tenant_id="t",
        user_id="alice",
        agent_instance_id="inst-test",
        conversation_id="c1",
        turn_id="turn-1",
        session_id="s1",
        trace_id="trace-1",
        tool_call_id="call-1",
        session_key=session_key,
        task_date="2026-06-14",
        task_key=task_key_for(session_key, "task-1"),
        task="整理本周会议纪要",
        user_text="帮我整理本周会议纪要",
        task_type="document_work",
        expected_output="摘要和行动项",
        context_summary="用户希望稍后返回整理结果。",
        request_payload={"task_id": "task-1", "session_key": session_key},
        callback_subject="long_task.progress.task-1",
    )

    async with uow_factory() as uow:
        await uow.long_tasks.create(record)
        await uow.commit()

    async with uow_factory() as uow:
        stored = await uow.long_tasks.get("task-1")

    assert stored is not None
    assert stored.status is LongTaskStatus.ACCEPTED
    assert stored.session_key == "e.alice.20260614"
    assert stored.task_key == "e.alice.20260614.task-1"
    assert stored.request_payload["session_key"] == session_key

    async with uow_factory() as uow:
        await uow.long_tasks.mark_worker_claimed(
            "task-1",
            worker_id="worker-1",
        )
        await uow.long_tasks.attach_mementos_run(
            "task-1",
            mementos_session_id=session_key,
            mementos_run_id="run-1",
            latest_seq=3,
            workspace_dir="/tmp/mementos/task-1",
        )
        await uow.long_tasks.append_progress(
            "task-1",
            {"seq": 4, "text": "已完成资料收集"},
            summary="资料收集中",
            latest_seq=4,
        )
        await uow.long_tasks.complete(
            "task-1",
            result_text="整理完成",
            result_payload={"summary": "整理完成"},
            artifact_paths=["/tmp/report.md"],
        )
        await uow.commit()

    async with uow_factory() as uow:
        stored = await uow.long_tasks.get("task-1")
        user_tasks = await uow.long_tasks.list_by_user(
            tenant_id="t",
            user_id="alice",
        )

    assert stored is not None
    assert stored.status is LongTaskStatus.SUCCEEDED
    assert stored.submitted_at is not None
    assert stored.started_at is not None
    assert stored.completed_at is not None
    assert stored.worker_id == "worker-1"
    assert stored.attempt_count == 1
    assert stored.mementos_session_id == session_key
    assert stored.mementos_latest_seq == 4
    assert stored.progress_events == [{"seq": 4, "text": "已完成资料收集"}]
    assert stored.result_payload == {"summary": "整理完成"}
    assert stored.artifact_paths == ["/tmp/report.md"]
    assert [task.id for task in user_tasks] == ["task-1"]


async def test_long_task_repository_marks_failures(uow_factory) -> None:
    session_key = session_key_for("alice", "2026-06-14")
    record = LongTaskRecord(
        id="task-fail",
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
        task_key=task_key_for(session_key, "task-fail"),
        task="失败用例",
    )

    async with uow_factory() as uow:
        await uow.long_tasks.create(record)
        await uow.commit()

    async with uow_factory() as uow:
        failed = await uow.long_tasks.fail(
            "task-fail",
            error_code="worker_error",
            error_message="worker failed",
            error_payload={"retryable": True},
        )
        await uow.commit()

    assert failed is not None
    assert failed.status is LongTaskStatus.FAILED
    assert failed.error_code == "worker_error"
    assert failed.error_payload == {"retryable": True}
    assert failed.completed_at is not None
